"""
SEC EDGAR Connector - Rail 1: Statutory Securities Filings

Implements ingestion from SEC EDGAR via:
1. Company submissions JSON API (data.sec.gov)
2. RSS feeds for real-time filings
3. Full-text search for backfill

SEC Fair Access Guidelines:
- Max 10 requests/second
- Identify via User-Agent with contact email
- Respect rate limiting
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from typing import Any
from urllib.parse import urljoin

import requests

from thoth_terminal.config import settings
from thoth_terminal.text import clean_whitespace, extract_text_from_html, extract_text_from_pdf, is_pdf_response

logger = logging.getLogger("thoth.sec_edgar")

# SEC endpoints
SEC_DATA_BASE = "https://data.sec.gov"
SEC_EFTS_BASE = "https://efts.sec.gov"
SEC_WWW_BASE = "https://www.sec.gov"

# Filing types we care about
QUARTERLY_FILINGS = {"10-Q", "10-Q/A"}
ANNUAL_FILINGS = {"10-K", "10-K/A"}
CURRENT_REPORTS = {"8-K", "8-K/A"}  # Often contains earnings releases as Exhibit 99.1
FOREIGN_FILINGS = {"6-K", "6-K/A", "20-F", "20-F/A", "40-F", "40-F/A"}
PROXY_FILINGS = {"DEF 14A", "DEFA14A"}
PROSPECTUS_FILINGS = {"424B2", "424B3", "424B4", "S-1", "S-1/A"}
TERMINATION_FILINGS = {"15-12B", "15-12G", "15-15D", "25-NSE"}

EARNINGS_RELEVANT_FILINGS = QUARTERLY_FILINGS | ANNUAL_FILINGS | CURRENT_REPORTS | FOREIGN_FILINGS
SOURCE_PACK_ANNUAL_FILINGS = ANNUAL_FILINGS | {"20-F", "20-F/A", "40-F", "40-F/A"}
SOURCE_PACK_QUARTERLY_FILINGS = QUARTERLY_FILINGS
_SOURCE_PACK_FORM_PRIORITY = {
    **{form: -1 for form in TERMINATION_FILINGS},
    **{form: 0 for form in SOURCE_PACK_ANNUAL_FILINGS},
    **{form: 1 for form in SOURCE_PACK_QUARTERLY_FILINGS},
    **{"6-K": 2, "6-K/A": 2},
    **{form: 3 for form in CURRENT_REPORTS},
}

# Rate limiting
MIN_REQUEST_INTERVAL = 0.11  # ~9 req/sec to stay under 10/sec
_last_request_time: float = 0.0


def _get_user_agent() -> str:
    """Build compliant User-Agent per SEC guidelines."""
    contact = getattr(settings, "sec_edgar_contact_email", None) or "admin@example.com"
    return f"Thoth-Terminal/1.0 ({contact})"


def _rate_limit() -> None:
    """Ensure we don't exceed SEC rate limits."""
    global _last_request_time
    now = time.monotonic()
    elapsed = now - _last_request_time
    if elapsed < MIN_REQUEST_INTERVAL:
        time.sleep(MIN_REQUEST_INTERVAL - elapsed)
    _last_request_time = time.monotonic()


def _make_request(
    url: str,
    *,
    timeout: float = 30.0,
    params: dict[str, Any] | None = None,
    accept: str = "application/json",
) -> requests.Response:
    """Make rate-limited request to SEC with proper headers."""
    _rate_limit()
    headers = {
        "User-Agent": _get_user_agent(),
        "Accept": accept,
        "Accept-Encoding": "gzip, deflate",
    }
    logger.debug("SEC request: %s", url)
    resp = requests.get(url, headers=headers, params=params, timeout=timeout, allow_redirects=True)
    resp.raise_for_status()
    return resp


