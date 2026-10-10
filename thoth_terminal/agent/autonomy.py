from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session, joinedload

from thoth_terminal.db import utcnow
from thoth_terminal.models import AgentChangeEvent, AgentChangeRun, Note, Thesis, ThesisProposal
from thoth_terminal.notes_service import create_note
from thoth_terminal.thesis_service import create_thesis


RUN_STATUS_RUNNING = "running"
RUN_STATUS_SUCCESS = "success"
RUN_STATUS_ERROR = "error"
PROPOSAL_STATUS_PENDING = "pending"
PROPOSAL_STATUS_REJECTED = "rejected"
PROPOSAL_STATUS_PROMOTED = "promoted"


def start_change_run(
    session: Session,
    *,
    task_kind: str,
    engine_kind: str,
    guild_id: int | None = None,
    automation_source: str | None = None,
    note_id: int | None = None,
    source_job_id: int | None = None,
    dedupe_key: str | None = None,
    payload: dict[str, Any] | None = None,
) -> AgentChangeRun:
    now = utcnow()
    dedupe = str(dedupe_key or "").strip() or None
    if dedupe:
        existing = (
            session.query(AgentChangeRun)
            .filter(AgentChangeRun.dedupe_key == dedupe)
            .filter(AgentChangeRun.status == RUN_STATUS_RUNNING)
            .order_by(AgentChangeRun.id.desc())
            .first()
        )
        if existing:
            return existing
    run = AgentChangeRun(
        guild_id=int(guild_id) if guild_id is not None else None,
        task_kind=str(task_kind).strip(),
        status=RUN_STATUS_RUNNING,
        engine_kind=str(engine_kind).strip() or "legacy_internal",
        automation_source=str(automation_source).strip() if automation_source else None,
        note_id=int(note_id) if note_id is not None else None,
        source_job_id=int(source_job_id) if source_job_id is not None else None,
        dedupe_key=dedupe,
        summary=None,
        payload=dict(payload or {}),
        created_at=now,
        updated_at=now,
        completed_at=None,
    )
    session.add(run)
    session.flush()
    return run


def record_change_event(
    session: Session,
    *,
    run: AgentChangeRun,
    object_type: str,
    object_id: int | None,
    change_kind: str,
    before_summary: str | None = None,
    after_summary: str | None = None,
    confidence: float | None = None,
    rationale: str | None = None,
    dedupe_key: str | None = None,
    payload: dict[str, Any] | None = None,
) -> AgentChangeEvent:
    dedupe = str(dedupe_key or "").strip() or None
    if dedupe:
        existing = (
            session.query(AgentChangeEvent)
            .filter(AgentChangeEvent.run_id == int(run.id))
            .filter(AgentChangeEvent.dedupe_key == dedupe)
            .first()
        )
        if existing:
            return existing
    row = AgentChangeEvent(
        run_id=int(run.id),
        object_type=str(object_type).strip(),
        object_id=int(object_id) if object_id is not None else None,
        change_kind=str(change_kind).strip(),
        before_summary=str(before_summary).strip() if before_summary else None,
        after_summary=str(after_summary).strip() if after_summary else None,
        confidence=float(confidence) if confidence is not None else None,
        rationale=str(rationale).strip() if rationale else None,
        dedupe_key=dedupe,
        payload=dict(payload or {}),
        created_at=utcnow(),
    )
    run.updated_at = utcnow()
    session.add(row)
    session.flush()
    return row


def finish_change_run(
    session: Session,
    *,
    run: AgentChangeRun,
    status: str,
    summary: str | None = None,
    note_id: int | None = None,
    payload: dict[str, Any] | None = None,
) -> AgentChangeRun:
    run.status = str(status or RUN_STATUS_SUCCESS).strip().lower() or RUN_STATUS_SUCCESS
    run.summary = str(summary).strip() if summary else run.summary
    if note_id is not None:
        run.note_id = int(note_id)
    if isinstance(payload, dict):
        merged = dict(run.payload or {})
        merged.update(payload)
        run.payload = merged
    run.updated_at = utcnow()
    run.completed_at = run.updated_at
    session.flush()
    return run


