from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests
import urllib3

from thoth_terminal.connectors.official_common import keyword_allowed, stable_external_id
from thoth_terminal.localized_names import resolve_taiwan_company_name, resolve_taiwan_industry_name
from thoth_terminal.net import DEFAULT_HEADERS


TWSE_OPENAPI_BASE = "https://openapi.twse.com.tw/v1"
MOPS_MONTHLY_REVENUE_ENDPOINTS = {
    "listed": {
        "path": "/opendata/t187ap05_L",
        "label": "TWSE listed companies",
        "market": "listed",
    },
    "public": {
        "path": "/opendata/t187ap05_P",
        "label": "public companies",
        "market": "public",
    },
}

def _request_json(api_url: str, *, timeout_s: float) -> Any:
    try:
        resp = requests.get(
            api_url,
            headers={**DEFAULT_HEADERS, "Accept": "application/json,text/csv,*/*"},
            timeout=timeout_s,
        )
    except requests.exceptions.SSLError:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        resp = requests.get(
            api_url,
            headers={**DEFAULT_HEADERS, "Accept": "application/json,text/csv,*/*"},
            timeout=timeout_s,
            verify=False,
        )
    resp.raise_for_status()
    return resp.json()


def _roc_date(value: object) -> datetime | None:
    raw = "".join(ch for ch in str(value or "").strip() if ch.isdigit())
    if len(raw) < 7:
        return None
    try:
        return datetime(int(raw[:3]) + 1911, int(raw[3:5]), int(raw[5:7]))
    except Exception:
        return None


def _roc_month_start(value: object) -> datetime | None:
    raw = "".join(ch for ch in str(value or "").strip() if ch.isdigit())
    if len(raw) < 5:
        return None
    try:
        return datetime(int(raw[:3]) + 1911, int(raw[3:5]), 1)
    except Exception:
        return None


def _float_or_none(value: object) -> float | None:
    raw = str(value or "").strip().replace(",", "")
    if raw in {"", "-", "nan", "NaN"}:
        return None
    try:
        return float(raw)
    except Exception:
        return None


def _string_set(value: object) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        raw_items = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raw_items = [value]
    return {str(v).strip() for v in raw_items if str(v).strip()}


def _cfg_map_value(cfg: dict[str, Any] | None, key: str, code: str) -> Any:
    mapping = (cfg or {}).get(key) if isinstance(cfg, dict) else None
    if not isinstance(mapping, dict):
        return None
    return mapping.get(code) or mapping.get(str(code).strip().upper())


def _ntd_short(thousand_value: float | None) -> str:
    if thousand_value is None:
        return "n/a"
    amount = float(thousand_value) * 1000.0
    abs_amount = abs(amount)
    if abs_amount >= 1_000_000_000_000:
        return f"NT${amount / 1_000_000_000_000:.2f}T"
    if abs_amount >= 1_000_000_000:
        return f"NT${amount / 1_000_000_000:.1f}B"
    if abs_amount >= 1_000_000:
        return f"NT${amount / 1_000_000:.1f}M"
    return f"NT${amount:,.0f}"


def _pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.1f}%"


def _market_configs(cfg: dict[str, Any]) -> list[dict[str, str]]:
    if cfg.get("api_url") or cfg.get("data_url"):
        return [
            {
                "path": str(cfg.get("endpoint_path") or ""),
                "url": str(cfg.get("api_url") or cfg.get("data_url") or "").strip(),
                "label": str(cfg.get("market_label") or "MOPS monthly revenue").strip(),
                "market": str(cfg.get("market") or "custom").strip().lower() or "custom",
            }
        ]
    raw_markets = cfg.get("markets") or cfg.get("market") or ["listed"]
    if isinstance(raw_markets, str):
        raw_markets = [raw_markets]
    out: list[dict[str, str]] = []
    for market in raw_markets:
        key = str(market or "").strip().lower()
        meta = MOPS_MONTHLY_REVENUE_ENDPOINTS.get(key)
        if not meta:
            continue
        out.append(
            {
                "path": meta["path"],
                "url": f"{TWSE_OPENAPI_BASE}{meta['path']}",
                "label": meta["label"],
                "market": meta["market"],
            }
        )
    return out or [
        {
            "path": MOPS_MONTHLY_REVENUE_ENDPOINTS["listed"]["path"],
            "url": f"{TWSE_OPENAPI_BASE}{MOPS_MONTHLY_REVENUE_ENDPOINTS['listed']['path']}",
            "label": MOPS_MONTHLY_REVENUE_ENDPOINTS["listed"]["label"],
            "market": "listed",
        }
    ]


