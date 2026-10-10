from __future__ import annotations

import hashlib
import re
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session, selectinload

from thoth_terminal.db import utcnow
from thoth_terminal.models import InformalResearchDocument, InformalResearchDocumentEvent


_FALLBACK_WRITE_OPS = {"research_doc_write", "research_write", "write_research_destination"}


def _text(value: object, *, limit: int | None = None) -> str:
    out = str(value or "").strip()
    if limit is not None and len(out) > limit:
        return out[:limit].rstrip()
    return out


def _slugify(value: object, *, default: str = "research-note", limit: int = 180) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return (slug or default)[:limit].strip("-") or default


def _hash_short(value: object) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16]


def _word_count(value: object) -> int:
    return len(re.findall(r"\b[\w$%./+-]+\b", _text(value)))


def _append_content(existing: str, addition: str) -> tuple[str, int]:
    existing_text = _text(existing)
    addition_text = _text(addition)
    if not addition_text:
        return existing_text, 0
    if addition_text in existing_text:
        return existing_text, 0
    if not existing_text:
        return addition_text, _word_count(addition_text)
    return f"{existing_text}\n\n---\n\n{addition_text}", _word_count(addition_text)


def _payload_content(payload: dict[str, Any]) -> str:
    return (
        _text(payload.get("content_md"))
        or _text(payload.get("append_md"))
        or _text(payload.get("body_md"))
        or _text(payload.get("new_information"))
        or _text(payload.get("summary"))
    )


def _payload_title(payload: dict[str, Any]) -> str:
    destination = payload.get("destination") if isinstance(payload.get("destination"), dict) else {}
    return (
        _text(payload.get("title"), limit=255)
        or _text(payload.get("write_summary"), limit=255)
        or _text(destination.get("subject"), limit=255)
        or _text(payload.get("subject"), limit=255)
        or "Informal research note"
    )


def _document_key(payload: dict[str, Any], *, title: str, subject: str | None, category: str | None) -> str:
    explicit = _text(payload.get("doc_key") or payload.get("document_key"), limit=255)
    if explicit:
        return explicit
    basis = "|".join(part for part in (category or "informal", subject or "", title) if part)
    return f"informal:{_slugify(basis, default='research-note', limit=190)}:{_hash_short(basis)}"


