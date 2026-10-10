from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Iterable
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from thoth_terminal.net import FetchError, fetch_with_fallback
from thoth_terminal.text import clean_whitespace, parse_datetime_iso


_TZ_MAP = {
    "EST": "America/New_York",
    "EDT": "America/New_York",
    "CST": "America/Chicago",
    "CDT": "America/Chicago",
    "MST": "America/Denver",
    "MDT": "America/Denver",
    "PST": "America/Los_Angeles",
    "PDT": "America/Los_Angeles",
    "GMT": "Etc/GMT",
    "UTC": "UTC",
    "CET": "Europe/Paris",
    "CEST": "Europe/Paris",
    "BST": "Europe/London",
    "HKT": "Asia/Hong_Kong",
}

_TZ_RE = re.compile(r"\b(" + "|".join(_TZ_MAP.keys()) + r")\b", re.IGNORECASE)
_HAS_TZ_RE = re.compile(r"(Z|[+-]\d{2}:?\d{2})$")
_HAS_TIME_RE = re.compile(r"\d{1,2}:\d{2}")
_YEAR_RE = re.compile(r"\b(20\d{2})\b")
_DATE_YMD_RE = re.compile(r"\b(?P<year>20\d{2})[./-](?P<month>\d{1,2})[./-](?P<day>\d{1,2})\b")
_DATE_MONTH_RE = re.compile(
    r"\b(?P<month>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?"
    r"\s*(?P<day>\d{1,2})(?:,?\s*(?P<year>\d{4}))?",
    re.IGNORECASE,
)
_TIME_RE = re.compile(r"\b(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>am|pm)\b", re.IGNORECASE)

_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}
_BLOCK_PATTERNS = (
    "cf-chl",
    "challenge-platform",
    "attention required",
    "verify you are human",
    "captcha",
    "access denied",
    "request blocked",
    "bot detection",
)


def _looks_blocked(html: str | None) -> bool:
    if not html:
        return True
    lowered = html.lower()
    return any(token in lowered for token in _BLOCK_PATTERNS)


def _matches_keywords(text: str, *, include: list[str], exclude: list[str]) -> bool:
    t = text.lower()
    if include:
        if not any(k.lower() in t for k in include if k.strip()):
            return False
    if exclude:
        if any(k.lower() in t for k in exclude if k.strip()):
            return False
    return True


def _classify_event(title: str) -> str:
    t = title.casefold()
    if "monthly sales" in t or "monthly revenue" in t:
        return "monthly_sales"
    if "earnings" in t or "financial results" in t or "results" in t and "conference" in t:
        return "earnings_event"
    if "conference call" in t or "earnings call" in t:
        return "earnings_event"
    if "results" in t and ("quarter" in t or "q1" in t or "q2" in t or "q3" in t or "q4" in t or "fiscal" in t or "fy" in t):
        return "earnings_event"
    return "calendar_event"


def _extract_timezone(text: str | None, default_tz: str | None) -> str | None:
    if text:
        match = _TZ_RE.search(text)
        if match:
            key = match.group(1).upper()
            return _TZ_MAP.get(key)
    return default_tz


def _normalize_datetime(
    dt: datetime | None,
    *,
    raw_value: str | None,
    date_text: str | None,
    default_tz: str | None,
) -> datetime | None:
    if not dt:
        return None
    has_tz_suffix = bool(raw_value and _HAS_TZ_RE.search(raw_value))
    tz_name = None if has_tz_suffix else _extract_timezone(date_text, default_tz)
    if dt.hour == 0 and dt.minute == 0 and not (date_text and _HAS_TIME_RE.search(date_text)):
        dt = dt.replace(hour=12, minute=0, second=0)
    if tz_name:
        try:
            dt = dt.replace(tzinfo=ZoneInfo(tz_name)).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
        except Exception:
            pass
    return dt


