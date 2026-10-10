from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import requests

from thoth_terminal.config import settings
from thoth_terminal.trade.common import (
    build_trade_series_from_rows,
    build_trade_summary_document_items,
    fetch_tabular_url,
    normalize_trade_config,
    parse_tabular_payload,
)


USITC_DATAWEB_RUN_REPORT_URL = "https://datawebws.usitc.gov/dataweb/api/v2/report2/runReport"
USITC_DATAWEB_VALIDATE_COMMODITIES_URL = "https://datawebws.usitc.gov/dataweb/api/v2/commodity/validateCommodityNumbersOnly"
_MONTH_LABELS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}
_DATAWEB_VALUE_METRICS = {
    "import": "CONS_CUSTOMS_VALUE",
    "export": "FAS_VALUE",
}
_DATAWEB_QUANTITY_METRICS = {
    "import": "CONS_FIR_UNIT_QUANT",
    "export": "FIRST_UNIT_QUANTITY",
}


def _flatten_column_labels(column_groups: object, labels: list[str] | None = None) -> list[str]:
    out = labels if labels is not None else []
    if isinstance(column_groups, list):
        for item in column_groups:
            _flatten_column_labels(item, out)
    elif isinstance(column_groups, dict):
        if isinstance(column_groups.get("columns"), list):
            _flatten_column_labels(column_groups.get("columns"), out)
        elif column_groups.get("label") is not None:
            out.append(str(column_groups.get("label") or "").strip())
    return out


def _normalize_label(value: object) -> str:
    raw = str(value or "").strip().lower()
    return "".join(ch if ch.isalnum() else "_" for ch in raw).strip("_")


def _dataweb_rows(payload: object, *, metric_field: str = "value") -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    dto = payload.get("dto")
    tables = dto.get("tables") if isinstance(dto, dict) else None
    if not isinstance(tables, list):
        return []
    rows: list[dict[str, Any]] = []
    for table in tables:
        if not isinstance(table, dict):
            continue
        labels = _flatten_column_labels(table.get("column_groups"))
        row_groups = table.get("row_groups")
        if not isinstance(row_groups, list):
            continue
        for group in row_groups:
            data_rows = group.get("rowsNew") if isinstance(group, dict) else None
            if not isinstance(data_rows, list):
                continue
            for data_row in data_rows:
                entries = data_row.get("rowEntries") if isinstance(data_row, dict) else None
                if not isinstance(entries, list):
                    continue
                row: dict[str, Any] = {}
                for idx, entry in enumerate(entries):
                    if not isinstance(entry, dict):
                        continue
                    label = labels[idx] if idx < len(labels) else f"column_{idx + 1}"
                    row[label] = entry.get("value")
                    row[_normalize_label(label)] = entry.get("value")
                rows.extend(_canonicalize_dataweb_row(row, metric_field=metric_field))
    return rows


def _canonicalize_dataweb_row(row: dict[str, Any], *, metric_field: str = "value") -> list[dict[str, Any]]:
    out = dict(row)
    for key, value in list(row.items()):
        key_norm = _normalize_label(key)
        if "year" in key_norm and "year" not in out:
            out["year"] = value
        if "month" in key_norm and "month" not in out:
            out["month"] = value
        if ("commodity" in key_norm or "hts" in key_norm) and "hs_code" not in out:
            out["hs_code"] = value
        if ("customs_value" in key_norm or "value" == key_norm or key_norm.endswith("_value")) and "value" not in out:
            out["value"] = value
        if "quantity" in key_norm and "quantity" not in out:
            out["quantity"] = value
        if "country" in key_norm and "partner" not in out:
            out["partner"] = value
    month_fields = [(month, label) for label, month in _MONTH_LABELS.items() if label in out]
    if not month_fields:
        if "month" not in out and out.get("year"):
            out["month"] = 1
        return [out]
    expanded: list[dict[str, Any]] = []
    for month, label in month_fields:
        value = out.get(label)
        if value in (None, ""):
            continue
        point = dict(out)
        point["month"] = month
        point[metric_field] = value
        expanded.append(point)
    return expanded


def _dataweb_trade_type(flow: object) -> str:
    flow_v = str(flow or "import").strip().lower()
    return "Export" if flow_v == "export" else "Import"


def _dataweb_metric(cfg: dict[str, Any]) -> tuple[str, str]:
    flow = str(cfg.get("flow") or "import").strip().lower()
    metrics = [str(metric).strip().lower() for metric in (cfg.get("metrics") or []) if str(metric).strip()]
    metric = metrics[0] if metrics else "value_usd"
    if metric == "quantity":
        return _DATAWEB_QUANTITY_METRICS.get(flow, "CONS_FIR_UNIT_QUANT"), "quantity"
    return _DATAWEB_VALUE_METRICS.get(flow, "CONS_CUSTOMS_VALUE"), "value"


