from __future__ import annotations

from datetime import datetime
from typing import Any

import requests

from thoth_terminal.connectors.official_common import keyword_allowed, recent_date_window, safe_json_dumps, stable_external_id
from thoth_terminal.net import DEFAULT_HEADERS

PATENTSVIEW_API = "https://search.patentsview.org/api/v1/patent/"


def fetch_patentsview_search(config: dict[str, Any]) -> dict[str, Any]:
    cfg = dict(config or {})
    api_url = str(cfg.get("api_url") or PATENTSVIEW_API).strip()
    request_body = cfg.get("request_body")
    if not isinstance(request_body, dict):
        start_dt, end_dt = recent_date_window(cfg, default_days=30)
        query_text = str(cfg.get("query") or cfg.get("search_query") or "").strip()
        if not query_text:
            raise ValueError("patentsview_search requires request_body or query in source config.")
        request_body = {
            "q": {
                "_and": [
                    {"_text_any": {"patent_title": query_text}},
                    {"_gte": {"patent_date": start_dt.strftime("%Y-%m-%d")}},
                    {"_lte": {"patent_date": end_dt.strftime("%Y-%m-%d")}},
                ]
            },
            "f": cfg.get("fields") or ["patent_id", "patent_title", "patent_date", "assignee_organization"],
            "o": {"per_page": max(1, min(int(cfg.get("max_items_per_run") or 25), 100))},
        }
    resp = requests.post(
        api_url,
        json=request_body,
        headers={**DEFAULT_HEADERS, "Accept": "application/json", "Content-Type": "application/json"},
        timeout=float(cfg.get("timeout_s") or 30.0),
    )
    resp.raise_for_status()
    payload = resp.json()
    rows = []
    if isinstance(payload, dict):
        for key in ("patents", "results", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                rows = value
                break
    limit = max(1, min(int(cfg.get("max_items_per_run") or 25), 100))
    items: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = str(row.get("patent_title") or row.get("title") or "").strip()
        patent_id = str(row.get("patent_id") or row.get("publication_number") or "").strip()
        assignee = str(row.get("assignee_organization") or row.get("assignee") or "").strip()
        if not keyword_allowed(cfg, title, assignee, patent_id):
            continue
        published_at = None
        raw_date = str(row.get("patent_date") or row.get("publication_date") or "").strip()
        if raw_date:
            for fmt in ("%Y-%m-%d", "%Y%m%d"):
                try:
                    published_at = datetime.strptime(raw_date, fmt)
                    break
                except Exception:
                    continue
        normalized = "\n".join(part for part in [title, assignee, raw_date] if part).strip()
        items.append(
            {
                "kind": "patent_record",
                "external_id": patent_id or stable_external_id("patentsview_search", title, assignee, raw_date),
                "url": api_url,
                "canonical_url": api_url,
                "title": title or (patent_id or "Patent record"),
                "author": assignee or "PatentsView",
                "published_at": published_at,
                "raw_text": normalized or safe_json_dumps(row),
                "normalized_text": normalized or safe_json_dumps(row),
                "extra": {
                    "connector": "patentsview_search",
                    "source_system": "PatentsView",
                    "patent_id": patent_id or None,
                    "row": dict(row),
                },
            }
        )
        if len(items) >= limit:
            break
    return {"items": items, "cursor": None, "stats": {"count": len(items), "source_kind": "patentsview_search"}}