def _parse_date_time_text(text: str | None, *, year_hint: int | None = None) -> tuple[datetime | None, str | None]:
    if not text:
        return None, None
    cleaned = " ".join(str(text).split())
    if not cleaned:
        return None, None

    if year_hint is None:
        year_match = _YEAR_RE.search(cleaned)
        if year_match:
            try:
                year_hint = int(year_match.group(1))
            except Exception:
                year_hint = None

    match = _DATE_YMD_RE.search(cleaned)
    if match:
        year = int(match.group("year"))
        month = int(match.group("month"))
        day = int(match.group("day"))
        hour = 12
        minute = 0
        time_match = _TIME_RE.search(cleaned)
        if time_match:
            hour = int(time_match.group("hour"))
            minute = int(time_match.group("minute") or 0)
            ampm = time_match.group("ampm").lower()
            if ampm == "pm" and hour != 12:
                hour += 12
            if ampm == "am" and hour == 12:
                hour = 0
        return datetime(year, month, day, hour, minute), match.group(0)

    match = _DATE_MONTH_RE.search(cleaned)
    if match:
        month_key = match.group("month").lower()
        month = _MONTHS.get(month_key[:3], _MONTHS.get(month_key))
        if not month:
            return None, None
        day = int(match.group("day"))
        year = int(match.group("year")) if match.group("year") else year_hint
        if not year:
            return None, None
        hour = 12
        minute = 0
        time_match = _TIME_RE.search(cleaned)
        if time_match:
            hour = int(time_match.group("hour"))
            minute = int(time_match.group("minute") or 0)
            ampm = time_match.group("ampm").lower()
            if ampm == "pm" and hour != 12:
                hour += 12
            if ampm == "am" and hour == 12:
                hour = 0
        return datetime(year, month, day, hour, minute), match.group(0)

    return None, None


def _clean_title(text: str, *, date_tokens: list[str | None]) -> str:
    title = text
    for token in date_tokens:
        if token:
            title = title.replace(token, "")
    title = re.sub(r"^\\s*(20\\d{2})\\b", "", title)
    title = re.sub(
        r"^\\s*(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\\.?\\s*",
        "",
        title,
        flags=re.IGNORECASE,
    )
    return clean_whitespace(title)


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


def _fetch_html_with_browser(
    *,
    url: str,
    storage_state_path: str | None,
    headless: bool,
    user_agent: str | None,
    browser_channel: str | None,
    user_data_dir: str | None,
    slow_mo_ms: int,
    wait_ms: int,
    scrolls: int,
    accept_selector: str | None,
    timeout_ms: int,
) -> str | None:
    from pathlib import Path

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        launch_kwargs: dict[str, object] = {"headless": headless}
        if browser_channel:
            launch_kwargs["channel"] = str(browser_channel)
        if slow_mo_ms and slow_mo_ms > 0:
            launch_kwargs["slow_mo"] = int(slow_mo_ms)

        browser = None
        if user_data_dir:
            context_kwargs: dict[str, object] = {}
            if user_agent:
                context_kwargs["user_agent"] = user_agent
            context = p.chromium.launch_persistent_context(
                str(user_data_dir),
                **launch_kwargs,
                **context_kwargs,
            )
        else:
            context_kwargs = {}
            if storage_state_path and Path(storage_state_path).exists():
                context_kwargs["storage_state"] = storage_state_path
            if user_agent:
                context_kwargs["user_agent"] = user_agent
            browser = p.chromium.launch(**launch_kwargs)
            context = browser.new_context(**context_kwargs)
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        if accept_selector:
            try:
                page.locator(accept_selector).first.click(timeout=1500)
            except Exception:
                pass
        if wait_ms > 0:
            page.wait_for_timeout(wait_ms)
        if scrolls > 0:
            for _ in range(scrolls):
                page.mouse.wheel(0, 2200)
                page.wait_for_timeout(800)
        html = page.content()
        context.close()
        if browser:
            browser.close()
        return html


