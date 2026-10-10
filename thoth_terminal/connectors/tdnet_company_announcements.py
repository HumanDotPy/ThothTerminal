from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from thoth_terminal.connectors.official_common import keyword_allowed, recent_date_window, stable_external_id
from thoth_terminal.net import DEFAULT_HEADERS

TDNET_BASE = "https://www.release.tdnet.info"


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


def _rotate(values: list[str], *, start: int, count: int) -> tuple[list[str], int]:
    if not values or count <= 0:
        return [], start
    out: list[str] = []
    idx = max(0, int(start or 0))
    for _ in range(min(count, len(values))):
        out.append(values[idx % len(values)])
        idx += 1
    return out, idx % len(values)


def _retry_after_seconds(response: requests.Response | None) -> int | None:
    if response is None:
        return None
    raw = str(response.headers.get("Retry-After") or "").strip()
    if not raw:
        return None
    try:
        return max(60, int(float(raw)))
    except Exception:
        return None


def _fetch_tdnet_query(cfg: dict[str, Any], *, query: str, limit: int) -> list[dict[str, Any]]:
    cfg = dict(cfg or {})
    start_dt, end_dt = recent_date_window(cfg, default_days=30)

    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    first_page = max(1, int(cfg.get("page") or 1))
    max_pages = max(1, min(int(cfg.get("max_pages_per_query") or cfg.get("pages_per_query") or 1), 25))
    previous_page_signature = ""
    for page in range(first_page, first_page + max_pages):
        form = {
            "t0": str(cfg.get("from_date") or start_dt.strftime("%Y%m%d")),
            "t1": str(cfg.get("to_date") or end_dt.strftime("%Y%m%d")),
            "q": query,
            "p": str(page),
        }
        resp = requests.post(
            f"{TDNET_BASE}/onsf/TDJFSearch_e",
            headers={**DEFAULT_HEADERS, "Referer": f"{TDNET_BASE}/index_e.html"},
            data=form,
            timeout=float(cfg.get("timeout_s") or 30.0),
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text or "", "html.parser")
        table = soup.find("table", id="maintable")
        if table is None:
            break
        rows = table.find_all("tr")
        page_signature = "|".join(row.get_text(" ", strip=True) for row in rows[:3])
        if page_signature and page_signature == previous_page_signature:
            break
        previous_page_signature = page_signature
        for row in rows:
            cols = row.find_all("td")
            if len(cols) < 5:
                continue
            published_raw = cols[0].get_text(" ", strip=True)
            code = cols[1].get_text(" ", strip=True)
            company = cols[2].get_text(" ", strip=True)
            sector = cols[3].get_text(" ", strip=True)
            anchor = cols[4].find("a")
            title = anchor.get_text(" ", strip=True) if anchor else cols[4].get_text(" ", strip=True)
            if not keyword_allowed(cfg, title, company, sector, code):
                continue
            published_at = None
            try:
                published_at = datetime.strptime(published_raw, "%Y/%m/%d %H:%M")
            except Exception:
                published_at = None
            if published_at and (published_at < start_dt or published_at > end_dt):
                continue
            url = urljoin(TDNET_BASE, str(anchor.get("href") or "").strip()) if anchor else f"{TDNET_BASE}/index_e.html"
            xbrl = None
            if len(cols) > 5:
                xbrl_anchor = cols[5].find("a")
                xbrl = urljoin(TDNET_BASE, str(xbrl_anchor.get("href") or "").strip()) if xbrl_anchor else None
            notes = cols[6].get_text(" ", strip=True) if len(cols) > 6 else ""
            external_id = stable_external_id("tdnet_company_announcements", published_raw, code, company, title)
            if external_id in seen:
                continue
            seen.add(external_id)
            normalized = "\n".join(part for part in [company, sector, title, notes] if part).strip()
            items.append(
                {
                    "kind": "exchange_disclosure",
                    "external_id": external_id,
                    "url": url,
                    "canonical_url": url,
                    "title": title,
                    "author": company or "TDnet",
                    "published_at": published_at,
                    "raw_text": normalized,
                    "normalized_text": normalized,
                    "extra": {
                        "connector": "tdnet_company_announcements",
                        "exchange": "JPX",
                        "stock_code": code or None,
                        "company_name": company or None,
                        "company_name_en": company or None,
                        "sector": sector or None,
                        "xbrl_url": xbrl,
                        "notes": notes or None,
                        "source_identifiers": [
                            {
                                "id_type": "JP_STOCK",
                                "id_value": code,
                                "exchange": "JPX",
                                "source_key": "stock_code",
                                "source": "tdnet_company_announcements",
                                "market": "JPX",
                                "is_primary": True,
                            }
                        ]
                        if code
                        else [],
                    },
                }
            )
            if len(items) >= limit:
                break
        if len(items) >= limit:
            break
        next_page_token = f"changePage({page + 1})"
        if not soup.find("a", href=lambda value: value and next_page_token in value):
            break
    return items


