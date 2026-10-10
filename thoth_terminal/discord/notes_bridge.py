from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from urllib.parse import quote_plus

from sqlalchemy.orm import Session

from thoth_terminal.config import settings
from thoth_terminal.db import utcnow
from thoth_terminal.discord.models import DiscordWatchlistEntry
from thoth_terminal.models import Entity
from thoth_terminal.models import Note
from thoth_terminal.notes_query import list_notes
from thoth_terminal.notes_service import create_note
from thoth_terminal.thesis_service import resolve_entities


DISCORD_NOTE_TYPE = "discord"


def public_base_url() -> str:
    base = (getattr(settings, "public_base_url", None) or "").strip()
    if not base:
        base = f"http://{settings.host}:{settings.port}"
    return base.rstrip("/")


def note_url(note_id: int) -> str:
    return f"{public_base_url()}/notes/{int(note_id)}"


def thesis_url(thesis_id: int) -> str:
    return f"{public_base_url()}/theses/{int(thesis_id)}"


def dataset_url(dataset_id: int) -> str:
    return f"{public_base_url()}/visualizations/datasets/{int(dataset_id)}"


def chart_url(chart_id: int) -> str:
    return f"{public_base_url()}/visualizations/{int(chart_id)}"


def instrument_request_url(request_id: int) -> str:
    return f"{public_base_url()}/instruments/requests/{int(request_id)}"


def product_stage_url(entry_id: int) -> str:
    return f"{public_base_url()}/stage-tracker/{int(entry_id)}"


def supply_chain_url(chain_id: int, *, node_key: str | None = None) -> str:
    base = f"{public_base_url()}/visualizations/supply-chains/{int(chain_id)}/command-center"
    focus = str(node_key or "").strip()
    if not focus:
        return base
    return f"{base}?node={quote_plus(focus)}"


def notes_search_url(q: str) -> str:
    return f"{public_base_url()}/notes?q={quote_plus(q)}&note_type={DISCORD_NOTE_TYPE}"


def discord_message_url(*, guild_id: int, channel_id: int, message_id: int) -> str:
    return f"https://discord.com/channels/{int(guild_id)}/{int(channel_id)}/{int(message_id)}"


def _now() -> datetime:
    return utcnow()


def _norm_symbol(value: str) -> str:
    return (value or "").strip().lstrip("$").upper()


def _norm_subject_entity(subject: str) -> str:
    s = (subject or "").strip()
    if not s:
        return s
    if s.startswith("$") and len(s) > 1:
        # Treat leading $ as decoration for tickers.
        s = s[1:].strip() or s
    # Canonicalize topic-style subjects.
    low = s.lower()
    if low.startswith("sector:"):
        rest = s.split(":", 1)[1].strip().lower()
        return f"Sector:{rest}" if rest else "Sector:"
    if low.startswith("subgroup:"):
        rest = s.split(":", 1)[1].strip().lower()
        return f"Subgroup:{rest}" if rest else "Subgroup:"
    if low.startswith("masterlist:"):
        rest = s.split(":", 1)[1].strip().lower()
        return f"Masterlist:{rest}" if rest else "Masterlist:"
    if low.startswith("mastersubgroup:"):
        rest = s.split(":", 1)[1].strip().lower()
        return f"MasterSubgroup:{rest}" if rest else "MasterSubgroup:"
    if low.startswith("watchlist:"):
        rest = s.split(":", 1)[1].strip().lower()
        return f"Watchlist:{rest}" if rest else "Watchlist:"
    return s


def _sector_entity(sector_key: str) -> str:
    return f"Sector:{sector_key}"


def _subgroup_entity(sector_key: str, subgroup_key: str) -> str:
    return f"Subgroup:{sector_key}:{subgroup_key}"


def _masterlist_entity(masterlist_key: str) -> str:
    return f"Masterlist:{masterlist_key}"


def _master_subgroup_entity(masterlist_key: str, subgroup_key: str) -> str:
    return f"MasterSubgroup:{masterlist_key}:{subgroup_key}"


def _watchlist_entity(kind: str) -> str:
    return f"Watchlist:{kind}"


