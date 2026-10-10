"""Route invited users' uploaded evidence into operator research intake."""

from __future__ import annotations

import hashlib

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from thoth_terminal.db import utcnow
from thoth_terminal.models import (
    AnalystEvidence, CloudGoalEvent, CloudGoalRun, Document, ExternalAgentEvidence,
    ExternalAgentPrincipal, ExternalAgentResearchRequest, InformalResearchDocumentEvent,
    InteractiveAgentOperation, LocalAgentWriteDiff, RoadmapMention,
)


def queue_evidence_request(
    session: Session, *, principal_id: int, document_id: int,
    request_key: str = "", request_text: str = "",
) -> ExternalAgentResearchRequest:
    grant = session.query(ExternalAgentEvidence).filter_by(
        principal_id=int(principal_id), document_id=int(document_id),
    ).first()
    if grant is None:
        raise ValueError("Evidence document does not belong to this principal")
    text = str(request_text or "").strip()
    if len(text) > 1200:
        raise ValueError("Research request must be at most 1200 characters")
    key = str(request_key or "").strip() or hashlib.sha256(f"document:{int(document_id)}".encode()).hexdigest()[:32]
    if len(key) > 120:
        raise ValueError("request_key must be at most 120 characters")
    previous = session.query(ExternalAgentResearchRequest).filter_by(principal_id=int(principal_id), request_key=key).first()
    if previous is not None:
        if previous.document_id != int(document_id) or previous.request_text != text:
            raise ValueError("request_key was already used for a different research request")
        return previous
    row = ExternalAgentResearchRequest(
        principal_id=int(principal_id), document_id=int(document_id), request_key=key,
        request_text=text, status="submitted", created_at=utcnow(),
    )
    session.add(row)
    session.flush()
    return row


def queue_operator_evidence_request(
    session: Session, *, document_id: int, request_key: str = "", request_text: str = "",
) -> ExternalAgentResearchRequest:
    """Route an owner agent's existing source through the same research queue."""
    document = session.get(Document, int(document_id))
    if document is None:
        raise ValueError("Source document not found")
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        insert = pg_insert
    elif dialect == "sqlite":
        insert = sqlite_insert
    else:
        raise ValueError("Research intake requires PostgreSQL or SQLite")
    now = utcnow()
    session.execute(insert(ExternalAgentPrincipal).values(
        slug="owner-agent", display_name="Owner agent", status="active",
        created_at=now, updated_at=now,
    ).on_conflict_do_nothing(index_elements=["slug"]))
    principal = session.query(ExternalAgentPrincipal).filter_by(slug="owner-agent").one()
    if principal.status != "active":
        raise ValueError("Owner agent principal is disabled")
    session.execute(insert(ExternalAgentEvidence).values(
        principal_id=int(principal.id), document_id=int(document.id), created_at=now,
    ).on_conflict_do_nothing(index_elements=["principal_id", "document_id"]))
    return queue_evidence_request(
        session, principal_id=int(principal.id), document_id=int(document.id),
        request_key=request_key, request_text=request_text,
    )


def serialize_evidence_request(session: Session, row: ExternalAgentResearchRequest) -> dict[str, object]:
    principal = session.query(ExternalAgentPrincipal).filter_by(id=row.principal_id).one()
    document = session.query(Document).filter_by(id=row.document_id).one()
    return {
        "request_id": int(row.id), "principal": principal.slug,
        "document_id": int(row.document_id), "document_title": document.title,
        "request_text": row.request_text, "status": row.status,
        "created_at": row.created_at.isoformat(), "operator_note": row.operator_note,
        "resolution_operation_key": row.resolution_operation_key,
    }


def list_evidence_requests(session: Session, *, status: str = "submitted", limit: int = 20,
                           include_owner_requests: bool = True) -> list[dict[str, object]]:
    if status not in {"submitted", "reviewed", "declined", "all"}:
        raise ValueError("Invalid research request status")
    query = session.query(ExternalAgentResearchRequest)
    if not include_owner_requests:
        query = query.join(ExternalAgentPrincipal, ExternalAgentPrincipal.id == ExternalAgentResearchRequest.principal_id).filter(
            ExternalAgentPrincipal.slug != "owner-agent",
        )
    if status != "all":
        query = query.filter(ExternalAgentResearchRequest.status == status)
    rows = query.order_by(ExternalAgentResearchRequest.created_at.asc()).limit(max(1, min(int(limit), 50))).all()
    return [serialize_evidence_request(session, row) for row in rows]


