from __future__ import annotations

import hashlib
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from thoth_terminal.net import fetch_with_fallback
from thoth_terminal.text import clean_whitespace
from thoth_terminal.db import utcnow


_EVENT_RE = re.compile(
    r"Earnings Conference will be held on [A-Za-z]+,\s+([A-Za-z]+ \d{1,2}, \d{4}),\s+at\s+(\d{1,2}:\d{2})\s+Taiwan time/\s+(\d{1,2}:\d{2})\s+Eastern Time",
    re.IGNORECASE,
)


def _extract_markdown(text: str) -> list[str]:
    if "Markdown Content:" in text:
        text = text.split("Markdown Content:", 1)[1]
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines


def _quarter_title(year: int, q: int) -> str:
    return f"TSMC {year} Q{q} Quarterly Results"


def _build_event_doc(line: str, *, year: int, quarter: int, url: str) -> dict | None:
    m = _EVENT_RE.search(line)
    if not m:
        return None
    date_str, time_tw, time_et = m.groups()
    try:
        dt_local = datetime.strptime(f"{date_str} {time_tw}", "%B %d, %Y %H:%M")
        tz = ZoneInfo("Asia/Taipei")
        dt_utc = dt_local.replace(tzinfo=tz).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    except Exception:
        dt_utc = None
    title = f"TSMC Q{quarter} {year} Earnings Conference"
    key = f"{date_str}|{time_tw}|{title}"
    external_id = "tsmc_qr_event_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    body = clean_whitespace(
        "\n".join(
            [
                title,
                f"Date: {date_str}",
                f"Time (Taiwan): {time_tw}",
                f"Time (ET): {time_et}",
            ]
        )
    )
    return {
        "kind": "event",
        "external_id": external_id,
        "url": url,
        "canonical_url": url,
        "title": title,
        "published_at": dt_utc,
        "raw_text": body,
        "normalized_text": body,
        "extra": {
            "connector": "tsmc_quarterly",
            "company_slug": "tsmc",
            "company_doc_type": "earnings_event",
            "event_date": date_str,
            "event_time_taiwan": time_tw,
            "event_time_et": time_et,
        },
        "links": [],
    }


def fetch_tsmc_quarterly(config: dict) -> list[dict]:
    base_url = (config.get("base_url") or "https://investor.tsmc.com/english/quarterly-results/{year}/q{quarter}").strip()
    years = config.get("years") or []
    if isinstance(years, int):
        years = [years]
    if not years:
        years = [utcnow().year - 1]
    quarters = config.get("quarters") or [1, 2, 3, 4]
    if isinstance(quarters, int):
        quarters = [quarters]

    items: list[dict] = []
    max_items = int(config.get("max_items_per_run", 40) or 40)

    for year in years:
        for q in quarters:
            url = base_url.format(year=int(year), quarter=int(q), q=int(q))
            res = fetch_with_fallback(url, timeout_s=25.0)
            text = res.text or ""
            lines = _extract_markdown(text)
            if not lines:
                continue

            title = lines[0]
            title = title if title else _quarter_title(int(year), int(q))
            body = clean_whitespace("\n".join(lines[:40]))
            key = f"{year}|{q}|{url}"
            external_id = "tsmc_qr_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]

            items.append(
                {
                    "kind": "document",
                    "external_id": external_id,
                    "url": url,
                    "canonical_url": url,
                    "title": title,
                    "published_at": None,
                    "raw_text": body,
                    "normalized_text": body,
                    "extra": {
                        "connector": "tsmc_quarterly",
                        "company_slug": "tsmc",
                        "company_doc_type": "earnings_release",
                        "doc_source": "quarterly_results_page",
                    },
                    "links": [],
                }
            )

            for line in lines:
                event_doc = _build_event_doc(line, year=int(year), quarter=int(q), url=url)
                if event_doc:
                    items.append(event_doc)
                    break

            if len(items) >= max_items:
                return items[:max_items]

    return items
