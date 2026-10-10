from __future__ import annotations

import hashlib
import json
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from thoth_terminal.agent.context_bundle import build_recent_context_bundle, render_recent_context_markdown
from thoth_terminal.cloud_goal_run_service import estimate_tokens
from thoth_terminal.config import settings
from thoth_terminal.db import utcnow
from thoth_terminal.local_agent_service import daily_summary_context
from thoth_terminal.models import (
    Attachment, CloudGoalEvent, CloudGoalRun, CloudPromptStateSnapshot,
    Document, DocumentEntity, Entity, Source,
)


CLOUD_PROMPT_VOLATILE_COMPILER_VERSION = "cloud-prompt-volatile-v2"
CLOUD_PROMPT_VOLATILE_START_MARKER = "<thoth_volatile_state>"
CLOUD_PROMPT_VOLATILE_END_MARKER = "</thoth_volatile_state>"
CLOUD_PROMPT_VOLATILE_AGENT_KEY = "cloud-research-shared-volatile-state"

logger = logging.getLogger(__name__)

_VOLATILE_SECTIONS = [
    "documents",
    "notes",
    "datasets",
    "charts",
    "supply_chains",
    "instrument_requests",
    "product_stages",
    "transcripts",
    "theses",
    "research_library",
    "agent_actions",
    "change_runs",
    "thesis_proposals",
    "document_events",
    "ingestion_runs",
    "backlog_stats",
]


def _text(value: object) -> str:
    return str(value or "").strip()


