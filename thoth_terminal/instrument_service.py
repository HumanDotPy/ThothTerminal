from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session, load_only

from thoth_terminal.db import utcnow
from thoth_terminal.models import CoverageGap, InstrumentRequest, ResearchInstrument, ResearchSubject, ResearchUniverseRow


REQUEST_STATUS_DRAFT = "draft"
REQUEST_STATUS_REVIEW = "review"
REQUEST_STATUS_LIVE = "live"
REQUEST_STATUS_FULFILLED = "fulfilled"
REQUEST_STATUS_REJECTED = "rejected"
REQUEST_STATUSES = {
    REQUEST_STATUS_DRAFT,
    REQUEST_STATUS_REVIEW,
    REQUEST_STATUS_LIVE,
    REQUEST_STATUS_FULFILLED,
    REQUEST_STATUS_REJECTED,
}
UNRESOLVED_REQUEST_STATUSES = {REQUEST_STATUS_DRAFT, REQUEST_STATUS_REVIEW}
ACTIVE_REQUEST_STATUSES = {REQUEST_STATUS_DRAFT, REQUEST_STATUS_REVIEW, REQUEST_STATUS_LIVE}
CLOSED_REQUEST_STATUSES = {REQUEST_STATUS_FULFILLED, REQUEST_STATUS_REJECTED, "resolved"}
MAX_REQUEST_KEY_LENGTH = 64


def _slugify(value: str, *, default: str = "request") -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return cleaned or default


def _bounded_request_key(value: str) -> str:
    key = str(value or "").strip()
    if len(key) <= MAX_REQUEST_KEY_LENGTH:
        return key
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    prefix_length = MAX_REQUEST_KEY_LENGTH - len(digest) - 1
    return f"{key[:prefix_length].rstrip(':-')}-{digest}"


def _bounded_optional_text(value: str | None, max_length: int) -> str | None:
    text = str(value or "").strip()
    return text[:max_length] or None


def build_instrument_request_key(
    *,
    intent: str,
    title: str | None = None,
    target_entities: list[str] | None = None,
    target_themes: list[str] | None = None,
    desired_outputs: list[str] | None = None,
) -> str:
    parts = [
        _slugify(intent or "instrument", default="instrument"),
        _slugify(title or "", default="untitled"),
        ",".join(sorted({_slugify(v, default="x") for v in (target_entities or []) if str(v).strip()})),
        ",".join(sorted({_slugify(v, default="x") for v in (target_themes or []) if str(v).strip()})),
        ",".join(sorted({_slugify(v, default="x") for v in (desired_outputs or []) if str(v).strip()})),
    ]
    return _bounded_request_key(":".join([part for part in parts if part]))


def serialize_instrument_request(row: InstrumentRequest) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "request_key": row.request_key,
        "title": row.title,
        "intent": row.intent,
        "summary": row.summary,
        "target_entities": list(row.target_entities or []),
        "target_themes": list(row.target_themes or []),
        "desired_outputs": list(row.desired_outputs or []),
        "evidence_bundle": list(row.evidence_bundle or []),
        "provenance_summary": row.provenance_summary,
        "evidence_tier": row.evidence_tier,
        "status": row.status,
        "priority": int(row.priority or 0),
        "automation_source": row.automation_source,
        "source_kind": row.source_kind,
        "draft_payload": dict(row.draft_payload or {}),
        "live_payload": dict(row.live_payload or {}),
        "linked_objects": dict(row.linked_objects or {}),
        "review_note": row.review_note,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "reviewed_at": row.reviewed_at.isoformat() if row.reviewed_at else None,
    }


def serialize_instrument_request_summary(row: InstrumentRequest) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "request_key": row.request_key,
        "title": row.title,
        "intent": row.intent,
        "summary": row.summary,
        "target_entities": list(row.target_entities or []),
        "target_themes": list(row.target_themes or []),
        "desired_outputs": list(row.desired_outputs or []),
        "evidence_tier": row.evidence_tier,
        "status": row.status,
        "priority": int(row.priority or 0),
        "automation_source": row.automation_source,
        "source_kind": row.source_kind,
        "linked_objects": dict(row.linked_objects or {}),
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _int_values(*values: Any) -> set[int]:
    out: set[int] = set()
    for value in values:
        if isinstance(value, (list, tuple, set)):
            out.update(_int_values(*value))
            continue
        try:
            if isinstance(value, bool):
                continue
            text = str(value or "").strip()
            if text:
                out.add(int(text))
        except Exception:
            continue
    return out


