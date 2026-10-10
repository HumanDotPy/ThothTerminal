from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
import os
import re
import time
import logging
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

from thoth_terminal.config import settings
from thoth_terminal.browser_ingestion_runtime import (
    BrowserIngestionError,
    is_shared_browser_profile,
    mark_browser_progress,
)
from thoth_terminal.document_media import build_tweet_media_manifest, canonicalize_x_media_url
from thoth_terminal.runtime_env import resolve_migrated_repo_path
from thoth_terminal.text import clean_whitespace, parse_datetime_iso


_DEFAULT_LINKED_GOOGLE_DOC_HANDLES = frozenset({"pequityresearch"})
_MAX_LINKED_GOOGLE_DOCS = 10
_MAX_LINKED_GOOGLE_DOC_BYTES = 25_000_000
_MAX_LINKED_GOOGLE_DOC_CHARS = 1_000_000
_MAX_TRUSTED_MEDIA_BYTES = 12_000_000
_GOOGLE_DOC_URL_RE = re.compile(
    r"^https?://docs\.google\.com/document/(?:u/\d+/)?d/([A-Za-z0-9_-]+)(?:[/?#].*)?$",
    re.IGNORECASE,
)


def _normalized_x_handle(value: object) -> str:
    return str(value or "").strip().lstrip("@").casefold()


def _linked_google_doc_handles(value: object) -> set[str]:
    if value is None:
        return set(_DEFAULT_LINKED_GOOGLE_DOC_HANDLES)
    if isinstance(value, str):
        values = value.split(",")
    elif isinstance(value, (list, tuple, set, frozenset)):
        values = list(value)
    else:
        values = []
    return {_normalized_x_handle(item) for item in values if _normalized_x_handle(item)}


def _google_doc_id(url: object) -> str | None:
    match = _GOOGLE_DOC_URL_RE.match(str(url or "").strip())
    return match.group(1) if match else None


def _google_doc_export_url(file_id: str) -> str:
    return f"https://docs.google.com/document/d/{file_id}/export?format=txt"


def _fetch_linked_google_docs(
    context: object,
    links: list[str],
    *,
    handle: str | None,
    config: dict,
    timeout_ms: int,
) -> list[dict[str, object]]:
    """Fetch bounded text exports for explicitly trusted X-account links.

    Public/shared Docs are attempted through the existing browser context first;
    the read-only Drive API is used only when the browser export is unavailable.
    The returned attachment bytes are consumed by the normal ingest attachment
    path, while metadata remains JSON-safe for ``Document.extra``.
    """
    allowed_handles = _linked_google_doc_handles(config.get("linked_google_doc_handles"))
    if _normalized_x_handle(handle) not in allowed_handles:
        return []
    try:
        max_docs = max(0, min(int(config.get("max_linked_google_docs", 2) or 0), _MAX_LINKED_GOOGLE_DOCS))
        max_bytes = max(
            0,
            min(
                int(config.get("max_linked_google_doc_bytes", 5_000_000) or 0),
                _MAX_LINKED_GOOGLE_DOC_BYTES,
            ),
        )
        max_chars = max(
            0,
            min(int(config.get("max_linked_google_doc_chars", 200_000) or 0), _MAX_LINKED_GOOGLE_DOC_CHARS),
        )
    except (TypeError, ValueError):
        max_docs, max_bytes, max_chars = 2, 5_000_000, 200_000
    if max_docs <= 0 or max_bytes <= 0 or max_chars <= 0:
        return []

    request_context = getattr(context, "request", None)
    request_get = getattr(request_context, "get", None)
    drive_service: object | None = None
    drive_attempted = False
    out: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    attempts = 0
    max_attempts = max(max_docs * 3, max_docs)
    short_link_attempts = 0
    for link in links:
        source_url = str(link or "").strip()
        # X normally exposes t.co URLs in anchor hrefs. Inspect the redirect
        # without following arbitrary destinations; only Docs exports are read.
        if urlparse(source_url).hostname == "t.co" and callable(request_get):
            if short_link_attempts >= max_attempts:
                continue
            short_link_attempts += 1
            try:
                redirect = request_get(source_url, timeout=timeout_ms, max_redirects=0)
                destination = urljoin(source_url, str(getattr(redirect, "headers", {}).get("location") or ""))
                if _google_doc_id(destination):
                    source_url = destination
                dispose = getattr(redirect, "dispose", None)
                if callable(dispose):
                    dispose()
            except Exception:
                continue
        file_id = _google_doc_id(source_url)
        if not file_id or file_id in seen_ids:
            continue
        seen_ids.add(file_id)
        attempts += 1
        if attempts > max_attempts:
            break
        export_url = _google_doc_export_url(file_id)
        data: bytes | None = None

        if callable(request_get):
            try:
                response = request_get(export_url, timeout=timeout_ms)
                status = int(getattr(response, "status", 200) or 200)
                if bool(getattr(response, "ok", status < 400)) and status < 400:
                    headers = getattr(response, "headers", {})
                    content_type = str(headers.get("content-type") or "").split(";", 1)[0].strip().lower()
                    if content_type != "text/plain":
                        raise ValueError("Google Docs export did not return text/plain")
                    if int(headers.get("content-length") or 0) > max_bytes:
                        raise ValueError("Google Docs export exceeds configured limit")
                    body = getattr(response, "body", None)
                    if callable(body):
                        candidate = body()
                        if isinstance(candidate, (bytes, bytearray)):
                            data = bytes(candidate)
                            probe = data[:1024].decode("utf-8", errors="ignore").lstrip().casefold()
                            if probe.startswith(("<!doctype html", "<html")):
                                data = None
            except Exception:
                data = None

        if data is None:
            if not drive_attempted:
                drive_attempted = True
                try:
                    from thoth_terminal.google_oauth import DRIVE_READONLY_SCOPE, build_service

                    drive_service = build_service("drive", "v3", scopes=[DRIVE_READONLY_SCOPE])
                except Exception:
                    drive_service = None
            try:
                if drive_service is not None:
                    response = (
                        drive_service.files()
                        .export_media(fileId=file_id, mimeType="text/plain")
                        .execute()
                    )
                    if isinstance(response, (bytes, bytearray)):
                        data = bytes(response)
            except Exception:
                data = None

        if not data:
            out.append({"url": source_url, "file_id": file_id, "status": "failed", "failure": "export_failed"})
            continue
        if len(data) > max_bytes:
            out.append({"url": source_url, "file_id": file_id, "status": "failed", "failure": "too_large"})
            continue
        probe = data[:1024].decode("utf-8", errors="ignore").lstrip().casefold()
        if probe.startswith("<!doctype html") or probe.startswith("<html") or ("sign in" in probe and "<" in probe):
            out.append({"url": source_url, "file_id": file_id, "status": "failed", "failure": "non_text_response"})
            continue
        text = clean_whitespace(data.decode("utf-8", errors="ignore"))
        if not text:
            out.append({"url": source_url, "file_id": file_id, "status": "failed", "failure": "empty_export"})
            continue
        truncated = len(text) > max_chars
        if len(text) > max_chars:
            text = text[:max_chars].rstrip()
        out.append(
            {
                "url": source_url,
                "file_id": file_id,
                "status": "ok",
                "truncated": truncated,
                "text": text,
                "attachment": {
                    "kind": "file",
                    "filename": f"google_doc_{file_id}.txt",
                    "content_type": "text/plain",
                    "original_url": source_url,
                    "data_bytes": data,
                    "ext": ".txt",
                },
            }
        )
        if len(out) >= max_docs:
            # Failures do not consume the success budget, but the attempt cap
            # above keeps a malformed list from causing unbounded requests.
            if sum(1 for row in out if row.get("status") == "ok") >= max_docs:
                break
    return out


