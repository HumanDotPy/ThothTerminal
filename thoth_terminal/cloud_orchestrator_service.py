from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from thoth_terminal.agent.context_bundle import build_recent_context_bundle, render_recent_context_markdown
from thoth_terminal.agent.llm_client import AgentLlmError, ChatMessage, llm_metadata
from thoth_terminal.agent_request_manifest_service import manifested_chat_json, manifested_responses_tools
from thoth_terminal.cloud_goal_run_service import (
    CLOUD_GOAL_COMPLETE,
    CLOUD_GOAL_FAILED,
    DEFAULT_TOKEN_POLICY,
    accumulate_llm_usage_stats,
    append_cloud_goal_event,
    compact_cloud_goal_context,
    create_cloud_goal_run,
    get_cloud_goal_run,
    list_cloud_goal_events,
    render_cloud_goal_context,
    serialize_cloud_goal_run,
    should_compact_context,
    utcnow,
)
from thoth_terminal.cloud_goal_lineage_service import (
    append_cloud_goal_lineage_acceptance_event,
    create_cloud_goal_lineage,
    serialize_cloud_goal_lineage,
    sync_cloud_goal_lineage,
)
from thoth_terminal.cloud_task_catalog import list_task_capabilities, task_catalog_markdown
from thoth_terminal.cloud_skill_runtime_service import cloud_skill_snapshot, render_cloud_skill_catalog, render_cloud_skill_instructions, resolve_cloud_skills
from thoth_terminal.config import settings
from thoth_terminal.mcp_client import ThothMcpClient
from thoth_terminal.gap_hunter_agent_service import continue_gap_hunter_goal_run, start_gap_hunter_goal_run
from thoth_terminal.local_agent_service import daily_summary_context
from thoth_terminal.red_team_agent_service import continue_red_team_goal_run, start_red_team_goal_run
from thoth_terminal.report_body_builder_agent_service import continue_report_body_builder_goal_run, start_report_body_builder_goal_run
from thoth_terminal.source_pack_agent_service import continue_source_pack_goal_run, start_source_pack_goal_run
from thoth_terminal.bottleneck_constraint_agent_service import continue_bottleneck_constraint_goal_run, start_bottleneck_constraint_goal_run


CLOUD_ORCHESTRATOR_SYSTEM = """Return json only.
You are Thoth's autonomous lead analyst and research orchestrator.

Own the stated goal until it is complete, paused, cleared, or genuinely blocked. Act like an inquisitive senior analyst managing a team: inspect the terminal, ask what is missing, form a plan, delegate bounded work, demand source attribution, challenge weak synthesis, and decide what deserves durable Thoth state. You are the final arbiter of effort and minimum-viable quality for this goal. Do not wait for the operator to enumerate obvious next steps.

Use DeepSeek V4 Flash at max effort for every DeepSeek role. Flash-tier and strong-tier remain workload labels for decomposition and review depth, not different DeepSeek checkpoints. Downstream agents receive completed dependency outputs automatically. Flag genuinely difficult, consequential, or unresolved judgment for operator/Codex review instead of escalating to DeepSeek V4 Pro.

Rules:
- Use the task capability catalog. Do not invent unavailable tools.
- Look sideways across documents, reports, knowledge assets, datasets, supply chains, theses, product stages, instrument requests, and recent agent state before deciding that coverage is sufficient.
- Prefer bounded Flash-tier tasks when they are enough.
- Use strong-tier workflow gates for final report writing, serious red team, bottleneck reports, or high-conviction thesis synthesis, while retaining DeepSeek V4 Flash as the model.
- Surface difficult or consequential decisions for operator/Codex review; do not route them to DeepSeek V4 Pro.
- Do not assign tiny/small models to thesis formation, final reports, adversarial judgment, or bottleneck classification.
- Keep steps bounded and inspectable. One step should have one clear objective and one task capability.
- Build complete dependency chains: evidence/source pack -> synthesis or model -> independent challenge -> revision or durable destination when warranted.
- Separate propose-only work from durable writes.
- Best-effort-first is the default. Trusted Substack and specialist sources may support an initial durable version when claims are explicitly attributed. Lack of a second source is a verification gap, not an automatic rejection.
- Revise the active goal when new evidence shows the original objective is too narrow, incorrectly framed, duplicated, or already satisfied. Preserve the revision reason.
- Do not expose local runtime internals, private URLs, operator commands, API keys, or portfolio state.
- If the objective is ambiguous, create a discovery-first plan rather than refusing.

Return exactly one JSON object:
{
  "action": "finalize",
  "rationale": "why this plan is the right next plan",
  "plan_title": "short specific title",
  "plan_summary": "short summary",
  "steps": [
    {
      "step_id": "s1",
      "task_id": "one task_id from the catalog",
      "model_tier": "flash|strong|local|human",
      "objective": "bounded step objective",
      "inputs": {"query": "", "subject": "", "report_id": null},
      "expected_output": "what should come back",
      "allowed_write": "none|cloud_goal|daily_summary|report_addendum|candidate",
      "depends_on": [],
      "skill_slugs": ["optional Thoth-native skills to load for this child"],
      "stop_condition": "how to know this step is done",
      "escalation": "when to use a stronger task or human review"
    }
  ],
  "execution_policy": {
    "max_steps": 0,
    "default_model_tier": "flash",
    "requires_operator_approval_for_writes": true,
    "auto_execute": false
  },
  "goal_update": {"objective": "optional revised durable goal", "reason": "why the goal should change"},
  "risks": [],
  "missing_context": [],
  "confidence": 0.0
}
"""


