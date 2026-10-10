from __future__ import annotations

import html
import re
from datetime import datetime
from typing import Any

import requests

from thoth_terminal.connectors.official_common import keyword_allowed, recent_date_window, stable_external_id
from thoth_terminal.net import DEFAULT_HEADERS

CNINFO_BASE = "http://www.cninfo.com.cn"
CNINFO_STATIC_BASE = "http://static.cninfo.com.cn"
CNINFO_STOCK_LIST_URL = f"{CNINFO_BASE}/new/data/szse_stock.json"
CNINFO_QUERY_URL = f"{CNINFO_BASE}/new/hisAnnouncement/query"


def _headers() -> dict[str, str]:
    return {
        **DEFAULT_HEADERS,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": CNINFO_BASE,
        "Referer": f"{CNINFO_BASE}/new/commonUrl/pageOfSearch?url=disclosure/list/search",
        "X-Requested-With": "XMLHttpRequest",
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
    return [str(item).strip() for item in raw if str(item).strip()]


def _normalize_cn_stock_code(value: object) -> str | None:
    raw = str(value or "").strip().upper()
    if not raw:
        return None
    raw = re.sub(r"\.(SS|SH|SZ|CN)$", "", raw)
    digits = re.sub(r"\D+", "", raw)
    return digits if re.fullmatch(r"[0-9]{6}", digits) else None


def _infer_column(stock_code: str) -> str:
    if stock_code.startswith(("6", "9")):
        return "sse"
    return "szse"


def _ticker(stock_code: str) -> str:
    suffix = "SS" if _infer_column(stock_code) == "sse" else "SZ"
    return f"{stock_code}.{suffix}"


def _mapped(cfg: dict[str, Any], key: str, stock_code: str) -> Any:
    values = cfg.get(key)
    if isinstance(values, dict):
        return values.get(stock_code)
    return None


def _rotate(values: list[str], *, start: int, count: int) -> tuple[list[str], int]:
    if not values or count <= 0:
        return [], start
    out: list[str] = []
    idx = max(0, int(start or 0))
    for _ in range(min(count, len(values))):
        out.append(values[idx % len(values)])
        idx += 1
    return out, idx % len(values)


def _clean_text(value: object) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _announcement_time(value: object) -> datetime | None:
    if isinstance(value, (int, float)) and value > 0:
        try:
            return datetime.fromtimestamp(float(value) / 1000.0)
        except Exception:
            return None
    raw = str(value or "").strip()
    if raw.isdigit():
        try:
            return datetime.fromtimestamp(float(raw) / 1000.0)
        except Exception:
            return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw[:19], fmt)
        except Exception:
            continue
    return None


def fetch_cninfo_stock_map(config: dict[str, Any] | None = None) -> dict[str, dict[str, str]]:
    cfg = dict(config or {})
    resp = requests.get(
        str(cfg.get("stock_list_url") or CNINFO_STOCK_LIST_URL),
        headers=_headers(),
        timeout=float(cfg.get("timeout_s") or 30.0),
    )
    resp.raise_for_status()
    payload = resp.json()
    rows = payload.get("stockList") if isinstance(payload, dict) else []
    out: dict[str, dict[str, str]] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        code = _normalize_cn_stock_code(row.get("code"))
        if not code:
            continue
        out[code] = {
            "stock_code": code,
            "org_id": str(row.get("orgId") or "").strip(),
            "sec_name": _clean_text(row.get("zwjc")),
            "category": _clean_text(row.get("category")),
            "pinyin": _clean_text(row.get("pinyin")),
            "column": _infer_column(code),
        }
    return out


