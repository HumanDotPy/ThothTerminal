from __future__ import annotations

import csv
import hashlib
import json
import re
from datetime import date, datetime
from io import BytesIO, StringIO
from threading import RLock
from typing import Any
import weakref

from sqlalchemy import func
from sqlalchemy.orm import Session

from thoth_terminal.db import utcnow
from thoth_terminal.dataset_catalog import (
    DATASET_FAMILY_COLUMN_TEMPLATES,
    DATASET_FAMILY_MATERIALIZER_FAMILIES,
    DATASET_FAMILY_META,
    DATASET_FAMILY_REQUIRED_ROLES,
    DATASET_OBJECT_TYPES,
    DATASET_VIEW_OPERATOR_VALUES,
    DATASET_VIEW_OPERATORS,
)
from thoth_terminal.models import (
    ChartDatasetView,
    Dataset,
    DatasetColumn,
    Document,
    DatasetLink,
    DatasetRow,
    DatasetRowContent,
    DatasetVersion,
    DatasetView,
    InstrumentRequest,
    ProductStageEntry,
    StructuredDataFact,
)
from thoth_terminal.product_stage_service import serialize_product_stage_entry


_DATASET_SQL_FIRST_ROW_THRESHOLD = 1000
_DATASET_TABLE_ENSURE_LOCK = RLock()
_DATASET_TABLE_ENSURED_ENGINES: "weakref.WeakSet[Any]" = weakref.WeakSet()


def _slugify(value: str, *, default: str = "dataset") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return slug[:120] or default


def _table_models() -> tuple[type[Any], ...]:
    return (Dataset, DatasetVersion, DatasetColumn, DatasetRowContent, DatasetRow, DatasetView, DatasetLink, ChartDatasetView)


def ensure_dataset_tables(session: Session) -> None:
    bind = session.get_bind()
    engine = getattr(bind, "engine", bind)
    if engine in _DATASET_TABLE_ENSURED_ENGINES:
        return
    with _DATASET_TABLE_ENSURE_LOCK:
        if engine in _DATASET_TABLE_ENSURED_ENGINES:
            return
        failed = False
        for model in _table_models():
            try:
                model.__table__.create(bind=bind, checkfirst=True)
            except Exception:
                failed = True
        if not failed:
            _DATASET_TABLE_ENSURED_ENGINES.add(engine)


def _dataset_unique_slug(session: Session, title: str, *, slug: str | None = None) -> str:
    base = _slugify(slug or title)
    existing = {row[0] for row in session.query(Dataset.slug).filter(Dataset.slug.like(f"{base}%")).all()}
    if base not in existing:
        return base
    idx = 2
    while f"{base}-{idx}" in existing:
        idx += 1
    return f"{base}-{idx}"


def _dataset_version_payload(version: DatasetVersion | None) -> dict[str, Any] | None:
    if not version:
        return None
    return {
        "id": int(version.id),
        "version": int(version.version),
        "status": version.status,
        "label": version.label,
        "source_event": version.source_event,
        "created_at": version.created_at.isoformat() if version.created_at else None,
        "updated_at": version.updated_at.isoformat() if version.updated_at else None,
    }


def _parse_iso_date(value: Any) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except Exception:
        return None


