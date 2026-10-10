from __future__ import annotations

import csv
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from io import StringIO
from typing import Any, Iterable

import requests
from sqlalchemy import func
from sqlalchemy.orm import Session

from thoth_terminal.dataset_service import sync_trade_dataset
from thoth_terminal.db import utcnow
from thoth_terminal.models import DataPoint, DataSeries, Source
from thoth_terminal.net import DEFAULT_HEADERS
from thoth_terminal.structured_data_service import sync_trade_customs_facts
from thoth_terminal.trade.codebook import SEMI_CORE_REPORTERS, expand_theme_codes, infer_trade_themes, normalize_hs_code, theme_for_hs_code

ALLOWED_TRADE_METRICS = {"value_usd", "weight_kg", "quantity"}
ALLOWED_TRADE_FLOWS = {"export", "import"}
TRADE_CATEGORY = "trade"
TRADE_SOURCE_KIND = "trade_customs"
TRADE_SECTION_NAME = "trade_series"


@dataclass(frozen=True)
class TradeSummaryRow:
    slug: str
    name: str
    metric: str
    reporter_iso: str
    partner_scope: str
    flow: str
    hs_code: str
    theme: str | None
    source_label: str | None
    source_url: str | None
    latest_date: str | None
    latest_value: float | None
    prior_value: float | None
    yoy_value: float | None
    delta_abs: float | None
    delta_pct: float | None
    yoy_delta_abs: float | None
    yoy_delta_pct: float | None
    unit: str


def _safe_float(value: object, *, scale: float = 1.0) -> float | None:
    if value is None:
        return None
    raw = str(value).strip().replace(",", "")
    if not raw:
        return None
    try:
        return float(raw) * float(scale)
    except Exception:
        return None


def _safe_int(value: object) -> int | None:
    try:
        return int(str(value).strip())
    except Exception:
        return None


def _month_start_from_parts(year_v: object, month_v: object) -> date | None:
    year_i = _safe_int(year_v)
    month_i = _safe_int(month_v)
    if year_i is None or month_i is None or month_i < 1 or month_i > 12:
        return None
    try:
        return date(year_i, month_i, 1)
    except Exception:
        return None


def normalize_trade_config(
    config: dict[str, Any] | None,
    *,
    default_reporter_iso: str,
    default_source_label: str,
) -> dict[str, Any]:
    cfg = dict(config or {})
    reporter_iso = str(cfg.get("reporter_iso") or default_reporter_iso or "").strip().upper() or default_reporter_iso
    partner_scope = str(cfg.get("partner_scope") or "world").strip() or "world"
    flow = str(cfg.get("flow") or "export").strip().lower() or "export"
    if flow not in ALLOWED_TRADE_FLOWS:
        raise ValueError("flow must be export or import")
    metrics_raw = cfg.get("metrics")
    if isinstance(metrics_raw, str):
        metrics = [part.strip().lower() for part in metrics_raw.split(",") if part.strip()]
    elif isinstance(metrics_raw, list):
        metrics = [str(part).strip().lower() for part in metrics_raw if str(part).strip()]
    else:
        metrics = ["value_usd"]
    metrics = [metric for metric in metrics if metric in ALLOWED_TRADE_METRICS] or ["value_usd"]
    themes_raw = cfg.get("themes")
    if isinstance(themes_raw, str):
        themes = [part.strip().lower() for part in themes_raw.split(",") if part.strip()]
    elif isinstance(themes_raw, list):
        themes = [str(part).strip().lower() for part in themes_raw if str(part).strip()]
    else:
        themes = []
    hs_codes_raw = cfg.get("hs_codes")
    if isinstance(hs_codes_raw, str):
        explicit_codes = [part.strip() for part in hs_codes_raw.split(",") if part.strip()]
    elif isinstance(hs_codes_raw, list):
        explicit_codes = [str(part).strip() for part in hs_codes_raw if str(part).strip()]
    else:
        explicit_codes = []
    hs_codes = expand_theme_codes(themes, extra_codes=explicit_codes)
    return {
        **cfg,
        "reporter_iso": reporter_iso,
        "partner_scope": partner_scope,
        "flow": flow,
        "metrics": metrics,
        "themes": themes,
        "hs_codes": hs_codes,
        "frequency": "monthly",
        "lookback_periods": max(3, min(int(cfg.get("lookback_periods") or 12), 60)),
        "publish_summary_doc": bool(cfg.get("publish_summary_doc", True)),
        "source_label": str(cfg.get("source_label") or default_source_label).strip() or default_source_label,
        "hs_revision": str(cfg.get("hs_revision") or "HS").strip() or "HS",
    }


