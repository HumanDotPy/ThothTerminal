from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from thoth_terminal.connectors.official_common import keyword_allowed, stable_external_id
from thoth_terminal.connectors.sec_edgar import EARNINGS_RELEVANT_FILINGS, fetch_rss_filings


def _parse_atom_datetime(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None


def _parse_title_identity(title: str) -> tuple[str | None, str | None]:
    match = re.search(r"\s-\s(?P<company>.+?)\s*\((?P<cik>\d{1,10})\)", title)
    if not match:
        return None, None
    company = re.sub(r"\s*\([^)]*\)\s*$", "", match.group("company")).strip()
    cik = match.group("cik").zfill(10)
    return company or None, cik


def _summary_items(summary: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for match in re.finditer(r"\bItem\s+([0-9]{1,2}\.[0-9]{2})\b", summary or "", flags=re.IGNORECASE):
        item = match.group(1)
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def fetch_sec_edgar_rss(config: dict[str, Any]) -> dict[str, Any]:
    """Fetch the SEC current-filings Atom feed as an ingestion connector."""
    cfg = dict(config or {})
    filing_types_raw = cfg.get("filing_types")
    filing_types = (
        {str(item).strip() for item in filing_types_raw if str(item).strip()}
        if isinstance(filing_types_raw, list)
        else EARNINGS_RELEVANT_FILINGS
    )
    max_items = max(1, min(int(cfg.get("max_items_per_run") or 100), 500))
    feed_rows = fetch_rss_filings(filing_types=filing_types, limit=max_items, raise_on_error=True)

    items: list[dict[str, Any]] = []
    for row in feed_rows:
        title = str(row.get("title") or "").strip() or "SEC current filing"
        summary = str(row.get("summary") or "").strip()
        link = str(row.get("link") or "").strip()
        form_type = str(row.get("form_type") or "").strip()
        if not keyword_allowed(cfg, title, summary, form_type):
            continue
        normalized = "\n".join(
            part
            for part in [
                f"SEC current filing: {title}",
                f"Form type: {form_type}" if form_type else "",
                summary,
            ]
            if part
        )
        company_name, cik = _parse_title_identity(title)
        filing_items = _summary_items(summary)
        is_earnings_release = form_type in {"8-K", "8-K/A"} and (
            "2.02" in filing_items or re.search(r"\b(earnings|financial results?|quarterly results?)\b", summary, flags=re.IGNORECASE) is not None
        )
        items.append(
            {
                "kind": "sec_filing",
                "external_id": stable_external_id("sec_edgar_rss", link, title, row.get("updated")),
                "url": link or None,
                "canonical_url": link or None,
                "title": title,
                "author": company_name or "SEC EDGAR",
                "published_at": _parse_atom_datetime(row.get("updated")),
                "raw_text": normalized,
                "normalized_text": normalized,
                "extra": {
                    "connector": "sec_edgar_rss",
                    "feed": "current_filings_atom",
                    "form_type": form_type or None,
                    "filing_type": form_type or None,
                    "company_name": company_name,
                    "cik": cik,
                    "items": filing_items,
                    "is_earnings_release": is_earnings_release,
                    "updated": row.get("updated"),
                },
            }
        )
        if len(items) >= max_items:
            break

    return {
        "items": items,
        "cursor": None,
        "stats": {"count": len(items), "source_kind": "sec_edgar_rss"},
    }
