from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
import html as html_lib
import re
import time
from typing import Any
from urllib.parse import parse_qsl, unquote, urljoin, urlencode, urlparse, urlunparse

import requests
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from thoth_terminal.db import utcnow
from thoth_terminal.net import fetch_with_fallback
from thoth_terminal.models import (
    Company,
    CompanyDocument,
    Document,
    EarningsEvent,
    IRArtifact,
    Issuer,
    IssuerIdentifier,
    Source,
)
from thoth_terminal.periods import normalize_period_from_text


_TICKER_RE = re.compile(r"^[A-Z0-9.\-]{1,16}$")
_QY_RE = re.compile(r"\bQ([1-4])\s*(?:FY)?\s*([12]\d{3})\b", re.IGNORECASE)
_YQ_RE = re.compile(r"\b([12]\d{3})\s*Q([1-4])\b", re.IGNORECASE)
_FYQ_RE = re.compile(r"\bFY\s*([12]\d{3})[\s_-]*(?:Q([1-4])|([1-4])Q)\b", re.IGNORECASE)
_FY_RE = re.compile(r"\b(?:FY|fiscal\s+year|full\s+year)\s*([12]\d{3})\b", re.IGNORECASE)
_QFY2_RE = re.compile(r"\bQ([1-4])\s*(?:FY)?\s*([0-9]{2})\b", re.IGNORECASE)
_QWORD_Y_RE = re.compile(
    r"\b(first|1st|second|2nd|third|3rd|fourth|4th)[\s-]+quarter\b"
    r"(?:\s+and\s+full\s+year)?(?:\s+of|\s+fiscal|\s+fy)?\s*([12]\d{3}|[0-9]{2})\b",
    re.IGNORECASE,
)
_Y_QWORD_RE = re.compile(
    r"\b([12]\d{3})\s+(first|1st|second|2nd|third|3rd|fourth|4th)[\s-]+quarter\b",
    re.IGNORECASE,
)
_URL_QY_RE = re.compile(
    r"(?:^|[-_/])q([1-4])(?:[-_/]?(?:fy)?)?[-_/]?(20\d{2}|\d{2})(?:[-_/]|$)",
    re.IGNORECASE,
)
_URL_YQ_RE = re.compile(
    r"(?:^|[-_/])(20\d{2}|\d{2})(?:[-_/]?(?:fy)?)?[-_/]?q([1-4])(?:[-_/]|$)",
    re.IGNORECASE,
)
_EARNINGS_MIN_FISCAL_YEAR = 1990
_EARNINGS_MAX_FUTURE_YEARS = 3
_FOOL_TRANSCRIPTS_INDEX_URL = "https://www.fool.com/earnings-call-transcripts/"
_FOOL_LINK_RE = re.compile(
    r"https://www\.fool\.com/earnings/call-transcripts/\d{4}/\d{2}/\d{2}/[^\"'\s<>()]+",
    re.IGNORECASE,
)
_FOOL_REL_LINK_RE = re.compile(
    r"/earnings/call-transcripts/\d{4}/\d{2}/\d{2}/[^\"'\s<>()]+",
    re.IGNORECASE,
)
_FOOL_ARTICLE_PATH_RE = re.compile(
    r"^/earnings/call-transcripts/(?P<yyyy>\d{4})/(?P<mm>\d{2})/(?P<dd>\d{2})/(?P<slug>[^/?#]+)",
    re.IGNORECASE,
)
_FOOL_SLUG_QY_RE = re.compile(
    r"-(?P<ticker>[a-z0-9.\-]{1,16})-q(?P<q>[1-4])-(?P<yy>\d{2,4})-earnings(?:-call)?-transcript/?$",
    re.IGNORECASE,
)
_FOOL_CACHE_TTL_S = 300
_fool_index_cache_html: str | None = None
_fool_index_cache_at: float = 0.0
_WEB_SEARCH_URL = "https://search.yahoo.com/search"
_FOOL_SEARCH_CACHE_TTL_S = 600
_fool_search_cache: dict[str, tuple[float, list[str]]] = {}
_STOCKANALYSIS_TRANSCRIPTS_URL_TEMPLATE = "https://stockanalysis.com/{path}/transcripts/"
_STOCKANALYSIS_CACHE_TTL_S = 300
_stockanalysis_transcript_cache: dict[str, tuple[float, str]] = {}
_STOCKANALYSIS_TRANSCRIPT_LINK_RE = re.compile(
    r'href="(?P<href>/(?P<path>(?:stocks/[a-z0-9.\-]+|quote/[a-z]+/[a-z0-9.\-]+)/transcripts/(?P<slug>[^"/]+)/))"[^>]*>'
    r"(?P<title>[^<]+)</a>\s*<button\b.*?<span[^>]*>(?P<date>[A-Za-z]{3,9}\s+\d{1,2},\s+\d{4})</span>",
    re.IGNORECASE | re.DOTALL,
)
_TSMC_QUARTERLY_URL_TEMPLATE = "https://investor.tsmc.com/english/quarterly-results/{year}/q{quarter}"
_TSMC_TRANSCRIPT_LINK_RE = re.compile(
    r'href="(?P<href>[^"]*TSMC%20(?P<q>[1-4])Q(?P<yy>\d{2})%20Transcript\.pdf)"',
    re.IGNORECASE,
)
_TSMC_EVENT_DATE_RE = re.compile(
    r"Earnings Conference will be held on\s+[A-Za-z]+,\s+([A-Za-z]+ \d{1,2}, \d{4})",
    re.IGNORECASE,
)
_KIOXIA_OFFICIAL_TRANSCRIPT_URLS = (
    "https://www.kioxia-holdings.com/en-jp/ir/library/presentation.html",
    "https://www.kioxia-holdings.com/en-jp/ir/library/presentation/archive.html",
)
_KIOXIA_DATE_PREFIX_RE = re.compile(r"^\s*(\d{1,2})-(\d{1,2})-([12]\d{3})\b")
_KIOXIA_OFFICIAL_CALLOUT_RE = re.compile(
    r"\b(transcript|script|q\s*&\s*a|q&a|qa[\s_-]+summary|q-and-a|questions?\s+and\s+answers?)\b",
    re.IGNORECASE,
)
_OFFICIAL_CALL_MATERIAL_RE = re.compile(
    r"\b("
    r"transcript|q\s*&\s*a|q&a|q-and-a|qa[\s_-]+summary|questions?\s+and\s+answers?|"
    r"earnings\s+call|earnings\s+briefing|financial\s+briefing|financial\s+summary|"
    r"briefing|presentation(?:s| materials)?"
    r")\b|"
    r"(法人(?:說明|说明)會|法說會|法说会|業績(?:發布|發佈)?會|业绩(?:发布)?会|"
    r"投資者(?:說明|说明)?會|投资者(?:说明)?会|簡報|简报|影音|音訊|音频)",
    re.IGNORECASE,
)
_OFFICIAL_CALL_MATERIAL_EXCLUDE_RE = re.compile(
    r"\b("
    r"summary\s+of\s+consolidated\s+financial\s+results|"
    r"consolidated\s+financial\s+results\s+for|"
    r"financial\s+results\s+for\s+the|"
    r"ten\s+years?\s+summary|10[\s_-]*years?[\s_-]*summary|"
    r"fact\s+book|supplementary\s+sheet|"
    r"securities\s+report|annual\s+report|integrated\s+report|dividend|notice\s+of"
    r")\b",
    re.IGNORECASE,
)
_OFFICIAL_CALL_MATERIAL_PAGE_RE = re.compile(
    r"\b("
    r"earnings\s+release|conference\s+call|webcast|audio|financial\s+summary|q\s*&\s*a|q&a|briefing|presentation|"
    r"report|results?|library|events?"
    r")\b|"
    r"(投資人|投资人|投資者|投资者|法人(?:說明|说明)會|法說會|法说会|業績|业绩|簡報|简报|影音|音訊|音频)",
    re.IGNORECASE,
)
_URL_YYYYMMDD_RE = re.compile(r"(?<!\d)(20\d{2})(0[1-9]|1[0-2])([0-2]\d|3[01])(?!\d)")
_URL_DD_MON_YYYY_RE = re.compile(
    r"(?<!\d)([0-3]?\d)[-_ ](Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*[-_ ](20\d{2})(?!\d)",
    re.IGNORECASE,
)
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]*)\]\((\S+?)(?:\s+\"([^\"]*)\")?\)")
_OFFICIAL_ANNUAL_RESULTS_RE = re.compile(
    r"\b(?:FY\s*)?(20\d{2}|[0-9]{2})[\s_-]+(?:annual|full[\s_-]*year)[\s_-]+results?\b|"
    r"\b(?:annual|full[\s_-]*year)[\s_-]+results?[\s_-]+(?:for[\s_-]+)?(?:FY\s*)?(20\d{2}|[0-9]{2})\b",
    re.IGNORECASE,
)
_OFFICIAL_INTERIM_RESULTS_RE = re.compile(
    r"\b(?:FY\s*)?(20\d{2}|[0-9]{2})[\s_-]+(?:interim|half[\s_-]*year|1st[\s_-]*half|first[\s_-]*half)[\s_-]+results?\b|"
    r"\b(?:interim|half[\s_-]*year|1st[\s_-]*half|first[\s_-]*half)[\s_-]+results?[\s_-]+(?:for[\s_-]+)?(?:FY\s*)?(20\d{2}|[0-9]{2})\b",
    re.IGNORECASE,
)
_OFFICIAL_NUMERIC_Q_RE = re.compile(
    r"\b(?:FY\s*)?(20\d{2}|[0-9]{2})[\s_-]*(?:Q([1-4])|([1-4])Q)\b|"
    r"\b(?:Q([1-4])|([1-4])Q)[\s_-]*(?:FY\s*)?(20\d{2}|[0-9]{2})\b",
    re.IGNORECASE,
)
_OFFICIAL_CHINESE_Q_RE = re.compile(
    r"(20\d{2}|[0-9]{2})\s*年\s*(?:第?\s*([一二三四1234])\s*(?:季|季度)|"
    r"(上半年|中期|半年度)|(全年|年度))",
    re.IGNORECASE,
)
_UNREADABLE_TRANSCRIPT_FETCH_STATES = {"too_short", "empty", "error"}
_QWORD_TO_INT = {
    "first": 1,
    "1st": 1,
    "second": 2,
    "2nd": 2,
    "third": 3,
    "3rd": 3,
    "fourth": 4,
    "4th": 4,
}
_CHINESE_Q_TO_INT = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "1": 1,
    "2": 2,
    "3": 3,
    "4": 4,
}
_MONTH_ABBR_TO_INT = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