def trade_series_slug(reporter_iso: object, flow: object, partner_scope: object, hs_code: object, metric: object) -> str:
    reporter = str(reporter_iso or "").strip().upper() or "XX"
    trade_flow = str(flow or "").strip().lower() or "export"
    partner = str(partner_scope or "world").strip().lower().replace(" ", "_")
    code = normalize_hs_code(hs_code) or "unknown"
    metric_v = str(metric or "").strip().lower() or "value_usd"
    return f"{reporter.lower()}-{trade_flow}-{partner}-{code}-{metric_v}"


def trade_series_name(
    reporter_iso: object,
    flow: object,
    partner_scope: object,
    hs_code: object,
    metric: object,
    *,
    theme: str | None = None,
) -> str:
    reporter = str(reporter_iso or "").strip().upper() or "XX"
    partner = str(partner_scope or "world").strip() or "world"
    flow_key = str(flow or "").strip().lower() or "export"
    trade_flow = "Imports" if flow_key.startswith("import") else "Exports" if flow_key.startswith("export") else flow_key.title()
    metric_key = str(metric or "").strip().lower()
    metric_v = {
        "value_usd": "USD",
        "weight_kg": "kg",
        "quantity": "units",
    }.get(metric_key, metric_key.replace("_", " ").upper() if metric_key else "value")
    hs_v = normalize_hs_code(hs_code) or str(hs_code or "").strip() or "unknown"
    theme_label = str(theme or "").replace("_", " ").title().replace(" / ", " ")
    partner_label = "" if partner.lower() == "world" else f" to {partner}" if flow_key.startswith("export") else f" from {partner}"
    if theme:
        return f"{reporter} {theme_label} {trade_flow}{partner_label} in {metric_v} (HS {hs_v})"
    return f"{reporter} {trade_flow}{partner_label} in {metric_v} (HS {hs_v})"


def metric_unit(metric: str) -> str:
    if metric == "value_usd":
        return "usd"
    if metric == "weight_kg":
        return "kg"
    return "units"


def month_range_endpoints(*, lookback_periods: int, now: datetime | None = None) -> tuple[date, date]:
    ref = (now or utcnow()).date().replace(day=1)
    start = ref
    for _ in range(max(0, int(lookback_periods or 12) - 1)):
        start = (start.replace(day=1) - timedelta(days=1)).replace(day=1)
    return start, ref


def normalize_json_path(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(part).strip() for part in value if str(part).strip()]
    if isinstance(value, str) and value.strip():
        return [part.strip() for part in value.split(".") if part.strip()]
    return []


def extract_items_by_path(payload: object, path: Iterable[str] | None = None) -> list[dict[str, Any]]:
    cur = payload
    for part in path or []:
        cur = cur.get(part) if isinstance(cur, dict) else None
        if cur is None:
            return []
    if isinstance(cur, list):
        return [row for row in cur if isinstance(row, dict)]
    if isinstance(cur, dict):
        if isinstance(cur.get("item"), list):
            return [row for row in cur.get("item") if isinstance(row, dict)]
        if isinstance(cur.get("item"), dict):
            return [cur.get("item")]  # type: ignore[list-item]
        return [cur]
    return []


def fetch_tabular_url(
    *,
    url: str,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout_s: float = 40.0,
) -> tuple[str, str | None]:
    resp = requests.get(
        url,
        params=params or None,
        headers={**DEFAULT_HEADERS, **(headers or {})},
        timeout=timeout_s,
    )
    resp.raise_for_status()
    return resp.text, resp.headers.get("content-type")


def parse_tabular_payload(
    text: str,
    *,
    content_type: str | None = None,
    response_format: str | None = None,
    json_items_path: Iterable[str] | None = None,
    csv_delimiter: str = ",",
) -> list[dict[str, Any]]:
    fmt = str(response_format or "").strip().lower()
    content = str(content_type or "").lower()
    if fmt == "json" or "json" in content or text.lstrip().startswith(("{", "[")):
        payload = json.loads(text)
        path = normalize_json_path(json_items_path)
        if path:
            return extract_items_by_path(payload, path=path)
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
        if isinstance(payload, dict):
            for candidate in ("result", "results", "data", "items", "response"):
                rows = extract_items_by_path(payload, path=[candidate])
                if rows:
                    return rows
            return [payload]
        return []
    reader = csv.DictReader(StringIO(text), delimiter=csv_delimiter)
    return [dict(row) for row in reader if isinstance(row, dict)]