def build_active_universe_request_scope(session: Session) -> dict[str, set[int]]:
    active_company_ids = {
        int(row[0])
        for row in (
            session.query(ResearchInstrument.company_id)
            .join(ResearchUniverseRow, ResearchUniverseRow.instrument_id == ResearchInstrument.id)
            .filter(ResearchUniverseRow.archived_at.is_(None), ResearchInstrument.company_id.isnot(None))
            .all()
        )
        if row[0] is not None
    }
    active_issuer_ids = {
        int(row[0])
        for row in (
            session.query(ResearchInstrument.issuer_id)
            .join(ResearchUniverseRow, ResearchUniverseRow.instrument_id == ResearchInstrument.id)
            .filter(ResearchUniverseRow.archived_at.is_(None), ResearchInstrument.issuer_id.isnot(None))
            .all()
        )
        if row[0] is not None
    }
    active_subject_ids: set[int] = set()
    subject_filters = []
    if active_company_ids:
        subject_filters.append(ResearchSubject.company_id.in_(sorted(active_company_ids)))
    if active_issuer_ids:
        subject_filters.append(ResearchSubject.issuer_id.in_(sorted(active_issuer_ids)))
    if subject_filters:
        active_subject_ids.update(
            int(row[0])
            for row in session.query(ResearchSubject.id).filter(or_(*subject_filters)).all()
            if row[0] is not None
        )
    return {
        "company_ids": active_company_ids,
        "issuer_ids": active_issuer_ids,
        "subject_ids": active_subject_ids,
    }


def _instrument_request_scope_refs(row: InstrumentRequest) -> dict[str, Any]:
    linked = dict(row.linked_objects or {}) if isinstance(row.linked_objects, dict) else {}
    gap_keys: set[str] = set()
    raw_gap_key = linked.get("gap_key")
    if raw_gap_key not in (None, ""):
        gap_keys.add(str(raw_gap_key).strip())
    raw_gap_keys = linked.get("gap_keys")
    if isinstance(raw_gap_keys, (list, tuple, set)):
        gap_keys.update(str(value).strip() for value in raw_gap_keys if str(value or "").strip())
    elif raw_gap_keys not in (None, ""):
        gap_keys.add(str(raw_gap_keys).strip())
    return {
        "subject_ids": _int_values(linked.get("subject_id"), linked.get("subject_ids")),
        "company_ids": _int_values(linked.get("company_id"), linked.get("company_ids")),
        "issuer_ids": _int_values(linked.get("issuer_id"), linked.get("issuer_ids")),
        "gap_ids": _int_values(linked.get("gap_id"), linked.get("gap_ids")),
        "gap_keys": gap_keys,
        "decision_spine": str(row.automation_source or "").strip() == "decision_spine",
    }


def _scope_from_refs(refs: dict[str, Any], active_scope: dict[str, set[int]]) -> str:
    active_company_ids = active_scope.get("company_ids") or set()
    active_issuer_ids = active_scope.get("issuer_ids") or set()
    active_subject_ids = active_scope.get("subject_ids") or set()
    subject_ids = set(refs.get("subject_ids") or set())
    company_ids = set(refs.get("company_ids") or set())
    issuer_ids = set(refs.get("issuer_ids") or set())
    if subject_ids & active_subject_ids or company_ids & active_company_ids or issuer_ids & active_issuer_ids:
        return "active_universe"
    if bool(refs.get("decision_spine")):
        return "off_universe"
    return "manual"