def create_thesis_proposal(
    session: Session,
    *,
    title: str,
    statement: str,
    entity_tokens: list[str],
    guild_id: int | None = None,
    kind: str = "claim",
    importance: int = 3,
    stale_after_days: int | None = 90,
    novelty_score: float | None = None,
    confidence: float | None = None,
    rationale: str | None = None,
    evidence: list[dict[str, Any]] | None = None,
    source_job_id: int | None = None,
    client_request_id: str | None = None,
    note_body_md: str | None = None,
    payload: dict[str, Any] | None = None,
) -> ThesisProposal:
    cid = str(client_request_id or "").strip() or None
    if cid:
        existing = session.query(ThesisProposal).filter_by(client_request_id=cid).first()
        if existing:
            return existing
    note: Note | None = None
    note_body = str(note_body_md or "").strip()
    if note_body:
        note = create_note(
            session,
            title=f"Thesis Proposal — {str(title).strip()[:180]}",
            body_md=note_body,
            note_type="general",
            importance=3,
            entity_tokens=[str(v).strip() for v in entity_tokens if str(v).strip()] or None,
            client_request_id=(f"{cid}:note" if cid else None),
            extra={"agent": {"kind": "thesis_proposal"}},
        )
    now = utcnow()
    row = ThesisProposal(
        guild_id=int(guild_id) if guild_id is not None else None,
        status=PROPOSAL_STATUS_PENDING,
        title=str(title).strip(),
        statement=str(statement).strip(),
        kind=str(kind).strip() or "claim",
        importance=int(importance),
        stale_after_days=int(stale_after_days) if stale_after_days is not None else None,
        novelty_score=float(novelty_score) if novelty_score is not None else None,
        confidence=float(confidence) if confidence is not None else None,
        rationale=str(rationale).strip() if rationale else None,
        note_id=int(note.id) if note is not None else None,
        source_job_id=int(source_job_id) if source_job_id is not None else None,
        approved_thesis_id=None,
        client_request_id=cid,
        payload={
            **dict(payload or {}),
            "entity_tokens": [str(v).strip() for v in entity_tokens if str(v).strip()],
            "evidence": list(evidence or []),
        },
        created_at=now,
        updated_at=now,
        reviewed_at=None,
    )
    session.add(row)
    session.flush()
    return row


def list_change_runs(
    session: Session,
    *,
    guild_id: int | None = None,
    task_kind: str | None = None,
    limit: int = 20,
) -> list[AgentChangeRun]:
    q = session.query(AgentChangeRun).options(joinedload(AgentChangeRun.note)).order_by(AgentChangeRun.created_at.desc(), AgentChangeRun.id.desc())
    if guild_id is not None:
        q = q.filter(AgentChangeRun.guild_id == int(guild_id))
    if task_kind:
        q = q.filter(AgentChangeRun.task_kind == str(task_kind).strip())
    return q.limit(max(1, min(int(limit or 20), 100))).all()


def get_change_run(session: Session, *, run_id: int) -> AgentChangeRun | None:
    return (
        session.query(AgentChangeRun)
        .options(joinedload(AgentChangeRun.note), joinedload(AgentChangeRun.changes))
        .filter(AgentChangeRun.id == int(run_id))
        .first()
    )


def list_thesis_proposals(
    session: Session,
    *,
    status: str | None = None,
    guild_id: int | None = None,
    limit: int = 50,
) -> list[ThesisProposal]:
    q = session.query(ThesisProposal).options(joinedload(ThesisProposal.note), joinedload(ThesisProposal.approved_thesis))
    if status:
        q = q.filter(ThesisProposal.status == str(status).strip())
    if guild_id is not None:
        q = q.filter(ThesisProposal.guild_id == int(guild_id))
    q = q.order_by(ThesisProposal.updated_at.desc(), ThesisProposal.id.desc())
    return q.limit(max(1, min(int(limit or 50), 200))).all()


