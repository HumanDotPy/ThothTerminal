from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_

from thoth_terminal.config import settings
from thoth_terminal.db import session_factory, session_scope, utcnow
from thoth_terminal.discord.models import DiscordOutboxItem

NOTICE_AFTER_POST_KEY = "_thoth_notice_after_post"


def _now() -> datetime:
    return utcnow()


@dataclass(frozen=True)
class ClaimedOutboxItem:
    id: int
    guild_id: int
    channel_id: int
    thread_id: int | None
    content: str | None
    embed_json: dict[str, Any] | None
    attempts: int
    max_attempts: int


def create_outbox_item(
    *,
    guild_id: int,
    channel_id: int,
    thread_id: int | None = None,
    content: str | None = None,
    embed_json: dict[str, Any] | None = None,
    dedupe_key: str | None = None,
    priority: int = 5,
    run_after: datetime | None = None,
    created_by_kind: str = "agent",
    created_by_user_id: int | None = None,
    max_attempts: int | None = None,
) -> DiscordOutboxItem:
    dedupe = (dedupe_key or "").strip() or None
    created_by_kind_v = (created_by_kind or "agent").strip().lower() or "agent"
    if created_by_kind_v not in {"agent", "user", "system"}:
        created_by_kind_v = "agent"
    now = _now()
    with session_scope() as session:
        if dedupe:
            existing = (
                session.query(DiscordOutboxItem)
                .filter_by(guild_id=int(guild_id), dedupe_key=dedupe)
                .first()
            )
            if existing:
                return existing
        item = DiscordOutboxItem(
            guild_id=int(guild_id),
            channel_id=int(channel_id),
            thread_id=int(thread_id) if thread_id else None,
            status="pending",
            priority=int(priority),
            dedupe_key=dedupe,
            content=(content if content is not None else None),
            embed_json=(embed_json if isinstance(embed_json, dict) else None),
            run_after=run_after,
            locked_at=None,
            attempts=0,
            max_attempts=int(max_attempts if max_attempts is not None else 5),
            last_error=None,
            created_at=now,
            created_by_kind=created_by_kind_v,
            created_by_user_id=int(created_by_user_id) if created_by_user_id else None,
            posted_at=None,
            discord_message_id=None,
        )
        session.add(item)
        session.flush()
        return item


def list_outbox(
    *,
    guild_id: int,
    status: str | None = None,
    limit: int = 20,
) -> list[DiscordOutboxItem]:
    limit = max(1, min(int(limit or 20), 100))
    status_v = (status or "").strip().lower() or None
    SessionLocal = session_factory()
    with SessionLocal() as session:
        q = session.query(DiscordOutboxItem).filter_by(guild_id=int(guild_id))
        if status_v:
            q = q.filter(DiscordOutboxItem.status == status_v)
        return (
            q.order_by(DiscordOutboxItem.created_at.desc(), DiscordOutboxItem.id.desc())
            .limit(limit)
            .all()
        )


def cancel_outbox_item(*, guild_id: int, outbox_id: int) -> bool:
    with session_scope() as session:
        row = (
            session.query(DiscordOutboxItem)
            .filter_by(guild_id=int(guild_id), id=int(outbox_id))
            .first()
        )
        if not row:
            return False
        if row.status in {"posted", "canceled"}:
            return False
        row.status = "canceled"
        row.locked_at = None
        session.flush()
        return True


def retry_outbox_item(*, guild_id: int, outbox_id: int) -> bool:
    now = _now()
    with session_scope() as session:
        row = (
            session.query(DiscordOutboxItem)
            .filter_by(guild_id=int(guild_id), id=int(outbox_id))
            .first()
        )
        if not row or row.status == "posted":
            return False
        row.status = "pending"
        row.locked_at = None
        row.run_after = now
        row.attempts = 0
        row.last_error = None
        session.flush()
        return True


def discord_message_url(*, guild_id: int, channel_id: int, message_id: int) -> str:
    return f"https://discord.com/channels/{int(guild_id)}/{int(channel_id)}/{int(message_id)}"


def add_notice_after_post(
    embed_json: dict[str, Any] | None,
    *,
    channel_id: int,
    lines: list[str],
    dedupe_key: str | None = None,
    priority: int = 6,
) -> dict[str, Any] | None:
    if not isinstance(embed_json, dict):
        return embed_json
    content_lines = [str(line or "").strip() for line in lines if str(line or "").strip()]
    if not content_lines:
        return embed_json
    payload = dict(embed_json)
    payload[NOTICE_AFTER_POST_KEY] = {
        "channel_id": int(channel_id),
        "lines": content_lines[:8],
        "dedupe_key": (str(dedupe_key).strip() if dedupe_key else None),
        "priority": max(1, int(priority)),
    }
    return payload


