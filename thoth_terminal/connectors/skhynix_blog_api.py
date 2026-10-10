from __future__ import annotations

import html as html_lib
from datetime import datetime

import requests

from thoth_terminal.net import FetchError
from thoth_terminal.text import extract_text_from_html


def _matches_keywords(text: str, *, include: list[str], exclude: list[str]) -> bool:
    t = text.lower()
    if include:
        if not any(k.lower() in t for k in include if k.strip()):
            return False
    if exclude:
        if any(k.lower() in t for k in exclude if k.strip()):
            return False
    return True


def _parse_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    raw = raw.strip()
    for fmt in ("%Y.%m.%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def fetch_skhynix_blog_api(config: dict) -> list[dict]:
    api_url = config.get("api_url") or "https://researchapi.skhynix.com/api/blog/blogListSearch"
    if not api_url:
        raise ValueError("skhynix_blog_api connector requires config.api_url")

    max_items = int(config.get("max_items_per_run", 20) or 20)
    payload = {
        "cpage": int(config.get("page", 1) or 1),
        "blog_cate": list(config.get("blog_categories") or []),
        "show_row": max_items,
        "search_text": str(config.get("search_text") or ""),
    }

    include_keywords = list(config.get("include_keywords") or [])
    exclude_keywords = list(config.get("exclude_keywords") or [])

    try:
        resp = requests.post(api_url, json=payload, timeout=25)
        resp.raise_for_status()
    except Exception as exc:
        raise FetchError(f"Request failed for {api_url}: {exc}", url=str(api_url)) from exc

    try:
        data = resp.json()
    except Exception as exc:
        raise FetchError(f"Invalid JSON from {api_url}: {exc}", url=str(api_url)) from exc

    items: list[dict] = []
    for row in data.get("list") or []:
        title = str(row.get("blog_title") or "").strip() or None
        raw_html = row.get("blog_content") or row.get("list_blog_content") or ""
        raw_html = html_lib.unescape(str(raw_html))
        normalized_text = extract_text_from_html(raw_html, url=None) if raw_html else ""
        published_at = _parse_date(row.get("blog_issue_dt"))

        link = row.get("blog_link")
        seq = row.get("seq")
        url = str(link).strip() if link else f"https://research.skhynix.com/blog?seq={seq}"

        haystack = " ".join([title or "", normalized_text or "", url])
        if not _matches_keywords(haystack, include=include_keywords, exclude=exclude_keywords):
            continue

        items.append(
            {
                "external_id": seq,
                "url": url,
                "canonical_url": url,
                "title": title,
                "author": row.get("blog_author"),
                "published_at": published_at,
                "raw_text": normalized_text or title or url,
                "normalized_text": normalized_text or title or url,
                "raw_html": raw_html,
                "image_urls": [],
                "extra": {
                    "connector": "skhynix_blog_api",
                    "api_url": api_url,
                    "blog_link": link,
                    "blog_cate": row.get("blog_cate"),
                    "blog_cate_nm": row.get("blog_cate_nm"),
                },
            }
        )

    return items
