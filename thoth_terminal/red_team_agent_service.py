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
from thoth_terminal.instrument_service import create_instrument_request, serialize_instrument_request_summary
from thoth_terminal.models import CloudGoalRun, ResearchReport, ResearchReportVersion
from thoth_terminal.research_report_service import get_research_report_state


RED_TEAM_SYSTEM = """Return json only.
You are Thoth Red Team, an adversarial finance research reviewer.
Challenge reports, theses, company coverage, supply-chain claims, and bottleneck narratives.
Focus on bugs in reasoning: weak evidence, stale evidence, unsupported causal links, hidden assumptions, missing counterevidence, overconfident classifications, model fragility, and claims that need official/public corroboration.
Do not invent citations. Do not rewrite the report. Produce concrete review actions.
When a context packet includes a report body excerpt, critique that excerpt directly before asking for more context.

Thoth sourcing strategy:
- Thoth deliberately uses a selective analyst channel, not broad social-feed polling.
- Most source flow is institutional analyst research and institution-derived material; a smaller share is vetted specialist analyst commentary that often relays institution, supplier, customer, channel-check, or expert-network information second hand.
- Treat vetted analyst relay as potentially valuable evidence, not automatically as low-quality social chatter.
- Still grade provenance explicitly: primary filing/company disclosure/customer evidence > institutional analyst research > vetted specialist relay > unvetted social commentary.
- For market-moving claims, pricing claims, qualification claims, supply-chain claims, and bottleneck claims, ask what would corroborate the relay rather than dismissing it only because the visible surface is social.
- Single-author quantitative claims from a named Substack or vetted specialist source are admissible attributed evidence. Treat lack of independent corroboration as a corroboration gap; their single-author status alone is not a critical finding and must not automatically trigger revision or quarantine.
- Put a properly attributed, internally coherent single-author number in weak_evidence, recommended_tests, or a later-verification request when corroboration would improve confidence. Escalate it to critical_findings only when the report strips attribution, contradicts stronger evidence, contains an internal or physical inconsistency, materially misstates the source, or makes a high-confidence conclusion depend on treating the claim as independently verified.
- Keep attribution and truth separate: cite what the author claimed, preserve the exact source boundary, and challenge any stronger conclusion the report draws from it.

Return exactly one json object:
{
  "action": "request_context|continue|finalize",
  "rationale": "short reason",
  "context_requests": [{"request_type":"report|search|company|question", "query":"...", "report_id": 123, "reason":"..."}],
  "working_context_md": "markdown update for the durable working context",
  "final_result": {
    "summary": "",
    "critical_findings": [],
    "weak_evidence": [],
    "missing_counterevidence": [],
    "stale_or_suspect_material": [],
    "hidden_assumptions": [],
    "recommended_tests": [],
    "draft_requests": [],
    "next_actions": []
  }
}
Use action=request_context when more bounded context is needed.
Use action=finalize only when the critique is useful enough for review.
"""


RED_TEAM_KEYS = [
    "summary",
    "critical_findings",
    "weak_evidence",
    "missing_counterevidence",
    "stale_or_suspect_material",
    "hidden_assumptions",
    "recommended_tests",
    "draft_requests",
    "next_actions",
]


SOURCE_STRATEGY_MD = """## Thoth Sourcing Strategy

Thoth's research intake is intentionally selective. The visible source surface can include social-channel material, but that does not mean the system is polling a general algorithmic feed. The normal source mix is roughly institutional analyst research and institution-derived material first, with a smaller share of vetted specialist analyst commentary that often relays information from institutions, suppliers, customers, channel checks, or expert networks.

For red-team work, do not treat every social-looking item as raw social chatter. Grade provenance by level:
- primary filing, company disclosure, customer disclosure, regulator data, or direct dataset
- institutional analyst research or institution-derived material
- vetted specialist analyst relay
- unvetted social commentary

The right critique is usually not \"this came through social, therefore ignore it.\" The right critique is whether an important relay claim has enough corroboration for its weight in the report, especially for pricing, qualification, customer allocation, capacity, supply-chain, roadmap, and bottleneck claims.

Single-author quantitative claims from a named Substack or vetted specialist source remain usable when the report attributes them explicitly. Lack of a second source is a corroboration gap, not by itself a material defect. Record such claims as later-verification items when useful. Treat them as material objections only when attribution is lost, stronger evidence conflicts, the claim is internally or physically inconsistent, the source is materially misrepresented, or the report relies on the number as independently verified support for a high-confidence conclusion.
"""


def _text(value: object) -> str:
    return str(value or "").strip()