def _dataweb_years(cfg: dict[str, Any]) -> list[str]:
    raw = cfg.get("years")
    if isinstance(raw, list):
        years = [str(year).strip() for year in raw if str(year).strip()]
        if years:
            return years
    current = datetime.now(UTC).year
    lookback = int(cfg.get("lookback_periods") or 12)
    if lookback > 12:
        return [str(current - 1), str(current)]
    return [str(current - 1), str(current)]


def _dataweb_headers(api_key: str, cfg: dict[str, Any]) -> dict[str, str]:
    if not api_key:
        raise ValueError("trade_usitc_dataweb requires THOTH_USITC_DATAWEB_API_KEY or source config api_key for DataWeb runReport.")
    return {
        "Content-Type": "application/json; charset=utf-8",
        "Authorization": f"Bearer {api_key}",
        **dict(cfg.get("headers") or {}),
    }


def _validate_dataweb_commodities(cfg: dict[str, Any], api_key: str) -> list[dict[str, str]]:
    codes = [str(code).strip() for code in (cfg.get("hs_codes") or []) if str(code).strip()]
    if not codes:
        raise ValueError("trade_usitc_dataweb generated DataWeb mode requires themes or hs_codes.")
    trade_type = _dataweb_trade_type(cfg.get("flow"))
    resp = requests.post(
        USITC_DATAWEB_VALIDATE_COMMODITIES_URL,
        headers=_dataweb_headers(api_key, cfg),
        json={
            "tradeType": trade_type,
            "classificationSystem": str(cfg.get("classification_system") or "HTS").strip() or "HTS",
            "search": ",".join(codes),
            "level": None,
        },
        timeout=float(cfg.get("timeout_s") or 60.0),
        verify=bool(cfg.get("verify_ssl", True)),
    )
    resp.raise_for_status()
    options = resp.json().get("options") if isinstance(resp.json(), dict) else None
    found: dict[str, str] = {}
    if isinstance(options, list):
        for option in options:
            if not isinstance(option, dict):
                continue
            code = str(option.get("name") or "").strip()
            desc = str(option.get("value") or "").strip()
            if code:
                found[code] = desc or code
    return [{"name": code, "value": found.get(code, code)} for code in codes]


def build_dataweb_request_body(cfg: dict[str, Any], *, api_key: str) -> tuple[dict[str, Any], str]:
    metric_code, metric_field = _dataweb_metric(cfg)
    trade_type = _dataweb_trade_type(cfg.get("flow"))
    commodities = _validate_dataweb_commodities(cfg, api_key)
    codes = [item["name"] for item in commodities]
    years = _dataweb_years(cfg)
    return (
        {
            "savedQueryName": str(cfg.get("saved_query_name") or "Thoth semiconductor trade").strip(),
            "savedQueryDesc": str(cfg.get("saved_query_desc") or "Generated by Thoth Terminal").strip(),
            "isOwner": True,
            "runMonthly": False,
            "reportOptions": {
                "tradeType": trade_type,
                "classificationSystem": str(cfg.get("classification_system") or "HTS").strip() or "HTS",
            },
            "searchOptions": {
                "MiscGroup": {
                    "districts": {
                        "aggregation": str(cfg.get("districts_aggregation") or "Aggregate District"),
                        "districtGroups": {"userGroups": []},
                        "districts": [],
                        "districtsExpanded": [{"name": "All Districts", "value": "all"}],
                        "districtsSelectType": "all",
                    },
                    "importPrograms": {"aggregation": None, "importPrograms": [], "programsSelectType": "all"},
                    "extImportPrograms": {
                        "aggregation": "Aggregate CSC",
                        "extImportPrograms": [],
                        "extImportProgramsExpanded": [],
                        "programsSelectType": "all",
                    },
                    "provisionCodes": {
                        "aggregation": "Aggregate RPCODE",
                        "provisionCodesSelectType": "all",
                        "rateProvisionCodes": [],
                        "rateProvisionCodesExpanded": [],
                    },
                },
                "commodities": {
                    "aggregation": str(cfg.get("commodities_aggregation") or "Break Out Commodities"),
                    "codeDisplayFormat": "YES",
                    "commodities": codes,
                    "commoditiesExpanded": commodities,
                    "commoditiesManual": "",
                    "commodityGroups": {"systemGroups": [], "userGroups": []},
                    "commoditySelectType": "list",
                    "granularity": str(cfg.get("granularity") or "6"),
                    "groupGranularity": None,
                    "searchGranularity": None,
                },
                "componentSettings": {
                    "dataToReport": [metric_code],
                    "scale": str(cfg.get("dataweb_scale") or "1"),
                    "timeframeSelectType": str(cfg.get("timeframe_select_type") or "fullYears"),
                    "years": years,
                    "startDate": cfg.get("start_date"),
                    "endDate": cfg.get("end_date"),
                    "startMonth": cfg.get("start_month"),
                    "endMonth": cfg.get("end_month"),
                    "yearsTimeline": str(cfg.get("years_timeline") or "Monthly"),
                },
                "countries": {
                    "aggregation": str(cfg.get("countries_aggregation") or "Aggregate Countries"),
                    "countries": [],
                    "countriesExpanded": [{"name": "All Countries", "value": "all"}],
                    "countriesSelectType": "all",
                    "countryGroups": {"systemGroups": [], "userGroups": []},
                },
            },
            "sortingAndDataFormat": {
                "DataSort": {"columnOrder": [], "fullColumnOrder": [], "sortOrder": []},
                "reportCustomizations": {
                    "exportCombineTables": False,
                    "showAllSubtotal": True,
                    "subtotalRecords": "",
                    "totalRecords": str(cfg.get("total_records") or "20000"),
                    "exportRawData": False,
                },
            },
        },
        metric_field,
    )


