from __future__ import annotations

import re
import requests
import urllib3
from datetime import datetime, timedelta, timezone
import json
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from thoth_terminal.connectors.document_fetch import build_fetched_document_payload
from thoth_terminal.net import DEFAULT_HEADERS, FetchError, FetchResult, fetch, fetch_with_fallback
from thoth_terminal.text import (
    extract_image_urls_from_html,
    extract_text_from_html,
    extract_title_from_html,
    parse_datetime_iso,
    strip_boilerplate,
)


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


_BLOCK_PATTERNS = (
    "access denied",
    "bm-verify",
    "edgedsuite",
    "request blocked",
    "attention required",
    "verify you are human",
    "cf-chl",
    "challenge-platform",
    "captcha-delivery.com",
    "geo.captcha-delivery.com",
    "datadome captcha",
    'title="datadome captcha"',
    "please enable js and disable any ad blocker",
)

_REUTERS_ARTICLE_RE = re.compile(r"^/(?:[^/?#]+/){1,5}[^/?#]+-\d{4}-\d{2}-\d{2}/?$")
_REUTERS_NON_ARTICLE_SEGMENTS = {
    "account",
    "arc",
    "auth",
    "business/video",
    "graphics",
    "legal",
    "newsletters",
    "podcast",
    "pictures",
    "plus",
    "resizer",
    "video",
    "videos",
    "world/fact-check",
}


def _looks_blocked(html: str | None) -> bool:
    if not html:
        return True
    lowered = html.lower()
    return any(token in lowered for token in _BLOCK_PATTERNS)


def _fetch_simple(url: str, *, timeout_s: float) -> str | None:
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=timeout_s,
            allow_redirects=True,
        )
        return resp.text
    except Exception:
        return None


def _extract_published_at(html: str) -> datetime | None:
    soup = BeautifulSoup(html, "html.parser")

    # Common meta tags
    for selector in [
        ("meta", {"property": "article:published_time"}),
        ("meta", {"name": "pubdate"}),
        ("meta", {"name": "publish_date"}),
        ("meta", {"name": "date"}),
    ]:
        tag = soup.find(selector[0], selector[1])
        if tag and tag.get("content"):
            dt = parse_datetime_iso(tag["content"])
            if dt:
                return dt

    time_tag = soup.find("time")
    if time_tag:
        for attr in ["datetime", "content"]:
            if time_tag.get(attr):
                dt = parse_datetime_iso(time_tag.get(attr))
                if dt:
                    return dt

    return None


def _iter_strings(node):
    if isinstance(node, dict):
        for v in node.values():
            yield from _iter_strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _iter_strings(v)
    elif isinstance(node, str):
        yield node


def _looks_like_reuters_article(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme and parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.netloc or "").lower()
    if host not in {"www.reuters.com", "reuters.com"}:
        return False
    path = parsed.path or ""
    if not path or not _REUTERS_ARTICLE_RE.match(path):
        return False
    lowered = path.strip("/").lower()
    if not lowered:
        return False
    if parsed.query and "rpc=" in parsed.query.lower():
        return False
    if any(f"/{token}/" in f"/{lowered}/" for token in _REUTERS_NON_ARTICLE_SEGMENTS):
        return False
    tail = lowered.rsplit("/", 1)[-1]
    if any(token in tail for token in ("live-updates", "pictures", "graphics", "video")):
        return False
    return True


def _extract_reuters_links(html: str, *, list_url: str, max_items: int) -> list[tuple[str, str | None]]:
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    out: list[tuple[str, str | None]] = []

    def _add(raw_url: str, title_hint: str | None = None) -> None:
        abs_url = urljoin(list_url, raw_url)
        if not _looks_like_reuters_article(abs_url):
            return
        if abs_url in seen:
            return
        seen.add(abs_url)
        out.append((abs_url, title_hint))

    for a in soup.find_all("a", href=True):
        href = a.get("href")
        if not href:
            continue
        title_hint = a.get_text(" ", strip=True) or None
        _add(href, title_hint)
        if len(out) >= max_items:
            return out[:max_items]

    script = soup.find("script", id="__NEXT_DATA__")
    if script and script.string:
        try:
            data = json.loads(script.string)
            for s in _iter_strings(data):
                if isinstance(s, str) and (s.startswith("/")):
                    _add(s, None)
                    if len(out) >= max_items:
                        break
        except Exception:
            pass

    return out[:max_items]


def _fetch_with_optional_insecure_ssl(url: str, *, timeout_s: float, allow_insecure_ssl: bool) -> FetchResult:
    direct_error: FetchError | None = None
    try:
        return fetch(url, timeout_s=timeout_s)
    except FetchError as exc:
        direct_error = exc
    if allow_insecure_ssl:
        try:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            resp = requests.get(
                url,
                headers=DEFAULT_HEADERS,
                timeout=timeout_s,
                allow_redirects=True,
                verify=False,
            )
            resp.raise_for_status()
            return FetchResult(
                url=str(resp.url),
                status_code=resp.status_code,
                content_type=resp.headers.get("content-type"),
                text=resp.text,
                content=resp.content,
            )
        except requests.RequestException:
            pass
    try:
        return fetch_with_fallback(url, timeout_s=timeout_s)
    except FetchError as exc:
        raise (direct_error or exc) from exc


