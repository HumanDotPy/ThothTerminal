from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from thoth_terminal.db import utcnow
from thoth_terminal.models import Company, Document, ProductStageClaim, ProductStageEntry, ProductStageHistory


PRODUCT_STAGES = ("r_and_d", "sampling", "qualification", "pre_production", "mass_production")
DATE_PRECISIONS = ("day", "month", "quarter", "year", "text")
PRODUCT_STAGE_CLAIM_STATUSES = ("draft", "accepted", "rejected", "superseded")
ACTIVE_PRODUCT_STAGE_CLAIM_STATUSES = {"draft", "accepted"}


def _slugify(value: str, *, default: str = "item") -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return cleaned or default


def build_product_stage_tracker_key(*, company_name: str, product_name: str) -> str:
    return ":".join(
        [
            _slugify(company_name, default="company"),
            _slugify(product_name, default="product"),
        ]
    )


def build_product_stage_entry_key(*, company_name: str, product_name: str, stage: str | None = None) -> str:
    return build_product_stage_tracker_key(company_name=company_name, product_name=product_name)


def _normalize_stage(value: str | None) -> str:
    stage_norm = str(value or "").strip().lower()
    if stage_norm not in PRODUCT_STAGES:
        raise ValueError("Unsupported stage.")
    return stage_norm


def _normalize_precision(value: str | None, *, target_date: date | None = None) -> str:
    precision_norm = str(value or "").strip().lower() or ("day" if target_date else "text")
    return precision_norm if precision_norm in DATE_PRECISIONS else "text"


def _normalize_text(value: str | None) -> str | None:
    cleaned = str(value or "").strip()
    return cleaned or None


def _normalize_int_list(values: list[int] | list[str] | None) -> list[int]:
    return sorted({int(value) for value in (values or []) if str(value).isdigit()})


def _normalize_evidence_bundle(values: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [dict(value) for value in (values or []) if isinstance(value, dict)]


def _merge_int_lists(*groups: list[int] | None) -> list[int]:
    merged: set[int] = set()
    for group in groups:
        merged.update(_normalize_int_list(group))
    return sorted(merged)


def _claim_source_date(session: Session, *, source_document_id: int | None, source_date: datetime | None) -> datetime | None:
    if source_date is not None:
        return source_date
    if source_document_id is None:
        return None
    doc = session.query(Document).filter_by(id=int(source_document_id)).first()
    if doc is None:
        return None
    return doc.published_at or doc.ingested_at


def build_product_stage_claim_key(
    *,
    tracker_key: str,
    stage: str,
    target_date: date | None = None,
    date_text: str | None = None,
    source_document_id: int | None = None,
    summary: str | None = None,
    evidence_tier: str | None = None,
    automation_source: str | None = None,
) -> str:
    digest = hashlib.sha1(
        "|".join(
            [
                tracker_key,
                stage,
                target_date.isoformat() if target_date else "",
                str(date_text or "").strip(),
                str(source_document_id or ""),
                str(summary or "").strip(),
                str(evidence_tier or "").strip(),
                str(automation_source or "").strip(),
            ]
        ).encode("utf-8")
    ).hexdigest()[:16]
    return f"{tracker_key}:{_slugify(stage, default='stage')}:{digest}"


def serialize_product_stage_claim(row: ProductStageClaim) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "entry_id": int(row.entry_id),
        "claim_key": row.claim_key,
        "stage": row.stage,
        "target_date": row.target_date.isoformat() if row.target_date else None,
        "date_precision": row.date_precision,
        "date_text": row.date_text,
        "certainty": row.certainty,
        "evidence_tier": row.evidence_tier,
        "provenance_summary": row.provenance_summary,
        "summary": row.summary,
        "rationale": row.rationale,
        "review_status": row.review_status,
        "source_document_id": int(row.source_document_id) if row.source_document_id is not None else None,
        "source_date": row.source_date.isoformat() if row.source_date else None,
        "linked_dataset_ids": [int(v) for v in (row.linked_dataset_ids or [])],
        "linked_supply_chain_ids": [int(v) for v in (row.linked_supply_chain_ids or [])],
        "linked_note_ids": [int(v) for v in (row.linked_note_ids or [])],
        "linked_thesis_ids": [int(v) for v in (row.linked_thesis_ids or [])],
        "automation_source": row.automation_source,
        "source_kind": row.source_kind,
        "extra": dict(row.extra or {}),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "reviewed_at": row.reviewed_at.isoformat() if row.reviewed_at else None,
    }


