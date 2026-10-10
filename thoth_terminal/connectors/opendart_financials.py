from __future__ import annotations

from datetime import date, datetime
from typing import Any

import requests

from thoth_terminal.config import settings
from thoth_terminal.connectors.official_common import safe_json_dumps, stable_external_id
from thoth_terminal.net import DEFAULT_HEADERS


OPENDART_BASE = "https://engopendart.fss.or.kr/engapi"

REPORT_CODES = {
    "annual": "11011",
    "1q": "11013",
    "half": "11012",
    "2q": "11012",
    "3q": "11014",
}
REPORT_PERIODS = {
    "11011": {"fiscal_quarter": None, "period_month": 12, "period_day": 31, "period_label": "FY{year}"},
    "11013": {"fiscal_quarter": 1, "period_month": 3, "period_day": 31, "period_label": "FY{year} Q1"},
    "11012": {"fiscal_quarter": 2, "period_month": 6, "period_day": 30, "period_label": "FY{year} Q2"},
    "11014": {"fiscal_quarter": 3, "period_month": 9, "period_day": 30, "period_label": "FY{year} Q3"},
}

KNOWN_CORP_NAMES = {
    "00126380": "Samsung Electronics",
    "00164779": "SK hynix",
}


def _list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        raw = [value]
    return [str(v).strip() for v in raw if str(v).strip()]


def _years(config: dict[str, Any]) -> list[int]:
    raw = _list(config.get("years"))
    if raw:
        out: list[int] = []
        for val in raw:
            try:
                out.append(int(val))
            except Exception:
                continue
        return out
    now_year = datetime.now().year
    count = max(1, min(int(config.get("year_count") or 3), 8))
    return [now_year - idx for idx in range(count)]


def _report_codes(config: dict[str, Any]) -> list[str]:
    raw = _list(config.get("reprt_codes") or config.get("report_codes"))
    if raw:
        return [REPORT_CODES.get(v.lower(), v) for v in raw]
    return [REPORT_CODES["annual"], REPORT_CODES["3q"], REPORT_CODES["half"], REPORT_CODES["1q"]]


def _as_of_date(config: dict[str, Any]) -> date:
    raw = str(config.get("as_of_date") or "").strip()
    if raw:
        try:
            return datetime.fromisoformat(raw).date()
        except Exception:
            pass
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date()
        except Exception:
            pass
    return datetime.now().date()


def _current_report_priority(as_of: date) -> list[str]:
    if as_of.month >= 11:
        return [REPORT_CODES["3q"], REPORT_CODES["half"], REPORT_CODES["1q"]]
    if as_of.month >= 8:
        return [REPORT_CODES["half"], REPORT_CODES["1q"]]
    if as_of.month >= 5:
        return [REPORT_CODES["1q"]]
    return []


def _year_report_pairs(config: dict[str, Any]) -> list[tuple[int, str]]:
    years = _years(config)
    report_codes = _report_codes(config)
    base_pairs = [(year, report_code) for year in years for report_code in report_codes]
    if not bool(config.get("source_pack_priority_mode", True)):
        return base_pairs

    available = set(base_pairs)
    as_of = _as_of_date(config)
    preferred: list[tuple[int, str]] = []

    prior_annual = (as_of.year - 1, REPORT_CODES["annual"])
    if bool(config.get("source_pack_annual_first")) and prior_annual in available:
        preferred.append(prior_annual)

    for report_code in _current_report_priority(as_of):
        pair = (as_of.year, report_code)
        if pair in available:
            preferred.append(pair)

    if not bool(config.get("source_pack_annual_first")) and prior_annual in available:
        preferred.append(prior_annual)

    if as_of.month < 5:
        for report_code in [REPORT_CODES["3q"], REPORT_CODES["half"], REPORT_CODES["1q"]]:
            pair = (as_of.year - 1, report_code)
            if pair in available:
                preferred.append(pair)

    seen: set[tuple[int, str]] = set()
    ordered: list[tuple[int, str]] = []
    for pair in [*preferred, *base_pairs]:
        if pair in available and pair not in seen:
            seen.add(pair)
            ordered.append(pair)
    return ordered


def _period_meta(year: int, report_code: str) -> dict[str, Any]:
    spec = REPORT_PERIODS.get(str(report_code), {})
    month = int(spec.get("period_month") or 12)
    day = int(spec.get("period_day") or 31)
    try:
        period_end = date(int(year), month, day)
    except Exception:
        period_end = None
    fiscal_quarter = spec.get("fiscal_quarter")
    period_label = str(spec.get("period_label") or "FY{year}").format(year=year)
    return {
        "fiscal_year": int(year),
        "fiscal_quarter": int(fiscal_quarter) if fiscal_quarter is not None else None,
        "period_of_report": period_end.isoformat() if period_end is not None else None,
        "period_label": period_label,
        "issuer_fiscal_year_label": period_label,
        "vendor_fiscal_label": period_label,
    }


def _request(params: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
    resp = requests.get(
        f"{OPENDART_BASE}/fnlttSinglAcntAll.json",
        params=params,
        headers={**DEFAULT_HEADERS, "Accept": "application/json"},
        timeout=timeout_s,
    )
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, dict):
        return {}
    status = str(payload.get("status") or "").strip()
    if status and status not in {"000", "013"}:
        raise ValueError(f"OpenDART {status}: {payload.get('message')}")
    return payload


