"""Immutable, evidence-backed reviews of individual roadmap forecast versions."""
from __future__ import annotations

import hashlib
import heapq
import json
import math
import re
from collections import deque
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session, aliased

from thoth_terminal.db import utcnow
from thoth_terminal.models import Document, RoadmapEntry, RoadmapMention


OUTCOMES = {"pending", "unresolved", "complete", "partial", "missed"}
_SNAPSHOT_FIELDS = (
    "title", "statement", "owner", "expected_window", "target_date", "target_year",
    "target_quarter", "confidence", "confidence_label", "model_use", "resolution_criteria",
    "previous_forecast_mention_id", "revision_kind", "change_rationale", "source_document_id",
)
_LEGACY_KINDS = {"projection", "observation", "delayed", "moved", "revised"}


def _today() -> date:
    return datetime.now(ZoneInfo("America/New_York")).date()


def _snapshot(payload: dict[str, Any], *, mention_kind: str, expected_window: str | None,
              source_document_id: int | None) -> dict[str, Any] | None:
    stored = payload.get("forecast_snapshot")
    if isinstance(stored, dict):
        result = {key: stored.get(key) for key in _SNAPSHOT_FIELDS}
        result["snapshot_basis"] = "immutable_forecast_snapshot"
        result["missing_resolution_criteria"] = not bool(result.get("resolution_criteria"))
        return result
    if mention_kind not in _LEGACY_KINDS:
        return None
    # Legacy update payloads contain only what that sighting supplied. Filling
    # gaps from today's entry would rewrite an old forecast after a revision.
    result = {key: payload.get(key) for key in _SNAPSHOT_FIELDS}
    for field, aliases in {
        "title": ("entry", "topic", "roadmap_item"),
        "statement": ("claim",),
        "expected_window": ("date_window", "timing"),
        "target_year": ("year",), "target_quarter": ("quarter",),
        "confidence": ("confidence_value",),
    }.items():
        if result[field] is None:
            result[field] = next((payload.get(key) for key in aliases if payload.get(key) is not None), None)
    result["expected_window"] = result["expected_window"] or expected_window
    result["source_document_id"] = result["source_document_id"] or source_document_id
    result.update(snapshot_basis="legacy_supplied_fields", missing_resolution_criteria=not bool(result["resolution_criteria"]),
                  original_window_uncertain=not bool(result["expected_window"] or result["target_date"] or result["target_year"]))
    return result


def forecast_snapshot_for_mention(mention: RoadmapMention) -> dict[str, Any] | None:
    if mention.mention_kind == "outcome_review":
        return None
    return _snapshot(mention.payload or {}, mention_kind=mention.mention_kind,
                     expected_window=mention.expected_window, source_document_id=mention.source_document_id)


def _temporal(snapshot: dict[str, Any], as_of: date) -> dict[str, Any]:
    # A local import avoids a cycle when roadmap_service delegates readbacks here.
    from thoth_terminal.roadmap_service import _date_or_none, _int_or_none, _roadmap_display_timing
    window = re.sub(r"\b(20\d{2})\s*[-/]?\s*Q([1-4])\b", r"Q\2 \1", str(snapshot.get("expected_window") or ""), flags=re.IGNORECASE)
    row = SimpleNamespace(target_date=_date_or_none(snapshot.get("target_date")),
                          target_year=_int_or_none(snapshot.get("target_year")),
                          target_quarter=_int_or_none(snapshot.get("target_quarter")),
                          expected_window=window)
    horizon, deadline, basis = _roadmap_display_timing(row, today=as_of)
    fiscal_uncertain = basis == "fiscal label"
    period_start = None
    if deadline != date.max:
        if basis == "dated":
            period_start = deadline
        else:
            years = [int(value) for value in re.findall(r"\b(20\d{2})\b", window)]
            start_year = min(years) if years else deadline.year
            quarter_match = re.search(r"\bQ([1-4])\b|\b([1-4])Q\b", window, flags=re.IGNORECASE)
            start_quarter = int(quarter_match.group(1) or quarter_match.group(2)) if quarter_match else row.target_quarter
            start_month = 3 * (start_quarter - 1) + 1 if start_quarter in {1, 2, 3, 4} else 1
            period_start = date(start_year, start_month, 1)
    return {"deadline": None if deadline == date.max else deadline.isoformat(),
            "period_start": period_start.isoformat() if period_start else None,
            "window_basis": basis, "horizon": horizon, "as_of": as_of.isoformat(),
            "deadline_is_calendar_proxy": fiscal_uncertain,
            "temporal_uncertainty": "Fiscal year end is unknown; calendar period end is a review proxy." if fiscal_uncertain else None}


