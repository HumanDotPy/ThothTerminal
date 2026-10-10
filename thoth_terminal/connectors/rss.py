from __future__ import annotations

from datetime import datetime
import re
from time import monotonic
from urllib.parse import urljoin

import feedparser

from thoth_terminal.connectors.document_fetch import build_fetched_document_payload
from thoth_terminal.net import FetchError, fetch_with_fallback
from thoth_terminal.text import clean_whitespace, extract_image_urls_from_html, extract_text_from_html


def _matches_keywords(text: str, *, include: list[str], exclude: list[str]) -> bool:
    t = text.lower()
    def _contains(keyword: str) -> bool:
        needle = keyword.strip().lower()
        if not needle:
            return False
        if needle.isascii() and needle.isalpha() and len(needle) <= 3:
            return bool(re.search(rf"\b{re.escape(needle)}\b", t))
        return needle in t
    if include:
        if not any(_contains(k) for k in include if k.strip()):
            return False
    if exclude:
        if any(_contains(k) for k in exclude if k.strip()):
            return False
    return True


def _to_datetime(value: object) -> datetime | None:
    if not value:
        return None
    try:
        # feedparser gives time.struct_time
        return datetime(*value[:6])
    except Exception:
        return None


_TITLE_DATE_RE = re.compile(r"(\\d{1,2})[/-](\\d{1,2})[/-](\\d{2,4})")


def _parse_title_date(title: str | None) -> datetime | None:
    if not title:
        return None
    match = _TITLE_DATE_RE.search(title)
    if not match:
        return None
    month = int(match.group(1))
    day = int(match.group(2))
    year = int(match.group(3))
    if year < 100:
        year += 2000
    try:
        return datetime(year, month, day, 12, 0, 0)
    except Exception:
        return None


def _float_config(config: dict, key: str, default: float, *, minimum: float, maximum: float) -> float:
    try:
        value = float(config.get(key, default))
    except Exception:
        value = default
    return max(minimum, min(maximum, value))


def _int_config(config: dict, key: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(config.get(key, default))
    except Exception:
        value = default
    return max(minimum, min(maximum, value))


def fetch_rss(config: dict) -> list[dict]:
    feed_url = config.get("feed_url")
    if not feed_url:
        raise ValueError("rss connector requires config.feed_url")

    # Fetch with browser-like headers to reduce 401/403 blocks on some IR/Atom/RSS endpoints.
    feed_timeout_s = _float_config(config, "feed_timeout_s", 25.0, minimum=1.0, maximum=60.0)
    res = fetch_with_fallback(feed_url, timeout_s=feed_timeout_s)
    parsed = feedparser.parse(res.content)
    items: list[dict] = []
    fetch_article = bool(config.get("fetch_article", True))
    max_items = _int_config(config, "max_items_per_run", 50, minimum=1, maximum=500)
    article_timeout_s = _float_config(config, "fetch_article_timeout_s", 8.0, minimum=1.0, maximum=60.0)
    if "article_timeout_s" in config and "fetch_article_timeout_s" not in config:
        article_timeout_s = _float_config(config, "article_timeout_s", 8.0, minimum=1.0, maximum=60.0)
    max_article_fetches = _int_config(config, "max_article_fetches", min(8, max_items), minimum=0, maximum=max_items)
    article_budget_s = _float_config(config, "article_fetch_budget_s", 90.0, minimum=0.0, maximum=600.0)
    article_attempts = _int_config(config, "fetch_article_attempts", 1, minimum=1, maximum=3)
    article_started_at = monotonic()
    article_fetches = 0

    include_keywords = list(config.get("include_keywords") or [])
    exclude_keywords = list(config.get("exclude_keywords") or [])

    for entry in parsed.entries[:max_items]:
        link = entry.get("link")
        if not link:
            continue
        url = urljoin(feed_url, link)
        external_id = entry.get("id") or url
        title = entry.get("title") or None
        author = entry.get("author") or None
        published_at = _to_datetime(entry.get("published_parsed") or entry.get("updated_parsed"))
        if config.get("title_date_infer"):
            title_date = _parse_title_date(title or "")
            if title_date:
                published_at = title_date

        raw_text = entry.get("summary") or ""
        normalized_text = clean_whitespace(raw_text)
        attachments: list[dict] = []
        item_extra: dict[str, object] = {}

        haystack = " ".join([title or "", normalized_text or "", url])
        if not _matches_keywords(haystack, include=include_keywords, exclude=exclude_keywords):
            continue

        raw_html = None
        image_urls: list[str] = []
        if fetch_article:
            skip_reason = None
            if article_fetches >= max_article_fetches:
                skip_reason = "max_article_fetches"
            elif article_budget_s and (monotonic() - article_started_at) >= article_budget_s:
                skip_reason = "article_fetch_budget_s"
            if skip_reason:
                item_extra["article_fetch_skipped"] = skip_reason
            else:
                article_fetches += 1
                try:
                    res = fetch_with_fallback(url, timeout_s=article_timeout_s, max_attempts=article_attempts)
                    payload = build_fetched_document_payload(
                        request_url=url,
                        result=res,
                        download_article_images=bool(config.get("download_article_images")),
                        max_images=3,
                    )
                    raw_html = payload.get("raw_html")
                    attachments = list(payload.get("attachments") or [])
                    image_urls = list(payload.get("image_urls") or [])
                    normalized_text = str(payload.get("normalized_text") or normalized_text or "").strip()
                    raw_text = normalized_text or raw_text
                    extra_payload = payload.get("extra") or {}
                    if isinstance(extra_payload, dict):
                        item_extra = dict(extra_payload)
                    payload_title = str(payload.get("title") or "").strip() or None
                    if payload_title:
                        pdf_like = bool(item_extra.get("is_pdf") or item_extra.get("content_kind") == "pdf")
                        if (not title) or (pdf_like and len((title or "").strip()) < 18):
                            title = payload_title
                except FetchError as exc:
                    item_extra["article_fetch_error"] = str(exc)[:300]

        items.append(
            {
                "external_id": external_id,
                "url": url,
                "canonical_url": url,
                "title": title,
                "author": author,
                "published_at": published_at,
                "raw_text": raw_text,
                "normalized_text": normalized_text,
                "raw_html": raw_html,
                "image_urls": image_urls,
                "attachments": attachments,
                "extra": {
                    "connector": "rss",
                    "feed_url": feed_url,
                    "title_date_infer": bool(config.get("title_date_infer")),
                    **item_extra,
                },
            }
        )

    return items