@dataclass(frozen=True)
class TranscriptRecord:
    ticker: str
    company_or_issuer: str
    period_label: str | None
    fiscal_year: int | None
    fiscal_quarter: int | None
    published_at: datetime | None
    url: str
    source_kind: str
    source_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "company_or_issuer": self.company_or_issuer,
            "period_label": self.period_label,
            "fiscal_year": self.fiscal_year,
            "fiscal_quarter": self.fiscal_quarter,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "url": self.url,
            "source_kind": self.source_kind,
            "source_id": self.source_id,
        }


@dataclass(frozen=True)
class _ProfileCompany:
    id: int
    slug: str | None
    name: str | None
    ticker: str | None
    exchange: str | None
    region: str | None
    ir_url: str | None
    filings_url: str | None
    reports_url: str | None
    calendar_url: str | None
    metadata_json: dict[str, Any] | None = None


@dataclass(frozen=True)
class _ProfileIssuer:
    id: int
    display_name: str | None
    legal_name: str | None
    jurisdiction: str | None
    website: str | None
    ir_website: str | None
    extra: dict[str, Any] | None = None


def normalize_ticker(ticker: str | None) -> str:
    sym = (ticker or "").strip().upper().lstrip("$")
    if not sym:
        return ""
    if not _TICKER_RE.match(sym):
        return ""
    return sym


def _period_label(year: int | None, quarter: int | None) -> str | None:
    if year is None:
        return None
    if quarter is None:
        return f"FY {year}"
    return f"Q{quarter} {year}"


def earnings_fiscal_year_bounds(
    *,
    today: date | None = None,
    max_future_years: int = _EARNINGS_MAX_FUTURE_YEARS,
) -> tuple[int, int]:
    today_v = today or date.today()
    return _EARNINGS_MIN_FISCAL_YEAR, int(today_v.year) + max(0, int(max_future_years))


def is_plausible_earnings_fiscal_year(
    year: int | str | None,
    *,
    today: date | None = None,
    max_future_years: int = _EARNINGS_MAX_FUTURE_YEARS,
) -> bool:
    if year is None:
        return False
    try:
        year_v = int(year)
    except Exception:
        return False
    min_year, max_year = earnings_fiscal_year_bounds(today=today, max_future_years=max_future_years)
    return min_year <= year_v <= max_year


def _normalize_year_token(value: str | int | None) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text.isdigit():
        return None
    if len(text) == 4:
        year = int(text)
        if is_plausible_earnings_fiscal_year(year):
            return year
        return None
    if len(text) == 2:
        n = int(text)
        # Financial transcript references for 2-digit years are typically 20YY.
        year = 2000 + n if n <= 79 else 1900 + n
        return year if is_plausible_earnings_fiscal_year(year) else None
    return None


def _parse_period_from_text(title: str | None, url: str | None) -> tuple[int | None, int | None]:
    blob = " ".join([(title or ""), (url or "")]).strip()
    if not blob:
        return None, None
    match = _FYQ_RE.search(blob)
    if match:
        yr = _normalize_year_token(match.group(1))
        q = match.group(2) or match.group(3)
        if yr is not None and q is not None:
            return yr, int(q)
    match = _QY_RE.search(blob)
    if match:
        yr = _normalize_year_token(match.group(2))
        if yr is not None:
            return yr, int(match.group(1))
    match = _QFY2_RE.search(blob)
    if match:
        yr = _normalize_year_token(match.group(2))
        if yr is not None:
            return yr, int(match.group(1))
    match = _YQ_RE.search(blob)
    if match:
        yr = _normalize_year_token(match.group(1))
        if yr is not None:
            return yr, int(match.group(2))
    match = _QWORD_Y_RE.search(blob)
    if match:
        q = _QWORD_TO_INT.get(match.group(1).strip().lower())
        yr = _normalize_year_token(match.group(2))
        if q is not None and yr is not None:
            return yr, q
    match = _Y_QWORD_RE.search(blob)
    if match:
        yr = _normalize_year_token(match.group(1))
        q = _QWORD_TO_INT.get(match.group(2).strip().lower())
        if q is not None and yr is not None:
            return yr, q
    match = _FY_RE.search(blob)
    if match:
        yr = _normalize_year_token(match.group(1))
        if yr is not None:
            return yr, None
    if url:
        match = _URL_QY_RE.search(url)
        if match:
            yr = _normalize_year_token(match.group(2))
            if yr is not None:
                return yr, int(match.group(1))
        match = _URL_YQ_RE.search(url)
        if match:
            yr = _normalize_year_token(match.group(1))
            if yr is not None:
                return yr, int(match.group(2))
    return None, None


def _normalize_fool_url(url: str) -> str:
    cleaned = (url or "").strip()
    cleaned = cleaned.replace("\\u0026", "&")
    cleaned = cleaned.rstrip("\\")
    while cleaned and cleaned[-1] in {",", ";", ")", "]", "}"}:
        cleaned = cleaned[:-1]
    if cleaned.startswith("/"):
        cleaned = urljoin("https://www.fool.com", cleaned)
    return cleaned


def _extract_fool_links(html: str) -> list[str]:
    links: list[str] = []
    seen: set[str] = set()
    for raw in _FOOL_LINK_RE.findall(html or ""):
        url = _normalize_fool_url(raw)
        if not url or url in seen:
            continue
        seen.add(url)
        links.append(url)
    for raw in _FOOL_REL_LINK_RE.findall(html or ""):
        url = _normalize_fool_url(raw)
        if not url or url in seen:
            continue
        seen.add(url)
        links.append(url)
    return links


def _fool_record_from_url(*, url: str, ticker_hint: str) -> TranscriptRecord | None:
    cleaned = _normalize_fool_url(url)
    if not cleaned:
        return None
    parsed = urlparse(cleaned)
    if not parsed.netloc.endswith("fool.com"):
        return None
    path_match = _FOOL_ARTICLE_PATH_RE.search(parsed.path or "")
    if not path_match:
        return None

    published = None
    try:
        published = datetime(
            int(path_match.group("yyyy")),
            int(path_match.group("mm")),
            int(path_match.group("dd")),
        )
    except Exception:
        published = None

    row_year, row_quarter = _parse_period_from_text(None, cleaned)
    slug = path_match.group("slug") or ""
    ticker_from_slug: str | None = None
    qy_match = _FOOL_SLUG_QY_RE.search(slug)
    if qy_match:
        ticker_candidate = normalize_ticker(qy_match.group("ticker"))
        if ticker_candidate:
            ticker_from_slug = ticker_candidate
        q_v = int(qy_match.group("q"))
        y_v = _normalize_year_token(qy_match.group("yy"))
        if q_v in {1, 2, 3, 4}:
            row_quarter = q_v
        if y_v is not None:
            row_year = y_v

    sym = normalize_ticker(ticker_hint)
    if ticker_from_slug:
        if sym and ticker_from_slug != sym:
            return None
        sym = ticker_from_slug
    if not sym:
        return None

    token = sym.lower().replace(".", "-")
    slug_l = slug.lower()
    if token not in slug_l and f"-{token}-" not in f"-{slug_l}-" and ticker_from_slug is None:
        return None

    return TranscriptRecord(
        ticker=sym,
        company_or_issuer=sym,
        period_label=_period_label(row_year, row_quarter),
        fiscal_year=row_year,
        fiscal_quarter=row_quarter,
        published_at=published,
        url=cleaned,
        source_kind="motley_fool_live",
        source_id=f"fool:{cleaned}",
    )


