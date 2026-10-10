from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import re
from typing import Any
from urllib.parse import urlparse

from thoth_terminal.config import settings
from thoth_terminal.db import session_factory, session_scope, utcnow
from thoth_terminal.discord.models import (
    DiscordEvidenceItem,
    DiscordEvidenceTarget,
    DiscordExecIdea,
    DiscordGuildSettings,
    DiscordOutboxItem,
    DiscordQueueItem,
    DiscordTickerSource,
    DiscordWatchlistEntry,
    DiscordWatchlistSector,
    DiscordWatchlistSubgroup,
)
from thoth_terminal.models import Company, Issuer, IssuerIdentifier, Job
from thoth_terminal.discord.yahoo import (
    YahooFinanceError,
    fetch_adjclose_1y,
    fetch_bulk_returns,
    fetch_quotes,
    pick_adjclose,
)


def _now() -> datetime:
    return utcnow()


def _discord_yahoo_bulk_only() -> bool:
    return bool(getattr(settings, "discord_yahoo_bulk_only", True))


def normalize_symbol(symbol: str) -> str:
    return (symbol or "").strip().lstrip("$").upper()


EXEC_SECTOR = "Executive"
EXEC_SECTOR_KEY = "exec"
EXEC_SUBGROUP = "Executive"
EXEC_SUBGROUP_KEY = "exec"


def _normalize_list_kind(list_kind: str) -> str:
    kind = (list_kind or "").strip().lower()
    allowed = {"exec", "community", "masterlist"}
    if kind not in allowed:
        raise ValueError("list_kind must be exec|community|masterlist.")
    return kind


def _grouped_list_kind(list_kind: str) -> bool:
    return _normalize_list_kind(list_kind) in {"community", "masterlist"}


def normalize_sector(sector: str | None) -> tuple[str, str]:
    s = (sector or "").strip()
    if not s:
        s = "Uncategorized"
    return s, s.strip().lower()


def normalize_subgroup(subgroup: str | None) -> tuple[str, str]:
    g = (subgroup or "").strip()
    if not g:
        g = "General"
    return g, g.strip().lower()


def sector_create(*, guild_id: int, list_kind: str, sector: str, user_id: int) -> tuple[str, str, str]:
    """
    Register a sector label (even with 0 tickers). Returns (sector, sector_key, status).
    """
    kind = _normalize_list_kind(list_kind)
    if kind == "exec":
        raise ValueError("Sectors are not used for exec watchlists.")
    raw = (sector or "").strip()
    if not raw:
        raise ValueError("Missing sector.")
    sector_val, sector_key = normalize_sector(raw)
    now = _now()
    with session_scope() as session:
        row = (
            session.query(DiscordWatchlistSector)
            .filter_by(guild_id=int(guild_id), list_kind=kind, sector_key=sector_key)
            .first()
        )
        if not row:
            row = DiscordWatchlistSector(
                guild_id=int(guild_id),
                list_kind=kind,
                sector=sector_val,
                sector_key=sector_key,
                created_at=now,
                created_by_user_id=int(user_id),
                updated_at=now,
                updated_by_user_id=int(user_id),
                archived_at=None,
                archived_by_user_id=None,
            )
            session.add(row)
            return sector_val, sector_key, "created"
        status = "already"
        changed = False
        if row.archived_at is not None:
            row.archived_at = None
            row.archived_by_user_id = None
            changed = True
            status = "reactivated"
        if sector_val and sector_val != row.sector:
            row.sector = sector_val
            changed = True
            status = "updated"
        if changed:
            row.updated_at = now
            row.updated_by_user_id = int(user_id)
        return row.sector, row.sector_key, status


def subgroup_create(
    *,
    guild_id: int,
    list_kind: str,
    sector: str,
    subgroup: str,
    user_id: int,
) -> tuple[str, str, str, str, str]:
    """
    Register a subgroup label under a sector (even with 0 tickers).
    Returns (sector, sector_key, subgroup, subgroup_key, status).
    """
    kind = _normalize_list_kind(list_kind)
    if kind == "exec":
        raise ValueError("Subgroups are not used for exec watchlists.")
    raw_sector = (sector or "").strip()
    raw_group = (subgroup or "").strip()
    if not raw_sector:
        raise ValueError("Missing sector.")
    if not raw_group:
        raise ValueError("Missing subgroup.")

    # Ensure the sector exists in the registry (and get its key).
    sec, sec_key, _ = sector_create(guild_id=guild_id, list_kind=kind, sector=raw_sector, user_id=user_id)
    grp, grp_key = normalize_subgroup(raw_group)
    now = _now()

    with session_scope() as session:
        row = (
            session.query(DiscordWatchlistSubgroup)
            .filter_by(guild_id=int(guild_id), list_kind=kind, sector_key=str(sec_key), subgroup_key=str(grp_key))
            .first()
        )
        if not row:
            row = DiscordWatchlistSubgroup(
                guild_id=int(guild_id),
                list_kind=kind,
                sector_key=str(sec_key),
                subgroup=grp,
                subgroup_key=str(grp_key),
                created_at=now,
                created_by_user_id=int(user_id),
                updated_at=now,
                updated_by_user_id=int(user_id),
                archived_at=None,
                archived_by_user_id=None,
            )
            session.add(row)
            return sec, sec_key, grp, grp_key, "created"

        status = "already"
        changed = False
        if row.archived_at is not None:
            row.archived_at = None
            row.archived_by_user_id = None
            changed = True
            status = "reactivated"
        if grp and grp != row.subgroup:
            row.subgroup = grp
            changed = True
            status = "updated"
        if changed:
            row.updated_at = now
            row.updated_by_user_id = int(user_id)
        return sec, sec_key, row.subgroup, row.subgroup_key, status


def autocomplete_sector_labels(
    *,
    guild_id: int,
    list_kind: str,
    current: str,
    limit: int = 25,
) -> list[str]:
    kind = _normalize_list_kind(list_kind)
    if not _grouped_list_kind(kind):
        return []
    q = (current or "").strip().lower()
    limit = max(1, min(int(limit or 25), 25))

    SessionLocal = session_factory()
    with SessionLocal() as session:
        labels: list[str] = []
        seen: set[str] = set()

        reg = (
            session.query(DiscordWatchlistSector.sector)
            .filter_by(guild_id=int(guild_id), list_kind=kind, archived_at=None)
            .order_by(DiscordWatchlistSector.sector.asc())
            .all()
        )
        for (name,) in reg:
            if not name:
                continue
            s = str(name).strip()
            if not s:
                continue
            if q and q not in s.lower():
                continue
            key = s.lower()
            if key in seen:
                continue
            seen.add(key)
            labels.append(s)
            if len(labels) >= limit:
                return labels

        ent = (
            session.query(DiscordWatchlistEntry.sector)
            .filter_by(guild_id=int(guild_id), list_kind=kind, removed_at=None)
            .distinct()
            .order_by(DiscordWatchlistEntry.sector.asc())
            .all()
        )
        for (name,) in ent:
            if not name:
                continue
            s = str(name).strip()
            if not s:
                continue
            if q and q not in s.lower():
                continue
            key = s.lower()
            if key in seen:
                continue
            seen.add(key)
            labels.append(s)
            if len(labels) >= limit:
                break

        if kind == "community" and (not q or "uncategorized".startswith(q)):
            if "uncategorized" not in seen:
                labels.insert(0, "Uncategorized")
        return labels[:limit]


def autocomplete_subgroup_labels(
    *,
    guild_id: int,
    list_kind: str,
    sector: str,
    current: str,
    limit: int = 25,
) -> list[str]:
    kind = _normalize_list_kind(list_kind)
    if not _grouped_list_kind(kind):
        return []
    sec = (sector or "").strip()
    if not sec:
        return []
    _sec_val, sec_key = normalize_sector(sec)
    q = (current or "").strip().lower()
    limit = max(1, min(int(limit or 25), 25))

    SessionLocal = session_factory()
    with SessionLocal() as session:
        labels: list[str] = []
        seen: set[str] = set()

        reg = (
            session.query(DiscordWatchlistSubgroup.subgroup)
            .filter_by(guild_id=int(guild_id), list_kind=kind, sector_key=str(sec_key), archived_at=None)
            .order_by(DiscordWatchlistSubgroup.subgroup.asc())
            .all()
        )
        for (name,) in reg:
            if not name:
                continue
            s = str(name).strip()
            if not s:
                continue
            if q and q not in s.lower():
                continue
            key = s.lower()
            if key in seen:
                continue
            seen.add(key)
            labels.append(s)
            if len(labels) >= limit:
                return labels

        ent = (
            session.query(DiscordWatchlistEntry.subgroup)
            .filter_by(guild_id=int(guild_id), list_kind=kind, sector_key=str(sec_key), removed_at=None)
            .distinct()
            .order_by(DiscordWatchlistEntry.subgroup.asc())
            .all()
        )
        for (name,) in ent:
            if not name:
                continue
            s = str(name).strip()
            if not s:
                continue
            if q and q not in s.lower():
                continue
            key = s.lower()
            if key in seen:
                continue
            seen.add(key)
            labels.append(s)
            if len(labels) >= limit:
                break

        if not q or "general".startswith(q):
            if "general" not in seen:
                labels.insert(0, "General")
        return labels[:limit]


def rename_subgroup(
    *,
    guild_id: int,
    list_kind: str,
    sector: str,
    old_subgroup: str,
    new_subgroup: str,
    user_id: int,
) -> int:
    """
    Rename a subgroup within a sector/masterlist, updating:
    - discord_watchlist_entries subgroup/subgroup_key
    - discord_watchlist_subgroups registry row (if present)
    - Thoth entity canonical_name (Subgroup:* or MasterSubgroup:*), if present

    Returns number of watchlist entries updated.
    """
    kind = _normalize_list_kind(list_kind)
    if kind == "exec":
        raise ValueError("Exec watchlists do not support subgroups.")
    sec_raw = (sector or "").strip()
    if not sec_raw:
        raise ValueError("Missing sector/masterlist.")
    old_raw = (old_subgroup or "").strip()
    new_raw = (new_subgroup or "").strip()
    if not old_raw:
        raise ValueError("Missing old subgroup.")
    if not new_raw:
        raise ValueError("Missing new subgroup.")

    _sec_val, sec_key = normalize_sector(sec_raw)
    _old_val, old_key = normalize_subgroup(old_raw)
    new_val, new_key = normalize_subgroup(new_raw)

    now = _now()
    with session_scope() as session:
        # Find entries to update (may be empty if this subgroup was registered but unused).
        rows = (
            session.query(DiscordWatchlistEntry)
            .filter_by(
                guild_id=int(guild_id),
                list_kind=str(kind),
                sector_key=str(sec_key),
                subgroup_key=str(old_key),
            )
            .all()
        )

        # Collision check: renaming changes subgroup_key, which is part of the uniqueness constraint.
        if old_key != new_key:
            old_syms = {r.symbol for r in rows}
            existing = (
                session.query(DiscordWatchlistEntry.symbol)
                .filter_by(
                    guild_id=int(guild_id),
                    list_kind=str(kind),
                    sector_key=str(sec_key),
                    subgroup_key=str(new_key),
                )
                .all()
            )
            existing_syms = {str(r[0]) for r in existing if r and r[0]}
            colliding = sorted(old_syms & existing_syms)
            if colliding:
                sample = ", ".join(colliding[:8])
                more = f" (+{len(colliding) - 8} more)" if len(colliding) > 8 else ""
                raise ValueError(
                    "Rename would collide with existing memberships in the target subgroup for: "
                    f"{sample}{more}. Remove/move those first."
                )

        # Update entries.
        for r in rows:
            r.subgroup = new_val
            r.subgroup_key = new_key

        updated = len(rows)

        # Update subgroup registry if present; if not present and it's not the default, create it.
        reg_old = (
            session.query(DiscordWatchlistSubgroup)
            .filter_by(guild_id=int(guild_id), list_kind=str(kind), sector_key=str(sec_key), subgroup_key=str(old_key))
            .first()
        )
        if reg_old:
            if old_key != new_key:
                reg_new = (
                    session.query(DiscordWatchlistSubgroup)
                    .filter_by(guild_id=int(guild_id), list_kind=str(kind), sector_key=str(sec_key), subgroup_key=str(new_key))
                    .first()
                )
                if reg_new:
                    raise ValueError("Target subgroup already exists in the registry.")
            reg_old.subgroup = new_val
            reg_old.subgroup_key = new_key
            reg_old.updated_at = now
            reg_old.updated_by_user_id = int(user_id)
        else:
            if not (kind == "community" and new_key == "general"):
                reg_new = (
                    session.query(DiscordWatchlistSubgroup)
                    .filter_by(guild_id=int(guild_id), list_kind=str(kind), sector_key=str(sec_key), subgroup_key=str(new_key))
                    .first()
                )
                if not reg_new:
                    reg_new = DiscordWatchlistSubgroup(
                        guild_id=int(guild_id),
                        list_kind=str(kind),
                        sector_key=str(sec_key),
                        subgroup=str(new_val),
                        subgroup_key=str(new_key),
                        created_at=now,
                        created_by_user_id=int(user_id),
                        updated_at=now,
                        updated_by_user_id=int(user_id),
                        archived_at=None,
                        archived_by_user_id=None,
                    )
                    session.add(reg_new)
        if not rows and not reg_old:
            raise ValueError("No entries or registered subgroup found for that name.")

        # Best-effort: rename Thoth entity token so note filters keep working.
        try:
            from thoth_terminal.models import Entity

            if kind == "community":
                old_tok = f"Subgroup:{sec_key}:{old_key}"
                new_tok = f"Subgroup:{sec_key}:{new_key}"
            else:
                old_tok = f"MasterSubgroup:{sec_key}:{old_key}"
                new_tok = f"MasterSubgroup:{sec_key}:{new_key}"

            ent_old = session.query(Entity).filter_by(canonical_name=old_tok).first()
            if ent_old and old_tok != new_tok:
                ent_new = session.query(Entity).filter_by(canonical_name=new_tok).first()
                if ent_new:
                    raise ValueError("Entity rename would collide; please choose a different subgroup name.")
                ent_old.canonical_name = new_tok
                if not ent_old.kind or ent_old.kind == "other":
                    ent_old.kind = "topic"
        except Exception:
            # Don't fail the rename because of entity-layer issues.
            pass

        session.flush()
        return updated