def _fetch_calendar_html(list_url: str, *, config: dict, force_browser: bool = False) -> str:
    if not force_browser:
        res = fetch_with_fallback(list_url, timeout_s=25.0)
        return res.text or ""

    storage_state_path = config.get("storage_state_path") or config.get("browser_storage_state_path")
    headless = bool(config.get("headless", True))
    user_agent = config.get("user_agent")
    browser_channel = config.get("browser_channel")
    user_data_dir = config.get("user_data_dir") or config.get("browser_user_data_dir")
    slow_mo_ms = int(config.get("slow_mo_ms") or 0)
    wait_ms = int(config.get("browser_wait_ms") or 2500)
    scrolls = int(config.get("browser_scrolls") or 0)
    accept_selector = config.get("accept_cookies_selector")
    timeout_ms = int(config.get("browser_timeout_ms") or 60_000)

    try:
        html = _fetch_html_with_browser(
            url=list_url,
            storage_state_path=storage_state_path,
            headless=headless,
            user_agent=user_agent,
            browser_channel=browser_channel,
            user_data_dir=user_data_dir,
            slow_mo_ms=slow_mo_ms,
            wait_ms=wait_ms,
            scrolls=scrolls,
            accept_selector=accept_selector,
            timeout_ms=timeout_ms,
        )
        if _looks_blocked(html):
            raise FetchError("Browser fetch blocked or returned challenge page.", url=list_url)
    except Exception:
        retry_headed = bool(config.get("retry_headed_on_fail", False))
        if headless and retry_headed:
            html = _fetch_html_with_browser(
                url=list_url,
                storage_state_path=storage_state_path,
                headless=False,
                user_agent=user_agent,
                browser_channel=browser_channel,
                user_data_dir=user_data_dir,
                slow_mo_ms=slow_mo_ms,
                wait_ms=wait_ms,
                scrolls=scrolls,
                accept_selector=accept_selector,
                timeout_ms=timeout_ms,
            )
            if _looks_blocked(html):
                raise FetchError("Browser fetch blocked or returned challenge page.", url=list_url)
        else:
            raise
    return html or ""


def _parse_calendar_html(
    html: str,
    *,
    list_url: str,
    max_items: int,
    item_selector: str | None,
    date_selector: str | None,
    time_selector: str | None,
    title_selector: str | None,
    link_selector: str | None,
    default_tz: str | None,
    default_year: int | None,
    include_keywords: list[str],
    exclude_keywords: list[str],
    config: dict,
) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")

    containers = soup.select(item_selector) if item_selector else []
    if not containers:
        containers = []
        for time_tag in soup.find_all("time"):
            if time_tag.parent and time_tag.parent not in containers:
                containers.append(time_tag.parent)

    items: list[dict] = []
    seen: set[str] = set()

    for node in containers:
        time_tag = node.select_one(date_selector) if date_selector else node.find("time")
        if not time_tag:
            continue
        date_text = time_tag.get_text(" ", strip=True) if time_tag else None
        datetime_attr = time_tag.get("datetime") if time_tag else None
        time_node = node.select_one(time_selector) if time_selector else None
        time_text = time_node.get_text(" ", strip=True) if time_node else None
        combined_text = " ".join([t for t in [date_text, time_text] if t]) or None
        dt = parse_datetime_iso(str(datetime_attr)) if datetime_attr else None
        matched_date = None
        if not dt:
            dt, matched_date = _parse_date_time_text(combined_text or date_text, year_hint=default_year)
        dt = _normalize_datetime(
            dt,
            raw_value=str(datetime_attr) if datetime_attr else None,
            date_text=combined_text or date_text,
            default_tz=default_tz,
        )

        title_node = None
        if title_selector:
            title_node = node.select_one(title_selector)
        if not title_node:
            title_node = node.select_one(".media-heading") or node.select_one("h2") or node.select_one("h3") or node.select_one("h4")

        link_node = node.select_one(link_selector) if link_selector else None
        title = _first_text([title_node, link_node])
        if not title:
            full_text = node.get_text(" ", strip=True)
            title = _clean_title(
                full_text,
                date_tokens=[
                    matched_date,
                    combined_text,
                    date_text,
                    time_text,
                ],
            )
            title = title or None
        if not title:
            continue

        haystack = " ".join([title, combined_text or date_text or "", list_url])
        if not _matches_keywords(haystack, include=include_keywords, exclude=exclude_keywords):
            continue

        href = link_node.get("href") if link_node else None
        detail_url = urljoin(list_url, href) if href else None
        if detail_url and "/ical" in detail_url:
            detail_url = None
        url = detail_url or list_url

        date_display = combined_text or date_text
        event_type = _classify_event(title)
        external_key = f"{title}|{date_display or ''}|{url}"
        external_id = "ir_cal_" + hashlib.sha256(external_key.encode("utf-8")).hexdigest()[:16]
        if external_id in seen:
            continue
        seen.add(external_id)

        body = clean_whitespace(
            "\n".join([title, f"Date: {date_display}" if date_display else "", f"Source: {list_url}"])
        )

        items.append(
            {
                "kind": "event",
                "external_id": external_id,
                "url": url,
                "canonical_url": url,
                "title": title,
                "published_at": dt,
                "raw_text": body,
                "normalized_text": body,
                "extra": {
                    "connector": "ir_calendar",
                    "event_date_text": date_display,
                    "event_timezone": _extract_timezone(date_display, default_tz),
                    "event_type": event_type,
                    "company_slug": config.get("company_slug"),
                    "company_doc_type": event_type,
                },
                "links": [detail_url] if detail_url else [],
            }
        )
        if len(items) >= max_items:
            break

    return items


