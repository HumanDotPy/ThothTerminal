from __future__ import annotations

from pathlib import Path
import logging
import re
from urllib.parse import urljoin, urlparse

from playwright.sync_api import sync_playwright

from thoth_terminal.connectors.document_fetch import build_fetched_document_payload
from thoth_terminal.connectors.web_list import (
    _build_items,
    _collect_candidates,
    _looks_like_reuters_article,
    _resolve_list_url,
)
from thoth_terminal.net import FetchError, fetch_with_fallback


_BLOCK_PATTERNS = (
    "cf-chl",
    "challenge-platform",
    "attention required",
    "verify you are human",
    "access denied",
    "request blocked",
    "bot detection",
    "captcha-delivery.com",
    "geo.captcha-delivery.com",
    "datadome captcha",
    'title="datadome captcha"',
    "please enable js and disable any ad blocker",
)

_TRENDFORCE_PRESS_RE = re.compile(r"^https://www\.trendforce\.com/presscenter/news/\d{8}-\d+\.html$")
_TRENDFORCE_NEWS_RE = re.compile(r"^https://www\.trendforce\.com/news/\d{4}/\d{2}/\d{2}/[^/?#]+/?$")


def _looks_blocked(html: str | None) -> bool:
    if not html:
        return True
    lowered = html.lower()
    return any(token in lowered for token in _BLOCK_PATTERNS)