def _resolve_list_url(list_url: str) -> str:
    # Simple templating for date-based ranking/list pages.
    # Example: https://www.sedaily.com/RankAll/al/YYYYMMDD/2
    if "YYYYMMDD" in list_url:
        return list_url.replace("YYYYMMDD", datetime.now(timezone.utc).strftime("%Y%m%d"))
    if "YYYY" in list_url:
        return list_url.replace("YYYY", datetime.now(timezone.utc).strftime("%Y"))
    return list_url


def _collect_candidates(html: str, *, list_url: str, config: dict) -> list[tuple[str, str | None]]:
    allowed_hosts = set(config.get("allowed_hosts") or [])
    include_url_regex = config.get("include_url_regex")
    exclude_url_regex = config.get("exclude_url_regex")
    include_rx = re.compile(include_url_regex) if include_url_regex else None
    exclude_rx = re.compile(exclude_url_regex) if exclude_url_regex else None

    include_keywords = list(config.get("include_keywords") or [])
    exclude_keywords = list(config.get("exclude_keywords") or [])

    max_items = int(config.get("max_items_per_run", 25) or 25)
    soup = BeautifulSoup(html, "html.parser")
    strip_boilerplate(soup)

    seen: set[str] = set()
    candidates: list[tuple[str, str | None]] = []

    def _accept(abs_url: str, title_hint: str | None) -> None:
        parsed = urlparse(abs_url)
        if parsed.scheme and parsed.scheme not in ("http", "https"):
            return
        if abs_url.rstrip("/") == list_url.rstrip("/"):
            return
        parsed_list = urlparse(list_url)
        if (
            parsed.scheme == parsed_list.scheme
            and parsed.netloc == parsed_list.netloc
            and parsed.path.rstrip("/") == parsed_list.path.rstrip("/")
            and parsed.fragment
        ):
            return
        if allowed_hosts and parsed.netloc not in allowed_hosts:
            return
        if include_rx and not include_rx.search(abs_url):
            return
        if exclude_rx and exclude_rx.search(abs_url):
            return
        if title_hint and len(title_hint) < 6:
            return
        haystack = " ".join([title_hint or "", abs_url])
        if not _matches_keywords(haystack, include=include_keywords, exclude=exclude_keywords):
            return
        if abs_url in seen:
            return
        seen.add(abs_url)
        candidates.append((abs_url, title_hint))

    for a in soup.find_all("a", href=True):
        href = a.get("href")
        if not href:
            continue
        abs_url = urljoin(list_url, href)
        title_hint = a.get_text(" ", strip=True) or None
        _accept(abs_url, title_hint)
        if len(candidates) >= max_items:
            break

    if "reuters.com" in urlparse(list_url).netloc and len(candidates) < max_items:
        for abs_url, title_hint in _extract_reuters_links(html, list_url=list_url, max_items=max_items):
            _accept(abs_url, title_hint)
            if len(candidates) >= max_items:
                break

    return candidates


def _build_items(
    candidates: list[tuple[str, str | None]],
    *,
    list_url: str,
    config: dict,
    fetch_article_html,
) -> list[dict]:
    fetch_article = bool(config.get("fetch_article", True))
    recent_days = config.get("recent_days")
    cutoff = None
    try:
        if recent_days is not None:
            days_int = int(recent_days)
            if days_int > 0:
                cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days_int)
    except Exception:
        cutoff = None

    items: list[dict] = []
    include_keywords = list(config.get("include_keywords") or [])
    exclude_keywords = list(config.get("exclude_keywords") or [])

    def _title_hint_needs_replacement(value: str | None, *, url: str) -> bool:
        raw = (value or "").strip()
        if not raw:
            return True
        lowered = raw.lower()
        return lowered.startswith("http://") or lowered.startswith("https://") or lowered == url.lower()

    for url, title_hint in candidates:
        raw_html = None
        normalized_text = ""
        title = title_hint
        published_at = None
        image_urls: list[str] = []
        attachments: list[dict] = []
        article_extra: dict = {}

        if fetch_article:
            try:
                fetched_article = fetch_article_html(url)
                if isinstance(fetched_article, dict):
                    raw_html = fetched_article.get("raw_html")
                    normalized_text = str(fetched_article.get("normalized_text") or "").strip()
                    image_urls = list(fetched_article.get("image_urls") or [])
                    attachments = list(fetched_article.get("attachments") or [])
                    raw_extra = fetched_article.get("extra") or {}
                    if isinstance(raw_extra, dict):
                        article_extra = dict(raw_extra)
                    fetched_title = str(fetched_article.get("title") or "").strip() or None
                    pdf_like = bool(article_extra.get("is_pdf") or article_extra.get("content_kind") == "pdf")
                    if fetched_title and (
                        _title_hint_needs_replacement(title, url=url)
                        or (pdf_like and len((title or "").strip()) < 18)
                    ):
                        title = fetched_title
                    article_published = fetched_article.get("published_at")
                    if isinstance(article_published, datetime):
                        published_at = article_published
                else:
                    raw_html = fetched_article
                    if raw_html:
                        normalized_text = extract_text_from_html(raw_html, url=url)
                        if _title_hint_needs_replacement(title, url=url):
                            title = extract_title_from_html(raw_html, url=url) or title
                        published_at = _extract_published_at(raw_html)
                        if config.get("download_article_images"):
                            image_urls = extract_image_urls_from_html(raw_html, url=url, limit=3)
            except FetchError:
                raw_html = None
        if cutoff and published_at and published_at < cutoff:
            continue
        if not _matches_keywords(
            " ".join(part for part in [title or title_hint or "", url, normalized_text] if part).strip(),
            include=include_keywords,
            exclude=exclude_keywords,
        ):
            continue

        items.append(
            {
                "external_id": None,
                "url": url,
                "canonical_url": url,
                "title": title,
                "author": None,
                "published_at": published_at,
                "raw_text": normalized_text or title_hint or url,
                "normalized_text": normalized_text or title_hint or url,
                "raw_html": raw_html,
                "image_urls": image_urls,
                "attachments": attachments,
                "extra": {"connector": "web_list", "list_url": list_url, **article_extra},
            }
        )

    return items


