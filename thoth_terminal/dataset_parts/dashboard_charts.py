from __future__ import annotations

import csv
import hashlib
import json
import re
from datetime import date, datetime
from io import BytesIO, StringIO
from threading import RLock
from typing import Any

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
    DatasetVersion,
    DatasetView,
    InstrumentRequest,
    ProductStageEntry,
    StructuredDataFact,
)
from thoth_terminal.product_stage_service import serialize_product_stage_entry



def _non_empty(value: Any) -> bool:
    return value not in (None, "")


def _column_lookup(columns: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    return {str(col.get("key") or ""): dict(col) for col in (columns or []) if isinstance(col, dict)}


def _first_column_by_role(columns: list[dict[str, Any]] | None, role: str) -> dict[str, Any] | None:
    for col in columns or []:
        if str(col.get("role") or "").strip().lower() == role:
            return dict(col)
    return None


def _columns_by_role(columns: list[dict[str, Any]] | None, role: str) -> list[dict[str, Any]]:
    return [dict(col) for col in (columns or []) if str(col.get("role") or "").strip().lower() == role]


def _dimension_summary(rows: list[dict[str, Any]], key: str, *, limit: int = 5) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for row in rows:
        value = str((row.get("values") or {}).get(key) or "").strip()
        if not value:
            continue
        counts[value] = counts.get(value, 0) + 1
    items = [{"label": label, "value": count} for label, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]]
    return items


def _parse_dataset_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        if len(raw) == 7 and raw[4] == "-":
            return date.fromisoformat(f"{raw}-01")
        return date.fromisoformat(raw[:10])
    except Exception:
        return None


def _rows_sorted_by_timestamp(rows: list[dict[str, Any]], timestamp_key: str | None) -> list[dict[str, Any]]:
    if not timestamp_key:
        return list(rows)
    return sorted(
        rows,
        key=lambda row: (
            _parse_dataset_date((row.get("values") or {}).get(timestamp_key)) or date.min,
            str(row.get("row_key") or ""),
        ),
    )


def _series_points(rows: list[dict[str, Any]], *, x_key: str, y_key: str, limit: int = 240) -> list[dict[str, Any]]:
    series: list[dict[str, Any]] = []
    for row in rows:
        values = row.get("values") or {}
        y_value = values.get(y_key)
        if not isinstance(y_value, (int, float)) or isinstance(y_value, bool):
            continue
        x_value = values.get(x_key)
        series.append({"date": str(x_value or row.get("row_key") or ""), "value": float(y_value)})
    if len(series) <= limit:
        return series
    return series[-limit:]


