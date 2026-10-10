from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from thoth_terminal.connectors.official_common import keyword_allowed, recent_date_window, stable_external_id
from thoth_terminal.net import DEFAULT_HEADERS

ASX_BASE = "https://www.asx.com.au"
ASX_ANNOUNCEMENTS_URL = f"{ASX_BASE}/asx/v2/statistics/announcements.do"


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


def _normalize_asx_code(value: object) -> str | None:
    raw = str(value or "").strip().upper()
    if not raw:
        return None
    raw = re.sub(r"\.AX$", "", raw)
    raw = re.sub(r"[^0-9A-Z]+", "", raw)
    return raw if re.fullmatch(r"[0-9A-Z]{2,6}", raw) else None


def _rotate(values: list[str], *, start: int, count: int) -> tuple[list[str], int]:
    if not values or count <= 0:
        return [], start
    out: list[str] = []
    idx = max(0, int(start or 0))
    for _ in range(min(count, len(values))):
        out.append(values[idx % len(values)])
        idx += 1
    return out, idx % len(values)


def _mapped(cfg: dict[str, Any], key: str, stock_code: str) -> Any:
    values = cfg.get(key)
    if isinstance(values, dict):
        return values.get(stock_code)
    return None


def _parse_asx_datetime(date_text: str, time_text: str) -> datetime | None:
    raw = f"{date_text} {time_text}".strip().upper()
    for fmt in ("%d/%m/%Y %I:%M %p", "%d/%m/%y %I:%M %p"):
        try:
            return datetime.strptime(raw, fmt)
        except Exception:
            continue
    return None


def _fetch_asx_code(cfg: dict[str, Any], *, stock_code: str, limit: int) -> list[dict[str, Any]]:
    start_dt, end_dt = recent_date_window(cfg, default_days=180)
    params = {
        "by": "asxCode",
        "asxCode": stock_code,
        "timeframe": str(cfg.get("timeframe") or "D"),
        "period": str(cfg.get("period") or "M6"),
    }
    resp = requests.get(
        ASX_ANNOUNCEMENTS_URL,
        headers={**DEFAULT_HEADERS, "Accept": "text/html,*/*"},
        params=params,
        timeout=float(cfg.get("timeout_s") or 30.0),
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text or "", "html.parser")
    table = soup.find("table")
    if table is None:
        return []

    company_heading = soup.find("h2")
    heading_text = company_heading.get_text(" ", strip=True) if company_heading else ""
    company_name = _mapped(cfg, "company_name_by_stock_code", stock_code)
    company_slug = _mapped(cfg, "company_slug_by_stock_code", stock_code)
    company_id = _mapped(cfg, "company_id_by_stock_code", stock_code)
    ticker = _mapped(cfg, "ticker_by_stock_code", stock_code) or f"{stock_code}.AX"

    items: list[dict[str, Any]] = []
    for row in table.find_all("tr"):
        cols = row.find_all("td")
        if len(cols) < 3:
            continue
        date_cell_text = cols[0].get_text(" ", strip=True)
        date_match = re.search(r"\d{1,2}/\d{1,2}/\d{2,4}", date_cell_text)
        time_node = cols[0].find(class_="dates-time")
        time_text = time_node.get_text(" ", strip=True) if time_node else ""
        published_at = _parse_asx_datetime(date_match.group(0) if date_match else "", time_text)
        if published_at and (published_at < start_dt or published_at > end_dt):
            continue
        price_sensitive = bool(cols[1].get_text(" ", strip=True).strip())
        anchor = cols[2].find("a")
        if anchor is None:
            continue
        url = urljoin(ASX_BASE, str(anchor.get("href") or "").strip())
        title = anchor.get_text(" ", strip=True)
        title = re.sub(r"\s+", " ", re.sub(r"\b\d+\s+pages?\b", "", title, flags=re.IGNORECASE)).strip()
        title = re.sub(r"\s*\d+(?:\.\d+)?KB\s*$", "", title, flags=re.IGNORECASE).strip()
        if not keyword_allowed(cfg, title, company_name, heading_text, stock_code):
            continue
        ids_match = re.search(r"idsId=([0-9A-Za-z_-]+)", url)
        pages_node = anchor.find(class_="page")
        pages_match = re.search(r"\d+", pages_node.get_text(" ", strip=True) if pages_node else "")
        file_size_node = anchor.find(class_="filesize")
        normalized = "\n".join(part for part in [company_name or heading_text, title] if part).strip()
        items.append(
            {
                "kind": "exchange_disclosure",
                "external_id": ids_match.group(1) if ids_match else stable_external_id("asx_announcements", stock_code, url, title),
                "url": url,
                "canonical_url": url,
                "title": title or f"ASX announcement - {stock_code}",
                "author": str(company_name or heading_text or "ASX").strip(),
                "published_at": published_at,
                "raw_text": normalized,
                "normalized_text": normalized,
                "extra": {
                    "connector": "asx_announcements",
                    "exchange": "ASX",
                    "jurisdiction": "AU",
                    "stock_code": stock_code,
                    "ticker": ticker,
                    "company_id": company_id if str(company_id or "").isdigit() else None,
                    "resolved_company_id": company_id if str(company_id or "").isdigit() else None,
                    "company_slug": company_slug or None,
                    "company_name": company_name or None,
                    "company_display_name": company_name or None,
                    "price_sensitive": price_sensitive,
                    "announcement_id": ids_match.group(1) if ids_match else None,
                    "pages": int(pages_match.group(0)) if pages_match else None,
                    "file_size": file_size_node.get_text(" ", strip=True) if file_size_node else None,
                    "source_identifiers": [
                        {
                            "id_type": "ASX_STOCK",
                            "id_value": stock_code,
                            "exchange": "ASX",
                            "source_key": "stock_code",
                            "source": "asx_announcements",
                            "market": "ASX",
                            "is_primary": True,
                        }
                    ],
                },
            }
        )
        if len(items) >= limit:
            break
    return items


def fetch_asx_announcements(
    config: dict[str, Any],
    *,
    source_cursor: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = dict(config or {})
    cursor = dict(source_cursor or {})
    limit = max(1, min(int(cfg.get("max_items_per_run") or 100), 1000))
    stock_codes = [_normalize_asx_code(value) for value in _list(cfg.get("stock_codes") or cfg.get("asx_codes"))]
    stock_codes = [value for value in stock_codes if value]
    next_cursor = dict(cursor)
    if stock_codes:
        selected_codes, next_idx = _rotate(
            stock_codes,
            start=int(cursor.get("stock_code_index") or 0),
            count=max(1, int(cfg.get("stock_codes_per_run") or cfg.get("asx_codes_per_run") or 4)),
        )
        next_cursor["stock_code_index"] = next_idx
    else:
        selected_codes = [_normalize_asx_code(cfg.get("stock_code") or cfg.get("asx_code"))]
        selected_codes = [value for value in selected_codes if value]

    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for stock_code in selected_codes:
        if len(items) >= limit:
            break
        for item in _fetch_asx_code(cfg, stock_code=stock_code, limit=limit - len(items)):
            key = str(item.get("external_id") or item.get("canonical_url") or item.get("url") or "")
            if key in seen:
                continue
            seen.add(key)
            items.append(item)
            if len(items) >= limit:
                break
    return {
        "items": items,
        "cursor": next_cursor,
        "stats": {
            "count": len(items),
            "source_kind": "asx_announcements",
            "selected_stock_codes": selected_codes,
        },
    }
