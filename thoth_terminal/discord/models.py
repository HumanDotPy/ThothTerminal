from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    JSON,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from thoth_terminal.models import Base


class DiscordGuildSettings(Base):
    __tablename__ = "discord_guild_settings"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    memo_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    desk_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    desk_thread_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    desk_thread_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    desk_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    outbox_paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    report_permission_level: Mapped[str] = mapped_column(String(16), nullable=False, default="read_only")
    agent_settings_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_by_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)


class DiscordLinkScanState(Base):
    __tablename__ = "discord_link_scan_states"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    last_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_scanned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        Index("idx_discord_link_scan_updated", "guild_id", "updated_at"),
    )


class DiscordLinkInboxItem(Base):
    __tablename__ = "discord_link_inbox_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    author_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    message_created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    url_raw: Mapped[str] = mapped_column(String(2048), nullable=False)
    url_canonical: Mapped[str] = mapped_column(String(2048), nullable=False)
    url_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    message_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    embed_title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    deterministic_ticker: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="collected")
    agentic_decision_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    evidence_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    resolution_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resolution_candidates_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    resolution_request_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    queue_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    seen_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint("guild_id", "url_hash", name="uq_discord_link_inbox_guild_url_hash"),
        Index("idx_discord_link_inbox_status", "guild_id", "status", "updated_at"),
        Index("idx_discord_link_inbox_message", "guild_id", "channel_id", "message_id"),
    )


class DiscordEvidenceItem(Base):
    __tablename__ = "discord_evidence_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    url_raw: Mapped[str] = mapped_column(String(2048), nullable=False)
    url_canonical: Mapped[str] = mapped_column(String(2048), nullable=False)
    url_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    sentiment: Mapped[str | None] = mapped_column(String(16), nullable=True)
    tags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, default="other")
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    resolution_state: Mapped[str] = mapped_column(String(32), nullable=False, default="resolved")
    candidate_targets_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_by_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    discord_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    discord_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    __table_args__ = (
        UniqueConstraint("guild_id", "url_hash", name="uq_discord_evidence_guild_url_hash"),
        Index("idx_discord_evidence_created", "guild_id", "created_at"),
        Index("idx_discord_evidence_pinned", "guild_id", "pinned", "created_at"),
        Index("idx_discord_evidence_resolution", "guild_id", "resolution_state", "updated_at"),
    )


class DiscordEvidenceTarget(Base):
    __tablename__ = "discord_evidence_targets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    evidence_id: Mapped[int] = mapped_column(Integer, nullable=False)
    target_key: Mapped[str] = mapped_column(String(255), nullable=False)
    target_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    target_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    issuer_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    company_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    resolution_method: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    extra_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint("evidence_id", "target_key", name="uq_discord_evidence_target_key"),
        Index("idx_discord_evidence_target_evidence", "evidence_id"),
        Index("idx_discord_evidence_target_symbol", "symbol", "updated_at"),
        Index("idx_discord_evidence_target_issuer", "issuer_id", "updated_at"),
        Index("idx_discord_evidence_target_kind", "target_kind", "updated_at"),
    )


class DiscordWatchlistEntry(Base):
    __tablename__ = "discord_watchlist_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    list_kind: Mapped[str] = mapped_column(String(32), nullable=False)  # exec|community|masterlist
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    sector: Mapped[str] = mapped_column(String(128), nullable=False)
    sector_key: Mapped[str] = mapped_column(String(128), nullable=False)
    subgroup: Mapped[str] = mapped_column(String(128), nullable=False)
    subgroup_key: Mapped[str] = mapped_column(String(128), nullable=False)
    added_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    added_by_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    removed_by_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    baseline_due_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    baseline_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    baseline_currency: Mapped[str | None] = mapped_column(String(16), nullable=True)
    baseline_quote_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    baseline_captured_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_quote_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_quote_currency: Mapped[str | None] = mapped_column(String(16), nullable=True)
    last_quote_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    company_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    exchange: Mapped[str | None] = mapped_column(String(64), nullable=True)
    full_exchange_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_returns: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    last_returns_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "guild_id",
            "list_kind",
            "sector_key",
            "subgroup_key",
            "symbol",
            name="uq_discord_watchlist_guild_kind_sector_subgroup_symbol",
        ),
        Index("idx_discord_watchlist_active", "guild_id", "list_kind", "removed_at"),
        Index("idx_discord_watchlist_sector", "guild_id", "list_kind", "sector_key"),
        Index("idx_discord_watchlist_subgroup", "guild_id", "list_kind", "sector_key", "subgroup_key"),
        Index("idx_discord_watchlist_baseline_due", "guild_id", "baseline_due_at"),
    )


