from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from thoth_terminal.config import settings
from thoth_terminal.connectors.official_common import keyword_allowed, stable_external_id
from thoth_terminal.net import DEFAULT_HEADERS

SAM_API_BASE = "https://api.sam.gov/prod/opportunities/v2/search"


def _sam_date(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).strftime("%m/%d/%Y")
        except Exception:
            continue
    return text


def fetch_sam_gov_contracts(config: dict[str, Any]) -> dict[str, Any]:
    cfg = dict(config or {})
    api_key = str(cfg.get("api_key") or getattr(settings, "sam_gov_api_key", None) or "").strip()
    if not api_key:
        raise ValueError("sam_gov_contracts requires THOTH_SAM_GOV_API_KEY or source config api_key.")
    params = dict(cfg.get("query_params") or {})
    params.setdefault("api_key", api_key)
    if cfg.get("title"):
        params.setdefault("title", str(cfg.get("title")).strip())
    elif cfg.get("keywords"):
        # SAM.gov does not expose a generic keyword parameter on this endpoint.
        # Use title for server-side narrowing and keep keyword_allowed as a local filter.
        params.setdefault("title", str(cfg.get("keywords")).strip())
    if cfg.get("posted_from"):
        params.setdefault("postedFrom", _sam_date(cfg.get("posted_from")))
    if cfg.get("posted_to"):
        params.setdefault("postedTo", _sam_date(cfg.get("posted_to")))
    if not params.get("postedFrom") or not params.get("postedTo"):
        lookback_days = max(1, min(int(cfg.get("lookback_days") or 30), 365))
        today = datetime.now(timezone.utc).date()
        params.setdefault("postedFrom", (today - timedelta(days=lookback_days)).strftime("%m/%d/%Y"))
        params.setdefault("postedTo", today.strftime("%m/%d/%Y"))
    params.setdefault("offset", int(cfg.get("offset") or 0))
    if cfg.get("limit"):
        params.setdefault("limit", int(cfg.get("limit")))
    resp = requests.get(
        str(cfg.get("api_url") or SAM_API_BASE).strip(),
        params=params,
        headers={**DEFAULT_HEADERS, "Accept": "application/json"},
        timeout=float(cfg.get("timeout_s") or 30.0),
    )
    resp.raise_for_status()
    payload = resp.json()
    rows = payload.get("opportunitiesData") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        rows = []
    limit = max(1, min(int(cfg.get("max_items_per_run") or 50), 500))
    items: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or row.get("solicitationTitle") or "").strip()
        department = str(row.get("fullParentPathName") or row.get("department") or "").strip()
        notice_id = str(row.get("noticeId") or row.get("solicitationNumber") or "").strip()
        if not keyword_allowed(cfg, title, department, notice_id):
            continue
        published_at = None
        for key in ("postedDate", "publishDate", "date"):
            raw = str(row.get(key) or "").strip()
            if not raw:
                continue
            for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S%z"):
                try:
                    published_at = datetime.strptime(raw, fmt)
                    break
                except Exception:
                    continue
            if published_at:
                break
        url = str(row.get("uiLink") or row.get("link") or row.get("url") or SAM_API_BASE).strip() or SAM_API_BASE
        normalized = "\n".join(
            part for part in [title, department, str(row.get("type") or "").strip(), str(row.get("responseDeadLine") or "").strip()] if part
        ).strip()
        items.append(
            {
                "kind": "procurement_notice",
                "external_id": notice_id or stable_external_id("sam_gov_contracts", title, url),
                "url": url,
                "canonical_url": url,
                "title": title or (notice_id or "SAM.gov opportunity"),
                "author": department or "SAM.gov",
                "published_at": published_at,
                "raw_text": normalized,
                "normalized_text": normalized,
                "extra": {
                    "connector": "sam_gov_contracts",
                    "source_system": "SAM.gov",
                    "notice_id": notice_id or None,
                    "solicitation_number": str(row.get("solicitationNumber") or "").strip() or None,
                    "notice_type": str(row.get("type") or "").strip() or None,
                    "response_deadline": str(row.get("responseDeadLine") or "").strip() or None,
                },
            }
        )
        if len(items) >= limit:
            break
    return {"items": items, "cursor": None, "stats": {"count": len(items), "source_kind": "sam_gov_contracts"}}