def _review_dict(row: RoadmapMention) -> dict[str, Any]:
    payload = row.payload or {}
    return {"mention_id": row.id, "forecast_mention_id": payload.get("forecast_mention_id"),
            "outcome": payload.get("outcome", "unresolved"), "rationale": payload.get("rationale"),
            "source_document_ids": payload.get("source_document_ids", []), "confidence": row.confidence,
            "observed_window": payload.get("observed_window"), "actual_date": payload.get("actual_date"),
            "uncertainty": payload.get("uncertainty"), "operation_key": payload.get("operation_key"),
            "cloud_run_id": row.cloud_goal_run_id, "as_of": payload.get("evaluated_as_of") or payload.get("as_of"),
            "created_at": row.created_at.isoformat()}


def review_roadmap_forecast(session: Session, *, entry_key: str, forecast_mention_id: int,
                           outcome: str, rationale: str, source_document_ids: list[int], confidence: float,
                           operation_key: str, cloud_run_id: int, observed_window: str | None = None,
                           actual_date: str | None = None, uncertainty: str | None = None,
                           actor: str = "worldview_agent", as_of: date | None = None) -> dict[str, Any]:
    """Append a review. Authorization and CloudGoalEvent recording belong to the caller."""
    explicit_as_of = as_of
    as_of = as_of or _today()
    outcome = str(outcome).strip().lower()
    if outcome not in OUTCOMES:
        raise ValueError("outcome must be pending, unresolved, complete, partial, or missed")
    rationale = str(rationale or "").strip()
    uncertainty = str(uncertainty or "").strip() or None
    operation_key = str(operation_key or "").strip()
    if not rationale or len(rationale) > 12000:
        raise ValueError("A specific outcome rationale of 1-12000 characters is required")
    if not operation_key or len(operation_key) > 255:
        raise ValueError("operation_key must contain 1-255 characters")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise ValueError("confidence must be a finite number from 0 to 1")
    if (confidence < 0.7 or outcome == "unresolved") and not uncertainty:
        raise ValueError("Low-confidence or unresolved reviews require explicit uncertainty and missing evidence")
    if not isinstance(source_document_ids, list) or len(source_document_ids) > 40 or any(type(value) is not int or value <= 0 for value in source_document_ids):
        raise ValueError("source_document_ids must contain at most 40 positive document IDs")
    source_ids = sorted(set(source_document_ids))
    if outcome in {"complete", "partial", "missed"} and not source_ids:
        raise ValueError("complete, partial, and missed outcomes require source documents")
    if actual_date is not None:
        try:
            actual_date = date.fromisoformat(actual_date).isoformat()
        except (TypeError, ValueError):
            raise ValueError("actual_date must be an ISO date") from None
        if date.fromisoformat(actual_date) > as_of:
            raise ValueError("actual_date cannot be in the future")
    observed_window = str(observed_window or "").strip() or None
    if observed_window and len(observed_window) > 128:
        raise ValueError("observed_window must not exceed 128 characters")
    if type(cloud_run_id) is not int or cloud_run_id <= 0:
        raise ValueError("cloud_run_id must be a positive run ID")
    entry = session.query(RoadmapEntry).filter(RoadmapEntry.entry_key == entry_key).first()
    if entry is None:
        raise ValueError("Roadmap entry does not exist")
    forecast = session.get(RoadmapMention, forecast_mention_id)
    if forecast is None or forecast.entry_id != entry.id:
        raise ValueError("Forecast mention must belong to the specified roadmap entry")
    snapshot = forecast_snapshot_for_mention(forecast)
    if snapshot is None:
        raise ValueError("Mention is not a forecast version")
    forecast_timing = _temporal(snapshot, as_of)
    deadline = forecast_timing["deadline"]
    observed_timing = _temporal({"expected_window": observed_window}, as_of)
    observed_deadline = actual_date or observed_timing["deadline"]
    if outcome == "complete":
        if not observed_deadline:
            raise ValueError("Complete requires an actual date or a dated observed window; unknown timing remains unresolved")
        if not actual_date and observed_timing["period_start"] and observed_timing["period_start"] > as_of.isoformat():
            raise ValueError("Observed completion window cannot be in the future")
        if deadline and observed_deadline > deadline:
            raise ValueError("Late fulfillment cannot score the original forecast complete; review its timing as partial or missed")
        if (not deadline or forecast_timing["deadline_is_calendar_proxy"] or observed_timing["deadline_is_calendar_proxy"]) and not uncertainty:
            raise ValueError("Uncertain forecast or fiscal timing requires explicit uncertainty")
    if outcome == "complete" and snapshot.get("missing_resolution_criteria") and not uncertainty:
        raise ValueError("A complete review with missing original resolution criteria requires explicit uncertainty")
    found_ids = {value for (value,) in session.query(Document.id).filter(Document.id.in_(source_ids))} if source_ids else set()
    if found_ids != set(source_ids):
        raise ValueError("Every source document must exist")
    request = {"entry_key": entry_key, "forecast_mention_id": forecast_mention_id, "outcome": outcome,
               "rationale": rationale, "source_document_ids": source_ids, "confidence": float(confidence),
               "observed_window": observed_window, "actual_date": actual_date, "uncertainty": uncertainty,
               "operation_key": operation_key, "cloud_run_id": cloud_run_id, "actor": actor,
               "as_of": explicit_as_of.isoformat() if explicit_as_of else None}
    digest = hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    mention_key = "roadmap-outcome:" + hashlib.sha256(f"{cloud_run_id}:{operation_key}".encode()).hexdigest()
    existing = session.query(RoadmapMention).filter(RoadmapMention.mention_key == mention_key).first()
    if existing is not None:
        if (existing.payload or {}).get("request_digest") != digest:
            raise ValueError("operation_key already exists with a different review payload")
        return {"entry_key": entry_key, "entry_id": entry.id, "created": False, **_review_dict(existing)}
    mention = RoadmapMention(mention_key=mention_key, entry_id=entry.id,
                            mention_kind="outcome_review", summary=rationale, confidence=float(confidence),
                            source_document_id=source_ids[0] if source_ids else None, cloud_goal_run_id=cloud_run_id,
                            created_by=actor, created_at=utcnow(),
                            payload={**request, "request_digest": digest, "forecast_snapshot": snapshot,
                                     "evaluated_as_of": as_of.isoformat(), "observed_timing": observed_timing,
                                     "forecast_created_at": forecast.created_at.isoformat(),
                                     "evaluated_forecast_mention_id": forecast_mention_id,
                                     "causal_explanation": rationale,
                                     "observed": {"window": observed_window, "actual_date": actual_date}})
    session.add(mention)
    session.flush()
    return {"entry_key": entry_key, "entry_id": entry.id, "created": True, **_review_dict(mention)}


