from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

import requests
from bs4 import BeautifulSoup

from thoth_terminal.connectors.official_common import keyword_allowed, recent_date_window, stable_external_id
from thoth_terminal.net import DEFAULT_HEADERS

KRX_KIND_BASE = "https://engkind.krx.co.kr"


def _viewer_url(acpt_no: str, doc_no: str, form_procs_cd: str) -> str:
    return (
        f"{KRX_KIND_BASE}/common/disclsviewer.do?method=search"
        f"&acptNo={acpt_no}&docno={doc_no}&formProcsCd={form_procs_cd}&language=english"
    )


def _list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        raw = [value]
    return [str(v).strip() for v in raw if str(v).strip()]


def _rotate(values: list[str], *, start: int, count: int) -> tuple[list[str], int]:
    if not values or count <= 0:
        return [], start
    out: list[str] = []
    idx = max(0, int(start or 0))
    for _ in range(min(count, len(values))):
        out.append(values[idx % len(values)])
        idx += 1
    return out, idx % len(values)


def _mapped(cfg: dict[str, Any], key: str, lookup: object) -> Any:
    raw = str(lookup or "").strip()
    if not raw:
        return None
    mapping = cfg.get(key)
    if not isinstance(mapping, dict):
        return None
    variants = [raw, raw.upper(), raw.lower(), raw.casefold()]
    for variant in variants:
        if variant in mapping:
            return mapping.get(variant)
    for map_key, value in mapping.items():
        if str(map_key or "").strip().casefold() == raw.casefold():
            return value
    return None


def _norm_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def _query_dates(cfg: dict[str, Any], cursor: dict[str, Any]) -> tuple[list[datetime], dict[str, Any]]:
    start_dt, end_dt = recent_date_window(cfg, default_days=0)
    today = datetime.now().date()
    end_day = min(end_dt.date(), today)
    start_day = min(start_dt.date(), end_day)
    anchor = end_day.isoformat()
    offset = int(cursor.get("date_offset") or 0)
    if str(cursor.get("date_anchor") or "") != anchor:
        offset = 0
    dates_per_run = max(1, min(int(cfg.get("dates_per_run") or 1), 31))
    span_days = max(0, (end_day - start_day).days)
    selected: list[datetime] = []
    for step in range(dates_per_run):
        candidate_offset = offset + step
        if candidate_offset > span_days:
            break
        selected.append(datetime.combine(end_day - timedelta(days=candidate_offset), datetime.min.time()))
    next_offset = offset + len(selected)
    if next_offset > span_days:
        next_offset = 0
    next_cursor = dict(cursor)
    next_cursor["date_anchor"] = anchor
    next_cursor["date_offset"] = next_offset
    next_cursor["date_window_start"] = start_day.isoformat()
    next_cursor["date_window_end"] = end_day.isoformat()
    return selected or [datetime.combine(end_day, datetime.min.time())], next_cursor


def _table(soup: BeautifulSoup):
    return soup.find("table", id="maintable") or soup.select_one("table.list") or soup.find("table")


def _fetch_krx_date(cfg: dict[str, Any], *, query_date: datetime, page_size: int, company_name: str = "") -> str:
    today_str = datetime.now().date().isoformat()
    query_day = query_date.date().isoformat()
    body = {
        "method": "searchTodayDisclosureSub",
        "forward": "todaydisclosure_sub",
        "currentPageSize": str(page_size),
        "pageIndex": str(cfg.get("page_index") or "1"),
        "tabType": str(cfg.get("market_type") or "0").strip() or "0",
        "searchCodeType": "char" if company_name else str(cfg.get("search_code_type") or "").strip(),
        "todayFlag": "Y" if query_day == today_str else "N",
        "startDate": query_day,
        "searchCorpName": company_name,
        "orderMode": str(cfg.get("order_mode") or "1"),
        "orderStat": str(cfg.get("order_stat") or "D"),
        "repIsuSrtCd": str(cfg.get("rep_issuance_code") or "").strip(),
        "nowDate": today_str,
    }
    resp = requests.post(
        f"{KRX_KIND_BASE}/disclosure/todaydisclosure.do",
        headers={**DEFAULT_HEADERS, "Referer": f"{KRX_KIND_BASE}/main.do?method=loadInitPage&scrnmode=1"},
        data=body,
        timeout=float(cfg.get("timeout_s") or 30.0),
    )
    resp.raise_for_status()
    return resp.text or ""


