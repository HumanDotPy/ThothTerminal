from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import re

from sqlalchemy.orm import Session

from thoth_terminal.agent.context_bundle import (
    build_recent_context_bundle,
    render_recent_context_markdown,
)
from thoth_terminal.agent_actions import upsert_agent_action
from thoth_terminal.agent.execution_policy import (
    SynthesisResult,
    TASK_CONTEXT_COMPRESSION,
    TASK_DESK_DIGEST,
    run_synthesis_task,
)
from thoth_terminal.agent.llm_client import AgentLlmError, ChatMessage, llm_available
from thoth_terminal.agent_request_manifest_service import manifested_chat_json
from thoth_terminal.config import settings
from thoth_terminal.db import session_factory, session_scope, utcnow
from thoth_terminal.discord import notes_bridge, outbox, services
from thoth_terminal.discord.desk import get_agent_settings, get_desk_settings
from thoth_terminal.discord.formatting import format_pct
from thoth_terminal.discord.models import DiscordEvidenceItem, DiscordEvidenceTarget
from thoth_terminal.ingest import upsert_internal_document
from thoth_terminal.instrument_service import create_instrument_request
from thoth_terminal.jobs import enqueue_agent_context_compression_job, enqueue_job
from thoth_terminal.local_model_runtime import local_model_endpoint, local_model_name
from thoth_terminal.models import Entity, Job, Note
from thoth_terminal.thesis_service import resolve_entities


AGENT_JOB_DIGEST = "agent_desk_digest"
AGENT_JOB_TICKER_TIMELINE = "agent_ticker_timeline"
AGENT_JOB_CONTEXT_COMPRESSION = "agent_context_compression"
_ACTION_KEY_RE = re.compile(r"[^a-z0-9]+")
_SIGNAL_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_\-/&+]{2,}")
_TICKER_RE = re.compile(r"\$[A-Za-z][A-Za-z0-9]{0,5}")
_SIGNAL_STOPWORDS = {
    "about", "after", "analyst", "analysts", "article", "articles", "auto", "automatically", "before", "broad",
    "business", "china", "count", "counts", "daily", "desk", "digest", "documents", "earnings", "finance",
    "financial", "generated", "global", "group", "highlights", "hours", "latest", "lookback", "market", "markets",
    "just", "money", "new", "news", "notes", "recent", "report", "reports", "research", "semiconductor", "semiconductors",
    "source", "sources", "summary", "terminal", "thoth", "today", "update", "updates", "well", "window", "your", "yup",
    "than", "the", "and", "http", "https", "www", "com", "are", "for", "this", "you", "with", "from", "into", "that",
    "they", "them", "their", "there", "here", "what", "when", "where", "which", "while", "will", "would", "could",
    "should", "still", "only", "need", "needs", "have", "has", "had", "was", "were", "why", "how", "lets", "let",
    "deal", "great", "good", "guys", "bro", "bang", "last", "year", "acting", "like", "hyped", "dropped", "list",
    "company", "companies", "april", "january", "february", "march", "may", "june", "july", "august", "september",
    "october", "november", "december", "shipment", "shipments", "upgrade", "upgrading", "holder", "holders",
    "funding", "pipeline", "forecast", "maintain", "maintained", "rating", "target", "raised", "raise", "cut",
    "lowered", "reiterate", "reiterated", "bullish", "bearish", "price", "prices",
}


@dataclass(frozen=True)
class DigestScheduleSlot:
    key: str
    label: str
    hhmm: str
    lookback_hours: int
    days: set[int]
    announce_in_main: bool


def _now() -> datetime:
    return utcnow()


def _llm_config() -> tuple[str | None, str | None, float, int]:
    endpoint = local_model_endpoint()
    model = local_model_name()
    timeout_s = float(getattr(settings, "agent_llm_timeout_s", 60) or 60)
    max_chars = int(getattr(settings, "agent_llm_max_input_chars", 24000) or 24000)
    return endpoint, model, timeout_s, max_chars


def _llm_available_for_agent() -> bool:
    endpoint, model, _, _ = _llm_config()
    return llm_available(endpoint=endpoint, model=model, provider=getattr(settings, "ai_provider", "openai"))


def _chat_json(messages: list[ChatMessage]) -> dict[str, Any]:
    endpoint, model, timeout_s, max_chars = _llm_config()
    with session_scope() as audit_session:
        return manifested_chat_json(
            audit_session,
            run_kind="discord_agent_job",
            run_id=None,
            task_kind="ticker_timeline_summary",
            messages=messages,
            endpoint=endpoint,
            model=model,
            timeout_s=timeout_s,
            max_input_chars=max_chars,
            provider=getattr(settings, "ai_provider", "openai"),
            permission_profile="propose-only",
        )


def _parse_hhmm(value: str, *, default: time) -> time:
    raw = (value or "").strip()
    if not raw:
        return default
    parts = raw.split(":")
    if len(parts) < 2:
        return default
    try:
        hh = int(parts[0])
        mm = int(parts[1])
    except Exception:
        return default
    if hh < 0 or hh > 23 or mm < 0 or mm > 59:
        return default
    return time(hour=hh, minute=mm)


_DOW = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _parse_days(value: str, *, default: set[int]) -> set[int]:
    raw = (value or "").strip().lower()
    if not raw:
        return set(default)
    out: set[int] = set()
    for part in raw.split(","):
        key = part.strip()[:3]
        if key in _DOW:
            out.add(_DOW[key])
    return out or set(default)


def _desk_tz() -> ZoneInfo:
    name = str(getattr(settings, "discord_desk_digest_tz", "America/New_York") or "America/New_York")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _digest_schedule_slots() -> list[DigestScheduleSlot]:
    default_days = _parse_days(
        getattr(settings, "discord_desk_digest_days", "mon,tue,wed,thu,fri"),
        default={0, 1, 2, 3, 4},
    )
    announce_in_main = bool(getattr(settings, "discord_desk_digest_main_notice_enabled", True))
    slots: list[DigestScheduleSlot] = []
    if bool(getattr(settings, "discord_desk_digest_midday_enabled", False)):
        slots.append(
            DigestScheduleSlot(
                key="midday",
                label="Midday Digest",
                hhmm=str(getattr(settings, "discord_desk_digest_midday_hhmm", "12:15") or "12:15"),
                lookback_hours=max(1, int(getattr(settings, "discord_desk_digest_midday_lookback_hours", 8) or 8)),
                days=set(default_days),
                announce_in_main=announce_in_main,
            )
        )
    slots.append(
        DigestScheduleSlot(
            key="eod",
            label="Daily Research Summary",
            hhmm=str(getattr(settings, "discord_desk_digest_hhmm", "21:00") or "21:00"),
            lookback_hours=max(1, int(getattr(settings, "discord_desk_digest_lookback_hours", 24) or 24)),
            days=set(default_days),
            announce_in_main=announce_in_main,
        )
    )
    return slots


def list_digest_schedule_slots() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for slot in _digest_schedule_slots():
        rows.append(
            {
                "key": slot.key,
                "label": slot.label,
                "hhmm": slot.hhmm,
                "lookback_hours": int(slot.lookback_hours),
                "days": sorted(int(day) for day in slot.days),
                "announce_in_main": bool(slot.announce_in_main),
            }
        )
    return rows


def _find_digest_slot(slot_key: str | None) -> DigestScheduleSlot | None:
    wanted = str(slot_key or "").strip().lower()
    if not wanted:
        return None
    for slot in _digest_schedule_slots():
        if slot.key == wanted:
            return slot
    return None


def _should_enqueue_digest_slot(now_utc: datetime, slot: DigestScheduleSlot) -> tuple[bool, str]:
    tz = _desk_tz()
    now_local = now_utc.replace(tzinfo=timezone.utc).astimezone(tz)

    allowed = set(slot.days or {0, 1, 2, 3, 4})
    if now_local.weekday() not in allowed:
        return False, now_local.date().isoformat()

    target_t = _parse_hhmm(slot.hhmm, default=time(21, 0))
    target_local = now_local.replace(hour=target_t.hour, minute=target_t.minute, second=0, microsecond=0)
    if now_local < target_local:
        return False, now_local.date().isoformat()

    return True, now_local.date().isoformat()


def _desk_thread_ids(*, guild_id: int) -> tuple[int | None, int | None]:
    cfg = get_desk_settings(guild_id=int(guild_id))
    return cfg.desk_channel_id, cfg.desk_thread_id


def _agent_setting_int(cfg: dict[str, Any], key: str, default: int, *, min_v: int | None = None, max_v: int | None = None) -> int:
    raw = cfg.get(key)
    try:
        val = int(raw)
    except Exception:
        val = int(default)
    if min_v is not None:
        val = max(int(min_v), val)
    if max_v is not None:
        val = min(int(max_v), val)
    return val


def _agent_setting_sections(cfg: dict[str, Any]) -> list[str] | None:
    raw = cfg.get("default_include_sections")
    if not isinstance(raw, list):
        return None
    out = [str(x).strip() for x in raw if str(x).strip()]
    return out or None