def _claim_rank_tuple(row: ProductStageClaim) -> tuple[float, float, int]:
    source_ts = row.source_date.timestamp() if row.source_date else 0.0
    created_ts = row.created_at.timestamp() if row.created_at else 0.0
    return (-source_ts, -created_ts, -int(row.id or 0))


def _choose_resolved_claim(claims: list[ProductStageClaim], resolved_claim_id: int | None = None) -> ProductStageClaim | None:
    claim_map = {int(claim.id): claim for claim in claims}
    if resolved_claim_id is not None and int(resolved_claim_id) in claim_map:
        claim = claim_map[int(resolved_claim_id)]
        if claim.review_status == "accepted":
            return claim
    accepted = [claim for claim in claims if claim.review_status == "accepted"]
    if not accepted:
        return None
    accepted.sort(
        key=lambda claim: (
            claim.reviewed_at.timestamp() if claim.reviewed_at else 0.0,
            claim.updated_at.timestamp() if claim.updated_at else 0.0,
            int(claim.id or 0),
        ),
        reverse=True,
    )
    return accepted[0]


def _choose_candidate_claim(claims: list[ProductStageClaim]) -> ProductStageClaim | None:
    active = [claim for claim in claims if claim.review_status in ACTIVE_PRODUCT_STAGE_CLAIM_STATUSES]
    if not active:
        return None
    return sorted(active, key=_claim_rank_tuple)[0]


def _claim_signature(row: ProductStageClaim) -> tuple[str, str, str]:
    return (
        str(row.stage or "").strip(),
        row.target_date.isoformat() if row.target_date else "",
        str(row.date_text or "").strip().lower(),
    )


def _derive_conflict_state(
    claims: list[ProductStageClaim],
    *,
    resolved: ProductStageClaim | None,
    candidate: ProductStageClaim | None,
) -> str:
    active = [claim for claim in claims if claim.review_status in ACTIVE_PRODUCT_STAGE_CLAIM_STATUSES]
    if not active:
        return "none"
    if len(active) == 1:
        return "single_source"
    unique_signatures = {_claim_signature(claim) for claim in active}
    if resolved is not None and candidate is not None and int(resolved.id) != int(candidate.id):
        return "candidate_differs"
    if len(unique_signatures) == 1:
        return "corroborated"
    return "contested"