def _fetch_fool_transcript_rows(
    *,
    ticker: str,
    year: int | None,
    quarter: int | None,
    most_recent: bool,
    limit: int,
    lookback_days: int | None,
) -> list[TranscriptRecord]:
    sym = normalize_ticker(ticker)
    if not sym:
        return []
    html = _get_fool_transcripts_index_html()
    if not html:
        return []

    cutoff_dt = None
    if lookback_days is not None and lookback_days > 0:
        cutoff_dt = utcnow() - timedelta(days=int(lookback_days))

    out: list[TranscriptRecord] = []
    for url in _extract_fool_links(html):
        row = _fool_record_from_url(url=url, ticker_hint=sym)
        if not row:
            continue
        if year is not None and row.fiscal_year != year:
            continue
        if quarter is not None and row.fiscal_quarter != quarter:
            continue
        if cutoff_dt and row.published_at and row.published_at < cutoff_dt:
            continue
        out.append(row)

    out.sort(key=lambda row: row.published_at or datetime.min, reverse=True)
    if most_recent:
        return out[:1]
    return out[: max(1, min(int(limit or 10), 20))]


def _get_fool_transcripts_index_html() -> str:
    global _fool_index_cache_at, _fool_index_cache_html
    now = time.time()
    if _fool_index_cache_html and (now - _fool_index_cache_at) < _FOOL_CACHE_TTL_S:
        return _fool_index_cache_html
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
    }
    try:
        resp = requests.get(_FOOL_TRANSCRIPTS_INDEX_URL, headers=headers, timeout=15)
    except Exception:
        return ""
    if resp.status_code != 200:
        return ""
    html = resp.text or ""
    if html:
        _fool_index_cache_html = html
        _fool_index_cache_at = now
    return html


def _query_web_for_fool_links(*, query: str) -> list[str]:
    now = time.time()
    cached = _fool_search_cache.get(query)
    if cached and (now - float(cached[0])) < _FOOL_SEARCH_CACHE_TTL_S:
        return list(cached[1])

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
    }
    try:
        resp = requests.get(
            _WEB_SEARCH_URL,
            params={"p": query},
            headers=headers,
            timeout=15,
        )
    except Exception:
        return []
    if resp.status_code != 200:
        return []

    links: list[str] = []
    seen: set[str] = set()
    html = resp.text or ""
    for url in _extract_fool_links(html):
        if url in seen:
            continue
        seen.add(url)
        links.append(url)
    for href in re.findall(r'href="([^"]+)"', html, re.IGNORECASE):
        candidate = (href or "").strip()
        if not candidate:
            continue
        if "fool.com/earnings/call-transcripts/" in candidate:
            norm = _normalize_fool_url(candidate)
            if norm and norm not in seen:
                seen.add(norm)
                links.append(norm)
            continue
        if "/RU=" not in candidate:
            continue
        ru_encoded = candidate.split("/RU=", 1)[-1]
        ru_encoded = ru_encoded.split("/RK=", 1)[0]
        ru_encoded = ru_encoded.split("/RS=", 1)[0]
        decoded = unquote(ru_encoded).strip()
        if "fool.com/earnings/call-transcripts/" not in decoded:
            continue
        norm = _normalize_fool_url(decoded)
        if norm and norm not in seen:
            seen.add(norm)
            links.append(norm)
    if links:
        _fool_search_cache[query] = (now, list(links))
    return links


def _fetch_fool_transcript_rows_via_search(
    *,
    ticker: str,
    year: int | None,
    quarter: int | None,
    most_recent: bool,
    limit: int,
    lookback_days: int | None,
) -> list[TranscriptRecord]:
    sym = normalize_ticker(ticker)
    if not sym:
        return []

    cutoff_dt = None
    if lookback_days is not None and lookback_days > 0:
        cutoff_dt = utcnow() - timedelta(days=int(lookback_days))

    queries: list[str] = []
    if year is not None and quarter is not None:
        slug_token = f"-{sym.lower().replace('.', '-')}-q{int(quarter)}-{int(year)}"
        queries.append(f'site:fool.com/earnings/call-transcripts "{slug_token}"')
        queries.append(f"site:fool.com/earnings/call-transcripts {sym} Q{quarter} {year} earnings call transcript")
        queries.append(f"site:fool.com/earnings/call-transcripts {sym} q{quarter} {str(year)[-2:]} transcript")
    elif year is not None:
        slug_token = f"-{sym.lower().replace('.', '-')}-q"
        queries.append(f'site:fool.com/earnings/call-transcripts "{slug_token}" {year}')
        queries.append(f"site:fool.com/earnings/call-transcripts {sym} {year} earnings call transcript")
    else:
        token = sym.lower().replace(".", "-")
        slug_token = f"-{token}-q"
        queries.append(f'site:fool.com/earnings/call-transcripts "{slug_token}"')
        queries.append(f"site:fool.com/earnings/call-transcripts {token} transcript")
        queries.append(f"site:fool.com/earnings/call-transcripts {sym} earnings call transcript")

    out: list[TranscriptRecord] = []
    seen: set[str] = set()
    for query in queries:
        for url in _query_web_for_fool_links(query=query):
            if not url or url in seen:
                continue
            seen.add(url)
            row = _fool_record_from_url(url=url, ticker_hint=sym)
            if not row:
                continue
            if year is not None and row.fiscal_year != year:
                continue
            if quarter is not None and row.fiscal_quarter != quarter:
                continue
            if cutoff_dt and row.published_at and row.published_at < cutoff_dt:
                continue
            out.append(row)
        if most_recent and year is None and quarter is None and out:
            newest = max((r.published_at or datetime.min) for r in out)
            if newest >= (utcnow() - timedelta(days=540)):
                break

    by_url: dict[str, TranscriptRecord] = {}
    for row in out:
        existing = by_url.get(row.url)
        if not existing or (row.published_at or datetime.min) > (existing.published_at or datetime.min):
            by_url[row.url] = row
    ordered = sorted(by_url.values(), key=lambda row: row.published_at or datetime.min, reverse=True)
    if most_recent:
        return ordered[:1]
    return ordered[: max(1, min(int(limit or 10), 20))]


def _stockanalysis_slug_for_ticker(ticker: str) -> str:
    return ticker.lower().replace(".", "-")


def _stockanalysis_paths_for_ticker(ticker: str) -> list[str]:
    sym = normalize_ticker(ticker)
    if not sym:
        return []

    paths: list[str] = []
    raw = sym.upper()
    korea_match = re.fullmatch(r"(?P<code>\d{6})(?:\.K[QS])?", raw)
    if korea_match:
        paths.append(f"quote/krx/{korea_match.group('code').lower()}")
    hk_match = re.fullmatch(r"0*(?P<code>\d{1,5})(?:\.HK)?", raw)
    if hk_match:
        code = int(hk_match.group("code"))
        paths.append(f"quote/hkg/{code:04d}")
        if not raw.endswith(".HK") and code >= 1000:
            paths.append(f"quote/tyo/{code}")
            paths.append(f"quote/tpe/{code}")
    taiwan_match = re.fullmatch(r"(?P<code>\d{4})(?:\.(?:TW|TWO|TPE))", raw)
    if taiwan_match:
        paths.append(f"quote/tpe/{taiwan_match.group('code')}")

    stock_slug = _stockanalysis_slug_for_ticker(sym)
    paths.append(f"stocks/{stock_slug}")

    deduped: list[str] = []
    seen: set[str] = set()
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        deduped.append(path)
    return deduped


def _parse_stockanalysis_date(value: str | None) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _get_stockanalysis_transcripts_html(*, transcript_path: str | None = None, ticker_slug: str | None = None) -> str:
    path = (transcript_path or "").strip().strip("/").lower()
    if not path and ticker_slug:
        path = f"stocks/{str(ticker_slug).strip().lower().strip('/')}"
    if not path:
        return ""
    now = time.time()
    cached = _stockanalysis_transcript_cache.get(path)
    if cached and (now - float(cached[0])) < _STOCKANALYSIS_CACHE_TTL_S:
        return cached[1]

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
    }
    try:
        resp = requests.get(
            _STOCKANALYSIS_TRANSCRIPTS_URL_TEMPLATE.format(path=path),
            headers=headers,
            timeout=15,
        )
    except Exception:
        return ""
    if resp.status_code != 200:
        return ""
    page = resp.text or ""
    if page:
        _stockanalysis_transcript_cache[path] = (now, page)
    return page


