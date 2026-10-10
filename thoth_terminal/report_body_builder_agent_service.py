from __future__ import annotations

import json
import re
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
from thoth_terminal.models import CloudGoalRun, ResearchReport, ResearchReportVersion
from thoth_terminal.research_report_service import append_research_report_addendum, get_research_report_state


REPORT_BODY_BUILDER_SYSTEM = """Return json only.
You are Thoth Report Body Builder, a finance and technology research writer.
Your job is to turn Thoth report memory, source timelines, thesis ledgers, notes, and evidence packets into a real report body that a human analyst can read.

Use high intelligence and reasoning-capable model behavior. The work requires technical understanding, financial relevance, source discipline, and clean structure. Do not write generic filler.

Target first-pass success. If the provided context is substantial, write a complete one-shot report body rather than a thin draft. Use context requests only when a bounded lookup would materially change the answer; do not request context just to defer writing.

Thoth sourcing strategy:
- Thoth uses a selective analyst channel, not broad social-feed polling.
- Most source flow is institutional analyst research and institution-derived material; a smaller share is vetted specialist analyst commentary that often relays institution, supplier, customer, channel-check, or expert-network information second hand.
- Preserve this distinction. Do not flatten vetted analyst relay into "random social media."
- Label evidence tiers: primary/company/regulatory/customer data, institutional analyst synthesis, vetted specialist relay, unvetted social, and gap/speculation.
- For important pricing, qualification, roadmap, customer-allocation, supply-chain, and bottleneck claims, say what corroboration is still needed.

Write a body that is useful even when evidence is incomplete. Use clear caveats instead of refusing to write.

Core question:
Does this company control, enable, or sit close to the scarcest, least substitutable, most time-sensitive part of a value chain at the moment that chain is being forced to scale?

Classify the setup explicitly. Bottlenecks, supply crunches, and extreme demand are different and can coexist:
- Bottleneck: a constraint that limits system throughput or adoption because alternatives are scarce, qualification is deep, expansion is slow, or substitution is costly.
- Supply crunch: near-term shortage or tightness that may be cyclical, temporary, inventory-driven, allocation-driven, or capacity-driven.
- Extreme demand: downstream pull that can create pricing power, backlog, or urgency even when the constrained input is not structurally unique.
Separate bottleneck ownership, bottleneck exposure, and economic capture. A company may benefit from a bottleneck without owning it, and may own a technically critical node without capturing much economics.

Use evidence labels explicitly: Supported, Assumption, Missing, N/A, and N/I. Missing means the report should not pretend the fact is known. Assumption means an inference from available evidence or general technical knowledge. N/A means the axis does not apply. N/I means the axis applies in principle but is not important for this company right now.

Use this structure when applicable:
1. Abstract
2. One-Sentence Company Explanation
3. Core Thesis
4. Product / Capability Mechanics
5. Structural Transition
6. Value-Chain Role
7. Bottleneck / Supply Crunch / Extreme Demand Classification
8. Criticality, Substitutability, And Failure Cost
9. Capacity, Backlog, And Expansion Time
10. Roadmap: Public And Structural
11. Defensibility, IP Control, And Qualification Depth
12. Co-Design, Vertical Integration, And System Control
13. Pricing Power And Economic Capture
14. TAM / SAM / Constraint-Adjusted Opportunity
15. Multi-Path Optionality
16. Recognition Gap
17. Catalysts
18. M&A / Strategic Alternatives
19. Risks And Falsification
20. Evidence Quality
21. Open Questions / Missing Information

Technical depth requirements:
- Explain the actual products and capabilities. Define core terms.
- Explain what has to work physically, electrically, optically, chemically, thermally, mechanically, or in manufacturing.
- Do not avoid technical detail just because a dedicated product report could also explain it. Some repetition is acceptable when it improves the company report.
- Distinguish public roadmap from structural roadmap. Public roadmap is what the company or customers have said. Structural roadmap is what likely has to happen under technical, capacity, qualification, and customer constraints; mark it as Assumption when inferred.
- Do not hallucinate exact numbers. Use qualitative TAM/SAM and constraint-adjusted opportunity when numbers are missing.
- For AI serving economics, use Thoth dataset rails when present instead of inventing economics: `artificial-analysis-model-economics`, `ai-model-cost-quality-frontier`, `inferencex-upstream-benchmarks`, `ai-provider-api-pricing`, `ai-serving-scenario-assumptions`, and `ai-serving-margin-scenarios`. Label utilization, cache hit rate, price realization, and GPU capex as Assumption or Missing unless directly sourced.

Return exactly one json object:
{
  "action": "request_context|continue|finalize",
  "rationale": "short reason",
  "context_requests": [{"request_type":"report|search|company|question", "query":"...", "report_id": 123, "reason":"..."}],
  "working_context_md": "markdown update for durable working context",
  "final_result": {
    "summary": "",
    "title": "",
    "body_md": "",
    "evidence_tiers": [],
    "assumptions": [],
    "gaps": [],
    "recommended_checks": [],
    "persist_tags": []
  }
}
Use action=request_context when a bounded lookup would materially improve the body.
Use action=finalize when the body is useful enough as a draft addendum.
"""


