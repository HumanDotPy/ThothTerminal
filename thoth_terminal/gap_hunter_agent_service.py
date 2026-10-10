from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from thoth_terminal.agent.llm_client import ChatMessage, llm_metadata
from thoth_terminal.agent_request_manifest_service import manifested_chat_json
from thoth_terminal.cloud_goal_run_service import (
    CLOUD_GOAL_COMPLETE,
    CLOUD_GOAL_FAILED,
    CLOUD_GOAL_RUNNING,
    CLOUD_GOAL_WAITING,
    accumulate_llm_usage_stats,
    append_cloud_goal_event,
    create_cloud_goal_run,
    estimate_tokens,
    get_cloud_goal_run,
    is_transient_cloud_model_failure,
    list_cloud_goal_events,
    mark_cloud_goal_model_cooldown,
    render_cloud_goal_context,
    resume_cloud_goal_after_cooldown,
    serialize_cloud_goal_run,
    should_compact_context,
    utcnow,
)
from thoth_terminal.cloud_write_policy import evaluate_cloud_write_permission
from thoth_terminal.config import settings
from thoth_terminal.gap_hunter_service import (
    normalize_gap_hunter_result,
    persist_gap_hunter_candidates,
    resolve_gap_hunter_context_request,
    universe_coverage_index,
)
from thoth_terminal.models import CloudGoalRun


GAP_HUNTER_GOAL_SYSTEM = """Return json only.
You are Thoth Gap Hunter running as a durable long-context cloud goal.
Use the working context and tool packets to decide what to inspect next.
Do not invent source citations. Do not claim company-level report quality unless a drilldown packet contains it.
You may ask for more context, continue reasoning, or finalize.

Return exactly one json object:
{
  "action": "request_context|continue|finalize",
  "rationale": "short reason",
  "context_requests": [{"request_type":"keyword|section_drilldown|drilldown", "query":"...", "section_id": 123, "reason":"..."}],
  "working_context_md": "markdown update to append/replace the current working context",
  "final_result": {
    "coverage_summary": "",
    "missing_company_reports": [],
    "missing_product_explainers": [],
    "technical_definition_gaps": [],
    "super_report_candidates": [],
    "bottleneck_candidates": [],
    "demotion_candidates": [],
    "followup_context_requests": [],
    "next_best_actions": []
  }
}
Use action=request_context when another bounded packet is needed.
Use action=finalize only when the result is useful enough to create reviewable candidates.
"""


def _default_objective() -> str:
    return (
        "Autonomously audit Thoth universe coverage for missing company reports, product explainers, "
        "technical definitions, super-report candidates, bottleneck candidates, demotions, and next context requests. "
        "Start broad, inspect high-value gaps, and only finalize when the candidate list is useful."
    )


def _context_packet_md(label: str, packet: dict[str, Any]) -> str:
    return f"## Context Packet: {label}\n\n```json\n{json.dumps(packet, ensure_ascii=False)}\n```"