def _fetch_stockanalysis_transcript_rows(
    *,
    ticker: str,
    year: int | None,
    quarter: int | None,
    most_recent: bool,
    limit: int,
    lookback_days: int | None,
) -> list[TranscriptRecord]:
    sym = normalize_ticker(ticker)
    if not sym:
        return []
    cutoff_dt = None
    if lookback_days is not None and lookback_days > 0:
        cutoff_dt = utcnow() - timedelta(days=int(lookback_days))

    out: list[TranscriptRecord] = []
    seen: set[str] = set()
    for transcript_path in _stockanalysis_paths_for_ticker(sym):
        page = _get_stockanalysis_transcripts_html(transcript_path=transcript_path)
        if not page:
            continue
        expected_prefix = f"/{transcript_path}/transcripts/"
        for match in _STOCKANALYSIS_TRANSCRIPT_LINK_RE.finditer(page):
            href = html_lib.unescape(match.group("href") or "").strip()
            if not href.lower().startswith(expected_prefix):
                continue
            title = html_lib.unescape(re.sub(r"<[^>]+>", " ", match.group("title") or ""))
            title = re.sub(r"\s+", " ", title).strip()
            if "earnings call" not in title.lower():
                continue
            url = urljoin("https://stockanalysis.com", href)
            if not url or url in seen:
                continue
            seen.add(url)

            published = _parse_stockanalysis_date(match.group("date"))
            row_year, row_quarter = _parse_period_from_text(title, url)
            if year is not None and row_year != year:
                continue
            if quarter is not None and row_quarter != quarter:
                continue
            if cutoff_dt and published and published < cutoff_dt:
                continue
            out.append(
                TranscriptRecord(
                    ticker=sym,
                    company_or_issuer=sym,
                    period_label=_period_label(row_year, row_quarter),
                    fiscal_year=row_year,
                    fiscal_quarter=row_quarter,
                    published_at=published,
                    url=url,
                    source_kind="stockanalysis_live",
                    source_id=f"stockanalysis:{url}",
                )
            )
        if most_recent and out:
            break

    out.sort(key=lambda row: row.published_at or datetime.min, reverse=True)
    if most_recent:
        return out[:1]
    return out[: max(1, min(int(limit or 10), 20))]


def _parse_tsmc_event_date(text: str | None) -> datetime | None:
    match = _TSMC_EVENT_DATE_RE.search(text or "")
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%B %d, %Y")
    except ValueError:
        return None


def _tsmc_periods_to_try(*, year: int | None, quarter: int | None) -> list[tuple[int, int]]:
    if year is not None and quarter is not None:
        return [(int(year), int(quarter))]
    if year is not None:
        return [(int(year), q) for q in (4, 3, 2, 1)]
    current_year = utcnow().year
    return [(yr, q) for yr in (current_year, current_year - 1) for q in (4, 3, 2, 1)]


def _fetch_tsmc_investor_transcript_rows(
    *,
    ticker: str,
    year: int | None,
    quarter: int | None,
    most_recent: bool,
    limit: int,
    lookback_days: int | None,
) -> list[TranscriptRecord]:
    sym = normalize_ticker(ticker)
    if sym not in {"TSM", "2330", "2330.TW"}:
        return []
    if quarter is not None and quarter not in {1, 2, 3, 4}:
        return []

    cutoff_dt = None
    if lookback_days is not None and lookback_days > 0:
        cutoff_dt = utcnow() - timedelta(days=int(lookback_days))

    out: list[TranscriptRecord] = []
    seen: set[str] = set()
    for period_year, period_quarter in _tsmc_periods_to_try(year=year, quarter=quarter):
        page_url = _TSMC_QUARTERLY_URL_TEMPLATE.format(year=period_year, quarter=period_quarter)
        try:
            result = fetch_with_fallback(page_url, timeout_s=25.0)
        except Exception:
            continue
        page = result.text or ""
        if not page:
            continue
        published = _parse_tsmc_event_date(page)
        for match in _TSMC_TRANSCRIPT_LINK_RE.finditer(page):
            row_quarter = int(match.group("q"))
            row_year = _normalize_year_token(match.group("yy"))
            if row_year != period_year or row_quarter != period_quarter:
                continue
            if year is not None and row_year != year:
                continue
            if quarter is not None and row_quarter != quarter:
                continue
            href = html_lib.unescape(match.group("href") or "").strip()
            url = urljoin("https://investor.tsmc.com", href)
            if not url or url in seen:
                continue
            if cutoff_dt and published and published < cutoff_dt:
                continue
            seen.add(url)
            out.append(
                TranscriptRecord(
                    ticker="TSM",
                    company_or_issuer="TSMC",
                    period_label=_period_label(row_year, row_quarter),
                    fiscal_year=row_year,
                    fiscal_quarter=row_quarter,
                    published_at=published,
                    url=url,
                    source_kind="tsmc_investor_transcript",
                    source_id=f"tsmc:{url}",
                )
            )
        if most_recent and out:
            break

    out.sort(key=lambda row: row.published_at or datetime.min, reverse=True)
    if most_recent:
        return out[:1]
    return out[: max(1, min(int(limit or 10), 20))]