def _fetch_dataweb_run_report(api_url: str, cfg: dict[str, Any], api_key: str) -> list[dict[str, Any]]:
    request_body = cfg.get("request_body")
    if not isinstance(request_body, dict):
        request_body, metric_field = build_dataweb_request_body(cfg, api_key=api_key)
    else:
        _, metric_field = _dataweb_metric(cfg)
    resp = requests.post(
        api_url,
        headers=_dataweb_headers(api_key, cfg),
        json=request_body,
        timeout=float(cfg.get("timeout_s") or 60.0),
        verify=bool(cfg.get("verify_ssl", True)),
    )
    resp.raise_for_status()
    rows = _dataweb_rows(resp.json(), metric_field=metric_field)
    for row in rows:
        row.setdefault("source_ref", f"USITC DataWeb runReport {datetime.now(UTC).date().isoformat()}")
    return rows


def fetch_trade_usitc_dataweb(config: dict[str, Any]) -> dict[str, Any]:
    cfg = normalize_trade_config(config, default_reporter_iso="US", default_source_label="USITC DataWeb")
    api_url = str(cfg.get("api_url") or cfg.get("data_url") or "").strip()
    if not api_url and (isinstance(cfg.get("request_body"), dict) or cfg.get("hs_codes") or cfg.get("themes")):
        api_url = USITC_DATAWEB_RUN_REPORT_URL
    if not api_url:
        raise ValueError("trade_usitc_dataweb requires api_url or data_url in source config.")
    api_key = str(cfg.get("api_key") or getattr(settings, "usitc_dataweb_api_key", None) or "").strip()
    api_key_in = str(cfg.get("api_key_in") or "query").strip().lower()
    api_key_field = str(cfg.get("api_key_field") or "api_key").strip() or "api_key"
    dataweb_mode = bool(cfg.get("request_body")) or "datawebws.usitc.gov/dataweb" in api_url
    if dataweb_mode:
        rows = _fetch_dataweb_run_report(api_url, cfg, api_key)
    else:
        params = dict(cfg.get("query_params") or {})
        headers = dict(cfg.get("headers") or {})
        if api_key:
            if api_key_in in {"bearer", "authorization"}:
                headers["Authorization"] = f"Bearer {api_key}"
            elif api_key_in == "header":
                headers[api_key_field] = api_key
            else:
                params[api_key_field] = api_key
        text, content_type = fetch_tabular_url(
            url=api_url,
            params=params,
            headers=headers,
            timeout_s=float(cfg.get("timeout_s") or 40.0),
        )
        rows = parse_tabular_payload(
            text,
            content_type=content_type,
            response_format=cfg.get("response_format"),
            json_items_path=cfg.get("json_items_path"),
            csv_delimiter=str(cfg.get("csv_delimiter") or ","),
        )
    series_payloads = build_trade_series_from_rows(rows, cfg=cfg, default_source_url=api_url)
    items = build_trade_summary_document_items(source_name=str(cfg.get("source_label") or "USITC DataWeb"), cfg=cfg, series_payloads=series_payloads)
    return {
        "items": items,
        "cursor": None,
        "stats": {"row_count": len(rows), "trade_series": series_payloads, "source_kind": "trade_usitc_dataweb"},
    }