def _fetch_trusted_media_attachments(
    context: object,
    media_urls: list[str],
    *,
    handle: str | None,
    config: dict,
    timeout_ms: int,
) -> list[dict[str, object]]:
    """Materialize X media for the explicitly trusted account when needed.

    The normal ingest path remains responsible for configured source-wide media
    downloads. This account-scoped fallback only runs when those flags are off,
    so @pequityresearch media is still retained without changing other authors'
    behavior.
    """
    allowed_handles = _linked_google_doc_handles(config.get("linked_google_doc_handles"))
    if _normalized_x_handle(handle) not in allowed_handles:
        return []
    if config.get("download_post_media") or config.get("download_article_images") or config.get("ocr_enabled"):
        return []
    request_context = getattr(context, "request", None)
    request_get = getattr(request_context, "get", None)
    if not callable(request_get):
        return []
    try:
        max_images = max(0, min(int(config.get("max_images_per_doc", 6) or 0), 12))
        max_bytes = max(
            0,
            min(int(config.get("max_image_bytes", 5_000_000) or 0), _MAX_TRUSTED_MEDIA_BYTES),
        )
    except (TypeError, ValueError):
        max_images, max_bytes = 6, 5_000_000
    if max_images <= 0 or max_bytes <= 0:
        return []

    out: list[dict[str, object]] = []
    seen: set[str] = set()
    for media_url in media_urls:
        source_url = str(media_url or "").strip()
        if not source_url or source_url in seen:
            continue
        parsed = urlparse(source_url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"pbs.twimg.com", "abs.twimg.com", "ton.twimg.com"}:
            continue
        seen.add(source_url)
        try:
            response = request_get(source_url, timeout=timeout_ms)
            status = int(getattr(response, "status", 200) or 200)
            if not bool(getattr(response, "ok", status < 400)) or status >= 400:
                continue
            body = getattr(response, "body", None)
            data = body() if callable(body) else None
            if not isinstance(data, (bytes, bytearray)):
                continue
            data_bytes = bytes(data)
            if not data_bytes or len(data_bytes) > max_bytes:
                continue
            headers = getattr(response, "headers", {})
            content_type = str(headers.get("content-type") or "image/jpeg").split(";", 1)[0].strip().lower()
            if not content_type.startswith("image/"):
                continue
            out.append(
                {
                    "kind": "image",
                    "content_type": content_type,
                    "original_url": source_url,
                    "data_bytes": data_bytes,
                }
            )
        except Exception:
            continue
        if len(out) >= max_images:
            break
    return out


def _close_browser_resource(resource: object) -> None:
    try:
        getattr(resource, "close")()
    except Exception:
        pass


@dataclass(frozen=True)
class TweetData:
    url: str
    tweet_id: str | None
    text: str
    quoted_text: str | None
    quoted_url: str | None
    author: str | None
    handle: str | None
    created_at: str | None
    links: list[str]
    media: list[str]
    article_url: str | None = None


@dataclass(frozen=True)
class ThreadContext:
    ancestors: list[TweetData]
    replies: list[TweetData]


@dataclass(frozen=True)
class XArticleData:
    url: str
    title: str | None
    text: str | None
    media: list[str]
    links: list[str]
    media_manifest: list[dict[str, object]] | None = None


def _abs_url(href: str | None) -> str | None:
    if not href:
        return None
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("/"):
        return urljoin("https://x.com", href)
    return href


def _canonical_x_article_url(url: str | None) -> str | None:
    value = str(url or "").strip()
    if not value:
        return None
    return re.sub(r"/media/\d+(?:[/?#].*)?$", "", value) or None


def _original_x_media_url(url: str) -> str:
    return canonicalize_x_media_url(url)


def _extract_links(article) -> list[str]:
    links: list[str] = []
    for a in article.query_selector_all("a[href]"):
        href = _abs_url(a.get_attribute("href"))
        if not href or not href.startswith("http"):
            continue
        if "x.com/" in href or "twitter.com/" in href:
            if "/article/" in href:
                if "/media/" in href:
                    continue
                links.append(href)
                continue
            if "/status/" in href:
                continue
            # skip profile/nav links
            continue
        links.append(href)
    # De-dupe while preserving order
    out: list[str] = []
    seen: set[str] = set()
    for l in links:
        if l in seen:
            continue
        seen.add(l)
        out.append(l)
    return out


def _extract_article_url(article) -> str | None:
    fallback = None
    for link in article.query_selector_all('a[href*="/article/"]'):
        href = _abs_url(link.get_attribute("href"))
        if not href:
            continue
        if "/media/" in href:
            fallback = fallback or _canonical_x_article_url(href)
            continue
        return _canonical_x_article_url(href)
    if fallback:
        return fallback
    try:
        if article.query_selector('[data-testid="twitterArticleReadView"], [data-testid="twitter-article-title"]'):
            status_url = _extract_status_url(article)
            if status_url and "/status/" in status_url:
                return status_url.replace("/status/", "/article/", 1)
    except Exception:
        pass
    return None


def _extract_media(article) -> list[str]:
    urls: list[str] = []
    for img in article.query_selector_all("img"):
        src = (img.get_attribute("src") or "").strip()
        if not src:
            continue
        if "pbs.twimg.com/media" in src or "twimg.com/media" in src:
            urls.append(_original_x_media_url(src))
    out: list[str] = []
    seen: set[str] = set()
    for u in urls:
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def _extract_handle(article) -> str | None:
    for a in article.query_selector_all("a"):
        txt = (a.inner_text() or "").strip()
        if txt.startswith("@") and len(txt) > 1:
            return txt.lstrip("@")
    return None


def _extract_author(article) -> str | None:
    el = article.query_selector('div[data-testid="User-Name"] span')
    if el:
        txt = (el.inner_text() or "").strip()
        return txt or None
    return None


def _extract_texts(article) -> list[str]:
    texts = []
    for el in article.query_selector_all('[data-testid="tweetText"]'):
        txt = (el.inner_text() or "").strip()
        if txt:
            texts.append(txt)
    return texts


def _extract_status_url(article) -> str | None:
    time_el = article.query_selector("time")
    if time_el:
        try:
            href = time_el.evaluate(
                """(el) => {
                    const link = el.closest('a[href*="/status/"]');
                    return link ? link.getAttribute("href") : null;
                }"""
            )
            abs_href = _abs_url(href)
            if abs_href and "/status/" in abs_href:
                return abs_href
        except Exception:
            pass
    for a in article.query_selector_all('a[href*="/status/"]'):
        href = a.get_attribute("href") or ""
        if "/status/" not in href:
            continue
        return _abs_url(href)
    return None


def _extract_quoted_url(article) -> str | None:
    q = article.query_selector('div[aria-label="Quoted Tweet"]')
    if not q:
        return None
    link = q.query_selector('a[href*="/status/"]')
    if link:
        return _abs_url(link.get_attribute("href"))
    return None


def _parse_article(article) -> TweetData | None:
    url = _extract_status_url(article)
    if not url:
        return None
    tweet_id = None
    if "/status/" in url:
        try:
            tweet_id = url.split("/status/", 1)[1].split("?", 1)[0]
        except Exception:
            tweet_id = None
    texts = _extract_texts(article)
    text = texts[0] if texts else ""
    quoted_text = texts[1] if len(texts) > 1 else None
    quoted_url = _extract_quoted_url(article)
    author = _extract_author(article)
    handle = _extract_handle(article)
    time_el = article.query_selector("time")
    created_at = time_el.get_attribute("datetime") if time_el else None
    links = _extract_links(article)
    media = _extract_media(article)
    article_url = _extract_article_url(article)
    return TweetData(
        url=str(url),
        tweet_id=tweet_id,
        text=text,
        quoted_text=quoted_text,
        quoted_url=quoted_url,
        author=author,
        handle=handle,
        created_at=created_at,
        links=links,
        media=media,
        article_url=article_url,
    )


def _detect_login(page) -> bool:
    """Heuristic: return True only when clearly on a login wall."""
    try:
        url = (page.url or "").lower()
        if "/login" in url or "/i/flow/login" in url:
            return True
    except Exception:
        return False
    try:
        if page.query_selector('input[name="session[username_or_email]"], input[name="text"], input[name="password"]'):
            return True
    except Exception:
        return False
    try:
        if page.query_selector("div[data-testid='LoginForm_Login_Button']"):
            return True
        if page.query_selector("a[href*='/login'][role='link']"):
            return True
    except Exception:
        return False
    return False


