from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from thoth_terminal.browser_ingestion_runtime import mark_browser_progress
from thoth_terminal.connectors.web_list import _extract_published_at
from thoth_terminal.connectors.web_list_browser import _load_page, _maybe_int
from thoth_terminal.net import FetchError
from thoth_terminal.runtime_env import resolve_migrated_repo_path
from thoth_terminal.text import (
    clean_whitespace,
    extract_image_urls_from_html,
    extract_text_from_html,
    extract_title_from_html,
    parse_datetime_iso,
)


def _close_browser_resource(resource: object) -> None:
    try:
        getattr(resource, "close")()
    except Exception:
        pass


@dataclass(frozen=True)
class ReaderCandidate:
    url: str
    title_hint: str | None = None
    published_at: datetime | None = None


_SUBSTACK_INBOX_WORKSET_LIMIT = 2048


def _reader_publication_date(value: object) -> datetime | None:
    parsed = parse_datetime_iso(str(value)) if value else None
    if parsed is None:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _freshest_candidates(candidates: list[ReaderCandidate]) -> list[ReaderCandidate]:
    # Unknown dates stay eligible ahead of dated entries: they can be new posts.
    # Python's stable sort preserves visible order among entries without dates.
    return sorted(candidates, key=lambda row: row.published_at or datetime.max.replace(tzinfo=timezone.utc), reverse=True)


_SUBSTACK_PAYWALL_TOKENS = (
    "subscribe to read",
    "upgrade to paid",
    "this post is for paid subscribers",
    "become a paid subscriber",
    "start a free trial",
    "pledge your support",
    "only paid subscribers",
)

_NOTE_PAYWALL_TOKENS = (
    "この記事は有料です",
    "この記事はメンバーシップに加入すると読めます",
    "この続きをみるには",
    "購入手続きへ",
    "メンバーシップに加入",
    "ログインしてください",
)

_READER_BLOCK_PATTERNS = (
    "cf-chl",
    "attention required",
    "verify you are human",
    "access denied",
    "request blocked",
    "bot detection",
    "captcha-delivery.com",
    "geo.captcha-delivery.com",
    "datadome captcha",
    'title="datadome captcha"',
)
_READER_HARD_BLOCK_PATTERNS = (
    "cf-chl",
    "captcha-delivery.com",
    "geo.captcha-delivery.com",
    "datadome captcha",
    'title="datadome captcha"',
)
_READER_VISIBLE_BLOCK_PATTERNS = (
    "attention required",
    "verify you are human",
    "access denied",
    "request blocked",
    "bot detection",
    "challenge-platform",
)

_TRACKING_QUERY_RE = re.compile(r"^(?:utm_|fbclid$|gclid$|mc_cid$|mc_eid$)", re.IGNORECASE)
_NOTE_ARTICLE_PATH_RE = re.compile(r"^/([^/?#]+)/n/([^/?#]+)")


def _sha_url(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8", errors="ignore")).hexdigest()[:24]


def _clean_url(url: str, *, keep_query: bool = True) -> str:
    parsed = urlparse(str(url or "").strip())
    if not parsed.scheme or not parsed.netloc:
        return str(url or "").strip()
    if keep_query and parsed.query:
        kept = []
        for part in parsed.query.split("&"):
            key = part.split("=", 1)[0]
            if key and not _TRACKING_QUERY_RE.search(key):
                kept.append(part)
        query = "&".join(kept)
    else:
        query = ""
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip("/") or "/", "", query, ""))


def _host_matches(host: str, pattern: str) -> bool:
    h = host.strip().lower()
    p = pattern.strip().lower()
    if not h or not p:
        return False
    if p.startswith("*."):
        suffix = p[1:]
        return h.endswith(suffix) and h != suffix.lstrip(".")
    if p.startswith("."):
        return h.endswith(p) or h == p.lstrip(".")
    return h == p


def _allowed_by_config(url: str, config: dict[str, Any]) -> bool:
    allowed_hosts = [str(x).strip().lower() for x in (config.get("allowed_hosts") or []) if str(x).strip()]
    if not allowed_hosts:
        return True
    host = urlparse(url).netloc.strip().lower()
    return any(_host_matches(host, pattern) for pattern in allowed_hosts)


def _looks_like_substack_article_url(url: str, config: dict[str, Any] | None = None) -> bool:
    cfg = config or {}
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path or ""
    if not parsed.scheme.startswith("http"):
        return False
    excluded = (
        "/account",
        "/chat",
        "/inbox",
        "/login",
        "/notes",
        "/people",
        "/profile",
        "/recommendations",
        "/settings",
        "/subscribe",
    )
    if any(path == prefix or path.startswith(prefix + "/") for prefix in excluded):
        return False
    if host == "substack.com":
        return path.startswith("/home/post/") or path.startswith("/p/") or path.startswith("/inbox/post/")
    if host.endswith(".substack.com"):
        return "/p/" in path or path.startswith("/i/")
    # Custom-domain Substacks are allowed only when the source explicitly allowlists them.
    return _allowed_by_config(url, cfg) and bool(cfg.get("allow_custom_substack_domains"))