def _topic_entity_tokens(session: Session, *, guild_id: int, symbol: str) -> list[str]:
    sym = _norm_symbol(symbol)
    if not sym:
        return []
    rows = (
        session.query(DiscordWatchlistEntry)
        .filter_by(guild_id=int(guild_id), symbol=sym, removed_at=None)
        .all()
    )

    out: list[str] = []

    if any(r.list_kind == "exec" for r in rows):
        out.append(_watchlist_entity("exec"))

    comm = [r for r in rows if r.list_kind == "community"]
    if comm:
        out.append(_watchlist_entity("community"))
        sector_keys = {str(r.sector_key) for r in comm if getattr(r, "sector_key", None)}
        for sk in sorted(sector_keys):
            out.append(_sector_entity(sk))
        subgroup_pairs = {(str(r.sector_key), str(r.subgroup_key)) for r in comm if getattr(r, "sector_key", None) and getattr(r, "subgroup_key", None)}
        for sk, gk in sorted(subgroup_pairs):
            out.append(_subgroup_entity(sk, gk))

    master = [r for r in rows if r.list_kind == "masterlist"]
    if master:
        out.append(_watchlist_entity("masterlist"))
        m_keys = {str(r.sector_key) for r in master if getattr(r, "sector_key", None)}
        for mk in sorted(m_keys):
            out.append(_masterlist_entity(mk))
        m_pairs = {(str(r.sector_key), str(r.subgroup_key)) for r in master if getattr(r, "sector_key", None) and getattr(r, "subgroup_key", None)}
        for mk, gk in sorted(m_pairs):
            out.append(_master_subgroup_entity(mk, gk))

    return out


def _active_watchlist_symbols(session: Session, *, guild_id: int) -> set[str]:
    rows = (
        session.query(DiscordWatchlistEntry.symbol)
        .filter_by(guild_id=int(guild_id), removed_at=None)
        .distinct()
        .all()
    )
    return {str(r[0]).upper() for r in rows if r and r[0]}


_DOLLAR_TICKER_RE = re.compile(r"\$(?P<sym>[A-Za-z0-9][A-Za-z0-9.\-]{0,15})")
_TOKEN_RE = re.compile(r"\b(?P<sym>[A-Za-z0-9]{1,12}(?:[.\-][A-Za-z0-9]{1,12})?)\b")
_ENTITY_TICKER_RE = re.compile(r"^[A-Z0-9]{1,12}(?:[.\-][A-Z0-9]{1,12})?$")


def _desired_entity_kind(token: str) -> str:
    t = (token or "").strip()
    if not t:
        return "other"
    if (
        t.startswith("Sector:")
        or t.startswith("Subgroup:")
        or t.startswith("Watchlist:")
        or t.startswith("Masterlist:")
        or t.startswith("MasterSubgroup:")
    ):
        return "topic"
    if _ENTITY_TICKER_RE.match(t):
        return "ticker"
    return "other"


def _apply_entity_kinds(session: Session, *, tokens: list[str], entities: list[Entity]) -> None:
    by_name = {(e.canonical_name or "").strip().lower(): e for e in entities if e and e.canonical_name}
    for tok in tokens:
        ent = by_name.get((tok or "").strip().lower())
        if not ent:
            continue
        desired = _desired_entity_kind(tok)
        if desired != "other" and (not ent.kind or ent.kind == "other"):
            ent.kind = desired
    session.flush()


def detect_symbols_from_text(text: str, *, watchlist_symbols: set[str]) -> tuple[set[str], set[str]]:
    """
    Returns (explicit_syms, watchlist_syms).

    - explicit_syms: symbols referenced as $TICKER (always included)
    - watchlist_syms: plain tokens that match an active watchlist symbol
    """
    raw = text or ""
    explicit = {_norm_symbol(m.group("sym")) for m in _DOLLAR_TICKER_RE.finditer(raw)}
    plain: set[str] = set()
    for m in _TOKEN_RE.finditer(raw):
        tok = _norm_symbol(m.group("sym"))
        if tok and tok in watchlist_symbols:
            plain.add(tok)
    return explicit, plain