def fetch_ir_calendar(config: dict) -> list[dict]:
    list_url = (config.get("list_url") or "").strip()
    if not list_url:
        raise ValueError("ir_calendar connector requires config.list_url")

    fallback_urls = [list_url] + list(config.get("list_url_fallbacks") or [])
    use_browser = bool(config.get("use_browser", False))
    fallback_on_empty = bool(config.get("browser_fallback_on_empty", True))
    html = ""
    used_url = list_url
    last_error: Exception | None = None

    max_items = int(config.get("max_items_per_run", 60) or 60)
    item_selector = config.get("item_selector") or "article.media"
    date_selector = config.get("date_selector") or "time"
    time_selector = config.get("time_selector")
    title_selector = config.get("title_selector")
    link_selector = config.get("link_selector") or "a"
    default_tz = config.get("default_timezone")
    default_year = config.get("default_year")
    if isinstance(default_year, str) and default_year.lower() == "current":
        default_year = datetime.now(ZoneInfo("UTC")).year
    elif isinstance(default_year, str):
        try:
            default_year = int(default_year)
        except Exception:
            default_year = None
    elif not isinstance(default_year, int):
        default_year = None

    include_keywords = list(config.get("include_keywords") or [])
    exclude_keywords = list(config.get("exclude_keywords") or [])

    for candidate_url in fallback_urls:
        try:
            html = _fetch_calendar_html(candidate_url, config=config, force_browser=False)
            if _looks_blocked(html):
                raise FetchError("Calendar fetch blocked or empty HTML.", url=candidate_url)
            used_url = candidate_url
            items = _parse_calendar_html(
                html,
                list_url=candidate_url,
                max_items=max_items,
                item_selector=item_selector,
                date_selector=date_selector,
                time_selector=time_selector,
                title_selector=title_selector,
                link_selector=link_selector,
                default_tz=default_tz,
                default_year=default_year,
                include_keywords=include_keywords,
                exclude_keywords=exclude_keywords,
                config=config,
            )
            if items:
                return items
            if use_browser and fallback_on_empty:
                html = _fetch_calendar_html(candidate_url, config=config, force_browser=True)
                if _looks_blocked(html):
                    raise FetchError("Calendar fetch blocked or empty HTML.", url=candidate_url)
                items = _parse_calendar_html(
                    html,
                    list_url=candidate_url,
                    max_items=max_items,
                    item_selector=item_selector,
                    date_selector=date_selector,
                    time_selector=time_selector,
                    title_selector=title_selector,
                    link_selector=link_selector,
                    default_tz=default_tz,
                    default_year=default_year,
                    include_keywords=include_keywords,
                    exclude_keywords=exclude_keywords,
                    config=config,
                )
                if items:
                    return items
            used_url = candidate_url
        except Exception as exc:
            last_error = exc
            html = ""
            continue

    if last_error:
        raise last_error
    return []
