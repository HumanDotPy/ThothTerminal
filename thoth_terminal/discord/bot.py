from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, TypeVar
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import tasks

from thoth_terminal.config import settings
from thoth_terminal.discord import agent_jobs
from thoth_terminal.discord import desk as desk_mod
from thoth_terminal.discord import link_intake
from thoth_terminal.discord import notes_bridge
from thoth_terminal.discord import outbox as outbox_mod
from thoth_terminal.discord import services
from thoth_terminal.discord.commands.public import PublicCommandDeps, register_public_commands
from thoth_terminal.discord.commands.review import ReviewCommandDeps, register_review_commands
from thoth_terminal.discord.formatting import (
    format_exidea_id,
    format_pct,
    format_queue_id,
    format_src_id,
    render_table,
)
from thoth_terminal.discord.yahoo import YahooFinanceError, search_symbols
from thoth_terminal.runtime_health import write_service_heartbeat
from thoth_terminal.local_model_runtime import local_model_endpoint, local_model_name
from thoth_terminal.runtime_info import get_runtime_identity, runtime_fingerprint

try:
    ET = ZoneInfo("America/New_York")
    ET_LABEL = "ET"
except Exception:
    ET = timezone.utc
    ET_LABEL = "UTC"


logger = logging.getLogger("thoth.discord.bot")


def _require_bot_config() -> tuple[str, int, int]:
    token = settings.discord_token
    guild_id = settings.discord_guild_id
    exec_user_id = settings.discord_exec_user_id
    if not token:
        raise RuntimeError("Missing THOTH_DISCORD_TOKEN.")
    if not guild_id:
        raise RuntimeError("Missing THOTH_DISCORD_GUILD_ID.")
    if not exec_user_id:
        raise RuntimeError("Missing THOTH_DISCORD_EXEC_USER_ID.")
    return str(token), int(guild_id), int(exec_user_id)


def _is_expected_guild(interaction: discord.Interaction, *, guild_id: int) -> bool:
    return bool(interaction.guild_id and int(interaction.guild_id) == int(guild_id))