def fetch_opendart_financials(config: dict[str, Any], *, source_cursor: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = dict(config or {})
    cursor = dict(source_cursor or {})
    api_key = str(cfg.get("api_key") or getattr(settings, "opendart_api_key", None) or "").strip()
    if not api_key:
        raise ValueError("OpenDART financials requires THOTH_OPENDART_API_KEY or source config api_key.")
    timeout_s = float(cfg.get("timeout_s") or 30.0)
    corp_codes = _list(cfg.get("corp_codes") or cfg.get("corp_code"))
    if not corp_codes:
        raise ValueError("OpenDART financials requires corp_codes.")
    company_id_by_corp_code = cfg.get("company_id_by_corp_code") if isinstance(cfg.get("company_id_by_corp_code"), dict) else {}
    company_slug_by_corp_code = cfg.get("company_slug_by_corp_code") if isinstance(cfg.get("company_slug_by_corp_code"), dict) else {}
    ticker_by_corp_code = cfg.get("ticker_by_corp_code") if isinstance(cfg.get("ticker_by_corp_code"), dict) else {}
    company_name_by_corp_code = cfg.get("company_name_by_corp_code") if isinstance(cfg.get("company_name_by_corp_code"), dict) else {}
    fs_divs = _list(cfg.get("fs_divs") or cfg.get("fs_div")) or ["CFS", "OFS"]
    limit = max(1, min(int(cfg.get("max_items_per_run") or 100), 500))

    items: list[dict[str, Any]] = []
    requests_seen = 0
    rows_seen = 0
    year_report_pairs = _year_report_pairs(cfg)
    combos = [
        (corp_code, year, report_code, fs_div)
        for year, report_code in year_report_pairs
        for corp_code in corp_codes
        for fs_div in fs_divs
    ]
    start_idx = int(cursor.get("combo_index") or 0)
    requests_per_run = max(1, min(int(cfg.get("requests_per_run") or len(combos) or 1), max(1, len(combos) or 1)))
    selected = [combos[(start_idx + offset) % len(combos)] for offset in range(min(requests_per_run, len(combos)))] if combos else []
    next_cursor = dict(cursor)
    if combos:
        next_cursor["combo_index"] = (start_idx + len(selected)) % len(combos)
    for corp_code, year, report_code, fs_div in selected:
        if len(items) >= limit:
            break
        params = {
            "crtfc_key": api_key,
            "corp_code": corp_code,
            "bsns_year": str(year),
            "reprt_code": report_code,
            "fs_div": fs_div,
        }
        payload = _request(params, timeout_s=timeout_s)
        requests_seen += 1
        rows = payload.get("list") if isinstance(payload.get("list"), list) else []
        rows_seen += len(rows)
        if not rows:
            continue
        first = rows[0] if isinstance(rows[0], dict) else {}
        company_name = str(company_name_by_corp_code.get(corp_code) or first.get("corp_name") or KNOWN_CORP_NAMES.get(corp_code) or corp_code).strip()
        title = f"{company_name} OpenDART financial statements {year} {report_code} {fs_div}"
        text = safe_json_dumps(rows)
        extra = {
            "connector": "opendart_financials",
            "regulator": "OpenDART",
            "country": "KR",
            "section": "financials",
            "corp_code": corp_code,
            "corp_name": company_name,
            "stock_code": str(ticker_by_corp_code.get(corp_code) or "").strip() or None,
            "business_year": year,
            "report_code": report_code,
            "fs_div": fs_div,
            **_period_meta(year, report_code),
            "payload": rows,
            "source_identifiers": [
                {
                    "id_type": "DART_CORP",
                    "id_value": corp_code,
                    "source_key": "corp_code",
                    "source": "opendart_financials",
                    "market": "KR",
                    "is_primary": True,
                }
            ],
        }
        if ticker_by_corp_code.get(corp_code):
            extra["source_identifiers"].append(
                {
                    "id_type": "KR_STOCK",
                    "id_value": ticker_by_corp_code[corp_code],
                    "exchange": "KRX",
                    "source_key": "stock_code",
                    "source": "opendart_financials",
                    "market": "KRX",
                }
            )
        if company_slug_by_corp_code.get(corp_code):
            extra["company_slug"] = company_slug_by_corp_code[corp_code]
        if company_id_by_corp_code.get(corp_code) is not None:
            extra["company_id"] = company_id_by_corp_code[corp_code]
            extra["resolved_company_id"] = company_id_by_corp_code[corp_code]
            extra["identity_resolution"] = {"source": "opendart_source_config", "confidence": 0.98}
        items.append(
            {
                "kind": "securities_filing",
                "external_id": stable_external_id("opendart_financials", corp_code, year, report_code, fs_div),
                "url": f"{OPENDART_BASE}/fnlttSinglAcntAll.json",
                "canonical_url": f"{OPENDART_BASE}/fnlttSinglAcntAll.json?corp_code={corp_code}&bsns_year={year}&reprt_code={report_code}&fs_div={fs_div}",
                "title": title,
                "author": company_name,
                "published_at": None,
                "raw_text": f"{title}\n{text}",
                "normalized_text": f"{title}\n{text}",
                "extra": extra,
            }
        )
    return {
        "items": items,
        "cursor": next_cursor,
        "stats": {
            "count": len(items),
            "requests_seen": requests_seen,
            "rows_seen": rows_seen,
            "combos_total": len(combos),
            "combos_selected": len(selected),
            "source_kind": "opendart_financials",
            "source_pack_priority_mode": bool(cfg.get("source_pack_priority_mode", True)),
            "year_report_pairs": [f"{year}:{report_code}" for year, report_code in year_report_pairs],
        },
    }