def review_thesis_proposal(
    session: Session,
    *,
    proposal_id: int,
    decision: str,
    reviewer_note: str | None = None,
) -> ThesisProposal:
    row = (
        session.query(ThesisProposal)
        .options(joinedload(ThesisProposal.note), joinedload(ThesisProposal.approved_thesis))
        .filter(ThesisProposal.id == int(proposal_id))
        .first()
    )
    if not row:
        raise ValueError("Proposal not found.")
    decision_v = str(decision or "").strip().lower()
    if decision_v not in {"approve", "reject"}:
        raise ValueError("decision must be approve or reject.")
    if row.status == PROPOSAL_STATUS_PROMOTED and row.approved_thesis_id:
        return row
    now = utcnow()
    if decision_v == "reject":
        row.status = PROPOSAL_STATUS_REJECTED
        row.reviewed_at = now
        row.updated_at = now
        payload = dict(row.payload or {})
        if reviewer_note:
            payload["reviewer_note"] = str(reviewer_note).strip()
        row.payload = payload
        session.flush()
        return row

    payload = dict(row.payload or {})
    entity_tokens = [str(v).strip() for v in payload.get("entity_tokens") or [] if str(v).strip()]
    thesis = create_thesis(
        session,
        title=row.title,
        statement=row.statement,
        kind=row.kind,
        status="watching",
        importance=int(row.importance or 3),
        stale_after_days=int(row.stale_after_days) if row.stale_after_days is not None else None,
        entity_tokens=entity_tokens,
        domain_slugs=[str(v).strip() for v in payload.get("domains") or [] if str(v).strip()],
        tag_slugs=[str(v).strip() for v in payload.get("tags") or [] if str(v).strip()],
        event_type_slugs=[str(v).strip() for v in payload.get("event_types") or [] if str(v).strip()],
        client_request_id=(f"proposal:{int(row.id)}"),
        extra={
            "proposal_id": int(row.id),
            "proposal_confidence": float(row.confidence) if row.confidence is not None else None,
            "proposal_novelty_score": float(row.novelty_score) if row.novelty_score is not None else None,
        },
    )
    row.status = PROPOSAL_STATUS_PROMOTED
    row.approved_thesis_id = int(thesis.id)
    row.reviewed_at = now
    row.updated_at = now
    if reviewer_note:
        payload["reviewer_note"] = str(reviewer_note).strip()
    row.payload = payload
    session.flush()
    return row


def serialize_change_event(row: AgentChangeEvent) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "run_id": int(row.run_id),
        "object_type": str(row.object_type),
        "object_id": int(row.object_id) if row.object_id is not None else None,
        "change_kind": str(row.change_kind),
        "before_summary": row.before_summary,
        "after_summary": row.after_summary,
        "confidence": float(row.confidence) if row.confidence is not None else None,
        "rationale": row.rationale,
        "dedupe_key": row.dedupe_key,
        "payload": dict(row.payload or {}),
        "created_at": row.created_at.isoformat() if isinstance(row.created_at, datetime) else None,
    }


def serialize_change_run(row: AgentChangeRun, *, include_changes: bool = False) -> dict[str, Any]:
    data = {
        "id": int(row.id),
        "guild_id": int(row.guild_id) if row.guild_id is not None else None,
        "task_kind": str(row.task_kind),
        "status": str(row.status),
        "engine_kind": str(row.engine_kind),
        "automation_source": row.automation_source,
        "note_id": int(row.note_id) if row.note_id is not None else None,
        "source_job_id": int(row.source_job_id) if row.source_job_id is not None else None,
        "dedupe_key": row.dedupe_key,
        "summary": row.summary,
        "payload": dict(row.payload or {}),
        "created_at": row.created_at.isoformat() if isinstance(row.created_at, datetime) else None,
        "updated_at": row.updated_at.isoformat() if isinstance(row.updated_at, datetime) else None,
        "completed_at": row.completed_at.isoformat() if isinstance(row.completed_at, datetime) else None,
    }
    if include_changes:
        data["changes"] = [serialize_change_event(ch) for ch in (row.changes or [])]
    return data


def serialize_thesis_proposal(row: ThesisProposal) -> dict[str, Any]:
    payload = dict(row.payload or {})
    return {
        "id": int(row.id),
        "guild_id": int(row.guild_id) if row.guild_id is not None else None,
        "status": str(row.status),
        "title": str(row.title),
        "statement": str(row.statement),
        "kind": str(row.kind),
        "importance": int(row.importance or 0),
        "stale_after_days": int(row.stale_after_days) if row.stale_after_days is not None else None,
        "novelty_score": float(row.novelty_score) if row.novelty_score is not None else None,
        "confidence": float(row.confidence) if row.confidence is not None else None,
        "rationale": row.rationale,
        "note_id": int(row.note_id) if row.note_id is not None else None,
        "source_job_id": int(row.source_job_id) if row.source_job_id is not None else None,
        "approved_thesis_id": int(row.approved_thesis_id) if row.approved_thesis_id is not None else None,
        "entity_tokens": [str(v).strip() for v in payload.get("entity_tokens") or [] if str(v).strip()],
        "evidence": list(payload.get("evidence") or []),
        "payload": payload,
        "created_at": row.created_at.isoformat() if isinstance(row.created_at, datetime) else None,
        "updated_at": row.updated_at.isoformat() if isinstance(row.updated_at, datetime) else None,
        "reviewed_at": row.reviewed_at.isoformat() if isinstance(row.reviewed_at, datetime) else None,
    }
