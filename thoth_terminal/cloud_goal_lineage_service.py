from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy.orm import Session

from thoth_terminal.cloud_goal_run_service import append_cloud_goal_event, get_cloud_goal_run, utcnow
from thoth_terminal.cloud_task_catalog import get_task_capability
from thoth_terminal.models import (
    AgentRequestManifest,
    CloudGoalEvent,
    CloudGoalLineage,
    CloudGoalRun,
)


def _text(value: object) -> str:
    return str(value or "").strip()


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _latest_request_manifest(
    session: Session,
    run_id: int,
    *,
    at_or_before: Any | None = None,
) -> AgentRequestManifest | None:
    query = session.query(AgentRequestManifest).filter(
        AgentRequestManifest.run_kind == "cloud_goal",
        AgentRequestManifest.run_id == int(run_id),
    )
    if at_or_before is not None:
        query = query.filter(AgentRequestManifest.created_at <= at_or_before)
    return query.order_by(AgentRequestManifest.id.desc()).first()


def _latest_terminal_event(session: Session, run_id: int) -> CloudGoalEvent | None:
    return (
        session.query(CloudGoalEvent)
        .filter(
            CloudGoalEvent.run_id == int(run_id),
            CloudGoalEvent.event_kind.in_(["final", "error", "worker_interruption_recovered"]),
        )
        .order_by(CloudGoalEvent.id.desc())
        .first()
    )


def serialize_cloud_goal_lineage(row: CloudGoalLineage) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "lineage_key": row.lineage_key,
        "parent_run_id": int(row.parent_run_id),
        "child_run_id": int(row.child_run_id),
        "step_id": row.step_id,
        "task_id": row.task_id,
        "status": row.status,
        "terminal_disposition": row.terminal_disposition,
        "delegation_intent": dict(row.delegation_intent_json or {}),
        "capability_profile": dict(row.capability_profile_json or {}),
        "permission_profile": dict(row.permission_profile_json or {}),
        "model_role": row.model_role,
        "provider": row.provider,
        "model": row.model,
        "delegating_request_manifest_id": row.delegating_request_manifest_id,
        "accepted_request_manifest_id": row.accepted_request_manifest_id,
        "accepted_output_event_id": row.accepted_output_event_id,
        "accepted_output_sha256": row.accepted_output_sha256,
        "accepted_output": dict(row.accepted_output_json or {}),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        "accepted_at": row.accepted_at.isoformat() if row.accepted_at else None,
    }