def normalize_red_team_result(value: dict[str, Any] | None) -> dict[str, Any]:
    payload = dict(value or {})
    out: dict[str, Any] = {"summary": _text(payload.get("summary") or payload.get("coverage_summary"))}
    for key in RED_TEAM_KEYS:
        if key == "summary":
            continue
        raw = payload.get(key)
        out[key] = raw if isinstance(raw, list) else []
    return out


def _latest_report_version(session: Session, report_id: int) -> ResearchReportVersion | None:
    return (
        session.query(ResearchReportVersion)
        .filter_by(report_id=int(report_id), status="published")
        .order_by(ResearchReportVersion.version.desc(), ResearchReportVersion.id.desc())
        .first()
    )


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
        version = _latest_report_version(session, int(raw_report_id)) if raw_report_id else None
    content = _text(version.content_md if version is not None else "")
    return {
        "kind": "report_context",
        "state": state,
        "content_excerpt": content[:60_000],
        "content_chars": len(content),
    }


def _snippet_around(text: str, query: str, *, size: int = 1400) -> str:
    body = str(text or "")
    needle = _text(query).lower()
    idx = body.lower().find(needle) if needle else -1
    if idx < 0:
        return body[:size]
    start = max(0, idx - size // 3)
    end = min(len(body), start + size)
    return body[start:end].strip()


def _report_search_context_packet(session: Session, *, query: str, limit: int = 6) -> dict[str, Any]:
    q = _text(query)
    if not q:
        return {"kind": "report_search", "query": q, "matches": []}
    pattern = f"%{q}%"
    rows = (
        session.query(ResearchReportVersion, ResearchReport)
        .join(ResearchReport, ResearchReport.id == ResearchReportVersion.report_id)
        .filter(ResearchReportVersion.status == "published")
        .filter(ResearchReportVersion.content_md.ilike(pattern))
        .order_by(ResearchReportVersion.updated_at.desc(), ResearchReportVersion.id.desc())
        .limit(max(1, min(12, int(limit or 6))))
        .all()
    )
    matches = []
    for version, report in rows:
        matches.append(
            {
                "report_id": int(report.id),
                "title": report.title or report.slug,
                "slug": report.slug,
                "version": int(version.version or 0),
                "content_chars": len(version.content_md or ""),
                "snippet": _snippet_around(version.content_md or "", q),
            }
        )
    return {"kind": "report_search", "query": q, "matches": matches}


def _packet_md(label: str, packet: dict[str, Any]) -> str:
    if packet.get("kind") == "report_context":
        state = packet.get("state") if isinstance(packet.get("state"), dict) else {}
        report = state.get("report") if isinstance(state.get("report"), dict) else {}
        subject = state.get("subject") if isinstance(state.get("subject"), dict) else {}
        excerpt = _text(packet.get("content_excerpt"))
        meta = {
            "report": report,
            "subject": subject,
            "coverage": state.get("coverage"),
            "freshness": state.get("freshness"),
            "content_chars": packet.get("content_chars"),
        }
        return (
            f"## Red Team Context Packet: {label}\n\n"
            f"### Report\n"
            f"- report_id: {report.get('id')}\n"
            f"- title: {report.get('title')}\n"
            f"- subject: {subject.get('display_name') or report.get('display_name')}\n"
            f"- freshness: {state.get('freshness')}\n"
            f"- content_chars: {packet.get('content_chars')}\n\n"
            f"### Report Body Excerpt\n\n{excerpt[:80_000]}\n\n"
            f"### Report Metadata JSON\n\n```json\n{json.dumps(meta, ensure_ascii=False)}\n```"
        )
    if packet.get("kind") == "report_search":
        parts = [
            f"## Red Team Context Packet: {label}",
            "",
            f"### Report Search Results",
            f"- query: {packet.get('query')}",
            f"- matches: {len(packet.get('matches') if isinstance(packet.get('matches'), list) else [])}",
        ]
        for idx, match in enumerate(packet.get("matches") if isinstance(packet.get("matches"), list) else [], start=1):
            if not isinstance(match, dict):
                continue
            parts.extend(
                [
                    "",
                    f"### Match {idx}: {match.get('title')}",
                    f"- report_id: {match.get('report_id')}",
                    f"- slug: {match.get('slug')}",
                    f"- content_chars: {match.get('content_chars')}",
                    "",
                    str(match.get("snippet") or "")[:2200],
                ]
            )
        return "\n".join(parts)
    return f"## Red Team Context Packet: {label}\n\n```json\n{json.dumps(packet, ensure_ascii=False)}\n```"


def start_red_team_goal_run(
    session: Session,
    *,
    objective: str | None = None,
    provider: str = "openrouter",
    model: str = "openrouter/free",
    report_id: int | None = None,
    subject: str | None = None,
    token_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    target = f"report_id={report_id}" if report_id is not None else f"subject={subject}" if subject else "operator objective"
    run = create_cloud_goal_run(
        session,
        task_kind="red_team",
        objective=objective or f"Red-team the selected Thoth research object: {target}.",
        provider=provider,
        model=model,
        token_policy=token_policy,
    )
    if report_id is not None or _text(subject):
        packet = _report_context_packet(session, report_id=report_id, subject=subject)
        append_cloud_goal_event(
            session,
            run,
            event_kind="tool_result",
            summary=f"Initial red-team report context: {target}",
            content_md=_packet_md("initial_report_context", packet),
            payload={"tool": "report_context", "packet": packet},
        )
        run.current_context_md = (
            "# Red Team Working Context\n\n"
            "No compaction has been needed. Review the report context packet and challenge its claims.\n\n"
            f"{SOURCE_STRATEGY_MD}"
        )
    session.flush()
    return serialize_cloud_goal_run(run, events=list_cloud_goal_events(session, int(run.id)))


def _model_step(session: Session, run: CloudGoalRun, rendered_context: str) -> dict[str, Any]:
    return manifested_chat_json(
        session,
        run_kind="cloud_goal",
        run_id=int(run.id),
        task_kind=run.task_kind,
        messages=[ChatMessage(role="system", content=RED_TEAM_SYSTEM), ChatMessage(role="user", content=rendered_context)],
        inbox_boundary="next_step",
        provider=run.provider,
        model=run.model,
        timeout_s=float(getattr(settings, "agent_llm_timeout_s", 60) or 60),
        max_input_chars=None,
        max_output_tokens=None,
        prompt_cache_key="thoth-red-team-v1",
        permission_profile="cloud-goal-model-no-direct-tools",
    )


def _handle_context_request(session: Session, req: dict[str, Any]) -> dict[str, Any]:
    request_type = _text(req.get("request_type") or req.get("type")).lower()
    if request_type == "report" or req.get("report_id"):
        report_id = req.get("report_id")
        subject = _text(req.get("query") or req.get("subject"))
        return _report_context_packet(session, report_id=int(report_id) if report_id else None, subject=subject or None)
    query = _text(req.get("query") or req.get("search") or req.get("topic") or req.get("subject"))
    if query:
        return _report_search_context_packet(session, query=query)
    return {"kind": "unsupported_context_request", "request": dict(req)}


def persist_red_team_requests(
    session: Session,
    *,
    run: CloudGoalRun,
    result: dict[str, Any],
    llm_meta: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    persisted: list[dict[str, Any]] = []
    candidates = []
    for key in ("critical_findings", "weak_evidence", "missing_counterevidence", "stale_or_suspect_material", "hidden_assumptions", "draft_requests"):
        rows = result.get(key)
        if isinstance(rows, list):
            for row in rows[:10]:
                candidates.append((key, row))
    for kind, item in candidates[:30]:
        if isinstance(item, dict):
            title_part = _text(item.get("claim") or item.get("title") or item.get("topic") or item.get("summary") or kind)
            summary = _text(item.get("reason") or item.get("rationale") or item.get("summary") or item)
        else:
            title_part = _text(item)[:120] or kind
            summary = _text(item)
        request = create_instrument_request(
            session,
            title=f"Red Team: {title_part}"[:220],
            intent="red_team_review",
            summary=summary,
            desired_outputs=["counterevidence_review", "research_gap"],
            provenance_summary=f"Red Team cloud goal run {run.id}",
            status="draft",
            priority=3 if kind == "critical_findings" else 5,
            automation_source="red_team",
            source_kind=kind,
            draft_payload={"candidate": item if isinstance(item, dict) else {"text": item}, "result_summary": result.get("summary"), "llm": dict(llm_meta or {})},
            linked_objects={"cloud_goal_run_id": int(run.id), "task_kind": "red_team"},
            request_key=f"red_team:{run.id}:{kind}:{len(persisted)}",
        )
        persisted.append(serialize_instrument_request_summary(request))
    return persisted


def step_red_team_goal_run(
    session: Session,
    run_id: int,
    *,
    persist_candidates: bool = False,
    operator_approved: bool = False,
    max_context_requests: int = 2,
) -> dict[str, Any]:
    run = get_cloud_goal_run(session, int(run_id))
    if run is None:
        raise ValueError("Red Team run not found.")
    if run.status == CLOUD_GOAL_WAITING and not resume_cloud_goal_after_cooldown(
        session,
        run,
        summary="Red Team cooldown elapsed; retrying model step.",
    ):
        return serialize_cloud_goal_run(run, events=list_cloud_goal_events(session, int(run.id), limit=400))
    if run.status != CLOUD_GOAL_RUNNING:
        return serialize_cloud_goal_run(run, events=list_cloud_goal_events(session, int(run.id), limit=400))
    events = list_cloud_goal_events(session, int(run.id), limit=400)
    rendered = render_cloud_goal_context(run, events)
    should_compact, reason = should_compact_context(run, rendered)
    if should_compact:
        run.current_context_md = rendered[-80_000:]
        append_cloud_goal_event(session, run, event_kind="compaction", summary=reason, content_md=run.current_context_md)
    stats = dict(run.stats_json or {})
    step_index = int(stats.get("steps") or 0) + 1
    try:
        decision = _model_step(session, run, render_cloud_goal_context(run, list_cloud_goal_events(session, int(run.id), limit=400)))
    except Exception as exc:
        if is_transient_cloud_model_failure(exc):
            mark_cloud_goal_model_cooldown(
                session,
                run,
                exc,
                step_index=step_index,
                summary_prefix="Transient Red Team model failure",
            )
            return serialize_cloud_goal_run(run, events=list_cloud_goal_events(session, int(run.id), limit=400))
        run.status = CLOUD_GOAL_FAILED
        run.error = str(exc)
        run.updated_at = utcnow()
        append_cloud_goal_event(session, run, event_kind="error", summary=str(exc), step_index=step_index)
        session.flush()
        return serialize_cloud_goal_run(run, events=list_cloud_goal_events(session, int(run.id), limit=400))
    meta = llm_metadata(decision)
    if meta.get("model"):
        run.routed_model = meta.get("model")
    stats["steps"] = step_index
    stats = accumulate_llm_usage_stats(stats, meta, include_reasoning=False)
    run.stats_json = stats
    working = _text(decision.get("working_context_md"))
    if working:
        run.current_context_md = working
    append_cloud_goal_event(
        session,
        run,
        event_kind="model_decision",
        summary=_text(decision.get("rationale") or decision.get("action") or "red-team decision"),
        content_md=working or None,
        payload={"decision": decision, "llm": meta},
        step_index=step_index,
    )
    requests = decision.get("context_requests")
    if not isinstance(requests, list):
        requests = []
    action = _text(decision.get("action")).lower() or "continue"
    if "action" not in decision and "final_result" not in decision:
        append_cloud_goal_event(
            session,
            run,
            event_kind="model_malformed",
            summary="Model returned parseable JSON without the red-team action schema.",
            payload={"decision": decision, "llm": meta},
            step_index=step_index,
        )
        run.updated_at = utcnow()
        session.flush()
        return serialize_cloud_goal_run(run, events=list_cloud_goal_events(session, int(run.id), limit=500))
    if action == "request_context" or requests:
        for req in requests[: max(1, min(8, int(max_context_requests or 2)))]:
            if not isinstance(req, dict):
                continue
            packet = _handle_context_request(session, req)
            append_cloud_goal_event(
                session,
                run,
                event_kind="tool_result",
                summary=f"Context request: {req.get('query') or req.get('report_id') or req.get('request_type')}",
                content_md=_packet_md(_text(req.get("query") or req.get("report_id") or "context"), packet),
                payload={"tool": "red_team_context", "request": req, "packet": packet},
                step_index=step_index,
            )
    elif action == "finalize":
        final = normalize_red_team_result(decision.get("final_result") if isinstance(decision.get("final_result"), dict) else decision)
        persisted = []
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
                persisted = persist_red_team_requests(session, run=run, result=final, llm_meta=meta)
        run.final_result = final
        run.status = CLOUD_GOAL_COMPLETE
        run.completed_at = utcnow()
        append_cloud_goal_event(
            session,
            run,
            event_kind="final",
            summary=final.get("summary"),
            payload={"final_result": final, "persisted_candidates": persisted, "write_policy": write_policy},
            step_index=step_index,
        )
    run.updated_at = utcnow()
    session.flush()
    return serialize_cloud_goal_run(run, events=list_cloud_goal_events(session, int(run.id), limit=500))


def continue_red_team_goal_run(
    session: Session,
    run_id: int,
    *,
    max_steps: int = 3,
    persist_candidates: bool = False,
    operator_approved: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for _idx in range(max(1, min(20, int(max_steps or 3)))):
        payload = step_red_team_goal_run(
            session,
            int(run_id),
            persist_candidates=persist_candidates,
            operator_approved=operator_approved,
        )
        if _text(payload.get("status")) != CLOUD_GOAL_RUNNING:
            break
    return payload
