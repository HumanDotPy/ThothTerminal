from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

import requests

from thoth_terminal.connectors.official_common import keyword_allowed, parse_datetime_candidates, recent_date_window, stable_external_id
from thoth_terminal.net import DEFAULT_HEADERS


NSE_BASE = "https://www.nseindia.com"
NSE_CORPORATE_ANNOUNCEMENTS_PAGE = f"{NSE_BASE}/companies-listing/corporate-filings-announcements"
NSE_CORPORATE_ANNOUNCEMENTS_API = f"{NSE_BASE}/api/corporate-announcements"


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


def _normalize_nse_symbol(value: object) -> str | None:
    raw = str(value or "").strip().upper()
    if not raw:
        return None
    raw = re.sub(r"\.(NS|NSE|IN)$", "", raw)
    raw = re.sub(r"[^0-9A-Z&-]+", "", raw)
    return raw if re.fullmatch(r"[0-9A-Z][0-9A-Z&-]{0,19}", raw) else None


def _rotate(values: list[str], *, start: int, count: int) -> tuple[list[str], int]:
    if not values or count <= 0:
        return [], start
    out: list[str] = []
    idx = max(0, int(start or 0))
    for _ in range(min(count, len(values))):
        out.append(values[idx % len(values)])
        idx += 1
    return out, idx % len(values)


def _mapped(cfg: dict[str, Any], key: str, symbol: str) -> Any:
    values = cfg.get(key)
    if isinstance(values, dict):
        return values.get(symbol)
    return None


def _parse_nse_datetime(row: dict[str, Any]) -> datetime | None:
    return parse_datetime_candidates(
        row.get("sort_date") or row.get("an_dt") or row.get("dt"),
        ["%Y-%m-%d %H:%M:%S", "%d-%b-%Y %H:%M:%S", "%d%m%Y%H%M%S"],
    )


def _nse_headers() -> dict[str, str]:
    return {
        **DEFAULT_HEADERS,
        "Accept": "application/json,text/plain,*/*",
        "Referer": NSE_CORPORATE_ANNOUNCEMENTS_PAGE,
    }


def _fetch_symbol_rows(cfg: dict[str, Any], *, session: requests.Session, symbol: str) -> list[dict[str, Any]]:
    params = {"index": "equities", "symbol": symbol}
    timeout_s = float(cfg.get("timeout_s") or 30.0)
    headers = _nse_headers()
    # NSE's public API expects a browser session cookie. The page hit is cheap and
    # keeps this connector on the official rail instead of falling back to wrappers.
    session.get(NSE_CORPORATE_ANNOUNCEMENTS_PAGE, headers=headers, timeout=timeout_s)
    resp = session.get(
        f"{NSE_CORPORATE_ANNOUNCEMENTS_API}?{urlencode(params)}",
        headers=headers,
        timeout=timeout_s,
    )
    resp.raise_for_status()
    payload = resp.json()
    return payload if isinstance(payload, list) else []


