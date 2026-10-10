from __future__ import annotations

from typing import Any


AIX_ISSUERS_URL = "https://aix.kz/issuers/"
KASE_ISSUERS_URL = "https://kase.kz/en/issuers/"
KASE_NEWS_URL = "https://kase.kz/en/information/news/"


def fetch_kazakhstan_announcements(config: dict[str, Any], *, source_cursor: dict[str, Any] | None = None) -> dict[str, Any]:
    """Register Kazakhstan exchange disclosure coverage without inventing filings.

    AIX and KASE provide official issuer and disclosure surfaces, but coverage and
    page shape differ across issuers. This connector binds active Kazakhstan issuers
    to the official exchange rail while materialization remains a future issuer/API
    implementation.
    """
    cfg = dict(config or {})
    stock_codes = sorted(
        {
            str(value).strip().upper()
            for value in (cfg.get("stock_codes") or cfg.get("kz_stock_codes") or cfg.get("aix_symbols") or [])
            if str(value).strip()
        }
    )
    issuer_names = sorted({str(value).strip() for value in (cfg.get("issuer_names") or []) if str(value).strip()})
    cursor = dict(source_cursor or {})
    cursor["last_aix_issuers_url"] = AIX_ISSUERS_URL
    cursor["last_kase_issuers_url"] = KASE_ISSUERS_URL
    cursor["last_kase_news_url"] = KASE_NEWS_URL
    return {
        "items": [],
        "cursor": cursor,
        "stats": {
            "aix_issuers_url": AIX_ISSUERS_URL,
            "kase_issuers_url": KASE_ISSUERS_URL,
            "kase_news_url": KASE_NEWS_URL,
            "stock_code_count": len(stock_codes),
            "issuer_name_count": len(issuer_names),
            "materialized_items": 0,
            "status": "official_search_binding_only",
            "note": "AIX/KASE Kazakhstan issuer coverage is registered; issuer-specific materialization is not assumed.",
        },
    }