class _AnchorCollector(HTMLParser):
    def __init__(self, *, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url
        self.href: str | None = None
        self.text_parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.context_parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_name = tag.lower()
        if tag_name in {"script", "style", "noscript", "svg"}:
            self._skip_depth += 1
            return
        if tag_name != "a":
            return
        attrs_d = {str(key).lower(): value for key, value in attrs}
        href = str(attrs_d.get("href") or "").strip()
        if not href:
            return
        self.href = href
        self.text_parts = []

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        if self.href is not None:
            self.text_parts.append(data)
            return
        text = " ".join(str(data or "").split()).strip()
        if not text:
            return
        if len(text) > 240:
            text = text[:240].rstrip()
        if text in {">", "|", "｜", "-", "–", "—"}:
            return
        if not self.context_parts or self.context_parts[-1] != text:
            self.context_parts.append(text)
            self.context_parts = self.context_parts[-12:]

    def handle_endtag(self, tag: str) -> None:
        tag_name = tag.lower()
        if tag_name in {"script", "style", "noscript", "svg"}:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag_name != "a" or self.href is None:
            return
        label = " ".join("".join(self.text_parts).split())
        if _anchor_label_needs_context(label):
            label = " ".join([*self.context_parts[-6:], label]).strip()
        url = urljoin(self.base_url, html_lib.unescape(self.href).strip())
        if label or url:
            self.links.append((label, url))
        if label and (not self.context_parts or self.context_parts[-1] != label):
            self.context_parts.append(label)
            self.context_parts = self.context_parts[-12:]
        self.href = None
        self.text_parts = []


def _anchor_label_needs_context(label: str | None) -> bool:
    text = " ".join(str(label or "").split()).strip().lower()
    if not text:
        return True
    if text in {
        "transcript",
        "presentation",
        "presentation materials",
        "webcast",
        "audio",
        "pdf",
        "download",
        "download as pdf",
        "view",
        "details",
        "click here",
        "here",
        "下載",
        "下载",
        "影音",
        "影音檔",
        "音訊",
        "音频",
        "簡報",
        "简报",
    }:
        return True
    if _OFFICIAL_CALL_MATERIAL_RE.search(text):
        return False
    if re.fullmatch(r"(download(?:\s+as)?\s*)?(pdf|xlsx?|docx?|zip)(?:\s+[\d.]+\s*[km]b)?", text):
        return True
    if text in {"download as data"}:
        return True
    return False


def _html_anchor_links(html: str, *, base_url: str) -> list[tuple[str, str]]:
    parser = _AnchorCollector(base_url=base_url)
    try:
        parser.feed(html or "")
    except Exception:
        return []
    return parser.links


def _markdown_anchor_links(text: str, *, base_url: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    context_parts: list[str] = []
    for raw_line in str(text or "").splitlines():
        line = " ".join(raw_line.split()).strip()
        if not line:
            continue
        matches = list(_MARKDOWN_LINK_RE.finditer(line))
        for match in matches:
            label = " ".join((match.group(1) or "").split()).strip()
            href = (match.group(2) or "").strip()
            title = " ".join((match.group(3) or "").split()).strip()
            if not href or href.lower().startswith(("javascript:", "mailto:")):
                continue
            label_parts = [*context_parts[-3:], label]
            if title and title.casefold() != label.casefold():
                label_parts.append(title)
            full_label = " ".join(part for part in label_parts if part).strip()
            out.append((full_label, urljoin(base_url, href)))
        clean_context = _MARKDOWN_LINK_RE.sub("", line)
        clean_context = re.sub(r"^[#*\-\s]+", "", clean_context).strip()
        clean_context = re.sub(r"\s+", " ", clean_context)
        if not clean_context:
            continue
        if len(clean_context) > 180:
            clean_context = clean_context[:180].rstrip()
        if clean_context.lower().startswith(("skip to", "image ", "title:", "url source:", "published time:", "markdown content:")):
            continue
        if not context_parts or context_parts[-1] != clean_context:
            context_parts.append(clean_context)
            context_parts = context_parts[-12:]
    return out


def _parse_kioxia_published_at(label: str | None) -> datetime | None:
    match = _KIOXIA_DATE_PREFIX_RE.search(label or "")
    if not match:
        return None
    try:
        return datetime(int(match.group(3)), int(match.group(1)), int(match.group(2)))
    except ValueError:
        return None


def _is_kioxia_official_call_material(label: str | None, url: str | None) -> bool:
    blob = " ".join([label or "", unquote(url or "")]).strip()
    if not blob:
        return False
    if not _KIOXIA_OFFICIAL_CALLOUT_RE.search(blob):
        return False
    host = urlparse(url or "").netloc.lower()
    return host in {"www.kioxia-holdings.com", "kioxia-holdings.com"}


def _fetch_kioxia_ir_call_material_rows(
    *,
    ticker: str,
    year: int | None,
    quarter: int | None,
    most_recent: bool,
    limit: int,
    lookback_days: int | None,
) -> list[TranscriptRecord]:
    sym = normalize_ticker(ticker)
    if sym not in {"285A", "285A.T"}:
        return []
    if quarter is not None and quarter not in {1, 2, 3, 4}:
        return []

    cutoff_dt = None
    if lookback_days is not None and lookback_days > 0:
        cutoff_dt = utcnow() - timedelta(days=int(lookback_days))

    out: list[TranscriptRecord] = []
    seen: set[str] = set()
    for page_url in _KIOXIA_OFFICIAL_TRANSCRIPT_URLS:
        try:
            result = fetch_with_fallback(page_url, timeout_s=25.0)
        except Exception:
            continue
        for label, url in _html_anchor_links(result.text or "", base_url=page_url):
            if url in seen:
                continue
            if not _is_kioxia_official_call_material(label, url):
                continue
            row_year, row_quarter = _parse_period_from_text(label, url)
            if not _period_matches(row_year=row_year, row_quarter=row_quarter, year=year, quarter=quarter):
                continue
            published = _parse_kioxia_published_at(label)
            if cutoff_dt and published and published < cutoff_dt:
                continue
            seen.add(url)
            out.append(
                TranscriptRecord(
                    ticker="285A.T",
                    company_or_issuer="KIOXIA Holdings",
                    period_label=_period_label(row_year, row_quarter),
                    fiscal_year=row_year,
                    fiscal_quarter=row_quarter,
                    published_at=published,
                    url=url,
                    source_kind="kioxia_ir_call_material",
                    source_id=f"kioxia:{url}",
                )
            )
        if most_recent and out:
            break

    out.sort(key=lambda row: row.published_at or datetime.min, reverse=True)
    if most_recent:
        return out[:1]
    return out[: max(1, min(int(limit or 10), 20))]


def _fetch_issuer_official_transcript_rows(
    *,
    ticker: str,
    year: int | None,
    quarter: int | None,
    most_recent: bool,
    limit: int,
    lookback_days: int | None,
) -> list[TranscriptRecord]:
    out: list[TranscriptRecord] = []
    for fetcher in (_fetch_tsmc_investor_transcript_rows, _fetch_kioxia_ir_call_material_rows):
        rows = fetcher(
            ticker=ticker,
            year=year,
            quarter=quarter,
            most_recent=most_recent,
            limit=limit,
            lookback_days=lookback_days,
        )
        if rows:
            out.extend(rows)
            if most_recent:
                break
    out.sort(key=lambda row: row.published_at or datetime.min, reverse=True)
    if most_recent:
        return out[:1]
    return out[: max(1, min(int(limit or 10), 20))]


def _ticker_lookup_symbols(ticker: str) -> list[str]:
    sym = normalize_ticker(ticker)
    if not sym:
        return []
    out = [sym]
    if "." in sym:
        stem = sym.split(".", 1)[0]
        if len(stem) >= 4 or any(ch.isdigit() for ch in stem):
            out.append(stem)
    elif re.fullmatch(r"\d{3}[A-Z]", sym):
        out.append(f"{sym}.T")
    elif sym.isdigit():
        out.append(f"{sym}.HK")
        out.append(f"{sym}.T")
        zfilled = sym.zfill(4)
        if zfilled != sym:
            out.append(zfilled)
            out.append(f"{zfilled}.HK")
    return out


def _companies_for_ticker_lookup(session: Session, *, ticker: str) -> list[Company]:
    symbols = _ticker_lookup_symbols(ticker)
    if not symbols:
        return []
    rows = (
        session.query(Company)
        .filter(func.upper(Company.ticker).in_(symbols))
        .order_by(Company.id.asc())
        .limit(10)
        .all()
    )
    return rows


def _issuer_for_company(session: Session, company: Company) -> Issuer | None:
    if company.issuer_id is None:
        return None
    return session.get(Issuer, int(company.issuer_id))


def _snapshot_profile_company(company: Company) -> _ProfileCompany:
    return _ProfileCompany(
        id=int(company.id),
        slug=str(company.slug or "").strip() or None,
        name=str(company.name or "").strip() or None,
        ticker=str(company.ticker or "").strip() or None,
        exchange=str(company.exchange or "").strip() or None,
        region=str(company.region or "").strip() or None,
        ir_url=str(company.ir_url or "").strip() or None,
        filings_url=str(company.filings_url or "").strip() or None,
        reports_url=str(company.reports_url or "").strip() or None,
        calendar_url=str(company.calendar_url or "").strip() or None,
    )


def _snapshot_profile_issuer(issuer: Issuer | None) -> _ProfileIssuer | None:
    if issuer is None:
        return None
    extra = dict(issuer.extra or {}) if isinstance(issuer.extra, dict) else None
    return _ProfileIssuer(
        id=int(issuer.id),
        display_name=str(issuer.display_name or "").strip() or None,
        legal_name=str(issuer.legal_name or "").strip() or None,
        jurisdiction=str(issuer.jurisdiction or "").strip() or None,
        website=str(issuer.website or "").strip() or None,
        ir_website=str(issuer.ir_website or "").strip() or None,
        extra=extra,
    )


def _source_profile_snapshots(session: Session, *, ticker: str) -> list[tuple[_ProfileCompany, _ProfileIssuer | None]]:
    if not hasattr(session, "query"):
        return []
    try:
        bind = session.get_bind()
    except Exception:
        bind = None
    lookup_session = Session(bind=bind) if bind is not None else session
    close_lookup = lookup_session is not session
    try:
        out: list[tuple[_ProfileCompany, _ProfileIssuer | None]] = []
        for company in _companies_for_ticker_lookup(lookup_session, ticker=ticker):
            issuer = _issuer_for_company(lookup_session, company)
            out.append((_snapshot_profile_company(company), _snapshot_profile_issuer(issuer)))
        return out
    finally:
        if close_lookup:
            lookup_session.close()


def _release_readonly_transaction(session: Session) -> None:
    try:
        if session.in_transaction() and not session.new and not session.dirty and not session.deleted:
            session.rollback()
    except Exception:
        return


def _http_url(value: object, *, base_url: str | None = None) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    url = urljoin(base_url, raw) if base_url else raw
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return url


def _is_generic_exchange_profile_root(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    if host in {"twse.com.tw", "www.twse.com.tw"}:
        return True
    if host.endswith(".twse.com.tw") and (
        "/rwd/" in path
        or "/company/" in path
        or "/exchange/" in path
        or "/mops/" in path
    ):
        return True
    if host in {"mops.twse.com.tw", "mopsov.twse.com.tw", "mopsplus.twse.com.tw"}:
        return True
    if host.endswith(".tpex.org.tw"):
        return True
    return False


def _profile_source_urls(company: Company | _ProfileCompany, issuer: Issuer | _ProfileIssuer | None) -> list[str]:
    values = [
        getattr(company, "reports_url", None),
        getattr(company, "filings_url", None),
        getattr(company, "calendar_url", None),
        getattr(company, "ir_url", None),
        getattr(issuer, "ir_website", None) if issuer is not None else None,
        getattr(issuer, "website", None) if issuer is not None else None,
    ]
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        url = _http_url(value)
        if url and _is_generic_exchange_profile_root(url):
            continue
        key = url.rstrip("/").casefold() if url else ""
        if url and key not in seen:
            seen.add(key)
            out.append(url)
    return out


def _source_config_matches_company(source: Source, company: _ProfileCompany, issuer: _ProfileIssuer | None) -> bool:
    cfg = source.config if isinstance(source.config, dict) else {}
    slug = str(cfg.get("company_slug") or "").strip().casefold()
    slugs = {str(company.slug or "").strip().casefold()} if company.slug else set()
    slugs.update(
        value
        for value in (
            str(getattr(company, "ticker", "") or "").strip().casefold(),
            str(getattr(issuer, "display_name", "") or "").strip().casefold() if issuer is not None else "",
            str(getattr(issuer, "legal_name", "") or "").strip().casefold() if issuer is not None else "",
        )
        if value
    )
    if slug and slug in slugs:
        return True
    slug_values = cfg.get("company_slugs")
    if isinstance(slug_values, list) and slugs.intersection(str(value or "").strip().casefold() for value in slug_values):
        return True
    company_id = cfg.get("company_id")
    try:
        if company_id is not None and int(company_id) == int(company.id):
            return True
    except Exception:
        pass
    issuer_id = cfg.get("issuer_id")
    try:
        if issuer is not None and issuer_id is not None and int(issuer_id) == int(issuer.id):
            return True
    except Exception:
        pass
    ticker = str(cfg.get("ticker") or cfg.get("symbol") or "").strip().upper()
    if ticker and normalize_ticker(ticker) == normalize_ticker(company.ticker):
        return True
    return False


def _source_config_call_material_urls(session: Session, *, company: _ProfileCompany, issuer: _ProfileIssuer | None) -> list[str]:
    if not hasattr(session, "query"):
        return []
    try:
        sources = session.query(Source).order_by(Source.priority.asc(), Source.id.asc()).limit(5000).all()
    except Exception:
        return []

    out: list[str] = []
    seen: set[str] = set()
    url_keys = (
        "call_material_url",
        "call_material_page_url",
        "transcript_url",
        "transcripts_url",
        "presentation_url",
        "presentations_url",
        "list_url",
        "url",
    )
    list_keys = (
        "call_material_urls",
        "call_material_page_urls",
        "transcript_urls",
        "transcripts_urls",
        "presentation_urls",
        "presentations_urls",
        "list_urls",
        "urls",
    )
    for source in sources:
        cfg = source.config if isinstance(source.config, dict) else {}
        category = str(source.category or "").strip().lower()
        company_doc_type = str(cfg.get("company_doc_type") or "").strip().lower()
        connector = str(cfg.get("connector") or "").strip().lower()
        transcript_policy = cfg.get("transcript_gate_policy")
        if (
            category != "transcripts"
            and company_doc_type not in {"earnings_transcript", "transcript"}
            and not isinstance(transcript_policy, dict)
            and "transcript" not in str(source.name or "").lower()
            and "call" not in str(source.name or "").lower()
        ):
            continue
        if connector in {"quartr_api", "marketscreener_scrape"}:
            continue
        if not _source_config_matches_company(source, company, issuer):
            continue
        values: list[Any] = [cfg.get(key) for key in url_keys]
        for key in list_keys:
            raw = cfg.get(key)
            if isinstance(raw, list):
                values.extend(raw)
        for value in values:
            url = _http_url(value)
            key = url.rstrip("/").casefold() if url else ""
            if url and key not in seen:
                seen.add(key)
                out.append(url)
    return out


def _with_year_query(url: str, year: int | None) -> str | None:
    if year is None:
        return None
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    if parsed.path.lower().endswith((".pdf", ".zip", ".xlsx", ".xls", ".doc", ".docx")):
        return None
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key.lower() != "year"]
    query.append(("year", str(int(year))))
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(query), parsed.fragment))


