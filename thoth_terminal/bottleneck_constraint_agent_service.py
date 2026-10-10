from __future__ import annotations

import re
from typing import Any

from sqlalchemy.orm import Session

from thoth_terminal.agent.llm_client import ChatMessage, llm_metadata
from thoth_terminal.agent_request_manifest_service import manifested_chat_text
from thoth_terminal.bottleneck_constraint_report_prompt import BOTTLENECK_CONSTRAINT_SUPER_REPORT_PROMPT
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
from thoth_terminal.models import CloudGoalRun, ResearchReport, ResearchReportVersion
from thoth_terminal.research_report_service import get_research_report_state


def _text(value: object) -> str:
    return str(value or "").strip()


def normalize_bottleneck_constraint_result(value: dict[str, Any] | None) -> dict[str, Any]:
    payload = dict(value or {})
    body = _text(payload.get("body_md") or payload.get("text") or payload.get("report_md"))
    summary = _text(payload.get("summary"))
    if not summary and body:
        for line in body.splitlines():
            cleaned = line.strip(" #*-")
            if cleaned:
                summary = cleaned[:400]
                break
    return {
        "summary": summary,
        "title": _text(payload.get("title")) or "Bottleneck / constraint scaffold",
        "body_md": body,
        "state_label": _state_label(body, payload.get("state_label")),
        "model_work_needed": payload.get("model_work_needed") if isinstance(payload.get("model_work_needed"), list) else [],
        "dataset_work_needed": payload.get("dataset_work_needed") if isinstance(payload.get("dataset_work_needed"), list) else [],
        "gap_priorities": payload.get("gap_priorities") if isinstance(payload.get("gap_priorities"), list) else [],
        "recommended_next_tasks": payload.get("recommended_next_tasks") if isinstance(payload.get("recommended_next_tasks"), list) else [],
    }


def _state_label(body: str, explicit: object = None) -> str:
    raw = _text(explicit).lower()
    allowed = {"scaffold", "minimum_viable", "analyst_grade_candidate", "needs_revision"}
    if raw in allowed:
        return raw
    lowered = str(body or "").lower()
    head = lowered[:5000]
    explicit_patterns = [
        r"report state\s*:\s*`?([a-z_ ]+)`?",
        r"state label\s*[:|]\s*`?([a-z_ ]+)`?",
        r"\|\s*state label\s*\|\s*`?([a-z_ ]+)`?",
    ]
    for pattern in explicit_patterns:
        match = re.search(pattern, head)
        if not match:
            continue
        label = match.group(1).strip().replace(" ", "_")
        if label in allowed:
            return label
    for label in ("scaffold", "needs_revision", "analyst_grade_candidate", "minimum_viable"):
        if label in lowered or label.replace("_", " ") in lowered:
            return label
    return "scaffold"


def _latest_report_version(session: Session, report_id: int) -> ResearchReportVersion | None:
    return (
        session.query(ResearchReportVersion)
        .filter_by(report_id=int(report_id), status="published")
        .order_by(ResearchReportVersion.version.desc(), ResearchReportVersion.id.desc())
        .first()
    )


