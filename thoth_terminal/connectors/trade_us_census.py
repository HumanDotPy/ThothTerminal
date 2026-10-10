from __future__ import annotations

from typing import Any

import requests

from thoth_terminal.net import DEFAULT_HEADERS
from thoth_terminal.trade import (
    build_trade_series_payload,
    build_trade_summary_document_items,
    month_range_endpoints,
    normalize_hs_code,
    normalize_trade_config,
    theme_for_hs_code,
)


_EXPORT_WORLD_URL = "https://api.census.gov/data/timeseries/intltrade/exports/hsexport"
_IMPORT_WORLD_URL = "https://api.census.gov/data/timeseries/intltrade/imports/hsimport"
_EXPORT_PARTNER_URL = "https://api.census.gov/data/timeseries/intltrade/exports/hs"
_IMPORT_PARTNER_URL = "https://api.census.gov/data/timeseries/intltrade/imports/hs"


def _response_rows(url: str, *, params: dict[str, Any]) -> list[dict[str, Any]]:
    resp = requests.get(url, params=params, headers=DEFAULT_HEADERS, timeout=45.0)
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, list) or not payload:
        return []
    header = payload[0]
    if not isinstance(header, list):
        return []
    rows: list[dict[str, Any]] = []
    for raw in payload[1:]:
        if not isinstance(raw, list):
            continue
        rows.append({str(header[idx]): raw[idx] for idx in range(min(len(header), len(raw)))})
    return rows


def fetch_trade_us_census(config: dict[str, Any]) -> dict[str, Any]:
    cfg = normalize_trade_config(config, default_reporter_iso="US", default_source_label="U.S. Census IntlTrade")
    flow = str(cfg.get("flow") or "export")
    partner_scope = str(cfg.get("partner_scope") or "world")
    lookback_periods = int(cfg.get("lookback_periods") or 12)
    start, end = month_range_endpoints(lookback_periods=lookback_periods)
    commodity_key = "E_COMMODITY" if flow == "export" else "I_COMMODITY"
    value_key = "ALL_VAL_MO" if flow == "export" else "GEN_VAL_MO"
    weight_key = "ALL_WGT_MO" if flow == "export" else "GEN_WGT_MO"
    quantity_key = str(cfg.get("quantity_field") or "").strip()
    metric_fields = {
        "value_usd": value_key,
        "weight_kg": weight_key,
        "quantity": quantity_key,
    }
    metrics = [metric for metric in (cfg.get("metrics") or []) if metric_fields.get(metric)]
    hs_codes = [code for code in (cfg.get("hs_codes") or []) if normalize_hs_code(code)]
    series_payloads: list[dict[str, Any]] = []
    for hs_code in hs_codes:
        code = normalize_hs_code(hs_code)
        if not code:
            continue
        world_mode = partner_scope.lower() == "world"
        url = (
            cfg.get("api_url")
            or (_EXPORT_WORLD_URL if (flow == "export" and world_mode) else _IMPORT_WORLD_URL if world_mode else _EXPORT_PARTNER_URL if flow == "export" else _IMPORT_PARTNER_URL)
        )
        get_fields = ["YEAR", "MONTH", commodity_key]
        get_fields.extend([field for field in metric_fields.values() if field])
        params: dict[str, Any] = {
            "get": ",".join(get_fields),
            "time": f"from {start.strftime('%Y-%m')} to {end.strftime('%Y-%m')}",
            commodity_key: code,
        }
        if world_mode:
            params["for"] = "world:*"
        else:
            params["CTY_CODE"] = partner_scope
        rows = _response_rows(str(url), params=params)
        points_by_metric: dict[str, list[dict[str, Any]]] = {metric: [] for metric in metrics}
        for row in rows:
            year = row.get("YEAR")
            month = row.get("MONTH")
            try:
                period = f"{int(year):04d}-{int(month):02d}-01"
            except Exception:
                continue
            for metric in metrics:
                field_name = metric_fields.get(metric)
                raw_val = row.get(field_name) if field_name else None
                if raw_val in (None, ""):
                    continue
                try:
                    value = float(str(raw_val).replace(",", ""))
                except Exception:
                    continue
                points_by_metric[metric].append({"period_date": period, "value": value, "source_ref": str(url)})
        for metric, points in points_by_metric.items():
            if not points:
                continue
            points.sort(key=lambda row: str(row.get("period_date") or ""))
            series_payloads.append(
                build_trade_series_payload(
                    reporter_iso="US",
                    partner_scope=("world" if world_mode else partner_scope),
                    flow=flow,
                    hs_code=code,
                    metric=metric,
                    source_label=str(cfg.get("source_label") or "U.S. Census IntlTrade"),
                    source_url=str(url),
                    hs_revision=str(cfg.get("hs_revision") or "HS"),
                    points=points,
                    theme=theme_for_hs_code(code),
                    extra={"country_adapter": "us_census"},
                )
            )
    items = build_trade_summary_document_items(source_name=str(cfg.get("source_label") or "U.S. Census IntlTrade"), cfg=cfg, series_payloads=series_payloads)
    return {"items": items, "cursor": None, "stats": {"trade_series": series_payloads, "source_kind": "trade_us_census"}}