def enqueue_daily_digest_if_due() -> list[Job]:
    if not bool(getattr(settings, "agent_enabled", False)):
        return []
    if not bool(getattr(settings, "agent_auto_digest_enabled", True)):
        return []
    if not bool(getattr(settings, "discord_desk_enabled", True)):
        return []

    guild_id = getattr(settings, "discord_guild_id", None)
    if not guild_id:
        return []
    guild_id = int(guild_id)

    # Require desk thread config.
    desk_channel_id, desk_thread_id = _desk_thread_ids(guild_id=guild_id)
    if not desk_channel_id or not desk_thread_id:
        return []

    now = _now()
    jobs: list[Job] = []
    with session_scope() as session:
        for slot in _digest_schedule_slots():
            ok, date_key = _should_enqueue_digest_slot(now, slot)
            if not ok:
                continue
            dedupe_key = f"agent_digest:{guild_id}:{date_key}:{slot.key}"
            existing = session.query(Job).filter_by(kind=AGENT_JOB_DIGEST, dedupe_key=dedupe_key).first()
            if existing:
                continue
            jobs.append(
                enqueue_job(
                    session,
                    kind=AGENT_JOB_DIGEST,
                    payload={
                        "guild_id": guild_id,
                        "mode": "scheduled",
                        "slot_key": slot.key,
                        "slot_label": slot.label,
                        "lookback_hours": int(slot.lookback_hours),
                        "post_channel_id": int(desk_channel_id),
                        "post_thread_id": int(desk_thread_id),
                        "date_key": date_key,
                        "announce_in_main": bool(slot.announce_in_main),
                    },
                    dedupe_key=dedupe_key,
                    priority=2,
                )
            )
    return jobs


def enqueue_manual_digest(
    *,
    guild_id: int,
    post_channel_id: int,
    post_thread_id: int | None,
    slot_key: str | None = None,
    lookback_hours: int | None = None,
    title_override: str | None = None,
    announce_in_main: bool | None = None,
    dedupe_key: str | None = None,
) -> Job:
    slot = _find_digest_slot(slot_key)
    if slot_key and slot is None:
        valid = ", ".join(slot_row.key for slot_row in _digest_schedule_slots()) or "midday, eod"
        raise ValueError(f"Unknown digest slot '{slot_key}'. Valid slots: {valid}.")
    lookback_default = int(slot.lookback_hours) if slot is not None else int(getattr(settings, "discord_desk_digest_lookback_hours", 24) or 24)
    date_key = _now().replace(tzinfo=timezone.utc).astimezone(_desk_tz()).date().isoformat()
    lookback = int(lookback_hours if lookback_hours is not None else lookback_default)
    payload = {
        "guild_id": int(guild_id),
        "mode": "manual",
        "lookback_hours": lookback,
        "post_channel_id": int(post_channel_id),
        "post_thread_id": int(post_thread_id) if post_thread_id else None,
        "date_key": date_key,
        "slot_key": (slot.key if slot is not None else None),
        "slot_label": (slot.label if slot is not None else None),
        "title_override": (str(title_override).strip() if isinstance(title_override, str) and str(title_override).strip() else None),
        "announce_in_main": (
            bool(slot.announce_in_main)
            if announce_in_main is None and slot is not None
            else (
                bool(getattr(settings, "discord_desk_digest_main_notice_enabled", True))
                if announce_in_main is None
                else bool(announce_in_main)
            )
        ),
    }
    with session_scope() as session:
        return enqueue_job(
            session,
            kind=AGENT_JOB_DIGEST,
            payload=payload,
            dedupe_key=(dedupe_key.strip() if isinstance(dedupe_key, str) and dedupe_key.strip() else None),
            priority=2,
        )


def enqueue_ticker_timeline(
    *,
    guild_id: int,
    ticker: str,
    days: int = 30,
    post_to_outbox: bool = False,
    post_channel_id: int | None = None,
    post_thread_id: int | None = None,
    dedupe_key: str | None = None,
) -> Job:
    sym = services.normalize_symbol(ticker)
    if not sym:
        raise ValueError("Missing ticker.")
    days_v = max(1, min(int(days or 30), 365))
    payload = {
        "guild_id": int(guild_id),
        "ticker": sym,
        "days": days_v,
        "post_to_outbox": bool(post_to_outbox),
        "post_channel_id": int(post_channel_id) if post_channel_id else None,
        "post_thread_id": int(post_thread_id) if post_thread_id else None,
    }
    with session_scope() as session:
        return enqueue_job(
            session,
            kind=AGENT_JOB_TICKER_TIMELINE,
            payload=payload,
            dedupe_key=(dedupe_key.strip() if isinstance(dedupe_key, str) and dedupe_key.strip() else None),
            priority=6,
        )


def enqueue_context_compression(
    *,
    guild_id: int,
    mode: str = "custom",
    lookback_hours: int | None = None,
    focus_query: str | None = None,
    tickers: list[str] | None = None,
    include_sections: list[str] | None = None,
    post_to_outbox: bool = False,
    post_channel_id: int | None = None,
    post_thread_id: int | None = None,
    persist_note: bool = True,
    title_override: str | None = None,
    client_request_id: str | None = None,
    dedupe_key: str | None = None,
) -> Job:
    with session_scope() as session:
        return enqueue_agent_context_compression_job(
            session,
            guild_id=int(guild_id),
            mode=mode,
            lookback_hours=lookback_hours,
            focus_query=focus_query,
            tickers=tickers,
            include_sections=include_sections,
            post_to_outbox=bool(post_to_outbox),
            post_channel_id=post_channel_id,
            post_thread_id=post_thread_id,
            persist_note=bool(persist_note),
            title_override=title_override,
            client_request_id=client_request_id,
            dedupe_key=dedupe_key,
        )


def _note_snippet(note: Note, *, max_len: int = 160) -> str:
    body = (getattr(note, "body_md", "") or "").strip().replace("\n", " ")
    if len(body) <= max_len:
        return body
    return body[: max(0, max_len - 1)] + "…"


def _action_dedupe_key(prefix: str, text: str) -> str:
    norm = _ACTION_KEY_RE.sub("-", str(text or "").strip().lower()).strip("-")
    return f"{prefix}:{norm[:96] or 'item'}"


def _persist_follow_up_actions(
    session: Session,
    *,
    guild_id: int,
    note_id: int | None,
    source_kind: str,
    source_title: str,
    actions: list[str],
    priority: int,
) -> None:
    for idx, action_text in enumerate(actions):
        text = str(action_text or "").strip()
        if not text:
            continue
        upsert_agent_action(
            session,
            guild_id=int(guild_id),
            kind="follow_up",
            title=text[:255],
            priority=int(priority),
            dedupe_key=_action_dedupe_key(f"{source_kind}:{guild_id}", text),
            rationale=f"{source_title}: follow-up surfaced by agent output.",
            note_id=int(note_id) if note_id is not None else None,
            payload={
                "source_kind": source_kind,
                "source_title": source_title,
                "source_note_id": int(note_id) if note_id is not None else None,
                "action_index": int(idx),
            },
        )


def _infer_requested_outputs(text: str) -> list[str]:
    low = str(text or "").strip().lower()
    outputs: list[str] = []
    if "chart" in low:
        outputs.append("chart")
    if "dataset" in low or "table" in low:
        outputs.append("dataset")
        if "table" in low and "table" not in outputs:
            outputs.append("table")
    if "tracker" in low or "stage" in low:
        outputs.append("stage_tracker")
    if "instrument" in low and "dataset" not in outputs and "chart" not in outputs:
        outputs.append("instrument")
    return outputs or ["instrument"]


def _persist_instrument_requests(
    session: Session,
    *,
    guild_id: int,
    note_id: int | None,
    source_kind: str,
    source_title: str,
    summary: str | None,
    actions: list[str],
    coverage_gaps: list[str],
    priority: int,
) -> None:
    candidates = []
    for text in list(actions or []) + list(coverage_gaps or []):
        clean = str(text or "").strip()
        if not clean:
            continue
        low = clean.lower()
        if not any(token in low for token in ("chart", "dataset", "table", "tracker", "instrument")):
            continue
        candidates.append(clean)
    for idx, text in enumerate(candidates[:6]):
        linked_objects = {"note_ids": [int(note_id)]} if note_id is not None else {}
        create_instrument_request(
            session,
            title=text[:255],
            intent="coverage_gap",
            summary=(summary or source_title or text)[:1200],
            desired_outputs=_infer_requested_outputs(text),
            evidence_bundle=[{"title": source_title, "source": source_kind, "url": notes_bridge.note_url(int(note_id)) if note_id is not None else None}],
            provenance_summary=f"{source_title}: request surfaced from {source_kind} coverage gaps.",
            evidence_tier="trusted_analyst",
            priority=int(priority),
            automation_source=source_kind,
            source_kind="agent",
            linked_objects=linked_objects,
        )


def _format_movers(rows: list[services.PerfRow], *, limit: int = 8) -> str:
    # Prefer since_24h; drop pending/None.
    vals = [r for r in rows if r.since_24h is not None and not r.baseline_pending]
    vals.sort(key=lambda r: float(r.since_24h or 0.0), reverse=True)
    top = vals[: max(0, int(limit))]
    lines: list[str] = []
    for r in top:
        lines.append(f"${r.symbol} {format_pct(r.since_24h)}")
    return "\n".join(lines) if lines else "No movers yet (baselines pending or market data unavailable)."


def _as_pct(v: float | None) -> str:
    if v is None:
        return "—"
    try:
        return f"{v*100:+.1f}%"
    except Exception:
        return "—"


def _top_movers_table(rows: list[services.PerfRow], *, limit: int = 8) -> str:
    vals = [r for r in rows if r.since_24h is not None and not r.baseline_pending]
    vals.sort(key=lambda r: float(r.since_24h or 0.0), reverse=True)
    top = vals[: max(0, int(limit))]
    if not top:
        return "No movers yet (baselines pending or market data unavailable)."
    lines = ["Ticker | Since(24h) | 1M | YTD | 1Y", "---|---:|---:|---:|---:"]
    for r in top:
        lines.append(
            f"${r.symbol} | {_as_pct(r.since_24h)} | {_as_pct(r.m1)} | {_as_pct(r.ytd)} | {_as_pct(r.y1)}"
        )
    return "\n".join(lines)


