from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable

import discord
from discord import app_commands

from thoth_terminal.discord import services
from thoth_terminal.discord.formatting import format_queue_id, format_src_id


@dataclass(frozen=True)
class ReviewCommandDeps:
    ensure_guild: Callable[[discord.Interaction], Awaitable[bool]]
    ensure_contributor: Callable[[discord.Interaction], Awaitable[bool]]
    ensure_moderator: Callable[[discord.Interaction], Awaitable[bool]]
    deny: Callable[[discord.Interaction, str], Awaitable[None]]
    parse_queue_id: Callable[[str], int]


def _short(value: object, limit: int = 240) -> str:
    text = " ".join(str(value or "").replace("\n", " ").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "..."


def register_review_commands(
    tree: app_commands.CommandTree,
    *,
    ctx: object,
    guild: discord.Object,
    deps: ReviewCommandDeps,
) -> None:
    review = app_commands.Group(name="review", description="Review submitted research evidence.")

    @review.command(name="evidence", description="List evidence submissions awaiting review.")
    async def review_evidence(
        interaction: discord.Interaction,
        limit: app_commands.Range[int, 1, 50] = 20,
    ) -> None:
        if not await deps.ensure_guild(interaction):
            return
        if not await deps.ensure_contributor(interaction):
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        items = await asyncio.to_thread(services.list_queue, guild_id=ctx.guild_id, status="pending", limit=int(limit))
        source_items = [item for item in items if str(item.kind or "") == "source_add"]
        if not source_items:
            await interaction.followup.send("No evidence submissions are waiting for review.", ephemeral=True)
            return
        lines = []
        for item in source_items:
            payload = item.payload if isinstance(item.payload, dict) else {}
            ticker = str(payload.get("ticker") or "?")
            title = _short(str(payload.get("title") or payload.get("url") or "Source"), 80)
            url = _short(str(payload.get("url") or ""), 100)
            by = f"<@{item.created_by_user_id}>"
            lines.append(f"{format_queue_id(item.id)} | ${ticker} | {title} | {by} | {url}")
        embed = discord.Embed(title="Evidence Review", color=0x93C5FD)
        embed.add_field(name="Pending", value="\n".join(lines)[:1024], inline=False)
        embed.set_footer(text="Moderators can approve or reject evidence submissions.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @review.command(name="approve", description="Approve an evidence submission (moderator-only).")
    async def review_approve(interaction: discord.Interaction, id: str) -> None:
        if not await deps.ensure_guild(interaction):
            return
        if not await deps.ensure_moderator(interaction):
            return
        try:
            qid = deps.parse_queue_id(id)
        except Exception as exc:
            await deps.deny(interaction, str(exc))
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        item = await asyncio.to_thread(services.get_queue_item, guild_id=ctx.guild_id, queue_id=qid)
        if not item or str(item.kind or "") != "source_add":
            await interaction.followup.send("Evidence submission not found.", ephemeral=True)
            return
        if item.status != "pending":
            await interaction.followup.send(f"{format_queue_id(item.id)} is already {item.status}.", ephemeral=True)
            return
        payload = item.payload if isinstance(item.payload, dict) else {}
        try:
            src_row = await asyncio.to_thread(
                services.create_ticker_source,
                guild_id=ctx.guild_id,
                symbol=str(payload.get("ticker") or ""),
                title=str(payload.get("title") or ""),
                url=str(payload.get("url") or ""),
                note=payload.get("note") if isinstance(payload.get("note"), str) else None,
                sentiment=None,
                tags=[str(tag) for tag in (payload.get("tags") if isinstance(payload.get("tags"), list) else [])],
                source_type=str(payload.get("type") or "other"),
                user_id=interaction.user.id,
            )
            await asyncio.to_thread(
                services.decide_queue_item,
                guild_id=ctx.guild_id,
                queue_id=item.id,
                decision="approved",
                decided_by_user_id=interaction.user.id,
            )
        except Exception as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        embed = discord.Embed(title=f"Evidence Approved - ${src_row.symbol}", color=0x6EE7B7)
        embed.add_field(name="Title", value=src_row.title[:1024], inline=False)
        embed.add_field(name="Source", value=src_row.url[:1024], inline=False)
        embed.add_field(name="Evidence ID", value=format_src_id(src_row.id), inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @review.command(name="tag", description="Tag an evidence submission before moderator approval.")
    async def review_tag(interaction: discord.Interaction, id: str, tags: str) -> None:
        if not await deps.ensure_guild(interaction):
            return
        if not await deps.ensure_contributor(interaction):
            return
        try:
            qid = deps.parse_queue_id(id)
        except Exception as exc:
            await deps.deny(interaction, str(exc))
            return
        try:
            item = await asyncio.to_thread(
                services.tag_queue_evidence_item,
                guild_id=ctx.guild_id,
                queue_id=qid,
                tags=services.split_csv(tags),
                tagged_by_user_id=interaction.user.id,
            )
        except Exception as exc:
            await deps.deny(interaction, str(exc))
            return
        payload = item.payload if isinstance(item.payload, dict) else {}
        tag_list = [str(tag) for tag in (payload.get("tags") if isinstance(payload.get("tags"), list) else [])]
        await interaction.response.send_message(
            f"Tagged {format_queue_id(item.id)}: {', '.join(tag_list)}.",
            ephemeral=True,
        )

    @review.command(name="reject", description="Reject an evidence submission (moderator-only).")
    async def review_reject(interaction: discord.Interaction, id: str, reason: str | None = None) -> None:
        if not await deps.ensure_guild(interaction):
            return
        if not await deps.ensure_moderator(interaction):
            return
        try:
            qid = deps.parse_queue_id(id)
        except Exception as exc:
            await deps.deny(interaction, str(exc))
            return
        item = await asyncio.to_thread(services.get_queue_item, guild_id=ctx.guild_id, queue_id=qid)
        if not item or str(item.kind or "") != "source_add":
            await deps.deny(interaction, "Evidence submission not found.")
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
            await deps.deny(interaction, str(exc))
            return
        await interaction.response.send_message(f"Rejected {format_queue_id(qid)}.", ephemeral=True)

    tree.add_command(review, guild=guild)
