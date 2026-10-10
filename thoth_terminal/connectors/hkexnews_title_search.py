from __future__ import annotations

import html
import json
import re
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

import requests

from thoth_terminal.connectors.official_common import keyword_allowed, recent_date_window, stable_external_id
from thoth_terminal.net import DEFAULT_HEADERS

HKEX_BASE = "https://www1.hkexnews.hk"
MAX_DOCUMENT_TITLE_LEN = 512


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


def _normalize_hk_stock_code(value: object) -> str | None:
    raw = str(value or "").strip().upper()
    if not raw:
        return None
    raw = raw.removesuffix(".HK")
    if re.fullmatch(r"\d{1,5}", raw):
        return raw.zfill(5)
    if re.fullmatch(r"\d+", raw):
        return None
    normalized = re.sub(r"<[^>]+>", " ", raw)
    for match in re.findall(r"(?<!\d)\d{1,5}(?!\d)", normalized):
        return match.zfill(5)
    return None


def _normalize_hkex_query_id(value: object) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    digits = "".join(ch for ch in raw if ch.isdigit())
    return digits or None


def _clean_hkex_text(value: object) -> str:
    text = html.unescape(str(value or "").strip())
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _truncate_title(value: str) -> str:
    text = str(value or "").strip()
    if len(text) <= MAX_DOCUMENT_TITLE_LEN:
        return text
    return text[: MAX_DOCUMENT_TITLE_LEN - 3].rstrip() + "..."


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
        return values.get(stock_code) or values.get(stock_code.lstrip("0"))
    return None


def _hkex_query_entries(cfg: dict[str, Any]) -> list[dict[str, str | None]]:
    query_id_by_code: dict[str, str] = {}
    for key in ("stock_query_id_by_stock_code", "hkex_stock_id_by_stock_code", "stock_id_by_stock_code"):
        values = cfg.get(key)
        if not isinstance(values, dict):
            continue
        for raw_code, raw_query_id in values.items():
            code = _normalize_hk_stock_code(raw_code)
            query_id = _normalize_hkex_query_id(raw_query_id)
            if code and query_id:
                query_id_by_code[code] = query_id

    code_by_query_id: dict[str, str] = {}
    for key in ("stock_code_by_stock_query_id", "stock_code_by_hkex_stock_id", "stock_code_by_stock_id"):
        values = cfg.get(key)
        if not isinstance(values, dict):
            continue
        for raw_query_id, raw_code in values.items():
            query_id = _normalize_hkex_query_id(raw_query_id)
            code = _normalize_hk_stock_code(raw_code)
            if query_id and code:
                code_by_query_id[query_id] = code
    for code, query_id in query_id_by_code.items():
        code_by_query_id.setdefault(query_id, code)

    entries: list[dict[str, str | None]] = []
    seen: set[tuple[str | None, str | None]] = set()

    visible_codes = [_normalize_hk_stock_code(value) for value in _list(cfg.get("stock_ids") or cfg.get("stock_codes"))]
    for code in [value for value in visible_codes if value]:
        query_id = query_id_by_code.get(code) or code
        key = (query_id, code)
        if key not in seen:
            seen.add(key)
            entries.append({"query_id": query_id, "stock_code": code})

    query_values = _list(cfg.get("stock_query_ids") or cfg.get("hkex_stock_ids"))
    single_query_id = _normalize_hkex_query_id(cfg.get("stock_id"))
    if single_query_id:
        query_values.append(single_query_id)
    for raw_query_id in query_values:
        query_id = _normalize_hkex_query_id(raw_query_id)
        if not query_id:
            continue
        code = code_by_query_id.get(query_id)
        key = (query_id, code)
        if key not in seen:
            seen.add(key)
            entries.append({"query_id": query_id, "stock_code": code})

    return entries