SOURCE_STRATEGY_MD = """## Thoth Sourcing Strategy

Thoth's research intake is intentionally selective. The visible source surface can include social-channel material, but the system is not polling a general algorithmic feed. The normal source mix is roughly institutional analyst research and institution-derived material first, with a smaller share of vetted specialist analyst commentary that often relays information from institutions, suppliers, customers, channel checks, or expert networks.

When writing a report body, preserve provenance:
- primary/company/regulatory/customer data
- institutional analyst synthesis
- vetted specialist analyst relay
- unvetted social commentary
- gap/speculation

Do not dismiss vetted analyst relay only because it entered through a social surface. Do mark whether the claim has enough corroboration for the weight assigned to it.
"""


RESULT_KEYS = [
    "summary",
    "title",
    "body_md",
    "evidence_tiers",
    "assumptions",
    "gaps",
    "recommended_checks",
    "persist_tags",
]

REPORT_CONTEXT_CACHE_VERSION = "report-context-multipass-v1"
REPORT_CONTEXT_MULTIPASS_TASKS: tuple[tuple[str, str], ...] = (
    ("body_draft", "Write the main report body using the required structure where applicable."),
    ("evidence_gaps", "Identify weak assumptions, missing evidence, and recommended checks."),
    ("red_team_brief", "Produce a concise adversarial critique focused on causal logic, evidence quality, and falsification."),
)


def _text(value: object) -> str:
    return str(value or "").strip()


def normalize_report_body_result(value: dict[str, Any] | None) -> dict[str, Any]:
    payload = dict(value or {})
    out = {
        "summary": _text(payload.get("summary")),
        "title": _text(payload.get("title")) or "Report body draft",
        "body_md": _text(payload.get("body_md")),
    }
    for key in RESULT_KEYS:
        if key in out:
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


def _token_set(value: object) -> set[str]:
    text = _text(value).lower()
    return {part for part in re.findall(r"[a-z0-9][a-z0-9+.-]{2,}", text) if part not in {"report", "addendum", "technical", "technology", "company"}}