def _display_label(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    known = {
        "hs": "HS",
        "ipo": "IPO",
        "url": "URL",
        "usd": "USD",
        "yoy": "YoY",
        "mom": "MoM",
        "qoq": "QoQ",
        "id": "ID",
        "sec": "SEC",
        "asx": "ASX",
        "krx": "KRX",
        "twse": "TWSE",
        "hkex": "HKEX",
        "jpx": "JPX",
    }
    parts = re.split(r"[_\s/-]+", raw)
    return " ".join(known.get(part.lower(), part.capitalize()) for part in parts if part)


def _column_label(col: dict[str, Any] | None, fallback: str = "") -> str:
    if not col:
        return _display_label(fallback) or fallback
    return str(col.get("label") or "").strip() or _display_label(col.get("key") or fallback) or fallback


def _column_unit(col: dict[str, Any] | None) -> str:
    if not col:
        return ""
    config = col.get("config") if isinstance(col.get("config"), dict) else {}
    for key in ("unit", "units", "display_unit", "axis_unit", "currency"):
        value = config.get(key)
        if value not in (None, ""):
            return str(value)
    key_label = f"{col.get('key') or ''} {col.get('label') or ''}".lower()
    if "%" in key_label or "percent" in key_label or "margin" in key_label or "rate" in key_label or "yield" in key_label:
        return "%"
    if "usd" in key_label or "dollar" in key_label:
        return "USD"
    return ""


def _chart_context_items(columns: list[dict[str, Any]], rows: list[dict[str, Any]], *, exclude: set[str]) -> list[dict[str, str]]:
    if not rows:
        return []
    preferred = [
        "company",
        "company_name",
        "issuer",
        "ticker",
        "market",
        "exchange",
        "reporter_iso",
        "partner_scope",
        "partner_iso",
        "flow",
        "hs_code",
        "commodity",
        "source_kind",
    ]
    candidates = [col for col in columns if str(col.get("key") or "") not in exclude and str(col.get("role") or "").lower() != "metric"]
    candidates.sort(key=lambda col: preferred.index(str(col.get("key") or "")) if str(col.get("key") or "") in preferred else len(preferred))
    items: list[dict[str, str]] = []
    for col in candidates:
        key = str(col.get("key") or "")
        if not key:
            continue
        lowered_key = key.lower()
        if lowered_key != "source_kind" and any(fragment in lowered_key for fragment in ("date", "time", "url", "document", "notes", "description")):
            continue
        values = []
        seen: set[str] = set()
        for row in rows[:120]:
            value = (row.get("values") or {}).get(key)
            text = str(value or "").strip()
            if not text or len(text) > 48 or text in seen:
                continue
            seen.add(text)
            values.append(text)
            if len(values) > 3:
                break
        if not values or len(values) > 3:
            continue
        items.append({"label": _column_label(col, key), "value": ", ".join(values)})
        if len(items) >= 3:
            break
    return items


def _metric_points(rows: list[dict[str, Any]], *, timestamp_key: str | None, metric_key: str | None) -> list[dict[str, Any]]:
    if not metric_key:
        return []
    points: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        values = row.get("values") or {}
        raw_value = values.get(metric_key)
        if not isinstance(raw_value, (int, float)) or isinstance(raw_value, bool):
            continue
        point_date = _parse_dataset_date(values.get(timestamp_key or "")) if timestamp_key else None
        points.append(
            {
                "date": point_date,
                "date_label": point_date.isoformat() if point_date else str(values.get(timestamp_key or "") or row.get("row_key") or ""),
                "value": float(raw_value),
                "row": row,
                "index": index,
            }
        )
    return sorted(points, key=lambda point: (point["date"] or date.min, point["index"]))


def _same_month_prior_year(points: list[dict[str, Any]], latest: dict[str, Any] | None) -> dict[str, Any] | None:
    latest_date = latest.get("date") if latest else None
    if not isinstance(latest_date, date):
        return None
    target_year = latest_date.year - 1
    for point in reversed(points):
        point_date = point.get("date")
        if isinstance(point_date, date) and point_date.year == target_year and point_date.month == latest_date.month:
            return point
    return None


def _delta_parts(latest: dict[str, Any] | None, baseline: dict[str, Any] | None) -> tuple[float | None, float | None]:
    if not latest or not baseline:
        return None, None
    latest_value = latest.get("value")
    baseline_value = baseline.get("value")
    if not isinstance(latest_value, (int, float)) or not isinstance(baseline_value, (int, float)):
        return None, None
    delta = float(latest_value) - float(baseline_value)
    pct = None if float(baseline_value) == 0 else (delta / abs(float(baseline_value))) * 100.0
    return delta, pct


def _format_delta(delta: float | None, pct: float | None) -> str:
    if delta is None:
        return "—"
    if pct is None:
        return str(_format_compact_number(delta))
    return f"{_format_compact_number(delta)} ({pct:+.1f}%)"


def _row_text_value(row: dict[str, Any], *keys: str, extra: dict[str, Any] | None = None) -> str:
    values = row.get("values") or {}
    sources = (values, extra or {})
    for key in keys:
        for source in sources:
            value = source.get(key)
            if value in (None, "") or isinstance(value, bool):
                continue
            text = str(value).strip()
            if text:
                return text
    return ""


def _trade_mirror_status(group_rows: list[dict[str, Any]], *, partner: str, mirror_present: bool) -> str:
    partner_key = str(partner or "").strip().lower()
    if partner_key in {"", "world", "global", "all", "total"}:
        return "World aggregate"
    mirror_keys = {"mirror_value", "mirror_reporter_iso", "mirror_partner_iso", "mirror_flow"}
    for row in group_rows:
        values = row.get("values") or {}
        if any(str(values.get(key) or "").strip() for key in mirror_keys):
            return "Present"
    return "Schema empty" if mirror_present else "Needed"


def _trade_reverse_flow(flow: str) -> str:
    key = str(flow or "").strip().lower()
    mapping = {
        "export": "import",
        "exports": "imports",
        "import": "export",
        "imports": "exports",
        "re-export": "re-import",
        "re-exports": "re-imports",
        "reimport": "reexport",
        "reimports": "reexports",
        "outbound": "inbound",
        "inbound": "outbound",
    }
    return mapping.get(key, key)


def _trade_reverse_group_key(group_key: tuple[str, str, str, str, str]) -> tuple[str, str, str, str, str] | None:
    reporter, partner, flow, hs_code, theme = group_key
    reporter_v = str(reporter or "").strip()
    partner_v = str(partner or "").strip()
    partner_key = partner_v.lower()
    if not reporter_v or not partner_v or partner_key in {"world", "global", "all", "total"}:
        return None
    return (partner_v, reporter_v, _trade_reverse_flow(flow), hs_code, theme)


def _trade_group_mirror_status(
    groups: dict[tuple[str, str, str, str, str], list[dict[str, Any]]],
    group_key: tuple[str, str, str, str, str],
    *,
    mirror_present: bool,
) -> str:
    status = _trade_mirror_status(groups.get(group_key, []), partner=group_key[1], mirror_present=mirror_present)
    if status == "Present":
        return status
    reverse_key = _trade_reverse_group_key(group_key)
    if reverse_key is not None and reverse_key in groups:
        return "Present"
    return status


def _trade_group_rows(dataset: Dataset, rows: list[dict[str, Any]]) -> dict[tuple[str, str, str, str, str], list[dict[str, Any]]]:
    extra = dict(dataset.extra or {})
    groups: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = {}
    for row in rows:
        reporter = _row_text_value(row, "reporter_iso", "reporter", "country_iso", "country", extra=extra)
        partner = _row_text_value(row, "partner_scope", "partner_iso", "partner", "counterparty_iso", extra=extra)
        flow = _row_text_value(row, "flow", "trade_flow", extra=extra)
        hs_code = _row_text_value(row, "hs_code", "commodity_code", "code", extra=extra)
        theme = _row_text_value(row, "theme", "commodity", "product", extra=extra)
        groups.setdefault((reporter, partner, flow, hs_code, theme), []).append(row)
    return groups


def _trade_group_label(reporter: str, partner: str, flow: str, hs_code: str, theme: str) -> str:
    hs_theme = hs_code or "HS?"
    if theme:
        hs_theme = f"{hs_theme} · {theme}" if hs_code else theme
    return " / ".join(part for part in (reporter or "Reporter?", partner or "Partner?", flow or "Flow?", hs_theme) if part)


def _trade_group_filter_rules(row: dict[str, Any], group_key: tuple[str, str, str, str, str]) -> list[dict[str, Any]]:
    values = row.get("values") or {}
    dimensions = [
        (group_key[0], ("reporter_iso", "reporter", "country_iso", "country")),
        (group_key[1], ("partner_scope", "partner_iso", "partner", "counterparty_iso")),
        (group_key[2], ("flow", "trade_flow")),
        (group_key[3], ("hs_code", "commodity_code", "code")),
        (group_key[4], ("theme", "commodity", "product")),
    ]
    rules: list[dict[str, Any]] = []
    seen_columns: set[str] = set()
    for target, candidate_keys in dimensions:
        target_text = str(target or "").strip()
        if not target_text:
            continue
        for key in candidate_keys:
            if key in seen_columns:
                continue
            value_text = str(values.get(key) or "").strip()
            if value_text != target_text:
                continue
            seen_columns.add(key)
            rules.append({"column": key, "op": "eq", "value": target})
            break
    return rules


def _trade_mirror_coverage_card(
    dataset: Dataset,
    rows: list[dict[str, Any]],
    *,
    timestamp_key: str | None,
    metric_key: str | None,
    mirror_present: bool,
    example_limit: int = 3,
) -> tuple[dict[str, Any] | None, dict[str, int]]:
    if not rows or not metric_key:
        return None, {}
    groups = _trade_group_rows(dataset, rows)
    counts: dict[str, int] = {}
    examples: dict[str, list[str]] = {}
    for group_key, group_rows in groups.items():
        reporter, partner, flow, hs_code, theme = group_key
        group_sorted = _rows_sorted_by_timestamp(group_rows, timestamp_key)
        if not _metric_points(group_sorted, timestamp_key=timestamp_key, metric_key=metric_key):
            continue
        status = _trade_group_mirror_status(groups, group_key, mirror_present=mirror_present)
        counts[status] = counts.get(status, 0) + 1
        bucket = examples.setdefault(status, [])
        if len(bucket) < example_limit:
            bucket.append(_trade_group_label(reporter, partner, flow, hs_code, theme))

    if not counts:
        return None, counts
    analyst_use = {
        "Needed": "Add partner mirror flow before relying on bilateral deltas.",
        "Schema empty": "Mirror columns exist, but this group still needs populated mirror values.",
        "Present": "Mirror fields are available for cross-checking.",
        "World aggregate": "Mirror not required for total/global partner scope.",
    }
    status_order = {"Needed": 0, "Schema empty": 1, "Present": 2, "World aggregate": 3}
    table_rows = []
    for status, count in sorted(counts.items(), key=lambda item: (status_order.get(item[0], 9), item[0])):
        table_rows.append(
            {
                "row_key": status.lower().replace(" ", "_"),
                "values": {
                    "status": status,
                    "groups": count,
                    "examples": "; ".join(examples.get(status, [])) or "—",
                    "analyst_use": analyst_use.get(status, "Review before using this trade group."),
                },
            }
        )
    return (
        {
            "title": "Mirror Coverage",
            "kind": "table",
            "wide": True,
            "description": "Bilateral mirror readiness by reporter, partner, flow, and HS group.",
            "columns": [
                {"key": "status", "label": "Status"},
                {"key": "groups", "label": "Groups", "class_name": "is-numeric"},
                {"key": "examples", "label": "Examples"},
                {"key": "analyst_use", "label": "Analyst Use"},
            ],
            "rows": table_rows,
        },
        counts,
    )


def _trade_grouped_chart_cards(
    dataset: Dataset,
    columns: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    *,
    timestamp_key: str | None,
    metric_key: str | None,
    metric_column: dict[str, Any] | None,
    limit: int = 6,
) -> list[dict[str, Any]]:
    if not rows or not timestamp_key or not metric_key:
        return []
    timestamp_column = next((col for col in columns if str(col.get("key") or "") == timestamp_key), None)
    groups = _trade_group_rows(dataset, rows)
    candidates: list[dict[str, Any]] = []
    for group_key, group_rows in groups.items():
        reporter, partner, flow, hs_code, theme = group_key
        points = _metric_points(
            _rows_sorted_by_timestamp(group_rows, timestamp_key),
            timestamp_key=timestamp_key,
            metric_key=metric_key,
        )
        if not points:
            continue
        latest = points[-1]
        latest_value = latest.get("value")
        candidates.append(
            {
                "_sort_date": latest.get("date") or date.min,
                "_sort_value": abs(float(latest_value)) if isinstance(latest_value, (int, float)) and not isinstance(latest_value, bool) else 0.0,
                "name": _trade_group_label(reporter, partner, flow, hs_code, theme),
                "unit": _column_unit(metric_column),
                "display_type": "line",
                "data": [{"date": point.get("date_label") or "", "value": point.get("value")} for point in points[-240:]],
                "filters": {"rules": _trade_group_filter_rules(group_rows[0], group_key)},
            }
        )
    if len(candidates) < 2:
        return []
    candidates.sort(key=lambda item: (item["_sort_date"], item["_sort_value"], str(item.get("name") or "")), reverse=True)
    selected = candidates[: max(1, int(limit))]
    series = [
        {
            "name": str(item.get("name") or "Trade group"),
            "unit": str(item.get("unit") or ""),
            "display_type": "line",
            "data": item.get("data") or [],
        }
        for item in selected
    ]
    y_label = _column_label(metric_column, metric_key)
    x_label = _column_label(timestamp_column, timestamp_key)
    unit = _column_unit(metric_column)
    return [
        {
            "title": f"Grouped {y_label}",
            "x_key": timestamp_key,
            "y_key": metric_key,
            "x_label": x_label,
            "y_label": y_label,
            "series_label": y_label,
            "subtitle": f"{x_label} · top {len(series)} of {len(candidates)} reporter/partner/flow/HS groups",
            "unit": unit,
            "display_type": "line",
            "baseline_zero": False,
            "show_legend": True,
            "series_views": [
                {
                    "series_name": str(item.get("name") or "Trade group"),
                    "view_name": str(item.get("name") or "Trade group"),
                    "view_slug": f"grouped-{_slugify(str(item.get('name') or 'trade-group'), default='trade-group')}",
                    "filters": item.get("filters") if isinstance(item.get("filters"), dict) else {},
                    "columns": list(
                        dict.fromkeys(
                            [timestamp_key, metric_key]
                            + [
                                str(rule.get("column") or "")
                                for rule in ((item.get("filters") or {}).get("rules") or [])
                                if str(rule.get("column") or "").strip()
                            ]
                        )
                    ),
                    "sort_spec": [{"column": timestamp_key, "direction": "asc"}],
                    "display_type": "line",
                }
                for item in selected
            ],
            "context": [
                {"label": "Groups", "value": f"{len(series)}/{len(candidates)}"},
                {"label": "Metric", "value": y_label},
            ],
            "stats": _chart_stats(series[0]["data"], unit=unit),
            "series": series,
        }
    ]


def _trade_country_hs_lens(
    dataset: Dataset,
    columns: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    *,
    timestamp_key: str | None,
    metric_key: str | None,
    metric_column: dict[str, Any] | None,
    mirror_present: bool,
    limit: int = 12,
) -> tuple[dict[str, Any] | None, int]:
    if not rows or not metric_key:
        return None, 0
    groups = _trade_group_rows(dataset, rows)

    summary_rows: list[dict[str, Any]] = []
    for group_key, group_rows in groups.items():
        reporter, partner, flow, hs_code, theme = group_key
        group_sorted = _rows_sorted_by_timestamp(group_rows, timestamp_key)
        points = _metric_points(group_sorted, timestamp_key=timestamp_key, metric_key=metric_key)
        if not points:
            continue
        latest = points[-1]
        prior = points[-2] if len(points) > 1 else None
        yoy = _same_month_prior_year(points, latest)
        mom_delta, mom_pct = _delta_parts(latest, prior)
        yoy_delta, yoy_pct = _delta_parts(latest, yoy)
        latest_value = latest.get("value")
        hs_theme = hs_code or "—"
        if theme:
            hs_theme = f"{hs_theme} · {theme}" if hs_theme != "—" else theme
        summary_rows.append(
            {
                "_sort_date": latest.get("date") or date.min,
                "_sort_value": abs(float(latest_value)) if isinstance(latest_value, (int, float)) and not isinstance(latest_value, bool) else 0.0,
                "row_key": "|".join(part or "-" for part in (reporter, partner, flow, hs_code, theme)),
                "values": {
                    "reporter": reporter or "—",
                    "partner": partner or "—",
                    "flow": flow or "—",
                    "hs_theme": hs_theme,
                    "period": latest.get("date_label") or "—",
                    "latest": _format_compact_number(latest_value) if latest_value is not None else "—",
                    "mom": _format_delta(mom_delta, mom_pct),
                    "yoy": _format_delta(yoy_delta, yoy_pct),
                    "periods": len(points),
                    "mirror": _trade_group_mirror_status(groups, group_key, mirror_present=mirror_present),
                },
            }
        )

    if not summary_rows:
        return None, len(groups)
    summary_rows.sort(key=lambda row: (row["_sort_date"], row["_sort_value"], str(row["values"].get("reporter") or "")), reverse=True)
    table_rows = [
        {
            "row_key": row["row_key"],
            "values": row["values"],
        }
        for row in summary_rows[:limit]
    ]
    columns_payload = [
        {"key": "reporter", "label": "Reporter"},
        {"key": "partner", "label": "Partner"},
        {"key": "flow", "label": "Flow"},
        {"key": "hs_theme", "label": "HS / Theme"},
        {"key": "period", "label": "Latest Period", "class_name": "is-date"},
        {"key": "latest", "label": _column_label(metric_column, metric_key), "class_name": "is-numeric"},
        {"key": "mom", "label": "MoM", "class_name": "is-numeric"},
        {"key": "yoy", "label": "YoY", "class_name": "is-numeric"},
        {"key": "periods", "label": "Periods", "class_name": "is-numeric"},
        {"key": "mirror", "label": "Mirror"},
    ]
    return (
        {
            "title": "Country / HS Lens",
            "kind": "table",
            "wide": True,
            "description": "Latest non-future point by reporter, partner, flow, and HS group; MoM and YoY stay within each group.",
            "columns": columns_payload,
            "rows": table_rows,
        },
        len(groups),
    )


def _chart_stats(series: list[dict[str, Any]], *, unit: str) -> dict[str, Any]:
    if not series:
        return {"count": 0, "unit": unit}
    values = [float(point["value"]) for point in series if isinstance(point.get("value"), (int, float)) and not isinstance(point.get("value"), bool)]
    latest = series[-1] if series else None
    previous = series[-2] if len(series) > 1 else None
    delta_pct = None
    if latest and previous and previous.get("value") not in (None, 0):
        try:
            delta_pct = ((float(latest["value"]) - float(previous["value"])) / abs(float(previous["value"]))) * 100
        except Exception:
            delta_pct = None
    return {
        "count": len(series),
        "unit": unit,
        "latest": latest,
        "previous": previous,
        "delta_pct": delta_pct,
        "min": min(values) if values else None,
        "max": max(values) if values else None,
        "range": f"{series[0].get('date')} to {series[-1].get('date')}" if len(series) > 1 else str(series[0].get("date") or ""),
    }


def _chart_cards(columns: list[dict[str, Any]], rows: list[dict[str, Any]], *, max_cards: int = 3) -> list[dict[str, Any]]:
    timestamp = _first_column_by_role(columns, "timestamp")
    metric_columns = _columns_by_role(columns, "metric")
    if not timestamp or not metric_columns:
        return []
    chart_cards: list[dict[str, Any]] = []
    x_key = str(timestamp.get("key") or "")
    for col in metric_columns[:max_cards]:
        y_key = str(col.get("key") or "")
        series = _series_points(rows, x_key=x_key, y_key=y_key)
        if not series:
            continue
        unit = _column_unit(col)
        x_label = _column_label(timestamp, x_key)
        y_label = _column_label(col, y_key)
        context = _chart_context_items(columns, rows, exclude={x_key, y_key})
        subtitle_parts = [x_label, f"{len(series)} observations"]
        chart_cards.append(
            {
                "title": y_label,
                "x_key": x_key,
                "y_key": y_key,
                "x_label": x_label,
                "y_label": y_label,
                "series_label": y_label,
                "subtitle": " · ".join(part for part in subtitle_parts if part),
                "unit": unit,
                "display_type": "line",
                "baseline_zero": False,
                "context": context,
                "stats": _chart_stats(series, unit=unit),
                "series": series,
            }
        )
    return chart_cards


def build_dataset_chart_cards(columns: list[dict[str, Any]], rows: list[dict[str, Any]], *, max_cards: int = 3) -> list[dict[str, Any]]:
    return _chart_cards(columns, rows, max_cards=max_cards)
