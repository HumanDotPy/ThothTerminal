from __future__ import annotations

from datetime import datetime
from typing import Any

import requests

from thoth_terminal.connectors.official_common import keyword_allowed, safe_json_dumps, stable_external_id
from thoth_terminal.net import DEFAULT_HEADERS

TED_API_BASE = "https://api.ted.europa.eu"


def _pick_notice_value(row: dict[str, Any], *candidates: str) -> str:
    for key in candidates:
        value = row.get(key)
        if value is None:
            continue
        if isinstance(value, dict):
            for sub in ("eng", "en", "value", "text"):
                if str(value.get(sub) or "").strip():
                    return str(value.get(sub)).strip()
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    for sub in ("eng", "en", "value", "text"):
                        if str(item.get(sub) or "").strip():
                            return str(item.get(sub)).strip()
                elif str(item or "").strip():
                    return str(item).strip()
        if str(value or "").strip():
            return str(value).strip()
    return ""


def fetch_eu_ted_search(config: dict[str, Any]) -> dict[str, Any]:
    cfg = dict(config or {})
    query = str(cfg.get("query") or "").strip()
    if not query:
        raise ValueError("eu_ted_search requires query in source config.")
    fields = cfg.get("fields") or ["publication-number", "FT", "notice-id"]
    if not isinstance(fields, list) or not fields:
        raise ValueError("eu_ted_search requires non-empty fields list in source config.")
    body = {
        "query": query,
        "fields": [str(v).strip() for v in fields if str(v).strip()],
        "page": max(1, int(cfg.get("page") or 1)),
        "limit": max(1, min(int(cfg.get("max_items_per_run") or cfg.get("limit") or 25), 250)),
        "scope": str(cfg.get("scope") or "ACTIVE"),
        "paginationMode": str(cfg.get("pagination_mode") or "PAGE_NUMBER"),
        "onlyLatestVersions": bool(cfg.get("only_latest_versions", True)),
    }
    if cfg.get("check_query_syntax") is not None:
        body["checkQuerySyntax"] = bool(cfg.get("check_query_syntax"))
    if cfg.get("iteration_next_token"):
        body["iterationNextToken"] = str(cfg.get("iteration_next_token")).strip()

    resp = requests.post(
        f"{TED_API_BASE}/v3/notices/search",
        json=body,
        headers={**DEFAULT_HEADERS, "Accept": "application/json", "Content-Type": "application/json"},
        timeout=float(cfg.get("timeout_s") or 30.0),
    )
    resp.raise_for_status()
    payload = resp.json()
    notices = payload.get("notices") if isinstance(payload, dict) else None
    if not isinstance(notices, list):
        notices = []

    title_field = str(cfg.get("title_field") or "").strip()
    url_template = str(cfg.get("url_template") or "").strip()
    date_field = str(cfg.get("date_field") or "").strip()
    items: list[dict[str, Any]] = []
    for row in notices:
        if not isinstance(row, dict):
            continue
        title = _pick_notice_value(row, title_field, "FT", "title", "notice-title", "description-glo", "publication-number")
        if not keyword_allowed(cfg, title, safe_json_dumps(row)):
            continue
        identifier = _pick_notice_value(row, "notice-id", "publication-number", "ND", "BT-701(a)-notice")
        published_at = None
        raw_date = _pick_notice_value(row, date_field, "publication-date", "date")
        if raw_date:
            for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
                try:
                    published_at = datetime.strptime(raw_date, fmt)
                    break
                except Exception:
                    continue
        url = f"{TED_API_BASE}/v3/notices/search"
        if url_template and identifier:
            try:
                url = url_template.format(identifier=identifier, **{str(k): v for k, v in row.items()})
            except Exception:
                url = f"{TED_API_BASE}/v3/notices/search"
        items.append(
            {
                "kind": "procurement_notice",
                "external_id": identifier or stable_external_id("eu_ted_search", title, safe_json_dumps(row)),
                "url": url,
                "canonical_url": url,
                "title": title or (identifier or "TED notice"),
                "author": "TED",
                "published_at": published_at,
                "raw_text": safe_json_dumps(row),
                "normalized_text": safe_json_dumps(row),
                "extra": {
                    "connector": "eu_ted_search",
                    "source_system": "TED",
                    "query": query,
                    "fields": list(body["fields"]),
                    "notice_row": dict(row),
                },
            }
        )
    return {"items": items, "cursor": None, "stats": {"count": len(items), "source_kind": "eu_ted_search", "total_notice_count": payload.get("totalNoticeCount") if isinstance(payload, dict) else None}}

