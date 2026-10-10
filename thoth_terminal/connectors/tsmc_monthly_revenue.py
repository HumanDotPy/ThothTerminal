from __future__ import annotations

import hashlib
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from thoth_terminal.net import fetch_with_fallback
from thoth_terminal.text import clean_whitespace
from thoth_terminal.db import utcnow


_MONTH_MAP = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

_ROW_RE = re.compile(r"^\|\s*([A-Za-z]{3})\.?\s*\|\s*([\d,]+)?\s*\|\s*([\d\.-]+%?)?\s*\|?")


def _extract_markdown(text: str) -> list[str]:
    if "Markdown Content:" in text:
        text = text.split("Markdown Content:", 1)[1]
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def fetch_tsmc_monthly_revenue(config: dict) -> list[dict]:
    base_url = (config.get("base_url") or "https://investor.tsmc.com/english/monthly-revenue/{year}").strip()
    years = config.get("years") or []
    if isinstance(years, int):
        years = [years]
    if not years:
        cur_year = utcnow().year
        years = [cur_year, cur_year - 1]

    items: list[dict] = []
    max_items = int(config.get("max_items_per_run", 120) or 120)

    for year in years:
        url = base_url.format(year=int(year))
        res = fetch_with_fallback(url, timeout_s=25.0)
        text = res.text or ""
        lines = _extract_markdown(text)

        for line in lines:
            m = _ROW_RE.match(line)
            if not m:
                continue
            mon = (m.group(1) or "").lower()
            month = _MONTH_MAP.get(mon)
            if not month:
                continue
            revenue = (m.group(2) or "").replace(",", "").strip()
            yoy = (m.group(3) or "").strip()
            if not revenue:
                continue
            try:
                revenue_val = float(revenue)
            except Exception:
                continue
            # Use month start in Taiwan time; UI will localize.
            try:
                dt_local = datetime(int(year), int(month), 1, 9, 0, 0, tzinfo=ZoneInfo("Asia/Taipei"))
                dt_utc = dt_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
            except Exception:
                dt_utc = None

            title = f"TSMC {year} {mon.capitalize()} Monthly Revenue"
            key = f"{year}|{month}|{revenue_val}"
            external_id = "tsmc_mr_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
            body = clean_whitespace(
                "\n".join(
                    [
                        title,
                        f"Revenue (NT$ million): {revenue_val:,.0f}",
                        f"YoY: {yoy}" if yoy else "",
                        f"Source: {url}",
                    ]
                )
            )
            items.append(
                {
                    "kind": "event",
                    "external_id": external_id,
                    "url": url,
                    "canonical_url": url,
                    "title": title,
                    "published_at": dt_utc,
                    "raw_text": body,
                    "normalized_text": body,
                    "extra": {
                        "connector": "tsmc_monthly_revenue",
                        "company_slug": "tsmc",
                        "company_doc_type": "monthly_sales",
                        "currency": "TWD",
                        "unit": "NT$ million",
                        "value": revenue_val,
                        "yoy": yoy,
                        "month": int(month),
                        "year": int(year),
                    },
                    "links": [],
                }
            )
            if len(items) >= max_items:
                return items[:max_items]

    return items