def split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [p.strip() for p in value.split(",") if p.strip()]


def _baseline_due(now: datetime) -> datetime:
    delay_h = int(getattr(settings, "discord_baseline_delay_hours", 24) or 24)
    return now + timedelta(hours=delay_h)


def get_memo_url(*, guild_id: int) -> str | None:
    SessionLocal = session_factory()
    with SessionLocal() as session:
        row = session.query(DiscordGuildSettings).filter_by(guild_id=int(guild_id)).first()
        return row.memo_url if row else None


def set_memo_url(*, guild_id: int, url: str, user_id: int) -> None:
    now = _now()
    with session_scope() as session:
        row = session.query(DiscordGuildSettings).filter_by(guild_id=int(guild_id)).first()
        if not row:
            row = DiscordGuildSettings(
                guild_id=int(guild_id),
                memo_url=url,
                updated_at=now,
                updated_by_user_id=int(user_id),
            )
            session.add(row)
        else:
            row.memo_url = url
            row.updated_at = now
            row.updated_by_user_id = int(user_id)


def get_report_permission_level(*, guild_id: int) -> str:
    SessionLocal = session_factory()
    with SessionLocal() as session:
        row = session.query(DiscordGuildSettings).filter_by(guild_id=int(guild_id)).first()
        level = str(getattr(row, "report_permission_level", "") or "").strip().lower()
        return level if level in {"read_only", "elevated"} else "read_only"


def set_report_permission_level(*, guild_id: int, level: str, user_id: int) -> str:
    level_norm = str(level or "").strip().lower()
    if level_norm not in {"read_only", "elevated"}:
        raise ValueError("report permission level must be read_only|elevated.")
    now = _now()
    with session_scope() as session:
        row = session.query(DiscordGuildSettings).filter_by(guild_id=int(guild_id)).first()
        if not row:
            row = DiscordGuildSettings(
                guild_id=int(guild_id),
                memo_url=None,
                report_permission_level=level_norm,
                updated_at=now,
                updated_by_user_id=int(user_id),
            )
            session.add(row)
        else:
            row.report_permission_level = level_norm
            row.updated_at = now
            row.updated_by_user_id = int(user_id)
    return level_norm


def _ensure_guild_settings(session, *, guild_id: int, user_id: int | None = None) -> DiscordGuildSettings:
    row = session.query(DiscordGuildSettings).filter_by(guild_id=int(guild_id)).first()
    if row is not None:
        return row
    row = DiscordGuildSettings(
        guild_id=int(guild_id),
        memo_url=None,
        updated_at=_now(),
        updated_by_user_id=int(user_id or 0),
    )
    session.add(row)
    session.flush()
    return row


def _alert_subscription_rows(row: DiscordGuildSettings | None) -> list[dict[str, Any]]:
    if row is None:
        return []
    settings_json = row.agent_settings_json if isinstance(row.agent_settings_json, dict) else {}
    return [dict(item) for item in (settings_json.get("public_alert_subscriptions") or []) if isinstance(item, dict)]


def list_public_alert_subscriptions(*, guild_id: int) -> list[PublicAlertSubscription]:
    SessionLocal = session_factory()
    with SessionLocal() as session:
        row = session.query(DiscordGuildSettings).filter_by(guild_id=int(guild_id)).first()
        out: list[PublicAlertSubscription] = []
        for item in _alert_subscription_rows(row):
            try:
                channel_id = int(item.get("channel_id"))
            except Exception:
                continue
            query = str(item.get("query") or "").strip() or None
            try:
                created_by = int(item.get("created_by_user_id") or 0)
            except Exception:
                created_by = 0
            out.append(
                PublicAlertSubscription(
                    channel_id=channel_id,
                    query=query,
                    created_by_user_id=created_by,
                    created_at=(str(item.get("created_at")) if item.get("created_at") else None),
                )
            )
        return out


def upsert_public_alert_subscription(
    *,
    guild_id: int,
    channel_id: int,
    query: str | None,
    user_id: int,
) -> PublicAlertSubscription:
    query_v = str(query or "").strip() or None
    now = _now()
    key = f"{int(channel_id)}:{(query_v or '*').lower()}"
    with session_scope() as session:
        row = _ensure_guild_settings(session, guild_id=int(guild_id), user_id=int(user_id))
        settings_json = dict(row.agent_settings_json or {})
        rows = _alert_subscription_rows(row)
        next_rows = [item for item in rows if str(item.get("key") or "") != key]
        payload = {
            "key": key,
            "channel_id": int(channel_id),
            "query": query_v,
            "created_by_user_id": int(user_id),
            "created_at": now.isoformat(),
        }
        next_rows.append(payload)
        settings_json["public_alert_subscriptions"] = next_rows
        row.agent_settings_json = settings_json
        row.updated_at = now
        row.updated_by_user_id = int(user_id)
        session.flush()
        return PublicAlertSubscription(
            channel_id=int(channel_id),
            query=query_v,
            created_by_user_id=int(user_id),
            created_at=now.isoformat(),
        )


def remove_public_alert_subscription(*, guild_id: int, channel_id: int, query: str | None, user_id: int) -> bool:
    query_v = str(query or "").strip() or None
    key = f"{int(channel_id)}:{(query_v or '*').lower()}"
    now = _now()
    with session_scope() as session:
        row = session.query(DiscordGuildSettings).filter_by(guild_id=int(guild_id)).first()
        if row is None:
            return False
        settings_json = dict(row.agent_settings_json or {})
        rows = _alert_subscription_rows(row)
        next_rows = [item for item in rows if str(item.get("key") or "") != key]
        if len(next_rows) == len(rows):
            return False
        settings_json["public_alert_subscriptions"] = next_rows
        row.agent_settings_json = settings_json
        row.updated_at = now
        row.updated_by_user_id = int(user_id)
        return True


def _public_alert_embed(alerts: list[object], *, query: str | None = None) -> dict[str, Any]:
    scope = f" matching {query}" if query else ""
    fields = []
    for alert in alerts[:10]:
        name = str(getattr(alert, "name", "") or "Alert")
        chain = str(getattr(alert, "chain_title", "") or "")
        trigger = str(getattr(alert, "trigger", "") or "")
        value = " | ".join(part for part in (chain, trigger) if part)
        fields.append({"name": name[:256], "value": value[:1024] or "Active", "inline": False})
    return {
        "title": "Research Alerts",
        "description": f"Active public alert rules{scope}.",
        "color": 0xF87171,
        "fields": fields,
        "footer": {"text": "Public research alert projection"},
    }


def enqueue_public_alert_notification(
    *,
    guild_id: int,
    channel_id: int,
    query: str | None = None,
    user_id: int | None = None,
    limit: int = 10,
) -> int | None:
    from hashlib import sha1

    from thoth_terminal.discord import outbox as outbox_mod
    from thoth_terminal.discord import public_dto

    query_v = str(query or "").strip() or None
    alerts = public_dto.public_alerts(query=query_v, limit=max(1, min(int(limit or 10), 25)))
    if not alerts:
        return None
    fingerprint_basis = "|".join(
        f"{getattr(row, 'chain_slug', '')}:{getattr(row, 'name', '')}:{getattr(row, 'trigger', '')}"
        for row in alerts[:10]
    )
    fingerprint = sha1(fingerprint_basis.encode("utf-8")).hexdigest()[:16]
    query_key = (query_v or "all").lower().replace(" ", "-")[:80]
    item = outbox_mod.create_outbox_item(
        guild_id=int(guild_id),
        channel_id=int(channel_id),
        content=None,
        embed_json=_public_alert_embed(alerts, query=query_v),
        dedupe_key=f"public_alerts:{int(guild_id)}:{int(channel_id)}:{query_key}:{fingerprint}",
        priority=4,
        created_by_kind="user" if user_id is not None else "system",
        created_by_user_id=int(user_id) if user_id is not None else None,
    )
    return int(item.id)


def enqueue_public_alert_notifications_for_subscriptions(*, guild_id: int, limit: int = 10) -> list[int]:
    out: list[int] = []
    for sub in list_public_alert_subscriptions(guild_id=int(guild_id)):
        item_id = enqueue_public_alert_notification(
            guild_id=int(guild_id),
            channel_id=int(sub.channel_id),
            query=sub.query,
            user_id=None,
            limit=limit,
        )
        if item_id is not None:
            out.append(int(item_id))
    return out


@dataclass(frozen=True)
class WatchlistAddResult:
    list_kind: str
    symbol: str
    provider_symbol: str
    status: str  # added|reactivated|already|updated
    sector: str | None = None
    sector_key: str | None = None
    subgroup: str | None = None
    subgroup_key: str | None = None
    validation_pending: bool = False


def watchlist_add(
    *,
    guild_id: int,
    list_kind: str,
    symbol: str,
    user_id: int,
    sector: str | None = None,
    subgroup: str | None = None,
) -> WatchlistAddResult:
    kind = _normalize_list_kind(list_kind)
    sym = normalize_symbol(symbol)
    if not sym:
        raise ValueError("Missing ticker.")
    now = _now()
    sector_val: str
    sector_key: str
    subgroup_val: str
    subgroup_key: str

    if kind == "exec":
        sector_val, sector_key = EXEC_SECTOR, EXEC_SECTOR_KEY
        subgroup_val, subgroup_key = EXEC_SUBGROUP, EXEC_SUBGROUP_KEY
    elif kind == "community":
        sector_val, sector_key = normalize_sector(sector)
        subgroup_val, subgroup_key = normalize_subgroup(subgroup)
    else:
        raw_sector = (sector or "").strip()
        if not raw_sector:
            raise ValueError("Missing masterlist.")
        sector_val, sector_key = normalize_sector(raw_sector)
        subgroup_val, subgroup_key = normalize_subgroup(subgroup)

    # Avoid market-data calls on add; reuse an existing resolved provider_symbol when available.
    provider_sym: str | None = None
    SessionLocal = session_factory()
    with SessionLocal() as session:
        any_row = (
            session.query(DiscordWatchlistEntry.provider_symbol)
            .filter_by(guild_id=int(guild_id), symbol=sym)
            .order_by(DiscordWatchlistEntry.id.asc())
            .first()
        )
        if any_row and any_row[0]:
            provider_sym = str(any_row[0]).strip() or None

    validation_pending = False
    provider_sym = provider_sym or sym

    with session_scope() as session:
        def _upsert_sector_registry() -> None:
            if not _grouped_list_kind(kind):
                return
            # Don't clutter the registry with the default bucket unless explicitly created.
            if kind == "community" and sector_key == "uncategorized":
                return
            row = (
                session.query(DiscordWatchlistSector)
                .filter_by(guild_id=int(guild_id), list_kind=kind, sector_key=sector_key)
                .first()
            )
            if not row:
                row = DiscordWatchlistSector(
                    guild_id=int(guild_id),
                    list_kind=kind,
                    sector=sector_val,
                    sector_key=sector_key,
                    created_at=now,
                    created_by_user_id=int(user_id),
                    updated_at=now,
                    updated_by_user_id=int(user_id),
                    archived_at=None,
                    archived_by_user_id=None,
                )
                session.add(row)
                return
            changed = False
            if row.archived_at is not None:
                row.archived_at = None
                row.archived_by_user_id = None
                changed = True
            if sector_val != row.sector:
                row.sector = sector_val
                changed = True
            if changed:
                row.updated_at = now
                row.updated_by_user_id = int(user_id)

        def _upsert_subgroup_registry() -> None:
            if not _grouped_list_kind(kind):
                return
            # Don't clutter the registry with the default subgroup unless explicitly created.
            if kind == "community" and subgroup_key == "general":
                return
            row = (
                session.query(DiscordWatchlistSubgroup)
                .filter_by(
                    guild_id=int(guild_id),
                    list_kind=kind,
                    sector_key=str(sector_key),
                    subgroup_key=str(subgroup_key),
                )
                .first()
            )
            if not row:
                row = DiscordWatchlistSubgroup(
                    guild_id=int(guild_id),
                    list_kind=kind,
                    sector_key=str(sector_key),
                    subgroup=str(subgroup_val),
                    subgroup_key=str(subgroup_key),
                    created_at=now,
                    created_by_user_id=int(user_id),
                    updated_at=now,
                    updated_by_user_id=int(user_id),
                    archived_at=None,
                    archived_by_user_id=None,
                )
                session.add(row)
                return
            changed = False
            if row.archived_at is not None:
                row.archived_at = None
                row.archived_by_user_id = None
                changed = True
            if subgroup_val != row.subgroup:
                row.subgroup = str(subgroup_val)
                changed = True
            if changed:
                row.updated_at = now
                row.updated_by_user_id = int(user_id)

        entry = (
            session.query(DiscordWatchlistEntry)
            .filter_by(
                guild_id=int(guild_id),
                list_kind=str(kind),
                symbol=sym,
                sector_key=str(sector_key),
                subgroup_key=str(subgroup_key),
            )
            .first()
        )
        if entry:
            if entry.removed_at is not None:
                entry.provider_symbol = str(provider_sym or sym)
                entry.sector = sector_val
                entry.sector_key = sector_key
                entry.subgroup = subgroup_val
                entry.subgroup_key = subgroup_key
                _upsert_sector_registry()
                _upsert_subgroup_registry()
                entry.added_at = now
                entry.added_by_user_id = int(user_id)
                entry.removed_at = None
                entry.removed_by_user_id = None
                entry.baseline_due_at = _baseline_due(now)
                entry.baseline_price = None
                entry.baseline_currency = None
                entry.baseline_quote_at = None
                entry.baseline_captured_at = None
                entry.last_quote_price = None
                entry.last_quote_currency = None
                entry.last_quote_at = None
                entry.last_returns = {}
                entry.last_returns_at = None
                status = "reactivated"
            else:
                status = "already"
                changed = False
                if entry.sector != sector_val:
                    entry.sector = sector_val
                    changed = True
                if entry.subgroup != subgroup_val:
                    entry.subgroup = subgroup_val
                    changed = True
                if changed:
                    status = "updated"
                _upsert_sector_registry()
                _upsert_subgroup_registry()
        else:
            entry = DiscordWatchlistEntry(
                guild_id=int(guild_id),
                list_kind=str(kind),
                symbol=sym,
                provider_symbol=str(provider_sym or sym),
                sector=str(sector_val),
                sector_key=str(sector_key),
                subgroup=str(subgroup_val),
                subgroup_key=str(subgroup_key),
                added_at=now,
                added_by_user_id=int(user_id),
                removed_at=None,
                removed_by_user_id=None,
                baseline_due_at=_baseline_due(now),
                baseline_price=None,
                baseline_currency=None,
                baseline_quote_at=None,
                baseline_captured_at=None,
                last_quote_price=None,
                last_quote_currency=None,
                last_quote_at=None,
                last_returns={},
                last_returns_at=None,
            )
            session.add(entry)
            _upsert_sector_registry()
            _upsert_subgroup_registry()
            status = "added"

        # Community special-case: if it was previously in Uncategorized, remove that membership once it is placed
        # into a real sector (so symbols don't stick around in the default bucket forever).
        if kind == "community" and sector_key != "uncategorized":
            uncats = (
                session.query(DiscordWatchlistEntry)
                .filter_by(
                    guild_id=int(guild_id),
                    list_kind="community",
                    symbol=sym,
                    sector_key="uncategorized",
                    removed_at=None,
                )
                .all()
            )
            for u in uncats:
                if entry and u.id == entry.id:
                    continue
                u.removed_at = now
                u.removed_by_user_id = int(user_id)

    return WatchlistAddResult(
        list_kind=kind,
        symbol=sym,
        provider_symbol=str(provider_sym or sym),
        status=status,
        sector=(sector_val if _grouped_list_kind(kind) else None),
        sector_key=(sector_key if _grouped_list_kind(kind) else None),
        subgroup=(subgroup_val if _grouped_list_kind(kind) else None),
        subgroup_key=(subgroup_key if _grouped_list_kind(kind) else None),
        validation_pending=validation_pending,
    )


