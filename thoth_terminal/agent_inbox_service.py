from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from thoth_terminal.models import AgentInboxItem


DELIVERY_PERSIST = "persist"
DELIVERY_INJECT_NEXT_STEP = "inject_next_step"
DELIVERY_INJECT_NEXT_TURN = "inject_next_turn"
DELIVERY_WAKE = "wake"
DELIVERY_MODES = {
    DELIVERY_PERSIST,
    DELIVERY_INJECT_NEXT_STEP,
    DELIVERY_INJECT_NEXT_TURN,
    DELIVERY_WAKE,
}

STATUS_PERSISTED = "persisted"
STATUS_PENDING = "pending"
STATUS_CLAIMED = "claimed"
STATUS_APPLIED = "applied"
STATUS_CANCELED = "canceled"


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def normalize_delivery_mode(value: object) -> str:
    mode = str(value or "").strip().lower().replace("-", "_")
    if mode not in DELIVERY_MODES:
        raise ValueError("delivery_mode must be persist|inject_next_step|inject_next_turn|wake")
    return mode


@dataclass(frozen=True)
class AgentInboxClaim:
    token: str
    items: tuple[AgentInboxItem, ...]

    @property
    def item_ids(self) -> list[int]:
        return [int(row.id) for row in self.items]


def enqueue_agent_inbox_item(
    session: Session,
    *,
    target_kind: str,
    target_id: int,
    delivery_mode: str,
    dedupe_key: str,
    content_md: str | None = None,
    payload: dict[str, Any] | None = None,
    source_kind: str | None = None,
    source_id: object | None = None,
) -> AgentInboxItem:
    """Persist an input without implying that an agent should run.

    `wake` records a wake request, but the caller must dispatch it through the
    target's existing scheduler/job rail. This service deliberately does not
    create a second execution loop.
    """

    target_kind_v = str(target_kind or "").strip()
    dedupe_key_v = str(dedupe_key or "").strip()
    if not target_kind_v:
        raise ValueError("target_kind is required")
    if not dedupe_key_v:
        raise ValueError("dedupe_key is required")
    if len(dedupe_key_v) > 255:
        raise ValueError("dedupe_key must be at most 255 characters")
    mode = normalize_delivery_mode(delivery_mode)
    existing = (
        session.query(AgentInboxItem)
        .filter_by(target_kind=target_kind_v, target_id=int(target_id), dedupe_key=dedupe_key_v)
        .first()
    )
    if existing is not None:
        if existing.delivery_mode != mode:
            raise ValueError("dedupe_key already exists with different delivery semantics")
        return existing
    now = utcnow()
    row = AgentInboxItem(
        target_kind=target_kind_v,
        target_id=int(target_id),
        delivery_mode=mode,
        status=STATUS_PERSISTED if mode == DELIVERY_PERSIST else STATUS_PENDING,
        dedupe_key=dedupe_key_v,
        source_kind=str(source_kind or "").strip() or None,
        source_id=str(source_id).strip() if source_id is not None and str(source_id).strip() else None,
        content_md=str(content_md or "").strip() or None,
        payload_json=dict(payload or {}),
        wake_requested_at=now if mode == DELIVERY_WAKE else None,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    return row


def _recover_expired_claims(session: Session, *, target_kind: str, target_id: int, now: datetime) -> None:
    rows = (
        session.query(AgentInboxItem)
        .filter(
            AgentInboxItem.target_kind == str(target_kind),
            AgentInboxItem.target_id == int(target_id),
            AgentInboxItem.status == STATUS_CLAIMED,
            AgentInboxItem.claim_expires_at.is_not(None),
            AgentInboxItem.claim_expires_at <= now,
        )
        .all()
    )
    for row in rows:
        row.status = STATUS_PENDING
        row.claim_token = None
        row.claim_expires_at = None
        row.claimed_at = None
        row.updated_at = now


def claim_agent_inbox_items(
    session: Session,
    *,
    target_kind: str,
    target_id: int,
    boundary: str,
    limit: int = 50,
    lease_seconds: int = 900,
) -> AgentInboxClaim | None:
    """Claim inputs for one model boundary.

    A next-turn boundary also admits pending next-step injections: if no model
    request occurred in the prior turn, their promised "next request" remains
    the request that starts the new turn.
    """

    boundary_v = str(boundary or "").strip().lower().replace("-", "_")
    if boundary_v not in {"next_step", "next_turn"}:
        raise ValueError("boundary must be next_step|next_turn")
    now = utcnow()
    _recover_expired_claims(session, target_kind=target_kind, target_id=int(target_id), now=now)
    modes = [DELIVERY_INJECT_NEXT_STEP]
    if boundary_v == "next_turn":
        modes.extend([DELIVERY_INJECT_NEXT_TURN, DELIVERY_WAKE])
    query = (
        session.query(AgentInboxItem)
        .filter(
            AgentInboxItem.target_kind == str(target_kind),
            AgentInboxItem.target_id == int(target_id),
            AgentInboxItem.status == STATUS_PENDING,
            AgentInboxItem.delivery_mode.in_(modes),
        )
        .order_by(AgentInboxItem.id.asc())
        .limit(max(1, min(int(limit or 50), 200)))
    )
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True)
    rows = query.all()
    if not rows:
        return None
    token = uuid.uuid4().hex
    expiry = now + timedelta(seconds=max(30, int(lease_seconds or 900)))
    for row in rows:
        row.status = STATUS_CLAIMED
        row.claim_token = token
        row.claimed_at = now
        row.claim_expires_at = expiry
        row.attempt_count = int(row.attempt_count or 0) + 1
        row.updated_at = now
    session.flush()
    return AgentInboxClaim(token=token, items=tuple(rows))


