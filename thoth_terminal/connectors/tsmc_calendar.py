from __future__ import annotations

import hashlib
import re

from thoth_terminal.net import fetch_with_fallback
from thoth_terminal.text import clean_whitespace, parse_datetime_iso


_EVENT_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+(.+)$"
)


def _extract_lines(text: str) -> list[str]:
    if "Markdown Content:" in text:
        text = text.split("Markdown Content:", 1)[1]
    lines = [ln.strip() for ln in text.splitlines() if ln and ln.strip()]
    return lines


def _strip_leading_link(line: str) -> str:
    return re.sub(r"^\[\]\([^)]+\)", "", line).strip()


def _split_title_location(rest: str) -> tuple[str, str | None]:
    parts = re.split(r"\s{2,}", rest)
    title = parts[0].strip()
    location = parts[1].strip() if len(parts) > 1 else None
    return (title, location)


def _classify_event(title: str) -> str:
    t = title.casefold()
    if "monthly sales" in t:
        return "monthly_sales"
    if "results" in t or "earnings" in t or "conference call" in t or "conference" in t:
        return "earnings_event"
    return "calendar_event"


def fetch_tsmc_calendar(config: dict) -> list[dict]:
    list_url = (config.get("list_url") or "").strip()
    if not list_url:
        raise ValueError("tsmc_calendar connector requires config.list_url")

    max_items = int(config.get("max_items_per_run", 80) or 80)
    res = fetch_with_fallback(list_url, timeout_s=25.0)
    text = res.text or ""
    lines = _extract_lines(text)

    items: list[dict] = []
    for raw in lines:
        line = _strip_leading_link(raw)
        match = _EVENT_LINE_RE.match(line)
        if not match:
            continue
        start_s, end_s, rest = match.groups()
        rest = rest.strip()

        tz = None
        tz_match = re.search(r"\b([A-Za-z]+/[A-Za-z_]+)\b\s+public\s*$", rest)
        if tz_match:
            tz = tz_match.group(1)
            rest = rest[: tz_match.start()].strip()
        elif rest.lower().endswith("public"):
            rest = rest[:-6].strip()

        title, location = _split_title_location(rest)
        if not title:
            continue

        event_type = _classify_event(title)
        external_key = f"{start_s}|{title}"
        external_id = "tsmc_cal_" + hashlib.sha256(external_key.encode("utf-8")).hexdigest()[:16]

        published_at = parse_datetime_iso(start_s)
        body_parts = [title, f"Start: {start_s}", f"End: {end_s}"]
        if tz:
            body_parts.append(f"Time zone: {tz}")
        if location:
            body_parts.append(f"Location: {location}")
        body = clean_whitespace("\n".join(body_parts))

        items.append(
            {
                "kind": "event",
                "external_id": external_id,
                "url": list_url,
                "canonical_url": list_url,
                "title": title,
                "published_at": published_at,
                "raw_text": body,
                "normalized_text": body,
                "extra": {
                    "connector": "tsmc_calendar",
                    "event_start": start_s,
                    "event_end": end_s,
                    "event_tz": tz,
                    "event_location": location,
                    "event_type": event_type,
                    "company_slug": "tsmc",
                    "company_doc_type": event_type,
                },
                "links": [],
            }
        )
        if len(items) >= max_items:
            break

    return items