def _normalize_row(
    row: dict[str, Any],
    *,
    market: str,
    market_label: str,
    api_url: str,
    endpoint_path: str,
    config: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    code = str(row.get("公司代號") or row.get("company_code") or "").strip()
    company_name = str(row.get("公司名稱") or row.get("company_name") or "").strip()
    period_dt = _roc_month_start(row.get("資料年月") or row.get("period"))
    report_dt = _roc_date(row.get("出表日期") or row.get("report_date"))
    if not code or not company_name or period_dt is None:
        return None
    period_date = period_dt.date().isoformat()
    report_date = report_dt.date().isoformat() if report_dt else None
    revenue = _float_or_none(row.get("營業收入-當月營收"))
    prev_revenue = _float_or_none(row.get("營業收入-上月營收"))
    prior_year_revenue = _float_or_none(row.get("營業收入-去年當月營收"))
    mom_pct = _float_or_none(row.get("營業收入-上月比較增減(%)"))
    yoy_pct = _float_or_none(row.get("營業收入-去年同月增減(%)"))
    cumulative = _float_or_none(row.get("累計營業收入-當月累計營收"))
    cumulative_prior = _float_or_none(row.get("累計營業收入-去年累計營收"))
    cumulative_yoy = _float_or_none(row.get("累計營業收入-前期比較增減(%)"))
    industry = str(row.get("產業別") or "").strip()
    company_identity = resolve_taiwan_company_name(company_code=code, local_name=company_name, config=config)
    mapped_company_name = str(_cfg_map_value(config, "company_name_by_stock_code", code) or "").strip()
    mapped_company_id = _cfg_map_value(config, "company_id_by_stock_code", code)
    mapped_company_slug = str(_cfg_map_value(config, "company_slug_by_stock_code", code) or "").strip()
    mapped_ticker = str(_cfg_map_value(config, "ticker_by_stock_code", code) or "").strip()
    if mapped_company_name and not company_identity.get("company_name_en"):
        company_identity = {
            **company_identity,
            "company_name": mapped_company_name,
            "company_name_en": mapped_company_name,
            "company_aliases": list(company_identity.get("company_aliases") or []) + [mapped_company_name],
            "company_name_source": "source_pack",
        }
    if row.get("company_name_en"):
        company_identity = {
            **company_identity,
            "company_name": str(row.get("company_name_en") or "").strip(),
            "company_name_en": str(row.get("company_name_en") or "").strip(),
            "company_aliases": list(company_identity.get("company_aliases") or [])
            + [str(row.get("company_name_en") or "").strip()],
            "company_name_source": "source_row",
        }
    industry_identity = resolve_taiwan_industry_name(industry, config=config)
    if row.get("industry_en"):
        industry_identity = {
            **industry_identity,
            "industry": str(row.get("industry_en") or "").strip(),
            "industry_en": str(row.get("industry_en") or "").strip(),
            "industry_name_source": "source_row",
        }
    note = str(row.get("備註") or "").strip()
    return {
        "market": market,
        "market_label": market_label,
        "company_code": code,
        "company_name": company_name,
        "company_name_en": company_identity.get("company_name_en"),
        "company_display_name": company_identity.get("company_name") or company_name,
        "company_name_original": company_identity.get("company_name_original") or company_name,
        "company_aliases": company_identity.get("company_aliases") or [],
        "company_name_source": company_identity.get("company_name_source"),
        "industry": industry,
        "industry_en": industry_identity.get("industry_en"),
        "industry_display_name": industry_identity.get("industry") or industry,
        "industry_original": industry_identity.get("industry_original") or industry,
        "industry_name_source": industry_identity.get("industry_name_source"),
        "period_date": period_date,
        "period_roc": str(row.get("資料年月") or "").strip(),
        "report_date": report_date,
        "report_date_roc": str(row.get("出表日期") or "").strip(),
        "revenue_ntd_thousand": revenue,
        "prior_month_revenue_ntd_thousand": prev_revenue,
        "prior_year_month_revenue_ntd_thousand": prior_year_revenue,
        "mom_pct": mom_pct,
        "yoy_pct": yoy_pct,
        "cumulative_revenue_ntd_thousand": cumulative,
        "cumulative_prior_year_revenue_ntd_thousand": cumulative_prior,
        "cumulative_yoy_pct": cumulative_yoy,
        "note": note if note != "-" else "",
        "source_url": api_url,
        "endpoint_path": endpoint_path,
        "ticker": mapped_ticker or code,
        "company_id": mapped_company_id if str(mapped_company_id or "").isdigit() else None,
        "resolved_company_id": mapped_company_id if str(mapped_company_id or "").isdigit() else None,
        "company_slug": mapped_company_slug or None,
        "source_identifiers": [
            {
                "id_type": "TW_STOCK",
                "id_value": code,
                "exchange": "TWSE" if market == "listed" else market.upper(),
                "source_key": "company_code",
                "source": "taiwan_mops_monthly_revenue",
                "market": market,
                "is_primary": True,
            }
        ],
        "source_row": dict(row),
    }


def _published_at(row: dict[str, Any]) -> datetime | None:
    report_date = row.get("report_date")
    if not report_date:
        return None
    try:
        local = datetime.fromisoformat(str(report_date)).replace(hour=18, minute=0, second=0, tzinfo=ZoneInfo("Asia/Taipei"))
        return local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    except Exception:
        return None


def _passes_company_filter(row: dict[str, Any], codes: set[str]) -> bool:
    return not codes or str(row.get("company_code") or "").strip() in codes


def _doc_sort_key(row: dict[str, Any]) -> tuple[int, float, float, str]:
    industry = str(row.get("industry") or "").lower()
    semi_bonus = 1 if ("半導體" in industry or "semiconductor" in industry) else 0
    yoy = row.get("yoy_pct")
    revenue = row.get("revenue_ntd_thousand")
    return (
        semi_bonus,
        abs(float(yoy)) if isinstance(yoy, (int, float)) else -1.0,
        float(revenue) if isinstance(revenue, (int, float)) else -1.0,
        str(row.get("company_code") or ""),
    )


def fetch_taiwan_mops_monthly_revenue(config: dict[str, Any]) -> dict[str, Any]:
    cfg = dict(config or {})
    timeout_s = float(cfg.get("timeout_s") or 30.0)
    stats_company_codes = _string_set(cfg.get("stats_company_codes"))
    doc_company_codes = _string_set(cfg.get("document_company_codes") or cfg.get("company_codes"))
    max_items = max(0, min(int(cfg.get("max_items_per_run") or 60), 1000))

    rows: list[dict[str, Any]] = []
    for market_cfg in _market_configs(cfg):
        api_url = market_cfg["url"]
        payload = _request_json(api_url, timeout_s=timeout_s)
        raw_rows = payload if isinstance(payload, list) else [payload] if isinstance(payload, dict) else []
        for raw in raw_rows:
            if not isinstance(raw, dict):
                continue
            normalized = _normalize_row(
                raw,
                market=market_cfg["market"],
                market_label=market_cfg["label"],
                api_url=api_url,
                endpoint_path=market_cfg["path"],
                config=cfg,
            )
            if normalized is None:
                continue
            if not _passes_company_filter(normalized, stats_company_codes):
                continue
            rows.append(normalized)

    doc_rows = [row for row in rows if _passes_company_filter(row, doc_company_codes)]
    doc_rows = [
        row
        for row in doc_rows
        if keyword_allowed(
            cfg,
            row.get("company_code"),
            row.get("company_name"),
            row.get("company_name_en"),
            row.get("company_display_name"),
            *(row.get("company_aliases") or []),
            row.get("industry"),
            row.get("industry_en"),
            row.get("industry_display_name"),
            row.get("note"),
        )
    ]
    doc_rows.sort(key=_doc_sort_key, reverse=True)
    if max_items:
        doc_rows = doc_rows[:max_items]
    else:
        doc_rows = []

    items: list[dict[str, Any]] = []
    for row in doc_rows:
        company = str(
            row.get("company_display_name")
            or row.get("company_name_en")
            or row.get("company_name")
            or row.get("company_code")
            or "Taiwan issuer"
        )
        company_original = str(row.get("company_name_original") or row.get("company_name") or "").strip()
        code = str(row.get("company_code") or "")
        title = (
            f"{company} {row.get('period_date', '')[:7]} monthly revenue "
            f"{_ntd_short(row.get('revenue_ntd_thousand'))} "
            f"({_pct(row.get('mom_pct'))} MoM, {_pct(row.get('yoy_pct'))} YoY)"
        )
        body = "\n".join(
            part
            for part in [
                title,
                f"Company: {company} ({code})",
                f"Original company name: {company_original}" if company_original and company_original != company else "",
                (
                    "Industry: "
                    f"{row.get('industry_display_name') or row.get('industry_en') or row.get('industry') or 'unknown'}"
                ),
                (
                    f"Original industry: {row.get('industry_original') or row.get('industry')}"
                    if row.get("industry_original")
                    and row.get("industry_display_name")
                    and row.get("industry_original") != row.get("industry_display_name")
                    else ""
                ),
                f"Market: {row.get('market_label')}",
                f"Period: {row.get('period_date')}",
                f"Revenue: {_ntd_short(row.get('revenue_ntd_thousand'))} (raw official unit: NT$ thousand)",
                f"Prior month: {_ntd_short(row.get('prior_month_revenue_ntd_thousand'))}",
                f"Same month last year: {_ntd_short(row.get('prior_year_month_revenue_ntd_thousand'))}",
                f"MoM: {_pct(row.get('mom_pct'))}",
                f"YoY: {_pct(row.get('yoy_pct'))}",
                f"YTD revenue: {_ntd_short(row.get('cumulative_revenue_ntd_thousand'))}",
                f"YTD YoY: {_pct(row.get('cumulative_yoy_pct'))}",
                f"Source: {row.get('source_url')}",
                f"Note: {row.get('note')}" if row.get("note") else "",
            ]
            if part
        )
        items.append(
            {
                "kind": "exchange_disclosure",
                "external_id": stable_external_id(
                    "taiwan_mops_monthly_revenue",
                    row.get("market"),
                    row.get("period_date"),
                    code,
                ),
                "url": row.get("source_url"),
                "canonical_url": row.get("source_url"),
                "title": title,
                "author": company,
                "published_at": _published_at(row),
                "raw_text": body,
                "normalized_text": body,
                "extra": {
                    "connector": "taiwan_mops_monthly_revenue",
                    "exchange": "TWSE/MOPS",
                    "company_code": code,
                    "stock_code": code,
                    "ticker": row.get("ticker") or code,
                    "company_id": row.get("company_id"),
                    "resolved_company_id": row.get("resolved_company_id"),
                    "company_slug": row.get("company_slug"),
                    "company_name": company,
                    "company_name_original": company_original or None,
                    "company_name_en": row.get("company_name_en") or None,
                    "company_display_name": company,
                    "company_aliases": row.get("company_aliases") or [],
                    "company_name_source": row.get("company_name_source"),
                    "industry": row.get("industry_original") or row.get("industry"),
                    "industry_en": row.get("industry_en"),
                    "industry_display_name": row.get("industry_display_name"),
                    "industry_name_source": row.get("industry_name_source"),
                    "market": row.get("market"),
                    "period_date": row.get("period_date"),
                    "report_date": row.get("report_date"),
                    "currency": "TWD",
                    "unit": "NT$ thousand",
                    "revenue_ntd_thousand": row.get("revenue_ntd_thousand"),
                    "mom_pct": row.get("mom_pct"),
                    "yoy_pct": row.get("yoy_pct"),
                    "source_row": row.get("source_row"),
                    "source_identifiers": row.get("source_identifiers") or [],
                },
            }
        )

    return {
        "items": items,
        "cursor": None,
        "stats": {
            "source_kind": "taiwan_mops_monthly_revenue",
            "monthly_revenue_rows": rows,
            "row_count": len(rows),
            "document_count": len(items),
            "unit": "NT$ thousand",
            "currency": "TWD",
        },
    }