def _gather_recent_notes(session: Session, *, cutoff: datetime) -> list[Note]:
    notes = (
        session.query(Note)
        .filter(Note.note_type == notes_bridge.DISCORD_NOTE_TYPE)
        .filter(Note.status != "archived")
        .filter(Note.updated_at >= cutoff)
        .order_by(Note.updated_at.desc())
        .limit(200)
        .all()
    )
    out: list[Note] = []
    for n in notes:
        extra = n.extra if isinstance(n.extra, dict) else {}
        discord_extra = extra.get("discord") if isinstance(extra.get("discord"), dict) else {}
        kind = str(discord_extra.get("kind") or "")
        if kind in {"agent_digest", "source"}:
            continue
        out.append(n)
    return out


def _source_record_from_evidence_row(evidence: DiscordEvidenceItem, target: DiscordEvidenceTarget | None) -> services.SourceRecord:
    symbol = services.normalize_symbol(getattr(target, "symbol", None) or "")
    return services.SourceRecord(
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
        target_kind=(str(target.target_kind) if target else "other"),
        target_key=(str(target.target_key) if target else None),
        target_label=(str(target.target_label) if target and target.target_label else None),
    )


def _gather_recent_sources(session: Session, *, guild_id: int, cutoff: datetime) -> list[services.SourceRecord]:
    rows = (
        session.query(DiscordEvidenceItem, DiscordEvidenceTarget)
        .outerjoin(DiscordEvidenceTarget, DiscordEvidenceTarget.evidence_id == DiscordEvidenceItem.id)
        .filter(DiscordEvidenceItem.guild_id == int(guild_id))
        .filter(DiscordEvidenceItem.created_at >= cutoff)
        .order_by(
            DiscordEvidenceItem.created_at.desc(),
            DiscordEvidenceTarget.is_primary.desc(),
            DiscordEvidenceTarget.id.asc(),
        )
        .limit(400)
        .all()
    )
    out: list[services.SourceRecord] = []
    seen: set[int] = set()
    for evidence, target in rows:
        evidence_id = int(evidence.id)
        if evidence_id in seen:
            continue
        seen.add(evidence_id)
        out.append(_source_record_from_evidence_row(evidence, target))
        if len(out) >= 200:
            break
    return out


def _digest_prompt(*, sources: list[services.SourceRecord], notes: list[Note], movers_md: str, context_md: str | None = None) -> str:
    src_lines = []
    for s in sources[:30]:
        sent = (s.sentiment or "").strip()
        sent_txt = f" ({sent})" if sent else ""
        src_lines.append(f"- ${s.symbol}{sent_txt}: {s.title} — {s.url}")
    note_lines = []
    for n in notes[:30]:
        note_lines.append(f"- {n.title}: {_note_snippet(n)}")

    prompt = (
        "Return JSON ONLY with keys: summary (string), key_points (array of strings), actions (array of strings), risks (array of strings), evidence (array of objects), coverage_gaps (array of strings), proposed_mutations (array of objects).\n"
        "Write this like an analyst summary, not a digest, source inventory, or generic corroboration warning.\n"
        "Summary: 3-5 sentences that explain what changed, why it matters, which entities are implicated, and which structured surfaces should be checked.\n"
        "Key points: each bullet should pair a concrete fact or claim with its implication; include MoM/YoY trade or dataset deltas when context provides them.\n"
        "Actions: only concrete checks worth queueing, such as entity resolution, rolling report refresh, trade-flow comparison, or research request creation.\n"
        "Risks: confirmation gaps, rumor risk, contradictory evidence, bad data hygiene, or missing higher-precedence evidence.\n"
        "Evidence rows are the place for raw source references; keep them out of the lead summary unless they are essential.\n"
        "Do not repeat boilerplate like 'directional until corroborated' unless it changes a concrete next action.\n"
        "Keep each key point short (<= 180 chars). Max 6 key points, 5 actions, 5 risks, 5 evidence rows.\n"
        "Do not lead with counts of documents, notes, or sources unless the window is effectively empty.\n\n"
        "Recent sources:\n"
        + ("\n".join(src_lines) if src_lines else "- (none)")
        + "\n\nRecent notes:\n"
        + ("\n".join(note_lines) if note_lines else "- (none)")
        + "\n\nTop movers (markdown table):\n"
        + movers_md
    )
    if context_md and str(context_md).strip():
        prompt += "\n\nBroader Thoth context:\n" + str(context_md).strip()
    return prompt


def _digest_markdown(
    *,
    date_label: str,
    lookback_hours: int,
    summary: str,
    highlights: list[str],
    actions: list[str],
    risks: list[str],
    surface_lines: list[str],
    evidence_lines: list[str],
    coverage_gaps: list[str],
    movers_md: str,
    sources: list[services.SourceRecord],
    notes: list[Note],
) -> str:
    md = [f"# Daily Desk Summary — {date_label}", f"_Lookback: {int(lookback_hours)}h_", ""]
    if summary:
        md.append("## Executive Summary")
        md.append(summary)
        md.append("")
    if highlights:
        md.append("## What Changed")
        md.extend([f"- {h}" for h in highlights[:6]])
        md.append("")
    if risks:
        md.append("## Why It Matters / Watchouts")
        md.extend([f"- {r}" for r in risks[:5]])
        md.append("")
    if actions:
        md.append("## Recommended Follow-Up")
        md.extend([f"- {a}" for a in actions[:5]])
        md.append("")
    if surface_lines:
        md.append("## Structured Surfaces")
        md.extend([f"- {line}" for line in surface_lines[:6]])
        md.append("")
    if coverage_gaps:
        md.append("## Coverage Gaps")
        md.extend([f"- {gap}" for gap in coverage_gaps[:6]])
        md.append("")
    if evidence_lines:
        md.append("## Evidence / Source Appendix")
        md.extend([f"- {line}" for line in evidence_lines[:8]])
        md.append("")
    md.append("## Market Context")
    md.append(movers_md)
    md.append("")
    return "\n".join(md).strip() + "\n"


def _digest_note_title(*, date_key: str, title_override: str | None = None, slot_label: str | None = None) -> str:
    override = str(title_override or "").strip()
    if override:
        return override
    slot_label_v = str(slot_label or "").strip()
    if slot_label_v:
        return f"{slot_label_v} — {date_key}"
    return f"Desk Digest — {date_key}"


def _discord_compact_message(lines: list[str], *, limit: int = 1800) -> str | None:
    out: list[str] = []
    total = 0
    for raw_line in lines:
        line = str(raw_line or "").rstrip()
        if not line and (not out or out[-1] == ""):
            continue
        extra = len(line) + (1 if out else 0)
        if total + extra > limit:
            remaining = limit - total - (1 if out else 0)
            if remaining > 1:
                out.append((line[: max(0, remaining - 1)]).rstrip() + "…")
            break
        out.append(line)
        total += extra
    text = "\n".join(out).strip()
    return text or None


def _enqueue_thread_summary_post(
    *,
    guild_id: int,
    post_channel_id: int | None,
    post_thread_id: int | None,
    thread_lines: list[str],
    embed: dict[str, Any] | None,
    priority: int = 5,
    dedupe_key_base: str | None = None,
    announce_in_main: bool | None = None,
    notice_lines: list[str] | None = None,
) -> None:
    if not post_channel_id:
        return
    thread_content = _discord_compact_message(thread_lines, limit=1800)
    should_announce = bool(post_thread_id) if announce_in_main is None else bool(announce_in_main)
    embed_payload = dict(embed) if isinstance(embed, dict) else None
    if post_thread_id and should_announce and embed_payload is not None:
        embed_payload = outbox.add_notice_after_post(
            embed_payload,
            channel_id=int(post_channel_id),
            lines=notice_lines or [],
            dedupe_key=(f"{dedupe_key_base}:notice" if dedupe_key_base else None),
            priority=max(1, int(priority) + 1),
        )
    outbox.create_outbox_item(
        guild_id=guild_id,
        channel_id=int(post_channel_id),
        thread_id=int(post_thread_id) if post_thread_id else None,
        content=thread_content,
        embed_json=embed_payload,
        dedupe_key=(f"{dedupe_key_base}:post" if dedupe_key_base else None),
        priority=int(priority),
        created_by_kind="agent",
        created_by_user_id=None,
        max_attempts=5,
    )


def _structured_surface_lines(bundle: dict[str, Any], *, limit: int = 6) -> list[str]:
    sections = bundle.get("sections") if isinstance(bundle.get("sections"), dict) else {}
    out: list[str] = []

    for label, key in (
        ("Dataset", "datasets"),
        ("Supply chain", "supply_chains"),
        ("Report", "research_library"),
        ("Chart", "charts"),
        ("Request", "instrument_requests"),
        ("Stage", "product_stages"),
    ):
        for row in sections.get(key) or []:
            if not isinstance(row, dict):
                continue
            title = str(row.get("title") or row.get("product_name") or "").strip()
            if key == "product_stages":
                title = f"{str(row.get('company_name') or '').strip()} · {str(row.get('product_name') or '').strip()} · {str(row.get('stage') or '').strip()}".strip(" ·")
            url = str(row.get("url") or "").strip()
            if not title or not url:
                continue
            out.append(f"{label}: {title} | {url}")
            if len(out) >= limit:
                return out
    return out