def watchlist_remove(
    *,
    guild_id: int,
    list_kind: str,
    symbol: str,
    user_id: int,
    sector: str | None = None,
    subgroup: str | None = None,
    all_matches: bool = False,
) -> int:
    kind = _normalize_list_kind(list_kind)
    sym = normalize_symbol(symbol)
    if not sym:
        raise ValueError("Missing ticker.")
    now = _now()
    with session_scope() as session:
        q = session.query(DiscordWatchlistEntry).filter_by(
            guild_id=int(guild_id),
            list_kind=str(kind),
            symbol=sym,
            removed_at=None,
        )
        sector_key = sector.strip().lower() if (sector and sector.strip()) else None
        subgroup_key = subgroup.strip().lower() if (subgroup and subgroup.strip()) else None
        if _grouped_list_kind(kind) and sector_key:
            q = q.filter(DiscordWatchlistEntry.sector_key == sector_key)
        if _grouped_list_kind(kind) and subgroup_key:
            q = q.filter(DiscordWatchlistEntry.subgroup_key == subgroup_key)

        rows = q.order_by(
            DiscordWatchlistEntry.sector_key.asc(),
            DiscordWatchlistEntry.subgroup_key.asc(),
            DiscordWatchlistEntry.symbol.asc(),
        ).all()
        if not rows:
            return 0

        if all_matches:
            for r in rows:
                r.removed_at = now
                r.removed_by_user_id = int(user_id)
            return len(rows)

        if _grouped_list_kind(kind):
            if sector_key and not subgroup_key and len(rows) > 1:
                raise ValueError(
                    f"Ambiguous: ${sym} appears in {len(rows)} subgroups in that sector. "
                    "Provide subgroup, or set all_matches=true."
                )
            if not sector_key and len(rows) > 1:
                raise ValueError(
                    f"Ambiguous: ${sym} appears in {len(rows)} places. Provide sector/subgroup, or set all_matches=true."
                )

        rows[0].removed_at = now
        rows[0].removed_by_user_id = int(user_id)
        return 1


def list_watchlist_symbols(*, guild_id: int, list_kind: str) -> list[str]:
    kind = _normalize_list_kind(list_kind)
    SessionLocal = session_factory()
    with SessionLocal() as session:
        rows = (
            session.query(DiscordWatchlistEntry.symbol)
            .filter_by(guild_id=int(guild_id), list_kind=str(kind), removed_at=None)
            .distinct()
            .order_by(DiscordWatchlistEntry.symbol.asc())
            .all()
        )
        return [r[0] for r in rows]


def list_active_watchlist_guild_ids() -> list[int]:
    SessionLocal = session_factory()
    with SessionLocal() as session:
        rows = (
            session.query(DiscordWatchlistEntry.guild_id)
            .filter_by(removed_at=None)
            .distinct()
            .order_by(DiscordWatchlistEntry.guild_id.asc())
            .all()
        )
        return [int(r[0]) for r in rows if r and r[0] is not None]


def _latest_job_for_guild(
    session,
    *,
    kind: str,
    guild_id: int,
) -> Job | None:
    rows = (
        session.query(Job)
        .filter(Job.kind == str(kind))
        .order_by(Job.id.desc())
        .limit(40)
        .all()
    )
    for row in rows:
        payload = row.payload if isinstance(row.payload, dict) else {}
        gid = payload.get("guild_id")
        if gid is None:
            continue
        try:
            if int(gid) == int(guild_id):
                return row
        except Exception:
            continue
    return rows[0] if rows else None


def discord_pipeline_health(*, guild_id: int) -> dict[str, Any]:
    now = _now()
    SessionLocal = session_factory()
    with SessionLocal() as session:
        entries = (
            session.query(DiscordWatchlistEntry)
            .filter_by(guild_id=int(guild_id), removed_at=None)
            .all()
        )
        total = len(entries)
        quoted = 0
        age_lt_1h = 0
        age_1h_6h = 0
        age_gt_6h = 0
        missing_quote_at = 0
        for entry in entries:
            if entry.last_quote_price is not None:
                quoted += 1
            if not entry.last_quote_at:
                missing_quote_at += 1
                continue
            age_s = (now - entry.last_quote_at).total_seconds()
            if age_s < 3600:
                age_lt_1h += 1
            elif age_s < 21600:
                age_1h_6h += 1
            else:
                age_gt_6h += 1

        refresh_job = _latest_job_for_guild(
            session,
            kind="discord_watchlist_refresh",
            guild_id=int(guild_id),
        )
        transcript_job = _latest_job_for_guild(
            session,
            kind="discord_watchlist_transcript_scan",
            guild_id=int(guild_id),
        )

        transcript_outbox_24h = (
            session.query(DiscordOutboxItem)
            .filter(DiscordOutboxItem.guild_id == int(guild_id))
            .filter(DiscordOutboxItem.dedupe_key.like("transcript:%"))
            .filter(DiscordOutboxItem.created_at >= (now - timedelta(hours=24)))
            .count()
        )
        transcript_outbox_failed_24h = (
            session.query(DiscordOutboxItem)
            .filter(DiscordOutboxItem.guild_id == int(guild_id))
            .filter(DiscordOutboxItem.dedupe_key.like("transcript:%"))
            .filter(DiscordOutboxItem.status == "failed")
            .filter(DiscordOutboxItem.created_at >= (now - timedelta(hours=24)))
            .count()
        )

        likely_causes: list[str] = []
        if total > 0 and quoted == 0:
            likely_causes.append("No cached quotes; Yahoo bulk fetch may be failing or symbols may not map.")
        if refresh_job and str(refresh_job.status) == "error":
            likely_causes.append("Latest watchlist refresh job is in error.")
        if transcript_job and str(transcript_job.status) == "error":
            likely_causes.append("Latest transcript scan job is in error.")

        def _job_payload(row: Job | None) -> dict[str, Any] | None:
            if not row:
                return None
            return {
                "id": int(row.id),
                "status": str(row.status),
                "attempts": int(row.attempts or 0),
                "last_error": str(row.last_error or "")[:300],
                "updated_at": str(row.updated_at),
            }

        return {
            "watchlist_entries_total": total,
            "watchlist_entries_with_quotes": quoted,
            "quote_freshness": {
                "lt_1h": age_lt_1h,
                "h1_to_h6": age_1h_6h,
                "gt_6h": age_gt_6h,
                "missing_quote_at": missing_quote_at,
            },
            "latest_watchlist_refresh_job": _job_payload(refresh_job),
            "latest_transcript_scan_job": _job_payload(transcript_job),
            "transcript_outbox_24h": {
                "created": int(transcript_outbox_24h),
                "failed": int(transcript_outbox_failed_24h),
            },
            "likely_causes": likely_causes,
        }


def list_watchlist_by_sector(*, guild_id: int, list_kind: str = "community") -> dict[str, list[str]]:
    kind = _normalize_list_kind(list_kind)
    if not _grouped_list_kind(kind):
        raise ValueError("Sectors are only supported for community/masterlist watchlists.")
    SessionLocal = session_factory()
    with SessionLocal() as session:
        sector_rows = (
            session.query(DiscordWatchlistSector.sector, DiscordWatchlistSector.sector_key)
            .filter_by(guild_id=int(guild_id), list_kind=kind, archived_at=None)
            .order_by(DiscordWatchlistSector.sector.asc())
            .all()
        )
        registered_by_key = {str(k): str(s) for s, k in sector_rows if s and k}

        out: dict[str, list[str]] = {}
        # Seed registered sectors so they show even when empty.
        for s, _k in sector_rows:
            if not s:
                continue
            out.setdefault(str(s), [])

        rows = (
            session.query(DiscordWatchlistEntry.sector, DiscordWatchlistEntry.sector_key, DiscordWatchlistEntry.symbol)
            .filter_by(guild_id=int(guild_id), list_kind=kind, removed_at=None)
            .order_by(DiscordWatchlistEntry.sector_key.asc(), DiscordWatchlistEntry.sector.asc(), DiscordWatchlistEntry.symbol.asc())
            .all()
        )

    for sector, sector_key, sym in rows:
        key = str(sector_key or "").strip().lower() or "uncategorized"
        disp = registered_by_key.get(key) or (sector or "Uncategorized")
        out.setdefault(disp, []).append(sym)

    for k in list(out.keys()):
        out[k] = sorted(set(out[k]))
    return out


def list_watchlist_by_sector_subgroup(
    *,
    guild_id: int,
    list_kind: str = "community",
) -> dict[str, dict[str, list[str]]]:
    """
    Returns {sector_display: {subgroup_display: [symbols...]}} including registered empty sectors/subgroups.
    """
    kind = _normalize_list_kind(list_kind)
    if not _grouped_list_kind(kind):
        raise ValueError("Sectors/subgroups are only supported for community/masterlist watchlists.")
    SessionLocal = session_factory()
    with SessionLocal() as session:
        sector_rows = (
            session.query(DiscordWatchlistSector.sector, DiscordWatchlistSector.sector_key)
            .filter_by(guild_id=int(guild_id), list_kind=kind, archived_at=None)
            .order_by(DiscordWatchlistSector.sector.asc())
            .all()
        )
        sector_disp_by_key = {str(k): str(s) for s, k in sector_rows if s and k}

        subgroup_rows = (
            session.query(DiscordWatchlistSubgroup.sector_key, DiscordWatchlistSubgroup.subgroup, DiscordWatchlistSubgroup.subgroup_key)
            .filter_by(guild_id=int(guild_id), list_kind=kind, archived_at=None)
            .order_by(DiscordWatchlistSubgroup.sector_key.asc(), DiscordWatchlistSubgroup.subgroup.asc())
            .all()
        )
        subgroup_disp_by_key = {(str(sk), str(gk)): str(g) for sk, g, gk in subgroup_rows if sk and g and gk}

        out: dict[str, dict[str, list[str]]] = {}

        # Seed registered sectors so they show even when empty.
        for s, _k in sector_rows:
            if not s:
                continue
            out.setdefault(str(s), {})

        # Seed registered subgroups so they show even when empty.
        for sk, g, gk in subgroup_rows:
            if not sk or not g or not gk:
                continue
            sec_disp = sector_disp_by_key.get(str(sk)) or str(sk)
            out.setdefault(sec_disp, {})
            out[sec_disp].setdefault(str(g), [])

        rows = (
            session.query(
                DiscordWatchlistEntry.sector,
                DiscordWatchlistEntry.sector_key,
                DiscordWatchlistEntry.subgroup,
                DiscordWatchlistEntry.subgroup_key,
                DiscordWatchlistEntry.symbol,
            )
            .filter_by(guild_id=int(guild_id), list_kind=kind, removed_at=None)
            .order_by(
                DiscordWatchlistEntry.sector_key.asc(),
                DiscordWatchlistEntry.sector.asc(),
                DiscordWatchlistEntry.subgroup_key.asc(),
                DiscordWatchlistEntry.subgroup.asc(),
                DiscordWatchlistEntry.symbol.asc(),
            )
            .all()
        )

    for sector, sector_key, subgroup, subgroup_key, sym in rows:
        sk = (sector_key or (sector or "").strip().lower() or "uncategorized")
        sec_disp = sector_disp_by_key.get(sk) or (sector or "Uncategorized")
        gk = (subgroup_key or (subgroup or "").strip().lower() or "general")
        grp_disp = subgroup_disp_by_key.get((sk, gk)) or (subgroup or "General")
        out.setdefault(sec_disp, {}).setdefault(grp_disp, []).append(sym)

    for sec in list(out.keys()):
        for grp in list(out[sec].keys()):
            out[sec][grp] = sorted(set(out[sec][grp]))
    return out


def list_sectors(*, guild_id: int, list_kind: str = "community") -> list[tuple[str, int]]:
    grouped = list_watchlist_by_sector(guild_id=guild_id, list_kind=list_kind)
    rows = [(name, len(syms)) for name, syms in grouped.items()]
    rows.sort(key=lambda t: (-int(t[1]), str(t[0]).lower()))
    return [(str(name), int(n)) for name, n in rows]


def list_subgroups(
    *,
    guild_id: int,
    list_kind: str = "community",
    sector: str | None = None,
) -> list[tuple[str, str, int]]:
    grouped = list_watchlist_by_sector_subgroup(guild_id=guild_id, list_kind=list_kind)
    if not grouped:
        return []
    if sector:
        key = sector.strip().lower()
        match = None
        for sec, groups in grouped.items():
            if sec.strip().lower() == key:
                match = (sec, groups)
                break
        if not match:
            return []
        sec, groups = match
        rows = [(sec, grp, len(syms)) for grp, syms in groups.items()]
    else:
        rows = []
        for sec, groups in grouped.items():
            for grp, syms in groups.items():
                rows.append((sec, grp, len(syms)))
    rows.sort(key=lambda t: (str(t[0]).lower(), -int(t[2]), str(t[1]).lower()))
    return [(str(sec), str(grp), int(n)) for sec, grp, n in rows]


