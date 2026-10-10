from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from threading import RLock
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from sqlalchemy import Engine, inspect, text as sql_text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from thoth_terminal.config import settings
from thoth_terminal.db import database_backend, get_engine
from thoth_terminal.models import Base
from thoth_terminal.seed import seed_all


_INIT_DB_LOCK = RLock()

_TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "dclid",
    "mc_cid",
    "mc_eid",
    "mkt_tok",
    "igshid",
    "si",
    "spm",
}


def _slugify(value: str | None, *, default: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return cleaned or default


def _canonicalize_url(raw: str | None) -> str | None:
    value = str(raw or "").strip()
    if not value:
        return None
    if value.startswith("www."):
        value = "https://" + value
    parsed = urlparse(value)
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        return None
    netloc = (parsed.netloc or "").strip().lower()
    if not netloc:
        return None
    if netloc.startswith("www."):
        netloc = netloc[4:]
    if netloc.endswith(":80") and scheme == "http":
        netloc = netloc[:-3]
    if netloc.endswith(":443") and scheme == "https":
        netloc = netloc[:-4]
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    kept: list[tuple[str, str]] = []
    for key, val in parse_qsl(parsed.query or "", keep_blank_values=True):
        lk = key.strip().lower()
        if lk.startswith("utm_") or lk in _TRACKING_QUERY_KEYS:
            continue
        kept.append((key, val))
    query = urlencode(sorted(kept), doseq=True)
    return urlunparse((scheme, netloc, path or "/", "", query, ""))


def _hash_url(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _ensure_discord_schema(engine: Engine) -> None:
    with engine.begin() as conn:
        def _table_exists(name: str) -> bool:
            return bool(
                conn.execute(
                    sql_text(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=:name LIMIT 1"
                    ),
                    {"name": name},
                ).scalar()
            )

        def _cols(table: str) -> set[str]:
            return {
                row[1]
                for row in conn.execute(sql_text(f"PRAGMA table_info({table})")).fetchall()
            }

        def _add_column(table: str, *, name: str, ddl: str) -> None:
            cols = _cols(table)
            if name in cols:
                return
            conn.execute(sql_text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))

        def _has_unique_index(table: str, cols: list[str]) -> bool:
            try:
                idxs = conn.execute(sql_text(f"PRAGMA index_list({table})")).fetchall()
            except Exception:
                return False
            for row in idxs:
                # PRAGMA index_list: seq, name, unique, origin, partial
                if len(row) < 3:
                    continue
                name = row[1]
                unique = int(row[2] or 0)
                if not unique or not name:
                    continue
                info = conn.execute(sql_text(f"PRAGMA index_info({name})")).fetchall()
                idx_cols = [r[2] for r in info if len(r) >= 3 and r[2]]
                if idx_cols == cols:
                    return True
            return False

        def _rebuild_watchlist_entries() -> None:
            conn.execute(sql_text("DROP TABLE IF EXISTS discord_watchlist_entries_new"))
            conn.execute(
                sql_text(
                    """
                    CREATE TABLE discord_watchlist_entries_new (
                        id INTEGER PRIMARY KEY,
                        guild_id INTEGER NOT NULL,
                        list_kind TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        provider_symbol TEXT NOT NULL,
                        sector TEXT NOT NULL,
                        sector_key TEXT NOT NULL,
                        subgroup TEXT NOT NULL,
                        subgroup_key TEXT NOT NULL,
                        added_at DATETIME NOT NULL,
                        added_by_user_id INTEGER NOT NULL,
                        removed_at DATETIME,
                        removed_by_user_id INTEGER,
                        baseline_due_at DATETIME NOT NULL,
                        baseline_price REAL,
                        baseline_currency TEXT,
                        baseline_quote_at DATETIME,
                        baseline_captured_at DATETIME,
                        last_quote_price REAL,
                        last_quote_currency TEXT,
                        last_quote_at DATETIME,
                        company_name TEXT,
                        exchange TEXT,
                        full_exchange_name TEXT,
                        last_returns JSON NOT NULL DEFAULT '{}',
                        last_returns_at DATETIME,
                        UNIQUE(guild_id, list_kind, sector_key, subgroup_key, symbol)
                    );
                    """
                )
            )

            conn.execute(
                sql_text(
                    """
                    INSERT INTO discord_watchlist_entries_new (
                        id, guild_id, list_kind, symbol, provider_symbol,
                        sector, sector_key, subgroup, subgroup_key,
                        added_at, added_by_user_id, removed_at, removed_by_user_id,
                        baseline_due_at, baseline_price, baseline_currency, baseline_quote_at, baseline_captured_at,
                        last_quote_price, last_quote_currency, last_quote_at,
                        company_name, exchange, full_exchange_name,
                        last_returns, last_returns_at
                    )
                    SELECT
                        id,
                        guild_id,
                        COALESCE(NULLIF(TRIM(list_kind), ''), 'community') AS list_kind,
                        symbol,
                        COALESCE(NULLIF(TRIM(provider_symbol), ''), symbol) AS provider_symbol,
                        CASE
                          WHEN COALESCE(NULLIF(TRIM(list_kind), ''), 'community') = 'exec' THEN 'Executive'
                          ELSE COALESCE(NULLIF(TRIM(sector), ''), 'Uncategorized')
                        END AS sector,
                        CASE
                          WHEN COALESCE(NULLIF(TRIM(list_kind), ''), 'community') = 'exec' THEN 'exec'
                          ELSE COALESCE(NULLIF(TRIM(sector_key), ''), LOWER(COALESCE(NULLIF(TRIM(sector), ''), 'Uncategorized')))
                        END AS sector_key,
                        CASE
                          WHEN COALESCE(NULLIF(TRIM(list_kind), ''), 'community') = 'exec' THEN 'Executive'
                          ELSE COALESCE(NULLIF(TRIM(subgroup), ''), 'General')
                        END AS subgroup,
                        CASE
                          WHEN COALESCE(NULLIF(TRIM(list_kind), ''), 'community') = 'exec' THEN 'exec'
                          ELSE COALESCE(NULLIF(TRIM(subgroup_key), ''), LOWER(COALESCE(NULLIF(TRIM(subgroup), ''), 'General')))
                        END AS subgroup_key,
                        added_at,
                        added_by_user_id,
                        removed_at,
                        removed_by_user_id,
                        COALESCE(baseline_due_at, added_at) AS baseline_due_at,
                        baseline_price,
                        baseline_currency,
                        baseline_quote_at,
                        baseline_captured_at,
                        last_quote_price,
                        last_quote_currency,
                        last_quote_at,
                        company_name,
                        exchange,
                        full_exchange_name,
                        COALESCE(last_returns, '{}') AS last_returns,
                        last_returns_at
                    FROM discord_watchlist_entries;
                    """
                )
            )

            conn.execute(sql_text("DROP TABLE discord_watchlist_entries"))
            conn.execute(sql_text("ALTER TABLE discord_watchlist_entries_new RENAME TO discord_watchlist_entries"))
            conn.execute(
                sql_text(
                    "CREATE INDEX IF NOT EXISTS idx_discord_watchlist_active ON discord_watchlist_entries(guild_id, list_kind, removed_at)"
                )
            )
            conn.execute(
                sql_text(
                    "CREATE INDEX IF NOT EXISTS idx_discord_watchlist_sector ON discord_watchlist_entries(guild_id, list_kind, sector_key)"
                )
            )
            conn.execute(
                sql_text(
                    "CREATE INDEX IF NOT EXISTS idx_discord_watchlist_subgroup ON discord_watchlist_entries(guild_id, list_kind, sector_key, subgroup_key)"
                )
            )
            conn.execute(
                sql_text(
                    "CREATE INDEX IF NOT EXISTS idx_discord_watchlist_baseline_due ON discord_watchlist_entries(guild_id, baseline_due_at)"
                )
            )

        def _rebuild_watchlist_sectors() -> None:
            conn.execute(sql_text("DROP TABLE IF EXISTS discord_watchlist_sectors_new"))
            conn.execute(
                sql_text(
                    """
                    CREATE TABLE discord_watchlist_sectors_new (
                        id INTEGER PRIMARY KEY,
                        guild_id INTEGER NOT NULL,
                        list_kind TEXT NOT NULL DEFAULT 'community',
                        sector TEXT NOT NULL,
                        sector_key TEXT NOT NULL,
                        created_at DATETIME NOT NULL,
                        created_by_user_id INTEGER NOT NULL,
                        updated_at DATETIME NOT NULL,
                        updated_by_user_id INTEGER NOT NULL,
                        archived_at DATETIME,
                        archived_by_user_id INTEGER,
                        UNIQUE(guild_id, list_kind, sector_key)
                    );
                    """
                )
            )
            cols = _cols("discord_watchlist_sectors")
            has_list_kind = "list_kind" in cols
            conn.execute(
                sql_text(
                    f"""
                    INSERT INTO discord_watchlist_sectors_new (
                        id, guild_id, list_kind, sector, sector_key,
                        created_at, created_by_user_id, updated_at, updated_by_user_id,
                        archived_at, archived_by_user_id
                    )
                    SELECT
                        id,
                        guild_id,
                        {"COALESCE(NULLIF(TRIM(list_kind), ''), 'community')" if has_list_kind else "'community'"} AS list_kind,
                        COALESCE(NULLIF(TRIM(sector), ''), 'Uncategorized') AS sector,
                        COALESCE(NULLIF(TRIM(sector_key), ''), LOWER(COALESCE(NULLIF(TRIM(sector), ''), 'Uncategorized'))) AS sector_key,
                        created_at,
                        created_by_user_id,
                        updated_at,
                        updated_by_user_id,
                        archived_at,
                        archived_by_user_id
                    FROM discord_watchlist_sectors;
                    """
                )
            )
            conn.execute(sql_text("DROP TABLE discord_watchlist_sectors"))
            conn.execute(sql_text("ALTER TABLE discord_watchlist_sectors_new RENAME TO discord_watchlist_sectors"))
            conn.execute(
                sql_text(
                    "CREATE INDEX IF NOT EXISTS idx_discord_watchlist_sectors_active ON discord_watchlist_sectors(guild_id, list_kind, archived_at)"
                )
            )
            conn.execute(
                sql_text(
                    "CREATE INDEX IF NOT EXISTS idx_discord_watchlist_sectors_key ON discord_watchlist_sectors(guild_id, list_kind, sector_key)"
                )
            )

        def _rebuild_watchlist_subgroups() -> None:
            conn.execute(sql_text("DROP TABLE IF EXISTS discord_watchlist_subgroups_new"))
            conn.execute(
                sql_text(
                    """
                    CREATE TABLE discord_watchlist_subgroups_new (
                        id INTEGER PRIMARY KEY,
                        guild_id INTEGER NOT NULL,
                        list_kind TEXT NOT NULL DEFAULT 'community',
                        sector_key TEXT NOT NULL,
                        subgroup TEXT NOT NULL,
                        subgroup_key TEXT NOT NULL,
                        created_at DATETIME NOT NULL,
                        created_by_user_id INTEGER NOT NULL,
                        updated_at DATETIME NOT NULL,
                        updated_by_user_id INTEGER NOT NULL,
                        archived_at DATETIME,
                        archived_by_user_id INTEGER,
                        UNIQUE(guild_id, list_kind, sector_key, subgroup_key)
                    );
                    """
                )
            )
            cols = _cols("discord_watchlist_subgroups")
            has_list_kind = "list_kind" in cols
            conn.execute(
                sql_text(
                    f"""
                    INSERT INTO discord_watchlist_subgroups_new (
                        id, guild_id, list_kind, sector_key, subgroup, subgroup_key,
                        created_at, created_by_user_id, updated_at, updated_by_user_id,
                        archived_at, archived_by_user_id
                    )
                    SELECT
                        id,
                        guild_id,
                        {"COALESCE(NULLIF(TRIM(list_kind), ''), 'community')" if has_list_kind else "'community'"} AS list_kind,
                        COALESCE(NULLIF(TRIM(sector_key), ''), 'uncategorized') AS sector_key,
                        COALESCE(NULLIF(TRIM(subgroup), ''), 'General') AS subgroup,
                        COALESCE(NULLIF(TRIM(subgroup_key), ''), LOWER(COALESCE(NULLIF(TRIM(subgroup), ''), 'General'))) AS subgroup_key,
                        created_at,
                        created_by_user_id,
                        updated_at,
                        updated_by_user_id,
                        archived_at,
                        archived_by_user_id
                    FROM discord_watchlist_subgroups;
                    """
                )
            )
            conn.execute(sql_text("DROP TABLE discord_watchlist_subgroups"))
            conn.execute(sql_text("ALTER TABLE discord_watchlist_subgroups_new RENAME TO discord_watchlist_subgroups"))
            conn.execute(
                sql_text(
                    "CREATE INDEX IF NOT EXISTS idx_discord_watchlist_subgroups_active ON discord_watchlist_subgroups(guild_id, list_kind, sector_key, archived_at)"
                )
            )
            conn.execute(
                sql_text(
                    "CREATE INDEX IF NOT EXISTS idx_discord_watchlist_subgroups_key ON discord_watchlist_subgroups(guild_id, list_kind, sector_key, subgroup_key)"
                )
            )

        if _table_exists("discord_watchlist_entries"):
            _add_column("discord_watchlist_entries", name="exchange", ddl="exchange TEXT")
            _add_column(
                "discord_watchlist_entries",
                name="full_exchange_name",
                ddl="full_exchange_name TEXT",
            )
            _add_column(
                "discord_watchlist_entries",
                name="company_name",
                ddl="company_name TEXT",
            )
            _add_column("discord_watchlist_entries", name="subgroup", ddl="subgroup TEXT")
            _add_column("discord_watchlist_entries", name="subgroup_key", ddl="subgroup_key TEXT")

            cols = _cols("discord_watchlist_entries")
            if "sector" in cols and "sector_key" in cols:
                # Backfill missing/blank keys for existing rows.
                conn.execute(
                    sql_text(
                        """
                        UPDATE discord_watchlist_entries
                        SET
                          sector = CASE WHEN list_kind = 'exec' THEN 'Executive' ELSE COALESCE(NULLIF(TRIM(sector), ''), 'Uncategorized') END,
                          sector_key = CASE WHEN list_kind = 'exec' THEN 'exec' ELSE COALESCE(NULLIF(TRIM(sector_key), ''), LOWER(COALESCE(NULLIF(TRIM(sector), ''), 'Uncategorized'))) END
                        WHERE sector IS NULL OR TRIM(sector) = '' OR sector_key IS NULL OR TRIM(sector_key) = '';
                        """
                    )
                )
            if "subgroup" in cols and "subgroup_key" in cols:
                conn.execute(
                    sql_text(
                        """
                        UPDATE discord_watchlist_entries
                        SET
                          subgroup = CASE WHEN list_kind = 'exec' THEN 'Executive' ELSE COALESCE(NULLIF(TRIM(subgroup), ''), 'General') END,
                          subgroup_key = CASE WHEN list_kind = 'exec' THEN 'exec' ELSE COALESCE(NULLIF(TRIM(subgroup_key), ''), LOWER(COALESCE(NULLIF(TRIM(subgroup), ''), 'General'))) END
                        WHERE subgroup IS NULL OR TRIM(subgroup) = '' OR subgroup_key IS NULL OR TRIM(subgroup_key) = '';
                        """
                    )
                )

            # Rebuild to update unique constraints (allow multi-membership via sector/subgroup) and NOT NULLs.
            if not _has_unique_index(
                "discord_watchlist_entries",
                ["guild_id", "list_kind", "sector_key", "subgroup_key", "symbol"],
            ):
                _rebuild_watchlist_entries()

        if _table_exists("discord_watchlist_sectors"):
            # Ensure list_kind exists and uniqueness includes it (community vs masterlist).
            if not _has_unique_index(
                "discord_watchlist_sectors",
                ["guild_id", "list_kind", "sector_key"],
            ):
                _rebuild_watchlist_sectors()

        if _table_exists("discord_watchlist_subgroups"):
            if not _has_unique_index(
                "discord_watchlist_subgroups",
                ["guild_id", "list_kind", "sector_key", "subgroup_key"],
            ):
                _rebuild_watchlist_subgroups()

        if _table_exists("discord_ticker_sources"):
            _add_column("discord_ticker_sources", name="sentiment", ddl="sentiment TEXT")

        if _table_exists("discord_exec_ideas"):
            _add_column(
                "discord_exec_ideas",
                name="horizon_n",
                ddl="horizon_n INTEGER NOT NULL DEFAULT 1",
            )

        if _table_exists("discord_guild_settings"):
            _add_column("discord_guild_settings", name="desk_channel_id", ddl="desk_channel_id INTEGER")
            _add_column("discord_guild_settings", name="desk_thread_id", ddl="desk_thread_id INTEGER")
            _add_column("discord_guild_settings", name="desk_thread_name", ddl="desk_thread_name TEXT")
            _add_column(
                "discord_guild_settings",
                name="desk_enabled",
                ddl="desk_enabled INTEGER NOT NULL DEFAULT 1",
            )
            _add_column(
                "discord_guild_settings",
                name="outbox_paused",
                ddl="outbox_paused INTEGER NOT NULL DEFAULT 0",
            )
            _add_column(
                "discord_guild_settings",
                name="report_permission_level",
                ddl="report_permission_level TEXT NOT NULL DEFAULT 'read_only'",
            )
            _add_column(
                "discord_guild_settings",
                name="agent_settings_json",
                ddl="agent_settings_json JSON NOT NULL DEFAULT '{}'",
            )
            conn.execute(
                sql_text(
                    """
                    UPDATE discord_guild_settings
                    SET report_permission_level = COALESCE(NULLIF(TRIM(report_permission_level), ''), 'read_only')
                    WHERE report_permission_level IS NULL OR TRIM(report_permission_level) = '';
                    """
                )
            )
            guild_id = int(getattr(settings, "discord_guild_id", 0) or 0)
            if guild_id:
                updated_by = int(getattr(settings, "discord_exec_user_id", 0) or 0)
                exists = conn.execute(
                    sql_text("SELECT 1 FROM discord_guild_settings WHERE guild_id = :guild_id LIMIT 1"),
                    {"guild_id": guild_id},
                ).fetchone()
                if exists:
                    conn.execute(
                        sql_text(
                            """
                            UPDATE discord_guild_settings
                            SET report_permission_level = 'elevated',
                                updated_at = COALESCE(updated_at, CURRENT_TIMESTAMP),
                                updated_by_user_id = CASE
                                    WHEN COALESCE(updated_by_user_id, 0) = 0 THEN :updated_by
                                    ELSE updated_by_user_id
                                END
                            WHERE guild_id = :guild_id
                            """
                        ),
                        {"guild_id": guild_id, "updated_by": updated_by},
                    )
                else:
                    conn.execute(
                        sql_text(
                            """
                            INSERT INTO discord_guild_settings(
                                guild_id,
                                memo_url,
                                desk_channel_id,
                                desk_thread_id,
                                desk_thread_name,
                                desk_enabled,
                                outbox_paused,
                                report_permission_level,
                                agent_settings_json,
                                updated_at,
                                updated_by_user_id
                            )
                            VALUES(
                                :guild_id,
                                NULL,
                                NULL,
                                NULL,
                                NULL,
                                1,
                                0,
                                'elevated',
                                '{}',
                                CURRENT_TIMESTAMP,
                                :updated_by
                            )
                            """
                        ),
                        {"guild_id": guild_id, "updated_by": updated_by},
                    )

        if not _table_exists("discord_link_scan_states"):
            conn.execute(
                sql_text(
                    """
                    CREATE TABLE discord_link_scan_states (
                        guild_id INTEGER NOT NULL,
                        channel_id INTEGER NOT NULL,
                        last_message_id INTEGER,
                        last_scanned_at DATETIME,
                        last_error TEXT,
                        updated_at DATETIME NOT NULL,
                        PRIMARY KEY (guild_id, channel_id)
                    );
                    """
                )
            )
        else:
            _add_column("discord_link_scan_states", name="last_message_id", ddl="last_message_id INTEGER")
            _add_column("discord_link_scan_states", name="last_scanned_at", ddl="last_scanned_at DATETIME")
            _add_column("discord_link_scan_states", name="last_error", ddl="last_error TEXT")
            _add_column("discord_link_scan_states", name="updated_at", ddl="updated_at DATETIME")
            conn.execute(
                sql_text(
                    """
                    UPDATE discord_link_scan_states
                    SET updated_at = COALESCE(updated_at, last_scanned_at, CURRENT_TIMESTAMP)
                    WHERE updated_at IS NULL;
                    """
                )
            )
        conn.execute(
            sql_text(
                "CREATE INDEX IF NOT EXISTS idx_discord_link_scan_updated ON discord_link_scan_states(guild_id, updated_at)"
            )
        )

        if not _table_exists("discord_link_inbox_items"):
            conn.execute(
                sql_text(
                    """
                    CREATE TABLE discord_link_inbox_items (
                        id INTEGER PRIMARY KEY,
                        guild_id INTEGER NOT NULL,
                        channel_id INTEGER NOT NULL,
                        message_id INTEGER NOT NULL,
                        author_user_id INTEGER,
                        message_created_at DATETIME,
                        url_raw TEXT NOT NULL,
                        url_canonical TEXT NOT NULL,
                        url_hash TEXT NOT NULL,
                        message_excerpt TEXT,
                        embed_title TEXT,
                        deterministic_ticker TEXT,
                        status TEXT NOT NULL DEFAULT 'collected',
                        agentic_decision_json JSON,
                        evidence_id INTEGER,
                        resolution_state TEXT,
                        resolution_candidates_json JSON,
                        resolution_request_id INTEGER,
                        source_id INTEGER,
                        queue_id INTEGER,
                        seen_count INTEGER NOT NULL DEFAULT 1,
                        created_at DATETIME NOT NULL,
                        updated_at DATETIME NOT NULL
                    );
                    """
                )
            )
        else:
            _add_column("discord_link_inbox_items", name="author_user_id", ddl="author_user_id INTEGER")
            _add_column("discord_link_inbox_items", name="message_created_at", ddl="message_created_at DATETIME")
            _add_column("discord_link_inbox_items", name="url_raw", ddl="url_raw TEXT")
            _add_column("discord_link_inbox_items", name="url_canonical", ddl="url_canonical TEXT")
            _add_column("discord_link_inbox_items", name="url_hash", ddl="url_hash TEXT")
            _add_column("discord_link_inbox_items", name="message_excerpt", ddl="message_excerpt TEXT")
            _add_column("discord_link_inbox_items", name="embed_title", ddl="embed_title TEXT")
            _add_column("discord_link_inbox_items", name="deterministic_ticker", ddl="deterministic_ticker TEXT")
            _add_column("discord_link_inbox_items", name="status", ddl="status TEXT")
            _add_column("discord_link_inbox_items", name="agentic_decision_json", ddl="agentic_decision_json JSON")
            _add_column("discord_link_inbox_items", name="evidence_id", ddl="evidence_id INTEGER")
            _add_column("discord_link_inbox_items", name="resolution_state", ddl="resolution_state TEXT")
            _add_column("discord_link_inbox_items", name="resolution_candidates_json", ddl="resolution_candidates_json JSON")
            _add_column("discord_link_inbox_items", name="resolution_request_id", ddl="resolution_request_id INTEGER")
            _add_column("discord_link_inbox_items", name="source_id", ddl="source_id INTEGER")
            _add_column("discord_link_inbox_items", name="queue_id", ddl="queue_id INTEGER")
            _add_column("discord_link_inbox_items", name="seen_count", ddl="seen_count INTEGER")
            _add_column("discord_link_inbox_items", name="created_at", ddl="created_at DATETIME")
            _add_column("discord_link_inbox_items", name="updated_at", ddl="updated_at DATETIME")
            conn.execute(
                sql_text(
                    """
                    UPDATE discord_link_inbox_items
                    SET
                      status = COALESCE(NULLIF(TRIM(status), ''), 'collected'),
                      resolution_state = COALESCE(NULLIF(TRIM(resolution_state), ''), CASE WHEN COALESCE(evidence_id, source_id) IS NOT NULL OR NULLIF(TRIM(deterministic_ticker), '') IS NOT NULL THEN 'resolved' ELSE 'pending' END),
                      seen_count = COALESCE(seen_count, 1),
                      created_at = COALESCE(created_at, CURRENT_TIMESTAMP),
                      updated_at = COALESCE(updated_at, created_at, CURRENT_TIMESTAMP)
                    WHERE status IS NULL
                       OR TRIM(status) = ''
                       OR resolution_state IS NULL
                       OR TRIM(resolution_state) = ''
                       OR seen_count IS NULL
                       OR created_at IS NULL
                       OR updated_at IS NULL;
                    """
                )
            )
        conn.execute(
            sql_text(
                """
                DELETE FROM discord_link_inbox_items
                WHERE id NOT IN (
                    SELECT MIN(id)
                    FROM discord_link_inbox_items
                    GROUP BY guild_id, url_hash
                );
                """
            )
        )
        conn.execute(
            sql_text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_discord_link_inbox_guild_url_hash ON discord_link_inbox_items(guild_id, url_hash)"
            )
        )
        conn.execute(
            sql_text(
                "CREATE INDEX IF NOT EXISTS idx_discord_link_inbox_status ON discord_link_inbox_items(guild_id, status, updated_at)"
            )
        )
        conn.execute(
            sql_text(
                "CREATE INDEX IF NOT EXISTS idx_discord_link_inbox_message ON discord_link_inbox_items(guild_id, channel_id, message_id)"
            )
        )

        conn.execute(
            sql_text(
                """
                CREATE TABLE IF NOT EXISTS discord_evidence_items (
                    id INTEGER PRIMARY KEY,
                    guild_id INTEGER NOT NULL,
                    url_raw TEXT NOT NULL,
                    url_canonical TEXT NOT NULL,
                    url_hash TEXT NOT NULL,
                    title TEXT NOT NULL,
                    note TEXT,
                    sentiment TEXT,
                    tags JSON NOT NULL DEFAULT '[]',
                    source_type TEXT NOT NULL DEFAULT 'other',
                    pinned INTEGER NOT NULL DEFAULT 0,
                    resolution_state TEXT NOT NULL DEFAULT 'resolved',
                    candidate_targets_json JSON,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    created_by_user_id INTEGER NOT NULL,
                    discord_message_id INTEGER,
                    discord_channel_id INTEGER
                )
                """
            )
        )
        conn.execute(
            sql_text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_discord_evidence_guild_url_hash ON discord_evidence_items(guild_id, url_hash)"
            )
        )
        conn.execute(
            sql_text(
                "CREATE INDEX IF NOT EXISTS idx_discord_evidence_created ON discord_evidence_items(guild_id, created_at)"
            )
        )
        conn.execute(
            sql_text(
                "CREATE INDEX IF NOT EXISTS idx_discord_evidence_pinned ON discord_evidence_items(guild_id, pinned, created_at)"
            )
        )
        conn.execute(
            sql_text(
                "CREATE INDEX IF NOT EXISTS idx_discord_evidence_resolution ON discord_evidence_items(guild_id, resolution_state, updated_at)"
            )
        )
        conn.execute(
            sql_text(
                """
                CREATE TABLE IF NOT EXISTS discord_evidence_targets (
                    id INTEGER PRIMARY KEY,
                    evidence_id INTEGER NOT NULL,
                    target_key TEXT NOT NULL,
                    target_kind TEXT NOT NULL,
                    target_label TEXT,
                    issuer_id INTEGER,
                    company_id INTEGER,
                    symbol TEXT,
                    confidence REAL,
                    resolution_method TEXT,
                    is_primary INTEGER NOT NULL DEFAULT 0,
                    extra_json JSON,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                )
                """
            )
        )
        conn.execute(
            sql_text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_discord_evidence_target_key ON discord_evidence_targets(evidence_id, target_key)"
            )
        )
        conn.execute(
            sql_text(
                "CREATE INDEX IF NOT EXISTS idx_discord_evidence_target_evidence ON discord_evidence_targets(evidence_id)"
            )
        )
        conn.execute(
            sql_text(
                "CREATE INDEX IF NOT EXISTS idx_discord_evidence_target_symbol ON discord_evidence_targets(symbol, updated_at)"
            )
        )
        conn.execute(
            sql_text(
                "CREATE INDEX IF NOT EXISTS idx_discord_evidence_target_issuer ON discord_evidence_targets(issuer_id, updated_at)"
            )
        )

        def _json_list(value: object) -> list[str]:
            if isinstance(value, list):
                return [str(v).strip() for v in value if str(v).strip()]
            raw = str(value or "").strip()
            if not raw:
                return []
            try:
                parsed = json.loads(raw)
            except Exception:
                return []
            if not isinstance(parsed, list):
                return []
            return [str(v).strip() for v in parsed if str(v).strip()]

        used_issuer_slugs = {
            str(row[0]).strip().lower()
            for row in conn.execute(sql_text("SELECT slug FROM issuers WHERE slug IS NOT NULL")).fetchall()
            if row and row[0]
        }

        def _ensure_company_issuer(*, company_id: int, company_name: str, ticker: str | None, exchange: str | None) -> int:
            current = conn.execute(
                sql_text("SELECT issuer_id FROM companies WHERE id = :company_id"),
                {"company_id": int(company_id)},
            ).fetchone()
            if current and current[0]:
                return int(current[0])
            base_slug = _slugify(company_name or ticker or f"issuer-{company_id}", default=f"issuer-{company_id}")
            slug = base_slug
            suffix = 2
            while slug.lower() in used_issuer_slugs:
                slug = f"{base_slug}-{suffix}"
                suffix += 1
            used_issuer_slugs.add(slug.lower())
            result = conn.execute(
                sql_text(
                    """
                    INSERT INTO issuers(slug, legal_name, display_name, jurisdiction, entity_status, sector, industry, sic_code, website, ir_website, created_at, updated_at, extra)
                    VALUES(:slug, :legal_name, :display_name, NULL, 'ACTIVE', NULL, NULL, NULL, NULL, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, '{}')
                    """
                ),
                {
                    "slug": slug,
                    "legal_name": company_name or ticker or slug,
                    "display_name": company_name or ticker or slug,
                },
            )
            issuer_id = int(result.lastrowid)
            conn.execute(
                sql_text("UPDATE companies SET issuer_id = :issuer_id WHERE id = :company_id"),
                {"issuer_id": issuer_id, "company_id": int(company_id)},
            )
            if ticker:
                exists = conn.execute(
                    sql_text(
                        "SELECT id FROM issuer_identifiers WHERE id_type = 'TICKER' AND UPPER(id_value) = :sym LIMIT 1"
                    ),
                    {"sym": str(ticker).upper()},
                ).fetchone()
                if not exists:
                    conn.execute(
                        sql_text(
                            """
                            INSERT INTO issuer_identifiers(issuer_id, id_type, id_value, exchange, currency, is_primary, valid_from, valid_to, created_at)
                            VALUES(:issuer_id, 'TICKER', :id_value, :exchange, NULL, 1, NULL, NULL, CURRENT_TIMESTAMP)
                            """
                        ),
                        {
                            "issuer_id": issuer_id,
                            "id_value": str(ticker).upper(),
                            "exchange": str(exchange or "").strip() or None,
                        },
                    )
            return issuer_id

        def _resolve_symbol_target(symbol: str | None) -> dict[str, object] | None:
            sym = str(symbol or "").strip().upper()
            if not sym:
                return None
            issuer_row = conn.execute(
                sql_text(
                    """
                    SELECT i.id, i.display_name
                    FROM issuer_identifiers ident
                    JOIN issuers i ON i.id = ident.issuer_id
                    WHERE ident.id_type = 'TICKER' AND UPPER(ident.id_value) = :sym
                    ORDER BY ident.is_primary DESC, ident.id ASC
                    LIMIT 1
                    """
                ),
                {"sym": sym},
            ).fetchone()
            company_row = conn.execute(
                sql_text(
                    """
                    SELECT id, issuer_id, name, exchange
                    FROM companies
                    WHERE UPPER(COALESCE(ticker, '')) = :sym
                    ORDER BY id ASC
                    LIMIT 1
                    """
                ),
                {"sym": sym},
            ).fetchone()
            issuer_id = int(issuer_row[0]) if issuer_row and issuer_row[0] is not None else None
            company_id = int(company_row[0]) if company_row and company_row[0] is not None else None
            label = str(issuer_row[1] or "").strip() if issuer_row else ""
            if company_row:
                if issuer_id is None:
                    issuer_id = int(company_row[1]) if company_row[1] is not None else None
                if issuer_id is None:
                    issuer_id = _ensure_company_issuer(
                        company_id=int(company_row[0]),
                        company_name=str(company_row[2] or "").strip(),
                        ticker=sym,
                        exchange=str(company_row[3] or "").strip() or None,
                    )
                if not label:
                    label = str(company_row[2] or "").strip()
            if issuer_id is not None and not label:
                label_row = conn.execute(
                    sql_text("SELECT display_name FROM issuers WHERE id = :issuer_id"),
                    {"issuer_id": issuer_id},
                ).fetchone()
                label = str(label_row[0] or "").strip() if label_row else ""
            return {
                "target_key": f"ticker:{sym}",
                "target_kind": "ticker",
                "target_label": label or sym,
                "issuer_id": issuer_id,
                "company_id": company_id,
                "symbol": sym,
            }

        evidence_by_key: dict[tuple[int, str], int] = {
            (int(row[1]), str(row[2])): int(row[0])
            for row in conn.execute(
                sql_text("SELECT id, guild_id, url_hash FROM discord_evidence_items")
            ).fetchall()
        }

        def _ensure_evidence(
            *,
            guild_id: int,
            url_raw: str | None,
            url_canonical: str | None,
            title: str,
            note: str | None,
            sentiment: str | None,
            tags: list[str] | None,
            source_type: str | None,
            pinned: bool,
            resolution_state: str,
            candidate_targets_json: dict[str, object] | None,
            created_at: object,
            updated_at: object,
            created_by_user_id: int,
            discord_message_id: int | None,
            discord_channel_id: int | None,
        ) -> int | None:
            canonical = _canonicalize_url(url_canonical or url_raw) or str(url_canonical or url_raw or "").strip()
            if not canonical:
                return None
            digest = _hash_url(canonical)
            key = (int(guild_id), digest)
            existing_id = evidence_by_key.get(key)
            payload = {
                "guild_id": int(guild_id),
                "url_raw": str(url_raw or canonical)[:2048],
                "url_canonical": canonical[:2048],
                "url_hash": digest,
                "title": (str(title or "").strip() or canonical)[:512],
                "note": (str(note).strip()[:4000] if isinstance(note, str) and str(note).strip() else None),
                "sentiment": (str(sentiment).strip().lower()[:16] if isinstance(sentiment, str) and str(sentiment).strip() else None),
                "tags": json.dumps(sorted({str(tag).strip() for tag in (tags or []) if str(tag).strip()})),
                "source_type": (str(source_type or "other").strip() or "other")[:32],
                "pinned": 1 if bool(pinned) else 0,
                "resolution_state": str(resolution_state or "resolved").strip() or "resolved",
                "candidate_targets_json": json.dumps(candidate_targets_json) if candidate_targets_json else None,
                "created_at": created_at,
                "updated_at": updated_at,
                "created_by_user_id": int(created_by_user_id),
                "discord_message_id": int(discord_message_id) if discord_message_id else None,
                "discord_channel_id": int(discord_channel_id) if discord_channel_id else None,
            }
            if existing_id is not None:
                conn.execute(
                    sql_text(
                        """
                        UPDATE discord_evidence_items
                        SET
                            title = CASE WHEN NULLIF(TRIM(COALESCE(title, '')), '') IS NULL THEN :title ELSE title END,
                            note = CASE WHEN NULLIF(TRIM(COALESCE(note, '')), '') IS NULL THEN :note ELSE note END,
                            sentiment = COALESCE(sentiment, :sentiment),
                            source_type = CASE WHEN NULLIF(TRIM(COALESCE(source_type, '')), '') IS NULL THEN :source_type ELSE source_type END,
                            pinned = CASE WHEN pinned = 1 OR :pinned = 1 THEN 1 ELSE 0 END,
                            resolution_state = CASE
                                WHEN resolution_state = 'resolved' OR :resolution_state = 'resolved' THEN 'resolved'
                                WHEN resolution_state = 'request_created' OR :resolution_state = 'request_created' THEN 'request_created'
                                ELSE COALESCE(NULLIF(TRIM(:resolution_state), ''), resolution_state)
                            END,
                            candidate_targets_json = COALESCE(candidate_targets_json, :candidate_targets_json),
                            updated_at = CASE WHEN updated_at >= :updated_at THEN updated_at ELSE :updated_at END,
                            discord_message_id = COALESCE(discord_message_id, :discord_message_id),
                            discord_channel_id = COALESCE(discord_channel_id, :discord_channel_id)
                        WHERE id = :evidence_id
                        """
                    ),
                    {**payload, "evidence_id": int(existing_id)},
                )
                return int(existing_id)
            result = conn.execute(
                sql_text(
                    """
                    INSERT INTO discord_evidence_items(
                        guild_id, url_raw, url_canonical, url_hash, title, note, sentiment, tags, source_type,
                        pinned, resolution_state, candidate_targets_json, created_at, updated_at, created_by_user_id,
                        discord_message_id, discord_channel_id
                    )
                    VALUES(
                        :guild_id, :url_raw, :url_canonical, :url_hash, :title, :note, :sentiment, :tags, :source_type,
                        :pinned, :resolution_state, :candidate_targets_json, :created_at, :updated_at, :created_by_user_id,
                        :discord_message_id, :discord_channel_id
                    )
                    """
                ),
                payload,
            )
            evidence_id = int(result.lastrowid)
            evidence_by_key[key] = evidence_id
            return evidence_id

        def _ensure_target(
            *,
            evidence_id: int,
            target_key: str,
            target_kind: str,
            target_label: str | None,
            issuer_id: int | None,
            company_id: int | None,
            symbol: str | None,
            resolution_method: str,
            is_primary: bool,
        ) -> None:
            conn.execute(
                sql_text(
                    """
                    INSERT OR IGNORE INTO discord_evidence_targets(
                        evidence_id, target_key, target_kind, target_label, issuer_id, company_id, symbol,
                        confidence, resolution_method, is_primary, extra_json, created_at, updated_at
                    )
                    VALUES(
                        :evidence_id, :target_key, :target_kind, :target_label, :issuer_id, :company_id, :symbol,
                        1.0, :resolution_method, :is_primary, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                ),
                {
                    "evidence_id": int(evidence_id),
                    "target_key": str(target_key),
                    "target_kind": str(target_kind),
                    "target_label": str(target_label or "").strip() or None,
                    "issuer_id": int(issuer_id) if issuer_id is not None else None,
                    "company_id": int(company_id) if company_id is not None else None,
                    "symbol": str(symbol).strip().upper() if isinstance(symbol, str) and str(symbol).strip() else None,
                    "resolution_method": str(resolution_method),
                    "is_primary": 1 if bool(is_primary) else 0,
                },
            )

        if _table_exists("discord_ticker_sources"):
            legacy_rows = conn.execute(
                sql_text(
                    """
                    SELECT id, guild_id, symbol, title, url, note, sentiment, tags, source_type, pinned,
                           created_at, created_by_user_id, discord_message_id, discord_channel_id
                    FROM discord_ticker_sources
                    ORDER BY id ASC
                    """
                )
            ).fetchall()
            source_to_evidence: dict[int, int] = {}
            for row in legacy_rows:
                evidence_id = _ensure_evidence(
                    guild_id=int(row[1]),
                    url_raw=str(row[4] or ""),
                    url_canonical=str(row[4] or ""),
                    title=str(row[3] or row[4] or ""),
                    note=str(row[5] or "").strip() or None,
                    sentiment=str(row[6] or "").strip() or None,
                    tags=_json_list(row[7]),
                    source_type=str(row[8] or "other"),
                    pinned=bool(row[9]),
                    resolution_state="resolved",
                    candidate_targets_json=None,
                    created_at=row[10],
                    updated_at=row[10],
                    created_by_user_id=int(row[11] or 0),
                    discord_message_id=(int(row[12]) if row[12] else None),
                    discord_channel_id=(int(row[13]) if row[13] else None),
                )
                if evidence_id is None:
                    continue
                source_to_evidence[int(row[0])] = int(evidence_id)
                target = _resolve_symbol_target(str(row[2] or ""))
                if target:
                    _ensure_target(
                        evidence_id=int(evidence_id),
                        target_key=str(target["target_key"]),
                        target_kind=str(target["target_kind"]),
                        target_label=str(target.get("target_label") or ""),
                        issuer_id=(int(target["issuer_id"]) if target.get("issuer_id") is not None else None),
                        company_id=(int(target["company_id"]) if target.get("company_id") is not None else None),
                        symbol=(str(target.get("symbol") or "") or None),
                        resolution_method="legacy_source_backfill",
                        is_primary=True,
                    )
            for source_id, evidence_id in source_to_evidence.items():
                conn.execute(
                    sql_text(
                        "UPDATE discord_link_inbox_items SET evidence_id = COALESCE(evidence_id, :evidence_id), source_id = :evidence_id WHERE source_id = :source_id"
                    ),
                    {"source_id": int(source_id), "evidence_id": int(evidence_id)},
                )

        inbox_rows = conn.execute(
            sql_text(
                """
                SELECT id, guild_id, channel_id, message_id, url_raw, url_canonical, url_hash, message_excerpt,
                       embed_title, deterministic_ticker, status, evidence_id, source_id, created_at, updated_at,
                       resolution_candidates_json, resolution_request_id
                FROM discord_link_inbox_items
                ORDER BY id ASC
                """
            )
        ).fetchall()
        for row in inbox_rows:
            existing_evidence_id = int(row[11]) if row[11] is not None else None
            candidate_targets = None
            if row[15]:
                try:
                    candidate_targets = json.loads(str(row[15]))
                except Exception:
                    candidate_targets = None
            evidence_id = existing_evidence_id or _ensure_evidence(
                guild_id=int(row[1]),
                url_raw=str(row[4] or ""),
                url_canonical=str(row[5] or row[4] or ""),
                title=str(row[8] or row[7] or row[5] or ""),
                note=str(row[7] or "").strip() or None,
                sentiment=None,
                tags=[],
                source_type="research",
                pinned=False,
                resolution_state=("request_created" if row[16] is not None else ("resolved" if str(row[10] or "").strip() in {"deterministic_ingested", "agentic_ingested"} else "pending")),
                candidate_targets_json=(candidate_targets if isinstance(candidate_targets, dict) else None),
                created_at=row[13],
                updated_at=row[14],
                created_by_user_id=0,
                discord_message_id=(int(row[3]) if row[3] else None),
                discord_channel_id=(int(row[2]) if row[2] else None),
            )
            if evidence_id is None:
                continue
            if row[9]:
                target = _resolve_symbol_target(str(row[9]))
                if target:
                    _ensure_target(
                        evidence_id=int(evidence_id),
                        target_key=str(target["target_key"]),
                        target_kind=str(target["target_kind"]),
                        target_label=str(target.get("target_label") or ""),
                        issuer_id=(int(target["issuer_id"]) if target.get("issuer_id") is not None else None),
                        company_id=(int(target["company_id"]) if target.get("company_id") is not None else None),
                        symbol=(str(target.get("symbol") or "") or None),
                        resolution_method="legacy_inbox_backfill",
                        is_primary=True,
                    )
            conn.execute(
                sql_text(
                    """
                    UPDATE discord_link_inbox_items
                    SET
                        evidence_id = :evidence_id,
                        source_id = COALESCE(source_id, :evidence_id),
                        resolution_state = CASE
                            WHEN resolution_request_id IS NOT NULL THEN 'request_created'
                            WHEN NULLIF(TRIM(COALESCE(deterministic_ticker, '')), '') IS NOT NULL OR :has_target = 1 THEN 'resolved'
                            ELSE COALESCE(NULLIF(TRIM(resolution_state), ''), 'pending')
                        END
                    WHERE id = :inbox_id
                    """
                ),
                {
                    "evidence_id": int(evidence_id),
                    "inbox_id": int(row[0]),
                    "has_target": 1 if row[9] else 0,
                },
            )

        try:
            conn.execute(
                sql_text(
                    """
                    UPDATE notes
                    SET status = 'archived',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE note_type = 'discord'
                      AND json_extract(extra, '$.discord.kind') = 'source'
                    """
                )
            )
        except Exception:
            pass


def _ensure_triage(engine: Engine) -> None:
    with engine.begin() as conn:
        cols = {
            row[1]
            for row in conn.execute(sql_text("PRAGMA table_info(documents)")).fetchall()
        }

        def _add_column(name: str, ddl: str) -> None:
            if name in cols:
                return
            conn.execute(sql_text(f"ALTER TABLE documents ADD COLUMN {ddl}"))

        _add_column("triage_state", "triage_state TEXT NOT NULL DEFAULT 'NEW'")
        _add_column("saved", "saved INTEGER NOT NULL DEFAULT 0")
        _add_column("seen_at", "seen_at DATETIME")
        _add_column("opened_at", "opened_at DATETIME")
        _add_column("saved_at", "saved_at DATETIME")
        _add_column("actioned_at", "actioned_at DATETIME")
        _add_column("ignored_at", "ignored_at DATETIME")
        _add_column("triage_updated_at", "triage_updated_at DATETIME")
        _add_column("search_vector", "search_vector TEXT")

        conn.execute(
            sql_text(
                """
                UPDATE documents
                SET triage_updated_at = COALESCE(triage_updated_at, ingested_at)
                WHERE triage_updated_at IS NULL
                """
            )
        )

        ev_exists = conn.execute(
            sql_text(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='document_events' LIMIT 1"
            )
        ).scalar()
        if not ev_exists:
            conn.execute(
                sql_text(
                    """
                    CREATE TABLE document_events (
                        id INTEGER PRIMARY KEY,
                        document_id INTEGER NOT NULL,
                        event_type TEXT NOT NULL,
                        event_at DATETIME NOT NULL,
                        meta JSON NOT NULL DEFAULT '{}',
                        FOREIGN KEY(document_id) REFERENCES documents(id)
                    );
                    """
                )
            )

        conn.execute(
            sql_text(
                "CREATE INDEX IF NOT EXISTS idx_documents_triage_published ON documents(triage_state, published_at)"
            )
        )
        conn.execute(
            sql_text(
                "CREATE INDEX IF NOT EXISTS idx_documents_triage_ingested ON documents(triage_state, ingested_at)"
            )
        )
        conn.execute(
            sql_text(
                "CREATE INDEX IF NOT EXISTS idx_documents_saved_updated ON documents(saved, triage_updated_at)"
            )
        )
        conn.execute(
            sql_text(
                "CREATE INDEX IF NOT EXISTS idx_documents_feed_counts ON documents(kind, title, triage_state, saved)"
            )
        )
        conn.execute(
            sql_text("CREATE INDEX IF NOT EXISTS idx_documents_seen_at ON documents(seen_at)")
        )
        conn.execute(
            sql_text(
                "CREATE INDEX IF NOT EXISTS idx_document_events_doc ON document_events(document_id, event_at)"
            )
        )

        rules_exists = conn.execute(
            sql_text(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='notification_rules' LIMIT 1"
            )
        ).scalar()
        if not rules_exists:
            conn.execute(
                sql_text(
                    """
                    CREATE TABLE notification_rules (
                        id INTEGER PRIMARY KEY,
                        name TEXT NOT NULL,
                        enabled INTEGER NOT NULL DEFAULT 1,
                        action TEXT NOT NULL DEFAULT 'toast',
                        source_ids JSON NOT NULL DEFAULT '[]',
                        domain_slugs JSON NOT NULL DEFAULT '[]',
                        entity_ids JSON NOT NULL DEFAULT '[]',
                        tag_slugs JSON NOT NULL DEFAULT '[]',
                        keyword_regex TEXT,
                        created_at DATETIME NOT NULL,
                        updated_at DATETIME NOT NULL
                    );
                    """
                )
            )
        notif_cols = {
            row[1]
            for row in conn.execute(sql_text("PRAGMA table_info(notification_rules)")).fetchall()
        }

        def _add_notif_column(name: str, ddl: str) -> None:
            if name in notif_cols:
                return
            conn.execute(sql_text(f"ALTER TABLE notification_rules ADD COLUMN {ddl}"))

        _add_notif_column("quiet_start", "quiet_start TEXT")
        _add_notif_column("quiet_end", "quiet_end TEXT")
        _add_notif_column("quiet_tz", "quiet_tz TEXT")
        conn.execute(
            sql_text(
                "CREATE INDEX IF NOT EXISTS idx_notification_rules_enabled ON notification_rules(enabled)"
            )
        )


def _ensure_mentions(engine: Engine) -> None:
    with engine.begin() as conn:
        exists = conn.execute(
            sql_text(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='document_mentions' LIMIT 1"
            )
        ).scalar()
        if not exists:
            conn.execute(
                sql_text(
                    """
                    CREATE TABLE document_mentions (
                        id INTEGER PRIMARY KEY,
                        source_document_id INTEGER NOT NULL,
                        target_document_id INTEGER NOT NULL,
                        url TEXT,
                        created_at DATETIME NOT NULL,
                        FOREIGN KEY(source_document_id) REFERENCES documents(id),
                        FOREIGN KEY(target_document_id) REFERENCES documents(id),
                        UNIQUE(source_document_id, target_document_id, url)
                    );
                    """
                )
            )
        conn.execute(
            sql_text(
                "CREATE INDEX IF NOT EXISTS idx_document_mentions_source ON document_mentions(source_document_id)"
            )
        )
        conn.execute(
            sql_text(
                "CREATE INDEX IF NOT EXISTS idx_document_mentions_target ON document_mentions(target_document_id)"
            )
        )
        conn.execute(
            sql_text(
                "CREATE INDEX IF NOT EXISTS idx_document_mentions_url ON document_mentions(url)"
            )
        )


def _ensure_fts(engine: Engine) -> None:
    with engine.begin() as conn:
        doc_exists = conn.execute(
            sql_text(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='document_fts' LIMIT 1"
            )
        ).scalar()

        if not doc_exists:
            conn.execute(
                sql_text(
                    """
                    CREATE VIRTUAL TABLE document_fts
                    USING fts5(
                        title,
                        normalized_text,
                        translation_en,
                        content='documents',
                        content_rowid='id'
                    );
                    """
                )
            )

            conn.execute(
                sql_text(
                    """
                    CREATE TRIGGER documents_ai
                    AFTER INSERT ON documents BEGIN
                        INSERT INTO document_fts(rowid, title, normalized_text, translation_en)
                        VALUES (new.id, new.title, new.normalized_text, new.translation_en);
                    END;
                    """
                )
            )
            conn.execute(
                sql_text(
                    """
                    CREATE TRIGGER documents_ad
                    AFTER DELETE ON documents BEGIN
                        INSERT INTO document_fts(document_fts, rowid, title, normalized_text, translation_en)
                        VALUES ('delete', old.id, old.title, old.normalized_text, old.translation_en);
                    END;
                    """
                )
            )
            conn.execute(
                sql_text(
                    """
                    CREATE TRIGGER documents_au
                    AFTER UPDATE ON documents BEGIN
                        INSERT INTO document_fts(document_fts, rowid, title, normalized_text, translation_en)
                        VALUES ('delete', old.id, old.title, old.normalized_text, old.translation_en);
                        INSERT INTO document_fts(rowid, title, normalized_text, translation_en)
                        VALUES (new.id, new.title, new.normalized_text, new.translation_en);
                    END;
                    """
                )
            )

        note_exists = conn.execute(
            sql_text(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='note_fts' LIMIT 1"
            )
        ).scalar()
        if note_exists:
            return

        conn.execute(
            sql_text(
                """
                CREATE VIRTUAL TABLE note_fts
                USING fts5(
                    title,
                    body_md,
                    content='notes',
                    content_rowid='id'
                );
                """
            )
        )

        conn.execute(
            sql_text(
                """
                CREATE TRIGGER notes_ai
                AFTER INSERT ON notes BEGIN
                    INSERT INTO note_fts(rowid, title, body_md)
                    VALUES (new.id, new.title, new.body_md);
                END;
                """
            )
        )
        conn.execute(
            sql_text(
                """
                CREATE TRIGGER notes_ad
                AFTER DELETE ON notes BEGIN
                    INSERT INTO note_fts(note_fts, rowid, title, body_md)
                    VALUES ('delete', old.id, old.title, old.body_md);
                END;
                """
            )
        )
        conn.execute(
            sql_text(
                """
                CREATE TRIGGER notes_au
                AFTER UPDATE ON notes BEGIN
                    INSERT INTO note_fts(note_fts, rowid, title, body_md)
                    VALUES ('delete', old.id, old.title, old.body_md);
                    INSERT INTO note_fts(rowid, title, body_md)
                    VALUES (new.id, new.title, new.body_md);
                END;
                """
            )
        )


def _ensure_postgres_search_indexes(engine: Engine) -> None:
    from thoth_terminal.catalog_index_maintenance import catalog_index_status
    from thoth_terminal.document_search_maintenance import ensure_document_search_startup
    from thoth_terminal.institutional_search import search_index_statements

    with engine.begin() as conn:
        ensure_document_search_startup(conn)
        conn.execute(sql_text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        for statement in search_index_statements():
            conn.execute(sql_text(statement))
        # Catalog builds are explicit online maintenance, never startup DDL.
        missing = [name for name, ready in catalog_index_status(conn).items() if not ready]
        if missing:
            logging.getLogger(__name__).warning(
                "Catalog indexes unavailable: %s; run scripts/maintain_catalog_indexes.py install",
                ", ".join(missing),
            )


def _ensure_research_canon_schema(engine: Engine) -> None:
    """Create cross-dialect indexes and invariants for research-canon rails."""
    backend = database_backend(str(engine.url))
    inspector = inspect(engine)

    def _has_table(name: str) -> bool:
        try:
            return bool(inspector.has_table(name))
        except Exception:
            return False

    with engine.begin() as conn:
        if _has_table("research_subject_scenarios"):
            selected_true = "selected_canon IS TRUE" if backend == "postgresql" else "selected_canon = 1"
            selected_false = "FALSE" if backend == "postgresql" else "0"
            # Keep the newest selected active/draft scenario and demote older duplicates before
            # adding the partial unique index. This makes startup resilient after manual edits.
            conn.execute(
                sql_text(
                    f"""
                    WITH ranked AS (
                        SELECT
                            id,
                            ROW_NUMBER() OVER (
                                PARTITION BY subject_id
                                ORDER BY updated_at DESC, id DESC
                            ) AS rn
                        FROM research_subject_scenarios
                        WHERE {selected_true}
                          AND status IN ('draft', 'active')
                    )
                    UPDATE research_subject_scenarios
                    SET selected_canon = {selected_false},
                        status = CASE WHEN status = 'active' THEN 'superseded' ELSE status END,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id IN (SELECT id FROM ranked WHERE rn > 1)
                    """
                )
            )
            conn.execute(
                sql_text(
                    "CREATE INDEX IF NOT EXISTS idx_research_subject_scenarios_subject_status "
                    "ON research_subject_scenarios(subject_id, status, selected_canon)"
                )
            )
            conn.execute(
                sql_text(
                    "CREATE INDEX IF NOT EXISTS idx_research_subject_scenarios_kind_status "
                    "ON research_subject_scenarios(scenario_kind, status)"
                )
            )
            partial_where = (
                "selected_canon IS TRUE AND status IN ('draft', 'active')"
                if backend == "postgresql"
                else "selected_canon = 1 AND status IN ('draft', 'active')"
            )
            conn.execute(
                sql_text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_research_subject_scenarios_one_active_canon "
                    f"ON research_subject_scenarios(subject_id) WHERE {partial_where}"
                )
            )

        if _has_table("research_source_packs"):
            conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_research_source_packs_status_updated ON research_source_packs(status, updated_at)"))
            conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_research_source_packs_reviewed ON research_source_packs(reviewed_at)"))
        if _has_table("research_report_quality"):
            conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_research_report_quality_state_eval ON research_report_quality(state, evaluated_at)"))
            conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_research_report_quality_report_version ON research_report_quality(report_id, report_version_id)"))
        if _has_table("research_campaigns"):
            conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_research_campaigns_status_updated ON research_campaigns(status, updated_at)"))
            conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_research_campaigns_universe_status ON research_campaigns(universe_id, status)"))
        if _has_table("research_campaign_items"):
            conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_research_campaign_items_queue ON research_campaign_items(campaign_id, status, priority, updated_at)"))
            conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_research_campaign_items_subject_status ON research_campaign_items(subject_id, status)"))
            conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_research_campaign_items_target_lookup ON research_campaign_items(target_type, target_key)"))
        if _has_table("agent_canon_cards"):
            conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_agent_canon_cards_status_generated ON agent_canon_cards(status, generated_at)"))
            conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_agent_canon_cards_subject_status ON agent_canon_cards(subject_id, status)"))
        if _has_table("bottleneck_model_links"):
            conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_bottleneck_model_links_object ON bottleneck_model_links(object_type, object_id, relation)"))
            conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_bottleneck_model_links_model_relation ON bottleneck_model_links(model_id, relation)"))
        if _has_table("coverage_gaps"):
            conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_coverage_gaps_owner_kind_status ON coverage_gaps(owner_ref, gap_kind, status)"))
            conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_coverage_gaps_subject_status_kind ON coverage_gaps(subject_id, status, gap_kind)"))


def _ensure_issuer_schema(engine: Engine) -> None:
    """Ensure the three-rail issuer architecture tables exist."""
    with engine.begin() as conn:
        # Check if issuers table exists
        issuers_exists = conn.execute(
            sql_text(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='issuers' LIMIT 1"
            )
        ).scalar()

        if not issuers_exists:
            # Create issuers table
            conn.execute(
                sql_text(
                    """
                    CREATE TABLE issuers (
                        id INTEGER PRIMARY KEY,
                        slug TEXT NOT NULL UNIQUE,
                        lei TEXT UNIQUE,
                        legal_name TEXT NOT NULL,
                        display_name TEXT NOT NULL,
                        jurisdiction TEXT,
                        entity_status TEXT,
                        sector TEXT,
                        industry TEXT,
                        sic_code TEXT,
                        website TEXT,
                        ir_website TEXT,
                        created_at DATETIME NOT NULL,
                        updated_at DATETIME NOT NULL,
                        extra JSON NOT NULL DEFAULT '{}'
                    );
                    """
                )
            )
            conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_issuers_lei ON issuers(lei)"))
            conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_issuers_display_name ON issuers(display_name)"))
            conn.execute(sql_text("CREATE UNIQUE INDEX IF NOT EXISTS idx_issuers_slug ON issuers(slug)"))

        issuer_cols = {
            row[1]
            for row in conn.execute(sql_text("PRAGMA table_info(issuers)")).fetchall()
        }
        if "slug" not in issuer_cols:
            conn.execute(sql_text("ALTER TABLE issuers ADD COLUMN slug TEXT"))
        rows = conn.execute(
            sql_text("SELECT id, display_name, legal_name, slug FROM issuers ORDER BY id ASC")
        ).fetchall()
        used_slugs: set[str] = set()
        for row in rows:
            slug = str(row[3] or "").strip().lower()
            if slug:
                used_slugs.add(slug)
        for row in rows:
            issuer_id = int(row[0])
            current_slug = str(row[3] or "").strip()
            if current_slug:
                continue
            base_slug = _slugify(
                str(row[1] or row[2] or f"issuer-{issuer_id}"),
                default=f"issuer-{issuer_id}",
            )
            slug = base_slug
            counter = 2
            while slug.lower() in used_slugs:
                slug = f"{base_slug}-{counter}"
                counter += 1
            used_slugs.add(slug.lower())
            conn.execute(
                sql_text("UPDATE issuers SET slug = :slug WHERE id = :issuer_id"),
                {"slug": slug, "issuer_id": issuer_id},
            )
        conn.execute(sql_text("CREATE UNIQUE INDEX IF NOT EXISTS idx_issuers_slug ON issuers(slug)"))

        # Create issuer_identifiers table
        idents_exists = conn.execute(
            sql_text(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='issuer_identifiers' LIMIT 1"
            )
        ).scalar()

        if not idents_exists:
            conn.execute(
                sql_text(
                    """
                    CREATE TABLE issuer_identifiers (
                        id INTEGER PRIMARY KEY,
                        issuer_id INTEGER NOT NULL,
                        id_type TEXT NOT NULL,
                        id_value TEXT NOT NULL,
                        exchange TEXT,
                        currency TEXT,
                        is_primary INTEGER NOT NULL DEFAULT 0,
                        valid_from DATETIME,
                        valid_to DATETIME,
                        created_at DATETIME NOT NULL,
                        FOREIGN KEY(issuer_id) REFERENCES issuers(id),
                        UNIQUE(id_type, id_value)
                    );
                    """
                )
            )
            conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_issuer_identifiers_issuer ON issuer_identifiers(issuer_id)"))
            conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_issuer_identifiers_value ON issuer_identifiers(id_value)"))

        # Create securities_filings table (Rail 1)
        filings_exists = conn.execute(
            sql_text(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='securities_filings' LIMIT 1"
            )
        ).scalar()

        if not filings_exists:
            conn.execute(
                sql_text(
                    """
                    CREATE TABLE securities_filings (
                        id INTEGER PRIMARY KEY,
                        issuer_id INTEGER NOT NULL,
                        document_id INTEGER,
                        regulator TEXT NOT NULL,
                        accession_number TEXT NOT NULL,
                        filing_type TEXT NOT NULL,
                        filing_subtype TEXT,
                        filed_at DATETIME NOT NULL,
                        accepted_at DATETIME,
                        period_end DATE,
                        fiscal_year INTEGER,
                        fiscal_quarter INTEGER,
                        title TEXT,
                        description TEXT,
                        primary_document_url TEXT,
                        index_url TEXT,
                        filing_url TEXT,
                        exhibits JSON NOT NULL DEFAULT '{}',
                        has_xbrl INTEGER NOT NULL DEFAULT 0,
                        xbrl_url TEXT,
                        is_earnings_release INTEGER NOT NULL DEFAULT 0,
                        is_guidance INTEGER NOT NULL DEFAULT 0,
                        raw_meta JSON NOT NULL DEFAULT '{}',
                        created_at DATETIME NOT NULL,
                        FOREIGN KEY(issuer_id) REFERENCES issuers(id),
                        FOREIGN KEY(document_id) REFERENCES documents(id),
                        UNIQUE(regulator, accession_number)
                    );
                    """
                )
            )
            conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_securities_filings_issuer ON securities_filings(issuer_id)"))
            conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_securities_filings_filed ON securities_filings(filed_at)"))
            conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_securities_filings_type ON securities_filings(filing_type)"))

        # Create exchange_disclosures table (Rail 2)
        disclosures_exists = conn.execute(
            sql_text(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='exchange_disclosures' LIMIT 1"
            )
        ).scalar()

        if not disclosures_exists:
            conn.execute(
                sql_text(
                    """
                    CREATE TABLE exchange_disclosures (
                        id INTEGER PRIMARY KEY,
                        issuer_id INTEGER NOT NULL,
                        document_id INTEGER,
                        exchange TEXT NOT NULL,
                        disclosure_id TEXT NOT NULL,
                        disclosure_type TEXT NOT NULL,
                        disclosure_subtype TEXT,
                        announced_at DATETIME NOT NULL,
                        period_end DATE,
                        fiscal_year INTEGER,
                        fiscal_quarter INTEGER,
                        title TEXT,
                        headline TEXT,
                        disclosure_url TEXT,
                        attachment_urls JSON NOT NULL DEFAULT '[]',
                        is_earnings_related INTEGER NOT NULL DEFAULT 0,
                        is_material_event INTEGER NOT NULL DEFAULT 0,
                        language TEXT,
                        raw_meta JSON NOT NULL DEFAULT '{}',
                        created_at DATETIME NOT NULL,
                        FOREIGN KEY(issuer_id) REFERENCES issuers(id),
                        FOREIGN KEY(document_id) REFERENCES documents(id),
                        UNIQUE(exchange, disclosure_id)
                    );
                    """
                )
            )
            conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_exchange_disclosures_issuer ON exchange_disclosures(issuer_id)"))
            conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_exchange_disclosures_announced ON exchange_disclosures(announced_at)"))

        # Create ir_artifacts table (Rail 3)
        artifacts_exists = conn.execute(
            sql_text(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ir_artifacts' LIMIT 1"
            )
        ).scalar()

        if not artifacts_exists:
            conn.execute(
                sql_text(
                    """
                    CREATE TABLE ir_artifacts (
                        id INTEGER PRIMARY KEY,
                        issuer_id INTEGER NOT NULL,
                        document_id INTEGER,
                        artifact_type TEXT NOT NULL,
                        artifact_subtype TEXT,
                        published_at DATETIME,
                        event_date DATE,
                        fiscal_year INTEGER,
                        fiscal_quarter INTEGER,
                        title TEXT,
                        description TEXT,
                        canonical_url TEXT NOT NULL,
                        download_url TEXT,
                        webcast_url TEXT,
                        audio_url TEXT,
                        is_derived_transcript INTEGER NOT NULL DEFAULT 0,
                        ical_uid TEXT,
                        language TEXT,
                        raw_meta JSON NOT NULL DEFAULT '{}',
                        created_at DATETIME NOT NULL,
                        FOREIGN KEY(issuer_id) REFERENCES issuers(id),
                        FOREIGN KEY(document_id) REFERENCES documents(id),
                        UNIQUE(issuer_id, artifact_type, canonical_url)
                    );
                    """
                )
            )
            conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_ir_artifacts_issuer ON ir_artifacts(issuer_id)"))
            conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_ir_artifacts_published ON ir_artifacts(published_at)"))

        # Create earnings_events table
        earnings_exists = conn.execute(
            sql_text(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='earnings_events' LIMIT 1"
            )
        ).scalar()

        if not earnings_exists:
            conn.execute(
                sql_text(
                    """
                    CREATE TABLE earnings_events (
                        id INTEGER PRIMARY KEY,
                        issuer_id INTEGER NOT NULL,
                        fiscal_year INTEGER NOT NULL,
                        fiscal_quarter INTEGER,
                        period_end DATE,
                        scheduled_date DATE,
                        scheduled_time TEXT,
                        scheduled_tz TEXT,
                        date_provenance TEXT NOT NULL DEFAULT 'estimated',
                        date_source TEXT,
                        actual_release_at DATETIME,
                        actual_call_at DATETIME,
                        status TEXT NOT NULL DEFAULT 'scheduled',
                        release_filing_id INTEGER,
                        release_disclosure_id INTEGER,
                        release_artifact_id INTEGER,
                        presentation_artifact_id INTEGER,
                        transcript_artifact_id INTEGER,
                        webcast_artifact_id INTEGER,
                        guidance_summary TEXT,
                        raw_meta JSON NOT NULL DEFAULT '{}',
                        created_at DATETIME NOT NULL,
                        updated_at DATETIME NOT NULL,
                        FOREIGN KEY(issuer_id) REFERENCES issuers(id),
                        FOREIGN KEY(release_filing_id) REFERENCES securities_filings(id),
                        FOREIGN KEY(release_disclosure_id) REFERENCES exchange_disclosures(id),
                        FOREIGN KEY(release_artifact_id) REFERENCES ir_artifacts(id),
                        FOREIGN KEY(presentation_artifact_id) REFERENCES ir_artifacts(id),
                        FOREIGN KEY(transcript_artifact_id) REFERENCES ir_artifacts(id),
                        FOREIGN KEY(webcast_artifact_id) REFERENCES ir_artifacts(id),
                        UNIQUE(issuer_id, fiscal_year, fiscal_quarter)
                    );
                    """
                )
            )
            conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_earnings_events_issuer ON earnings_events(issuer_id)"))
            conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_earnings_events_scheduled ON earnings_events(scheduled_date)"))

        # Add issuer_id column to companies table if it doesn't exist
        company_cols = {
            row[1]
            for row in conn.execute(sql_text("PRAGMA table_info(companies)")).fetchall()
        }
        if "issuer_id" not in company_cols:
            conn.execute(sql_text("ALTER TABLE companies ADD COLUMN issuer_id INTEGER REFERENCES issuers(id)"))


def _ensure_institutional_data_schema(engine: Engine) -> None:
    with engine.begin() as conn:
        def _table_exists(name: str) -> bool:
            return bool(
                conn.execute(
                    sql_text(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=:name LIMIT 1"
                    ),
                    {"name": name},
                ).scalar()
            )

        def _cols(table: str) -> set[str]:
            try:
                rows = conn.execute(sql_text(f"PRAGMA table_info({table})")).fetchall()
            except Exception:
                return set()
            return {row[1] for row in rows if len(row) > 1}

        def _add_column(table: str, *, name: str, ddl: str) -> None:
            if not _table_exists(table):
                return
            cols = _cols(table)
            if name in cols:
                return
            conn.execute(sql_text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))

        _add_column("datasets", name="family", ddl="family TEXT NOT NULL DEFAULT 'general'")

        conn.execute(
            sql_text(
                """
                CREATE TABLE IF NOT EXISTS structured_data_facts (
                    id INTEGER PRIMARY KEY,
                    dataset_family TEXT NOT NULL,
                    fact_key TEXT NOT NULL,
                    group_key TEXT NOT NULL,
                    company_id INTEGER REFERENCES companies(id),
                    document_id INTEGER REFERENCES documents(id),
                    source_id INTEGER REFERENCES sources(id),
                    ingestion_run_id INTEGER REFERENCES ingestion_runs(id),
                    dataset_id INTEGER REFERENCES datasets(id),
                    period_date DATE,
                    period_label TEXT,
                    as_of_date DATE,
                    metric_key TEXT NOT NULL,
                    metric_label TEXT,
                    statement_type TEXT,
                    value_number REAL,
                    value_text TEXT,
                    unit TEXT,
                    currency TEXT,
                    evidence_tier TEXT,
                    provenance_label TEXT,
                    provenance_url TEXT,
                    payload JSON NOT NULL DEFAULT '{}',
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    UNIQUE(dataset_family, fact_key)
                )
                """
            )
        )
        conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_structured_data_facts_family ON structured_data_facts(dataset_family)"))
        conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_structured_data_facts_group_key ON structured_data_facts(group_key)"))
        conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_structured_data_facts_company ON structured_data_facts(company_id)"))
        conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_structured_data_facts_document ON structured_data_facts(document_id)"))
        conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_structured_data_facts_dataset ON structured_data_facts(dataset_id)"))
        conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_structured_data_facts_period ON structured_data_facts(period_date)"))
        conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_structured_data_facts_as_of ON structured_data_facts(as_of_date)"))

        conn.execute(
            sql_text(
                """
                CREATE TABLE IF NOT EXISTS instrument_requests (
                    id INTEGER PRIMARY KEY,
                    request_key TEXT NOT NULL,
                    title TEXT NOT NULL,
                    intent TEXT NOT NULL DEFAULT 'instrument',
                    summary TEXT,
                    target_entities JSON NOT NULL DEFAULT '[]',
                    target_themes JSON NOT NULL DEFAULT '[]',
                    desired_outputs JSON NOT NULL DEFAULT '[]',
                    evidence_bundle JSON NOT NULL DEFAULT '[]',
                    provenance_summary TEXT,
                    evidence_tier TEXT,
                    status TEXT NOT NULL DEFAULT 'draft',
                    priority INTEGER NOT NULL DEFAULT 5,
                    automation_source TEXT,
                    source_kind TEXT,
                    draft_payload JSON NOT NULL DEFAULT '{}',
                    live_payload JSON NOT NULL DEFAULT '{}',
                    linked_objects JSON NOT NULL DEFAULT '{}',
                    review_note TEXT,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    reviewed_at DATETIME,
                    UNIQUE(request_key)
                )
                """
            )
        )
        conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_instrument_requests_status ON instrument_requests(status)"))
        conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_instrument_requests_priority ON instrument_requests(priority)"))
        conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_instrument_requests_request_key ON instrument_requests(request_key)"))

        conn.execute(
            sql_text(
                """
                CREATE TABLE IF NOT EXISTS product_stage_entries (
                    id INTEGER PRIMARY KEY,
                    entry_key TEXT NOT NULL,
                    company_id INTEGER REFERENCES companies(id),
                    company_name TEXT NOT NULL,
                    product_name TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    target_date DATE,
                    date_precision TEXT,
                    date_text TEXT,
                    certainty TEXT NOT NULL DEFAULT 'attributed',
                    status TEXT NOT NULL DEFAULT 'draft',
                    notes TEXT,
                    evidence_tier TEXT,
                    evidence_bundle JSON NOT NULL DEFAULT '[]',
                    linked_dataset_ids JSON NOT NULL DEFAULT '[]',
                    linked_supply_chain_ids JSON NOT NULL DEFAULT '[]',
                    linked_note_ids JSON NOT NULL DEFAULT '[]',
                    linked_thesis_ids JSON NOT NULL DEFAULT '[]',
                    extra JSON NOT NULL DEFAULT '{}',
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    UNIQUE(entry_key)
                )
                """
            )
        )
        conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_product_stage_entries_company ON product_stage_entries(company_id)"))
        conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_product_stage_entries_stage ON product_stage_entries(stage)"))
        conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_product_stage_entries_target_date ON product_stage_entries(target_date)"))
        conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_product_stage_entries_status ON product_stage_entries(status)"))
        _add_column("product_stage_entries", name="tracker_key", ddl="tracker_key TEXT")
        _add_column("product_stage_entries", name="candidate_claim_id", ddl="candidate_claim_id INTEGER")
        _add_column("product_stage_entries", name="resolved_claim_id", ddl="resolved_claim_id INTEGER")
        _add_column("product_stage_entries", name="claim_count", ddl="claim_count INTEGER NOT NULL DEFAULT 0")
        _add_column("product_stage_entries", name="conflict_state", ddl="conflict_state TEXT")
        _add_column("product_stage_entries", name="highest_evidence_tier", ddl="highest_evidence_tier TEXT")
        _add_column("product_stage_entries", name="last_claim_at", ddl="last_claim_at DATETIME")
        conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_product_stage_entries_candidate_claim ON product_stage_entries(candidate_claim_id)"))
        conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_product_stage_entries_resolved_claim ON product_stage_entries(resolved_claim_id)"))
        conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_product_stage_entries_conflict_state ON product_stage_entries(conflict_state)"))
        conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_product_stage_entries_highest_evidence ON product_stage_entries(highest_evidence_tier)"))
        conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_product_stage_entries_last_claim_at ON product_stage_entries(last_claim_at)"))

        conn.execute(
            sql_text(
                """
                CREATE TABLE IF NOT EXISTS product_stage_history (
                    id INTEGER PRIMARY KEY,
                    entry_id INTEGER NOT NULL REFERENCES product_stage_entries(id),
                    action TEXT NOT NULL,
                    previous_payload JSON NOT NULL DEFAULT '{}',
                    current_payload JSON NOT NULL DEFAULT '{}',
                    note TEXT,
                    source_document_id INTEGER REFERENCES documents(id),
                    created_at DATETIME NOT NULL
                )
                """
            )
        )
        conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_product_stage_history_entry ON product_stage_history(entry_id)"))
        conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_product_stage_history_document ON product_stage_history(source_document_id)"))

        conn.execute(
            sql_text(
                """
                CREATE TABLE IF NOT EXISTS product_stage_claims (
                    id INTEGER PRIMARY KEY,
                    entry_id INTEGER NOT NULL REFERENCES product_stage_entries(id),
                    claim_key TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    target_date DATE,
                    date_precision TEXT,
                    date_text TEXT,
                    certainty TEXT NOT NULL DEFAULT 'attributed',
                    evidence_tier TEXT,
                    provenance_summary TEXT,
                    summary TEXT,
                    rationale TEXT,
                    review_status TEXT NOT NULL DEFAULT 'draft',
                    source_document_id INTEGER REFERENCES documents(id),
                    source_date DATETIME,
                    linked_dataset_ids JSON NOT NULL DEFAULT '[]',
                    linked_supply_chain_ids JSON NOT NULL DEFAULT '[]',
                    linked_note_ids JSON NOT NULL DEFAULT '[]',
                    linked_thesis_ids JSON NOT NULL DEFAULT '[]',
                    automation_source TEXT,
                    source_kind TEXT,
                    extra JSON NOT NULL DEFAULT '{}',
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    reviewed_at DATETIME,
                    UNIQUE(claim_key)
                )
                """
            )
        )
        conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_product_stage_claims_entry ON product_stage_claims(entry_id)"))
        conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_product_stage_claims_stage ON product_stage_claims(stage)"))
        conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_product_stage_claims_review_status ON product_stage_claims(review_status)"))
        conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_product_stage_claims_evidence_tier ON product_stage_claims(evidence_tier)"))
        conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_product_stage_claims_source_date ON product_stage_claims(source_date)"))
        conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_product_stage_claims_source_document ON product_stage_claims(source_document_id)"))


def _product_stage_tracker_key(company_name: str | None, product_name: str | None) -> str:
    def _slug(value: str | None, *, default: str) -> str:
        cleaned = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
        return cleaned or default

    return f"{_slug(company_name, default='company')}:{_slug(product_name, default='product')}"


def _backfill_product_stage_v2(engine: Engine) -> None:
    from thoth_terminal.product_stage_service import rebuild_product_stage_entry_state
    from thoth_terminal.models import ProductStageClaim, ProductStageEntry, ProductStageHistory

    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    try:
        entries = (
            session.query(ProductStageEntry)
            .order_by(ProductStageEntry.updated_at.asc(), ProductStageEntry.id.asc())
            .all()
        )
        if not entries:
            return

        grouped: dict[str, list[ProductStageEntry]] = {}
        for row in entries:
            tracker_key = _product_stage_tracker_key(row.company_name, row.product_name)
            grouped.setdefault(tracker_key, []).append(row)

        for tracker_key, group in grouped.items():
            keeper = sorted(
                group,
                key=lambda row: (
                    row.updated_at.isoformat() if row.updated_at else "",
                    int(row.id or 0),
                ),
                reverse=True,
            )[0]
            keeper.tracker_key = tracker_key

            merged_dataset_ids = {
                int(value)
                for item in group
                for value in (item.linked_dataset_ids or [])
                if str(value).isdigit()
            }
            merged_chain_ids = {
                int(value)
                for item in group
                for value in (item.linked_supply_chain_ids or [])
                if str(value).isdigit()
            }
            merged_note_ids = {
                int(value)
                for item in group
                for value in (item.linked_note_ids or [])
                if str(value).isdigit()
            }
            merged_thesis_ids = {
                int(value)
                for item in group
                for value in (item.linked_thesis_ids or [])
                if str(value).isdigit()
            }
            keeper.linked_dataset_ids = sorted(merged_dataset_ids)
            keeper.linked_supply_chain_ids = sorted(merged_chain_ids)
            keeper.linked_note_ids = sorted(merged_note_ids)
            keeper.linked_thesis_ids = sorted(merged_thesis_ids)

            for row in sorted(group, key=lambda item: (item.updated_at, item.id)):
                claim_key = f"legacy-stage:{int(row.id)}"
                claim = session.query(ProductStageClaim).filter_by(claim_key=claim_key).first()
                if claim is None:
                    claim = ProductStageClaim(
                        entry_id=int(keeper.id),
                        claim_key=claim_key,
                        stage=row.stage,
                        target_date=row.target_date,
                        date_precision=row.date_precision,
                        date_text=row.date_text,
                        certainty=row.certainty,
                        evidence_tier=row.evidence_tier,
                        provenance_summary="Backfilled from legacy ProductStageEntry state.",
                        summary=row.notes,
                        rationale="Legacy stage row imported into Product Stage Tracker V2.",
                        review_status="accepted" if int(row.id) == int(keeper.id) else "superseded",
                        source_document_id=None,
                        source_date=row.updated_at,
                        linked_dataset_ids=list(row.linked_dataset_ids or []),
                        linked_supply_chain_ids=list(row.linked_supply_chain_ids or []),
                        linked_note_ids=list(row.linked_note_ids or []),
                        linked_thesis_ids=list(row.linked_thesis_ids or []),
                        automation_source="migration_v2",
                        source_kind="legacy_product_stage_entry",
                        extra={"legacy_entry_id": int(row.id)},
                        created_at=row.created_at,
                        updated_at=row.updated_at,
                        reviewed_at=row.updated_at if int(row.id) == int(keeper.id) else None,
                    )
                    session.add(claim)
                elif int(claim.entry_id) != int(keeper.id):
                    claim.entry_id = int(keeper.id)

                if int(row.id) != int(keeper.id):
                    session.query(ProductStageHistory).filter_by(entry_id=int(row.id)).update(
                        {"entry_id": int(keeper.id)},
                        synchronize_session=False,
                    )

            session.flush()
            rebuild_product_stage_entry_state(session, keeper)
            session.flush()

            for row in group:
                if int(row.id) == int(keeper.id):
                    continue
                session.delete(row)

        session.flush()
        session.execute(
            sql_text(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_product_stage_entries_tracker_key_unique "
                "ON product_stage_entries(tracker_key) WHERE tracker_key IS NOT NULL"
            )
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _ensure_research_knowledge_schema(engine: Engine) -> None:
    with engine.begin() as conn:
        def _table_exists(name: str) -> bool:
            return bool(
                conn.execute(
                    sql_text(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=:name LIMIT 1"
                    ),
                    {"name": name},
                ).scalar()
            )

        def _cols(table: str) -> set[str]:
            try:
                rows = conn.execute(sql_text(f"PRAGMA table_info({table})")).fetchall()
            except Exception:
                return set()
            return {row[1] for row in rows if len(row) > 1}

        def _add_column(table: str, *, name: str, ddl: str) -> None:
            if not _table_exists(table):
                return
            if name in _cols(table):
                return
            conn.execute(sql_text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))

        _add_column("theses", name="last_stale_check_at", ddl="last_stale_check_at DATETIME")
        _add_column("theses", name="stale_check_state", ddl="stale_check_state TEXT")

        _add_column("notes", name="stale_after_days", ddl="stale_after_days INTEGER")
        _add_column("notes", name="last_reviewed_at", ddl="last_reviewed_at DATETIME")
        _add_column("notes", name="last_stale_check_at", ddl="last_stale_check_at DATETIME")
        _add_column("notes", name="stale_check_state", ddl="stale_check_state TEXT")

        conn.execute(
            sql_text(
                """
                CREATE TABLE IF NOT EXISTS research_subjects (
                    id INTEGER PRIMARY KEY,
                    slug TEXT NOT NULL,
                    subject_key TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    legal_name TEXT,
                    description TEXT,
                    status TEXT NOT NULL DEFAULT 'active',
                    issuer_id INTEGER REFERENCES issuers(id),
                    company_id INTEGER REFERENCES companies(id),
                    primary_entity_id INTEGER REFERENCES entities(id),
                    extra JSON NOT NULL DEFAULT '{}',
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                )
                """
            )
        )
        conn.execute(sql_text("CREATE UNIQUE INDEX IF NOT EXISTS idx_research_subjects_slug_unique ON research_subjects(slug)"))
        conn.execute(sql_text("CREATE UNIQUE INDEX IF NOT EXISTS idx_research_subjects_subject_key_unique ON research_subjects(subject_key)"))
        conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_research_subjects_kind ON research_subjects(kind)"))
        conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_research_subjects_issuer ON research_subjects(issuer_id)"))
        conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_research_subjects_company ON research_subjects(company_id)"))
        conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_research_subjects_entity ON research_subjects(primary_entity_id)"))

        conn.execute(
            sql_text(
                """
                CREATE TABLE IF NOT EXISTS research_subject_aliases (
                    id INTEGER PRIMARY KEY,
                    subject_id INTEGER NOT NULL REFERENCES research_subjects(id),
                    alias TEXT NOT NULL,
                    alias_type TEXT,
                    created_at DATETIME NOT NULL
                )
                """
            )
        )
        conn.execute(
            sql_text(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_research_subject_aliases_subject_alias_unique "
                "ON research_subject_aliases(subject_id, alias)"
            )
        )
        conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_research_subject_aliases_alias ON research_subject_aliases(alias)"))

        conn.execute(
            sql_text(
                """
                CREATE TABLE IF NOT EXISTS research_subject_relations (
                    id INTEGER PRIMARY KEY,
                    source_subject_id INTEGER NOT NULL REFERENCES research_subjects(id),
                    target_subject_id INTEGER NOT NULL REFERENCES research_subjects(id),
                    relation_kind TEXT NOT NULL,
                    confidence REAL,
                    reviewed BOOLEAN NOT NULL DEFAULT 1,
                    note TEXT,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                )
                """
            )
        )
        conn.execute(
            sql_text(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_research_subject_relations_pair_kind_unique "
                "ON research_subject_relations(source_subject_id, target_subject_id, relation_kind)"
            )
        )
        conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_research_subject_relations_source ON research_subject_relations(source_subject_id)"))
        conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_research_subject_relations_target ON research_subject_relations(target_subject_id)"))
        conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_research_subject_relations_kind ON research_subject_relations(relation_kind)"))

        _add_column("research_reports", name="subject_id", ddl="subject_id INTEGER REFERENCES research_subjects(id)")
        conn.execute(
            sql_text(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_research_reports_subject_id_unique "
                "ON research_reports(subject_id) WHERE subject_id IS NOT NULL"
            )
        )

        conn.execute(
            sql_text(
                """
                CREATE TABLE IF NOT EXISTS knowledge_assets (
                    id INTEGER PRIMARY KEY,
                    subject_id INTEGER NOT NULL REFERENCES research_subjects(id),
                    slug TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT,
                    status TEXT NOT NULL DEFAULT 'active',
                    stale_after_days INTEGER,
                    latest_published_version_id INTEGER,
                    last_reviewed_at DATETIME,
                    last_stale_check_at DATETIME,
                    stale_check_state TEXT,
                    extra JSON NOT NULL DEFAULT '{}',
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                )
                """
            )
        )
        conn.execute(
            sql_text(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_knowledge_assets_subject_kind_unique "
                "ON knowledge_assets(subject_id, kind)"
            )
        )
        conn.execute(sql_text("CREATE UNIQUE INDEX IF NOT EXISTS idx_knowledge_assets_slug_unique ON knowledge_assets(slug)"))
        conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_knowledge_assets_status ON knowledge_assets(status)"))

        conn.execute(
            sql_text(
                """
                CREATE TABLE IF NOT EXISTS knowledge_asset_versions (
                    id INTEGER PRIMARY KEY,
                    asset_id INTEGER NOT NULL REFERENCES knowledge_assets(id),
                    version INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'draft',
                    title TEXT NOT NULL,
                    summary TEXT,
                    content_md TEXT NOT NULL DEFAULT '',
                    content_json JSON NOT NULL DEFAULT '{}',
                    coverage_json JSON NOT NULL DEFAULT '{}',
                    evidence_bundle JSON NOT NULL DEFAULT '[]',
                    ai_metadata JSON NOT NULL DEFAULT '{}',
                    as_of_at DATETIME NOT NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    published_at DATETIME
                )
                """
            )
        )
        conn.execute(
            sql_text(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_knowledge_asset_versions_asset_version_unique "
                "ON knowledge_asset_versions(asset_id, version)"
            )
        )
        conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_knowledge_asset_versions_asset ON knowledge_asset_versions(asset_id)"))
        conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_knowledge_asset_versions_status ON knowledge_asset_versions(status)"))

        conn.execute(
            sql_text(
                """
                CREATE TABLE IF NOT EXISTS knowledge_asset_links (
                    id INTEGER PRIMARY KEY,
                    asset_id INTEGER NOT NULL REFERENCES knowledge_assets(id),
                    object_type TEXT NOT NULL,
                    object_id INTEGER NOT NULL,
                    relation TEXT NOT NULL DEFAULT 'references',
                    created_at DATETIME NOT NULL
                )
                """
            )
        )
        conn.execute(
            sql_text(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_knowledge_asset_links_asset_object_relation_unique "
                "ON knowledge_asset_links(asset_id, object_type, object_id, relation)"
            )
        )
        conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_knowledge_asset_links_object ON knowledge_asset_links(object_type, object_id)"))

        conn.execute(
            sql_text(
                """
                CREATE TABLE IF NOT EXISTS freshness_reviews (
                    id INTEGER PRIMARY KEY,
                    object_type TEXT NOT NULL,
                    object_id INTEGER NOT NULL,
                    subject_id INTEGER REFERENCES research_subjects(id),
                    disposition TEXT NOT NULL,
                    trigger_kind TEXT,
                    rationale TEXT,
                    summary TEXT,
                    evidence_bundle JSON NOT NULL DEFAULT '[]',
                    payload JSON NOT NULL DEFAULT '{}',
                    model TEXT,
                    live_web_used BOOLEAN NOT NULL DEFAULT 0,
                    created_at DATETIME NOT NULL,
                    checked_at DATETIME NOT NULL
                )
                """
            )
        )
        conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_freshness_reviews_object ON freshness_reviews(object_type, object_id)"))
        conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_freshness_reviews_subject ON freshness_reviews(subject_id)"))
        conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_freshness_reviews_disposition ON freshness_reviews(disposition)"))
        conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_freshness_reviews_checked_at ON freshness_reviews(checked_at)"))


def _ensure_bottleneck_schema(engine: Engine) -> None:
    backend = database_backend(str(engine.url))
    with engine.begin() as conn:
        def _table_exists(name: str) -> bool:
            if backend == "postgresql":
                return bool(conn.execute(sql_text("SELECT to_regclass(:name)"), {"name": name}).scalar())
            return bool(
                conn.execute(
                    sql_text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:name LIMIT 1"),
                    {"name": name},
                ).scalar()
            )

        def _cols(table: str) -> set[str]:
            if not _table_exists(table):
                return set()
            if backend == "postgresql":
                rows = conn.execute(
                    sql_text(
                        """
                        SELECT column_name
                        FROM information_schema.columns
                        WHERE table_schema = current_schema()
                          AND table_name = :table
                        """
                    ),
                    {"table": table},
                ).fetchall()
                return {str(row[0]) for row in rows}
            rows = conn.execute(sql_text(f"PRAGMA table_info({table})")).fetchall()
            return {row[1] for row in rows if len(row) > 1}

        def _add_column(table: str, *, name: str, sqlite_ddl: str, postgres_ddl: str | None = None) -> None:
            if name in _cols(table):
                return
            ddl = postgres_ddl if backend == "postgresql" and postgres_ddl is not None else sqlite_ddl
            conn.execute(sql_text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))

        if not _table_exists("bottleneck_assessments"):
            return

        _add_column("bottleneck_assessments", name="model_id", sqlite_ddl="model_id INTEGER")
        _add_column("bottleneck_assessments", name="supply_chain_id", sqlite_ddl="supply_chain_id INTEGER")
        _add_column("bottleneck_assessments", name="snapshot_id", sqlite_ddl="snapshot_id INTEGER")
        _add_column("bottleneck_assessments", name="scope_type", sqlite_ddl="scope_type TEXT")
        _add_column("bottleneck_assessments", name="scope_key", sqlite_ddl="scope_key TEXT")
        _add_column("bottleneck_assessments", name="constrained_input", sqlite_ddl="constrained_input TEXT")
        _add_column("bottleneck_assessments", name="output_unit", sqlite_ddl="output_unit TEXT")
        _add_column("bottleneck_assessments", name="regime", sqlite_ddl="regime TEXT")
        _add_column("bottleneck_assessments", name="bindingness_score", sqlite_ddl="bindingness_score REAL", postgres_ddl="bindingness_score DOUBLE PRECISION")
        _add_column("bottleneck_assessments", name="shadow_price_score", sqlite_ddl="shadow_price_score REAL", postgres_ddl="shadow_price_score DOUBLE PRECISION")
        _add_column("bottleneck_assessments", name="ramp_resistance_score", sqlite_ddl="ramp_resistance_score REAL", postgres_ddl="ramp_resistance_score DOUBLE PRECISION")
        _add_column("bottleneck_assessments", name="substitutability_score", sqlite_ddl="substitutability_score REAL", postgres_ddl="substitutability_score DOUBLE PRECISION")
        _add_column("bottleneck_assessments", name="persistence_score", sqlite_ddl="persistence_score REAL", postgres_ddl="persistence_score DOUBLE PRECISION")
        _add_column("bottleneck_assessments", name="latent_demand_absorption_score", sqlite_ddl="latent_demand_absorption_score REAL", postgres_ddl="latent_demand_absorption_score DOUBLE PRECISION")
        _add_column("bottleneck_assessments", name="qualified_supplier_count", sqlite_ddl="qualified_supplier_count INTEGER")
        _add_column("bottleneck_assessments", name="marginal_system_leverage", sqlite_ddl="marginal_system_leverage JSON NOT NULL DEFAULT '{}'", postgres_ddl="marginal_system_leverage JSON NOT NULL DEFAULT '{}'")
        _add_column("bottleneck_assessments", name="supplier_qualification_json", sqlite_ddl="supplier_qualification_json JSON NOT NULL DEFAULT '{}'", postgres_ddl="supplier_qualification_json JSON NOT NULL DEFAULT '{}'")
        _add_column("bottleneck_assessments", name="evidence_json", sqlite_ddl="evidence_json JSON NOT NULL DEFAULT '[]'", postgres_ddl="evidence_json JSON NOT NULL DEFAULT '[]'")
        _add_column("bottleneck_assessments", name="scorecard_json", sqlite_ddl="scorecard_json JSON NOT NULL DEFAULT '{}'", postgres_ddl="scorecard_json JSON NOT NULL DEFAULT '{}'")

        if _table_exists("bottleneck_models"):
            conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_bottleneck_models_scope ON bottleneck_models(scope_type, scope_key)"))
            conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_bottleneck_models_supply_chain ON bottleneck_models(supply_chain_id)"))
            conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_bottleneck_models_regime ON bottleneck_models(regime)"))
        conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_bottleneck_assessments_model ON bottleneck_assessments(model_id)"))
        conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_bottleneck_assessments_supply_chain ON bottleneck_assessments(supply_chain_id)"))
        conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_bottleneck_assessments_scope ON bottleneck_assessments(scope_type, scope_key)"))
        conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_bottleneck_assessments_regime ON bottleneck_assessments(regime)"))


def _ensure_earnings_period_schema(engine: Engine) -> None:
    """Add durable fiscal/calendar period labels to legacy earnings events."""
    inspector = inspect(engine)
    if not inspector.has_table("earnings_events"):
        return
    existing = {column["name"] for column in inspector.get_columns("earnings_events")}
    additions = {
        "period_start": "DATE",
        "calendar_quarter": "VARCHAR(8)",
        "issuer_fiscal_year_label": "VARCHAR(32)",
        "vendor_fiscal_label": "VARCHAR(32)",
        "source_label_raw": "TEXT",
    }
    with engine.begin() as conn:
        for column, definition in additions.items():
            if column not in existing:
                conn.execute(sql_text(f"ALTER TABLE earnings_events ADD COLUMN {column} {definition}"))
        statements = (
            "CREATE INDEX IF NOT EXISTS idx_earnings_events_period_end ON earnings_events(period_end)",
            "CREATE INDEX IF NOT EXISTS idx_earnings_events_calendar_quarter ON earnings_events(calendar_quarter)",
            "CREATE INDEX IF NOT EXISTS idx_earnings_events_period_labels ON earnings_events(issuer_fiscal_year_label, vendor_fiscal_label)",
        )
        for statement in statements:
            try:
                conn.execute(sql_text(statement))
            except Exception:
                pass


def _backfill_research_subjects(engine: Engine) -> None:
    from thoth_terminal.models import (
        Company,
        Entity,
        EntityAlias,
        Issuer,
        IssuerIdentifier,
        ResearchReport,
        ResearchSubject,
        ResearchSubjectAlias,
    )

    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    try:
        now = datetime.now(UTC).replace(tzinfo=None)

        def _slugify_local(value: str | None, *, default: str) -> str:
            cleaned = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
            return cleaned or default

        def _upsert_alias(subject_id: int, alias: str | None, alias_type: str | None) -> None:
            alias_v = str(alias or "").strip()
            if not alias_v:
                return
            row = (
                session.query(ResearchSubjectAlias)
                .filter_by(subject_id=int(subject_id), alias=alias_v)
                .first()
            )
            if row is None:
                session.add(
                    ResearchSubjectAlias(
                        subject_id=int(subject_id),
                        alias=alias_v,
                        alias_type=alias_type,
                        created_at=now,
                    )
                )

        def _ensure_subject(
            *,
            subject_key: str,
            slug: str,
            kind: str,
            display_name: str,
            legal_name: str | None = None,
            description: str | None = None,
            issuer_id: int | None = None,
            company_id: int | None = None,
            primary_entity_id: int | None = None,
            aliases: list[tuple[str, str | None]] | None = None,
        ) -> ResearchSubject:
            row = session.query(ResearchSubject).filter_by(subject_key=subject_key).first()
            if row is None:
                row = ResearchSubject(
                    slug=slug,
                    subject_key=subject_key,
                    kind=kind,
                    display_name=display_name,
                    legal_name=legal_name,
                    description=description,
                    status="active",
                    issuer_id=issuer_id,
                    company_id=company_id,
                    primary_entity_id=primary_entity_id,
                    extra={},
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                session.flush()
            else:
                row.slug = slug or row.slug
                row.kind = kind or row.kind
                row.display_name = display_name or row.display_name
                row.legal_name = legal_name if legal_name is not None else row.legal_name
                row.description = description if description is not None else row.description
                row.issuer_id = issuer_id if issuer_id is not None else row.issuer_id
                row.company_id = company_id if company_id is not None else row.company_id
                row.primary_entity_id = primary_entity_id if primary_entity_id is not None else row.primary_entity_id
                row.updated_at = now
                session.flush()
            for alias_v, alias_type in aliases or []:
                _upsert_alias(int(row.id), alias_v, alias_type)
            return row

        company_reports = session.query(ResearchReport).order_by(ResearchReport.id.asc()).all()
        for report in company_reports:
            issuer = session.query(Issuer).filter_by(id=int(report.issuer_id)).first() if report.issuer_id is not None else None
            company = session.query(Company).filter_by(id=int(report.company_id)).first() if report.company_id is not None else None
            display_name = (
                str((issuer.display_name if issuer else "") or "").strip()
                or str((company.name if company else "") or "").strip()
                or str(report.title or "").replace("Research Report — ", "", 1).strip()
                or str(report.slug or "").strip()
                or f"report-{int(report.id)}"
            )
            slug = (
                str(report.latest_subject_slug or "").strip()
                or str(report.slug or "").strip()
                or str((issuer.slug if issuer else "") or "").strip()
                or str((company.slug if company else "") or "").strip()
                or _slugify_local(display_name, default=f"report-{int(report.id)}")
            )
            subject_key = str(report.subject_key or "").strip() or (
                f"issuer:{int(report.issuer_id)}"
                if report.issuer_id is not None
                else f"company:{int(report.company_id)}"
            )
            alias_rows: list[tuple[str, str | None]] = [(display_name, "name"), (slug, "slug")]
            if issuer and issuer.legal_name:
                alias_rows.append((issuer.legal_name, "legal_name"))
            if company and company.name:
                alias_rows.append((company.name, "company_name"))
            if company and company.ticker:
                alias_rows.append((company.ticker.upper(), "ticker"))
                alias_rows.append((f"${company.ticker.upper()}", "ticker"))
            if report.subject_key:
                alias_rows.append((report.subject_key, "subject_key"))
            if issuer is not None and issuer.id is not None:
                ticker_rows = (
                    session.query(IssuerIdentifier)
                    .filter(IssuerIdentifier.issuer_id == int(issuer.id), IssuerIdentifier.id_type == "TICKER")
                    .order_by(IssuerIdentifier.is_primary.desc(), IssuerIdentifier.id.asc())
                    .all()
                )
                for ident in ticker_rows:
                    ticker = str(ident.id_value or "").strip().upper()
                    if ticker:
                        alias_rows.append((ticker, "ticker"))
                        alias_rows.append((f"${ticker}", "ticker"))
            subject_row = _ensure_subject(
                subject_key=subject_key,
                slug=slug,
                kind="company",
                display_name=display_name,
                legal_name=str((issuer.legal_name if issuer else "") or "").strip() or None,
                description=None,
                issuer_id=int(issuer.id) if issuer is not None else None,
                company_id=int(company.id) if company is not None else None,
                primary_entity_id=None,
                aliases=alias_rows,
            )
            report.subject_id = int(subject_row.id)
            report.latest_subject_slug = subject_row.slug

        entities = (
            session.query(Entity)
            .filter(Entity.kind.in_(["technology", "component", "theme", "concept", "process", "domain", "product"]))
            .order_by(Entity.id.asc())
            .all()
        )
        for entity in entities:
            display_name = str(entity.canonical_name or "").strip()
            if not display_name:
                continue
            kind_map = {
                "technology": "technology",
                "process": "technology",
                "component": "component",
                "product": "component",
                "theme": "theme",
                "concept": "theme",
                "domain": "theme",
            }
            subject_kind = kind_map.get(str(entity.kind or "").strip().lower(), "theme")
            slug = _slugify_local(display_name, default=f"subject-{int(entity.id)}")
            aliases = [(display_name, "entity"), (slug, "slug")]
            alias_rows = (
                session.query(EntityAlias)
                .filter(EntityAlias.entity_id == int(entity.id))
                .order_by(EntityAlias.id.asc())
                .all()
            )
            for alias in alias_rows:
                aliases.append((str(alias.alias or "").strip(), "entity_alias"))
            _ensure_subject(
                subject_key=f"entity:{int(entity.id)}",
                slug=slug,
                kind=subject_kind,
                display_name=display_name,
                legal_name=None,
                description=str(entity.description or "").strip() or None,
                primary_entity_id=int(entity.id),
                aliases=aliases,
            )

        session.flush()
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _ensure_jobs_indexes(engine: Engine) -> None:
    if database_backend(str(engine.url)) == "postgresql":
        with engine.begin() as conn:
            conn.execute(
                sql_text(
                    "CREATE INDEX IF NOT EXISTS idx_jobs_actionable_status_kind_priority_created "
                    "ON jobs(status, kind, priority, created_at) "
                    "WHERE status IN ('pending', 'running', 'error')"
                )
            )
            conn.execute(
                sql_text(
                    "CREATE INDEX IF NOT EXISTS idx_jobs_running_locked_at "
                    "ON jobs(locked_at) WHERE status = 'running'"
                )
            )
            conn.execute(
                sql_text(
                    "CREATE INDEX IF NOT EXISTS idx_jobs_open_dedupe_key_status "
                    "ON jobs(dedupe_key, status) "
                    "WHERE status IN ('pending', 'running') AND dedupe_key IS NOT NULL"
                )
            )
            conn.execute(
                sql_text(
                    "CREATE INDEX IF NOT EXISTS idx_jobs_cloud_history_dedupe_key "
                    "ON jobs(dedupe_key) "
                    "WHERE kind = 'cloud_research_automation' AND dedupe_key IS NOT NULL"
                )
            )
            conn.execute(sql_text("DROP INDEX IF EXISTS idx_jobs_status_run_after_priority_created"))
            conn.execute(sql_text("DROP INDEX IF EXISTS idx_jobs_status_locked_at"))
            conn.execute(sql_text("DROP INDEX IF EXISTS idx_jobs_dedupe_key_status"))
        return
    with engine.begin() as conn:
        conn.execute(
            sql_text(
                "CREATE INDEX IF NOT EXISTS idx_jobs_status_run_after_priority_created "
                "ON jobs(status, run_after, priority, created_at)"
            )
        )
        conn.execute(
            sql_text(
                "CREATE INDEX IF NOT EXISTS idx_jobs_status_locked_at "
                "ON jobs(status, locked_at)"
            )
        )
        conn.execute(
            sql_text(
                "CREATE INDEX IF NOT EXISTS idx_jobs_dedupe_key_status "
                "ON jobs(dedupe_key, status)"
            )
        )


def _execute_best_effort_index_statements(engine: Engine, statements: list[str] | tuple[str, ...]) -> None:
    backend = database_backend(str(engine.url))
    for statement in statements:
        try:
            with engine.begin() as conn:
                if backend == "postgresql":
                    try:
                        conn.execute(sql_text("SET LOCAL lock_timeout = '5000ms'"))
                    except Exception:
                        pass
                conn.execute(sql_text(statement))
        except Exception:
            pass


def _ensure_local_agent_schema(engine: Engine) -> None:
    inspector = inspect(engine)
    local_agent_index_statements: list[str] = []
    if inspector.has_table("local_agent_entity_routes"):
        existing = {column["name"] for column in inspector.get_columns("local_agent_entity_routes")}
        additions = {
            "temporal_posture": "VARCHAR(32)",
            "evidence_tier": "VARCHAR(32)",
            "new_information": "TEXT",
        }
        with engine.begin() as conn:
            for column, definition in additions.items():
                if column not in existing:
                    conn.execute(sql_text(f"ALTER TABLE local_agent_entity_routes ADD COLUMN {column} {definition}"))
            for column in ("temporal_posture", "evidence_tier"):
                try:
                    conn.execute(sql_text(f"CREATE INDEX IF NOT EXISTS idx_local_agent_entity_routes_{column} ON local_agent_entity_routes({column})"))
                except Exception:
                    pass
        local_agent_index_statements.extend(
            [
                "CREATE INDEX IF NOT EXISTS idx_local_agent_entity_routes_document_created ON local_agent_entity_routes(document_id, created_at)",
                "CREATE INDEX IF NOT EXISTS idx_local_agent_entity_routes_run_created ON local_agent_entity_routes(run_id, created_at)",
            ]
        )
    if inspector.has_table("local_agent_document_compressions"):
        existing = {column["name"] for column in inspector.get_columns("local_agent_document_compressions")}
        additions = {
            "information_gain": "VARCHAR(32)",
            "document_disposition": "VARCHAR(32)",
            "skip_reason": "TEXT",
        }
        with engine.begin() as conn:
            for column, definition in additions.items():
                if column not in existing:
                    conn.execute(sql_text(f"ALTER TABLE local_agent_document_compressions ADD COLUMN {column} {definition}"))
            for column in ("information_gain", "document_disposition"):
                try:
                    conn.execute(sql_text(f"CREATE INDEX IF NOT EXISTS idx_local_agent_document_compressions_{column} ON local_agent_document_compressions({column})"))
                except Exception:
                    pass
        local_agent_index_statements.extend(
            [
                "CREATE INDEX IF NOT EXISTS idx_local_agent_compressions_status_document ON local_agent_document_compressions(status, document_id)",
                "CREATE INDEX IF NOT EXISTS idx_local_agent_compressions_created_document ON local_agent_document_compressions(created_at, document_id)",
                "CREATE INDEX IF NOT EXISTS idx_local_agent_compressions_run_created ON local_agent_document_compressions(run_id, created_at)",
            ]
        )
    if inspector.has_table("local_agent_deep_analysis_traces"):
        statements = (
            "CREATE INDEX IF NOT EXISTS idx_local_agent_deep_traces_compression ON local_agent_deep_analysis_traces(compression_id)",
            "CREATE INDEX IF NOT EXISTS idx_local_agent_deep_traces_document ON local_agent_deep_analysis_traces(document_id)",
            "CREATE INDEX IF NOT EXISTS idx_local_agent_deep_traces_status_created ON local_agent_deep_analysis_traces(status, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_local_agent_deep_traces_signal ON local_agent_deep_analysis_traces(signal_level)",
        )
        with engine.begin() as conn:
            for statement in statements:
                try:
                    conn.execute(sql_text(statement))
                except Exception:
                    pass
    if inspector.has_table("local_agent_actions"):
        local_agent_index_statements.extend(
            [
                "CREATE INDEX IF NOT EXISTS idx_local_agent_actions_run_created ON local_agent_actions(run_id, created_at)",
                "CREATE INDEX IF NOT EXISTS idx_local_agent_actions_status_created ON local_agent_actions(status, created_at)",
                "CREATE INDEX IF NOT EXISTS idx_local_agent_actions_document_created ON local_agent_actions(document_id, created_at)",
            ]
        )
    if inspector.has_table("local_agent_entity_claims"):
        local_agent_index_statements.extend(
            [
                "CREATE INDEX IF NOT EXISTS idx_local_agent_claims_document_kind ON local_agent_entity_claims(document_id, entity_id, claim_kind)",
                "CREATE INDEX IF NOT EXISTS idx_local_agent_claims_compression_kind ON local_agent_entity_claims(compression_id, entity_id, claim_kind)",
                "CREATE INDEX IF NOT EXISTS idx_local_agent_claims_run_created ON local_agent_entity_claims(run_id, created_at)",
            ]
        )
    if inspector.has_table("local_agent_unresolved_entities"):
        local_agent_index_statements.append(
            "CREATE INDEX IF NOT EXISTS idx_local_agent_unresolved_document_status ON local_agent_unresolved_entities(document_id, status)"
        )
    if inspector.has_table("local_agent_write_diffs"):
        local_agent_index_statements.extend(
            [
                "CREATE INDEX IF NOT EXISTS idx_local_agent_write_diffs_run_created ON local_agent_write_diffs(run_id, created_at)",
                "CREATE INDEX IF NOT EXISTS idx_local_agent_write_diffs_destination_created ON local_agent_write_diffs(destination_type, destination_id, created_at)",
            ]
        )
    if inspector.has_table("documents"):
        local_agent_index_statements.extend(
            [
                "CREATE INDEX IF NOT EXISTS idx_documents_agent_queue_published ON documents(triage_state, kind, importance, published_at)",
                "CREATE INDEX IF NOT EXISTS idx_documents_agent_queue_ingested ON documents(triage_state, kind, importance, ingested_at)",
            ]
        )
    if local_agent_index_statements:
        _execute_best_effort_index_statements(engine, local_agent_index_statements)


def _ensure_house_view_schema(engine: Engine) -> None:
    """Indexes for house-view theses/questions that live on existing rails."""
    statements = (
        "CREATE INDEX IF NOT EXISTS idx_theses_status_updated ON theses(status, updated_at)",
        "CREATE INDEX IF NOT EXISTS idx_theses_kind_status_updated ON theses(kind, status, updated_at)",
        "CREATE INDEX IF NOT EXISTS idx_theses_review_state ON theses(status, last_reviewed_at)",
        "CREATE INDEX IF NOT EXISTS idx_notes_type_status_updated ON notes(note_type, status, updated_at)",
        "CREATE INDEX IF NOT EXISTS idx_thesis_proposals_status_updated ON thesis_proposals(status, updated_at)",
        "CREATE INDEX IF NOT EXISTS idx_thesis_proposals_kind_status ON thesis_proposals(kind, status)",
        "CREATE INDEX IF NOT EXISTS idx_instrument_requests_intent_source_status ON instrument_requests(intent, source_kind, status)",
        "CREATE INDEX IF NOT EXISTS idx_instrument_requests_source_updated ON instrument_requests(source_kind, updated_at)",
    )
    with engine.begin() as conn:
        for statement in statements:
            try:
                conn.execute(sql_text(statement))
            except Exception:
                pass


def _ensure_source_identity_schema(engine: Engine) -> None:
    """Indexes and additive columns for source actor/affiliation attribution."""
    inspector = inspect(engine)
    with engine.begin() as conn:
        if inspector.has_table("analyst_evidence"):
            existing = {column["name"] for column in inspector.get_columns("analyst_evidence")}
            additions = {
                "observed_source_actor_id": "INTEGER",
                "underlying_source_actor_id": "INTEGER",
                "source_identity_status": "VARCHAR(64)",
                "source_side": "VARCHAR(64)",
                "source_role_class": "VARCHAR(64)",
            }
            for column, definition in additions.items():
                if column not in existing:
                    conn.execute(sql_text(f"ALTER TABLE analyst_evidence ADD COLUMN {column} {definition}"))
        statements = (
            "CREATE INDEX IF NOT EXISTS idx_source_actors_type_side ON source_actors(actor_type, side)",
            "CREATE INDEX IF NOT EXISTS idx_source_actors_status_updated ON source_actors(status, updated_at)",
            "CREATE INDEX IF NOT EXISTS idx_source_actor_aliases_lookup ON source_actor_aliases(alias_kind, normalized_alias, platform)",
            "CREATE INDEX IF NOT EXISTS idx_source_affiliations_person_status ON source_affiliations(person_actor_id, status)",
            "CREATE INDEX IF NOT EXISTS idx_source_affiliations_org_status ON source_affiliations(organization_actor_id, status)",
            "CREATE INDEX IF NOT EXISTS idx_source_attribution_edges_evidence_order ON source_attribution_edges(analyst_evidence_id, edge_order)",
            "CREATE INDEX IF NOT EXISTS idx_source_attribution_edges_actor_role ON source_attribution_edges(actor_id, role)",
            "CREATE INDEX IF NOT EXISTS idx_analyst_evidence_observed_actor ON analyst_evidence(observed_source_actor_id)",
            "CREATE INDEX IF NOT EXISTS idx_analyst_evidence_underlying_actor ON analyst_evidence(underlying_source_actor_id)",
            "CREATE INDEX IF NOT EXISTS idx_analyst_evidence_source_identity ON analyst_evidence(source_identity_status, source_side, source_role_class)",
        )
        for statement in statements:
            try:
                conn.execute(sql_text(statement))
            except Exception:
                pass


def _is_postgres_lock_timeout_error(exc: OperationalError) -> bool:
    text = str(exc).lower()
    return (
        "lock timeout" in text
        or "lock_not_available" in text
        or "55p03" in text
        or "deadlock detected" in text
        or "deadlockdetected" in text
        or "40p01" in text
    )


def _postgres_has_startup_lock_blocker(engine: Engine) -> bool:
    try:
        with engine.connect() as conn:
            return bool(
                conn.execute(
                    sql_text(
                        """
                        SELECT 1
                        FROM pg_stat_activity
                        WHERE datname = current_database()
                          AND pid <> pg_backend_pid()
                          AND state = 'idle in transaction'
                          AND now() - xact_start > interval '5 seconds'
                        LIMIT 1
                        """
                    )
                ).scalar()
            )
    except Exception:
        return False


def init_db(*, allow_locked: bool = False) -> None:
    with _INIT_DB_LOCK:
        _init_db_locked(allow_locked=allow_locked)


def _init_db_locked(*, allow_locked: bool = False) -> None:
    engine = get_engine()
    backend = database_backend(str(engine.url))

    # Ensure optional tables (e.g., Discord bot) are registered before create_all().
    import thoth_terminal.discord.models  # noqa: F401

    # Optional startup DB check. Even PRAGMA quick_check can take several seconds
    # on multi-GB SQLite stores, so routine desktop startup skips it by default.
    if backend == "sqlite" and getattr(settings, "startup_db_quick_check", False):
        with engine.connect() as conn:
            try:
                res = conn.execute(sql_text("PRAGMA quick_check;"))
                ok = res.scalar()
                if ok != "ok":
                    raise RuntimeError(f"SQLite quick check failed: {ok}")
            except Exception:
                # Let the caller decide how to recover; keeping the error explicit helps avoid silent corruption.
                raise

    if backend == "postgresql" and allow_locked and _postgres_has_startup_lock_blocker(engine):
        return

    tries = 4
    for attempt in range(tries):
        try:
            Base.metadata.create_all(engine)
            from thoth_terminal.dataset_storage_migration import migrate_dataset_payloads
            migrate_dataset_payloads(engine)
            _ensure_research_canon_schema(engine)
            _ensure_earnings_period_schema(engine)
            _ensure_bottleneck_schema(engine)
            _ensure_house_view_schema(engine)
            _ensure_source_identity_schema(engine)
            if backend == "sqlite":
                _ensure_issuer_schema(engine)
                _ensure_discord_schema(engine)
                _ensure_fts(engine)
                _ensure_triage(engine)
                _ensure_mentions(engine)
                _ensure_institutional_data_schema(engine)
                _ensure_research_knowledge_schema(engine)
            elif backend == "postgresql":
                _ensure_postgres_search_indexes(engine)
            _ensure_jobs_indexes(engine)
            if backend == "sqlite":
                _backfill_product_stage_v2(engine)
                _backfill_research_subjects(engine)
            _ensure_local_agent_schema(engine)
            seed_all(engine)
            return
        except OperationalError as e:
            if backend == "postgresql" and allow_locked and _is_postgres_lock_timeout_error(e):
                return
            if "database is locked" not in str(e).lower():
                raise
            if allow_locked:
                # Another Thoth process likely holds a write lock; skip migrations/startup work.
                return
            if attempt < tries - 1:
                import time

                time.sleep(0.4 * (2**attempt))
                continue
            raise RuntimeError(
                "SQLite database is locked. Close other Thoth processes (or any SQLite browser) using the same DB, "
                "then retry. If you intentionally want a separate instance, set THOTH_DB_PATH to a different file."
            ) from e
