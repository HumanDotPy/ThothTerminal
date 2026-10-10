from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urljoin

import requests
import urllib3
from bs4 import BeautifulSoup

from thoth_terminal.connectors.official_common import keyword_allowed, stable_external_id
from thoth_terminal.connectors.sec_edgar import fetch_rss_filings
from thoth_terminal.net import DEFAULT_HEADERS


IPO_SEC_FORMS = {
    "S-1",
    "S-1/A",
    "F-1",
    "F-1/A",
    "S-11",
    "S-11/A",
    "424B4",
}

SOURCE_URLS = {
    "asx": "https://www.asx.com.au/listings/upcoming-floats-and-listings",
    "euronext": "https://live.euronext.com/en/ipo-showcase/all",
    "jpx": "https://www.jpx.co.jp/english/listing/stocks/new/index.html",
    "krx": "https://kind.krx.co.kr/listinvstg/listingcompany.do",
    "nasdaq": "https://www.nasdaq.com/market-activity/ipos",
    "twse_new": "https://www.twse.com.tw/rwd/en/company/newlisting?response=json",
    "twse_apply": "https://www.twse.com.tw/rwd/en/company/applylisting?response=json",
}


def _clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ")).strip()


def _digits(value: object) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _num(value: object) -> float | None:
    raw = _clean(value)
    if not raw or raw in {"-", "N/A", "TBC"}:
        return None
    match = re.search(r"-?\d[\d,]*(?:\.\d+)?", raw)
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except Exception:
        return None


def _currency(value: object, *, default: str | None = None) -> str | None:
    raw = _clean(value).upper()
    if "AUD" in raw:
        return "AUD"
    if "JPY" in raw:
        return "JPY"
    if "KRW" in raw:
        return "KRW"
    if "TWD" in raw:
        return "TWD"
    if raw.startswith("$") or "US$" in raw or "USD" in raw:
        return "USD"
    return default


def _date_iso(value: object, *, dayfirst: bool = False) -> str | None:
    raw = _clean(value)
    if not raw or raw.upper() in {"-", "N/A", "TBC"}:
        return None
    raw = re.sub(r"\s*##\s*$", "", raw).strip()
    raw = raw.replace("Sept.", "Sep.")
    match = re.search(r"\b\d{4}[/-]\d{1,2}[/-]\d{1,2}\b", raw)
    if match:
        token = match.group(0).replace("/", "-")
        try:
            return datetime.strptime(token, "%Y-%m-%d").date().isoformat()
        except Exception:
            pass
    match = re.search(r"\b\d{1,2}\s+[A-Za-z]+\s+\d{4}\b", raw)
    if match:
        try:
            return datetime.strptime(match.group(0), "%d %B %Y").date().isoformat()
        except Exception:
            pass
    match = re.search(r"\b[A-Za-z]{3,9}\.?\s+\d{1,2},\s+\d{4}\b", raw)
    if match:
        token = match.group(0).replace(".", "")
        for fmt in ("%b %d, %Y", "%B %d, %Y"):
            try:
                return datetime.strptime(token, fmt).date().isoformat()
            except Exception:
                continue
    slash_match = re.search(r"\b\d{1,2}/\d{1,2}/\d{4}\b", raw)
    if slash_match:
        fmt_order = ("%d/%m/%Y", "%m/%d/%Y") if dayfirst else ("%m/%d/%Y", "%d/%m/%Y")
        for fmt in fmt_order:
            try:
                return datetime.strptime(slash_match.group(0), fmt).date().isoformat()
            except Exception:
                continue
    dash_match = re.search(r"\b\d{1,2}-\d{1,2}-\d{4}\b", raw)
    if dash_match:
        fmt_order = ("%d-%m-%Y", "%m-%d-%Y") if dayfirst else ("%m-%d-%Y", "%d-%m-%Y")
        for fmt in fmt_order:
            try:
                return datetime.strptime(dash_match.group(0), fmt).date().isoformat()
            except Exception:
                continue
    return None


