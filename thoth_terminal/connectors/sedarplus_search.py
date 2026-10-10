from __future__ import annotations

from typing import Any


SEDARPLUS_DOCUMENT_SEARCH_URL = (
    "https://www.sedarplus.ca/csa-party/service/create.html"
    "?_locale=en&service=searchDocuments&targetAppCode=csa-party"
)


def fetch_sedarplus_search(config: dict[str, Any], *, source_cursor: dict[str, Any] | None = None) -> dict[str, Any]:
    """Register SEDAR+ official-search coverage without inventing filing records.

    SEDAR+ exposes public document search, but it does not provide a stable supported
    public JSON API in the way EDGAR/OpenDART/EDINETDB do. This connector therefore
    acts as a durable official-search binding rail: it lets source-pack coverage track
    which active Canadian issuers have an official SEDAR+ path, while leaving actual
    document materialization to a future browser/API implementation.
    """
    cfg = dict(config or {})
    stock_codes = sorted({str(value).strip().upper() for value in (cfg.get("ca_stock_codes") or []) if str(value).strip()})
    profile_names = sorted({str(value).strip() for value in (cfg.get("profile_names") or []) if str(value).strip()})
    cursor = dict(source_cursor or {})
    cursor["last_official_search_url"] = SEDARPLUS_DOCUMENT_SEARCH_URL
    return {
        "items": [],
        "cursor": cursor,
        "stats": {
            "official_search_url": SEDARPLUS_DOCUMENT_SEARCH_URL,
            "ca_stock_code_count": len(stock_codes),
            "profile_name_count": len(profile_names),
            "materialized_items": 0,
            "status": "official_search_binding_only",
            "note": "SEDAR+ public document search is registered; no supported public JSON filing API is assumed.",
        },
    }
