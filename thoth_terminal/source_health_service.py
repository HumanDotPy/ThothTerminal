from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
import math
import statistics
from typing import Any, Iterable

from sqlalchemy import func
from sqlalchemy.orm import Session

from thoth_terminal.models import Document, IngestionRun, Source


_HISTORY_DAYS = 90
_DEFAULT_HARD_SILENCE_DAYS = 7
_DEFAULT_CONTINUOUS_CATEGORIES = {"analyst", "research", "social", "tier3_wire", "web"}
_NONCONTINUOUS_CONNECTORS = {"local_research_library"}


def _positive_number(value: object, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return float(ordered[index])


def classify_adaptive_source_silence(
    *,
    now: datetime,
    productive_run_times: Iterable[datetime],
    successful_run_times: Iterable[datetime],
    latest_document_at: datetime | None,
    polling_interval_seconds: int | None,
    config: dict[str, Any] | None = None,
) -> dict[str, object]:
    cfg = dict(config or {})
    if cfg.get("adaptive_silence_enabled") is False:
        return {"enabled": False, "common": False, "silent": False, "critical": False}

    productive = sorted({value for value in productive_run_times if value is not None})
    successful = sorted({value for value in successful_run_times if value is not None})
    intervals = [
        max(1.0, (current - previous).total_seconds())
        for previous, current in zip(productive, productive[1:])
        if current > previous
    ]
    median_interval_s = float(statistics.median(intervals)) if intervals else None
    p90_interval_s = _percentile(intervals, 0.9)
    productive_days = {value.date() for value in productive}
    productive_span_s = (
        max(0.0, (productive[-1] - productive[0]).total_seconds()) if len(productive) >= 2 else 0.0
    )

    min_productive_runs = max(2, int(_positive_number(cfg.get("adaptive_silence_min_productive_runs"), 4)))
    max_common_interval_s = _positive_number(cfg.get("adaptive_silence_max_common_days"), 4) * 86400
    category = str(cfg.get("_source_category") or "").strip().lower()
    connector = str(cfg.get("connector") or "").strip().lower()
    continuous_semantics = bool(
        not category
        or category in _DEFAULT_CONTINUOUS_CATEGORIES
        or cfg.get("adaptive_silence_enabled") is True
    ) and connector not in _NONCONTINUOUS_CONNECTORS
    common = bool(
        continuous_semantics
        and len(productive) >= min_productive_runs
        and len(productive_days) >= min_productive_runs
        and productive_span_s >= 7 * 86400
        and median_interval_s is not None
        and median_interval_s <= max_common_interval_s
    )

    last_productive_at = productive[-1] if productive else None
    latest_new_at = max(
        [value for value in (last_productive_at, latest_document_at) if value is not None],
        default=None,
    )
    new_age_s = max(0, int((now - latest_new_at).total_seconds())) if latest_new_at else None
    zero_success_runs = (
        sum(1 for value in successful if last_productive_at is None or value > last_productive_at)
        if successful
        else 0
    )

    hard_s = int(_positive_number(cfg.get("adaptive_silence_hard_days"), _DEFAULT_HARD_SILENCE_DAYS) * 86400)
    explicit_warning_minutes = cfg.get("content_freshness_sla_minutes")
    if explicit_warning_minutes is not None and str(explicit_warning_minutes).strip():
        warning_s = int(_positive_number(explicit_warning_minutes, 1440) * 60)
        common = bool(latest_new_at)
    elif common:
        floor_s = int(_positive_number(cfg.get("adaptive_silence_floor_hours"), 24) * 3600)
        poll_s = max(0, int(polling_interval_seconds or 0))
        warning_s = int(
            max(
                floor_s,
                poll_s * 6,
                (median_interval_s or 0) * 3,
                (p90_interval_s or 0) * 1.5,
            )
        )
    else:
        warning_s = None
    if warning_s is not None:
        warning_s = min(max(1, int(warning_s)), hard_s)

    critical = bool(common and new_age_s is not None and new_age_s > hard_s)
    silent = bool(
        common
        and new_age_s is not None
        and warning_s is not None
        and new_age_s > warning_s
        and (critical or zero_success_runs >= 2)
    )
    return {
        "enabled": True,
        "common": common,
        "silent": silent,
        "critical": critical,
        "severity": "critical" if critical else ("warning" if silent else None),
        "latest_new_at": latest_new_at,
        "new_age_s": new_age_s,
        "warning_s": warning_s,
        "hard_s": hard_s,
        "median_interval_s": int(median_interval_s) if median_interval_s is not None else None,
        "p90_interval_s": int(p90_interval_s) if p90_interval_s is not None else None,
        "productive_run_count": len(productive),
        "productive_day_count": len(productive_days),
        "productive_span_s": int(productive_span_s),
        "successful_run_count": len(successful),
        "zero_success_runs": zero_success_runs,
    }


def build_adaptive_source_silence_profiles(
    session: Session,
    *,
    sources: Iterable[Source],
    now: datetime,
) -> dict[int, dict[str, object]]:
    source_list = list(sources)
    source_ids = [int(source.id) for source in source_list]
    if not source_ids:
        return {}

    history_start = now - timedelta(days=_HISTORY_DAYS)
    run_rows = (
        session.query(IngestionRun.source_id, IngestionRun.started_at, IngestionRun.items_new)
        .filter(IngestionRun.source_id.in_(source_ids))
        .filter(IngestionRun.status == "success")
        .filter(IngestionRun.started_at >= history_start)
        .order_by(IngestionRun.source_id.asc(), IngestionRun.started_at.asc())
        .all()
    )
    successful_by_source: dict[int, list[datetime]] = defaultdict(list)
    productive_by_source: dict[int, list[datetime]] = defaultdict(list)
    for source_id, started_at, items_new in run_rows:
        successful_by_source[int(source_id)].append(started_at)
        if int(items_new or 0) > 0:
            productive_by_source[int(source_id)].append(started_at)

    latest_documents = {
        int(source_id): ingested_at
        for source_id, ingested_at in (
            session.query(Document.source_id, func.max(Document.ingested_at))
            .filter(Document.source_id.in_(source_ids))
            .group_by(Document.source_id)
            .all()
        )
    }
    return {
        int(source.id): classify_adaptive_source_silence(
            now=now,
            productive_run_times=productive_by_source.get(int(source.id), []),
            successful_run_times=successful_by_source.get(int(source.id), []),
            latest_document_at=latest_documents.get(int(source.id)),
            polling_interval_seconds=source.polling_interval_seconds,
            config={**dict(source.config or {}), "_source_category": str(source.category or "")},
        )
        for source in source_list
    }
