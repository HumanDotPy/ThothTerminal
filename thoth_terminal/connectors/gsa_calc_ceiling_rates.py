from __future__ import annotations

from datetime import datetime
from typing import Any

import requests

from thoth_terminal.connectors.official_common import keyword_allowed, safe_json_dumps, stable_external_id
from thoth_terminal.net import DEFAULT_HEADERS


GSA_CALC_CEILING_RATES_URL = "https://api.gsa.gov/acquisition/calc/v3/api/ceilingrates/"


def _parse_datetime(value: object) -> datetime | None:
    raw = str(value or "").strip().replace("Z", "+00:00")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except Exception:
        return None


def _extract_rate_rows(payload: object) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    hits_obj = payload.get("hits")
    if not isinstance(hits_obj, dict):
        return []
    hits = hits_obj.get("hits")
    if not isinstance(hits, list):
        return []
    rows: list[dict[str, Any]] = []
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        source = hit.get("_source")
        if not isinstance(source, dict):
            continue
        row = dict(source)
        if hit.get("_id") is not None:
            row["_id"] = hit.get("_id")
        rows.append(row)
    return rows


def fetch_gsa_calc_ceiling_rates(config: dict[str, Any]) -> dict[str, Any]:
    """Fetch keyless GSA CALC+ labor ceiling-rate rows."""
    cfg = dict(config or {})
    url = str(cfg.get("api_url") or cfg.get("data_url") or GSA_CALC_CEILING_RATES_URL).strip()
    page_size = max(1, min(int(cfg.get("page_size") or cfg.get("max_items_per_run") or 50), 300))
    params: dict[str, Any] = {
        "page": int(cfg.get("page") or 1),
        "page_size": page_size,
    }
    if cfg.get("keyword"):
        params["keyword"] = str(cfg.get("keyword")).strip()
    if cfg.get("search"):
        params["search"] = str(cfg.get("search")).strip()
    if cfg.get("ordering"):
        params["ordering"] = str(cfg.get("ordering")).strip()
    if cfg.get("sort"):
        params["sort"] = str(cfg.get("sort")).strip()
    filters = cfg.get("filters") or cfg.get("filter")
    if filters:
        params["filter"] = filters

    resp = requests.get(
        url,
        params=params,
        headers={**DEFAULT_HEADERS, "Accept": "application/json", **dict(cfg.get("headers") or {})},
        timeout=float(cfg.get("timeout_s") or 30.0),
    )
    resp.raise_for_status()
    rows = _extract_rate_rows(resp.json())
    limit = max(1, min(int(cfg.get("max_items_per_run") or page_size), page_size))
    items: list[dict[str, Any]] = []
    for row in rows:
        labor_category = str(row.get("labor_category") or "").strip()
        vendor = str(row.get("vendor_name") or "").strip()
        price = str(row.get("current_price") or "").strip()
        sin = str(row.get("sin") or "").strip()
        title = " - ".join(part for part in [labor_category, vendor, price and f"${price}/hr"] if part) or "GSA CALC+ ceiling-rate row"
        text = safe_json_dumps(row)
        if not keyword_allowed(cfg, title, text, sin):
            continue
        row_id = row.get("id") or row.get("_id")
        item_url = url
        items.append(
            {
                "kind": "institutional_record",
                "external_id": str(row_id or stable_external_id("gsa_calc_ceiling_rates", title, text)),
                "url": item_url,
                "canonical_url": item_url,
                "title": title,
                "author": "GSA CALC+",
                "published_at": _parse_datetime(row.get("_timestamp")),
                "raw_text": text,
                "normalized_text": text,
                "extra": {
                    "connector": "gsa_calc_ceiling_rates",
                    "source_system": "GSA CALC+",
                    "record_kind": "labor_ceiling_rate",
                    "row": row,
                },
            }
        )
        if len(items) >= limit:
            break
    return {
        "items": items,
        "cursor": None,
        "stats": {
            "count": len(items),
            "source_kind": "gsa_calc_ceiling_rates",
            "record_kind": "labor_ceiling_rate",
        },
    }