def start_gap_hunter_goal_run(
    session: Session,
    *,
    objective: str | None = None,
    provider: str = "deepseek",
    model: str = "deepseek-v4-flash",
    token_policy: dict[str, Any] | None = None,
    keyword: str | None = None,
    section_id: int | None = None,
    universe_id: int | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    run = create_cloud_goal_run(
        session,
        task_kind="gap_hunter",
        objective=objective or _default_objective(),
        provider=provider,
        model=model,
        token_policy=token_policy,
    )
    packet = universe_coverage_index(
        session,
        keyword=keyword,
        section_id=section_id,
        universe_id=universe_id,
        limit=limit,
    )
    append_cloud_goal_event(
        session,
        run,
        event_kind="tool_result",
        summary=f"Initial universe coverage packet: {packet.get('mode')}",
        content_md=_context_packet_md("initial_universe_coverage", packet),
        payload={"tool": "universe_coverage_index", "packet": packet},
    )
    run.current_context_md = "\n\n".join(
        [
            "# Gap Hunter Working Context",
            "No compaction has been needed. Use the raw ledger packets directly.",
            "",
            "## Initial Packet",
            f"mode={packet.get('mode')} row_count={packet.get('row_count')}",
        ]
    )
    session.flush()
    events = list_cloud_goal_events(session, int(run.id))
    return serialize_cloud_goal_run(run, events=events)


def _compact_working_context(session: Session, run: CloudGoalRun, rendered: str, *, reason: str) -> None:
    prompt = (
        "Return json only with key compacted_context_md. Compact the following cloud goal context into readable markdown. "
        "Preserve objective, inspected packets, IDs, section keys, candidate gaps, open questions, and next context requests. "
        "Do not create new claims.\n\n"
        f"{rendered}"
    )
    result = manifested_chat_json(
        session,
        run_kind="cloud_goal",
        run_id=int(run.id),
        task_kind="gap_hunter_compaction",
        messages=[ChatMessage(role="system", content="Return json only."), ChatMessage(role="user", content=prompt)],
        provider=run.provider,
        model=run.model,
        timeout_s=float(getattr(settings, "agent_llm_timeout_s", 60) or 60),
        max_input_chars=None,
        max_output_tokens=None,
        prompt_cache_key="thoth-gap-hunter-compaction-v1",
        permission_profile="cloud-goal-model-no-direct-tools",
    )
    compacted = str(result.get("compacted_context_md") or "").strip() or rendered[-24000:]
    run.current_context_md = compacted
    stats = dict(run.stats_json or {})
    stats["compactions"] = int(stats.get("compactions") or 0) + 1
    run.stats_json = stats
    append_cloud_goal_event(
        session,
        run,
        event_kind="compaction",
        summary=reason,
        content_md=compacted,
        payload={"llm": llm_metadata(result)},
    )


def _model_step(session: Session, run: CloudGoalRun, rendered_context: str) -> dict[str, Any]:
    return manifested_chat_json(
        session,
        run_kind="cloud_goal",
        run_id=int(run.id),
        task_kind=run.task_kind,
        messages=[
            ChatMessage(role="system", content=GAP_HUNTER_GOAL_SYSTEM),
            ChatMessage(role="user", content=rendered_context),
        ],
        inbox_boundary="next_step",
        provider=run.provider,
        model=run.model,
        timeout_s=float(getattr(settings, "agent_llm_timeout_s", 60) or 60),
        max_input_chars=None,
        max_output_tokens=None,
        prompt_cache_key="thoth-gap-hunter-v1",
        permission_profile="cloud-goal-model-no-direct-tools",
    )


def step_gap_hunter_goal_run(
    session: Session,
    run_id: int,
    *,
    persist_candidates: bool = False,
    operator_approved: bool = False,
    max_context_requests: int = 3,
) -> dict[str, Any]:
    run = get_cloud_goal_run(session, int(run_id))
    if run is None:
        raise ValueError("Gap Hunter run not found.")
    if run.status == CLOUD_GOAL_WAITING and not resume_cloud_goal_after_cooldown(
        session,
        run,
        summary="Gap Hunter cooldown elapsed; retrying model step.",
    ):
        events = list_cloud_goal_events(session, int(run.id))
        return serialize_cloud_goal_run(run, events=events)
    if run.status not in {CLOUD_GOAL_RUNNING}:
        events = list_cloud_goal_events(session, int(run.id))
        return serialize_cloud_goal_run(run, events=events)

    events = list_cloud_goal_events(session, int(run.id), limit=300)
    rendered = render_cloud_goal_context(run, events)
    should_compact, compact_reason = should_compact_context(run, rendered)
    if should_compact:
        _compact_working_context(session, run, rendered, reason=compact_reason)
        events = list_cloud_goal_events(session, int(run.id), limit=300)
        rendered = render_cloud_goal_context(run, events)

    stats = dict(run.stats_json or {})
    step_index = int(stats.get("steps") or 0) + 1
    try:
        decision = _model_step(session, run, rendered)
    except Exception as exc:
        if is_transient_cloud_model_failure(exc):
            mark_cloud_goal_model_cooldown(
                session,
                run,
                exc,
                step_index=step_index,
                summary_prefix="Transient Gap Hunter model failure",
            )
            return serialize_cloud_goal_run(run, events=list_cloud_goal_events(session, int(run.id)))
        run.status = CLOUD_GOAL_FAILED
        run.error = str(exc)
        run.updated_at = utcnow()
        append_cloud_goal_event(session, run, event_kind="error", summary=str(exc), step_index=step_index)
        session.flush()
        return serialize_cloud_goal_run(run, events=list_cloud_goal_events(session, int(run.id)))

    meta = llm_metadata(decision)
    if meta.get("model"):
        run.routed_model = meta.get("model")
    stats["steps"] = step_index
    stats = accumulate_llm_usage_stats(stats, meta, include_reasoning=False)
    run.stats_json = stats
    append_cloud_goal_event(
        session,
        run,
        event_kind="model_decision",
        summary=str(decision.get("rationale") or decision.get("action") or "model decision"),
        content_md=str(decision.get("working_context_md") or "").strip() or None,
        payload={"decision": decision, "llm": meta},
        step_index=step_index,
    )

    action = str(decision.get("action") or "continue").strip().lower()
    working = str(decision.get("working_context_md") or "").strip()
    if working:
        run.current_context_md = working

    requests = decision.get("context_requests")
    if not isinstance(requests, list):
        requests = []
    if action == "request_context" or requests:
        for req in requests[: max(1, min(10, int(max_context_requests or 3)))]:
            if not isinstance(req, dict):
                continue
            packet = resolve_gap_hunter_context_request(session, req, limit=200)
            append_cloud_goal_event(
                session,
                run,
                event_kind="tool_result",
                summary=f"Context request: {req.get('query') or req.get('section_id') or req.get('request_type')}",
                content_md=_context_packet_md(str(req.get("query") or req.get("section_id") or "context_request"), packet),
                payload={"tool": "universe_coverage_index", "request": req, "packet": packet},
                step_index=step_index,
            )
    elif action == "finalize":
        final = normalize_gap_hunter_result(decision.get("final_result") if isinstance(decision.get("final_result"), dict) else decision)
        persisted: list[dict[str, Any]] = []
        write_policy = None
        if persist_candidates:
            write_policy = evaluate_cloud_write_permission(
                "candidate_requests",
                operator_approved=operator_approved,
                run=run,
                context={"step_index": step_index},
            )
            append_cloud_goal_event(
                session,
                run,
                event_kind="write_authorized" if write_policy.get("ok") else "write_denied",
                summary=write_policy.get("reason"),
                payload={"write_policy": write_policy},
                step_index=step_index,
            )
            if write_policy.get("ok"):
                persisted = persist_gap_hunter_candidates(
                    session,
                    result=final,
                    index_payload={"mode": "cloud_goal", "run_id": int(run.id), "write_policy": write_policy},
                    question=run.objective,
                    provider=run.provider,
                    model=run.model,
                    llm_meta=meta,
                )
        run.final_result = final
        run.status = CLOUD_GOAL_COMPLETE
        run.completed_at = utcnow()
        append_cloud_goal_event(
            session,
            run,
            event_kind="final",
            summary=final.get("coverage_summary"),
            payload={"final_result": final, "persisted_candidates": persisted, "write_policy": write_policy},
            step_index=step_index,
        )

    run.updated_at = utcnow()
    session.flush()
    return serialize_cloud_goal_run(run, events=list_cloud_goal_events(session, int(run.id), limit=400))


def continue_gap_hunter_goal_run(
    session: Session,
    run_id: int,
    *,
    max_steps: int = 5,
    persist_candidates: bool = False,
    operator_approved: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for _idx in range(max(1, min(25, int(max_steps or 5)))):
        payload = step_gap_hunter_goal_run(
            session,
            run_id,
            persist_candidates=persist_candidates,
            operator_approved=operator_approved,
        )
        if str(payload.get("status") or "") != CLOUD_GOAL_RUNNING:
            break
    return payload