def _create_digest_note(
    session: Session,
    *,
    guild_id: int,
    date_key: str,
    title: str,
    body_md: str,
    llm_used: bool,
    llm_error: str | None,
    engine_metadata: dict[str, Any] | None = None,
) -> Note:
    client_id = f"discord_agent_digest:{int(guild_id)}:{date_key}"
    extra = {
        "kind": "agent_digest",
        "guild_id": int(guild_id),
        "date_key": date_key,
        "llm_used": bool(llm_used),
        "llm_error": llm_error,
        "engine_metadata": dict(engine_metadata or {}),
    }
    note = notes_bridge.upsert_discord_note(
        session,
        client_request_id=client_id,
        title=title,
        body_md=body_md,
        entity_tokens=["Watchlist:exec", "Watchlist:community"],
        extra_discord=extra,
    )
    return note


def _publish_agent_note_document(
    session: Session,
    *,
    external_id: str,
    title: str,
    body_md: str,
    note_id: int,
    published_at: datetime | None = None,
    kind: str,
    extra: dict[str, Any] | None = None,
) -> None:
    note_url = notes_bridge.note_url(int(note_id))
    doc_extra = {
        "agent_generated": True,
        "agent_kind": str(kind or "").strip() or "agent_output",
        "note_id": int(note_id),
        "note_url": note_url,
    }
    if isinstance(extra, dict):
        doc_extra.update(extra)
    upsert_internal_document(
        session,
        external_id=external_id,
        kind=kind,
        title=title,
        text=body_md,
        url=note_url,
        canonical_url=note_url,
        published_at=published_at,
        extra=doc_extra,
    )


def handle_agent_desk_digest(payload: dict[str, Any]) -> None:
    guild_id = int(payload.get("guild_id") or 0)
    if not guild_id:
        return
    lookback_h = int(payload.get("lookback_hours") or getattr(settings, "discord_desk_digest_lookback_hours", 24) or 24)
    post_channel_id = int(payload.get("post_channel_id") or 0)
    post_thread_id = int(payload.get("post_thread_id") or 0) or None
    date_key = str(payload.get("date_key") or "").strip() or _now().date().isoformat()
    slot_key = str(payload.get("slot_key") or "").strip().lower() or None
    slot_label = str(payload.get("slot_label") or "").strip() or None
    doc_key = f"{date_key}:{slot_key}" if slot_key else date_key
    title = _digest_note_title(
        date_key=date_key,
        title_override=str(payload.get("title_override") or "").strip() or None,
        slot_label=slot_label,
    )
    announce_in_main = (
        bool(payload.get("announce_in_main"))
        if payload.get("announce_in_main") is not None
        else bool(getattr(settings, "discord_desk_digest_main_notice_enabled", True))
    )

    cutoff = _now() - timedelta(hours=max(1, lookback_h))

    # Best-effort: refresh exec performance before producing movers.
    try:
        services.refresh_watchlist_perf(guild_id=guild_id, list_kind="exec", force_refresh=False)
    except Exception:
        pass

    perf_cached = services.list_watchlist_perf_cached(guild_id=guild_id, list_kind="exec")
    movers_md = _top_movers_table(perf_cached.rows, limit=8)

    SessionLocal = session_factory()
    with SessionLocal() as session:
        recent_notes = _gather_recent_notes(session, cutoff=cutoff)
        recent_sources = _gather_recent_sources(session, guild_id=guild_id, cutoff=cutoff)
        bundle = build_recent_context_bundle(
            session,
            guild_id=guild_id,
            lookback_hours=lookback_h,
            max_items_per_section=int(getattr(settings, "agent_context_max_items_per_section", 25) or 25),
            include_full_text=False,
        )
    context_md = render_recent_context_markdown(bundle, max_chars=8000)

    def _fallback_digest_result() -> SynthesisResult:
        return _fallback_synthesis_from_bundle(
            bundle=bundle,
            lookback_hours=lookback_h,
            focus_query=None,
        )

    with session_scope() as audit_session:
        digest_result = run_synthesis_task(
            task_kind=TASK_DESK_DIGEST,
            prompt=_digest_prompt(
                sources=recent_sources,
                notes=recent_notes,
                movers_md=movers_md,
                context_md=context_md,
            ),
            fallback_factory=_fallback_digest_result,
            requested_engine=str(payload.get("engine") or "").strip() or None,
            audit_session=audit_session,
        )
    summary = digest_result.summary or _fallback_digest_result().summary
    highlights = digest_result.key_points or _fallback_digest_result().key_points
    actions = digest_result.actions
    risks = digest_result.risks
    coverage_gaps = digest_result.coverage_gaps
    evidence_lines: list[str] = []
    for item in digest_result.evidence or []:
        if not isinstance(item, dict):
            continue
        title_line = str(item.get("title") or "").strip()
        if not title_line:
            continue
        source = str(item.get("source") or "").strip()
        prefix = f"[{source}] " if source else ""
        tail = str(item.get("url") or "").strip()
        evidence_lines.append(prefix + title_line + (f" | {tail}" if tail else ""))
    if not evidence_lines:
        evidence_lines = [
            f"[{str(item.get('source') or '').strip()}] {str(item.get('title') or '').strip()}".strip()
            for item in (_fallback_digest_result().evidence or [])
            if isinstance(item, dict) and str(item.get("title") or "").strip()
        ]
    llm_used = bool((digest_result.engine_metadata or {}).get("llm_used"))
    llm_error = str((digest_result.engine_metadata or {}).get("fallback_reason") or "").strip() or None
    surface_lines = _structured_surface_lines(bundle)

    body_md = _digest_markdown(
        date_label=date_key,
        lookback_hours=lookback_h,
        summary=summary,
        highlights=highlights,
        actions=actions,
        risks=risks,
        surface_lines=surface_lines,
        evidence_lines=evidence_lines,
        coverage_gaps=coverage_gaps,
        movers_md=movers_md,
        sources=recent_sources,
        notes=recent_notes,
    )

    with session_scope() as session:
        note = _create_digest_note(
            session,
            guild_id=guild_id,
            date_key=doc_key,
            title=title,
            body_md=body_md,
            llm_used=bool(llm_used),
            llm_error=llm_error,
            engine_metadata=digest_result.engine_metadata,
        )
        note_id = int(note.id)
        _publish_agent_note_document(
            session,
            external_id=f"agent_digest:{guild_id}:{doc_key}",
            title=title,
            body_md=body_md,
            note_id=note_id,
            published_at=_now(),
            kind="agent_digest",
            extra={
                "guild_id": int(guild_id),
                "date_key": date_key,
                "slot_key": slot_key,
                "slot_label": slot_label,
                "lookback_hours": int(lookback_h),
                "llm_used": bool(llm_used),
                "llm_error": llm_error,
                "engine_metadata": digest_result.engine_metadata,
                "summary": summary,
                "coverage_gaps": coverage_gaps,
            },
        )
        _persist_follow_up_actions(
            session,
            guild_id=guild_id,
            note_id=note_id,
            source_kind="agent_digest",
            source_title=title,
            actions=actions,
            priority=5,
        )
        _persist_instrument_requests(
            session,
            guild_id=guild_id,
            note_id=note_id,
            source_kind="agent_digest",
            source_title=title,
            summary=summary,
            actions=actions,
            coverage_gaps=coverage_gaps,
            priority=5,
        )

    # Create outbox post (embed).
    thoth_link = notes_bridge.note_url(note_id)
    thread_lines: list[str] = [f"**{title}**"]
    if summary:
        thread_lines.append(summary.strip())
    if highlights:
        thread_lines.append("")
        thread_lines.append("Highlights:")
        thread_lines.extend([f"• {h}" for h in highlights[:5]])
    if risks:
        thread_lines.append("")
        thread_lines.append("Watchouts:")
        thread_lines.extend([f"• {r}" for r in risks[:3]])
    if actions:
        thread_lines.append("")
        thread_lines.append("Follow-Up:")
        thread_lines.extend([f"• {a}" for a in actions[:3]])
    thread_lines.append("")
    thread_lines.append(f"Thoth: {thoth_link}")
    thread_content = _discord_compact_message(thread_lines, limit=1800)
    desc_lines: list[str] = []
    if summary:
        desc_lines.append(summary.strip())
    if highlights:
        desc_lines.extend([f"• {h}" for h in highlights[:4]])
    embed = {
        "title": title[:256],
        "description": ("\n".join(desc_lines) if desc_lines else "Desk digest ready.")[:4000],
        "color": 0x60A5FA,
        "fields": [],
        "footer": {"text": "Generated automatically"},
    }
    if risks:
        embed["fields"].append({"name": "Watchouts", "value": "\n".join([f"• {r}" for r in risks[:3]])[:1024], "inline": False})
    elif coverage_gaps:
        embed["fields"].append({"name": "Coverage Gaps", "value": "\n".join([f"• {g}" for g in coverage_gaps[:3]])[:1024], "inline": False})
    if actions:
        embed["fields"].append({"name": "Follow-Up", "value": "\n".join([f"• {a}" for a in actions[:3]])[:1024], "inline": False})
    if surface_lines:
        embed["fields"].append({"name": "Open Surfaces", "value": "\n".join([f"• {line}" for line in surface_lines[:3]])[:1024], "inline": False})
    embed["fields"].append({"name": f"Top Exec Movers (Since {int(lookback_h)}h)", "value": _format_movers(perf_cached.rows, limit=8)[:1024], "inline": False})
    embed["fields"].append({"name": "Thoth", "value": thoth_link[:1024], "inline": False})
    if post_channel_id and post_thread_id and announce_in_main:
        embed = outbox.add_notice_after_post(
            embed,
            channel_id=int(post_channel_id),
            lines=[
                f"{title} is live in <#{int(post_thread_id)}>.",
                summary.strip() if summary else "Digest posted.",
                f"Thoth: {thoth_link}",
            ],
            dedupe_key=f"desk_digest_notice:{guild_id}:{doc_key}",
            priority=6,
        ) or embed
    if post_channel_id:
        outbox.create_outbox_item(
            guild_id=guild_id,
            channel_id=post_channel_id,
            thread_id=post_thread_id,
            content=thread_content,
            embed_json=embed,
            dedupe_key=f"desk_digest_post:{guild_id}:{doc_key}",
            priority=5,
            created_by_kind="agent",
            created_by_user_id=None,
            max_attempts=5,
        )