def upsert_discord_note(
    session: Session,
    *,
    client_request_id: str,
    title: str,
    body_md: str,
    entity_tokens: list[str] | None,
    extra_discord: dict[str, Any],
) -> Note:
    cid = (client_request_id or "").strip()
    if not cid:
        raise ValueError("Missing client_request_id.")

    now = _now()
    existing = session.query(Note).filter_by(client_request_id=cid).first()
    tokens = [t.strip() for t in (entity_tokens or []) if t and t.strip()]
    # Preserve ordering but unique.
    seen: set[str] = set()
    tokens_unique: list[str] = []
    for t in tokens:
        if t not in seen:
            tokens_unique.append(t)
            seen.add(t)

    if existing:
        existing.title = (title or "").strip() or existing.title
        existing.body_md = body_md or ""
        existing.note_type = DISCORD_NOTE_TYPE
        existing.updated_at = now
        existing.extra = dict(existing.extra or {})
        existing.extra["discord"] = dict(extra_discord or {})
        if tokens_unique:
            ents = resolve_entities(session, tokens_unique)
            _apply_entity_kinds(session, tokens=tokens_unique, entities=ents)
            existing.entities = ents
        session.flush()
        return existing

    note = create_note(
        session,
        title=(title or "").strip() or "(untitled)",
        body_md=body_md or "",
        note_type=DISCORD_NOTE_TYPE,
        status="active",
        importance=3,
        pinned=False,
        entity_tokens=tokens_unique or None,
        client_request_id=cid,
        extra={"discord": dict(extra_discord or {})},
    )
    if tokens_unique:
        _apply_entity_kinds(session, tokens=tokens_unique, entities=list(note.entities or []))
    session.flush()
    return note


def create_exec_manual_note(
    session: Session,
    *,
    guild_id: int,
    interaction_id: int,
    channel_id: int,
    user_id: int,
    subject: str,
    text: str,
    sentiment: str | None,
) -> Note:
    subj = _norm_subject_entity(subject)
    if not subj:
        raise ValueError("Missing subject.")
    body = (text or "").strip()
    if not body:
        raise ValueError("Missing text.")

    extra = {
        "kind": "manual_note",
        "guild_id": int(guild_id),
        "channel_id": int(channel_id),
        "user_id": int(user_id),
        "sentiment": (sentiment or None),
    }
    cid = f"discord_note:{int(guild_id)}:{int(interaction_id)}"

    entity_tokens: list[str] = [subj]

    # If the subject matches a watchlist ticker, attach watchlist/topic entities.
    sym = _norm_symbol(subj)
    topics = _topic_entity_tokens(session, guild_id=guild_id, symbol=sym)
    if topics:
        # Ensure the canonical ticker entity is present for retrieval.
        if sym and sym not in entity_tokens:
            entity_tokens.insert(0, sym)
        entity_tokens.extend(topics)

    title = f"{subj} — note"
    if sentiment:
        title = f"{subj} — {sentiment}"
    return upsert_discord_note(
        session,
        client_request_id=cid,
        title=title,
        body_md=body,
        entity_tokens=entity_tokens,
        extra_discord=extra,
    )


def create_exec_message_capture(
    session: Session,
    *,
    guild_id: int,
    channel_id: int,
    message_id: int,
    message_author: str | None,
    message_content: str,
    user_id: int,
    message_created_at: datetime | None,
) -> Note:
    permalink = discord_message_url(guild_id=guild_id, channel_id=channel_id, message_id=message_id)
    content = (message_content or "").strip()
    if not content:
        content = "(no text)"

    header = f"Captured from Discord: {permalink}"
    if message_author:
        header = f"Captured from Discord ({message_author}): {permalink}"
    if message_created_at:
        header = header + f"\nCapturedAt: {message_created_at.isoformat()}"

    body = header + "\n\n" + content
    cid = f"discord_msg:{int(guild_id)}:{int(channel_id)}:{int(message_id)}"
    extra = {
        "kind": "captured_message",
        "guild_id": int(guild_id),
        "channel_id": int(channel_id),
        "message_id": int(message_id),
        "user_id": int(user_id),
        "permalink": permalink,
    }

    watch_syms = _active_watchlist_symbols(session, guild_id=guild_id)
    explicit, plain = detect_symbols_from_text(content, watchlist_symbols=watch_syms)
    matched_watchlist = set(plain) | {s for s in explicit if s in watch_syms}
    entity_tokens: list[str] = []
    for sym in sorted(set(explicit) | set(plain)):
        if sym:
            entity_tokens.append(sym)

    for sym in sorted(matched_watchlist):
        entity_tokens.extend(_topic_entity_tokens(session, guild_id=guild_id, symbol=sym))

    title = "Discord capture"
    if entity_tokens:
        title = f"Discord capture — {', '.join(entity_tokens[:3])}"
        if len(entity_tokens) > 3:
            title += "…"

    return upsert_discord_note(
        session,
        client_request_id=cid,
        title=title,
        body_md=body,
        entity_tokens=entity_tokens or None,
        extra_discord=extra,
    )