def _snippet_around(text: str, query: str, *, size: int = 2600) -> str:
    body = str(text or "")
    needle = _text(query).lower()
    idx = body.lower().find(needle) if needle else -1
    if idx < 0:
        return body[:size]
    start = max(0, idx - size // 3)
    return body[start : min(len(body), start + size)].strip()


def _report_context_packet(session: Session, *, report_id: int | None = None, subject: str | None = None) -> dict[str, Any]:
    if report_id is not None:
        report = session.get(ResearchReport, int(report_id))
        if report is None:
            raise ValueError("Report not found.")
        state = get_research_report_state(session, subject=report)
        version = _latest_report_version(session, int(report.id))
    else:
        if not _text(subject):
            raise ValueError("report_id or subject is required.")
        state = get_research_report_state(session, subject=str(subject))
        raw_report_id = (state.get("report") or {}).get("id") if isinstance(state.get("report"), dict) else None
        report = session.get(ResearchReport, int(raw_report_id)) if raw_report_id else None
        version = _latest_report_version(session, int(raw_report_id)) if raw_report_id else None
    content = _text(version.content_md if version is not None else "")
    return {
        "kind": "report_context",
        "state": state,
        "report_id": int(report.id) if report is not None and report.id is not None else None,
        "version_id": int(version.id) if version is not None and version.id is not None else None,
        "content_excerpt": content[:100_000],
        "content_chars": len(content),
    }


def _report_search_context_packet(session: Session, *, query: str, limit: int = 8) -> dict[str, Any]:
    q = _text(query)
    if not q:
        return {"kind": "report_search", "query": q, "matches": []}
    rows = (
        session.query(ResearchReportVersion, ResearchReport)
        .join(ResearchReport, ResearchReport.id == ResearchReportVersion.report_id)
        .filter(ResearchReportVersion.status == "published")
        .filter(ResearchReportVersion.content_md.ilike(f"%{q}%"))
        .order_by(ResearchReportVersion.updated_at.desc(), ResearchReportVersion.id.desc())
        .limit(max(1, min(12, int(limit or 8))))
        .all()
    )
    return {
        "kind": "report_search",
        "query": q,
        "matches": [
            {
                "report_id": int(report.id),
                "title": report.title or report.slug,
                "slug": report.slug,
                "version": int(version.version or 0),
                "content_chars": len(version.content_md or ""),
                "snippet": _snippet_around(version.content_md or "", q),
            }
            for version, report in rows
        ],
    }


def _packet_md(label: str, packet: dict[str, Any]) -> str:
    if packet.get("kind") == "report_context":
        state = packet.get("state") if isinstance(packet.get("state"), dict) else {}
        report = state.get("report") if isinstance(state.get("report"), dict) else {}
        subject = state.get("subject") if isinstance(state.get("subject"), dict) else {}
        return (
            f"## Bottleneck Context Packet: {label}\n\n"
            f"- report_id: {packet.get('report_id') or report.get('id')}\n"
            f"- title: {report.get('title')}\n"
            f"- subject: {subject.get('display_name') or report.get('display_name')}\n"
            f"- content_chars: {packet.get('content_chars')}\n\n"
            f"### Existing Report / Research Memory\n\n{_text(packet.get('content_excerpt'))[:100_000]}"
        )
    if packet.get("kind") == "report_search":
        parts = [f"## Bottleneck Context Packet: {label}", "", f"- query: {packet.get('query')}"]
        for idx, match in enumerate(packet.get("matches") if isinstance(packet.get("matches"), list) else [], start=1):
            if not isinstance(match, dict):
                continue
            parts.extend(
                [
                    "",
                    f"### Match {idx}: {match.get('title')}",
                    f"- report_id: {match.get('report_id')}",
                    f"- slug: {match.get('slug')}",
                    "",
                    str(match.get("snippet") or "")[:2600],
                ]
            )
        return "\n".join(parts)
    return f"## Bottleneck Context Packet: {label}\n\n{packet}"


def start_bottleneck_constraint_goal_run(
    session: Session,
    *,
    objective: str | None = None,
    provider: str = "openrouter",
    model: str = "openrouter/free",
    query: str | None = None,
    report_id: int | None = None,
    subject: str | None = None,
    token_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    target = f"report_id={report_id}" if report_id is not None else f"subject={subject}" if subject else f"query={query}" if query else "operator objective"
    run = create_cloud_goal_run(
        session,
        task_kind="bottleneck_constraint",
        objective=objective or f"Build a bottleneck / constraint scaffold for {target}.",
        provider=provider,
        model=model,
        token_policy=token_policy,
    )
    if report_id is not None or _text(subject):
        packet = _report_context_packet(session, report_id=report_id, subject=subject)
        content = _packet_md("initial_report_context", packet)
        payload = {"tool": "report_context", "packet": packet}
    elif _text(query):
        packet = _report_search_context_packet(session, query=str(query))
        content = _packet_md("initial_search_context", packet)
        payload = {"tool": "report_search", "packet": packet}
    else:
        content = "No deterministic context was provided. Use the objective as the scope card input and return gaps if scope is unclear."
        payload = {"tool": "operator_objective"}
    append_cloud_goal_event(session, run, event_kind="tool_result", summary=f"Initial bottleneck context: {target}", content_md=content, payload=payload)
    run.current_context_md = "# Bottleneck / Constraint Working Context\n\nUse the initial context to build a scaffold, model plan, gaps, and next-pass tasks. Do not over-claim completion."
    session.flush()
    return serialize_cloud_goal_run(run, events=list_cloud_goal_events(session, int(run.id)))


def _model_step(session: Session, run: CloudGoalRun, rendered_context: str) -> dict[str, Any]:
    prompt = (
        BOTTLENECK_CONSTRAINT_SUPER_REPORT_PROMPT
        + "\n\nReturn markdown. Start with a title. If the report is not complete, make that explicit and provide the exact models, datasets, evidence cards, and instrument requests needed next."
    )
    return manifested_chat_text(
        session,
        run_kind="cloud_goal",
        run_id=int(run.id),
        task_kind=run.task_kind,
        messages=[ChatMessage(role="system", content=prompt), ChatMessage(role="user", content=rendered_context)],
        provider=run.provider,
        model=run.model,
        timeout_s=max(180.0, float(getattr(settings, "agent_llm_timeout_s", 60) or 60)),
        inbox_boundary="next_step",
        max_input_chars=None,
        max_output_tokens=None,
        prompt_cache_key="thoth-bottleneck-constraint-v1",
        permission_profile="cloud-goal-model-no-direct-tools",
    )


def step_bottleneck_constraint_goal_run(session: Session, run_id: int) -> dict[str, Any]:
    run = get_cloud_goal_run(session, int(run_id))
    if run is None:
        raise ValueError("Bottleneck / constraint run not found.")
    if run.status == CLOUD_GOAL_WAITING and not resume_cloud_goal_after_cooldown(
        session,
        run,
        summary="Bottleneck / constraint cooldown elapsed; retrying model step.",
    ):
        return serialize_cloud_goal_run(run, events=list_cloud_goal_events(session, int(run.id), limit=500))
    if run.status != CLOUD_GOAL_RUNNING:
        return serialize_cloud_goal_run(run, events=list_cloud_goal_events(session, int(run.id), limit=500))
    events = list_cloud_goal_events(session, int(run.id), limit=500)
    rendered = render_cloud_goal_context(run, events)
    should_compact, reason = should_compact_context(run, rendered)
    if should_compact:
        run.current_context_md = rendered[-120_000:]
        append_cloud_goal_event(session, run, event_kind="compaction", summary=reason, content_md=run.current_context_md)
        rendered = render_cloud_goal_context(run, list_cloud_goal_events(session, int(run.id), limit=500))
    stats = dict(run.stats_json or {})
    step_index = int(stats.get("steps") or 0) + 1
    session.flush()
    session.commit()
    try:
        result = _model_step(session, run, rendered)
    except Exception as exc:
        session.rollback()
        run = get_cloud_goal_run(session, int(run_id))
        if run is not None:
            if is_transient_cloud_model_failure(exc):
                mark_cloud_goal_model_cooldown(
                    session,
                    run,
                    exc,
                    step_index=step_index,
                    summary_prefix="Transient bottleneck / constraint model failure",
                )
                return serialize_cloud_goal_run(run, events=list_cloud_goal_events(session, int(run.id), limit=500))
            run.status = CLOUD_GOAL_FAILED
            run.error = str(exc)
            run.updated_at = utcnow()
            append_cloud_goal_event(session, run, event_kind="error", summary=str(exc), step_index=step_index)
            session.flush()
            return serialize_cloud_goal_run(run, events=list_cloud_goal_events(session, int(run.id), limit=500))
        raise
    run = get_cloud_goal_run(session, int(run_id))
    if run is None:
        raise ValueError("Bottleneck / constraint run not found after model call.")
    meta = llm_metadata(result)
    if meta.get("model"):
        run.routed_model = meta.get("model")
    stats = dict(run.stats_json or {})
    stats["steps"] = step_index
    stats = accumulate_llm_usage_stats(stats, meta)
    run.stats_json = stats
    body = _text(result.get("text"))
    final = normalize_bottleneck_constraint_result({"body_md": body})
    run.final_result = final
    run.status = CLOUD_GOAL_COMPLETE
    run.completed_at = utcnow()
    run.updated_at = utcnow()
    append_cloud_goal_event(session, run, event_kind="model_decision", summary=final.get("summary"), content_md=body[:120_000], payload={"llm": meta}, step_index=step_index)
    append_cloud_goal_event(session, run, event_kind="final", summary=final.get("summary"), payload={"final_result": final}, step_index=step_index)
    session.flush()
    return serialize_cloud_goal_run(run, events=list_cloud_goal_events(session, int(run.id), limit=500))


def continue_bottleneck_constraint_goal_run(session: Session, run_id: int, *, max_steps: int = 1) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for _idx in range(max(1, min(5, int(max_steps or 1)))):
        payload = step_bottleneck_constraint_goal_run(session, int(run_id))
        if _text(payload.get("status")) != CLOUD_GOAL_RUNNING:
            break
    return payload