def _profile_pages_to_try(roots: list[str], *, year: int | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for root in roots:
        for candidate in (root, _with_year_query(root, year)):
            if not candidate:
                continue
            key = candidate.rstrip("/").casefold()
            if key in seen:
                continue
            seen.add(key)
            out.append(candidate)
    return out


def _same_host(left: str, right: str) -> bool:
    left_host = urlparse(left).netloc.lower()
    right_host = urlparse(right).netloc.lower()
    return bool(left_host and right_host and left_host == right_host)


def _looks_like_official_call_material(label: str | None, url: str | None) -> bool:
    blob = " ".join([label or "", unquote(url or "")]).strip()
    if not blob:
        return False
    if not _OFFICIAL_CALL_MATERIAL_RE.search(blob):
        return False
    if _OFFICIAL_CALL_MATERIAL_EXCLUDE_RE.search(blob):
        return False
    return True


def _looks_like_official_material_subpage(label: str | None, url: str | None) -> bool:
    blob = " ".join([label or "", unquote(url or "")]).strip()
    if not blob:
        return False
    if _looks_like_official_call_material(label, url):
        return True
    return bool(_OFFICIAL_CALL_MATERIAL_PAGE_RE.search(blob))


def _official_call_material_score(label: str | None, url: str | None) -> int:
    blob = " ".join([label or "", unquote(url or "")]).lower()
    if "transcript" in blob or "script" in blob:
        return 50
    if re.search(r"\b(q\s*&\s*a|q&a|q-and-a|qa[\s_-]+summary|questions?\s+and\s+answers?)\b", blob):
        return 40
    if "briefing" in blob or "earnings call" in blob:
        return 30
    if "presentation" in blob:
        return 20
    if "financial summary" in blob:
        return 10
    return 1


def _parse_published_date_from_label_url(label: str | None, url: str | None) -> datetime | None:
    for text in (label or "", unquote(url or "")):
        match = _KIOXIA_DATE_PREFIX_RE.search(text)
        if match:
            try:
                return datetime(int(match.group(3)), int(match.group(1)), int(match.group(2)))
            except ValueError:
                pass
        match = _URL_YYYYMMDD_RE.search(text)
        if match:
            try:
                return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            except ValueError:
                pass
        match = _URL_DD_MON_YYYY_RE.search(text)
        if match:
            try:
                month = _MONTH_ABBR_TO_INT[str(match.group(2)).strip().lower()[:4].rstrip(".")]
                return datetime(int(match.group(3)), month, int(match.group(1)))
            except Exception:
                pass
    return None


def _two_or_four_digit_year(value: object) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        year = int(text)
    except Exception:
        return None
    if 0 <= year <= 99:
        return 2000 + year
    if 1900 <= year <= 2100:
        return year
    return None


def _official_results_period_fallback(label: str | None, url: str | None) -> tuple[int | None, int | None]:
    blob = " ".join([label or "", unquote(url or "")]).strip()
    if not blob:
        return None, None
    numeric_q = _OFFICIAL_NUMERIC_Q_RE.search(blob)
    if numeric_q:
        year = _two_or_four_digit_year(numeric_q.group(1) or numeric_q.group(6))
        quarter_raw = numeric_q.group(2) or numeric_q.group(3) or numeric_q.group(4) or numeric_q.group(5)
        if year is not None and quarter_raw is not None:
            return year, int(quarter_raw)
    chinese_q = _OFFICIAL_CHINESE_Q_RE.search(blob)
    if chinese_q:
        year = _two_or_four_digit_year(chinese_q.group(1))
        if year is not None:
            if chinese_q.group(2):
                return year, _CHINESE_Q_TO_INT.get(chinese_q.group(2))
            if chinese_q.group(3):
                return year, 2
            if chinese_q.group(4):
                return year, 4
    annual_match = _OFFICIAL_ANNUAL_RESULTS_RE.search(blob)
    if annual_match:
        year = _two_or_four_digit_year(annual_match.group(1) or annual_match.group(2))
        if year is not None:
            return year, 4
    interim_match = _OFFICIAL_INTERIM_RESULTS_RE.search(blob)
    if interim_match:
        year = _two_or_four_digit_year(interim_match.group(1) or interim_match.group(2))
        if year is not None:
            return year, 2
    return None, None


def _official_profile_material_row(
    *,
    label: str,
    url: str,
    ticker: str,
    company: Company | _ProfileCompany,
    issuer: Issuer | _ProfileIssuer | None,
    year: int | None,
    quarter: int | None,
    lookback_days: int | None,
) -> TranscriptRecord | None:
    if not _looks_like_official_call_material(label, url):
        return None
    clean_url = _http_url(url)
    if not clean_url:
        return None
    published = _parse_published_date_from_label_url(label, clean_url)
    cutoff_dt = utcnow() - timedelta(days=int(lookback_days)) if lookback_days is not None and lookback_days > 0 else None
    if cutoff_dt is not None and published is not None and published < cutoff_dt:
        return None
    period = normalize_period_from_text(
        label,
        clean_url,
        published_at=published,
        issuer=issuer,
        company=company,
        metadata={
            "ticker": ticker,
            "exchange": getattr(company, "exchange", None),
            "company_name": getattr(company, "name", None),
        },
        annual_as_q4=True,
    )
    row_year = period.fiscal_year
    row_quarter = period.fiscal_quarter
    if row_year is None and row_quarter is None:
        row_year, row_quarter = _official_results_period_fallback(label, clean_url)
    if row_year is not None or row_quarter is not None:
        if not _period_matches(row_year=row_year, row_quarter=row_quarter, year=year, quarter=quarter):
            return None
    elif quarter is not None:
        return None
    elif year is not None and (published is None or published.year != year):
        return None
    period_label = period.issuer_fiscal_year_label or _period_label(row_year, row_quarter)
    return TranscriptRecord(
        ticker=normalize_ticker(getattr(company, "ticker", None) or ticker) or ticker,
        company_or_issuer=str(getattr(company, "name", None) or getattr(issuer, "display_name", None) or ticker).strip(),
        period_label=period_label,
        fiscal_year=row_year,
        fiscal_quarter=row_quarter,
        published_at=published,
        url=clean_url,
        source_kind="issuer_official_call_material_page",
        source_id=f"issuer-official:{int(company.id)}:{clean_url}",
    )


def _fetch_profile_page_links(page_url: str) -> list[tuple[str, str]]:
    result = fetch_with_fallback(page_url, timeout_s=25.0)
    text = result.text or ""
    links = _html_anchor_links(text, base_url=page_url)
    md_links = _markdown_anchor_links(text, base_url=page_url)
    if not md_links:
        return links
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for label, url in [*links, *md_links]:
        key = (label.casefold(), url.casefold())
        if key in seen:
            continue
        seen.add(key)
        out.append((label, url))
    return out


def _sort_official_call_material_rows(
    rows: list[TranscriptRecord],
    *,
    score_by_url: dict[str, int],
    year: int | None,
    quarter: int | None,
    most_recent: bool,
    limit: int,
) -> list[TranscriptRecord]:
    rows.sort(
        key=lambda row: (
            1 if year is not None and row.fiscal_year == year and (quarter is None or row.fiscal_quarter == quarter) else 0,
            1 if row.fiscal_year is not None else 0,
            score_by_url.get(row.url.casefold(), 0),
            row.published_at or datetime.min,
        ),
        reverse=True,
    )
    if most_recent:
        return rows[:1]
    return rows[: max(1, min(int(limit or 10), 20))]


def _collect_official_call_material_rows_from_roots(
    *,
    roots: list[str],
    ticker: str,
    company: _ProfileCompany,
    issuer: _ProfileIssuer | None,
    year: int | None,
    quarter: int | None,
    most_recent: bool,
    lookback_days: int | None,
    out: list[TranscriptRecord],
    score_by_url: dict[str, int],
    seen_urls: set[str],
    pages_seen: set[str],
) -> None:
    queue: list[str] = []
    queued: set[str] = set()
    for root in roots:
        direct_label = unquote(urlparse(root).path.rsplit("/", 1)[-1] or root)
        direct_row = _official_profile_material_row(
            label=direct_label,
            url=root,
            ticker=ticker,
            company=company,
            issuer=issuer,
            year=year,
            quarter=quarter,
            lookback_days=lookback_days,
        )
        if direct_row is not None:
            key = direct_row.url.casefold()
            if key not in seen_urls:
                seen_urls.add(key)
                score_by_url[key] = _official_call_material_score(direct_label, root)
                out.append(direct_row)
        path = urlparse(root).path.lower()
        if path.endswith((".pdf", ".zip", ".xlsx", ".xls", ".doc", ".docx", ".ppt", ".pptx", ".mp3", ".m4a", ".wav", ".mp4")):
            continue
        for candidate in _profile_pages_to_try([root], year=year):
            key = candidate.rstrip("/").casefold()
            if key in queued:
                continue
            queued.add(key)
            queue.append(candidate)
    pages_checked = 0
    while queue and pages_checked < 8:
        page_url = queue.pop(0)
        page_key = page_url.rstrip("/").casefold()
        if page_key in pages_seen:
            continue
        pages_seen.add(page_key)
        pages_checked += 1
        try:
            links = _fetch_profile_page_links(page_url)
        except Exception:
            continue
        for label, raw_url in links:
            url = _http_url(raw_url, base_url=page_url)
            if not url:
                continue
            if url.rstrip("/").casefold() not in pages_seen and any(_same_host(url, root) for root in roots):
                path = urlparse(url).path.lower()
                if not path.endswith((".pdf", ".zip", ".xlsx", ".xls", ".doc", ".docx")) and _looks_like_official_material_subpage(label, url):
                    queue.append(url)
            row = _official_profile_material_row(
                label=label,
                url=url,
                ticker=ticker,
                company=company,
                issuer=issuer,
                year=year,
                quarter=quarter,
                lookback_days=lookback_days,
            )
            if row is None:
                continue
            key = row.url.casefold()
            if key in seen_urls:
                continue
            seen_urls.add(key)
            score_by_url[key] = _official_call_material_score(label, url)
            out.append(row)
        if most_recent and any(
            candidate.fiscal_year == year and (quarter is None or candidate.fiscal_quarter == quarter)
            for candidate in out
        ):
            break


def _fetch_official_call_material_rows_from_roots(
    session: Session,
    *,
    ticker: str,
    roots: list[str],
    year: int | None,
    quarter: int | None,
    most_recent: bool,
    limit: int,
    lookback_days: int | None,
) -> list[TranscriptRecord]:
    sym = normalize_ticker(ticker)
    if not sym:
        return []
    clean_roots: list[str] = []
    seen_roots: set[str] = set()
    for root in roots:
        url = _http_url(root)
        key = url.rstrip("/").casefold() if url else ""
        if url and key not in seen_roots:
            seen_roots.add(key)
            clean_roots.append(url)
    if not clean_roots:
        return []

    out: list[TranscriptRecord] = []
    score_by_url: dict[str, int] = {}
    seen_urls: set[str] = set()
    pages_seen: set[str] = set()
    for company, issuer in _source_profile_snapshots(session, ticker=sym):
        _collect_official_call_material_rows_from_roots(
            roots=clean_roots,
            ticker=sym,
            company=company,
            issuer=issuer,
            year=year,
            quarter=quarter,
            most_recent=most_recent,
            lookback_days=lookback_days,
            out=out,
            score_by_url=score_by_url,
            seen_urls=seen_urls,
            pages_seen=pages_seen,
        )
    return _sort_official_call_material_rows(
        out,
        score_by_url=score_by_url,
        year=year,
        quarter=quarter,
        most_recent=most_recent,
        limit=limit,
    )


def _fetch_source_profile_call_material_rows(
    session: Session,
    *,
    ticker: str,
    year: int | None,
    quarter: int | None,
    most_recent: bool,
    limit: int,
    lookback_days: int | None,
) -> list[TranscriptRecord]:
    sym = normalize_ticker(ticker)
    if not sym:
        return []
    out: list[TranscriptRecord] = []
    score_by_url: dict[str, int] = {}
    seen_urls: set[str] = set()
    pages_seen: set[str] = set()
    profiles = _source_profile_snapshots(session, ticker=sym)
    for company, issuer in profiles:
        roots = []
        seen_roots: set[str] = set()
        for root in [*_source_config_call_material_urls(session, company=company, issuer=issuer), *_profile_source_urls(company, issuer)]:
            key = root.rstrip("/").casefold()
            if key in seen_roots:
                continue
            seen_roots.add(key)
            roots.append(root)
        _collect_official_call_material_rows_from_roots(
            roots=roots,
            ticker=sym,
            company=company,
            issuer=issuer,
            year=year,
            quarter=quarter,
            most_recent=most_recent,
            lookback_days=lookback_days,
            out=out,
            score_by_url=score_by_url,
            seen_urls=seen_urls,
            pages_seen=pages_seen,
        )
    return _sort_official_call_material_rows(
        out,
        score_by_url=score_by_url,
        year=year,
        quarter=quarter,
        most_recent=most_recent,
        limit=limit,
    )


def _period_matches(
    *,
    row_year: int | None,
    row_quarter: int | None,
    year: int | None,
    quarter: int | None,
) -> bool:
    if year is None and quarter is None:
        return True
    if year is not None and row_year != year:
        return False
    if quarter is not None and row_quarter != quarter:
        return False
    return True


def _ticker_issuer_ids(session: Session, *, ticker: str) -> set[int]:
    rows = (
        session.query(IssuerIdentifier.issuer_id)
        .filter(IssuerIdentifier.id_type == "TICKER")
        .filter(func.upper(IssuerIdentifier.id_value) == ticker)
        .all()
    )
    out = {int(r[0]) for r in rows if r and r[0] is not None}
    company_rows = (
        session.query(Company.issuer_id)
        .filter(func.upper(Company.ticker) == ticker)
        .filter(Company.issuer_id.isnot(None))
        .all()
    )
    out.update(int(r[0]) for r in company_rows if r and r[0] is not None)
    return out


def _ticker_company_ids(session: Session, *, ticker: str) -> set[int]:
    rows = (
        session.query(Company.id)
        .filter(func.upper(Company.ticker) == ticker)
        .all()
    )
    return {int(r[0]) for r in rows if r and r[0] is not None}


def transcript_document_text_len(doc: Document | None) -> int:
    if doc is None:
        return 0
    return len(str(doc.translation_en or doc.normalized_text or doc.raw_text or "").strip())


def document_is_readable_transcript(doc: Document | None) -> bool:
    if doc is None:
        return False
    extra = doc.extra if isinstance(doc.extra, dict) else {}
    fetch_state = str(extra.get("transcript_content_fetch") or "").strip().lower()
    if fetch_state in _UNREADABLE_TRANSCRIPT_FETCH_STATES:
        return False
    return transcript_document_text_len(doc) >= 500


def list_transcripts(
    session: Session,
    *,
    ticker: str,
    most_recent: bool = True,
    year: int | None = None,
    quarter: int | None = None,
    limit: int = 10,
    lookback_days: int | None = None,
    include_unreadable: bool = True,
) -> list[TranscriptRecord]:
    sym = normalize_ticker(ticker)
    if not sym:
        return []
    limit_v = max(1, min(int(limit or 10), 50))
    if most_recent:
        limit_v = 1
    if quarter is not None and quarter not in {1, 2, 3, 4}:
        return []
    if year is not None and (year < 1990 or year > 2100):
        return []

    cutoff_dt = None
    if lookback_days is not None and lookback_days > 0:
        cutoff_dt = utcnow() - timedelta(days=int(lookback_days))

    issuer_ids = _ticker_issuer_ids(session, ticker=sym)
    company_ids = _ticker_company_ids(session, ticker=sym)

    out: list[TranscriptRecord] = []

    if issuer_ids:
        rows = (
            session.query(EarningsEvent, Issuer, IRArtifact)
            .join(Issuer, Issuer.id == EarningsEvent.issuer_id)
            .join(IRArtifact, IRArtifact.id == EarningsEvent.transcript_artifact_id)
            .filter(EarningsEvent.issuer_id.in_(issuer_ids))
            .order_by(IRArtifact.published_at.desc().nullslast(), EarningsEvent.updated_at.desc())
            .limit(max(50, limit_v * 5))
            .all()
        )
        for event, issuer, artifact in rows:
            published = artifact.published_at or event.actual_release_at
            if cutoff_dt and published and published < cutoff_dt:
                continue
            row_year = event.fiscal_year
            row_quarter = event.fiscal_quarter
            if not _period_matches(row_year=row_year, row_quarter=row_quarter, year=year, quarter=quarter):
                continue
            url = (artifact.canonical_url or artifact.download_url or artifact.webcast_url or "").strip()
            if not url:
                continue
            if not include_unreadable:
                doc = session.get(Document, int(artifact.document_id)) if artifact.document_id is not None else None
                if not document_is_readable_transcript(doc):
                    continue
            out.append(
                TranscriptRecord(
                    ticker=sym,
                    company_or_issuer=(issuer.display_name or issuer.legal_name or sym),
                    period_label=_period_label(row_year, row_quarter),
                    fiscal_year=row_year,
                    fiscal_quarter=row_quarter,
                    published_at=published,
                    url=url,
                    source_kind="ir_artifact",
                    source_id=f"artifact:{int(artifact.id)}",
                )
            )

    if company_ids:
        rows = (
            session.query(CompanyDocument, Document, Company)
            .join(Document, Document.id == CompanyDocument.document_id)
            .join(Company, Company.id == CompanyDocument.company_id)
            .filter(CompanyDocument.company_id.in_(company_ids))
            .filter(
                or_(
                    CompanyDocument.doc_type.in_(["earnings_transcript", "transcript"]),
                    func.lower(func.coalesce(Document.title, "")).like("%transcript%"),
                    func.lower(func.coalesce(Document.url, "")).like("%transcript%"),
                    func.lower(func.coalesce(Document.canonical_url, "")).like("%transcript%"),
                )
            )
            .order_by(Document.published_at.desc().nullslast(), Document.ingested_at.desc())
            .limit(max(80, limit_v * 6))
            .all()
        )
        for company_doc, document, company in rows:
            published = document.published_at or document.ingested_at
            if cutoff_dt and published and published < cutoff_dt:
                continue
            row_year, row_quarter = _parse_period_from_text(document.title, document.url or document.canonical_url)
            if not _period_matches(row_year=row_year, row_quarter=row_quarter, year=year, quarter=quarter):
                continue
            url = (document.url or document.canonical_url or "").strip()
            if not url:
                continue
            if not include_unreadable and not document_is_readable_transcript(document):
                continue
            out.append(
                TranscriptRecord(
                    ticker=sym,
                    company_or_issuer=(company.name or sym),
                    period_label=_period_label(row_year, row_quarter),
                    fiscal_year=row_year,
                    fiscal_quarter=row_quarter,
                    published_at=published,
                    url=url,
                    source_kind=f"company_document:{company_doc.doc_type or 'unknown'}",
                    source_id=f"document:{int(document.id)}",
                )
            )

    # Fallback: transcript documents that mention the ticker in title/URL even if company linkage is missing.
    symbol_slug = sym.lower()
    docs = (
        session.query(Document)
        .filter(
            or_(
                func.lower(func.coalesce(Document.title, "")).like("%transcript%"),
                func.lower(func.coalesce(Document.url, "")).like("%transcript%"),
                func.lower(func.coalesce(Document.canonical_url, "")).like("%transcript%"),
            )
        )
        .filter(
            or_(
                func.upper(func.coalesce(Document.title, "")).like(f"%({sym})%"),
                func.upper(func.coalesce(Document.title, "")).like(f"% {sym} %"),
                func.upper(func.coalesce(Document.title, "")).like(f"% {sym}.%"),
                func.lower(func.coalesce(Document.url, "")).like(f"%-{symbol_slug}-%"),
                func.lower(func.coalesce(Document.canonical_url, "")).like(f"%-{symbol_slug}-%"),
            )
        )
        .order_by(Document.published_at.desc().nullslast(), Document.ingested_at.desc())
        .limit(max(80, limit_v * 6))
        .all()
    )
    for document in docs:
        published = document.published_at or document.ingested_at
        if cutoff_dt and published and published < cutoff_dt:
            continue
        row_year, row_quarter = _parse_period_from_text(document.title, document.url or document.canonical_url)
        if not _period_matches(row_year=row_year, row_quarter=row_quarter, year=year, quarter=quarter):
            continue
        url = (document.url or document.canonical_url or "").strip()
        if not url:
            continue
        if not include_unreadable and not document_is_readable_transcript(document):
            continue
        out.append(
            TranscriptRecord(
                ticker=sym,
                company_or_issuer=sym,
                period_label=_period_label(row_year, row_quarter),
                fiscal_year=row_year,
                fiscal_quarter=row_quarter,
                published_at=published,
                url=url,
                source_kind="document_fallback",
                source_id=f"document:{int(document.id)}",
            )
        )

    # Deduplicate by URL and keep newest.
    best_by_url: dict[str, TranscriptRecord] = {}
    for row in out:
        existing = best_by_url.get(row.url)
        if not existing:
            best_by_url[row.url] = row
            continue
        if (row.published_at or datetime.min) > (existing.published_at or datetime.min):
            best_by_url[row.url] = row

    ordered = sorted(
        best_by_url.values(),
        key=lambda row: row.published_at or datetime.min,
        reverse=True,
    )
    return ordered[:limit_v]


def get_transcript(
    session: Session,
    *,
    ticker: str,
    most_recent: bool = True,
    year: int | None = None,
    quarter: int | None = None,
    lookback_days: int | None = None,
    allow_live_fallback: bool = True,
) -> TranscriptRecord | None:
    try:
        rows = list_transcripts(
            session,
            ticker=ticker,
            most_recent=most_recent,
            year=year,
            quarter=quarter,
            limit=1,
            lookback_days=lookback_days,
            include_unreadable=False,
        )
    except Exception:
        rows = []
    if rows:
        return rows[0]
    if not allow_live_fallback:
        return None
    _release_readonly_transaction(session)
    official_rows = _fetch_issuer_official_transcript_rows(
        ticker=ticker,
        year=year,
        quarter=quarter,
        most_recent=most_recent,
        limit=1,
        lookback_days=lookback_days,
    )
    if official_rows:
        return official_rows[0]
    official_rows = _fetch_source_profile_call_material_rows(
        session,
        ticker=ticker,
        year=year,
        quarter=quarter,
        most_recent=most_recent,
        limit=1,
        lookback_days=lookback_days,
    )
    if official_rows:
        return official_rows[0]
    live_rows = _fetch_stockanalysis_transcript_rows(
        ticker=ticker,
        year=year,
        quarter=quarter,
        most_recent=most_recent,
        limit=1,
        lookback_days=lookback_days,
    )
    if live_rows:
        return live_rows[0]
    live_rows = _fetch_fool_transcript_rows(
        ticker=ticker,
        year=year,
        quarter=quarter,
        most_recent=most_recent,
        limit=1,
        lookback_days=lookback_days,
    )
    if live_rows:
        return live_rows[0]
    live_rows = _fetch_fool_transcript_rows_via_search(
        ticker=ticker,
        year=year,
        quarter=quarter,
        most_recent=most_recent,
        limit=1,
        lookback_days=lookback_days,
    )
    if live_rows:
        return live_rows[0]
    return None
