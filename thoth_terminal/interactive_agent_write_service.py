"""Versioned, source-bound model writes from the operator's native Harness agent."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy.orm import Session

from thoth_terminal.local_agent_service import record_action, write_research_destination
from thoth_terminal.models import InteractiveAgentOperation
from thoth_terminal.worldview_write_service import upsert_sourced_consensus_evidence


def _apply_once(session: Session, *, operation_key: str, request: dict[str, Any],
                write: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    key = str(operation_key or "").strip()
    if not 1 <= len(key) <= 120:
        raise ValueError("operation_key must be 1-120 characters")
    request_hash = hashlib.sha256(json.dumps(request, sort_keys=True, default=str).encode()).hexdigest()
    previous = session.query(InteractiveAgentOperation).filter_by(operation_key=key).first()
    if previous is not None:
        if previous.request_hash != request_hash:
            raise ValueError("operation_key was already used for a different write")
        return {**dict(previous.receipt or {}), "replayed": True}
    # The unique database key rejects concurrent reuse; the caller's transaction
    # rolls back both the operation and its destination if either fails.
    operation = InteractiveAgentOperation(
        operation_key=key, request_hash=request_hash, receipt={},
        created_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    session.add(operation)
    session.flush()
    receipt = write()
    operation.receipt = receipt
    session.flush()
    return receipt


def apply_interactive_research_write(session: Session, *, operation_key: str,
                                     destination: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(destination, dict) or not isinstance(payload, dict):
        raise ValueError("destination and payload must be objects")

    def write() -> dict[str, Any]:
        write_payload = dict(payload)
        metadata = dict(write_payload.get("payload") or {}) if isinstance(write_payload.get("payload"), dict) else {}
        metadata.update(interactive_agent_operation_key=operation_key)
        write_payload["payload"] = metadata
        action = record_action(
            session, action_kind="interactive_research_write", status="running",
            tool_name="apply_interactive_research_write", endpoint="thoth_mcp",
            request_json={"operation_key": operation_key, "destination": destination},
            summary=str(write_payload.get("write_summary") or "")[:500] or None,
        )
        diff = write_research_destination(
            session, destination=destination, payload=write_payload,
            run_id=None, action_id=int(action.id),
        )
        receipt = {
            "operation_key": operation_key, "write_diff_id": int(diff.id),
            "destination_type": diff.destination_type, "destination_id": diff.destination_id,
            "after_version_id": diff.after_version_id, "write_summary": diff.write_summary,
            "replayed": False,
        }
        action.status = "success"
        action.destination_type = diff.destination_type
        action.destination_id = diff.destination_id
        action.summary = diff.write_summary
        action.confidence = diff.confidence
        action.response_json = receipt
        return receipt

    return _apply_once(session, operation_key=operation_key,
                       request={"kind": "research_write", "destination": destination, "payload": payload}, write=write)


def apply_interactive_consensus_evidence(session: Session, *, operation_key: str,
                                         payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")

    def write() -> dict[str, Any]:
        row, created, evidence_key, normalized, serialized = upsert_sourced_consensus_evidence(
            session, payload=payload,
            metadata={"interactive_agent_operation_key": operation_key},
        )
        receipt = {
            "operation_key": operation_key, "analyst_evidence_id": int(row.id),
            "evidence_key": evidence_key, "created": created,
            "subject_id": serialized["subject_id"],
            "estimate_owner": normalized["estimate_owner"],
            "estimate_metric": serialized["estimate_metric"],
            "estimate_period": serialized["estimate_period"],
            "estimate_value": serialized["estimate_value"],
            "replayed": False,
        }
        record_action(
            session, action_kind="interactive_consensus_evidence",
            tool_name="apply_interactive_consensus_evidence", endpoint="thoth_mcp",
            document_id=int(payload["source_document_id"]),
            request_json={"operation_key": operation_key, "source_document_id": payload["source_document_id"]},
            response_json=receipt, summary=str(normalized["summary"])[:500],
        )
        return receipt

    return _apply_once(session, operation_key=operation_key,
                       request={"kind": "consensus_evidence", "payload": payload}, write=write)