def _datetime_from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value[:10])
    except Exception:
        return None


def _parse_atom_datetime(value: object) -> datetime | None:
    raw = _clean(value)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def _parse_sec_title_identity(title: str) -> tuple[str | None, str | None]:
    match = re.search(r"\s-\s(?P<company>.+?)\s*\((?P<cik>\d{1,10})\)", title)
    if not match:
        return None, None
    company = re.sub(r"\s*\([^)]*\)\s*$", "", match.group("company")).strip()
    return company or None, match.group("cik").zfill(10)


def _price_range(value: object) -> tuple[float | None, float | None]:
    raw = _clean(value).replace(",", "")
    match = re.search(r"(?P<lo>\d+(?:\.\d+)?)\s*[-\u2013]\s*(?P<hi>\d+(?:\.\d+)?)", raw)
    if not match:
        return None, None
    try:
        return float(match.group("lo")), float(match.group("hi"))
    except Exception:
        return None, None


def _hkex_date_from_url(url: str) -> str | None:
    match = re.search(r"/(?P<year>20\d{2})/(?P<md>\d{4})/", url)
    if not match:
        return None
    year = match.group("year")
    md = match.group("md")
    return f"{year}-{md[:2]}-{md[2:]}"


def _event_text(event: dict[str, Any]) -> str:
    fields = [
        ("Company", event.get("company_name")),
        ("Ticker", event.get("ticker")),
        ("Exchange", event.get("exchange")),
        ("Market", event.get("market")),
        ("Stage", event.get("stage")),
        ("Listing date", event.get("listing_date")),
        ("Pricing date", event.get("pricing_date")),
        ("Application date", event.get("application_date")),
        ("Offer price", event.get("price_text") or event.get("offer_price")),
        ("Shares offered", event.get("shares_offered")),
        ("Proceeds", event.get("proceeds_amount")),
        ("Underwriters", event.get("underwriters")),
        ("Source", event.get("source_url")),
    ]
    return "\n".join(f"{label}: {value}" for label, value in fields if value not in (None, "", []))


def _item(connector: str, event: dict[str, Any], *, source_row: dict[str, Any] | list[Any] | None = None) -> dict[str, Any]:
    company = _clean(event.get("company_name")) or "IPO candidate"
    stage = _clean(event.get("stage")) or "ipo"
    exchange = _clean(event.get("exchange")) or _clean(event.get("region"))
    event_key = event.get("event_key") or stable_external_id(
        "ipo_event",
        connector,
        exchange,
        event.get("ticker"),
        company,
        stage,
        event.get("listing_date") or event.get("pricing_date") or event.get("application_date") or event.get("announcement_date"),
        event.get("source_url"),
    )
    event["event_key"] = event_key
    title = event.get("title") or f"{company} IPO {stage}"
    if exchange:
        title = f"{title} ({exchange})"
    published_at = (
        _datetime_from_iso(event.get("announcement_date"))
        or _datetime_from_iso(event.get("listing_date"))
        or _datetime_from_iso(event.get("pricing_date"))
        or _datetime_from_iso(event.get("application_date"))
    )
    normalized = _event_text(event)
    extra = {
        "connector": connector,
        "ipo_event": dict(event),
        "source_tier": event.get("source_tier"),
    }
    if source_row is not None:
        extra["source_row"] = source_row
    if event.get("attachments"):
        extra["links"] = list(event.get("attachments") or [])
    source_url = event.get("source_url")
    canonical_url = event.get("canonical_url")
    if not canonical_url and source_url:
        fragment = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(event_key)).strip("-")
        canonical_url = f"{source_url}#thoth-ipo-{fragment}"
    return {
        "kind": "ipo_calendar_event",
        "external_id": event_key,
        "url": source_url,
        "canonical_url": canonical_url,
        "title": title,
        "author": event.get("company_name") or exchange or "IPO calendar",
        "published_at": published_at,
        "raw_text": normalized,
        "normalized_text": normalized,
        "links": list(event.get("attachments") or []),
        "extra": extra,
    }