class DiscordWatchlistSector(Base):
    __tablename__ = "discord_watchlist_sectors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    list_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="community")  # community|masterlist
    sector: Mapped[str] = mapped_column(String(128), nullable=False)
    sector_key: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_by_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_by_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    archived_by_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    __table_args__ = (
        UniqueConstraint("guild_id", "list_kind", "sector_key", name="uq_discord_watchlist_sector_guild_kind_key"),
        Index("idx_discord_watchlist_sectors_active", "guild_id", "list_kind", "archived_at"),
        Index("idx_discord_watchlist_sectors_key", "guild_id", "list_kind", "sector_key"),
    )


class DiscordWatchlistSubgroup(Base):
    __tablename__ = "discord_watchlist_subgroups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    list_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="community")  # community|masterlist
    sector_key: Mapped[str] = mapped_column(String(128), nullable=False)
    subgroup: Mapped[str] = mapped_column(String(128), nullable=False)
    subgroup_key: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_by_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_by_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    archived_by_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "guild_id",
            "list_kind",
            "sector_key",
            "subgroup_key",
            name="uq_discord_watchlist_subgroup_guild_kind_sector_key",
        ),
        Index("idx_discord_watchlist_subgroups_active", "guild_id", "list_kind", "sector_key", "archived_at"),
        Index("idx_discord_watchlist_subgroups_key", "guild_id", "list_kind", "sector_key", "subgroup_key"),
    )


class DiscordTickerSource(Base):
    __tablename__ = "discord_ticker_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    sentiment: Mapped[str | None] = mapped_column(String(16), nullable=True)
    tags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, default="other")
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_by_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    discord_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    discord_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    __table_args__ = (
        Index("idx_discord_sources_symbol_created", "guild_id", "symbol", "created_at"),
        Index("idx_discord_sources_symbol_pinned", "guild_id", "symbol", "pinned"),
    )


class DiscordExecIdea(Base):
    __tablename__ = "discord_exec_ideas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    horizon: Mapped[str] = mapped_column(String(16), nullable=False)  # days|weeks|months|years
    horizon_n: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    thesis: Mapped[str] = mapped_column(Text, nullable=False)
    catalysts: Mapped[str | None] = mapped_column(Text, nullable=True)
    invalidation: Mapped[str | None] = mapped_column(Text, nullable=True)
    levels: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")  # active|closed
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    discord_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    discord_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    __table_args__ = (
        Index("idx_discord_exec_ideas_symbol_status", "guild_id", "symbol", "status"),
        Index("idx_discord_exec_ideas_created", "guild_id", "created_at"),
    )


class DiscordQueueItem(Base):
    __tablename__ = "discord_queue_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)  # exec_watchlist_add|source_add
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")  # pending|approved|rejected
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_by_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    decided_by_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("idx_discord_queue_status_created", "guild_id", "status", "created_at"),
    )


class DiscordOutboxItem(Base):
    __tablename__ = "discord_outbox_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    thread_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")  # pending|sending|posted|failed|canceled
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    dedupe_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    embed_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    run_after: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_by_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="agent")  # agent|user|system
    created_by_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    discord_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    __table_args__ = (
        UniqueConstraint("guild_id", "dedupe_key", name="uq_discord_outbox_guild_dedupe"),
        Index("idx_discord_outbox_claim", "guild_id", "status", "run_after", "priority", "created_at"),
        Index("idx_discord_outbox_posted", "guild_id", "posted_at"),
    )