def _looks_like_note_article_url(url: str, config: dict[str, Any] | None = None) -> bool:
    parsed = urlparse(url)
    if parsed.netloc.lower() != "note.com":
        return False
    match = _NOTE_ARTICLE_PATH_RE.match(parsed.path or "")
    if not match:
        return False
    creator_slug = str((config or {}).get("creator_slug") or "").strip().lower()
    if creator_slug and match.group(1).lower() != creator_slug:
        return False
    return True


def _is_candidate_url(url: str, *, site: str, config: dict[str, Any]) -> bool:
    if site == "substack":
        return _looks_like_substack_article_url(url, config)
    if site == "note":
        return _looks_like_note_article_url(url, config)
    return False


def _discover_candidates(page, *, list_url: str, site: str, config: dict[str, Any], max_items: int) -> list[ReaderCandidate]:
    candidates: list[ReaderCandidate] = []
    seen: set[str] = set()

    if _is_candidate_url(list_url, site=site, config=config):
        cleaned = _clean_url(list_url)
        candidates.append(ReaderCandidate(url=cleaned, title_hint=None))
        seen.add(cleaned)

    try:
        anchors = page.eval_on_selector_all(
            "a[href]",
            """
            nodes => nodes.map((node) => ({
                href: node.href || node.getAttribute('href'),
                text: (node.innerText || node.textContent || node.getAttribute('aria-label') || node.title || '').trim(),
                published_at: (() => {
                    let card = node;
                    for (let depth = 0; card && depth < 5; depth++, card = card.parentElement) {
                        const times = card.querySelectorAll('time[datetime]');
                        if (times.length === 1) return times[0].getAttribute('datetime');
                        if (card.tagName === 'ARTICLE') break;
                    }
                    return null;
                })()
            })).filter(row => row.href)
            """,
        )
    except Exception as exc:
        if site == "substack":
            raise FetchError(f"Browser reader inbox discovery failed: {exc}", url=list_url) from exc
        anchors = []

    for row in anchors or []:
        if not isinstance(row, dict):
            continue
        href = str(row.get("href") or "").strip()
        if not href:
            continue
        url = _clean_url(urljoin(list_url, href))
        if url in seen:
            continue
        if not _is_candidate_url(url, site=site, config=config):
            continue
        if not _allowed_by_config(url, config):
            continue
        title_hint = clean_whitespace(str(row.get("text") or ""))[:240] or None
        candidates.append(ReaderCandidate(
            url=url,
            title_hint=title_hint,
            published_at=_reader_publication_date(row.get("published_at")) if site == "substack" else None,
        ))
        seen.add(url)
        if site != "substack" and len(candidates) >= max_items:
            break

    if site == "substack":
        if len(candidates) > max_items:
            raise FetchError("Substack inbox discovery exceeds its bounded workset; outstanding URLs cannot all be preserved.", url=list_url)
        return _freshest_candidates(candidates)
    return candidates


