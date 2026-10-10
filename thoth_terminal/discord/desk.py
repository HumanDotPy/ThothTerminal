from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from thoth_terminal.db import session_factory, session_scope, utcnow
from thoth_terminal.discord.models import DiscordGuildSettings


def _now() -> datetime:
    return utcnow()


@dataclass(frozen=True)
class DeskSettings:
    guild_id: int
    desk_channel_id: int | None
    desk_thread_id: int | None
    desk_thread_name: str | None
    desk_enabled: bool
    outbox_paused: bool


def get_agent_settings(*, guild_id: int) -> dict[str, object]:
    SessionLocal = session_factory()
    with SessionLocal() as session:
        row = session.query(DiscordGuildSettings).filter_by(guild_id=int(guild_id)).first()
        raw = row.agent_settings_json if row and isinstance(row.agent_settings_json, dict) else {}
        return dict(raw)


def update_agent_settings(*, guild_id: int, patch: dict[str, object], user_id: int) -> dict[str, object]:
    now = _now()
    patch_v = dict(patch or {})
    with session_scope() as session:
        row = session.query(DiscordGuildSettings).filter_by(guild_id=int(guild_id)).first()
        current: dict[str, object] = {}
        if row and isinstance(row.agent_settings_json, dict):
            current = dict(row.agent_settings_json)
        current.update({k: v for k, v in patch_v.items() if str(k).strip()})
        if not row:
            row = DiscordGuildSettings(
                guild_id=int(guild_id),
                memo_url=None,
                desk_channel_id=None,
                desk_thread_id=None,
                desk_thread_name=None,
                desk_enabled=True,
                outbox_paused=False,
                agent_settings_json=current,
                updated_at=now,
                updated_by_user_id=int(user_id),
            )
            session.add(row)
            session.flush()
            return dict(current)
        row.agent_settings_json = current
        row.updated_at = now
        row.updated_by_user_id = int(user_id)
        session.flush()
        return dict(current)


def get_desk_settings(*, guild_id: int) -> DeskSettings:
    SessionLocal = session_factory()
    with SessionLocal() as session:
        row = session.query(DiscordGuildSettings).filter_by(guild_id=int(guild_id)).first()
        if not row:
            return DeskSettings(
                guild_id=int(guild_id),
                desk_channel_id=None,
                desk_thread_id=None,
                desk_thread_name=None,
                desk_enabled=True,
                outbox_paused=False,
            )
        return DeskSettings(
            guild_id=int(row.guild_id),
            desk_channel_id=(int(row.desk_channel_id) if row.desk_channel_id else None),
            desk_thread_id=(int(row.desk_thread_id) if row.desk_thread_id else None),
            desk_thread_name=(str(row.desk_thread_name) if row.desk_thread_name else None),
            desk_enabled=bool(row.desk_enabled),
            outbox_paused=bool(row.outbox_paused),
        )


def set_desk_thread(
    session: Session,
    *,
    guild_id: int,
    channel_id: int,
    thread_id: int,
    thread_name: str,
    user_id: int,
) -> None:
    now = _now()
    row = session.query(DiscordGuildSettings).filter_by(guild_id=int(guild_id)).first()
    if not row:
        row = DiscordGuildSettings(
            guild_id=int(guild_id),
            memo_url=None,
            desk_channel_id=int(channel_id),
            desk_thread_id=int(thread_id),
            desk_thread_name=str(thread_name),
            desk_enabled=True,
            outbox_paused=False,
            agent_settings_json={},
            updated_at=now,
            updated_by_user_id=int(user_id),
        )
        session.add(row)
        session.flush()
        return
    row.desk_channel_id = int(channel_id)
    row.desk_thread_id = int(thread_id)
    row.desk_thread_name = str(thread_name)
    if row.desk_enabled is None:
        row.desk_enabled = True
    row.updated_at = now
    row.updated_by_user_id = int(user_id)
    session.flush()


