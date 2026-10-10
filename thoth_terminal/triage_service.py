from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from thoth_terminal.config import settings
from thoth_terminal.db import utcnow
from thoth_terminal.models import Document, DocumentEvent

TRIAGE_NEW = "NEW"
TRIAGE_OPENED = "OPENED"
TRIAGE_ACTIONED = "ACTIONED"
TRIAGE_IGNORED = "IGNORED"
TRIAGE_ARCHIVED = "ARCHIVED"


def _log_event(
    session: Session,
    *,
    document_id: int,
    event_type: str,
    meta: dict[str, Any] | None = None,
) -> None:
    session.add(
        DocumentEvent(
            document_id=int(document_id),
            event_type=event_type,
            event_at=utcnow(),
            meta=meta or {},
        )
    )


def _get_doc(session: Session, document_id: int) -> Document:
    doc = session.query(Document).filter_by(id=int(document_id)).first()
    if not doc:
        raise ValueError(f"Document not found: {document_id}")
    return doc


def mark_opened(
    session: Session,
    *,
    document_id: int,
    meta: dict[str, Any] | None = None,
) -> Document:
    doc = _get_doc(session, document_id)
    now = utcnow()

    changed = False
    if doc.triage_state == TRIAGE_NEW:
        doc.triage_state = TRIAGE_OPENED
        changed = True

    if doc.opened_at is None:
        doc.opened_at = now
        changed = True

    if changed:
        doc.triage_updated_at = now
        _log_event(session, document_id=doc.id, event_type="OPENED", meta=meta)

    return doc


def mark_saved(
    session: Session,
    *,
    document_id: int,
    meta: dict[str, Any] | None = None,
) -> Document:
    doc = _get_doc(session, document_id)
    if doc.saved:
        return doc
    now = utcnow()
    doc.saved = True
    doc.saved_at = now
    doc.triage_updated_at = now
    _log_event(session, document_id=doc.id, event_type="SAVED", meta=meta)
    return doc


def mark_unsaved(
    session: Session,
    *,
    document_id: int,
    meta: dict[str, Any] | None = None,
) -> Document:
    doc = _get_doc(session, document_id)
    if not doc.saved:
        return doc
    now = utcnow()
    doc.saved = False
    doc.saved_at = None
    doc.triage_updated_at = now
    _log_event(session, document_id=doc.id, event_type="UNSAVED", meta=meta)
    return doc


def mark_actioned(
    session: Session,
    *,
    document_id: int,
    meta: dict[str, Any] | None = None,
) -> Document:
    doc = _get_doc(session, document_id)
    if doc.triage_state == TRIAGE_IGNORED:
        return doc
    now = utcnow()
    if doc.triage_state != TRIAGE_ACTIONED:
        doc.triage_state = TRIAGE_ACTIONED
        doc.actioned_at = now
        doc.triage_updated_at = now
        _log_event(session, document_id=doc.id, event_type="ACTIONED", meta=meta)
    return doc


def mark_ignored(
    session: Session,
    *,
    document_id: int,
    meta: dict[str, Any] | None = None,
) -> Document:
    doc = _get_doc(session, document_id)
    now = utcnow()
    if doc.triage_state != TRIAGE_IGNORED:
        doc.triage_state = TRIAGE_IGNORED
        doc.ignored_at = now
        doc.triage_updated_at = now
        if settings.triage_unsave_on_ignore:
            doc.saved = False
            doc.saved_at = None
        _log_event(session, document_id=doc.id, event_type="IGNORED", meta=meta)
    return doc


def mark_archived(
    session: Session,
    *,
    document_id: int,
    meta: dict[str, Any] | None = None,
) -> Document:
    doc = _get_doc(session, document_id)
    now = utcnow()
    if doc.triage_state != TRIAGE_ARCHIVED:
        doc.triage_state = TRIAGE_ARCHIVED
        doc.triage_updated_at = now
        doc.storage_tier = "warm"
        _log_event(session, document_id=doc.id, event_type="ARCHIVED", meta=meta)
    return doc


def reset_to_new(
    session: Session,
    *,
    document_id: int,
    meta: dict[str, Any] | None = None,
) -> Document:
    doc = _get_doc(session, document_id)
    now = utcnow()
    doc.triage_state = TRIAGE_NEW
    doc.opened_at = None
    doc.actioned_at = None
    doc.ignored_at = None
    doc.triage_updated_at = now
    _log_event(session, document_id=doc.id, event_type="STATE_RESET", meta=meta)
    return doc


def mark_seen(
    session: Session,
    *,
    document_id: int,
    meta: dict[str, Any] | None = None,
) -> Document:
    doc = _get_doc(session, document_id)
    if doc.seen_at is not None:
        return doc
    now = utcnow()
    doc.seen_at = now
    doc.triage_updated_at = now
    _log_event(session, document_id=doc.id, event_type="SEEN", meta=meta)
    return doc


def bulk_apply(
    session: Session,
    *,
    document_ids: list[int],
    action: str,
    meta: dict[str, Any] | None = None,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for doc_id in document_ids:
        try:
            if action == "save":
                doc = mark_saved(session, document_id=doc_id, meta=meta)
            elif action == "unsave":
                doc = mark_unsaved(session, document_id=doc_id, meta=meta)
            elif action == "ignore":
                doc = mark_ignored(session, document_id=doc_id, meta=meta)
            elif action == "archive":
                doc = mark_archived(session, document_id=doc_id, meta=meta)
            elif action == "actioned":
                doc = mark_actioned(session, document_id=doc_id, meta=meta)
            elif action == "reset_to_new":
                doc = reset_to_new(session, document_id=doc_id, meta=meta)
            elif action == "open":
                doc = mark_opened(session, document_id=doc_id, meta=meta)
            elif action == "seen":
                doc = mark_seen(session, document_id=doc_id, meta=meta)
            else:
                raise ValueError(f"Unknown action: {action}")

            key = doc.triage_state or TRIAGE_NEW
            counts[key] = counts.get(key, 0) + 1
        except Exception:
            counts["fail"] = counts.get("fail", 0) + 1
    return counts