def _version_query(session: Session, *, lightweight: bool = False):
    """Stream only forecast/review metadata, never document text or arbitrary payload state."""
    review_ref = RoadmapMention.payload["forecast_mention_id"].as_integer()
    latest = session.query(RoadmapMention.entry_id.label("entry_id"), review_ref.label("forecast_id"),
                           func.max(RoadmapMention.id).label("review_id")).filter(
                               RoadmapMention.mention_kind == "outcome_review").group_by(RoadmapMention.entry_id, review_ref).subquery()
    review = aliased(RoadmapMention)
    legacy_fields = set(_SNAPSHOT_FIELDS) | {"entry", "topic", "roadmap_item", "claim", "date_window", "timing", "year", "quarter", "confidence_value"}
    if lightweight:
        legacy_fields = {"title", "entry", "topic", "roadmap_item", "expected_window", "date_window", "timing", "target_date", "target_year", "year", "target_quarter", "quarter"}
        snapshot_columns = [RoadmapMention.payload["forecast_snapshot"].as_string().isnot(None).label("has_snapshot"),
                            *(RoadmapMention.payload["forecast_snapshot"][key].label("snapshot_" + key)
                              for key in ("title", "expected_window", "target_date", "target_year", "target_quarter"))]
    else:
        snapshot_columns = [RoadmapMention.payload["forecast_snapshot"].label("forecast_snapshot")]
    query = session.query(RoadmapMention.id.label("mention_id"), RoadmapMention.entry_id,
                          RoadmapMention.mention_kind, RoadmapMention.created_at,
                          RoadmapMention.expected_window, RoadmapMention.source_document_id,
                          *snapshot_columns,
                          *(RoadmapMention.payload[key].label("legacy_" + key) for key in sorted(legacy_fields)),
                          review.id.label("review_id"), review.payload["outcome"].as_string().label("outcome"),
                          review.created_at.label("reviewed_at"), RoadmapEntry.entry_key, RoadmapEntry.title.label("entry_title"))
    return query.join(RoadmapEntry, RoadmapEntry.id == RoadmapMention.entry_id).outerjoin(
        latest, and_(latest.c.entry_id == RoadmapMention.entry_id, latest.c.forecast_id == RoadmapMention.id)).outerjoin(
            review, review.id == latest.c.review_id).filter(_forecast_condition()).order_by(RoadmapMention.id)