def _recent_source_pack_context(session: Session, *, target_text: str, limit: int = 3) -> list[dict[str, Any]]:
    target_terms = _token_set(target_text)
    if not target_terms:
        return []
    rows = (
        session.query(CloudGoalRun)
        .filter(CloudGoalRun.task_kind == "source_pack_builder")
        .filter(CloudGoalRun.status == CLOUD_GOAL_COMPLETE)
        .order_by(CloudGoalRun.completed_at.desc().nullslast(), CloudGoalRun.updated_at.desc(), CloudGoalRun.id.desc())
        .limit(20)
        .all()
    )
    ranked: list[tuple[int, CloudGoalRun, dict[str, Any]]] = []
    for row in rows:
        final = row.final_result if isinstance(row.final_result, dict) else {}
        haystack = "\n".join([row.objective or "", final.get("summary") or "", final.get("source_pack_md") or ""])
        score = len(target_terms & _token_set(haystack))
        if score <= 0:
            continue
        ranked.append((score, row, final))
    ranked.sort(key=lambda item: (item[0], item[1].completed_at or item[1].updated_at, item[1].id or 0), reverse=True)
    packs: list[dict[str, Any]] = []
    for score, row, final in ranked[: max(1, min(5, int(limit or 3)))]:
        packs.append(
            {
                "run_id": int(row.id),
                "objective": row.objective,
                "provider": row.provider,
                "requested_model": row.model,
                "routed_model": row.routed_model,
                "completed_at": row.completed_at.isoformat() if row.completed_at else None,
                "overlap_score": score,
                "summary": _text(final.get("summary")),
                "key_items": final.get("key_items") if isinstance(final.get("key_items"), list) else [],
                "claims_to_corroborate": final.get("claims_to_corroborate") if isinstance(final.get("claims_to_corroborate"), list) else [],
                "gaps": final.get("gaps") if isinstance(final.get("gaps"), list) else [],
                "recommended_next_searches": final.get("recommended_next_searches") if isinstance(final.get("recommended_next_searches"), list) else [],
                "source_pack_excerpt": _text(final.get("source_pack_md"))[:24_000],
            }
        )
    return packs


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
    body_deficit = len(content) < 20_000 or content.count("\n## ") < 4
    state_report = state.get("report") if isinstance(state.get("report"), dict) else {}
    state_subject = state.get("subject") if isinstance(state.get("subject"), dict) else {}
    target_text = " ".join(
        [
            _text(subject),
            _text(report.title if report is not None else ""),
            _text(report.slug if report is not None else ""),
            _text(state_report.get("title")),
            _text(state_subject.get("display_name")),
        ]
    )
    return {
        "kind": "report_context",
        "state": state,
        "report_id": int(report.id) if report is not None and report.id is not None else None,
        "version_id": int(version.id) if version is not None and version.id is not None else None,
        "content_excerpt": content[:90_000],
        "content_chars": len(content),
        "body_deficit": bool(body_deficit),
        "source_strategy": SOURCE_STRATEGY_MD,
        "recent_source_packs": _recent_source_pack_context(session, target_text=target_text),
    }


def _snippet_around(text: str, query: str, *, size: int = 1800) -> str:
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
    rows = (
        session.query(ResearchReportVersion, ResearchReport)
        .join(ResearchReport, ResearchReport.id == ResearchReportVersion.report_id)
        .filter(ResearchReportVersion.status == "published")
        .filter(ResearchReportVersion.content_md.ilike(f"%{q}%"))
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
        meta = {
            "report": report,
            "subject": subject,
            "coverage": state.get("coverage"),
            "freshness": state.get("freshness"),
            "content_chars": packet.get("content_chars"),
            "body_deficit": packet.get("body_deficit"),
        }
        return (
            f"## Report Body Builder Context Packet: {label}\n\n"
            f"### Target\n"
            f"- report_id: {packet.get('report_id') or report.get('id')}\n"
            f"- title: {report.get('title')}\n"
            f"- subject: {subject.get('display_name') or report.get('display_name')}\n"
            f"- content_chars: {packet.get('content_chars')}\n"
            f"- body_deficit: {packet.get('body_deficit')}\n\n"
            f"{SOURCE_STRATEGY_MD}\n\n"
            f"### Existing Report Memory / Markdown\n\n{_text(packet.get('content_excerpt'))[:90_000]}\n\n"
            f"### Recent Related Cloud Source Packs\n\n```json\n{json.dumps(packet.get('recent_source_packs') or [], ensure_ascii=False)}\n```\n\n"
            f"### Report Metadata JSON\n\n```json\n{json.dumps(meta, ensure_ascii=False)}\n```"
        )
    if packet.get("kind") == "report_search":
        parts = [
            f"## Report Body Builder Context Packet: {label}",
            "",
            "### Report Search Results",
            f"- query: {packet.get('query')}",
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
                    "",
                    str(match.get("snippet") or "")[:2400],
                ]
            )
        return "\n".join(parts)
    return f"## Report Body Builder Context Packet: {label}\n\n```json\n{json.dumps(packet, ensure_ascii=False)}\n```"