@dataclass
class SECFiling:
    """Parsed SEC filing metadata."""
    cik: str
    accession_number: str
    filing_type: str
    filed_date: date
    accepted_at: datetime | None
    primary_document: str | None
    primary_doc_description: str | None
    period_of_report: date | None
    fiscal_year_end: str | None  # MMDD format
    company_name: str
    form_name: str
    is_xbrl: bool
    items: list[str]  # For 8-K: item numbers
    exhibits: dict[str, str]  # exhibit_number -> description
    filing_url: str
    index_url: str


def _parse_cik(cik: str | int) -> str:
    """Normalize CIK to 10-digit zero-padded string."""
    return str(cik).zfill(10)


def _parse_accession(accession: str) -> str:
    """Normalize accession number (with or without dashes)."""
    return accession.replace("-", "")


def _archive_cik_path(cik: str | int) -> str:
    digits = re.sub(r"\D+", "", str(cik or ""))
    if not digits:
        return "0"
    return str(int(digits))


def _archive_base_url(cik: str | int, accession: str) -> str:
    return f"{SEC_WWW_BASE}/Archives/edgar/data/{_archive_cik_path(cik)}/{_parse_accession(accession)}/"


def _accession_to_path(accession: str) -> str:
    """Convert accession number to URL path component."""
    clean = _parse_accession(accession)
    # Format: NNNNNNNNNN-YY-NNNNNN -> NNNNNNNNNN/YYNN/NNNNNN
    return f"{clean[:10]}/{clean[10:12]}-{clean[12:18]}"


def get_company_submissions(cik: str) -> dict[str, Any]:
    """
    Fetch company submissions from data.sec.gov JSON API.
    Returns filing history and company metadata.
    """
    cik_padded = _parse_cik(cik)
    url = f"{SEC_DATA_BASE}/submissions/CIK{cik_padded}.json"
    resp = _make_request(url)
    return resp.json()


def get_company_facts(cik: str) -> dict[str, Any]:
    """
    Fetch XBRL facts for a company (useful for extracting financials).
    """
    cik_padded = _parse_cik(cik)
    url = f"{SEC_DATA_BASE}/api/xbrl/companyfacts/CIK{cik_padded}.json"
    resp = _make_request(url)
    return resp.json()


def get_filing_index(cik: str, accession: str) -> dict[str, Any]:
    """
    Fetch filing index JSON for a specific filing.
    Returns list of documents in the filing.
    """
    url = urljoin(_archive_base_url(cik, accession), "index.json")
    try:
        resp = _make_request(url)
        return resp.json()
    except requests.HTTPError as e:
        if e.response.status_code == 404:
            return {}
        raise


_SEC_ACCESSION_DASH_RE = re.compile(r"\b\d{10}-\d{2}-\d{6}\b")
_SEC_ARCHIVE_PATH_RE = re.compile(
    r"/Archives/edgar/data/(?P<cik>\d+)/(?P<accession>\d{18})",
    flags=re.IGNORECASE,
)


def _metadata_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"none", "null", "nan"}:
        return None
    return text


def sec_filing_identity_from_url(
    url: str | None,
    *,
    extra: dict[str, Any] | None = None,
) -> tuple[str | None, str | None]:
    """Extract CIK/accession from a SEC Archive URL or connector metadata."""
    meta = dict(extra or {})
    cik = _metadata_text(meta.get("cik"))
    accession = _metadata_text(meta.get("accession_number"))
    raw_url = str(url or "").strip()

    if raw_url:
        match = _SEC_ARCHIVE_PATH_RE.search(raw_url)
        if match:
            cik = cik or match.group("cik")
            accession = accession or match.group("accession")
        if not accession:
            dash_match = _SEC_ACCESSION_DASH_RE.search(raw_url)
            if dash_match:
                accession = dash_match.group(0)

    if cik:
        cik = _parse_cik(cik)
    if accession:
        accession = _parse_accession(accession)
        if len(accession) < 18:
            accession = None
    if not cik and accession and len(accession) >= 10:
        cik = _parse_cik(accession[:10])
    return cik, accession


