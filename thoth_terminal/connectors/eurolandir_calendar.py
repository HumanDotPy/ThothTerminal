from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Iterable

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from thoth_terminal.connectors.ir_calendar import _classify_event, _matches_keywords, _normalize_datetime
from thoth_terminal.text import clean_whitespace


_DATE_LABEL_RE = re.compile(r"^\s*(\w+,\s+\d{1,2}\s+[A-Za-z]+\s+\d{4})\s*$")
_SHORT_DATE_RE = re.compile(r"^\s*(\d{1,2})(?:-\d{1,2})?-(\w{3})-(\d{2,4})\s*$")
_MONTHS = {
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


def _maybe_int(value: object, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _parse_date(text: str | None) -> datetime | None:
    if not text:
        return None
    raw = text.strip()
    if not raw:
        return None
    label_match = _DATE_LABEL_RE.match(raw)
    if label_match:
        raw = label_match.group(1)
    for fmt in ("%A, %d %B %Y", "%a, %d %B %Y"):
        try:
            return datetime.strptime(raw, fmt)
        except Exception:
            continue
    m = _SHORT_DATE_RE.match(raw)
    if m:
        day = int(m.group(1))
        mon = _MONTHS.get(m.group(2).lower())
        year = int(m.group(3))
        if year < 100:
            year += 2000
        if mon:
            return datetime(year, mon, day)
    return None


def _fetch_html_with_browser(config: dict, url: str) -> str | None:
    headless = bool(config.get("headless", True))
    user_agent = config.get("user_agent")
    browser_channel = config.get("browser_channel")
    slow_mo_ms = _maybe_int(config.get("slow_mo_ms"), 0)
    wait_ms = _maybe_int(config.get("browser_wait_ms"), 5000)
    timeout_ms = _maybe_int(config.get("browser_timeout_ms"), 60_000)

    with sync_playwright() as p:
        launch_kwargs: dict[str, object] = {"headless": headless}
        if browser_channel:
            launch_kwargs["channel"] = str(browser_channel)
        if slow_mo_ms > 0:
            launch_kwargs["slow_mo"] = slow_mo_ms

        browser = p.chromium.launch(**launch_kwargs)
        context_kwargs: dict[str, object] = {}
        if user_agent:
            context_kwargs["user_agent"] = user_agent
        context = browser.new_context(**context_kwargs)
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        if wait_ms > 0:
            page.wait_for_timeout(wait_ms)
        html = page.content()
        context.close()
        browser.close()
        return html


def _first_text(nodes: Iterable[object]) -> str | None:
    for node in nodes:
        if not node:
            continue
        try:
            text = node.get_text(" ", strip=True)
        except Exception:
            text = None
        if text:
            return text
    return None


def _extract_events(html: str) -> list[tuple[str, str | None]]:
    soup = BeautifulSoup(html, "html.parser")
    items: list[tuple[str, str | None]] = []
    for node in soup.select(".popup-content.popup-event-highlighted"):
        date_node = node.select_one(".popup-date")
        title_node = node.select_one(".popup-event-title")
        date_text = None
        if date_node:
            date_text = date_node.get("aria-label") or date_node.get_text(" ", strip=True)
        title = _first_text([title_node, node.select_one(".popup-event-name")])
        if not title:
            title = node.get_text(" ", strip=True)
        if not title or not date_text:
            continue
        items.append((title, date_text))
    return items


def fetch_eurolandir_calendar(config: dict) -> list[dict]:
    list_url = (config.get("list_url") or "").strip()
    if not list_url:
        raise ValueError("eurolandir_calendar connector requires config.list_url")

    max_items = int(config.get("max_items_per_run", 80) or 80)
    include_keywords = list(config.get("include_keywords") or [])
    exclude_keywords = list(config.get("exclude_keywords") or [])
    default_tz = config.get("default_timezone")

    html = _fetch_html_with_browser(config, list_url)
    if not html:
        return []

    events = _extract_events(html)
    items: list[dict] = []
    seen: set[str] = set()

    for title, date_text in events:
        haystack = " ".join([title, date_text, list_url])
        if not _matches_keywords(haystack, include=include_keywords, exclude=exclude_keywords):
            continue

        dt = _parse_date(date_text)
        if dt:
            dt = dt.replace(hour=12, minute=0, second=0)
        dt = _normalize_datetime(dt, raw_value=None, date_text=date_text, default_tz=default_tz)

        event_type = _classify_event(title)
        key = f"{title}|{date_text}"
        external_id = "euro_cal_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        if external_id in seen:
            continue
        seen.add(external_id)

        body = clean_whitespace("\n".join([title, f"Date: {date_text}", f"Source: {list_url}"]))

        items.append(
            {
                "kind": "event",
                "external_id": external_id,
                "url": list_url,
                "canonical_url": list_url,
                "title": title,
                "published_at": dt,
                "raw_text": body,
                "normalized_text": body,
                "extra": {
                    "connector": "eurolandir_calendar",
                    "event_date_text": date_text,
                    "event_timezone": default_tz,
                    "event_type": event_type,
                    "company_slug": config.get("company_slug"),
                    "company_doc_type": event_type,
                },
                "links": [],
            }
        )
        if len(items) >= max_items:
            break

    return items


def fetch_eurolandir_calendar_with_retry(config: dict) -> list[dict]:
    try:
        return fetch_eurolandir_calendar(config)
    except Exception:
        headless = bool(config.get("headless", True))
        retry_headed = bool(config.get("retry_headed_on_fail", False))
        if headless and retry_headed:
            cfg = dict(config or {})
            cfg["headless"] = False
            return fetch_eurolandir_calendar(cfg)
        raise