@dataclass(frozen=True)
class WatchlistEntryView:
    sector: str | None
    subgroup: str | None
    symbol: str
    provider_symbol: str
    company_name: str | None
    exchange: str | None
    full_exchange_name: str | None
    last_quote_price: float | None
    currency: str | None
    last_quote_at: datetime | None


def list_watchlist_entries(
    *,
    guild_id: int,
    list_kind: str,
    sector: str | None = None,
    subgroup: str | None = None,
) -> list[WatchlistEntryView]:
    kind = _normalize_list_kind(list_kind)
    sector_key = sector.strip().lower() if sector else None
    subgroup_key = subgroup.strip().lower() if subgroup else None
    SessionLocal = session_factory()
    with SessionLocal() as session:
        q = (
            session.query(DiscordWatchlistEntry)
            .filter_by(guild_id=int(guild_id), list_kind=str(kind), removed_at=None)
        )
        if _grouped_list_kind(kind) and sector_key:
            q = q.filter(DiscordWatchlistEntry.sector_key == sector_key)
        if _grouped_list_kind(kind) and subgroup_key:
            q = q.filter(DiscordWatchlistEntry.subgroup_key == subgroup_key)
        if _grouped_list_kind(kind):
            q = q.order_by(
                DiscordWatchlistEntry.sector_key.asc(),
                DiscordWatchlistEntry.sector.asc(),
                DiscordWatchlistEntry.subgroup_key.asc(),
                DiscordWatchlistEntry.subgroup.asc(),
                DiscordWatchlistEntry.symbol.asc(),
            )
        else:
            q = q.order_by(DiscordWatchlistEntry.symbol.asc())
        rows = q.all()
        return [
            WatchlistEntryView(
                sector=(r.sector if _grouped_list_kind(kind) else None),
                subgroup=(r.subgroup if _grouped_list_kind(kind) else None),
                symbol=r.symbol,
                provider_symbol=r.provider_symbol,
                company_name=getattr(r, "company_name", None),
                exchange=r.exchange,
                full_exchange_name=r.full_exchange_name,
                last_quote_price=(float(r.last_quote_price) if r.last_quote_price is not None else None),
                currency=r.last_quote_currency,
                last_quote_at=r.last_quote_at,
            )
            for r in rows
        ]


@dataclass(frozen=True)
class QuoteRefreshResult:
    updated: int
    asof_utc: datetime | None
    error: str | None


def refresh_watchlist_quotes(
    *,
    guild_id: int,
    list_kind: str,
    sector: str | None = None,
    subgroup: str | None = None,
    force_refresh: bool = False,
) -> QuoteRefreshResult:
    kind = _normalize_list_kind(list_kind)
    now = _now()
    sector_key = sector.strip().lower() if sector else None
    subgroup_key = subgroup.strip().lower() if subgroup else None
    SessionLocal = session_factory()
    with SessionLocal() as session:
        q = (
            session.query(DiscordWatchlistEntry)
            .filter_by(guild_id=int(guild_id), list_kind=str(kind), removed_at=None)
        )
        if _grouped_list_kind(kind) and sector_key:
            q = q.filter(DiscordWatchlistEntry.sector_key == sector_key)
        if _grouped_list_kind(kind) and subgroup_key:
            q = q.filter(DiscordWatchlistEntry.subgroup_key == subgroup_key)
        entries = q.order_by(DiscordWatchlistEntry.symbol.asc()).all()

    if not entries:
        return QuoteRefreshResult(updated=0, asof_utc=None, error=None)

    cache_s = int(getattr(settings, "discord_perf_cache_seconds", 600) or 600)
    cutoff = now - timedelta(seconds=cache_s)

    to_refresh: list[DiscordWatchlistEntry] = []
    for e in entries:
        if force_refresh:
            to_refresh.append(e)
            continue
        if e.last_quote_at is None or e.last_quote_at < cutoff:
            to_refresh.append(e)
            continue
        if e.last_quote_price is None:
            to_refresh.append(e)
            continue
        if not getattr(e, "company_name", None):
            to_refresh.append(e)
            continue
        if not e.full_exchange_name and not e.exchange:
            to_refresh.append(e)
            continue

    if not to_refresh:
        asof = max((e.last_quote_at for e in entries if e.last_quote_at), default=None)
        return QuoteRefreshResult(updated=0, asof_utc=asof, error=None)

    provider_symbols = sorted({e.provider_symbol for e in to_refresh if e.provider_symbol})
    try:
        quotes = fetch_quotes(
            provider_symbols,
            user_agent=settings.discord_market_user_agent,
            allow_chart_fallback=(not _discord_yahoo_bulk_only()),
        )
    except YahooFinanceError as exc:
        asof = max((e.last_quote_at for e in entries if e.last_quote_at), default=None)
        return QuoteRefreshResult(updated=0, asof_utc=asof, error=str(exc))

    resolved_quotes: dict[int, Any] = {}
    resolved_provider_symbols: dict[int, str] = {}
    missing = [e for e in to_refresh if e.provider_symbol not in quotes]
    if missing:
        candidates: set[str] = set()
        for e in missing:
            candidates.add(e.symbol)
            if "." in e.symbol:
                candidates.add(e.symbol.replace(".", "-"))
        try:
            alt_quotes = fetch_quotes(
                sorted(candidates),
                user_agent=settings.discord_market_user_agent,
                allow_chart_fallback=(not _discord_yahoo_bulk_only()),
            )
        except YahooFinanceError:
            alt_quotes = {}
        for e in missing:
            q = alt_quotes.get(e.symbol) or alt_quotes.get(e.symbol.replace(".", "-"))
            if q:
                resolved_quotes[e.id] = q
                resolved_provider_symbols[e.id] = q.symbol

    updated = 0
    asof_candidates: list[datetime] = []
    with session_scope() as session:
        for entry in to_refresh:
            quote = resolved_quotes.get(entry.id) or quotes.get(entry.provider_symbol)
            if not quote:
                continue
            row = session.query(DiscordWatchlistEntry).filter_by(id=int(entry.id)).first()
            if not row or row.removed_at is not None:
                continue
            if entry.id in resolved_provider_symbols:
                row.provider_symbol = resolved_provider_symbols[entry.id]
            row.last_quote_price = quote.price
            row.last_quote_currency = quote.currency
            row.last_quote_at = quote.market_time_utc
            row.exchange = quote.exchange
            row.full_exchange_name = quote.full_exchange_name
            row.company_name = quote.name
            if quote.market_time_utc:
                asof_candidates.append(quote.market_time_utc)
            updated += 1

    asof = max(asof_candidates) if asof_candidates else max((e.last_quote_at for e in entries if e.last_quote_at), default=None)
    warn = None
    if to_refresh and updated == 0:
        warn = "Yahoo returned no quote matches for this selection; check ticker/provider symbols."
    return QuoteRefreshResult(updated=updated, asof_utc=asof, error=warn)


@dataclass(frozen=True)
class PerfRow:
    sector: str | None
    subgroup: str | None
    symbol: str
    company_name: str | None
    exchange: str | None
    full_exchange_name: str | None
    since_24h: float | None
    baseline_pending: bool
    m1: float | None
    ytd: float | None
    y1: float | None


@dataclass(frozen=True)
class PerfResult:
    rows: list[PerfRow]
    asof_utc: datetime | None
    error: str | None


@dataclass(frozen=True)
class SourceRecord:
    id: int
    symbol: str
    title: str
    url: str
    note: str | None
    sentiment: str | None
    tags: list[str]
    source_type: str
    pinned: bool
    created_at: datetime
    created_by_user_id: int
    discord_message_id: int | None
    discord_channel_id: int | None
    evidence_id: int
    issuer_id: int | None = None
    company_id: int | None = None
    target_kind: str = "ticker"
    target_key: str | None = None
    target_label: str | None = None


@dataclass(frozen=True)
class EvidenceSubmissionResult:
    status: str
    queue_id: int | None
    evidence_id: int | None
    created: bool
    url_canonical: str
    target: str
    message: str


@dataclass(frozen=True)
class PublicAlertSubscription:
    channel_id: int
    query: str | None
    created_by_user_id: int
    created_at: str | None


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(value: str | None, *, default: str) -> str:
    cleaned = _SLUG_RE.sub("-", str(value or "").strip().lower()).strip("-")
    return cleaned or default


def _canonicalize_source_url(raw: str | None) -> str | None:
    from thoth_terminal.discord.link_intake import canonicalize_url

    return canonicalize_url(raw)


def _ensure_issuer_slug(session, issuer: Issuer) -> str:
    slug = str(getattr(issuer, "slug", "") or "").strip()
    if slug:
        return slug
    base = _slugify(
        str(getattr(issuer, "display_name", None) or getattr(issuer, "legal_name", None) or f"issuer-{int(issuer.id)}"),
        default=f"issuer-{int(issuer.id)}",
    )
    slug = base
    counter = 2
    while session.query(Issuer).filter(Issuer.slug == slug, Issuer.id != int(issuer.id)).first():
        slug = f"{base}-{counter}"
        counter += 1
    issuer.slug = slug
    session.flush()
    return slug


def _ensure_issuer_for_company(session, company: Company) -> int | None:
    if getattr(company, "issuer_id", None):
        issuer = session.query(Issuer).filter_by(id=int(company.issuer_id)).first()
        if issuer:
            _ensure_issuer_slug(session, issuer)
            return int(issuer.id)
    name = str(getattr(company, "name", "") or "").strip()
    if not name:
        return None
    issuer = (
        session.query(Issuer)
        .filter((Issuer.display_name == name) | (Issuer.legal_name == name))
        .first()
    )
    if issuer is None:
        issuer = Issuer(
            slug=_slugify(name, default=f"issuer-{int(company.id)}"),
            legal_name=name,
            display_name=name,
            jurisdiction=None,
            entity_status="ACTIVE",
            sector=getattr(company, "sector", None),
            industry=getattr(company, "industry", None),
            sic_code=None,
            website=None,
            ir_website=getattr(company, "ir_url", None),
            created_at=_now(),
            updated_at=_now(),
            extra={},
        )
        session.add(issuer)
        session.flush()
    else:
        _ensure_issuer_slug(session, issuer)
    company.issuer_id = int(issuer.id)
    session.flush()
    ticker = normalize_symbol(getattr(company, "ticker", None) or "")
    if ticker and not session.query(IssuerIdentifier).filter_by(id_type="TICKER", id_value=ticker).first():
        session.add(
            IssuerIdentifier(
                issuer_id=int(issuer.id),
                id_type="TICKER",
                id_value=ticker,
                exchange=str(getattr(company, "exchange", None) or "").strip() or None,
                currency=None,
                is_primary=True,
                valid_from=None,
                valid_to=None,
                created_at=_now(),
            )
        )
        session.flush()
    return int(issuer.id)


def _ensure_private_issuer(session, *, company_name: str) -> Issuer:
    name = str(company_name or "").strip()
    if not name:
        raise ValueError("Missing company name.")
    issuer = (
        session.query(Issuer)
        .filter((Issuer.display_name == name) | (Issuer.legal_name == name))
        .first()
    )
    if issuer is not None:
        _ensure_issuer_slug(session, issuer)
        return issuer
    base = _slugify(name, default="private-company")
    slug = base
    counter = 2
    while session.query(Issuer).filter_by(slug=slug).first():
        slug = f"{base}-{counter}"
        counter += 1
    issuer = Issuer(
        slug=slug,
        legal_name=name,
        display_name=name,
        jurisdiction=None,
        entity_status="PRIVATE",
        sector=None,
        industry=None,
        sic_code=None,
        website=None,
        ir_website=None,
        created_at=_now(),
        updated_at=_now(),
        extra={"private": True},
    )
    session.add(issuer)
    session.flush()
    return issuer


def _resolve_symbol_target(session, *, symbol: str) -> dict[str, Any]:
    sym = normalize_symbol(symbol)
    if not sym:
        raise ValueError("Missing ticker.")
    ident = (
        session.query(IssuerIdentifier)
        .filter(IssuerIdentifier.id_type == "TICKER", IssuerIdentifier.id_value == sym)
        .order_by(IssuerIdentifier.is_primary.desc(), IssuerIdentifier.id.asc())
        .first()
    )
    issuer = None
    if ident:
        issuer = session.query(Issuer).filter_by(id=int(ident.issuer_id)).first()
    company = session.query(Company).filter(Company.ticker == sym).order_by(Company.id.asc()).first()
    if issuer is None and company is not None:
        issuer_id = _ensure_issuer_for_company(session, company)
        issuer = session.query(Issuer).filter_by(id=int(issuer_id)).first() if issuer_id is not None else None
    if issuer is not None:
        _ensure_issuer_slug(session, issuer)
    return {
        "target_key": f"ticker:{sym}",
        "target_kind": "ticker",
        "target_label": (
            str(getattr(issuer, "display_name", None) or "")
            or str(getattr(company, "name", None) or "")
            or sym
        ),
        "issuer_id": int(issuer.id) if issuer is not None else None,
        "company_id": int(company.id) if company is not None else None,
        "symbol": sym,
    }