def fetch_nse_corporate_announcements(
    config: dict[str, Any],
    *,
    source_cursor: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = dict(config or {})
    cursor = dict(source_cursor or {})
    limit = max(1, min(int(cfg.get("max_items_per_run") or 100), 1000))
    symbols = [_normalize_nse_symbol(value) for value in _list(cfg.get("symbols") or cfg.get("nse_symbols"))]
    symbols = [value for value in symbols if value]
    if symbols:
        selected_symbols, next_idx = _rotate(
            symbols,
            start=int(cursor.get("symbol_index") or 0),
            count=max(1, int(cfg.get("symbols_per_run") or 3)),
        )
        cursor["symbol_index"] = next_idx
    else:
        selected_symbols = [_normalize_nse_symbol(cfg.get("symbol") or cfg.get("nse_symbol"))]
        selected_symbols = [value for value in selected_symbols if value]

    start_dt, end_dt = recent_date_window(cfg, default_days=180)
    session = requests.Session()
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    errors: list[dict[str, str]] = []
    for symbol in selected_symbols:
        if len(items) >= limit:
            break
        try:
            rows = _fetch_symbol_rows(cfg, session=session, symbol=symbol)
        except Exception as exc:
            errors.append({"symbol": symbol, "error": str(exc)[:240]})
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            published_at = _parse_nse_datetime(row)
            if published_at and (published_at < start_dt or published_at > end_dt):
                continue
            desc = re.sub(r"\s+", " ", str(row.get("desc") or "").strip())
            attachment_text = re.sub(r"\s+", " ", str(row.get("attchmntText") or "").strip())
            company_name = str(row.get("sm_name") or _mapped(cfg, "company_name_by_symbol", symbol) or "").strip()
            if not keyword_allowed(cfg, desc, attachment_text, company_name, symbol):
                continue
            url = str(row.get("attchmntFile") or "").strip()
            source_url = f"{NSE_CORPORATE_ANNOUNCEMENTS_API}?{urlencode({'index': 'equities', 'symbol': symbol})}"
            seq_id = str(row.get("seq_id") or "").strip()
            external_id = seq_id or stable_external_id("nse_corporate_announcements", symbol, row.get("dt"), desc, url)
            key = external_id or url
            if key in seen:
                continue
            seen.add(key)
            title = desc or f"NSE corporate announcement - {symbol}"
            company_id = _mapped(cfg, "company_id_by_symbol", symbol)
            company_slug = _mapped(cfg, "company_slug_by_symbol", symbol)
            ticker = _mapped(cfg, "ticker_by_symbol", symbol) or f"{symbol}.NS"
            isin = str(row.get("sm_isin") or "").strip()
            normalized = "\n".join(part for part in [company_name, title, attachment_text] if part).strip()
            identifiers = [
                {
                    "id_type": "NSE_STOCK",
                    "id_value": symbol,
                    "exchange": "NSE",
                    "source_key": "symbol",
                    "source": "nse_corporate_announcements",
                    "market": "NSE",
                    "is_primary": True,
                }
            ]
            if isin:
                identifiers.append(
                    {
                        "id_type": "ISIN",
                        "id_value": isin,
                        "exchange": "NSE",
                        "source_key": "sm_isin",
                        "source": "nse_corporate_announcements",
                        "market": "NSE",
                        "is_primary": False,
                    }
                )
            items.append(
                {
                    "kind": "exchange_disclosure",
                    "external_id": external_id,
                    "url": url or NSE_CORPORATE_ANNOUNCEMENTS_PAGE,
                    "canonical_url": url or NSE_CORPORATE_ANNOUNCEMENTS_PAGE,
                    "title": title,
                    "author": company_name or "NSE",
                    "published_at": published_at,
                    "raw_text": normalized,
                    "normalized_text": normalized,
                    "extra": {
                        "connector": "nse_corporate_announcements",
                        "exchange": "NSE",
                        "jurisdiction": "IN",
                        "symbol": symbol,
                        "ticker": ticker,
                        "isin": isin or None,
                        "company_id": company_id if str(company_id or "").isdigit() else None,
                        "resolved_company_id": company_id if str(company_id or "").isdigit() else None,
                        "company_slug": company_slug or None,
                        "company_name": company_name or None,
                        "company_display_name": company_name or None,
                        "seq_id": seq_id or None,
                        "announcement_datetime": str(row.get("an_dt") or "").strip() or None,
                        "exchange_disseminated_at": str(row.get("exchdisstime") or "").strip() or None,
                        "file_size": str(row.get("attFileSize") or row.get("fileSize") or "").strip() or None,
                        "has_xbrl": bool(row.get("hasXbrl")) if row.get("hasXbrl") is not None else None,
                        "document_api_url": source_url,
                        "official_page_url": NSE_CORPORATE_ANNOUNCEMENTS_PAGE,
                        "source_identifiers": identifiers,
                    },
                }
            )
            if len(items) >= limit:
                break

    cursor["last_official_search_url"] = NSE_CORPORATE_ANNOUNCEMENTS_PAGE
    return {
        "items": items,
        "cursor": cursor,
        "stats": {
            "count": len(items),
            "source_kind": "nse_corporate_announcements",
            "selected_symbols": selected_symbols,
            "errors": errors,
            "official_search_url": NSE_CORPORATE_ANNOUNCEMENTS_PAGE,
        },
    }