def upsert_informal_research_doc(
    session: Session,
    *,
    payload: dict[str, Any],
    actor: str = "cloud_heartbeat",
    cloud_run_id: int | None = None,
    operation_index: int | None = None,
    event_kind: str = "cloud_note",
    failure_reason: str | None = None,
    source_operation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = utcnow()
    destination = payload.get("destination") if isinstance(payload.get("destination"), dict) else {}
    promotion_target = payload.get("promotion_target") if isinstance(payload.get("promotion_target"), dict) else destination
    retry_payload = payload.get("retry_payload") if isinstance(payload.get("retry_payload"), dict) else {}
    title = _payload_title(payload)
    subject = _text(payload.get("subject") or destination.get("subject"), limit=255) or None
    category = _text(payload.get("category") or destination.get("type") or payload.get("destination_type"), limit=64) or None
    content_md = _payload_content(payload)
    summary = _text(payload.get("summary") or payload.get("write_summary") or title) or None
    doc_key = _document_key(payload, title=title, subject=subject, category=category)

    row = session.query(InformalResearchDocument).filter(InformalResearchDocument.doc_key == doc_key).first()
    created = False
    before_words = _word_count(row.content_md) if row is not None else 0
    if row is None:
        row = InformalResearchDocument(
            doc_key=doc_key,
            title=title,
            subject=subject,
            category=category,
            status=_text(payload.get("status"), limit=32) or "active",
            summary=summary,
            content_md="",
            source_run_id=cloud_run_id,
            source_operation_index=operation_index,
            failed_operation=dict(source_operation or payload.get("failed_operation") or {}),
            failure_reason=failure_reason or _text(payload.get("failure_reason")) or None,
            retry_payload=dict(retry_payload),
            promotion_target=dict(promotion_target or {}),
            word_count=0,
            payload=dict(payload.get("payload") or {}) if isinstance(payload.get("payload"), dict) else {},
            created_by=actor,
            created_at=now,
            updated_at=now,
            last_material_update_at=None,
        )
        session.add(row)
        session.flush()
        created = True
    else:
        row.title = title or row.title
        row.subject = subject or row.subject
        row.category = category or row.category
        row.summary = summary or row.summary
        row.failed_operation = dict(source_operation or payload.get("failed_operation") or row.failed_operation or {})
        row.failure_reason = failure_reason or _text(payload.get("failure_reason")) or row.failure_reason
        if retry_payload:
            row.retry_payload = dict(retry_payload)
        if promotion_target:
            row.promotion_target = dict(promotion_target)
        row.updated_at = now

    row.content_md, words_added = _append_content(row.content_md, content_md)
    after_words = _word_count(row.content_md)
    row.word_count = after_words
    if words_added:
        row.last_material_update_at = now

    event = InformalResearchDocumentEvent(
        document_id=int(row.id),
        source_run_id=cloud_run_id,
        event_kind=event_kind,
        summary=summary,
        content_md=content_md or None,
        failure_reason=failure_reason or _text(payload.get("failure_reason")) or None,
        source_operation=dict(source_operation or {}),
        payload={
            "created": created,
            "words_before": before_words,
            "words_after": after_words,
            "words_added": words_added,
            "promotion_target": dict(promotion_target or {}),
            "retry_payload": dict(retry_payload),
        },
        created_by=actor,
        created_at=now,
    )
    session.add(event)
    session.flush()
    return {
        "document": row,
        "event": event,
        "created": created,
        "words_added": words_added,
        "words_before": before_words,
        "words_after": after_words,
    }


def fallback_failed_durable_write_to_informal_doc(
    session: Session,
    *,
    op: str,
    raw_operation: dict[str, Any],
    payload: dict[str, Any],
    error: str,
    cloud_run_id: int,
    operation_index: int,
    actor: str = "cloud_heartbeat",
) -> dict[str, Any] | None:
    if op not in _FALLBACK_WRITE_OPS:
        return None
    content_md = _payload_content(payload)
    if _word_count(content_md) < 8:
        return None
    destination = payload.get("destination") if isinstance(payload.get("destination"), dict) else {}
    fallback_payload = {
        **payload,
        "title": _payload_title(payload),
        "subject": _text(payload.get("subject") or destination.get("subject") or payload.get("write_summary"), limit=255),
        "category": _text(payload.get("category") or destination.get("type") or "failed_durable_write", limit=64),
        "summary": _text(payload.get("write_summary") or payload.get("summary") or payload.get("new_information")) or f"Canonical {op} failed.",
        "content_md": content_md,
        "failure_reason": error,
        "failed_operation": dict(raw_operation),
        "retry_payload": dict(raw_operation),
        "promotion_target": dict(destination or {}),
    }
    result = upsert_informal_research_doc(
        session,
        payload=fallback_payload,
        actor=actor,
        cloud_run_id=int(cloud_run_id),
        operation_index=int(operation_index),
        event_kind="canonical_write_fallback",
        failure_reason=error,
        source_operation=dict(raw_operation),
    )
    row = result["document"]
    event = result["event"]
    return {
        "op": "informal_research_doc",
        "source_op": op,
        "informal_doc_id": int(row.id),
        "informal_doc_key": row.doc_key,
        "title": row.title,
        "subject": row.subject,
        "category": row.category,
        "event_id": int(event.id),
        "words_added": int(result.get("words_added") or 0),
        "failure_reason": error,
        "retry_available": bool(row.retry_payload),
        "promotion_target": row.promotion_target,
    }


def summarize_informal_research_doc_result(result: dict[str, Any]) -> dict[str, Any]:
    row = result["document"]
    event = result["event"]
    return {
        "informal_doc_id": int(row.id),
        "informal_doc_key": row.doc_key,
        "title": row.title,
        "subject": row.subject,
        "category": row.category,
        "event_id": int(event.id),
        "words_added": int(result.get("words_added") or 0),
        "word_count": int(row.word_count or 0),
        "failure_reason": row.failure_reason,
        "retry_available": bool(row.retry_payload),
        "promotion_target": row.promotion_target,
    }


def list_informal_research_docs(
    session: Session,
    *,
    q: str | None = None,
    status: str | None = None,
    category: str | None = None,
    limit: int = 200,
) -> list[InformalResearchDocument]:
    query = session.query(InformalResearchDocument).options(selectinload(InformalResearchDocument.events))
    if q:
        pattern = f"%{q.strip()}%"
        query = query.filter(
            or_(
                InformalResearchDocument.title.ilike(pattern),
                InformalResearchDocument.subject.ilike(pattern),
                InformalResearchDocument.summary.ilike(pattern),
                InformalResearchDocument.content_md.ilike(pattern),
            )
        )
    if status:
        query = query.filter(InformalResearchDocument.status == status)
    if category:
        query = query.filter(InformalResearchDocument.category == category)
    return (
        query.order_by(InformalResearchDocument.updated_at.desc(), InformalResearchDocument.id.desc())
        .limit(max(1, min(int(limit or 200), 500)))
        .all()
    )


def get_informal_research_doc(session: Session, doc_key_or_id: str) -> InformalResearchDocument | None:
    query = session.query(InformalResearchDocument).options(selectinload(InformalResearchDocument.events))
    if str(doc_key_or_id).isdigit():
        return query.filter(InformalResearchDocument.id == int(doc_key_or_id)).first()
    return query.filter(InformalResearchDocument.doc_key == doc_key_or_id).first()