def _source_record_from_evidence(session, evidence: DiscordEvidenceItem, target: DiscordEvidenceTarget | None = None) -> SourceRecord:
    symbol = normalize_symbol(getattr(target, "symbol", None) or "")
    if not symbol and getattr(target, "issuer_id", None):
        ident = (
            session.query(IssuerIdentifier)
            .filter(
                IssuerIdentifier.issuer_id == int(target.issuer_id),
                IssuerIdentifier.id_type == "TICKER",
            )
            .order_by(IssuerIdentifier.is_primary.desc(), IssuerIdentifier.id.asc())
            .first()
        )
        if ident:
            symbol = normalize_symbol(ident.id_value)
    return SourceRecord(
        id=int(evidence.id),
        symbol=symbol,
        title=str(evidence.title or ""),
        url=str(evidence.url_canonical or evidence.url_raw or ""),
        note=evidence.note,
        sentiment=evidence.sentiment,
        tags=list(evidence.tags or []),
        source_type=str(evidence.source_type or "other"),
        pinned=bool(evidence.pinned),
        created_at=evidence.created_at,
        created_by_user_id=int(evidence.created_by_user_id or 0),
        discord_message_id=(int(evidence.discord_message_id) if evidence.discord_message_id else None),
        discord_channel_id=(int(evidence.discord_channel_id) if evidence.discord_channel_id else None),
        evidence_id=int(evidence.id),
        issuer_id=(int(target.issuer_id) if target and target.issuer_id is not None else None),
        company_id=(int(target.company_id) if target and target.company_id is not None else None),
        target_kind=(str(target.target_kind) if target else "ticker"),
        target_key=(str(target.target_key) if target else None),
        target_label=(str(target.target_label) if target and target.target_label else None),
    )