def _filing_item_size(item: dict[str, Any]) -> int:
    raw = str(item.get("size") or "").strip().replace(",", "")
    try:
        return max(0, int(raw))
    except Exception:
        return 0


def _filing_primary_score(item: dict[str, Any]) -> int:
    name = str(item.get("name") or "").strip()
    lower = name.lower()
    if not name:
        return -1
    if lower.endswith((".xml", ".xsd", ".zip", ".json")):
        return -1
    if lower.endswith(("-index.html", "-index.htm", "-headers.html", "-headers.htm")):
        return -1
    if lower in {"filingsummary.xml", "metalinks.json"}:
        return -1
    if re.fullmatch(r"r\d+\.htm[l]?", lower):
        return -1
    if re.search(r"(^|[-_])ex(?:hibit)?[-_\. ]?\d", lower):
        return -1
    if not lower.endswith((".htm", ".html", ".txt")):
        return -1

    score = _filing_item_size(item)
    if lower.endswith((".htm", ".html")):
        score += 10_000_000
    if lower.endswith(".txt"):
        score += 1_000
    return score


def _select_primary_filing_item(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    scored = [(item, _filing_primary_score(item)) for item in items]
    scored = [(item, score) for item, score in scored if score >= 0]
    if not scored:
        return None
    scored.sort(key=lambda row: row[1], reverse=True)
    return scored[0][0]


def fetch_filing_primary_document_text(
    *,
    cik: str,
    accession: str,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """
    Fetch and text-extract the primary filing document from an EDGAR filing index.

    The SEC current-filings Atom feed only carries metadata. This helper resolves
    the archive index to the main HTML/TXT filing body on demand.
    """
    idx = get_filing_index(cik, accession)
    directory = idx.get("directory") if isinstance(idx, dict) else None
    items = directory.get("item", []) if isinstance(directory, dict) else []
    if not isinstance(items, list) or not items:
        return {"ok": False, "error": "SEC filing index contained no items."}

    primary = _select_primary_filing_item([dict(item) for item in items if isinstance(item, dict)])
    if primary is None:
        return {"ok": False, "error": "SEC filing index had no readable primary document candidate."}

    filename = str(primary.get("name") or "").strip()
    url = urljoin(_archive_base_url(cik, accession), filename)
    resp = _make_request(
        url,
        timeout=timeout,
        accept="text/html,text/plain,application/pdf,*/*",
    )
    content_type = resp.headers.get("content-type")
    body = resp.content or b""
    if is_pdf_response(content_type=content_type, url=url, content=body):
        text = extract_text_from_pdf(body)
    else:
        decoded = resp.text or ""
        if filename.lower().endswith((".htm", ".html")) or "html" in str(content_type or "").lower():
            text = extract_text_from_html(decoded, url=url)
        else:
            text = clean_whitespace(decoded)

    text = clean_whitespace(text)
    if not text:
        return {"ok": False, "url": url, "filename": filename, "error": "Primary filing document yielded no text."}
    return {
        "ok": True,
        "url": url,
        "filename": filename,
        "content_type": content_type,
        "text": text,
        "bytes": len(body),
    }


def parse_recent_filings(
    submissions: dict[str, Any],
    *,
    filing_types: set[str] | None = None,
    since_date: date | None = None,
    limit: int = 100,
    prioritize_source_pack_forms: bool = False,
) -> list[SECFiling]:
    """
    Parse company submissions JSON into SECFiling objects.
    """
    if filing_types is None:
        filing_types = EARNINGS_RELEVANT_FILINGS

    filings_data = submissions.get("filings", {}).get("recent", {})
    if not filings_data:
        return []

    company_name = submissions.get("name", "")
    cik = _parse_cik(submissions.get("cik", "0"))
    fiscal_year_end = submissions.get("fiscalYearEnd", None)

    # Get arrays
    accessions = filings_data.get("accessionNumber", [])
    forms = filings_data.get("form", [])
    filing_dates = filings_data.get("filingDate", [])
    primary_docs = filings_data.get("primaryDocument", [])
    primary_descriptions = filings_data.get("primaryDocDescription", [])
    accepted_times = filings_data.get("acceptanceDateTime", [])
    report_dates = filings_data.get("reportDate", [])
    is_xbrl_flags = filings_data.get("isXBRL", [])
    items_list = filings_data.get("items", [])

    results: list[SECFiling] = []
    scan_count = len(accessions) if prioritize_source_pack_forms else min(len(accessions), limit * 2)
    for i in range(scan_count):  # Check more to filter when source-pack anchors are required.
        form = forms[i] if i < len(forms) else ""
        if form not in filing_types:
            continue

        filing_date_str = filing_dates[i] if i < len(filing_dates) else ""
        try:
            filed_date = datetime.strptime(filing_date_str, "%Y-%m-%d").date()
        except ValueError:
            continue

        if since_date and filed_date < since_date:
            continue

        accession = accessions[i] if i < len(accessions) else ""
        acc_clean = _parse_accession(accession)
        acc_dash = f"{acc_clean[:10]}-{acc_clean[10:12]}-{acc_clean[12:]}"

        accepted_at = None
        if i < len(accepted_times) and accepted_times[i]:
            try:
                accepted_at = datetime.fromisoformat(accepted_times[i].replace("Z", "+00:00"))
            except ValueError:
                pass

        period_of_report = None
        if i < len(report_dates) and report_dates[i]:
            try:
                period_of_report = datetime.strptime(report_dates[i], "%Y-%m-%d").date()
            except ValueError:
                pass

        items_raw = items_list[i] if i < len(items_list) else ""
        items = [it.strip() for it in items_raw.split(",") if it.strip()] if items_raw else []

        filing_url = f"{SEC_WWW_BASE}/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type={form}&dateb=&owner=include&count=40"
        index_url = urljoin(_archive_base_url(cik, accession), "index.json")

        results.append(SECFiling(
            cik=cik,
            accession_number=accession,
            filing_type=form,
            filed_date=filed_date,
            accepted_at=accepted_at,
            primary_document=primary_docs[i] if i < len(primary_docs) else None,
            primary_doc_description=primary_descriptions[i] if i < len(primary_descriptions) else None,
            period_of_report=period_of_report,
            fiscal_year_end=fiscal_year_end,
            company_name=company_name,
            form_name=form,
            is_xbrl=bool(is_xbrl_flags[i]) if i < len(is_xbrl_flags) else False,
            items=items,
            exhibits={},  # Populated later if needed
            filing_url=filing_url,
            index_url=index_url,
        ))

        if len(results) >= limit and not prioritize_source_pack_forms:
            break

    if prioritize_source_pack_forms and len(results) > limit:
        results.sort(
            key=lambda row: (
                _SOURCE_PACK_FORM_PRIORITY.get(row.filing_type, 9),
                -row.filed_date.toordinal(),
                row.accession_number,
            )
        )
        results = results[:limit]

    return results


def get_filing_exhibits(cik: str, accession: str) -> dict[str, dict[str, str]]:
    """
    Fetch exhibit metadata for a filing.
    Returns dict of exhibit_number -> {description, url, sequence, type}.
    """
    idx = get_filing_index(cik, accession)
    if not idx:
        return {}

    base_url = _archive_base_url(cik, accession)

    exhibits: dict[str, dict[str, str]] = {}
    directory = idx.get("directory", {})
    items = directory.get("item", [])

    for item in items:
        name = item.get("name", "")
        doc_type = item.get("type", "")
        description = item.get("description", "")

        # Look for exhibit pattern (EX-99.1, EX-10.1, etc.)
        if doc_type.startswith("EX-") or "exhibit" in doc_type.lower():
            exhibit_num = doc_type.replace("EX-", "").strip()
            exhibits[exhibit_num] = {
                "description": description,
                "url": urljoin(base_url, name),
                "filename": name,
                "type": doc_type,
            }

    return exhibits


def is_earnings_release_8k(filing: SECFiling) -> bool:
    """
    Heuristic to determine if an 8-K contains an earnings release.
    Item 2.02 = "Results of Operations and Financial Condition"
    """
    if filing.filing_type not in CURRENT_REPORTS:
        return False
    # Item 2.02 is the standard earnings release item
    return "2.02" in filing.items


def fetch_filings_for_company(
    cik: str,
    *,
    filing_types: set[str] | None = None,
    since_date: date | None = None,
    limit: int = 50,
    prioritize_source_pack_forms: bool = False,
) -> list[SECFiling]:
    """
    Main entry point: fetch recent filings for a company by CIK.
    """
    if since_date is None:
        since_date = date.today() - timedelta(days=365)

    submissions = get_company_submissions(cik)
    return parse_recent_filings(
        submissions,
        filing_types=filing_types,
        since_date=since_date,
        limit=limit,
        prioritize_source_pack_forms=prioritize_source_pack_forms,
    )


def fetch_rss_filings(
    filing_types: set[str] | None = None,
    limit: int = 100,
    *,
    raise_on_error: bool = False,
) -> list[dict[str, Any]]:
    """
    Fetch recent filings from SEC RSS feed.
    Useful for real-time monitoring of all filings.
    """
    if filing_types is None:
        filing_types = EARNINGS_RELEVANT_FILINGS

    url = f"{SEC_WWW_BASE}/cgi-bin/browse-edgar"
    form_queries = sorted(filing_types) if filing_types else [""]

    try:
        from xml.etree import ElementTree as ET

        results: list[dict[str, Any]] = []
        seen_links: set[str] = set()
        for form_query in form_queries:
            params = {
                "action": "getcurrent",
                "type": form_query,
                "company": "",
                "dateb": "",
                "owner": "include",
                "count": str(max(10, min(limit * 3, 500))),
                "output": "atom",
            }
            resp = _make_request(url, params=params)
            root = ET.fromstring(resp.text)

            ns = {"atom": "http://www.w3.org/2005/Atom"}
            entries = root.findall("atom:entry", ns)

            for entry in entries:
                title = entry.findtext("atom:title", "", ns)
                link_el = entry.find("atom:link", ns)
                link = link_el.get("href", "") if link_el is not None else ""
                updated = entry.findtext("atom:updated", "", ns)
                summary = entry.findtext("atom:summary", "", ns)

                # Parse form type from title (format: "FORM_TYPE - Company Name (CIK)")
                form_type = title.split(" - ")[0].strip() if " - " in title else form_query
                if filing_types and form_type not in filing_types:
                    continue
                if link and link in seen_links:
                    continue
                if link:
                    seen_links.add(link)

                results.append({
                    "title": title,
                    "link": link,
                    "updated": updated,
                    "summary": summary,
                    "form_type": form_type,
                })

                if len(results) >= limit:
                    return results

        return results

    except Exception as e:
        logger.warning("Failed to fetch SEC RSS: %s", e)
        if raise_on_error:
            raise
        return []


def search_filings(
    query: str,
    *,
    filing_types: set[str] | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """
    Search EDGAR full-text search (EFTS).
    Useful for finding filings mentioning specific terms.
    """
    url = f"{SEC_EFTS_BASE}/LATEST/search-index"

    # Build query
    q_parts = [query]
    if filing_types:
        form_filter = " OR ".join([f'formType:"{ft}"' for ft in filing_types])
        q_parts.append(f"({form_filter})")

    params: dict[str, Any] = {
        "q": " AND ".join(q_parts),
        "dateRange": "custom",
        "startdt": (date_from or date.today() - timedelta(days=365)).isoformat(),
        "enddt": (date_to or date.today()).isoformat(),
    }

    try:
        resp = _make_request(url, params=params)
        data = resp.json()
        hits = data.get("hits", {}).get("hits", [])
        return hits[:limit]
    except Exception as e:
        logger.warning("EFTS search failed: %s", e)
        return []


# ==============================================================================
# Connector interface for Thoth ingestion
# ==============================================================================


def fetch_sec_edgar(config: dict) -> list[dict] | dict[str, Any]:
    """
    Thoth connector interface for SEC EDGAR.

    Config options:
        ciks: list of CIKs to monitor
        filing_types: list of form types (default: earnings-relevant)
        since_days: lookback window in days (default: 30)
        max_items_per_run: max filings to return (default: 50)
    """
    ciks = config.get("ciks", [])
    filing_types_raw = config.get("filing_types", None)
    filing_types = set(filing_types_raw) if filing_types_raw else EARNINGS_RELEVANT_FILINGS
    since_days = int(config.get("since_days", 30))
    max_items = int(config.get("max_items_per_run", 50))
    include_exhibits = bool(config.get("include_exhibits", True))
    rotate_ciks = bool(config.get("rotate_ciks", False))
    prioritize_source_pack_forms = bool(config.get("prioritize_source_pack_forms", False))
    if prioritize_source_pack_forms:
        max_items_per_cik_raw = config.get("source_pack_max_items_per_cik")
        max_items_per_cik = int(max_items_per_cik_raw) if max_items_per_cik_raw is not None else 6
        max_items_per_cik = max(1, max_items_per_cik)
    else:
        max_items_per_cik = 0
    source_cursor = config.get("_source_cursor") if isinstance(config.get("_source_cursor"), dict) else {}
    normalized_ciks = [_parse_cik(cik) for cik in ciks if str(cik or "").strip()]
    if rotate_ciks and normalized_ciks:
        try:
            cik_offset = int(source_cursor.get("cik_offset") or 0) % len(normalized_ciks)
        except Exception:
            cik_offset = 0
        max_ciks_per_run = int(config.get("max_ciks_per_run") or len(normalized_ciks))
        max_ciks_per_run = max(1, min(max_ciks_per_run, len(normalized_ciks)))
        ciks_to_fetch = (normalized_ciks[cik_offset:] + normalized_ciks[:cik_offset])[:max_ciks_per_run]
    else:
        cik_offset = 0
        ciks_to_fetch = normalized_ciks
    company_slug_by_cik = {
        _parse_cik(cik): str(value).strip()
        for cik, value in (config.get("company_slug_by_cik") or {}).items()
        if str(value or "").strip()
    } if isinstance(config.get("company_slug_by_cik"), dict) else {}
    ticker_by_cik = {
        _parse_cik(cik): str(value).strip().upper()
        for cik, value in (config.get("ticker_by_cik") or {}).items()
        if str(value or "").strip()
    } if isinstance(config.get("ticker_by_cik"), dict) else {}
    company_name_by_cik = {
        _parse_cik(cik): str(value).strip()
        for cik, value in (config.get("company_name_by_cik") or {}).items()
        if str(value or "").strip()
    } if isinstance(config.get("company_name_by_cik"), dict) else {}
    company_id_by_cik: dict[str, int] = {}
    if isinstance(config.get("company_id_by_cik"), dict):
        for cik, value in (config.get("company_id_by_cik") or {}).items():
            try:
                company_id_by_cik[_parse_cik(cik)] = int(value)
            except Exception:
                continue

    since_date = date.today() - timedelta(days=since_days)
    items: list[dict] = []

    ciks_processed = 0
    for cik in ciks_to_fetch:
        if len(items) >= max_items:
            break

        try:
            remaining_items = max_items - len(items)
            per_cik_limit = min(remaining_items, max_items_per_cik) if max_items_per_cik else remaining_items
            filings = fetch_filings_for_company(
                cik,
                filing_types=filing_types,
                since_date=since_date,
                limit=per_cik_limit,
                prioritize_source_pack_forms=prioritize_source_pack_forms,
            )

            for f in filings:
                exhibits = {}
                if include_exhibits and f.filing_type in CURRENT_REPORTS:
                    try:
                        exhibits = get_filing_exhibits(f.cik, f.accession_number)
                    except Exception as e:
                        logger.debug("Failed to fetch exhibits for %s: %s", f.accession_number, e)

                primary_url = None
                if f.primary_document:
                    primary_url = urljoin(_archive_base_url(f.cik, f.accession_number), f.primary_document)

                is_earnings = is_earnings_release_8k(f)

                title = f"{f.company_name} - {f.filing_type}"
                if f.primary_doc_description:
                    title = f"{f.company_name} - {f.filing_type}: {f.primary_doc_description}"
                mapped_company_name = company_name_by_cik.get(_parse_cik(f.cik)) or f.company_name
                extra = {
                    "connector": "sec_edgar",
                    "cik": f.cik,
                    "accession_number": f.accession_number,
                    "filing_type": f.filing_type,
                    "filed_date": f.filed_date.isoformat(),
                    "accepted_at": f.accepted_at.isoformat() if f.accepted_at else None,
                    "period_of_report": f.period_of_report.isoformat() if f.period_of_report else None,
                    "fiscal_year_end": f.fiscal_year_end,
                    "is_xbrl": f.is_xbrl,
                    "items": f.items,
                    "exhibits": exhibits,
                    "is_earnings_release": is_earnings,
                    "primary_document": f.primary_document,
                    "index_url": f.index_url,
                    "company_name": mapped_company_name,
                    "source_identifiers": [{"id_type": "CIK", "id_value": f.cik, "exchange": "SEC"}],
                }
                cik_key = _parse_cik(f.cik)
                if company_slug_by_cik.get(cik_key):
                    extra["company_slug"] = company_slug_by_cik[cik_key]
                if ticker_by_cik.get(cik_key):
                    extra["ticker"] = ticker_by_cik[cik_key]
                    extra["source_identifiers"].append({"id_type": "TICKER", "id_value": ticker_by_cik[cik_key]})
                if company_id_by_cik.get(cik_key) is not None:
                    extra["company_id"] = company_id_by_cik[cik_key]
                    extra["resolved_company_id"] = company_id_by_cik[cik_key]
                    extra["identity_resolution"] = {"source": "sec_edgar_source_config", "confidence": 0.98}

                items.append({
                    "kind": "sec_filing",
                    "external_id": f.accession_number,
                    "url": primary_url or f.filing_url,
                    "canonical_url": f.index_url,
                    "title": title,
                    "author": mapped_company_name,
                    "published_at": datetime.combine(f.filed_date, datetime.min.time()),
                    "raw_text": f"SEC Filing: {f.filing_type}\nCompany: {mapped_company_name}\nCIK: {f.cik}\nFiled: {f.filed_date}\nAccession: {f.accession_number}",
                    "normalized_text": f"SEC {f.filing_type} filing by {mapped_company_name} (CIK {f.cik}) on {f.filed_date}. Accession number: {f.accession_number}.",
                    "extra": extra,
                })

                if len(items) >= max_items:
                    break
            ciks_processed += 1

        except Exception as e:
            logger.error("Failed to fetch filings for CIK %s: %s", cik, e)
            ciks_processed += 1
            continue

    if rotate_ciks and normalized_ciks:
        next_offset = (cik_offset + ciks_processed) % len(normalized_ciks)
        return {
            "items": items,
            "cursor": {"cik_offset": next_offset, "cik_count": len(normalized_ciks)},
            "stats": {
                "count": len(items),
                "cik_count": len(normalized_ciks),
                "ciks_processed": ciks_processed,
                "cik_offset": cik_offset,
                "next_cik_offset": next_offset,
            },
        }
    return items


def probe_sec_edgar(config: dict) -> dict:
    """Probe to verify SEC EDGAR connectivity."""
    cfg = dict(config or {})
    cfg["max_items_per_run"] = 3
    cfg["since_days"] = 7
    items = fetch_sec_edgar(cfg)
    return {"count": len(items), "first_url": items[0].get("url") if items else None}