def instrument_request_scope_map(
    session: Session,
    rows: list[InstrumentRequest],
    *,
    active_scope: dict[str, set[int]] | None = None,
) -> dict[int, str]:
    if not rows:
        return {}
    scope = active_scope if active_scope is not None else build_active_universe_request_scope(session)
    refs_by_id: dict[int, dict[str, Any]] = {}
    gap_ids: set[int] = set()
    gap_keys: set[str] = set()
    for row in rows:
        refs = _instrument_request_scope_refs(row)
        refs_by_id[int(row.id)] = refs
        gap_ids.update(refs.get("gap_ids") or set())
        gap_keys.update(refs.get("gap_keys") or set())

    gap_subject_by_id: dict[int, int] = {}
    gap_subject_by_key: dict[str, int] = {}
    if gap_ids:
        for gap_id, subject_id in (
            session.query(CoverageGap.id, CoverageGap.subject_id)
            .filter(CoverageGap.id.in_(sorted(gap_ids)))
            .all()
        ):
            if subject_id is not None:
                gap_subject_by_id[int(gap_id)] = int(subject_id)
    if gap_keys:
        for gap_key, subject_id in (
            session.query(CoverageGap.gap_key, CoverageGap.subject_id)
            .filter(CoverageGap.gap_key.in_(sorted(gap_keys)))
            .all()
        ):
            if subject_id is not None:
                gap_subject_by_key[str(gap_key)] = int(subject_id)

    out: dict[int, str] = {}
    for row in rows:
        row_id = int(row.id)
        refs = refs_by_id[row_id]
        subject_ids = set(refs.get("subject_ids") or set())
        subject_ids.update(gap_subject_by_id[gap_id] for gap_id in refs.get("gap_ids") or set() if gap_id in gap_subject_by_id)
        subject_ids.update(gap_subject_by_key[gap_key] for gap_key in refs.get("gap_keys") or set() if gap_key in gap_subject_by_key)
        refs["subject_ids"] = subject_ids
        out[row_id] = _scope_from_refs(refs, scope)
    return out


def instrument_request_scope(
    session: Session,
    row: InstrumentRequest,
    *,
    active_scope: dict[str, set[int]] | None = None,
) -> str:
    scope = active_scope if active_scope is not None else build_active_universe_request_scope(session)
    refs = _instrument_request_scope_refs(row)
    gap_ids = refs.get("gap_ids") or set()
    gap_keys = refs.get("gap_keys") or set()
    subject_ids = set(refs.get("subject_ids") or set())
    if gap_ids:
        subject_ids.update(
            int(subject_id)
            for subject_id, in (
                session.query(CoverageGap.subject_id)
                .filter(CoverageGap.id.in_(sorted(gap_ids)), CoverageGap.subject_id.isnot(None))
                .all()
            )
        )
    if gap_keys:
        subject_ids.update(
            int(subject_id)
            for subject_id, in (
                session.query(CoverageGap.subject_id)
                .filter(CoverageGap.gap_key.in_(sorted(gap_keys)), CoverageGap.subject_id.isnot(None))
                .all()
            )
        )
    refs["subject_ids"] = subject_ids
    return _scope_from_refs(refs, scope)


def coverage_gap_scope(
    session: Session,
    row: CoverageGap,
    *,
    active_scope: dict[str, set[int]] | None = None,
) -> str:
    scope = active_scope if active_scope is not None else build_active_universe_request_scope(session)
    active_subject_ids = scope.get("subject_ids") or set()
    active_company_ids = scope.get("company_ids") or set()
    active_issuer_ids = scope.get("issuer_ids") or set()
    payload = dict(row.payload or {}) if isinstance(row.payload, dict) else {}
    subject_ids = _int_values(row.subject_id, payload.get("subject_id"), payload.get("subject_ids"))
    company_ids = _int_values(payload.get("company_id"), payload.get("company_ids"))
    issuer_ids = _int_values(payload.get("issuer_id"), payload.get("issuer_ids"))
    if subject_ids & active_subject_ids or company_ids & active_company_ids or issuer_ids & active_issuer_ids:
        return "active_universe"
    if row.subject_id is not None or str(row.gap_key or "").startswith("coverage_gap:"):
        return "off_universe"
    return "manual"


def request_queue_scope_matches(scope: str | None, row_scope: str) -> bool:
    scope_norm = str(scope or "").strip().lower().replace("-", "_")
    if scope_norm in {"", "all"}:
        return True
    if scope_norm in {"active_universe", "universe", "watchlist"}:
        return row_scope == "active_universe"
    if scope_norm in {"off_universe", "backlog", "global_backlog"}:
        return row_scope == "off_universe"
    if scope_norm in {"manual", "unscoped"}:
        return row_scope == "manual"
    return False