def _request_text(url: str, *, timeout_s: float, **kwargs: Any) -> str:
    resp = requests.get(url, headers={**DEFAULT_HEADERS, "Accept": "text/html,application/xhtml+xml,*/*"}, timeout=timeout_s, **kwargs)
    resp.raise_for_status()
    return resp.text


def _request_json(url: str, *, timeout_s: float, headers: dict[str, str] | None = None) -> Any:
    merged = {**DEFAULT_HEADERS, "Accept": "application/json,text/plain,*/*"}
    if headers:
        merged.update(headers)
    try:
        resp = requests.get(url, headers=merged, timeout=timeout_s)
    except requests.exceptions.SSLError:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        resp = requests.get(url, headers=merged, timeout=timeout_s, verify=False)
    resp.raise_for_status()
    return resp.json()


def fetch_sec_ipo_filings(config: dict[str, Any]) -> dict[str, Any]:
    cfg = dict(config or {})
    forms_raw = cfg.get("filing_types")
    forms = {str(item).strip().upper() for item in forms_raw if str(item).strip()} if isinstance(forms_raw, list) else set(IPO_SEC_FORMS)
    max_items = max(1, min(int(cfg.get("max_items_per_run") or 100), 500))
    feed_rows = fetch_rss_filings(filing_types=forms, limit=max_items, raise_on_error=True)
    stage_by_form = {
        "424B1": "priced",
        "424B3": "priced",
        "424B4": "priced",
        "EFFECT": "approved",
        "FWP": "marketing",
    }
    items: list[dict[str, Any]] = []
    for row in feed_rows:
        title = _clean(row.get("title")) or "SEC IPO filing"
        form_type = _clean(row.get("form_type")).upper()
        summary = _clean(row.get("summary"))
        if not keyword_allowed(cfg, title, form_type, summary):
            continue
        company_name, cik = _parse_sec_title_identity(title)
        published_at = _parse_atom_datetime(row.get("updated"))
        stage = stage_by_form.get(form_type)
        if stage is None:
            stage = "filed_update" if form_type.endswith("/A") else "filed"
        link = _clean(row.get("link")) or None
        event = {
            "region": "US",
            "exchange": "SEC",
            "stage": stage,
            "status": "active",
            "source_tier": "official_regulatory",
            "confidence": 0.85,
            "company_name": company_name or title,
            "cik": cik,
            "deal_type": "ipo",
            "announcement_date": published_at.date().isoformat() if published_at else None,
            "source_url": link,
            "title": title,
            "payload": {"form_type": form_type, "summary": summary, "updated": row.get("updated")},
        }
        items.append(_item("sec_ipo_filings", event, source_row=dict(row)))
        if len(items) >= max_items:
            break
    return {"items": items, "cursor": None, "stats": {"count": len(items), "source_kind": "sec_ipo_filings"}}