def _maybe_int(value: object, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _is_reuters_list_url(list_url: str) -> bool:
    return "reuters.com" in urlparse(list_url).netloc.lower()


def _is_trendforce_list_url(list_url: str) -> bool:
    return "trendforce.com" in urlparse(list_url).netloc.lower()


def _is_reuters_article_url(url: str, *, list_url: str) -> bool:
    _ = list_url
    return _looks_like_reuters_article(url)


def _is_trendforce_article_url(url: str) -> bool:
    if _TRENDFORCE_PRESS_RE.match(url):
        return True
    return bool(_TRENDFORCE_NEWS_RE.match(url))


def _discover_urls_from_anchors(page, *, list_url: str) -> list[str]:
    try:
        hrefs = page.eval_on_selector_all(
            "a[href]",
            "nodes => nodes.map(n => n.getAttribute('href')).filter(Boolean)",
        )
    except Exception:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for href in hrefs or []:
        abs_url = urljoin(list_url, str(href))
        if abs_url in seen:
            continue
        seen.add(abs_url)
        out.append(abs_url)
    return out


def _discover_site_candidates(
    page,
    *,
    html: str,
    list_url: str,
    config: dict,
    max_items: int,
) -> list[tuple[str, str | None]]:
    if _is_reuters_list_url(list_url):
        seen: set[str] = set()
        out: list[tuple[str, str | None]] = []
        for url in _discover_urls_from_anchors(page, list_url=list_url):
            if not _is_reuters_article_url(url, list_url=list_url):
                continue
            if url in seen:
                continue
            seen.add(url)
            out.append((url, None))
            if len(out) >= max_items:
                return out
        # Keep fallback for static snapshots while still enforcing strict Reuters URL filters.
        for url, title_hint in _collect_candidates(html, list_url=list_url, config=config):
            if not _is_reuters_article_url(url, list_url=list_url):
                continue
            if url in seen:
                continue
            seen.add(url)
            out.append((url, title_hint))
            if len(out) >= max_items:
                break
        return out

    if _is_trendforce_list_url(list_url):
        seen: set[str] = set()
        out: list[tuple[str, str | None]] = []
        for url in _discover_urls_from_anchors(page, list_url=list_url):
            if not _is_trendforce_article_url(url):
                continue
            if url in seen:
                continue
            seen.add(url)
            out.append((url, None))
            if len(out) >= max_items:
                return out
        for url, title_hint in _collect_candidates(html, list_url=list_url, config=config):
            if not _is_trendforce_article_url(url):
                continue
            if url in seen:
                continue
            seen.add(url)
            out.append((url, title_hint))
            if len(out) >= max_items:
                break
        return out

    return _collect_candidates(html, list_url=list_url, config=config)


def _fetch_html_with_browser(
    *,
    url: str,
    storage_state_path: str | None,
    headless: bool,
    user_agent: str | None,
    browser_channel: str | None,
    user_data_dir: str | None,
    profile_directory: str | None = None,
    slow_mo_ms: int,
    wait_ms: int,
    scrolls: int,
    accept_selector: str | None,
    timeout_ms: int,
) -> str | None:
    # If headless with a real profile dir, many browsers refuse to launch.
    # Prefer storage_state in a non-persistent context for background runs.
    if headless and user_data_dir:
        user_data_dir = None
    # Provide a reasonable default user agent (desktop Edge) if none supplied.
    default_ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0"
    if not user_agent:
        user_agent = default_ua
    with sync_playwright() as p:
        browser = None
        context = None
        page = None
        try:
            browser, context = _launch_context(
                p,
                storage_state_path=storage_state_path,
                headless=headless,
                user_agent=user_agent,
                browser_channel=browser_channel,
                user_data_dir=user_data_dir,
                profile_directory=profile_directory,
                slow_mo_ms=slow_mo_ms,
            )
            page = context.new_page()
            # Stealth: hide webdriver flag.
            page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
            return _load_page(
                page,
                url=url,
                wait_ms=wait_ms,
                scrolls=scrolls,
                accept_selector=accept_selector,
                timeout_ms=timeout_ms,
            )
        finally:
            for resource in (page, context, browser):
                if resource is None:
                    continue
                try:
                    resource.close()
                except Exception:
                    pass


def _mark_progress(config: dict, stage: str) -> None:
    raw = str(config.get("_thoth_progress_path") or "").strip()
    if not raw:
        return
    path = Path(raw)
    temp = path.with_name(f".{path.name}.tmp")
    try:
        temp.write_text(
            __import__("json").dumps({"stage": str(stage), "monotonic": __import__("time").monotonic()}),
            encoding="utf-8",
        )
        temp.replace(path)
    except OSError:
        pass


def _maybe_accept_cookies(page, accept_selector: str | None) -> None:
    selectors = [
        accept_selector,
        "#onetrust-accept-btn-handler",
        "button:has-text('Accept All')",
        "button:has-text('Accept all')",
        "[aria-label='Accept all']",
    ]
    for sel in selectors:
        if not sel:
            continue
        try:
            page.locator(sel).first.click(timeout=2000)
            page.wait_for_timeout(800)
            break
        except Exception:
            continue


def _load_page(
    page,
    *,
    url: str,
    wait_ms: int,
    scrolls: int,
    accept_selector: str | None,
    timeout_ms: int,
) -> str | None:
    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    _maybe_accept_cookies(page, accept_selector)
    # Give the page time to render after consent.
    page.wait_for_timeout(max(wait_ms, 3000))
    if wait_ms > 0:
        page.wait_for_timeout(wait_ms)
    if scrolls > 0:
        for _ in range(scrolls):
            page.mouse.wheel(0, 2200)
            page.wait_for_timeout(800)
    return page.content()


def _launch_context(
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
    # If headless with a real profile dir, many browsers refuse to launch.
    # Prefer storage_state in a non-persistent context for background runs.
    if headless and user_data_dir:
        user_data_dir = None
    default_ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0"
    if not user_agent:
        user_agent = default_ua

    launch_kwargs: dict[str, object] = {"headless": headless}
    if browser_channel:
        launch_kwargs["channel"] = str(browser_channel)
    if slow_mo_ms and slow_mo_ms > 0:
        launch_kwargs["slow_mo"] = int(slow_mo_ms)
    args = list(launch_kwargs.get("args") or [])
    args.extend([
        "--disable-blink-features=AutomationControlled",
        "--disable-features=IsolateOrigins,site-per-process",
    ])
    launch_kwargs["args"] = args

    browser = None
    if user_data_dir:
        if profile_directory:
            launch_args = list(launch_kwargs.get("args") or [])
            launch_args.append(f"--profile-directory={profile_directory}")
            launch_kwargs["args"] = launch_args
        context_kwargs: dict[str, object] = {
            "user_agent": user_agent,
            "viewport": {"width": 1366, "height": 900},
            "locale": "en-US",
            "timezone_id": "America/New_York",
        }
        try:
            context = playwright.chromium.launch_persistent_context(
                str(user_data_dir),
                **launch_kwargs,
                **context_kwargs,
            )
            return browser, context
        except Exception as exc:
            logging.getLogger("thoth.web_list_browser").warning(
                "Persistent context failed; retrying without user_data_dir: %s",
                exc,
            )
            user_data_dir = None
            # Remove profile-directory args when falling back to a non-persistent context.
            args = [arg for arg in (launch_kwargs.get("args") or []) if not str(arg).startswith("--profile-directory=")]
            launch_kwargs["args"] = args

    context_kwargs: dict[str, object] = {
        "user_agent": user_agent,
        "viewport": {"width": 1366, "height": 900},
        "locale": "en-US",
        "timezone_id": "America/New_York",
    }
    if storage_state_path and Path(storage_state_path).exists():
        context_kwargs["storage_state"] = storage_state_path
    browser = playwright.chromium.launch(**launch_kwargs)
    context = browser.new_context(**context_kwargs)

    return browser, context


def _fetch_web_list_browser_once(config: dict) -> list[dict]:
    list_url = config.get("list_url")
    if not list_url:
        raise ValueError("web_list_browser connector requires config.list_url")

    list_url = _resolve_list_url(str(list_url))
    storage_state_path = config.get("storage_state_path") or config.get("browser_storage_state_path")
    headless = bool(config.get("headless", True))
    user_agent = config.get("user_agent")
    browser_channel = config.get("browser_channel")
    user_data_dir = config.get("user_data_dir") or config.get("browser_user_data_dir")
    profile_directory = config.get("profile_directory") or config.get("browser_profile_directory")
    storage_state_path = storage_state_path or config.get("storage_state") or config.get("browser_storage_state_path")
    if not storage_state_path:
        from thoth_terminal.config import settings
        if getattr(settings, "x_storage_state_path", None):
            storage_state_path = str(settings.x_storage_state_path)
    slow_mo_ms = _maybe_int(config.get("slow_mo_ms"), 0)
    wait_ms = _maybe_int(config.get("browser_wait_ms"), 2500)
    scrolls = _maybe_int(config.get("browser_scrolls"), 0)
    accept_selector = config.get("accept_cookies_selector")
    timeout_ms = _maybe_int(config.get("browser_timeout_ms"), 60_000)
    # Fallback wait/scroll to help challenge pages render before we bail.
    if wait_ms < 4000:
        wait_ms = 4000
    if scrolls < 2:
        scrolls = 2
    max_items = int(config.get("max_items_per_run", 25) or 25)
    fetch_articles = bool(config.get("browser_fetch_articles", False))
    fetch_article = bool(config.get("fetch_article", True))
    article_timeout_s = float(config.get("article_timeout_s") or 25.0)

    _mark_progress(config, "launching_browser")
    with sync_playwright() as p:
        browser = None
        context = None
        page = None
        try:
            browser, context = _launch_context(
            p,
            storage_state_path=storage_state_path,
            headless=headless,
            user_agent=user_agent,
            browser_channel=browser_channel,
            user_data_dir=user_data_dir,
            profile_directory=profile_directory,
            slow_mo_ms=slow_mo_ms,
            )
            page = context.new_page()
            page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
            _mark_progress(config, "loading_list_page")
            html = _load_page(
            page,
            url=list_url,
            wait_ms=wait_ms,
            scrolls=scrolls,
            accept_selector=accept_selector,
            timeout_ms=timeout_ms,
            )
            if _looks_blocked(html):
                raise FetchError("Browser fetch blocked or returned challenge page.", url=list_url)
            if not html:
                return []

            _mark_progress(config, "discovering_candidates")
            candidates = _discover_site_candidates(
            page,
            html=html,
            list_url=list_url,
            config=config,
            max_items=max_items,
            )

            article_index = 0

            def _fetch_article_html(url: str) -> str | dict | None:
                nonlocal article_index
                article_index += 1
                _mark_progress(config, f"fetching_article:{article_index}/{len(candidates)}")
                pdf_hint = (urlparse(url).path or "").strip().lower().endswith(".pdf")
                if fetch_articles and not pdf_hint:
                    try:
                        return _load_page(
                            page,
                            url=url,
                            wait_ms=wait_ms,
                            scrolls=0,
                            accept_selector=accept_selector,
                            timeout_ms=timeout_ms,
                        )
                    except Exception as exc:
                        raise FetchError(f"Browser fetch failed for {url}: {exc}", url=url) from exc
                if not fetch_article:
                    return None
                try:
                    return build_fetched_document_payload(
                        request_url=url,
                        result=fetch_with_fallback(url, timeout_s=article_timeout_s),
                        download_article_images=bool(config.get("download_article_images")),
                        max_images=3,
                    )
                except Exception as exc:
                    raise FetchError(f"HTTP article fetch failed for {url}: {exc}", url=url) from exc

            items = _build_items(candidates, list_url=list_url, config=config, fetch_article_html=_fetch_article_html)
            _mark_progress(config, "completed")
            return items
        finally:
            for resource in (page, context, browser):
                if resource is None:
                    continue
                try:
                    resource.close()
                except Exception:
                    pass


def fetch_web_list_browser(config: dict) -> list[dict]:
    try:
        return _fetch_web_list_browser_once(config)
    except Exception as exc:
        list_url = _resolve_list_url(str(config.get("list_url") or ""))
        strict_headless = _is_reuters_list_url(list_url) or _is_trendforce_list_url(list_url)
        headless = bool(config.get("headless", True))
        retry_headed = bool(config.get("retry_headed_on_fail", False))
        if strict_headless:
            retry_headed = False
        if headless and retry_headed:
            retry_cfg = dict(config or {})
            retry_cfg["headless"] = False
            return _fetch_web_list_browser_once(retry_cfg)
        raise exc


def probe_web_list_browser(config: dict) -> dict:
    cfg = dict(config or {})
    cfg["browser_fetch_articles"] = False
    cfg["max_items_per_run"] = min(int(cfg.get("max_items_per_run", 5) or 5), 5)
    items = fetch_web_list_browser(cfg)
    first_url = items[0].get("url") if items else None
    return {"count": len(items), "first_url": first_url}
