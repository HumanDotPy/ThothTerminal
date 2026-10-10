from __future__ import annotations

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
from thoth_terminal.config import settings
from thoth_terminal.models import CloudGoalRun
from thoth_terminal.source_pack_service import build_source_pack, render_source_pack_markdown


SOURCE_PACK_SYSTEM = """Return json only.
You are Thoth Source Pack Builder.
Your job is to compile a compact, provenance-labeled source pack for downstream research agents.

Prefer compactness over exhaustiveness. Do not rewrite the whole report. Preserve only the source items, claims, provenance labels, and gaps that help another agent act.
If the deterministic source pack contains exact-anchor items from vetted specialist relay/social-wrapper sources, preserve them explicitly with their local ids and source boundary. Do not call an exact-anchor item missing merely because it is not primary/company evidence. Instead separate: "present as relay/internal evidence" from "not corroborated by primary or institutional evidence."

Source tiers:
- primary/company/regulatory/customer data
- technical paper/conference material
- institutional analyst synthesis
- vetted specialist analyst relay
- thoth report memory
- gap/speculation

Conference item boundaries:
- conference_program_card items prove talk/session existence, speaker/company/topic scope, and follow-up targets only.
- conference_paper_card, technical_source_card, and research_source_card items may support technical claims, but only within the snippet/source boundary.
- Do not summarize program-listed products as technically covered unless a deck, paper, issuer page, or other source card is also present.
- Do not infer deployment volume, customer allocation, share, yield, pricing, or production readiness from conference agenda entries.

Return exactly one json object:
{
  "action": "request_context|continue|finalize",
  "rationale": "short reason",
  "context_requests": [{"request_type":"search", "query":"...", "reason":"..."}],
  "working_context_md": "short markdown state",
  "final_result": {
    "summary": "",
    "source_pack_md": "",
    "key_items": [],
    "claims_to_corroborate": [],
    "gaps": [],
    "recommended_next_searches": []
  }
}
Use action=request_context only for bounded searches that would materially improve the pack.
Use action=finalize when the pack is compact and useful enough.
"""


SOURCE_PACK_CACHE_CONTEXT_VERSION = "source-pack-multipass-v1"
SOURCE_PACK_MULTIPASS_TASKS: tuple[tuple[str, str], ...] = (
    ("key_items", "Extract the highest-value source items with provenance labels and one-sentence relevance notes."),
    ("claims_to_corroborate", "List the claims that need corroboration, grouped by claim type and suggested evidence source."),
    ("next_searches", "Recommend the next bounded searches that would materially improve this source pack."),
)


def _text(value: object) -> str:
    return str(value or "").strip()