def resolve_taiwan_dataset_url(dataset_id: object, *, preferred_format: str = "CSV") -> str | None:
    dataset = str(dataset_id or "").strip()
    if not dataset:
        return None
    text, _content_type = fetch_tabular_url(url=f"https://data.gov.tw/api/v2/rest/dataset/{dataset}", timeout_s=25.0)
    payload = json.loads(text)
    result = payload.get("result") if isinstance(payload, dict) else None
    resources = result.get("resources") if isinstance(result, dict) else None
    if not isinstance(resources, list):
        return None
    preferred = str(preferred_format or "CSV").strip().upper()
    fallback_url = None
    for resource in resources:
        if not isinstance(resource, dict):
            continue
        download_url = str(resource.get("downloadURL") or resource.get("downloadUrl") or resource.get("resourceDownloadUrl") or "").strip()
        if not download_url:
            continue
        resource_format = str(resource.get("resourceFormat") or resource.get("format") or "").strip().upper()
        if resource_format == preferred:
            return download_url
        if fallback_url is None:
            fallback_url = download_url
    return fallback_url


def build_trade_series_payload(
    *,
    reporter_iso: str,
    partner_scope: str,
    flow: str,
    hs_code: str,
    metric: str,
    source_label: str,
    source_url: str | None,
    hs_revision: str,
    points: list[dict[str, Any]],
    theme: str | None = None,
    description: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    code = normalize_hs_code(hs_code)
    if not code:
        raise ValueError("Missing hs_code")
    theme_v = theme or theme_for_hs_code(code)
    return {
        "slug": trade_series_slug(reporter_iso, flow, partner_scope, code, metric),
        "name": trade_series_name(reporter_iso, flow, partner_scope, code, metric, theme=theme_v),
        "description": description or f"{source_label} {flow} {partner_scope} HS {code} {metric}.",
        "category": TRADE_CATEGORY,
        "unit": metric_unit(metric),
        "frequency": "monthly",
        "source_label": source_label,
        "source_url": source_url,
        "metric": metric,
        "reporter_iso": reporter_iso,
        "partner_scope": partner_scope,
        "flow": flow,
        "hs_code": code,
        "hs_revision": hs_revision,
        "theme": theme_v,
        "points": points,
        "extra": dict(extra or {}),
    }


def build_trade_series_from_rows(
    rows: list[dict[str, Any]],
    *,
    cfg: dict[str, Any],
    default_source_url: str | None = None,
) -> list[dict[str, Any]]:
    hs_field = str(cfg.get("hs_field") or "hs_code").strip()
    date_field = str(cfg.get("date_field") or cfg.get("period_field") or "").strip()
    year_field = str(cfg.get("year_field") or "year").strip()
    month_field = str(cfg.get("month_field") or "month").strip()
    value_field = str(cfg.get("value_field") or "value").strip()
    weight_field = str(cfg.get("weight_field") or "").strip()
    quantity_field = str(cfg.get("quantity_field") or "").strip()
    source_ref_field = str(cfg.get("source_ref_field") or "").strip()
    value_scale = float(cfg.get("value_scale") or 1.0)
    weight_scale = float(cfg.get("weight_scale") or 1.0)
    quantity_scale = float(cfg.get("quantity_scale") or 1.0)
    reporter_iso = str(cfg.get("reporter_iso") or "XX").strip().upper()
    partner_scope = str(cfg.get("partner_scope") or "world").strip() or "world"
    flow = str(cfg.get("flow") or "export").strip().lower() or "export"
    source_label = str(cfg.get("source_label") or reporter_iso).strip() or reporter_iso
    hs_revision = str(cfg.get("hs_revision") or "HS").strip() or "HS"
    tracked_codes = {normalize_hs_code(code) for code in (cfg.get("hs_codes") or [])}
    tracked_codes.discard(None)
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    complete_period_cutoff = _trade_complete_period_cutoff()
    for row in rows:
        if not isinstance(row, dict):
            continue
        if date_field:
            period = None
            raw = str(row.get(date_field) or "").strip()
            if raw:
                try:
                    period = date.fromisoformat((raw + "-01") if len(raw) == 7 else raw[:10]).replace(day=1)
                except Exception:
                    period = None
        else:
            period = _month_start_from_parts(row.get(year_field), row.get(month_field))
        if not period:
            continue
        if period > complete_period_cutoff:
            continue
        hs_code = normalize_hs_code(row.get(hs_field) or cfg.get("default_hs_code"))
        if not hs_code or (tracked_codes and hs_code not in tracked_codes):
            continue
        metrics = {
            "value_usd": (value_field, value_scale),
            "weight_kg": (weight_field, weight_scale),
            "quantity": (quantity_field, quantity_scale),
        }
        for metric in cfg.get("metrics") or []:
            field_name, scale = metrics.get(metric, ("", 1.0))
            if not field_name:
                continue
            value = _safe_float(row.get(field_name), scale=scale)
            if value is None:
                continue
            grouped[(hs_code, metric)].append(
                {
                    "period_date": period.isoformat(),
                    "value": value,
                    "source_ref": str(row.get(source_ref_field) or default_source_url or "").strip() or None,
                }
            )
    series: list[dict[str, Any]] = []
    for (hs_code, metric), points in grouped.items():
        points.sort(key=lambda row: str(row.get("period_date") or ""))
        while len(points) > 1 and _safe_float(points[-1].get("value")) == 0.0:
            points.pop()
        series.append(
            build_trade_series_payload(
                reporter_iso=reporter_iso,
                partner_scope=partner_scope if partner_scope != "world" else "world",
                flow=flow,
                hs_code=hs_code,
                metric=metric,
                source_label=source_label,
                source_url=default_source_url,
                hs_revision=hs_revision,
                points=points,
                theme=theme_for_hs_code(hs_code),
                extra={"ingestion_kind": TRADE_SOURCE_KIND},
            )
        )
    return series


def summarize_trade_series(series_payloads: list[dict[str, Any]], *, limit: int = 6) -> list[TradeSummaryRow]:
    rows: list[TradeSummaryRow] = []
    complete_period_cutoff = _trade_complete_period_cutoff()
    for item in series_payloads:
        if not isinstance(item, dict):
            continue
        points = []
        for row in item.get("points") or []:
            if not isinstance(row, dict):
                continue
            try:
                period = date.fromisoformat(str(row.get("period_date") or "")[:10])
            except Exception:
                continue
            if period <= complete_period_cutoff:
                points.append(row)
        points.sort(key=lambda row: str(row.get("period_date") or ""))
        while len(points) > 1 and _safe_float(points[-1].get("value")) == 0.0:
            points.pop()
        if not points:
            continue
        latest = points[-1]
        prior = points[-2] if len(points) >= 2 else None
        yoy = next(
            (
                point
                for point in points[:-1]
                if str(point.get("period_date") or "")[:7]
                == f"{int(str(latest.get('period_date') or '0000')[:4] or 0) - 1:04d}-{str(latest.get('period_date') or '')[5:7]}"
            ),
            None,
        )
        latest_value = _safe_float(latest.get("value"))
        if latest_value in (0, 0.0):
            continue
        prior_value = _safe_float(prior.get("value")) if prior else None
        yoy_value = _safe_float(yoy.get("value")) if yoy else None
        delta_abs = (latest_value - prior_value) if latest_value is not None and prior_value is not None else None
        delta_pct = None if delta_abs is None or prior_value in (None, 0) else delta_abs / float(prior_value)
        yoy_delta_abs = (latest_value - yoy_value) if latest_value is not None and yoy_value is not None else None
        yoy_delta_pct = None if yoy_delta_abs is None or yoy_value in (None, 0) else yoy_delta_abs / float(yoy_value)
        rows.append(
            TradeSummaryRow(
                slug=str(item.get("slug") or ""),
                name=str(item.get("name") or ""),
                metric=str(item.get("metric") or ""),
                reporter_iso=str(item.get("reporter_iso") or ""),
                partner_scope=str(item.get("partner_scope") or ""),
                flow=str(item.get("flow") or ""),
                hs_code=str(item.get("hs_code") or ""),
                theme=str(item.get("theme") or "").strip() or None,
                source_label=str(item.get("source_label") or "").strip() or None,
                source_url=str(item.get("source_url") or "").strip() or None,
                latest_date=str(latest.get("period_date") or "").strip() or None,
                latest_value=latest_value,
                prior_value=prior_value,
                yoy_value=yoy_value,
                delta_abs=delta_abs,
                delta_pct=delta_pct,
                yoy_delta_abs=yoy_delta_abs,
                yoy_delta_pct=yoy_delta_pct,
                unit=str(item.get("unit") or "units"),
            )
        )
    rows.sort(
        key=lambda row: (
            abs(float(row.yoy_delta_pct or 0.0)),
            abs(float(row.delta_pct or 0.0)),
            abs(float(row.delta_abs or 0.0)),
            row.slug,
        ),
        reverse=True,
    )
    return rows[: max(1, int(limit or 6))]


def build_trade_summary_document_items(
    *,
    source_name: str,
    cfg: dict[str, Any],
    series_payloads: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not cfg.get("publish_summary_doc"):
        return []
    top_rows = summarize_trade_series(series_payloads, limit=6)
    if not top_rows:
        return []
    now = utcnow()
    lines = [f"# Trade Update — {source_name}", ""]
    for row in top_rows:
        latest = f"{row.latest_value:,.0f}" if row.latest_value is not None else "n/a"
        deltas: list[str] = []
        if row.delta_pct is not None:
            deltas.append(f"MoM {row.delta_pct * 100:+.1f}%")
        if row.yoy_delta_pct is not None:
            deltas.append(f"YoY {row.yoy_delta_pct * 100:+.1f}%")
        delta = f" ({'; '.join(deltas)})" if deltas else ""
        lines.append(f"- {row.name}: {latest} {row.unit}{delta} | {row.latest_date or 'unknown period'}")
    themes = sorted({row.theme for row in top_rows if row.theme})
    title_suffix = " / ".join(theme.replace("_", " ").title() for theme in themes[:3]) if themes else "Monthly"
    return [
        {
            "kind": "document",
            "external_id": f"trade-summary:{source_name}:{cfg.get('reporter_iso')}:{cfg.get('flow')}:{now.strftime('%Y%m%d%H%M%S')}",
            "canonical_url": None,
            "url": None,
            "title": f"{source_name} Trade Update — {title_suffix}",
            "author": source_name,
            "published_at": now,
            "raw_text": "\n".join(lines).strip(),
            "normalized_text": "\n".join(lines).strip(),
            "extra": {
                "trade_summary": True,
                "trade_reporter_iso": cfg.get("reporter_iso"),
                "trade_flow": cfg.get("flow"),
                "trade_partner_scope": cfg.get("partner_scope"),
                "trade_themes": themes,
            },
        }
    ]


def upsert_trade_series_from_stats(
    session: Session,
    *,
    source: Source,
    fetch_stats: dict[str, Any] | None,
    ingestion_run_id: int | None = None,
) -> dict[str, int]:
    stats = dict(fetch_stats or {})
    raw_series = stats.get("trade_series")
    if not isinstance(raw_series, list):
        return {"series_created": 0, "series_updated": 0, "points_created": 0, "points_updated": 0}
    created_series = updated_series = created_points = updated_points = 0
    dataset_synced = 0
    facts_created = 0
    facts_updated = 0
    now = utcnow()
    for item in raw_series:
        if not isinstance(item, dict):
            continue
        slug = str(item.get("slug") or "").strip()
        if not slug:
            continue
        series = session.query(DataSeries).filter_by(slug=slug).first()
        extra = dict(series.extra or {}) if series and isinstance(series.extra, dict) else {}
        extra.update(
            {
                "source_id": int(source.id),
                "source_name": source.name,
                "source_kind": TRADE_SOURCE_KIND,
                "reporter_iso": str(item.get("reporter_iso") or ""),
                "partner_scope": str(item.get("partner_scope") or ""),
                "flow": str(item.get("flow") or ""),
                "hs_code": str(item.get("hs_code") or ""),
                "hs_revision": str(item.get("hs_revision") or ""),
                "metric": str(item.get("metric") or ""),
                "theme": item.get("theme"),
                "source_url": item.get("source_url"),
                "trade_extra": dict(item.get("extra") or {}),
            }
        )
        if series is None:
            series = DataSeries(
                slug=slug,
                name=str(item.get("name") or slug),
                description=str(item.get("description") or "").strip() or None,
                category=str(item.get("category") or TRADE_CATEGORY),
                unit=str(item.get("unit") or "units"),
                frequency=str(item.get("frequency") or "monthly"),
                source_label=str(item.get("source_label") or source.name)[:128] or source.name,
                axis_type="value",
                chart_type="line",
                color=None,
                created_at=now,
                updated_at=now,
                extra=extra,
            )
            session.add(series)
            session.flush()
            created_series += 1
        else:
            series.name = str(item.get("name") or series.name)
            series.description = str(item.get("description") or "").strip() or series.description
            series.category = str(item.get("category") or series.category or TRADE_CATEGORY)
            series.unit = str(item.get("unit") or series.unit or "units")
            series.frequency = str(item.get("frequency") or series.frequency or "monthly")
            series.source_label = str(item.get("source_label") or series.source_label or source.name)[:128]
            series.updated_at = now
            series.extra = extra
            updated_series += 1
        for point_row in item.get("points") or []:
            if not isinstance(point_row, dict):
                continue
            try:
                period_date = date.fromisoformat(str(point_row.get("period_date") or ""))
            except Exception:
                continue
            value = _safe_float(point_row.get("value"))
            if value is None:
                continue
            existing = session.query(DataPoint).filter_by(series_id=int(series.id), period_date=period_date, is_estimate=False).first()
            if existing is None:
                session.add(
                    DataPoint(
                        series_id=int(series.id),
                        period_date=period_date,
                        value=value,
                        is_estimate=False,
                        revision=0,
                        source_ref=str(point_row.get("source_ref") or item.get("source_url") or "")[:255] or None,
                        note=str(point_row.get("note") or "")[:512] or None,
                        created_at=now,
                        updated_at=now,
                    )
                )
                created_points += 1
            else:
                existing.value = value
                existing.source_ref = str(point_row.get("source_ref") or item.get("source_url") or "")[:255] or None
                existing.note = str(point_row.get("note") or "")[:512] or None
                existing.revision = int(existing.revision or 0) + 1
                existing.updated_at = now
                updated_points += 1
        try:
            dataset = sync_trade_dataset(session, source_name=source.name, item=item)
            dataset_synced += 1
            fact_stats = sync_trade_customs_facts(
                session,
                source=source,
                item=item,
                dataset_id=int(dataset.id),
                ingestion_run_id=ingestion_run_id,
            )
            facts_created += int(fact_stats.get("facts_created") or 0)
            facts_updated += int(fact_stats.get("facts_updated") or 0)
        except Exception:
            pass
    return {
        "series_created": created_series,
        "series_updated": updated_series,
        "points_created": created_points,
        "points_updated": updated_points,
        "datasets_synced": dataset_synced,
        "facts_created": facts_created,
        "facts_updated": facts_updated,
    }


def _series_extra_text(key: str) -> Any:
    return DataSeries.extra[str(key)].as_string()


def _series_filters_query(session: Session, *, reporter_iso: str | None = None, hs_code: str | None = None, theme: str | None = None, flow: str | None = None) -> Any:
    q = session.query(DataSeries).filter(DataSeries.category == TRADE_CATEGORY)
    if reporter_iso:
        q = q.filter(_series_extra_text("reporter_iso") == str(reporter_iso).upper())
    if hs_code and normalize_hs_code(hs_code):
        q = q.filter(_series_extra_text("hs_code") == normalize_hs_code(hs_code))
    if theme:
        q = q.filter(_series_extra_text("theme") == str(theme).strip().lower())
    if flow:
        q = q.filter(_series_extra_text("flow") == str(flow).strip().lower())
    return q


def _year_ago_point(points: list[DataPoint], latest_point: DataPoint | None) -> DataPoint | None:
    if latest_point is None:
        return None
    target_year = int(latest_point.period_date.year) - 1
    target_month = int(latest_point.period_date.month)
    for point in points:
        if int(point.period_date.year) == target_year and int(point.period_date.month) == target_month:
            return point
    return None


def _trade_complete_period_cutoff(*, now: datetime | None = None) -> date:
    # Monthly customs feeds should not promote the current incomplete month or future periods.
    current_month = (now or utcnow()).date().replace(day=1)
    return (current_month - timedelta(days=1)).replace(day=1)


def _future_points_for_series(session: Session, series_id: int, *, limit: int = 5, cutoff: date | None = None) -> list[DataPoint]:
    cutoff_v = cutoff or _trade_complete_period_cutoff()
    return (
        session.query(DataPoint)
        .filter(DataPoint.series_id == int(series_id))
        .filter(DataPoint.period_date > cutoff_v)
        .order_by(DataPoint.period_date.asc(), DataPoint.id.asc())
        .limit(max(1, int(limit or 5)))
        .all()
    )


def _future_point_count(session: Session, series_id: int, *, cutoff: date | None = None) -> int:
    cutoff_v = cutoff or _trade_complete_period_cutoff()
    return int(
        session.query(func.count(DataPoint.id))
        .filter(DataPoint.series_id == int(series_id))
        .filter(DataPoint.period_date > cutoff_v)
        .scalar()
        or 0
    )


def serialize_trade_series(
    series: DataSeries,
    *,
    latest_point: DataPoint | None = None,
    prior_point: DataPoint | None = None,
    yoy_point: DataPoint | None = None,
) -> dict[str, Any]:
    extra = series.extra if isinstance(series.extra, dict) else {}
    latest_value = float(latest_point.value) if latest_point else None
    prior_value = float(prior_point.value) if prior_point else None
    yoy_value = float(yoy_point.value) if yoy_point else None
    delta_abs = (latest_value - prior_value) if latest_value is not None and prior_value is not None else None
    delta_pct = (delta_abs / prior_value) if delta_abs is not None and prior_value not in (None, 0) else None
    yoy_delta_abs = (latest_value - yoy_value) if latest_value is not None and yoy_value is not None else None
    yoy_delta_pct = (yoy_delta_abs / yoy_value) if yoy_delta_abs is not None and yoy_value not in (None, 0) else None
    return {
        "id": int(series.id),
        "slug": series.slug,
        "name": series.name,
        "description": series.description,
        "category": series.category,
        "unit": series.unit,
        "frequency": series.frequency,
        "source_label": series.source_label,
        "updated_at": series.updated_at.isoformat() if series.updated_at else None,
        "reporter_iso": extra.get("reporter_iso"),
        "partner_scope": extra.get("partner_scope"),
        "flow": extra.get("flow"),
        "hs_code": extra.get("hs_code"),
        "hs_revision": extra.get("hs_revision"),
        "metric": extra.get("metric"),
        "theme": extra.get("theme"),
        "source_url": extra.get("source_url"),
        "latest_point": {
            "period_date": latest_point.period_date.isoformat(),
            "value": latest_value,
            "source_ref": latest_point.source_ref,
            "note": latest_point.note,
        }
        if latest_point
        else None,
        "prior_point": {
            "period_date": prior_point.period_date.isoformat(),
            "value": prior_value,
            "source_ref": prior_point.source_ref,
            "note": prior_point.note,
        }
        if prior_point
        else None,
        "yoy_point": {
            "period_date": yoy_point.period_date.isoformat(),
            "value": yoy_value,
            "source_ref": yoy_point.source_ref,
            "note": yoy_point.note,
        }
        if yoy_point
        else None,
        "delta_abs": delta_abs,
        "delta_pct": delta_pct,
        "yoy_delta_abs": yoy_delta_abs,
        "yoy_delta_pct": yoy_delta_pct,
    }


def latest_points_for_series(
    session: Session,
    series_id: int,
    *,
    limit: int = 2,
    include_future: bool = False,
    cutoff: date | None = None,
) -> list[DataPoint]:
    q = session.query(DataPoint).filter(DataPoint.series_id == int(series_id))
    if not include_future:
        q = q.filter(DataPoint.period_date <= (cutoff or _trade_complete_period_cutoff()))
    requested_limit = max(1, int(limit or 2))
    fetch_limit = requested_limit if include_future else min(60, requested_limit + 12)
    points = q.order_by(DataPoint.period_date.desc(), DataPoint.id.desc()).limit(fetch_limit).all()
    if not include_future:
        while len(points) > 1 and float(points[0].value) == 0.0:
            points.pop(0)
    return points[:requested_limit]


def list_trade_series_payload(
    session: Session,
    *,
    reporter_iso: str | None = None,
    hs_code: str | None = None,
    theme: str | None = None,
    flow: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    q = _series_filters_query(session, reporter_iso=reporter_iso, hs_code=hs_code, theme=theme, flow=flow).order_by(DataSeries.updated_at.desc(), DataSeries.id.desc())
    cutoff = _trade_complete_period_cutoff()
    for series in q.limit(max(1, min(int(limit or 50), 200))).all():
        points = latest_points_for_series(session, int(series.id), limit=14, cutoff=cutoff)
        latest = points[0] if points else None
        prior = points[1] if len(points) > 1 else None
        yoy = _year_ago_point(points, latest)
        row = serialize_trade_series(series, latest_point=latest, prior_point=prior, yoy_point=yoy)
        future_count = _future_point_count(session, int(series.id), cutoff=cutoff)
        if future_count:
            row["quarantined_future_points_count"] = future_count
            row["quarantined_unfinalized_points_count"] = future_count
            row["data_quality_warnings"] = [
                f"Ignored {future_count} incomplete/future monthly trade point(s) after {cutoff.isoformat()}."
            ]
        rows.append(row)
    return rows


def get_trade_series_payload(session: Session, *, slug: str | None = None, series_id: int | None = None) -> dict[str, Any] | None:
    series = session.query(DataSeries).filter_by(id=int(series_id)).first() if series_id is not None else None
    if series is None and slug:
        series = session.query(DataSeries).filter_by(slug=str(slug)).first()
    if series is None or series.category != TRADE_CATEGORY:
        return None
    cutoff = _trade_complete_period_cutoff()
    points = latest_points_for_series(session, int(series.id), limit=25, cutoff=cutoff)
    latest = points[0] if points else None
    prior = points[1] if len(points) > 1 else None
    yoy = _year_ago_point(points, latest)
    payload = serialize_trade_series(series, latest_point=latest, prior_point=prior, yoy_point=yoy)
    payload["recent_points"] = [
        {"period_date": point.period_date.isoformat(), "value": float(point.value), "source_ref": point.source_ref, "note": point.note}
        for point in points
    ]
    future_points = _future_points_for_series(session, int(series.id), limit=25, cutoff=cutoff)
    if future_points:
        payload["quarantined_future_points_count"] = len(future_points)
        payload["quarantined_unfinalized_points_count"] = len(future_points)
        payload["quarantined_future_points"] = [
            {"period_date": point.period_date.isoformat(), "value": float(point.value), "source_ref": point.source_ref, "note": point.note}
            for point in future_points
        ]
        payload["data_quality_warnings"] = [
            f"Ignored {len(future_points)} incomplete/future monthly trade point(s) after {cutoff.isoformat()}."
        ]
    return payload


def find_trade_series_by_hs_payload(session: Session, *, hs_code: str, limit: int = 50) -> list[dict[str, Any]]:
    return list_trade_series_payload(session, hs_code=hs_code, limit=limit)


def trade_latest_summary_payload(
    session: Session,
    *,
    reporter_iso: str | None = None,
    hs_code: str | None = None,
    theme: str | None = None,
    flow: str | None = None,
    limit: int = 6,
) -> dict[str, Any]:
    series_rows = list_trade_series_payload(session, reporter_iso=reporter_iso, hs_code=hs_code, theme=theme, flow=flow, limit=limit)
    summary_lines: list[str] = []
    warnings: list[str] = []
    for row in series_rows[: max(1, min(int(limit or 6), 12))]:
        for warning in row.get("data_quality_warnings") or []:
            text = str(warning or "").strip()
            if text and text not in warnings:
                warnings.append(text)
        latest = row.get("latest_point") or {}
        if not latest:
            continue
        latest_value = latest.get("value")
        delta_pct = row.get("delta_pct")
        yoy_delta_pct = row.get("yoy_delta_pct")
        metric = row.get("metric") or "value"
        if latest_value in (0, 0.0):
            warning = f"Skipped zero latest value for {row.get('slug')}: verify reporter/partner/HS coverage before using."
            if warning not in warnings:
                warnings.append(warning)
            continue
        line = f"{row.get('name')}: {latest_value} {row.get('unit')}"
        deltas: list[str] = []
        if delta_pct is not None:
            deltas.append(f"MoM {float(delta_pct) * 100:+.1f}%")
        if yoy_delta_pct is not None:
            deltas.append(f"YoY {float(yoy_delta_pct) * 100:+.1f}%")
        if deltas:
            line += f" ({'; '.join(deltas)})"
        line += f" | {latest.get('period_date')} | {metric}"
        summary_lines.append(line)
    return {"count": len(series_rows), "summary_lines": summary_lines, "series": series_rows, "data_quality_warnings": warnings}


def _usable_trade_context_row(row: dict[str, Any]) -> bool:
    latest = row.get("latest_point") if isinstance(row.get("latest_point"), dict) else None
    if not latest:
        return False
    try:
        latest_value = float(latest.get("value"))
    except Exception:
        return False
    return latest_value != 0.0


def relevant_trade_context(
    session: Session,
    *,
    text_parts: Iterable[object] | None = None,
    entity_tokens: Iterable[object] | None = None,
    reporter_isos: Iterable[object] | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    themes = infer_trade_themes(text_parts, entity_tokens=entity_tokens, limit=4)
    if not themes:
        return []
    reporters = [str(code or "").strip().upper() for code in (reporter_isos or SEMI_CORE_REPORTERS) if str(code or "").strip()]
    rows: list[dict[str, Any]] = []
    for theme in themes:
        for reporter in reporters:
            rows.extend(
                row
                for row in list_trade_series_payload(session, reporter_iso=reporter, theme=theme, limit=4)
                if _usable_trade_context_row(row)
            )
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        slug = str(row.get("slug") or "")
        if slug and slug not in seen:
            seen.add(slug)
            deduped.append(row)
    deduped.sort(key=lambda row: (abs(float(row.get("delta_pct") or 0.0)), abs(float(row.get("delta_abs") or 0.0)), str(row.get("slug") or "")), reverse=True)
    return deduped[: max(1, min(int(limit or 10), 24))]
