from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session, joinedload

from thoth_terminal.db import utcnow
from thoth_terminal.models import AgentAction, Document, Note, Thesis


ACTION_STATUS_PENDING = "pending"
ACTION_STATUS_RUNNING = "running"
ACTION_STATUS_COMPLETED = "completed"
ACTION_STATUS_SKIPPED = "skipped"
# Backward-compatible aliases for older rows and callers.
ACTION_STATUS_DONE = ACTION_STATUS_COMPLETED
ACTION_STATUS_DISMISSED = ACTION_STATUS_SKIPPED
ACTION_OPEN_STATUSES = {ACTION_STATUS_PENDING, ACTION_STATUS_RUNNING}
ACTION_CLOSED_STATUSES = {ACTION_STATUS_COMPLETED, ACTION_STATUS_SKIPPED}
_ACTION_STATUS_ALIASES = {
    ACTION_STATUS_PENDING: {ACTION_STATUS_PENDING},
    ACTION_STATUS_RUNNING: {ACTION_STATUS_RUNNING, "in_progress"},
    ACTION_STATUS_COMPLETED: {ACTION_STATUS_COMPLETED, "done", "complete"},
    ACTION_STATUS_SKIPPED: {ACTION_STATUS_SKIPPED, "dismissed", "skip"},
}


def canonical_action_status(status: str | None) -> str:
    raw = str(status or "").strip().lower()
    if not raw:
        return ACTION_STATUS_PENDING
    for canonical, aliases in _ACTION_STATUS_ALIASES.items():
        if raw in aliases:
            return canonical
    return raw


def action_status_filter_values(status: str | None) -> list[str]:
    canonical = canonical_action_status(status)
    values = _ACTION_STATUS_ALIASES.get(canonical, {canonical})
    return sorted(values)


def list_agent_actions(
    session: Session,
    *,
    guild_id: int | None = None,
    thesis_id: int | None = None,
    kind: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[AgentAction]:
    q = (
        session.query(AgentAction)
        .options(
            joinedload(AgentAction.thesis),
            joinedload(AgentAction.document).joinedload(Document.source),
            joinedload(AgentAction.note),
        )
        .order_by(AgentAction.priority.asc(), AgentAction.updated_at.desc(), AgentAction.id.desc())
    )
    if guild_id is not None:
        q = q.filter(AgentAction.guild_id == int(guild_id))
    if thesis_id is not None:
        q = q.filter(AgentAction.thesis_id == int(thesis_id))
    if kind:
        q = q.filter(AgentAction.kind == str(kind).strip())
    if status:
        q = q.filter(AgentAction.status.in_(action_status_filter_values(status)))
    return q.limit(max(1, min(int(limit or 50), 500))).all()


def upsert_agent_action(
    session: Session,
    *,
    kind: str,
    title: str,
    guild_id: int | None = None,
    thesis_id: int | None = None,
    document_id: int | None = None,
    note_id: int | None = None,
    source_job_id: int | None = None,
    status: str = ACTION_STATUS_PENDING,
    priority: int = 5,
    dedupe_key: str | None = None,
    rationale: str | None = None,
    evidence_score: float | None = None,
    due_at: datetime | None = None,
    payload: dict[str, Any] | None = None,
) -> AgentAction:
    now = utcnow()
    dedupe_val = str(dedupe_key or "").strip() or None
    status_v = canonical_action_status(status)
    action: AgentAction | None = None
    if dedupe_val:
        action = (
            session.query(AgentAction)
            .filter(AgentAction.dedupe_key == dedupe_val)
            .filter(AgentAction.status.in_(list(ACTION_OPEN_STATUSES)))
            .order_by(AgentAction.id.desc())
            .first()
        )
    if action:
        action.title = str(title).strip()[:255]
        action.priority = int(priority)
        action.guild_id = int(guild_id) if guild_id is not None else action.guild_id
        action.thesis_id = int(thesis_id) if thesis_id is not None else action.thesis_id
        action.document_id = int(document_id) if document_id is not None else action.document_id
        action.note_id = int(note_id) if note_id is not None else action.note_id
        action.source_job_id = int(source_job_id) if source_job_id is not None else action.source_job_id
        action.status = status_v or action.status
        action.rationale = str(rationale).strip() if rationale else action.rationale
        action.evidence_score = float(evidence_score) if evidence_score is not None else action.evidence_score
        action.due_at = due_at if due_at is not None else action.due_at
        if isinstance(payload, dict):
            merged = dict(action.payload or {})
            merged.update(payload)
            action.payload = merged
        action.updated_at = now
        return action

    action = AgentAction(
        guild_id=int(guild_id) if guild_id is not None else None,
        kind=str(kind).strip(),
        status=status_v,
        title=str(title).strip()[:255],
        priority=int(priority),
        dedupe_key=dedupe_val,
        rationale=str(rationale).strip() if rationale else None,
        evidence_score=float(evidence_score) if evidence_score is not None else None,
        thesis_id=int(thesis_id) if thesis_id is not None else None,
        document_id=int(document_id) if document_id is not None else None,
        note_id=int(note_id) if note_id is not None else None,
        source_job_id=int(source_job_id) if source_job_id is not None else None,
        due_at=due_at,
        completed_at=None,
        payload=dict(payload or {}),
        created_at=now,
        updated_at=now,
    )
    session.add(action)
    session.flush()
    return action


def update_agent_action_status(
    session: Session,
    *,
    action_id: int,
    status: str,
    resolution_note: str | None = None,
) -> AgentAction | None:
    action = session.query(AgentAction).filter_by(id=int(action_id)).first()
    if not action:
        return None
    status_v = canonical_action_status(status)
    if status_v not in ACTION_OPEN_STATUSES | ACTION_CLOSED_STATUSES:
        raise ValueError("Invalid action status.")
    action.status = status_v
    action.updated_at = utcnow()
    if status_v in ACTION_CLOSED_STATUSES:
        action.completed_at = action.updated_at
    else:
        action.completed_at = None
    if resolution_note:
        payload = dict(action.payload or {})
        payload["resolution_note"] = str(resolution_note).strip()
        action.payload = payload
    return action


def serialize_agent_action(action: AgentAction) -> dict[str, Any]:
    thesis = action.thesis if isinstance(action.thesis, Thesis) else None
    document = action.document if isinstance(action.document, Document) else None
    note = action.note if isinstance(action.note, Note) else None
    payload = action.payload if isinstance(action.payload, dict) else {}
    return {
        "id": int(action.id),
        "guild_id": int(action.guild_id) if action.guild_id is not None else None,
        "kind": str(action.kind),
        "status": canonical_action_status(action.status),
        "title": str(action.title),
        "priority": int(action.priority or 0),
        "dedupe_key": action.dedupe_key,
        "rationale": action.rationale,
        "evidence_score": float(action.evidence_score) if action.evidence_score is not None else None,
        "thesis_id": int(action.thesis_id) if action.thesis_id is not None else None,
        "thesis_title": thesis.title if thesis else None,
        "document_id": int(action.document_id) if action.document_id is not None else None,
        "document_title": document.title if document else None,
        "document_source": document.source.name if document and document.source else None,
        "note_id": int(action.note_id) if action.note_id is not None else None,
        "note_title": note.title if note else None,
        "source_job_id": int(action.source_job_id) if action.source_job_id is not None else None,
        "due_at": action.due_at.isoformat() if action.due_at else None,
        "completed_at": action.completed_at.isoformat() if action.completed_at else None,
        "created_at": action.created_at.isoformat() if action.created_at else None,
        "updated_at": action.updated_at.isoformat() if action.updated_at else None,
        "payload": payload,
    }