def _detect_page_issue(page) -> str | None:
    try:
        body = page.inner_text("body") or ""
    except Exception:
        return None
    if not body:
        return None
    if re.search(r"Something went wrong|Try again|rate limit|temporarily unavailable|Retry", body, re.I):
        return "page_error"
    return None


def _ensure_on_list(page, *, list_url: str, list_id: str | None, list_title: str | None = None, timeout_ms: int = 8000) -> bool:
    """Try to end up on the specific list page, even if Twitter bounces us to /i/lists or /<user>/lists."""
    import time as _time
    import re as _re

    deadline = _time.time() + (timeout_ms / 1000.0)
    target_id = list_id or ""
    title_token = (list_title or "").strip().lower() or list_url.rstrip("/").split("/")[-1].lower()
    attempts = 0
    max_attempts = 5
    log = logging.getLogger("thoth.x_list")

    def _is_on_target(url: str) -> bool:
        if "/lists/" not in url:
            return False
        if target_id and target_id in url:
            return True
        if title_token and title_token in url.lower():
            return True
        return False

    def _is_wrong_lists_page(url: str) -> bool:
        """Detect if we got bounced to a user's lists hub (not our target list)."""
        # Pattern: /<username>/lists (without the specific list id after)
        # vs /i/lists/<id> or /<user>/lists/<id>
        if "/lists" not in url:
            return False
        if target_id and target_id in url:
            return False
        # Check if URL ends with /lists or /lists/ (no list ID)
        if _re.search(r"/lists/?$", url):
            return True
        # Also catch /<user>/lists without an ID following
        if _re.search(r"/[^/]+/lists/?$", url) and target_id not in url:
            return True
        return False

    while _time.time() < deadline and attempts < max_attempts:
        attempts += 1
        try:
            cur = page.url or ""
        except Exception:
            break

        log.info("[nav] attempt %s url=%s", attempts, cur)

        if _is_on_target(cur):
            return True

        # If we got redirected to a user's lists page (not our target), force re-navigate
        if _is_wrong_lists_page(cur):
            log.warning("Detected wrong lists page: %s (wanted list %s), forcing re-navigation", cur, target_id)
            try:
                # Use 'commit' wait to catch early redirects
                page.goto(list_url, wait_until="commit")
                page.wait_for_timeout(500)
                # Now wait for content
                page.wait_for_load_state("domcontentloaded")
                page.wait_for_timeout(800)
                cur = page.url or ""
                if _is_on_target(cur):
                    return True
                # If still wrong, try with networkidle
                if _is_wrong_lists_page(cur):
                    page.goto(list_url, wait_until="networkidle", timeout=timeout_ms // 2)
                    page.wait_for_timeout(1000)
                    cur = page.url or ""
                    if _is_on_target(cur):
                        return True
            except Exception as e:
                log.debug("Re-navigation failed: %s", e)
            continue

        # If we are on a lists hub, try clicking into the list (by id or by title text), or just click first cell.
        if ("/i/lists" in cur) or _is_wrong_lists_page(cur):
            try:
                # Look for the target list card/link and click it.
                link = (
                    (page.query_selector(f"a[href*='{target_id}']") if target_id else None)
                    or (page.query_selector(f"a[href$='{target_id}']") if target_id else None)
                    or (page.query_selector(f"[role='link'][href*='{target_id}']") if target_id else None)
                )
                if link:
                    link.click()
                    page.wait_for_load_state("domcontentloaded")
                    page.wait_for_timeout(700)
                    cur = page.url or ""
                    if _is_on_target(cur):
                        return True
                else:
                    # JS fallback: find any link containing the id or the list title text
                    ok = page.evaluate(
                        """
(() => {
  const id = "%(id)s";
  const title = "%(title)s";
  const els = [...document.querySelectorAll('a, [role="link"]')];
  const target = els.find(el => {
    const href = (el.href || '');
    const txt = (el.textContent || '').toLowerCase();
    return (id && href.includes(id)) || (title && txt.includes(title));
  });
  if (target) { target.click(); return true; }
  return false;
})()
"""
                        % {"id": target_id, "title": title_token}
                    )
                    if ok:
                        page.wait_for_load_state("domcontentloaded")
                        page.wait_for_timeout(700)
                        cur = page.url or ""
                        if _is_on_target(cur):
                            return True
            except Exception:
                pass

        # Fallback: re-request the exact URL with different strategies
        try:
            # Try going to home first, then to the list (breaks redirect chain)
            page.goto("https://x.com/home", wait_until="domcontentloaded")
            page.wait_for_timeout(500)
            page.goto(list_url, wait_until="domcontentloaded")
            page.wait_for_timeout(800)
            cur = page.url or ""
            if _is_on_target(cur):
                # Stabilize: watch for immediate bounce; if bounced, retry loop.
                bounced = False
                for _ in range(3):
                    page.wait_for_timeout(600)
                    cur2 = page.url or ""
                    if not _is_on_target(cur2):
                        bounced = True
                        break
                if not bounced:
                    return True
        except Exception:
            pass
        page.wait_for_timeout(350)

    return False


def _activate_page(page) -> None:
    try:
        main = page.query_selector("main")
        if main:
            box = main.bounding_box()
            if box and box.get("width") and box.get("height"):
                # Click near the center of the main content to avoid header/back buttons.
                x = box["x"] + (box["width"] * 0.5)
                y = box["y"] + min(box["height"] * 0.4, box["height"] - 10)
                page.mouse.click(x, y)
                return
    except Exception:
        pass
    try:
        size = page.viewport_size
        if size and size.get("width") and size.get("height"):
            page.mouse.click(size["width"] / 2, size["height"] / 2)
            return
    except Exception:
        pass


def _wait_for_tweets(page, *, max_ms: int, poll_ms: int = 800) -> tuple[bool, str | None]:
    deadline = time.time() + (max_ms / 1000.0)
    while time.time() < deadline:
        if _detect_login(page):
            return False, "login_required"
        issue = _detect_page_issue(page)
        if issue:
            return False, issue
        if page.query_selector('article[data-testid="tweet"]'):
            return True, None
        page.wait_for_timeout(poll_ms)
    return False, "timeout_waiting_for_tweets"


def _maybe_int(value: object, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _compose_thread_block(label: str, items: list[TweetData]) -> str | None:
    rows = [t.text for t in items if t.text]
    if not rows:
        return None
    return f"{label}:\n" + "\n\n".join(rows)


def _compose_text(
    data: TweetData,
    thread: ThreadContext,
    *,
    article: XArticleData | None = None,
) -> str:
    parts: list[str] = []
    if data.text:
        parts.append(data.text)
    if article and article.text:
        article_block = article.text
        if article.title and article.title not in article_block:
            article_block = f"{article.title}\n\n{article_block}".strip()
        parts.append(f"X article:\n{article_block}")
    elif data.article_url:
        parts.append(f"X article link:\n{data.article_url}")
    if data.quoted_text:
        parts.append(f"Quoted tweet:\n{data.quoted_text}")
    before = _compose_thread_block("Earlier in same-author thread", thread.ancestors)
    if before:
        parts.append(before)
    after = _compose_thread_block("Later replies in same-author thread", thread.replies)
    if after:
        parts.append(after)
    return clean_whitespace("\n\n".join(parts))


def _compose_links(
    data: TweetData,
    thread: ThreadContext,
    *,
    article: XArticleData | None = None,
) -> list[str]:
    links: list[str] = []
    links.extend(data.links or [])
    if article:
        links.extend(article.links or [])
    for t in [*thread.ancestors, *thread.replies]:
        links.extend(t.links or [])
    out: list[str] = []
    seen: set[str] = set()
    for l in links:
        if l in seen:
            continue
        seen.add(l)
        out.append(l)
    return out


def _compose_media(
    data: TweetData,
    thread: ThreadContext,
    *,
    quoted: TweetData | None = None,
    article: XArticleData | None = None,
) -> list[str]:
    media: list[str] = []
    media.extend(data.media or [])
    if article:
        media.extend(article.media or [])
    if quoted:
        media.extend(quoted.media or [])
    for t in [*thread.ancestors, *thread.replies]:
        media.extend(t.media or [])
    out: list[str] = []
    seen: set[str] = set()
    for m in media:
        if m in seen:
            continue
        seen.add(m)
        out.append(m)
    return out


def _fetch_thread_context(
    context,
    url: str,
    handle: str | None,
    max_parts: int,
    *,
    wait_ms: int,
    timeout_ms: int,
    until_url: str | None = None,
) -> ThreadContext:
    if not url:
        return ThreadContext(ancestors=[], replies=[])
    page = context.new_page()
    try:
        page.set_default_timeout(timeout_ms)
        page.set_default_navigation_timeout(timeout_ms)
        page.goto(url, wait_until="domcontentloaded")
        if wait_ms > 0:
            page.wait_for_timeout(wait_ms)
        collected: list[TweetData] = []
        collected_ids: set[str] = set()
        if until_url:
            target_id = until_url.split("/status/", 1)[-1]
            stale_scrolls = 0
            for _ in range(120):
                visible = page.query_selector_all('article[data-testid="tweet"]')
                added = 0
                for article in visible:
                    article_url = _extract_status_url(article)
                    if not article_url or article_url in collected_ids:
                        continue
                    parsed = _parse_article(article)
                    if parsed:
                        collected.append(parsed)
                        collected_ids.add(parsed.url)
                        added += 1
                if any(item.tweet_id == target_id for item in collected):
                    break
                stale_scrolls = 0 if added else stale_scrolls + 1
                if stale_scrolls >= 6:
                    break
                page.mouse.wheel(0, 850)
                page.wait_for_timeout(450)
        articles = page.query_selector_all('article[data-testid="tweet"]')
        ordered: list[TweetData] = []
        seen_urls: set[str] = set()
        root_index: int | None = None
        for data in [*collected, *(_parse_article(art) for art in articles)]:
            if not data:
                continue
            if data.url in seen_urls:
                continue
            seen_urls.add(data.url)
            if handle and data.handle and data.handle.casefold() != handle.casefold():
                continue
            if data.tweet_id == url.split("/status/", 1)[-1] and root_index is None:
                root_index = len(ordered)
                ordered.append(data)
                continue
            ordered.append(data)
            if max_parts and len(ordered) >= (max_parts + 1):
                break
        if root_index is None:
            replies = [item for item in ordered if item.url != url]
            if max_parts > 0:
                replies = replies[:max_parts]
            return ThreadContext(ancestors=[], replies=replies)
        ancestors = [item for item in ordered[:root_index] if item.url != url]
        replies = [item for item in ordered[root_index + 1 :] if item.url != url]
        if max_parts > 0:
            if len(ancestors) >= max_parts:
                ancestors = ancestors[-max_parts:]
                replies = []
            else:
                replies = replies[: max_parts - len(ancestors)]
        return ThreadContext(ancestors=ancestors, replies=replies)
    finally:
        page.close()


def _fetch_single(context, url: str, *, wait_ms: int, timeout_ms: int, expected_tweet_id: str | None = None) -> TweetData | None:
    if not url:
        return None
    page = context.new_page()
    try:
        page.set_default_timeout(timeout_ms)
        page.set_default_navigation_timeout(timeout_ms)
        page.goto(url, wait_until="domcontentloaded")
        if wait_ms > 0:
            page.wait_for_timeout(wait_ms)
        for article in page.query_selector_all('article[data-testid="tweet"]'):
            parsed = _parse_article(article)
            if parsed and (expected_tweet_id is None or parsed.tweet_id == expected_tweet_id):
                return parsed
        return None
    finally:
        page.close()


def _fetch_x_article(
    context,
    url: str,
    *,
    author: str | None = None,
    handle: str | None = None,
    wait_ms: int,
    timeout_ms: int,
) -> XArticleData | None:
    if not url:
        return None
    page = context.new_page()
    try:
        page.set_default_timeout(timeout_ms)
        page.set_default_navigation_timeout(timeout_ms)
        page.goto(url, wait_until="domcontentloaded")
        if wait_ms > 0:
            page.wait_for_timeout(wait_ms)
        tweet_root = page.query_selector('article[data-testid="tweet"]') or page.query_selector("main")
        if not tweet_root:
            return XArticleData(url=url, title=None, text=None, media=[], links=[], media_manifest=[])
        article = (
            tweet_root.query_selector('[data-testid="twitterArticleReadView"]')
            or tweet_root.query_selector('[data-testid="twitterArticleRichTextView"]')
            or tweet_root
        )
        text_rows = article.evaluate(
            """(root) => {
                const skip = /^(Log in|Sign up|Reply|Replies|Repost|Like|Bookmark|Share post|Views?|Focus mode|Conversation|Read \\d+ replies|See new posts|More|Verified account|Image|Article|Upgrade to Premium)$/i;
                const nodes = [];
                const seen = new Set();
                const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
                while (walker.nextNode()) {
                    const txt = (walker.currentNode.textContent || "").replace(/\\s+/g, " ").trim();
                    if (!txt || txt.length < 3) continue;
                    if (skip.test(txt)) continue;
                    if (/^\\d+(\\.\\d+)?[KMB]?$/.test(txt)) continue;
                    if (seen.has(txt)) continue;
                    seen.add(txt);
                    nodes.push(txt);
                    if (nodes.length >= 220) break;
                }
                return nodes;
            }"""
        )
        media_manifest = article.evaluate(
            """(root) => Array.from(root.querySelectorAll('img')).map((img, idx) => {
                const src = img.currentSrc || img.src || img.getAttribute('data-src') || '';
                const before = node => Boolean(
                    node.compareDocumentPosition(img) & Node.DOCUMENT_POSITION_FOLLOWING
                );
                const after = node => Boolean(
                    node.compareDocumentPosition(img) & Node.DOCUMENT_POSITION_PRECEDING
                );
                const headings = Array.from(root.querySelectorAll('h1,h2,h3')).filter(before);
                const preceding = Array.from(root.querySelectorAll('p,div[dir="auto"]'))
                    .filter(before)
                    .slice(-2)
                    .map(node => (node.innerText || '').replace(/\\s+/g, ' ').trim())
                    .filter(Boolean);
                const following = Array.from(root.querySelectorAll('p,div[dir="auto"]'))
                    .filter(after)
                    .slice(0, 1)
                    .map(node => (node.innerText || '').replace(/\\s+/g, ' ').trim())
                    .filter(Boolean);
                return {
                    url: src,
                    kind: 'image',
                    label: `Article image ${idx + 1}`,
                    section: 'article',
                    position: idx + 1,
                    dom_index: idx + 1,
                    alt: (img.getAttribute('alt') || '').trim(),
                    heading: (headings.length ? headings[headings.length - 1].innerText : '').trim(),
                    preceding_text: preceding.join('\n'),
                    following_text: following.join('\n')
                };
            }).filter(row => row.url)"""
        )
        media = _extract_media(article)
        links = _extract_links(article)
        title = None
        title_element = tweet_root.query_selector('[data-testid="twitter-article-title"]')
        if title_element:
            title = clean_whitespace(title_element.inner_text() or "")[:240] or None
        if isinstance(text_rows, list) and text_rows:
            filtered = [str(row or "").strip() for row in text_rows if str(row or "").strip()]
            if author and filtered and filtered[0].strip().lower() == str(author).strip().lower():
                filtered = filtered[1:]
            if handle and filtered and filtered[0].lstrip("@").strip().lower() == str(handle).lstrip("@").strip().lower():
                filtered = filtered[1:]
            first = str(filtered[0] or "").strip() if filtered else ""
            if not title and first and not first.startswith("@"):
                title = first[:240]
        text = ""
        if isinstance(text_rows, list):
            filtered = [str(row or "").strip() for row in text_rows if str(row or "").strip()]
            if author and filtered and filtered[0].strip().lower() == str(author).strip().lower():
                filtered = filtered[1:]
            if handle and filtered and filtered[0].lstrip("@").strip().lower() == str(handle).lstrip("@").strip().lower():
                filtered = filtered[1:]
            text = clean_whitespace("\n\n".join(filtered))
        return XArticleData(
            url=url,
            title=title,
            text=text or None,
            media=media,
            links=links,
            media_manifest=[dict(row) for row in media_manifest if isinstance(row, dict)] if isinstance(media_manifest, list) else [],
        )
    finally:
        page.close()


def _fetch_x_list_once(config: dict) -> list[dict]:
    mark_browser_progress(config, "x:preparing")
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "Playwright not installed. Run: pip install playwright && python -m playwright install chromium"
        ) from exc
    status_url = (config.get("status_url") or "").strip()
    bottom_url = (config.get("bottom_url") or "").strip()
    list_url = (config.get("list_url") or "").strip()
    if status_url:
        from thoth_terminal.discord.link_intake import canonical_x_status_url
        status_url = canonical_x_status_url(status_url) or ""
        if not status_url:
            raise ValueError("Invalid X status URL")
        if bottom_url:
            bottom_url = canonical_x_status_url(bottom_url) or ""
            if not bottom_url:
                raise ValueError("Invalid X bottom status URL")
    if not list_url and not status_url:
        raise ValueError("x_list connector requires config.list_url")

    configured_storage_state = config.get("storage_state_path")
    storage_state = configured_storage_state or getattr(settings, "x_storage_state_path", "")
    if not storage_state:
        default_state = settings.data_dir / "x_storage_state.json"
        if default_state.exists():
            storage_state = str(default_state)
    if storage_state:
        storage_state = str(resolve_migrated_repo_path(storage_state))
    if configured_storage_state and (not storage_state or not Path(storage_state).exists()):
        raise ValueError(f"Configured X storage_state_path does not exist: {configured_storage_state}")
    user_data_dir = config.get("user_data_dir") or config.get("browser_user_data_dir") or getattr(
        settings, "x_user_data_dir", None
    )
    if user_data_dir:
        configured_user_data_dir = user_data_dir
        user_data_dir = resolve_migrated_repo_path(user_data_dir)
        if not user_data_dir.exists():
            logging.getLogger("thoth.x_list").warning(
                "Configured user_data_dir does not exist: %s; falling back to storage_state/new context.",
                configured_user_data_dir,
            )
            user_data_dir = None
        else:
            user_data_dir = str(user_data_dir)
            if is_shared_browser_profile(user_data_dir):
                raise BrowserIngestionError(
                    "Refusing X ingestion against the normal Edge/Chrome profile. "
                    "Use storage_state_path or a dedicated Thoth browser profile outside the browser User Data directory."
                )
    headless = bool(config.get("headless", True))
    browser_channel = config.get("browser_channel")
    profile_directory = config.get("profile_directory") or config.get("browser_profile_directory")
    slow_mo_ms = _maybe_int(config.get("slow_mo_ms", 0), 0)
    scrolls = _maybe_int(config.get("scrolls", 2), 2)
    max_items = _maybe_int(config.get("max_items_per_run", 30), 30)
    include_thread = bool(config.get("include_thread", True))
    max_thread_replies = _maybe_int(config.get("max_thread_replies", 50 if status_url else 6), 6)
    include_quoted = bool(config.get("include_quoted", True))
    include_articles = bool(config.get("include_articles", True))
    # Use slower, human-like defaults to reduce bounce/redirects.
    wait_ms = _maybe_int(config.get("wait_after_load_ms", 4500), 4500)
    scroll_delay_ms = _maybe_int(config.get("scroll_delay_ms", 3200), 3200)
    stabilize_ms = _maybe_int(config.get("stabilize_ms", max(wait_ms, 5000)), max(wait_ms, 5000))
    scroll_px = _maybe_int(config.get("scroll_px", 700), 700)
    timeout_ms = _maybe_int(config.get("timeout_ms", 120000), 120000)
    save_storage_state = bool(config.get("save_storage_state", False))
    # Use a realistic desktop UA to avoid X "browser not secure" warnings.
    user_agent = (config.get("user_agent") or "").strip() or (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0"
    )

    # Extract list id for fallback navigation
    import re as _re  # local alias to avoid shadowing
    # Accept either /i/lists/<id> or /lists/<id>
    m = _re.search("/lists/(\\d+)", list_url)
    list_id = m.group(1) if m else None
    # If we have no list id, trying to recover is pointless.
    if not list_id and not status_url:
        raise RuntimeError(f"Invalid list_url; could not find list id in {list_url}")
    list_title = (config.get("list_title") or "").strip() or None
    nav_log = logging.getLogger("thoth.x_list")

    items: list[dict] = []
    debug_dir = settings.data_dir / "debug_screens"
    debug_dir.mkdir(parents=True, exist_ok=True)
    enable_trace = os.getenv("THOTH_PLAYWRIGHT_TRACE") in {"1", "true", "TRUE", "yes", "on"}
    with sync_playwright() as p, ExitStack() as cleanup:
        launch_kwargs: dict[str, object] = {"headless": headless}
        # Auto-select Edge channel when user_data_dir points to an Edge profile and no channel was set.
        if not browser_channel and user_data_dir and "edge" in str(user_data_dir).lower():
            browser_channel = "msedge"
        if browser_channel:
            launch_kwargs["channel"] = str(browser_channel)
        if slow_mo_ms > 0:
            launch_kwargs["slow_mo"] = slow_mo_ms
        # Try to look less like automation (helps with X login blocks).
        args = list(launch_kwargs.get("args") or [])
        args.extend(
            [
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
            ]
        )
        launch_kwargs["args"] = args
        browser = None
        context_kwargs: dict[str, object] = {
            "user_agent": user_agent,
            "viewport": {"width": 1366, "height": 900},
            "locale": "en-US",
            "timezone_id": "America/New_York",
        }
        if storage_state and Path(storage_state).exists():
            context_kwargs["storage_state"] = storage_state
        if user_data_dir:
            if profile_directory:
                launch_args = list(launch_kwargs.get("args") or [])
                launch_args.append(f"--profile-directory={profile_directory}")
                launch_kwargs["args"] = launch_args
            # storage_state is ignored for persistent contexts; keep profile-based state.
            context_kwargs.pop("storage_state", None)
            context = p.chromium.launch_persistent_context(
                str(user_data_dir),
                **launch_kwargs,
                **context_kwargs,
            )
        else:
            browser = p.chromium.launch(**launch_kwargs)
            context = browser.new_context(**context_kwargs)
        if browser is not None:
            cleanup.callback(_close_browser_resource, browser)
        cleanup.callback(_close_browser_resource, context)
        if enable_trace:
            context.tracing.start(screenshots=True, snapshots=True, sources=True)
        context.set_default_timeout(timeout_ms)
        context.set_default_navigation_timeout(timeout_ms)

        page = context.new_page()
        cleanup.callback(_close_browser_resource, page)
        # Hide webdriver flag to reduce automation detection.
        try:
            page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        except Exception:
            pass
        page.on("framenavigated", lambda frame: nav_log.info("[nav] frame=%s", frame.url))

        def _snap(label: str) -> None:
            try:
                ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
                fname = debug_dir / f"x_list_{label}_{ts}.png"
                page.screenshot(path=str(fname), full_page=True)
                logging.getLogger("thoth.x_list").info("Saved debug screenshot: %s", fname)
            except Exception:
                pass

        def _click_by_title(title: str) -> bool:
            if not title:
                return False
            try:
                el = page.get_by_role("link", name=title, exact=True)
                el.hover()
                page.wait_for_timeout(500)
                el.click(timeout=2500)
                page.wait_for_load_state("domcontentloaded")
                page.wait_for_timeout(800)
                nav_log.info("[nav] clicked list via role link text=%s", title)
                return True
            except Exception:
                try:
                    el = page.get_by_text(title, exact=True)
                    el.hover()
                    page.wait_for_timeout(500)
                    el.click(timeout=2500)
                    page.wait_for_load_state("domcontentloaded")
                    page.wait_for_timeout(800)
                    nav_log.info("[nav] clicked list via text match text=%s", title)
                    return True
                except Exception:
                    return False

        def _click_first_list_cell() -> bool:
            try:
                cell = page.query_selector("[data-testid='ListCell']") or page.query_selector("[data-testid='cellInnerDiv']")
                if cell:
                    cell.scroll_into_view_if_needed()
                    page.wait_for_timeout(400)
                    cell.click()
                    page.wait_for_load_state("domcontentloaded")
                    page.wait_for_timeout(800)
                    nav_log.info("[nav] clicked first ListCell")
                    return True
            except Exception as e:
                nav_log.debug("click_first_list_cell failed: %s", e)
            return False

        try:
            if status_url:
                page.goto(status_url, wait_until="domcontentloaded")
                if wait_ms > 0:
                    page.wait_for_timeout(wait_ms)
                _activate_page(page)
                ok, reason = _wait_for_tweets(page, max_ms=timeout_ms)
                if not ok:
                    if reason == "login_required":
                        raise RuntimeError("X authentication is required; refresh the configured storage state.")
                    raise RuntimeError(f"X status not ready: {reason}")
                requested_id = status_url.split("/status/", 1)[1]
                articles = [
                    article for article in page.query_selector_all('article[data-testid="tweet"]')
                    if (parsed := _parse_article(article)) and parsed.tweet_id == requested_id
                ]
                if not articles:
                    raise RuntimeError("Requested X status was not found on its page")
            else:
                # Step 1: open home to ensure session cookies are fresh.
                if page.is_closed():
                    page = context.new_page()
                page.goto("https://x.com/home", wait_until="domcontentloaded")
                nav_log.info("[nav] opened home, url=%s", page.url)
                page.wait_for_timeout(1200)

                # Click the Lists entry via More -> Lists (works when Lists is hidden under More).
                try:
                    if page.is_closed():
                        page = context.new_page()
                    more = None
                    # Prefer the tabbar testid to avoid strict-mode multiple matches.
                    try:
                        more = page.get_by_test_id("AppTabBar_More_Menu")
                    except Exception:
                        more = None
                    if not more:
                        try:
                            more = page.get_by_role("button", name="More", exact=True)
                        except Exception:
                            more = None
                    if more:
                        more.hover()
                        page.wait_for_timeout(400)
                        more.click(timeout=4000)
                        page.wait_for_timeout(500)
                    lists_icon = None
                    try:
                        lists_icon = page.get_by_role("link", name="Lists")
                    except Exception:
                        lists_icon = None
                    if not lists_icon:
                        try:
                            lists_icon = page.get_by_role("menuitem", name="Lists")
                        except Exception:
                            lists_icon = None
                    if lists_icon:
                        lists_icon.hover()
                        page.wait_for_timeout(400)
                        lists_icon.click(timeout=4000)
                        page.wait_for_load_state("domcontentloaded")
                        nav_log.info("[nav] clicked Lists via More, url=%s", page.url)
                    else:
                        nav_log.info("Lists entry not found; will fall back to direct list URL")
                except Exception as e:
                    nav_log.warning("Failed to click Lists via More: %s", e)
                    if page.is_closed():
                        nav_log.warning("Lists click failed because page closed; reopening page and continuing")
                        page = context.new_page()
                        page.goto("https://x.com/home", wait_until="domcontentloaded")
                        page.wait_for_timeout(400)

                # Small scroll to bring lists into view
                try:
                    page.mouse.wheel(0, 600)
                    page.wait_for_timeout(400)
                except Exception:
                    pass

                # Step 2a: if already on a lists hub, try clicking the list before direct URL.
                if "/lists" in (page.url or "") and list_title:
                    if _click_by_title(list_title):
                        nav_log.info("[nav] success via title click pre-url")
                # Step 2b: load the list directly.
                if page.is_closed():
                    page = context.new_page()
                page.goto(list_url, wait_until="domcontentloaded")
                nav_log.info("[nav] goto list_url first pass url=%s", page.url)
                if list_url not in page.url:
                    page.goto(list_url, wait_until="networkidle")
                    nav_log.info("[nav] goto list_url second pass url=%s", page.url)
                # If we land on home (not list) and have a title, try clicking by title before retries.
                if "/home" in (page.url or "") and list_title:
                    if _click_by_title(list_title):
                        nav_log.info("[nav] clicked title from home after direct goto")

                def _on_target() -> bool:
                    return _ensure_on_list(
                        page,
                        list_url=list_url,
                        list_id=list_id,
                        list_title=list_title,
                        timeout_ms=max(timeout_ms // 2, 6000),
                    )

                ok_list = _on_target()
                # Removed go_back/history.back to avoid leaving the correct page.
                # Final attempt: force exact URL again then re-check.
                if not ok_list:
                    try:
                        page.goto(list_url, wait_until="networkidle")
                        nav_log.info("[nav] goto list_url final url=%s", page.url)
                        ok_list = _on_target()
                    except Exception:
                        ok_list = False
                if not ok_list and list_title:
                    if _click_by_title(list_title):
                        ok_list = _on_target()
                if not ok_list:
                    _snap("cant_reach_list")
                    raise RuntimeError(f"X list navigation failed; current_url={page.url or '<empty>'}")
                # Occasionally Twitter shows a blank shell; give it a short settle before auth check.
                page.wait_for_timeout(400)
                if wait_ms > 0:
                    page.wait_for_timeout(wait_ms)

                _activate_page(page)
                ok, reason = _wait_for_tweets(page, max_ms=timeout_ms)
                if not ok:
                    # One extra wait/retry before failing: sometimes tweets appear late.
                    page.wait_for_timeout(2000)
                    ok, reason = _wait_for_tweets(page, max_ms=timeout_ms // 2)
                    if not ok:
                        _snap("auth_or_ready_fail")
                        context.close()
                        if browser:
                            browser.close()
                        if reason == "login_required":
                            raise RuntimeError("X list authentication is required; refresh the configured storage state.")
                        raise RuntimeError(f"X list not ready: {reason}")

                # Let the feed settle so the newest tweets render before we start scrolling.
                if stabilize_ms > 0:
                    page.wait_for_timeout(stabilize_ms)
                # Extra "reading" pause to mimic human dwell time before scrolling.
                page.wait_for_timeout(1200)

                # To avoid bounce/redirect, re-check the URL after settling.
                cur_url = page.url or ""
                if not _ensure_on_list(
                    page,
                    list_url=list_url,
                    list_id=list_id,
                    list_title=list_title,
                    timeout_ms=max(timeout_ms // 3, 5000),
                ):
                    nav_log.warning("Lost target page after settle; aborting scroll to avoid ingesting wrong timeline")
                    _snap("lost_after_settle")
                    raise RuntimeError(f"X list left the target timeline after settling; current_url={page.url or '<empty>'}")

                # Scroll only after we are sure we're still on the target list.
                for scroll_index in range(scrolls):
                    mark_browser_progress(config, f"x:scroll:{scroll_index + 1}/{scrolls}")
                    page.mouse.wheel(0, scroll_px)
                    if scroll_delay_ms > 0:
                        page.wait_for_timeout(scroll_delay_ms)
                # Give the DOM a moment after the last scroll.
                page.wait_for_timeout(1200)

                articles = page.query_selector_all('article[data-testid="tweet"]')
                if not articles:
                    _snap("no_articles")
                    context.close()
                    if browser:
                        browser.close()
                    raise RuntimeError(
                        "No tweets found on list. If this persists, open Playwright and confirm the list loads."
                    )
        except Exception:
            _snap("exception")
            raise
        finally:
            if enable_trace:
                try:
                    trace_path = debug_dir / f"x_list_trace_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}.zip"
                    context.tracing.stop(path=str(trace_path))
                    nav_log.info("Saved Playwright trace: %s", trace_path)
                except Exception as e:
                    nav_log.warning("Failed to save trace: %s", e)
        seen: set[str] = set()
        for article_index, article in enumerate(articles, start=1):
            mark_browser_progress(config, f"x:article:{article_index}/{len(articles)}")
            data = _parse_article(article)
            if not data or not data.url:
                continue
            if data.url in seen:
                continue
            seen.add(data.url)

            thread = ThreadContext(ancestors=[], replies=[])
            thread_capture: dict[str, object] | None = None
            if include_thread:
                thread = _fetch_thread_context(
                    context,
                    data.url,
                    data.handle,
                    max_thread_replies,
                    wait_ms=wait_ms,
                    timeout_ms=timeout_ms,
                    until_url=bottom_url if status_url else None,
                )
                if status_url and bottom_url and bottom_url != status_url:
                    bottom_id = bottom_url.split("/status/", 1)[1]
                    expected_handle = (data.handle or status_url.split("/", 4)[3]).casefold()
                    bottom_index = next((index for index, tweet in enumerate(thread.replies) if tweet.tweet_id == bottom_id), None)
                    if bottom_index is None:
                        tail = _fetch_thread_context(
                            context, bottom_url, data.handle, max_thread_replies,
                            wait_ms=wait_ms, timeout_ms=timeout_ms,
                        )
                        bottom_post = _fetch_single(
                            context, bottom_url, wait_ms=wait_ms, timeout_ms=timeout_ms,
                            expected_tweet_id=bottom_id,
                        )
                        if bottom_post is None or not bottom_post.handle or bottom_post.handle.casefold() != expected_handle:
                            raise RuntimeError("Requested X thread end post could not be verified at its direct URL")
                        forward_ids = {item.tweet_id for item in thread.replies}
                        tail_ids = {item.tweet_id for item in tail.ancestors}
                        by_id = {
                            item.tweet_id: item for item in [*thread.replies, *tail.ancestors, bottom_post]
                            if item.tweet_id and item.tweet_id.isdigit() and item.handle and item.handle.casefold() == expected_handle
                            and int(data.tweet_id) < int(item.tweet_id) <= int(bottom_id)
                        }
                        thread = ThreadContext(
                            ancestors=[], replies=sorted(by_id.values(), key=lambda item: int(item.tweet_id))
                        )
                        thread_capture = {
                            "method": "forward_and_end_anchor",
                            "coverage": "overlapping_end_anchors" if forward_ids & tail_ids else "partial_with_unverified_middle",
                            "forward_posts": len(forward_ids),
                            "end_ancestors": len(tail_ids),
                        }
                    else:
                        thread = ThreadContext(ancestors=[], replies=thread.replies[: bottom_index + 1])
                        thread_capture = {"method": "forward_to_requested_end", "coverage": "end_observed"}
                if status_url:
                    thread = ThreadContext(ancestors=[], replies=[] if bottom_url == status_url else thread.replies)

            quoted_data = None
            if include_quoted and data.quoted_url:
                quoted_data = _fetch_single(context, data.quoted_url, wait_ms=wait_ms, timeout_ms=timeout_ms)
                if quoted_data and quoted_data.text:
                    data = TweetData(
                        url=data.url,
                        tweet_id=data.tweet_id,
                        text=data.text,
                        quoted_text=quoted_data.text,
                        quoted_url=data.quoted_url,
                        author=data.author,
                        handle=data.handle,
                        created_at=data.created_at,
                        links=data.links,
                        media=data.media,
                        article_url=data.article_url,
                    )

            article_data = None
            if include_articles and data.article_url:
                article_data = _fetch_x_article(
                    context,
                    data.article_url,
                    author=data.author,
                    handle=data.handle,
                    wait_ms=wait_ms,
                    timeout_ms=timeout_ms,
                )

            normalized_text = _compose_text(data, thread, article=article_data)
            links = _compose_links(data, thread, article=article_data)
            media = _compose_media(data, thread, quoted=quoted_data, article=article_data)
            linked_google_docs = _fetch_linked_google_docs(
                context,
                _compose_links(
                    data,
                    ThreadContext(
                        ancestors=[tweet for tweet in thread.ancestors if _normalized_x_handle(tweet.handle) == _normalized_x_handle(data.handle)],
                        replies=[tweet for tweet in thread.replies if _normalized_x_handle(tweet.handle) == _normalized_x_handle(data.handle)],
                    ),
                    article=article_data,
                ),
                handle=data.handle,
                config=config,
                timeout_ms=timeout_ms,
            )
            trusted_media_attachments = _fetch_trusted_media_attachments(
                context,
                media,
                handle=data.handle,
                config=config,
                timeout_ms=timeout_ms,
            )
            # de-dupe after merge
            if links:
                uniq_links: list[str] = []
                seen_links: set[str] = set()
                for l in links:
                    if l in seen_links:
                        continue
                    seen_links.add(l)
                    uniq_links.append(l)
                links = uniq_links
            if media:
                uniq_media: list[str] = []
                seen_media: set[str] = set()
                for m in media:
                    if m in seen_media:
                        continue
                    seen_media.add(m)
                    uniq_media.append(m)
                media = uniq_media

            title = None
            if data.handle and data.text:
                title = f"@{data.handle}: {clean_whitespace(data.text)[:160]}"
            elif data.text:
                title = clean_whitespace(data.text)[:160]

            if links:
                normalized_text = clean_whitespace(
                    f"{normalized_text}\n\nLinks:\n" + "\n".join(links)
                )
            if media:
                normalized_text = clean_whitespace(
                    f"{normalized_text}\n\nMedia:\n" + "\n".join(media)
                )
            if linked_google_docs:
                linked_doc_text = "\n\n".join(
                    f"Source: {row.get('url')}\n{str(row.get('text') or '').strip()}"
                    for row in linked_google_docs
                    if str(row.get("text") or "").strip()
                )
                if linked_doc_text:
                    normalized_text = clean_whitespace(
                        f"{normalized_text}\n\nLinked Google Docs:\n{linked_doc_text}"
                    )

            thread_rows = [
                {
                    "url": t.url,
                    "tweet_id": t.tweet_id,
                    "text": t.text,
                    "links": t.links,
                    "media": t.media,
                    "created_at": t.created_at,
                    "relation": "ancestor",
                }
                for t in thread.ancestors
            ] + [
                {
                    "url": t.url,
                    "tweet_id": t.tweet_id,
                    "text": t.text,
                    "links": t.links,
                    "media": t.media,
                    "created_at": t.created_at,
                    "relation": "reply",
                }
                for t in thread.replies
            ]

            published_at = None
            if data.created_at:
                published_at = parse_datetime_iso(str(data.created_at))

            extra_payload = {
                "connector": "x_thread" if status_url else "x_list",
                "list_url": list_url or None,
                "requested_top_url": status_url or None,
                "requested_bottom_url": bottom_url or None,
                "thread_capture": thread_capture,
                "handle": data.handle,
                "tweet_id": data.tweet_id,
                "root_text": data.text,
                "article_url": data.article_url,
                "root_media": list(data.media or []),
                "quoted_text": data.quoted_text,
                "quoted_url": data.quoted_url,
                "quoted": {
                    "url": quoted_data.url if quoted_data else data.quoted_url,
                    "tweet_id": quoted_data.tweet_id if quoted_data else None,
                    "text": quoted_data.text if quoted_data else data.quoted_text,
                    "links": quoted_data.links if quoted_data else [],
                    "media": quoted_data.media if quoted_data else [],
                },
                "article": (
                    {
                        "url": article_data.url,
                        "title": article_data.title,
                        "text": article_data.text,
                        "links": article_data.links,
                        "media": article_data.media_manifest or article_data.media,
                    }
                    if article_data
                    else ({"url": data.article_url} if data.article_url else None)
                ),
                "links": links,
                "media": media,
                "thread_context": {
                    "ancestors": [row for row in thread_rows if row.get("relation") == "ancestor"],
                    "replies": [row for row in thread_rows if row.get("relation") == "reply"],
                },
                "thread": thread_rows,
            }
            if linked_google_docs:
                extra_payload["linked_google_docs"] = [
                    {
                        "url": row.get("url"),
                        "file_id": row.get("file_id"),
                        "status": row.get("status") or "failed",
                        "failure": row.get("failure"),
                        "truncated": bool(row.get("truncated")),
                        "extracted_chars": len(str(row.get("text") or "")),
                    }
                    for row in linked_google_docs
                ]
            extra_payload["media_manifest"] = build_tweet_media_manifest(
                {
                    "url": data.url,
                    "tweet_id": data.tweet_id,
                    **extra_payload,
                }
            )

            items.append(
                {
                    "kind": "tweet",
                    "external_id": data.tweet_id,
                    "url": data.url,
                    "canonical_url": data.url,
                    "title": title,
                    "author": data.author,
                    "published_at": published_at,
                    "raw_text": normalized_text or data.text or data.url,
                    "normalized_text": normalized_text or data.text or data.url,
                    "image_urls": media,
                    "attachments": [
                        row["attachment"]
                        for row in linked_google_docs
                        if isinstance(row.get("attachment"), dict)
                    ] + trusted_media_attachments,
                    "extra": extra_payload,
                    "links": links,
                }
            )
            if len(items) >= max_items:
                break
        if save_storage_state and storage_state:
            try:
                Path(storage_state).parent.mkdir(parents=True, exist_ok=True)
                context.storage_state(path=storage_state)
            except Exception:
                pass
        context.close()
        if browser:
            browser.close()

    if not items:
        raise RuntimeError("X list rendered tweet articles but produced no ingestible items.")
    mark_browser_progress(config, "x:completed")
    return items


def _fetch_x_list_with_retry(config: dict) -> list[dict]:
    try:
        items = _fetch_x_list_once(config)
        headless = bool(config.get("headless", True))
        retry_headed = bool(config.get("retry_headed_on_fail", True))
        retry_on_empty = bool(config.get("retry_headed_on_empty", True))
        if headless and retry_headed and retry_on_empty and not items:
            retry_cfg = dict(config or {})
            retry_cfg["headless"] = False
            return _fetch_x_list_once(retry_cfg)
        return items
    except Exception as exc:
        headless = bool(config.get("headless", True))
        retry_headed = bool(config.get("retry_headed_on_fail", True))
        if headless and retry_headed:
            retry_cfg = dict(config or {})
            retry_cfg["headless"] = False
            return _fetch_x_list_once(retry_cfg)

        # Target/page closures happen occasionally with persistent profiles; retry once
        # with a clean, non-persistent context and headed mode to salvage the run.
        msg = str(exc).lower()
        is_target_closed = "target page" in msg or "targetclosed" in msg or "browser has been closed" in msg
        if is_target_closed:
            retry_cfg = dict(config or {})
            retry_cfg["headless"] = False if headless and retry_headed else headless
            retry_cfg.pop("user_data_dir", None)
            retry_cfg.pop("browser_user_data_dir", None)
            retry_cfg.pop("profile_directory", None)
            retry_cfg.pop("browser_profile_directory", None)
            retry_cfg.pop("browser_channel", None)
            if not retry_cfg.get("storage_state_path"):
                default_state = settings.data_dir / "x_storage_state.json"
                if default_state.exists():
                    retry_cfg["storage_state_path"] = str(default_state)
            return _fetch_x_list_once(retry_cfg)

        # Fallback: if persistent profile can't reach the list, retry once with storage_state only.
        if not config.get("_retry_storage_state"):
            storage_state = config.get("storage_state_path") or getattr(settings, "x_storage_state_path", None)
            if storage_state and resolve_migrated_repo_path(storage_state).exists():
                retry_cfg = dict(config or {})
                retry_cfg["_retry_storage_state"] = True
                retry_cfg["headless"] = False if headless and retry_headed else headless
                retry_cfg.pop("user_data_dir", None)
                retry_cfg.pop("browser_user_data_dir", None)
                retry_cfg.pop("profile_directory", None)
                retry_cfg.pop("browser_profile_directory", None)
                retry_cfg.pop("browser_channel", None)
                return _fetch_x_list_once(retry_cfg)
        raise exc


def _storage_state_exists(config: dict) -> bool:
    candidates: list[object] = [
        config.get("storage_state_path"),
        getattr(settings, "x_storage_state_path", None),
        settings.data_dir / "x_storage_state.json",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            if resolve_migrated_repo_path(candidate).exists():
                return True
        except Exception:
            continue
    return False


def _browser_launch_missing(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return (
        "executable doesn't exist" in msg
        or "please run the following command to download new browsers" in msg
        or "playwright install" in msg
    )


def _looks_like_edge_profile(path: str | None) -> bool:
    if not path:
        return False
    lowered = str(path).lower()
    return "microsoft/edge" in lowered or "microsoft\\edge" in lowered or "edge user data" in lowered


def _build_primary_secondary(config: dict) -> tuple[dict | None, dict | None]:
    prefer_chromium = bool(config.get("prefer_chromium_primary")) or (
        str(config.get("primary_browser") or "").lower() == "chromium"
    )
    if not prefer_chromium:
        return None, None
    primary_cfg = dict(config or {})
    secondary_cfg = dict(config or {})
    primary_cfg["_primary_attempt"] = "chromium"
    secondary_cfg["_secondary_attempt"] = "edge"

    # Primary: bundled Chromium (no channel). Drop Edge profile paths if present.
    primary_cfg.pop("browser_channel", None)
    if _looks_like_edge_profile(primary_cfg.get("user_data_dir")) or (
        str(config.get("browser_channel") or "").lower() == "msedge"
    ):
        primary_cfg.pop("user_data_dir", None)
        primary_cfg.pop("browser_user_data_dir", None)
        primary_cfg.pop("profile_directory", None)
        primary_cfg.pop("browser_profile_directory", None)

    # Do not burn a run on anonymous Chromium when the only authenticated state
    # is the Edge persistent profile. In that case the original config is the
    # real primary path.
    if not (
        primary_cfg.get("user_data_dir")
        or primary_cfg.get("browser_user_data_dir")
        or _storage_state_exists(primary_cfg)
    ):
        return None, None

    # Secondary: Edge channel + profile (if configured).
    if not secondary_cfg.get("browser_channel"):
        secondary_cfg["browser_channel"] = "msedge"
    return primary_cfg, secondary_cfg


def fetch_x_list(config: dict) -> list[dict]:
    primary_cfg, secondary_cfg = _build_primary_secondary(dict(config or {}))
    if primary_cfg:
        try:
            items = _fetch_x_list_with_retry(primary_cfg)
        except Exception as exc:
            if secondary_cfg and _browser_launch_missing(exc):
                logging.getLogger("thoth.x_list").warning(
                    "Primary Chromium launch failed; falling back to configured browser channel."
                )
                return _fetch_x_list_with_retry(secondary_cfg)
            raise
        if items:
            return items
        if secondary_cfg and bool(config.get("fallback_on_empty", True)):
            return _fetch_x_list_with_retry(secondary_cfg)
        return items
    return _fetch_x_list_with_retry(config)


def fetch_x_thread(config: dict, *, top_url: str | None = None, bottom_url: str | None = None) -> dict:
    """Fetch one bounded thread through the existing X browser and media pipeline."""
    cfg = dict(config or {})
    cfg.update({
        "status_url": top_url or cfg.get("status_url") or "",
        "bottom_url": bottom_url or cfg.get("bottom_url") or "",
        "list_url": "",
        "max_items_per_run": 1,
        "include_thread": True,
        "max_thread_replies": 250 if cfg.get("bottom_url") else 50,
    })
    if not cfg["status_url"]:
        raise ValueError("X thread ingestion requires a top status URL")
    items = fetch_x_list(cfg)
    if len(items) != 1:
        raise RuntimeError("X thread fetch did not return exactly one root status")
    return items[0]


def probe_x_list(config: dict) -> dict:
    cfg = dict(config or {})
    cfg["max_items_per_run"] = min(int(cfg.get("max_items_per_run", 5) or 5), 5)
    cfg["include_thread"] = False
    cfg["include_quoted"] = False
    if not cfg.get("scrolls"):
        cfg["scrolls"] = 1
    items = fetch_x_list(cfg)
    first_url = items[0].get("url") if items else None
    return {"count": len(items), "first_url": first_url}