def fetch_krx_kind_disclosure(
    config: dict[str, Any],
    *,
    source_cursor: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = dict(config or {})
    page_size = max(1, min(int(cfg.get("max_items_per_run") or 50), 200))
    cursor = dict(source_cursor or cfg.get("_source_cursor") or {})
    query_dates, next_cursor = _query_dates(cfg, cursor)
    company_names = _list(cfg.get("company_names"))
    if cfg.get("company_name"):
        company_names = [str(cfg.get("company_name") or "").strip()]
    company_names_per_run = max(0, min(int(cfg.get("company_names_per_run") or 0), 20))
    if company_names and company_names_per_run:
        selected_names, next_idx = _rotate(company_names, start=int(cursor.get("company_name_index") or 0), count=company_names_per_run)
        next_cursor["company_name_index"] = next_idx
    else:
        selected_names = []

    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    checked_dates: list[str] = []
    checked_company_names: list[str] = []
    start_dt, end_dt = recent_date_window(cfg, default_days=0)
    exact_company_names = {_norm_text(value) for value in _list(cfg.get("company_names")) if _norm_text(value)}
    company_name_exact_match = bool(cfg.get("company_name_exact_match")) and bool(exact_company_names)
    for query_date in query_dates:
        checked_dates.append(query_date.date().isoformat())
        query_names = selected_names or [""]
        for query_name in query_names:
            if query_name:
                checked_company_names.append(query_name)
            soup = BeautifulSoup(_fetch_krx_date(cfg, query_date=query_date, page_size=page_size, company_name=query_name), "html.parser")
            table = _table(soup)
            if table is None:
                continue
            for row in table.select("tr"):
                cols = row.find_all("td")
                if len(cols) < 4:
                    continue
                first_text = cols[0].get_text(" ", strip=True)
                if "There isn't any information" in first_text:
                    continue
                published_raw = cols[1].get_text(" ", strip=True)
                company_anchor = cols[2].find("a")
                title_anchor = cols[3].find("a")
                company = company_anchor.get_text(" ", strip=True) if company_anchor else cols[2].get_text(" ", strip=True)
                title = title_anchor.get_text(" ", strip=True) if title_anchor else cols[3].get_text(" ", strip=True)
                notes = cols[3].get_text(" ", strip=True)
                if company_name_exact_match:
                    if _norm_text(company) not in exact_company_names:
                        continue
                elif not keyword_allowed(cfg, title, company, notes):
                    continue
                published_at = None
                try:
                    published_at = datetime.strptime(published_raw, "%Y-%m-%d %H:%M")
                except Exception:
                    published_at = None
                if published_at and (published_at < start_dt or published_at > end_dt):
                    continue
                market_icon = cols[2].find("img")
                market = str(market_icon.get("alt") or "").strip() if market_icon else None
                krx_company_id = None
                if company_anchor and company_anchor.get("onclick"):
                    match = re.search(r"companysummary_open\('([^']+)'\)", company_anchor.get("onclick") or "")
                    if match:
                        krx_company_id = match.group(1)
                acpt_no = doc_no = form_procs_cd = None
                if title_anchor and title_anchor.get("href"):
                    match = re.search(r"openDisclsViewer\('([^']*)',\s*'([^']*)',\s*'([^']*)'\)", title_anchor.get("href") or "")
                    if match:
                        acpt_no, doc_no, form_procs_cd = match.groups()
                url = _viewer_url(acpt_no or "", doc_no or "", form_procs_cd or "") if acpt_no else f"{KRX_KIND_BASE}/disclosure/todaydisclosure.do?method=searchTodayDisclosureMain"
                external_id = acpt_no or stable_external_id("krx_kind_disclosure", published_raw, company, title)
                if external_id in seen:
                    continue
                seen.add(external_id)
                mapped_company_id = _mapped(cfg, "company_id_by_krx_company_id", krx_company_id) or _mapped(cfg, "company_id_by_company_name", company)
                mapped_company_slug = _mapped(cfg, "company_slug_by_krx_company_id", krx_company_id) or _mapped(cfg, "company_slug_by_company_name", company)
                mapped_ticker = _mapped(cfg, "ticker_by_krx_company_id", krx_company_id) or _mapped(cfg, "ticker_by_company_name", company)
                normalized = "\n".join(part for part in [company, title, published_raw] if part).strip()
                identifiers = []
                if krx_company_id:
                    identifiers.append(
                        {
                            "id_type": "KRX_KIND",
                            "id_value": krx_company_id,
                            "exchange": "KRX",
                            "source_key": "krx_company_id",
                            "source": "krx_kind_disclosure",
                            "market": market or "KRX",
                            "is_primary": True,
                        }
                    )
                items.append(
                    {
                        "kind": "exchange_disclosure",
                        "external_id": external_id,
                        "url": url,
                        "canonical_url": url,
                        "title": title,
                        "author": company or "KRX KIND",
                        "published_at": published_at,
                        "raw_text": normalized,
                        "normalized_text": normalized,
                        "extra": {
                            "connector": "krx_kind_disclosure",
                            "exchange": "KRX",
                            "market": market,
                            "company_name": company or None,
                            "company_display_name": company or None,
                            "company_id": krx_company_id,
                            "krx_company_id": krx_company_id,
                            "resolved_company_id": mapped_company_id if str(mapped_company_id or "").isdigit() else None,
                            "company_slug": mapped_company_slug or None,
                            "ticker": mapped_ticker or None,
                            "accept_no": acpt_no,
                            "doc_no": doc_no,
                            "form_procs_cd": form_procs_cd,
                            "query_date": query_date.date().isoformat(),
                            "source_identifiers": identifiers,
                        },
                    }
                )
                if len(items) >= page_size:
                    break
            if len(items) >= page_size:
                break
        if len(items) >= page_size:
            break
    return {
        "items": items,
        "cursor": next_cursor,
        "stats": {
            "count": len(items),
            "source_kind": "krx_kind_disclosure",
            "checked_dates": checked_dates,
            "selected_company_names": checked_company_names,
            "date_offset": next_cursor.get("date_offset"),
        },
    }