async def _deny(interaction: discord.Interaction, msg: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


async def _ensure_guild(interaction: discord.Interaction, *, guild_id: int) -> bool:
    if not interaction.guild_id:
        await _deny(interaction, "This command must be used in a server channel.")
        return False
    if not _is_expected_guild(interaction, guild_id=guild_id):
        await _deny(interaction, "Bot not configured for this server.")
        return False
    allowed_channel_id = getattr(settings, "discord_channel_id", None)
    if allowed_channel_id:
        allowed_channel_id = int(allowed_channel_id)
        chan_id = int(interaction.channel_id or 0)
        if chan_id != allowed_channel_id:
            parent_id = getattr(getattr(interaction, "channel", None), "parent_id", None)
            if parent_id is None or int(parent_id) != allowed_channel_id:
                await _deny(interaction, f"Please use this command in <#{allowed_channel_id}>.")
                return False
    return True


async def _ensure_exec(interaction: discord.Interaction, *, exec_user_id: int) -> bool:
    if int(interaction.user.id) != int(exec_user_id):
        await _deny(interaction, "Not authorized.")
        return False
    return True


def _is_exec_user(interaction: discord.Interaction, *, exec_user_id: int) -> bool:
    return int(getattr(getattr(interaction, "user", None), "id", 0) or 0) == int(exec_user_id)


def _configured_role_ids(attr: str) -> set[int]:
    raw = getattr(settings, attr, None)
    if raw is None:
        return set()
    if isinstance(raw, (list, tuple, set)):
        values = raw
    else:
        values = str(raw).replace(";", ",").split(",")
    out: set[int] = set()
    for value in values:
        try:
            out.add(int(str(value).strip()))
        except Exception:
            continue
    return out


def _interaction_role_ids(interaction: discord.Interaction) -> set[int]:
    roles = getattr(getattr(interaction, "user", None), "roles", None) or []
    out: set[int] = set()
    for role in roles:
        try:
            out.add(int(getattr(role, "id")))
        except Exception:
            continue
    return out


def _has_configured_role(interaction: discord.Interaction, *attrs: str) -> bool:
    wanted: set[int] = set()
    for attr in attrs:
        wanted |= _configured_role_ids(attr)
    return bool(wanted and (_interaction_role_ids(interaction) & wanted))


def _is_operator_user(interaction: discord.Interaction, *, exec_user_id: int) -> bool:
    return _is_exec_user(interaction, exec_user_id=exec_user_id) or _has_configured_role(
        interaction,
        "discord_operator_role_ids",
    )


def _is_moderator_user(interaction: discord.Interaction, *, exec_user_id: int) -> bool:
    return _is_operator_user(interaction, exec_user_id=exec_user_id) or _has_configured_role(
        interaction,
        "discord_moderator_role_ids",
    )


def _is_contributor_user(interaction: discord.Interaction, *, exec_user_id: int) -> bool:
    return _is_moderator_user(interaction, exec_user_id=exec_user_id) or _has_configured_role(
        interaction,
        "discord_contributor_role_ids",
    )


async def _ensure_contributor(interaction: discord.Interaction, *, exec_user_id: int) -> bool:
    if not _is_contributor_user(interaction, exec_user_id=exec_user_id):
        await _deny(interaction, "Contributor access required.")
        return False
    return True


async def _ensure_moderator(interaction: discord.Interaction, *, exec_user_id: int) -> bool:
    if not _is_moderator_user(interaction, exec_user_id=exec_user_id):
        await _deny(interaction, "Moderator access required.")
        return False
    return True


def _operator_commands_enabled() -> bool:
    return bool(getattr(settings, "discord_operator_commands_enabled", False))


def _parse_prefixed_id(value: str, *, prefix: str) -> int:
    raw = (value or "").strip().upper()
    if raw.startswith(prefix + "-"):
        raw = raw[len(prefix) + 1 :]
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        raise ValueError(f"Invalid ID (expected {prefix}-000123).")
    return int(digits)


def _format_asof(asof_utc: datetime | None) -> str:
    if not asof_utc:
        return "AsOf: (cached)"
    aware = asof_utc.replace(tzinfo=timezone.utc)
    et = aware.astimezone(ET)
    return f"AsOf: {et:%Y-%m-%d %H:%M} {ET_LABEL}"


def _exchange_disp(full_exchange_name: str | None, exchange: str | None) -> str:
    full = (full_exchange_name or "").strip()
    code = (exchange or "").strip()
    if full and code and full != code:
        return f"{full} ({code})"
    return full or code


def _quote_disp(price: float | None, currency: str | None) -> str:
    if price is None:
        return "—"
    cur = (currency or "").strip()
    if abs(float(price)) >= 1000:
        value = f"{float(price):,.2f}"
    elif abs(float(price)) >= 1:
        value = f"{float(price):.2f}"
    else:
        value = f"{float(price):.4f}"
    return f"{value} {cur}".strip()


def _short(value: str | None, max_len: int) -> str:
    s = (value or "").strip()
    if len(s) <= max_len:
        return s
    if max_len <= 1:
        return s[:max_len]
    return s[: max_len - 1] + "…"


def _unescape_newlines(value: str | None) -> str | None:
    """
    Discord slash-command inputs often make it awkward to enter literal newlines.
    Support the user-friendly escapes "\\n" and "/n" across note-like fields.
    """
    if value is None:
        return None
    s = str(value)
    if not s:
        return s
    return s.replace("\\n", "\n").replace("/n", "\n")


def _sort_perf_rows(
    rows: list[services.PerfRow],
    *,
    sort: Literal["since", "m1", "ytd", "y1"],
    order: Literal["asc", "desc"],
) -> list[services.PerfRow]:
    def pick(row: services.PerfRow) -> float | None:
        if sort == "since":
            return row.since_24h
        if sort == "m1":
            return row.m1
        if sort == "ytd":
            return row.ytd
        return row.y1

    with_vals: list[tuple[services.PerfRow, float]] = []
    no_vals: list[services.PerfRow] = []
    for r in rows:
        v = pick(r)
        if v is None:
            no_vals.append(r)
        else:
            with_vals.append((r, float(v)))
    with_vals.sort(key=lambda t: t[1], reverse=(order == "desc"))
    return [r for r, _ in with_vals] + no_vals


def _table_chunks(headers: list[str], rows: list[list[str]], *, max_chars: int = 1800) -> list[str]:
    if not rows:
        return [render_table(headers, [])]
    chunks: list[list[list[str]]] = []
    cur: list[list[str]] = []
    for row in rows:
        candidate = cur + [row]
        if len(render_table(headers, candidate)) > max_chars and cur:
            chunks.append(cur)
            cur = [row]
        else:
            cur = candidate
    if cur:
        chunks.append(cur)
    return [render_table(headers, c) for c in chunks]


def _paginate_rows(rows: list[list[str]], *, page: int, page_size: int) -> tuple[list[list[str]], str]:
    total = len(rows)
    if total <= 0:
        return rows, "Page: 1/1"
    page_size = max(1, int(page_size))
    pages = max(1, (total + page_size - 1) // page_size)
    page = int(page)
    if page < 1 or page > pages:
        raise ValueError(f"Page out of range: {page} (1-{pages}).")
    start = (page - 1) * page_size
    end = min(total, start + page_size)
    label = f"Page: {page}/{pages} • Rows: {start + 1}-{end} of {total}"
    return rows[start:end], label


@dataclass(frozen=True)
class BotContext:
    guild_id: int
    exec_user_id: int
    guild_obj: discord.Object


T = TypeVar("T")


def _pages(total: int, page_size: int) -> int:
    total = max(0, int(total))
    page_size = max(1, int(page_size))
    return max(1, (total + page_size - 1) // page_size)


def _slice_page(items: list[T], *, page: int, page_size: int) -> tuple[list[T], str, int]:
    total = len(items)
    pages = _pages(total, page_size)
    page = max(1, min(int(page), pages))
    start = (page - 1) * int(page_size)
    end = min(total, start + int(page_size))
    label = f"Page: {page}/{pages} • Rows: {start + 1}-{end} of {total}" if total else "Page: 1/1"
    return items[start:end], label, pages


def _enqueue_watchlist_refresh_debounced(*, guild_id: int) -> None:
    if not bool(getattr(settings, "discord_watchlist_refresh_enabled", True)):
        return
    from thoth_terminal.db import session_scope
    from thoth_terminal.jobs import enqueue_discord_watchlist_refresh_debounced

    delay_s = int(getattr(settings, "discord_watchlist_immediate_refresh_delay_seconds", 8) or 8)
    with session_scope() as session:
        enqueue_discord_watchlist_refresh_debounced(
            session,
            guild_id=int(guild_id),
            delay_seconds=delay_s,
        )


def _enqueue_watchlist_transcript_scan_debounced(*, guild_id: int) -> None:
    if not bool(getattr(settings, "discord_transcript_scan_enabled", True)):
        return
    if not bool(getattr(settings, "discord_transcript_scan_immediate_enabled", False)):
        return
    from thoth_terminal.db import session_scope
    from thoth_terminal.jobs import enqueue_discord_watchlist_transcript_scan_debounced

    delay_s = int(getattr(settings, "discord_watchlist_immediate_refresh_delay_seconds", 8) or 8)
    lookback_days = int(getattr(settings, "discord_transcript_scan_lookback_days", 120) or 120)
    with session_scope() as session:
        enqueue_discord_watchlist_transcript_scan_debounced(
            session,
            guild_id=int(guild_id),
            delay_seconds=delay_s,
            lookback_days=max(1, lookback_days),
        )


def _enqueue_watchlist_followup_jobs_debounced(*, guild_id: int) -> None:
    _enqueue_watchlist_refresh_debounced(guild_id=guild_id)
    _enqueue_watchlist_transcript_scan_debounced(guild_id=guild_id)


def _link_agentic_llm_ready() -> bool:
    endpoint = local_model_endpoint() or ""
    model = local_model_name() or ""
    return bool(endpoint and model)


def _rich_cards_from_rows(headers: list[str], rows: list[list[str]]) -> list[str]:
    label_map = {
        "masterlist": "ML",
        "sector": "Sec",
        "subgroup": "SG",
        "ticker": "T",
        "name": "N",
        "last": "Px",
        "exchange": "Ex",
        "since(24h)": "24h",
    }
    cards: list[str] = []
    for row in rows:
        primary = (row[0] if row else "—") or "—"
        details: list[str] = []
        for idx in range(1, min(len(headers), len(row))):
            key = headers[idx]
            val = row[idx] if row[idx] else "—"
            key_disp = label_map.get(str(key).strip().lower(), str(key).strip())
            details.append(f"{key_disp}: {val}")
        if details:
            cards.append(f"• {primary} • " + " • ".join(details))
        else:
            cards.append(f"• {primary}")
    return cards


def _compact_listing_cards(
    *,
    rows: list[list[str]],
    ticker_idx: int,
    name_idx: int,
    exchange_idx: int | None = None,
    price_idx: int | None = None,
    group_idx: list[int] | None = None,
) -> list[str]:
    cards: list[str] = []
    for row in rows:
        ticker = (row[ticker_idx] if ticker_idx < len(row) else "—") or "—"
        name = (row[name_idx] if name_idx < len(row) else "—") or "—"
        title = f"• {ticker} — {name}"
        meta: list[str] = []
        if group_idx:
            groups: list[str] = []
            for idx in group_idx:
                value = (row[idx] if idx < len(row) else "").strip()
                if value and value != "—":
                    groups.append(value)
            if groups:
                meta.append(" / ".join(groups))
        if price_idx is not None and price_idx < len(row):
            px = (row[price_idx] or "—").strip()
            if px:
                meta.append(px)
        if exchange_idx is not None and exchange_idx < len(row):
            ex = (row[exchange_idx] or "—").strip()
            if ex:
                meta.append(ex)
        if meta:
            title = title + "\n  " + " • ".join(meta)
        cards.append(title)
    return cards


def _project_rows(
    *,
    headers: list[str],
    rows: list[list[str]],
    drop_headers: set[str],
) -> tuple[list[str], list[list[str]]]:
    if not rows:
        kept_headers = [h for h in headers if h.strip().lower() not in drop_headers]
        return kept_headers, []
    keep_idx = [idx for idx, h in enumerate(headers) if h.strip().lower() not in drop_headers]
    kept_headers = [headers[idx] for idx in keep_idx]
    out_rows: list[list[str]] = []
    for row in rows:
        projected = [(row[idx] if idx < len(row) else "—") for idx in keep_idx]
        out_rows.append(projected)
    return kept_headers, out_rows


def _build_rich_embed(
    *,
    title: str,
    color: int,
    header_lines: list[str] | None,
    page_label: str | None,
    cards: list[str],
    footer: str | None = None,
) -> discord.Embed:
    desc_lines: list[str] = []
    for line in header_lines or []:
        if line and str(line).strip():
            desc_lines.append(str(line).strip())
    if page_label:
        desc_lines.append(page_label)
    desc_lines.append("")
    desc_lines.extend(cards or ["(no rows)"])
    embed = discord.Embed(title=title[:256], description="\n".join(desc_lines)[:4096], color=color)
    if footer:
        embed.set_footer(text=str(footer)[:2048])
    return embed


def _build_rich_embeds(
    *,
    title: str,
    color: int,
    headers: list[str],
    rows: list[list[str]],
    header_lines: list[str] | None = None,
    footer: str | None = None,
    page_size: int = 10,
) -> list[discord.Embed]:
    cards = _rich_cards_from_rows(headers, rows)
    pages = _pages(len(cards), page_size)
    embeds: list[discord.Embed] = []
    for page in range(1, pages + 1):
        page_cards, page_label, _ = _slice_page(cards, page=page, page_size=page_size)
        embeds.append(
            _build_rich_embed(
                title=title,
                color=color,
                header_lines=header_lines,
                page_label=page_label,
                cards=page_cards,
                footer=footer,
            )
        )
    return embeds


async def _send_embed_pages(
    interaction: discord.Interaction,
    *,
    embeds: list[discord.Embed],
    ephemeral: bool = False,
    deferred: bool = False,
) -> None:
    if not embeds:
        embeds = [_build_rich_embed(title="Results", color=0x93C5FD, header_lines=None, page_label=None, cards=[])]
    if deferred:
        await interaction.followup.send(embed=embeds[0], ephemeral=ephemeral)
    else:
        await interaction.response.send_message(embed=embeds[0], ephemeral=ephemeral)
    for embed in embeds[1:]:
        await interaction.followup.send(embed=embed, ephemeral=ephemeral)


class WatchlistDashboardView(discord.ui.View):
    def __init__(
        self,
        *,
        ctx: BotContext,
        list_kind: Literal["exec", "community", "masterlist"],
        mode: Literal["list", "perf"] = "perf",
        sector: str | None = None,
        subgroup: str | None = None,
        sort: Literal["since", "m1", "ytd", "y1"] = "since",
        order: Literal["asc", "desc"] = "desc",
        page_size: int = 10,
        exec_only: bool = False,
    ) -> None:
        super().__init__(timeout=900)
        self.ctx = ctx
        self.list_kind = list_kind
        self.mode = mode
        self.sector = sector if list_kind in {"community", "masterlist"} else None
        self.subgroup = subgroup if list_kind in {"community", "masterlist"} else None
        self.sort = sort
        self.order = order
        self.page = 1
        self.page_size = max(5, min(int(page_size or 10), 40))
        self.exec_only = bool(exec_only)

        self._last_embed_pages = 1

        self._sector_select: discord.ui.Select | None = None
        self._subgroup_select: discord.ui.Select | None = None
        self._sort_select: discord.ui.Select | None = None
        self._prev_btn: discord.ui.Button | None = None
        self._next_btn: discord.ui.Button | None = None
        self._refresh_btn: discord.ui.Button | None = None
        self._mode_btn: discord.ui.Button | None = None
        self._order_btn: discord.ui.Button | None = None

        self._build_controls()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not await _ensure_guild(interaction, guild_id=self.ctx.guild_id):
            return False
        if self.exec_only and not await _ensure_exec(interaction, exec_user_id=self.ctx.exec_user_id):
            return False
        return True

    def _build_controls(self) -> None:
        if self.list_kind in {"community", "masterlist"}:
            self._sector_select = discord.ui.Select(
                placeholder="Sector: All",
                min_values=1,
                max_values=1,
                row=0,
                options=[discord.SelectOption(label="All", value="__ALL__", default=True)],
            )
            self._sector_select.callback = self._on_sector_selected  # type: ignore[assignment]
            self.add_item(self._sector_select)

            self._subgroup_select = discord.ui.Select(
                placeholder="Subgroup: All",
                min_values=1,
                max_values=1,
                row=1,
                disabled=True,
                options=[discord.SelectOption(label="All", value="__ALL__", default=True)],
            )
            self._subgroup_select.callback = self._on_subgroup_selected  # type: ignore[assignment]
            self.add_item(self._subgroup_select)

        self._sort_select = discord.ui.Select(
            placeholder="Sort: Since(24h)",
            min_values=1,
            max_values=1,
            row=2,
            disabled=(self.mode != "perf"),
            options=[
                discord.SelectOption(label="Since(24h)", value="since", default=True),
                discord.SelectOption(label="1M", value="m1", default=False),
                discord.SelectOption(label="YTD", value="ytd", default=False),
                discord.SelectOption(label="1Y", value="y1", default=False),
            ],
        )
        self._sort_select.callback = self._on_sort_selected  # type: ignore[assignment]
        self.add_item(self._sort_select)

        self._prev_btn = discord.ui.Button(label="Prev", style=discord.ButtonStyle.secondary, row=3, disabled=True)
        self._next_btn = discord.ui.Button(label="Next", style=discord.ButtonStyle.secondary, row=3, disabled=True)
        self._refresh_btn = discord.ui.Button(label="Refresh", style=discord.ButtonStyle.primary, row=3)
        self._mode_btn = discord.ui.Button(label="Mode: Perf", style=discord.ButtonStyle.secondary, row=3)
        self._order_btn = discord.ui.Button(label="Order: Desc", style=discord.ButtonStyle.secondary, row=3, disabled=(self.mode != "perf"))

        self._prev_btn.callback = self._on_prev  # type: ignore[assignment]
        self._next_btn.callback = self._on_next  # type: ignore[assignment]
        self._refresh_btn.callback = self._on_refresh  # type: ignore[assignment]
        self._mode_btn.callback = self._on_toggle_mode  # type: ignore[assignment]
        self._order_btn.callback = self._on_toggle_order  # type: ignore[assignment]

        self.add_item(self._prev_btn)
        self.add_item(self._next_btn)
        self.add_item(self._refresh_btn)
        self.add_item(self._mode_btn)
        self.add_item(self._order_btn)

    async def _ensure_select_options(self) -> None:
        if self._sector_select is not None:
            labels = await asyncio.to_thread(
                services.autocomplete_sector_labels,
                guild_id=self.ctx.guild_id,
                list_kind=self.list_kind,
                current="",
                limit=25,
            )
            cur = (self.sector or "").strip().lower()
            options = [discord.SelectOption(label="All", value="__ALL__", default=(not cur))]
            for s in labels:
                if not s:
                    continue
                label = str(s).strip()
                if not label:
                    continue
                value = label[:100]
                options.append(discord.SelectOption(label=label[:100], value=value, default=(label.strip().lower() == cur)))
            self._sector_select.options = options[:25]
            self._sector_select.placeholder = (
                ("Masterlist: All" if self.list_kind == "masterlist" else "Sector: All")
                if not self.sector
                else (f"{'Masterlist' if self.list_kind == 'masterlist' else 'Sector'}: {self.sector}"[:100])
            )

        if self._subgroup_select is not None:
            if self.sector:
                labels = await asyncio.to_thread(
                    services.autocomplete_subgroup_labels,
                    guild_id=self.ctx.guild_id,
                    list_kind=self.list_kind,
                    sector=str(self.sector),
                    current="",
                    limit=25,
                )
                cur = (self.subgroup or "").strip().lower()
                options = [discord.SelectOption(label="All", value="__ALL__", default=(not cur))]
                for s in labels:
                    if not s:
                        continue
                    label = str(s).strip()
                    if not label:
                        continue
                    value = label[:100]
                    options.append(discord.SelectOption(label=label[:100], value=value, default=(label.strip().lower() == cur)))
                self._subgroup_select.options = options[:25]
                self._subgroup_select.disabled = False
                self._subgroup_select.placeholder = (
                    "Subgroup: All" if not self.subgroup else f"Subgroup: {self.subgroup}"[:100]
                )
            else:
                self._subgroup_select.options = [discord.SelectOption(label="All", value="__ALL__", default=True)]
                self._subgroup_select.disabled = True
                self._subgroup_select.placeholder = "Subgroup: (pick a sector)"

        if self._sort_select is not None:
            self._sort_select.disabled = (self.mode != "perf")
            for opt in self._sort_select.options:
                opt.default = bool(opt.value == self.sort)

        if self._mode_btn is not None:
            self._mode_btn.label = f"Mode: {'Perf' if self.mode == 'perf' else 'List'}"
        if self._order_btn is not None:
            self._order_btn.disabled = (self.mode != "perf")
            self._order_btn.label = f"Order: {'Desc' if self.order == 'desc' else 'Asc'}"

    def _set_paging_controls(self, *, pages: int) -> None:
        pages = max(1, int(pages or 1))
        self._last_embed_pages = pages
        if self._prev_btn is not None:
            self._prev_btn.disabled = (self.page <= 1)
        if self._next_btn is not None:
            self._next_btn.disabled = (self.page >= pages)

    async def build_embed(self, *, refresh: bool) -> discord.Embed:
        await self._ensure_select_options()

        kind_label = (
            "Executive Watchlist" if self.list_kind == "exec" else ("Masterlists" if self.list_kind == "masterlist" else "Community Watchlist")
        )
        title = f"{kind_label} — {'Performance' if self.mode == 'perf' else 'List'}"
        color = 0xFBBF24 if self.list_kind == "exec" else (0xA78BFA if self.list_kind == "masterlist" else 0x93C5FD)

        desc_lines: list[str] = []
        filt_bits: list[str] = []
        if self.sector:
            filt_bits.append(f"{'Masterlist' if self.list_kind == 'masterlist' else 'Sector'}: {self.sector}")
        if self.subgroup:
            filt_bits.append(f"Subgroup: {self.subgroup}")
        if filt_bits:
            desc_lines.append(" • ".join(filt_bits))

        if self.mode == "list":
            refresh_res = None
            if refresh:
                try:
                    refresh_res = await asyncio.to_thread(
                        services.refresh_watchlist_quotes,
                        guild_id=self.ctx.guild_id,
                        list_kind=self.list_kind,
                        sector=self.sector,
                        subgroup=self.subgroup,
                        force_refresh=True,
                    )
                except Exception as exc:
                    refresh_res = services.QuoteRefreshResult(updated=0, asof_utc=None, error=str(exc))

            entries = await asyncio.to_thread(
                services.list_watchlist_entries,
                guild_id=self.ctx.guild_id,
                list_kind=self.list_kind,
                sector=self.sector,
                subgroup=self.subgroup,
            )
            asof = refresh_res.asof_utc if refresh_res else max((e.last_quote_at for e in entries if e.last_quote_at), default=None)
            header = _format_asof(asof)

            lines: list[str] = []
            for e in entries:
                loc = ""
                if self.list_kind in {"community", "masterlist"}:
                    sec = e.sector or ("—" if self.list_kind == "masterlist" else "Uncategorized")
                    grp = e.subgroup or "General"
                    loc = f"{sec} / {grp} • "
                nm = _short(e.company_name, 44) or "—"
                ex = _exchange_disp(e.full_exchange_name, e.exchange) or "—"
                lines.append(f"• {loc}{e.symbol} — {nm} — {ex}")

            page_lines, page_label, pages = _slice_page(lines, page=self.page, page_size=self.page_size)
            self.page = max(1, min(self.page, pages))
            self._set_paging_controls(pages=pages)

            desc = "\n".join([header, page_label] + (desc_lines if desc_lines else []) + [""] + (page_lines or ["(no entries)"]))
            embed = discord.Embed(title=title[:256], description=desc[:4096], color=color)
            if refresh_res and refresh_res.error:
                embed.set_footer(text="Market data unavailable (Yahoo). Showing cached names when available.")
            return embed

        # perf mode
        if refresh:
            result = await asyncio.to_thread(
                services.refresh_watchlist_perf,
                guild_id=self.ctx.guild_id,
                list_kind=self.list_kind,
                sector=self.sector,
                subgroup=self.subgroup,
                force_refresh=True,
            )
        else:
            result = await asyncio.to_thread(
                services.list_watchlist_perf_cached,
                guild_id=self.ctx.guild_id,
                list_kind=self.list_kind,
                sector=self.sector,
                subgroup=self.subgroup,
            )

        rows_sorted = _sort_perf_rows(result.rows, sort=self.sort, order=self.order)
        header = _format_asof(result.asof_utc)

        lines: list[str] = []
        for r in rows_sorted:
            loc = ""
            if self.list_kind in {"community", "masterlist"}:
                sec = r.sector or ("—" if self.list_kind == "masterlist" else "Uncategorized")
                grp = r.subgroup or "General"
                loc = f"{sec} / {grp} • "
            nm = _short(r.company_name, 42) or "—"
            ex = _exchange_disp(r.full_exchange_name, r.exchange) or "—"
            since = "PENDING" if r.baseline_pending else format_pct(r.since_24h)
            lines.append(f"• {loc}{r.symbol} — {nm} — {ex}")
            lines.append(
                f"  Since(24h): {since} • 1M: {format_pct(r.m1)} • YTD: {format_pct(r.ytd)} • 1Y: {format_pct(r.y1)}"
            )

        page_lines, page_label, pages = _slice_page(lines, page=self.page, page_size=self.page_size * 2)
        self.page = max(1, min(self.page, pages))
        self._set_paging_controls(pages=pages)

        desc = "\n".join([header, page_label] + (desc_lines if desc_lines else []) + [""] + (page_lines or ["(no entries)"]))
        embed = discord.Embed(title=title[:256], description=desc[:4096], color=color)
        if result.error:
            embed.set_footer(text="Market data unavailable (Yahoo). Showing cached returns when available.")
        return embed

    async def _edit(self, interaction: discord.Interaction, *, refresh: bool, defer: bool) -> None:
        if defer:
            await interaction.response.defer(thinking=True)
        embed = await self.build_embed(refresh=refresh)
        if defer:
            await interaction.followup.edit_message(interaction.message.id, embed=embed, view=self)  # type: ignore[arg-type]
        else:
            await interaction.response.edit_message(embed=embed, view=self)

    async def _on_prev(self, interaction: discord.Interaction) -> None:
        self.page = max(1, int(self.page) - 1)
        await self._edit(interaction, refresh=False, defer=False)

    async def _on_next(self, interaction: discord.Interaction) -> None:
        self.page = min(int(self._last_embed_pages or 1), int(self.page) + 1)
        await self._edit(interaction, refresh=False, defer=False)

    async def _on_refresh(self, interaction: discord.Interaction) -> None:
        await self._edit(interaction, refresh=True, defer=True)

    async def _on_toggle_mode(self, interaction: discord.Interaction) -> None:
        self.mode = "list" if self.mode == "perf" else "perf"
        self.page = 1
        await self._edit(interaction, refresh=False, defer=True)

    async def _on_toggle_order(self, interaction: discord.Interaction) -> None:
        if self.mode != "perf":
            return
        self.order = "asc" if self.order == "desc" else "desc"
        self.page = 1
        await self._edit(interaction, refresh=False, defer=False)

    async def _on_sort_selected(self, interaction: discord.Interaction) -> None:
        if not self._sort_select or not self._sort_select.values:
            return
        val = str(self._sort_select.values[0] or "").strip()
        if val in {"since", "m1", "ytd", "y1"}:
            self.sort = val  # type: ignore[assignment]
        self.page = 1
        await self._edit(interaction, refresh=False, defer=False)

    async def _on_sector_selected(self, interaction: discord.Interaction) -> None:
        if not self._sector_select or not self._sector_select.values:
            return
        val = str(self._sector_select.values[0] or "").strip()
        if val == "__ALL__":
            self.sector = None
            self.subgroup = None
        else:
            self.sector = val
            self.subgroup = None
        self.page = 1
        await self._edit(interaction, refresh=False, defer=False)

    async def _on_subgroup_selected(self, interaction: discord.Interaction) -> None:
        if not self._subgroup_select or not self._subgroup_select.values:
            return
        val = str(self._subgroup_select.values[0] or "").strip()
        if val == "__ALL__":
            self.subgroup = None
        else:
            self.subgroup = val
        self.page = 1
        await self._edit(interaction, refresh=False, defer=False)


def register_commands(tree: app_commands.CommandTree, *, ctx: BotContext) -> None:
    guild = ctx.guild_obj

    async def _ac_watchlist_sector(
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        if not _is_expected_guild(interaction, guild_id=ctx.guild_id):
            return []
        labels = await asyncio.to_thread(
            services.autocomplete_sector_labels,
            guild_id=ctx.guild_id,
            list_kind="community",
            current=current,
            limit=25,
        )
        return [app_commands.Choice(name=s, value=s) for s in labels]

    async def _ac_watchlist_subgroup(
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        if not _is_expected_guild(interaction, guild_id=ctx.guild_id):
            return []
        sector = getattr(getattr(interaction, "namespace", None), "sector", None)
        if not sector:
            return []
        labels = await asyncio.to_thread(
            services.autocomplete_subgroup_labels,
            guild_id=ctx.guild_id,
            list_kind="community",
            sector=str(sector),
            current=current,
            limit=25,
        )
        return [app_commands.Choice(name=s, value=s) for s in labels]

    async def _ac_masterlist_name(
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        if not _is_expected_guild(interaction, guild_id=ctx.guild_id):
            return []
        labels = await asyncio.to_thread(
            services.autocomplete_sector_labels,
            guild_id=ctx.guild_id,
            list_kind="masterlist",
            current=current,
            limit=25,
        )
        return [app_commands.Choice(name=s, value=s) for s in labels]

    async def _ac_masterlist_subgroup(
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        if not _is_expected_guild(interaction, guild_id=ctx.guild_id):
            return []
        ml = getattr(getattr(interaction, "namespace", None), "masterlist", None)
        if not ml:
            return []
        labels = await asyncio.to_thread(
            services.autocomplete_subgroup_labels,
            guild_id=ctx.guild_id,
            list_kind="masterlist",
            sector=str(ml),
            current=current,
            limit=25,
        )
        return [app_commands.Choice(name=s, value=s) for s in labels]

    # /memo (show) and /memo url:<...> (set by executive)
    @tree.command(name="memo", description="Show or set the canonical Research Memo link.", guild=guild)
    @app_commands.describe(url="If provided, executive-only: set the memo URL")
    async def memo(interaction: discord.Interaction, url: str | None = None) -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        if url and not await _ensure_exec(interaction, exec_user_id=ctx.exec_user_id):
            return
        if url:
            await asyncio.to_thread(services.set_memo_url, guild_id=ctx.guild_id, url=url, user_id=interaction.user.id)
            await interaction.response.send_message("Research Memo updated.", ephemeral=False)
            return
        memo_url = await asyncio.to_thread(services.get_memo_url, guild_id=ctx.guild_id)
        if memo_url:
            await interaction.response.send_message(f"Research Memo: {memo_url}", ephemeral=False)
        else:
            await interaction.response.send_message(
                "Research Memo not set. Executive: `/memo url:<link>`",
                ephemeral=False,
            )

    if not _operator_commands_enabled():
        tree.remove_command("memo", guild=guild)

    # /desk ... (exec-only; agentic desk thread + digest controls)
    desk = app_commands.Group(name="desk", description="Desk automation (exec-only).")

    @desk.command(name="setup", description="Create or bind the Thoth Desk thread (exec-only).")
    async def desk_setup(interaction: discord.Interaction) -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        if not await _ensure_exec(interaction, exec_user_id=ctx.exec_user_id):
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        channel_id = int(getattr(settings, "discord_channel_id", None) or int(interaction.channel_id or 0))
        if not channel_id:
            await interaction.followup.send("Missing desk channel (set THOTH_DISCORD_CHANNEL_ID).", ephemeral=True)
            return
        try:
            thread_id = await desk_mod.ensure_desk_thread(
                interaction.client,
                guild_id=ctx.guild_id,
                channel_id=channel_id,
                thread_name=str(getattr(settings, "discord_desk_thread_name", "Thoth Desk") or "Thoth Desk"),
                user_id=interaction.user.id,
            )
        except Exception as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(f"Desk thread ready: <#{int(thread_id)}>", ephemeral=True)

    @desk.command(name="status", description="Show desk automation status (exec-only).")
    @app_commands.describe(public="Post in-channel (default is ephemeral)")
    async def desk_status(interaction: discord.Interaction, public: bool = False) -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        if not await _ensure_exec(interaction, exec_user_id=ctx.exec_user_id):
            return
        await interaction.response.defer(thinking=True, ephemeral=(not bool(public)))
        cfg = await asyncio.to_thread(desk_mod.get_desk_settings, guild_id=ctx.guild_id)
        pending = await asyncio.to_thread(outbox_mod.list_outbox, guild_id=ctx.guild_id, status="pending", limit=100)
        sending = await asyncio.to_thread(outbox_mod.list_outbox, guild_id=ctx.guild_id, status="sending", limit=100)
        failed = await asyncio.to_thread(outbox_mod.list_outbox, guild_id=ctx.guild_id, status="failed", limit=20)

        # Latest digest job (best-effort).
        def _latest_job() -> dict[str, Any] | None:
            from thoth_terminal.db import session_factory
            from thoth_terminal.models import Job

            SessionLocal = session_factory()
            with SessionLocal() as session:
                j = (
                    session.query(Job)
                    .filter(Job.kind == agent_jobs.AGENT_JOB_DIGEST)
                    .order_by(Job.created_at.desc(), Job.id.desc())
                    .first()
                )
                if not j:
                    return None
                return {
                    "id": int(j.id),
                    "status": str(j.status),
                    "attempts": int(j.attempts or 0),
                    "last_error": str(j.last_error or "")[:300],
                    "updated_at": str(j.updated_at),
                }

        latest = await asyncio.to_thread(_latest_job)
        pipeline = await asyncio.to_thread(services.discord_pipeline_health, guild_id=ctx.guild_id)
        link_stats = await asyncio.to_thread(link_intake.inbox_stats, guild_id=ctx.guild_id)
        runtime_info = get_runtime_identity()
        digest_slots = await asyncio.to_thread(agent_jobs.list_digest_schedule_slots)

        endpoint = local_model_endpoint() or ""
        model = local_model_name() or ""
        llm_ok = bool(endpoint and model)

        embed = discord.Embed(title="Thoth Desk — Status", color=0x93C5FD)
        embed.add_field(name="Desk enabled", value=str(bool(cfg.desk_enabled)), inline=True)
        embed.add_field(name="Outbox paused", value=str(bool(cfg.outbox_paused)), inline=True)
        embed.add_field(
            name="Desk thread",
            value=(f"<#{cfg.desk_thread_id}>" if cfg.desk_thread_id else "(not set; run `/desk setup`)"),
            inline=False,
        )
        if digest_slots:
            embed.add_field(
                name="Digest schedule",
                value="\n".join(
                    [
                        f"{str(slot.get('label') or slot.get('key') or 'digest')} @ {str(slot.get('hhmm') or '')} ({int(slot.get('lookback_hours') or 0)}h)"
                        + (" + main notice" if bool(slot.get("announce_in_main")) else "")
                        for slot in digest_slots
                    ]
                )[:1024],
                inline=False,
            )
        embed.add_field(name="Outbox", value=f"pending={len(pending)} • sending={len(sending)} • failed={len(failed)}", inline=False)
        if latest:
            embed.add_field(
                name="Latest digest job",
                value=f"id={latest['id']} • status={latest['status']} • attempts={latest['attempts']}\n{latest['last_error']}".strip()[:1024],
                inline=False,
            )
        quote_fresh = pipeline.get("quote_freshness") if isinstance(pipeline, dict) else {}
        if isinstance(quote_fresh, dict):
            embed.add_field(
                name="Market pipeline",
                value=(
                    f"entries={pipeline.get('watchlist_entries_total', 0)} • quoted={pipeline.get('watchlist_entries_with_quotes', 0)}\n"
                    f"<1h={quote_fresh.get('lt_1h', 0)} • 1-6h={quote_fresh.get('h1_to_h6', 0)} • >6h={quote_fresh.get('gt_6h', 0)} • no_ts={quote_fresh.get('missing_quote_at', 0)}"
                )[:1024],
                inline=False,
            )
        refresh_job = pipeline.get("latest_watchlist_refresh_job") if isinstance(pipeline, dict) else None
        transcript_job = pipeline.get("latest_transcript_scan_job") if isinstance(pipeline, dict) else None
        if isinstance(refresh_job, dict) or isinstance(transcript_job, dict):
            embed.add_field(
                name="Jobs",
                value=(
                    f"refresh={((refresh_job or {}).get('status') if isinstance(refresh_job, dict) else 'n/a')} "
                    f"scan={((transcript_job or {}).get('status') if isinstance(transcript_job, dict) else 'n/a')}\n"
                    f"outbox24h={((pipeline.get('transcript_outbox_24h') or {}).get('created', 0) if isinstance(pipeline, dict) else 0)} "
                    f"failed={((pipeline.get('transcript_outbox_24h') or {}).get('failed', 0) if isinstance(pipeline, dict) else 0)}"
                )[:1024],
                inline=False,
            )
        causes = pipeline.get("likely_causes") if isinstance(pipeline, dict) else []
        if isinstance(causes, list) and causes:
            embed.add_field(
                name="Likely Causes",
                value="\n".join([f"- {str(c)[:140]}" for c in causes][:3])[:1024],
                inline=False,
            )
        link_counts = link_stats.get("inbox_counts") if isinstance(link_stats, dict) else {}
        if isinstance(link_counts, dict):
            embed.add_field(
                name="Link Intake",
                value=(
                    f"collected={int(link_counts.get(link_intake.INBOX_STATUS_COLLECTED, 0))} "
                    f"det={int(link_counts.get(link_intake.INBOX_STATUS_DETERMINISTIC_INGESTED, 0))} "
                    f"agentic={int(link_counts.get(link_intake.INBOX_STATUS_AGENTIC_INGESTED, 0))} "
                    f"queued={int(link_counts.get(link_intake.INBOX_STATUS_QUEUED, 0))}\n"
                    f"last_scan={link_stats.get('last_scan_at') or '—'}"
                )[:1024],
                inline=False,
            )
        embed.add_field(
            name="Runtime",
            value=(
                f"{runtime_fingerprint()}\n"
                f"platform={runtime_info.get('platform')} pid={runtime_info.get('pid')}"
            )[:1024],
            inline=False,
        )
        embed.add_field(name="Local LLM", value=("configured" if llm_ok else "not configured"), inline=True)
        await interaction.followup.send(embed=embed, ephemeral=(not bool(public)))

    desk_outbox = app_commands.Group(name="outbox", description="Discord outbox operations.")

    def _outbox_item_summary(item: Any) -> str:
        dest = f"<#{int(item.thread_id)}>" if getattr(item, "thread_id", None) else f"<#{int(item.channel_id)}>"
        bits = [
            f"`{int(item.id)}` {str(item.status)} -> {dest}",
            f"attempts={int(item.attempts or 0)}/{int(item.max_attempts or 0)}",
        ]
        if getattr(item, "discord_message_id", None):
            channel_id = int(item.thread_id or item.channel_id)
            bits.append(outbox_mod.discord_message_url(guild_id=int(item.guild_id), channel_id=channel_id, message_id=int(item.discord_message_id)))
        content = str(getattr(item, "content", "") or "").strip().replace("\n", " ")
        embed_json = getattr(item, "embed_json", None)
        title = str((embed_json or {}).get("title") or "").strip() if isinstance(embed_json, dict) else ""
        preview = content or title
        if preview:
            bits.append(preview[:180])
        if getattr(item, "last_error", None):
            bits.append(f"error={str(item.last_error)[:220]}")
        return "\n".join(bits)[:1024]

    @desk_outbox.command(name="list", description="List Discord outbox items (exec-only).")
    async def desk_outbox_list(
        interaction: discord.Interaction,
        status: Literal["pending", "sending", "failed", "posted", "canceled", "all"] = "failed",
        limit: app_commands.Range[int, 1, 25] = 10,
    ) -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        if not await _ensure_exec(interaction, exec_user_id=ctx.exec_user_id):
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        status_filter = None if status == "all" else str(status)
        items = await asyncio.to_thread(outbox_mod.list_outbox, guild_id=ctx.guild_id, status=status_filter, limit=int(limit))
        embed = discord.Embed(title=f"Discord Outbox — {status}", color=0x93C5FD)
        if not items:
            embed.description = "No matching outbox items."
        for item in items[: int(limit)]:
            embed.add_field(
                name=f"#{int(item.id)} · {str(item.status)}",
                value=_outbox_item_summary(item),
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @desk_outbox.command(name="retry", description="Retry a failed/canceled/sending outbox item (exec-only).")
    async def desk_outbox_retry(interaction: discord.Interaction, outbox_id: int) -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        if not await _ensure_exec(interaction, exec_user_id=ctx.exec_user_id):
            return
        ok = await asyncio.to_thread(outbox_mod.retry_outbox_item, guild_id=ctx.guild_id, outbox_id=int(outbox_id))
        await interaction.response.send_message(
            f"Outbox item #{int(outbox_id)} queued for retry." if ok else f"Outbox item #{int(outbox_id)} cannot be retried.",
            ephemeral=True,
        )

    @desk_outbox.command(name="cancel", description="Cancel a pending/sending/failed outbox item (exec-only).")
    async def desk_outbox_cancel(interaction: discord.Interaction, outbox_id: int) -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        if not await _ensure_exec(interaction, exec_user_id=ctx.exec_user_id):
            return
        ok = await asyncio.to_thread(outbox_mod.cancel_outbox_item, guild_id=ctx.guild_id, outbox_id=int(outbox_id))
        await interaction.response.send_message(
            f"Canceled outbox item #{int(outbox_id)}." if ok else f"Outbox item #{int(outbox_id)} cannot be canceled.",
            ephemeral=True,
        )

    desk.add_command(desk_outbox)

    desk_links = app_commands.Group(name="links", description="Auto-link intake controls.")

    @desk_links.command(name="x_thread", description="Ingest one bounded X thread (exec-only).")
    @app_commands.describe(top_url="First post in the thread", bottom_url="Last post to include, if known")
    async def desk_links_x_thread(
        interaction: discord.Interaction,
        top_url: str,
        bottom_url: str | None = None,
    ) -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        if not await _ensure_exec(interaction, exec_user_id=ctx.exec_user_id):
            return
        top = link_intake.canonical_x_status_url(top_url)
        bottom = link_intake.canonical_x_status_url(bottom_url) if bottom_url else None
        if not top or (bottom_url and not bottom):
            await _deny(interaction, "Provide valid X status URLs for the thread bounds.")
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            job_id = await asyncio.to_thread(
                link_intake.enqueue_x_thread,
                top_url=top,
                bottom_url=bottom,
                requested_by_user_id=int(interaction.user.id),
            )
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except Exception:
            logger.exception("Failed to queue one-off X thread ingestion")
            await interaction.followup.send("Could not queue the X thread ingestion job.", ephemeral=True)
            return
        await interaction.followup.send(f"Queued one-off X thread ingestion as job #{job_id}.", ephemeral=True)

    @desk_links.command(name="status", description="Show auto-link collector status (exec-only).")
    @app_commands.describe(public="Post in-channel (default is ephemeral)")
    async def desk_links_status(interaction: discord.Interaction, public: bool = False) -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        if not await _ensure_exec(interaction, exec_user_id=ctx.exec_user_id):
            return
        await interaction.response.defer(thinking=True, ephemeral=(not bool(public)))
        stats = await asyncio.to_thread(link_intake.inbox_stats, guild_id=ctx.guild_id)
        counts = stats.get("inbox_counts") if isinstance(stats, dict) else {}
        counts = counts if isinstance(counts, dict) else {}
        embed = discord.Embed(title="Desk Links — Status", color=0x93C5FD)
        embed.add_field(
            name="Collector",
            value=(
                f"enabled={bool(stats.get('collector_enabled'))} • "
                f"interval={int(stats.get('collect_interval_minutes') or 0)}m • "
                f"channel={int(stats.get('scan_channel_id') or 0)}"
            )[:1024],
            inline=False,
        )
        embed.add_field(
            name="Inbox",
            value=(
                f"collected={int(counts.get(link_intake.INBOX_STATUS_COLLECTED, 0))} • "
                f"deterministic={int(counts.get(link_intake.INBOX_STATUS_DETERMINISTIC_INGESTED, 0))} • "
                f"agentic={int(counts.get(link_intake.INBOX_STATUS_AGENTIC_INGESTED, 0))} • "
                f"queued={int(counts.get(link_intake.INBOX_STATUS_QUEUED, 0))} • "
                f"ignored={int(counts.get(link_intake.INBOX_STATUS_IGNORED, 0))}"
            )[:1024],
            inline=False,
        )
        embed.add_field(
            name="Last Scan",
            value=(
                f"at={stats.get('last_scan_at') or '—'}\n"
                f"error={stats.get('last_scan_error') or 'none'}"
            )[:1024],
            inline=False,
        )
        embed.add_field(
            name="24h",
            value=(
                f"deterministic_ingested={int(stats.get('deterministic_ingested_24h') or 0)} • "
                f"queued={int(stats.get('queued_24h') or 0)}"
            )[:1024],
            inline=False,
        )
        await interaction.followup.send(embed=embed, ephemeral=(not bool(public)))

    if bool(getattr(settings, "discord_link_agentic_ingest_enabled", False)) and _link_agentic_llm_ready():
        @desk_links.command(name="ingest", description="Run agentic ingestion on collected links (exec-only).")
        @app_commands.describe(
            limit="Maximum collected links to evaluate",
            dry_run="Return proposed decisions without applying them",
        )
        async def desk_links_ingest(
            interaction: discord.Interaction,
            limit: app_commands.Range[int, 1, 100] = 20,
            dry_run: bool = False,
        ) -> None:
            if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
                return
            if not await _ensure_exec(interaction, exec_user_id=ctx.exec_user_id):
                return
            await interaction.response.defer(thinking=True, ephemeral=True)
            try:
                result = await asyncio.to_thread(
                    link_intake.run_agentic_ingest,
                    guild_id=ctx.guild_id,
                    limit=int(limit),
                    dry_run=bool(dry_run),
                )
            except Exception as exc:
                await interaction.followup.send(str(exc), ephemeral=True)
                return
            if bool(dry_run):
                decisions = result.get("decisions") if isinstance(result, dict) else []
                count = len(decisions) if isinstance(decisions, list) else 0
                await interaction.followup.send(f"Dry run complete. Proposed decisions: {count}.", ephemeral=True)
                return
            await interaction.followup.send(
                (
                    f"Ingestion complete. ok={int(result.get('ok_count', 0))} "
                    f"errors={int(result.get('error_count', 0))} total={int(result.get('count', 0))}."
                ),
                ephemeral=True,
            )

    desk.add_command(desk_links)

    desk_digest = app_commands.Group(name="digest", description="Desk digest controls.")
    desk.add_command(desk_digest)

    @desk_digest.command(name="now", description="Enqueue a desk digest job now (exec-only).")
    @app_commands.describe(slot="Optional digest slot key, for example midday or eod", hours="Optional lookback override in hours")
    async def desk_digest_now(
        interaction: discord.Interaction,
        slot: str | None = None,
        hours: app_commands.Range[int, 1, 72] | None = None,
    ) -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        if not await _ensure_exec(interaction, exec_user_id=ctx.exec_user_id):
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        channel_id = int(getattr(settings, "discord_channel_id", None) or int(interaction.channel_id or 0))
        if not channel_id:
            await interaction.followup.send("Missing desk channel (set THOTH_DISCORD_CHANNEL_ID).", ephemeral=True)
            return
        cfg = await asyncio.to_thread(desk_mod.get_desk_settings, guild_id=ctx.guild_id)
        thread_id = cfg.desk_thread_id
        if not thread_id:
            try:
                thread_id = await desk_mod.ensure_desk_thread(
                    interaction.client,
                    guild_id=ctx.guild_id,
                    channel_id=channel_id,
                    thread_name=str(getattr(settings, "discord_desk_thread_name", "Thoth Desk") or "Thoth Desk"),
                    user_id=interaction.user.id,
                )
            except Exception as exc:
                await interaction.followup.send(str(exc), ephemeral=True)
                return
        try:
            job = await asyncio.to_thread(
                agent_jobs.enqueue_manual_digest,
                guild_id=ctx.guild_id,
                post_channel_id=channel_id,
                post_thread_id=int(thread_id),
                slot_key=(slot or "").strip() or None,
                lookback_hours=(int(hours) if hours is not None else None),
                dedupe_key=f"agent_digest_manual:{ctx.guild_id}:{int(interaction.id)}",
            )
        except Exception as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        slot_suffix = f" slot={str(slot).strip().lower()}" if str(slot or "").strip() else ""
        hours_suffix = f" lookback={int(hours)}h" if hours is not None else ""
        await interaction.followup.send(
            f"Digest enqueued (job id={int(job.id)}). Will post in <#{int(thread_id)}>{slot_suffix}{hours_suffix}.",
            ephemeral=True,
        )

    @desk.command(name="pause", description="Pause/unpause outbox sending (exec-only).")
    async def desk_pause(interaction: discord.Interaction, state: Literal["on", "off"]) -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        if not await _ensure_exec(interaction, exec_user_id=ctx.exec_user_id):
            return
        paused = (state or "").strip().lower() == "on"
        try:
            await asyncio.to_thread(desk_mod.set_outbox_paused, guild_id=ctx.guild_id, paused=paused, user_id=interaction.user.id)
        except Exception as exc:
            await _deny(interaction, str(exc))
            return
        await interaction.response.send_message(f"Outbox paused: {paused}.", ephemeral=True)

    if _operator_commands_enabled():
        tree.add_command(desk, guild=guild)

    # /agent ... (exec-only; enqueues worker jobs)
    agent = app_commands.Group(name="agent", description="Agentic research (exec-only).")

    @agent.command(name="ticker", description="Enqueue a ticker timeline report (exec-only).")
    @app_commands.describe(post="If true, also post summary to the desk thread via outbox.")
    async def agent_ticker(
        interaction: discord.Interaction,
        ticker: str,
        days: app_commands.Range[int, 1, 365] = 30,
        post: bool = False,
    ) -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        if not await _ensure_exec(interaction, exec_user_id=ctx.exec_user_id):
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        sym = services.normalize_symbol(ticker)
        if not sym:
            await interaction.followup.send("Missing ticker.", ephemeral=True)
            return

        # Create a stub note immediately so we can return a stable link while the worker job runs.
        client_id = f"discord_agent_ticker:{ctx.guild_id}:{sym}:{int(days)}"

        def _stub_note() -> int:
            from thoth_terminal.db import session_scope

            with session_scope() as session:
                note = notes_bridge.upsert_discord_note(
                    session,
                    client_request_id=client_id,
                    title=f"Ticker Timeline — ${sym}",
                    body_md="Queued. Generating timeline…\n",
                    entity_tokens=[sym],
                    extra_discord={"kind": "agent_ticker_timeline", "state": "queued", "guild_id": int(ctx.guild_id), "ticker": sym, "days": int(days)},
                )
                return int(note.id)

        try:
            note_id = await asyncio.to_thread(_stub_note)
        except Exception as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        link = notes_bridge.note_url(note_id)

        post_channel_id: int | None = None
        post_thread_id: int | None = None
        if post:
            channel_id = int(getattr(settings, "discord_channel_id", None) or int(interaction.channel_id or 0))
            cfg = await asyncio.to_thread(desk_mod.get_desk_settings, guild_id=ctx.guild_id)
            post_channel_id = int(cfg.desk_channel_id or channel_id) if channel_id else None
            post_thread_id = int(cfg.desk_thread_id) if cfg.desk_thread_id else None

        try:
            job = await asyncio.to_thread(
                agent_jobs.enqueue_ticker_timeline,
                guild_id=ctx.guild_id,
                ticker=sym,
                days=int(days),
                post_to_outbox=bool(post),
                post_channel_id=post_channel_id,
                post_thread_id=post_thread_id,
                dedupe_key=f"agent_ticker:{ctx.guild_id}:{sym}:{int(days)}:{int(interaction.id)}",
            )
        except Exception as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        if post:
            dest = f" (will post in <#{int(post_thread_id)}>)" if post_thread_id else ""
            await interaction.followup.send(f"Enqueued (job id={int(job.id)}){dest}. Note: {link}", ephemeral=True)
        else:
            await interaction.followup.send(f"Enqueued (job id={int(job.id)}). Note: {link}", ephemeral=True)

    @agent.command(name="compress", description="Enqueue a broad context compression job (exec-only).")
    @app_commands.describe(
        mode="Compression mode",
        focus="Optional focus query/topic",
        hours="Lookback window in hours",
        post="If true, also post summary to the desk thread via outbox.",
    )
    async def agent_compress(
        interaction: discord.Interaction,
        mode: Literal["daily_master", "topic", "decision_brief", "custom"] = "daily_master",
        focus: str | None = None,
        hours: app_commands.Range[int, 1, 168] = 24,
        post: bool = False,
    ) -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        if not await _ensure_exec(interaction, exec_user_id=ctx.exec_user_id):
            return
        await interaction.response.defer(thinking=True, ephemeral=True)

        post_channel_id: int | None = None
        post_thread_id: int | None = None
        if post:
            channel_id = int(getattr(settings, "discord_channel_id", None) or int(interaction.channel_id or 0))
            cfg = await asyncio.to_thread(desk_mod.get_desk_settings, guild_id=ctx.guild_id)
            if not cfg.desk_channel_id and not channel_id:
                await interaction.followup.send("Desk channel is not configured.", ephemeral=True)
                return
            post_channel_id = int(cfg.desk_channel_id or channel_id) if (cfg.desk_channel_id or channel_id) else None
            post_thread_id = int(cfg.desk_thread_id) if cfg.desk_thread_id else None

        try:
            job = await asyncio.to_thread(
                agent_jobs.enqueue_context_compression,
                guild_id=ctx.guild_id,
                mode=str(mode),
                lookback_hours=int(hours),
                focus_query=(focus or "").strip() or None,
                post_to_outbox=bool(post),
                post_channel_id=post_channel_id,
                post_thread_id=post_thread_id,
                persist_note=True,
                dedupe_key=f"agent_context_compress:{ctx.guild_id}:{int(interaction.id)}",
            )
        except Exception as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        suffix = f" (will post in <#{int(post_thread_id)}>)" if post and post_thread_id else ""
        await interaction.followup.send(
            f"Compression enqueued (job id={int(job.id)}) mode={mode} lookback={int(hours)}h{suffix}.",
            ephemeral=True,
        )

    if _operator_commands_enabled():
        tree.add_command(agent, guild=guild)

    # /symbol search (Yahoo)
    symbol = app_commands.Group(name="symbol", description="Symbol lookup (Yahoo).")

    @symbol.command(name="search", description="Search for likely Yahoo symbols by name.")
    @app_commands.describe(format="Output style (rich or table)")
    async def symbol_search(
        interaction: discord.Interaction,
        query: str,
        limit: app_commands.Range[int, 1, 25] = 8,
        format: Literal["rich", "table"] = "rich",
    ) -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        await interaction.response.defer(thinking=True)
        try:
            results = await asyncio.to_thread(
                search_symbols,
                query,
                user_agent=settings.discord_market_user_agent,
                limit=int(limit),
            )
        except YahooFinanceError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except Exception as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        if not results:
            await interaction.followup.send("No matches.", ephemeral=False)
            return

        table_rows: list[list[str]] = []
        for r in results:
            table_rows.append([r.symbol, r.exchange or "", (r.name or "")[:80]])
        if format == "table":
            msg = render_table(["Symbol", "Exchange", "Name"], table_rows)
            await interaction.followup.send(msg, ephemeral=False)
            return
        embeds = _build_rich_embeds(
            title=f"Symbol Search — {query}",
            color=0x93C5FD,
            headers=["Symbol", "Exchange", "Name"],
            rows=table_rows,
            header_lines=[f"Query: {query}"],
            page_size=10,
        )
        await _send_embed_pages(interaction, embeds=embeds, deferred=True, ephemeral=False)

    tree.add_command(symbol, guild=guild)

    # Executive watchlist (/xwl ...)
    xwl = app_commands.Group(name="xwl", description="Executive watchlist (canonical).")

    @xwl.command(name="add", description="Add a ticker to the executive watchlist.")
    async def xwl_add(interaction: discord.Interaction, ticker: str) -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        if not await _ensure_exec(interaction, exec_user_id=ctx.exec_user_id):
            return
        await interaction.response.defer(thinking=True)
        try:
            res = await asyncio.to_thread(
                services.watchlist_add,
                guild_id=ctx.guild_id,
                list_kind="exec",
                symbol=ticker,
                user_id=interaction.user.id,
            )
        except Exception as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        if res.status in {"added", "reactivated"}:
            await asyncio.to_thread(_enqueue_watchlist_followup_jobs_debounced, guild_id=ctx.guild_id)
            suffix = " (Yahoo validation pending)" if res.validation_pending else ""
            await interaction.followup.send(
                f"Executive watchlist: added ${res.symbol}. Baseline captures after 24h.{suffix}",
                ephemeral=False,
            )
        else:
            await interaction.followup.send(f"Executive watchlist: ${res.symbol} already listed.", ephemeral=False)

    @xwl.command(name="remove", description="Remove a ticker from the executive watchlist.")
    async def xwl_remove(interaction: discord.Interaction, ticker: str) -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        if not await _ensure_exec(interaction, exec_user_id=ctx.exec_user_id):
            return
        try:
            removed = await asyncio.to_thread(
                services.watchlist_remove,
                guild_id=ctx.guild_id,
                list_kind="exec",
                symbol=ticker,
                user_id=interaction.user.id,
            )
        except Exception as exc:
            await _deny(interaction, str(exc))
            return
        if removed:
            await interaction.response.send_message(f"Executive watchlist: removed ${services.normalize_symbol(ticker)}.", ephemeral=False)
        else:
            await interaction.response.send_message("Executive watchlist: ticker not found.", ephemeral=True)

    @xwl.command(name="list", description="List executive watchlist tickers.")
    @app_commands.describe(format="Output style (rich or table)")
    async def xwl_list(interaction: discord.Interaction, format: Literal["rich", "table"] = "rich") -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        if not await _ensure_exec(interaction, exec_user_id=ctx.exec_user_id):
            return
        await interaction.response.defer(thinking=True)
        refresh = await asyncio.to_thread(services.refresh_watchlist_quotes, guild_id=ctx.guild_id, list_kind="exec")
        entries = await asyncio.to_thread(services.list_watchlist_entries, guild_id=ctx.guild_id, list_kind="exec")
        if not entries:
            await interaction.followup.send("Executive watchlist: —", ephemeral=False)
            return
        table_rows: list[list[str]] = []
        for e in entries:
            table_rows.append(
                [
                    e.symbol,
                    _short(e.company_name, 40) or "—",
                    _quote_disp(e.last_quote_price, e.currency),
                    _exchange_disp(e.full_exchange_name, e.exchange) or "—",
                ]
            )
        header = _format_asof(refresh.asof_utc)
        if format == "table":
            chunks = _table_chunks(["Ticker", "Name", "Last", "Exchange"], table_rows)
            first = header + "\n" + chunks[0]
            if refresh.error:
                first += "\nMarket data unavailable (Yahoo)."
            await interaction.followup.send(first, ephemeral=False)
            for chunk in chunks[1:]:
                await interaction.followup.send(chunk, ephemeral=False)
            return
        footer = "Market data unavailable (Yahoo). Showing cached names when available." if refresh.error else None
        embeds = _build_rich_embeds(
            title="Executive Watchlist — List",
            color=0xFBBF24,
            headers=["Ticker", "Name", "Last", "Exchange"],
            rows=table_rows,
            header_lines=[header],
            footer=footer,
            page_size=12,
        )
        await _send_embed_pages(interaction, embeds=embeds, deferred=True, ephemeral=False)

    @xwl.command(name="perf", description="Show executive watchlist performance table.")
    @app_commands.describe(force_refresh="Force refresh (ignore cache)", format="Output style (rich or table)")
    async def xwl_perf(
        interaction: discord.Interaction,
        sort: Literal["since", "m1", "ytd", "y1"] = "since",
        order: Literal["asc", "desc"] = "desc",
        force_refresh: bool = False,
        format: Literal["rich", "table"] = "rich",
    ) -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        if not await _ensure_exec(interaction, exec_user_id=ctx.exec_user_id):
            return
        await interaction.response.defer(thinking=True)
        result = await asyncio.to_thread(
            services.refresh_watchlist_perf,
            guild_id=ctx.guild_id,
            list_kind="exec",
            force_refresh=force_refresh,
        )
        rows_sorted = _sort_perf_rows(result.rows, sort=sort, order=order)
        table_rows: list[list[str]] = []
        for r in rows_sorted:
            since = "PENDING" if r.baseline_pending else format_pct(r.since_24h)
            table_rows.append(
                [
                    r.symbol,
                    _short(r.company_name, 34) or "—",
                    _exchange_disp(r.full_exchange_name, r.exchange) or "—",
                    since,
                    format_pct(r.m1),
                    format_pct(r.ytd),
                    format_pct(r.y1),
                ]
            )

        header = _format_asof(result.asof_utc)
        if format == "table":
            chunks = _table_chunks(["Ticker", "Name", "Exchange", "Since(24h)", "1M", "YTD", "1Y"], table_rows)
            first = header + "\n" + chunks[0]
            if result.error:
                first += "\nMarket data unavailable (Yahoo)."
            await interaction.followup.send(first, ephemeral=False)
            for chunk in chunks[1:]:
                await interaction.followup.send(chunk, ephemeral=False)
            return
        footer = "Market data unavailable (Yahoo). Showing cached returns when available." if result.error else None
        embeds = _build_rich_embeds(
            title="Executive Watchlist — Performance",
            color=0xFBBF24,
            headers=["Ticker", "Name", "Exchange", "Since(24h)", "1M", "YTD", "1Y"],
            rows=table_rows,
            header_lines=[header, f"Sort: {sort.upper()} ({order})"],
            footer=footer,
            page_size=8,
        )
        await _send_embed_pages(interaction, embeds=embeds, deferred=True, ephemeral=False)

    @xwl.command(name="refresh", description="Refresh executive watchlist cache (no table).")
    async def xwl_refresh(interaction: discord.Interaction) -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        if not await _ensure_exec(interaction, exec_user_id=ctx.exec_user_id):
            return
        await interaction.response.defer(thinking=True)
        await asyncio.to_thread(
            services.refresh_watchlist_perf,
            guild_id=ctx.guild_id,
            list_kind="exec",
            force_refresh=True,
        )
        await interaction.followup.send("Executive watchlist: refreshed.", ephemeral=False)

    @xwl.command(name="dashboard", description="Interactive, mobile-friendly exec watchlist viewer.")
    @app_commands.describe(public="Post in-channel (default is ephemeral)", mode="Initial view", page_size="Tickers per page (approx)")
    async def xwl_dashboard(
        interaction: discord.Interaction,
        mode: Literal["list", "perf"] = "perf",
        page_size: app_commands.Range[int, 5, 40] = 10,
        public: bool = False,
    ) -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        if not await _ensure_exec(interaction, exec_user_id=ctx.exec_user_id):
            return
        await interaction.response.defer(thinking=True, ephemeral=(not bool(public)))
        view = WatchlistDashboardView(
            ctx=ctx,
            list_kind="exec",
            mode=mode,
            sort="since",
            order="desc",
            page_size=int(page_size),
            exec_only=True,
        )
        try:
            embed = await view.build_embed(refresh=False)
        except Exception as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(embed=embed, view=view, ephemeral=(not bool(public)))

    if _operator_commands_enabled():
        tree.add_command(xwl, guild=guild)

    # Community watchlist (/watchlist ...)
    watchlist = app_commands.Group(name="watchlist", description="Shared community watchlist with sectors.")

    @watchlist.command(name="add", description="Add a ticker to the shared watchlist (moderator-only).")
    @app_commands.describe(sector="Sector label (free text)", subgroup="Subgroup label within the sector (free text)")
    @app_commands.autocomplete(sector=_ac_watchlist_sector, subgroup=_ac_watchlist_subgroup)
    async def watchlist_add(
        interaction: discord.Interaction,
        ticker: str,
        sector: str | None = None,
        subgroup: str | None = None,
    ) -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        if not await _ensure_moderator(interaction, exec_user_id=ctx.exec_user_id):
            return
        if sector is not None and not str(sector).strip():
            await _deny(interaction, "Sector cannot be blank. Omit it to use 'Uncategorized'.")
            return
        if subgroup is not None and not str(subgroup).strip():
            await _deny(interaction, "Subgroup cannot be blank. Omit it to use 'General'.")
            return
        await interaction.response.defer(thinking=True)
        try:
            res = await asyncio.to_thread(
                services.watchlist_add,
                guild_id=ctx.guild_id,
                list_kind="community",
                symbol=ticker,
                sector=sector,
                subgroup=subgroup,
                user_id=interaction.user.id,
            )
        except Exception as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        sector_disp = res.sector or "Uncategorized"
        subgroup_disp = res.subgroup or "General"
        if res.status in {"added", "reactivated"}:
            await asyncio.to_thread(_enqueue_watchlist_followup_jobs_debounced, guild_id=ctx.guild_id)
            suffix = " (Yahoo validation pending)" if res.validation_pending else ""
            await interaction.followup.send(
                f"Watchlist: added ${res.symbol} (sector: {sector_disp} / {subgroup_disp}). Baseline captures after 24h.{suffix}",
                ephemeral=False,
            )
        elif res.status == "updated":
            await asyncio.to_thread(_enqueue_watchlist_followup_jobs_debounced, guild_id=ctx.guild_id)
            await interaction.followup.send(
                f"Watchlist: updated ${res.symbol} to {sector_disp} / {subgroup_disp}.",
                ephemeral=False,
            )
        else:
            await interaction.followup.send(
                f"Watchlist: ${res.symbol} already listed (sector: {sector_disp} / {subgroup_disp}).",
                ephemeral=False,
            )

    @watchlist.command(name="remove", description="Remove a ticker from the shared watchlist (moderator-only).")
    @app_commands.describe(
        sector="Optional sector filter (required if subgroup provided)",
        subgroup="Optional subgroup filter (requires sector)",
        all_matches="Remove all matching entries (useful when a ticker appears multiple times)",
    )
    @app_commands.autocomplete(sector=_ac_watchlist_sector, subgroup=_ac_watchlist_subgroup)
    async def watchlist_remove(
        interaction: discord.Interaction,
        ticker: str,
        sector: str | None = None,
        subgroup: str | None = None,
        all_matches: bool = False,
    ) -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        if not await _ensure_moderator(interaction, exec_user_id=ctx.exec_user_id):
            return
        if subgroup and not sector:
            await _deny(interaction, "To filter by subgroup, also provide sector.")
            return
        try:
            removed = await asyncio.to_thread(
                services.watchlist_remove,
                guild_id=ctx.guild_id,
                list_kind="community",
                symbol=ticker,
                user_id=interaction.user.id,
                sector=sector,
                subgroup=subgroup,
                all_matches=bool(all_matches),
            )
        except Exception as exc:
            await _deny(interaction, str(exc))
            return
        if removed:
            plural = "entry" if int(removed) == 1 else "entries"
            await interaction.response.send_message(
                f"Watchlist: removed {int(removed)} {plural} for ${services.normalize_symbol(ticker)}.",
                ephemeral=False,
            )
        else:
            await interaction.response.send_message("Watchlist: ticker not found.", ephemeral=True)

    @watchlist.command(name="sectors", description="List sectors on the shared watchlist.")
    @app_commands.describe(format="Output style (rich or table)")
    async def watchlist_sectors(interaction: discord.Interaction, format: Literal["rich", "table"] = "rich") -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        rows = await asyncio.to_thread(services.list_sectors, guild_id=ctx.guild_id)
        if not rows:
            await interaction.response.send_message("Watchlist sectors: —", ephemeral=False)
            return
        table_rows = [[name, str(n)] for name, n in rows]
        if format == "table":
            await interaction.response.send_message(render_table(["Sector", "Count"], table_rows), ephemeral=False)
            return
        embeds = _build_rich_embeds(
            title="Community Watchlist — Sectors",
            color=0x93C5FD,
            headers=["Sector", "Count"],
            rows=table_rows,
            page_size=12,
        )
        await _send_embed_pages(interaction, embeds=embeds, deferred=False, ephemeral=False)

    @watchlist.command(name="sector_create", description="Register a sector label (moderator-only).")
    @app_commands.autocomplete(sector=_ac_watchlist_sector)
    async def watchlist_sector_create(interaction: discord.Interaction, sector: str) -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        if not await _ensure_moderator(interaction, exec_user_id=ctx.exec_user_id):
            return
        try:
            sec, _key, status = await asyncio.to_thread(
                services.sector_create,
                guild_id=ctx.guild_id,
                list_kind="community",
                sector=sector,
                user_id=interaction.user.id,
            )
        except Exception as exc:
            await _deny(interaction, str(exc))
            return
        await interaction.response.send_message(f"Watchlist sector: {sec} ({status}).", ephemeral=False)

    @watchlist.command(name="subgroup_create", description="Register a subgroup label under a sector (moderator-only).")
    @app_commands.describe(sector="Sector label to attach this subgroup under")
    @app_commands.autocomplete(sector=_ac_watchlist_sector)
    async def watchlist_subgroup_create(interaction: discord.Interaction, sector: str, subgroup: str) -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        if not await _ensure_moderator(interaction, exec_user_id=ctx.exec_user_id):
            return
        try:
            sec, _sec_key, grp, _grp_key, status = await asyncio.to_thread(
                services.subgroup_create,
                guild_id=ctx.guild_id,
                list_kind="community",
                sector=sector,
                subgroup=subgroup,
                user_id=interaction.user.id,
            )
        except Exception as exc:
            await _deny(interaction, str(exc))
            return
        await interaction.response.send_message(f"Watchlist subgroup: {sec} / {grp} ({status}).", ephemeral=False)

    @watchlist.command(name="subgroup_rename", description="Rename a subgroup label under a sector (moderator-only).")
    @app_commands.describe(sector="Sector name", old_subgroup="Existing subgroup", new_subgroup="New subgroup name")
    @app_commands.autocomplete(sector=_ac_watchlist_sector, old_subgroup=_ac_watchlist_subgroup)
    async def watchlist_subgroup_rename(
        interaction: discord.Interaction,
        sector: str,
        old_subgroup: str,
        new_subgroup: str,
    ) -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        if not await _ensure_moderator(interaction, exec_user_id=ctx.exec_user_id):
            return
        await interaction.response.defer(thinking=True)
        try:
            updated = await asyncio.to_thread(
                services.rename_subgroup,
                guild_id=ctx.guild_id,
                list_kind="community",
                sector=sector,
                old_subgroup=old_subgroup,
                new_subgroup=new_subgroup,
                user_id=interaction.user.id,
            )
        except Exception as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(
            f"Watchlist subgroup renamed: {sector} / {old_subgroup} → {new_subgroup} (updated {int(updated)} entries).",
            ephemeral=False,
        )

    @watchlist.command(name="subgroups", description="List subgroups (with counts), optionally filtered by sector.")
    @app_commands.describe(sector="If provided, only show subgroups in this sector", format="Output style (rich or table)")
    @app_commands.autocomplete(sector=_ac_watchlist_sector)
    async def watchlist_subgroups(
        interaction: discord.Interaction,
        sector: str | None = None,
        format: Literal["rich", "table"] = "rich",
    ) -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        rows = await asyncio.to_thread(services.list_subgroups, guild_id=ctx.guild_id, sector=sector)
        if not rows:
            if sector:
                await interaction.response.send_message(f"Watchlist: no subgroups in sector '{sector}'.", ephemeral=True)
            else:
                await interaction.response.send_message("Watchlist subgroups: —", ephemeral=False)
            return
        table_rows = [[sec, grp, str(n)] for sec, grp, n in rows]
        if format == "table":
            await interaction.response.send_message(render_table(["Sector", "Subgroup", "Count"], table_rows), ephemeral=False)
            return
        header_lines = [f"Sector filter: {sector}"] if sector else None
        embeds = _build_rich_embeds(
            title="Community Watchlist — Subgroups",
            color=0x93C5FD,
            headers=["Sector", "Subgroup", "Count"],
            rows=table_rows,
            header_lines=header_lines,
            page_size=10,
        )
        await _send_embed_pages(interaction, embeds=embeds, deferred=False, ephemeral=False)

    @watchlist.command(name="list", description="List the shared watchlist, optionally filtered by sector/subgroup.")
    @app_commands.describe(
        sector="If provided, only show tickers in this sector",
        subgroup="If provided, only show tickers in this subgroup (requires sector)",
        page="Page number (1-based)",
        page_size="Rows per page",
        format="Output style (rich or table)",
    )
    @app_commands.autocomplete(sector=_ac_watchlist_sector, subgroup=_ac_watchlist_subgroup)
    async def watchlist_list(
        interaction: discord.Interaction,
        sector: str | None = None,
        subgroup: str | None = None,
        page: app_commands.Range[int, 1, 200] = 1,
        page_size: app_commands.Range[int, 5, 80] = 40,
        format: Literal["rich", "table"] = "rich",
    ) -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        if subgroup and not sector:
            await _deny(interaction, "To filter by subgroup, also provide sector.")
            return
        await interaction.response.defer(thinking=True)
        refresh = await asyncio.to_thread(
            services.refresh_watchlist_quotes,
            guild_id=ctx.guild_id,
            list_kind="community",
            sector=sector,
            subgroup=subgroup,
        )
        grouped = await asyncio.to_thread(services.list_watchlist_by_sector_subgroup, guild_id=ctx.guild_id)
        entries = await asyncio.to_thread(
            services.list_watchlist_entries,
            guild_id=ctx.guild_id,
            list_kind="community",
            sector=sector,
            subgroup=subgroup,
        )
        if not grouped and not entries:
            await interaction.followup.send("Watchlist: —", ephemeral=False)
            return

        by_sym = {e.symbol: e for e in entries}

        def _find_sector_match(label: str) -> tuple[str, dict[str, list[str]]] | None:
            key = label.strip().lower()
            for sec, groups in grouped.items():
                if sec.strip().lower() == key:
                    return sec, groups
            return None

        def _find_subgroup_match(groups: dict[str, list[str]], label: str) -> tuple[str, list[str]] | None:
            key = label.strip().lower()
            for grp, syms in groups.items():
                if grp.strip().lower() == key:
                    return grp, syms
            return None

        if sector:
            match = _find_sector_match(sector)
            if not match:
                await interaction.followup.send(f"Watchlist: no sector '{sector}'.", ephemeral=True)
                return
            sec, groups = match

            if subgroup:
                sg = _find_subgroup_match(groups, subgroup)
                if not sg:
                    await interaction.followup.send(f"Watchlist: no subgroup '{subgroup}' in sector '{sec}'.", ephemeral=True)
                    return
                grp, syms = sg
                table_rows = []
                if not syms:
                    table_rows.append([sec, grp, "—", "—", "—", "—"])
                else:
                    for sym in syms:
                        e = by_sym.get(sym)
                        table_rows.append(
                            [
                                sec,
                                grp,
                                sym,
                                _short(e.company_name if e else None, 40) or "—",
                                _quote_disp((e.last_quote_price if e else None), (e.currency if e else None)),
                                _exchange_disp(e.full_exchange_name if e else None, e.exchange if e else None) or "—",
                            ]
                        )
                try:
                    page_rows, page_label = _paginate_rows(table_rows, page=int(page), page_size=int(page_size))
                except Exception as exc:
                    await interaction.followup.send(str(exc), ephemeral=True)
                    return
                header = _format_asof(refresh.asof_utc) + "\n" + page_label
                if format == "table":
                    table_headers, table_rows = _project_rows(
                        headers=["Sector", "Subgroup", "Ticker", "Name", "Last", "Exchange"],
                        rows=page_rows,
                        drop_headers={"sector", "subgroup"},
                    )
                    chunks = _table_chunks(table_headers, table_rows)
                    first = header + "\n" + chunks[0]
                    if refresh.error:
                        first += "\nMarket data unavailable (Yahoo)."
                    await interaction.followup.send(first, ephemeral=False)
                    for chunk in chunks[1:]:
                        await interaction.followup.send(chunk, ephemeral=False)
                else:
                    footer = "Market data unavailable (Yahoo). Showing cached names when available." if refresh.error else None
                    card_headers, card_rows = _project_rows(
                        headers=["Sector", "Subgroup", "Ticker", "Name", "Last", "Exchange"],
                        rows=page_rows,
                        drop_headers={"sector", "subgroup"},
                    )
                    embed = _build_rich_embed(
                        title="Community Watchlist — List",
                        color=0x93C5FD,
                        header_lines=[_format_asof(refresh.asof_utc), f"Sector: {sec}", f"Subgroup: {grp}"],
                        page_label=page_label,
                        cards=_rich_cards_from_rows(card_headers, card_rows),
                        footer=footer,
                    )
                    await _send_embed_pages(interaction, embeds=[embed], deferred=True, ephemeral=False)
                return

            table_rows = []
            if not groups:
                table_rows.append([sec, "—", "—", "—", "—", "—"])
            else:
                for grp in sorted(groups.keys(), key=lambda s: s.lower()):
                    syms = groups.get(grp) or []
                    if not syms:
                        table_rows.append([sec, grp, "—", "—", "—", "—"])
                        continue
                    for sym in syms:
                        e = by_sym.get(sym)
                        table_rows.append(
                            [
                                sec,
                                grp,
                                sym,
                                _short(e.company_name if e else None, 40) or "—",
                                _quote_disp((e.last_quote_price if e else None), (e.currency if e else None)),
                                _exchange_disp(e.full_exchange_name if e else None, e.exchange if e else None) or "—",
                            ]
                        )
            try:
                page_rows, page_label = _paginate_rows(table_rows, page=int(page), page_size=int(page_size))
            except Exception as exc:
                await interaction.followup.send(str(exc), ephemeral=True)
                return
            header = _format_asof(refresh.asof_utc) + "\n" + page_label
            if format == "table":
                table_headers, table_rows = _project_rows(
                    headers=["Sector", "Subgroup", "Ticker", "Name", "Last", "Exchange"],
                    rows=page_rows,
                    drop_headers={"sector"},
                )
                chunks = _table_chunks(table_headers, table_rows)
                first = header + "\n" + chunks[0]
                if refresh.error:
                    first += "\nMarket data unavailable (Yahoo)."
                await interaction.followup.send(first, ephemeral=False)
                for chunk in chunks[1:]:
                    await interaction.followup.send(chunk, ephemeral=False)
            else:
                footer = "Market data unavailable (Yahoo). Showing cached names when available." if refresh.error else None
                card_headers, card_rows = _project_rows(
                    headers=["Sector", "Subgroup", "Ticker", "Name", "Last", "Exchange"],
                    rows=page_rows,
                    drop_headers={"sector"},
                )
                embed = _build_rich_embed(
                    title="Community Watchlist — List",
                    color=0x93C5FD,
                    header_lines=[_format_asof(refresh.asof_utc), f"Sector: {sec}"],
                    page_label=page_label,
                    cards=_rich_cards_from_rows(card_headers, card_rows),
                    footer=footer,
                )
                await _send_embed_pages(interaction, embeds=[embed], deferred=True, ephemeral=False)
            return

        # Full watchlist table (includes empty registered sectors).
        table_rows = []
        for sec in sorted(grouped.keys(), key=lambda s: s.lower()):
            groups = grouped.get(sec) or {}
            if not groups:
                table_rows.append([sec, "—", "—", "—", "—", "—"])
                continue
            for grp in sorted(groups.keys(), key=lambda s: s.lower()):
                syms = groups.get(grp) or []
                if not syms:
                    table_rows.append([sec, grp, "—", "—", "—", "—"])
                    continue
                for sym in syms:
                    e = by_sym.get(sym)
                    table_rows.append(
                        [
                            sec,
                            grp,
                            sym,
                            _short(e.company_name if e else None, 40) or "—",
                            _quote_disp((e.last_quote_price if e else None), (e.currency if e else None)),
                            _exchange_disp(e.full_exchange_name if e else None, e.exchange if e else None) or "—",
                        ]
                    )
        try:
            page_rows, page_label = _paginate_rows(table_rows, page=int(page), page_size=int(page_size))
        except Exception as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        header = _format_asof(refresh.asof_utc) + "\n" + page_label
        if format == "table":
            chunks = _table_chunks(["Sector", "Subgroup", "Ticker", "Name", "Last", "Exchange"], page_rows)
            first = header + "\n" + chunks[0]
            if refresh.error:
                first += "\nMarket data unavailable (Yahoo)."
            await interaction.followup.send(first, ephemeral=False)
            for chunk in chunks[1:]:
                await interaction.followup.send(chunk, ephemeral=False)
            return
        footer = "Market data unavailable (Yahoo). Showing cached names when available." if refresh.error else None
        embed = _build_rich_embed(
            title="Community Watchlist — List",
            color=0x93C5FD,
            header_lines=[_format_asof(refresh.asof_utc)],
            page_label=page_label,
            cards=_rich_cards_from_rows(["Sector", "Subgroup", "Ticker", "Name", "Last", "Exchange"], page_rows),
            footer=footer,
        )
        await _send_embed_pages(interaction, embeds=[embed], deferred=True, ephemeral=False)

    @watchlist.command(name="perf", description="Show shared watchlist performance table.")
    @app_commands.describe(
        force_refresh="Force refresh (ignore cache)",
        page="Page number (1-based)",
        page_size="Rows per page",
        format="Output style (rich or table)",
    )
    async def watchlist_perf(
        interaction: discord.Interaction,
        sort: Literal["since", "m1", "ytd", "y1"] = "since",
        order: Literal["asc", "desc"] = "desc",
        force_refresh: bool = False,
        page: app_commands.Range[int, 1, 200] = 1,
        page_size: app_commands.Range[int, 5, 60] = 25,
        format: Literal["rich", "table"] = "rich",
    ) -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        await interaction.response.defer(thinking=True)
        result = await asyncio.to_thread(
            services.refresh_watchlist_perf,
            guild_id=ctx.guild_id,
            list_kind="community",
            force_refresh=force_refresh,
        )
        rows_sorted = _sort_perf_rows(result.rows, sort=sort, order=order)
        table_rows: list[list[str]] = []
        for r in rows_sorted:
            since = "PENDING" if r.baseline_pending else format_pct(r.since_24h)
            table_rows.append(
                [
                    r.sector or "Uncategorized",
                    r.subgroup or "General",
                    r.symbol,
                    _short(r.company_name, 30) or "—",
                    _exchange_disp(r.full_exchange_name, r.exchange) or "—",
                    since,
                    format_pct(r.m1),
                    format_pct(r.ytd),
                    format_pct(r.y1),
                ]
            )

        try:
            page_rows, page_label = _paginate_rows(table_rows, page=int(page), page_size=int(page_size))
        except Exception as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        header = _format_asof(result.asof_utc) + "\n" + page_label
        if format == "table":
            chunks = _table_chunks(["Sector", "Subgroup", "Ticker", "Name", "Exchange", "Since(24h)", "1M", "YTD", "1Y"], page_rows)
            first = header + "\n" + chunks[0]
            if result.error:
                first += "\nMarket data unavailable (Yahoo)."
            await interaction.followup.send(first, ephemeral=False)
            for chunk in chunks[1:]:
                await interaction.followup.send(chunk, ephemeral=False)
            return
        footer = "Market data unavailable (Yahoo). Showing cached returns when available." if result.error else None
        embed = _build_rich_embed(
            title="Community Watchlist — Performance",
            color=0x93C5FD,
            header_lines=[_format_asof(result.asof_utc), f"Sort: {sort.upper()} ({order})"],
            page_label=page_label,
            cards=_rich_cards_from_rows(["Sector", "Subgroup", "Ticker", "Name", "Exchange", "Since(24h)", "1M", "YTD", "1Y"], page_rows),
            footer=footer,
        )
        await _send_embed_pages(interaction, embeds=[embed], deferred=True, ephemeral=False)

    @watchlist.command(name="sector", description="Show performance table for a specific sector.")
    @app_commands.describe(
        sector="Sector name to filter",
        subgroup="Optional subgroup filter within this sector",
        page="Page number (1-based)",
        page_size="Rows per page",
        format="Output style (rich or table)",
    )
    @app_commands.autocomplete(sector=_ac_watchlist_sector, subgroup=_ac_watchlist_subgroup)
    async def watchlist_sector(
        interaction: discord.Interaction,
        sector: str,
        subgroup: str | None = None,
        sort: Literal["since", "m1", "ytd", "y1"] = "since",
        order: Literal["asc", "desc"] = "desc",
        force_refresh: bool = False,
        page: app_commands.Range[int, 1, 200] = 1,
        page_size: app_commands.Range[int, 5, 60] = 30,
        format: Literal["rich", "table"] = "rich",
    ) -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        await interaction.response.defer(thinking=True)
        result = await asyncio.to_thread(
            services.refresh_watchlist_perf,
            guild_id=ctx.guild_id,
            list_kind="community",
            sector=sector,
            subgroup=subgroup,
            force_refresh=force_refresh,
        )
        rows_sorted = _sort_perf_rows(result.rows, sort=sort, order=order)
        table_rows: list[list[str]] = []
        for r in rows_sorted:
            since = "PENDING" if r.baseline_pending else format_pct(r.since_24h)
            if subgroup:
                table_rows.append(
                    [
                        r.symbol,
                        _short(r.company_name, 34) or "—",
                        _exchange_disp(r.full_exchange_name, r.exchange) or "—",
                        since,
                        format_pct(r.m1),
                        format_pct(r.ytd),
                        format_pct(r.y1),
                    ]
                )
            else:
                table_rows.append(
                    [
                        r.subgroup or "General",
                        r.symbol,
                        _short(r.company_name, 34) or "—",
                        _exchange_disp(r.full_exchange_name, r.exchange) or "—",
                        since,
                        format_pct(r.m1),
                        format_pct(r.ytd),
                        format_pct(r.y1),
                    ]
                )

        try:
            page_rows, page_label = _paginate_rows(table_rows, page=int(page), page_size=int(page_size))
        except Exception as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        header = _format_asof(result.asof_utc) + "\n" + page_label
        if format == "table":
            if subgroup:
                chunks = _table_chunks(["Ticker", "Name", "Exchange", "Since(24h)", "1M", "YTD", "1Y"], page_rows)
            else:
                chunks = _table_chunks(["Subgroup", "Ticker", "Name", "Exchange", "Since(24h)", "1M", "YTD", "1Y"], page_rows)
            first = header + f"\nSector: {sector}\n" + (f"Subgroup: {subgroup}\n" if subgroup else "") + chunks[0]
            if result.error:
                first += "\nMarket data unavailable (Yahoo)."
            await interaction.followup.send(first, ephemeral=False)
            for chunk in chunks[1:]:
                await interaction.followup.send(chunk, ephemeral=False)
            return
        headers = (
            ["Ticker", "Name", "Exchange", "Since(24h)", "1M", "YTD", "1Y"]
            if subgroup
            else ["Subgroup", "Ticker", "Name", "Exchange", "Since(24h)", "1M", "YTD", "1Y"]
        )
        footer = "Market data unavailable (Yahoo). Showing cached returns when available." if result.error else None
        head = [_format_asof(result.asof_utc), f"Sector: {sector}", f"Sort: {sort.upper()} ({order})"]
        if subgroup:
            head.append(f"Subgroup: {subgroup}")
        embed = _build_rich_embed(
            title="Community Watchlist — Sector Performance",
            color=0x93C5FD,
            header_lines=head,
            page_label=page_label,
            cards=_rich_cards_from_rows(headers, page_rows),
            footer=footer,
        )
        await _send_embed_pages(interaction, embeds=[embed], deferred=True, ephemeral=False)

    @watchlist.command(name="dashboard", description="Interactive, mobile-friendly watchlist viewer.")
    @app_commands.describe(
        public="Post in-channel (default is ephemeral)",
        mode="Initial view",
        sector="Optional sector filter",
        subgroup="Optional subgroup filter (requires sector)",
        page_size="Tickers per page (approx)",
    )
    @app_commands.autocomplete(sector=_ac_watchlist_sector, subgroup=_ac_watchlist_subgroup)
    async def watchlist_dashboard(
        interaction: discord.Interaction,
        mode: Literal["list", "perf"] = "perf",
        sector: str | None = None,
        subgroup: str | None = None,
        page_size: app_commands.Range[int, 5, 40] = 10,
        public: bool = False,
    ) -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        if subgroup and not sector:
            await _deny(interaction, "To filter by subgroup, also provide sector.")
            return
        await interaction.response.defer(thinking=True, ephemeral=(not bool(public)))
        view = WatchlistDashboardView(
            ctx=ctx,
            list_kind="community",
            mode=mode,
            sector=sector,
            subgroup=subgroup,
            sort="since",
            order="desc",
            page_size=int(page_size),
            exec_only=False,
        )
        try:
            embed = await view.build_embed(refresh=False)
        except Exception as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(embed=embed, view=view, ephemeral=(not bool(public)))

    tree.add_command(watchlist, guild=guild)

    # /masterlist ... (curated thematic lists; exec edits, everyone can view)
    masterlist = app_commands.Group(name="masterlist", description="Masterlists (large thematic lists).")

    @masterlist.command(name="add", description="Add a ticker to a masterlist (exec-only).")
    @app_commands.describe(masterlist="Masterlist name", subgroup="Optional subgroup under this masterlist")
    @app_commands.autocomplete(masterlist=_ac_masterlist_name, subgroup=_ac_masterlist_subgroup)
    async def masterlist_add(
        interaction: discord.Interaction,
        masterlist: str,
        ticker: str,
        subgroup: str | None = None,
    ) -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        if not await _ensure_exec(interaction, exec_user_id=ctx.exec_user_id):
            return
        await interaction.response.defer(thinking=True)
        try:
            res = await asyncio.to_thread(
                services.watchlist_add,
                guild_id=ctx.guild_id,
                list_kind="masterlist",
                symbol=ticker,
                sector=masterlist,
                subgroup=subgroup,
                user_id=interaction.user.id,
            )
        except Exception as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        ml_disp = res.sector or masterlist
        sg_disp = res.subgroup or "General"
        if res.status in {"added", "reactivated"}:
            await asyncio.to_thread(_enqueue_watchlist_followup_jobs_debounced, guild_id=ctx.guild_id)
            await interaction.followup.send(
                f"Masterlist: added ${res.symbol} ({ml_disp} / {sg_disp}). Baseline captures after 24h.",
                ephemeral=False,
            )
        elif res.status == "updated":
            await asyncio.to_thread(_enqueue_watchlist_followup_jobs_debounced, guild_id=ctx.guild_id)
            await interaction.followup.send(
                f"Masterlist: updated ${res.symbol} to {ml_disp} / {sg_disp}.",
                ephemeral=False,
            )
        else:
            await interaction.followup.send(
                f"Masterlist: ${res.symbol} already listed ({ml_disp} / {sg_disp}).",
                ephemeral=False,
            )

    @masterlist.command(name="remove", description="Remove a ticker from masterlists (exec-only).")
    @app_commands.describe(
        masterlist="Optional masterlist filter (required if subgroup provided)",
        subgroup="Optional subgroup filter (requires masterlist)",
        all_matches="Remove all matching entries (useful when a ticker appears multiple times)",
    )
    @app_commands.autocomplete(masterlist=_ac_masterlist_name, subgroup=_ac_masterlist_subgroup)
    async def masterlist_remove(
        interaction: discord.Interaction,
        ticker: str,
        masterlist: str | None = None,
        subgroup: str | None = None,
        all_matches: bool = False,
    ) -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        if not await _ensure_exec(interaction, exec_user_id=ctx.exec_user_id):
            return
        if subgroup and not masterlist:
            await _deny(interaction, "To filter by subgroup, also provide masterlist.")
            return
        try:
            removed = await asyncio.to_thread(
                services.watchlist_remove,
                guild_id=ctx.guild_id,
                list_kind="masterlist",
                symbol=ticker,
                user_id=interaction.user.id,
                sector=masterlist,
                subgroup=subgroup,
                all_matches=bool(all_matches),
            )
        except Exception as exc:
            await _deny(interaction, str(exc))
            return
        if removed:
            plural = "entry" if int(removed) == 1 else "entries"
            await interaction.response.send_message(
                f"Masterlist: removed {int(removed)} {plural} for ${services.normalize_symbol(ticker)}.",
                ephemeral=False,
            )
        else:
            await interaction.response.send_message("Masterlist: ticker not found.", ephemeral=True)

    @masterlist.command(name="create", description="Register a masterlist label (even with 0 tickers).")
    @app_commands.autocomplete(masterlist=_ac_masterlist_name)
    async def masterlist_create(interaction: discord.Interaction, masterlist: str) -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        if not await _ensure_exec(interaction, exec_user_id=ctx.exec_user_id):
            return
        try:
            sec, _key, status = await asyncio.to_thread(
                services.sector_create,
                guild_id=ctx.guild_id,
                list_kind="masterlist",
                sector=masterlist,
                user_id=interaction.user.id,
            )
        except Exception as exc:
            await _deny(interaction, str(exc))
            return
        await interaction.response.send_message(f"Masterlist: {sec} ({status}).", ephemeral=False)

    @masterlist.command(name="subgroup_create", description="Register a subgroup label under a masterlist (even with 0 tickers).")
    @app_commands.autocomplete(masterlist=_ac_masterlist_name, subgroup=_ac_masterlist_subgroup)
    async def masterlist_subgroup_create(interaction: discord.Interaction, masterlist: str, subgroup: str) -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        if not await _ensure_exec(interaction, exec_user_id=ctx.exec_user_id):
            return
        try:
            sec, _sec_key, grp, _grp_key, status = await asyncio.to_thread(
                services.subgroup_create,
                guild_id=ctx.guild_id,
                list_kind="masterlist",
                sector=masterlist,
                subgroup=subgroup,
                user_id=interaction.user.id,
            )
        except Exception as exc:
            await _deny(interaction, str(exc))
            return
        await interaction.response.send_message(f"Masterlist subgroup: {sec} / {grp} ({status}).", ephemeral=False)

    @masterlist.command(name="subgroup_rename", description="Rename a subgroup label under a masterlist (exec-only).")
    @app_commands.describe(masterlist="Masterlist name", old_subgroup="Existing subgroup", new_subgroup="New subgroup name")
    @app_commands.autocomplete(masterlist=_ac_masterlist_name, old_subgroup=_ac_masterlist_subgroup)
    async def masterlist_subgroup_rename(
        interaction: discord.Interaction,
        masterlist: str,
        old_subgroup: str,
        new_subgroup: str,
    ) -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        if not await _ensure_exec(interaction, exec_user_id=ctx.exec_user_id):
            return
        await interaction.response.defer(thinking=True)
        try:
            updated = await asyncio.to_thread(
                services.rename_subgroup,
                guild_id=ctx.guild_id,
                list_kind="masterlist",
                sector=masterlist,
                old_subgroup=old_subgroup,
                new_subgroup=new_subgroup,
                user_id=interaction.user.id,
            )
        except Exception as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(
            f"Masterlist subgroup renamed: {masterlist} / {old_subgroup} → {new_subgroup} (updated {int(updated)} entries).",
            ephemeral=False,
        )

    @masterlist.command(name="lists", description="List masterlists with counts.")
    @app_commands.describe(format="Output style (rich or table)")
    async def masterlist_lists(interaction: discord.Interaction, format: Literal["rich", "table"] = "rich") -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        rows = await asyncio.to_thread(services.list_sectors, guild_id=ctx.guild_id, list_kind="masterlist")
        if not rows:
            await interaction.response.send_message("Masterlists: —", ephemeral=False)
            return
        table_rows = [[name, str(n)] for name, n in rows]
        if format == "table":
            await interaction.response.send_message(render_table(["Masterlist", "Count"], table_rows), ephemeral=False)
            return
        embeds = _build_rich_embeds(
            title="Masterlists — Lists",
            color=0xA78BFA,
            headers=["Masterlist", "Count"],
            rows=table_rows,
            page_size=12,
        )
        await _send_embed_pages(interaction, embeds=embeds, deferred=False, ephemeral=False)

    @masterlist.command(name="subgroups", description="List masterlist subgroups (with counts), optionally filtered by masterlist.")
    @app_commands.describe(format="Output style (rich or table)")
    @app_commands.autocomplete(masterlist=_ac_masterlist_name)
    async def masterlist_subgroups(
        interaction: discord.Interaction,
        masterlist: str | None = None,
        format: Literal["rich", "table"] = "rich",
    ) -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        rows = await asyncio.to_thread(services.list_subgroups, guild_id=ctx.guild_id, list_kind="masterlist", sector=masterlist)
        if not rows:
            if masterlist:
                await interaction.response.send_message(f"Masterlist: no subgroups in '{masterlist}'.", ephemeral=True)
            else:
                await interaction.response.send_message("Masterlist subgroups: —", ephemeral=False)
            return
        table_rows = [[sec, grp, str(n)] for sec, grp, n in rows]
        if format == "table":
            await interaction.response.send_message(render_table(["Masterlist", "Subgroup", "Count"], table_rows), ephemeral=False)
            return
        header_lines = [f"Masterlist filter: {masterlist}"] if masterlist else None
        embeds = _build_rich_embeds(
            title="Masterlists — Subgroups",
            color=0xA78BFA,
            headers=["Masterlist", "Subgroup", "Count"],
            rows=table_rows,
            header_lines=header_lines,
            page_size=10,
        )
        await _send_embed_pages(interaction, embeds=embeds, deferred=False, ephemeral=False)

    @masterlist.command(name="list", description="List masterlist tickers, optionally filtered by masterlist/subgroup.")
    @app_commands.describe(
        masterlist="If provided, only show tickers in this masterlist",
        subgroup="If provided, only show tickers in this subgroup (requires masterlist)",
        page="Page number (1-based)",
        page_size="Rows per page",
        format="Output style (rich or table)",
    )
    @app_commands.autocomplete(masterlist=_ac_masterlist_name, subgroup=_ac_masterlist_subgroup)
    async def masterlist_list(
        interaction: discord.Interaction,
        masterlist: str | None = None,
        subgroup: str | None = None,
        page: app_commands.Range[int, 1, 200] = 1,
        page_size: app_commands.Range[int, 5, 80] = 40,
        format: Literal["rich", "table"] = "rich",
    ) -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        if subgroup and not masterlist:
            await _deny(interaction, "To filter by subgroup, also provide masterlist.")
            return
        await interaction.response.defer(thinking=True)
        refresh = await asyncio.to_thread(
            services.refresh_watchlist_quotes,
            guild_id=ctx.guild_id,
            list_kind="masterlist",
            sector=masterlist,
            subgroup=subgroup,
        )
        grouped = await asyncio.to_thread(
            services.list_watchlist_by_sector_subgroup,
            guild_id=ctx.guild_id,
            list_kind="masterlist",
        )
        entries = await asyncio.to_thread(
            services.list_watchlist_entries,
            guild_id=ctx.guild_id,
            list_kind="masterlist",
            sector=masterlist,
            subgroup=subgroup,
        )
        if not grouped and not entries:
            await interaction.followup.send("Masterlists: —", ephemeral=False)
            return

        by_sym = {e.symbol: e for e in entries}

        def _find_sector_match(label: str) -> tuple[str, dict[str, list[str]]] | None:
            key = label.strip().lower()
            for sec, groups in grouped.items():
                if sec.strip().lower() == key:
                    return sec, groups
            return None

        def _find_subgroup_match(groups: dict[str, list[str]], label: str) -> tuple[str, list[str]] | None:
            key = label.strip().lower()
            for grp, syms in groups.items():
                if grp.strip().lower() == key:
                    return grp, syms
            return None

        if masterlist:
            match = _find_sector_match(masterlist)
            if not match:
                await interaction.followup.send(f"Masterlist: no list '{masterlist}'.", ephemeral=True)
                return
            sec, groups = match

            if subgroup:
                sg = _find_subgroup_match(groups, subgroup)
                if not sg:
                    await interaction.followup.send(f"Masterlist: no subgroup '{subgroup}' in '{sec}'.", ephemeral=True)
                    return
                grp, syms = sg
                table_rows: list[list[str]] = []
                if not syms:
                    table_rows.append([sec, grp, "—", "—", "—", "—"])
                else:
                    for sym in syms:
                        e = by_sym.get(sym)
                        table_rows.append(
                            [
                                sec,
                                grp,
                                sym,
                                _short(e.company_name if e else None, 40) or "—",
                                _quote_disp((e.last_quote_price if e else None), (e.currency if e else None)),
                                _exchange_disp(e.full_exchange_name if e else None, e.exchange if e else None) or "—",
                            ]
                        )
                try:
                    page_rows, page_label = _paginate_rows(table_rows, page=int(page), page_size=int(page_size))
                except Exception as exc:
                    await interaction.followup.send(str(exc), ephemeral=True)
                    return
                header = _format_asof(refresh.asof_utc) + "\n" + page_label
                if format == "table":
                    table_headers, table_rows = _project_rows(
                        headers=["Masterlist", "Subgroup", "Ticker", "Name", "Last", "Exchange"],
                        rows=page_rows,
                        drop_headers={"masterlist", "subgroup"},
                    )
                    chunks = _table_chunks(table_headers, table_rows)
                    first = header + "\n" + chunks[0]
                    if refresh.error:
                        first += "\nMarket data unavailable (Yahoo)."
                    await interaction.followup.send(first, ephemeral=False)
                    for chunk in chunks[1:]:
                        await interaction.followup.send(chunk, ephemeral=False)
                else:
                    footer = "Market data unavailable (Yahoo). Showing cached names when available." if refresh.error else None
                    card_headers, card_rows = _project_rows(
                        headers=["Masterlist", "Subgroup", "Ticker", "Name", "Last", "Exchange"],
                        rows=page_rows,
                        drop_headers={"masterlist", "subgroup"},
                    )
                    embed = _build_rich_embed(
                        title="Masterlists — List",
                        color=0xA78BFA,
                        header_lines=[_format_asof(refresh.asof_utc), f"Masterlist: {sec}", f"Subgroup: {grp}"],
                        page_label=page_label,
                        cards=_compact_listing_cards(
                            rows=card_rows,
                            ticker_idx=0,
                            name_idx=1,
                            price_idx=2,
                            exchange_idx=3,
                        ),
                        footer=footer,
                    )
                    await _send_embed_pages(interaction, embeds=[embed], deferred=True, ephemeral=False)
                return

            table_rows = []
            if not groups:
                table_rows.append([sec, "—", "—", "—", "—", "—"])
            else:
                for grp in sorted(groups.keys(), key=lambda s: s.lower()):
                    syms = groups.get(grp) or []
                    if not syms:
                        table_rows.append([sec, grp, "—", "—", "—", "—"])
                        continue
                    for sym in syms:
                        e = by_sym.get(sym)
                        table_rows.append(
                            [
                                sec,
                                grp,
                                sym,
                                _short(e.company_name if e else None, 40) or "—",
                                _quote_disp((e.last_quote_price if e else None), (e.currency if e else None)),
                                _exchange_disp(e.full_exchange_name if e else None, e.exchange if e else None) or "—",
                            ]
                        )
            try:
                page_rows, page_label = _paginate_rows(table_rows, page=int(page), page_size=int(page_size))
            except Exception as exc:
                await interaction.followup.send(str(exc), ephemeral=True)
                return
            header = _format_asof(refresh.asof_utc) + "\n" + page_label
            if format == "table":
                table_headers, table_rows = _project_rows(
                    headers=["Masterlist", "Subgroup", "Ticker", "Name", "Last", "Exchange"],
                    rows=page_rows,
                    drop_headers={"masterlist"},
                )
                chunks = _table_chunks(table_headers, table_rows)
                first = header + "\n" + chunks[0]
                if refresh.error:
                    first += "\nMarket data unavailable (Yahoo)."
                await interaction.followup.send(first, ephemeral=False)
                for chunk in chunks[1:]:
                    await interaction.followup.send(chunk, ephemeral=False)
            else:
                footer = "Market data unavailable (Yahoo). Showing cached names when available." if refresh.error else None
                card_headers, card_rows = _project_rows(
                    headers=["Masterlist", "Subgroup", "Ticker", "Name", "Last", "Exchange"],
                    rows=page_rows,
                    drop_headers={"masterlist"},
                )
                embed = _build_rich_embed(
                    title="Masterlists — List",
                    color=0xA78BFA,
                    header_lines=[_format_asof(refresh.asof_utc), f"Masterlist: {sec}"],
                    page_label=page_label,
                    cards=_compact_listing_cards(
                        rows=card_rows,
                        ticker_idx=1,
                        name_idx=2,
                        price_idx=3,
                        exchange_idx=4,
                        group_idx=[0],
                    ),
                    footer=footer,
                )
                await _send_embed_pages(interaction, embeds=[embed], deferred=True, ephemeral=False)
            return

        table_rows = []
        for sec in sorted(grouped.keys(), key=lambda s: s.lower()):
            groups = grouped.get(sec) or {}
            if not groups:
                table_rows.append([sec, "—", "—", "—", "—", "—"])
                continue
            for grp in sorted(groups.keys(), key=lambda s: s.lower()):
                syms = groups.get(grp) or []
                if not syms:
                    table_rows.append([sec, grp, "—", "—", "—", "—"])
                    continue
                for sym in syms:
                    e = by_sym.get(sym)
                    table_rows.append(
                        [
                            sec,
                            grp,
                            sym,
                            _short(e.company_name if e else None, 40) or "—",
                            _quote_disp((e.last_quote_price if e else None), (e.currency if e else None)),
                            _exchange_disp(e.full_exchange_name if e else None, e.exchange if e else None) or "—",
                        ]
                    )
        try:
            page_rows, page_label = _paginate_rows(table_rows, page=int(page), page_size=int(page_size))
        except Exception as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        header = _format_asof(refresh.asof_utc) + "\n" + page_label
        if format == "table":
            chunks = _table_chunks(["Masterlist", "Subgroup", "Ticker", "Name", "Last", "Exchange"], page_rows)
            first = header + "\n" + chunks[0]
            if refresh.error:
                first += "\nMarket data unavailable (Yahoo)."
            await interaction.followup.send(first, ephemeral=False)
            for chunk in chunks[1:]:
                await interaction.followup.send(chunk, ephemeral=False)
            return
        footer = "Market data unavailable (Yahoo). Showing cached names when available." if refresh.error else None
        embed = _build_rich_embed(
            title="Masterlists — List",
            color=0xA78BFA,
            header_lines=[_format_asof(refresh.asof_utc)],
            page_label=page_label,
            cards=_compact_listing_cards(
                rows=page_rows,
                ticker_idx=2,
                name_idx=3,
                price_idx=4,
                exchange_idx=5,
                group_idx=[0, 1],
            ),
            footer=footer,
        )
        await _send_embed_pages(interaction, embeds=[embed], deferred=True, ephemeral=False)

    @masterlist.command(name="perf", description="Show masterlist performance table.")
    @app_commands.describe(
        force_refresh="Force refresh (ignore cache)",
        page="Page number (1-based)",
        page_size="Rows per page",
        format="Output style (rich or table)",
    )
    async def masterlist_perf(
        interaction: discord.Interaction,
        sort: Literal["since", "m1", "ytd", "y1"] = "since",
        order: Literal["asc", "desc"] = "desc",
        force_refresh: bool = False,
        page: app_commands.Range[int, 1, 200] = 1,
        page_size: app_commands.Range[int, 5, 60] = 25,
        format: Literal["rich", "table"] = "rich",
    ) -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        await interaction.response.defer(thinking=True)
        result = await asyncio.to_thread(
            services.refresh_watchlist_perf,
            guild_id=ctx.guild_id,
            list_kind="masterlist",
            force_refresh=force_refresh,
        )
        rows_sorted = _sort_perf_rows(result.rows, sort=sort, order=order)
        table_rows: list[list[str]] = []
        for r in rows_sorted:
            since = "PENDING" if r.baseline_pending else format_pct(r.since_24h)
            table_rows.append(
                [
                    r.sector or "—",
                    r.subgroup or "General",
                    r.symbol,
                    _short(r.company_name, 30) or "—",
                    _exchange_disp(r.full_exchange_name, r.exchange) or "—",
                    since,
                    format_pct(r.m1),
                    format_pct(r.ytd),
                    format_pct(r.y1),
                ]
            )

        try:
            page_rows, page_label = _paginate_rows(table_rows, page=int(page), page_size=int(page_size))
        except Exception as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        header = _format_asof(result.asof_utc) + "\n" + page_label
        if format == "table":
            chunks = _table_chunks(["Masterlist", "Subgroup", "Ticker", "Name", "Exchange", "Since(24h)", "1M", "YTD", "1Y"], page_rows)
            first = header + "\n" + chunks[0]
            if result.error:
                first += "\nMarket data unavailable (Yahoo)."
            await interaction.followup.send(first, ephemeral=False)
            for chunk in chunks[1:]:
                await interaction.followup.send(chunk, ephemeral=False)
            return
        footer = "Market data unavailable (Yahoo). Showing cached returns when available." if result.error else None
        embed = _build_rich_embed(
            title="Masterlists — Performance",
            color=0xA78BFA,
            header_lines=[_format_asof(result.asof_utc), f"Sort: {sort.upper()} ({order})"],
            page_label=page_label,
            cards=_rich_cards_from_rows(["Masterlist", "Subgroup", "Ticker", "Name", "Exchange", "Since(24h)", "1M", "YTD", "1Y"], page_rows),
            footer=footer,
        )
        await _send_embed_pages(interaction, embeds=[embed], deferred=True, ephemeral=False)

    @masterlist.command(name="dashboard", description="Interactive, mobile-friendly masterlist viewer.")
    @app_commands.describe(
        public="Post in-channel (default is ephemeral)",
        mode="Initial view",
        masterlist="Optional masterlist filter",
        subgroup="Optional subgroup filter (requires masterlist)",
        page_size="Tickers per page (approx)",
    )
    @app_commands.autocomplete(masterlist=_ac_masterlist_name, subgroup=_ac_masterlist_subgroup)
    async def masterlist_dashboard(
        interaction: discord.Interaction,
        mode: Literal["list", "perf"] = "perf",
        masterlist: str | None = None,
        subgroup: str | None = None,
        page_size: app_commands.Range[int, 5, 40] = 10,
        public: bool = False,
    ) -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        if subgroup and not masterlist:
            await _deny(interaction, "To filter by subgroup, also provide masterlist.")
            return
        await interaction.response.defer(thinking=True, ephemeral=(not bool(public)))
        view = WatchlistDashboardView(
            ctx=ctx,
            list_kind="masterlist",
            mode=mode,
            sector=masterlist,
            subgroup=subgroup,
            sort="since",
            order="desc",
            page_size=int(page_size),
            exec_only=False,
        )
        try:
            embed = await view.build_embed(refresh=False)
        except Exception as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(embed=embed, view=view, ephemeral=(not bool(public)))

    if _operator_commands_enabled():
        tree.add_command(masterlist, guild=guild)

    # /src list
    src = app_commands.Group(name="src", description="Ticker sources (evidence links).")

    @src.command(name="list", description="List stored sources for a ticker.")
    @app_commands.describe(format="Output style (rich or table)")
    async def src_list(
        interaction: discord.Interaction,
        ticker: str,
        limit: int = 5,
        format: Literal["rich", "table"] = "rich",
    ) -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        try:
            rows = await asyncio.to_thread(services.list_sources, guild_id=ctx.guild_id, symbol=ticker, limit=limit)
        except Exception as exc:
            await _deny(interaction, str(exc))
            return
        if not rows:
            await interaction.response.send_message(f"${services.normalize_symbol(ticker)} sources: —", ephemeral=False)
            return
        table_rows: list[list[str]] = []
        for r in rows:
            pid = format_src_id(r.id)
            table_rows.append([pid, _short(r.title, 64) or "—", _short(r.url, 96) or "—"])
        if format == "table":
            msg = render_table(["ID", "Title", "URL"], table_rows)
            await interaction.response.send_message(msg, ephemeral=False)
            return
        embeds = _build_rich_embeds(
            title=f"{services.normalize_symbol(ticker)} — Sources",
            color=0x6EE7B7,
            headers=["ID", "Title", "URL"],
            rows=table_rows,
            page_size=8,
        )
        await _send_embed_pages(interaction, embeds=embeds, deferred=False, ephemeral=False)

    if _operator_commands_enabled():
        tree.add_command(src, guild=guild)

    # Executive sources (/xsrc ...)
    xsrc = app_commands.Group(name="xsrc", description="Executive evidence commands.")

    @xsrc.command(name="add", description="Add evidence (source link) to a ticker and broadcast it.")
    @app_commands.describe(tags="Comma-separated tags (optional)")
    async def xsrc_add(
        interaction: discord.Interaction,
        ticker: str,
        title: str,
        url: str,
        note: str | None = None,
        sentiment: Literal["bullish", "neutral", "bearish", "mixed"] | None = None,
        type: Literal["filing", "earnings", "macro", "research", "other"] = "other",
        tags: str | None = None,
    ) -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        if not await _ensure_exec(interaction, exec_user_id=ctx.exec_user_id):
            return
        await interaction.response.defer(thinking=True)
        note_v = _unescape_newlines(note)
        try:
            src_row = await asyncio.to_thread(
                services.create_ticker_source,
                guild_id=ctx.guild_id,
                symbol=ticker,
                title=title,
                url=url,
                note=note_v,
                sentiment=sentiment,
                tags=services.split_csv(tags),
                source_type=type,
                user_id=interaction.user.id,
            )
        except Exception as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        embed = discord.Embed(title=f"Evidence Added — ${src_row.symbol}", color=0x6EE7B7)
        embed.add_field(name="Title", value=src_row.title[:1024], inline=False)
        embed.add_field(name="Link", value=src_row.url[:1024], inline=False)
        if src_row.note:
            embed.add_field(name="Executive Note", value=src_row.note[:1024], inline=False)
        if src_row.sentiment:
            embed.add_field(name="Sentiment", value=src_row.sentiment, inline=True)
        embed.add_field(name="Type", value=src_row.source_type, inline=True)
        if src_row.tags:
            embed.add_field(name="Tags", value=", ".join(src_row.tags)[:1024], inline=True)
        embed.add_field(name="ID", value=format_src_id(src_row.id), inline=True)
        embed.set_footer(text=f"Added by {interaction.user.display_name}")
        msg = await interaction.followup.send(embed=embed, ephemeral=False)
        await asyncio.to_thread(services.update_source_discord_ids, source_id=src_row.id, message_id=msg.id, channel_id=msg.channel.id)

    @xsrc.command(name="pin", description="Pin a source by ID.")
    async def xsrc_pin(interaction: discord.Interaction, id: str) -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        if not await _ensure_exec(interaction, exec_user_id=ctx.exec_user_id):
            return
        try:
            source_id = _parse_prefixed_id(id, prefix="SRC")
        except Exception as exc:
            await _deny(interaction, str(exc))
            return
        ok = await asyncio.to_thread(services.set_source_pinned, source_id=source_id, pinned=True)
        if ok:
            await interaction.response.send_message(f"Pinned {format_src_id(source_id)}.", ephemeral=False)
        else:
            await interaction.response.send_message("Source not found.", ephemeral=True)

    @xsrc.command(name="unpin", description="Unpin a source by ID.")
    async def xsrc_unpin(interaction: discord.Interaction, id: str) -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        if not await _ensure_exec(interaction, exec_user_id=ctx.exec_user_id):
            return
        try:
            source_id = _parse_prefixed_id(id, prefix="SRC")
        except Exception as exc:
            await _deny(interaction, str(exc))
            return
        ok = await asyncio.to_thread(services.set_source_pinned, source_id=source_id, pinned=False)
        if ok:
            await interaction.response.send_message(f"Unpinned {format_src_id(source_id)}.", ephemeral=False)
        else:
            await interaction.response.send_message("Source not found.", ephemeral=True)

    @xsrc.command(name="sentiment", description="Set/clear sentiment for a source by ID.")
    async def xsrc_sentiment(
        interaction: discord.Interaction,
        id: str,
        sentiment: Literal["bullish", "neutral", "bearish", "mixed", "clear"],
    ) -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        if not await _ensure_exec(interaction, exec_user_id=ctx.exec_user_id):
            return
        try:
            source_id = _parse_prefixed_id(id, prefix="SRC")
        except Exception as exc:
            await _deny(interaction, str(exc))
            return
        try:
            ok = await asyncio.to_thread(services.set_source_sentiment, source_id=source_id, sentiment=sentiment)
        except Exception as exc:
            await _deny(interaction, str(exc))
            return
        if not ok:
            await interaction.response.send_message("Source not found.", ephemeral=True)
            return

        disp = "cleared" if (sentiment or "").strip().lower() == "clear" else sentiment
        await interaction.response.send_message(f"{format_src_id(source_id)} sentiment: {disp}.", ephemeral=False)

    if _operator_commands_enabled():
        tree.add_command(xsrc, guild=guild)

    # Executive notes (/xnote ...)
    xnote = app_commands.Group(name="xnote", description="Executive quick notes (saved to Thoth).")

    @xnote.command(name="add", description="Save a quick executive note into Thoth (note_type=discord).")
    async def xnote_add(
        interaction: discord.Interaction,
        subject: str,
        text: str,
        sentiment: Literal["bullish", "neutral", "bearish", "mixed"] | None = None,
    ) -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        if not await _ensure_exec(interaction, exec_user_id=ctx.exec_user_id):
            return
        await interaction.response.defer(thinking=True)
        text_v = _unescape_newlines(text) or ""
        try:
            def _save() -> int:
                from thoth_terminal.db import session_scope

                with session_scope() as session:
                    note_row = notes_bridge.create_exec_manual_note(
                        session,
                        guild_id=ctx.guild_id,
                        interaction_id=int(interaction.id),
                        channel_id=int(interaction.channel_id or 0),
                        user_id=interaction.user.id,
                        subject=subject,
                        text=text_v,
                        sentiment=sentiment,
                    )
                    return int(note_row.id)

            note_id = await asyncio.to_thread(_save)
        except Exception as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        link = notes_bridge.note_url(note_id)
        embed = discord.Embed(title="Executive Note", color=0xFDE68A)
        embed.add_field(name="Subject", value=(subject or "").strip()[:1024], inline=False)
        if sentiment:
            embed.add_field(name="Sentiment", value=sentiment, inline=True)
        snippet = (text_v or "").strip()
        if len(snippet) > 900:
            snippet = snippet[:900] + "…"
        embed.add_field(name="Text", value=snippet[:1024] or "(empty)", inline=False)
        embed.add_field(name="Thoth", value=link[:1024], inline=False)
        embed.set_footer(text=f"Saved by {interaction.user.display_name}")
        await interaction.followup.send(embed=embed, ephemeral=False)

    if _operator_commands_enabled():
        tree.add_command(xnote, guild=guild)

    # Context menu: Save any message into Thoth (operator-only when legacy commands are enabled)
    async def save_to_thoth(interaction: discord.Interaction, message: discord.Message) -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        if not await _ensure_exec(interaction, exec_user_id=ctx.exec_user_id):
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            def _save() -> int:
                from thoth_terminal.db import session_scope

                with session_scope() as session:
                    note_row = notes_bridge.create_exec_message_capture(
                        session,
                        guild_id=ctx.guild_id,
                        channel_id=int(message.channel.id),
                        message_id=int(message.id),
                        message_author=(
                            (getattr(getattr(message, "author", None), "display_name", None))
                            or (getattr(getattr(message, "author", None), "name", None))
                            or None
                        ),
                        message_content=str(getattr(message, "content", "") or ""),
                        user_id=interaction.user.id,
                        message_created_at=getattr(message, "created_at", None),
                    )
                    return int(note_row.id)

            note_id = await asyncio.to_thread(_save)
        except Exception as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        await interaction.followup.send(f"Saved: {notes_bridge.note_url(note_id)}", ephemeral=True)

    if _operator_commands_enabled():
        tree.context_menu(name="Save to Thoth", guild=guild)(save_to_thoth)

    # Executive ideas (/xidea ...)
    xidea = app_commands.Group(name="xidea", description="Executive ideas (canonical reasoning cards).")

    @xidea.command(name="publish", description="Publish an executive idea card.")
    async def xidea_publish(
        interaction: discord.Interaction,
        ticker: str,
        thesis: str,
        horizon: Literal["days", "weeks", "months", "years"] = "months",
        horizon_n: app_commands.Range[int, 1, 120] = 1,
        catalysts: str | None = None,
        invalidation: str | None = None,
        levels: str | None = None,
        confidence: app_commands.Range[int, 1, 5] = 3,
    ) -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        if not await _ensure_exec(interaction, exec_user_id=ctx.exec_user_id):
            return
        await interaction.response.defer(thinking=True)
        thesis_v = _unescape_newlines(thesis) or ""
        catalysts_v = _unescape_newlines(catalysts)
        invalidation_v = _unescape_newlines(invalidation)
        levels_v = _unescape_newlines(levels)
        try:
            idea = await asyncio.to_thread(
                services.create_exec_idea,
                guild_id=ctx.guild_id,
                symbol=ticker,
                horizon=horizon,
                horizon_n=int(horizon_n),
                thesis=thesis_v,
                catalysts=catalysts_v,
                invalidation=invalidation_v,
                levels=levels_v,
                confidence=int(confidence),
                user_id=interaction.user.id,
            )
        except Exception as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        # Mirror into Thoth as a note for search/browse.
        try:
            def _mirror() -> int:
                from thoth_terminal.db import session_scope

                with session_scope() as session:
                    n = notes_bridge.upsert_note_for_exec_idea(
                        session,
                        guild_id=ctx.guild_id,
                        channel_id=int(interaction.channel_id or 0),
                        message_id=None,
                        user_id=interaction.user.id,
                        idea_id=idea.id,
                        symbol=idea.symbol,
                        horizon=idea.horizon,
                        horizon_n=getattr(idea, "horizon_n", 1),
                        thesis=idea.thesis,
                        catalysts=idea.catalysts,
                        invalidation=idea.invalidation,
                        levels=idea.levels,
                        confidence=idea.confidence,
                        status=idea.status,
                    )
                    return int(n.id)

            note_id = await asyncio.to_thread(_mirror)
            thoth_link = notes_bridge.note_url(note_id)
        except Exception:
            thoth_link = None

        embed = discord.Embed(title=f"Executive Idea — ${idea.symbol}", color=0x93C5FD)
        unit = (idea.horizon or "").strip()
        n = int(getattr(idea, "horizon_n", 1) or 1)
        singular = unit[:-1] if unit.endswith("s") else unit
        unit_disp = singular if n == 1 else unit
        embed.add_field(name="Horizon", value=f"{n} {unit_disp}", inline=True)
        embed.add_field(name="Confidence", value=str(idea.confidence), inline=True)
        embed.add_field(name="ID", value=format_exidea_id(idea.id), inline=True)
        if thoth_link:
            embed.add_field(name="Thoth", value=thoth_link[:1024], inline=False)
        embed.add_field(name="Thesis", value=(idea.thesis or "")[:1024], inline=False)
        if idea.catalysts:
            embed.add_field(name="Catalysts", value=idea.catalysts[:1024], inline=False)
        if idea.invalidation:
            embed.add_field(name="Invalidation", value=idea.invalidation[:1024], inline=False)
        if idea.levels:
            embed.add_field(name="Levels", value=idea.levels[:1024], inline=False)
        embed.set_footer(text=f"Published by {interaction.user.display_name}")
        msg = await interaction.followup.send(embed=embed, ephemeral=False)
        await asyncio.to_thread(services.update_exec_idea_discord_ids, idea_id=idea.id, message_id=msg.id, channel_id=msg.channel.id)

        if thoth_link:
            try:
                def _mirror_update() -> None:
                    from thoth_terminal.db import session_scope

                    with session_scope() as session:
                        notes_bridge.upsert_note_for_exec_idea(
                            session,
                            guild_id=ctx.guild_id,
                            channel_id=int(msg.channel.id),
                            message_id=int(msg.id),
                            user_id=interaction.user.id,
                            idea_id=idea.id,
                            symbol=idea.symbol,
                            horizon=idea.horizon,
                            horizon_n=getattr(idea, "horizon_n", 1),
                            thesis=idea.thesis,
                            catalysts=idea.catalysts,
                            invalidation=idea.invalidation,
                            levels=idea.levels,
                            confidence=idea.confidence,
                            status=idea.status,
                        )

                await asyncio.to_thread(_mirror_update)
            except Exception:
                pass

    @xidea.command(name="update", description="Append an update note to an executive idea.")
    async def xidea_update(
        interaction: discord.Interaction,
        id: str,
        note: str,
        confidence: app_commands.Range[int, 1, 5] | None = None,
    ) -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        if not await _ensure_exec(interaction, exec_user_id=ctx.exec_user_id):
            return
        try:
            idea_id = _parse_prefixed_id(id, prefix="EXIDEA")
        except Exception as exc:
            await _deny(interaction, str(exc))
            return
        try:
            note_v = _unescape_newlines(note) or ""
            idea = await asyncio.to_thread(services.update_exec_idea, idea_id=idea_id, note=note_v, confidence=confidence)
        except Exception as exc:
            await _deny(interaction, str(exc))
            return
        # Best-effort: keep the mirrored Thoth note updated.
        try:
            def _mirror_update() -> None:
                from thoth_terminal.db import session_scope

                with session_scope() as session:
                    notes_bridge.upsert_note_for_exec_idea(
                        session,
                        guild_id=ctx.guild_id,
                        channel_id=int(getattr(idea, "discord_channel_id", None) or interaction.channel_id or 0),
                        message_id=(int(getattr(idea, "discord_message_id", 0)) if getattr(idea, "discord_message_id", None) else None),
                        user_id=interaction.user.id,
                        idea_id=idea.id,
                        symbol=idea.symbol,
                        horizon=idea.horizon,
                        horizon_n=getattr(idea, "horizon_n", 1),
                        thesis=idea.thesis,
                        catalysts=idea.catalysts,
                        invalidation=idea.invalidation,
                        levels=idea.levels,
                        confidence=idea.confidence,
                        status=idea.status,
                    )

            await asyncio.to_thread(_mirror_update)
        except Exception:
            pass
        await interaction.response.send_message(f"Updated {format_exidea_id(idea_id)}.", ephemeral=False)

    @xidea.command(name="close", description="Close an executive idea.")
    async def xidea_close(
        interaction: discord.Interaction,
        id: str,
        outcome: Literal["hit", "miss", "mixed"],
        note: str | None = None,
    ) -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        if not await _ensure_exec(interaction, exec_user_id=ctx.exec_user_id):
            return
        try:
            idea_id = _parse_prefixed_id(id, prefix="EXIDEA")
        except Exception as exc:
            await _deny(interaction, str(exc))
            return
        try:
            note_v = _unescape_newlines(note)
            idea = await asyncio.to_thread(services.close_exec_idea, idea_id=idea_id, outcome=outcome, note=note_v)
        except Exception as exc:
            await _deny(interaction, str(exc))
            return
        try:
            def _mirror_update() -> None:
                from thoth_terminal.db import session_scope

                with session_scope() as session:
                    notes_bridge.upsert_note_for_exec_idea(
                        session,
                        guild_id=ctx.guild_id,
                        channel_id=int(getattr(idea, "discord_channel_id", None) or interaction.channel_id or 0),
                        message_id=(int(getattr(idea, "discord_message_id", 0)) if getattr(idea, "discord_message_id", None) else None),
                        user_id=interaction.user.id,
                        idea_id=idea.id,
                        symbol=idea.symbol,
                        horizon=idea.horizon,
                        horizon_n=getattr(idea, "horizon_n", 1),
                        thesis=idea.thesis,
                        catalysts=idea.catalysts,
                        invalidation=idea.invalidation,
                        levels=idea.levels,
                        confidence=idea.confidence,
                        status=idea.status,
                    )

            await asyncio.to_thread(_mirror_update)
        except Exception:
            pass
        await interaction.response.send_message(f"Closed {format_exidea_id(idea_id)} (outcome={outcome}).", ephemeral=False)

    if _operator_commands_enabled():
        tree.add_command(xidea, guild=guild)

    # /ticker brief
    ticker = app_commands.Group(name="ticker", description="Ticker utilities.")

    @ticker.command(name="brief", description="Show executive brief for a ticker.")
    async def ticker_brief(interaction: discord.Interaction, ticker: str) -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        await interaction.response.defer(thinking=True)
        try:
            exec_row, comm_rows, master_rows = await asyncio.to_thread(
                services.watchlist_status,
                guild_id=ctx.guild_id,
                symbol=ticker,
            )
            idea = await asyncio.to_thread(services.latest_active_exec_idea, guild_id=ctx.guild_id, symbol=ticker)
            pinned, recent = await asyncio.to_thread(services.list_sources_split, guild_id=ctx.guild_id, symbol=ticker)
        except Exception as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        sym = services.normalize_symbol(ticker)
        company_name = None
        for row in [exec_row] + list(comm_rows or []) + list(master_rows or []):
            if row and getattr(row, "company_name", None):
                company_name = company_name or row.company_name
        title = f"${sym} — Executive Brief"
        if company_name:
            title = f"${sym} — {_short(company_name, 50)} — Brief"
        embed = discord.Embed(title=title[:256], color=0xFBBF24)

        provider_sym = None
        exchange_full = None
        exchange_code = None
        currency = None
        for row in [exec_row] + list(comm_rows or []) + list(master_rows or []):
            if not row:
                continue
            provider_sym = provider_sym or row.provider_symbol
            exchange_full = exchange_full or row.full_exchange_name
            exchange_code = exchange_code or row.exchange
            currency = currency or row.last_quote_currency

        exchange_disp = None
        if exchange_full and exchange_code and exchange_full != exchange_code:
            exchange_disp = f"{exchange_full} ({exchange_code})"
        else:
            exchange_disp = exchange_full or exchange_code
        market_lines: list[str] = []
        if provider_sym:
            market_lines.append(f"Yahoo: `{provider_sym}`")
        if exchange_disp:
            market_lines.append(f"Exchange: {exchange_disp}")
        if currency:
            market_lines.append(f"Currency: {currency}")
        if market_lines:
            embed.add_field(name="Market", value="\n".join(market_lines)[:1024], inline=False)

        if exec_row:
            r = exec_row.last_returns if isinstance(exec_row.last_returns, dict) else {}
            since = r.get("since_24h") if isinstance(r.get("since_24h"), (int, float)) else None
            pending = exec_row.baseline_price is None
            since_s = "PENDING" if pending else format_pct(float(since) if since is not None else None)
            embed.add_field(name="Executive Watchlist", value=f"Yes • Since(24h): {since_s}", inline=False)
        else:
            embed.add_field(name="Executive Watchlist", value="No", inline=False)

        if comm_rows:
            lines: list[str] = []
            shown = 0
            for row in comm_rows:
                r = row.last_returns if isinstance(row.last_returns, dict) else {}
                since = r.get("since_24h") if isinstance(r.get("since_24h"), (int, float)) else None
                pending = row.baseline_price is None
                since_s = "PENDING" if pending else format_pct(float(since) if since is not None else None)
                sec = row.sector or "Uncategorized"
                sub = getattr(row, "subgroup", None) or "General"
                lines.append(f"{sec} / {sub} • Since(24h): {since_s}")
                shown += 1
                if shown >= 6:
                    break
            more = max(0, len(comm_rows) - shown)
            if more:
                lines.append(f"… (+{more} more)")
            val = f"Yes • {len(comm_rows)} memberships\n" + "\n".join(lines)
            embed.add_field(name="Community Watchlist", value=val[:1024], inline=False)
        else:
            embed.add_field(name="Community Watchlist", value="No", inline=False)

        if master_rows:
            lines = []
            shown = 0
            for row in master_rows:
                r = row.last_returns if isinstance(row.last_returns, dict) else {}
                since = r.get("since_24h") if isinstance(r.get("since_24h"), (int, float)) else None
                pending = row.baseline_price is None
                since_s = "PENDING" if pending else format_pct(float(since) if since is not None else None)
                ml = row.sector or "—"
                sub = getattr(row, "subgroup", None) or "General"
                lines.append(f"{ml} / {sub} • Since(24h): {since_s}")
                shown += 1
                if shown >= 6:
                    break
            more = max(0, len(master_rows) - shown)
            if more:
                lines.append(f"… (+{more} more)")
            val = f"Yes • {len(master_rows)} memberships\n" + "\n".join(lines)
            embed.add_field(name="Masterlists", value=val[:1024], inline=False)
        else:
            embed.add_field(name="Masterlists", value="No", inline=False)

        if idea:
            snippet = (idea.thesis or "").strip()
            if len(snippet) > 300:
                snippet = snippet[:300] + "…"
            unit = (idea.horizon or "").strip()
            n = int(getattr(idea, "horizon_n", 1) or 1)
            singular = unit[:-1] if unit.endswith("s") else unit
            unit_disp = singular if n == 1 else unit
            embed.add_field(
                name="Latest Exec Idea",
                value=f"{format_exidea_id(idea.id)} • {n} {unit_disp}\n{snippet}"[:1024],
                inline=False,
            )

        if pinned:
            lines = [f"{format_src_id(s.id)} — {s.title}" for s in pinned]
            embed.add_field(name="Pinned Sources", value="\n".join(lines)[:1024], inline=False)
        if recent:
            lines = [f"{format_src_id(s.id)} — {s.title}" for s in recent[:3]]
            embed.add_field(name="Recent Sources", value="\n".join(lines)[:1024], inline=False)

        await interaction.followup.send(embed=embed, ephemeral=False)

    @ticker.command(name="transcript", description="Find earnings transcript links for a ticker.")
    @app_commands.describe(
        most_recent="Return the latest transcript when no period is specified",
        year="Fiscal year filter (for example 2025)",
        quarter="Fiscal quarter filter (1-4, requires year)",
    )
    async def ticker_transcript(
        interaction: discord.Interaction,
        ticker: str,
        most_recent: bool = True,
        year: app_commands.Range[int, 1990, 2100] | None = None,
        quarter: app_commands.Range[int, 1, 4] | None = None,
    ) -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        if quarter is not None and year is None:
            await _deny(interaction, "Quarter requires year. Example: year=2025 quarter=3.")
            return

        sym = services.normalize_symbol(ticker)
        year_v = int(year) if year is not None else None
        quarter_v = int(quarter) if quarter is not None else None
        if year_v is not None and quarter_v is not None:
            query_most_recent = False
        elif year_v is not None:
            query_most_recent = True
        else:
            query_most_recent = bool(most_recent)

        await interaction.response.defer(thinking=True)
        try:
            def _lookup():
                from thoth_terminal.db import session_scope
                from thoth_terminal.transcripts import get_transcript, list_transcripts

                with session_scope() as session:
                    requested = get_transcript(
                        session,
                        ticker=sym,
                        most_recent=query_most_recent,
                        year=year_v,
                        quarter=quarter_v,
                    )
                    if requested:
                        return requested, None, []
                    try:
                        alternatives = list_transcripts(
                            session,
                            ticker=sym,
                            most_recent=False,
                            limit=5,
                        )
                    except Exception:
                        alternatives = []
                    latest = alternatives[0] if alternatives else None
                    return None, latest, alternatives

            row, fallback_row, alternatives = await asyncio.to_thread(_lookup)
        except Exception as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        if not row:
            if fallback_row and year_v is not None and quarter_v is not None:
                available_periods = ", ".join(
                    [
                        (alt.period_label or "Unspecified")
                        for alt in alternatives
                        if alt.period_label
                    ][:3]
                )
                embed = discord.Embed(title=f"${sym} — Transcript", color=0xFBBF24)
                embed.add_field(
                    name="Notice",
                    value=f"Requested Q{quarter_v} {year_v} not found. Showing latest available transcript.",
                    inline=False,
                )
                if available_periods:
                    embed.add_field(name="Available", value=available_periods[:1024], inline=False)
                embed.add_field(name="Company", value=_short(fallback_row.company_or_issuer, 120) or sym, inline=False)
                embed.add_field(name="Period", value=(fallback_row.period_label or "Unspecified"), inline=True)
                embed.add_field(
                    name="Published",
                    value=(fallback_row.published_at.strftime("%Y-%m-%d") if fallback_row.published_at else "Unknown"),
                    inline=True,
                )
                embed.add_field(name="Source", value=fallback_row.source_kind, inline=True)
                embed.add_field(name="Transcript", value=fallback_row.url[:1024], inline=False)
                await interaction.followup.send(embed=embed, ephemeral=False)
                return
            if year_v is not None and quarter_v is not None:
                msg = (
                    f"No transcript found for ${sym} (Q{quarter_v} {year_v}). "
                    "No match in local IR records or live transcript fallback."
                )
            elif year_v is not None:
                msg = (
                    f"No transcript found for ${sym} in FY {year_v}. "
                    "No match in local IR records or live transcript fallback."
                )
            else:
                msg = (
                    f"No transcript found for ${sym}. "
                    "No match in local IR records or live transcript fallback."
                )
            await interaction.followup.send(msg, ephemeral=True)
            return

        embed = discord.Embed(title=f"${sym} — Transcript", color=0x93C5FD)
        embed.add_field(name="Company", value=_short(row.company_or_issuer, 120) or sym, inline=False)
        embed.add_field(name="Period", value=(row.period_label or "Unspecified"), inline=True)
        embed.add_field(
            name="Published",
            value=(row.published_at.strftime("%Y-%m-%d") if row.published_at else "Unknown"),
            inline=True,
        )
        embed.add_field(name="Source", value=row.source_kind, inline=True)
        embed.add_field(name="Transcript", value=row.url[:1024], inline=False)
        await interaction.followup.send(embed=embed, ephemeral=False)

    if _operator_commands_enabled():
        tree.add_command(ticker, guild=guild)

    # /suggest ...
    suggest = app_commands.Group(name="suggest", description="Submit suggestions for executive review.")

    @suggest.command(name="watchlist", description="Suggest a ticker for the executive watchlist (ephemeral).")
    async def suggest_watchlist(interaction: discord.Interaction, ticker: str, note: str | None = None) -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        note_v = _unescape_newlines(note)
        try:
            item = await asyncio.to_thread(
                services.suggest_exec_watchlist,
                guild_id=ctx.guild_id,
                symbol=ticker,
                note=note_v,
                user_id=interaction.user.id,
            )
        except Exception as exc:
            await _deny(interaction, str(exc))
            return
        await interaction.response.send_message(f"Suggestion submitted: {format_queue_id(item.id)}.", ephemeral=True)

    @suggest.command(name="source", description="Suggest a source link for a ticker (ephemeral).")
    async def suggest_source(
        interaction: discord.Interaction,
        ticker: str,
        url: str,
        title: str | None = None,
        note: str | None = None,
        type: Literal["filing", "earnings", "macro", "research", "other"] = "other",
    ) -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        note_v = _unescape_newlines(note)
        try:
            item = await asyncio.to_thread(
                services.suggest_source,
                guild_id=ctx.guild_id,
                symbol=ticker,
                url=url,
                title=title,
                note=note_v,
                source_type=type,
                user_id=interaction.user.id,
            )
        except Exception as exc:
            await _deny(interaction, str(exc))
            return
        await interaction.response.send_message(f"Suggestion submitted: {format_queue_id(item.id)}.", ephemeral=True)

    if _operator_commands_enabled():
        tree.add_command(suggest, guild=guild)

    # /queue ...
    queue = app_commands.Group(name="queue", description="Executive review queue.")

    @queue.command(name="list", description="List pending suggestions.")
    @app_commands.describe(format="Output style (rich or table)")
    async def queue_list(
        interaction: discord.Interaction,
        limit: app_commands.Range[int, 1, 50] = 20,
        format: Literal["rich", "table"] = "rich",
    ) -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        if not await _ensure_exec(interaction, exec_user_id=ctx.exec_user_id):
            return
        await interaction.response.defer(thinking=True)
        items = await asyncio.to_thread(services.list_queue, guild_id=ctx.guild_id, status="pending", limit=int(limit))
        if not items:
            await interaction.followup.send("Queue: —", ephemeral=False)
            return
        lines: list[str] = []
        table_rows: list[list[str]] = []
        for it in items:
            qid = format_queue_id(it.id)
            who = f"<@{it.created_by_user_id}>"
            payload = it.payload if isinstance(it.payload, dict) else {}
            if it.kind == "exec_watchlist_add":
                t = payload.get("ticker") or "?"
                note = payload.get("note")
                note_s = None
                if note:
                    note_s = " ".join(str(note).split())
                    if len(note_s) > 80:
                        note_s = note_s[:80] + "…"
                suffix = f" — {note_s}" if note_s else ""
                lines.append(f"{qid}  watchlist_add  {t}  by {who}{suffix}")
                table_rows.append([qid, "watchlist_add", str(t), who, note_s or "—"])
            elif it.kind == "source_add":
                t = payload.get("ticker") or "?"
                url = payload.get("url") or ""
                lines.append(f"{qid}  source_add  {t}  by {who}  {url}")
                table_rows.append([qid, "source_add", str(t), who, _short(str(url), 80)])
            elif it.kind == "masterlist_add":
                t = payload.get("ticker") or "?"
                ml = payload.get("masterlist") or payload.get("sector") or "?"
                lines.append(f"{qid}  masterlist_add  {t} ({ml}) by {who}")
                table_rows.append([qid, "masterlist_add", f"{t} ({ml})", who, "—"])
            elif it.kind == "masterlist_remove":
                t = payload.get("ticker") or "?"
                ml = payload.get("masterlist") or payload.get("sector") or "all"
                lines.append(f"{qid}  masterlist_remove  {t} ({ml}) by {who}")
                table_rows.append([qid, "masterlist_remove", f"{t} ({ml})", who, "—"])
            else:
                lines.append(f"{qid}  {it.kind}  by {who}")
                table_rows.append([qid, str(it.kind), "—", who, "—"])
        if format == "table":
            msg = "```text\n" + "\n".join(lines) + "\n```"
            await interaction.followup.send(msg if len(msg) <= 1900 else msg[:1900] + "…\n```", ephemeral=False)
            return
        embeds = _build_rich_embeds(
            title="Executive Queue — Pending",
            color=0xFBBF24,
            headers=["Queue ID", "Kind", "Target", "By", "Details"],
            rows=table_rows,
            page_size=8,
        )
        await _send_embed_pages(interaction, embeds=embeds, deferred=True, ephemeral=False)

    @queue.command(name="approve", description="Approve a suggestion (exec-only).")
    async def queue_approve(interaction: discord.Interaction, id: str) -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        if not await _ensure_exec(interaction, exec_user_id=ctx.exec_user_id):
            return
        try:
            qid = _parse_prefixed_id(id, prefix="Q")
        except Exception as exc:
            await _deny(interaction, str(exc))
            return
        await interaction.response.defer(thinking=True)
        item = await asyncio.to_thread(services.get_queue_item, guild_id=ctx.guild_id, queue_id=qid)
        if not item:
            await interaction.followup.send("Queue item not found.", ephemeral=True)
            return
        if item.status != "pending":
            await interaction.followup.send(f"{format_queue_id(item.id)} is already {item.status}.", ephemeral=True)
            return
        payload = item.payload if isinstance(item.payload, dict) else {}

        if item.kind == "exec_watchlist_add":
            ticker = payload.get("ticker") or ""
            try:
                await asyncio.to_thread(
                    services.watchlist_add,
                    guild_id=ctx.guild_id,
                    list_kind="exec",
                    symbol=ticker,
                    user_id=interaction.user.id,
                )
            except Exception as exc:
                await interaction.followup.send(str(exc), ephemeral=True)
                return
            await asyncio.to_thread(_enqueue_watchlist_followup_jobs_debounced, guild_id=ctx.guild_id)
            await asyncio.to_thread(
                services.decide_queue_item,
                guild_id=ctx.guild_id,
                queue_id=item.id,
                decision="approved",
                decided_by_user_id=interaction.user.id,
            )
            await interaction.followup.send(f"Approved {format_queue_id(item.id)} → added ${services.normalize_symbol(ticker)}.", ephemeral=False)
            return

        if item.kind == "source_add":
            ticker = payload.get("ticker") or ""
            url = payload.get("url") or ""
            title = payload.get("title") or ""
            note = payload.get("note")
            stype = payload.get("type") or "other"
            tags_payload = payload.get("tags") if isinstance(payload.get("tags"), list) else []
            try:
                src_row = await asyncio.to_thread(
                    services.create_ticker_source,
                    guild_id=ctx.guild_id,
                    symbol=ticker,
                    title=title,
                    url=url,
                    note=note,
                    sentiment=None,
                    tags=[str(tag) for tag in tags_payload],
                    source_type=stype,
                    user_id=interaction.user.id,
                )
            except Exception as exc:
                await interaction.followup.send(str(exc), ephemeral=True)
                return
            embed = discord.Embed(title=f"Evidence Added — ${src_row.symbol}", color=0x6EE7B7)
            embed.add_field(name="Title", value=src_row.title[:1024], inline=False)
            embed.add_field(name="Link", value=src_row.url[:1024], inline=False)
            if src_row.note:
                embed.add_field(name="Note", value=src_row.note[:1024], inline=False)
            embed.add_field(name="Type", value=src_row.source_type, inline=True)
            embed.add_field(name="ID", value=format_src_id(src_row.id), inline=True)
            msg = await interaction.followup.send(embed=embed, ephemeral=False)
            await asyncio.to_thread(services.update_source_discord_ids, source_id=src_row.id, message_id=msg.id, channel_id=msg.channel.id)
            await asyncio.to_thread(
                services.decide_queue_item,
                guild_id=ctx.guild_id,
                queue_id=item.id,
                decision="approved",
                decided_by_user_id=interaction.user.id,
            )
            await interaction.followup.send(f"Approved {format_queue_id(item.id)}.", ephemeral=False)
            return

        if item.kind == "masterlist_add":
            masterlist = payload.get("masterlist") or payload.get("sector") or ""
            ticker = payload.get("ticker") or ""
            subgroup = payload.get("subgroup")
            if not masterlist or not ticker:
                await interaction.followup.send("Queue payload missing masterlist/ticker.", ephemeral=True)
                return
            try:
                res = await asyncio.to_thread(
                    services.watchlist_add,
                    guild_id=ctx.guild_id,
                    list_kind="masterlist",
                    symbol=str(ticker),
                    sector=str(masterlist),
                    subgroup=(str(subgroup) if isinstance(subgroup, str) and subgroup.strip() else None),
                    user_id=interaction.user.id,
                )
            except Exception as exc:
                await interaction.followup.send(str(exc), ephemeral=True)
                return
            await asyncio.to_thread(_enqueue_watchlist_followup_jobs_debounced, guild_id=ctx.guild_id)
            await asyncio.to_thread(
                services.decide_queue_item,
                guild_id=ctx.guild_id,
                queue_id=item.id,
                decision="approved",
                decided_by_user_id=interaction.user.id,
            )
            ml_disp = res.sector or str(masterlist)
            sg_disp = res.subgroup or "General"
            await interaction.followup.send(
                f"Approved {format_queue_id(item.id)} → masterlist added ${res.symbol} ({ml_disp} / {sg_disp}).",
                ephemeral=False,
            )
            return

        if item.kind == "masterlist_remove":
            ticker = payload.get("ticker") or ""
            masterlist = payload.get("masterlist") or payload.get("sector")
            subgroup = payload.get("subgroup")
            all_matches = bool(payload.get("all_matches")) if payload.get("all_matches") is not None else False
            if not ticker:
                await interaction.followup.send("Queue payload missing ticker.", ephemeral=True)
                return
            try:
                removed = await asyncio.to_thread(
                    services.watchlist_remove,
                    guild_id=ctx.guild_id,
                    list_kind="masterlist",
                    symbol=str(ticker),
                    sector=(str(masterlist) if isinstance(masterlist, str) and masterlist.strip() else None),
                    subgroup=(str(subgroup) if isinstance(subgroup, str) and subgroup.strip() else None),
                    all_matches=bool(all_matches),
                    user_id=interaction.user.id,
                )
            except Exception as exc:
                await interaction.followup.send(str(exc), ephemeral=True)
                return
            await asyncio.to_thread(_enqueue_watchlist_followup_jobs_debounced, guild_id=ctx.guild_id)
            await asyncio.to_thread(
                services.decide_queue_item,
                guild_id=ctx.guild_id,
                queue_id=item.id,
                decision="approved",
                decided_by_user_id=interaction.user.id,
            )
            plural = "entry" if int(removed) == 1 else "entries"
            await interaction.followup.send(
                f"Approved {format_queue_id(item.id)} → masterlist removed {int(removed)} {plural} for ${services.normalize_symbol(str(ticker))}.",
                ephemeral=False,
            )
            return

        await interaction.followup.send("Unsupported queue item kind.", ephemeral=True)

    @queue.command(name="reject", description="Reject a suggestion (exec-only).")
    async def queue_reject(interaction: discord.Interaction, id: str, reason: str | None = None) -> None:
        if not await _ensure_guild(interaction, guild_id=ctx.guild_id):
            return
        if not await _ensure_exec(interaction, exec_user_id=ctx.exec_user_id):
            return
        try:
            qid = _parse_prefixed_id(id, prefix="Q")
        except Exception as exc:
            await _deny(interaction, str(exc))
            return
        try:
            await asyncio.to_thread(
                services.decide_queue_item,
                guild_id=ctx.guild_id,
                queue_id=qid,
                decision="rejected",
                decided_by_user_id=interaction.user.id,
                decision_note=reason,
            )
        except Exception as exc:
            await _deny(interaction, str(exc))
            return
        await interaction.response.send_message(f"Rejected {format_queue_id(qid)}.", ephemeral=False)

    if _operator_commands_enabled():
        tree.add_command(queue, guild=guild)

    async def _public_ensure_guild(interaction: discord.Interaction) -> bool:
        return await _ensure_guild(interaction, guild_id=ctx.guild_id)

    async def _review_ensure_contributor(interaction: discord.Interaction) -> bool:
        return await _ensure_contributor(interaction, exec_user_id=ctx.exec_user_id)

    async def _review_ensure_moderator(interaction: discord.Interaction) -> bool:
        return await _ensure_moderator(interaction, exec_user_id=ctx.exec_user_id)

    register_review_commands(
        tree,
        ctx=ctx,
        guild=guild,
        deps=ReviewCommandDeps(
            ensure_guild=_public_ensure_guild,
            ensure_contributor=_review_ensure_contributor,
            ensure_moderator=_review_ensure_moderator,
            deny=_deny,
            parse_queue_id=lambda value: _parse_prefixed_id(value, prefix="Q"),
        ),
    )

    register_public_commands(
        tree,
        ctx=ctx,
        guild=guild,
        deps=PublicCommandDeps(
            ensure_guild=_public_ensure_guild,
            deny=_deny,
            is_contributor=lambda interaction: _is_contributor_user(interaction, exec_user_id=ctx.exec_user_id),
            is_operator=lambda interaction: _is_operator_user(interaction, exec_user_id=ctx.exec_user_id),
            operator_commands_enabled=_operator_commands_enabled,
        ),
    )


class ThothDiscordBot(discord.Client):
    def __init__(self, *, ctx: BotContext, sync_only: bool = False) -> None:
        intents = discord.Intents.default()
        if bool(getattr(settings, "discord_link_collect_enabled", False)):
            # Auto-link intake requires message content to read shared URLs.
            intents.message_content = True
        super().__init__(intents=intents)
        self.ctx = ctx
        self.sync_only = sync_only
        self.tree = app_commands.CommandTree(self)
        register_commands(self.tree, ctx=ctx)

    async def setup_hook(self) -> None:
        if not self.sync_only:
            self.heartbeat_loop.start()
        # Guild sync for fast iteration.
        await self.tree.sync(guild=self.ctx.guild_obj)
        if not self.sync_only:
            poll_s = int(getattr(settings, "discord_baseline_poll_seconds", 60) or 60)
            self.baseline_loop.change_interval(seconds=max(10, poll_s))
            self.baseline_loop.start()

            if bool(getattr(settings, "discord_desk_enabled", True)):
                outbox_s = int(getattr(settings, "discord_outbox_poll_seconds", 10) or 10)
                self.outbox_loop.change_interval(seconds=max(3, outbox_s))
                self.outbox_loop.start()

            if bool(getattr(settings, "discord_link_collect_enabled", False)):
                collect_s = max(
                    60,
                    int(getattr(settings, "discord_link_collect_interval_minutes", 10) or 10) * 60,
                )
                self.link_collect_loop.change_interval(seconds=collect_s)
                self.link_collect_loop.start()
        else:
            await self.close()

    def _heartbeat_details(self) -> dict[str, Any]:
        latency = None
        try:
            latency = round(float(self.latency) * 1000.0, 1)
        except (TypeError, ValueError):
            pass
        return {
            "gateway_ready": bool(self.is_ready()),
            "closed": bool(self.is_closed()),
            "latency_ms": latency,
            "guild_id": int(self.ctx.guild_id),
            "loops": {
                "heartbeat": bool(self.heartbeat_loop.is_running()),
                "baseline": bool(self.baseline_loop.is_running()),
                "outbox": bool(self.outbox_loop.is_running()),
                "link_collect": bool(self.link_collect_loop.is_running()),
            },
        }

    def _publish_heartbeat(self, *, status: str | None = None) -> None:
        resolved_status = status or ("healthy" if self.is_ready() and not self.is_closed() else "starting")
        write_service_heartbeat("discord", status=resolved_status, details=self._heartbeat_details())

    async def on_ready(self) -> None:
        self._publish_heartbeat(status="healthy")
        logger.info(
            "discord gateway ready user=%s latency_ms=%s",
            self.user,
            self._heartbeat_details().get("latency_ms"),
        )

    async def close(self) -> None:
        if not self.sync_only:
            try:
                self._publish_heartbeat(status="stopping")
            except Exception:
                logger.warning("discord failed to publish stopping heartbeat", exc_info=True)
            for loop in (self.heartbeat_loop, self.baseline_loop, self.outbox_loop, self.link_collect_loop):
                if loop.is_running():
                    loop.cancel()
        await super().close()

    async def _resolve_scan_root_channel(self) -> discord.abc.GuildChannel | discord.Thread | None:
        channel_id = int(
            getattr(settings, "discord_link_scan_channel_id", 0)
            or getattr(settings, "discord_channel_id", 0)
            or 0
        )
        if channel_id <= 0:
            return None
        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except Exception:
                return None
        if isinstance(channel, (discord.abc.GuildChannel, discord.Thread)):
            return channel
        return None

    async def _scan_targets(
        self,
        *,
        root: discord.abc.GuildChannel | discord.Thread,
    ) -> list[discord.abc.Messageable]:
        if isinstance(root, discord.Thread):
            return [root]
        out: list[discord.abc.Messageable] = []
        if isinstance(root, discord.TextChannel):
            out.append(root)
            seen = {int(root.id)}
            for thread in list(getattr(root, "threads", []) or []):
                if not isinstance(thread, discord.Thread):
                    continue
                if thread.archived:
                    continue
                tid = int(thread.id)
                if tid in seen:
                    continue
                out.append(thread)
                seen.add(tid)
            guild = root.guild
            if guild is not None:
                try:
                    active_threads = await guild.active_threads()
                except Exception:
                    active_threads = []
                for thread in active_threads:
                    if not isinstance(thread, discord.Thread):
                        continue
                    if int(getattr(thread, "parent_id", 0) or 0) != int(root.id):
                        continue
                    tid = int(thread.id)
                    if tid in seen:
                        continue
                    out.append(thread)
                    seen.add(tid)
            return out
        if isinstance(root, discord.abc.Messageable):
            return [root]
        return []

    async def _collect_channel_links(
        self,
        *,
        channel: discord.abc.Messageable,
        matcher: link_intake.TickerNameMatcher,
        remaining_messages: int,
        remaining_links: int,
    ) -> tuple[int, int, int]:
        channel_id = int(getattr(channel, "id", 0) or 0)
        if channel_id <= 0:
            return 0, remaining_messages, remaining_links

        state = await asyncio.to_thread(
            link_intake.get_scan_state,
            guild_id=self.ctx.guild_id,
            channel_id=channel_id,
        )
        boot_mode = (getattr(settings, "discord_link_bootstrap_mode", "24h") or "24h").strip().lower()
        processed_messages = 0
        ingested = 0
        latest_seen_id = int(state.get("last_message_id") or 0) if isinstance(state, dict) else 0

        if (not state) and boot_mode == "now":
            latest_msg_id = None
            try:
                async for msg in channel.history(limit=1, oldest_first=False):  # type: ignore[attr-defined]
                    latest_msg_id = int(msg.id)
                    break
            except Exception:
                latest_msg_id = None
            await asyncio.to_thread(
                link_intake.upsert_scan_state,
                guild_id=self.ctx.guild_id,
                channel_id=channel_id,
                last_message_id=latest_msg_id,
                last_scanned_at=datetime.now(timezone.utc).replace(tzinfo=None),
                last_error=None,
            )
            return 0, remaining_messages, remaining_links

        history_kwargs: dict[str, Any] = {
            "limit": max(1, int(remaining_messages)),
            "oldest_first": True,
        }
        if state and state.get("last_message_id"):
            recovered_from_bootstrap = False
            if boot_mode in {"24h", "7d"}:
                inbox_total = await asyncio.to_thread(link_intake.inbox_total_count, guild_id=self.ctx.guild_id)
                if inbox_total <= 0:
                    after_dt = await asyncio.to_thread(link_intake.bootstrap_after_datetime, boot_mode)
                    if after_dt is not None:
                        history_kwargs["after"] = after_dt
                        recovered_from_bootstrap = True
            if not recovered_from_bootstrap:
                history_kwargs["after"] = discord.Object(id=int(state["last_message_id"]))
        elif not state:
            after_dt = await asyncio.to_thread(link_intake.bootstrap_after_datetime, boot_mode)
            if after_dt is not None:
                history_kwargs["after"] = after_dt

        try:
            async for msg in channel.history(**history_kwargs):  # type: ignore[attr-defined]
                if remaining_messages <= 0 or remaining_links <= 0:
                    break
                processed_messages += 1
                remaining_messages -= 1
                latest_seen_id = max(latest_seen_id, int(msg.id))

                if link_intake.should_skip_message_author(
                    author_is_bot=bool(getattr(getattr(msg, "author", None), "bot", False)),
                    webhook_id=getattr(msg, "webhook_id", None),
                ):
                    continue

                embed_urls: list[str] = []
                embed_titles: list[str] = []
                embed_descriptions: list[str] = []
                for emb in list(getattr(msg, "embeds", []) or []):
                    if getattr(emb, "url", None):
                        embed_urls.append(str(emb.url))
                    if getattr(emb, "title", None):
                        embed_titles.append(str(emb.title))
                    if getattr(emb, "description", None):
                        desc = str(emb.description)
                        embed_descriptions.append(desc)
                        for candidate in link_intake.extract_urls_from_text(desc):
                            embed_urls.append(candidate)
                    try:
                        emb_payload = emb.to_dict()
                    except Exception:
                        emb_payload = {}
                    if isinstance(emb_payload, dict):
                        for candidate in (
                            emb_payload.get("url"),
                            (emb_payload.get("author") or {}).get("url")
                            if isinstance(emb_payload.get("author"), dict)
                            else None,
                            (emb_payload.get("provider") or {}).get("url")
                            if isinstance(emb_payload.get("provider"), dict)
                            else None,
                        ):
                            if isinstance(candidate, str) and candidate.strip():
                                embed_urls.append(candidate.strip())
                attachment_urls = [str(a.url) for a in list(getattr(msg, "attachments", []) or []) if getattr(a, "url", None)]
                raw_content = str(getattr(msg, "content", "") or "")
                if not raw_content and getattr(msg, "clean_content", None):
                    raw_content = str(getattr(msg, "clean_content", "") or "")
                urls = link_intake.extract_candidate_urls(
                    content=raw_content,
                    embed_urls=embed_urls,
                    attachment_urls=attachment_urls,
                )
                if not urls:
                    continue
                excerpt = link_intake.build_message_excerpt(
                    " ".join([raw_content, " ".join(embed_descriptions)]).strip()
                )
                embed_title = embed_titles[0] if embed_titles else None
                match_text = " ".join(
                    [
                        raw_content,
                        str(embed_title or ""),
                        " ".join(embed_descriptions),
                        " ".join(urls),
                    ]
                ).strip()

                for raw_url in urls:
                    if remaining_links <= 0:
                        break
                    remaining_links -= 1
                    result = await asyncio.to_thread(
                        link_intake.intake_link_candidate,
                        guild_id=self.ctx.guild_id,
                        channel_id=channel_id,
                        message_id=int(msg.id),
                        author_user_id=(int(msg.author.id) if getattr(msg, "author", None) else None),
                        message_created_at=getattr(msg, "created_at", None),
                        url_raw=str(raw_url),
                        message_excerpt=excerpt,
                        embed_title=embed_title,
                        match_text=match_text,
                        matcher=matcher,
                        user_id=0,
                    )
                    if result.status == link_intake.INBOX_STATUS_DETERMINISTIC_INGESTED and result.created:
                        ingested += 1

            await asyncio.to_thread(
                link_intake.upsert_scan_state,
                guild_id=self.ctx.guild_id,
                channel_id=channel_id,
                last_message_id=(latest_seen_id if latest_seen_id > 0 else None),
                last_scanned_at=datetime.now(timezone.utc).replace(tzinfo=None),
                last_error=None,
            )
        except Exception as exc:
            await asyncio.to_thread(
                link_intake.upsert_scan_state,
                guild_id=self.ctx.guild_id,
                channel_id=channel_id,
                last_message_id=(latest_seen_id if latest_seen_id > 0 else None),
                last_scanned_at=datetime.now(timezone.utc).replace(tzinfo=None),
                last_error=f"{type(exc).__name__}: {exc}",
            )
        return ingested, remaining_messages, remaining_links

    async def _run_link_collect_cycle(self) -> None:
        root = await self._resolve_scan_root_channel()
        if root is None:
            return
        targets = await self._scan_targets(root=root)
        if not targets:
            return
        matcher = await asyncio.to_thread(link_intake.load_ticker_name_matcher)
        remaining_messages = max(1, int(getattr(settings, "discord_link_collect_max_messages_per_run", 300) or 300))
        remaining_links = max(1, int(getattr(settings, "discord_link_collect_max_links_per_run", 200) or 200))
        deterministic_ingested = 0
        for target in targets:
            if remaining_messages <= 0 or remaining_links <= 0:
                break
            ingested, remaining_messages, remaining_links = await self._collect_channel_links(
                channel=target,
                matcher=matcher,
                remaining_messages=remaining_messages,
                remaining_links=remaining_links,
            )
            deterministic_ingested += int(ingested)

        if deterministic_ingested <= 0:
            return

        if isinstance(root, discord.abc.Messageable):
            try:
                await root.send(
                    f"Auto-ingested {int(deterministic_ingested)} sources",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except Exception:
                pass

    @tasks.loop(seconds=60)
    async def baseline_loop(self) -> None:
        try:
            await asyncio.to_thread(services.capture_due_baselines, guild_id=self.ctx.guild_id)
        except Exception:
            logger.exception("discord baseline loop failed; continuing")

    @tasks.loop(seconds=15)
    async def heartbeat_loop(self) -> None:
        try:
            self._publish_heartbeat()
        except Exception:
            logger.exception("discord heartbeat publish failed; continuing")

    @tasks.loop(seconds=600)
    async def link_collect_loop(self) -> None:
        try:
            await self._run_link_collect_cycle()
        except Exception:
            logger.exception("discord link collect loop failed; continuing")

    def _embed_from_json(self, payload: dict[str, Any] | None) -> discord.Embed | None:
        if not isinstance(payload, dict):
            return None
        title = str(payload.get("title") or "").strip() or None
        description = payload.get("description")
        desc = str(description) if isinstance(description, str) and description.strip() else None
        color = payload.get("color")
        color_val = None
        if isinstance(color, int):
            color_val = int(color)
        elif isinstance(color, str):
            raw = color.strip().lower().lstrip("#")
            try:
                color_val = int(raw, 16)
            except Exception:
                color_val = None
        embed = discord.Embed(title=title, description=desc, color=color_val or 0x93C5FD)

        fields = payload.get("fields")
        if isinstance(fields, list):
            for f in fields[:25]:
                if not isinstance(f, dict):
                    continue
                name = str(f.get("name") or "").strip()
                value = str(f.get("value") or "").strip()
                if not name or not value:
                    continue
                inline = bool(f.get("inline")) if f.get("inline") is not None else False
                embed.add_field(name=name[:256], value=value[:1024], inline=inline)

        footer = payload.get("footer")
        if isinstance(footer, dict):
            text = footer.get("text")
            if isinstance(text, str) and text.strip():
                embed.set_footer(text=text.strip()[:2048])
        return embed

    async def _send_outbox_item(self, item: outbox_mod.ClaimedOutboxItem) -> int | None:
        dest = None
        if item.thread_id:
            try:
                ch = self.get_channel(int(item.thread_id))
                if not ch:
                    ch = await self.fetch_channel(int(item.thread_id))
                if isinstance(ch, discord.Thread):
                    if ch.archived:
                        try:
                            await ch.edit(archived=False)
                        except Exception:
                            pass
                    dest = ch
            except Exception:
                dest = None

        if dest is None:
            ch = self.get_channel(int(item.channel_id))
            if not ch:
                ch = await self.fetch_channel(int(item.channel_id))
            dest = ch

        embed = self._embed_from_json(item.embed_json)
        content = (item.content or "").strip() or None
        msg = await dest.send(  # type: ignore[union-attr]
            content=content,
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return int(getattr(msg, "id", 0) or 0) or None

    @tasks.loop(seconds=10)
    async def outbox_loop(self) -> None:
        try:
            if not bool(getattr(settings, "discord_desk_enabled", True)):
                return
            cfg = await asyncio.to_thread(desk_mod.get_desk_settings, guild_id=self.ctx.guild_id)
            if not bool(cfg.desk_enabled) or bool(cfg.outbox_paused):
                return

            max_send = int(getattr(settings, "discord_outbox_max_send_per_tick", 2) or 2)
            max_send = max(1, min(max_send, 10))
            max_per_hour = int(getattr(settings, "discord_outbox_max_per_hour", 12) or 12)

            for _ in range(max_send):
                item = await asyncio.to_thread(
                    outbox_mod.claim_next_outbox_item,
                    guild_id=self.ctx.guild_id,
                    max_per_hour=max_per_hour,
                )
                if not item:
                    return
                try:
                    msg_id = await self._send_outbox_item(item)
                    await asyncio.to_thread(
                        outbox_mod.mark_outbox_posted,
                        guild_id=self.ctx.guild_id,
                        outbox_id=int(item.id),
                        discord_message_id=msg_id,
                    )
                    await asyncio.to_thread(
                        outbox_mod.enqueue_notice_for_posted_item,
                        item=item,
                        discord_message_id=msg_id,
                    )
                except Exception as exc:
                    await asyncio.to_thread(
                        outbox_mod.mark_outbox_failed,
                        guild_id=self.ctx.guild_id,
                        outbox_id=int(item.id),
                        error=f"{type(exc).__name__}: {exc}",
                    )
        except Exception:
            logger.exception("discord outbox loop failed before item send; continuing")


def run_bot() -> None:
    token, guild_id, exec_user_id = _require_bot_config()
    logger.info("discord bot startup: %s", runtime_fingerprint())
    bot = ThothDiscordBot(ctx=BotContext(guild_id=guild_id, exec_user_id=exec_user_id, guild_obj=discord.Object(id=guild_id)))
    bot.run(token)


def sync_once() -> None:
    token, guild_id, exec_user_id = _require_bot_config()
    logger.info("discord sync startup: %s", runtime_fingerprint())

    async def _main() -> None:
        bot = ThothDiscordBot(
            ctx=BotContext(guild_id=guild_id, exec_user_id=exec_user_id, guild_obj=discord.Object(id=guild_id)),
            sync_only=True,
        )
        await bot.start(token)

    asyncio.run(_main())