def fetch_tdnet_company_announcements(
    config: dict[str, Any],
    *,
    source_cursor: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = dict(config or {})
    cursor = dict(source_cursor or {})
    limit = max(1, min(int(cfg.get("max_items_per_run") or 100), 1000))
    priority_queries = _list(cfg.get("priority_queries"))
    tracked_queries = _list(cfg.get("tracked_queries"))
    selected_query_specs: list[tuple[str, str]] = []
    next_cursor = dict(cursor)
    priority_start_idx = int(cursor.get("priority_query_index") or 0)
    priority_count = max(0, int(cfg.get("priority_queries_per_run") or 0))
    if priority_queries:
        if priority_count <= 0:
            priority_count = max(1, min(len(priority_queries), int(cfg.get("queries_per_run") or cfg.get("tracked_queries_per_run") or 2)))
        priority_selected, _priority_next_idx = _rotate(
            priority_queries,
            start=priority_start_idx,
            count=priority_count,
        )
        selected_query_specs.extend(("priority", query) for query in priority_selected)
    start_idx = int(cursor.get("tracked_query_index") or 0)
    if tracked_queries:
        selected_queries, next_idx = _rotate(
            tracked_queries,
            start=start_idx,
            count=max(1, int(cfg.get("queries_per_run") or cfg.get("tracked_queries_per_run") or 2)),
        )
        selected_query_specs.extend(("tracked", query) for query in selected_queries)
    else:
        fallback_query = str(cfg.get("search_query") or cfg.get("query") or "").strip()
        if not selected_query_specs:
            selected_query_specs.append(("default", fallback_query))

    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    completed_queries = 0
    completed_by_kind = {"priority": 0, "tracked": 0, "default": 0}
    rate_limited = False
    retry_after = None
    for query_kind, query in selected_query_specs:
        if len(items) >= limit:
            break
        try:
            query_items = _fetch_tdnet_query(cfg, query=query, limit=limit - len(items))
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 429:
                rate_limited = True
                retry_after = _retry_after_seconds(exc.response)
                break
            raise
        completed_queries += 1
        completed_by_kind[query_kind] = int(completed_by_kind.get(query_kind, 0)) + 1
        for item in query_items:
            key = str(item.get("external_id") or item.get("canonical_url") or item.get("url") or "")
            if key in seen:
                continue
            seen.add(key)
            items.append(item)
            if len(items) >= limit:
                break
    if priority_queries:
        next_cursor["priority_query_index"] = (priority_start_idx + completed_by_kind["priority"]) % len(priority_queries)
    if tracked_queries:
        next_cursor["tracked_query_index"] = (start_idx + completed_by_kind["tracked"]) % len(tracked_queries)
    stats = {
        "count": len(items),
        "source_kind": "tdnet_company_announcements",
        "selected_queries": [query for _kind, query in selected_query_specs],
        "selected_priority_queries": [query for kind, query in selected_query_specs if kind == "priority"],
        "selected_tracked_queries": [query for kind, query in selected_query_specs if kind == "tracked"],
        "completed_queries": completed_queries,
        "completed_priority_queries": completed_by_kind["priority"],
        "completed_tracked_queries": completed_by_kind["tracked"],
        "rate_limited": rate_limited,
    }
    if retry_after is not None:
        stats["retry_after_seconds"] = retry_after
    return {
        "items": items,
        "cursor": next_cursor,
        "stats": stats,
    }
