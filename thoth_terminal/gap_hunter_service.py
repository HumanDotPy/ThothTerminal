from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from thoth_terminal.agent.llm_client import ChatMessage, llm_metadata
from thoth_terminal.agent_request_manifest_service import manifested_chat_json
from thoth_terminal.cloud_write_policy import evaluate_cloud_write_permission
from thoth_terminal.config import settings
from thoth_terminal.instrument_service import create_instrument_request, serialize_instrument_request_summary
from thoth_terminal.models import (
    ResearchInstrument,
    ResearchReport,
    ResearchReportQuality,
    ResearchReportVersion,
    ResearchSubject,
    ResearchUniverse,
    ResearchUniverseRow,
    ResearchUniverseSection,
)


_THIN_REPORT_CHAR_LIMIT = 3500
GAP_HUNTER_RESULT_KEYS = [
    "coverage_summary",
    "missing_company_reports",
    "missing_product_explainers",
    "technical_definition_gaps",
    "super_report_candidates",
    "bottleneck_candidates",
    "demotion_candidates",
    "followup_context_requests",
    "next_best_actions",
]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _age_days(value: datetime | None) -> int | None:
    if value is None:
        return None
    return max(0, int((_utcnow() - value.replace(tzinfo=None)).total_seconds() // 86400))


def _text(value: object) -> str:
    return str(value or "").strip()


def _terms(keyword: str | None) -> list[str]:
    raw = _text(keyword)
    if not raw:
        return []
    return [part for part in raw.replace(",", " ").split() if part]


def _like(value: str) -> str:
    return f"%{value}%"


def _match_filters(terms: list[str]) -> list[Any]:
    filters: list[Any] = []
    for term in terms:
        needle = _like(term)
        filters.append(
            or_(
                ResearchUniverse.name.ilike(needle),
                ResearchUniverse.slug.ilike(needle),
                ResearchUniverseSection.name.ilike(needle),
                ResearchUniverseSection.section_key.ilike(needle),
                ResearchUniverseRow.display_label.ilike(needle),
                ResearchUniverseRow.role.ilike(needle),
                ResearchUniverseRow.notes.ilike(needle),
                ResearchInstrument.display_name.ilike(needle),
                ResearchInstrument.ticker.ilike(needle),
                ResearchInstrument.yahoo_symbol.ilike(needle),
            )
        )
    return filters


def _base_row_query(session: Session):
    return (
        session.query(ResearchUniverseRow, ResearchUniverseSection, ResearchUniverse, ResearchInstrument)
        .join(ResearchUniverseSection, ResearchUniverseSection.id == ResearchUniverseRow.section_id)
        .join(ResearchUniverse, ResearchUniverse.id == ResearchUniverseRow.universe_id)
        .join(ResearchInstrument, ResearchInstrument.id == ResearchUniverseRow.instrument_id)
        .filter(ResearchUniverse.status == "active", ResearchUniverseRow.archived_at.is_(None))
    )


def _report_meta_for_rows(
    session: Session,
    rows: list[tuple[ResearchUniverseRow, ResearchUniverseSection, ResearchUniverse, ResearchInstrument]],
) -> dict[int, dict[str, Any]]:
    if not rows:
        return {}
    issuer_ids = {int(instrument.issuer_id) for _row, _section, _universe, instrument in rows if instrument.issuer_id}
    company_ids = {int(instrument.company_id) for _row, _section, _universe, instrument in rows if instrument.company_id}
    reports: list[ResearchReport] = []
    if issuer_ids or company_ids:
        filters = []
        if issuer_ids:
            filters.append(ResearchReport.issuer_id.in_(issuer_ids))
        if company_ids:
            filters.append(ResearchReport.company_id.in_(company_ids))
        reports = session.query(ResearchReport).filter(ResearchReport.status == "active", or_(*filters)).all()

    report_by_issuer = {int(report.issuer_id): report for report in reports if report.issuer_id}
    report_by_company = {int(report.company_id): report for report in reports if report.company_id}
    report_ids = [int(report.id) for report in reports]

    subjects: list[ResearchSubject] = []
    if issuer_ids or company_ids:
        filters = []
        if issuer_ids:
            filters.append(ResearchSubject.issuer_id.in_(issuer_ids))
        if company_ids:
            filters.append(ResearchSubject.company_id.in_(company_ids))
        subjects = session.query(ResearchSubject).filter(ResearchSubject.status == "active", or_(*filters)).all()
    subject_by_issuer = {int(subject.issuer_id): subject for subject in subjects if subject.issuer_id}
    subject_by_company = {int(subject.company_id): subject for subject in subjects if subject.company_id}
    subject_ids = [int(subject.id) for subject in subjects]

    quality_by_subject: dict[int, ResearchReportQuality] = {}
    if subject_ids:
        for quality in session.query(ResearchReportQuality).filter(ResearchReportQuality.subject_id.in_(subject_ids)).all():
            quality_by_subject[int(quality.subject_id)] = quality

    versions_by_report: dict[int, ResearchReportVersion] = {}
    if report_ids:
        versions = (
            session.query(ResearchReportVersion)
            .filter(ResearchReportVersion.report_id.in_(report_ids))
            .filter(ResearchReportVersion.status == "published")
            .order_by(
                ResearchReportVersion.report_id.asc(),
                ResearchReportVersion.version.desc(),
                ResearchReportVersion.id.desc(),
            )
            .all()
        )
        for version in versions:
            versions_by_report.setdefault(int(version.report_id), version)

    out: dict[int, dict[str, Any]] = {}
    for row, _section, _universe, inst in rows:
        report = None
        subject = None
        if inst.issuer_id:
            report = report_by_issuer.get(int(inst.issuer_id))
            subject = subject_by_issuer.get(int(inst.issuer_id))
        if report is None and inst.company_id:
            report = report_by_company.get(int(inst.company_id))
        if subject is None and inst.company_id:
            subject = subject_by_company.get(int(inst.company_id))
        version = versions_by_report.get(int(report.id)) if report is not None else None
        quality = quality_by_subject.get(int(subject.id)) if subject is not None else None
        body_chars = len(_text(version.content_md)) if version is not None else 0
        state = _text(quality.state) if quality is not None else ("unknown" if report is not None else "missing")
        suspect = bool(report is not None and ((body_chars and body_chars < _THIN_REPORT_CHAR_LIMIT) or state in {"missing", "thin", "poor"}))
        out[int(row.id)] = {
            "has_company_report": report is not None,
            "report_id": int(report.id) if report is not None else None,
            "report_title": report.title if report is not None else None,
            "report_quality": state,
            "report_quality_score": int(quality.score) if quality is not None else None,
            "report_age_days": _age_days(version.published_at or version.updated_at) if version is not None else None,
            "automated_report_suspect": suspect,
            "latest_report_body_chars": body_chars or None,
        }
    return out


def _row_payload(
    row: ResearchUniverseRow,
    section: ResearchUniverseSection,
    universe: ResearchUniverse,
    instrument: ResearchInstrument,
    report_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    meta = report_meta or {}
    return {
        "universe_id": int(universe.id),
        "universe_slug": universe.slug,
        "universe_name": universe.name,
        "section_id": int(section.id),
        "section_key": section.section_key,
        "section_name": section.name,
        "row_id": int(row.id),
        "instrument_id": int(instrument.id),
        "company": row.display_label or instrument.display_name,
        "ticker": instrument.ticker or instrument.yahoo_symbol,
        "role": row.role,
        "exposure_type": row.exposure_type,
        "exposure_strength": row.exposure_strength,
        "conviction": row.conviction,
        "note": row.notes,
        "issuer_id": int(instrument.issuer_id) if instrument.issuer_id else None,
        "company_id": int(instrument.company_id) if instrument.company_id else None,
        **meta,
    }


def universe_coverage_overview(session: Session) -> dict[str, Any]:
    universes = (
        session.query(ResearchUniverse)
        .filter(ResearchUniverse.status == "active")
        .order_by(ResearchUniverse.sort_order.asc(), ResearchUniverse.name.asc(), ResearchUniverse.id.asc())
        .all()
    )
    universe_ids = [int(row.id) for row in universes]
    sections = []
    if universe_ids:
        sections = (
            session.query(ResearchUniverseSection)
            .filter(ResearchUniverseSection.universe_id.in_(universe_ids))
            .order_by(
                ResearchUniverseSection.universe_id.asc(),
                ResearchUniverseSection.sort_order.asc(),
                ResearchUniverseSection.id.asc(),
            )
            .all()
        )
    counts = {
        int(section_id): int(count)
        for section_id, count in (
            session.query(ResearchUniverseRow.section_id, func.count(ResearchUniverseRow.id))
            .filter(ResearchUniverseRow.archived_at.is_(None))
            .group_by(ResearchUniverseRow.section_id)
            .all()
        )
    }
    sections_by_universe: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for section in sections:
        sections_by_universe[int(section.universe_id)].append(
            {
                "section_id": int(section.id),
                "section_key": section.section_key,
                "section_name": section.name,
                "description": section.description,
                "row_count": counts.get(int(section.id), 0),
            }
        )
    items = []
    for universe in universes:
        universe_sections = sections_by_universe.get(int(universe.id), [])
        items.append(
            {
                "universe_id": int(universe.id),
                "universe_slug": universe.slug,
                "universe_name": universe.name,
                "description": universe.description,
                "section_count": len(universe_sections),
                "row_count": sum(int(section["row_count"]) for section in universe_sections),
                "sections": universe_sections,
            }
        )
    return {
        "mode": "overview",
        "context_policy": "section_only_first_pass",
        "items": items,
        "row_count": sum(int(item["row_count"]) for item in items),
    }


def universe_coverage_drilldown(
    session: Session,
    *,
    keyword: str | None = None,
    universe_id: int | None = None,
    section_id: int | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    terms = _terms(keyword)
    q = _base_row_query(session)
    if universe_id:
        q = q.filter(ResearchUniverseRow.universe_id == int(universe_id))
    if section_id:
        q = q.filter(ResearchUniverseRow.section_id == int(section_id))
    for clause in _match_filters(terms):
        q = q.filter(clause)
    q = q.order_by(
        ResearchUniverse.sort_order.asc(),
        ResearchUniverseSection.sort_order.asc(),
        ResearchUniverseRow.sort_order.asc(),
        ResearchUniverseRow.id.asc(),
    ).limit(max(1, min(500, int(limit or 200))))
    results = q.all()
    report_meta = _report_meta_for_rows(session, results)
    rows = [
        _row_payload(row, section, universe, instrument, report_meta.get(int(row.id)))
        for row, section, universe, instrument in results
    ]
    groups: dict[str, dict[str, Any]] = {}
    for item in rows:
        key = f"{item['universe_id']}:{item['section_id']}"
        group = groups.setdefault(
            key,
            {
                "universe_id": item["universe_id"],
                "universe_slug": item["universe_slug"],
                "universe_name": item["universe_name"],
                "section_id": item["section_id"],
                "section_key": item["section_key"],
                "section_name": item["section_name"],
                "match_count": 0,
            },
        )
        group["match_count"] += 1
    return {
        "mode": "drilldown",
        "keyword": _text(keyword) or None,
        "universe_id": universe_id,
        "section_id": section_id,
        "context_policy": "row_detail_only_after_keyword_or_section",
        "groups": list(groups.values()),
        "rows": rows,
        "row_count": len(rows),
        "limit": max(1, min(500, int(limit or 200))),
    }


def universe_coverage_index(
    session: Session,
    *,
    keyword: str | None = None,
    universe_id: int | None = None,
    section_id: int | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    if _terms(keyword) or section_id:
        return universe_coverage_drilldown(
            session,
            keyword=keyword,
            universe_id=universe_id,
            section_id=section_id,
            limit=limit,
        )
    return universe_coverage_overview(session)


def gap_hunter_system_prompt() -> str:
    return (
        "Return json only. You are Thoth Gap Hunter, a finance research coverage auditor. "
        "Your job is to inspect the provided universe coverage packet and identify what Thoth does not yet "
        "understand well enough. Do not write state. Do not claim searches were performed unless the packet "
        "contains that evidence. Be inquisitive, but express next steps as structured followup context requests. "
        "Prefer super-report or product-primer recommendations when the missing thing is domain mechanics, "
        "technical definitions, public roadmap, or product architecture rather than a proven bottleneck. "
        "Return exactly one json object with these keys: coverage_summary string; missing_company_reports array; "
        "missing_product_explainers array; technical_definition_gaps array; super_report_candidates array; "
        "bottleneck_candidates array; demotion_candidates array; followup_context_requests array; "
        "next_best_actions array. Use empty arrays when a category has no candidates."
    )


def gap_hunter_user_prompt(*, index_payload: dict[str, Any], question: str | None = None) -> str:
    question_v = _text(question) or "What is missing before this universe coverage is useful for finance research?"
    return (
        "Analyze this Gap Hunter universe coverage packet.\n\n"
        f"Operator question: {question_v}\n\n"
        "Required json keys:\n"
        "- coverage_summary: string\n"
        "- missing_company_reports: array of objects\n"
        "- missing_product_explainers: array of objects\n"
        "- technical_definition_gaps: array of objects\n"
        "- super_report_candidates: array of objects\n"
        "- bottleneck_candidates: array of objects\n"
        "- demotion_candidates: array of objects\n"
        "- followup_context_requests: array of objects with request_type, query or ids, and reason\n"
        "- next_best_actions: array of short strings\n\n"
        "Rules:\n"
        "- If the packet is mode=overview, stay at universe/section level and ask for drilldowns instead of pretending to know company details.\n"
        "- If the packet is mode=drilldown, use company/role/note/report metadata to identify concrete gaps.\n"
        "- Treat missing, thin, poor, unknown, or automated_report_suspect reports as coverage risks.\n"
        "- Separate missing definitions from missing company reports.\n"
        "- Do not invent source citations.\n\n"
        f"Coverage packet json:\n{index_payload}\n\n"
        "Return exactly the required json object now, with all required keys present."
    )


def normalize_gap_hunter_result(value: dict[str, Any] | None) -> dict[str, Any]:
    payload = dict(value or {})
    out: dict[str, Any] = {}
    out["coverage_summary"] = _text(payload.get("coverage_summary") or payload.get("summary"))
    for key in GAP_HUNTER_RESULT_KEYS:
        if key == "coverage_summary":
            continue
        raw = payload.get(key)
        out[key] = raw if isinstance(raw, list) else []
    return out


def _usage_from_llm_meta(meta: dict[str, str]) -> dict[str, int]:
    usage = {
        key: int(meta[key])
        for key in ("input_tokens", "output_tokens", "total_tokens", "reasoning_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens")
        if str(meta.get(key) or "").isdigit()
    }
    hit = int(usage.get("prompt_cache_hit_tokens") or 0)
    miss = int(usage.get("prompt_cache_miss_tokens") or 0)
    if hit + miss > 0:
        usage["cache_hit_rate"] = round(hit / (hit + miss), 6)
    return usage


def run_gap_hunter_cloud_task(
    session: Session,
    *,
    provider: str,
    model: str,
    question: str | None = None,
    keyword: str | None = None,
    universe_id: int | None = None,
    section_id: int | None = None,
    limit: int = 200,
    persist_candidates: bool = False,
    operator_approved: bool = False,
    catalog_row: dict[str, Any] | None = None,
) -> dict[str, Any]:
    index_payload = universe_coverage_index(
        session,
        keyword=keyword,
        universe_id=universe_id,
        section_id=section_id,
        limit=limit,
    )
    raw_result = manifested_chat_json(
        session,
        run_kind="gap_hunter_quick",
        run_id=int(universe_id) if universe_id is not None else None,
        task_kind="gap_hunter_quick",
        messages=[
            ChatMessage(role="system", content=gap_hunter_system_prompt()),
            ChatMessage(role="user", content=gap_hunter_user_prompt(index_payload=index_payload, question=question)),
        ],
        provider=provider,
        model=model,
        timeout_s=float(getattr(settings, "agent_llm_timeout_s", 60) or 60),
        max_input_chars=max(120000, int(getattr(settings, "agent_llm_max_input_chars", 24000) or 24000)),
        prompt_cache_key="thoth-gap-hunter-quick-v1",
        permission_profile="propose-only" if not persist_candidates else "candidate-requests-policy-gated",
    )
    meta = llm_metadata(raw_result)
    normalized = normalize_gap_hunter_result(raw_result)
    persisted: list[dict[str, Any]] = []
    write_policy = None
    if persist_candidates:
        write_policy = evaluate_cloud_write_permission(
            "candidate_requests",
            operator_approved=operator_approved,
            context={"task_kind": "gap_hunter_quick", "provider": provider, "model": model},
        )
        if write_policy.get("ok"):
            persisted = persist_gap_hunter_candidates(
                session,
                result=normalized,
                index_payload={**index_payload, "write_policy": write_policy},
                question=question,
                provider=provider,
                model=model,
                llm_meta=meta,
            )
    return {
        "index": index_payload,
        "result": normalized,
        "raw_result": raw_result,
        "provider": provider,
        "model": model,
        "requested_model": model,
        "submitted_model": meta.get("submitted_model") or model,
        "routed_model": meta.get("model") or model,
        "usage": _usage_from_llm_meta(meta),
        "pricing": catalog_row,
        "persisted_candidates": persisted,
        "write_policy": write_policy,
    }


_CANDIDATE_OUTPUTS = {
    "missing_company_reports": ["company_report"],
    "missing_product_explainers": ["product_explainer", "product_report"],
    "technical_definition_gaps": ["technical_definition", "technical_primer"],
    "super_report_candidates": ["super_report", "product_architecture_report"],
    "bottleneck_candidates": ["bottleneck_report_candidate"],
    "demotion_candidates": ["coverage_classification_review"],
    "followup_context_requests": ["context_request"],
}


def _candidate_text(candidate: Any, *keys: str, fallback: str = "") -> str:
    if isinstance(candidate, dict):
        for key in keys:
            value = _text(candidate.get(key))
            if value:
                return value
    value = _text(candidate)
    return value or fallback


def _candidate_list(candidate: Any, *keys: str) -> list[str]:
    if not isinstance(candidate, dict):
        return []
    out: list[str] = []
    for key in keys:
        raw = candidate.get(key)
        if isinstance(raw, list):
            out.extend(_text(value) for value in raw if _text(value))
        elif _text(raw):
            out.append(_text(raw))
    seen: set[str] = set()
    deduped: list[str] = []
    for value in out:
        lower = value.lower()
        if lower in seen:
            continue
        seen.add(lower)
        deduped.append(value)
    return deduped[:12]


def _candidate_priority(kind: str, candidate: Any) -> int:
    text = " ".join(
        [
            kind,
            _candidate_text(candidate, "priority", "severity", "confidence", fallback=""),
            _candidate_text(candidate, "reason", "rationale", "summary", fallback=""),
        ]
    ).lower()
    if any(token in text for token in ("critical", "high", "must", "core", "missing report", "bottleneck")):
        return 3
    if any(token in text for token in ("low", "nice", "later")):
        return 7
    return 5


def _candidate_title(kind: str, candidate: Any) -> str:
    subject = _candidate_text(
        candidate,
        "company",
        "ticker",
        "topic",
        "term",
        "concept",
        "title",
        "query",
        fallback=kind.replace("_", " "),
    )
    label = kind.replace("_", " ").title()
    return _text(f"Gap Hunter: {label} - {subject}")[:220]


def _candidate_request_key(kind: str, candidate: Any, index_payload: dict[str, Any]) -> str:
    subject = _candidate_text(candidate, "company", "ticker", "topic", "term", "concept", "title", "query", fallback="gap")
    scope = [
        str(index_payload.get("mode") or ""),
        str(index_payload.get("keyword") or ""),
        str(index_payload.get("universe_id") or ""),
        str(index_payload.get("section_id") or ""),
    ]
    return "gap_hunter:" + ":".join([kind, subject.lower().replace(" ", "-")[:80], *scope])


def _candidate_linked_objects(candidate: Any, index_payload: dict[str, Any]) -> dict[str, Any]:
    linked: dict[str, Any] = {
        "source": "gap_hunter",
        "index_mode": index_payload.get("mode"),
        "keyword": index_payload.get("keyword"),
        "universe_id": index_payload.get("universe_id"),
        "section_id": index_payload.get("section_id"),
    }
    if isinstance(candidate, dict):
        for key in ("universe_id", "section_id", "row_id", "instrument_id", "issuer_id", "company_id", "report_id"):
            value = candidate.get(key)
            if value is not None:
                linked[key] = value
    return {key: value for key, value in linked.items() if value is not None and value != ""}


def persist_gap_hunter_candidates(
    session: Session,
    *,
    result: dict[str, Any],
    index_payload: dict[str, Any],
    question: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    llm_meta: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    persisted: list[dict[str, Any]] = []
    for kind, desired_outputs in _CANDIDATE_OUTPUTS.items():
        raw_items = result.get(kind)
        if not isinstance(raw_items, list):
            continue
        for candidate in raw_items[:30]:
            title = _candidate_title(kind, candidate)
            summary = _candidate_text(candidate, "summary", "reason", "rationale", "why", "description", fallback=title)
            row = create_instrument_request(
                session,
                title=title,
                intent="research_gap",
                summary=summary,
                target_entities=_candidate_list(candidate, "companies", "entities", "tickers", "company", "ticker"),
                target_themes=_candidate_list(candidate, "themes", "topics", "topic", "term", "concept"),
                desired_outputs=desired_outputs,
                provenance_summary="Gap Hunter cloud task",
                status="draft",
                priority=_candidate_priority(kind, candidate),
                automation_source="gap_hunter",
                source_kind=kind,
                draft_payload={
                    "candidate": candidate if isinstance(candidate, dict) else {"text": _text(candidate)},
                    "candidate_kind": kind,
                    "question": _text(question) or None,
                    "coverage_summary": result.get("coverage_summary"),
                    "llm": dict(llm_meta or {}),
                    "provider": provider,
                    "model": model,
                },
                linked_objects=_candidate_linked_objects(candidate, index_payload),
                request_key=_candidate_request_key(kind, candidate, index_payload),
            )
            persisted.append(serialize_instrument_request_summary(row))
    session.flush()
    return persisted


def resolve_gap_hunter_context_request(
    session: Session,
    request: dict[str, Any],
    *,
    limit: int = 200,
) -> dict[str, Any]:
    request_type = _text(request.get("request_type") or request.get("type")).lower()
    section_id = request.get("section_id")
    universe_id = request.get("universe_id")
    keyword = _text(request.get("keyword") or request.get("query") or request.get("term"))
    if request_type in {"section", "section_drilldown", "drilldown"} and section_id:
        return universe_coverage_index(session, section_id=int(section_id), limit=limit)
    if keyword:
        return universe_coverage_index(session, keyword=keyword, universe_id=int(universe_id) if universe_id else None, limit=limit)
    if section_id:
        return universe_coverage_index(session, section_id=int(section_id), limit=limit)
    return universe_coverage_index(session, universe_id=int(universe_id) if universe_id else None, limit=limit)
