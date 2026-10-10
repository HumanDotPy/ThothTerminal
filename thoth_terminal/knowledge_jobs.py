from __future__ import annotations

from datetime import datetime
from typing import Any

from thoth_terminal.db import session_scope
from thoth_terminal.freshness_service import due_freshness_review_targets, run_freshness_review
from thoth_terminal.jobs import enqueue_job
from thoth_terminal.knowledge_service import generate_knowledge_asset_draft


JOB_KIND_KNOWLEDGE_ASSET_DRAFT = "knowledge_asset_draft"
JOB_KIND_FRESHNESS_REVIEW = "freshness_review"


def enqueue_knowledge_asset_draft(
    session,
    *,
    subject: str | int,
    kind: str,
    instructions: str | None = None,
    window_days: int | None = None,
    dedupe_key: str | None = None,
    run_after: datetime | None = None,
    max_attempts: int = 3,
):
    return enqueue_job(
        session,
        kind=JOB_KIND_KNOWLEDGE_ASSET_DRAFT,
        payload={
            "subject": subject,
            "kind": kind,
            "instructions": str(instructions or "").strip() or None,
            "window_days": int(window_days) if window_days is not None else None,
        },
        dedupe_key=str(dedupe_key or "").strip() or None,
        priority=4,
        run_after=run_after,
        max_attempts=int(max_attempts),
    )


def enqueue_freshness_review(
    session,
    *,
    object_type: str,
    object_id: int,
    trigger_kind: str = "scheduler",
    allow_live_web: bool = True,
    dedupe_key: str | None = None,
    run_after: datetime | None = None,
    max_attempts: int = 3,
):
    return enqueue_job(
        session,
        kind=JOB_KIND_FRESHNESS_REVIEW,
        payload={
            "object_type": str(object_type),
            "object_id": int(object_id),
            "trigger_kind": str(trigger_kind or "scheduler"),
            "allow_live_web": bool(allow_live_web),
        },
        dedupe_key=str(dedupe_key or "").strip() or f"freshness_review:{object_type}:{int(object_id)}",
        priority=4,
        run_after=run_after,
        max_attempts=int(max_attempts),
    )


def enqueue_due_freshness_reviews(session, *, limit: int = 100) -> int:
    count = 0
    for row in due_freshness_review_targets(session, limit=limit):
        enqueue_freshness_review(
            session,
            object_type=str(row.get("object_type") or ""),
            object_id=int(row.get("object_id")),
            trigger_kind="scheduler",
            allow_live_web=True,
            dedupe_key=f"freshness_review:{row.get('object_type')}:{int(row.get('object_id'))}",
        )
        count += 1
    return count


def handle_knowledge_asset_draft(payload: dict[str, Any]) -> None:
    subject = payload.get("subject")
    kind = payload.get("kind")
    if subject is None or not str(subject).strip() or kind is None or not str(kind).strip():
        return
    with session_scope() as session:
        generate_knowledge_asset_draft(
            session,
            subject=subject,
            kind=str(kind),
            instructions=str(payload.get("instructions") or "").strip() or None,
            window_days=int(payload.get("window_days")) if str(payload.get("window_days") or "").isdigit() else None,
        )


def handle_freshness_review(payload: dict[str, Any]) -> None:
    object_type = payload.get("object_type")
    object_id = payload.get("object_id")
    if object_type is None or not str(object_type).strip() or object_id is None or not str(object_id).isdigit():
        return
    with session_scope() as session:
        run_freshness_review(
            session,
            object_type=str(object_type),
            object_id=int(object_id),
            trigger_kind=str(payload.get("trigger_kind") or "worker"),
            allow_live_web=bool(payload.get("allow_live_web", True)),
        )