def start_report_body_builder_goal_run(
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
        task_kind="report_body_builder",
        objective=objective or f"Write a substantive report body draft for {target}.",
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
            summary=f"Initial report body context: {target}",
            content_md=_packet_md("initial_report_context", packet),
            payload={"tool": "report_context", "packet": packet},
        )
        run.current_context_md = (
            "# Report Body Builder Working Context\n\n"
            "No compaction has been needed. Build a readable draft body from the report memory and preserve provenance labels.\n\n"
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
        messages=[
            ChatMessage(role="system", content=REPORT_BODY_BUILDER_SYSTEM),
            ChatMessage(role="user", content=rendered_context),
        ],
        inbox_boundary="next_step",
        provider=run.provider,
        model=run.model,
        timeout_s=max(120.0, float(getattr(settings, "agent_llm_timeout_s", 60) or 60)),
        max_input_chars=None,
        max_output_tokens=None,
        prompt_cache_key="thoth-report-body-builder-v1",
        permission_profile="cloud-goal-model-no-direct-tools",
    )


def report_same_context_messages(report_context_md: str, *, task: str, task_key: str = "custom") -> list[ChatMessage]:
    stable_context = (
        f"# Thoth Report Cache Context\n"
        f"context_pack_version: {REPORT_CONTEXT_CACHE_VERSION}\n\n"
        "# Report Context Packet\n\n"
        f"{_text(report_context_md)}\n\n"
        "# Task\n\n"
    )
    return [
        ChatMessage(role="system", content=REPORT_BODY_BUILDER_SYSTEM),
        ChatMessage(role="user", content=f"{stable_context}task_key: {_text(task_key) or 'custom'}\n\n{_text(task)}"),
    ]


def _initial_report_context_from_events(events: list[Any]) -> str:
    for event in events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        if payload.get("tool") == "report_context" and event.content_md:
            return _text(event.content_md)
    return ""