def serialize_product_stage_entry(row: ProductStageEntry) -> dict[str, Any]:
    claims = list(row.claims or [])
    claims_sorted = sorted(
        claims,
        key=lambda claim: (
            claim.created_at.timestamp() if claim.created_at else 0.0,
            int(claim.id or 0),
        ),
        reverse=True,
    )
    claim_map = {int(claim.id): claim for claim in claims}
    resolved = claim_map.get(int(row.resolved_claim_id)) if row.resolved_claim_id is not None else None
    if resolved is None:
        resolved = _choose_resolved_claim(claims, row.resolved_claim_id)
    candidate = claim_map.get(int(row.candidate_claim_id)) if row.candidate_claim_id is not None else None
    if candidate is None:
        candidate = _choose_candidate_claim(claims)
    current_reviewed = serialize_product_stage_claim(resolved) if resolved is not None else None
    current_candidate = serialize_product_stage_claim(candidate) if candidate is not None else None
    accepted_count = len([claim for claim in claims if claim.review_status == "accepted"])
    draft_count = len([claim for claim in claims if claim.review_status == "draft"])
    rejected_count = len([claim for claim in claims if claim.review_status == "rejected"])
    superseded_count = len([claim for claim in claims if claim.review_status == "superseded"])
    conflict_state = str(row.conflict_state or _derive_conflict_state(claims, resolved=resolved, candidate=candidate))
    return {
        "id": int(row.id),
        "entry_key": row.entry_key,
        "tracker_key": row.tracker_key,
        "company_id": int(row.company_id) if row.company_id is not None else None,
        "company_name": row.company_name,
        "product_name": row.product_name,
        "stage": row.stage,
        "target_date": row.target_date.isoformat() if row.target_date else None,
        "date_precision": row.date_precision,
        "date_text": row.date_text,
        "certainty": row.certainty,
        "status": row.status,
        "notes": row.notes,
        "evidence_tier": row.evidence_tier,
        "evidence_bundle": list(row.evidence_bundle or []),
        "linked_dataset_ids": [int(v) for v in (row.linked_dataset_ids or [])],
        "linked_supply_chain_ids": [int(v) for v in (row.linked_supply_chain_ids or [])],
        "linked_note_ids": [int(v) for v in (row.linked_note_ids or [])],
        "linked_thesis_ids": [int(v) for v in (row.linked_thesis_ids or [])],
        "candidate_claim_id": int(row.candidate_claim_id) if row.candidate_claim_id is not None else None,
        "resolved_claim_id": int(row.resolved_claim_id) if row.resolved_claim_id is not None else None,
        "claim_count": int(row.claim_count or len(claims)),
        "conflict_state": conflict_state,
        "highest_evidence_tier": row.highest_evidence_tier,
        "last_claim_at": row.last_claim_at.isoformat() if row.last_claim_at else None,
        "current_reviewed": current_reviewed,
        "current_candidate": current_candidate,
        "claim_summary": {
            "total": int(row.claim_count or len(claims)),
            "active": len([claim for claim in claims if claim.review_status in ACTIVE_PRODUCT_STAGE_CLAIM_STATUSES]),
            "draft": draft_count,
            "accepted": accepted_count,
            "rejected": rejected_count,
            "superseded": superseded_count,
            "highest_evidence_tier": row.highest_evidence_tier,
            "conflict_state": conflict_state,
        },
        "claims_preview": [serialize_product_stage_claim(claim) for claim in claims_sorted[:5]],
        "extra": dict(row.extra or {}),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def serialize_product_stage_history(row: ProductStageHistory) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "entry_id": int(row.entry_id),
        "action": row.action,
        "previous_payload": dict(row.previous_payload or {}),
        "current_payload": dict(row.current_payload or {}),
        "note": row.note,
        "source_document_id": int(row.source_document_id) if row.source_document_id is not None else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _ensure_company_shell(session: Session, *, company_name: str) -> Company | None:
    name = str(company_name or "").strip()
    if not name:
        return None
    tracker_slug = _slugify(name, default="company")
    existing = (
        session.query(Company)
        .filter(or_(Company.name.ilike(name), Company.slug == tracker_slug))
        .order_by(Company.id.asc())
        .first()
    )
    if existing:
        return existing
    now = utcnow()
    row = Company(
        slug=tracker_slug,
        name=name,
        ticker=None,
        exchange=None,
        region=None,
        sector=None,
        industry=None,
        supply_chain_stage=None,
        ir_url=None,
        filings_url=None,
        reports_url=None,
        calendar_url=None,
        created_at=now,
        issuer_id=None,
    )
    session.add(row)
    session.flush()
    return row


def _get_tracker_entry(
    session: Session,
    *,
    entry_id: int | None = None,
    company_name: str | None = None,
    product_name: str | None = None,
) -> ProductStageEntry | None:
    if entry_id is not None:
        return session.query(ProductStageEntry).filter_by(id=int(entry_id)).first()
    if not company_name or not product_name:
        return None
    tracker_key = build_product_stage_tracker_key(company_name=company_name, product_name=product_name)
    row = session.query(ProductStageEntry).filter_by(tracker_key=tracker_key).first()
    if row is not None:
        return row
    return (
        session.query(ProductStageEntry)
        .filter(
            ProductStageEntry.company_name.ilike(str(company_name).strip()),
            ProductStageEntry.product_name.ilike(str(product_name).strip()),
        )
        .order_by(ProductStageEntry.updated_at.desc(), ProductStageEntry.id.desc())
        .first()
    )


def _record_product_stage_history(
    session: Session,
    *,
    entry: ProductStageEntry,
    action: str,
    previous_payload: dict[str, Any],
    current_payload: dict[str, Any],
    note: str | None = None,
    source_document_id: int | None = None,
) -> None:
    session.add(
        ProductStageHistory(
            entry_id=int(entry.id),
            action=action,
            previous_payload=previous_payload,
            current_payload=current_payload,
            note=_normalize_text(note),
            source_document_id=int(source_document_id) if source_document_id is not None else None,
            created_at=utcnow(),
        )
    )


def rebuild_product_stage_entry_state(session: Session, entry: ProductStageEntry) -> ProductStageEntry:
    claims = (
        session.query(ProductStageClaim)
        .filter(ProductStageClaim.entry_id == int(entry.id))
        .order_by(ProductStageClaim.created_at.desc(), ProductStageClaim.id.desc())
        .all()
    )
    resolved = _choose_resolved_claim(claims, entry.resolved_claim_id)
    candidate = _choose_candidate_claim(claims)
    if candidate is None and resolved is not None:
        candidate = resolved

    active_claims = [claim for claim in claims if claim.review_status in ACTIVE_PRODUCT_STAGE_CLAIM_STATUSES]
    # A source-medium label does not establish which forecast is most useful.
    # Preserve the candidate's label for compatibility with existing views.
    highest_tier = candidate.evidence_tier if candidate is not None else None
    last_claim_at = max((claim.created_at or claim.updated_at) for claim in claims) if claims else None

    entry.candidate_claim_id = int(candidate.id) if candidate is not None else None
    entry.resolved_claim_id = int(resolved.id) if resolved is not None else None
    entry.claim_count = len(claims)
    entry.highest_evidence_tier = _normalize_text(highest_tier)
    entry.last_claim_at = last_claim_at
    entry.conflict_state = _derive_conflict_state(claims, resolved=resolved, candidate=candidate)

    chosen = resolved or candidate
    if chosen is not None:
        entry.stage = chosen.stage
        entry.target_date = chosen.target_date
        entry.date_precision = chosen.date_precision
        entry.date_text = chosen.date_text
        entry.certainty = chosen.certainty
        entry.evidence_tier = chosen.evidence_tier
        entry.notes = chosen.summary or entry.notes

    if active_claims:
        entry.linked_dataset_ids = _merge_int_lists(*[claim.linked_dataset_ids for claim in active_claims], entry.linked_dataset_ids)
        entry.linked_supply_chain_ids = _merge_int_lists(*[claim.linked_supply_chain_ids for claim in active_claims], entry.linked_supply_chain_ids)
        entry.linked_note_ids = _merge_int_lists(*[claim.linked_note_ids for claim in active_claims], entry.linked_note_ids)
        entry.linked_thesis_ids = _merge_int_lists(*[claim.linked_thesis_ids for claim in active_claims], entry.linked_thesis_ids)

    entry.updated_at = max(filter(None, [entry.updated_at, last_claim_at, utcnow()]))
    session.flush()
    return entry


def _ensure_tracker_entry(
    session: Session,
    *,
    company_name: str,
    product_name: str,
    stage: str,
    target_date: date | None = None,
    date_precision: str | None = None,
    date_text: str | None = None,
    certainty: str | None = None,
    status: str | None = None,
    notes: str | None = None,
    evidence_tier: str | None = None,
    evidence_bundle: list[dict[str, Any]] | None = None,
    linked_dataset_ids: list[int] | None = None,
    linked_supply_chain_ids: list[int] | None = None,
    linked_note_ids: list[int] | None = None,
    linked_thesis_ids: list[int] | None = None,
    extra: dict[str, Any] | None = None,
) -> ProductStageEntry:
    tracker_key = build_product_stage_tracker_key(company_name=company_name, product_name=product_name)
    row = _get_tracker_entry(session, company_name=company_name, product_name=product_name)
    company = _ensure_company_shell(session, company_name=company_name)
    now = utcnow()
    if row is None:
        row = ProductStageEntry(
            entry_key=build_product_stage_entry_key(company_name=company_name, product_name=product_name, stage=stage),
            tracker_key=tracker_key,
            company_id=int(company.id) if company is not None else None,
            company_name=str(company_name or "").strip(),
            product_name=str(product_name or "").strip(),
            stage=stage,
            target_date=target_date,
            date_precision=date_precision,
            date_text=_normalize_text(date_text),
            certainty=_normalize_text(certainty) or "attributed",
            status=str(status or "draft").strip().lower() or "draft",
            notes=_normalize_text(notes),
            evidence_tier=_normalize_text(evidence_tier),
            evidence_bundle=_normalize_evidence_bundle(evidence_bundle),
            linked_dataset_ids=_normalize_int_list(linked_dataset_ids),
            linked_supply_chain_ids=_normalize_int_list(linked_supply_chain_ids),
            linked_note_ids=_normalize_int_list(linked_note_ids),
            linked_thesis_ids=_normalize_int_list(linked_thesis_ids),
            candidate_claim_id=None,
            resolved_claim_id=None,
            claim_count=0,
            conflict_state="none",
            highest_evidence_tier=None,
            last_claim_at=None,
            extra=dict(extra or {}),
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    else:
        row.tracker_key = tracker_key
        row.company_id = int(company.id) if company is not None else row.company_id
        row.company_name = str(company_name or "").strip() or row.company_name
        row.product_name = str(product_name or "").strip() or row.product_name
        row.status = str(status or row.status or "draft").strip().lower() or "draft"
        row.linked_dataset_ids = _merge_int_lists(row.linked_dataset_ids, linked_dataset_ids)
        row.linked_supply_chain_ids = _merge_int_lists(row.linked_supply_chain_ids, linked_supply_chain_ids)
        row.linked_note_ids = _merge_int_lists(row.linked_note_ids, linked_note_ids)
        row.linked_thesis_ids = _merge_int_lists(row.linked_thesis_ids, linked_thesis_ids)
        row.evidence_bundle = _normalize_evidence_bundle(evidence_bundle) or list(row.evidence_bundle or [])
        merged_extra = dict(row.extra or {})
        merged_extra.update(dict(extra or {}))
        row.extra = merged_extra
        row.updated_at = now
    session.flush()
    return row


def list_product_stage_entries(
    session: Session,
    *,
    query: str | None = None,
    company: str | None = None,
    product: str | None = None,
    stage: str | None = None,
    status: str | None = None,
    evidence_tier: str | None = None,
    conflict_state: str | None = None,
    limit: int = 50,
) -> list[ProductStageEntry]:
    q = session.query(ProductStageEntry)
    query_norm = str(query or "").strip()
    if query_norm:
        pattern = f"%{query_norm}%"
        q = q.filter(
            or_(
                ProductStageEntry.company_name.ilike(pattern),
                ProductStageEntry.product_name.ilike(pattern),
                ProductStageEntry.notes.ilike(pattern),
                ProductStageEntry.date_text.ilike(pattern),
            )
        )
    if company:
        q = q.filter(ProductStageEntry.company_name.ilike(f"%{str(company).strip()}%"))
    if product:
        q = q.filter(ProductStageEntry.product_name.ilike(f"%{str(product).strip()}%"))
    if stage:
        q = q.filter(ProductStageEntry.stage == str(stage).strip().lower())
    if status:
        q = q.filter(ProductStageEntry.status == str(status).strip().lower())
    if evidence_tier:
        q = q.filter(ProductStageEntry.highest_evidence_tier == str(evidence_tier).strip())
    if conflict_state:
        q = q.filter(ProductStageEntry.conflict_state == str(conflict_state).strip().lower())
    return (
        q.order_by(ProductStageEntry.last_claim_at.desc().nullslast(), ProductStageEntry.updated_at.desc(), ProductStageEntry.id.desc())
        .limit(max(1, min(int(limit or 50), 200)))
        .all()
    )


def query_product_stage(
    session: Session,
    *,
    company: str | None = None,
    product: str | None = None,
    stage: str | None = None,
    limit: int = 50,
) -> list[ProductStageEntry]:
    return list_product_stage_entries(
        session,
        company=company,
        product=product,
        stage=stage,
        limit=limit,
    )


def get_product_stage_entry(session: Session, entry_id: int) -> ProductStageEntry | None:
    row = session.query(ProductStageEntry).filter_by(id=int(entry_id)).first()
    if row is None:
        return None
    rebuild_product_stage_entry_state(session, row)
    return row


def get_product_stage_claim(session: Session, claim_id: int) -> ProductStageClaim | None:
    return session.query(ProductStageClaim).filter_by(id=int(claim_id)).first()


def list_product_stage_claims(
    session: Session,
    *,
    entry_id: int,
    review_status: str | None = None,
    limit: int = 100,
) -> list[ProductStageClaim]:
    q = session.query(ProductStageClaim).filter(ProductStageClaim.entry_id == int(entry_id))
    if review_status:
        q = q.filter(ProductStageClaim.review_status == str(review_status).strip().lower())
    return (
        q.order_by(ProductStageClaim.created_at.desc(), ProductStageClaim.id.desc())
        .limit(max(1, min(int(limit or 100), 300)))
        .all()
    )


def list_product_stage_history(session: Session, entry_id: int) -> list[ProductStageHistory]:
    return (
        session.query(ProductStageHistory)
        .filter_by(entry_id=int(entry_id))
        .order_by(ProductStageHistory.created_at.desc(), ProductStageHistory.id.desc())
        .all()
    )


def create_product_stage_claim(
    session: Session,
    *,
    entry_id: int | None = None,
    company_name: str | None = None,
    product_name: str | None = None,
    stage: str,
    target_date: date | None = None,
    date_precision: str | None = None,
    date_text: str | None = None,
    certainty: str | None = None,
    evidence_tier: str | None = None,
    provenance_summary: str | None = None,
    summary: str | None = None,
    rationale: str | None = None,
    source_document_id: int | None = None,
    source_date: datetime | None = None,
    linked_dataset_ids: list[int] | None = None,
    linked_supply_chain_ids: list[int] | None = None,
    linked_note_ids: list[int] | None = None,
    linked_thesis_ids: list[int] | None = None,
    automation_source: str | None = None,
    source_kind: str | None = None,
    review_status: str = "draft",
    claim_key: str | None = None,
    extra: dict[str, Any] | None = None,
    history_note: str | None = None,
    entry_status: str | None = None,
) -> ProductStageClaim:
    stage_norm = _normalize_stage(stage)
    precision_norm = _normalize_precision(date_precision, target_date=target_date)
    review_status_norm = str(review_status or "draft").strip().lower() or "draft"
    if review_status_norm not in PRODUCT_STAGE_CLAIM_STATUSES:
        raise ValueError("Unsupported review status.")

    existing_entry = _get_tracker_entry(session, entry_id=entry_id, company_name=company_name, product_name=product_name)
    if existing_entry is None:
        if not company_name or not product_name:
            raise ValueError("company_name and product_name are required when entry_id is not provided.")
        existing_entry = _ensure_tracker_entry(
            session,
            company_name=company_name,
            product_name=product_name,
            stage=stage_norm,
            target_date=target_date,
            date_precision=precision_norm,
            date_text=date_text,
            certainty=certainty,
            status=entry_status or "draft",
            notes=summary,
            evidence_tier=evidence_tier,
            linked_dataset_ids=linked_dataset_ids,
            linked_supply_chain_ids=linked_supply_chain_ids,
            linked_note_ids=linked_note_ids,
            linked_thesis_ids=linked_thesis_ids,
            extra=extra,
        )

    previous_payload = serialize_product_stage_entry(existing_entry)
    tracker_key = existing_entry.tracker_key or build_product_stage_tracker_key(
        company_name=existing_entry.company_name,
        product_name=existing_entry.product_name,
    )
    source_date_value = _claim_source_date(session, source_document_id=source_document_id, source_date=source_date)
    claim_key_value = str(claim_key or "").strip() or build_product_stage_claim_key(
        tracker_key=tracker_key,
        stage=stage_norm,
        target_date=target_date,
        date_text=date_text,
        source_document_id=source_document_id,
        summary=summary,
        evidence_tier=evidence_tier,
        automation_source=automation_source,
    )
    now = utcnow()
    row = session.query(ProductStageClaim).filter_by(claim_key=claim_key_value).first()
    action = "claim_created"
    if row is None:
        row = ProductStageClaim(
            entry_id=int(existing_entry.id),
            claim_key=claim_key_value,
            stage=stage_norm,
            target_date=target_date,
            date_precision=precision_norm,
            date_text=_normalize_text(date_text),
            certainty=_normalize_text(certainty) or "attributed",
            evidence_tier=_normalize_text(evidence_tier),
            provenance_summary=_normalize_text(provenance_summary),
            summary=_normalize_text(summary),
            rationale=_normalize_text(rationale),
            review_status=review_status_norm,
            source_document_id=int(source_document_id) if source_document_id is not None else None,
            source_date=source_date_value,
            linked_dataset_ids=_normalize_int_list(linked_dataset_ids),
            linked_supply_chain_ids=_normalize_int_list(linked_supply_chain_ids),
            linked_note_ids=_normalize_int_list(linked_note_ids),
            linked_thesis_ids=_normalize_int_list(linked_thesis_ids),
            automation_source=_normalize_text(automation_source),
            source_kind=_normalize_text(source_kind),
            extra=dict(extra or {}),
            created_at=now,
            updated_at=now,
            reviewed_at=now if review_status_norm in {"accepted", "rejected", "superseded"} else None,
        )
        session.add(row)
    else:
        row.entry_id = int(existing_entry.id)
        row.stage = stage_norm
        row.target_date = target_date
        row.date_precision = precision_norm
        row.date_text = _normalize_text(date_text)
        row.certainty = _normalize_text(certainty) or row.certainty or "attributed"
        row.evidence_tier = _normalize_text(evidence_tier) or row.evidence_tier
        row.provenance_summary = _normalize_text(provenance_summary) or row.provenance_summary
        row.summary = _normalize_text(summary) or row.summary
        row.rationale = _normalize_text(rationale) or row.rationale
        row.review_status = review_status_norm
        row.source_document_id = int(source_document_id) if source_document_id is not None else row.source_document_id
        row.source_date = source_date_value or row.source_date
        row.linked_dataset_ids = _normalize_int_list(linked_dataset_ids) or list(row.linked_dataset_ids or [])
        row.linked_supply_chain_ids = _normalize_int_list(linked_supply_chain_ids) or list(row.linked_supply_chain_ids or [])
        row.linked_note_ids = _normalize_int_list(linked_note_ids) or list(row.linked_note_ids or [])
        row.linked_thesis_ids = _normalize_int_list(linked_thesis_ids) or list(row.linked_thesis_ids or [])
        row.automation_source = _normalize_text(automation_source) or row.automation_source
        row.source_kind = _normalize_text(source_kind) or row.source_kind
        merged_extra = dict(row.extra or {})
        merged_extra.update(dict(extra or {}))
        row.extra = merged_extra
        row.updated_at = now
        if review_status_norm in {"accepted", "rejected", "superseded"}:
            row.reviewed_at = now
        action = "claim_updated"

    session.flush()
    if review_status_norm == "accepted":
        for other in (
            session.query(ProductStageClaim)
            .filter(
                ProductStageClaim.entry_id == int(existing_entry.id),
                ProductStageClaim.review_status == "accepted",
                ProductStageClaim.id != int(row.id),
            )
            .all()
        ):
            other.review_status = "superseded"
            other.reviewed_at = now

    if entry_status:
        existing_entry.status = str(entry_status).strip().lower() or existing_entry.status
    rebuild_product_stage_entry_state(session, existing_entry)
    current_payload = serialize_product_stage_entry(existing_entry)
    _record_product_stage_history(
        session,
        entry=existing_entry,
        action=action if review_status_norm == "draft" else f"claim_{review_status_norm}",
        previous_payload=previous_payload,
        current_payload={
            "entry": current_payload,
            "claim": serialize_product_stage_claim(row),
        },
        note=history_note or rationale or summary,
        source_document_id=source_document_id,
    )
    session.flush()
    return row


def review_product_stage_claim(
    session: Session,
    claim_id: int,
    *,
    decision: str,
    reviewer_note: str | None = None,
) -> ProductStageClaim:
    row = get_product_stage_claim(session, int(claim_id))
    if row is None:
        raise ValueError("Product stage claim not found.")
    entry = get_product_stage_entry(session, int(row.entry_id))
    if entry is None:
        raise ValueError("Product stage entry not found.")
    previous_payload = serialize_product_stage_entry(entry)
    decision_norm = str(decision or "").strip().lower()
    if decision_norm in {"accept", "approve", "resolved"}:
        row.review_status = "accepted"
    elif decision_norm in {"reject", "dismiss"}:
        row.review_status = "rejected"
    elif decision_norm in {"supersede", "superseded"}:
        row.review_status = "superseded"
    else:
        raise ValueError("Unsupported review decision.")
    row.reviewed_at = utcnow()
    row.updated_at = row.reviewed_at
    if decision_norm in {"accept", "approve", "resolved"}:
        for other in (
            session.query(ProductStageClaim)
            .filter(
                ProductStageClaim.entry_id == int(entry.id),
                ProductStageClaim.review_status == "accepted",
                ProductStageClaim.id != int(row.id),
            )
            .all()
        ):
            other.review_status = "superseded"
            other.reviewed_at = row.reviewed_at
    rebuild_product_stage_entry_state(session, entry)
    _record_product_stage_history(
        session,
        entry=entry,
        action=f"claim_{row.review_status}",
        previous_payload=previous_payload,
        current_payload={
            "entry": serialize_product_stage_entry(entry),
            "claim": serialize_product_stage_claim(row),
        },
        note=reviewer_note,
        source_document_id=row.source_document_id,
    )
    session.flush()
    return row


def update_product_stage_entry(
    session: Session,
    *,
    entry_id: int | None = None,
    company_name: str,
    product_name: str,
    stage: str,
    target_date: date | None = None,
    date_precision: str | None = None,
    date_text: str | None = None,
    certainty: str | None = None,
    status: str | None = None,
    notes: str | None = None,
    evidence_tier: str | None = None,
    evidence_bundle: list[dict[str, Any]] | None = None,
    linked_dataset_ids: list[int] | None = None,
    linked_supply_chain_ids: list[int] | None = None,
    linked_note_ids: list[int] | None = None,
    linked_thesis_ids: list[int] | None = None,
    extra: dict[str, Any] | None = None,
    source_document_id: int | None = None,
    history_note: str | None = None,
) -> ProductStageEntry:
    create_product_stage_claim(
        session,
        entry_id=entry_id,
        company_name=company_name,
        product_name=product_name,
        stage=stage,
        target_date=target_date,
        date_precision=date_precision,
        date_text=date_text,
        certainty=certainty,
        evidence_tier=evidence_tier,
        provenance_summary="Manual override via update_product_stage_entry.",
        summary=notes,
        rationale=history_note,
        source_document_id=source_document_id,
        linked_dataset_ids=linked_dataset_ids,
        linked_supply_chain_ids=linked_supply_chain_ids,
        linked_note_ids=linked_note_ids,
        linked_thesis_ids=linked_thesis_ids,
        automation_source="manual_override",
        source_kind="manual_override",
        review_status="accepted",
        extra=extra,
        history_note=history_note or "Manual override",
        entry_status=status or "review",
    )
    row = _get_tracker_entry(session, entry_id=entry_id, company_name=company_name, product_name=product_name)
    if row is None:
        raise ValueError("Product stage entry not found after manual override.")
    if evidence_bundle is not None:
        row.evidence_bundle = _normalize_evidence_bundle(evidence_bundle)
    if extra:
        merged_extra = dict(row.extra or {})
        merged_extra.update(dict(extra))
        row.extra = merged_extra
    if status:
        row.status = str(status).strip().lower() or row.status
    rebuild_product_stage_entry_state(session, row)
    return row


def list_product_stage_entries_linked_to_note(session: Session, note_id: int) -> list[ProductStageEntry]:
    target = int(note_id)
    out: list[ProductStageEntry] = []
    for row in list_product_stage_entries(session, limit=500):
        if target in {int(value) for value in (row.linked_note_ids or []) if str(value).isdigit()}:
            out.append(row)
    return out


def list_product_stage_entries_linked_to_thesis(session: Session, thesis_id: int) -> list[ProductStageEntry]:
    target = int(thesis_id)
    out: list[ProductStageEntry] = []
    for row in list_product_stage_entries(session, limit=500):
        if target in {int(value) for value in (row.linked_thesis_ids or []) if str(value).isdigit()}:
            out.append(row)
    return out


def link_product_stage_entry_object(
    session: Session,
    *,
    entry_id: int,
    object_type: str,
    object_id: int,
) -> ProductStageEntry:
    row = get_product_stage_entry(session, int(entry_id))
    if row is None:
        raise ValueError("Product stage entry not found.")
    if object_type == "note":
        row.linked_note_ids = _merge_int_lists(row.linked_note_ids, [int(object_id)])
    elif object_type == "thesis":
        row.linked_thesis_ids = _merge_int_lists(row.linked_thesis_ids, [int(object_id)])
    else:
        raise ValueError("Unsupported stage link object_type.")
    row.updated_at = utcnow()
    session.flush()
    return row


def unlink_product_stage_entry_object(
    session: Session,
    *,
    entry_id: int,
    object_type: str,
    object_id: int,
) -> ProductStageEntry:
    row = get_product_stage_entry(session, int(entry_id))
    if row is None:
        raise ValueError("Product stage entry not found.")
    target = int(object_id)
    if object_type == "note":
        row.linked_note_ids = [value for value in (row.linked_note_ids or []) if int(value) != target]
    elif object_type == "thesis":
        row.linked_thesis_ids = [value for value in (row.linked_thesis_ids or []) if int(value) != target]
    else:
        raise ValueError("Unsupported stage link object_type.")
    row.updated_at = utcnow()
    session.flush()
    return row