def _substack_backfill_publications(config: dict[str, Any]) -> list[dict[str, str]]:
    publications: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in config.get("backfill_publications") or []:
        if isinstance(raw, str):
            name = ""
            url = raw
        elif isinstance(raw, dict):
            name = clean_whitespace(str(raw.get("name") or ""))
            url = str(raw.get("url") or raw.get("base_url") or "")
        else:
            continue
        parsed = urlparse(url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        base_url = urlunparse((parsed.scheme, parsed.netloc.lower(), "", "", "", "")).rstrip("/")
        if base_url in seen:
            continue
        seen.add(base_url)
        publications.append({"name": name or parsed.netloc, "url": base_url})
    return publications


def _substack_backfill_state(
    config: dict[str, Any], source_cursor: dict[str, Any] | None
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    publications = _substack_backfill_publications(config)
    cursor = dict(source_cursor or {})
    raw_state = cursor.get("substack_archive_backfill")
    state = dict(raw_state) if isinstance(raw_state, dict) else {}
    offsets = state.get("offsets")
    completed = state.get("completed")
    state["version"] = 1
    state["offsets"] = dict(offsets) if isinstance(offsets, dict) else {}
    state["completed"] = [str(x) for x in completed] if isinstance(completed, list) else []
    state["next_publication_index"] = max(0, _maybe_int(state.get("next_publication_index"), 0))
    cursor["substack_archive_backfill"] = state
    return publications, cursor


def _substack_archive_candidates(
    context,
    *,
    publication: dict[str, str],
    offset: int,
    limit: int,
    timeout_ms: int,
) -> list[ReaderCandidate]:
    base_url = publication["url"].rstrip("/")
    api_url = f"{base_url}/api/v1/archive?sort=new&search=&offset={max(0, offset)}&limit={max(1, limit)}"
    try:
        response = context.request.get(api_url, timeout=timeout_ms)
        if not response.ok:
            raise FetchError(f"Substack archive API returned HTTP {response.status}.", url=api_url)
        payload = json.loads(response.text())
    except FetchError:
        raise
    except Exception as exc:
        raise FetchError(f"Substack archive discovery failed: {exc}", url=api_url) from exc
    if not isinstance(payload, list):
        raise FetchError("Substack archive API returned an unexpected payload.", url=api_url)

    candidates: list[ReaderCandidate] = []
    for row in payload:
        if not isinstance(row, dict):
            raise FetchError("Substack archive API returned an invalid post row.", url=api_url)
        raw_url = str(row.get("canonical_url") or "").strip()
        slug = str(row.get("slug") or "").strip().strip("/")
        if not raw_url and slug:
            raw_url = f"{base_url}/p/{slug}"
        url = _clean_url(raw_url)
        if not url:
            raise FetchError("Substack archive API returned a post without a URL or slug.", url=api_url)
        candidates.append(
            ReaderCandidate(
                url=url,
                title_hint=clean_whitespace(str(row.get("title") or ""))[:240] or None,
                published_at=_reader_publication_date(row.get("post_date") or row.get("published_at")),
            )
        )
    return candidates


def _meta_content(soup: BeautifulSoup, *selectors: tuple[str, dict[str, str]]) -> str | None:
    for name, attrs in selectors:
        tag = soup.find(name, attrs)
        if tag and tag.get("content"):
            value = clean_whitespace(str(tag.get("content") or ""))
            if value:
                return value
    return None


def _extract_author(html: str) -> str | None:
    soup = BeautifulSoup(html or "", "html.parser")
    return _meta_content(
        soup,
        ("meta", {"name": "author"}),
        ("meta", {"property": "article:author"}),
        ("meta", {"name": "twitter:creator"}),
    )


def _paywall_reason(*, site: str, html: str, text: str, min_chars: int) -> str | None:
    body_text = clean_whitespace(BeautifulSoup(html or "", "html.parser").get_text(" ", strip=True))
    haystack = f"{body_text}\n{text}".lower()
    tokens = _SUBSTACK_PAYWALL_TOKENS if site == "substack" else _NOTE_PAYWALL_TOKENS
    matched = [token for token in tokens if token.lower() in haystack]
    if site == "substack" and "this post is for paid subscribers" in matched:
        return "locked_or_unentitled"
    if matched and len(clean_whitespace(text)) < int(min_chars):
        return "locked_or_unentitled"
    if site == "substack" and "sign in" in haystack and len(clean_whitespace(text)) < int(min_chars):
        return "login_required"
    return None


def _reader_looks_blocked(html: str | None) -> bool:
    if not html:
        return True
    lowered = html.lower()
    if any(token in lowered for token in _READER_HARD_BLOCK_PATTERNS):
        return True
    soup = BeautifulSoup(html or "", "html.parser")
    visible = clean_whitespace(soup.get_text(" ", strip=True)).lower()
    title = clean_whitespace(str(soup.title.get_text(" ", strip=True) if soup.title else "")).lower()
    visible_haystack = f"{title}\n{visible}"
    return any(token in visible_haystack for token in _READER_VISIBLE_BLOCK_PATTERNS)


def _extract_browser_image_manifest(page, *, article_url: str, html: str, max_images: int) -> list[dict[str, Any]]:
    try:
        rows = page.eval_on_selector_all(
            "article img, main img",
            """
            nodes => nodes.map((img, idx) => {
                const srcset = img.getAttribute('srcset') || img.getAttribute('data-srcset') || '';
                let src = img.currentSrc || img.src || img.getAttribute('data-src') ||
                    img.getAttribute('data-original') || img.getAttribute('data-lazy-src') || '';
                if (!src && srcset) {
                    const parts = srcset.split(',').map(s => s.trim()).filter(Boolean);
                    if (parts.length) src = parts[parts.length - 1].split(/\\s+/)[0];
                }
                const figure = img.closest('figure');
                const caption = figure ? (figure.querySelector('figcaption')?.innerText || '').trim() : '';
                const contentRoot = img.closest('article, main') || document.body || document;
                const before = node => Boolean(
                    node.compareDocumentPosition(img) & Node.DOCUMENT_POSITION_FOLLOWING
                );
                const after = node => Boolean(
                    node.compareDocumentPosition(img) & Node.DOCUMENT_POSITION_PRECEDING
                );
                const headings = Array.from(contentRoot.querySelectorAll('h1,h2,h3')).filter(before);
                const heading = (headings.length ? headings[headings.length - 1].innerText : '').trim();
                const precedingParagraphs = Array.from(contentRoot.querySelectorAll('p'))
                    .filter(before)
                    .slice(-2)
                    .map(node => (node.innerText || '').trim())
                    .filter(Boolean);
                const followingParagraphs = Array.from(contentRoot.querySelectorAll('p'))
                    .filter(after)
                    .slice(0, 1)
                    .map(node => (node.innerText || '').trim())
                    .filter(Boolean);
                return {
                    url: src,
                    alt: (img.getAttribute('alt') || '').trim(),
                    caption,
                    heading,
                    preceding_text: precedingParagraphs.join('\n'),
                    following_text: followingParagraphs.join('\n'),
                    width: img.naturalWidth || img.width || null,
                    height: img.naturalHeight || img.height || null,
                    index: idx + 1,
                    dom_index: idx + 1
                };
            }).filter(row => row.url)
            """,
        )
    except Exception:
        rows = []

    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(row: dict[str, Any] | str) -> None:
        if isinstance(row, str):
            raw = row
            payload: dict[str, Any] = {"url": raw}
        else:
            raw = str(row.get("url") or "")
            payload = dict(row)
        if not raw or raw.startswith("data:"):
            return
        url = urljoin(article_url, raw)
        parsed = urlparse(url)
        path = parsed.path.lower()
        if path.endswith(".svg"):
            return
        width = payload.get("width")
        height = payload.get("height")
        try:
            if width is not None and height is not None and int(width) < 80 and int(height) < 80:
                low = f"{url} {payload.get('alt') or ''}".lower()
                if any(token in low for token in ("avatar", "logo", "profile", "icon")):
                    return
        except Exception:
            pass
        if url in seen:
            return
        seen.add(url)
        payload["url"] = url
        payload["kind"] = "image"
        payload["label"] = payload.get("label") or f"Article image {len(out) + 1}"
        out.append(payload)

    for row in rows or []:
        if isinstance(row, dict):
            _add(row)
        if len(out) >= max_images:
            return out[:max_images]

    for url in extract_image_urls_from_html(html or "", url=article_url, limit=max_images):
        _add(url)
        if len(out) >= max_images:
            break

    return out[:max_images]


def _image_context_lines(image_manifest: list[dict[str, Any]], *, limit: int = 40) -> list[str]:
    lines: list[str] = []
    for row in image_manifest[: max(0, int(limit))]:
        url = str(row.get("url") or "").strip()
        if not url:
            continue
        label = str(row.get("label") or "Article image").strip()
        parts = []
        for key in ("alt", "caption", "heading"):
            value = clean_whitespace(str(row.get(key) or ""))
            if value and value not in parts:
                parts.append(value)
        context = " | ".join(parts)
        lines.append(f"- {label}: {context} ({url})" if context else f"- {label}: {url}")
    return lines


def _append_image_context(text: str, image_manifest: list[dict[str, Any]], config: dict[str, Any]) -> str:
    if not bool(config.get("append_image_manifest_to_text", True)):
        return text
    lines = _image_context_lines(image_manifest, limit=_maybe_int(config.get("max_article_images"), 40))
    if not lines:
        return text
    return f"{text.strip()}\n\nArticle images:\n" + "\n".join(lines)


def _html_attachment(*, html: str, url: str, site: str) -> dict[str, Any]:
    return {
        "kind": "html",
        "filename": f"{site}-{_sha_url(url)}.html",
        "content_type": "text/html",
        "data_bytes": html.encode("utf-8", errors="ignore"),
        "original_url": url,
    }


def _download_image_attachments(context, image_manifest: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    if not bool(config.get("browser_download_images", True)):
        return []
    max_images = _maybe_int(config.get("max_images_per_doc") or config.get("max_article_images"), 40)
    max_bytes = _maybe_int(config.get("max_image_bytes"), 12_000_000)
    timeout_ms = _maybe_int(config.get("image_timeout_ms"), 20_000)
    image_budget_s = max(0, _maybe_int(config.get("image_download_budget_s"), 0))
    started_at = monotonic()
    out: list[dict[str, Any]] = []
    for row in image_manifest[:max_images]:
        if image_budget_s and (monotonic() - started_at) >= image_budget_s:
            break
        url = str(row.get("url") or "").strip()
        if not url:
            continue
        try:
            resp = context.request.get(url, timeout=timeout_ms)
            if not resp.ok:
                continue
            data = resp.body()
            if not data or (max_bytes > 0 and len(data) > max_bytes):
                continue
            content_type = resp.headers.get("content-type")
            if content_type and not content_type.lower().startswith("image/"):
                continue
            out.append(
                {
                    "kind": "image",
                    "filename": f"image-{len(out) + 1}",
                    "content_type": content_type or "image/jpeg",
                    "data_bytes": data,
                    "original_url": url,
                }
            )
        except Exception:
            continue
    return out


def _build_article_item(
    *,
    page,
    context,
    site: str,
    candidate: ReaderCandidate,
    list_url: str,
    config: dict[str, Any],
) -> dict[str, Any] | None:
    wait_ms = max(_maybe_int(config.get("browser_wait_ms"), 3500), 1500)
    scrolls = max(_maybe_int(config.get("browser_scrolls"), 4), 1)
    timeout_ms = _maybe_int(config.get("browser_timeout_ms"), 60_000)
    accept_selector = config.get("accept_cookies_selector")
    try:
        html = _load_page(
            page,
            url=candidate.url,
            wait_ms=wait_ms,
            scrolls=scrolls,
            accept_selector=str(accept_selector) if accept_selector else None,
            timeout_ms=timeout_ms,
        )
    except Exception as exc:
        raise FetchError(f"Browser article fetch failed for {candidate.url}: {exc}", url=candidate.url) from exc
    if not html or _reader_looks_blocked(html):
        return None
    final_url = _clean_url(str(page.url or candidate.url))
    normalized = clean_whitespace(extract_text_from_html(html, url=final_url))
    min_text_chars = _maybe_int(config.get("min_article_text_chars"), 600)
    reason = _paywall_reason(site=site, html=html, text=normalized, min_chars=min_text_chars)
    capture_teaser = bool(config.get("capture_paywalled_teasers", False))
    if reason and bool(config.get("skip_paywalled", True)) and not capture_teaser:
        return None
    if len(normalized) < _maybe_int(config.get("min_visible_text_chars"), 160):
        return None

    title = extract_title_from_html(html, url=final_url) or candidate.title_hint or final_url
    author = _extract_author(html)
    published_at = _extract_published_at(html)
    max_images = _maybe_int(config.get("max_article_images"), 40)
    image_manifest = _extract_browser_image_manifest(page, article_url=final_url, html=html, max_images=max_images)
    image_urls = [str(row.get("url")) for row in image_manifest if row.get("url")]
    text_with_images = _append_image_context(normalized, image_manifest, config)

    attachments: list[dict[str, Any]] = []
    if bool(config.get("store_article_html_attachment", True)):
        attachments.append(_html_attachment(html=html, url=final_url, site=site))
    if bool(config.get("download_article_images")):
        attachments.extend(_download_image_attachments(context, image_manifest, config))

    return {
        "kind": str(config.get("document_kind") or "subscription_report"),
        "external_id": f"{site}:{_sha_url(final_url)}",
        "url": final_url,
        "canonical_url": final_url,
        "title": title,
        "author": author,
        "published_at": published_at,
        "raw_text": text_with_images,
        "normalized_text": text_with_images,
        "raw_html": html,
        "image_urls": image_urls,
        "attachments": attachments,
        "extra": {
            "connector": f"{site}_reader_browser",
            "list_url": list_url,
            "source_url": candidate.url,
            "report_candidate": True,
            "report_source_kind": "paid_subscription_reader",
            "entitlement": {
                "rights_class": str(config.get("rights_class") or "paid"),
                "acquisition_mode": "authenticated_browser",
                "policy": "visible_only_no_paywall_bypass",
                "skip_paywalled": bool(config.get("skip_paywalled", True)),
                "capture_paywalled_teasers": capture_teaser,
            },
            "paywall_state": "visible" if not reason else reason,
            "extraction_status": "visible" if not reason else "paid_teaser",
            "extraction_warning": None
            if not reason
            else "Only teaser/free-visible text was captured; full paid body is not available for this account.",
            "image_manifest": image_manifest,
            "report_extraction": {
                "site": site,
                "status": "visible" if not reason else "paid_teaser",
                "warning": None
                if not reason
                else "Only teaser/free-visible text was captured; full paid body is not available for this account.",
                "text_chars": len(normalized),
                "image_count": len(image_manifest),
                "html_attachment": bool(config.get("store_article_html_attachment", True)),
                "image_attachment_count": len([a for a in attachments if a.get("kind") == "image"]),
            },
        },
    }


def _storage_state_path(config: dict[str, Any]) -> str | None:
    raw = (
        config.get("storage_state_path")
        or config.get("browser_storage_state_path")
        or config.get("storage_state")
    )
    if raw is None:
        return None
    path = str(raw).strip()
    if not path:
        return None
    return str(resolve_migrated_repo_path(path))


def _profile_auth_dir(config: dict[str, Any]) -> str | None:
    raw = config.get("user_data_dir") or config.get("browser_user_data_dir")
    if raw is None:
        return None
    path = str(raw).strip()
    if not path:
        return None
    try:
        resolved = resolve_migrated_repo_path(path)
        if resolved.exists():
            return str(resolved)
    except Exception:
        return None
    return None


def _looks_like_edge_profile(path: str | None) -> bool:
    if not path:
        return False
    lowered = str(path).lower()
    return "microsoft\\edge" in lowered or "microsoft/edge" in lowered or "edge user data" in lowered


def _require_browser_entitlement_state(config: dict[str, Any]) -> None:
    if not bool(config.get("require_authenticated_session", True)):
        return
    storage_state_path = _storage_state_path(config)
    if storage_state_path and Path(storage_state_path).exists():
        return
    if _profile_auth_dir(config):
        return
    raise ValueError(
        "Paid reader connectors require a logged-in Playwright storage_state_path or browser user_data_dir. "
        "Run `python -m thoth_terminal.cli browser-login --url <login-or-reader-url> --storage data/paid_reader_storage_state.json` "
        "and set the source storage_state_path to that file, or configure the source with an authenticated Edge user_data_dir/profile_directory."
    )


def _launch_paid_reader_context(
    playwright,
    *,
    storage_state_path: str | None,
    headless: bool,
    user_agent: str | None,
    browser_channel: str | None,
    user_data_dir: str | None,
    profile_directory: str | None,
    slow_mo_ms: int,
):
    default_ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0"
    if not user_agent:
        user_agent = default_ua

    launch_kwargs: dict[str, object] = {"headless": headless}
    if browser_channel:
        launch_kwargs["channel"] = str(browser_channel)
    if slow_mo_ms and slow_mo_ms > 0:
        launch_kwargs["slow_mo"] = int(slow_mo_ms)
    args = list(launch_kwargs.get("args") or [])
    args.extend(
        [
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
        ]
    )
    launch_kwargs["args"] = args

    context_kwargs: dict[str, object] = {
        "user_agent": user_agent,
        "viewport": {"width": 1366, "height": 900},
        "locale": "en-US",
        "timezone_id": "America/New_York",
    }

    if user_data_dir:
        if profile_directory:
            launch_args = list(launch_kwargs.get("args") or [])
            launch_args.append(f"--profile-directory={profile_directory}")
            launch_kwargs["args"] = launch_args
        # Paid readers must not fall back to anonymous mode if the authenticated
        # persistent profile is blocked/locked.
        context = playwright.chromium.launch_persistent_context(
            str(user_data_dir),
            **launch_kwargs,
            **context_kwargs,
        )
        return None, context

    if storage_state_path and Path(storage_state_path).exists():
        context_kwargs["storage_state"] = storage_state_path
    browser = playwright.chromium.launch(**launch_kwargs)
    context = browser.new_context(**context_kwargs)
    return browser, context


def _fetch_paid_reader_browser(
    config: dict[str, Any],
    *,
    site: str,
    source_cursor: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = dict(config or {})
    mark_browser_progress(cfg, f"{site}:preparing")
    _require_browser_entitlement_state(cfg)

    list_url = str(cfg.get("list_url") or cfg.get("url") or "").strip()
    if not list_url:
        raise ValueError(f"{site} reader connector requires config.list_url")

    max_items = _maybe_int(cfg.get("max_items_per_run"), 25)
    storage_state_path = _storage_state_path(cfg)
    headless = bool(cfg.get("headless", True))
    user_agent = cfg.get("user_agent")
    browser_channel = cfg.get("browser_channel")
    user_data_dir = cfg.get("user_data_dir") or cfg.get("browser_user_data_dir")
    profile_directory = cfg.get("profile_directory") or cfg.get("browser_profile_directory")
    if user_data_dir and not Path(str(user_data_dir)).exists():
        user_data_dir = None
        profile_directory = None
    if user_data_dir:
        # Persistent browser profiles carry the user's paid-reader entitlement.
        # Launch them headed; Chromium/Edge profiles are unreliable or refused in headless mode.
        headless = False
        if not browser_channel and _looks_like_edge_profile(str(user_data_dir)):
            browser_channel = "msedge"
    slow_mo_ms = _maybe_int(cfg.get("slow_mo_ms"), 0)
    wait_ms = max(_maybe_int(cfg.get("browser_wait_ms"), 3500), 2500)
    scrolls = max(_maybe_int(cfg.get("list_scrolls") or cfg.get("browser_scrolls"), 4), 1)
    timeout_ms = _maybe_int(cfg.get("browser_timeout_ms"), 60_000)
    accept_selector = cfg.get("accept_cookies_selector")
    max_article_fetches = min(max_items, max(0, _maybe_int(cfg.get("max_article_fetches"), max_items)))
    article_budget_s = max(0, _maybe_int(cfg.get("article_fetch_budget_s"), 0))
    articles_started_at = monotonic()
    article_fetches = 0
    article_budget_exhausted = False

    items: list[dict[str, Any]] = []
    skipped = 0
    next_cursor: dict[str, Any] | None = None
    backfill_stats: dict[str, Any] = {}
    live_stats: dict[str, Any] = {}
    article_errors: list[dict[str, str]] = []
    with sync_playwright() as p, ExitStack() as cleanup:
        mark_browser_progress(cfg, f"{site}:launching_browser")
        browser, context = _launch_paid_reader_context(
            p,
            storage_state_path=storage_state_path,
            headless=headless,
            user_agent=user_agent,
            browser_channel=browser_channel,
            user_data_dir=user_data_dir,
            profile_directory=profile_directory,
            slow_mo_ms=slow_mo_ms,
        )
        if browser is not None:
            cleanup.callback(_close_browser_resource, browser)
        cleanup.callback(_close_browser_resource, context)
        page = context.new_page()
        cleanup.callback(_close_browser_resource, page)
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        cursor_payload: dict[str, Any] = dict(source_cursor or {})
        inbox_state: dict[str, Any] = {}
        seen_urls: dict[str, None] = {}
        pending: dict[str, ReaderCandidate] = {}
        if site == "substack":
            raw_inbox = cursor_payload.get("substack_live_inbox")
            inbox_state = dict(raw_inbox) if isinstance(raw_inbox, dict) else {}
            seen_urls = dict.fromkeys(str(url) for url in (inbox_state.get("seen_urls") or [])[-_SUBSTACK_INBOX_WORKSET_LIMIT:])
            for row in inbox_state.get("pending") or []:
                if not isinstance(row, dict):
                    continue
                url = _clean_url(str(row.get("url") or ""))
                if url in seen_urls or not _is_candidate_url(url, site=site, config=cfg) or not _allowed_by_config(url, cfg):
                    continue
                pending[url] = ReaderCandidate(url, row.get("title_hint"), _reader_publication_date(row.get("published_at")))

        # Live discovery is mandatory on every poll, including during backfill.
        # A broken inbox must fail visibly before archive work can mask it.
        mark_browser_progress(cfg, f"{site}:loading_list")
        html = _load_page(
            page,
            url=list_url,
            wait_ms=wait_ms,
            scrolls=scrolls,
            accept_selector=str(accept_selector) if accept_selector else None,
            timeout_ms=timeout_ms,
        )
        if not html or _reader_looks_blocked(html):
            raise FetchError("Browser reader list fetch blocked or empty.", url=list_url)
        discovery_limit = _SUBSTACK_INBOX_WORKSET_LIMIT + len(seen_urls) if site == "substack" else max_items
        discovered = _discover_candidates(page, list_url=list_url, site=site, config=cfg, max_items=discovery_limit)
        if site == "substack":
            live_candidates = {row.url: row for row in discovered if row.url not in seen_urls}
            for url, candidate in pending.items():
                live_candidates.setdefault(url, candidate)
            if len(live_candidates) > _SUBSTACK_INBOX_WORKSET_LIMIT:
                raise FetchError("Substack inbox pending workset exceeds 2048 posts; outstanding URLs cannot all be preserved.", url=list_url)
            pending = live_candidates
            candidates = _freshest_candidates(list(pending.values()))
            live_stats = {
                "discovered_count": len(discovered), "candidate_count": len(candidates), "accepted_count": 0,
                "undated_candidate_count": sum(row.published_at is None for row in candidates),
            }
        else:
            candidates = discovered
        candidate_count = len(candidates)

        def budget_available() -> bool:
            nonlocal article_budget_exhausted
            if article_fetches >= max_article_fetches or len(items) >= max_items:
                return False
            if article_budget_s and (monotonic() - articles_started_at) >= article_budget_s:
                article_budget_exhausted = True
                return False
            return True

        def fetch_article(candidate: ReaderCandidate, *, candidate_list_url: str) -> bool:
            nonlocal article_fetches, skipped
            article_fetches += 1
            mark_browser_progress(cfg, f"{site}:article:{article_fetches}/{max_article_fetches}")
            try:
                item = _build_article_item(
                    page=page, context=context, site=site, candidate=candidate,
                    list_url=candidate_list_url, config=cfg,
                )
            except FetchError as exc:
                article_errors.append({"url": candidate.url, "error": str(exc)})
                skipped += 1
                return False
            if item is None:
                skipped += 1
                return False
            items.append(item)
            complete = (item.get("extra") or {}).get("extraction_status", "visible") == "visible"
            if complete and site == "substack":
                pending.pop(candidate.url, None)
                for url in (candidate.url, str(item.get("canonical_url") or item.get("url") or "")):
                    if url:
                        seen_urls.pop(url, None)
                        seen_urls[url] = None
            return complete

        for candidate in candidates:
            if not budget_available():
                skipped += 1
                continue
            complete = fetch_article(candidate, candidate_list_url=list_url)
            if site == "substack" and complete:
                live_stats["accepted_count"] += 1

        backfill_publication: dict[str, str] | None = None
        backfill_offset = 0
        backfill_index = 0
        backfill_enabled = site == "substack" and bool(cfg.get("archive_backfill_enabled"))
        publications: list[dict[str, str]] = []
        backfill_state: dict[str, Any] = {}
        if backfill_enabled:
            publications, cursor_payload = _substack_backfill_state(cfg, cursor_payload)
            backfill_state = cursor_payload["substack_archive_backfill"]
            completed = set(backfill_state.get("completed") or [])
            available = [row for row in publications if row["url"] not in completed]
            if available and budget_available():
                start = int(backfill_state.get("next_publication_index") or 0) % len(publications)
                for delta in range(len(publications)):
                    idx = (start + delta) % len(publications)
                    if publications[idx]["url"] not in completed:
                        backfill_index = idx
                        backfill_publication = publications[idx]
                        break
            elif not available:
                backfill_state["complete"] = True

        if backfill_publication is not None:
            candidate_list_url = f"{backfill_publication['url']}/archive"
            today = datetime.now(timezone.utc).date()
            try:
                cutoff = today.replace(year=today.year - 1)
            except ValueError:
                cutoff = today.replace(year=today.year - 1, day=28)
            archive_limit = min(max_article_fetches - article_fetches, max_items - len(items))
            offsets = backfill_state["offsets"]
            backfill_offset = max(0, _maybe_int(offsets.get(backfill_publication["url"]), 0))
            archive_unavailable = False
            archive_error: str | None = None
            try:
                archive_candidates = _substack_archive_candidates(
                    context,
                    publication=backfill_publication,
                    offset=backfill_offset,
                    limit=archive_limit,
                    timeout_ms=timeout_ms,
                )
            except FetchError as exc:
                # Custom-domain publications can stop exposing Substack's
                # archive API after a migration. Treat a definitive 404 as an
                # exhausted backfill target so one stale publication cannot
                # wedge the live inbox source forever. Transient failures and
                # auth/challenge responses still fail loudly.
                if "HTTP 404" not in str(exc):
                    if not items:
                        raise
                    # Archive failure must not discard successful live ingestion.
                    archive_error = str(exc)
                else:
                    archive_unavailable = True
                archive_candidates = []
            candidate_count += len(archive_candidates)
            backfill_stats = {
                "active": True,
                "publication": backfill_publication["name"],
                "publication_url": backfill_publication["url"],
                "offset_before": backfill_offset,
                "publication_count": len(publications),
                "completed_publication_count": len(backfill_state.get("completed") or []),
                "archive_unavailable": archive_unavailable,
                "cutoff_date": cutoff.isoformat(),
            }
            if archive_error:
                backfill_stats["error"] = archive_error
            backfill_advanced = 0
            cutoff_reached = False
            for candidate in archive_candidates:
                if candidate.published_at is None:
                    backfill_stats["publication_date_gap"] = {
                        "url": candidate.url,
                        "error": "Archive post has no usable publication date; cutoff eligibility is unknown.",
                    }
                    break
                if candidate.published_at is not None and candidate.published_at.date() < cutoff:
                    cutoff_reached = True
                    break
                if candidate.url in seen_urls:
                    backfill_advanced += 1
                    continue
                if not budget_available():
                    break
                if not fetch_article(candidate, candidate_list_url=candidate_list_url):
                    break
                backfill_advanced += 1
            offsets = backfill_state["offsets"]
            if not archive_error:
                offsets[backfill_publication["url"]] = backfill_offset + backfill_advanced
            exhausted = not archive_error and (archive_unavailable or cutoff_reached or len(archive_candidates) < archive_limit)
            if cutoff_reached or (exhausted and backfill_advanced == len(archive_candidates)):
                completed = list(dict.fromkeys([*(backfill_state.get("completed") or []), backfill_publication["url"]]))
                backfill_state["completed"] = completed
            if not archive_error:
                backfill_state["next_publication_index"] = (backfill_index + 1) % max(1, len(publications))
                backfill_state["complete"] = len(backfill_state.get("completed") or []) >= len(publications)
            backfill_stats.update(
                {
                    "offset_after": backfill_offset + backfill_advanced,
                    "batch_exhausted_publication": exhausted,
                    "completed_publication_count": len(backfill_state.get("completed") or []),
                    "complete": bool(backfill_state.get("complete")),
                    "cutoff_reached": cutoff_reached,
                }
            )
        if site == "substack":
            inbox_state.update({
                "version": 1,
                "seen_urls": list(seen_urls)[-_SUBSTACK_INBOX_WORKSET_LIMIT:],
                "pending": [{
                    "url": row.url,
                    "title_hint": row.title_hint,
                    "published_at": row.published_at.isoformat() if row.published_at else None,
                } for row in pending.values()],
            })
            cursor_payload["substack_live_inbox"] = inbox_state
            live_stats["pending_count"] = len(pending)
            next_cursor = cursor_payload

        context.close()
        if browser:
            browser.close()

    mark_browser_progress(cfg, f"{site}:completed")
    return {
        "items": items,
        "cursor": next_cursor,
        "stats": {
            "connector": f"{site}_reader_browser",
            "site": site,
            "list_url": list_url,
            "candidate_count": candidate_count,
            "article_fetch_count": article_fetches,
            "max_article_fetches": max_article_fetches,
            "article_fetch_budget_s": article_budget_s,
            "article_budget_exhausted": article_budget_exhausted,
            "accepted_count": len(items),
            "skipped_count": skipped,
            "policy": "visible_only_no_paywall_bypass",
            "archive_backfill": backfill_stats,
            "live_inbox": live_stats,
            "article_errors": article_errors,
        },
    }


def fetch_substack_inbox_browser(
    config: dict[str, Any], *, source_cursor: dict[str, Any] | None = None
) -> dict[str, Any]:
    cfg = dict(config or {})
    embedded_cursor = cfg.pop("_source_cursor", None)
    if source_cursor is None and isinstance(embedded_cursor, dict):
        source_cursor = dict(embedded_cursor)
    cfg.setdefault("allowed_hosts", ["substack.com", "*.substack.com"])
    cfg.setdefault("skip_paywalled", True)
    cfg.setdefault("require_authenticated_session", True)
    return _fetch_paid_reader_browser(cfg, site="substack", source_cursor=source_cursor)


def fetch_note_creator_browser(config: dict[str, Any]) -> dict[str, Any]:
    cfg = dict(config or {})
    cfg.setdefault("allowed_hosts", ["note.com"])
    cfg.setdefault("skip_paywalled", True)
    cfg.setdefault("require_authenticated_session", True)
    return _fetch_paid_reader_browser(cfg, site="note")


def probe_paid_reader_browser(config: dict[str, Any], *, site: str | None = None) -> dict[str, Any]:
    cfg = dict(config or {})
    site = str(site or cfg.pop("_probe_site", None) or "substack").strip().lower()
    if site not in {"substack", "note"}:
        raise ValueError("paid reader probe site must be substack or note")
    cfg["download_article_images"] = False
    cfg["store_article_html_attachment"] = False
    cfg["max_items_per_run"] = min(_maybe_int(cfg.get("max_items_per_run"), 5), 5)
    result = _fetch_paid_reader_browser(cfg, site=site)
    items = list(result.get("items") or [])
    return {
        "count": len(items),
        "first_url": items[0].get("url") if items else None,
        "stats": result.get("stats") or {},
    }
