from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Literal
from zoneinfo import ZoneInfo

import discord
from discord import app_commands

from thoth_terminal.config import settings
from thoth_terminal.db import session_scope
from thoth_terminal.discord import cloud_qa, desk as desk_mod, notes_bridge, public_dto, services
from thoth_terminal.discord.formatting import format_queue_id, format_src_id
from thoth_terminal.gap_hunter_service import run_gap_hunter_cloud_task
from thoth_terminal.report_jobs import enqueue_report_build
from thoth_terminal.research_report_service import get_research_report_state


try:
    ET = ZoneInfo("America/New_York")
    ET_LABEL = "ET"
except Exception:
    ET = timezone.utc
    ET_LABEL = "UTC"


@dataclass(frozen=True)
class PublicCommandDeps:
    ensure_guild: Callable[[discord.Interaction], Awaitable[bool]]
    deny: Callable[[discord.Interaction, str], Awaitable[None]]
    is_contributor: Callable[[discord.Interaction], bool]
    is_operator: Callable[[discord.Interaction], bool]
    operator_commands_enabled: Callable[[], bool]


def _short(value: object, limit: int = 240) -> str:
    text = " ".join(str(value or "").replace("\n", " ").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "..."


def _unescape_newlines(value: str | None) -> str | None:
    if value is None:
        return None
    return str(value).replace("\\n", "\n")


def _discord_datetime_label(value: object | None) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return raw
    return dt.astimezone(ET).strftime("%Y-%m-%d %H:%M") + f" {ET_LABEL}"


def _report_metric_lookup(metrics: list[dict[str, Any]], label: str) -> int | None:
    wanted = str(label).strip().lower()
    for row in metrics:
        if str(row.get("label") or "").strip().lower() != wanted:
            continue
        value = row.get("value")
        try:
            return int(value)  # type: ignore[arg-type]
        except Exception:
            return None
    return None


def _report_links_view(*, report_url: str, artifact_links: dict[str, str]) -> discord.ui.View:
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Open Report", style=discord.ButtonStyle.link, url=report_url))
    for label, artifact_type in (("PDF", "pdf"), ("Markdown", "markdown"), ("JSON", "json"), ("AI Clean", "ai_clean_markdown")):
        artifact_url = str(artifact_links.get(artifact_type) or "").strip()
        if artifact_url:
            view.add_item(discord.ui.Button(label=label, style=discord.ButtonStyle.link, url=artifact_url))
    return view


def _search_result_pages(rows: list[public_dto.PublicSearchResult], *, max_chars: int = 1900) -> list[str]:
    """Keep every selected result visible within Discord's message limit."""

    blocks: list[str] = []
    for item in rows:
        symbol = f" ${item.symbol}" if item.symbol else ""
        link = f" - {_short(item.url, 900)}" if item.url else ""
        block = f"- {item.kind}{symbol}: {_short(item.title, 350)}{link}"
        if item.summary:
            block += f"\n  {_short(item.summary, 260)}"
        blocks.append(block)
    pages: list[str] = []
    current = ""
    payload_limit = max_chars - 80
    for block in blocks:
        if current and len(current) + len(block) + 1 > payload_limit:
            pages.append(current)
            current = ""
        current = f"{current}\n{block}" if current else block
    if current:
        pages.append(current)
    return [f"Search results ({index}/{len(pages)}):\n{page}" for index, page in enumerate(pages, start=1)]


def register_public_commands(
    tree: app_commands.CommandTree,
    *,
    ctx: Any,
    guild: discord.Object,
    deps: PublicCommandDeps,
) -> None:
    @tree.command(name="help", description="Show research assistant help.", guild=guild)
    @app_commands.describe(public="Post in-channel (default is ephemeral)")
    async def help_cmd(interaction: discord.Interaction, public: bool = False) -> None:
        if not await deps.ensure_guild(interaction):
            return
        embed = discord.Embed(title="Research Assistant - Help", color=0x93C5FD)
        embed.add_field(
            name="Research",
            value="\n".join(
                [
                    "`/ask` - sourced Q&A over approved guild research memory",
                    "`/submit_evidence` - submit a source for review",
                    "`/brief` - public ticker brief",
                    "`/company` - company profile and coverage",
                    "`/report` - public research report lookup",
                    "`/search` - search approved public evidence and reports",
                    "`/compare` - compare two public company profiles",
                ]
            )[:1024],
            inline=False,
        )
        embed.add_field(
            name="Markets",
            value="\n".join(
                [
                    "`/watchlist dashboard` - interactive community watchlist",
                    "`/watchlist list` - browse tracked tickers",
                    "`/watchlist perf` - performance table",
                    "`/digest` - recent approved evidence and reports",
                    "`/alerts` - list, subscribe to, and publish public research alerts",
                    "`/transcripts` - earnings transcript lookup",
                    "`/supply_chain` - published supply-chain exposure maps",
                    "`/symbol search` - ticker lookup",
                ]
            )[:1024],
            inline=False,
        )
        if deps.is_contributor(interaction):
            embed.add_field(
                name="Review",
                value="\n".join(
                    [
                        "`/review evidence` - list evidence submissions awaiting review",
                        "`/review tag` - contributor tagging for a pending source",
                        "`/review approve` - moderator approval for a source",
                        "`/review reject` - moderator rejection for a source",
                        "`/gap_hunter` - contributor coverage gap audit",
                    ]
                )[:1024],
                inline=False,
            )
        if deps.operator_commands_enabled() and deps.is_operator(interaction):
            embed.add_field(
                name="Operator",
                value="\n".join(
                    [
                        "`/desk ...` - automation controls",
                        "`/agent ...` - background research jobs",
                        "`/queue ...` - review queue",
                        "`/xwl`, `/xsrc`, `/xnote`, `/xidea` - legacy operator surfaces",
                    ]
                )[:1024],
                inline=False,
            )
        embed.set_footer(text="Public commands expose approved guild research memory only.")
        await interaction.response.send_message(embed=embed, ephemeral=(not bool(public)))

    @tree.command(name="search", description="Search approved guild research memory.", guild=guild)
    async def search(interaction: discord.Interaction, q: str, limit: app_commands.Range[int, 1, 30] = 10) -> None:
        if not await deps.ensure_guild(interaction):
            return
        await interaction.response.defer(thinking=True)
        try:
            rows = await asyncio.to_thread(public_dto.search_public_memory, guild_id=ctx.guild_id, query=q, limit=int(limit))
        except Exception as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        if not rows:
            await interaction.followup.send("No results.", ephemeral=False)
            return
        for page in _search_result_pages(rows):
            await interaction.followup.send(page, ephemeral=False)

    @tree.command(name="submit_evidence", description="Submit a source for public research review.", guild=guild)
    @app_commands.describe(
        ticker="Ticker the source is most directly about",
        tags="Comma-separated public tags, for example earnings, capex, risk",
    )
    async def submit_evidence(
        interaction: discord.Interaction,
        ticker: str,
        url: str,
        title: str | None = None,
        note: str | None = None,
        type: Literal["filing", "earnings", "macro", "research", "transcript", "supply_chain", "other"] = "other",
        tags: str | None = None,
    ) -> None:
        if not await deps.ensure_guild(interaction):
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            result = await asyncio.to_thread(
                services.submit_evidence,
                guild_id=ctx.guild_id,
                symbol=ticker,
                url=url,
                title=title,
                note=_unescape_newlines(note),
                source_type=type,
                tags=services.split_csv(tags),
                user_id=interaction.user.id,
            )
        except Exception as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        embed = discord.Embed(title="Evidence Submitted", color=0x6EE7B7)
        embed.description = result.message
        embed.add_field(name="Target", value=f"${result.target}", inline=True)
        embed.add_field(name="Status", value=result.status.replace("_", " "), inline=True)
        if result.queue_id is not None:
            embed.add_field(name="Review ID", value=format_queue_id(result.queue_id), inline=True)
        if result.evidence_id is not None:
            embed.add_field(name="Evidence ID", value=format_src_id(result.evidence_id), inline=True)
        embed.add_field(name="Source", value=result.url_canonical[:1024], inline=False)
        embed.set_footer(text="Approved submissions become searchable public research memory.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @tree.command(name="ask", description="Ask a sourced public research question.", guild=guild)
    @app_commands.describe(
        subject="Optional ticker, company, or topic to narrow context",
        model="Optional OpenRouter free model, for example openrouter/free",
        public="Post in-channel instead of ephemerally",
    )
    async def ask(
        interaction: discord.Interaction,
        question: str,
        subject: str | None = None,
        model: str | None = None,
        public: bool = False,
    ) -> None:
        if not await deps.ensure_guild(interaction):
            return
        await interaction.response.defer(thinking=True, ephemeral=(not bool(public)))
        try:
            answer = await asyncio.to_thread(
                cloud_qa.ask_public_question,
                guild_id=ctx.guild_id,
                question=question,
                subject=subject,
                model=model,
            )
        except Exception as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        embed = discord.Embed(title="Research Answer", description=answer.answer[:4000], color=0x93C5FD)
        if answer.key_points:
            embed.add_field(name="Key Points", value="\n".join(f"- {p}" for p in answer.key_points)[:1024], inline=False)
        if answer.sources:
            src_lines = []
            for src in answer.sources[:5]:
                label = str(src.get("title") or src.get("url") or "source").strip()
                url_v = str(src.get("url") or "").strip()
                src_lines.append(f"{label} - {url_v}" if url_v else label)
            embed.add_field(name="Sources", value="\n".join(src_lines)[:1024], inline=False)
        if answer.risks:
            embed.add_field(name="Risks", value="\n".join(f"- {p}" for p in answer.risks)[:1024], inline=False)
        if answer.unknowns:
            embed.add_field(name="Unknowns", value="\n".join(f"- {p}" for p in answer.unknowns)[:1024], inline=False)
        if answer.model != answer.requested_model:
            model_label = f"requested {answer.requested_model} -> routed {answer.model}"
        else:
            model_label = answer.model
        embed.set_footer(text=f"{answer.provider} / {model_label} / public context only")
        await interaction.followup.send(embed=embed, ephemeral=(not bool(public)))

    @tree.command(name="gap_hunter", description="Audit universe coverage gaps for contributor review.", guild=guild)
    @app_commands.describe(
        question="Optional coverage question to ask the cloud Gap Hunter",
        keyword="Optional keyword, company, section, role, or concept drilldown",
        model="Optional OpenRouter free model, for example openrouter/free",
        persist="Create draft instrument requests for candidates",
        public="Post in-channel instead of ephemerally",
    )
    async def gap_hunter(
        interaction: discord.Interaction,
        question: str | None = None,
        keyword: str | None = None,
        model: str | None = None,
        persist: bool = False,
        public: bool = False,
    ) -> None:
        if not await deps.ensure_guild(interaction):
            return
        if not deps.is_contributor(interaction):
            await deps.deny(interaction, "Gap Hunter is available to contributors and operators.")
            return
        await interaction.response.defer(thinking=True, ephemeral=(not bool(public)))

        def _run() -> dict[str, Any]:
            with session_scope() as session:
                payload = run_gap_hunter_cloud_task(
                    session,
                    provider="openrouter",
                    model=str(model or getattr(settings, "openrouter_model", "openrouter/free") or "openrouter/free"),
                    question=question,
                    keyword=keyword,
                    persist_candidates=bool(persist),
                    operator_approved=bool(persist) and deps.is_operator(interaction),
                )
                if payload.get("persisted_candidates"):
                    session.commit()
                return payload

        try:
            result = await asyncio.to_thread(_run)
        except Exception as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        task = result.get("result") if isinstance(result.get("result"), dict) else {}
        index = result.get("index") if isinstance(result.get("index"), dict) else {}
        title = "Gap Hunter"
        if keyword:
            title += f" - {_short(keyword, 80)}"
        embed = discord.Embed(title=title[:256], description=_short(task.get("coverage_summary"), 900), color=0xA78BFA)
        embed.add_field(name="Packet", value=f"{index.get('mode', 'unknown')} / {index.get('row_count', 0)} rows", inline=True)
        for key, label in (
            ("missing_company_reports", "Company Reports"),
            ("missing_product_explainers", "Product Explainers"),
            ("technical_definition_gaps", "Definitions"),
            ("super_report_candidates", "Super Reports"),
            ("followup_context_requests", "Follow-ups"),
            ("next_best_actions", "Next Actions"),
        ):
            rows = task.get(key)
            if not isinstance(rows, list) or not rows:
                continue
            lines: list[str] = []
            for item in rows[:5]:
                if isinstance(item, dict):
                    head = item.get("company") or item.get("ticker") or item.get("topic") or item.get("term") or item.get("title") or item.get("query")
                    tail = item.get("reason") or item.get("rationale") or item.get("summary") or item.get("description")
                    lines.append(_short(" - ".join(str(v) for v in (head, tail) if v), 180))
                else:
                    lines.append(_short(item, 180))
            embed.add_field(name=label, value="\n".join(f"- {line}" for line in lines)[:1024], inline=False)
        persisted = result.get("persisted_candidates")
        if isinstance(persisted, list) and persisted:
            embed.add_field(name="Draft Requests", value=str(len(persisted)), inline=True)
        model_label = result.get("routed_model") or result.get("submitted_model") or result.get("model")
        embed.set_footer(text=f"openrouter / {model_label} / contributor context")
        await interaction.followup.send(embed=embed, ephemeral=(not bool(public)))

    @tree.command(name="brief", description="Show a public ticker brief.", guild=guild)
    async def brief(interaction: discord.Interaction, ticker: str) -> None:
        if not await deps.ensure_guild(interaction):
            return
        await interaction.response.defer(thinking=True)
        try:
            brief_payload = await asyncio.to_thread(public_dto.ticker_brief, guild_id=ctx.guild_id, ticker=ticker)
        except Exception as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        title = (
            f"${brief_payload.ticker} - Brief"
            if not brief_payload.company_name
            else f"${brief_payload.ticker} - {_short(brief_payload.company_name, 56)}"
        )
        embed = discord.Embed(title=title[:256], color=0xFBBF24)
        if brief_payload.watchlist_memberships:
            embed.add_field(name="Community Watchlist", value="\n".join(brief_payload.watchlist_memberships[:8])[:1024], inline=False)
        else:
            embed.add_field(name="Community Watchlist", value="Not currently listed.", inline=False)
        if brief_payload.themes:
            embed.add_field(name="Themes", value="\n".join(brief_payload.themes[:8])[:1024], inline=False)
        if brief_payload.evidence:
            lines = []
            for item in brief_payload.evidence[:6]:
                link = f" - {item.url}" if item.url else ""
                lines.append(f"{item.kind}: {item.title}{link}")
            embed.add_field(name="Approved Evidence", value="\n".join(lines)[:1024], inline=False)
        embed.set_footer(text="Public brief excludes operator-only notes and workflow state.")
        await interaction.followup.send(embed=embed, ephemeral=False)

    @tree.command(name="company", description="Show a public company profile.", guild=guild)
    async def company(interaction: discord.Interaction, subject: str) -> None:
        if not await deps.ensure_guild(interaction):
            return
        await interaction.response.defer(thinking=True)
        try:
            profile = await asyncio.to_thread(public_dto.company_profile, guild_id=ctx.guild_id, subject=subject)
        except Exception as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        title = f"{profile.name} (${profile.ticker})" if profile.ticker else profile.name
        embed = discord.Embed(title=title[:256], color=0x60A5FA)
        details = []
        if profile.exchange:
            details.append(f"Exchange: {profile.exchange}")
        if profile.sector:
            details.append(f"Sector: {profile.sector}")
        if profile.industry:
            details.append(f"Industry: {profile.industry}")
        if details:
            embed.add_field(name="Profile", value="\n".join(details)[:1024], inline=False)
        embed.add_field(
            name="Coverage",
            value=(
                f"Reports: {profile.report_count}\n"
                f"Transcripts: {profile.transcript_count}\n"
                f"Approved evidence: {profile.evidence_count}"
            ),
            inline=False,
        )
        if profile.watchlist_memberships:
            embed.add_field(name="Community Watchlists", value="\n".join(profile.watchlist_memberships[:8])[:1024], inline=False)
        if profile.website:
            embed.add_field(name="Investor Relations", value=profile.website[:1024], inline=False)
        await interaction.followup.send(embed=embed, ephemeral=False)

    @tree.command(name="digest", description="Show recent approved public research activity.", guild=guild)
    async def digest(
        interaction: discord.Interaction,
        days: app_commands.Range[int, 1, 30] = 7,
        limit: app_commands.Range[int, 1, 25] = 10,
    ) -> None:
        if not await deps.ensure_guild(interaction):
            return
        await interaction.response.defer(thinking=True)
        try:
            rows = await asyncio.to_thread(public_dto.public_digest, guild_id=ctx.guild_id, days=int(days), limit=int(limit))
        except Exception as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        if not rows:
            await interaction.followup.send(f"No approved public research activity in the last {int(days)} days.", ephemeral=False)
            return
        embed = discord.Embed(title="Research Digest", color=0x6EE7B7)
        embed.description = f"Approved evidence and public reports from the last {int(days)} days."
        lines: list[str] = []
        for item in rows:
            symbol = f" ${item.symbol}" if item.symbol else ""
            when = _discord_datetime_label(item.updated_at.isoformat() if item.updated_at else None)
            suffix = f" ({when})" if when else ""
            link = f" - {item.url}" if item.url else ""
            lines.append(f"{item.kind}{symbol}: {item.title}{link}{suffix}")
            if item.summary:
                lines.append(_short(item.summary, 180))
        embed.add_field(name="Recent", value="\n".join(lines)[:1024], inline=False)
        embed.set_footer(text="Public digest excludes pending, private, and operator-only material.")
        await interaction.followup.send(embed=embed, ephemeral=False)

    @tree.command(name="alerts", description="Show active public research alert rules.", guild=guild)
    async def alerts(
        interaction: discord.Interaction,
        action: Literal["list", "subscribe", "unsubscribe", "subscriptions", "publish"] = "list",
        query: str | None = None,
        limit: app_commands.Range[int, 1, 25] = 10,
    ) -> None:
        if not await deps.ensure_guild(interaction):
            return
        action_v = str(action or "list").strip().lower()
        if action_v in {"subscribe", "unsubscribe", "subscriptions", "publish"} and not deps.is_contributor(interaction):
            await deps.deny(interaction, "Contributor access required to manage alert subscriptions.")
            return
        await interaction.response.defer(thinking=True, ephemeral=(action_v != "list"))
        if action_v == "subscribe":
            channel_id = int(interaction.channel_id or 0)
            if not channel_id:
                await interaction.followup.send("Cannot resolve this channel.", ephemeral=True)
                return
            sub = await asyncio.to_thread(
                services.upsert_public_alert_subscription,
                guild_id=ctx.guild_id,
                channel_id=channel_id,
                query=query,
                user_id=interaction.user.id,
            )
            scope = f"matching `{sub.query}`" if sub.query else "all public alert rules"
            await interaction.followup.send(f"Subscribed <#{sub.channel_id}> to {scope}.", ephemeral=True)
            return
        if action_v == "unsubscribe":
            channel_id = int(interaction.channel_id or 0)
            if not channel_id:
                await interaction.followup.send("Cannot resolve this channel.", ephemeral=True)
                return
            removed = await asyncio.to_thread(
                services.remove_public_alert_subscription,
                guild_id=ctx.guild_id,
                channel_id=channel_id,
                query=query,
                user_id=interaction.user.id,
            )
            scope = f"matching `{query}`" if str(query or "").strip() else "all public alert rules"
            await interaction.followup.send(
                f"Unsubscribed <#{channel_id}> from {scope}." if removed else "No matching alert subscription found.",
                ephemeral=True,
            )
            return
        if action_v == "subscriptions":
            subs = await asyncio.to_thread(services.list_public_alert_subscriptions, guild_id=ctx.guild_id)
            if not subs:
                await interaction.followup.send("No public alert subscriptions are configured.", ephemeral=True)
                return
            lines = []
            for sub in subs[:25]:
                scope = f"`{sub.query}`" if sub.query else "all public alert rules"
                lines.append(f"<#{sub.channel_id}> - {scope} - by <@{sub.created_by_user_id}>")
            embed = discord.Embed(title="Alert Subscriptions", color=0xF87171)
            embed.add_field(name="Channels", value="\n".join(lines)[:1024], inline=False)
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        if action_v == "publish":
            channel_id = int(interaction.channel_id or 0)
            if not channel_id:
                await interaction.followup.send("Cannot resolve this channel.", ephemeral=True)
                return
            outbox_id = await asyncio.to_thread(
                services.enqueue_public_alert_notification,
                guild_id=ctx.guild_id,
                channel_id=channel_id,
                query=query,
                user_id=interaction.user.id,
                limit=int(limit),
            )
            if outbox_id is None:
                await interaction.followup.send("No active public alert rules matched.", ephemeral=True)
                return
            await interaction.followup.send(f"Queued public alert notification for <#{channel_id}>.", ephemeral=True)
            return
        try:
            rows = await asyncio.to_thread(public_dto.public_alerts, query=query, limit=int(limit))
        except Exception as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        if not rows:
            await interaction.followup.send("No active public alert rules found.", ephemeral=False)
            return
        embed = discord.Embed(title="Research Alerts", color=0xF87171)
        lines = []
        for row in rows:
            last = _discord_datetime_label(row.last_triggered_at.isoformat() if row.last_triggered_at else None)
            tail = f" | last: {last}" if last else ""
            lines.append(f"{row.name} | {row.chain_title} | {row.scope} | {row.trigger}{tail}")
        embed.add_field(name="Active Rules", value="\n".join(lines)[:1024], inline=False)
        embed.set_footer(text="Alert rules are public projections of approved supply-chain monitoring.")
        await interaction.followup.send(embed=embed, ephemeral=False)

    @tree.command(name="compare", description="Compare two public company research profiles.", guild=guild)
    async def compare(interaction: discord.Interaction, left: str, right: str) -> None:
        if not await deps.ensure_guild(interaction):
            return
        await interaction.response.defer(thinking=True)
        try:
            result = await asyncio.to_thread(public_dto.compare_companies, guild_id=ctx.guild_id, left=left, right=right)
        except Exception as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        embed = discord.Embed(title=f"{result.left.name} vs {result.right.name}"[:256], color=0xA78BFA)

        def _profile_lines(profile: public_dto.PublicCompanyProfile) -> str:
            bits = []
            if profile.ticker:
                bits.append(f"Ticker: ${profile.ticker}")
            if profile.sector:
                bits.append(f"Sector: {profile.sector}")
            if profile.industry:
                bits.append(f"Industry: {profile.industry}")
            bits.append(f"Reports: {profile.report_count}")
            bits.append(f"Transcripts: {profile.transcript_count}")
            bits.append(f"Approved evidence: {profile.evidence_count}")
            if profile.watchlist_memberships:
                bits.append("Watchlists: " + ", ".join(profile.watchlist_memberships[:3]))
            return "\n".join(bits)[:1024]

        embed.add_field(name=result.left.name[:256], value=_profile_lines(result.left), inline=True)
        embed.add_field(name=result.right.name[:256], value=_profile_lines(result.right), inline=True)
        recent_lines = []
        for label, rows in (
            (result.left.ticker or result.left.name, result.left_recent_evidence),
            (result.right.ticker or result.right.name, result.right_recent_evidence),
        ):
            if rows:
                recent_lines.append(f"{label}: " + "; ".join(row.title for row in rows[:3]))
        if recent_lines:
            embed.add_field(name="Recent Evidence", value="\n".join(recent_lines)[:1024], inline=False)
        embed.set_footer(text="Comparison uses approved public guild research memory only.")
        await interaction.followup.send(embed=embed, ephemeral=False)

    @tree.command(name="supply_chain", description="Show published public supply-chain exposure maps.", guild=guild)
    async def supply_chain(
        interaction: discord.Interaction,
        query: str | None = None,
        limit: app_commands.Range[int, 1, 10] = 3,
    ) -> None:
        if not await deps.ensure_guild(interaction):
            return
        await interaction.response.defer(thinking=True)
        try:
            rows = await asyncio.to_thread(public_dto.supply_chain_views, query=query, limit=int(limit))
        except Exception as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        if not rows:
            await interaction.followup.send("No published public supply-chain maps found.", ephemeral=False)
            return
        first = rows[0]
        embed = discord.Embed(title=first.title[:256], color=0x34D399)
        description_bits = []
        if first.description:
            description_bits.append(_short(first.description, 500))
        meta = []
        if first.product:
            meta.append(f"Product: {first.product}")
        if first.category:
            meta.append(f"Category: {first.category}")
        if first.version:
            meta.append(f"Version: {first.version}")
        if meta:
            description_bits.append("\n".join(meta))
        if description_bits:
            embed.description = "\n\n".join(description_bits)[:4000]
        counts = first.counts
        embed.add_field(
            name="Coverage",
            value=(
                f"Layers: {counts.get('layers', 0)}\n"
                f"Nodes: {counts.get('nodes', 0)}\n"
                f"Edges: {counts.get('edges', 0)}\n"
                f"Datasets: {counts.get('bindings', 0)}\n"
                f"Alerts: {counts.get('alerts', 0)}"
            ),
            inline=True,
        )
        if first.key_nodes:
            node_lines = []
            for node in first.key_nodes[:5]:
                label = str(node.get("name") or "Node")
                layer = str(node.get("layer") or "").strip()
                maker = str(node.get("manufacturer") or "").strip()
                bits = [part for part in (layer, maker) if part]
                node_lines.append(f"{label}" + (f" | {', '.join(bits)}" if bits else ""))
            embed.add_field(name="Exposure Nodes", value="\n".join(node_lines)[:1024], inline=False)
        if first.alerts:
            embed.add_field(name="Alerts", value="\n".join(f"{row.name}: {row.trigger}" for row in first.alerts[:5])[:1024], inline=False)
        if len(rows) > 1:
            embed.add_field(name="Other Matches", value="\n".join(row.title for row in rows[1:])[:1024], inline=False)
        embed.set_footer(text=f"Supply chain: {first.slug} | published public projection")
        await interaction.followup.send(embed=embed, ephemeral=False)

    @tree.command(name="transcripts", description="Find public earnings transcript links for a ticker.", guild=guild)
    @app_commands.describe(
        most_recent="Return the latest transcript when no period is specified",
        year="Fiscal year filter, for example 2025",
        quarter="Fiscal quarter filter, 1-4; requires year",
    )
    async def transcripts(
        interaction: discord.Interaction,
        ticker: str,
        most_recent: bool = True,
        year: app_commands.Range[int, 1990, 2100] | None = None,
        quarter: app_commands.Range[int, 1, 4] | None = None,
    ) -> None:
        if not await deps.ensure_guild(interaction):
            return
        if quarter is not None and year is None:
            await deps.deny(interaction, "Quarter requires year. Example: year=2025 quarter=3.")
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
            def _lookup() -> tuple[Any, Any, list[Any]]:
                from thoth_terminal.db import session_scope
                from thoth_terminal.transcripts import get_transcript, list_transcripts

                with session_scope() as session:
                    requested = get_transcript(session, ticker=sym, most_recent=query_most_recent, year=year_v, quarter=quarter_v)
                    if requested:
                        return requested, None, []
                    try:
                        alternatives = list_transcripts(session, ticker=sym, most_recent=False, limit=5)
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
                row = fallback_row
                notice = f"Requested Q{quarter_v} {year_v} not found. Showing latest available transcript."
            else:
                await interaction.followup.send(f"No public transcript found for ${sym}.", ephemeral=True)
                return
        else:
            notice = None
        embed = discord.Embed(title=f"${sym} - Transcript", color=0x93C5FD)
        if notice:
            embed.add_field(name="Notice", value=notice, inline=False)
            available_periods = ", ".join([(alt.period_label or "Unspecified") for alt in alternatives if alt.period_label][:3])
            if available_periods:
                embed.add_field(name="Available", value=available_periods[:1024], inline=False)
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

    @tree.command(name="report", description="Fetch a public research report for a company or concept.", guild=guild)
    @app_commands.describe(subject="Ticker, company, concept, technology, or subject slug", refresh="Operator-only: queue a stale-check rebuild when permitted.")
    async def report_command(
        interaction: discord.Interaction,
        subject: str,
        refresh: bool = False,
    ) -> None:
        if not await deps.ensure_guild(interaction):
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        raw_subject = str(subject or "").strip()
        if not raw_subject:
            await interaction.followup.send("Missing subject.", ephemeral=True)
            return
        allow_compile = bool(refresh) and deps.is_operator(interaction)
        if refresh and not allow_compile:
            await interaction.followup.send("Report refresh is available to operators only.", ephemeral=True)
            return
        cfg = await asyncio.to_thread(desk_mod.get_desk_settings, guild_id=ctx.guild_id)
        notify_channel_id = int(getattr(settings, "discord_channel_id", None) or int(interaction.channel_id or 0) or 0)
        notify_thread_id: int | None = None
        current_parent_id = getattr(getattr(interaction, "channel", None), "parent_id", None)
        if current_parent_id is not None and int(interaction.channel_id or 0):
            notify_channel_id = int(current_parent_id)
            notify_thread_id = int(interaction.channel_id or 0)
        if cfg.desk_channel_id:
            notify_channel_id = int(cfg.desk_channel_id)
        if cfg.desk_thread_id:
            notify_thread_id = int(cfg.desk_thread_id)
        notify_discord = {
            "guild_id": ctx.guild_id,
            "channel_id": int(notify_channel_id) if notify_channel_id else None,
            "thread_id": int(notify_thread_id) if notify_thread_id else None,
            "announce_in_main": bool(notify_thread_id),
            "requested_by_user_id": int(interaction.user.id),
            "dedupe_key_prefix": f"discord_report_notify:{ctx.guild_id}:{int(interaction.id)}",
        }

        def _report_action() -> dict[str, object]:
            from thoth_terminal.db import session_scope

            permission_level = services.get_report_permission_level(guild_id=ctx.guild_id)
            can_compile = permission_level == "elevated" and bool(allow_compile)
            with session_scope() as session:
                state_payload = get_research_report_state(session, subject=raw_subject)
                report = dict(state_payload.get("report") or {})
                version = dict(state_payload.get("version") or {}) if isinstance(state_payload.get("version"), dict) else None
                if version is not None and not refresh:
                    return {"state": "available", "permission_level": permission_level, **state_payload}
                if not can_compile:
                    return {"state": "read_only", "permission_level": permission_level, **state_payload}
                job = enqueue_report_build(
                    session,
                    subject=str(report.get("slug") or raw_subject),
                    force=False,
                    trigger_kind="discord",
                    trigger_label="refresh" if refresh else ("compile_if_missing" if version is None else "stale_check"),
                    dedupe_key=f"research_report_build:{report.get('subject_key') or raw_subject}",
                    notify_discord=notify_discord,
                )
                return {"state": "queued", "job_id": int(job.id), "permission_level": permission_level, **state_payload}

        try:
            outcome = await asyncio.to_thread(_report_action)
        except Exception as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        state = str(outcome.get("state") or "")
        report_payload = dict(outcome.get("report") or {})
        version_payload = dict(outcome.get("version") or {}) if isinstance(outcome.get("version"), dict) else None
        subject_payload = dict(outcome.get("subject") or {})
        coverage_payload = dict(outcome.get("coverage") or {})
        freshness_payload = dict(outcome.get("freshness") or {})
        report_slug = str(report_payload.get("slug") or "").strip()
        public_report_url = public_dto.public_report_reader_url(report_slug) if report_slug else None
        report_url = str(public_report_url or notes_bridge.public_base_url()).strip()
        subject_label = str(subject_payload.get("display_name") or report_payload.get("display_name") or raw_subject)
        kind = str(subject_payload.get("kind") or report_payload.get("subject_kind") or "").strip()
        metric_rows = [dict(row) for row in (coverage_payload.get("metrics") or []) if isinstance(row, dict)]
        gaps = [str(row).strip() for row in (coverage_payload.get("gaps") or []) if str(row).strip()]
        ticker = str(subject_payload.get("primary_ticker") or report_payload.get("primary_ticker") or "").strip()
        tickers = [str(row).strip() for row in (subject_payload.get("tickers") or []) if str(row).strip()]
        aliases = [str(row).strip() for row in (subject_payload.get("aliases") or []) if str(row).strip()]
        latest_version = int(version_payload.get("version")) if version_payload and version_payload.get("version") is not None else None
        has_public_report = latest_version is not None
        if not has_public_report:
            subject_label = raw_subject
        as_of_label = _discord_datetime_label(version_payload.get("as_of_at") if version_payload else None)
        published_label = _discord_datetime_label(version_payload.get("published_at") if version_payload else None)
        freshness_state = str(freshness_payload.get("state") or "unpublished")
        age_days = freshness_payload.get("age_days")
        coverage_line_parts: list[str] = []
        for label in ("Primers", "Teardowns", "Theses", "Notes", "Transcripts", "Transcript summaries", "Supply chains", "Datasets"):
            value = _report_metric_lookup(metric_rows, label)
            if value is not None:
                coverage_line_parts.append(f"{label}: {value}")
        header_bits: list[str] = []
        if has_public_report and kind:
            header_bits.append(f"Type: {kind}")
        if has_public_report and ticker:
            header_bits.append(f"Primary ticker: ${ticker}")
        elif has_public_report and tickers:
            header_bits.append(f"Tickers: {', '.join([f'${row}' for row in tickers[:3]])}")
        if has_public_report:
            header_bits.append(f"Version: v{latest_version}")
        if has_public_report and freshness_state in {"fresh", "stale"}:
            freshness_line = "Fresh" if freshness_state == "fresh" else "Stale"
            if age_days is not None:
                freshness_line += f" ({int(age_days)}d old)"
            header_bits.append(freshness_line)

        embed = discord.Embed(title=f"Research Report - {subject_label}", color=0x60A5FA)
        description_lines: list[str] = []
        if state == "available":
            description_lines.append("Latest public report is available.")
        elif state == "queued":
            description_lines.append("Report refresh queued.")
            if latest_version is not None:
                description_lines.append("Serving the latest public report until the refresh completes.")
            if notify_thread_id:
                description_lines.append(f"Completion will post in <#{int(notify_thread_id)}>.")
            elif notify_channel_id:
                description_lines.append(f"Completion will post in <#{int(notify_channel_id)}>.")
        elif state == "read_only":
            description_lines.append("Latest public report is available." if latest_version is not None else "No public report is available yet.")
        if has_public_report and subject_payload.get("description"):
            description_lines.append(str(subject_payload.get("description")).strip()[:300])
        if description_lines:
            embed.description = "\n".join(description_lines)[:4000]

        if header_bits:
            embed.add_field(name="Subject", value="\n".join(header_bits)[:1024], inline=False)
        if published_label or as_of_label:
            time_lines = []
            if published_label:
                time_lines.append(f"Published: {published_label}")
            if as_of_label:
                time_lines.append(f"As of: {as_of_label}")
            embed.add_field(name="Timing", value="\n".join(time_lines)[:1024], inline=False)
        if has_public_report and coverage_line_parts:
            embed.add_field(name="Coverage", value=" | ".join(coverage_line_parts)[:1024], inline=False)
        if has_public_report and gaps:
            embed.add_field(name="Top Gaps", value="\n".join([f"- {gap}" for gap in gaps[:3]])[:1024], inline=False)
        artifact_links: dict[str, str] = {}
        if has_public_report and aliases:
            embed.set_footer(text=f"Aliases: {', '.join(aliases[:4])}"[:2048])
        view = _report_links_view(report_url=report_url, artifact_links=artifact_links)
        if state in {"available", "read_only", "queued"}:
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
            return
        await interaction.followup.send("Unable to resolve report state.", ephemeral=True)