def _fetch_hkexnews_query(
    config: dict[str, Any],
    *,
    stock_query_id: str | None,
    stock_code_hint: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    cfg = dict(config or {})
    start_dt, end_dt = recent_date_window(cfg, default_days=30)
    params = {
        "sortDir": str(cfg.get("sort_dir") or "0"),
        "sortByOptions": str(cfg.get("sort_by") or "DateTime"),
        "category": str(cfg.get("category") or "0"),
        "market": str(cfg.get("market") or "SEHK"),
        "stockId": str(stock_query_id or cfg.get("stock_id") or ""),
        "documentType": str(cfg.get("document_type") or "-1"),
        "fromDate": str(cfg.get("from_date") or start_dt.strftime("%Y%m%d")),
        "toDate": str(cfg.get("to_date") or end_dt.strftime("%Y%m%d")),
        "title": str(cfg.get("title_query") or cfg.get("search_query") or cfg.get("query") or ""),
        "searchType": str(cfg.get("search_type") or "1"),
        "t1code": str(cfg.get("t1code") or "-2"),
        "t2Gcode": str(cfg.get("t2gcode") or "-2"),
        "t2code": str(cfg.get("t2code") or "-2"),
        "rowRange": str(max(1, min(int(limit or cfg.get("max_items_per_run") or 100), 1000))),
        "lang": str(cfg.get("lang") or "E"),
    }
    resp = requests.get(
        f"{HKEX_BASE}/search/titleSearchServlet.do",
        headers={**DEFAULT_HEADERS, "Accept": "application/json,*/*"},
        params=params,
        timeout=float(cfg.get("timeout_s") or 30.0),
    )
    resp.raise_for_status()
    payload = resp.json()
    raw_result = payload.get("result")
    rows = []
    if isinstance(raw_result, str) and raw_result.strip():
        rows = json.loads(raw_result)
    elif isinstance(raw_result, list):
        rows = raw_result
    if not isinstance(rows, list):
        rows = []

    items: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = _clean_hkex_text(row.get("TITLE"))
        stock_name = _clean_hkex_text(row.get("STOCK_NAME"))
        raw_stock_code = str(row.get("STOCK_CODE") or "").strip()
        short_text = _clean_hkex_text(row.get("SHORT_TEXT"))
        long_text = _clean_hkex_text(row.get("LONG_TEXT"))
        if not keyword_allowed(cfg, title, stock_name, raw_stock_code, stock_code_hint, short_text, long_text):
            continue
        published_at = None
        try:
            published_at = datetime.strptime(str(row.get("DATE_TIME") or "").strip(), "%d/%m/%Y %H:%M")
        except Exception:
            published_at = None
        if published_at and (published_at < start_dt or published_at > end_dt):
            continue
        file_link = str(row.get("FILE_LINK") or "").strip()
        url = urljoin(HKEX_BASE, file_link) if file_link else f"{HKEX_BASE}/search/titlesearch.xhtml?lang=en"
        stock_code = _normalize_hk_stock_code(raw_stock_code) or _normalize_hk_stock_code(stock_code_hint)
        company_id = _mapped(cfg, "company_id_by_stock_code", stock_code or "") if stock_code else None
        company_slug = _mapped(cfg, "company_slug_by_stock_code", stock_code or "") if stock_code else None
        ticker = _mapped(cfg, "ticker_by_stock_code", stock_code or "") if stock_code else None
        company_name = _mapped(cfg, "company_name_by_stock_code", stock_code or "") if stock_code else None
        normalized = "\n".join(part for part in [stock_name, title, long_text or short_text] if part).strip()
        items.append(
            {
                "kind": "exchange_disclosure",
                "external_id": str(row.get("NEWS_ID") or stable_external_id("hkexnews_title_search", url, title, stock_name)),
                "url": url,
                "canonical_url": url,
                "title": _truncate_title(title or (stock_name or "HKEXnews disclosure")),
                "author": stock_name or "HKEXnews",
                "published_at": published_at,
                "raw_text": normalized,
                "normalized_text": normalized,
                "extra": {
                    "connector": "hkexnews_title_search",
                    "exchange": "HKEX",
                    "stock_code": stock_code,
                    "hkex_stock_id": stock_query_id,
                    "ticker": ticker or (f"{stock_code.lstrip('0')}.HK" if stock_code else None),
                    "company_id": company_id if str(company_id or "").isdigit() else None,
                    "resolved_company_id": company_id if str(company_id or "").isdigit() else None,
                    "company_slug": company_slug or None,
                    "company_name": company_name or stock_name or None,
                    "company_display_name": company_name or stock_name or None,
                    "stock_name": stock_name or None,
                    "file_type": str(row.get("FILE_TYPE") or "").strip() or None,
                    "file_info": str(row.get("FILE_INFO") or "").strip() or None,
                    "headline_category": long_text or short_text or None,
                    "news_id": str(row.get("NEWS_ID") or "").strip() or None,
                    "source_identifiers": [
                        {
                            "id_type": "HK_STOCK",
                            "id_value": stock_code,
                            "exchange": "HKEX",
                            "source_key": "stock_code",
                            "source": "hkexnews_title_search",
                            "market": "HKEX",
                            "is_primary": True,
                        }
                    ]
                    if stock_code
                    else [],
                },
            }
        )
        if len(items) >= limit:
            break
    return items


def fetch_hkexnews_title_search(config: dict[str, Any]) -> dict[str, Any]:
    cfg = dict(config or {})
    cursor = dict(cfg.get("_source_cursor") or {})
    limit = max(1, min(int(cfg.get("max_items_per_run") or 100), 1000))
    stock_entries = _hkex_query_entries(cfg)
    selected_entries: list[dict[str, str | None]]
    next_cursor = dict(cursor)
    if stock_entries:
        selected_query_ids, next_idx = _rotate(
            [str(entry["query_id"]) for entry in stock_entries if entry.get("query_id")],
            start=int(cursor.get("stock_id_index") or 0),
            count=max(1, int(cfg.get("stock_ids_per_run") or cfg.get("max_stock_ids_per_run") or 4)),
        )
        next_cursor["stock_id_index"] = next_idx
        entry_by_query_id = {str(entry["query_id"]): entry for entry in stock_entries if entry.get("query_id")}
        selected_entries = [entry_by_query_id[query_id] for query_id in selected_query_ids if query_id in entry_by_query_id]
    else:
        selected_entries = [{"query_id": str(cfg.get("stock_id") or "").strip() or None, "stock_code": None}]

    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    per_stock_limit = max(
        1,
        min(
            limit,
            int(
                cfg.get("max_items_per_stock")
                or cfg.get("max_items_per_stock_id")
                or ((limit + max(1, len(selected_entries)) - 1) // max(1, len(selected_entries)))
            ),
        ),
    )
    priority_title_queries = _list(cfg.get("priority_title_queries") or cfg.get("title_queries"))
    priority_title_limit = max(
        1,
        min(
            per_stock_limit,
            int(cfg.get("priority_title_query_limit") or cfg.get("max_items_per_priority_title_query") or 5),
        ),
    )
    title_queries_used: list[str] = []

    def add_query_items(entry: dict[str, str | None], query_cfg: dict[str, Any], query_limit: int) -> None:
        if query_limit <= 0:
            return
        for item in _fetch_hkexnews_query(
            query_cfg,
            stock_query_id=entry.get("query_id"),
            stock_code_hint=entry.get("stock_code"),
            limit=min(query_limit, limit - len(items)),
        ):
            key = str(item.get("external_id") or item.get("canonical_url") or item.get("url") or "")
            if key in seen:
                continue
            seen.add(key)
            items.append(item)
            if len(items) >= limit:
                break

    for entry in selected_entries:
        if len(items) >= limit:
            break
        before_stock = len(items)
        for title_query in priority_title_queries:
            if len(items) >= limit or len(items) - before_stock >= per_stock_limit:
                break
            query_cfg = {**cfg, "title_query": title_query}
            add_query_items(entry, query_cfg, min(priority_title_limit, per_stock_limit - (len(items) - before_stock)))
            title_queries_used.append(title_query)
        if len(items) >= limit or len(items) - before_stock >= per_stock_limit:
            continue
        add_query_items(entry, cfg, per_stock_limit - (len(items) - before_stock))
    return {
        "items": items,
        "cursor": next_cursor,
        "stats": {
            "count": len(items),
            "source_kind": "hkexnews_title_search",
            "selected_stock_ids": [entry.get("stock_code") or entry.get("query_id") for entry in selected_entries],
            "selected_stock_query_ids": [entry.get("query_id") for entry in selected_entries],
            "selected_stock_codes": [entry.get("stock_code") for entry in selected_entries],
            "per_stock_limit": per_stock_limit,
            "priority_title_queries": title_queries_used,
        },
    }
