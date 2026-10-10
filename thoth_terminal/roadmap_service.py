from __future__ import annotations

import hashlib
import heapq
import json
import re
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from sqlalchemy import or_
from sqlalchemy.orm import Session, selectinload

from thoth_terminal.db import utcnow
from thoth_terminal.models import RoadmapEntry, RoadmapEntryView, RoadmapMention, RoadmapView


_ENTRY_PROJECTION_KINDS = {"entry", "projection", "view", "claim", "milestone"}
_MATERIAL_CHANGE_FIELDS = (
    "title",
    "owner",
    "statement",
    "expected_window",
    "target_date",
    "target_year",
    "target_quarter",
    "status",
    "stance",
    "confidence_label",
    "confidence",
    "model_use",
)


def _text(value: object, *, limit: int | None = None) -> str:
    out = str(value or "").strip()
    if limit is not None and len(out) > limit:
        return out[:limit].rstrip()
    return out


def _slugify(value: object, *, default: str = "item", limit: int = 180) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return (slug or default)[:limit].strip("-") or default


def _hash_short(value: object) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16]


def _list_text(values: object, *, limit: int = 24) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in values[:limit]:
        text = _text(item, limit=255)
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            out.append(text)
    return out


def _float_or_none(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except Exception:
        return None


def _int_or_none(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except Exception:
        return None


def _date_or_none(value: object) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = _text(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except Exception:
        return None


def _target_year(update: dict[str, Any]) -> int | None:
    for key in ("target_year", "year"):
        if key in update:
            return _validated_target_integer(update[key], key, maximum=9998)
    window = _text(update.get("expected_window") or update.get("date_window") or update.get("timing"))
    years = [int(value) for value in re.findall(r"\b(20\d{2}|19\d{2})\b", window)]
    return max(years) if years else None


def _quarter_from_window(window: str) -> int | None:
    matches = re.findall(r"(?:\bQ([1-4])\b|\b([1-4])Q\b)", window, flags=re.IGNORECASE)
    if not matches:
        return None
    first, second = matches[-1]
    return int(first or second)


def _target_quarter(update: dict[str, Any]) -> int | None:
    for key in ("target_quarter", "quarter"):
        if key in update:
            return _validated_target_integer(update[key], key, maximum=4)
    window = _text(update.get("expected_window") or update.get("date_window") or update.get("timing"))
    return _quarter_from_window(window)


def _validated_target_integer(value: object, name: str, *, maximum: int) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not re.fullmatch(r"\d+", str(value).strip()):
        raise ValueError(f"{name} must be an integer between 1 and {maximum}")
    number = int(str(value).strip())
    if not 1 <= number <= maximum:
        raise ValueError(f"{name} must be an integer between 1 and {maximum}")
    return number


def _validated_target_date(update: dict[str, Any]) -> date | None:
    value = update.get("target_date")
    parsed = _date_or_none(value)
    if value not in (None, "") and parsed is None:
        raise ValueError("target_date must be an ISO date (YYYY-MM-DD)")
    return parsed


def _confidence_label(update: dict[str, Any]) -> str | None:
    value = update.get("confidence_label")
    if value is None:
        value = update.get("confidence")
    if isinstance(value, (int, float)):
        return None
    text = _text(value, limit=32).lower().replace(" ", "_").replace("-", "_")
    return text or None


def _confidence_value(update: dict[str, Any]) -> float | None:
    value = update.get("confidence_value")
    if value is None and isinstance(update.get("confidence"), (int, float)):
        value = update.get("confidence")
    return _float_or_none(value)


def _entry_title(update: dict[str, Any]) -> str:
    return (
        _text(update.get("title"), limit=255)
        or _text(update.get("entry"), limit=255)
        or _text(update.get("topic"), limit=255)
        or _text(update.get("roadmap_item"), limit=255)
        or _text(update.get("claim"), limit=255)
        or "Roadmap entry"
    )


def _entry_statement(update: dict[str, Any], *, title: str) -> str:
    return (
        _text(update.get("statement"))
        or _text(update.get("claim"))
        or _text(update.get("summary"))
        or title
    )


def _entry_key(update: dict[str, Any], *, title: str, expected_window: str | None, target_year: int | None) -> str:
    explicit = _text(update.get("entry_key") or update.get("roadmap_entry_key"), limit=255)
    if explicit:
        return explicit
    subject = _text(update.get("subject") or update.get("company") or update.get("technology") or update.get("category"))
    basis = "|".join(
        part
        for part in (
            subject,
            title,
            expected_window or "",
            str(target_year or ""),
        )
        if part
    )
    slug = _slugify(basis, default="roadmap-entry", limit=190)
    return f"roadmap:{slug}:{_hash_short(basis)}"


def _mention_key(
    update: dict[str, Any],
    *,
    entry_key: str,
    cloud_run_id: int | None,
    operation_index: int | None,
    update_index: int,
) -> str:
    explicit = _text(update.get("mention_key") or update.get("roadmap_mention_key"), limit=255)
    if explicit:
        return explicit
    source_part = _text(update.get("source_url") or update.get("url") or update.get("source_document_id"))
    summary = _text(update.get("summary") or update.get("claim") or update.get("statement"))
    basis = "|".join(
        [
            entry_key,
            str(cloud_run_id or ""),
            str(operation_index if operation_index is not None else ""),
            str(update_index),
            source_part,
            summary,
            json.dumps(update, sort_keys=True, default=str, separators=(",", ":")),
        ]
    )
    return f"roadmap-mention:{_hash_short(basis)}"


def _view_specs(update: dict[str, Any]) -> list[dict[str, str | None]]:
    raw = update.get("views")
    specs: list[dict[str, str | None]] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                title = _text(item.get("title") or item.get("name") or item.get("view"), limit=255)
                category = _text(item.get("category"), limit=64) or None
                summary = _text(item.get("summary")) or None
            else:
                title = _text(item, limit=255)
                category = None
                summary = None
            if title:
                specs.append({"title": title, "category": category, "summary": summary})
    for key in ("view_tags", "view", "lens", "lenses"):
        for item in _list_text(update.get(key)):
            specs.append({"title": item, "category": None, "summary": None})
    category = _text(update.get("category"), limit=64)
    if category:
        specs.append({"title": f"{category.title()} roadmap", "category": category, "summary": None})
    deduped: list[dict[str, str | None]] = []
    seen: set[str] = set()
    for spec in specs:
        title = str(spec.get("title") or "").strip()
        key = _slugify(title, default="view")
        if title and key not in seen:
            seen.add(key)
            deduped.append(spec)
    return deduped


def _ensure_view(session: Session, spec: dict[str, str | None], *, now: datetime) -> RoadmapView:
    title = _text(spec.get("title"), limit=255) or "Roadmap"
    view_key = f"roadmap-view:{_slugify(title, default='view')}"
    row = session.query(RoadmapView).filter(RoadmapView.view_key == view_key).first()
    if row is None:
        row = RoadmapView(
            view_key=view_key,
            title=title,
            category=_text(spec.get("category"), limit=64) or None,
            summary=_text(spec.get("summary")) or None,
            payload={},
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.flush()
    else:
        changed = False
        category = _text(spec.get("category"), limit=64) or row.category
        summary = _text(spec.get("summary")) or row.summary
        if category != row.category:
            row.category = category
            changed = True
        if summary != row.summary:
            row.summary = summary
            changed = True
        if changed:
            row.updated_at = now
    return row


def _link_entry_view(session: Session, entry: RoadmapEntry, view: RoadmapView, *, now: datetime) -> bool:
    exists = (
        session.query(RoadmapEntryView)
        .filter(RoadmapEntryView.entry_id == entry.id, RoadmapEntryView.view_id == view.id)
        .first()
    )
    if exists is not None:
        return False
    session.add(
        RoadmapEntryView(
            entry_id=int(entry.id),
            view_id=int(view.id),
            relation="member",
            created_at=now,
        )
    )
    return True


def _update_entry(row: RoadmapEntry, values: dict[str, Any], *, now: datetime) -> bool:
    material_changed = False
    changed = False
    for key, value in values.items():
        if key not in {"expected_window", "target_date", "target_year", "target_quarter"} and value in (None, "", [], {}) and getattr(row, key) not in (None, "", [], {}):
            continue
        old = getattr(row, key)
        if old != value:
            setattr(row, key, value)
            changed = True
            if key in _MATERIAL_CHANGE_FIELDS:
                material_changed = True
    if changed:
        row.updated_at = now
    if material_changed:
        row.last_material_change_at = now
    return material_changed


def upsert_roadmap_update(
    session: Session,
    *,
    payload: dict[str, Any],
    actor: str = "cloud_heartbeat",
    cloud_run_id: int | None = None,
) -> dict[str, Any]:
    """Apply one roadmap_update durable operation.

    The public operation can contain either a single entry-like payload or an
    ``updates``/``entries`` list. Every update resolves to an atomic
    RoadmapEntry and a RoadmapMention attached to that entry.
    """

    now = utcnow()
    updates_raw = payload.get("updates")
    if updates_raw is None:
        updates_raw = payload.get("entries")
    updates = [item for item in updates_raw if isinstance(item, dict)] if isinstance(updates_raw, list) else [payload]

    applied: list[dict[str, Any]] = []
    metadata = dict(payload.get("payload") or {}) if isinstance(payload.get("payload"), dict) else {}
    operation_index = _int_or_none(metadata.get("cloud_operation_index"))
    run_id = cloud_run_id or _int_or_none(metadata.get("cloud_goal_run_id"))

    for update_index, raw_update in enumerate(updates[:40]):
        update = dict(raw_update)
        for inherited_key in ("category", "views", "view_tags", "source_document_id", "source_url"):
            if inherited_key not in update and inherited_key in payload:
                update[inherited_key] = payload[inherited_key]

        title = _entry_title(update)
        expected_window = _text(
            update.get("expected_window") or update.get("date_window") or update.get("timing"),
            limit=128,
        ) or None
        target_year = _target_year(update)
        entry_key = _entry_key(update, title=title, expected_window=expected_window, target_year=target_year)
        statement = _entry_statement(update, title=title)
        source_document_id = _int_or_none(update.get("source_document_id") or update.get("document_id"))
        source_url = _text(update.get("source_url") or update.get("url"), limit=2048) or None
        confidence_label = _confidence_label(update)
        confidence = _confidence_value(update)

        mention_key = _mention_key(
            update, entry_key=entry_key, cloud_run_id=run_id,
            operation_index=operation_index, update_index=update_index,
        )
        existing_mention = session.query(RoadmapMention).filter(RoadmapMention.mention_key == mention_key).first()
        if existing_mention is not None:
            existing_entry = session.get(RoadmapEntry, existing_mention.entry_id)
            applied.append({
                "entry_id": int(existing_entry.id), "entry_key": existing_entry.entry_key,
                "mention_id": int(existing_mention.id), "mention_key": existing_mention.mention_key,
                "created": False, "mention_created": False, "material_changed": False,
                "mention_kind": existing_mention.mention_kind,
                "view_ids": [int(link.view_id) for link in existing_entry.view_links],
            })
            continue
        target_date = _validated_target_date(update)
        target_quarter = _target_quarter(update)
        if target_date is not None:
            date_quarter = (target_date.month - 1) // 3 + 1
            if any(key in update for key in ("target_year", "year")) and target_year not in (None, target_date.year):
                raise ValueError("target_year must agree with target_date")
            if any(key in update for key in ("target_quarter", "quarter")) and target_quarter not in (None, date_quarter):
                raise ValueError("target_quarter must agree with target_date")
            target_year = target_date.year
            target_quarter = date_quarter
        revision_kind = _text(update.get("revision_kind")).lower()
        if revision_kind and revision_kind not in {"initial", "delayed", "moved", "revised"}:
            raise ValueError("revision_kind must be initial, delayed, moved, or revised")

        row = session.query(RoadmapEntry).filter(RoadmapEntry.entry_key == entry_key).first()
        created = row is None
        if row is None:
            row = RoadmapEntry(
                entry_key=entry_key,
                title=title,
                statement=statement,
                category=_text(update.get("category"), limit=64) or None,
                subject=_text(update.get("subject") or update.get("company") or update.get("technology"), limit=255) or None,
                owner=_text(update.get("owner") or update.get("view_owner") or update.get("source_owner"), limit=255) or None,
                source_document_id=source_document_id,
                source_url=source_url,
                expected_window=expected_window,
                target_date=target_date,
                target_year=target_year,
                target_quarter=target_quarter,
                status=_text(update.get("status"), limit=32) or "active",
                stance=_text(update.get("stance"), limit=64) or None,
                confidence_label=confidence_label,
                confidence=confidence,
                model_use=_text(update.get("model_use"), limit=32) or None,
                materiality=_text(update.get("materiality"), limit=32) or None,
                applies_to=_list_text(update.get("applies_to")),
                affected_models=_list_text(update.get("affected_models") or update.get("model_links")),
                beneficiaries=_list_text(update.get("beneficiaries")),
                dependencies=_list_text(update.get("dependencies")),
                mention_count=0,
                reiteration_count=0,
                last_seen_at=now,
                last_material_change_at=now,
                payload={k: v for k, v in update.items() if k not in {"updates", "entries"}},
                created_by=actor,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            material_changed = True
        else:
            timing_replaced = expected_window is not None and expected_window != row.expected_window
            year_replaced = any(key in update for key in ("target_year", "year")) and target_year != row.target_year
            quarter_replaced = any(key in update for key in ("target_quarter", "quarter")) and target_quarter != row.target_quarter
            date_replaced = "target_date" in update and target_date != row.target_date
            typed_timing_replaced = year_replaced or quarter_replaced or date_replaced
            values = {
                "title": title if any(key in update for key in ("title", "entry", "topic", "roadmap_item", "claim")) else row.title,
                "statement": statement if any(key in update for key in ("statement", "claim")) else row.statement,
                "category": _text(update.get("category"), limit=64) or row.category,
                "subject": _text(update.get("subject") or update.get("company") or update.get("technology"), limit=255) or row.subject,
                "owner": _text(update.get("owner") or update.get("view_owner") or update.get("source_owner"), limit=255) or row.owner,
                "source_document_id": source_document_id or row.source_document_id,
                "source_url": source_url or row.source_url,
                "expected_window": expected_window if expected_window is not None or typed_timing_replaced else row.expected_window,
                "target_date": target_date if "target_date" in update or timing_replaced or year_replaced or quarter_replaced else row.target_date,
                "target_year": target_year if target_date is not None or any(key in update for key in ("target_year", "year")) or timing_replaced else (target_year or row.target_year),
                "target_quarter": target_quarter if target_date is not None or any(key in update for key in ("target_quarter", "quarter")) or timing_replaced or year_replaced else (target_quarter or row.target_quarter),
                "status": _text(update.get("status"), limit=32) or row.status,
                "stance": _text(update.get("stance"), limit=64) or row.stance,
                "confidence_label": confidence_label or row.confidence_label,
                "confidence": confidence if confidence is not None else row.confidence,
                "model_use": _text(update.get("model_use"), limit=32) or row.model_use,
                "materiality": _text(update.get("materiality"), limit=32) or row.materiality,
                "applies_to": _list_text(update.get("applies_to")) or row.applies_to,
                "affected_models": _list_text(update.get("affected_models") or update.get("model_links")) or row.affected_models,
                "beneficiaries": _list_text(update.get("beneficiaries")) or row.beneficiaries,
                "dependencies": _list_text(update.get("dependencies")) or row.dependencies,
            }
            material_changed = _update_entry(row, values, now=now)
            if "resolution_criteria" in update and (row.payload or {}).get("resolution_criteria") != update["resolution_criteria"]:
                row.payload = {**(row.payload or {}), "resolution_criteria": update["resolution_criteria"]}
                material_changed = True
                row.last_material_change_at = now
                row.updated_at = now
            row.last_seen_at = now

        mention_kind = _text(update.get("mention_kind") or update.get("type") or update.get("kind"), limit=32).lower()
        if mention_kind in _ENTRY_PROJECTION_KINDS:
            mention_kind = "projection"
        if not mention_kind:
            mention_kind = "observation"
        if not created and not material_changed and mention_kind in {"projection", "observation"}:
            mention_kind = "reiteration"
            row.reiteration_count = int(row.reiteration_count or 0) + 1

        mention = None
        mention_created = mention is None
        if mention is None:
            mention = RoadmapMention(
                mention_key=mention_key,
                entry_id=int(row.id),
                source_document_id=source_document_id,
                cloud_goal_run_id=run_id,
                mention_kind=mention_kind,
                summary=_text(update.get("summary") or update.get("claim") or update.get("statement") or title),
                source_owner=_text(update.get("source_owner") or update.get("view_owner") or update.get("owner"), limit=255) or None,
                source_url=source_url,
                expected_window=expected_window,
                stance=_text(update.get("stance"), limit=64) or None,
                confidence_label=confidence_label,
                confidence=confidence,
                materiality=_text(update.get("materiality"), limit=32) or None,
                evidence_tier=_text(update.get("evidence_tier"), limit=64) or None,
                payload={**{key: value for key, value in update.items() if key != "forecast_snapshot"}, "cloud_operation_metadata": metadata},
                created_by=actor,
                created_at=now,
            )
            session.add(mention)
            row.mention_count = int(row.mention_count or 0) + 1
            if created or material_changed:
                # Read only prior immutable snapshots; legacy mentions are never
                # backfilled from a mutable current entry.
                previous_row = (session.query(RoadmapMention.id)
                    .filter(RoadmapMention.entry_id == row.id, RoadmapMention.mention_kind != "outcome_review",
                            RoadmapMention.payload["forecast_snapshot"].as_string().isnot(None))
                    .order_by(RoadmapMention.created_at.desc(), RoadmapMention.id.desc())
                    .first())
                previous = previous_row.id if previous_row else None
                mention.payload = {**mention.payload, "forecast_snapshot": {
                    "title": row.title, "statement": row.statement, "owner": row.owner,
                    "expected_window": row.expected_window,
                    "target_date": row.target_date.isoformat() if row.target_date else None,
                    "target_year": row.target_year, "target_quarter": row.target_quarter,
                    "confidence": row.confidence, "confidence_label": row.confidence_label,
                    "model_use": row.model_use,
                    "resolution_criteria": (row.payload or {}).get("resolution_criteria"),
                    "previous_forecast_mention_id": previous,
                    "revision_kind": "initial" if created else (revision_kind or "revised"),
                    "change_rationale": _text(update.get("change_rationale")) or None,
                    "source_document_id": row.source_document_id,
                }}

        view_ids: list[int] = []
        for spec in _view_specs(update):
            view = _ensure_view(session, spec, now=now)
            _link_entry_view(session, row, view, now=now)
            view_ids.append(int(view.id))

        session.flush()
        applied.append(
            {
                "entry_id": int(row.id),
                "entry_key": row.entry_key,
                "mention_id": int(mention.id) if mention.id is not None else None,
                "mention_key": mention.mention_key,
                "created": created,
                "mention_created": mention_created,
                "material_changed": bool(material_changed),
                "mention_kind": mention_kind,
                "view_ids": view_ids,
            }
        )

    session.flush()
    return {"applied": applied}


def list_roadmap_entries(
    session: Session,
    *,
    q: str | None = None,
    view_key: str | None = None,
    category: str | None = None,
    limit: int = 200,
    period: str = "upcoming",
    today: date | None = None,
) -> list[RoadmapEntry]:
    today = today or datetime.now(ZoneInfo("America/New_York")).date()
    period = period.strip().lower() if isinstance(period, str) else "upcoming"
    if period not in {"upcoming", "past", "all"}:
        period = "upcoming"
    bounded_limit = max(1, min(int(limit), 500))

    if period == "all":
        # Keep each bounded branch independently useful so historical rows cannot
        # consume the query budget needed to show current and upcoming work.
        rows = _list_roadmap_period(
            session,
            q=q,
            view_key=view_key,
            category=category,
            period="upcoming",
            limit=bounded_limit,
            today=today,
        )
        rows.extend(
            _list_roadmap_period(
                session,
                q=q,
                view_key=view_key,
                category=category,
                period="past",
                limit=bounded_limit,
                today=today,
            )
        )
    else:
        rows = _list_roadmap_period(
            session,
            q=q,
            view_key=view_key,
            category=category,
            period=period,
            limit=bounded_limit,
            today=today,
        )

    for row in rows:
        horizon, sort_date, timing_basis = _roadmap_display_timing(row, today=today)
        row.ui_horizon = horizon
        row.ui_sort_date = sort_date
        row.ui_timing_basis = timing_basis
    if period == "upcoming":
        rows = [row for row in rows if row.ui_horizon != "Past / needs review"]
    elif period == "past":
        rows = [row for row in rows if row.ui_horizon == "Past / needs review"]
    rows.sort(key=lambda row: (_roadmap_horizon_rank(row.ui_horizon), row.ui_sort_date, -(row.updated_at.timestamp() if row.updated_at else 0)))
    return rows[:bounded_limit]


def _list_roadmap_period(
    session: Session,
    *,
    q: str | None,
    view_key: str | None,
    category: str | None,
    period: str,
    limit: int,
    today: date,
) -> list[RoadmapEntry]:
    query = session.query(RoadmapEntry)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(RoadmapEntry.title.ilike(like), RoadmapEntry.statement.ilike(like), RoadmapEntry.subject.ilike(like)))
    if category:
        query = query.filter(RoadmapEntry.category == category)
    if view_key:
        query = query.join(RoadmapEntryView).join(RoadmapView).filter(RoadmapView.view_key == view_key)

    base_query = query
    projection = base_query.with_entities(
        RoadmapEntry.id,
        RoadmapEntry.target_date,
        RoadmapEntry.expected_window,
        RoadmapEntry.target_year,
        RoadmapEntry.target_quarter,
        RoadmapEntry.updated_at,
    ).distinct().yield_per(500)

    def candidates() -> Iterable[tuple[tuple[object, ...], int]]:
        for entry_id, target_date, expected_window, target_year, target_quarter, updated_at in projection:
            timing_row = SimpleNamespace(
                target_date=target_date,
                expected_window=expected_window,
                target_year=target_year,
                target_quarter=target_quarter,
            )
            horizon, sort_date, _timing_basis = _roadmap_display_timing(timing_row, today=today)
            if period == "past" and horizon != "Past / needs review":
                continue
            if period == "upcoming" and horizon == "Past / needs review":
                continue
            updated_timestamp = updated_at.timestamp() if updated_at else 0.0
            yield (
                (_roadmap_horizon_rank(horizon), sort_date, -updated_timestamp, int(entry_id)),
                int(entry_id),
            )

    selected = heapq.nsmallest(limit, candidates(), key=lambda item: item[0])
    selected_ids = [entry_id for _sort_key, entry_id in selected]
    if not selected_ids:
        return []
    rows_by_id = {
        int(row.id): row
        for row in session.query(RoadmapEntry)
        .options(
            selectinload(RoadmapEntry.view_links).selectinload(RoadmapEntryView.view),
        )
        .filter(RoadmapEntry.id.in_(selected_ids))
        .all()
    }
    return [rows_by_id[entry_id] for entry_id in selected_ids if entry_id in rows_by_id]


def _roadmap_horizon_rank(value: str) -> int:
    return {"Near term": 0, "Next 12-24 months": 1, "Longer term": 2, "Past / needs review": 3, "Unscheduled": 4}.get(value, 4)


def _roadmap_display_timing(row: RoadmapEntry, *, today: date) -> tuple[str, date, str]:
    window = _text(row.expected_window)
    target = row.target_date
    basis = "dated" if target is not None else ""
    years = [int(value) for value in re.findall(r"(?<!\d)(20\d{2})(?!\d)", window)]
    window_quarter = _quarter_from_window(window)
    fiscal = bool(re.search(r"(?:^|\W)FY\s*20\d{2}(?:$|\W)|\bfiscal\b", window, flags=re.IGNORECASE))
    if target is None:
        iso_match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", window)
        if iso_match:
            target = _date_or_none(iso_match.group(1))
            basis = "dated"
    if target is None and years:
        # Classify a period by its end so Q1/year labels remain usable after
        # their period starts and date ranges do not become prematurely past.
        year = max(years)
        quarter = _int_or_none(row.target_quarter) or window_quarter
        if quarter not in {1, 2, 3, 4}:
            quarter = None
        target = _period_end(year, quarter)
        basis = "fiscal label" if fiscal else ("quarter window" if quarter else "year window")
    if target is None and row.target_year:
        quarter = _int_or_none(row.target_quarter) or window_quarter
        if quarter not in {1, 2, 3, 4}:
            quarter = None
        target = _period_end(int(row.target_year), quarter)
        basis = "target quarter" if quarter else "target year"
    if target is None:
        return "Unscheduled", date.max, basis or "unscheduled"
    if target < today:
        return "Past / needs review", target, basis
    if target <= today + timedelta(days=365):
        return "Near term", target, basis
    if target <= today + timedelta(days=730):
        return "Next 12-24 months", target, basis
    return "Longer term", target, basis


def _period_end(year: int, quarter: int | None) -> date:
    if quarter not in {1, 2, 3, 4}:
        return date(year, 12, 31)
    month = quarter * 3
    next_month = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    return next_month - timedelta(days=1)


def list_roadmap_views(session: Session, *, limit: int = 200) -> list[RoadmapView]:
    return session.query(RoadmapView).order_by(RoadmapView.title.asc()).limit(max(1, min(int(limit), 500))).all()


def get_roadmap_snapshot(
    session: Session, *, query: str = "", category: str = "", period: str = "all", limit: int = 20,
) -> dict[str, Any]:
    """Compact, source-linked readback for a research agent before a claim write."""
    rows = list_roadmap_entries(
        session, q=query[:200] or None, category=category[:64] or None,
        period=period, limit=max(1, min(int(limit), 30)),
    )
    from thoth_terminal.roadmap_outcome_service import get_roadmap_current_forecast_metadata
    forecast_metadata = get_roadmap_current_forecast_metadata(session, [row.id for row in rows])
    entries = []
    for row in rows:
        mentions = (
            session.query(RoadmapMention)
            .filter(RoadmapMention.entry_id == row.id)
            .order_by(RoadmapMention.created_at.desc(), RoadmapMention.id.desc())
            .limit(3).all()
        )
        entries.append({
            "entry_id": row.id, "entry_key": row.entry_key, "title": row.title,
            "statement": row.statement, "subject": row.subject, "category": row.category,
            "owner": row.owner, "source_document_id": row.source_document_id,
            "expected_window": row.expected_window, "target_date": row.target_date.isoformat() if row.target_date else None,
            "target_year": row.target_year, "target_quarter": row.target_quarter,
            "status": row.status, "stance": row.stance, "confidence": row.confidence,
            "model_use": row.model_use, "uncertainty": (row.payload or {}).get("uncertainty"),
            "resolution_criteria": (row.payload or {}).get("resolution_criteria"),
            **forecast_metadata.get(row.id, {}),
            "affected_models": row.affected_models, "horizon": row.ui_horizon,
            "mention_count": row.mention_count,
            "recent_mentions": [{
                "mention_id": mention.id, "source_document_id": mention.source_document_id,
                "source_owner": mention.source_owner, "kind": mention.mention_kind,
                "summary": mention.summary[:600],
                "created_at": mention.created_at.isoformat() if mention.created_at else None,
            } for mention in mentions],
        })
    return {"count": len(entries), "entries": entries}