def fetch_nasdaq_ipo_calendar(config: dict[str, Any]) -> dict[str, Any]:
    cfg = dict(config or {})
    max_items = max(1, min(int(cfg.get("max_items_per_run") or 100), 500))
    timeout_s = float(cfg.get("timeout_s") or 30.0)
    months: list[str] = []
    raw_months = cfg.get("months")
    if isinstance(raw_months, list):
        months = [_clean(m) for m in raw_months if _clean(m)]
    if not months:
        now = datetime.now()
        months_ahead = max(0, min(int(cfg.get("months_ahead") or 2), 12))
        month = now.month
        year = now.year
        for offset in range(months_ahead + 1):
            m = month + offset
            y = year + (m - 1) // 12
            mm = ((m - 1) % 12) + 1
            months.append(f"{y:04d}-{mm:02d}")
    headers = {
        "Origin": "https://www.nasdaq.com",
        "Referer": SOURCE_URLS["nasdaq"],
        "User-Agent": DEFAULT_HEADERS.get("User-Agent", "Mozilla/5.0"),
    }
    items: list[dict[str, Any]] = []
    for month in months:
        data = _request_json(f"https://api.nasdaq.com/api/ipo/calendar?date={month}", timeout_s=timeout_s, headers=headers)
        sections = data.get("data") if isinstance(data, dict) else {}
        if not isinstance(sections, dict):
            continue
        for section_name, section_payload in sections.items():
            if section_name in {"month", "year", "totalResults"} or not isinstance(section_payload, dict):
                continue
            rows = section_payload.get("rows") if isinstance(section_payload.get("rows"), list) else []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                company = _clean(row.get("companyName"))
                if not company or not keyword_allowed(cfg, company, row.get("proposedTickerSymbol"), row.get("proposedExchange")):
                    continue
                section = _clean(section_name).lower()
                stage = "filed" if section == "filed" else "expected" if section == "upcoming" else section
                if stage == "withdrawn":
                    status = "withdrawn"
                else:
                    status = "active"
                listing_date = _date_iso(row.get("expectedDate") or row.get("proposedListingDate") or row.get("listingDate"))
                pricing_date = _date_iso(row.get("pricedDate") or row.get("expectedPriceDate"))
                filed_date = _date_iso(row.get("filedDate"))
                withdrawn_date = _date_iso(row.get("withdrawDate"))
                price_text = _clean(row.get("proposedSharePrice"))
                lo, hi = _price_range(price_text)
                proceeds = _num(row.get("dollarValueOfSharesOffered"))
                event = {
                    "region": "US",
                    "exchange": "NASDAQ",
                    "market": _clean(row.get("proposedExchange")) or None,
                    "stage": stage,
                    "status": status,
                    "source_tier": "exchange_calendar_estimated",
                    "confidence": 0.6 if stage == "expected" else 0.72,
                    "company_name": company,
                    "ticker": _clean(row.get("proposedTickerSymbol")) or None,
                    "deal_type": "ipo",
                    "listing_date": listing_date,
                    "pricing_date": pricing_date,
                    "application_date": filed_date,
                    "announcement_date": withdrawn_date or pricing_date or listing_date or filed_date,
                    "price_text": price_text or None,
                    "offer_price": _num(price_text) if lo is None else None,
                    "price_range_low": lo,
                    "price_range_high": hi,
                    "currency": _currency(price_text, default="USD"),
                    "shares_offered": _num(row.get("sharesOffered")),
                    "proceeds_amount": proceeds,
                    "proceeds_currency": _currency(row.get("dollarValueOfSharesOffered"), default="USD") if proceeds is not None else None,
                    "source_url": SOURCE_URLS["nasdaq"],
                    "title": f"{company} IPO {stage}",
                    "payload": {"deal_id": row.get("dealID"), "section": section_name, "source_month": month, "row": row},
                }
                items.append(_item("nasdaq_ipo_calendar", event, source_row=dict(row)))
                if len(items) >= max_items:
                    return {"items": items, "cursor": None, "stats": {"count": len(items), "source_kind": "nasdaq_ipo_calendar"}}
    return {"items": items, "cursor": None, "stats": {"count": len(items), "source_kind": "nasdaq_ipo_calendar"}}


