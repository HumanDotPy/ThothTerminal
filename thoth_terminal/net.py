from __future__ import annotations

from dataclasses import dataclass

import requests


DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.7,zh;q=0.6,ko;q=0.5,ja;q=0.4",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

ALT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
        "Mobile/15E148 Safari/604.1"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


@dataclass(frozen=True)
class FetchResult:
    url: str
    status_code: int
    content_type: str | None
    text: str | None
    content: bytes


class FetchError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, url: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.url = url


def _fetch_raw(url: str, *, headers: dict, timeout_s: float) -> FetchResult:
    try:
        resp = requests.get(
            url,
            headers=headers,
            timeout=timeout_s,
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        raise FetchError(f"Request failed for {url}: {exc}", url=url) from exc

    content = resp.content
    content_type = resp.headers.get("content-type")
    text = None
    try:
        text = resp.text
    except Exception:
        text = None

    if resp.status_code >= 400:
        raise FetchError(
            f"HTTP {resp.status_code} for {url}",
            status_code=resp.status_code,
            url=str(resp.url),
        )

    return FetchResult(
        url=str(resp.url),
        status_code=resp.status_code,
        content_type=content_type,
        text=text,
        content=content,
    )


def fetch(url: str, *, timeout_s: float = 25.0) -> FetchResult:
    return _fetch_raw(url, headers=DEFAULT_HEADERS, timeout_s=timeout_s)


def fetch_with_fallback(url: str, *, timeout_s: float = 25.0, max_attempts: int | None = None) -> FetchResult:
    attempts = 0

    def _can_try() -> bool:
        if max_attempts is None:
            return True
        return attempts < max(1, int(max_attempts))

    def _record_try() -> None:
        nonlocal attempts
        attempts += 1

    if not _can_try():
        raise FetchError(f"No fetch attempts configured for {url}", url=url)
    _record_try()
    try:
        return fetch(url, timeout_s=timeout_s)
    except FetchError:
        pass

    if not _can_try():
        raise FetchError(f"Fetch failed for {url}", url=url)
    _record_try()
    try:
        return _fetch_raw(url, headers=ALT_HEADERS, timeout_s=timeout_s)
    except FetchError:
        pass

    # Last resort: text proxy (useful for blocked pages).
    if not _can_try():
        raise FetchError(f"Fetch failed for {url}", url=url)
    _record_try()
    try:
        tail = url
        if url.startswith("https://"):
            tail = url[len("https://") :]
        elif url.startswith("http://"):
            # Prefer https upgrade when possible
            tail = url[len("http://") :]
        proxy_url = f"https://r.jina.ai/https://{tail}"
        return _fetch_raw(proxy_url, headers=DEFAULT_HEADERS, timeout_s=timeout_s)
    except FetchError as exc:
        raise exc