def acknowledge_agent_inbox_claim(session: Session, claim: AgentInboxClaim) -> int:
    now = utcnow()
    count = 0
    for item_id in claim.item_ids:
        row = session.get(AgentInboxItem, int(item_id))
        if row is None or row.status != STATUS_CLAIMED or row.claim_token != claim.token:
            continue
        row.status = STATUS_APPLIED
        row.applied_at = now
        row.claim_expires_at = None
        row.updated_at = now
        count += 1
    session.flush()
    return count


def release_agent_inbox_claim(session: Session, claim: AgentInboxClaim, *, error: object | None = None) -> int:
    now = utcnow()
    count = 0
    for item_id in claim.item_ids:
        row = session.get(AgentInboxItem, int(item_id))
        if row is None or row.status != STATUS_CLAIMED or row.claim_token != claim.token:
            continue
        row.status = STATUS_PENDING
        row.claim_token = None
        row.claimed_at = None
        row.claim_expires_at = None
        row.last_error = str(error)[:2000] if error is not None else None
        row.updated_at = now
        count += 1
    session.flush()
    return count


def cancel_agent_inbox_item(session: Session, item_id: int) -> bool:
    row = session.get(AgentInboxItem, int(item_id))
    if row is None or row.status in {STATUS_APPLIED, STATUS_CANCELED, STATUS_PERSISTED}:
        return False
    now = utcnow()
    row.status = STATUS_CANCELED
    row.canceled_at = now
    row.claim_token = None
    row.claim_expires_at = None
    row.updated_at = now
    session.flush()
    return True


def render_agent_inbox_claim(claim: AgentInboxClaim) -> str:
    entries: list[dict[str, Any]] = []
    for row in claim.items:
        entries.append(
            {
                "inbox_item_id": int(row.id),
                "delivery_mode": row.delivery_mode,
                "source": {"kind": row.source_kind, "id": row.source_id},
                "content_md": row.content_md,
                "payload": dict(row.payload_json or {}),
            }
        )
    return "# Injected Agent Inbox\n\n" + json.dumps(entries, ensure_ascii=False, indent=2)


def serialize_agent_inbox_item(row: AgentInboxItem) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "target_kind": row.target_kind,
        "target_id": int(row.target_id),
        "delivery_mode": row.delivery_mode,
        "status": row.status,
        "dedupe_key": row.dedupe_key,
        "source_kind": row.source_kind,
        "source_id": row.source_id,
        "content_md": row.content_md,
        "payload": dict(row.payload_json or {}),
        "attempt_count": int(row.attempt_count or 0),
        "last_error": row.last_error,
        "wake_requested_at": row.wake_requested_at.isoformat() if row.wake_requested_at else None,
        "claimed_at": row.claimed_at.isoformat() if row.claimed_at else None,
        "applied_at": row.applied_at.isoformat() if row.applied_at else None,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def list_agent_inbox_items(
    session: Session,
    *,
    target_kind: str | None = None,
    target_id: int | None = None,
    status: str | None = None,
    delivery_mode: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    query = session.query(AgentInboxItem)
    if str(target_kind or "").strip():
        query = query.filter(AgentInboxItem.target_kind == str(target_kind).strip())
    if target_id is not None:
        query = query.filter(AgentInboxItem.target_id == int(target_id))
    if str(status or "").strip():
        query = query.filter(AgentInboxItem.status == str(status).strip().lower())
    if delivery_mode is not None:
        query = query.filter(AgentInboxItem.delivery_mode == normalize_delivery_mode(delivery_mode))
    rows = query.order_by(AgentInboxItem.id.desc()).limit(max(1, min(int(limit or 100), 500))).all()
    return [serialize_agent_inbox_item(row) for row in rows]
