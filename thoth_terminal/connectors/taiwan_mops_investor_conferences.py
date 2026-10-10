from __future__ import annotations

import html
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

import requests
import urllib3

from thoth_terminal.connectors.official_common import keyword_allowed, stable_external_id
from thoth_terminal.localized_names import resolve_taiwan_company_name
from thoth_terminal.net import DEFAULT_HEADERS


MOPSOV_BASE = "https://mopsov.twse.com.tw"
MOPS_CONFERENCE_PAGE = f"{MOPSOV_BASE}/mops/web/t100sb02_1"
MOPS_CONFERENCE_AJAX = f"{MOPSOV_BASE}/mops/web/ajax_t100sb02_1"
MOPS_FILE_DOWNLOAD = f"{MOPSOV_BASE}/server-java/FileDownLoad"
MOPS_CONFERENCE_FILE_PATH = "/home/html/nas/STR/"

_TR_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_CELL_RE = re.compile(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", re.IGNORECASE | re.DOTALL)
_FILE_NAME_RE = re.compile(r"fileName\.value\s*=\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)
_HREF_RE = re.compile(r"href\s*=\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)


def _string_set(value: object) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        raw_items = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raw_items = [value]
    return {str(v).strip() for v in raw_items if str(v).strip()}


def _cfg_map_value(cfg: dict[str, Any], key: str, code: str) -> Any:
    mapping = cfg.get(key)
    if not isinstance(mapping, dict):
        return None
    return mapping.get(code) or mapping.get(str(code).strip().upper())


def _strip_html(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", html.unescape(text).replace("\xa0", " ")).strip()


def _roc_year_months(cfg: dict[str, Any]) -> list[tuple[int, str]]:
    raw = cfg.get("query_year_months")
    if isinstance(raw, (list, tuple, set)):
        out: list[tuple[int, str]] = []
        for value in raw:
            text = str(value or "").strip()
            match = re.match(r"(?P<year>\d{2,4})[-/](?P<month>\d{1,2})$", text)
            if not match:
                continue
            year = int(match.group("year"))
            if year > 1900:
                year -= 1911
            month = int(match.group("month"))
            if 1 <= year <= 300 and 1 <= month <= 12:
                out.append((year, f"{month:02d}"))
        if out:
            return out
    now = datetime.now()
    months_back = max(1, min(int(cfg.get("months_back") or 8), 36))
    out = []
    year = now.year
    month = now.month
    for _ in range(months_back):
        out.append((year - 1911, f"{month:02d}"))
        month -= 1
        if month <= 0:
            month = 12
            year -= 1
    return out


def _market_configs(markets: object) -> list[dict[str, str]]:
    if markets is None or str(markets).strip() == "":
        raw_markets = ["listed", "otc"]
    elif isinstance(markets, (list, tuple, set)):
        raw_markets = list(markets)
    else:
        raw_markets = [markets]
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for value in raw_markets:
        market = str(value or "").strip().lower()
        if market in {"listed", "twse", "sii", "l"}:
            cfg = {"market": "listed", "typek": "sii", "exchange": "TWSE"}
        elif market in {"otc", "tpex", "two", "o"}:
            cfg = {"market": "otc", "typek": "otc", "exchange": "TPEX"}
        elif market in {"emerging", "rotc"}:
            cfg = {"market": "emerging", "typek": "rotc", "exchange": "ROTC"}
        elif market in {"public", "pub"}:
            cfg = {"market": "public", "typek": "pub", "exchange": "PUBLIC"}
        else:
            continue
        if cfg["typek"] not in seen:
            out.append(cfg)
            seen.add(cfg["typek"])
    return out or _market_configs(["listed", "otc"])


def _query_grid(cfg: dict[str, Any]) -> list[tuple[int, str, dict[str, str]]]:
    markets = _market_configs(cfg.get("markets") or ["listed", "otc"])
    return [(year, month, market_cfg) for year, month in _roc_year_months(cfg) for market_cfg in markets]


def _query_limit(cfg: dict[str, Any], grid_size: int) -> int:
    raw_limit = cfg.get("requests_per_run") or cfg.get("queries_per_run")
    if raw_limit is None:
        return grid_size if cfg.get("query_year_months") is not None else min(grid_size, 4)
    return max(1, min(int(raw_limit), grid_size))


def _request_mops_conference_html(*, typek: str, roc_year: int, month: str, timeout_s: float) -> str:
    headers = {
        **DEFAULT_HEADERS,
        "Accept": "text/html,application/xhtml+xml,*/*",
        "Origin": MOPSOV_BASE,
        "Referer": MOPS_CONFERENCE_PAGE,
    }
    data = {
        "step": "1",
        "firstin": "ture",
        "off": "1",
        "TYPEK": typek,
        "year": str(int(roc_year)),
        "month": str(month).zfill(2),
        "co_id": "",
    }
    try:
        resp = requests.post(MOPS_CONFERENCE_AJAX, headers=headers, data=data, timeout=timeout_s)
    except requests.exceptions.SSLError:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        resp = requests.post(MOPS_CONFERENCE_AJAX, headers=headers, data=data, timeout=timeout_s, verify=False)
    resp.raise_for_status()
    if "big5" in str(resp.headers.get("content-type") or "").lower():
        resp.encoding = "big5"
    return resp.text


def _roc_datetime(date_text: object, time_text: object = None) -> datetime | None:
    raw_date = str(date_text or "").strip()
    raw_time = str(time_text or "").strip() or "00:00"
    match = re.search(r"(?P<y>\d{2,3})[/-](?P<m>\d{1,2})[/-](?P<d>\d{1,2})", raw_date)
    if not match:
        return None
    time_match = re.search(r"(?P<h>\d{1,2}):(?P<mi>\d{1,2})(?::(?P<s>\d{1,2}))?", raw_time)
    try:
        year = int(match.group("y"))
        if year < 1000:
            year += 1911
        return datetime(
            year,
            int(match.group("m")),
            int(match.group("d")),
            int(time_match.group("h")) if time_match else 0,
            int(time_match.group("mi")) if time_match else 0,
            int(time_match.group("s")) if time_match and time_match.group("s") else 0,
        )
    except Exception:
        return None


def _taiwan_result_period_for_event(dt: datetime | None) -> tuple[int | None, int | None]:
    if dt is None:
        return None, None
    if dt.month <= 3:
        return dt.year - 1, 4
    if dt.month <= 6:
        return dt.year, 1
    if dt.month <= 9:
        return dt.year, 2
    return dt.year, 3


def _download_url(filename: str | None) -> str | None:
    clean = str(filename or "").strip()
    if not clean:
        return None
    query = urlencode(
        {
            "step": "9",
            "filePath": MOPS_CONFERENCE_FILE_PATH,
            "fileName": clean,
            "functionName": "t100sb02_1",
        }
    )
    return f"{MOPS_FILE_DOWNLOAD}?{query}"


def _hrefs(cell_html: str) -> list[str]:
    out: list[str] = []
    for href in _HREF_RE.findall(cell_html or ""):
        text = html.unescape(href).strip()
        if text and text != "#" and text not in out:
            out.append(text)
    return out


def _file_name(cell_html: str) -> str | None:
    match = _FILE_NAME_RE.search(cell_html or "")
    return html.unescape(match.group(1)).strip() if match else None


def _mapped_company_fields(cfg: dict[str, Any], code: str) -> dict[str, Any]:
    company_id = _cfg_map_value(cfg, "company_id_by_stock_code", code)
    company_slug = str(_cfg_map_value(cfg, "company_slug_by_stock_code", code) or "").strip()
    ticker = str(_cfg_map_value(cfg, "ticker_by_stock_code", code) or "").strip()
    return {
        "company_id": int(company_id) if str(company_id or "").isdigit() else None,
        "resolved_company_id": int(company_id) if str(company_id or "").isdigit() else None,
        "company_slug": company_slug or None,
        "ticker": ticker or code,
    }


def _source_identifiers(code: str, exchange: str) -> list[dict[str, Any]]:
    return [
        {
            "id_type": "TW_STOCK",
            "id_value": code,
            "exchange": exchange,
            "source_key": "company_code",
            "source": "taiwan_mops_investor_conferences",
            "market": exchange,
            "is_primary": True,
        }
    ] if code else []


def _conference_item(row: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any] | None:
    code = str(row.get("code") or "").strip()
    exchange = str(row.get("exchange") or "TWSE").strip()
    local_name = str(row.get("company_name_original") or "").strip()
    identity = resolve_taiwan_company_name(company_code=code, local_name=local_name, config=cfg)
    mapped_name = str(_cfg_map_value(cfg, "company_name_by_stock_code", code) or "").strip()
    company = str(mapped_name or identity.get("company_name") or local_name or code or "Taiwan issuer").strip()
    company_en = str(identity.get("company_name_en") or "").strip()
    aliases = [str(alias).strip() for alias in (identity.get("company_aliases") or []) if str(alias).strip()]
    published_at = row.get("published_at") if isinstance(row.get("published_at"), datetime) else None
    fiscal_year, fiscal_quarter = _taiwan_result_period_for_event(published_at)
    zh_url = _download_url(str(row.get("zh_file") or "").strip() or None)
    en_url = _download_url(str(row.get("en_file") or "").strip() or None)
    video_urls = [str(url).strip() for url in (row.get("video_urls") or []) if str(url).strip()]
    website_urls = [str(url).strip() for url in (row.get("website_urls") or []) if str(url).strip()]
    primary_url = en_url or zh_url or (video_urls[0] if video_urls else None) or str(row.get("query_url") or MOPS_CONFERENCE_PAGE)
    summary = str(row.get("summary") or "").strip()
    period_label = f"FY{fiscal_year} Q{fiscal_quarter}" if fiscal_year is not None and fiscal_quarter is not None else "investor"
    title = f"{company} {period_label} 法人說明會簡報 (MOPS)"
    body = "\n".join(
        part
        for part in [
            title,
            f"Company: {company} ({code})",
            f"Original company name: {local_name}" if local_name and local_name != company else "",
            f"Exchange: {exchange}",
            f"Event date: {published_at.date().isoformat()}" if published_at else "",
            f"Event time: {row.get('time_raw')}" if row.get("time_raw") else "",
            f"Location: {row.get('location')}" if row.get("location") else "",
            f"Summary: {summary}" if summary else "",
            f"Chinese presentation file: {row.get('zh_file')}" if row.get("zh_file") else "",
            f"English presentation file: {row.get('en_file')}" if row.get("en_file") else "",
            f"Company website: {website_urls[0]}" if website_urls else "",
            f"Video/audio URL: {video_urls[0]}" if video_urls else "",
            f"Notes: {row.get('notes')}" if row.get("notes") else "",
            f"MOPS query: {row.get('query_url')}" if row.get("query_url") else "",
        ]
        if part
    )
    if not keyword_allowed(cfg, code, company, company_en, local_name, *aliases, title, body):
        return None
    mapped = _mapped_company_fields(cfg, code)
    attachments = [url for url in [zh_url, en_url, *video_urls, *website_urls] if url]
    return {
        "kind": "exchange_disclosure",
        "external_id": stable_external_id(
            "taiwan_mops_investor_conferences",
            exchange,
            code,
            row.get("date_raw"),
            row.get("time_raw"),
            row.get("location"),
            row.get("summary"),
        ),
        "url": primary_url,
        "canonical_url": primary_url,
        "title": title,
        "author": company,
        "published_at": published_at,
        "raw_text": body,
        "normalized_text": body,
        "extra": {
            "connector": "taiwan_mops_investor_conferences",
            "regulator": "MOPS",
            "exchange": exchange,
            "jurisdiction": "TW",
            "stock_code": code,
            **mapped,
            "company_name": company,
            "company_name_en": company_en or None,
            "company_name_original": local_name or None,
            "company_display_name": company,
            "company_aliases": aliases,
            "company_name_source": identity.get("company_name_source"),
            "source_section": "investor_conferences",
            "is_call_material": True,
            "artifact_type": "presentation",
            "artifact_subtype": "earnings",
            "company_doc_type": "investor_presentation",
            "company_doc_label": title,
            "fiscal_year": fiscal_year,
            "fiscal_quarter": fiscal_quarter,
            "event_date": published_at.date().isoformat() if published_at else None,
            "event_time": row.get("time_raw"),
            "event_location": row.get("location"),
            "event_summary": summary or None,
            "event_external_key": stable_external_id(
                "taiwan_mops_investor_conferences_event",
                exchange,
                code,
                row.get("date_raw"),
                row.get("time_raw"),
                row.get("location"),
                row.get("summary"),
            ),
            "presentation_zh_url": zh_url,
            "presentation_en_url": en_url,
            "presentation_zh_file": row.get("zh_file"),
            "presentation_en_file": row.get("en_file"),
            "video_urls": video_urls,
            "company_website_urls": website_urls,
            "attachment_urls": attachments,
            "mops_query_url": row.get("query_url"),
            "source_identifiers": _source_identifiers(code, exchange),
            "source_row": row.get("source_row"),
        },
    }


def _parse_conference_rows(
    html_text: str,
    *,
    cfg: dict[str, Any],
    exchange: str,
    market: str,
    query_url: str,
    company_codes: set[str],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for tr in _TR_RE.findall(html_text or ""):
        cells_html = _CELL_RE.findall(tr)
        if len(cells_html) < 11:
            continue
        cells = [_strip_html(cell) for cell in cells_html]
        code = cells[0].strip()
        if not code or (company_codes and code not in company_codes):
            continue
        row = {
            "market": market,
            "exchange": exchange,
            "code": code,
            "company_name_original": cells[1].strip() if len(cells) > 1 else "",
            "date_raw": cells[2].strip() if len(cells) > 2 else "",
            "time_raw": cells[3].strip() if len(cells) > 3 else "",
            "location": cells[4].strip() if len(cells) > 4 else "",
            "summary": cells[5].strip() if len(cells) > 5 else "",
            "zh_file": _file_name(cells_html[6]) if len(cells_html) > 6 else None,
            "en_file": _file_name(cells_html[7]) if len(cells_html) > 7 else None,
            "website_urls": _hrefs(cells_html[8]) if len(cells_html) > 8 else [],
            "video_urls": _hrefs(cells_html[9]) if len(cells_html) > 9 else [],
            "notes": cells[10].strip() if len(cells) > 10 else "",
            "query_url": query_url,
            "source_row": {
                "cells": cells,
            },
        }
        row["published_at"] = _roc_datetime(row["date_raw"], row["time_raw"])
        item = _conference_item(row, cfg)
        if item is not None:
            out.append(item)
    return out


def fetch_taiwan_mops_investor_conferences(
    config: dict[str, Any],
    *,
    source_cursor: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = dict(config or {})
    cursor = dict(source_cursor or {})
    timeout_s = float(cfg.get("timeout_s") or 30.0)
    company_codes = _string_set(cfg.get("company_codes") or cfg.get("stock_codes"))
    max_items = max(1, min(int(cfg.get("max_items_per_run") or 500), 1000))
    grid = _query_grid(cfg)
    query_limit = _query_limit(cfg, len(grid)) if grid else 0
    rotate_queries = bool(grid and query_limit < len(grid))
    start_index = int(cursor.get("query_index") or 0) % len(grid) if grid else 0
    items: list[dict[str, Any]] = []
    stats: dict[str, Any] = {
        "source_kind": "taiwan_mops_investor_conferences",
        "queried": [],
        "errors": [],
        "company_code_count": len(company_codes),
        "query_count": len(grid),
        "query_limit": query_limit,
        "query_index_start": start_index if rotate_queries else 0,
        "rotated": rotate_queries,
    }
    processed_queries = 0
    for offset in range(query_limit):
        if not grid or len(items) >= max_items:
            break
        year, month, market_cfg = grid[(start_index + offset) % len(grid)]
        processed_queries += 1
        query_url = f"{MOPS_CONFERENCE_AJAX}?TYPEK={market_cfg['typek']}&year={year}&month={month}"
        try:
            text = _request_mops_conference_html(
                typek=market_cfg["typek"],
                roc_year=year,
                month=month,
                timeout_s=timeout_s,
            )
        except Exception as exc:
            stats["errors"].append(
                {
                    "market": market_cfg["market"],
                    "year": year,
                    "month": month,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        rows = _parse_conference_rows(
            text,
            cfg=cfg,
            exchange=market_cfg["exchange"],
            market=market_cfg["market"],
            query_url=query_url,
            company_codes=company_codes,
        )
        stats["queried"].append(
            {
                "market": market_cfg["market"],
                "year": year,
                "month": month,
                "matched_items": len(rows),
            }
        )
        for item in rows:
            items.append(item)
            if len(items) >= max_items:
                break
    items.sort(key=lambda item: (item.get("published_at") or datetime.min, str(item.get("external_id") or "")), reverse=True)
    stats["count"] = len(items)
    if rotate_queries:
        next_cursor = dict(cursor)
        next_cursor["query_index"] = (start_index + processed_queries) % len(grid)
    else:
        next_cursor = dict(source_cursor or {}) or None
    stats["query_index_next"] = (next_cursor or {}).get("query_index") if isinstance(next_cursor, dict) else None
    return {"items": items[:max_items], "cursor": next_cursor, "stats": stats}