def _timeline_note_markdown(*, sym: str, days: int, notes: list[Note], sources: list[services.SourceRecord], summary: str | None) -> str:
    md: list[str] = [f"# Ticker Timeline — ${sym}", f"_Window: last {int(days)} days_", ""]
    if summary:
        md.append("## Summary")
        md.append(summary.strip())
        md.append("")
    if sources:
        md.append("## Sources")
        for s in sources[:40]:
            sent = f" ({s.sentiment})" if s.sentiment else ""
            md.append(f"- {s.created_at.date().isoformat()} — {s.title}{sent} — {s.url}")
        md.append("")
    if notes:
        md.append("## Notes")
        for n in notes[:60]:
            md.append(f"- {n.updated_at.date().isoformat()} — {n.title} — {notes_bridge.note_url(int(n.id))}")
        md.append("")
    return "\n".join(md).strip() + "\n"


def handle_agent_ticker_timeline(payload: dict[str, Any]) -> None:
    guild_id = int(payload.get("guild_id") or 0)
    sym = services.normalize_symbol(str(payload.get("ticker") or ""))
    if not guild_id or not sym:
        return
    days = max(1, min(int(payload.get("days") or 30), 365))
    post_to_outbox = bool(payload.get("post_to_outbox"))
    post_channel_id = int(payload.get("post_channel_id") or 0) or None
    post_thread_id = int(payload.get("post_thread_id") or 0) or None

    cutoff = _now() - timedelta(days=days)

    SessionLocal = session_factory()
    with SessionLocal() as session:
        notes = (
            session.query(Note)
            .filter(Note.note_type == notes_bridge.DISCORD_NOTE_TYPE)
            .filter(Note.status != "archived")
            .filter(Note.updated_at >= cutoff)
            .join(Note.entities)
            .filter(Entity.canonical_name == sym)
            .order_by(Note.updated_at.desc())
            .limit(200)
            .all()
        )
        notes = [
            note
            for note in notes
            if str(((note.extra or {}).get("discord") or {}).get("kind") or "").strip().lower() != "source"
        ]
        source_rows = (
            session.query(DiscordEvidenceItem, DiscordEvidenceTarget)
            .join(DiscordEvidenceTarget, DiscordEvidenceTarget.evidence_id == DiscordEvidenceItem.id)
            .filter(DiscordEvidenceItem.guild_id == int(guild_id))
            .filter(DiscordEvidenceItem.created_at >= cutoff)
            .filter(DiscordEvidenceTarget.symbol == sym)
            .order_by(
                DiscordEvidenceItem.created_at.desc(),
                DiscordEvidenceTarget.is_primary.desc(),
                DiscordEvidenceTarget.id.asc(),
            )
            .limit(300)
            .all()
        )
        sources: list[services.SourceRecord] = []
        seen_source_ids: set[int] = set()
        for evidence, target in source_rows:
            evidence_id = int(evidence.id)
            if evidence_id in seen_source_ids:
                continue
            seen_source_ids.add(evidence_id)
            sources.append(_source_record_from_evidence_row(evidence, target))
            if len(sources) >= 200:
                break

    summary: str | None = None
    llm_error: str | None = None
    if _llm_available_for_agent():
        try:
            prompt = (
                "Return JSON ONLY with keys: summary (string), bullets (array of strings).\n"
                "summary <= 600 chars; bullets max 8 items.\n\n"
                f"Ticker: {sym}\n"
                f"Notes count: {len(notes)}\n"
                f"Sources count: {len(sources)}\n\n"
                "Latest sources:\n"
                + "\n".join([f"- {s.title} — {s.url}" for s in sources[:20]])
                + "\n\nLatest notes:\n"
                + "\n".join([f"- {n.title}: {_note_snippet(n)}" for n in notes[:20]])
            )
            parsed = _chat_json(
                [
                    ChatMessage(role="system", content="You summarize research timelines for a trader."),
                    ChatMessage(role="user", content=prompt),
                ]
            )
            summ = parsed.get("summary")
            if isinstance(summ, str) and summ.strip():
                bullets = parsed.get("bullets")
                extra_bullets: list[str] = []
                if isinstance(bullets, list):
                    extra_bullets = [str(b).strip() for b in bullets if str(b).strip()][:8]
                if extra_bullets:
                    summary = summ.strip() + "\n\n" + "\n".join([f"- {b}" for b in extra_bullets])
                else:
                    summary = summ.strip()
        except AgentLlmError as exc:
            llm_error = str(exc)
        except Exception as exc:
            llm_error = f"{type(exc).__name__}: {exc}"

    if not summary:
        summary = f"Items: {len(sources)} sources, {len(notes)} notes."
        if llm_error:
            summary += f" (LLM unavailable: {llm_error})"

    body_md = _timeline_note_markdown(sym=sym, days=days, notes=notes, sources=sources, summary=summary)
    client_id = f"discord_agent_ticker:{guild_id}:{sym}:{days}"
    extra = {"kind": "agent_ticker_timeline", "guild_id": int(guild_id), "ticker": sym, "days": int(days), "llm_error": llm_error}

    with session_scope() as session:
        # Ensure ticker entity is kind=ticker where possible (matches other discord mirroring behavior).
        ents = resolve_entities(session, [sym])
        for e in ents:
            if (e.canonical_name or "").upper() == sym and (not e.kind or e.kind == "other"):
                e.kind = "ticker"
        session.flush()

        note = notes_bridge.upsert_discord_note(
            session,
            client_request_id=client_id,
            title=f"Ticker Timeline — ${sym}",
            body_md=body_md,
            entity_tokens=[sym],
            extra_discord=extra,
        )
        note_id = int(note.id)

    link = notes_bridge.note_url(note_id)
    if post_to_outbox and post_channel_id:
        recent_source_titles = [str(s.title or "").strip() for s in sources[:3] if str(s.title or "").strip()]
        thread_lines = [f"**Ticker Timeline — ${sym}**"]
        if summary:
            thread_lines.extend([line for line in str(summary).strip().splitlines() if line.strip()])
        thread_lines.append("")
        thread_lines.append(f"Window: {int(days)}d • Sources: {len(sources)} • Notes: {len(notes)}")
        if recent_source_titles:
            thread_lines.append("Recent source lineup:")
            thread_lines.extend([f"• {title}" for title in recent_source_titles])
        thread_lines.append(f"Thoth: {link}")
        embed = {
            "title": f"Ticker Timeline — ${sym}",
            "description": (summary or "").strip()[:4000],
            "color": 0x34D399,
            "fields": [
                {"name": "Coverage", "value": f"Window: {int(days)}d • Sources: {len(sources)} • Notes: {len(notes)}"[:1024], "inline": False},
                {"name": "Thoth", "value": link[:1024], "inline": False},
            ],
            "footer": {"text": "Generated automatically"},
        }
        if recent_source_titles:
            embed["fields"].insert(1, {"name": "Recent Sources", "value": "\n".join([f"• {title}" for title in recent_source_titles])[:1024], "inline": False})
        _enqueue_thread_summary_post(
            guild_id=guild_id,
            post_channel_id=int(post_channel_id),
            post_thread_id=int(post_thread_id) if post_thread_id else None,
            thread_lines=thread_lines,
            embed=embed,
            priority=6,
            dedupe_key_base=None,
            announce_in_main=bool(post_thread_id),
            notice_lines=[
                f"Ticker timeline for ${sym} is live in <#{int(post_thread_id)}>." if post_thread_id else f"Ticker timeline for ${sym} is live.",
                f"Window: {int(days)}d • Sources: {len(sources)} • Notes: {len(notes)}",
                f"Thoth: {link}",
            ],
        )


def _compression_title(mode: str, *, title_override: str | None = None) -> str:
    if title_override and str(title_override).strip():
        return str(title_override).strip()
    mode_v = (mode or "custom").strip().lower()
    mapping = {
        "daily_master": "Daily Master Compression",
        "topic": "Topic Compression",
        "decision_brief": "Decision Brief",
        "custom": "Context Compression",
    }
    return mapping.get(mode_v, "Context Compression")


def _compression_prompt(*, mode: str, focus_query: str | None, context_md: str) -> str:
    mode_v = (mode or "custom").strip().lower()
    focus = (focus_query or "").strip()
    style_line = (
        "For daily_master, write this like SparkNotes for the feed: lead with the few developments that mattered and explain why they matter.\n"
        "Do not lead with counts or a source inventory.\n"
        if mode_v == "daily_master"
        else ""
    )
    return (
        "Return JSON ONLY with keys: summary (string), key_points (array of strings), "
        "actions (array of strings), risks (array of strings), evidence (array of objects), "
        "coverage_gaps (array of strings), proposed_mutations (array of objects).\n"
        "Each evidence object may include title, source, url, document_id, precedence_bucket.\n"
        "Keep summary <= 900 chars. key_points/actions/risks max 8 each. coverage_gaps max 6. "
        "Do not invent citations or mutations that are not supported by the supplied context.\n"
        "This is a synthesis task, not a context dump. Distill what changed, what matters, and what still needs confirmation.\n"
        "Explicitly account for evidence precedence: separate higher-precedence / corroborated evidence from social or rumor-like items.\n"
        "Prefer cross-source themes, thesis implications, and supply-chain implications over repeating raw links.\n"
        + style_line
        + f"Mode: {mode_v}\n"
        + (f"Focus query: {focus}\n" if focus else "")
        + "\nRecent Thoth context:\n"
        + context_md
    )


