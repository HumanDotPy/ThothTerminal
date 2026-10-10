from __future__ import annotations

from typing import Any


BURSA_COMPANY_ANNOUNCEMENTS_URL = "https://www.bursamalaysia.com/market_information/announcements/company_announcement"
BURSA_LISTING_DIRECTORY_URL = "https://www.bursamalaysia.com/trade/trading_resources/listing_directory/company-profile"


def fetch_bursa_announcements(config: dict[str, Any], *, source_cursor: dict[str, Any] | None = None) -> dict[str, Any]:
    """Register Bursa Malaysia official-announcement coverage without inventing filings.

    Bursa company announcements are the official disclosure surface for Bursa-listed
    issuers, but the public website is challenge-prone from the local runtime. This
    connector is therefore a binding rail: active Bursa issuers can be tracked as
    having an official disclosure path while materialization remains a future
    browser/API implementation.
    """
    cfg = dict(config or {})
    stock_codes = sorted(
        {
            str(value).strip().upper()
            for value in (cfg.get("stock_codes") or cfg.get("my_stock_codes") or cfg.get("bursa_stock_codes") or [])
            if str(value).strip()
        }
    )
    issuer_names = sorted({str(value).strip() for value in (cfg.get("issuer_names") or []) if str(value).strip()})
    cursor = dict(source_cursor or {})
    cursor["last_official_search_url"] = BURSA_COMPANY_ANNOUNCEMENTS_URL
    cursor["last_listing_directory_url"] = BURSA_LISTING_DIRECTORY_URL
    return {
        "items": [],
        "cursor": cursor,
        "stats": {
            "official_search_url": BURSA_COMPANY_ANNOUNCEMENTS_URL,
            "listing_directory_url": BURSA_LISTING_DIRECTORY_URL,
            "stock_code_count": len(stock_codes),
            "issuer_name_count": len(issuer_names),
            "materialized_items": 0,
            "status": "official_search_binding_only",
            "note": "Bursa Malaysia company-announcement coverage is registered; no stable public JSON materialization API is assumed.",
        },
    }