def _forecast_condition():
    return and_(RoadmapMention.mention_kind != "outcome_review", or_(
        RoadmapMention.payload["forecast_snapshot"].as_string().isnot(None),
        RoadmapMention.mention_kind.in_(_LEGACY_KINDS)))


def _version(row: Any, as_of: date) -> dict[str, Any] | None:
    values = row._mapping
    payload = {key.removeprefix("legacy_"): value for key, value in values.items() if key.startswith("legacy_")}
    payload["forecast_snapshot"] = values.get("forecast_snapshot")
    if values.get("has_snapshot"):
        payload["forecast_snapshot"] = {key.removeprefix("snapshot_"): value for key, value in values.items() if key.startswith("snapshot_")}
    snapshot = _snapshot(payload, mention_kind=row.mention_kind, expected_window=row.expected_window,
                         source_document_id=row.source_document_id)
    if snapshot is None:
        return None
    return {"forecast_mention_id": row.mention_id, "entry_key": row.entry_key, "entry_id": row.entry_id,
            "title": snapshot.get("title") or row.entry_title, "forecast_snapshot": snapshot,
            "forecast_created_at": row.created_at.isoformat(), "source_document_id": snapshot.get("source_document_id"),
            "created_at": row.created_at.isoformat(), "latest_outcome": row.outcome or "pending",
            "latest_review_id": row.review_id, "reviewed_at": row.reviewed_at.isoformat() if row.reviewed_at else None,
            **_temporal(snapshot, as_of)}


def get_roadmap_entry_history(session: Session, *, entry_key: str, limit: int = 30) -> dict[str, Any]:
    limit = max(1, min(int(limit), 100))
    entry = session.query(RoadmapEntry).filter(RoadmapEntry.entry_key == entry_key).first()
    if entry is None:
        raise ValueError("Roadmap entry does not exist")
    today = _today()
    original = None
    recent: deque[dict[str, Any]] = deque(maxlen=limit)
    version_count = 0
    for row in _version_query(session, lightweight=True).filter(RoadmapMention.entry_id == entry.id).yield_per(256):
        version = _version(row, today)
        if version is not None:
            original = original or version
            recent.append(version)
            version_count += 1
    selected_ids = {version["forecast_mention_id"] for version in recent}
    if original:
        selected_ids.add(original["forecast_mention_id"])
    hydrated = {row.mention_id: _version(row, today) for row in _version_query(session).filter(RoadmapMention.id.in_(selected_ids))} if selected_ids else {}
    recent = deque((hydrated[version["forecast_mention_id"]] for version in recent), maxlen=limit)
    original = hydrated[original["forecast_mention_id"]] if original else None
    mentions = session.query(RoadmapMention).filter(RoadmapMention.entry_id == entry.id)
    mention_count = mentions.count()
    outcome_query = mentions.filter(RoadmapMention.mention_kind == "outcome_review")
    outcome_count = outcome_query.count()
    outcomes = [_review_dict(row) for row in outcome_query.order_by(RoadmapMention.id.desc()).limit(limit)]
    recent_mentions = [{"mention_id": row.id, "mention_kind": row.mention_kind, "summary": row.summary[:2000],
                        "source_document_id": row.source_document_id, "created_at": row.created_at.isoformat()}
                       for row in mentions.with_entities(RoadmapMention.id, RoadmapMention.mention_kind,
                           RoadmapMention.summary, RoadmapMention.source_document_id, RoadmapMention.created_at)
                       .order_by(RoadmapMention.id.desc()).limit(limit)]
    return {"entry": {"entry_id": entry.id, "entry_key": entry.entry_key, "title": entry.title,
                      "statement": entry.statement, "owner": entry.owner, "expected_window": entry.expected_window,
                      "resolution_criteria": (entry.payload or {}).get("resolution_criteria"),
                      "target_date": entry.target_date.isoformat() if entry.target_date else None,
                      "target_year": entry.target_year, "target_quarter": entry.target_quarter},
            "current_forecast": recent[-1] if recent else None,
            "original_forecast": original, "original_snapshot": original["forecast_snapshot"] if original else None,
            "forecast_versions": list(recent), "recent_mentions": recent_mentions, "outcome_history": outcomes,
            "counts": {"forecast_versions": version_count, "mentions": mention_count, "outcome_reviews": outcome_count},
            "truncation": {"forecast_versions": max(0, version_count - len(recent)),
                           "mentions": max(0, mention_count - len(recent_mentions)),
                           "outcome_reviews": max(0, outcome_count - len(outcomes))}, "limit": limit}