def _fetch_cninfo_query(
    config: dict[str, Any],
    *,
    stock_code: str | None,
    org_id: str | None,
    column: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    cfg = dict(config or {})
    start_dt, end_dt = recent_date_window(cfg, default_days=30)
    post_data = {
        "pageNum": str(max(1, int(cfg.get("page_num") or 1))),
        "pageSize": str(max(1, min(int(cfg.get("page_size") or limit or 30), 30))),
        "column": str(column or cfg.get("column") or (_infer_column(stock_code) if stock_code else "szse")),
        "tabName": str(cfg.get("tab_name") or "fulltext"),
        "plate": str(cfg.get("plate") or ""),
        "stock": f"{stock_code},{org_id}" if stock_code and org_id else str(stock_code or cfg.get("stock") or ""),
        "searchkey": str(cfg.get("searchkey") or cfg.get("search_query") or ""),
        "secid": str(cfg.get("secid") or ""),
        "category": str(cfg.get("category") or ""),
        "trade": str(cfg.get("trade") or ""),
        "seDate": f"{start_dt.strftime('%Y-%m-%d')}~{end_dt.strftime('%Y-%m-%d')}",
        "sortName": str(cfg.get("sort_name") or ""),
        "sortType": str(cfg.get("sort_type") or ""),
        "isHLtitle": str(cfg.get("is_hl_title") or "true").lower(),
    }
    resp = requests.post(
        str(cfg.get("query_url") or CNINFO_QUERY_URL),
        headers=_headers(),
        data=post_data,
        timeout=float(cfg.get("timeout_s") or 30.0),
    )
    resp.raise_for_status()
    payload = resp.json()
    rows = payload.get("announcements") if isinstance(payload, dict) else []
    items: list[dict[str, Any]] = []
    filter_cfg = dict(cfg)
    if not bool(cfg.get("apply_include_keywords")):
        filter_cfg["include_keywords"] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        code = _normalize_cn_stock_code(row.get("secCode") or stock_code)
        if not code:
            continue
        title = _clean_text(row.get("announcementTitle") or row.get("shortTitle"))
        sec_name = _clean_text(row.get("secName") or row.get("tileSecName"))
        if not keyword_allowed(filter_cfg, code, sec_name, title, row.get("announcementTypeName")):
            continue
        published_at = _announcement_time(row.get("announcementTime") or row.get("storageTime"))
        adjunct_url = str(row.get("adjunctUrl") or "").strip().lstrip("/")
        url = f"{CNINFO_STATIC_BASE}/{adjunct_url}" if adjunct_url else f"{CNINFO_BASE}/new/disclosure/stock?stockCode={code}"
        company_id = _mapped(cfg, "company_id_by_stock_code", code)
        company_slug = _mapped(cfg, "company_slug_by_stock_code", code)
        mapped_ticker = _mapped(cfg, "ticker_by_stock_code", code)
        company_name = _mapped(cfg, "company_name_by_stock_code", code)
        org_id_v = str(row.get("orgId") or org_id or _mapped(cfg, "stock_org_id_by_stock_code", code) or "").strip() or None
        external_id = str(row.get("announcementId") or "").strip() or stable_external_id("cninfo_announcements", code, title, url)
        normalized = "\n".join(part for part in [sec_name, code, title] if part).strip()
        items.append(
            {
                "kind": "exchange_disclosure",
                "external_id": f"cninfo:{external_id}",
                "url": url,
                "canonical_url": url,
                "title": title or f"{code} CNINFO announcement",
                "author": company_name or sec_name or "CNINFO",
                "published_at": published_at,
                "raw_text": normalized,
                "normalized_text": normalized,
                "extra": {
                    "connector": "cninfo_announcements",
                    "exchange": "CNINFO",
                    "market": str(column or _infer_column(code)).upper(),
                    "country": "CN",
                    "stock_code": code,
                    "ticker": mapped_ticker or _ticker(code),
                    "company_id": company_id if str(company_id or "").isdigit() else None,
                    "resolved_company_id": company_id if str(company_id or "").isdigit() else None,
                    "company_slug": company_slug or None,
                    "company_name": company_name or sec_name or None,
                    "company_display_name": company_name or sec_name or None,
                    "sec_name": sec_name or None,
                    "org_id": org_id_v,
                    "announcement_id": external_id,
                    "announcement_type": str(row.get("announcementType") or "").strip() or None,
                    "announcement_type_name": _clean_text(row.get("announcementTypeName")) or None,
                    "column_id": str(row.get("columnId") or "").strip() or None,
                    "page_column": str(row.get("pageColumn") or "").strip() or None,
                    "adjunct_type": str(row.get("adjunctType") or "").strip() or None,
                    "adjunct_size_kb": row.get("adjunctSize"),
                    "source_identifiers": [
                        {
                            "id_type": "CN_STOCK",
                            "id_value": code,
                            "exchange": str(column or _infer_column(code)).upper(),
                            "source_key": "stock_code",
                            "source": "cninfo_announcements",
                            "market": "CNINFO",
                            "is_primary": True,
                        },
                        {
                            "id_type": "CNINFO_ORG",
                            "id_value": org_id_v,
                            "exchange": "CNINFO",
                            "source_key": "org_id",
                            "source": "cninfo_announcements",
                            "market": "CNINFO",
                            "is_primary": False,
                        },
                    ]
                    if org_id_v
                    else [
                        {
                            "id_type": "CN_STOCK",
                            "id_value": code,
                            "exchange": str(column or _infer_column(code)).upper(),
                            "source_key": "stock_code",
                            "source": "cninfo_announcements",
                            "market": "CNINFO",
                            "is_primary": True,
                        }
                    ],
                },
            }
        )
        if len(items) >= limit:
            break
    return items


def fetch_cninfo_announcements(config: dict[str, Any]) -> dict[str, Any]:
    cfg = dict(config or {})
    cursor = dict(cfg.get("_source_cursor") or {})
    limit = max(1, min(int(cfg.get("max_items_per_run") or 100), 1000))
    stock_codes = [_normalize_cn_stock_code(value) for value in _list(cfg.get("stock_codes") or cfg.get("stock_ids"))]
    stock_codes = [value for value in stock_codes if value]
    selected_stock_codes: list[str | None]
    next_cursor = dict(cursor)
    if stock_codes:
        selected_stock_codes, next_idx = _rotate(
            stock_codes,
            start=int(cursor.get("stock_code_index") or 0),
            count=max(1, int(cfg.get("stock_codes_per_run") or cfg.get("max_stock_codes_per_run") or 4)),
        )
        next_cursor["stock_code_index"] = next_idx
    else:
        selected_stock_codes = [_normalize_cn_stock_code(cfg.get("stock_code") or cfg.get("stock"))]

    stock_map: dict[str, dict[str, str]] = {}
    needs_stock_map = any(
        code and not _mapped(cfg, "stock_org_id_by_stock_code", code)
        for code in selected_stock_codes
    )
    if needs_stock_map and bool(cfg.get("fetch_stock_map_if_missing", True)):
        stock_map = fetch_cninfo_stock_map(cfg)

    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for stock_code in selected_stock_codes:
        if len(items) >= limit:
            break
        org_id = str(_mapped(cfg, "stock_org_id_by_stock_code", stock_code or "") or "").strip() or None
        column = str(_mapped(cfg, "stock_column_by_stock_code", stock_code or "") or "").strip().lower() or None
        if stock_code and stock_code in stock_map:
            org_id = org_id or stock_map[stock_code].get("org_id")
            column = column or stock_map[stock_code].get("column")
        for item in _fetch_cninfo_query(cfg, stock_code=stock_code, org_id=org_id, column=column, limit=limit - len(items)):
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
            "source_kind": "cninfo_announcements",
            "selected_stock_codes": selected_stock_codes,
        },
    }