def _short(value: object, limit: int) -> str:
    text = _text(value)
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _packed(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _arrival_preview(value: object, limit: int) -> str:
    return _short(" ".join(_text(value).split()), limit)


def list_document_arrival_cards(
    session: Session,
    *,
    since: datetime,
    until: datetime,
    offset: int = 0,
    limit: int = 400,
    max_chars: int = 200_000,
    exclude_document_ids: list[int] | None = None,
    query: str = "",
    status_only: bool = False,
) -> dict[str, Any]:
    """Search and page a frozen arrival window without judging its claims."""
    offset = max(0, int(offset))
    limit = max(1, min(int(limit), 400))
    excluded = {int(value) for value in (exclude_document_ids or []) if str(value).isdigit()}
    if len(excluded) > 5000:
        raise ValueError("Too many excluded document ids")
    query = " ".join(str(query or "").split())
    if len(query) > 120:
        raise ValueError("Arrival query must be at most 120 characters")
    text_expr = func.coalesce(func.nullif(Document.translation_en, ""), Document.normalized_text)
    base = (
        session.query(Document)
        .filter(Document.ingested_at >= since, Document.ingested_at <= until)
        .filter(~Document.kind.in_(["operator_feedback", "operator_question", "operator_response"]))
    )
    window_total = base.count()
    if excluded:
        base = base.filter(~Document.id.in_(excluded))
    remaining_total = base.count() if excluded else window_total
    if query:
        term = f"%{query}%"
        base = base.join(Source, Source.id == Document.source_id).filter(or_(
            Document.title.ilike(term), Document.author.ilike(term),
            Source.name.ilike(term), func.substr(text_expr, 1, 640).ilike(term),
        ))
    total = base.count() if query else remaining_total
    if status_only:
        return {
            "window_total": window_total,
            "excluded_count": window_total - remaining_total,
            "remaining_total": remaining_total,
            "total": total,
            "listed": 0,
            "omitted": total,
            "next_offset": 0 if total else None,
            "cards": [],
            "preview_chars": 0,
            "query_scope": "title, author, source, leading 640 text characters" if query else None,
        }
    rows = (
        (base if query else base.join(Source, Source.id == Document.source_id)).with_entities(
            Document.id, Document.kind, Document.title, Document.author,
            Document.published_at, Document.ingested_at, Document.language,
            Document.extra, Source.name.label("source_name"),
            func.substr(text_expr, 1, 640).label("preview_text"),
            func.length(text_expr).label("text_chars"),
            func.length(func.coalesce(Document.translation_en, "")).label("translation_chars"),
        )
        .order_by(Document.ingested_at.desc(), Document.id.desc())
        .offset(offset).limit(limit).all()
    )
    ids = [int(row.id) for row in rows]
    image_counts: dict[int, int] = {}
    entity_hints: dict[int, list[str]] = {}
    if ids:
        for document_id, count in (
            session.query(Attachment.document_id, func.count(Attachment.id))
            .filter(Attachment.document_id.in_(ids), Attachment.kind == "image")
            .group_by(Attachment.document_id).all()
        ):
            image_counts[int(document_id)] = int(count)
        for document_id, name in (
            session.query(DocumentEntity.document_id, Entity.canonical_name)
            .join(Entity, Entity.id == DocumentEntity.entity_id)
            .filter(DocumentEntity.document_id.in_(ids))
            .order_by(DocumentEntity.document_id, Entity.canonical_name).all()
        ):
            hints = entity_hints.setdefault(int(document_id), [])
            if len(hints) < 4:
                hints.append(_short(name, 48))
    cards: list[dict[str, Any]] = []
    char_count = 0
    for row in rows:
        extra = row.extra if isinstance(row.extra, dict) else {}
        report_extraction = extra.get("report_extraction") if isinstance(extra.get("report_extraction"), dict) else {}
        extraction_status = _text(extra.get("extraction_status") or report_extraction.get("status"))
        manifest = extra.get("image_manifest") if isinstance(extra.get("image_manifest"), list) else []
        image_context = []
        for item in manifest:
            if not isinstance(item, dict):
                continue
            hint = _arrival_preview(item.get("caption") or item.get("alt") or item.get("heading"), 110)
            if hint:
                image_context.append(hint)
            if len(image_context) >= 2:
                break
        preview = _arrival_preview(row.preview_text, 600)
        title = _arrival_preview(row.title, 180)
        pdf_title = title
        if preview and title and (preview.casefold().startswith(title.casefold()) or title.casefold().startswith(preview.casefold())):
            title = ""
        text_chars = int(row.text_chars or 0)
        card = {
            "id": int(row.id),
            "source": _short(row.source_name, 48),
            "author": _short(row.author, 80) or None,
            "published_at": row.published_at.isoformat() if row.published_at else None,
            "ingested_at": row.ingested_at.isoformat(),
            "kind": _short(row.kind, 32),
            "title": title or None,
            "text": preview or None,
            "text_scope": "full" if text_chars <= 600 else "leading_excerpt",
            "text_chars": text_chars,
            "language": _short(row.language, 16) or None,
            "translated": bool(row.translation_chars),
            "extraction_status": extraction_status or None,
            "image_count": image_counts.get(int(row.id), 0),
            "image_context": image_context,
            "entity_hints": entity_hints.get(int(row.id), []),
        }
        pdf_structure = extra.get("pdf_structure") if isinstance(extra.get("pdf_structure"), dict) else {}
        if row.kind == "pdf" or extra.get("content_kind") == "pdf":
            # Attached raster images are not the PDF's pages. Expose the page
            # index so the agent knows it can inspect original slide pixels.
            card["title"] = pdf_title or None
            card["pdf_pages_indexed"] = int(pdf_structure.get("page_count") or 0)
            card["page_images_available"] = bool(pdf_structure.get("page_count"))
        encoded_len = len(_packed(card))
        if cards and char_count + encoded_len > max_chars:
            break
        cards.append(card)
        char_count += encoded_len
    next_offset = offset + len(cards)
    return {
        "window_total": window_total,
        "excluded_count": window_total - remaining_total,
        "remaining_total": remaining_total,
        "total": total,
        "listed": len(cards),
        "omitted": max(0, total - next_offset),
        "next_offset": next_offset if next_offset < total else None,
        "cards": cards,
        "preview_chars": char_count,
        "query_scope": "title, author, source, leading 640 text characters" if query else None,
    }


def pending_document_arrival_leads(session: Session, *, limit: int = 50) -> list[dict[str, Any]]:
    """Carry the latest unresolved selection disposition across scheduled runs."""
    events = (
        session.query(CloudGoalEvent)
        .join(CloudGoalRun, CloudGoalRun.id == CloudGoalEvent.run_id)
        .filter(
            CloudGoalEvent.event_kind == "arrival_selection_audit",
            CloudGoalRun.task_kind == "cloud_research_heartbeat",
        )
        .order_by(CloudGoalEvent.id.desc()).limit(100).all()
    )
    latest: dict[int, tuple[str, str]] = {}
    for event in events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        for value in payload.get("opened_document_ids") or []:
            if str(value).isdigit():
                latest.setdefault(int(value), ("opened", ""))
        for item in payload.get("deferred") or []:
            if isinstance(item, dict) and str(item.get("document_id")).isdigit():
                latest.setdefault(int(item["document_id"]), ("deferred", _short(item.get("reason"), 180)))
        for value in payload.get("unreadable_document_ids") or []:
            if str(value).isdigit():
                latest.setdefault(int(value), ("unreadable", "Original could not be read"))
    unresolved = [(document_id, state, reason) for document_id, (state, reason) in latest.items() if state != "opened"][:limit]
    if not unresolved:
        return []
    documents = {
        int(row.id): row for row in (
            session.query(Document.id, Document.title, Source.name.label("source_name"))
            .join(Source, Source.id == Document.source_id)
            .filter(Document.id.in_([item[0] for item in unresolved])).all()
        )
    }
    return [
        {"id": document_id, "source": _short(documents[document_id].source_name, 48),
         "title": _arrival_preview(documents[document_id].title, 140),
         "status": state, "reason": reason}
        for document_id, state, reason in unresolved if document_id in documents
    ]


def _epoch_bounds(now: datetime, hours: int) -> tuple[datetime, datetime]:
    bounded = max(1, min(int(hours or 24), 168))
    aware = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
    seconds = bounded * 3600
    start_seconds = int(aware.timestamp()) // seconds * seconds
    start = datetime.fromtimestamp(start_seconds, tz=timezone.utc).replace(tzinfo=None)
    return start, start + timedelta(hours=bounded)


def _recent_bundle(session: Session, *, lookback_hours: int, max_items: int) -> dict[str, Any]:
    bundle = build_recent_context_bundle(
        session,
        lookback_hours=max(1, int(lookback_hours)),
        include_sections=_VOLATILE_SECTIONS,
        max_items_per_section=max(1, int(max_items)),
        max_items_per_source=10,
        include_full_text=False,
    )
    # generated_at is transport/debug metadata. It is intentionally retained in
    # the database manifest, never in the model-visible prefix.
    bundle.pop("generated_at", None)
    return bundle


def _render_recent_bundle(bundle: dict[str, Any]) -> str:
    rendered = render_recent_context_markdown({"generated_at": "", **bundle}, max_chars=None)
    return "\n".join(line for line in rendered.splitlines() if not line.startswith("Generated:"))


def _without_runtime_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(payload or {})
    for key in (
        "generated_at",
        "lookback_hours",
        "source_document_ids",
        "compression_ids",
    ):
        cleaned.pop(key, None)
    return cleaned


def _prior_dispatches(session: Session, *, limit: int) -> list[dict[str, Any]]:
    rows = (
        session.query(CloudGoalRun)
        .filter(
            CloudGoalRun.status == "complete",
            CloudGoalRun.task_kind.in_([
                "cloud_research_heartbeat",
                "cloud_research_nightly",
                "cloud_research_inquisitive",
            ]),
        )
        .order_by(CloudGoalRun.completed_at.desc(), CloudGoalRun.id.desc())
        .limit(max(1, min(int(limit or 20), 80)))
        .all()
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        result = dict(row.final_result or {})
        out.append(
            {
                "title": _short(result.get("title"), 240),
                "summary": _short(result.get("summary"), 900),
                "key_points": [_short(item, 320) for item in list(result.get("key_points") or [])[:6]],
            }
        )
    return out


def _compiler_options() -> dict[str, int]:
    return {
        "lookback_hours": max(1, min(int(getattr(settings, "cloud_research_volatile_state_lookback_hours", 168) or 168), 720)),
        "max_items": max(10, min(int(getattr(settings, "cloud_research_volatile_state_max_items", 45) or 45), 100)),
        "local_limit": max(20, min(int(getattr(settings, "cloud_research_volatile_state_local_limit", 160) or 160), 450)),
    }


def compile_cloud_prompt_volatile_state(session: Session) -> tuple[str, dict[str, Any]]:
    options = _compiler_options()
    lookback_hours = options["lookback_hours"]
    max_items = options["max_items"]
    local_limit = options["local_limit"]
    bundle = _recent_bundle(session, lookback_hours=lookback_hours, max_items=max_items)
    raw_local_context = daily_summary_context(session, hours=lookback_hours, limit=local_limit)
    local_context = _without_runtime_metadata(raw_local_context)
    payload = {
        "recent_terminal_context": _render_recent_bundle(bundle),
        "local_agent_work_and_durable_state": local_context,
        "prior_cloud_dispatches": _prior_dispatches(session, limit=20),
    }
    body = _packed(payload)
    content = "\n\n".join(
        [
            CLOUD_PROMPT_VOLATILE_START_MARKER,
            "# Shared Volatile Research State",
            "",
            "This frozen change-state block is identical across DeepSeek research workloads. Newer evidence and the task delta follow its closing marker.",
            "",
            "```json",
            body,
            "```",
            CLOUD_PROMPT_VOLATILE_END_MARKER,
        ]
    )
    return content, {
        "version": CLOUD_PROMPT_VOLATILE_COMPILER_VERSION,
        "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "chars": len(content),
        "token_estimate": estimate_tokens(content),
        "lookback_hours": lookback_hours,
        "max_items": max_items,
        "local_limit": local_limit,
        "counts": dict(bundle.get("counts") or {}),
        # Provenance stays in the persisted manifest for audits and downstream
        # summaries; it is deliberately absent from content_md.
        "source_document_ids": list(raw_local_context.get("source_document_ids") or []),
        "compression_ids": list(raw_local_context.get("compression_ids") or []),
    }


def get_or_create_cloud_prompt_volatile_snapshot(
    session: Session,
    *,
    now: datetime | None = None,
) -> tuple[CloudPromptStateSnapshot, bool]:
    captured_at = now or utcnow()
    epoch_start, epoch_end = _epoch_bounds(
        captured_at,
        int(getattr(settings, "cloud_research_volatile_state_epoch_hours", 24) or 24),
    )
    options_hash = hashlib.sha256(_packed(_compiler_options()).encode("utf-8")).hexdigest()[:12]
    snapshot_key = (
        f"{CLOUD_PROMPT_VOLATILE_AGENT_KEY}:{CLOUD_PROMPT_VOLATILE_COMPILER_VERSION}:"
        f"{options_hash}:{epoch_start.isoformat()}"
    )
    existing = session.query(CloudPromptStateSnapshot).filter_by(snapshot_key=snapshot_key).first()
    if existing is not None:
        return existing, False
    try:
        content, manifest = compile_cloud_prompt_volatile_state(session)
    except MemoryError:
        # Keep scheduled research alive if a fresh packed delta cannot be built
        # under transient memory pressure. The caller still appends a live delta
        # after this prior immutable snapshot.
        previous = (
            session.query(CloudPromptStateSnapshot)
            .filter(
                CloudPromptStateSnapshot.agent_key == CLOUD_PROMPT_VOLATILE_AGENT_KEY,
                CloudPromptStateSnapshot.compiler_version == CLOUD_PROMPT_VOLATILE_COMPILER_VERSION,
                CloudPromptStateSnapshot.status == "active",
            )
            .order_by(CloudPromptStateSnapshot.epoch_start.desc(), CloudPromptStateSnapshot.id.desc())
            .first()
        )
        if previous is None:
            raise
        logger.exception(
            "volatile prompt snapshot rebuild exhausted memory; reusing snapshot id=%s epoch_start=%s",
            previous.id,
            previous.epoch_start,
        )
        return previous, False
    row = CloudPromptStateSnapshot(
        snapshot_key=snapshot_key,
        agent_key=CLOUD_PROMPT_VOLATILE_AGENT_KEY,
        compiler_version=CLOUD_PROMPT_VOLATILE_COMPILER_VERSION,
        epoch_start=epoch_start,
        epoch_end=epoch_end,
        status="active",
        content_sha256=str(manifest["sha256"]),
        content_md=content,
        manifest_json=manifest,
        created_at=captured_at,
        updated_at=captured_at,
    )
    session.add(row)
    session.flush()
    return row, True


def render_cloud_prompt_volatile_delta(
    session: Session,
    snapshot: CloudPromptStateSnapshot,
    *,
    arrival_since: datetime | None = None,
) -> tuple[str, dict[str, Any]]:
    captured_at = utcnow()
    age_hours = max(1, min(24, int(math.ceil(max(0.0, (captured_at - snapshot.created_at).total_seconds()) / 3600.0))))
    bundle = _recent_bundle(session, lookback_hours=age_hours, max_items=20)
    local_context = _without_runtime_metadata(
        daily_summary_context(session, hours=age_hours, limit=60)
    )
    arrival_start = arrival_since if arrival_since is not None else snapshot.created_at
    arrivals = list_document_arrival_cards(
        session, since=arrival_start, until=captured_at, limit=50, max_chars=24_000,
    )
    carryover = pending_document_arrival_leads(session)
    payload = {
        "recent_terminal_changes": _render_recent_bundle(bundle),
        "recent_local_agent_changes": local_context,
        "document_arrivals": arrivals,
        "carried_deferred_leads": carryover,
    }
    body = _packed(payload)
    rendered = "\n\n".join(
        [
            "# Volatile State Delta",
            "",
            f"Source arrival window: {arrival_start.isoformat()} through {captured_at.isoformat()}. "
            f"Total {arrivals['total']}; initial preview {arrivals['listed']}; omitted {arrivals['omitted']}. "
            "Use list_document_arrival_cards to page the complete window. If this section is clipped, "
            "retrieve cards through that tool rather than treating the visible preview as complete.",
            "",
            "```json",
            body,
            "```",
        ]
    )
    return rendered, {
        "sha256": hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
        "chars": len(rendered),
        "token_estimate": estimate_tokens(rendered),
        "lookback_hours": age_hours,
        "counts": dict(bundle.get("counts") or {}),
        "arrivals_total": arrivals["total"],
        "arrivals_listed": arrivals["listed"],
        "arrivals_omitted": arrivals["omitted"],
        "arrival_document_ids": [int(card["id"]) for card in arrivals["cards"]],
        "carryover_document_ids": [int(item["id"]) for item in carryover],
        "arrival_start_at": arrival_start.isoformat(),
        "arrival_end_at": captured_at.isoformat(),
    }