def fetch_web_list(config: dict) -> list[dict]:
    list_url = config.get("list_url")
    if not list_url:
        raise ValueError("web_list connector requires config.list_url")

    use_browser = bool(config.get("use_browser", False))

    def _fetch_with_browser() -> list[dict]:
        from thoth_terminal.browser_ingestion_runtime import run_browser_ingestion
        from thoth_terminal.connectors.web_list_browser import fetch_web_list_browser

        cfg = dict(config or {})
        if "browser_fetch_articles" not in cfg:
            cfg["browser_fetch_articles"] = bool(cfg.get("fetch_article"))
        if "browser_scrolls" not in cfg and cfg.get("scrolls") is not None:
            cfg["browser_scrolls"] = cfg.get("scrolls")
        return run_browser_ingestion(fetch_web_list_browser, cfg)

    list_timeout_s = float(config.get("list_timeout_s") or 25.0)
    article_timeout_s = float(config.get("article_timeout_s") or 25.0)
    allow_insecure_ssl = bool(config.get("allow_insecure_ssl", False))
    fallback_urls = [list_url] + list(config.get("list_url_fallbacks") or [])
    resolved_urls = [_resolve_list_url(url) for url in fallback_urls if url]
    html = ""
    used_url = _resolve_list_url(list_url)
    last_error: Exception | None = None

    for candidate_url in resolved_urls:
        try:
            res = _fetch_with_optional_insecure_ssl(
                candidate_url,
                timeout_s=list_timeout_s,
                allow_insecure_ssl=allow_insecure_ssl,
            )
        except FetchError as exc:
            last_error = exc
            continue
        html = res.text or ""
        if _looks_blocked(html):
            simple_html = _fetch_simple(candidate_url, timeout_s=list_timeout_s)
            if simple_html:
                html = simple_html
        if html and not _looks_blocked(html):
            used_url = candidate_url
            break
        last_error = FetchError("Blocked or empty list HTML", url=candidate_url)
        html = ""

    if not html:
        if use_browser:
            return _fetch_with_browser()
        if last_error:
            raise last_error
        return []

    list_url = used_url
    candidates = _collect_candidates(html, list_url=list_url, config=config)
    if not candidates and use_browser and config.get("browser_fallback_on_empty", True):
        return _fetch_with_browser()

    def _fetch_html(url: str) -> dict | str | None:
        ares = _fetch_with_optional_insecure_ssl(
            url,
            timeout_s=article_timeout_s,
            allow_insecure_ssl=allow_insecure_ssl,
        )
        payload = build_fetched_document_payload(
            request_url=url,
            result=ares,
            download_article_images=bool(config.get("download_article_images")),
            max_images=3,
        )
        if payload.get("extra", {}).get("is_pdf"):
            return payload
        if ares.text and not _looks_blocked(ares.text):
            return ares.text
        simple_html = _fetch_simple(url, timeout_s=article_timeout_s)
        if simple_html and not _looks_blocked(simple_html):
            return simple_html
        return ares.text or simple_html

    return _build_items(candidates, list_url=list_url, config=config, fetch_article_html=_fetch_html)


def parse_web_list_html(html: str, *, list_url: str, config: dict) -> list[dict]:
    list_url = _resolve_list_url(list_url)
    candidates = _collect_candidates(html, list_url=list_url, config=config)
    return _build_items(candidates, list_url=list_url, config=config, fetch_article_html=lambda _url: None)
