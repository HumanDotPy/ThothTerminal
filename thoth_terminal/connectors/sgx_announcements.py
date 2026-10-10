from __future__ import annotations

from typing import Any


SGX_COMPANY_ANNOUNCEMENTS_URL = "https://www.sgx.com/securities/company-announcements"
SGX_LINKS_CORPORATE_ANNOUNCEMENTS_URL = "https://links.sgx.com/1.0.0/corporate-announcements"


def fetch_sgx_announcements(config: dict[str, Any], *, source_cursor: dict[str, Any] | None = None) -> dict[str, Any]:
    """Register SGXNet official-announcement coverage without inventing filings.

    SGXNet announcements are public and canonical, but the SGX Links endpoints are
    challenge-prone from the local runtime and do not behave like a stable public API.
    This connector is therefore a binding rail: active SGX issuers can be tracked as
    having an official disclosure path while materialization remains a future browser/API
    implementation.
    """
    cfg = dict(config or {})
    stock_codes = sorted({str(value).strip().upper() for value in (cfg.get("stock_codes") or cfg.get("sgx_stock_codes") or []) if str(value).strip()})
    issuer_names = sorted({str(value).strip() for value in (cfg.get("issuer_names") or []) if str(value).strip()})
    cursor = dict(source_cursor or {})
    cursor["last_official_search_url"] = SGX_COMPANY_ANNOUNCEMENTS_URL
    cursor["last_links_endpoint_url"] = SGX_LINKS_CORPORATE_ANNOUNCEMENTS_URL
    return {
        "items": [],
        "cursor": cursor,
        "stats": {
            "official_search_url": SGX_COMPANY_ANNOUNCEMENTS_URL,
            "links_endpoint_url": SGX_LINKS_CORPORATE_ANNOUNCEMENTS_URL,
            "stock_code_count": len(stock_codes),
            "issuer_name_count": len(issuer_names),
            "materialized_items": 0,
            "status": "official_search_binding_only",
            "note": "SGXNet/company-announcements coverage is registered; no stable public JSON materialization API is assumed.",
        },
    }