def _instrument_request_query(
    session: Session,
    *,
    query: str | None = None,
    status: str | None = None,
):
    q = session.query(InstrumentRequest)
    query_norm = str(query or "").strip()
    if query_norm:
        pattern = f"%{query_norm}%"
        q = q.filter(
            or_(
                InstrumentRequest.title.ilike(pattern),
                InstrumentRequest.summary.ilike(pattern),
                InstrumentRequest.intent.ilike(pattern),
                InstrumentRequest.provenance_summary.ilike(pattern),
            )
        )
    status_norm = str(status or "").strip().lower()
    if status_norm in {"open", "unresolved", "queued"}:
        q = q.filter(InstrumentRequest.status.in_(sorted(UNRESOLVED_REQUEST_STATUSES)))
    elif status_norm == "active":
        q = q.filter(InstrumentRequest.status.in_(sorted(ACTIVE_REQUEST_STATUSES)))
    elif status_norm in {"closed", "done", "complete", "completed"}:
        q = q.filter(InstrumentRequest.status.in_(sorted(CLOSED_REQUEST_STATUSES)))
    elif status_norm and status_norm != "all":
        q = q.filter(InstrumentRequest.status == status_norm)
    return q


def instrument_request_scope_counts(
    session: Session,
    *,
    query: str | None = None,
    status: str | None = None,
) -> dict[str, int]:
    q = _instrument_request_query(session, query=query, status=status)
    q = q.options(load_only(InstrumentRequest.id, InstrumentRequest.automation_source, InstrumentRequest.linked_objects))
    rows = q.order_by(None).all()
    active_scope = build_active_universe_request_scope(session)
    scope_by_id = instrument_request_scope_map(session, rows, active_scope=active_scope)
    counts = {"active_universe": 0, "off_universe": 0, "manual": 0, "all": len(rows)}
    for row in rows:
        row_scope = scope_by_id.get(int(row.id), "manual")
        if row_scope in counts:
            counts[row_scope] += 1
    return counts


def list_instrument_requests(
    session: Session,
    *,
    query: str | None = None,
    status: str | None = None,
    scope: str | None = None,
    limit: int = 50,
    include_payloads: bool = False,
) -> list[InstrumentRequest]:
    q = _instrument_request_query(session, query=query, status=status)
    if not include_payloads:
        q = q.options(
            load_only(
                InstrumentRequest.id,
                InstrumentRequest.request_key,
                InstrumentRequest.title,
                InstrumentRequest.intent,
                InstrumentRequest.summary,
                InstrumentRequest.target_entities,
                InstrumentRequest.target_themes,
                InstrumentRequest.desired_outputs,
                InstrumentRequest.evidence_tier,
                InstrumentRequest.status,
                InstrumentRequest.priority,
                InstrumentRequest.automation_source,
                InstrumentRequest.source_kind,
                InstrumentRequest.linked_objects,
                InstrumentRequest.updated_at,
            )
        )
    max_limit = max(1, min(int(limit or 50), 200))
    scope_norm = str(scope or "").strip().lower().replace("-", "_")
    if scope_norm in {"", "all"}:
        return q.order_by(InstrumentRequest.priority.desc(), InstrumentRequest.updated_at.desc()).limit(max_limit).all()
    fetch_limit = max(max_limit * 8, 500)
    candidates = q.order_by(InstrumentRequest.priority.desc(), InstrumentRequest.updated_at.desc()).limit(min(fetch_limit, 2500)).all()
    active_scope = build_active_universe_request_scope(session)
    scope_by_id = instrument_request_scope_map(session, candidates, active_scope=active_scope)
    filtered: list[InstrumentRequest] = []
    for row in candidates:
        row_scope = scope_by_id.get(int(row.id), "manual")
        if request_queue_scope_matches(scope_norm, row_scope):
            filtered.append(row)
        if len(filtered) >= max_limit:
            break
    return filtered