def upsert_note_for_source(
    session: Session,
    *,
    guild_id: int,
    channel_id: int,
    message_id: int | None,
    user_id: int,
    src_id: int,
    symbol: str,
    title: str,
    url: str,
    note: str | None,
    source_type: str,
    tags: list[str] | None,
    sentiment: str | None,
) -> Note:
    sym = _norm_symbol(symbol)
    if not sym:
        raise ValueError("Missing ticker.")

    tags_v = [t.strip() for t in (tags or []) if t and t.strip()]
    lines: list[str] = [f"URL: {url}"]
    if sentiment:
        lines.append(f"Sentiment: {sentiment}")
    if source_type:
        lines.append(f"Type: {source_type}")
    if tags_v:
        lines.append("Tags: " + ", ".join(tags_v))
    if note and note.strip():
        lines.append("\n" + note.strip())
    body = "\n".join(lines).strip()

    extra = {
        "kind": "source",
        "guild_id": int(guild_id),
        "channel_id": int(channel_id),
        "message_id": (int(message_id) if message_id else None),
        "user_id": int(user_id),
        "src_id": int(src_id),
        "symbol": sym,
        "sentiment": (sentiment or None),
        "source_type": (source_type or None),
        "tags": tags_v,
        "url": url,
    }
    cid = f"discord_src:{int(guild_id)}:{int(src_id)}"
    entity_tokens = [sym] + _topic_entity_tokens(session, guild_id=guild_id, symbol=sym)
    return upsert_discord_note(
        session,
        client_request_id=cid,
        title=f"Source — {sym} — {(title or '').strip()[:120]}",
        body_md=body,
        entity_tokens=entity_tokens,
        extra_discord=extra,
    )


def upsert_note_for_exec_idea(
    session: Session,
    *,
    guild_id: int,
    channel_id: int,
    message_id: int | None,
    user_id: int,
    idea_id: int,
    symbol: str,
    horizon: str,
    horizon_n: int,
    thesis: str,
    catalysts: str | None,
    invalidation: str | None,
    levels: str | None,
    confidence: int,
    status: str,
) -> Note:
    sym = _norm_symbol(symbol)
    if not sym:
        raise ValueError("Missing ticker.")

    unit = (horizon or "").strip()
    n = int(horizon_n or 1)
    singular = unit[:-1] if unit.endswith("s") else unit
    unit_disp = singular if n == 1 else unit

    parts: list[str] = [
        f"Horizon: {n} {unit_disp}",
        f"Confidence: {int(confidence)}",
    ]
    if catalysts and catalysts.strip():
        parts.append("\nCatalysts:\n" + catalysts.strip())
    if invalidation and invalidation.strip():
        parts.append("\nInvalidation:\n" + invalidation.strip())
    if levels and levels.strip():
        parts.append("\nLevels:\n" + levels.strip())
    parts.append("\nThesis:\n" + (thesis or "").strip())
    body = "\n".join(parts).strip()

    extra = {
        "kind": "exec_idea",
        "guild_id": int(guild_id),
        "channel_id": int(channel_id),
        "message_id": (int(message_id) if message_id else None),
        "user_id": int(user_id),
        "exidea_id": int(idea_id),
        "symbol": sym,
        "status": (status or "active"),
        "horizon": unit,
        "horizon_n": int(n),
        "confidence": int(confidence),
    }
    cid = f"discord_exidea:{int(guild_id)}:{int(idea_id)}"
    entity_tokens = [sym] + _topic_entity_tokens(session, guild_id=guild_id, symbol=sym)
    return upsert_discord_note(
        session,
        client_request_id=cid,
        title=f"Exec Idea — {sym} — {n} {unit_disp}",
        body_md=body,
        entity_tokens=entity_tokens,
        extra_discord=extra,
    )


def search_discord_notes(session: Session, *, query: str, limit: int = 10) -> list[Note]:
    q = (query or "").strip()
    if not q:
        return []
    limit = max(1, min(int(limit or 10), 30))
    return list_notes(session, query=q, note_type=DISCORD_NOTE_TYPE, limit=limit)