def enqueue_notice_for_posted_item(
    *,
    item: ClaimedOutboxItem,
    discord_message_id: int | None,
) -> DiscordOutboxItem | None:
    if not item.thread_id or not discord_message_id:
        return None
    meta = (item.embed_json or {}).get(NOTICE_AFTER_POST_KEY) if isinstance(item.embed_json, dict) else None
    if not isinstance(meta, dict):
        return None
    lines = [str(line or "").strip() for line in (meta.get("lines") or []) if str(line or "").strip()]
    if not lines:
        return None
    jump_url = discord_message_url(
        guild_id=int(item.guild_id),
        channel_id=int(item.thread_id),
        message_id=int(discord_message_id),
    )
    content = "\n".join([*lines, jump_url]).strip()[:1800]
    if not content:
        return None
    channel_id = int(meta.get("channel_id") or item.channel_id)
    dedupe_key = str(meta.get("dedupe_key") or "").strip() or (
        f"{item.guild_id}:{item.id}:posted_notice:{int(discord_message_id)}"
    )
    priority = max(1, int(meta.get("priority") or 6))
    return create_outbox_item(
        guild_id=int(item.guild_id),
        channel_id=channel_id,
        thread_id=None,
        content=content,
        embed_json=None,
        dedupe_key=dedupe_key,
        priority=priority,
        run_after=_now(),
        created_by_kind="agent",
        created_by_user_id=None,
        max_attempts=5,
    )


def _posted_last_hour(session, *, guild_id: int) -> int:
    now = _now()
    cutoff = now - timedelta(hours=1)
    return (
        session.query(DiscordOutboxItem)
        .filter_by(guild_id=int(guild_id), status="posted")
        .filter(DiscordOutboxItem.posted_at.isnot(None))
        .filter(DiscordOutboxItem.posted_at >= cutoff)
        .count()
    )


def claim_next_outbox_item(
    *,
    guild_id: int,
    max_per_hour: int | None = None,
    stale_lock_seconds: int = 300,
) -> ClaimedOutboxItem | None:
    now = _now()
    stale_cutoff = now - timedelta(seconds=max(0, int(stale_lock_seconds)))
    max_per_hour = int(
        max_per_hour
        if max_per_hour is not None
        else getattr(settings, "discord_outbox_max_per_hour", 12) or 12
    )
    if max_per_hour <= 0:
        return None

    with session_scope() as session:
        if _posted_last_hour(session, guild_id=int(guild_id)) >= max_per_hour:
            return None

        row = (
            session.query(DiscordOutboxItem)
            .filter_by(guild_id=int(guild_id), status="pending")
            .filter(or_(DiscordOutboxItem.run_after.is_(None), DiscordOutboxItem.run_after <= now))
            .filter(or_(DiscordOutboxItem.locked_at.is_(None), DiscordOutboxItem.locked_at <= stale_cutoff))
            .filter(or_(DiscordOutboxItem.max_attempts.is_(None), DiscordOutboxItem.attempts < DiscordOutboxItem.max_attempts))
            .order_by(DiscordOutboxItem.priority.asc(), DiscordOutboxItem.created_at.asc(), DiscordOutboxItem.id.asc())
            .first()
        )
        if not row:
            return None

        row.status = "sending"
        row.locked_at = now
        row.attempts = int(row.attempts or 0) + 1
        session.flush()

        return ClaimedOutboxItem(
            id=int(row.id),
            guild_id=int(row.guild_id),
            channel_id=int(row.channel_id),
            thread_id=(int(row.thread_id) if row.thread_id else None),
            content=row.content,
            embed_json=(dict(row.embed_json) if isinstance(row.embed_json, dict) else None),
            attempts=int(row.attempts or 0),
            max_attempts=int(row.max_attempts or 0) if row.max_attempts is not None else 0,
        )


def mark_outbox_posted(*, guild_id: int, outbox_id: int, discord_message_id: int | None) -> None:
    now = _now()
    with session_scope() as session:
        row = (
            session.query(DiscordOutboxItem)
            .filter_by(guild_id=int(guild_id), id=int(outbox_id))
            .first()
        )
        if not row:
            return
        row.status = "posted"
        row.locked_at = None
        row.last_error = None
        row.posted_at = now
        row.discord_message_id = int(discord_message_id) if discord_message_id else None
        session.flush()


def mark_outbox_failed(
    *,
    guild_id: int,
    outbox_id: int,
    error: str,
    retry_after_seconds: int | None = None,
) -> None:
    now = _now()
    err = (error or "").strip() or "unknown error"
    with session_scope() as session:
        row = (
            session.query(DiscordOutboxItem)
            .filter_by(guild_id=int(guild_id), id=int(outbox_id))
            .first()
        )
        if not row:
            return
        row.last_error = err

        max_attempts = int(row.max_attempts or 0) if row.max_attempts is not None else 0
        attempts = int(row.attempts or 0)
        if max_attempts and attempts >= max_attempts:
            row.status = "failed"
            row.locked_at = None
            session.flush()
            return

        # Backoff: either caller-provided retry, or 10s, 20s, 40s, ... capped at 10m.
        if retry_after_seconds is not None:
            delay_s = max(1, int(retry_after_seconds))
        else:
            delay_s = min(600, 10 * (2 ** max(0, attempts - 1)))
        row.status = "pending"
        row.locked_at = None
        row.run_after = now + timedelta(seconds=delay_s)
        session.flush()