def get_instrument_request(session: Session, request_id: int) -> InstrumentRequest | None:
    return session.query(InstrumentRequest).filter_by(id=int(request_id)).first()


def create_instrument_request(
    session: Session,
    *,
    title: str,
    intent: str,
    summary: str | None = None,
    target_entities: list[str] | None = None,
    target_themes: list[str] | None = None,
    desired_outputs: list[str] | None = None,
    evidence_bundle: list[dict[str, Any]] | None = None,
    provenance_summary: str | None = None,
    evidence_tier: str | None = None,
    status: str = REQUEST_STATUS_DRAFT,
    priority: int = 5,
    automation_source: str | None = None,
    source_kind: str | None = None,
    draft_payload: dict[str, Any] | None = None,
    live_payload: dict[str, Any] | None = None,
    linked_objects: dict[str, Any] | None = None,
    request_key: str | None = None,
) -> InstrumentRequest:
    now = utcnow()
    bounded_intent = _bounded_optional_text(intent, 64) or "instrument"
    bounded_evidence_tier = _bounded_optional_text(evidence_tier, 64)
    bounded_automation_source = _bounded_optional_text(automation_source, 64)
    bounded_source_kind = _bounded_optional_text(source_kind, 128)
    key = _bounded_request_key(str(request_key or "").strip()) if request_key else build_instrument_request_key(
        intent=intent,
        title=title,
        target_entities=target_entities,
        target_themes=target_themes,
        desired_outputs=desired_outputs,
    )
    row = session.query(InstrumentRequest).filter_by(request_key=key).first()
    if row is None:
        row = InstrumentRequest(
            request_key=key,
            title=title.strip() or "Untitled Instrument Request",
            intent=bounded_intent,
            summary=str(summary or "").strip() or None,
            target_entities=[str(v).strip() for v in (target_entities or []) if str(v).strip()],
            target_themes=[str(v).strip() for v in (target_themes or []) if str(v).strip()],
            desired_outputs=[str(v).strip() for v in (desired_outputs or []) if str(v).strip()],
            evidence_bundle=[dict(v) for v in (evidence_bundle or []) if isinstance(v, dict)],
            provenance_summary=str(provenance_summary or "").strip() or None,
            evidence_tier=bounded_evidence_tier,
            status=status if status in REQUEST_STATUSES else REQUEST_STATUS_DRAFT,
            priority=max(1, min(int(priority or 5), 10)),
            automation_source=bounded_automation_source,
            source_kind=bounded_source_kind,
            draft_payload=dict(draft_payload or {}),
            live_payload=dict(live_payload or {}),
            linked_objects=dict(linked_objects or {}),
            review_note=None,
            created_at=now,
            updated_at=now,
            reviewed_at=None,
        )
        session.add(row)
    else:
        row.title = title.strip() or row.title
        row.intent = bounded_intent or row.intent
        row.summary = str(summary or "").strip() or row.summary
        row.target_entities = [str(v).strip() for v in (target_entities or row.target_entities or []) if str(v).strip()]
        row.target_themes = [str(v).strip() for v in (target_themes or row.target_themes or []) if str(v).strip()]
        row.desired_outputs = [str(v).strip() for v in (desired_outputs or row.desired_outputs or []) if str(v).strip()]
        row.evidence_bundle = [dict(v) for v in (evidence_bundle or row.evidence_bundle or []) if isinstance(v, dict)]
        row.provenance_summary = str(provenance_summary or "").strip() or row.provenance_summary
        row.evidence_tier = bounded_evidence_tier or row.evidence_tier
        row.priority = max(1, min(int(priority or row.priority or 5), 10))
        row.automation_source = bounded_automation_source or row.automation_source
        row.source_kind = bounded_source_kind or row.source_kind
        if draft_payload:
            merged_draft = dict(row.draft_payload or {})
            merged_draft.update(dict(draft_payload))
            row.draft_payload = merged_draft
        if linked_objects:
            merged_links = dict(row.linked_objects or {})
            merged_links.update(dict(linked_objects))
            row.linked_objects = merged_links
    row.updated_at = now
    session.flush()
    return row