def _text(value: object) -> str:
    return str(value or "").strip()


def _int(value: object, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _float(value: object, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _list_text(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_text(item) for item in value if _text(item)]


_CLOUD_READ_TOOL_NAMES = (
    "fetch",
    "get_company",
    "get_note",
    "get_source",
    "list_companies",
    "list_notes",
    "list_sources",
    "search_documents",
    "expand_document_match",
)
_CLOUD_PTC_TOOL_NAMES = (
    "fetch",
    "get_company",
    "get_note",
    "get_source",
    "list_companies",
    "list_notes",
    "list_sources",
    "search_documents",
    "expand_document_match",
)


def _context_packet(
    session: Session,
    *,
    objective: str,
    lookback_hours: int,
    max_chars: int | None = None,
    skill_prompt: str | None = None,
) -> str:
    bundle = build_recent_context_bundle(
        session,
        lookback_hours=max(1, min(int(lookback_hours or 168), 720)),
        include_sections=[
            "documents",
            "notes",
            "datasets",
            "charts",
            "supply_chains",
            "instrument_requests",
            "product_stages",
            "transcripts",
            "theses",
            "research_library",
            "agent_actions",
            "change_runs",
            "thesis_proposals",
            "document_events",
            "ingestion_runs",
            "backlog_stats",
        ],
        max_items_per_section=28,
        max_items_per_source=6,
        include_full_text=False,
    )
    recent_md = render_recent_context_markdown(bundle, max_chars=None)
    local_context = daily_summary_context(session, hours=lookback_hours, limit=240)
    local_md = json.dumps(local_context, ensure_ascii=False, indent=2)
    return (
        "# Cloud Orchestrator Input\n\n"
        "## Objective\n"
        f"{objective}\n\n"
        "## Active Analyst Skill\n\n"
        f"{_text(skill_prompt) or 'Use the default Thoth autonomous analyst contract.'}\n\n"
        "## Capability Catalog\n\n"
        f"{task_catalog_markdown()}\n\n"
        f"{render_cloud_skill_catalog(session)}\n\n"
        "## Recent Terminal Context\n\n"
        f"{recent_md}\n\n"
        "## Local Agent Work Context\n\n"
        "```json\n"
        f"{local_md}\n"
        "```\n"
    )


def _normalize_plan(payload: dict[str, Any] | None) -> dict[str, Any]:
    raw = dict(payload or {})
    available_ids = {item["task_id"] for item in list_task_capabilities()}
    steps: list[dict[str, Any]] = []
    for idx, item in enumerate(raw.get("steps") if isinstance(raw.get("steps"), list) else [], start=1):
        if not isinstance(item, dict):
            continue
        task_id = _text(item.get("task_id"))
        if task_id not in available_ids:
            continue
        inputs = item.get("inputs") if isinstance(item.get("inputs"), dict) else {}
        deps = item.get("depends_on") if isinstance(item.get("depends_on"), list) else []
        skill_slugs = item.get("skill_slugs") if isinstance(item.get("skill_slugs"), list) else []
        steps.append(
            {
                "step_id": _text(item.get("step_id")) or f"s{idx}",
                "task_id": task_id,
                "model_tier": _text(item.get("model_tier")) or "flash",
                "objective": _text(item.get("objective")),
                "inputs": dict(inputs),
                "expected_output": _text(item.get("expected_output")),
                "allowed_write": _text(item.get("allowed_write")) or "none",
                "depends_on": [_text(dep) for dep in deps if _text(dep)],
                "skill_slugs": [_text(slug) for slug in skill_slugs if _text(slug)],
                "stop_condition": _text(item.get("stop_condition")),
                "escalation": _text(item.get("escalation")),
            }
        )
    policy = raw.get("execution_policy") if isinstance(raw.get("execution_policy"), dict) else {}
    return {
        "action": "finalize",
        "rationale": _text(raw.get("rationale")),
        "plan_title": _text(raw.get("plan_title")) or "Cloud Research Orchestration Plan",
        "plan_summary": _text(raw.get("plan_summary")),
        "steps": steps[:24],
        "execution_policy": {
            "max_steps": max(1, min(_int(policy.get("max_steps"), len(steps) or 1), 24)),
            "default_model_tier": _text(policy.get("default_model_tier")) or "flash",
            "requires_operator_approval_for_writes": bool(policy.get("requires_operator_approval_for_writes", True)),
            "auto_execute": bool(policy.get("auto_execute", False)),
        },
        "risks": _list_text(raw.get("risks")),
        "missing_context": _list_text(raw.get("missing_context")),
        "confidence": _float(raw.get("confidence"), 0.0),
        "goal_update": dict(raw.get("goal_update") or {}) if isinstance(raw.get("goal_update"), dict) else {},
        "capabilities": list_task_capabilities(),
    }


def start_cloud_orchestrator_run(
    session: Session,
    *,
    objective: str,
    provider: str | None = None,
    model: str | None = None,
    lookback_hours: int = 168,
    token_policy: dict[str, Any] | None = None,
    skill_prompt: str | None = None,
    strong_provider: str | None = None,
    strong_model: str | None = None,
    flash_provider: str | None = None,
    flash_model: str | None = None,
    permission_profile: dict[str, Any] | None = None,
    subagent_prompts: dict[str, str] | None = None,
    tool_route: str = "direct",
    tool_profile: str = "analyst_v1",
) -> dict[str, Any]:
    objective_text = _text(objective)
    if not objective_text:
        raise ValueError("objective is required.")
    route = _text(tool_route).lower() or "direct"
    if route not in {"direct", "parallel_direct", "ptc"}:
        raise ValueError("tool_route must be direct, parallel_direct, or ptc")
    context_md = _context_packet(
        session,
        objective=objective_text,
        lookback_hours=max(1, min(int(lookback_hours or 168), 720)),
        max_chars=None,
        skill_prompt=skill_prompt,
    )
    run = create_cloud_goal_run(
        session,
        task_kind="cloud_orchestrator",
        objective=objective_text,
        provider=_text(provider) or _text(getattr(settings, "cloud_research_provider", "openrouter")) or "openrouter",
        model=_text(model) or _text(getattr(settings, "cloud_research_model", "openrouter/free")) or "openrouter/free",
        token_policy=token_policy or dict(DEFAULT_TOKEN_POLICY),
        initial_context_md=context_md,
    )
    strong_provider_v = _text(strong_provider) or run.provider
    flash_provider_v = _text(flash_provider) or run.provider
    strong_model_v = _text(strong_model) or run.model
    flash_model_v = _text(flash_model) or run.model
    if strong_provider_v.lower() == "deepseek":
        strong_model_v = "deepseek-v4-flash"
    if flash_provider_v.lower() == "deepseek":
        flash_model_v = "deepseek-v4-flash"
    append_cloud_goal_event(
        session,
        run,
        event_kind="tool_result",
        summary="Prepared cloud orchestrator context and capability catalog.",
        payload={"capability_count": len(list_task_capabilities()), "lookback_hours": int(lookback_hours or 168)},
    )
    stats = dict(run.stats_json or {})
    stats.update(
        {
            "goal_state": "active",
            "goal_revisions": [],
            "skill_prompt": _text(skill_prompt),
            "model_roles": {
                "orchestrator": {"provider": run.provider, "model": run.model},
                "strong": {
                    "provider": strong_provider_v,
                    "model": strong_model_v,
                },
                "flash": {
                    "provider": flash_provider_v,
                    "model": flash_model_v,
                },
            },
            "permission_profile": dict(permission_profile or {}),
            "tool_route": route,
            "tool_profile": _text(tool_profile) or "analyst_v1",
            "subagent_prompts": {
                str(key): _text(value)
                for key, value in dict(subagent_prompts or {}).items()
                if _text(key) and _text(value)
            },
        }
    )
    run.stats_json = stats
    session.flush()
    return serialize_cloud_goal_run(run, events=run.events)


def _cloud_orchestrator_responses_dispatch(
    *,
    session: Session,
    run: Any,
    context_md: str,
    provider: str,
    model: str,
    timeout_s: float,
    route: str,
    tool_profile: str,
) -> dict[str, Any] | None:
    """Return an opt-in tool-enabled dispatch, or None for the legacy path."""

    if str(provider or "").strip().lower() != "openai" or route == "direct":
        return None
    client = ThothMcpClient(timeout_s=min(float(timeout_s), 120.0))
    selected_names = _CLOUD_PTC_TOOL_NAMES if route == "ptc" else _CLOUD_READ_TOOL_NAMES
    definitions = client.openai_tool_definitions(
        tool_profile=_text(tool_profile) or "analyst_v1",
        selected_names=selected_names,
        max_tools=16,
        allow_programmatic=route == "ptc",
    )
    if not definitions:
        return None
    route_instruction = (
        "Use Programmatic Tool Calling only for bounded read-only filtering, joining, ranking, deduplication, or aggregation; preserve source identifiers and citations."
        if route == "ptc"
        else "Use independent read tools in parallel only when safe; keep all writes and judgment-changing work direct."
    )
    try:
        return manifested_responses_tools(
            session,
            run_kind="cloud_goal",
            run_id=int(run.id),
            task_kind=run.task_kind,
            messages=[
                ChatMessage(role="system", content=CLOUD_ORCHESTRATOR_SYSTEM),
                ChatMessage(role="system", content=route_instruction),
                ChatMessage(role="user", content=context_md),
            ],
            tool_definitions=definitions,
            tool_executor=client.call_tool_with_metadata,
            provider=provider,
            model=model,
            timeout_s=timeout_s,
            max_input_chars=None,
            max_output_tokens=None,
            permission_profile="cloud-goal-model-with-curated-tools",
            capability_profile="analyst_v2",
            tool_route=route,
            json_output=True,
            prompt_cache_key="thoth-cloud-orchestrator-v1",
            store=False,
            inbox_boundary="next_step",
        )
    except AgentLlmError as exc:
        # The optional route is explicitly gated.  A disabled Responses/PTC
        # adapter must leave the existing inbox-aware chat route available;
        # other provider/API failures remain visible to the caller.
        if "disabled by configuration" not in str(exc).lower():
            raise
        return None


def step_cloud_orchestrator_run(session: Session, run_id: int) -> dict[str, Any]:
    run = get_cloud_goal_run(session, int(run_id))
    if run is None or run.task_kind != "cloud_orchestrator":
        raise ValueError("Cloud orchestrator run not found.")
    if run.status == CLOUD_GOAL_COMPLETE:
        return serialize_cloud_goal_run(run, events=run.events)
    context_md = run.current_context_md or ""
    provider = run.provider
    model = run.model
    run_stats = dict(run.stats_json or {})
    tool_route = _text(run_stats.get("tool_route")).lower() or "direct"
    tool_profile = _text(run_stats.get("tool_profile")) or "analyst_v1"
    timeout_s = max(30.0, _float(getattr(settings, "cloud_research_timeout_s", 1200.0), 1200.0))
    session.flush()
    session.commit()
    try:
        result = _cloud_orchestrator_responses_dispatch(
            session=session,
            run=run,
            context_md=context_md,
            provider=provider,
            model=model,
            timeout_s=timeout_s,
            route=tool_route,
            tool_profile=tool_profile,
        )
        if result is None:
            result = manifested_chat_json(
                session,
                run_kind="cloud_goal",
                run_id=int(run.id),
                task_kind=run.task_kind,
                messages=[
                    ChatMessage(role="system", content=CLOUD_ORCHESTRATOR_SYSTEM),
                    ChatMessage(role="user", content=context_md),
                ],
                inbox_boundary="next_step",
                provider=provider,
                model=model,
                timeout_s=timeout_s,
                max_input_chars=None,
                max_output_tokens=None,
                prompt_cache_key="thoth-cloud-orchestrator-v1",
                permission_profile="cloud-goal-model-no-direct-tools",
            )
        final = _normalize_plan(result)
        run = get_cloud_goal_run(session, int(run_id))
        if run is None:
            raise ValueError("Cloud orchestrator run disappeared.")
        metadata = llm_metadata(result)
        run.routed_model = metadata.get("model") or metadata.get("response_model") or run.routed_model
        stats = dict(run.stats_json or {})
        goal_update = final.get("goal_update") if isinstance(final.get("goal_update"), dict) else {}
        revised_objective = _text(goal_update.get("objective"))
        if revised_objective and revised_objective != run.objective:
            revisions = list(stats.get("goal_revisions") or [])
            revisions.append(
                {
                    "from": run.objective,
                    "to": revised_objective,
                    "reason": _text(goal_update.get("reason")) or "Lead analyst revised the goal during planning.",
                    "at": utcnow().isoformat(),
                }
            )
            run.objective = revised_objective
            stats["goal_revisions"] = revisions
            append_cloud_goal_event(
                session,
                run,
                event_kind="goal_revised",
                summary=_text(goal_update.get("reason")) or "Lead analyst revised the active goal.",
                content_md=revised_objective,
                payload={"goal_update": goal_update},
            )
        usage = {
            key: _int(metadata.get(key), 0)
            for key in ("input_tokens", "output_tokens", "total_tokens", "reasoning_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens")
            if metadata.get(key) is not None
        }
        stats["steps"] = int(stats.get("steps") or 0) + 1
        stats = accumulate_llm_usage_stats(stats, metadata)
        stats["usage"] = usage
        stats["provider"] = metadata.get("provider") or provider
        stats["requested_model"] = metadata.get("requested_model") or model
        stats["routed_model"] = run.routed_model
        run.stats_json = stats
        run.final_result = final
        run.status = CLOUD_GOAL_COMPLETE
        run.completed_at = utcnow()
        run.updated_at = utcnow()
        append_cloud_goal_event(
            session,
            run,
            event_kind="final",
            summary=final.get("plan_summary") or final.get("plan_title"),
            content_md=_plan_markdown(final),
            payload={"llm": metadata, "steps": final.get("steps") or []},
        )
        session.flush()
        return serialize_cloud_goal_run(run, events=run.events)
    except Exception as exc:
        run = get_cloud_goal_run(session, int(run_id))
        if run is not None:
            run.status = CLOUD_GOAL_FAILED
            run.error = f"{type(exc).__name__}: {exc}"
            run.updated_at = utcnow()
            append_cloud_goal_event(session, run, event_kind="error", summary=run.error)
            session.flush()
        raise


def continue_cloud_orchestrator_run(session: Session, run_id: int, *, max_steps: int = 1) -> dict[str, Any]:
    payload: dict[str, Any] | None = None
    for _ in range(max(1, min(int(max_steps or 1), 3))):
        payload = step_cloud_orchestrator_run(session, int(run_id))
        if payload.get("status") in {CLOUD_GOAL_COMPLETE, CLOUD_GOAL_FAILED}:
            break
    return payload or step_cloud_orchestrator_run(session, int(run_id))


def _child_model_for_step(run: Any, step: dict[str, Any]) -> tuple[str, str]:
    inputs = step.get("inputs") if isinstance(step.get("inputs"), dict) else {}
    stats = dict(getattr(run, "stats_json", {}) or {})
    roles = stats.get("model_roles") if isinstance(stats.get("model_roles"), dict) else {}
    tier = _text(step.get("model_tier")).lower()
    role_name = "strong" if tier == "strong" else "flash" if tier == "flash" else "orchestrator"
    role = roles.get(role_name) if isinstance(roles.get(role_name), dict) else {}
    provider = _text(inputs.get("provider")) or _text(role.get("provider")) or _text(run.provider) or _text(getattr(settings, "cloud_research_provider", "openrouter")) or "openrouter"
    model = _text(inputs.get("model")) or _text(role.get("model")) or _text(run.model) or _text(getattr(settings, "cloud_research_model", "openrouter/free")) or "openrouter/free"
    return provider, model


def _dependency_context(session: Session, run: Any, step: dict[str, Any]) -> str:
    deps = {_text(value) for value in (step.get("depends_on") if isinstance(step.get("depends_on"), list) else []) if _text(value)}
    stats = dict(run.stats_json or {})
    handoffs = [item for item in (stats.get("handoffs") or []) if isinstance(item, dict)]
    selected = [item for item in handoffs if not deps or _text(item.get("step_id")) in deps]
    parts = ["# Parent Goal Context", "", f"Goal: {run.objective}"]
    prompts = stats.get("subagent_prompts") if isinstance(stats.get("subagent_prompts"), dict) else {}
    task_prompt = _text(prompts.get(_text(step.get("task_id"))))
    if task_prompt:
        parts.extend(["", "## Operator-Editable Role Prompt", "", task_prompt])
    requested_skill_slugs = [_text(value) for value in step.get("skill_slugs") or [] if _text(value)]
    if requested_skill_slugs:
        active_skills = resolve_cloud_skills(session, explicit_slugs=requested_skill_slugs)
        parts.extend(["", "## Activated Thoth Skills", "", render_cloud_skill_instructions(active_skills, session=session)])
    for item in selected:
        child_id = _nullable_int(item.get("child_run_id"))
        child = get_cloud_goal_run(session, child_id) if child_id is not None else None
        if child is None:
            continue
        parts.extend(
            [
                "",
                f"## Dependency {item.get('step_id')} / {child.task_kind} / run {child.id}",
                "",
                json.dumps(child.final_result or {}, ensure_ascii=False, indent=2),
            ]
        )
    return "\n".join(parts).strip()


def _attach_dependency_context(session: Session, parent: Any, step: dict[str, Any], child_run_id: int) -> None:
    child = get_cloud_goal_run(session, int(child_run_id))
    if child is None:
        return
    context_md = _dependency_context(session, parent, step)
    if not context_md:
        return
    requested_skill_slugs = [_text(value) for value in step.get("skill_slugs") or [] if _text(value)]
    if requested_skill_slugs:
        active_skills = resolve_cloud_skills(session, explicit_slugs=requested_skill_slugs)
        stats = dict(child.stats_json or {})
        stats["active_skills"] = cloud_skill_snapshot(active_skills)
        child.stats_json = stats
    append_cloud_goal_event(
        session,
        child,
        event_kind="tool_result",
        summary=f"Parent goal and dependency outputs from orchestrator run {parent.id}.",
        content_md=context_md,
        payload={"tool": "orchestrator_dependencies", "parent_run_id": int(parent.id), "step_id": _text(step.get("step_id"))},
    )


def _child_token_policy(step: dict[str, Any]) -> dict[str, Any]:
    return dict(DEFAULT_TOKEN_POLICY)


def _nullable_int(value: object) -> int | None:
    try:
        return int(value) if value is not None and str(value).strip() else None
    except Exception:
        return None


def _create_child_run_for_step(session: Session, run: Any, step: dict[str, Any]) -> dict[str, Any]:
    task_id = _text(step.get("task_id"))
    inputs = step.get("inputs") if isinstance(step.get("inputs"), dict) else {}
    provider, model = _child_model_for_step(run, step)
    objective = _text(step.get("objective")) or f"Execute orchestrator step {step.get('step_id')}: {task_id}"
    token_policy = _child_token_policy(step)
    query = _text(inputs.get("query")) or objective
    subject = _text(inputs.get("subject")) or None
    report_id = _nullable_int(inputs.get("report_id"))
    if task_id == "source_pack_builder":
        return start_source_pack_goal_run(
            session,
            objective=objective,
            provider=provider,
            model=model,
            query=query,
            report_id=report_id,
            subject=subject,
            limit=max(1, min(_int(inputs.get("limit"), 18), 40)),
            token_policy=token_policy,
        )
    if task_id == "gap_hunter":
        return start_gap_hunter_goal_run(
            session,
            objective=objective,
            provider=provider,
            model=model,
            keyword=_text(inputs.get("keyword")) or _text(inputs.get("query")) or None,
            section_id=_nullable_int(inputs.get("section_id")),
            universe_id=_nullable_int(inputs.get("universe_id")),
            limit=max(1, min(_int(inputs.get("limit"), 200), 500)),
            token_policy=token_policy,
        )
    if task_id == "red_team":
        return start_red_team_goal_run(
            session,
            objective=objective,
            provider=provider,
            model=model,
            report_id=report_id,
            subject=subject or query,
            token_policy=token_policy,
        )
    if task_id == "report_body_builder":
        return start_report_body_builder_goal_run(
            session,
            objective=objective,
            provider=provider,
            model=model,
            report_id=report_id,
            subject=subject or query,
            token_policy=token_policy,
        )
    if task_id == "bottleneck_constraint":
        return start_bottleneck_constraint_goal_run(
            session,
            objective=objective,
            provider=provider,
            model=model,
            query=query,
            report_id=report_id,
            subject=subject,
            token_policy=token_policy,
        )
    raise ValueError(f"Task {task_id} does not have an executable handoff adapter yet.")


def _allowed_child_write_flags(
    step: dict[str, Any],
    *,
    allow_durable_writes: bool,
    operator_approved: bool,
) -> dict[str, bool]:
    allowed_write = _text(step.get("allowed_write")).lower()
    approved = bool(allow_durable_writes and operator_approved)
    return {
        "persist_addendum": approved and allowed_write == "report_addendum",
        "persist_candidates": approved and allowed_write in {"candidate", "candidate_requests"},
        "operator_approved": bool(operator_approved),
    }


def _continue_child_run(
    session: Session,
    task_id: str,
    child_run_id: int,
    *,
    max_steps: int,
    step: dict[str, Any] | None = None,
    allow_durable_writes: bool = False,
    operator_approved: bool = False,
) -> dict[str, Any]:
    flags = _allowed_child_write_flags(step or {}, allow_durable_writes=allow_durable_writes, operator_approved=operator_approved)
    if task_id == "source_pack_builder":
        return continue_source_pack_goal_run(session, child_run_id, max_steps=max_steps)
    if task_id == "gap_hunter":
        return continue_gap_hunter_goal_run(
            session,
            child_run_id,
            max_steps=max_steps,
            persist_candidates=flags["persist_candidates"],
            operator_approved=flags["operator_approved"],
        )
    if task_id == "red_team":
        return continue_red_team_goal_run(
            session,
            child_run_id,
            max_steps=max_steps,
            persist_candidates=flags["persist_candidates"],
            operator_approved=flags["operator_approved"],
        )
    if task_id == "report_body_builder":
        return continue_report_body_builder_goal_run(
            session,
            child_run_id,
            max_steps=max_steps,
            persist_addendum=flags["persist_addendum"],
            operator_approved=flags["operator_approved"],
        )
    if task_id == "bottleneck_constraint":
        return continue_bottleneck_constraint_goal_run(session, child_run_id, max_steps=max_steps)
    raise ValueError(f"Task {task_id} does not have an executable continuation adapter yet.")


def _next_dispatchable_step(run: Any) -> dict[str, Any] | None:
    final = dict(run.final_result or {})
    stats = dict(run.stats_json or {})
    executed = {str(item) for item in stats.get("executed_step_ids", []) if str(item)}
    for step in final.get("steps") if isinstance(final.get("steps"), list) else []:
        if not isinstance(step, dict):
            continue
        step_id = _text(step.get("step_id"))
        if not step_id or step_id in executed:
            continue
        deps = [_text(dep) for dep in (step.get("depends_on") if isinstance(step.get("depends_on"), list) else []) if _text(dep)]
        if all(dep in executed for dep in deps):
            return step
    return None


def dispatch_next_cloud_orchestrator_step(
    session: Session,
    run_id: int,
    *,
    execute_child: bool = False,
    child_max_steps: int = 1,
    compact_if_needed: bool = True,
    allow_durable_writes: bool = False,
    operator_approved: bool = False,
) -> dict[str, Any]:
    run = get_cloud_goal_run(session, int(run_id))
    if run is None or run.task_kind != "cloud_orchestrator":
        raise ValueError("Cloud orchestrator run not found.")
    if run.status != CLOUD_GOAL_COMPLETE:
        raise ValueError("Cloud orchestrator must have a finalized plan before handoff.")
    step = _next_dispatchable_step(run)
    if step is None:
        append_cloud_goal_event(session, run, event_kind="handoff_complete", summary="No remaining dispatchable orchestrator steps.")
        session.flush()
        return serialize_cloud_goal_run(run, events=list_cloud_goal_events(session, int(run.id), limit=1000))

    task_id = _text(step.get("task_id"))
    step_id = _text(step.get("step_id"))
    tier = _text(step.get("model_tier")).lower()
    if tier in {"human", "local"}:
        stats = dict(run.stats_json or {})
        executed = [str(item) for item in stats.get("executed_step_ids", []) if str(item)]
        executed.append(step_id)
        stats["executed_step_ids"] = sorted(set(executed), key=executed.index)
        handoffs = list(stats.get("handoffs") or [])
        handoffs.append({"step_id": step_id, "task_id": task_id, "status": "skipped", "reason": f"{tier} step"})
        stats["handoffs"] = handoffs
        run.stats_json = stats
        append_cloud_goal_event(
            session,
            run,
            event_kind="handoff_skipped",
            summary=f"Skipped {step_id} {task_id}: {tier} step is not cloud-executable.",
            payload={"step": step},
        )
        session.flush()
        return serialize_cloud_goal_run(run, events=list_cloud_goal_events(session, int(run.id), limit=1000))

    child = _create_child_run_for_step(session, run, step)
    child_run_id = int(child["id"])
    child_row = get_cloud_goal_run(session, child_run_id)
    if child_row is None:
        raise ValueError(f"Delegated child run #{child_run_id} was not persisted.")
    write_flags = _allowed_child_write_flags(
        step,
        allow_durable_writes=allow_durable_writes,
        operator_approved=operator_approved,
    )
    lineage = create_cloud_goal_lineage(
        session,
        parent_run=run,
        child_run=child_row,
        step=step,
        permission_profile={
            "parent": dict((run.stats_json or {}).get("permission_profile") or {}),
            "dispatch": write_flags,
        },
    )
    _attach_dependency_context(session, run, step, child_run_id)
    child = serialize_cloud_goal_run(
        get_cloud_goal_run(session, child_run_id),
        events=list_cloud_goal_events(session, child_run_id, limit=500),
    ) if get_cloud_goal_run(session, child_run_id) is not None else child
    child_result = child
    if execute_child:
        child_result = _continue_child_run(
            session,
            task_id,
            child_run_id,
            max_steps=max(1, min(int(child_max_steps or 1), 5)),
            step=step,
            allow_durable_writes=allow_durable_writes,
            operator_approved=operator_approved,
        )
    lineage = sync_cloud_goal_lineage(
        session,
        lineage,
        child_payload=child_result,
        accept_output=bool(execute_child and _text(child_result.get("status")).lower() == "complete"),
    )

    run = get_cloud_goal_run(session, int(run_id))
    if run is None:
        raise ValueError("Cloud orchestrator run disappeared during handoff.")
    stats = dict(run.stats_json or {})
    executed = [str(item) for item in stats.get("executed_step_ids", []) if str(item)]
    executed.append(step_id)
    stats["executed_step_ids"] = sorted(set(executed), key=executed.index)
    handoffs = list(stats.get("handoffs") or [])
    handoffs.append(
        {
            "step_id": step_id,
            "task_id": task_id,
            "child_run_id": child_run_id,
            "child_status": child_result.get("status"),
            "executed_child": bool(execute_child),
            "allow_durable_writes": bool(allow_durable_writes),
            "operator_approved": bool(operator_approved),
            "allowed_write": _text(step.get("allowed_write")),
            "lineage_id": int(lineage.id),
            "terminal_disposition": lineage.terminal_disposition,
        }
    )
    stats["handoffs"] = handoffs
    run.stats_json = stats
    append_cloud_goal_event(
        session,
        run,
        event_kind="handoff_started" if not execute_child else "handoff_executed",
        summary=f"Dispatched {step_id} to {task_id} as cloud run #{child_run_id}.",
        content_md=_handoff_markdown(step, child_result),
        payload={
            "step": step,
            "child_run": child_result,
            "lineage": serialize_cloud_goal_lineage(lineage),
            "write_flags": write_flags,
        },
    )
    if lineage.terminal_disposition == "accepted":
        append_cloud_goal_lineage_acceptance_event(session, lineage)
    if compact_if_needed:
        rendered = render_cloud_goal_context(run, list_cloud_goal_events(session, int(run.id), limit=1000))
        needed, why = should_compact_context(run, rendered)
        if needed:
            return compact_cloud_goal_context(session, int(run.id), reason=f"orchestrator handoff ledger: {why}")["run"]
    session.flush()
    return serialize_cloud_goal_run(run, events=list_cloud_goal_events(session, int(run.id), limit=1000))


def continue_cloud_orchestrator_handoffs(
    session: Session,
    run_id: int,
    *,
    max_handoffs: int = 1,
    execute_child: bool = False,
    child_max_steps: int = 1,
    compact_if_needed: bool = True,
    allow_durable_writes: bool = False,
    operator_approved: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] | None = None
    for _ in range(max(1, min(int(max_handoffs or 1), 24))):
        run = get_cloud_goal_run(session, int(run_id))
        if run is None:
            raise ValueError("Cloud orchestrator run not found.")
        if _next_dispatchable_step(run) is None:
            break
        payload = dispatch_next_cloud_orchestrator_step(
            session,
            int(run_id),
            execute_child=execute_child,
            child_max_steps=child_max_steps,
            compact_if_needed=compact_if_needed,
            allow_durable_writes=allow_durable_writes,
            operator_approved=operator_approved,
        )
    if payload is not None:
        return payload
    run = get_cloud_goal_run(session, int(run_id))
    if run is None:
        raise ValueError("Cloud orchestrator run not found.")
    return serialize_cloud_goal_run(run, events=list_cloud_goal_events(session, int(run.id), limit=1000))


def _handoff_markdown(step: dict[str, Any], child_result: dict[str, Any]) -> str:
    lines = [
        f"## Handoff: {step.get('step_id')} -> {step.get('task_id')}",
        "",
        f"- child_run_id: {child_result.get('id')}",
        f"- child_status: {child_result.get('status')}",
        f"- objective: {step.get('objective')}",
        f"- expected_output: {step.get('expected_output')}",
    ]
    final = child_result.get("final_result") if isinstance(child_result.get("final_result"), dict) else {}
    summary = _text(final.get("summary"))
    if summary:
        lines.append(f"- child_summary: {summary}")
    return "\n".join(lines).strip()


def _plan_markdown(plan: dict[str, Any]) -> str:
    lines = [
        f"# {plan.get('plan_title') or 'Cloud Research Orchestration Plan'}",
        "",
        str(plan.get("plan_summary") or "").strip(),
        "",
        "## Steps",
    ]
    for step in plan.get("steps") or []:
        if not isinstance(step, dict):
            continue
        lines.append("")
        lines.append(f"### {step.get('step_id')} - {step.get('task_id')}")
        lines.append(f"- model_tier: {step.get('model_tier')}")
        lines.append(f"- objective: {step.get('objective')}")
        lines.append(f"- expected_output: {step.get('expected_output')}")
        lines.append(f"- allowed_write: {step.get('allowed_write')}")
        if step.get("stop_condition"):
            lines.append(f"- stop_condition: {step.get('stop_condition')}")
        if step.get("escalation"):
            lines.append(f"- escalation: {step.get('escalation')}")
    if plan.get("risks"):
        lines.append("")
        lines.append("## Risks")
        lines.extend(f"- {item}" for item in plan.get("risks") or [])
    if plan.get("missing_context"):
        lines.append("")
        lines.append("## Missing Context")
        lines.extend(f"- {item}" for item in plan.get("missing_context") or [])
    return "\n".join(lines).strip()