def normalize_source_pack_result(value: dict[str, Any] | None, fallback_pack: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = dict(value or {})
    fallback_md = str((fallback_pack or {}).get("markdown") or "")
    return {
        "summary": _text(payload.get("summary")) or _text((fallback_pack or {}).get("summary")),
        "source_pack_md": _text(payload.get("source_pack_md")) or fallback_md,
        "key_items": payload.get("key_items") if isinstance(payload.get("key_items"), list) else [],
        "claims_to_corroborate": payload.get("claims_to_corroborate") if isinstance(payload.get("claims_to_corroborate"), list) else [],
        "gaps": payload.get("gaps") if isinstance(payload.get("gaps"), list) else list((fallback_pack or {}).get("gaps") or []),
        "recommended_next_searches": payload.get("recommended_next_searches") if isinstance(payload.get("recommended_next_searches"), list) else [],
    }


def start_source_pack_goal_run(
    session: Session,
    *,
    objective: str | None = None,
    provider: str = "openrouter",
    model: str = "openrouter/free",
    query: str | None = None,
    report_id: int | None = None,
    subject: str | None = None,
    limit: int = 18,
    token_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    pack = build_source_pack(session, query=query, report_id=report_id, subject=subject, limit=limit)
    run = create_cloud_goal_run(
        session,
        task_kind="source_pack_builder",
        objective=objective or f"Build a compact source pack for: {pack.get('query')}",
        provider=provider,
        model=model,
        token_policy=token_policy,
    )
    append_cloud_goal_event(
        session,
        run,
        event_kind="tool_result",
        summary=f"Initial deterministic source pack: {pack.get('query')}",
        content_md=pack["markdown"],
        payload={"tool": "source_pack", "pack": {k: v for k, v in pack.items() if k != "markdown"}},
    )
    run.current_context_md = "# Source Pack Builder Working Context\n\nInitial deterministic source pack is available. Compact it for downstream agents."
    session.flush()
    return serialize_cloud_goal_run(run, events=list_cloud_goal_events(session, int(run.id)))


def _model_step(session: Session, run: CloudGoalRun, rendered_context: str) -> dict[str, Any]:
    return manifested_chat_json(
        session,
        run_kind="cloud_goal",
        run_id=int(run.id),
        task_kind=run.task_kind,
        messages=[ChatMessage(role="system", content=SOURCE_PACK_SYSTEM), ChatMessage(role="user", content=rendered_context)],
        inbox_boundary="next_step",
        provider=run.provider,
        model=run.model,
        timeout_s=max(180.0, float(getattr(settings, "agent_llm_timeout_s", 60) or 60)),
        max_input_chars=None,
        max_output_tokens=None,
        prompt_cache_key="thoth-source-pack-builder-v1",
        permission_profile="cloud-goal-model-no-direct-tools",
    )


def source_pack_same_context_messages(source_pack_md: str, *, task: str, task_key: str = "custom") -> list[ChatMessage]:
    stable_context = (
        f"# Thoth Source Pack Cache Context\n"
        f"context_pack_version: {SOURCE_PACK_CACHE_CONTEXT_VERSION}\n\n"
        "# Source Pack Context\n\n"
        f"{_text(source_pack_md)}\n\n"
        "# Task\n\n"
    )
    return [
        ChatMessage(role="system", content=SOURCE_PACK_SYSTEM),
        ChatMessage(role="user", content=f"{stable_context}task_key: {_text(task_key) or 'custom'}\n\n{_text(task)}"),
    ]


def run_source_pack_same_context_passes(
    session: Session,
    run_id: int,
    *,
    tasks: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    run = get_cloud_goal_run(session, int(run_id))
    if run is None:
        raise ValueError("Source Pack Builder run not found.")
    fallback_pack = _fallback_pack_from_events(list_cloud_goal_events(session, int(run.id), limit=500))
    source_pack_md = _text((fallback_pack or {}).get("markdown"))
    if not source_pack_md:
        raise ValueError("No source pack markdown is available for same-context passes.")
    task_rows = tasks or [{"task_key": key, "task": task} for key, task in SOURCE_PACK_MULTIPASS_TASKS]
    results: list[dict[str, Any]] = []
    stats = dict(run.stats_json or {})
    stats["context_pack_version"] = SOURCE_PACK_CACHE_CONTEXT_VERSION
    stats["stable_prefix_chars"] = len(
        source_pack_same_context_messages(source_pack_md, task="", task_key="stable_prefix_probe")[1].content.rsplit("# Task", 1)[0]
    )
    session.flush()
    session.commit()
    for idx, item in enumerate(task_rows, start=1):
        task_key = _text(item.get("task_key")) or f"pass_{idx}"
        result = manifested_chat_json(
            session,
            run_kind="cloud_goal",
            run_id=int(run.id),
            task_kind=f"source_pack_same_context:{task_key}",
            messages=source_pack_same_context_messages(source_pack_md, task=_text(item.get("task")), task_key=task_key),
            provider=run.provider,
            model=run.model,
            timeout_s=max(180.0, float(getattr(settings, "agent_llm_timeout_s", 60) or 60)),
            max_input_chars=None,
            max_output_tokens=None,
            prompt_cache_key=f"thoth-source-pack-context-{SOURCE_PACK_CACHE_CONTEXT_VERSION}",
            permission_profile="cloud-goal-model-no-direct-tools",
        )
        meta = llm_metadata(result)
        stats = accumulate_llm_usage_stats(stats, meta)
        results.append({"task_key": task_key, "result": result, "llm": meta})
    run = get_cloud_goal_run(session, int(run_id))
    if run is None:
        raise ValueError("Source Pack Builder run disappeared.")
    run.stats_json = stats
    append_cloud_goal_event(
        session,
        run,
        event_kind="same_context_passes",
        summary=f"Ran {len(results)} source-pack same-context cache-oriented pass(es).",
        payload={"context_pack_version": SOURCE_PACK_CACHE_CONTEXT_VERSION, "results": results},
    )
    session.flush()
    return serialize_cloud_goal_run(run, events=list_cloud_goal_events(session, int(run.id), limit=500))


def _fallback_pack_from_events(events: list[Any]) -> dict[str, Any] | None:
    for event in events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        pack = payload.get("pack") if isinstance(payload.get("pack"), dict) else None
        if pack is not None:
            pack = dict(pack)
            pack["markdown"] = event.content_md or render_source_pack_markdown(pack)
            return pack
    return None


def _handle_context_request(session: Session, req: dict[str, Any]) -> dict[str, Any]:
    query = _text(req.get("query") or req.get("search") or req.get("subject"))
    if not query:
        return {"kind": "unsupported_context_request", "request": dict(req)}
    return build_source_pack(session, query=query, limit=10, include_object_search=True)


def step_source_pack_goal_run(
    session: Session,
    run_id: int,
    *,
    max_context_requests: int = 3,
) -> dict[str, Any]:
    run = get_cloud_goal_run(session, int(run_id))
    if run is None:
        raise ValueError("Source Pack Builder run not found.")
    if run.status == CLOUD_GOAL_WAITING and not resume_cloud_goal_after_cooldown(
        session,
        run,
        summary="Source-pack cooldown elapsed; retrying model compaction.",
    ):
        return serialize_cloud_goal_run(run, events=list_cloud_goal_events(session, int(run.id), limit=500))
    if run.status != CLOUD_GOAL_RUNNING:
        return serialize_cloud_goal_run(run, events=list_cloud_goal_events(session, int(run.id), limit=500))
    events = list_cloud_goal_events(session, int(run.id), limit=500)
    rendered = render_cloud_goal_context(run, events)
    should_compact, reason = should_compact_context(run, rendered)
    if should_compact:
        run.current_context_md = rendered[-90_000:]
        append_cloud_goal_event(session, run, event_kind="compaction", summary=reason, content_md=run.current_context_md)
    stats = dict(run.stats_json or {})
    step_index = int(stats.get("steps") or 0) + 1
    try:
        decision = _model_step(session, run, render_cloud_goal_context(run, list_cloud_goal_events(session, int(run.id), limit=500)))
    except Exception as exc:
        fallback_pack = _fallback_pack_from_events(list_cloud_goal_events(session, int(run.id), limit=500))
        if fallback_pack is not None:
            final = normalize_source_pack_result(None, fallback_pack=fallback_pack)
            final["summary"] = final.get("summary") or f"Fallback deterministic source pack after model failure: {fallback_pack.get('query')}"
            final["gaps"] = [
                *list(final.get("gaps") or []),
                f"Cloud model failed during source-pack compaction ({str(exc)[:240]}); this pack is deterministic and not model-refined.",
            ]
            run.final_result = final
            if is_transient_cloud_model_failure(exc):
                run.error = None
                mark_cloud_goal_model_cooldown(
                    session,
                    run,
                    exc,
                    step_index=step_index,
                    fallback_available=True,
                    summary_prefix="Transient model failure; deterministic pack saved",
                )
            else:
                run.status = CLOUD_GOAL_COMPLETE
                run.completed_at = utcnow()
                run.error = None
                append_cloud_goal_event(
                    session,
                    run,
                    event_kind="model_fallback",
                    summary=str(exc),
                    payload={"error": str(exc), "fallback_available": True},
                    step_index=step_index,
                )
            append_cloud_goal_event(session, run, event_kind="fallback_pack", summary=final.get("summary"), payload={"final_result": final}, step_index=step_index)
            run.updated_at = utcnow()
            session.flush()
            return serialize_cloud_goal_run(run, events=list_cloud_goal_events(session, int(run.id), limit=500))
        if is_transient_cloud_model_failure(exc):
            mark_cloud_goal_model_cooldown(
                session,
                run,
                exc,
                step_index=step_index,
                fallback_available=False,
                summary_prefix="Transient source-pack model failure",
            )
        else:
            run.status = CLOUD_GOAL_FAILED
            run.error = str(exc)
            run.updated_at = utcnow()
            append_cloud_goal_event(session, run, event_kind="error", summary=str(exc), step_index=step_index)
            session.flush()
        return serialize_cloud_goal_run(run, events=list_cloud_goal_events(session, int(run.id), limit=500))
    meta = llm_metadata(decision)
    if meta.get("model"):
        run.routed_model = meta.get("model")
    stats["steps"] = step_index
    stats = accumulate_llm_usage_stats(stats, meta, include_reasoning=False)
    run.stats_json = stats
    working = _text(decision.get("working_context_md"))
    if working:
        run.current_context_md = working
    append_cloud_goal_event(session, run, event_kind="model_decision", summary=_text(decision.get("rationale") or decision.get("action") or "source pack decision"), content_md=working or None, payload={"decision": decision, "llm": meta}, step_index=step_index)
    requests = decision.get("context_requests")
    if not isinstance(requests, list):
        requests = []
    action = _text(decision.get("action")).lower() or "continue"
    if action == "request_context" or requests:
        for req in requests[: max(1, min(8, int(max_context_requests or 3)))]:
            if not isinstance(req, dict):
                continue
            pack = _handle_context_request(session, req)
            append_cloud_goal_event(session, run, event_kind="tool_result", summary=f"Context search: {req.get('query') or req.get('subject')}", content_md=pack.get("markdown") or render_source_pack_markdown(pack), payload={"tool": "source_pack_search", "request": req, "pack": {k: v for k, v in pack.items() if k != "markdown"}}, step_index=step_index)
    elif action == "finalize":
        fallback_pack = _fallback_pack_from_events(list_cloud_goal_events(session, int(run.id), limit=500))
        final = normalize_source_pack_result(decision.get("final_result") if isinstance(decision.get("final_result"), dict) else decision, fallback_pack=fallback_pack)
        run.final_result = final
        run.status = CLOUD_GOAL_COMPLETE
        run.completed_at = utcnow()
        append_cloud_goal_event(session, run, event_kind="final", summary=final.get("summary"), payload={"final_result": final}, step_index=step_index)
    run.updated_at = utcnow()
    session.flush()
    return serialize_cloud_goal_run(run, events=list_cloud_goal_events(session, int(run.id), limit=500))


def continue_source_pack_goal_run(session: Session, run_id: int, *, max_steps: int = 3) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for _idx in range(max(1, min(20, int(max_steps or 3)))):
        payload = step_source_pack_goal_run(session, int(run_id))
        if _text(payload.get("status")) != CLOUD_GOAL_RUNNING:
            break
    return payload
