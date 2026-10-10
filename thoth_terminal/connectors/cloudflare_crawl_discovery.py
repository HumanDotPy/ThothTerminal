from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlparse

import requests

from thoth_terminal.config import settings
from thoth_terminal.net import DEFAULT_HEADERS


_DEFAULT_BASE = "https://api.cloudflare.com/client/v4"
_TERMINAL_STATUSES = {
    "completed",
    "cancelled_due_to_timeout",
    "cancelled_due_to_limits",
    "cancelled_by_user",
    "errored",
}


def _cf_headers(token: str) -> dict[str, str]:
    return {**DEFAULT_HEADERS, "Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _allowed_url(url: str, *, allowed_hosts: set[str]) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    host = str(parsed.netloc or "").lower().split(":")[0]
    return bool(host) and host in allowed_hosts


def _crawl_result_url(base_url: str, account_id: str, job_id: str) -> str:
    return f"{base_url}/accounts/{account_id}/browser-rendering/crawl/{job_id}"


def _normalize_page_record(page: dict[str, Any], *, allowed_hosts: set[str], start_url: str, rendered: bool) -> dict[str, Any] | None:
    url = str(page.get("url") or "").strip()
    if not url or not _allowed_url(url, allowed_hosts=allowed_hosts):
        return None
    metadata = page.get("metadata") if isinstance(page.get("metadata"), dict) else {}
    title = str(page.get("title") or metadata.get("title") or metadata.get("url") or url).strip() or url
    markdown = str(page.get("markdown") or page.get("text") or "").strip()
    return {
        "kind": "document",
        "external_id": f"cloudflare-crawl:{url}",
        "canonical_url": url,
        "url": url,
        "title": title[:255],
        "author": None,
        "published_at": None,
        "raw_text": markdown or title,
        "normalized_text": markdown or title,
        "extra": {
            "crawl_source": "cloudflare",
            "crawl_parent_url": start_url,
            "crawl_rendered": rendered,
            "crawl_status": str(page.get("status") or "").strip() or None,
        },
    }


def _poll_crawl_job(*, result_url: str, headers: dict[str, str], poll_attempts: int, poll_delay_s: float) -> dict[str, Any]:
    for attempt in range(poll_attempts):
        resp = requests.get(f"{result_url}?limit=1", headers=headers, timeout=60.0)
        resp.raise_for_status()
        body = resp.json()
        result = body.get("result") if isinstance(body, dict) else None
        if isinstance(result, dict):
            status = str(result.get("status") or "").strip().lower()
            if status in _TERMINAL_STATUSES:
                return result
        if attempt + 1 < poll_attempts:
            time.sleep(poll_delay_s)
    raise RuntimeError("cloudflare crawl job did not reach a terminal state within the polling timeout")


def _fetch_crawl_records(*, result_url: str, headers: dict[str, str], page_limit: int) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    cursor: str | None = None
    result_meta: dict[str, Any] | None = None
    while True:
        params = {"status": "completed", "limit": min(page_limit, 1000)}
        if cursor:
            params["cursor"] = cursor
        resp = requests.get(result_url, params=params, headers=headers, timeout=60.0)
        resp.raise_for_status()
        body = resp.json()
        result = body.get("result") if isinstance(body, dict) else None
        if not isinstance(result, dict):
            break
        result_meta = result
        batch = result.get("records")
        if isinstance(batch, list):
            records.extend(item for item in batch if isinstance(item, dict))
        cursor_val = result.get("cursor")
        cursor = str(cursor_val).strip() if cursor_val is not None else ""
        if not cursor:
            break
    return {"records": records, "result": result_meta or {}}


def fetch_cloudflare_crawl_discovery(config: dict[str, Any]) -> dict[str, Any]:
    account_id = str(config.get("cloudflare_account_id") or getattr(settings, "cloudflare_account_id", "") or "").strip()
    api_token = str(config.get("cloudflare_api_token") or getattr(settings, "cloudflare_api_token", "") or "").strip()
    if not account_id or not api_token:
        raise ValueError("cloudflare_crawl_discovery requires Cloudflare account id and api token")
    start_url = str(config.get("start_url") or config.get("list_url") or "").strip()
    if not start_url:
        raise ValueError("cloudflare_crawl_discovery requires start_url")
    allowed_hosts = {str(host).strip().lower() for host in (config.get("allowed_hosts") or []) if str(host).strip()}
    if not allowed_hosts:
        parsed = urlparse(start_url)
        if parsed.netloc:
            allowed_hosts.add(parsed.netloc.lower())
    if not _allowed_url(start_url, allowed_hosts=allowed_hosts):
        raise ValueError("start_url must be inside the configured allowlist")
    page_limit = max(1, min(int(config.get("page_limit") or 20), 100))
    poll_attempts = max(1, min(int(config.get("poll_attempts") or 20), 240))
    poll_delay_s = max(0.2, min(float(config.get("poll_delay_seconds") or 3.0), 60.0))
    rendered = bool(config.get("render", False))
    base_url = str(config.get("cloudflare_api_base") or _DEFAULT_BASE).strip().rstrip("/")
    crawl_url = f"{base_url}/accounts/{account_id}/browser-rendering/crawl"
    headers = _cf_headers(api_token)
    include_patterns = list(config.get("include_patterns") or [])
    exclude_patterns = list(config.get("exclude_patterns") or [])
    payload = {
        "url": start_url,
        "limit": page_limit,
        "render": rendered,
        "depth": max(0, int(config.get("max_depth") or 1)),
        "formats": ["markdown"],
    }
    options: dict[str, Any] = {}
    if include_patterns:
        options["includePatterns"] = include_patterns
    if exclude_patterns:
        options["excludePatterns"] = exclude_patterns
    if options:
        payload["options"] = options
    for key in (
        "source",
        "maxAge",
        "modifiedSince",
        "authenticate",
        "setExtraHTTPHeaders",
        "gotoOptions",
        "waitForSelector",
        "rejectResourceTypes",
    ):
        if key in config and config.get(key) is not None:
            payload[key] = config.get(key)

    resp = requests.post(crawl_url, json=payload, headers=headers, timeout=60.0)
    resp.raise_for_status()
    body = resp.json()
    result = body.get("result") if isinstance(body, dict) else None
    pages: list[dict[str, Any]] = []
    crawl_job_status: str | None = None
    crawl_job_id: str | None = None
    crawl_total: int | None = None
    crawl_finished: int | None = None
    if isinstance(result, dict) and isinstance(result.get("pages"), list):
        pages = [item for item in result.get("pages", []) if isinstance(item, dict)]
    else:
        crawl_job_id = str(result or "").strip() if result is not None else ""
        if crawl_job_id:
            result_url = _crawl_result_url(base_url, account_id, crawl_job_id)
            terminal = _poll_crawl_job(
                result_url=result_url,
                headers=headers,
                poll_attempts=poll_attempts,
                poll_delay_s=poll_delay_s,
            )
            crawl_job_status = str(terminal.get("status") or "").strip() or None
            crawl_total = int(terminal.get("total") or 0) or None
            crawl_finished = int(terminal.get("finished") or 0) or None
            if crawl_job_status != "completed":
                raise RuntimeError(f"cloudflare crawl job ended with status={crawl_job_status}")
            records_payload = _fetch_crawl_records(result_url=result_url, headers=headers, page_limit=page_limit)
            result_meta = records_payload.get("result") if isinstance(records_payload, dict) else {}
            if isinstance(result_meta, dict):
                crawl_job_status = str(result_meta.get("status") or crawl_job_status or "").strip() or None
                crawl_total = int(result_meta.get("total") or crawl_total or 0) or None
                crawl_finished = int(result_meta.get("finished") or crawl_finished or 0) or None
            pages = list(records_payload.get("records") or [])

    items: list[dict[str, Any]] = []
    discovered = 0
    for page in pages:
        if not isinstance(page, dict):
            continue
        item = _normalize_page_record(page, allowed_hosts=allowed_hosts, start_url=start_url, rendered=rendered)
        if not item:
            continue
        discovered += 1
        items.append(item)
    return {
        "items": items,
        "cursor": None,
        "stats": {
            "crawl_start_url": start_url,
            "crawl_discovered": discovered,
            "crawl_page_limit": page_limit,
            "crawl_allowed_hosts": sorted(allowed_hosts),
            "crawl_job_id": crawl_job_id,
            "crawl_job_status": crawl_job_status,
            "crawl_total": crawl_total,
            "crawl_finished": crawl_finished,
        },
    }