def run_report_same_context_passes(
    session: Session,
    run_id: int,
    *,
    tasks: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    run = get_cloud_goal_run(session, int(run_id))
    if run is None:
        raise ValueError("Report Body Builder run not found.")
    events = list_cloud_goal_events(session, int(run.id), limit=500)
    report_context_md = _initial_report_context_from_events(events)
    if not report_context_md:
        raise ValueError("No initial report context packet is available for same-context passes.")
    task_rows = tasks or [{"task_key": key, "task": task} for key, task in REPORT_CONTEXT_MULTIPASS_TASKS]
    results: list[dict[str, Any]] = []
    stats = dict(run.stats_json or {})
    stats["context_pack_version"] = REPORT_CONTEXT_CACHE_VERSION
    stats["stable_prefix_chars"] = len(
        report_same_context_messages(report_context_md, task="", task_key="stable_prefix_probe")[1].content.rsplit("# Task", 1)[0]
    )
    session.flush()
    session.commit()
    for idx, item in enumerate(task_rows, start=1):
        task_key = _text(item.get("task_key")) or f"pass_{idx}"
        result = manifested_chat_json(
            session,
            run_kind="cloud_goal",
            run_id=int(run.id),
            task_kind=f"report_body_same_context:{task_key}",
            messages=report_same_context_messages(report_context_md, task=_text(item.get("task")), task_key=task_key),
            provider=run.provider,
            model=run.model,
            timeout_s=max(120.0, float(getattr(settings, "agent_llm_timeout_s", 60) or 60)),
            max_input_chars=None,
            max_output_tokens=None,
            prompt_cache_key=f"thoth-report-context-{REPORT_CONTEXT_CACHE_VERSION}",
            permission_profile="cloud-goal-model-no-direct-tools",
        )
        meta = llm_metadata(result)
        stats = accumulate_llm_usage_stats(stats, meta)
        results.append({"task_key": task_key, "result": result, "llm": meta})
    run = get_cloud_goal_run(session, int(run_id))
    if run is None:
        raise ValueError("Report Body Builder run disappeared.")
    run.stats_json = stats
    append_cloud_goal_event(
        session,
        run,
        event_kind="same_context_passes",
        summary=f"Ran {len(results)} report same-context cache-oriented pass(es).",
        payload={"context_pack_version": REPORT_CONTEXT_CACHE_VERSION, "results": results},
    )
    session.flush()
    return serialize_cloud_goal_run(run, events=list_cloud_goal_events(session, int(run.id), limit=500))


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


def persist_report_body_addendum(
    session: Session,
    *,
    run: CloudGoalRun,
    result: dict[str, Any],
    llm_meta: dict[str, str] | None = None,
    write_policy: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    body_md = _text(result.get("body_md"))
    if not body_md:
        return None
    events = list_cloud_goal_events(session, int(run.id), limit=50)
    report_id: int | None = None
    version_id: int | None = None
    for event in events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        packet = payload.get("packet") if isinstance(payload.get("packet"), dict) else {}
        if packet.get("kind") == "report_context":
            if packet.get("report_id"):
                report_id = int(packet["report_id"])
            if packet.get("version_id"):
                version_id = int(packet["version_id"])
            break
    if report_id is None:
        return None
    report = session.get(ResearchReport, int(report_id))
    if report is None:
        return None
    title = _text(result.get("title")) or "Cloud report body draft"
    tags = result.get("persist_tags") if isinstance(result.get("persist_tags"), list) else []
    tags = [*tags, "cloud-report-body-builder"]
    summary = _text(result.get("summary")) or "Cloud-generated report body draft."
    body = (
        f"{body_md.rstrip()}\n\n"
        "## Builder Provenance Notes\n\n"
        f"- cloud_goal_run_id: {int(run.id)}\n"
        f"- provider: {run.provider}\n"
        f"- requested_model: {run.model}\n"
        f"- routed_model: {run.routed_model or (llm_meta or {}).get('model') or ''}\n"
        f"- write_actor: {(write_policy or {}).get('actor') or 'cloud_agent'}\n"
        f"- openshell_ready: {(write_policy or {}).get('openshell_ready')}\n"
    )
    return append_research_report_addendum(
        session,
        subject=report,
        version_id=version_id,
        title=title,
        summary=summary,
        body_md=body,
        tags=tags,
        actor="cloud_report_body_builder",
    )


def step_report_body_builder_goal_run(
    session: Session,
    run_id: int,
    *,
    persist_addendum: bool = False,
    operator_approved: bool = False,
    max_context_requests: int = 3,
) -> dict[str, Any]:
    run = get_cloud_goal_run(session, int(run_id))
    if run is None:
        raise ValueError("Report Body Builder run not found.")
    if run.status == CLOUD_GOAL_WAITING and not resume_cloud_goal_after_cooldown(
        session,
        run,
        summary="Report Body Builder cooldown elapsed; retrying model step.",
    ):
        return serialize_cloud_goal_run(run, events=list_cloud_goal_events(session, int(run.id), limit=500))
    if run.status != CLOUD_GOAL_RUNNING:
        return serialize_cloud_goal_run(run, events=list_cloud_goal_events(session, int(run.id), limit=500))
    events = list_cloud_goal_events(session, int(run.id), limit=500)
    rendered = render_cloud_goal_context(run, events)
    should_compact, reason = should_compact_context(run, rendered)
    if should_compact:
        run.current_context_md = rendered[-100_000:]
        append_cloud_goal_event(session, run, event_kind="compaction", summary=reason, content_md=run.current_context_md)
        rendered = render_cloud_goal_context(run, list_cloud_goal_events(session, int(run.id), limit=500))
    stats = dict(run.stats_json or {})
    step_index = int(stats.get("steps") or 0) + 1
    # Long cloud calls can exceed Postgres idle-in-transaction limits. Flush and
    # close the current transaction before waiting on the provider, then resume
    # writes with the same session after the response.
    session.flush()
    session.commit()
    try:
        decision = _model_step(session, run, rendered)
    except Exception as exc:
        if is_transient_cloud_model_failure(exc):
            mark_cloud_goal_model_cooldown(
                session,
                run,
                exc,
                step_index=step_index,
                summary_prefix="Transient Report Body Builder model failure",
            )
            return serialize_cloud_goal_run(run, events=list_cloud_goal_events(session, int(run.id), limit=500))
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
    stats = accumulate_llm_usage_stats(stats, meta)
    run.stats_json = stats
    working = _text(decision.get("working_context_md"))
    if working:
        run.current_context_md = working
    append_cloud_goal_event(
        session,
        run,
        event_kind="model_decision",
        summary=_text(decision.get("rationale") or decision.get("action") or "report body builder decision"),
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
            summary="Model returned parseable JSON without the report body builder action schema.",
            payload={"decision": decision, "llm": meta},
            step_index=step_index,
        )
        run.updated_at = utcnow()
        session.flush()
        return serialize_cloud_goal_run(run, events=list_cloud_goal_events(session, int(run.id), limit=500))
    if action == "request_context" or requests:
        for req in requests[: max(1, min(8, int(max_context_requests or 3)))]:
            if not isinstance(req, dict):
                continue
            packet = _handle_context_request(session, req)
            append_cloud_goal_event(
                session,
                run,
                event_kind="tool_result",
                summary=f"Context request: {req.get('query') or req.get('report_id') or req.get('request_type')}",
                content_md=_packet_md(_text(req.get("query") or req.get("report_id") or "context"), packet),
                payload={"tool": "report_body_context", "request": req, "packet": packet},
                step_index=step_index,
            )
    elif action == "finalize":
        final = normalize_report_body_result(decision.get("final_result") if isinstance(decision.get("final_result"), dict) else decision)
        persisted = None
        write_policy = None
        if persist_addendum:
            write_policy = evaluate_cloud_write_permission(
                "report_addendum",
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
                persisted = persist_report_body_addendum(session, run=run, result=final, llm_meta=meta, write_policy=write_policy)
        run.final_result = final
        run.status = CLOUD_GOAL_COMPLETE
        run.completed_at = utcnow()
        append_cloud_goal_event(
            session,
            run,
            event_kind="final",
            summary=final.get("summary"),
            payload={"final_result": final, "persisted_addendum": persisted, "write_policy": write_policy},
            step_index=step_index,
        )
    run.updated_at = utcnow()
    session.flush()
    return serialize_cloud_goal_run(run, events=list_cloud_goal_events(session, int(run.id), limit=500))


def continue_report_body_builder_goal_run(
    session: Session,
    run_id: int,
    *,
    max_steps: int = 3,
    persist_addendum: bool = False,
    operator_approved: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for _idx in range(max(1, min(20, int(max_steps or 3)))):
        payload = step_report_body_builder_goal_run(
            session,
            int(run_id),
            persist_addendum=persist_addendum,
            operator_approved=operator_approved,
        )
        if _text(payload.get("status")) != CLOUD_GOAL_RUNNING:
            break
    return payload