def set_outbox_paused(*, guild_id: int, paused: bool, user_id: int) -> None:
    now = _now()
    with session_scope() as session:
        row = session.query(DiscordGuildSettings).filter_by(guild_id=int(guild_id)).first()
        if not row:
            row = DiscordGuildSettings(
                guild_id=int(guild_id),
                memo_url=None,
                desk_channel_id=None,
                desk_thread_id=None,
                desk_thread_name=None,
                desk_enabled=True,
                outbox_paused=bool(paused),
                agent_settings_json={},
                updated_at=now,
                updated_by_user_id=int(user_id),
            )
            session.add(row)
            return
        row.outbox_paused = bool(paused)
        row.updated_at = now
        row.updated_by_user_id = int(user_id)


def set_desk_enabled(*, guild_id: int, enabled: bool, user_id: int) -> None:
    now = _now()
    with session_scope() as session:
        row = session.query(DiscordGuildSettings).filter_by(guild_id=int(guild_id)).first()
        if not row:
            row = DiscordGuildSettings(
                guild_id=int(guild_id),
                memo_url=None,
                desk_channel_id=None,
                desk_thread_id=None,
                desk_thread_name=None,
                desk_enabled=bool(enabled),
                outbox_paused=False,
                agent_settings_json={},
                updated_at=now,
                updated_by_user_id=int(user_id),
            )
            session.add(row)
            return
        row.desk_enabled = bool(enabled)
        row.updated_at = now
        row.updated_by_user_id = int(user_id)


async def ensure_desk_thread(
    client,
    *,
    guild_id: int,
    channel_id: int,
    thread_name: str,
    user_id: int,
) -> int:
    """
    Ensure a public thread exists under the given channel. Persists the thread id/name in DB.

    Notes:
    - This function imports discord.py lazily and should only be used from the Discord bot runtime.
    """
    import discord

    name = (thread_name or "").strip() or "Thoth Desk"

    # Fast path: DB configured.
    cfg = get_desk_settings(guild_id=int(guild_id))
    if cfg.desk_thread_id:
        try:
            ch = client.get_channel(int(cfg.desk_thread_id))
            if ch and isinstance(ch, discord.Thread):
                return int(ch.id)
            th = await client.fetch_channel(int(cfg.desk_thread_id))
            if th and isinstance(th, discord.Thread):
                return int(th.id)
        except Exception:
            pass

    # Resolve channel.
    chan = client.get_channel(int(channel_id))
    if not chan:
        chan = await client.fetch_channel(int(channel_id))
    if not isinstance(chan, discord.TextChannel):
        raise ValueError("Desk channel must be a text channel.")

    wanted = name.casefold()

    # Search active threads.
    for t in getattr(chan, "threads", []) or []:
        try:
            if str(getattr(t, "name", "")).casefold() == wanted:
                tid = int(t.id)
                with session_scope() as session:
                    set_desk_thread(
                        session,
                        guild_id=int(guild_id),
                        channel_id=int(channel_id),
                        thread_id=tid,
                        thread_name=name,
                        user_id=int(user_id),
                    )
                return tid
        except Exception:
            continue

    # Search archived public threads (best-effort).
    try:
        async for t in chan.archived_threads(limit=50, private=False):
            if str(getattr(t, "name", "")).casefold() == wanted:
                try:
                    await t.edit(archived=False)
                except Exception:
                    pass
                tid = int(t.id)
                with session_scope() as session:
                    set_desk_thread(
                        session,
                        guild_id=int(guild_id),
                        channel_id=int(channel_id),
                        thread_id=tid,
                        thread_name=name,
                        user_id=int(user_id),
                    )
                return tid
    except Exception:
        pass

    # Create thread.
    try:
        thread = await chan.create_thread(
            name=name,
            type=discord.ChannelType.public_thread,
            auto_archive_duration=10080,  # 7 days
            reason="Thoth Desk thread",
        )
    except discord.Forbidden as exc:
        raise RuntimeError(
            "Bot lacks permission to create threads in this channel. "
            "Grant 'Create Public Threads' and 'Send Messages in Threads'."
        ) from exc

    tid = int(thread.id)
    with session_scope() as session:
        set_desk_thread(
            session,
            guild_id=int(guild_id),
            channel_id=int(channel_id),
            thread_id=tid,
            thread_name=name,
            user_id=int(user_id),
        )
    return tid