def review_instrument_request(
    session: Session,
    request_id: int,
    *,
    decision: str,
    reviewer_note: str | None = None,
    live_payload: dict[str, Any] | None = None,
) -> InstrumentRequest:
    row = get_instrument_request(session, request_id)
    if row is None:
        raise ValueError("Instrument request not found.")
    decision_norm = str(decision or "").strip().lower()
    if decision_norm in {"submit", "review", "request_review"}:
        row.status = REQUEST_STATUS_REVIEW
    elif decision_norm in {"approve", "publish", "live"}:
        row.status = REQUEST_STATUS_LIVE
        row.live_payload = dict(live_payload or row.live_payload or row.draft_payload or {})
    elif decision_norm in {"reject", "dismiss", "skip"}:
        row.status = REQUEST_STATUS_REJECTED
    else:
        raise ValueError("Unsupported review decision.")
    row.review_note = str(reviewer_note or "").strip() or None
    row.reviewed_at = utcnow()
    row.updated_at = row.reviewed_at
    session.flush()
    return row


def run_instrument_request(
    session: Session,
    request_id: int,
    *,
    draft_payload_patch: dict[str, Any] | None = None,
    linked_objects_patch: dict[str, Any] | None = None,
    run_summary: str | None = None,
) -> InstrumentRequest:
    row = get_instrument_request(session, request_id)
    if row is None:
        raise ValueError("Instrument request not found.")
    draft = dict(row.draft_payload or {})
    if draft_payload_patch:
        draft.update(dict(draft_payload_patch))
    if run_summary:
        draft["run_summary"] = str(run_summary).strip()
    row.draft_payload = draft
    if linked_objects_patch:
        linked = dict(row.linked_objects or {})
        linked.update(dict(linked_objects_patch))
        row.linked_objects = linked
    if row.status == REQUEST_STATUS_DRAFT:
        row.status = REQUEST_STATUS_REVIEW
    row.updated_at = utcnow()
    session.flush()
    return row