def fetch_asx_ipo_calendar(config: dict[str, Any]) -> dict[str, Any]:
    cfg = dict(config or {})
    max_items = max(1, min(int(cfg.get("max_items_per_run") or 100), 500))
    html = _request_text(str(cfg.get("url") or SOURCE_URLS["asx"]), timeout_s=float(cfg.get("timeout_s") or 30.0))
    soup = BeautifulSoup(html, "html.parser")
    items: list[dict[str, Any]] = []
    for h3 in soup.find_all("h3"):
        heading = _clean(h3.get_text(" ", strip=True))
        if " - " not in heading:
            continue
        table = h3.find_next("table")
        if table is None:
            continue
        company, listing_text = heading.split(" - ", 1)
        company = _clean(company)
        if not company or not keyword_allowed(cfg, company, listing_text):
            continue
        fields: dict[str, str] = {}
        for tr in table.find_all("tr"):
            cells = [_clean(c.get_text(" ", strip=True)) for c in tr.find_all(["th", "td"])]
            if len(cells) >= 2:
                fields[cells[0].lower()] = cells[1]
        if "listing date" not in fields:
            continue
        price_text = fields.get("issue price")
        event = {
            "region": "AU",
            "exchange": "ASX",
            "stage": "expected",
            "status": "active",
            "source_tier": "official_exchange_calendar",
            "confidence": 0.72,
            "company_name": company,
            "ticker": _clean(fields.get("security code")) or None,
            "security_type": _clean(fields.get("issue type")) or None,
            "deal_type": "ipo",
            "listing_date": _date_iso(fields.get("listing date")),
            "offer_close_date": _date_iso(fields.get("expected offer close date")),
            "price_text": price_text,
            "offer_price": _num(price_text),
            "currency": _currency(price_text, default="AUD") if price_text else None,
            "proceeds_amount": _num(fields.get("capital to be raised")),
            "proceeds_currency": _currency(fields.get("capital to be raised"), default="AUD") if fields.get("capital to be raised") else None,
            "underwriters": _clean(fields.get("underwriter")) or None,
            "source_url": SOURCE_URLS["asx"],
            "title": heading,
            "payload": {"fields": fields, "principal_activities": fields.get("principal activities")},
        }
        items.append(_item("asx_ipo_calendar", event, source_row=fields))
        if len(items) >= max_items:
            break
    return {"items": items, "cursor": None, "stats": {"count": len(items), "source_kind": "asx_ipo_calendar"}}


def fetch_euronext_ipo_calendar(config: dict[str, Any]) -> dict[str, Any]:
    cfg = dict(config or {})
    max_items = max(1, min(int(cfg.get("max_items_per_run") or 200), 500))
    html = _request_text(str(cfg.get("url") or SOURCE_URLS["euronext"]), timeout_s=float(cfg.get("timeout_s") or 30.0))
    soup = BeautifulSoup(html, "html.parser")
    items: list[dict[str, Any]] = []
    for tr in soup.find_all("tr"):
        cells = [_clean(c.get_text(" ", strip=True)) for c in tr.find_all(["td", "th"])]
        if len(cells) < 6 or cells[0].lower() == "date":
            continue
        company = cells[1]
        if not company or not keyword_allowed(cfg, company, cells[2], cells[4], cells[5]):
            continue
        listing_date = _date_iso(cells[0], dayfirst=True)
        event = {
            "region": "EU",
            "exchange": "EURONEXT",
            "market": cells[5] or None,
            "stage": "listed",
            "status": "active",
            "source_tier": "official_exchange_calendar",
            "confidence": 0.8,
            "company_name": company,
            "ticker": cells[2] or None,
            "isin": cells[3] or None,
            "deal_type": "ipo",
            "listing_date": listing_date,
            "announcement_date": listing_date,
            "source_url": SOURCE_URLS["euronext"],
            "title": f"{company} IPO listed on {cells[5]}",
            "payload": {"location": cells[4], "row": cells},
        }
        items.append(_item("euronext_ipo_calendar", event, source_row=cells))
        if len(items) >= max_items:
            break
    return {"items": items, "cursor": None, "stats": {"count": len(items), "source_kind": "euronext_ipo_calendar"}}