def _source_precedence_bucket(source_name: str | None) -> str:
    raw = str(source_name or "").strip().lower()
    if not raw:
        return "secondary"
    if raw in {"primary", "issuer_local", "issuer_confirmed"}:
        return "primary"
    if raw in {"trusted_reporting", "secondary", "sell_side", "trusted_news_trade_press"}:
        return "secondary"
    if raw in {"analyst_social", "deprecated_low_confidence", "social", "trusted_analyst", "directional_unverified"}:
        return "social"
    if any(token in raw for token in ("x_list", "discord", "tweet", "twitter")):
        return "social"
    if any(token in raw for token in ("reuters", "press", "goldman", "ubs", "bernstein", "jefferies")):
        return "secondary"
    if any(token in raw for token in ("investor", "ir_", "filing", "transcript", "sec_", "earnings", "news_release")):
        return "primary"
    return "secondary"


def _normalize_signal_key(title: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", " ", str(title or "").lower()).strip()
    return " ".join(cleaned.split()[:12])


def _clean_signal_title(title: str | None) -> str:
    raw = str(title or "").strip()
    if not raw:
        return ""
    raw = re.sub(r"^@\w+:\s*", "", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    raw = re.sub(r"[(\[{<]+\s*$", "", raw).rstrip(" ,;:-")
    return raw


def _is_translation_heavy_title(title: str | None) -> bool:
    raw = str(title or "").strip()
    return bool(raw) and not raw.isascii()


def _is_low_signal_social_title(title: str) -> bool:
    key = _normalize_signal_key(title)
    words = [part for part in key.split() if part]
    if not words:
        return True
    if len(" ".join(words)) < 18:
        return True
    if len(set(words)) <= 2:
        return True
    return False


def _collect_context_signals(bundle: dict[str, Any], *, limit: int = 10) -> list[dict[str, str]]:
    sections = bundle.get("sections") if isinstance(bundle.get("sections"), dict) else {}
    signals: list[dict[str, str]] = []

    for row in sections.get("documents") or []:
        if not isinstance(row, dict):
            continue
        title = _clean_signal_title(
            row.get("refinery_signal")
            or row.get("refinery_summary")
            or row.get("title")
        )
        signals.append(
            {
                "kind": "document",
                "title": title,
                "source": str(row.get("source") or "").strip(),
                "timestamp": str(row.get("published_at") or row.get("ingested_at") or ""),
                "url": str(row.get("canonical_url") or row.get("url") or "").strip(),
                "bucket": _source_precedence_bucket(str(row.get("precedence_bucket") or row.get("source") or "")),
            }
        )

    for row in sections.get("notes") or []:
        if not isinstance(row, dict):
            continue
        note_id = row.get("id")
        note_url = notes_bridge.note_url(int(note_id)) if str(note_id).isdigit() else ""
        signals.append(
            {
                "kind": "note",
                "title": _clean_signal_title(row.get("title")),
                "source": "note",
                "timestamp": str(row.get("updated_at") or row.get("created_at") or ""),
                "url": note_url,
                "bucket": _source_precedence_bucket(str(row.get("precedence_bucket") or row.get("kind") or "secondary")),
            }
        )

    for row in sections.get("discord_sources") or []:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        title = _clean_signal_title(row.get("title"))
        if symbol and title and f"${symbol}" not in title:
            title = f"${symbol} {title}"
        signals.append(
            {
                "kind": "discord_source",
                "title": title,
                "source": "discord_source",
                "timestamp": str(row.get("created_at") or ""),
                "url": str(row.get("url") or "").strip(),
                "bucket": _source_precedence_bucket(str(row.get("precedence_bucket") or "analyst_social")),
            }
        )

    signals.sort(key=lambda row: row.get("timestamp") or "", reverse=True)
    deduped: list[dict[str, str]] = []
    seen: set[str] = set()
    for signal in signals:
        title = signal.get("title") or ""
        key = _normalize_signal_key(title)
        if not title or key in seen:
            continue
        if signal.get("bucket") == "social" and _is_low_signal_social_title(title):
            continue
        seen.add(key)
        deduped.append(signal)
        if len(deduped) >= limit:
            break
    return deduped


def _extract_context_topics(signals: list[dict[str, str]], *, limit: int = 4) -> list[str]:
    counts: Counter[str] = Counter()
    display: dict[str, str] = {}
    for signal in signals:
        seen_tokens: set[str] = set()
        title = signal.get("title") or ""
        for token in _TICKER_RE.findall(title):
            normalized = token.strip("$").lower()
            if not normalized or normalized in seen_tokens:
                continue
            seen_tokens.add(normalized)
            counts[normalized] += 2
            display.setdefault(normalized, token.upper())
        for token in _SIGNAL_TOKEN_RE.findall(title):
            normalized = token.strip("$").lower()
            if (
                len(normalized) < 4
                or normalized.isdigit()
                or normalized in seen_tokens
                or normalized in _SIGNAL_STOPWORDS
                or "_" in normalized
                or (token[:1].islower() and not any(ch.isdigit() or ch in "/&+-" for ch in token))
            ):
                continue
            seen_tokens.add(normalized)
            counts[normalized] += 1
            display.setdefault(
                normalized,
                token if (token[:1].isupper() or any(ch.isdigit() for ch in token) or any(ch in token for ch in "/&+-")) else normalized,
            )
    topics: list[str] = []
    for key, _count in counts.most_common(limit * 2):
        topics.append(display.get(key, key))
        if len(topics) >= limit:
            break
    return topics


def _human_join(parts: list[str]) -> str:
    items = [str(part).strip() for part in parts if str(part).strip()]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f", and {items[-1]}"


def _compact_signal_label(signal: dict[str, str], *, max_words: int = 16, max_chars: int = 110) -> str:
    title = str(signal.get("title") or "").strip().rstrip(".")
    if not title:
        return ""
    bucket = str(signal.get("bucket") or "secondary")
    topic_candidates = _extract_context_topics([signal], limit=4)
    first_clause = re.split(r"(?<=[.!?;])\s+", title, maxsplit=1)[0].strip().rstrip(" ,;:-")
    if bucket == "social" and (
        len(title) > max_chars
        or len(title.split()) > 10
        or title.lower().startswith(("so ", "bro", "lol", "lmao", "ok ", "okay "))
    ):
        if topic_candidates:
            return "social chatter around " + _human_join(topic_candidates[:4])
        return "low-signal social chatter"
    candidate = first_clause or title
    words = candidate.split()
    if len(candidate) > max_chars or len(words) > max_words:
        candidate = " ".join(words[:max_words]).rstrip(" ,;:-") + "..."
    return candidate


def _top_document_sources(bundle: dict[str, Any], *, limit: int = 3) -> list[str]:
    sections = bundle.get("sections") if isinstance(bundle.get("sections"), dict) else {}
    counts: Counter[str] = Counter()
    for row in sections.get("documents") or []:
        if not isinstance(row, dict):
            continue
        source = str(row.get("source") or "").strip()
        if source:
            counts[source] += 1
    return [name for name, _count in counts.most_common(limit)]


def _trade_context_lines(sections: dict[str, Any], *, limit: int = 4) -> list[str]:
    rows = [row for row in (sections.get("trade_series") or []) if isinstance(row, dict)]
    lines: list[str] = []
    for row in rows[: max(1, int(limit or 4))]:
        latest = row.get("latest_point") if isinstance(row.get("latest_point"), dict) else {}
        value = latest.get("value")
        unit = row.get("unit") or ""
        deltas: list[str] = []
        if row.get("delta_pct") is not None:
            deltas.append(f"MoM {float(row.get('delta_pct')) * 100:+.1f}%")
        if row.get("yoy_delta_pct") is not None:
            deltas.append(f"YoY {float(row.get('yoy_delta_pct')) * 100:+.1f}%")
        delta_txt = f" ({'; '.join(deltas)})" if deltas else ""
        zero_txt = " verify zero-value coverage" if value in (0, 0.0) else ""
        lines.append(
            f"{row.get('name')} {value} {unit}{delta_txt} | {latest.get('period_date')}{zero_txt}".strip()
        )
    return lines


def _compression_fallback_analysis(
    *,
    bundle: dict[str, Any],
    lookback_hours: int,
    focus_query: str | None,
) -> tuple[str, list[str], list[str], list[str], list[str], list[str]]:
    counts = bundle.get("counts") if isinstance(bundle.get("counts"), dict) else {}
    sections = bundle.get("sections") if isinstance(bundle.get("sections"), dict) else {}
    doc_count = int(counts.get("documents") or 0)
    note_count = int(counts.get("notes") or 0)
    discord_count = int(counts.get("discord_sources") or 0)
    thesis_count = int(counts.get("theses") or 0)
    action_count = int(counts.get("agent_actions") or 0)
    action_rows = [row for row in (sections.get("agent_actions") or []) if isinstance(row, dict)]
    research_rows = [row for row in (sections.get("research_library") or []) if isinstance(row, dict)]
    stale_reports = [str(row.get("title") or "").strip() for row in research_rows if row.get("stale") and str(row.get("title") or "").strip()]
    report_refresh_actions = [
        str(row.get("title") or "").strip()
        for row in action_rows
        if str(row.get("kind") or "").strip() == "research_report_refresh" and str(row.get("title") or "").strip()
    ]

    signals = _collect_context_signals(bundle, limit=10)
    topics = _extract_context_topics(signals, limit=4)
    top_sources = _top_document_sources(bundle, limit=3)
    trade_lines = _trade_context_lines(sections, limit=4)
    bucket_counts = Counter(signal.get("bucket") or "secondary" for signal in signals)
    primary_count = int(bucket_counts.get("primary") or 0)
    secondary_count = int(bucket_counts.get("secondary") or 0)
    social_count = int(bucket_counts.get("social") or 0)

    lead_titles = [_compact_signal_label(signal) for signal in signals[:3] if _compact_signal_label(signal)]
    summary_parts: list[str] = []
    social_only_window = social_count > 0 and primary_count == 0 and secondary_count == 0
    if social_only_window:
        topic_txt = _human_join(topics[:4]) if topics else "recent watchlist topics"
        summary_parts.append(
            f"Low durable signal over the last {int(lookback_hours)} hours: the visible flow was mostly social or analyst commentary around {topic_txt}, with no higher-precedence confirmation in the context bundle."
        )
    elif lead_titles:
        if len(lead_titles) == 1:
            summary_parts.append(f"The highest-signal item was {lead_titles[0]}.")
        else:
            head = "; ".join(lead_titles[:2])
            if len(lead_titles) > 2:
                head = head + f"; and {lead_titles[2]}"
            summary_parts.append("The highest-signal items were " + head + ".")
    if topics and not social_only_window:
        summary_parts.append("Recurring themes were " + _human_join(topics[:4]) + ".")
    if stale_reports or report_refresh_actions:
        names = stale_reports or report_refresh_actions
        summary_parts.append(f"Report-memory review flagged {len(names)} compiled report(s) for refresh or addendum work, led by {names[0]}.")
    if trade_lines:
        summary_parts.append("Structured trade context is available; any trade-led claim should be validated with MoM/YoY and mirror-flow checks before thesis changes.")
    if social_count > primary_count and not social_only_window:
        summary_parts.append("The evidence mix is commentary-heavy, so the right output is entity checks and research requests rather than broad thesis promotion.")
    elif primary_count:
        summary_parts.append("Some higher-precedence evidence is present, so the strongest items can be checked directly against active theses, reports, and supply-chain nodes.")
    elif doc_count or note_count or discord_count:
        summary_parts.append("The window was active, but the object-changing evidence remains thin.")
    if focus_query:
        summary_parts.append(f"Focus remained on {focus_query}.")
    if not summary_parts:
        summary_parts.append(f"Low durable signal over the last {int(lookback_hours)} hours; no object-changing evidence stood out.")
    summary = " ".join(summary_parts)

    bullets: list[str] = []
    for signal in signals[:4]:
        title = _compact_signal_label(signal)
        if not title:
            continue
        bucket = str(signal.get("bucket") or "secondary")
        if bucket == "social":
            bullets.append(f"{title} -> keep as a lead; resolve entity/object impact before promotion.")
        elif bucket == "primary":
            bullets.append(f"{title} -> primary-style evidence; check active theses, reports, and supply-chain nodes.")
        else:
            bullets.append(f"{title} -> secondary evidence; convert into a targeted check if it changes assumptions.")
        if len(bullets) >= 4:
            break
    if primary_count or secondary_count or social_count:
        bullets.append(f"Signal mix: primary {primary_count}, secondary {secondary_count}, social {social_count}.")
    if thesis_count == 0 and doc_count:
        bullets.append("This window was evidence intake heavy; no theses were updated inside the same lookback window.")
    if action_count:
        bullets.append(f"Open action flow includes {action_count} item(s); prioritize concrete object checks over generic summary work.")
    for title in stale_reports[:2]:
        bullets.append(f"Report review due: {title}.")
    if top_sources:
        bullets.append("Most visible source flow came from " + ", ".join(top_sources[:3]) + ".")
    for line in trade_lines[:3]:
        bullets.append("Trade check: " + line)

    actions: list[str] = []
    if signals:
        actions.append("Resolve named entities and inspect existing theses, reports, datasets, and supply-chain nodes before writing.")
    if trade_lines:
        actions.append("For trade-led claims, compare reporter and partner mirror flows plus MoM/YoY before creating a thesis-changing note.")
    if stale_reports or report_refresh_actions:
        actions.append("Refresh due compiled reports by adding evidence addenda first; request a full rewrite only if the core thesis changed.")
    if social_count:
        actions.append("Turn high-signal social or analyst claims into entity checks or research requests instead of copying the feed into the digest.")
    if doc_count and note_count:
        actions.append("Promote only the strongest Discord or X items into durable notes or actions after corroboration, rather than mirroring the whole feed.")
    if not actions:
        actions.append("No durable mutation is implied; keep the run as a low-signal status check.")
    run_errors = [
        row for row in (sections.get("ingestion_runs") or [])
        if isinstance(row, dict) and str(row.get("error") or "").strip()
    ]

    risks: list[str] = []
    combined_titles = " ".join(signal.get("title") or "" for signal in signals).lower()
    if social_count > primary_count:
        risks.append("Commentary-heavy flow can create false thesis pressure; promote only after object and source checks.")
    if not primary_count and doc_count:
        risks.append("Little or no primary-source confirmation landed in this window.")
    if any(_is_translation_heavy_title(signal.get("title")) for signal in signals):
        risks.append("Cross-language items need corroboration before they are used as thesis-changing evidence.")
    if stale_reports:
        risks.append("Some compiled reports are stale; agents should not treat them as current without addenda or refresh checks.")
    risk_markers = [
        ("strike", "Labor disruption headlines may matter for output or pricing, but they still need corroboration and scope checking."),
        ("export", "Export-control or licensing headlines can change demand assumptions quickly, but rumor-heavy flow needs confirmation."),
        ("hormuz", "Middle East shipping or energy headlines can bleed into chemicals, logistics, and freight assumptions."),
        ("force majeure", "Force-majeure language is high-signal for supply-chain stress and should be cross-checked quickly."),
    ]
    for marker, message in risk_markers:
        if marker in combined_titles and message not in risks:
            risks.append(message)

    coverage_gaps: list[str] = []
    if run_errors:
        blocked_sources = [str(row.get("source") or "").strip() for row in run_errors if str(row.get("source") or "").strip()]
        if blocked_sources:
            coverage_gaps.append("Blocked or failing monitored sources: " + ", ".join(blocked_sources[:3]) + ".")
    if not primary_count and doc_count:
        coverage_gaps.append("Recent window lacked much issuer-confirmed or clearly corroborated coverage.")

    evidence_lines: list[str] = []
    for signal in signals[:8]:
        source = signal.get("source") or signal.get("kind") or "signal"
        title = signal.get("title") or ""
        timestamp = signal.get("timestamp") or ""
        url = signal.get("url") or ""
        line = f"[{source}] {title}"
        if timestamp:
            line += f" | {timestamp[:19]}"
        if url:
            line += f" | {url}"
        evidence_lines.append(line)

    return summary, bullets[:8], actions[:8], risks[:8], evidence_lines[:8], coverage_gaps[:6]


def _fallback_synthesis_from_bundle(
    *,
    bundle: dict[str, Any],
    lookback_hours: int,
    focus_query: str | None,
) -> SynthesisResult:
    summary, bullets, actions, risks, evidence_lines, coverage_gaps = _compression_fallback_analysis(
        bundle=bundle,
        lookback_hours=lookback_hours,
        focus_query=focus_query,
    )
    evidence: list[dict[str, Any]] = []
    for signal in _collect_context_signals(bundle, limit=5):
        title = str(signal.get("title") or "").strip()
        if not title:
            continue
        evidence.append(
            {
                "title": title,
                "source": str(signal.get("source") or signal.get("kind") or "").strip(),
                "url": str(signal.get("url") or "").strip(),
            }
        )
    if not evidence:
        evidence = [{"title": line} for line in evidence_lines[:5] if str(line).strip()]
    return SynthesisResult(
        summary=summary,
        key_points=bullets,
        actions=actions,
        risks=risks,
        evidence=evidence,
        proposed_mutations=[],
        coverage_gaps=coverage_gaps,
    )


def _compression_note_markdown(
    *,
    title: str,
    mode: str,
    lookback_hours: int,
    focus_query: str | None,
    summary: str,
    bullets: list[str],
    actions: list[str],
    risks: list[str],
    surface_lines: list[str],
    evidence_lines: list[str],
    coverage_gaps: list[str] | None = None,
    appendix_md: str | None = None,
) -> str:
    md: list[str] = [f"# {title}", f"_Mode: {mode} • Lookback: {int(lookback_hours)}h_", ""]
    if focus_query and str(focus_query).strip():
        md.append(f"**Focus:** {str(focus_query).strip()}")
        md.append("")
    if summary:
        md.append("## Summary")
        md.append(summary.strip())
        md.append("")
    if bullets:
        md.append("## Key Points")
        md.extend([f"- {b}" for b in bullets[:8]])
        md.append("")
    if actions:
        md.append("## Actions")
        md.extend([f"- {a}" for a in actions[:8]])
        md.append("")
    if risks:
        md.append("## Risks")
        md.extend([f"- {r}" for r in risks[:8]])
        md.append("")
    if surface_lines:
        md.append("## Structured Surfaces")
        md.extend([f"- {line}" for line in surface_lines[:8]])
        md.append("")
    if coverage_gaps:
        md.append("## Coverage Gaps")
        md.extend([f"- {gap}" for gap in coverage_gaps[:8]])
        md.append("")
    if evidence_lines:
        md.append("## Evidence Trail")
        md.extend([f"- {line}" for line in evidence_lines[:12]])
        md.append("")
    if appendix_md and str(appendix_md).strip():
        md.append("## Context Snapshot")
        md.append(str(appendix_md).strip())
        md.append("")
    return "\n".join(md).strip() + "\n"


def handle_agent_context_compression(payload: dict[str, Any]) -> None:
    guild_id = int(payload.get("guild_id") or 0)
    if not guild_id:
        return
    guild_agent_cfg = get_agent_settings(guild_id=guild_id)
    mode = str(payload.get("mode") or "custom").strip().lower() or "custom"
    lookback_h = int(
        payload.get("lookback_hours")
        or guild_agent_cfg.get("context_default_lookback_hours")
        or getattr(settings, "agent_context_default_lookback_hours", 24)
        or 24
    )
    focus_query = str(payload.get("focus_query") or "").strip() or None
    tickers_raw = payload.get("tickers")
    tickers = [str(t).strip().upper() for t in tickers_raw if str(t).strip()] if isinstance(tickers_raw, list) else []
    include_sections_raw = payload.get("include_sections")
    if isinstance(include_sections_raw, list):
        include_sections = [str(s).strip() for s in include_sections_raw if str(s).strip()] or None
    else:
        include_sections = _agent_setting_sections(guild_agent_cfg)
    post_to_outbox = bool(payload.get("post_to_outbox"))
    post_channel_id = int(payload.get("post_channel_id") or 0) or None
    post_thread_id = int(payload.get("post_thread_id") or 0) or None
    persist_note = bool(payload.get("persist_note", True))
    title_override = str(payload.get("title_override") or "").strip() or None
    client_request_id = str(payload.get("client_request_id") or "").strip() or None
    max_items_per_section = _agent_setting_int(
        guild_agent_cfg,
        "context_max_items_per_section",
        int(getattr(settings, "agent_context_max_items_per_section", 25) or 25),
        min_v=1,
        max_v=200,
    )
    max_prompt_chars = _agent_setting_int(
        guild_agent_cfg,
        "context_max_prompt_chars",
        int(getattr(settings, "agent_context_max_prompt_chars", 24000) or 24000),
        min_v=2000,
        max_v=200000,
    )

    SessionLocal = session_factory()
    with SessionLocal() as session:
        bundle = build_recent_context_bundle(
            session,
            guild_id=guild_id,
            lookback_hours=lookback_h,
            tickers=tickers or None,
            include_sections=include_sections,
            max_items_per_section=int(max_items_per_section),
            include_full_text=False,
        )
    context_md = render_recent_context_markdown(
        bundle,
        max_chars=int(max_prompt_chars),
    )

    title = _compression_title(mode, title_override=title_override)
    fallback_summary, fallback_bullets, fallback_actions, fallback_risks, fallback_evidence_lines, fallback_coverage_gaps = _compression_fallback_analysis(
        bundle=bundle,
        lookback_hours=lookback_h,
        focus_query=focus_query,
    )

    def _fallback_result() -> SynthesisResult:
        result = _fallback_synthesis_from_bundle(
            bundle=bundle,
            lookback_hours=lookback_h,
            focus_query=focus_query,
        )
        return result

    with session_scope() as audit_session:
        synthesis = run_synthesis_task(
            task_kind=TASK_CONTEXT_COMPRESSION,
            prompt=_compression_prompt(mode=mode, focus_query=focus_query, context_md=context_md),
            fallback_factory=_fallback_result,
            requested_engine=str(payload.get("engine") or "").strip() or None,
            audit_session=audit_session,
        )
    summary = synthesis.summary or fallback_summary
    bullets = synthesis.key_points or fallback_bullets
    actions = synthesis.actions or fallback_actions
    risks = synthesis.risks or fallback_risks
    coverage_gaps = synthesis.coverage_gaps or fallback_coverage_gaps
    evidence_lines: list[str] = []
    for item in synthesis.evidence or []:
        if not isinstance(item, dict):
            continue
        title_line = str(item.get("title") or "").strip()
        if not title_line:
            continue
        source = str(item.get("source") or "").strip()
        prefix = f"[{source}] " if source else ""
        tail = str(item.get("url") or "").strip()
        evidence_lines.append(prefix + title_line + (f" | {tail}" if tail else ""))
    if not evidence_lines:
        evidence_lines = fallback_evidence_lines
    llm_used = bool((synthesis.engine_metadata or {}).get("llm_used"))
    llm_error = str((synthesis.engine_metadata or {}).get("fallback_reason") or "").strip() or None
    surface_lines = _structured_surface_lines(bundle)

    appendix_md = context_md if mode in {"custom", "topic"} else None

    body_md = _compression_note_markdown(
        title=title,
        mode=mode,
        lookback_hours=lookback_h,
        focus_query=focus_query,
        summary=summary,
        bullets=bullets,
        actions=actions,
        risks=risks,
        surface_lines=surface_lines,
        evidence_lines=evidence_lines,
        coverage_gaps=coverage_gaps,
        appendix_md=appendix_md,
    )

    note_id: int | None = None
    if persist_note:
        stamp = _now().strftime("%Y%m%d%H%M%S")
        cid_focus = (focus_query or "").strip().replace(" ", "-")[:24] or "none"
        client_id = client_request_id or f"discord_agent_context:{guild_id}:{mode}:{cid_focus}:{stamp}"
        extra = {
            "kind": "agent_context_compression",
            "guild_id": int(guild_id),
            "mode": mode,
            "lookback_hours": int(lookback_h),
            "focus_query": focus_query,
            "tickers": tickers,
            "llm_used": bool(llm_used),
            "llm_error": llm_error,
            "engine_metadata": synthesis.engine_metadata,
            "counts": bundle.get("counts") if isinstance(bundle.get("counts"), dict) else {},
        }
        with session_scope() as session:
            note = notes_bridge.upsert_discord_note(
                session,
                client_request_id=client_id,
                title=title,
                body_md=body_md,
                entity_tokens=tickers or None,
                extra_discord=extra,
            )
            note_id = int(note.id)
            _publish_agent_note_document(
                session,
                external_id=client_id,
                title=title,
                body_md=body_md,
                note_id=note_id,
                published_at=_now(),
                kind="agent_context_compression",
                extra={
                    "guild_id": int(guild_id),
                    "mode": mode,
                    "lookback_hours": int(lookback_h),
                    "focus_query": focus_query,
                    "tickers": tickers,
                    "llm_used": bool(llm_used),
                    "llm_error": llm_error,
                    "engine_metadata": synthesis.engine_metadata,
                    "counts": bundle.get("counts") if isinstance(bundle.get("counts"), dict) else {},
                },
            )
            _persist_follow_up_actions(
                session,
                guild_id=guild_id,
                note_id=note_id,
                source_kind="agent_context_compression",
                source_title=title,
                actions=actions,
                priority=4,
            )
            _persist_instrument_requests(
                session,
                guild_id=guild_id,
                note_id=note_id,
                source_kind="agent_context_compression",
                source_title=title,
                summary=summary,
                actions=actions,
                coverage_gaps=coverage_gaps,
                priority=4,
            )

    if post_to_outbox and post_channel_id:
        link = notes_bridge.note_url(note_id) if note_id else None
        desc_lines = []
        if summary:
            desc_lines.append(summary.strip())
        if bullets:
            desc_lines.extend([f"• {b}" for b in bullets[:5]])
        thread_lines = [f"**{title}**", f"Mode: {mode} • Lookback: {int(lookback_h)}h" + (f" • Focus: {focus_query}" if focus_query else "")]
        if summary:
            thread_lines.extend([line for line in str(summary).strip().splitlines() if line.strip()])
        if bullets:
            thread_lines.append("")
            thread_lines.append("Key points:")
            thread_lines.extend([f"• {b}" for b in bullets[:5]])
        if coverage_gaps:
            thread_lines.append("")
            thread_lines.append("Coverage gaps:")
            thread_lines.extend([f"• {gap}" for gap in coverage_gaps[:3]])
        if link:
            thread_lines.append("")
            thread_lines.append(f"Thoth: {link}")
        embed = {
            "title": title[:256],
            "description": ("\n".join(desc_lines) if desc_lines else "Compression generated.")[:4000],
            "color": 0xF59E0B,
            "fields": [],
            "footer": {"text": f"Mode: {mode}"},
        }
        embed["fields"].append(
            {
                "name": "Coverage",
                "value": (f"Lookback: {int(lookback_h)}h" + (f" • Focus: {focus_query}" if focus_query else ""))[:1024],
                "inline": False,
            }
        )
        if link:
            embed["fields"].append({"name": "Thoth", "value": link[:1024], "inline": False})
        if risks:
            embed["fields"].append({"name": "Watchouts", "value": "\n".join([f"• {r}" for r in risks[:3]])[:1024], "inline": False})
        elif coverage_gaps:
            embed["fields"].append({"name": "Coverage Gaps", "value": "\n".join([f"• {gap}" for gap in coverage_gaps[:3]])[:1024], "inline": False})
        if surface_lines:
            embed["fields"].append({"name": "Open Surfaces", "value": "\n".join([f"• {line}" for line in surface_lines[:3]])[:1024], "inline": False})
        _enqueue_thread_summary_post(
            guild_id=guild_id,
            post_channel_id=int(post_channel_id),
            post_thread_id=int(post_thread_id) if post_thread_id else None,
            thread_lines=thread_lines,
            embed=embed,
            priority=5,
            dedupe_key_base=None,
            announce_in_main=bool(post_thread_id),
            notice_lines=[
                f"{title} is live in <#{int(post_thread_id)}>." if post_thread_id else f"{title} is live.",
                f"Mode: {mode} • Lookback: {int(lookback_h)}h" + (f" • Focus: {focus_query}" if focus_query else ""),
                f"Thoth: {link}" if link else "",
            ],
        )