def worldview_resolution_key(cloud_run_id: int, write_operation_key: str) -> str:
    """Bounded reference to a run-scoped write without copying its capability."""
    if int(cloud_run_id) < 1 or not str(write_operation_key or "").strip():
        raise ValueError("A worldview run and write operation key are required")
    digest = hashlib.sha256(str(write_operation_key).encode()).hexdigest()[:24]
    return f"worldview:{int(cloud_run_id)}:{digest}"


def _worldview_cited_documents(session: Session, resolution_key: str) -> set[int]:
    parts = resolution_key.split(":")
    if len(parts) != 3 or parts[0] != "worldview" or not parts[1].isdigit() or len(parts[2]) != 24:
        return set()
    run_id = int(parts[1])
    run = session.get(CloudGoalRun, run_id)
    if run is None or run.task_kind != "cloud_research_heartbeat" or run.status not in {"running", "complete"}:
        return set()
    events = session.query(CloudGoalEvent).filter(
        CloudGoalEvent.run_id == run_id,
        CloudGoalEvent.event_kind.in_({
            "worldview_write", "worldview_consensus_evidence",
            "worldview_roadmap_update", "worldview_journal_write",
        }),
    ).all()
    cited: set[int] = set()
    for event in events:
        payload = dict(event.payload or {})
        operation_key = str(payload.get("operation_key") or "")
        if hashlib.sha256(operation_key.encode()).hexdigest()[:24] != parts[2]:
            continue
        receipt = dict(payload.get("receipt") or {})
        if receipt.get("write_diff_id"):
            row = session.get(LocalAgentWriteDiff, int(receipt["write_diff_id"]))
            if row:
                cited.update(int(value) for value in (row.source_document_ids_json or []) if isinstance(value, int))
        if receipt.get("analyst_evidence_id"):
            row = session.get(AnalystEvidence, int(receipt["analyst_evidence_id"]))
            if row and row.source_document_id:
                cited.add(int(row.source_document_id))
        if receipt.get("mention_id"):
            row = session.get(RoadmapMention, int(receipt["mention_id"]))
            if row and row.source_document_id:
                cited.add(int(row.source_document_id))
        if receipt.get("event_id"):
            row = session.get(InformalResearchDocumentEvent, int(receipt["event_id"]))
            if row:
                cited.update(int(value) for value in (row.payload or {}).get("source_document_ids", []) if isinstance(value, int))
    return cited


def resolve_evidence_request(
    session: Session, *, request_id: int, decision: str,
    operator_note: str, operation_key: str | None = None,
) -> dict[str, object]:
    if decision not in {"reviewed", "declined"}:
        raise ValueError("decision must be reviewed or declined")
    note = str(operator_note or "").strip()
    if not 1 <= len(note) <= 1200:
        raise ValueError("operator_note must be 1-1200 characters")
    row = session.query(ExternalAgentResearchRequest).filter_by(id=int(request_id)).with_for_update().first()
    if row is None:
        raise ValueError("Research request not found")
    if row.status != "submitted":
        if row.status == decision and row.operator_note == note and row.resolution_operation_key == operation_key:
            return serialize_evidence_request(session, row)
        raise ValueError("Research request is already resolved")
    if decision == "reviewed":
        key = str(operation_key or "").strip()
        if key.startswith("worldview:"):
            cited = _worldview_cited_documents(session, key)
        else:
            op = session.query(InteractiveAgentOperation).filter_by(operation_key=key).first() if key else None
            if op is None or not op.receipt:
                raise ValueError("A completed canonical write operation is required")
            receipt = dict(op.receipt)
            if receipt.get("write_diff_id"):
                diff = session.query(LocalAgentWriteDiff).filter_by(id=receipt["write_diff_id"]).first()
                cited = set(diff.source_document_ids_json or []) if diff is not None else set()
            elif receipt.get("analyst_evidence_id"):
                evidence = session.query(AnalystEvidence).filter_by(id=receipt["analyst_evidence_id"]).first()
                cited = {evidence.source_document_id} if evidence is not None else set()
            else:
                cited = set()
        if row.document_id not in cited:
            raise ValueError("Canonical write must cite the request's evidence document")
        row.resolution_operation_key = key
    elif operation_key:
        raise ValueError("A declined request cannot cite a write operation")
    row.status = decision
    row.operator_note = note
    row.resolved_at = utcnow()
    session.flush()
    return serialize_evidence_request(session, row)