def ensure_evidence_item(
    session,
    *,
    guild_id: int,
    url_raw: str,
    title: str,
    note: str | None,
    sentiment: str | None,
    tags: list[str] | None,
    source_type: str,
    user_id: int,
    discord_message_id: int | None = None,
    discord_channel_id: int | None = None,
    resolution_state: str = "resolved",
    candidate_targets_json: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> DiscordEvidenceItem:
    stamp = now or _now()
    canonical_url = _canonicalize_source_url(url_raw) or str(url_raw).strip()
    digest = __import__("hashlib").sha256(canonical_url.encode("utf-8")).hexdigest()
    evidence = (
        session.query(DiscordEvidenceItem)
        .filter_by(guild_id=int(guild_id), url_hash=digest)
        .first()
    )
    tags_v = sorted({str(tag).strip() for tag in (tags or []) if str(tag).strip()})
    note_v = str(note).strip() if isinstance(note, str) and str(note).strip() else None
    sentiment_v = str(sentiment).strip().lower() if isinstance(sentiment, str) and str(sentiment).strip() else None
    title_v = (str(title or "").strip() or canonical_url)[:512]
    if evidence is None:
        evidence = DiscordEvidenceItem(
            guild_id=int(guild_id),
            url_raw=str(url_raw).strip()[:2048],
            url_canonical=canonical_url[:2048],
            url_hash=digest,
            title=title_v,
            note=note_v,
            sentiment=sentiment_v,
            tags=tags_v,
            source_type=(str(source_type or "other").strip() or "other")[:32],
            pinned=False,
            resolution_state=str(resolution_state or "resolved").strip() or "resolved",
            candidate_targets_json=(dict(candidate_targets_json) if isinstance(candidate_targets_json, dict) else None),
            created_at=stamp,
            updated_at=stamp,
            created_by_user_id=int(user_id),
            discord_message_id=int(discord_message_id) if discord_message_id else None,
            discord_channel_id=int(discord_channel_id) if discord_channel_id else None,
        )
        session.add(evidence)
        session.flush()
        return evidence
    if title_v and not str(evidence.title or "").strip():
        evidence.title = title_v
    if note_v and not str(evidence.note or "").strip():
        evidence.note = note_v
    if sentiment_v:
        evidence.sentiment = sentiment_v
    if tags_v:
        evidence.tags = sorted({str(tag).strip() for tag in (evidence.tags or []) if str(tag).strip()} | set(tags_v))
    if not str(evidence.source_type or "").strip() or str(evidence.source_type or "").strip().lower() == "other":
        evidence.source_type = (str(source_type or "other").strip() or "other")[:32]
    if isinstance(candidate_targets_json, dict) and not isinstance(evidence.candidate_targets_json, dict):
        evidence.candidate_targets_json = dict(candidate_targets_json)
    if resolution_state:
        current = str(evidence.resolution_state or "").strip().lower()
        desired = str(resolution_state).strip().lower()
        if current != "resolved" or desired == "resolved":
            evidence.resolution_state = desired
    evidence.updated_at = stamp
    if discord_message_id and not evidence.discord_message_id:
        evidence.discord_message_id = int(discord_message_id)
    if discord_channel_id and not evidence.discord_channel_id:
        evidence.discord_channel_id = int(discord_channel_id)
    session.flush()
    return evidence


def build_topic_target(*, topic: str, label: str | None = None) -> dict[str, Any]:
    name = str(topic or "").strip().lower()
    if not name:
        raise ValueError("Missing topic.")
    return {
        "target_key": f"topic:{name}",
        "target_kind": "topic",
        "target_label": str(label or topic).strip() or topic,
        "issuer_id": None,
        "company_id": None,
        "symbol": None,
    }


def build_private_company_target(session, *, company_name: str) -> dict[str, Any]:
    issuer = _ensure_private_issuer(session, company_name=company_name)
    return {
        "target_key": f"issuer:{_ensure_issuer_slug(session, issuer)}",
        "target_kind": "issuer",
        "target_label": issuer.display_name,
        "issuer_id": int(issuer.id),
        "company_id": None,
        "symbol": None,
    }


def build_ticker_target(session, *, ticker: str) -> dict[str, Any]:
    return _resolve_symbol_target(session, symbol=ticker)


def attach_evidence_target(
    session,
    *,
    evidence: DiscordEvidenceItem,
    target: dict[str, Any],
    resolution_method: str,
    is_primary: bool = False,
    confidence: float = 1.0,
    now: datetime | None = None,
) -> DiscordEvidenceTarget:
    stamp = now or _now()
    target_key = str(target.get("target_key") or "").strip()
    if not target_key:
        raise ValueError("Missing target key.")
    row = (
        session.query(DiscordEvidenceTarget)
        .filter_by(evidence_id=int(evidence.id), target_key=target_key)
        .first()
    )
    if row is None:
        row = DiscordEvidenceTarget(
            evidence_id=int(evidence.id),
            target_key=target_key,
            target_kind=str(target.get("target_kind") or "other").strip() or "other",
            target_label=(str(target.get("target_label") or "").strip() or None),
            issuer_id=(int(target["issuer_id"]) if target.get("issuer_id") is not None else None),
            company_id=(int(target["company_id"]) if target.get("company_id") is not None else None),
            symbol=(normalize_symbol(target.get("symbol") or "") or None),
            confidence=float(confidence),
            resolution_method=str(resolution_method or "").strip() or None,
            is_primary=bool(is_primary),
            extra_json=(dict(target.get("extra_json")) if isinstance(target.get("extra_json"), dict) else None),
            created_at=stamp,
            updated_at=stamp,
        )
        session.add(row)
    else:
        row.target_kind = str(target.get("target_kind") or row.target_kind or "other").strip() or "other"
        row.target_label = (str(target.get("target_label") or row.target_label or "").strip() or None)
        if target.get("issuer_id") is not None:
            row.issuer_id = int(target["issuer_id"])
        if target.get("company_id") is not None:
            row.company_id = int(target["company_id"])
        if target.get("symbol"):
            row.symbol = normalize_symbol(target.get("symbol") or "") or row.symbol
        row.confidence = max(float(row.confidence or 0.0), float(confidence))
        row.resolution_method = str(resolution_method or row.resolution_method or "").strip() or row.resolution_method
        row.is_primary = bool(row.is_primary or is_primary)
        if isinstance(target.get("extra_json"), dict):
            merged = dict(row.extra_json or {})
            merged.update(dict(target.get("extra_json") or {}))
            row.extra_json = merged
        row.updated_at = stamp
    evidence.resolution_state = "resolved"
    evidence.updated_at = stamp
    session.flush()
    return row


def _pct_return(current: float | None, base: float | None) -> float | None:
    if current is None or base is None:
        return None
    if base == 0:
        return None
    return (current / base) - 1.0


@dataclass(frozen=True)
class GuildWatchlistRefreshResult:
    entries_total: int
    entries_with_quotes: int
    asof_utc: datetime | None
    error: str | None


def refresh_guild_watchlists(
    *,
    guild_id: int,
    force_refresh: bool = False,
) -> GuildWatchlistRefreshResult:
    now = _now()
    SessionLocal = session_factory()
    with SessionLocal() as session:
        entries = (
            session.query(DiscordWatchlistEntry)
            .filter_by(guild_id=int(guild_id), removed_at=None)
            .order_by(
                DiscordWatchlistEntry.list_kind.asc(),
                DiscordWatchlistEntry.sector_key.asc(),
                DiscordWatchlistEntry.subgroup_key.asc(),
                DiscordWatchlistEntry.symbol.asc(),
            )
            .all()
        )

    if not entries:
        return GuildWatchlistRefreshResult(entries_total=0, entries_with_quotes=0, asof_utc=None, error=None)

    bulk_only = _discord_yahoo_bulk_only()
    provider_symbols = sorted({e.provider_symbol for e in entries if e.provider_symbol})
    try:
        quotes = fetch_quotes(
            provider_symbols,
            user_agent=settings.discord_market_user_agent,
            allow_chart_fallback=(not bulk_only),
        )
    except YahooFinanceError as exc:
        asof = max((e.last_quote_at for e in entries if e.last_quote_at), default=None)
        return GuildWatchlistRefreshResult(
            entries_total=len(entries),
            entries_with_quotes=0,
            asof_utc=asof,
            error=str(exc),
        )

    # Resolve symbol drifts via one extra bulk pass; keep strict mode (no chart fallback).
    resolved_quotes: dict[int, Any] = {}
    resolved_provider_symbols: dict[int, str] = {}
    missing_entries = [e for e in entries if e.provider_symbol not in quotes]
    if missing_entries:
        candidates: set[str] = set()
        for e in missing_entries:
            candidates.add(e.symbol)
            if "." in e.symbol:
                candidates.add(e.symbol.replace(".", "-"))
        try:
            alt_quotes = fetch_quotes(
                sorted(candidates),
                user_agent=settings.discord_market_user_agent,
                allow_chart_fallback=(not bulk_only),
            )
        except YahooFinanceError:
            alt_quotes = {}
        for e in missing_entries:
            q = alt_quotes.get(e.symbol) or alt_quotes.get(e.symbol.replace(".", "-"))
            if q:
                resolved_quotes[e.id] = q
                resolved_provider_symbols[e.id] = q.symbol

    bulk_returns: dict[str, dict[str, float | None]] = {}
    if bulk_only:
        bulk_return_symbols: set[str] = set(provider_symbols)
        for entry in entries:
            bulk_return_symbols.add(entry.symbol)
            if "." in entry.symbol:
                bulk_return_symbols.add(entry.symbol.replace(".", "-"))
        for sym in resolved_provider_symbols.values():
            if sym:
                bulk_return_symbols.add(str(sym).upper())
        try:
            bulk_returns = fetch_bulk_returns(
                sorted(bulk_return_symbols),
                user_agent=settings.discord_market_user_agent,
            )
        except YahooFinanceError:
            bulk_returns = {}

    perf_cache_s = int(getattr(settings, "discord_perf_cache_seconds", 600) or 600)
    refresh_cutoff = now - timedelta(seconds=perf_cache_s)
    updates: list[dict[str, Any]] = []
    asof_candidates: list[datetime] = []
    quoted = 0

    for entry in entries:
        quote = resolved_quotes.get(entry.id) or quotes.get(entry.provider_symbol)
        if quote and quote.market_time_utc:
            asof_candidates.append(quote.market_time_utc)
            quoted += 1

        baseline_price = entry.baseline_price
        baseline_pending = baseline_price is None
        baseline_captured = False
        if baseline_price is None and entry.baseline_due_at and entry.baseline_due_at <= now and quote:
            baseline_price = quote.price
            baseline_pending = False
            baseline_captured = True

        cached = entry.last_returns if isinstance(entry.last_returns, dict) else {}
        m1 = cached.get("m1") if isinstance(cached.get("m1"), (int, float)) else None
        ytd = cached.get("ytd") if isinstance(cached.get("ytd"), (int, float)) else None
        y1 = cached.get("y1") if isinstance(cached.get("y1"), (int, float)) else None
        needs_refresh = force_refresh or entry.last_returns_at is None or (entry.last_returns_at < refresh_cutoff)

        if needs_refresh and quote:
            if quote.one_month_return is not None:
                m1 = float(quote.one_month_return)
            if quote.ytd_return is not None:
                ytd = float(quote.ytd_return)
            if quote.one_year_return is not None:
                y1 = float(quote.one_year_return)

            if bulk_only and any(v is None for v in (m1, ytd, y1)):
                keys = [
                    str(resolved_provider_symbols.get(entry.id) or "").upper(),
                    str(entry.provider_symbol or "").upper(),
                    str(entry.symbol or "").upper(),
                    str((entry.symbol or "").replace(".", "-")).upper(),
                ]
                bulk_ret = None
                for key in keys:
                    if not key:
                        continue
                    bulk_ret = bulk_returns.get(key)
                    if bulk_ret:
                        break
                if bulk_ret:
                    if m1 is None and isinstance(bulk_ret.get("m1"), (int, float)):
                        m1 = float(bulk_ret.get("m1"))
                    if ytd is None and isinstance(bulk_ret.get("ytd"), (int, float)):
                        ytd = float(bulk_ret.get("ytd"))
                    if y1 is None and isinstance(bulk_ret.get("y1"), (int, float)):
                        y1 = float(bulk_ret.get("y1"))

            missing = any(v is None for v in (m1, ytd, y1))
            if missing and (not bulk_only):
                try:
                    bars = fetch_adjclose_1y(entry.provider_symbol, user_agent=settings.discord_market_user_agent)
                    m1_base = pick_adjclose(bars, target_utc=now - timedelta(days=30), fallback_after=True)
                    y1_base = pick_adjclose(bars, target_utc=now - timedelta(days=365), fallback_after=True)
                    ytd_base = pick_adjclose(bars, target_utc=datetime(now.year, 1, 1), fallback_after=True)
                    m1 = _pct_return(quote.price, m1_base)
                    y1 = _pct_return(quote.price, y1_base)
                    ytd = _pct_return(quote.price, ytd_base)
                except YahooFinanceError:
                    pass

        since_24h = _pct_return(quote.price if quote else None, baseline_price)
        if baseline_pending:
            since_24h = None

        update_payload: dict[str, Any] = {
            "id": entry.id,
            "last_returns": dict(cached),
        }
        if entry.id in resolved_provider_symbols:
            update_payload["provider_symbol"] = resolved_provider_symbols[entry.id]
        if quote:
            update_payload["last_quote_price"] = quote.price
            update_payload["last_quote_currency"] = quote.currency
            update_payload["last_quote_at"] = quote.market_time_utc
            update_payload["company_name"] = quote.name
            update_payload["exchange"] = quote.exchange
            update_payload["full_exchange_name"] = quote.full_exchange_name
            update_payload["last_returns"]["since_24h"] = since_24h
        if needs_refresh and quote:
            update_payload["last_returns"]["m1"] = m1
            update_payload["last_returns"]["ytd"] = ytd
            update_payload["last_returns"]["y1"] = y1
            update_payload["last_returns_at"] = now
        if baseline_captured and quote:
            update_payload["baseline_price"] = baseline_price
            update_payload["baseline_currency"] = quote.currency
            update_payload["baseline_quote_at"] = quote.market_time_utc
            update_payload["baseline_captured_at"] = now
        updates.append(update_payload)

    with session_scope() as session:
        for upd in updates:
            row = session.query(DiscordWatchlistEntry).filter_by(id=int(upd["id"])).first()
            if not row:
                continue
            if "provider_symbol" in upd:
                row.provider_symbol = str(upd.get("provider_symbol") or row.provider_symbol)
            if "baseline_price" in upd:
                row.baseline_price = upd.get("baseline_price")
                row.baseline_currency = upd.get("baseline_currency")
                row.baseline_quote_at = upd.get("baseline_quote_at")
                row.baseline_captured_at = upd.get("baseline_captured_at")
            if "last_quote_price" in upd:
                row.last_quote_price = upd.get("last_quote_price")
                row.last_quote_currency = upd.get("last_quote_currency")
                row.last_quote_at = upd.get("last_quote_at")
                row.company_name = upd.get("company_name") or row.company_name
                row.exchange = upd.get("exchange")
                row.full_exchange_name = upd.get("full_exchange_name")
            row.last_returns = upd.get("last_returns") or {}
            if "last_returns_at" in upd:
                row.last_returns_at = upd.get("last_returns_at")

    asof = max(asof_candidates) if asof_candidates else max((e.last_quote_at for e in entries if e.last_quote_at), default=None)
    warn = None
    if entries and quoted == 0:
        warn = "Yahoo returned no quote matches for active watchlists; check ticker/provider symbols."
    return GuildWatchlistRefreshResult(
        entries_total=len(entries),
        entries_with_quotes=quoted,
        asof_utc=asof,
        error=warn,
    )


def refresh_watchlist_perf(
    *,
    guild_id: int,
    list_kind: str,
    sector: str | None = None,
    subgroup: str | None = None,
    force_refresh: bool = False,
) -> PerfResult:
    kind = _normalize_list_kind(list_kind)
    now = _now()
    sector_key = sector.strip().lower() if sector else None
    subgroup_key = subgroup.strip().lower() if subgroup else None
    SessionLocal = session_factory()
    with SessionLocal() as session:
        q = (
            session.query(DiscordWatchlistEntry)
            .filter_by(guild_id=int(guild_id), list_kind=str(kind), removed_at=None)
        )
        if _grouped_list_kind(kind) and sector_key:
            q = q.filter(DiscordWatchlistEntry.sector_key == sector_key)
        if _grouped_list_kind(kind) and subgroup_key:
            q = q.filter(DiscordWatchlistEntry.subgroup_key == subgroup_key)
        entries = q.order_by(DiscordWatchlistEntry.symbol.asc()).all()

    if not entries:
        return PerfResult(rows=[], asof_utc=None, error=None)

    bulk_only = _discord_yahoo_bulk_only()
    provider_symbols = [e.provider_symbol for e in entries]
    try:
        quotes = fetch_quotes(
            provider_symbols,
            user_agent=settings.discord_market_user_agent,
            allow_chart_fallback=(not bulk_only),
        )
    except YahooFinanceError as exc:
        # Don't mutate the DB; return cached rows if available.
        rows: list[PerfRow] = []
        for e in entries:
            r = e.last_returns if isinstance(e.last_returns, dict) else {}
            rows.append(
                PerfRow(
                    sector=e.sector if _grouped_list_kind(kind) else None,
                    subgroup=e.subgroup if _grouped_list_kind(kind) else None,
                    symbol=e.symbol,
                    company_name=getattr(e, "company_name", None),
                    exchange=e.exchange,
                    full_exchange_name=e.full_exchange_name,
                    since_24h=(r.get("since_24h") if isinstance(r.get("since_24h"), (int, float)) else None),
                    baseline_pending=(e.baseline_price is None),
                    m1=(r.get("m1") if isinstance(r.get("m1"), (int, float)) else None),
                    ytd=(r.get("ytd") if isinstance(r.get("ytd"), (int, float)) else None),
                    y1=(r.get("y1") if isinstance(r.get("y1"), (int, float)) else None),
                )
            )
        return PerfResult(rows=rows, asof_utc=None, error=str(exc))

    # If we couldn't quote some provider symbols (common after "validation pending" adds), try a single
    # fallback batch using the canonical symbol and common Yahoo normalization.
    resolved_quotes: dict[int, Any] = {}
    resolved_provider_symbols: dict[int, str] = {}
    missing_entries = [e for e in entries if e.provider_symbol not in quotes]
    if missing_entries:
        candidates: set[str] = set()
        for e in missing_entries:
            candidates.add(e.symbol)
            if "." in e.symbol:
                candidates.add(e.symbol.replace(".", "-"))
        try:
            alt_quotes = fetch_quotes(
                sorted(candidates),
                user_agent=settings.discord_market_user_agent,
                allow_chart_fallback=(not bulk_only),
            )
        except YahooFinanceError:
            alt_quotes = {}
        for e in missing_entries:
            q = alt_quotes.get(e.symbol) or alt_quotes.get(e.symbol.replace(".", "-"))
            if q:
                resolved_quotes[e.id] = q
                resolved_provider_symbols[e.id] = q.symbol

    bulk_returns: dict[str, dict[str, float | None]] = {}
    if bulk_only:
        bulk_return_symbols: set[str] = set()
        for entry in entries:
            if entry.provider_symbol:
                bulk_return_symbols.add(entry.provider_symbol)
            bulk_return_symbols.add(entry.symbol)
            if "." in entry.symbol:
                bulk_return_symbols.add(entry.symbol.replace(".", "-"))
        for sym in resolved_provider_symbols.values():
            if sym:
                bulk_return_symbols.add(str(sym).upper())
        try:
            bulk_returns = fetch_bulk_returns(
                sorted(bulk_return_symbols),
                user_agent=settings.discord_market_user_agent,
            )
        except YahooFinanceError:
            bulk_returns = {}

    perf_cache_s = int(getattr(settings, "discord_perf_cache_seconds", 600) or 600)
    refresh_cutoff = now - timedelta(seconds=perf_cache_s)

    updates: list[dict[str, Any]] = []
    computed_rows: list[PerfRow] = []
    asof_candidates: list[datetime] = []
    quoted_total = 0

    for entry in entries:
        quote = resolved_quotes.get(entry.id) or quotes.get(entry.provider_symbol)
        if quote:
            quoted_total += 1
        if quote and quote.market_time_utc:
            asof_candidates.append(quote.market_time_utc)

        baseline_price = entry.baseline_price
        baseline_pending = baseline_price is None

        # Capture baseline if due and missing.
        baseline_captured = False
        if baseline_price is None and entry.baseline_due_at and entry.baseline_due_at <= now and quote:
            baseline_price = quote.price
            baseline_pending = False
            baseline_captured = True

        # Use cached values unless stale/forced and we have a quote.
        cached = entry.last_returns if isinstance(entry.last_returns, dict) else {}
        m1 = cached.get("m1") if isinstance(cached.get("m1"), (int, float)) else None
        ytd = cached.get("ytd") if isinstance(cached.get("ytd"), (int, float)) else None
        y1 = cached.get("y1") if isinstance(cached.get("y1"), (int, float)) else None

        needs_refresh = force_refresh or entry.last_returns_at is None or (entry.last_returns_at < refresh_cutoff)

        if needs_refresh and quote:
            # Prefer quote-supplied returns when present to reduce chart calls.
            if quote.one_month_return is not None:
                m1 = float(quote.one_month_return)
            if quote.ytd_return is not None:
                ytd = float(quote.ytd_return)
            if quote.one_year_return is not None:
                y1 = float(quote.one_year_return)

            if bulk_only and any(v is None for v in (m1, ytd, y1)):
                keys = [
                    str(resolved_provider_symbols.get(entry.id) or "").upper(),
                    str(entry.provider_symbol or "").upper(),
                    str(entry.symbol or "").upper(),
                    str((entry.symbol or "").replace(".", "-")).upper(),
                ]
                bulk_ret = None
                for key in keys:
                    if not key:
                        continue
                    bulk_ret = bulk_returns.get(key)
                    if bulk_ret:
                        break
                if bulk_ret:
                    if m1 is None and isinstance(bulk_ret.get("m1"), (int, float)):
                        m1 = float(bulk_ret.get("m1"))
                    if ytd is None and isinstance(bulk_ret.get("ytd"), (int, float)):
                        ytd = float(bulk_ret.get("ytd"))
                    if y1 is None and isinstance(bulk_ret.get("y1"), (int, float)):
                        y1 = float(bulk_ret.get("y1"))

            missing = any(v is None for v in (m1, ytd, y1))
            if missing and (not bulk_only):
                try:
                    bars = fetch_adjclose_1y(entry.provider_symbol, user_agent=settings.discord_market_user_agent)
                    m1_base = pick_adjclose(bars, target_utc=now - timedelta(days=30), fallback_after=True)
                    y1_base = pick_adjclose(bars, target_utc=now - timedelta(days=365), fallback_after=True)
                    ytd_base = pick_adjclose(bars, target_utc=datetime(now.year, 1, 1), fallback_after=True)
                    m1 = _pct_return(quote.price, m1_base)
                    y1 = _pct_return(quote.price, y1_base)
                    ytd = _pct_return(quote.price, ytd_base)
                except YahooFinanceError:
                    # Keep cached values on per-ticker history failure.
                    pass

        since_24h = _pct_return(quote.price if quote else None, baseline_price)
        if baseline_pending:
            since_24h = None

        update_payload: dict[str, Any] = {
            "id": entry.id,
            "last_returns": dict(cached),
        }
        if entry.id in resolved_provider_symbols:
            update_payload["provider_symbol"] = resolved_provider_symbols[entry.id]
        if quote:
            update_payload["last_quote_price"] = quote.price
            update_payload["last_quote_currency"] = quote.currency
            update_payload["last_quote_at"] = quote.market_time_utc
            update_payload["company_name"] = quote.name
            update_payload["exchange"] = quote.exchange
            update_payload["full_exchange_name"] = quote.full_exchange_name
            update_payload["last_returns"]["since_24h"] = since_24h
        if needs_refresh and quote:
            update_payload["last_returns"]["m1"] = m1
            update_payload["last_returns"]["ytd"] = ytd
            update_payload["last_returns"]["y1"] = y1
            update_payload["last_returns_at"] = now
        if baseline_captured and quote:
            update_payload["baseline_price"] = baseline_price
            update_payload["baseline_currency"] = quote.currency
            update_payload["baseline_quote_at"] = quote.market_time_utc
            update_payload["baseline_captured_at"] = now
        updates.append(update_payload)

        computed_rows.append(
            PerfRow(
                sector=entry.sector if _grouped_list_kind(kind) else None,
                subgroup=entry.subgroup if _grouped_list_kind(kind) else None,
                symbol=entry.symbol,
                company_name=(quote.name if quote else getattr(entry, "company_name", None)),
                exchange=(quote.exchange if quote else entry.exchange),
                full_exchange_name=(quote.full_exchange_name if quote else entry.full_exchange_name),
                since_24h=since_24h,
                baseline_pending=baseline_pending,
                m1=m1,
                ytd=ytd,
                y1=y1,
            )
        )

    # Persist updates.
    with session_scope() as session:
        for upd in updates:
            row = session.query(DiscordWatchlistEntry).filter_by(id=int(upd["id"])).first()
            if not row:
                continue
            if "provider_symbol" in upd:
                row.provider_symbol = str(upd.get("provider_symbol") or row.provider_symbol)
            if "baseline_price" in upd:
                row.baseline_price = upd.get("baseline_price")
                row.baseline_currency = upd.get("baseline_currency")
                row.baseline_quote_at = upd.get("baseline_quote_at")
                row.baseline_captured_at = upd.get("baseline_captured_at")
            if "last_quote_price" in upd:
                row.last_quote_price = upd.get("last_quote_price")
                row.last_quote_currency = upd.get("last_quote_currency")
                row.last_quote_at = upd.get("last_quote_at")
                row.company_name = upd.get("company_name") or row.company_name
                row.exchange = upd.get("exchange")
                row.full_exchange_name = upd.get("full_exchange_name")
            row.last_returns = upd.get("last_returns") or {}
            if "last_returns_at" in upd:
                row.last_returns_at = upd.get("last_returns_at")

    asof = max(asof_candidates) if asof_candidates else None
    warn = None
    if entries and quoted_total == 0:
        warn = "Yahoo returned no quote matches for this selection; check ticker/provider symbols."
    return PerfResult(rows=computed_rows, asof_utc=asof, error=warn)


def list_watchlist_perf_cached(
    *,
    guild_id: int,
    list_kind: str,
    sector: str | None = None,
    subgroup: str | None = None,
) -> PerfResult:
    """
    Return cached performance rows from the DB without hitting Yahoo.

    This is useful for Discord UI interactions (paging/filtering/sorting) where we want
    fast responses, with a separate "refresh" action that can call Yahoo.
    """
    kind = _normalize_list_kind(list_kind)
    sector_key = sector.strip().lower() if sector else None
    subgroup_key = subgroup.strip().lower() if subgroup else None
    SessionLocal = session_factory()
    with SessionLocal() as session:
        q = (
            session.query(DiscordWatchlistEntry)
            .filter_by(guild_id=int(guild_id), list_kind=str(kind), removed_at=None)
        )
        if _grouped_list_kind(kind) and sector_key:
            q = q.filter(DiscordWatchlistEntry.sector_key == sector_key)
        if _grouped_list_kind(kind) and subgroup_key:
            q = q.filter(DiscordWatchlistEntry.subgroup_key == subgroup_key)
        if _grouped_list_kind(kind):
            q = q.order_by(
                DiscordWatchlistEntry.sector_key.asc(),
                DiscordWatchlistEntry.subgroup_key.asc(),
                DiscordWatchlistEntry.symbol.asc(),
            )
        else:
            q = q.order_by(DiscordWatchlistEntry.symbol.asc())
        entries = q.all()

    if not entries:
        return PerfResult(rows=[], asof_utc=None, error=None)

    rows: list[PerfRow] = []
    asof_candidates: list[datetime] = []
    for e in entries:
        r = e.last_returns if isinstance(e.last_returns, dict) else {}
        rows.append(
            PerfRow(
                sector=(e.sector if _grouped_list_kind(kind) else None),
                subgroup=(e.subgroup if _grouped_list_kind(kind) else None),
                symbol=e.symbol,
                company_name=getattr(e, "company_name", None),
                exchange=e.exchange,
                full_exchange_name=e.full_exchange_name,
                since_24h=(r.get("since_24h") if isinstance(r.get("since_24h"), (int, float)) else None),
                baseline_pending=(e.baseline_price is None),
                m1=(r.get("m1") if isinstance(r.get("m1"), (int, float)) else None),
                ytd=(r.get("ytd") if isinstance(r.get("ytd"), (int, float)) else None),
                y1=(r.get("y1") if isinstance(r.get("y1"), (int, float)) else None),
            )
        )
        if e.last_quote_at:
            asof_candidates.append(e.last_quote_at)
        if e.last_returns_at:
            asof_candidates.append(e.last_returns_at)

    asof = max(asof_candidates) if asof_candidates else None
    return PerfResult(rows=rows, asof_utc=asof, error=None)


def create_ticker_source(
    *,
    guild_id: int,
    symbol: str,
    title: str,
    url: str,
    note: str | None,
    sentiment: str | None,
    tags: list[str] | None,
    source_type: str,
    user_id: int,
    discord_message_id: int | None = None,
    discord_channel_id: int | None = None,
) -> SourceRecord:
    sym = normalize_symbol(symbol)
    if not sym:
        raise ValueError("Missing ticker.")
    if not url or not str(url).strip():
        raise ValueError("Missing URL.")
    title_v = (title or "").strip()
    if not title_v:
        parsed = urlparse(url)
        title_v = (parsed.netloc + parsed.path)[:200] or url[:200]

    canonical_url = _canonicalize_source_url(url) or str(url).strip()
    digest = __import__("hashlib").sha256(canonical_url.encode("utf-8")).hexdigest()
    note_v = note.strip() if isinstance(note, str) and note.strip() else None
    sentiment_v = sentiment.strip().lower() if isinstance(sentiment, str) and sentiment.strip() else None
    tags_v = sorted({str(tag).strip() for tag in (tags or []) if str(tag).strip()})
    now = _now()
    with session_scope() as session:
        target = _resolve_symbol_target(session, symbol=sym)
        evidence = (
            session.query(DiscordEvidenceItem)
            .filter_by(guild_id=int(guild_id), url_hash=digest)
            .first()
        )
        if evidence is None:
            evidence = DiscordEvidenceItem(
                guild_id=int(guild_id),
                url_raw=str(url).strip(),
                url_canonical=canonical_url,
                url_hash=digest,
                title=title_v,
                note=note_v,
                sentiment=sentiment_v,
                tags=tags_v,
                source_type=(source_type or "other").strip() or "other",
                pinned=False,
                resolution_state="resolved",
                candidate_targets_json=None,
                created_at=now,
                updated_at=now,
                created_by_user_id=int(user_id),
                discord_message_id=int(discord_message_id) if discord_message_id else None,
                discord_channel_id=int(discord_channel_id) if discord_channel_id else None,
            )
            session.add(evidence)
            session.flush()
        else:
            if not str(evidence.title or "").strip():
                evidence.title = title_v
            if note_v and not str(evidence.note or "").strip():
                evidence.note = note_v
            if sentiment_v:
                evidence.sentiment = sentiment_v
            if tags_v:
                existing_tags = {str(tag).strip() for tag in (evidence.tags or []) if str(tag).strip()}
                evidence.tags = sorted(existing_tags | set(tags_v))
            if not str(evidence.source_type or "").strip() or str(evidence.source_type or "").strip().lower() == "other":
                evidence.source_type = (source_type or "other").strip() or "other"
            evidence.resolution_state = "resolved"
            evidence.updated_at = now
            if discord_message_id and not evidence.discord_message_id:
                evidence.discord_message_id = int(discord_message_id)
            if discord_channel_id and not evidence.discord_channel_id:
                evidence.discord_channel_id = int(discord_channel_id)
            session.flush()
        target_row = (
            session.query(DiscordEvidenceTarget)
            .filter_by(evidence_id=int(evidence.id), target_key=str(target["target_key"]))
            .first()
        )
        if target_row is None:
            target_row = DiscordEvidenceTarget(
                evidence_id=int(evidence.id),
                target_key=str(target["target_key"]),
                target_kind=str(target["target_kind"]),
                target_label=str(target.get("target_label") or "") or None,
                issuer_id=(int(target["issuer_id"]) if target.get("issuer_id") is not None else None),
                company_id=(int(target["company_id"]) if target.get("company_id") is not None else None),
                symbol=str(target.get("symbol") or "") or None,
                confidence=1.0,
                resolution_method="manual_ticker",
                is_primary=True,
                extra_json=None,
                created_at=now,
                updated_at=now,
            )
            session.add(target_row)
        else:
            target_row.target_label = str(target.get("target_label") or target_row.target_label or "") or None
            if target.get("issuer_id") is not None:
                target_row.issuer_id = int(target["issuer_id"])
            if target.get("company_id") is not None:
                target_row.company_id = int(target["company_id"])
            if target.get("symbol"):
                target_row.symbol = str(target["symbol"])
            target_row.resolution_method = "manual_ticker"
            target_row.is_primary = True
            target_row.updated_at = now
        session.flush()
        return _source_record_from_evidence(session, evidence, target_row)


def _legacy_source_record(src: DiscordTickerSource) -> SourceRecord:
    return SourceRecord(
        id=int(src.id),
        symbol=normalize_symbol(src.symbol),
        title=str(src.title or ""),
        url=str(src.url or ""),
        note=src.note,
        sentiment=src.sentiment,
        tags=list(src.tags or []),
        source_type=str(src.source_type or "other"),
        pinned=bool(src.pinned),
        created_at=src.created_at,
        created_by_user_id=int(src.created_by_user_id or 0),
        discord_message_id=(int(src.discord_message_id) if src.discord_message_id else None),
        discord_channel_id=(int(src.discord_channel_id) if src.discord_channel_id else None),
        evidence_id=int(src.id),
        target_kind="ticker",
        target_key=f"ticker:{normalize_symbol(src.symbol)}",
        target_label=normalize_symbol(src.symbol),
    )


def _list_evidence_records(
    *,
    session,
    guild_id: int,
    symbol: str,
    limit: int,
    pinned_only: bool | None = None,
) -> list[SourceRecord]:
    q = (
        session.query(DiscordEvidenceItem, DiscordEvidenceTarget)
        .join(DiscordEvidenceTarget, DiscordEvidenceTarget.evidence_id == DiscordEvidenceItem.id)
        .filter(DiscordEvidenceItem.guild_id == int(guild_id))
        .filter(DiscordEvidenceTarget.symbol == symbol)
    )
    if pinned_only is not None:
        q = q.filter(DiscordEvidenceItem.pinned.is_(bool(pinned_only)))
    rows = (
        q.order_by(DiscordEvidenceItem.pinned.desc(), DiscordEvidenceItem.created_at.desc(), DiscordEvidenceItem.id.desc())
        .limit(max(0, int(limit)))
        .all()
    )
    return [_source_record_from_evidence(session, evidence, target) for evidence, target in rows]


def update_source_discord_ids(*, source_id: int, message_id: int, channel_id: int) -> None:
    with session_scope() as session:
        evidence = session.query(DiscordEvidenceItem).filter_by(id=int(source_id)).first()
        if evidence is not None:
            evidence.discord_message_id = int(message_id)
            evidence.discord_channel_id = int(channel_id)
            evidence.updated_at = _now()
            return
        src = session.query(DiscordTickerSource).filter_by(id=int(source_id)).first()
        if src is not None:
            src.discord_message_id = int(message_id)
            src.discord_channel_id = int(channel_id)


def set_source_pinned(*, source_id: int, pinned: bool) -> bool:
    with session_scope() as session:
        evidence = session.query(DiscordEvidenceItem).filter_by(id=int(source_id)).first()
        if evidence is not None:
            evidence.pinned = bool(pinned)
            evidence.updated_at = _now()
            return True
        src = session.query(DiscordTickerSource).filter_by(id=int(source_id)).first()
        if not src:
            return False
        src.pinned = bool(pinned)
        return True


def set_source_sentiment(*, source_id: int, sentiment: str | None) -> bool:
    val = (sentiment or "").strip().lower()
    if val in {"", "clear", "none", "null"}:
        val = ""
    allowed = {"bullish", "neutral", "bearish", "mixed"}
    if val and val not in allowed:
        raise ValueError("Sentiment must be bullish|neutral|bearish|mixed|clear.")
    with session_scope() as session:
        evidence = session.query(DiscordEvidenceItem).filter_by(id=int(source_id)).first()
        if evidence is not None:
            evidence.sentiment = (val or None)
            evidence.updated_at = _now()
            return True
        src = session.query(DiscordTickerSource).filter_by(id=int(source_id)).first()
        if not src:
            return False
        src.sentiment = (val or None)
        return True


def list_sources(*, guild_id: int, symbol: str, limit: int = 5) -> list[SourceRecord]:
    sym = normalize_symbol(symbol)
    if not sym:
        raise ValueError("Missing ticker.")
    limit = max(1, min(int(limit or 5), 25))
    SessionLocal = session_factory()
    with SessionLocal() as session:
        rows = _list_evidence_records(session=session, guild_id=int(guild_id), symbol=sym, limit=limit)
        if rows:
            return rows
        legacy = (
            session.query(DiscordTickerSource)
            .filter_by(guild_id=int(guild_id), symbol=sym)
            .order_by(DiscordTickerSource.pinned.desc(), DiscordTickerSource.created_at.desc())
            .limit(limit)
            .all()
        )
        return [_legacy_source_record(row) for row in legacy]


def get_source_by_id(*, source_id: int) -> SourceRecord | None:
    SessionLocal = session_factory()
    with SessionLocal() as session:
        evidence = session.query(DiscordEvidenceItem).filter_by(id=int(source_id)).first()
        if evidence is not None:
            target = (
                session.query(DiscordEvidenceTarget)
                .filter_by(evidence_id=int(evidence.id))
                .order_by(DiscordEvidenceTarget.is_primary.desc(), DiscordEvidenceTarget.id.asc())
                .first()
            )
            return _source_record_from_evidence(session, evidence, target)
        legacy = session.query(DiscordTickerSource).filter_by(id=int(source_id)).first()
        return _legacy_source_record(legacy) if legacy is not None else None


def list_sources_split(
    *,
    guild_id: int,
    symbol: str,
    pinned_limit: int = 3,
    recent_limit: int = 3,
) -> tuple[list[SourceRecord], list[SourceRecord]]:
    sym = normalize_symbol(symbol)
    if not sym:
        raise ValueError("Missing ticker.")
    SessionLocal = session_factory()
    with SessionLocal() as session:
        pinned = _list_evidence_records(
            session=session,
            guild_id=int(guild_id),
            symbol=sym,
            limit=max(0, int(pinned_limit)),
            pinned_only=True,
        )
        recent = _list_evidence_records(
            session=session,
            guild_id=int(guild_id),
            symbol=sym,
            limit=max(0, int(recent_limit)),
            pinned_only=None,
        )
        if pinned or recent:
            return pinned, recent
        legacy_pinned = (
            session.query(DiscordTickerSource)
            .filter_by(guild_id=int(guild_id), symbol=sym, pinned=True)
            .order_by(DiscordTickerSource.created_at.desc())
            .limit(max(0, int(pinned_limit)))
            .all()
        )
        legacy_recent = (
            session.query(DiscordTickerSource)
            .filter_by(guild_id=int(guild_id), symbol=sym)
            .order_by(DiscordTickerSource.created_at.desc())
            .limit(max(0, int(recent_limit)))
            .all()
        )
        return (
            [_legacy_source_record(row) for row in legacy_pinned],
            [_legacy_source_record(row) for row in legacy_recent],
        )


def create_exec_idea(
    *,
    guild_id: int,
    symbol: str,
    horizon: str,
    horizon_n: int = 1,
    thesis: str,
    catalysts: str | None,
    invalidation: str | None,
    levels: str | None,
    confidence: int,
    user_id: int,
    discord_message_id: int | None = None,
    discord_channel_id: int | None = None,
) -> DiscordExecIdea:
    sym = normalize_symbol(symbol)
    if not sym:
        raise ValueError("Missing ticker.")
    horizon_v = (horizon or "").strip().lower()
    if horizon_v not in {"days", "weeks", "months", "years"}:
        raise ValueError("Invalid horizon (days|weeks|months|years).")
    hn = int(horizon_n or 1)
    if hn < 1 or hn > 120:
        raise ValueError("Horizon must be 1–120.")
    conf = int(confidence)
    if conf < 1 or conf > 5:
        raise ValueError("Confidence must be 1–5.")
    now = _now()
    with session_scope() as session:
        idea = DiscordExecIdea(
            guild_id=int(guild_id),
            symbol=sym,
            horizon=horizon_v,
            horizon_n=hn,
            thesis=(thesis or "").strip(),
            catalysts=(catalysts.strip() if catalysts and catalysts.strip() else None),
            invalidation=(invalidation.strip() if invalidation and invalidation.strip() else None),
            levels=(levels.strip() if levels and levels.strip() else None),
            confidence=conf,
            status="active",
            created_at=now,
            updated_at=now,
            closed_at=None,
            created_by_user_id=int(user_id),
            discord_message_id=int(discord_message_id) if discord_message_id else None,
            discord_channel_id=int(discord_channel_id) if discord_channel_id else None,
        )
        session.add(idea)
        session.flush()
        return idea


def update_exec_idea_discord_ids(*, idea_id: int, message_id: int, channel_id: int) -> None:
    with session_scope() as session:
        idea = session.query(DiscordExecIdea).filter_by(id=int(idea_id)).first()
        if not idea:
            return
        idea.discord_message_id = int(message_id)
        idea.discord_channel_id = int(channel_id)


def update_exec_idea(*, idea_id: int, note: str, confidence: int | None = None) -> DiscordExecIdea:
    now = _now()
    with session_scope() as session:
        idea = session.query(DiscordExecIdea).filter_by(id=int(idea_id)).first()
        if not idea:
            raise ValueError("Idea not found.")
        note_v = (note or "").strip()
        if not note_v:
            raise ValueError("Missing update note.")
        idea.thesis = (idea.thesis or "").rstrip() + f"\n\nUpdate ({now.date().isoformat()}): {note_v}"
        if confidence is not None:
            conf = int(confidence)
            if conf < 1 or conf > 5:
                raise ValueError("Confidence must be 1–5.")
            idea.confidence = conf
        idea.updated_at = now
        session.flush()
        return idea


def close_exec_idea(*, idea_id: int, outcome: str, note: str | None = None) -> DiscordExecIdea:
    now = _now()
    outcome_v = (outcome or "").strip().lower()
    if outcome_v not in {"hit", "miss", "mixed"}:
        raise ValueError("Outcome must be hit|miss|mixed.")
    with session_scope() as session:
        idea = session.query(DiscordExecIdea).filter_by(id=int(idea_id)).first()
        if not idea:
            raise ValueError("Idea not found.")
        extra = f"\n\nClosed ({now.date().isoformat()}): outcome={outcome_v}"
        if note and note.strip():
            extra += f" — {note.strip()}"
        idea.thesis = (idea.thesis or "").rstrip() + extra
        idea.status = "closed"
        idea.closed_at = now
        idea.updated_at = now
        session.flush()
        return idea


def latest_active_exec_idea(*, guild_id: int, symbol: str) -> DiscordExecIdea | None:
    sym = normalize_symbol(symbol)
    if not sym:
        raise ValueError("Missing ticker.")
    SessionLocal = session_factory()
    with SessionLocal() as session:
        return (
            session.query(DiscordExecIdea)
            .filter_by(guild_id=int(guild_id), symbol=sym, status="active")
            .order_by(DiscordExecIdea.created_at.desc())
            .first()
        )


def watchlist_status(
    *,
    guild_id: int,
    symbol: str,
) -> tuple[DiscordWatchlistEntry | None, list[DiscordWatchlistEntry], list[DiscordWatchlistEntry]]:
    sym = normalize_symbol(symbol)
    if not sym:
        raise ValueError("Missing ticker.")
    SessionLocal = session_factory()
    with SessionLocal() as session:
        rows = (
            session.query(DiscordWatchlistEntry)
            .filter_by(guild_id=int(guild_id), symbol=sym, removed_at=None)
            .order_by(
                DiscordWatchlistEntry.list_kind.asc(),
                DiscordWatchlistEntry.sector_key.asc(),
                DiscordWatchlistEntry.subgroup_key.asc(),
            )
            .all()
        )
        exec_row = next((r for r in rows if r.list_kind == "exec"), None)
        community_rows = [r for r in rows if r.list_kind == "community"]
        masterlist_rows = [r for r in rows if r.list_kind == "masterlist"]
        return exec_row, community_rows, masterlist_rows


def suggest_exec_watchlist(*, guild_id: int, symbol: str, note: str | None, user_id: int) -> DiscordQueueItem:
    sym = normalize_symbol(symbol)
    if not sym:
        raise ValueError("Missing ticker.")
    payload = {"ticker": sym, "note": (note.strip() if note and note.strip() else None)}
    now = _now()
    with session_scope() as session:
        item = DiscordQueueItem(
            guild_id=int(guild_id),
            kind="exec_watchlist_add",
            status="pending",
            payload=payload,
            created_at=now,
            created_by_user_id=int(user_id),
            decided_at=None,
            decided_by_user_id=None,
            decision_note=None,
        )
        session.add(item)
        session.flush()
        return item


def suggest_source(
    *,
    guild_id: int,
    symbol: str,
    url: str,
    title: str | None,
    note: str | None,
    source_type: str | None,
    user_id: int,
    tags: list[str] | None = None,
) -> DiscordQueueItem:
    sym = normalize_symbol(symbol)
    if not sym:
        raise ValueError("Missing ticker.")
    if not url or not str(url).strip():
        raise ValueError("Missing URL.")
    canonical_url = _canonicalize_source_url(url) or str(url).strip()
    payload = {
        "ticker": sym,
        "url": str(url).strip(),
        "url_canonical": canonical_url,
        "title": (title.strip() if title and title.strip() else None),
        "note": (note.strip() if note and note.strip() else None),
        "type": (source_type.strip() if source_type and source_type.strip() else None),
        "tags": sorted({str(tag).strip() for tag in (tags or []) if str(tag).strip()}),
    }
    now = _now()
    with session_scope() as session:
        item = DiscordQueueItem(
            guild_id=int(guild_id),
            kind="source_add",
            status="pending",
            payload=payload,
            created_at=now,
            created_by_user_id=int(user_id),
            decided_at=None,
            decided_by_user_id=None,
            decision_note=None,
        )
        session.add(item)
        session.flush()
        return item


def submit_evidence(
    *,
    guild_id: int,
    symbol: str,
    url: str,
    title: str | None,
    note: str | None,
    source_type: str | None,
    tags: list[str] | None,
    user_id: int,
) -> EvidenceSubmissionResult:
    """
    Public evidence intake entry point.

    Submissions are intentionally review-first: the trusted public workflow is
    explicit user intent plus moderator approval, not automatic chat scraping.
    Already approved evidence is returned as a duplicate instead of creating a
    new review item.
    """
    sym = normalize_symbol(symbol)
    if not sym:
        raise ValueError("Missing ticker.")
    if not url or not str(url).strip():
        raise ValueError("Missing URL.")
    canonical_url = _canonicalize_source_url(url) or str(url).strip()
    digest = __import__("hashlib").sha256(canonical_url.encode("utf-8")).hexdigest()
    tags_v = sorted({str(tag).strip() for tag in (tags or []) if str(tag).strip()})
    title_v = (title.strip() if isinstance(title, str) and title.strip() else None)
    note_v = note.strip() if isinstance(note, str) and note.strip() else None
    type_v = source_type.strip() if isinstance(source_type, str) and source_type.strip() else "other"

    with session_scope() as session:
        existing = (
            session.query(DiscordEvidenceItem)
            .filter_by(guild_id=int(guild_id), url_hash=digest)
            .first()
        )
        if existing is not None:
            state = str(existing.resolution_state or "").strip().lower()
            if state in {"resolved", "approved", "public"}:
                return EvidenceSubmissionResult(
                    status="already_public",
                    queue_id=None,
                    evidence_id=int(existing.id),
                    created=False,
                    url_canonical=canonical_url,
                    target=sym,
                    message="That source is already in the public research memory.",
                )
            return EvidenceSubmissionResult(
                status="already_pending",
                queue_id=None,
                evidence_id=int(existing.id),
                created=False,
                url_canonical=canonical_url,
                target=sym,
                message="That source is already pending review.",
            )

        pending = (
            session.query(DiscordQueueItem)
            .filter_by(guild_id=int(guild_id), status="pending", kind="source_add")
            .order_by(DiscordQueueItem.created_at.asc())
            .limit(200)
            .all()
        )
        for item in pending:
            payload = item.payload if isinstance(item.payload, dict) else {}
            queued_url = str(payload.get("url_canonical") or payload.get("url") or "").strip()
            queued_canonical = _canonicalize_source_url(queued_url) or queued_url
            if queued_canonical == canonical_url:
                return EvidenceSubmissionResult(
                    status="already_queued",
                    queue_id=int(item.id),
                    evidence_id=None,
                    created=False,
                    url_canonical=canonical_url,
                    target=str(payload.get("ticker") or sym),
                    message="That source is already queued for review.",
                )

        now = _now()
        item = DiscordQueueItem(
            guild_id=int(guild_id),
            kind="source_add",
            status="pending",
            payload={
                "ticker": sym,
                "url": str(url).strip(),
                "url_canonical": canonical_url,
                "title": title_v,
                "note": note_v,
                "type": type_v,
                "tags": tags_v,
                "submitted_via": "submit_evidence",
            },
            created_at=now,
            created_by_user_id=int(user_id),
            decided_at=None,
            decided_by_user_id=None,
            decision_note=None,
        )
        session.add(item)
        session.flush()
        return EvidenceSubmissionResult(
            status="queued",
            queue_id=int(item.id),
            evidence_id=None,
            created=True,
            url_canonical=canonical_url,
            target=sym,
            message="Evidence submitted for review.",
        )


def list_queue(*, guild_id: int, status: str = "pending", limit: int = 20) -> list[DiscordQueueItem]:
    status_v = (status or "pending").strip().lower()
    limit = max(1, min(int(limit or 20), 50))
    SessionLocal = session_factory()
    with SessionLocal() as session:
        return (
            session.query(DiscordQueueItem)
            .filter_by(guild_id=int(guild_id), status=status_v)
            .order_by(DiscordQueueItem.created_at.asc())
            .limit(limit)
            .all()
        )


def decide_queue_item(
    *,
    guild_id: int,
    queue_id: int,
    decision: str,
    decided_by_user_id: int,
    decision_note: str | None = None,
) -> DiscordQueueItem:
    now = _now()
    decision_v = (decision or "").strip().lower()
    if decision_v not in {"approved", "rejected"}:
        raise ValueError("decision must be approved|rejected")
    with session_scope() as session:
        item = session.query(DiscordQueueItem).filter_by(guild_id=int(guild_id), id=int(queue_id)).first()
        if not item:
            raise ValueError("Queue item not found.")
        if item.status != "pending":
            return item
        item.status = decision_v
        item.decided_at = now
        item.decided_by_user_id = int(decided_by_user_id)
        item.decision_note = (decision_note.strip() if decision_note and decision_note.strip() else None)
        session.flush()
        return item


def tag_queue_evidence_item(
    *,
    guild_id: int,
    queue_id: int,
    tags: list[str] | None,
    tagged_by_user_id: int,
) -> DiscordQueueItem:
    tags_v = sorted({str(tag).strip() for tag in (tags or []) if str(tag).strip()})
    if not tags_v:
        raise ValueError("At least one tag is required.")
    with session_scope() as session:
        item = session.query(DiscordQueueItem).filter_by(guild_id=int(guild_id), id=int(queue_id)).first()
        if not item:
            raise ValueError("Queue item not found.")
        if str(item.kind or "") != "source_add":
            raise ValueError("Only evidence submissions can be tagged.")
        if item.status != "pending":
            raise ValueError("Only pending evidence submissions can be tagged.")
        payload = dict(item.payload or {})
        payload["tags"] = tags_v
        payload["tagged_by_user_id"] = int(tagged_by_user_id)
        payload["tagged_at"] = _now().isoformat()
        item.payload = payload
        session.flush()
        return item


def get_queue_item(*, guild_id: int, queue_id: int) -> DiscordQueueItem | None:
    SessionLocal = session_factory()
    with SessionLocal() as session:
        return session.query(DiscordQueueItem).filter_by(guild_id=int(guild_id), id=int(queue_id)).first()


def capture_due_baselines(*, guild_id: int) -> int:
    now = _now()
    SessionLocal = session_factory()
    with SessionLocal() as session:
        due = (
            session.query(DiscordWatchlistEntry)
            .filter_by(guild_id=int(guild_id), removed_at=None)
            .filter(DiscordWatchlistEntry.baseline_price.is_(None))
            .filter(DiscordWatchlistEntry.baseline_due_at <= now)
            .order_by(DiscordWatchlistEntry.baseline_due_at.asc())
            .limit(15)
            .all()
        )

    if not due:
        return 0

    provider_symbols = sorted({e.provider_symbol for e in due})
    try:
        quotes = fetch_quotes(
            provider_symbols,
            user_agent=settings.discord_market_user_agent,
            allow_chart_fallback=(not _discord_yahoo_bulk_only()),
        )
    except YahooFinanceError:
        return 0

    resolved_quotes: dict[int, Any] = {}
    resolved_provider_symbols: dict[int, str] = {}
    missing_due = [e for e in due if e.provider_symbol not in quotes]
    if missing_due:
        candidates: set[str] = set()
        for e in missing_due:
            candidates.add(e.symbol)
            if "." in e.symbol:
                candidates.add(e.symbol.replace(".", "-"))
        try:
            alt_quotes = fetch_quotes(
                sorted(candidates),
                user_agent=settings.discord_market_user_agent,
                allow_chart_fallback=(not _discord_yahoo_bulk_only()),
            )
        except YahooFinanceError:
            alt_quotes = {}
        for e in missing_due:
            q = alt_quotes.get(e.symbol) or alt_quotes.get(e.symbol.replace(".", "-"))
            if q:
                resolved_quotes[e.id] = q
                resolved_provider_symbols[e.id] = q.symbol

    captured = 0
    with session_scope() as session:
        for entry in due:
            quote = resolved_quotes.get(entry.id) or quotes.get(entry.provider_symbol)
            if not quote:
                continue
            row = session.query(DiscordWatchlistEntry).filter_by(id=int(entry.id)).first()
            if not row:
                continue
            if row.removed_at is not None or row.baseline_price is not None:
                continue
            if row.baseline_due_at and row.baseline_due_at > now:
                continue
            if entry.id in resolved_provider_symbols:
                row.provider_symbol = resolved_provider_symbols[entry.id]
            row.baseline_price = quote.price
            row.baseline_currency = quote.currency
            row.baseline_quote_at = quote.market_time_utc
            row.baseline_captured_at = now
            row.last_quote_price = quote.price
            row.last_quote_currency = quote.currency
            row.last_quote_at = quote.market_time_utc
            row.company_name = quote.name
            row.exchange = quote.exchange
            row.full_exchange_name = quote.full_exchange_name
            lr = row.last_returns if isinstance(row.last_returns, dict) else {}
            lr = dict(lr)
            lr["since_24h"] = 0.0
            row.last_returns = lr
            captured += 1

    return captured