def fetch_jpx_ipo_calendar(config: dict[str, Any]) -> dict[str, Any]:
    cfg = dict(config or {})
    max_items = max(1, min(int(cfg.get("max_items_per_run") or 100), 500))
    html = _request_text(str(cfg.get("url") or SOURCE_URLS["jpx"]), timeout_s=float(cfg.get("timeout_s") or 30.0))
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    items: list[dict[str, Any]] = []
    if table is None:
        return {"items": items, "cursor": None, "stats": {"count": 0, "source_kind": "jpx_ipo_calendar"}}
    rows = [[_clean(c.get_text(" ", strip=True)) for c in tr.find_all(["td", "th"])] for tr in table.find_all("tr")]
    idx = 0
    while idx < len(rows):
        row = rows[idx]
        if len(row) < 3 or not _date_iso(row[0]):
            idx += 1
            continue
        next_row = rows[idx + 1] if idx + 1 < len(rows) else []
        date_text = row[0]
        approval_match = re.search(r"\(([^)]+)\)", date_text)
        listing_text = date_text.split("(", 1)[0].strip()
        listing_date = _date_iso(listing_text)
        approval_date = _date_iso(approval_match.group(1)) if approval_match else None
        company = re.sub(r"\s+\*+$", "", row[1]).strip()
        company = re.sub(r"\s+\d+$", "", company).strip()
        ticker = row[2]
        if not keyword_allowed(cfg, company, ticker, next_row[0] if next_row else ""):
            idx += 2
            continue
        indicated = row[5] if len(row) > 5 else ""
        lo, hi = _price_range(indicated)
        offering_price = next_row[3] if len(next_row) > 3 else ""
        event = {
            "region": "JP",
            "exchange": "JPX",
            "market": next_row[0] if next_row else None,
            "stage": "approved",
            "status": "active",
            "source_tier": "official_exchange_calendar",
            "confidence": 0.82,
            "company_name": company,
            "ticker": ticker or None,
            "deal_type": "ipo",
            "listing_date": listing_date,
            "approval_date": approval_date,
            "announcement_date": approval_date or listing_date,
            "price_text": offering_price or indicated or None,
            "offer_price": _num(offering_price) if offering_price else None,
            "price_range_low": lo,
            "price_range_high": hi,
            "currency": "JPY" if offering_price or indicated else None,
            "shares_offered": _num(row[6] if len(row) > 6 else None),
            "source_url": SOURCE_URLS["jpx"],
            "title": f"{company} JPX new listing",
            "payload": {"primary_row": row, "secondary_row": next_row},
        }
        items.append(_item("jpx_ipo_calendar", event, source_row={"primary": row, "secondary": next_row}))
        if len(items) >= max_items:
            break
        idx += 2
    return {"items": items, "cursor": None, "stats": {"count": len(items), "source_kind": "jpx_ipo_calendar"}}


def fetch_hkex_ipo_calendar(config: dict[str, Any]) -> dict[str, Any]:
    cfg = dict(config or {})
    max_items = max(1, min(int(cfg.get("max_items_per_run") or 200), 500))
    boards = cfg.get("boards") if isinstance(cfg.get("boards"), list) else ["main-board", "gem"]
    items: list[dict[str, Any]] = []
    for board in boards:
        board_slug = _clean(board) or "main-board"
        url = str(cfg.get("url") or f"https://www2.hkexnews.hk/new-listings/new-listing-information/{board_slug}?sc_lang=en")
        html = _request_text(url, timeout_s=float(cfg.get("timeout_s") or 30.0))
        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table")
        if table is None:
            continue
        for tr in table.find_all("tr"):
            cells = tr.find_all(["td", "th"])
            values = [_clean(c.get_text(" ", strip=True)) for c in cells]
            if len(values) < 2 or values[0].lower() == "stock code":
                continue
            stock_code = values[0]
            company = values[1]
            if not company or not keyword_allowed(cfg, company, stock_code, board_slug):
                continue
            links: list[dict[str, str]] = []
            for idx, cell in enumerate(cells[2:], start=2):
                label = "new_listing_announcement" if idx == 2 else "prospectus" if idx == 3 else "allotment_results"
                for a in cell.find_all("a"):
                    href = _clean(a.get("href"))
                    if href:
                        links.append({"kind": label, "url": urljoin(url, href)})
            if not links:
                continue
            kinds = {link["kind"] for link in links}
            stage = "priced" if "allotment_results" in kinds else "filed" if "prospectus" in kinds else "announced"
            stage_links = [link["url"] for link in links if (stage == "priced" and link["kind"] == "allotment_results") or (stage == "filed" and link["kind"] == "prospectus")]
            source_url = stage_links[0] if stage_links else links[0]["url"]
            dates = [_hkex_date_from_url(link["url"]) for link in links]
            dates = [d for d in dates if d]
            announcement_date = max(dates) if dates else None
            event = {
                "region": "HK",
                "exchange": "HKEX",
                "market": "GEM" if board_slug.lower() == "gem" else "Main Board",
                "stage": stage,
                "status": "active",
                "source_tier": "official_exchange_filing",
                "confidence": 0.78,
                "company_name": company,
                "ticker": stock_code or None,
                "deal_type": "ipo",
                "announcement_date": announcement_date,
                "source_url": source_url,
                "attachments": [link["url"] for link in links],
                "title": f"{company} HKEX new listing information",
                "payload": {"board": board_slug, "links": links, "row": values},
            }
            items.append(_item("hkex_ipo_calendar", event, source_row={"board": board_slug, "row": values, "links": links}))
            if len(items) >= max_items:
                return {"items": items, "cursor": None, "stats": {"count": len(items), "source_kind": "hkex_ipo_calendar"}}
    return {"items": items, "cursor": None, "stats": {"count": len(items), "source_kind": "hkex_ipo_calendar"}}