def _merge_json_payload(base: dict[str, Any] | None, patch: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(base or {})
    if patch:
        for key, value in dict(patch).items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                nested = dict(merged.get(key) or {})
                nested.update(value)
                merged[key] = nested
            else:
                merged[key] = value
    return merged


def _merge_evidence_bundle(existing: list[dict[str, Any]] | None, patch: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in list(existing or []) + list(patch or []):
        if not isinstance(item, dict):
            continue
        clean = dict(item)
        key = json.dumps(clean, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        merged.append(clean)
    return merged


def _linked_gap_refs(linked_objects: dict[str, Any]) -> tuple[set[int], set[str]]:
    gap_ids = _int_values(linked_objects.get("gap_id"), linked_objects.get("gap_ids"), linked_objects.get("coverage_gap_id"), linked_objects.get("coverage_gap_ids"))
    gap_keys: set[str] = set()
    for key in ("gap_key", "coverage_gap_key"):
        raw = linked_objects.get(key)
        if raw not in (None, ""):
            gap_keys.add(str(raw).strip())
    for key in ("gap_keys", "coverage_gap_keys"):
        raw_values = linked_objects.get(key)
        if isinstance(raw_values, (list, tuple, set)):
            gap_keys.update(str(value).strip() for value in raw_values if str(value or "").strip())
        elif raw_values not in (None, ""):
            gap_keys.add(str(raw_values).strip())
    return gap_ids, gap_keys


def fulfill_instrument_request(
    session: Session,
    request_id: int,
    *,
    fulfillment_summary: str,
    fulfilled_by: str | None = None,
    live_payload_patch: dict[str, Any] | None = None,
    linked_objects_patch: dict[str, Any] | None = None,
    evidence_bundle_patch: list[dict[str, Any]] | None = None,
    verification: dict[str, Any] | None = None,
    close_linked_gaps: bool = True,
) -> InstrumentRequest:
    row = get_instrument_request(session, request_id)
    if row is None:
        raise ValueError("Instrument request not found.")
    summary = " ".join(str(fulfillment_summary or "").strip().split())
    if not summary:
        raise ValueError("fulfillment_summary is required.")

    now = utcnow()
    linked = _merge_json_payload(row.linked_objects or {}, linked_objects_patch)
    closed_gap_ids: list[int] = []
    closed_gap_keys: list[str] = []
    if close_linked_gaps:
        gap_ids, gap_keys = _linked_gap_refs(linked)
        gap_query = session.query(CoverageGap)
        filters = []
        if gap_ids:
            filters.append(CoverageGap.id.in_(sorted(gap_ids)))
        if gap_keys:
            filters.append(CoverageGap.gap_key.in_(sorted(gap_keys)))
        if filters:
            for gap in gap_query.filter(or_(*filters)).all():
                payload = dict(gap.payload or {})
                payload["resolved_by"] = "instrument_request_fulfilled"
                payload["resolved_at"] = now.isoformat()
                payload["instrument_request_id"] = int(row.id)
                payload["fulfillment_summary"] = summary
                if fulfilled_by:
                    payload["fulfilled_by"] = str(fulfilled_by).strip()
                gap.payload = payload
                gap.status = "resolved"
                gap.last_reviewed_at = now
                gap.updated_at = now
                session.add(gap)
                closed_gap_ids.append(int(gap.id))
                closed_gap_keys.append(str(gap.gap_key))

    fulfillment_payload: dict[str, Any] = {
        "fulfilled": True,
        "fulfilled_at": now.isoformat(),
        "fulfillment_summary": summary,
    }
    if fulfilled_by:
        fulfillment_payload["fulfilled_by"] = str(fulfilled_by).strip()
    if verification:
        fulfillment_payload["verification"] = dict(verification)
    if closed_gap_ids:
        fulfillment_payload["closed_gap_ids"] = closed_gap_ids
    if closed_gap_keys:
        fulfillment_payload["closed_gap_keys"] = closed_gap_keys

    base_live = dict(row.live_payload or row.draft_payload or {})
    live_payload = _merge_json_payload(base_live, live_payload_patch)
    live_payload["fulfillment"] = _merge_json_payload(live_payload.get("fulfillment") if isinstance(live_payload.get("fulfillment"), dict) else {}, fulfillment_payload)
    row.live_payload = live_payload
    row.linked_objects = linked
    row.evidence_bundle = _merge_evidence_bundle(row.evidence_bundle or [], evidence_bundle_patch or [])
    row.status = REQUEST_STATUS_FULFILLED
    row.review_note = summary
    row.reviewed_at = now
    row.updated_at = now
    session.flush()
    return row


def instrument_request_markdown(row: InstrumentRequest) -> str:
    evidence_lines = []
    for item in row.evidence_bundle or []:
        if not isinstance(item, dict):
            continue
        label = str(item.get("title") or item.get("label") or item.get("source") or "Evidence").strip()
        source = str(item.get("source") or "").strip()
        url = str(item.get("url") or "").strip()
        tail = f" ({source})" if source else ""
        evidence_lines.append(f"- {label}{tail}{f' — {url}' if url else ''}")
    lines = [
        f"# {row.title}",
        "",
        f"- Status: {row.status}",
        f"- Intent: {row.intent}",
        f"- Priority: {int(row.priority or 0)}",
        f"- Evidence tier: {row.evidence_tier or 'unknown'}",
        "",
        "## Summary",
        row.summary or "No summary yet.",
        "",
        "## Targets",
        f"- Entities: {', '.join(row.target_entities or []) or 'none'}",
        f"- Themes: {', '.join(row.target_themes or []) or 'none'}",
        f"- Outputs: {', '.join(row.desired_outputs or []) or 'none'}",
        "",
        "## Evidence",
        *(evidence_lines or ["- No evidence attached."]),
        "",
        "## Draft Payload",
        "```json",
        json.dumps(dict(row.draft_payload or {}), indent=2, sort_keys=True),
        "```",
    ]
    if row.live_payload:
        lines.extend(
            [
                "",
                "## Live Payload",
                "```json",
                json.dumps(dict(row.live_payload or {}), indent=2, sort_keys=True),
                "```",
            ]
        )
    if row.linked_objects:
        lines.extend(
            [
                "",
                "## Linked Objects",
                "```json",
                json.dumps(dict(row.linked_objects or {}), indent=2, sort_keys=True),
                "```",
            ]
        )
    return "\n".join(lines).strip() + "\n"