def get_roadmap_review_queue(session: Session, *, limit: int = 20, as_of: date | None = None,
                             lookahead_days: int = 30) -> dict[str, Any]:
    limit = max(1, min(int(limit), 100))
    as_of = as_of or _today()
    lookahead_days = max(0, min(int(lookahead_days), 3660))
    cutoff = as_of + timedelta(days=lookahead_days)
    counts = {"forecast_versions": 0, "eligible": 0, "settled": 0, "outside_window": 0, "unscheduled": 0}
    latest_by_entry: dict[int, int] = {}

    def candidates():
        for row in _version_query(session, lightweight=True).yield_per(256):
            version = _version(row, as_of)
            if version is None:
                continue
            counts["forecast_versions"] += 1
            latest_by_entry[row.entry_id] = row.mention_id
            if version["latest_outcome"] in {"complete", "missed"}:
                counts["settled"] += 1
                continue
            deadline = date.fromisoformat(version["deadline"]) if version["deadline"] else None
            if deadline is None:
                counts["unscheduled"] += 1
                continue
            if deadline > cutoff:
                counts["outside_window"] += 1
                continue
            counts["eligible"] += 1
            version["review_reason"] = "past_window_needs_evidence" if deadline < as_of else "window_due_for_review"
            yield ((deadline, row.mention_id), version)

    selected = [version for _, version in heapq.nsmallest(limit, candidates(), key=lambda item: item[0])]
    selected_ids = [version["forecast_mention_id"] for version in selected]
    hydrated = {row.mention_id: _version(row, as_of) for row in _version_query(session).filter(RoadmapMention.id.in_(selected_ids))} if selected_ids else {}
    for version in selected:
        version.update(hydrated[version["forecast_mention_id"]])
        version["is_current_forecast"] = latest_by_entry[version["entry_id"]] == version["forecast_mention_id"]
        version["superseded"] = not version["is_current_forecast"]
    return {"candidates": selected, "count": counts["eligible"], "selected_count": len(selected),
            "truncated_count": max(0, counts["eligible"] - len(selected)), "counts": counts,
            "limit": limit, "as_of": as_of.isoformat(), "lookahead_days": lookahead_days,
            "review_through": cutoff.isoformat(),
            "selection_basis": "Each original or revised forecast's own period-end deadline; latest review per forecast; elapsed dates never imply missed."}


def get_roadmap_current_forecast_metadata(session: Session, entry_ids: list[int]) -> dict[int, dict[str, Any]]:
    """Compact bulk metadata for at most 500 selected entries, without N+1 queries."""
    ids = sorted({int(value) for value in entry_ids})
    if len(ids) > 500:
        raise ValueError("Current forecast metadata is limited to 500 selected entries")
    if not ids:
        return {}
    latest: dict[int, dict[str, Any]] = {}
    latest_ids = session.query(func.max(RoadmapMention.id)).filter(
        RoadmapMention.entry_id.in_(ids), _forecast_condition()).group_by(RoadmapMention.entry_id)
    today = _today()
    for row in _version_query(session).filter(RoadmapMention.id.in_(latest_ids)).yield_per(256):
        version = _version(row, today)
        if version is not None:
            snapshot = version["forecast_snapshot"]
            latest[row.entry_id] = {"current_forecast_mention_id": row.mention_id,
                                    "revision_kind": snapshot.get("revision_kind"),
                                    "change_rationale": snapshot.get("change_rationale"),
                                    "outcome": version["latest_outcome"], "reviewed_at": version["reviewed_at"],
                                    "latest_review_id": version["latest_review_id"],
                                    "resolution_criteria": snapshot.get("resolution_criteria"),
                                    "explanation": None, "rationale": None}
    review_ids = [value["latest_review_id"] for value in latest.values() if value["latest_review_id"]]
    explanations = {row.id: row.rationale for row in session.query(
        RoadmapMention.id, RoadmapMention.payload["rationale"].as_string().label("rationale")
    ).filter(RoadmapMention.id.in_(review_ids))} if review_ids else {}
    for value in latest.values():
        value["explanation"] = value["rationale"] = explanations.get(value["latest_review_id"])
    return latest