def create_cloud_goal_lineage(
    session: Session,
    *,
    parent_run: CloudGoalRun,
    child_run: CloudGoalRun,
    step: dict[str, Any],
    permission_profile: dict[str, Any] | None = None,
) -> CloudGoalLineage:
    step_id = _text(step.get("step_id"))
    task_id = _text(step.get("task_id")) or child_run.task_kind
    if not step_id:
        raise ValueError("delegated step_id is required")
    existing = (
        session.query(CloudGoalLineage)
        .filter(CloudGoalLineage.parent_run_id == int(parent_run.id), CloudGoalLineage.step_id == step_id)
        .first()
    )
    if existing is not None:
        if int(existing.child_run_id) != int(child_run.id):
            raise ValueError(f"orchestrator step {step_id} already has child run #{int(existing.child_run_id)}")
        return existing
    parent_manifest = _latest_request_manifest(session, int(parent_run.id))
    now = utcnow()
    model_role = _text(step.get("model_tier")).lower() or "orchestrator"
    intent = {
        "objective": _text(step.get("objective")),
        "expected_output": _text(step.get("expected_output")),
        "depends_on": [_text(value) for value in step.get("depends_on") or [] if _text(value)],
        "allowed_write": _text(step.get("allowed_write")),
        "skill_slugs": [_text(value) for value in step.get("skill_slugs") or [] if _text(value)],
        "inputs": dict(step.get("inputs") or {}) if isinstance(step.get("inputs"), dict) else {},
    }
    row = CloudGoalLineage(
        lineage_key=f"cloud-goal:{int(parent_run.id)}:{step_id}:child:{int(child_run.id)}",
        parent_run_id=int(parent_run.id),
        child_run_id=int(child_run.id),
        step_id=step_id,
        task_id=task_id,
        status="running" if child_run.status == "running" else _text(child_run.status).lower() or "delegated",
        terminal_disposition="pending",
        delegation_intent_json=intent,
        capability_profile_json=dict(get_task_capability(task_id) or {}),
        permission_profile_json=dict(permission_profile or {}),
        model_role=model_role,
        provider=child_run.provider,
        model=child_run.model,
        delegating_request_manifest_id=int(parent_manifest.id) if parent_manifest is not None else None,
        accepted_output_json={},
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    return row


def sync_cloud_goal_lineage(
    session: Session,
    lineage: CloudGoalLineage | int,
    *,
    child_payload: dict[str, Any] | None = None,
    accept_output: bool = False,
) -> CloudGoalLineage:
    row = session.get(CloudGoalLineage, int(lineage)) if isinstance(lineage, int) else lineage
    if row is None:
        raise ValueError("Cloud goal lineage not found.")
    child = get_cloud_goal_run(session, int(row.child_run_id))
    if child is None:
        raise ValueError("Cloud goal lineage child run not found.")
    payload = dict(child_payload or {})
    child_status = _text(payload.get("status") or child.status).lower()
    output = dict(payload.get("final_result") or child.final_result or {})
    now = utcnow()
    if child_status == "complete":
        row.status = "complete"
        row.completed_at = child.completed_at or now
        if accept_output:
            terminal_event = _latest_terminal_event(session, int(child.id))
            child_manifest = _latest_request_manifest(
                session,
                int(child.id),
                at_or_before=terminal_event.created_at if terminal_event is not None else None,
            )
            row.terminal_disposition = "accepted"
            row.accepted_output_json = output
            row.accepted_output_sha256 = _sha256(output)
            row.accepted_output_event_id = int(terminal_event.id) if terminal_event is not None else None
            row.accepted_request_manifest_id = int(child_manifest.id) if child_manifest is not None else None
            row.accepted_at = now
        elif row.terminal_disposition == "pending":
            row.terminal_disposition = "ready_for_acceptance"
    elif child_status in {"failed", "error"}:
        row.status = "failed"
        row.terminal_disposition = "failed"
        row.completed_at = child.completed_at or now
    elif child_status in {"cancelled", "canceled"}:
        row.status = "cancelled"
        row.terminal_disposition = "cancelled"
        row.completed_at = child.completed_at or now
    elif child_status == "waiting":
        row.status = "waiting"
    else:
        row.status = "running"
    row.updated_at = now
    session.flush()
    return row


def accept_cloud_goal_lineage_output(session: Session, lineage_id: int) -> CloudGoalLineage:
    row = session.get(CloudGoalLineage, int(lineage_id))
    if row is None:
        raise ValueError("Cloud goal lineage not found.")
    was_accepted = row.terminal_disposition == "accepted"
    row = sync_cloud_goal_lineage(session, row, accept_output=True)
    if row.terminal_disposition != "accepted":
        raise ValueError("Cloud goal lineage child output is not complete and cannot be accepted.")
    if not was_accepted:
        append_cloud_goal_lineage_acceptance_event(session, row)
    return row


def append_cloud_goal_lineage_acceptance_event(session: Session, lineage: CloudGoalLineage) -> None:
    parent = get_cloud_goal_run(session, int(lineage.parent_run_id))
    if parent is None:
        return
    append_cloud_goal_event(
        session,
        parent,
        event_kind="handoff_output_accepted",
        summary=f"Accepted output from {lineage.step_id} / child run #{int(lineage.child_run_id)}.",
        payload={"lineage": serialize_cloud_goal_lineage(lineage)},
    )


def list_cloud_goal_lineages(
    session: Session,
    *,
    parent_run_id: int | None = None,
    child_run_id: int | None = None,
    status: str | None = None,
    limit: int = 100,
    reconcile: bool = True,
) -> list[dict[str, Any]]:
    query = session.query(CloudGoalLineage)
    if parent_run_id is not None:
        query = query.filter(CloudGoalLineage.parent_run_id == int(parent_run_id))
    if child_run_id is not None:
        query = query.filter(CloudGoalLineage.child_run_id == int(child_run_id))
    if _text(status):
        query = query.filter(CloudGoalLineage.status == _text(status).lower())
    rows = query.order_by(CloudGoalLineage.id.desc()).limit(max(1, min(1000, int(limit or 100)))).all()
    if reconcile:
        for row in rows:
            if row.terminal_disposition in {"pending", "ready_for_acceptance"}:
                sync_cloud_goal_lineage(session, row, accept_output=False)
    return [serialize_cloud_goal_lineage(row) for row in rows]