def fetch_krx_ipo_calendar(config: dict[str, Any]) -> dict[str, Any]:
    cfg = dict(config or {})
    max_items = max(1, min(int(cfg.get("max_items_per_run") or 200), 500))
    now = datetime.now()
    from_date = _clean(cfg.get("from_date"))
    to_date = _clean(cfg.get("to_date"))
    if not from_date:
        if cfg.get("lookback_days") is not None:
            lookback_days = max(0, int(cfg.get("lookback_days") or 0))
            from_date = (now.date() - timedelta(days=lookback_days)).isoformat()
        else:
            from_date = f"{now.year:04d}-01-01"
    if not to_date:
        to_date = f"{now.year:04d}-12-31"
    data = {
        "method": "searchListingTypeSub",
        "currentPageSize": str(max_items),
        "pageIndex": "1",
        "forward": "listingtype_sub",
        "listTypeArrStr": cfg.get("list_type_arr") or "01|",
        "choicTypeArrStr": cfg.get("choice_type_arr") or "01|02|03|04|05|",
        "secuGrpArrStr": cfg.get("security_group_arr") or "ST|FS|MF|SC|RT|IF|DR|",
        "marketType": cfg.get("market_type") or "",
        "country": cfg.get("country") or "",
        "industry": cfg.get("industry") or "",
        "repMajAgntDesignAdvserComp": cfg.get("advisor_company") or "",
        "repMajAgntComp": cfg.get("underwriter") or "",
        "designAdvserComp": cfg.get("designated_advisor") or "",
        "fromDate": from_date,
        "toDate": to_date,
    }
    resp = requests.post(
        str(cfg.get("url") or SOURCE_URLS["krx"]),
        data=data,
        headers={**DEFAULT_HEADERS, "Referer": "https://kind.krx.co.kr/listinvstg/listingcompany.do?method=searchListingTypeMain"},
        timeout=float(cfg.get("timeout_s") or 30.0),
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    items: list[dict[str, Any]] = []
    for tr in soup.find_all("tr"):
        cells = [_clean(c.get_text(" ", strip=True)) for c in tr.find_all(["td", "th"])]
        if len(cells) < 12 or not _date_iso(cells[1]):
            continue
        company = cells[0]
        if not company or not keyword_allowed(cfg, company, cells[4], cells[6], cells[10]):
            continue
        offer_price = _num(cells[8])
        proceeds = _num(cells[9])
        listing_date = _date_iso(cells[1])
        event = {
            "region": "KR",
            "exchange": "KRX",
            "market": _clean(cfg.get("market_type")) or None,
            "stage": "listed",
            "status": "active",
            "source_tier": "official_exchange_calendar",
            "confidence": 0.8,
            "company_name": company,
            "security_type": cells[3] or None,
            "deal_type": "ipo",
            "listing_date": listing_date,
            "announcement_date": listing_date,
            "price_text": cells[8] or None,
            "offer_price": offer_price,
            "currency": "KRW" if offer_price is not None else None,
            "proceeds_amount": proceeds * 1000 if proceeds is not None else None,
            "proceeds_currency": "KRW" if proceeds is not None else None,
            "underwriters": cells[6] or None,
            "source_url": "https://kind.krx.co.kr/listinvstg/listingcompany.do?method=searchListingTypeMain",
            "title": f"{company} KRX listing",
            "payload": {
                "listing_type": cells[2],
                "industry": cells[4],
                "country": cells[5],
                "par_value": cells[7],
                "principal_products": cells[10],
                "initial_listed_shares": cells[11],
                "row": cells,
            },
        }
        items.append(_item("krx_ipo_calendar", event, source_row=cells))
        if len(items) >= max_items:
            break
    return {"items": items, "cursor": None, "stats": {"count": len(items), "source_kind": "krx_ipo_calendar"}}


def fetch_twse_ipo_calendar(config: dict[str, Any]) -> dict[str, Any]:
    cfg = dict(config or {})
    max_items = max(1, min(int(cfg.get("max_items_per_run") or 200), 500))
    endpoints = cfg.get("endpoints") if isinstance(cfg.get("endpoints"), list) else ["newlisting", "applylisting"]
    endpoint_urls = {
        "newlisting": str(cfg.get("newlisting_url") or SOURCE_URLS["twse_new"]),
        "applylisting": str(cfg.get("applylisting_url") or SOURCE_URLS["twse_apply"]),
    }
    items: list[dict[str, Any]] = []
    for endpoint in endpoints:
        key = _clean(endpoint).lower()
        url = endpoint_urls.get(key, endpoint_urls["newlisting"])
        payload = _request_json(url, timeout_s=float(cfg.get("timeout_s") or 30.0))
        if not isinstance(payload, dict):
            continue
        fields = [_clean(f) for f in payload.get("fields") or []]
        data_rows = payload.get("data") if isinstance(payload.get("data"), list) else []
        for values in data_rows:
            if not isinstance(values, list):
                continue
            row = {fields[idx]: values[idx] for idx in range(min(len(fields), len(values)))}
            company = _clean(row.get("Company"))
            code = _clean(row.get("Code"))
            if not company or not keyword_allowed(cfg, company, code, row.get("Underwriter")):
                continue
            listing_date = _date_iso(row.get("Listing Date"))
            application_date = _date_iso(row.get("Application Date"))
            committee_date = _date_iso(row.get("Date of the Listing Review Committee") or row.get("Date of the Listing Review Committee "))
            approval_date = _date_iso(row.get("Date the application approved by the TWSE Board"))
            stage = "listed" if listing_date else "approved" if approval_date or committee_date else "filed"
            price_text = _clean(row.get("Underwriting price"))
            event = {
                "region": "TW",
                "exchange": "TWSE",
                "stage": stage,
                "status": "active",
                "source_tier": "official_exchange_calendar",
                "confidence": 0.82,
                "company_name": company,
                "ticker": code or None,
                "deal_type": "ipo",
                "listing_date": listing_date,
                "application_date": application_date,
                "approval_date": approval_date or committee_date,
                "announcement_date": listing_date or approval_date or committee_date or application_date,
                "price_text": price_text or None,
                "offer_price": _num(price_text),
                "currency": "TWD" if price_text else None,
                "underwriters": _clean(row.get("Underwriter")) or None,
                "source_url": url,
                "title": f"{company} TWSE listing",
                "payload": {"endpoint": key, "row": row, "stat": payload.get("stat"), "title": payload.get("title")},
            }
            items.append(_item("twse_ipo_calendar", event, source_row=row))
            if len(items) >= max_items:
                return {"items": items, "cursor": None, "stats": {"count": len(items), "source_kind": "twse_ipo_calendar"}}
    return {"items": items, "cursor": None, "stats": {"count": len(items), "source_kind": "twse_ipo_calendar"}}