def build_dataset_profile(columns: list[dict[str, Any]] | None, rows: list[dict[str, Any]] | None) -> dict[str, Any]:
    column_list = [dict(col) for col in (columns or []) if isinstance(col, dict)]
    row_list = [dict(row) for row in (rows or []) if isinstance(row, dict)]
    metric_columns = [col for col in column_list if str(col.get("data_type") or "") == "number" or str(col.get("role") or "") == "metric"]
    timestamp_columns = [col for col in column_list if str(col.get("data_type") or "") == "date" or str(col.get("role") or "") == "timestamp"]
    dimension_columns = [col for col in column_list if col not in metric_columns and col not in timestamp_columns]

    date_values: list[date] = []
    latest_row: dict[str, Any] | None = None
    latest_row_date: date | None = None
    timestamp_keys = [str(col.get("key") or "") for col in timestamp_columns if str(col.get("key") or "").strip()]
    for row in row_list:
        values = row.get("values") or {}
        for key in timestamp_keys:
            candidate = _parse_iso_date(values.get(key))
            if candidate is not None:
                date_values.append(candidate)
                if latest_row_date is None or candidate >= latest_row_date:
                    latest_row_date = candidate
                    latest_row = row

    cards: list[dict[str, Any]] = []
    for col in metric_columns[:4]:
        key = str(col.get("key") or "")
        label = str(col.get("label") or key or "Metric")
        series_values: list[float] = []
        latest_value: float | int | None = None
        latest_value_date: str | None = latest_row_date.isoformat() if latest_row_date else None
        if latest_row is not None:
            latest_candidate = (latest_row.get("values") or {}).get(key)
            if isinstance(latest_candidate, (int, float)) and not isinstance(latest_candidate, bool):
                latest_value = latest_candidate
        for row in row_list:
            value = (row.get("values") or {}).get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                series_values.append(float(value))
        if not series_values and latest_value is None:
            continue
        cards.append(
            {
                "key": key,
                "label": label,
                "latest": latest_value,
                "latest_display": _format_compact_number(latest_value),
                "latest_date": latest_value_date,
                "min": min(series_values) if series_values else None,
                "min_display": _format_compact_number(min(series_values)) if series_values else None,
                "max": max(series_values) if series_values else None,
                "max_display": _format_compact_number(max(series_values)) if series_values else None,
                "observations": len(series_values),
            }
        )

    return {
        "row_count": len(row_list),
        "column_count": len(column_list),
        "metric_count": len(metric_columns),
        "dimension_count": len(dimension_columns),
        "timestamp_count": len(timestamp_columns),
        "metric_columns": [{"key": str(col.get("key") or ""), "label": str(col.get("label") or col.get("key") or "")} for col in metric_columns],
        "dimension_columns": [{"key": str(col.get("key") or ""), "label": str(col.get("label") or col.get("key") or "")} for col in dimension_columns],
        "timestamp_columns": [{"key": str(col.get("key") or ""), "label": str(col.get("label") or col.get("key") or "")} for col in timestamp_columns],
        "date_range": {
            "start": min(date_values).isoformat(),
            "end": max(date_values).isoformat(),
            "label": f"{min(date_values).isoformat()} → {max(date_values).isoformat()}",
        }
        if date_values
        else None,
        "latest_row_key": str(latest_row.get("row_key") or "") if latest_row else None,
        "cards": cards,
    }


def dataset_family_meta(family: str | None) -> dict[str, str]:
    key = str(family or "general").strip().lower() or "general"
    return dict(DATASET_FAMILY_META.get(key) or DATASET_FAMILY_META["general"])


def dataset_family_column_template(family: str | None) -> list[dict[str, str]]:
    key = str(family or "general").strip().lower() or "general"
    return [dict(row) for row in DATASET_FAMILY_COLUMN_TEMPLATES.get(key, ())]


def dataset_display_title(title: str | None) -> str:
    raw = str(title or "").strip()
    if not raw:
        return "Untitled Dataset"
    match = re.match(
        r"^(?P<reporter>[A-Z]{2})\s+(?P<flow>Export|Import)\s+(?P<partner>\S+)\s+(?P<theme>.+?)\s+HS\s+(?P<hs>\d{4,10})\s+(?P<metric>.+)$",
        raw,
        flags=re.IGNORECASE,
    )
    if not match:
        return raw
    reporter = match.group("reporter").upper()
    flow_key = match.group("flow").lower()
    flow_label = "Exports" if flow_key == "export" else "Imports"
    partner = match.group("partner")
    theme = match.group("theme").replace("_", " ").strip().title()
    metric_key = match.group("metric").strip().lower().replace("_", " ")
    metric_label = {
        "value usd": "USD",
        "weight kg": "kg",
        "quantity": "units",
    }.get(metric_key, metric_key.upper())
    partner_label = "" if partner.lower() == "world" else f" to {partner}" if flow_key == "export" else f" from {partner}"
    return f"{reporter} {theme} {flow_label}{partner_label} in {metric_label} (HS {match.group('hs')})"
