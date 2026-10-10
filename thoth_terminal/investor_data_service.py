from __future__ import annotations

import re
import time
import unicodedata
from difflib import SequenceMatcher
from datetime import date, datetime
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from thoth_terminal.connectors.edinet_code_list import fetch_edinet_code_list
from thoth_terminal.dataset_identity import ensure_managed_company_dataset, lock_managed_company_dataset
from thoth_terminal.dataset_service import replace_dataset_draft
from thoth_terminal.config import settings
from thoth_terminal.db import utcnow
from thoth_terminal.institutional_service import sync_structured_fact_to_institutional
from thoth_terminal.localized_names import TAIWAN_COMPANY_EN
from thoth_terminal.models import (
    Company,
    CompanyDocument,
    Document,
    EarningsEvent,
    ExchangeDisclosure,
    IRArtifact,
    Issuer,
    IssuerIdentifier,
    ResearchInstrument,
    ResearchUniverse,
    ResearchUniverseRow,
    SecuritiesFiling,
    Source,
    SourceState,
)
from thoth_terminal.periods import normalize_period_from_text
from thoth_terminal.structured_data_service import (
    DATASET_FAMILY_EARNINGS_ACTUALS,
    DATASET_FAMILY_EARNINGS_FILINGS,
    DATASET_FAMILY_GUIDANCE_ACTUALS,
    materialize_company_fact_datasets,
    upsert_structured_fact,
)
from thoth_terminal.transcripts import is_plausible_earnings_fiscal_year


_ACTUAL_METRICS = {
    "revenue": ("Revenue", "currency"),
    "gross_profit": ("Gross Profit", "currency"),
    "operating_income": ("Operating Income", "currency"),
    "ordinary_income": ("Ordinary Income", "currency"),
    "net_income": ("Net Income", "currency"),
    "eps": ("EPS", "per_share"),
    "diluted_eps": ("Diluted EPS", "per_share"),
    "capex": ("Capex", "currency"),
    "rnd_expenses": ("R&D Expenses", "currency"),
    "cf_operating": ("Operating Cash Flow", "currency"),
    "cf_investing": ("Investing Cash Flow", "currency"),
    "cf_financing": ("Financing Cash Flow", "currency"),
    "total_assets": ("Total Assets", "currency"),
    "net_assets": ("Net Assets", "currency"),
    "equity_ratio": ("Equity Ratio", "%"),
    "equity_ratio_official": ("Equity Ratio", "%"),
    "roe": ("ROE", "%"),
    "roe_official": ("ROE", "%"),
    "operating_margin": ("Operating Margin", "%"),
    "oi_margin": ("Operating Margin", "%"),
    "revenue_yoy": ("Revenue YoY", "%"),
    "oi_yoy": ("Operating Income YoY", "%"),
}

_GUIDANCE_METRICS = {
    "forecast_revenue": ("Revenue Guidance", "currency"),
    "forecast_operating_income": ("Operating Income Guidance", "currency"),
    "forecast_ordinary_income": ("Ordinary Income Guidance", "currency"),
    "forecast_net_income": ("Net Income Guidance", "currency"),
    "forecast_eps": ("EPS Guidance", "per_share"),
    "forecast_dividend_per_share": ("Dividend Guidance", "per_share"),
}

_OPENDART_ACCOUNT_MAP = {
    "revenue": ("revenue", "Revenue", "currency"),
    "sales": ("revenue", "Revenue", "currency"),
    "operating revenue": ("revenue", "Revenue", "currency"),
    "operating income": ("operating_income", "Operating Income", "currency"),
    "profit from operations": ("operating_income", "Operating Income", "currency"),
    "net income": ("net_income", "Net Income", "currency"),
    "profit": ("net_income", "Net Income", "currency"),
    "basic earnings per share": ("eps", "EPS", "per_share"),
    "earnings per share": ("eps", "EPS", "per_share"),
    "total assets": ("total_assets", "Total Assets", "currency"),
    "total liabilities": ("total_liabilities", "Total Liabilities", "currency"),
    "total equity": ("net_assets", "Net Assets", "currency"),
    "cash and cash equivalents": ("cash", "Cash", "currency"),
}
_TAIWAN_MOPS_FINANCIAL_METRIC_MAP = {
    "revenue_ntd_thousand": ("revenue", "Revenue", "currency", 1000.0),
    "gross_profit_ntd_thousand": ("gross_profit", "Gross Profit", "currency", 1000.0),
    "operating_income_ntd_thousand": ("operating_income", "Operating Income", "currency", 1000.0),
    "net_income_ntd_thousand": ("net_income", "Net Income", "currency", 1000.0),
    "eps_ntd": ("eps", "EPS", "per_share", 1.0),
    "total_assets_ntd_thousand": ("total_assets", "Total Assets", "currency", 1000.0),
    "total_liabilities_ntd_thousand": ("total_liabilities", "Total Liabilities", "currency", 1000.0),
    "total_equity_ntd_thousand": ("net_assets", "Net Assets", "currency", 1000.0),
}


def _slugify(value: object, *, default: str = "company") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return slug[:128] or default


def _num(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    raw = str(value or "").strip().replace(",", "")
    if raw in {"", "-", "None", "null", "nan", "NaN"}:
        return None
    try:
        return float(raw)
    except Exception:
        return None


def _date(value: object) -> date | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if "," in raw and "GMT" in raw:
        try:
            return datetime.strptime(raw, "%a, %d %b %Y %H:%M:%S GMT").date()
        except Exception:
            return None
    try:
        return date.fromisoformat(raw[:10])
    except Exception:
        return None


def _period_date(row: dict[str, Any]) -> date | None:
    for key in ("period_date", "fiscal_year_end", "date", "submit_date", "disclosure_date"):
        parsed = _date(row.get(key))
        if parsed:
            return parsed
    fy = row.get("fiscal_year") or row.get("forecast_fiscal_year")
    try:
        return date(int(fy), 12, 31)
    except Exception:
        return None


def _period_label(row: dict[str, Any]) -> str | None:
    fy = row.get("fiscal_year") or row.get("forecast_fiscal_year")
    q = row.get("quarter") or row.get("fiscal_quarter")
    if fy and q:
        return f"FY{fy} Q{q}"
    if fy:
        return f"FY{fy}"
    return str(row.get("period_label") or "").strip() or None


def _sec_companyfacts_period_label(row: dict[str, Any]) -> str | None:
    fy = str(row.get("fy") or row.get("fiscal_year") or "").strip()
    fp = str(row.get("fp") or row.get("fiscal_period") or "").strip().upper()
    if fy and fp and fp not in {"FY", "CY"}:
        return f"FY{fy} {fp}"
    if fy:
        return f"FY{fy}"
    if fp:
        return fp
    return _period_label(row)


def _payload(extra: dict[str, Any]) -> Any:
    return extra.get("payload") if isinstance(extra.get("payload"), (dict, list)) else None


_OFFICIAL_FILING_CONNECTORS = {
    "edinet_disclosures",
    "edinetdb_financials",
    "filings_xbrl_org",
    "opendart_disclosures",
    "opendart_financials",
    "sec_companyfacts",
    "sec_edgar",
    "sec_edgar_rss",
    "sedarplus_search",
    "taiwan_mops_financial_reports",
}
_OFFICIAL_EXCHANGE_DISCLOSURE_CONNECTORS = {
    "asx_announcements",
    "bursa_announcements",
    "cninfo_announcements",
    "hkexnews_title_search",
    "kazakhstan_announcements",
    "krx_kind_disclosure",
    "nse_corporate_announcements",
    "sgx_announcements",
    "taiwan_mops_investor_conferences",
    "taiwan_mops_monthly_revenue",
    "tdnet_company_announcements",
    "tpex_infohub_company",
    "twse_openapi",
}
_OFFICIAL_INVESTOR_CONNECTORS = _OFFICIAL_FILING_CONNECTORS | _OFFICIAL_EXCHANGE_DISCLOSURE_CONNECTORS
_EXCHANGE_CONNECTOR_JURISDICTIONS = {
    "asx_announcements": "AU",
    "bursa_announcements": "MY",
    "cninfo_announcements": "CN",
    "hkexnews_title_search": "HK",
    "kazakhstan_announcements": "KZ",
    "krx_kind_disclosure": "KR",
    "nse_corporate_announcements": "IN",
    "sgx_announcements": "SG",
    "taiwan_mops_financial_reports": "TW",
    "taiwan_mops_investor_conferences": "TW",
    "tdnet_company_announcements": "JP",
    "tpex_infohub_company": "TW",
    "twse_openapi": "TW",
}
_EXCHANGE_CONNECTOR_EXCHANGES = {
    "asx_announcements": "ASX",
    "bursa_announcements": "BURSA",
    "cninfo_announcements": "CNINFO",
    "hkexnews_title_search": "HKEX",
    "kazakhstan_announcements": "AIX_KASE",
    "krx_kind_disclosure": "KRX",
    "nse_corporate_announcements": "NSE",
    "sgx_announcements": "SGX",
    "taiwan_mops_financial_reports": "TWSE/MOPS",
    "taiwan_mops_investor_conferences": "TWSE/MOPS",
    "tdnet_company_announcements": "JPX",
    "tpex_infohub_company": "TPEX",
    "twse_openapi": "TWSE",
}
_SEC_CURRENT_REPORT_FORMS = {"8-K", "8-K/A"}
_SEC_FINANCIAL_REPORT_FORMS = {"10-Q", "10-Q/A", "10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"}
_SEC_COMPANYFACTS_ACTIVE_SOURCE_NAME = "sec_companyfacts_active_universe"
_SEC_EDGAR_ACTIVE_SOURCE_NAME = "sec_edgar_active_universe_filings"
_SEC_COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_SEC_BROWSE_EDGAR_URL = "https://www.sec.gov/cgi-bin/browse-edgar"
_OPENDART_DISCLOSURES_SOURCE_NAME = "opendart_official_filings"
_OPENDART_FINANCIALS_SOURCE_NAME = "opendart_korea_financials"
_KRX_ACTIVE_SOURCE_NAME = "krx_active_universe_disclosures"
_OPENDART_CORP_CODE_URL = "https://opendart.fss.or.kr/api/corpCode.xml"
_TDNET_ACTIVE_SOURCE_NAME = "tdnet_semiconductor_announcements"
_EDINETDB_ACTIVE_SOURCE_NAME = "edinetdb_japan_company_financials"
_EDINET_OFFICIAL_SOURCE_NAME = "edinet_official_filings"
_TAIWAN_MOPS_MONTHLY_SOURCE_NAME = "taiwan_mops_monthly_revenue"
_TAIWAN_MOPS_FINANCIAL_SOURCE_NAME = "taiwan_mops_financial_reports"
_TAIWAN_MOPS_CONFERENCE_SOURCE_NAME = "taiwan_mops_investor_conferences"
_TWSE_MATERIAL_SOURCE_NAME = "twse_material_announcements"
_TPEX_INFOHUB_SOURCE_NAME = "tpex_infohub_company_pages"
_TAIWAN_SOURCE_PROFILE_OVERRIDES: dict[str, dict[str, str]] = {
    "2301": {
        "ir_url": "https://www.liteon.com/en/iroverview/investors-overview",
        "reports_url": "https://www.liteon.com/en/download/investor-conference/index/2026",
        "calendar_url": "https://www.liteon.com/en/investor-relations/recent-events",
        "issuer_website": "https://www.liteon.com",
        "issuer_ir_website": "https://www.liteon.com/en/iroverview/investors-overview",
    },
    "3081": {
        "ir_url": "https://www.lmoc.com.tw/",
        "reports_url": "https://www.lmoc.com.tw/index.php?id=308&index=5&lang=cht&option=module&task=showlist",
        "calendar_url": "https://www.lmoc.com.tw/index.php?id=308&index=5&lang=cht&option=module&task=showlist",
        "issuer_website": "https://www.lmoc.com.tw/",
        "issuer_ir_website": "https://www.lmoc.com.tw/",
    },
}
_HKEXNEWS_ACTIVE_SOURCE_NAME = "hkexnews_active_universe_disclosures"
_HKEX_ACTIVE_STOCK_LIST_URL = "https://www1.hkexnews.hk/ncms/script/eds/activestock_sehk_e.json"
_CNINFO_ACTIVE_SOURCE_NAME = "cninfo_active_universe_announcements"
_FILINGS_XBRL_ORG_ACTIVE_SOURCE_NAME = "filings_xbrl_org_active_universe_esef"
_SEDARPLUS_ACTIVE_SOURCE_NAME = "sedarplus_active_universe_filings"
_ASX_ACTIVE_SOURCE_NAME = "asx_active_universe_announcements"
_BURSA_ACTIVE_SOURCE_NAME = "bursa_active_universe_announcements"
_KAZAKHSTAN_ACTIVE_SOURCE_NAME = "kazakhstan_active_universe_announcements"
_SGX_ACTIVE_SOURCE_NAME = "sgx_active_universe_announcements"
_NSE_ACTIVE_SOURCE_NAME = "nse_active_universe_announcements"
_SEC_ACTIVE_UNIVERSE_FILING_TYPES = [
    "10-Q",
    "10-Q/A",
    "10-K",
    "10-K/A",
    "8-K",
    "8-K/A",
    "6-K",
    "6-K/A",
    "20-F",
    "20-F/A",
    "40-F",
    "40-F/A",
    "15-12B",
    "15-12G",
    "15-15D",
    "25-NSE",
]
_SEC_SOURCE_PACK_MAX_ITEMS_PER_RUN = 800
_SEC_SOURCE_PACK_MAX_CIKS_PER_RUN = 60
_SEC_SOURCE_PACK_MAX_ITEMS_PER_CIK = 6
_SEC_SOURCE_PACK_CURSOR_PLAN_KEY = (
    "source-pack-sec-v5:prioritize-annual-quarterly-termination:since730:ciks60:items800:per_cik6"
)
_SEC_LISTING_EXCHANGES = {
    "AMEX",
    "ARCA",
    "BATS",
    "CBOE",
    "NASDAQ",
    "NYSE",
    "NYSEAMERICAN",
    "NYSEARCA",
    "OTC",
    "OTCMKTS",
}
_SEC_TICKER_LOOKUP_FALLBACK_EXCHANGES = {"MEXI", "VALUE"}
_SEC_ARCHIVE_CIK_RE = re.compile(r"/Archives/edgar/data/(?P<cik>\d{1,10})/", flags=re.IGNORECASE)
_ACTIVE_UNIVERSE_MAPPING_SOURCE_NAMES_BY_CONNECTOR = {
    "sec_edgar": ["sec_edgar_active_universe_filings", "sec_companyfacts_active_universe"],
    "sec_edgar_rss": ["sec_edgar_active_universe_filings", "sec_companyfacts_active_universe"],
    "sec_companyfacts": ["sec_companyfacts_active_universe", "sec_edgar_active_universe_filings"],
    "opendart_disclosures": ["opendart_official_filings", "opendart_korea_financials"],
    "opendart_financials": ["opendart_korea_financials", "opendart_official_filings"],
    "krx_kind_disclosure": ["krx_active_universe_disclosures", "krx_today_disclosures"],
    "tdnet_company_announcements": ["tdnet_semiconductor_announcements"],
    "taiwan_mops_monthly_revenue": [
        "taiwan_mops_monthly_revenue",
        "taiwan_mops_financial_reports",
        "taiwan_mops_investor_conferences",
        "twse_material_announcements",
    ],
    "taiwan_mops_financial_reports": [
        "taiwan_mops_financial_reports",
        "taiwan_mops_monthly_revenue",
        "taiwan_mops_investor_conferences",
        "twse_material_announcements",
    ],
    "taiwan_mops_investor_conferences": [
        "taiwan_mops_investor_conferences",
        "twse_material_announcements",
        "tpex_infohub_company_pages",
        "taiwan_mops_financial_reports",
        "taiwan_mops_monthly_revenue",
    ],
    "tpex_infohub_company": [
        "tpex_infohub_company_pages",
        "taiwan_mops_investor_conferences",
        "twse_material_announcements",
        "taiwan_mops_financial_reports",
        "taiwan_mops_monthly_revenue",
    ],
    "twse_openapi": [
        "twse_material_announcements",
        "taiwan_mops_investor_conferences",
        "tpex_infohub_company_pages",
        "taiwan_mops_financial_reports",
        "taiwan_mops_monthly_revenue",
    ],
    "hkexnews_title_search": ["hkexnews_active_universe_disclosures"],
    "kazakhstan_announcements": ["kazakhstan_active_universe_announcements"],
    "cninfo_announcements": ["cninfo_active_universe_announcements"],
    "asx_announcements": ["asx_active_universe_announcements"],
    "bursa_announcements": ["bursa_active_universe_announcements"],
    "sgx_announcements": ["sgx_active_universe_announcements"],
    "nse_corporate_announcements": ["nse_active_universe_announcements"],
    "filings_xbrl_org": ["filings_xbrl_org_active_universe_esef"],
    "sedarplus_search": ["sedarplus_active_universe_filings"],
}


def _doc_extra(doc: Document) -> dict[str, Any]:
    return doc.extra if isinstance(doc.extra, dict) else {}


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    raw = str(value or "").strip().lower()
    return raw in {"1", "true", "yes", "y"}


def _sec_title_identity(title: str) -> tuple[str | None, str | None]:
    match = re.search(r"\s-\s(?P<company>.+?)\s*\((?P<cik>\d{1,10})\)", title or "")
    if not match:
        return None, None
    company = re.sub(r"\s*\([^)]*\)\s*$", "", match.group("company")).strip()
    return company or None, match.group("cik").zfill(10)


def _normalize_sec_cik(value: object) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    digits = re.sub(r"\D+", "", raw)
    if 1 <= len(digits) <= 10:
        return digits.zfill(10)
    return None


def _normalize_sec_ticker(value: object) -> str:
    raw = str(value or "").strip().upper()
    raw = re.sub(r"^(NASDAQ|NYSE|AMEX|OTC|OTCMKTS|CBOE|BATS)[:\s]+", "", raw)
    raw = raw.split()[0] if raw else ""
    return raw.strip()


def _sec_ticker_candidates(value: object) -> list[str]:
    raw = _normalize_sec_ticker(value)
    if not raw:
        return []
    candidates = [raw]
    if "-" in raw:
        candidates.append(raw.replace("-", "."))
    if "." in raw:
        candidates.append(raw.replace(".", "-"))
    # Provider symbols sometimes append a country suffix. Keep class-share
    # tickers such as BRK.B, but also try the base form as a last resort.
    if raw.count(".") == 1:
        suffix = raw.rsplit(".", 1)[1]
        if suffix in {"US", "N", "O", "A"}:
            candidates.append(raw.rsplit(".", 1)[0])
    seen: set[str] = set()
    out: list[str] = []
    for ticker in candidates:
        if ticker and ticker not in seen:
            seen.add(ticker)
            out.append(ticker)
    return out


def _sec_lookup_name(value: object) -> str:
    text = str(value or "").strip()
    text = re.sub(r"/[A-Z]{2}$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bINFRAS\b", "INFRASTRUCTURE", text, flags=re.IGNORECASE)
    return text


def _sec_lookup_name_score(candidate_name: object, names: list[object]) -> int:
    candidate = _sec_lookup_name(candidate_name)
    cand_compact = _compact_legal_name(candidate)
    cand_core = _core_legal_name(candidate)
    best = 0.0
    exact = False
    core_exact = False
    for raw_name in names:
        name = _sec_lookup_name(raw_name)
        q = _compact_legal_name(name)
        q_core = _core_legal_name(name)
        if not q:
            continue
        ratio = SequenceMatcher(None, q, cand_compact).ratio()
        core_ratio = SequenceMatcher(None, q_core, cand_core).ratio() if q_core and cand_core else 0.0
        best = max(best, ratio, core_ratio)
        exact = exact or bool(q and q == cand_compact)
        core_exact = core_exact or bool(q_core and cand_core and q_core == cand_core)
    score = int(best * 70)
    if exact:
        score += 30
    elif core_exact:
        score += 25
    return score


def _normalize_exchange_code(value: object) -> str:
    return re.sub(r"[^A-Z0-9]+", "", str(value or "").strip().upper())


def _is_usish_country(value: object) -> bool:
    raw = str(value or "").strip().upper()
    return raw in {"US", "USA", "UNITED STATES", "UNITED STATES OF AMERICA"}


def _is_sec_ticker_lookup_fallback_candidate(
    instrument: ResearchInstrument,
    company: Company | None,
) -> bool:
    exchange = _normalize_exchange_code(getattr(instrument, "exchange", None) or getattr(company, "exchange", None))
    ticker = _normalize_sec_ticker(getattr(instrument, "ticker", None) or getattr(company, "ticker", None))
    if not ticker or "." in ticker or not re.fullmatch(r"[A-Z][A-Z0-9-]{0,9}", ticker):
        return False
    if _is_esef_wrapper_or_fund_candidate(instrument, exchange):
        return False
    if exchange in _SEC_TICKER_LOOKUP_FALLBACK_EXCHANGES:
        return True
    return not exchange and bool(re.fullmatch(r"[A-Z]{1,4}", ticker))


def fetch_sec_company_ticker_map() -> dict[str, dict[str, str]]:
    """Fetch the SEC ticker-to-CIK map used to attach active-universe tickers."""
    from thoth_terminal.connectors.sec_edgar import _make_request

    resp = _make_request(_SEC_COMPANY_TICKERS_URL)
    payload = resp.json()
    rows = payload.values() if isinstance(payload, dict) else payload if isinstance(payload, list) else []
    out: dict[str, dict[str, str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        ticker = _normalize_sec_ticker(row.get("ticker"))
        cik = _normalize_sec_cik(row.get("cik_str") or row.get("cik"))
        if not ticker or not cik:
            continue
        out[ticker] = {
            "ticker": ticker,
            "cik": cik,
            "title": str(row.get("title") or "").strip(),
        }
    return out


def fetch_sec_browse_company_lookup(ticker: str, *, names: list[object] | None = None) -> dict[str, str] | None:
    """Resolve stale or provider-mirrored tickers through SEC's official browse-edgar Atom feed."""
    normalized = _normalize_sec_ticker(ticker)
    if not normalized:
        return None
    from xml.etree import ElementTree

    from thoth_terminal.connectors.sec_edgar import _make_request

    ns = {"a": "http://www.w3.org/2005/Atom"}

    def _company_info(params: dict[str, str]) -> Any | None:
        resp = _make_request(
            _SEC_BROWSE_EDGAR_URL,
            params={**params, "action": "getcompany", "owner": "exclude", "count": "10", "output": "atom"},
            accept="application/atom+xml, application/xml, text/xml",
            timeout=20.0,
        )
        try:
            root = ElementTree.fromstring(resp.content or str(resp.text or "").encode(resp.encoding or "utf-8"))
        except Exception:
            return None
        return root.find("a:company-info", ns)

    def _node_text(name: str) -> str:
        node = info.find(f"a:{name}", ns)
        return str(node.text or "").strip() if node is not None else ""

    info = _company_info({"CIK": normalized})
    if info is not None:
        cik = _normalize_sec_cik(_node_text("cik"))
        title = _node_text("conformed-name")
        if cik and title:
            score = _sec_lookup_name_score(title, names or [ticker])
            official_ticker_match = len(normalized) >= 3
            if score >= 78 or official_ticker_match:
                return {
                    "ticker": normalized,
                    "cik": cik,
                    "title": title,
                    "source": "sec_browse_edgar_atom",
                    "match_kind": "name" if score >= 78 else "official_ticker",
                    "match_score": str(score),
                }

    seen_names: set[str] = set()
    for raw_name in names or []:
        query_name = re.sub(r"\s+", " ", str(raw_name or "").strip())
        key = query_name.upper()
        if len(key) < 4 or key in seen_names:
            continue
        seen_names.add(key)
        info = _company_info({"company": query_name})
        if info is None:
            continue
        cik = _normalize_sec_cik(_node_text("cik"))
        title = _node_text("conformed-name")
        if not cik or not title:
            continue
        score = _sec_lookup_name_score(title, names or [ticker])
        if score < 78:
            continue
        return {
            "ticker": normalized,
            "cik": cik,
            "title": title,
            "source": "sec_browse_edgar_atom_name",
            "match_kind": "name",
            "match_score": str(score),
        }
    return None


def _normalize_kr_stock_code(value: object) -> str | None:
    raw = str(value or "").strip().upper()
    if not raw:
        return None
    raw = re.sub(r"\.(KS|KQ|KR)$", "", raw)
    digits = re.sub(r"\D+", "", raw)
    if not digits:
        return None
    return digits.zfill(6)[-6:]


def _normalize_jp_stock_code(value: object) -> str | None:
    raw = str(value or "").strip().upper()
    if not raw:
        return None
    raw = re.sub(r"\.(T|JP)$", "", raw)
    raw = re.sub(r"[^0-9A-Z]+", "", raw)
    if raw.endswith("0"):
        candidate = raw[:-1]
        if re.fullmatch(r"[0-9]{4}[A-Z]?|[0-9]{3}[A-Z]", candidate):
            raw = candidate
    match = re.search(r"^[0-9]{4}[A-Z]?$|^[0-9]{3}[A-Z]$", raw)
    return raw if match else None


def _normalize_edinet_code(value: object) -> str | None:
    raw = str(value or "").strip().upper()
    if not raw:
        return None
    raw = re.sub(r"[^0-9A-Z]+", "", raw)
    return raw if re.fullmatch(r"E[0-9A-Z]{4,8}", raw) else None


def _normalize_tw_stock_code(value: object) -> str | None:
    raw = str(value or "").strip().upper()
    if not raw:
        return None
    raw = re.sub(r"\.(TW|TWO|TPE)$", "", raw)
    raw = re.sub(r"[^0-9A-Z]+", "", raw)
    return raw if re.fullmatch(r"[0-9]{4}[A-Z]?", raw) else None


def _normalize_hk_stock_code(value: object) -> str | None:
    raw = str(value or "").strip().upper()
    if not raw:
        return None
    raw = re.sub(r"\.HK$", "", raw)
    digits = re.sub(r"\D+", "", raw)
    if 1 <= len(digits) <= 5:
        return digits.zfill(5)
    return None


def _normalize_hkex_stock_query_id(value: object) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    digits = re.sub(r"\D+", "", raw)
    return digits or None


def _fetch_hkex_active_stock_id_map(*, timeout_s: float = 30.0) -> tuple[dict[str, str], dict[str, str]]:
    import requests

    from thoth_terminal.net import DEFAULT_HEADERS

    resp = requests.get(
        _HKEX_ACTIVE_STOCK_LIST_URL,
        headers={**DEFAULT_HEADERS, "Accept": "application/json,*/*"},
        timeout=float(timeout_s),
    )
    resp.raise_for_status()
    payload = resp.json()
    if isinstance(payload, dict):
        rows = payload.get("stock") or payload.get("stocks") or payload.get("data") or payload.get("result") or []
    elif isinstance(payload, list):
        rows = payload
    else:
        rows = []

    query_id_by_code: dict[str, str] = {}
    name_by_code: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = _normalize_hk_stock_code(row.get("c") or row.get("code") or row.get("stockCode") or row.get("stock_code"))
        query_id = _normalize_hkex_stock_query_id(row.get("i") or row.get("id") or row.get("stockId") or row.get("stock_id"))
        if not code or not query_id:
            continue
        query_id_by_code[code] = query_id
        name = re.sub(r"\s+", " ", str(row.get("n") or row.get("name") or row.get("stockName") or "").strip())
        if name:
            name_by_code[code] = name
    return query_id_by_code, name_by_code


def _normalize_cn_stock_code(value: object) -> str | None:
    raw = str(value or "").strip().upper()
    if not raw:
        return None
    raw = re.sub(r"\.(SS|SH|SZ|CN)$", "", raw)
    digits = re.sub(r"\D+", "", raw)
    return digits if re.fullmatch(r"[0-9]{6}", digits) else None


def _normalize_ca_stock_code(value: object) -> str | None:
    raw = str(value or "").strip().upper()
    if not raw:
        return None
    if re.search(r"\.(T|JP|TW|TWO|TPE|HK|SS|SH|SZ|KS|KQ|L|DE|F|PA|MI|SW)$", raw):
        return None
    raw = re.sub(r"\.(TO|TSX|V|TSXV|CN|NE|CA)$", "", raw)
    raw = re.sub(r"[^0-9A-Z.]+", "", raw)
    if re.fullmatch(r"[A-Z0-9][A-Z0-9.]{0,14}", raw):
        return raw
    return None


def _normalize_asx_stock_code(value: object) -> str | None:
    raw = str(value or "").strip().upper()
    if not raw:
        return None
    raw = re.sub(r"\.AX$", "", raw)
    raw = re.sub(r"[^0-9A-Z]+", "", raw)
    return raw if re.fullmatch(r"[0-9A-Z]{2,6}", raw) else None


def _normalize_bursa_stock_code(value: object) -> str | None:
    raw = str(value or "").strip().upper()
    if not raw:
        return None
    raw = re.sub(r"\.(KL|KLS|MY)$", "", raw)
    raw = re.sub(r"^BURSA[:\s]+", "", raw)
    raw = re.sub(r"[^0-9A-Z]+", "", raw)
    return raw if re.fullmatch(r"[0-9A-Z]{3,8}", raw) else None


def _normalize_kz_stock_code(value: object) -> str | None:
    raw = str(value or "").strip().upper()
    if not raw:
        return None
    raw = re.sub(r"\.(KZ|KASE|AIX)$", "", raw)
    raw = re.sub(r"^(AIX|KASE)[:\s]+", "", raw)
    raw = re.sub(r"[^0-9A-Z.]+", "", raw)
    return raw if re.fullmatch(r"[0-9A-Z][0-9A-Z.]{0,9}", raw) else None


def _normalize_sgx_stock_code(value: object) -> str | None:
    raw = str(value or "").strip().upper()
    if not raw:
        return None
    raw = re.sub(r"\.SI$", "", raw)
    raw = re.sub(r"[^0-9A-Z]+", "", raw)
    return raw if re.fullmatch(r"[0-9A-Z]{1,10}", raw) else None


def _normalize_nse_symbol(value: object) -> str | None:
    raw = str(value or "").strip().upper()
    if not raw:
        return None
    raw = re.sub(r"\.(NS|NSE|IN)$", "", raw)
    raw = re.sub(r"[^0-9A-Z&-]+", "", raw)
    return raw if re.fullmatch(r"[0-9A-Z][0-9A-Z&-]{0,19}", raw) else None


def _ca_stock_exchange(value: object, fallback: object = None) -> str | None:
    raw = str(value or "").strip().upper()
    exchange = _normalize_exchange_code(fallback)
    if raw.endswith(".V") or exchange in {"TSXV", "TSXVENTURE", "VENTURE"}:
        return "TSXV"
    if raw.endswith((".CN", ".NE")) or exchange in {"CSE", "CNSX", "NEO"}:
        return "CSE" if exchange in {"CSE", "CNSX"} else "NEO" if exchange == "NEO" else "CSE"
    if raw.endswith(".TO") or exchange in {"TSE", "TSX"}:
        return "TSX"
    return "TSX" if not exchange else exchange


def _ca_stock_candidates(
    instrument: ResearchInstrument,
    company: Company | None,
    issuer: Issuer | None,
) -> list[tuple[str, str | None]]:
    country = str(getattr(instrument, "country", None) or getattr(company, "region", None) or getattr(issuer, "jurisdiction", None) or "").strip().upper()
    currency = str(getattr(instrument, "currency", None) or "").strip().upper()
    exchange = getattr(instrument, "exchange", None) or getattr(company, "exchange", None)
    company_ticker = getattr(company, "ticker", None) if company is not None else None
    values: list[tuple[object, object, bool]] = [
        (getattr(instrument, "yahoo_symbol", None), exchange, False),
        (getattr(instrument, "ticker", None), exchange, False),
        (company_ticker, getattr(company, "exchange", None) if company is not None else None, True),
    ]
    out: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for raw_value, raw_exchange, allow_home_country in values:
        raw = str(raw_value or "").strip().upper()
        if not raw:
            continue
        exchange_norm = _normalize_exchange_code(raw_exchange)
        is_canadian_exchange = exchange_norm in {"TSX", "TSXV", "CSE", "CNSX", "NEO"} or (
            exchange_norm == "TSE" and (country in {"CA", "CANADA"} or currency == "CAD")
        )
        is_canadian_symbol = raw.endswith((".TO", ".V", ".CN", ".NE", ".CA")) or is_canadian_exchange
        if not is_canadian_symbol and not (allow_home_country and country in {"CA", "CANADA"}):
            continue
        code = _normalize_ca_stock_code(raw)
        if not code or code in seen:
            continue
        # OTC tickers such as POETF should not displace the issuer's Canadian home ticker.
        if raw == code and _normalize_exchange_code(raw_exchange) in {"PINK", "OTC", "OTCMKTS"}:
            continue
        seen.add(code)
        out.append((code, _ca_stock_exchange(raw, raw_exchange)))
    return out


def _cninfo_column_for_stock_code(stock_code: str) -> str:
    return "sse" if str(stock_code).startswith(("6", "9")) else "szse"


def fetch_opendart_corp_code_map(api_key: str | None = None) -> dict[str, dict[str, str]]:
    """Fetch OpenDART's official stock-code to corporation-code mapping."""
    import io
    import zipfile
    import xml.etree.ElementTree as ET

    import requests

    from thoth_terminal.net import DEFAULT_HEADERS

    key = str(api_key or getattr(settings, "opendart_api_key", None) or "").strip()
    if not key:
        return {}
    resp = requests.get(
        _OPENDART_CORP_CODE_URL,
        params={"crtfc_key": key},
        headers={**DEFAULT_HEADERS, "Accept": "application/zip,application/xml,*/*"},
        timeout=60.0,
    )
    resp.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        name = next((item for item in zf.namelist() if item.lower().endswith(".xml")), zf.namelist()[0])
        xml_bytes = zf.read(name)
    root = ET.fromstring(xml_bytes)
    out: dict[str, dict[str, str]] = {}
    for node in root.findall(".//list"):
        stock_code = _normalize_kr_stock_code(node.findtext("stock_code"))
        corp_code = str(node.findtext("corp_code") or "").strip()
        if not stock_code or not corp_code:
            continue
        out[stock_code] = {
            "stock_code": stock_code,
            "corp_code": corp_code,
            "corp_name": str(node.findtext("corp_name") or "").strip(),
            "corp_eng_name": str(node.findtext("corp_eng_name") or "").strip(),
            "modify_date": str(node.findtext("modify_date") or "").strip(),
        }
    return out


def sec_cik_from_document(doc: Document) -> str | None:
    """Extract a SEC CIK from regulatory document metadata without network calls."""
    extra = _doc_extra(doc)
    for key in ("cik", "company_cik", "filer_cik", "cik_str"):
        cik = _normalize_sec_cik(extra.get(key))
        if cik:
            return cik

    _, title_cik = _sec_title_identity(str(doc.title or ""))
    if title_cik:
        return title_cik

    for raw_url in (doc.url, doc.canonical_url):
        match = _SEC_ARCHIVE_CIK_RE.search(str(raw_url or ""))
        if match:
            cik = _normalize_sec_cik(match.group("cik"))
            if cik:
                return cik

    accession = str(extra.get("accession_number") or "").strip()
    accession_digits = re.sub(r"\D+", "", accession)
    if len(accession_digits) >= 10:
        return accession_digits[:10].zfill(10)
    return None


def sec_ciks_from_company_documents(session: Session, *, company: Company, limit: int = 50) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    rows = (
        session.query(Document)
        .join(CompanyDocument, CompanyDocument.document_id == Document.id)
        .filter(CompanyDocument.company_id == int(company.id))
        .order_by(Document.ingested_at.desc(), Document.id.desc())
        .limit(max(1, int(limit or 50)))
        .all()
    )
    for doc in rows:
        extra = _doc_extra(doc)
        connector = str(extra.get("connector") or "").strip()
        if connector not in {"sec_edgar", "sec_edgar_rss", "sec_companyfacts"} and "sec" not in str(doc.kind or "").lower():
            continue
        cik = sec_cik_from_document(doc)
        if cik and cik not in seen:
            seen.add(cik)
            out.append(cik)
    return out


def ensure_sec_companyfacts_source_for_ciks(
    session: Session,
    *,
    ciks: list[str],
    reason: str = "decision_spine",
    company_slug_by_cik: dict[str, str] | None = None,
    ticker_by_cik: dict[str, str | None] | None = None,
    company_id_by_cik: dict[str, int] | None = None,
) -> dict[str, Any]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in ciks:
        cik = _normalize_sec_cik(raw)
        if cik and cik not in seen:
            seen.add(cik)
            normalized.append(cik)
    if not normalized:
        return {"registered": False, "reason": "no_ciks", "ciks": []}

    def _source_ciks(row: Source) -> set[str]:
        row_cfg = row.config if isinstance(row.config, dict) else {}
        if row_cfg.get("connector") != "sec_companyfacts" or row_cfg.get("deleted"):
            return set()
        return {cik for cik in (_normalize_sec_cik(raw) for raw in (row_cfg.get("ciks") or [])) if cik}

    def _apply_source_config(row: Source, *, target_ciks: list[str], added: list[str], covered_by_existing: bool) -> dict[str, Any]:
        cfg = dict(row.config or {})

        def _merge_map(key: str, updates: dict[str, Any] | None) -> dict[str, Any]:
            current = dict(cfg.get(key) if isinstance(cfg.get(key), dict) else {})
            for raw_cik, raw_value in (updates or {}).items():
                cik = _normalize_sec_cik(raw_cik)
                if cik and raw_value is not None and str(raw_value).strip():
                    current[cik] = raw_value
            return current

        existing = [_normalize_sec_cik(raw) for raw in (cfg.get("ciks") or [])]
        existing = [cik for cik in existing if cik]
        merged = sorted(set(existing).union(target_ciks), key=lambda item: int(item))
        cfg.update(
            {
                "connector": "sec_companyfacts",
                "ciks": merged,
                "company_slug_by_cik": _merge_map("company_slug_by_cik", company_slug_by_cik),
                "ticker_by_cik": _merge_map("ticker_by_cik", ticker_by_cik),
                "company_id_by_cik": _merge_map("company_id_by_cik", company_id_by_cik),
                "max_points_per_concept": int(cfg.get("max_points_per_concept") or 12),
                "max_items_per_run": max(int(cfg.get("max_items_per_run") or 0), min(max(len(merged), 20), 500)),
                "ciks_per_run": max(1, int(cfg.get("ciks_per_run") or cfg.get("requests_per_run") or 8)),
                "fetch_article": False,
                "download_article_images": False,
                "refresh_existing": True,
                "enabled": True,
                "last_registered_reason": reason,
                "source_quality": {
                    **(cfg.get("source_quality") if isinstance(cfg.get("source_quality"), dict) else {}),
                    "authority": "primary_regulator",
                    "timeliness": str(
                        (cfg.get("source_quality") if isinstance(cfg.get("source_quality"), dict) else {}).get("timeliness")
                        or "scheduled"
                    ),
                    "acquisition_fragility": "low",
                    "rights_class": "public",
                },
                "default_domains": list(cfg.get("default_domains") or []),
                "default_tags": list(cfg.get("default_tags") or []),
                "default_event_types": list(cfg.get("default_event_types") or []),
            }
        )
        if row.name == _SEC_COMPANYFACTS_ACTIVE_SOURCE_NAME:
            cfg["managed_by"] = "decision_spine"
            cfg["coverage_scope"] = "active_universe"
        row.config = cfg
        session.flush()
        state = session.query(SourceState).filter_by(source_id=int(row.id)).first()
        should_enqueue = bool(added) or state is None or state.last_success_at is None
        return {
            "registered": True,
            "source_id": int(row.id),
            "source_name": row.name,
            "ciks": target_ciks,
            "ciks_added": added,
            "cik_count": len(merged),
            "should_enqueue": should_enqueue,
            "covered_by_existing_source": covered_by_existing,
        }

    if len(normalized) == 1:
        cik = normalized[0]
        for candidate in session.query(Source).order_by(Source.id.asc()).all():
            if candidate.name == _SEC_COMPANYFACTS_ACTIVE_SOURCE_NAME:
                continue
            if cik in _source_ciks(candidate):
                return _apply_source_config(candidate, target_ciks=normalized, added=[], covered_by_existing=True)

    source = session.query(Source).filter_by(name=_SEC_COMPANYFACTS_ACTIVE_SOURCE_NAME).first()
    if source is None:
        source = Source(
            name=_SEC_COMPANYFACTS_ACTIVE_SOURCE_NAME,
            category="filings",
            priority=1,
            polling_interval_seconds=43200,
            config={},
            created_at=utcnow(),
        )
        session.add(source)
        session.flush()

    existing_set = _source_ciks(source)
    added = [cik for cik in normalized if cik not in existing_set]
    return _apply_source_config(source, target_ciks=normalized, added=added, covered_by_existing=False)


def ensure_sec_edgar_source_for_ciks(
    session: Session,
    *,
    ciks: list[str],
    reason: str = "active_universe_source_pack",
    company_slug_by_cik: dict[str, str] | None = None,
    ticker_by_cik: dict[str, str | None] | None = None,
    company_id_by_cik: dict[str, int] | None = None,
    company_name_by_cik: dict[str, str] | None = None,
) -> dict[str, Any]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in ciks:
        cik = _normalize_sec_cik(raw)
        if cik and cik not in seen:
            seen.add(cik)
            normalized.append(cik)
    if not normalized:
        return {"registered": False, "reason": "no_ciks", "ciks": []}

    source = session.query(Source).filter_by(name=_SEC_EDGAR_ACTIVE_SOURCE_NAME).first()
    if source is None:
        source = Source(
            name=_SEC_EDGAR_ACTIVE_SOURCE_NAME,
            category="filings",
            priority=1,
            polling_interval_seconds=43200,
            config={},
            created_at=utcnow(),
        )
        session.add(source)
        session.flush()

    cfg = dict(source.config or {})
    previous_cursor_plan_key = str(cfg.get("cursor_plan_key") or "").strip()

    def _merge_map(key: str, updates: dict[str, Any] | None) -> dict[str, Any]:
        current = dict(cfg.get(key) if isinstance(cfg.get(key), dict) else {})
        for raw_cik, raw_value in (updates or {}).items():
            cik = _normalize_sec_cik(raw_cik)
            if cik and raw_value is not None and str(raw_value).strip():
                current[cik] = raw_value
        return current

    existing = [_normalize_sec_cik(raw) for raw in (cfg.get("ciks") or [])]
    existing = [cik for cik in existing if cik]
    merged = sorted(set(existing).union(normalized), key=lambda item: int(item))
    added = [cik for cik in normalized if cik not in set(existing)]
    existing_filing_types = [
        str(value or "").strip()
        for value in (cfg.get("filing_types") or [])
        if str(value or "").strip()
    ]
    existing_filing_type_set = set(existing_filing_types)
    filing_types_added = [
        filing_type
        for filing_type in _SEC_ACTIVE_UNIVERSE_FILING_TYPES
        if filing_type not in existing_filing_type_set
    ]
    merged_filing_types = existing_filing_types + filing_types_added
    cfg.update(
        {
            "connector": "sec_edgar",
            "ciks": merged,
            "filing_types": merged_filing_types or list(_SEC_ACTIVE_UNIVERSE_FILING_TYPES),
            "since_days": max(int(cfg.get("since_days") or 0), 730),
            "max_items_per_run": max(int(cfg.get("max_items_per_run") or 0), _SEC_SOURCE_PACK_MAX_ITEMS_PER_RUN),
            "max_ciks_per_run": max(int(cfg.get("max_ciks_per_run") or 0), _SEC_SOURCE_PACK_MAX_CIKS_PER_RUN),
            "source_pack_max_items_per_cik": max(
                int(cfg.get("source_pack_max_items_per_cik") or 0),
                _SEC_SOURCE_PACK_MAX_ITEMS_PER_CIK,
            ),
            "rotate_ciks": True,
            "prioritize_source_pack_forms": True,
            "cursor_plan_key": _SEC_SOURCE_PACK_CURSOR_PLAN_KEY,
            "prefetch_outside_transaction": True,
            "defer_ingest_derived_sync": True,
            "include_exhibits": False,
            "company_slug_by_cik": _merge_map("company_slug_by_cik", company_slug_by_cik),
            "ticker_by_cik": _merge_map("ticker_by_cik", ticker_by_cik),
            "company_id_by_cik": _merge_map("company_id_by_cik", company_id_by_cik),
            "company_name_by_cik": _merge_map("company_name_by_cik", company_name_by_cik),
            "fetch_article": bool(cfg.get("fetch_article", False)),
            "download_article_images": bool(cfg.get("download_article_images", False)),
            "refresh_existing": True,
            "enabled": True,
            "managed_by": "source_pack",
            "coverage_scope": "active_universe",
            "last_registered_reason": reason,
            "source_quality": {
                **(cfg.get("source_quality") if isinstance(cfg.get("source_quality"), dict) else {}),
                "authority": "primary_regulator",
                "timeliness": str(
                    (cfg.get("source_quality") if isinstance(cfg.get("source_quality"), dict) else {}).get("timeliness")
                    or "scheduled"
                ),
                "acquisition_fragility": "low",
                "rights_class": "public",
            },
            "default_domains": list(cfg.get("default_domains") or []),
            "default_tags": list(cfg.get("default_tags") or []),
            "default_event_types": list(cfg.get("default_event_types") or []),
        }
    )
    source.config = cfg
    session.flush()
    state = session.query(SourceState).filter_by(source_id=int(source.id)).first()
    cursor_reset = False
    if state is not None and (
        previous_cursor_plan_key != str(cfg.get("cursor_plan_key") or "").strip()
        or bool(filing_types_added)
    ):
        state.cursor = {}
        cursor_reset = True
        session.flush()
    return {
        "registered": True,
        "source_id": int(source.id),
        "source_name": source.name,
        "ciks": normalized,
        "ciks_added": added,
        "cik_count": len(merged),
        "filing_types_added": filing_types_added,
        "cursor_reset": cursor_reset,
        "should_enqueue": bool(added) or cursor_reset or state is None or state.last_success_at is None,
    }


def _active_universe_sec_candidate(instrument: ResearchInstrument, company: Company | None, issuer: Issuer | None) -> bool:
    if not bool(getattr(instrument, "is_public", True)):
        return False
    exchange = _normalize_exchange_code(getattr(instrument, "exchange", None) or getattr(company, "exchange", None))
    if exchange in _SEC_LISTING_EXCHANGES:
        return True
    if _is_usish_country(getattr(instrument, "country", None) or getattr(company, "region", None) or getattr(issuer, "jurisdiction", None)):
        return True
    if _is_sec_ticker_lookup_fallback_candidate(instrument, company):
        return True
    if issuer is not None and issuer.id is not None:
        return any(
            _normalize_sec_cik(identifier.id_value)
            for identifier in getattr(issuer, "identifiers", []) or []
            if str(identifier.id_type or "").upper() == "CIK"
        )
    return False


def _issuer_ciks(issuer: Issuer | None) -> list[str]:
    if issuer is None:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for identifier in getattr(issuer, "identifiers", []) or []:
        if str(identifier.id_type or "").upper() != "CIK":
            continue
        cik = _normalize_sec_cik(identifier.id_value)
        if cik and cik not in seen:
            seen.add(cik)
            out.append(cik)
    return out


def _issuer_identifier_values(issuer: Issuer | None, id_type: str) -> list[str]:
    wanted = str(id_type or "").strip().upper()
    if issuer is None or not wanted:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for identifier in getattr(issuer, "identifiers", []) or []:
        if str(identifier.id_type or "").upper() != wanted:
            continue
        value = str(identifier.id_value or "").strip()
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _normalize_lei(value: object) -> str | None:
    raw = str(value or "").strip().upper()
    if len(raw) == 20 and raw.isalnum():
        return raw
    return None


def _issuer_leis(issuer: Issuer | None) -> list[str]:
    if issuer is None:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for raw in [getattr(issuer, "lei", None), *_issuer_identifier_values(issuer, "LEI")]:
        lei = _normalize_lei(raw)
        if lei and lei not in seen:
            seen.add(lei)
            out.append(lei)
    return out


_GLEIF_COUNTRY_ALIASES = {
    "AUSTRIA": "AT",
    "BELGIUM": "BE",
    "DENMARK": "DK",
    "FINLAND": "FI",
    "FRANCE": "FR",
    "GERMANY": "DE",
    "GREECE": "GR",
    "IRELAND": "IE",
    "ITALY": "IT",
    "NETHERLANDS": "NL",
    "NORWAY": "NO",
    "PORTUGAL": "PT",
    "SPAIN": "ES",
    "SWEDEN": "SE",
    "SWITZERLAND": "CH",
    "UNITED KINGDOM": "GB",
    "UK": "GB",
}
_GLEIF_ESEF_COUNTRIES = set(_GLEIF_COUNTRY_ALIASES.values())
_GLEIF_SUFFIX_COUNTRY = {
    "AS": "NL",
    "BR": "BE",
    "CO": "DK",
    "DE": "DE",
    "HE": "FI",
    "IR": "IE",
    "L": "GB",
    "LS": "PT",
    "MC": "ES",
    "MI": "IT",
    "OL": "NO",
    "PA": "FR",
    "ST": "SE",
    "SW": "CH",
    "VI": "AT",
    "AT": "GR",
}
_GLEIF_EXCHANGE_COUNTRY = {
    "AEB": "NL",
    "AMS": "NL",
    "ATH": "GR",
    "BVME": "IT",
    "EBS": "CH",
    "EURONEXTAMSTERDAM": "NL",
    "EURONEXTPARIS": "FR",
    "FWB": "DE",
    "FWB2": "DE",
    "IBIS": "DE",
    "LSE": "GB",
    "MIL": "IT",
    "SBF": "FR",
    "SIX": "CH",
    "XETRA": "DE",
}
_GLEIF_LEGAL_SUFFIXES = {
    "AB",
    "ABP",
    "AG",
    "AKTIENGESELLSCHAFT",
    "APS",
    "ASA",
    "AS",
    "BV",
    "CO",
    "CORP",
    "GMBH",
    "INC",
    "KGAA",
    "LLC",
    "LLP",
    "LP",
    "LTD",
    "LIMITED",
    "NV",
    "OYJ",
    "PLC",
    "SA",
    "SAS",
    "SE",
    "SPA",
}
_GLEIF_LEGAL_SUFFIX_PHRASES = (
    ("KOMMANDITGESELLSCHAFT", "AUF", "AKTIEN"),
    ("SOCIETA", "PER", "AZIONI"),
    ("SOCIEDAD", "ANONIMA"),
    ("SOCIETE", "ANONYME"),
)
_GLEIF_TRADING_SUFFIXES = {"N", "REG", "PREF", "VOR", "VORPREF"}
_GLEIF_SHARE_CLASS_SUFFIXES = {"A", "B", "C", "D"}
_GLEIF_NAME_ALIASES = {
    # DHL Group is the current trading brand; the active LEI legal name remains Deutsche Post AG.
    "DHL GROUP": ["Deutsche Post AG"],
    # The active GLEIF record for the Athens-listed central bank uses its Greek transliterated legal name.
    "BANK OF GREECE": ["TRAPEZA TIS ELLADOS"],
}


def _ascii_fold(value: object) -> str:
    text = str(value or "")
    text = text.translate(
        str.maketrans(
            {
                "Æ": "AE",
                "æ": "ae",
                "Ø": "O",
                "ø": "o",
                "Ł": "L",
                "ł": "l",
                "ß": "SS",
            }
        )
    )
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def _compact_legal_name(value: object) -> str:
    text = _ascii_fold(value).strip().upper()
    if not text:
        return ""
    text = re.sub(r"\b(SPONSORED|UNSPONSORED|SPON|UNSPON)\b", " ", text)
    text = re.sub(r"\b(ADR|ADS|GDR)\b", " ", text)
    text = re.sub(r"\bA\s*/\s*S\b", " AS ", text)
    text = re.sub(r"\bA\.?B\.?P\.?\b", " ABP ", text)
    text = re.sub(r"\bO\.?Y\.?J\.?\b", " OYJ ", text)
    text = re.sub(r"\bA\.?P\.?S\.?\b", " APS ", text)
    text = re.sub(r"\bK\.?G\.?A\.?A\.?\b", " KGAA ", text)
    text = re.sub(r"\bL\.?L\.?C\.?\b", " LLC ", text)
    text = re.sub(r"\bL\.?L\.?P\.?\b", " LLP ", text)
    text = re.sub(r"\bL\.?P\.?\b", " LP ", text)
    text = re.sub(r"\bA\.?G\.?\b", " AG ", text)
    text = re.sub(r"\b(S\.?P\.?A\.?)\b", " SPA ", text)
    text = re.sub(r"\b(N\.?V\.?)\b", " NV ", text)
    text = re.sub(r"\b(S\.?A\.?)\b", " SA ", text)
    text = re.sub(r"\b(P\.?L\.?C\.?)\b", " PLC ", text)
    text = text.replace("&", " AND ")
    text = re.sub(r"[^A-Z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _core_legal_name(value: object) -> str:
    tokens = _compact_legal_name(value).split()
    while tokens:
        removed = False
        while tokens and tokens[-1] in _GLEIF_LEGAL_SUFFIXES | _GLEIF_TRADING_SUFFIXES:
            tokens.pop()
            removed = True
        for phrase in _GLEIF_LEGAL_SUFFIX_PHRASES:
            if len(tokens) >= len(phrase) and tuple(tokens[-len(phrase) :]) == phrase:
                del tokens[-len(phrase) :]
                removed = True
                break
        if not removed:
            break
    return " ".join(tokens)


def _gleif_legal_form_expansions(value: object) -> list[str]:
    tokens = _compact_legal_name(value).split()
    while tokens and tokens[-1] in _GLEIF_TRADING_SUFFIXES:
        tokens.pop()
    if len(tokens) < 2:
        return []
    suffix = tokens[-1]
    base = " ".join(tokens[:-1]).strip()
    if not base:
        return []
    if suffix == "AG":
        return [f"{base} Aktiengesellschaft"]
    if suffix == "KGAA":
        return [f"{base} Kommanditgesellschaft auf Aktien"]
    if suffix == "SPA":
        return [f"{base} Societa per Azioni"]
    if suffix == "SA":
        return [f"{base} Societe Anonyme", f"{base} Sociedad Anonima"]
    if suffix == "AS":
        return [f"{base} A/S"]
    return []


def _has_gleif_public_company_form(value: object) -> bool:
    compact = _compact_legal_name(value)
    tokens = compact.split()
    if tokens and tokens[-1] in {"AB", "ABP", "AG", "AKTIENGESELLSCHAFT", "AS", "ASA", "KGAA", "NV", "OYJ", "PLC", "SA", "SE", "SPA"}:
        return True
    for phrase in _GLEIF_LEGAL_SUFFIX_PHRASES:
        needle = " ".join(phrase)
        if re.search(rf"\b{re.escape(needle)}\b", compact):
            return True
    return False


def _gleif_name_variants(*values: object) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        raw = re.sub(r"\s+", " ", str(value or "").strip())
        if not raw:
            continue
        variants = {
            raw,
            re.sub(r"\b(SPONSORED|UNSPONSORED|SPONS|SPON|UNSPON|ADR|ADS|GDR)\b", " ", raw, flags=re.IGNORECASE),
            re.sub(r"\s+(N|REG|PREF|VOR[-\s]?PREF)$", "", raw, flags=re.IGNORECASE).strip(),
        }
        receipt_clean = re.sub(
            r"\b(SPONSORED|UNSPONSORED|SPONS|SPON|UNSPON|ADR|ADS|GDR|DEPOSITARY|DEPOSITORY|REGS?)\b",
            " ",
            raw,
            flags=re.IGNORECASE,
        )
        receipt_clean = re.sub(r"[-_/]+", " ", receipt_clean)
        receipt_clean = re.sub(r"\s+", " ", receipt_clean).strip()
        if receipt_clean:
            variants.add(receipt_clean)
        for variant in list(variants):
            variants.update(_gleif_legal_form_expansions(variant))
            compact = _compact_legal_name(variant)
            core = _core_legal_name(variant)
            tokens = compact.split()
            if len(tokens) >= 3 and tokens[-1] in _GLEIF_SHARE_CLASS_SUFFIXES and tokens[-2] in _GLEIF_LEGAL_SUFFIXES:
                variants.add(" ".join(tokens[:-1]))
            if compact:
                variants.add(compact)
            if core and len(core) >= 3:
                variants.add(core)
                if core == "AIR LIQUIDE":
                    variants.add("L'AIR LIQUIDE")
            for alias in _GLEIF_NAME_ALIASES.get(core or compact, []):
                variants.add(alias)
        for variant in variants:
            text = re.sub(r"\s+", " ", str(variant or "").strip())
            key = _compact_legal_name(text)
            if len(key) < 3 or key in seen:
                continue
            seen.add(key)
            out.append(text[:160])
    out.sort(key=lambda item: (len(_core_legal_name(item)), len(_compact_legal_name(item))), reverse=True)
    return out[:8]


def _infer_gleif_country(
    *,
    instrument: ResearchInstrument,
    company: Company | None,
    issuer: Issuer | None,
    exchange: str,
) -> str | None:
    raw_values = [
        getattr(instrument, "country", None),
        getattr(issuer, "jurisdiction", None) if issuer is not None else None,
        getattr(company, "region", None) if company is not None else None,
    ]
    for raw in raw_values:
        text = str(raw or "").strip().upper()
        if not text:
            continue
        if text == "UK":
            return "GB"
        if len(text) == 2 and text in _GLEIF_ESEF_COUNTRIES:
            return text
        if text in _GLEIF_COUNTRY_ALIASES:
            return _GLEIF_COUNTRY_ALIASES[text]
    suffix_country = None
    for raw in [getattr(instrument, "yahoo_symbol", None), getattr(instrument, "ticker", None), getattr(company, "ticker", None) if company is not None else None]:
        text = str(raw or "").strip().upper()
        match = re.search(r"\.([A-Z]{1,3})$", text)
        if match:
            country = _GLEIF_SUFFIX_COUNTRY.get(match.group(1))
            if country:
                suffix_country = country
                break
    exchange_country = _GLEIF_EXCHANGE_COUNTRY.get(exchange)
    if suffix_country and exchange_country and suffix_country != exchange_country:
        return None
    return suffix_country


def _is_esef_wrapper_or_fund_candidate(instrument: ResearchInstrument, exchange: str) -> bool:
    text = " ".join(
        str(value or "")
        for value in [
            getattr(instrument, "security_type", None),
            getattr(instrument, "display_name", None),
            getattr(instrument, "legal_name", None),
            exchange,
        ]
    ).upper()
    if re.search(r"\b(ETF|ETN|FUND|TRUST|UCITS|BOND|TREASURY|NOTE)\b", text) or "ETF" in text:
        return True
    return bool(re.search(r"\b(ADR|ADS|GDR|DEPOSITARY|DEPOSITORY|SPONSORED|UNSPONSORED|REGS)\b", text))


def _is_esef_depositary_receipt_gleif_candidate(
    *,
    instrument: ResearchInstrument,
    company: Company | None,
    issuer: Issuer | None,
) -> bool:
    text = " ".join(
        str(value or "")
        for value in [
            getattr(instrument, "security_type", None),
            getattr(instrument, "display_name", None),
            getattr(instrument, "legal_name", None),
            getattr(instrument, "ticker", None),
            getattr(instrument, "yahoo_symbol", None),
        ]
    ).upper()
    if not re.search(r"\b(ADR|ADS|GDR|DEPOSITARY|DEPOSITORY|SPONSORED|UNSPONSORED|REGS)\b", text):
        return False
    if re.search(r"\b(ETF|ETN|FUND|TRUST|UCITS|BOND|TREASURY|NOTE)\b", text) or "ETF" in text:
        return False
    values = [
        getattr(issuer, "legal_name", None) if issuer is not None else None,
        getattr(issuer, "display_name", None) if issuer is not None else None,
        getattr(company, "name", None) if company is not None else None,
        getattr(instrument, "legal_name", None),
        getattr(instrument, "display_name", None),
    ]
    for value in values:
        stripped = re.sub(
            r"\b(SPONSORED|UNSPONSORED|SPONS|SPON|UNSPON|ADR|ADS|GDR|DEPOSITARY|DEPOSITORY|REGS?)\b",
            " ",
            str(value or ""),
            flags=re.IGNORECASE,
        )
        stripped = re.sub(r"[-_/]+", " ", stripped)
        stripped = re.sub(r"\s+", " ", stripped).strip()
        if stripped and _has_gleif_public_company_form(stripped):
            return True
    return False


def _is_us_primary_esef_false_positive(
    *,
    instrument: ResearchInstrument,
    company: Company | None,
    issuer: Issuer | None,
    exchange: str,
    country_raw: str,
) -> bool:
    raw_values = {
        country_raw,
        str(getattr(instrument, "country", None) or "").strip().upper(),
        str(getattr(company, "region", None) if company is not None else "").strip().upper(),
        str(getattr(issuer, "jurisdiction", None) if issuer is not None else "").strip().upper(),
    }
    us_country = bool(raw_values.intersection({"US", "USA", "UNITED STATES"}))
    us_exchange = exchange in {"NASDAQ", "NYSE", "AMEX", "ARCA", "BATS"}
    return (us_country or us_exchange) and not _is_esef_wrapper_or_fund_candidate(instrument, exchange)


def _gleif_match_score(candidate: Any, *, names: list[str], expected_country: str | None) -> tuple[int, str]:
    legal_name = str(getattr(candidate, "legal_name", "") or "").strip()
    if not legal_name:
        return 0, "missing_legal_name"
    jurisdiction = str(getattr(candidate, "legal_jurisdiction", "") or "").strip().upper()
    if expected_country and jurisdiction and jurisdiction != expected_country:
        return 0, "jurisdiction_mismatch"
    cand = _compact_legal_name(legal_name)
    cand_core = _core_legal_name(legal_name)
    best_ratio = 0.0
    exact = False
    core_exact = False
    prefix_public_company = False
    matched_name = ""
    for name in names:
        q = _compact_legal_name(name)
        q_core = _core_legal_name(name)
        if not q:
            continue
        ratio = SequenceMatcher(None, q, cand).ratio()
        core_ratio = SequenceMatcher(None, q_core, cand_core).ratio() if q_core and cand_core else 0.0
        ratio = max(ratio, core_ratio)
        if ratio > best_ratio:
            best_ratio = ratio
            matched_name = name
        exact = exact or bool(q and q == cand)
        core_exact = core_exact or bool(q_core and cand_core and q_core == cand_core)
        prefix_public_company = prefix_public_company or bool(
            _has_gleif_public_company_form(legal_name)
            and (
                (q_core and cand_core and len(q_core) >= 4 and cand_core.startswith(f"{q_core} "))
                or (q and cand and len(q) >= 6 and cand.startswith(f"{q} "))
            )
        )
    score = int(best_ratio * 45)
    if exact:
        score += 45
    elif core_exact:
        score += 38 if expected_country else 30 if _has_gleif_public_company_form(legal_name) else 15
    elif prefix_public_company:
        score += 44 if expected_country else 24
    elif best_ratio >= 0.94:
        score += 28
    if expected_country and jurisdiction == expected_country:
        score += 20
    if str(getattr(candidate, "entity_status", "") or "").strip().upper() == "ACTIVE":
        score += 8
    registration = str(getattr(candidate, "registration_status", "") or "").strip().upper()
    if registration == "ISSUED":
        score += 8
    elif registration in {"LAPSED", "RETIRED"}:
        score -= 8
    return score, matched_name or "no_name_match"


def _resolve_issuer_lei_with_gleif(
    session: Session,
    *,
    issuer: Issuer | None,
    company: Company | None,
    instrument: ResearchInstrument,
    exchange: str,
    max_queries: int,
) -> dict[str, Any]:
    if issuer is None or issuer.id is None:
        return {"status": "blocked_no_issuer", "queries_used": 0}
    if not bool(getattr(settings, "gleif_api_enabled", True)):
        return {"status": "disabled", "queries_used": 0}
    existing = _issuer_leis(issuer)
    if existing:
        return {"status": "already_has_lei", "lei": existing[0], "queries_used": 0}
    names = _gleif_name_variants(
        getattr(issuer, "legal_name", None),
        getattr(issuer, "display_name", None),
        getattr(company, "name", None) if company is not None else None,
        getattr(instrument, "legal_name", None),
        getattr(instrument, "display_name", None),
    )
    if not names:
        return {"status": "missing_name", "queries_used": 0}
    expected_country = _infer_gleif_country(instrument=instrument, company=company, issuer=issuer, exchange=exchange)
    queries_used = 0
    best: tuple[int, Any, str] | None = None
    try:
        from thoth_terminal.entity_resolution import search_lei_by_name
    except Exception as exc:
        return {"status": "resolver_unavailable", "error": f"{type(exc).__name__}: {exc}", "queries_used": 0}
    for name in names:
        if queries_used >= max(0, int(max_queries)):
            break
        queries_used += 1
        try:
            rows = search_lei_by_name(name, jurisdiction=expected_country, limit=25) if expected_country else search_lei_by_name(name, limit=25)
        except Exception as exc:
            return {
                "status": "resolver_error",
                "error": f"{type(exc).__name__}: {exc}",
                "queries_used": queries_used,
                "attempted_name": name,
            }
        for candidate in rows:
            score, matched_name = _gleif_match_score(candidate, names=names, expected_country=expected_country)
            if best is None or score > best[0]:
                best = (score, candidate, matched_name)
        if best is not None and best[0] >= 95:
            break
    if best is None:
        return {"status": "not_found", "queries_used": queries_used, "expected_country": expected_country, "attempted_names": names[:4]}
    score, candidate, matched_name = best
    lei = _normalize_lei(getattr(candidate, "lei", None))
    if not lei or score < 82:
        return {
            "status": "low_confidence",
            "queries_used": queries_used,
            "best_score": score,
            "best_legal_name": getattr(candidate, "legal_name", None),
            "expected_country": expected_country,
            "matched_name": matched_name,
        }
    holder = session.query(Issuer).filter(Issuer.lei == lei, Issuer.id != int(issuer.id)).first()
    if holder is not None:
        return {"status": "issuer_lei_conflict", "lei": lei, "other_issuer_id": int(holder.id), "queries_used": queries_used}
    status, related_id = _upsert_issuer_identifier(session, issuer=issuer, id_type="LEI", id_value=lei, exchange="GLEIF")
    if status == "conflict":
        return {"status": "identifier_conflict", "lei": lei, "other_issuer_id": related_id, "queries_used": queries_used}
    issuer.lei = issuer.lei or lei
    issuer.jurisdiction = issuer.jurisdiction or str(getattr(candidate, "legal_jurisdiction", "") or "").strip().upper() or None
    meta = dict(issuer.extra or {}) if isinstance(issuer.extra, dict) else {}
    meta["gleif_identity"] = {
        "lei": lei,
        "legal_name": getattr(candidate, "legal_name", None),
        "legal_jurisdiction": getattr(candidate, "legal_jurisdiction", None),
        "entity_status": getattr(candidate, "entity_status", None),
        "registration_status": getattr(candidate, "registration_status", None),
        "match_score": score,
        "matched_name": matched_name,
        "expected_country": expected_country,
        "source": "GLEIF API",
        "resolved_at": utcnow().isoformat(),
    }
    issuer.extra = meta
    issuer.updated_at = utcnow()
    session.flush()
    return {
        "status": "resolved" if status == "created" else "existing_identifier",
        "lei": lei,
        "score": score,
        "legal_name": getattr(candidate, "legal_name", None),
        "expected_country": expected_country,
        "queries_used": queries_used,
    }


def _upsert_issuer_cik_identifier(
    session: Session,
    *,
    issuer: Issuer,
    cik: str,
    ticker: str | None,
    source_title: str | None,
) -> tuple[str, int | None]:
    existing = session.query(IssuerIdentifier).filter_by(id_type="CIK", id_value=cik).first()
    if existing is not None:
        if int(existing.issuer_id) == int(issuer.id):
            return "existing", int(existing.id)
        return "conflict", int(existing.issuer_id)
    row = IssuerIdentifier(
        issuer_id=int(issuer.id),
        id_type="CIK",
        id_value=cik,
        exchange="SEC",
        currency=None,
        is_primary=False,
        valid_from=None,
        valid_to=None,
        created_at=utcnow(),
    )
    session.add(row)
    meta = dict(issuer.extra or {}) if isinstance(issuer.extra, dict) else {}
    sec_meta = dict(meta.get("sec_identity") if isinstance(meta.get("sec_identity"), dict) else {})
    sec_meta.update(
        {
            "cik": cik,
            "ticker": ticker,
            "sec_company_title": source_title,
            "source": "sec_company_tickers",
            "updated_at": utcnow().isoformat(),
        }
    )
    meta["sec_identity"] = sec_meta
    issuer.extra = meta
    issuer.updated_at = utcnow()
    session.flush()
    return "created", int(row.id)


def _upsert_issuer_identifier(
    session: Session,
    *,
    issuer: Issuer,
    id_type: str,
    id_value: str,
    exchange: str | None = None,
) -> tuple[str, int | None]:
    id_type_v = str(id_type or "").strip().upper()
    id_value_v = str(id_value or "").strip()
    if not id_type_v or not id_value_v:
        return "missing", None
    existing = session.query(IssuerIdentifier).filter_by(id_type=id_type_v, id_value=id_value_v).first()
    if existing is not None:
        if int(existing.issuer_id) == int(issuer.id):
            return "existing", int(existing.id)
        return "conflict", int(existing.issuer_id)
    row = IssuerIdentifier(
        issuer_id=int(issuer.id),
        id_type=id_type_v,
        id_value=id_value_v,
        exchange=exchange,
        currency=None,
        is_primary=False,
        valid_from=None,
        valid_to=None,
        created_at=utcnow(),
    )
    session.add(row)
    session.flush()
    return "created", int(row.id)


def ensure_active_universe_sec_sources(
    session: Session,
    *,
    universe_slug: str | None = None,
    limit: int | None = None,
    reason: str = "active_universe_source_pack",
    ticker_map: dict[str, dict[str, str]] | None = None,
    browse_lookup_budget: int = 40,
) -> dict[str, Any]:
    """Register official SEC filing sources for active-universe companies with public SEC tickers."""
    q = (
        session.query(ResearchUniverseRow)
        .join(ResearchUniverse, ResearchUniverse.id == ResearchUniverseRow.universe_id)
        .filter(ResearchUniverse.status == "active", ResearchUniverseRow.archived_at.is_(None))
        .order_by(ResearchUniverse.sort_order.asc(), ResearchUniverseRow.sort_order.asc(), ResearchUniverseRow.id.asc())
    )
    if universe_slug:
        q = q.filter(ResearchUniverse.slug == str(universe_slug).strip())
    rows = q.all()
    if limit is not None:
        rows = rows[: max(1, int(limit))]

    candidate_rows: list[tuple[ResearchInstrument, Company | None, Issuer | None]] = []
    for row in rows:
        instrument = row.instrument
        if instrument is None:
            continue
        company = session.get(Company, int(instrument.company_id)) if instrument.company_id is not None else None
        issuer = session.get(Issuer, int(instrument.issuer_id)) if instrument.issuer_id is not None else None
        if issuer is None and company is not None and company.issuer_id is not None:
            issuer = session.get(Issuer, int(company.issuer_id))
        if issuer is None:
            continue
        if _active_universe_sec_candidate(instrument, company, issuer):
            candidate_rows.append((instrument, company, issuer))

    if not candidate_rows:
        return {
            "registered": False,
            "reason": "no_sec_candidate_rows",
            "rows_scanned": len(rows),
            "sec_candidate_count": 0,
        }

    sec_map = ticker_map if ticker_map is not None else fetch_sec_company_ticker_map()
    ciks: list[str] = []
    company_slug_by_cik: dict[str, str] = {}
    ticker_by_cik: dict[str, str | None] = {}
    company_id_by_cik: dict[str, int] = {}
    company_name_by_cik: dict[str, str] = {}
    created = existing = conflicts = missing = 0
    browse_lookup_attempted = browse_lookup_resolved = browse_lookup_errors = 0
    examples: list[dict[str, Any]] = []
    seen_pairs: set[tuple[int, str]] = set()
    for instrument, company, issuer in candidate_rows:
        ticker = _normalize_sec_ticker(getattr(instrument, "ticker", None) or getattr(company, "ticker", None))
        source_title: str | None = None
        matched_ciks = _issuer_ciks(issuer)
        if not matched_ciks:
            match: dict[str, str] | None = None
            for candidate in _sec_ticker_candidates(ticker):
                match = sec_map.get(candidate)
                if match is not None:
                    ticker = candidate
                    break
            if match is None and browse_lookup_attempted < max(0, int(browse_lookup_budget)):
                lookup_names = [
                    getattr(issuer, "legal_name", None),
                    getattr(issuer, "display_name", None),
                    getattr(company, "name", None) if company is not None else None,
                    getattr(instrument, "legal_name", None),
                    getattr(instrument, "display_name", None),
                ]
                for candidate in _sec_ticker_candidates(ticker):
                    if browse_lookup_attempted >= max(0, int(browse_lookup_budget)):
                        break
                    browse_lookup_attempted += 1
                    try:
                        match = fetch_sec_browse_company_lookup(candidate, names=lookup_names)
                    except Exception as exc:
                        browse_lookup_errors += 1
                        if len(examples) < 8:
                            examples.append(
                                {
                                    "instrument_id": int(instrument.id),
                                    "display_name": instrument.display_name,
                                    "ticker": candidate,
                                    "exchange": instrument.exchange,
                                    "reason": "sec_browse_lookup_error",
                                    "error": f"{type(exc).__name__}: {exc}",
                                }
                            )
                        match = None
                    if match is not None:
                        ticker = candidate
                        browse_lookup_resolved += 1
                        break
            if match is None:
                missing += 1
                if len(examples) < 8:
                    examples.append(
                        {
                            "instrument_id": int(instrument.id),
                            "display_name": instrument.display_name,
                            "ticker": ticker,
                            "exchange": instrument.exchange,
                            "reason": "ticker_not_in_sec_company_tickers",
                        }
                    )
                continue
            cik = _normalize_sec_cik(match.get("cik"))
            if not cik:
                missing += 1
                continue
            matched_ciks = [cik]
            source_title = match.get("title")
        elif ticker:
            for candidate in _sec_ticker_candidates(ticker):
                match = sec_map.get(candidate)
                if match is not None:
                    ticker = candidate
                    source_title = match.get("title")
                    break
        for cik in matched_ciks:
            pair = (int(issuer.id), cik)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            status, related_id = _upsert_issuer_cik_identifier(
                session,
                issuer=issuer,
                cik=cik,
                ticker=ticker,
                source_title=source_title,
            )
            if status == "created":
                created += 1
            elif status == "existing":
                existing += 1
            else:
                conflicts += 1
                if len(examples) < 8:
                    examples.append(
                        {
                            "instrument_id": int(instrument.id),
                            "display_name": instrument.display_name,
                            "ticker": ticker,
                            "cik": cik,
                            "reason": "cik_already_bound_to_other_issuer",
                            "other_issuer_id": related_id,
                        }
                    )
                continue
            ciks.append(cik)
            if company is not None:
                company_slug_by_cik[cik] = company.slug
                if company.id is not None:
                    company_id_by_cik[cik] = int(company.id)
            ticker_by_cik[cik] = ticker or getattr(company, "ticker", None)
            company_name_by_cik[cik] = str(source_title or getattr(issuer, "display_name", None) or getattr(company, "name", "")).strip()

    edgar_source = ensure_sec_edgar_source_for_ciks(
        session,
        ciks=ciks,
        reason=reason,
        company_slug_by_cik=company_slug_by_cik,
        ticker_by_cik=ticker_by_cik,
        company_id_by_cik=company_id_by_cik,
        company_name_by_cik=company_name_by_cik,
    )
    companyfacts_source = ensure_sec_companyfacts_source_for_ciks(
        session,
        ciks=ciks,
        reason=reason,
        company_slug_by_cik=company_slug_by_cik,
        ticker_by_cik=ticker_by_cik,
        company_id_by_cik=company_id_by_cik,
    )
    return {
        "registered": bool(ciks),
        "rows_scanned": len(rows),
        "sec_candidate_count": len(candidate_rows),
        "ciks_registered": len(set(ciks)),
        "cik_identifiers_created": created,
        "cik_identifiers_existing": existing,
        "cik_identifier_conflicts": conflicts,
        "missing_ticker_matches": missing,
        "sec_browse_lookup_attempted": browse_lookup_attempted,
        "sec_browse_lookup_resolved": browse_lookup_resolved,
        "sec_browse_lookup_errors": browse_lookup_errors,
        "sec_edgar_source": edgar_source,
        "sec_companyfacts_source": companyfacts_source,
        "examples": examples,
    }


def ensure_active_universe_canada_sources(
    session: Session,
    *,
    universe_slug: str | None = None,
    limit: int | None = None,
    reason: str = "active_universe_source_pack",
) -> dict[str, Any]:
    """Register official SEDAR+ search bindings for active Canadian universe rows."""
    q = (
        session.query(ResearchUniverseRow)
        .join(ResearchUniverse, ResearchUniverse.id == ResearchUniverseRow.universe_id)
        .filter(ResearchUniverse.status == "active", ResearchUniverseRow.archived_at.is_(None))
        .order_by(ResearchUniverse.sort_order.asc(), ResearchUniverseRow.sort_order.asc(), ResearchUniverseRow.id.asc())
    )
    if universe_slug:
        q = q.filter(ResearchUniverse.slug == str(universe_slug).strip())
    rows = q.all()
    if limit is not None:
        rows = rows[: max(1, int(limit))]

    candidates: list[tuple[ResearchInstrument, Company | None, Issuer, list[tuple[str, str | None]]]] = []
    for row in rows:
        instrument = row.instrument
        if instrument is None or not bool(getattr(instrument, "is_public", True)):
            continue
        company = session.get(Company, int(instrument.company_id)) if instrument.company_id is not None else None
        issuer = session.get(Issuer, int(instrument.issuer_id)) if instrument.issuer_id is not None else None
        if issuer is None and company is not None and company.issuer_id is not None:
            issuer = session.get(Issuer, int(company.issuer_id))
        if issuer is None:
            continue
        stock_candidates = _ca_stock_candidates(instrument, company, issuer)
        country = str(getattr(instrument, "country", None) or getattr(company, "region", None) or getattr(issuer, "jurisdiction", None) or "").strip().upper()
        currency = str(getattr(instrument, "currency", None) or "").strip().upper()
        exchange = _normalize_exchange_code(getattr(instrument, "exchange", None) or getattr(company, "exchange", None))
        is_canadian_exchange = exchange in {"TSX", "TSXV", "CSE", "CNSX", "NEO"} or (
            exchange == "TSE" and (country in {"CA", "CANADA"} or currency == "CAD")
        )
        if stock_candidates or country in {"CA", "CANADA"} or is_canadian_exchange:
            candidates.append((instrument, company, issuer, stock_candidates))

    if not candidates:
        return {"registered": False, "reason": "no_canadian_candidate_rows", "rows_scanned": len(rows), "candidate_count": 0}

    ca_stock_codes: list[str] = []
    profile_names: list[str] = []
    company_id_by_code: dict[str, int] = {}
    company_slug_by_code: dict[str, str] = {}
    issuer_id_by_code: dict[str, int] = {}
    issuer_name_by_code: dict[str, str] = {}
    exchange_by_code: dict[str, str] = {}
    valid_pairs: set[tuple[int, str]] = set()
    created = existing = conflicts = missing = 0
    examples: list[dict[str, Any]] = []
    seen_issuer_code: set[tuple[int, str]] = set()
    for instrument, company, issuer, stock_candidates in candidates:
        existing_codes = _issuer_identifier_values(issuer, "CA_STOCK")
        merged_candidates = list(stock_candidates)
        for code in existing_codes:
            normalized = _normalize_ca_stock_code(code)
            if normalized and all(normalized != row[0] for row in merged_candidates):
                merged_candidates.append((normalized, None))
        if not merged_candidates:
            missing += 1
            if len(examples) < 8:
                examples.append(
                    {
                        "instrument_id": int(instrument.id),
                        "display_name": instrument.display_name,
                        "ticker": instrument.ticker,
                        "exchange": instrument.exchange,
                        "reason": "missing_canadian_stock_code",
                    }
                )
            continue
        profile_name = str(getattr(issuer, "legal_name", None) or getattr(company, "name", None) or instrument.display_name or "").strip()
        if profile_name:
            profile_names.append(profile_name)
        for stock_code, exchange in merged_candidates:
            pair = (int(issuer.id), stock_code)
            if pair in seen_issuer_code:
                continue
            seen_issuer_code.add(pair)
            status, related_id = _upsert_issuer_identifier(
                session,
                issuer=issuer,
                id_type="CA_STOCK",
                id_value=stock_code,
                exchange=exchange or "TSX",
            )
            if status == "created":
                created += 1
            elif status == "existing":
                existing += 1
            else:
                conflicts += 1
                if len(examples) < 8:
                    examples.append(
                        {
                            "instrument_id": int(instrument.id),
                            "display_name": instrument.display_name,
                            "stock_code": stock_code,
                            "reason": "ca_stock_already_bound_to_other_issuer",
                            "other_issuer_id": related_id,
                        }
                    )
                continue
            ca_stock_codes.append(stock_code)
            valid_pairs.add((int(issuer.id), stock_code))
            issuer_id_by_code[stock_code] = int(issuer.id)
            issuer_name_by_code[stock_code] = str(getattr(issuer, "display_name", None) or profile_name or instrument.display_name).strip()
            if exchange:
                exchange_by_code[stock_code] = exchange
            if company is not None:
                company_slug_by_code[stock_code] = company.slug
                if company.id is not None:
                    company_id_by_code[stock_code] = int(company.id)

    normalized_codes = sorted(set(ca_stock_codes))
    if not normalized_codes:
        return {
            "registered": False,
            "reason": "no_ca_stock_codes",
            "rows_scanned": len(rows),
            "candidate_count": len(candidates),
            "missing_stock_codes": missing,
            "examples": examples,
        }

    source = session.query(Source).filter_by(name=_SEDARPLUS_ACTIVE_SOURCE_NAME).first()
    if source is None:
        source = Source(
            name=_SEDARPLUS_ACTIVE_SOURCE_NAME,
            category="filings",
            priority=1,
            polling_interval_seconds=43200,
            config={},
            created_at=utcnow(),
        )
        session.add(source)
        session.flush()
    cfg = dict(source.config or {})
    existing_source_codes = [str(value).strip().upper() for value in (cfg.get("ca_stock_codes") or []) if str(value).strip()]
    merged_codes = sorted(set(normalized_codes) if limit is None else set(existing_source_codes).union(normalized_codes))
    added = [code for code in normalized_codes if code not in set(existing_source_codes)]
    removed = sorted(set(existing_source_codes) - set(normalized_codes)) if limit is None else []
    pruned_identifiers = 0
    if removed:
        stale_identifiers = (
            session.query(IssuerIdentifier)
            .filter(IssuerIdentifier.id_type == "CA_STOCK", IssuerIdentifier.id_value.in_(removed))
            .all()
        )
        for identifier in stale_identifiers:
            issuer_id = int(identifier.issuer_id)
            stock_code = str(identifier.id_value or "").strip().upper()
            if (issuer_id, stock_code) in valid_pairs:
                continue
            holder_issuer = session.get(Issuer, issuer_id)
            holder_company = session.query(Company).filter_by(issuer_id=issuer_id).order_by(Company.id.asc()).first()
            holder_country = str(
                getattr(holder_company, "region", None) or getattr(holder_issuer, "jurisdiction", None) or ""
            ).strip().upper()
            if holder_country in {"CA", "CANADA"}:
                continue
            session.delete(identifier)
            pruned_identifiers += 1
    try:
        from thoth_terminal.connectors.sedarplus_search import SEDARPLUS_DOCUMENT_SEARCH_URL
    except Exception:
        SEDARPLUS_DOCUMENT_SEARCH_URL = "https://www.sedarplus.ca/csa-party/service/create.html?_locale=en&service=searchDocuments&targetAppCode=csa-party"
    existing_profile_names = [str(value).strip() for value in (cfg.get("profile_names") or []) if str(value).strip()]

    def merged_mapping(key: str, current: dict[str, Any]) -> dict[str, Any]:
        existing = cfg.get(key) if isinstance(cfg.get(key), dict) else {}
        return dict(current) if limit is None else {**existing, **current}

    cfg.update(
        {
            "connector": "sedarplus_search",
            "enabled": True,
            "managed_by": "source_pack",
            "coverage_scope": "active_universe",
            "authority": "primary_regulator",
            "filing_system": "SEDAR+",
            "official_search_url": SEDARPLUS_DOCUMENT_SEARCH_URL,
            "ca_stock_codes": merged_codes,
            "profile_names": sorted(set(profile_names) if limit is None else set(existing_profile_names).union(profile_names)),
            "company_id_by_ca_stock_code": merged_mapping("company_id_by_ca_stock_code", company_id_by_code),
            "company_slug_by_ca_stock_code": merged_mapping("company_slug_by_ca_stock_code", company_slug_by_code),
            "issuer_id_by_ca_stock_code": merged_mapping("issuer_id_by_ca_stock_code", issuer_id_by_code),
            "issuer_name_by_ca_stock_code": merged_mapping("issuer_name_by_ca_stock_code", issuer_name_by_code),
            "exchange_by_ca_stock_code": merged_mapping("exchange_by_ca_stock_code", exchange_by_code),
            "last_registered_reason": reason,
            "source_quality": {
                **(cfg.get("source_quality") if isinstance(cfg.get("source_quality"), dict) else {}),
                "authority": "primary_regulator",
                "timeliness": "search_binding",
                "acquisition_fragility": "medium",
                "rights_class": "public",
                "materialization_status": "official_search_binding_only",
            },
        }
    )
    source.config = cfg
    session.flush()
    state = session.query(SourceState).filter_by(source_id=int(source.id)).first()
    return {
        "registered": True,
        "rows_scanned": len(rows),
        "candidate_count": len(candidates),
        "ca_stock_codes_registered": len(merged_codes),
        "ca_stock_codes_added": added,
        "ca_stock_codes_removed": removed,
        "ca_identifiers_created": created,
        "ca_identifiers_existing": existing,
        "ca_identifier_conflicts": conflicts,
        "ca_identifiers_pruned": pruned_identifiers,
        "missing_stock_codes": missing,
        "source": {
            "source_id": int(source.id),
            "source_name": source.name,
            "connector": "sedarplus_search",
            "ca_stock_code_count": len(merged_codes),
            "should_enqueue": bool(added) or state is None or state.last_success_at is None,
            "official_search_url": SEDARPLUS_DOCUMENT_SEARCH_URL,
        },
        "examples": examples,
    }


def ensure_active_universe_asx_sources(
    session: Session,
    *,
    universe_slug: str | None = None,
    limit: int | None = None,
    reason: str = "active_universe_source_pack",
) -> dict[str, Any]:
    """Refresh ASX official announcement source from active Australian universe rows."""
    q = (
        session.query(ResearchUniverseRow)
        .join(ResearchUniverse, ResearchUniverse.id == ResearchUniverseRow.universe_id)
        .filter(ResearchUniverse.status == "active", ResearchUniverseRow.archived_at.is_(None))
        .order_by(ResearchUniverse.sort_order.asc(), ResearchUniverseRow.sort_order.asc(), ResearchUniverseRow.id.asc())
    )
    if universe_slug:
        q = q.filter(ResearchUniverse.slug == str(universe_slug).strip())
    rows = q.all()
    if limit is not None:
        rows = rows[: max(1, int(limit))]

    stock_codes: set[str] = set()
    keywords: set[str] = set()
    company_id_by_stock_code: dict[str, int] = {}
    company_slug_by_stock_code: dict[str, str] = {}
    ticker_by_stock_code: dict[str, str] = {}
    company_name_by_stock_code: dict[str, str] = {}
    candidates = 0
    identifiers_created = identifiers_existing = identifiers_conflicts = missing = 0
    examples: list[dict[str, Any]] = []
    seen_issuer_code: set[tuple[int, str]] = set()
    for row in rows:
        instrument = row.instrument
        if instrument is None or not bool(getattr(instrument, "is_public", True)):
            continue
        company = session.get(Company, int(instrument.company_id)) if instrument.company_id is not None else None
        issuer = session.get(Issuer, int(instrument.issuer_id)) if instrument.issuer_id is not None else None
        if issuer is None and company is not None and company.issuer_id is not None:
            issuer = session.get(Issuer, int(company.issuer_id))
        exchange = _normalize_exchange_code(getattr(instrument, "exchange", None) or getattr(company, "exchange", None))
        country = str(getattr(instrument, "country", None) or getattr(company, "region", None) or getattr(issuer, "jurisdiction", None) or "").strip().upper()
        ticker = getattr(instrument, "ticker", None) or getattr(company, "ticker", None) or getattr(instrument, "yahoo_symbol", None)
        yahoo = str(getattr(instrument, "yahoo_symbol", None) or "").strip().upper()
        stock_code = _normalize_asx_stock_code(ticker) or _normalize_asx_stock_code(yahoo)
        existing_codes = _issuer_identifier_values(issuer, "ASX_STOCK") if issuer is not None else []
        in_scope = exchange == "ASX" or country in {"AU", "AUS", "AUSTRALIA"} or yahoo.endswith(".AX") or bool(existing_codes)
        if not in_scope:
            continue
        candidates += 1
        row_codes = [stock_code] if stock_code else []
        for existing_code in existing_codes:
            normalized = _normalize_asx_stock_code(existing_code)
            if normalized and normalized not in row_codes:
                row_codes.append(normalized)
        if not row_codes:
            missing += 1
            if len(examples) < 8:
                examples.append(
                    {
                        "instrument_id": int(instrument.id),
                        "display_name": instrument.display_name,
                        "ticker": instrument.ticker,
                        "exchange": instrument.exchange,
                        "reason": "missing_asx_stock_code",
                    }
                )
            continue
        for code in row_codes:
            if issuer is not None:
                pair = (int(issuer.id), code)
                if pair not in seen_issuer_code:
                    seen_issuer_code.add(pair)
                    status, related_id = _upsert_issuer_identifier(
                        session,
                        issuer=issuer,
                        id_type="ASX_STOCK",
                        id_value=code,
                        exchange="ASX",
                    )
                    if status == "created":
                        identifiers_created += 1
                    elif status == "existing":
                        identifiers_existing += 1
                    elif status == "conflict":
                        identifiers_conflicts += 1
                        if len(examples) < 8:
                            examples.append(
                                {
                                    "instrument_id": int(instrument.id),
                                    "display_name": instrument.display_name,
                                    "stock_code": code,
                                    "reason": "asx_stock_already_bound_to_other_issuer",
                                    "other_issuer_id": related_id,
                                }
                            )
                        continue
            stock_codes.add(code)
            ticker_by_stock_code[code] = f"{code}.AX"
            for value in [
                getattr(instrument, "display_name", None),
                getattr(instrument, "legal_name", None),
                getattr(company, "name", None) if company is not None else None,
                getattr(issuer, "display_name", None) if issuer is not None else None,
                getattr(issuer, "legal_name", None) if issuer is not None else None,
                code,
            ]:
                text = re.sub(r"\s+", " ", str(value or "").strip())
                if text and len(text) >= 3:
                    keywords.add(text[:120])
            name = str(
                getattr(company, "name", None)
                or getattr(issuer, "display_name", None)
                or getattr(issuer, "legal_name", None)
                or instrument.display_name
                or ""
            ).strip()
            if name:
                company_name_by_stock_code[code] = name
            if company is not None:
                company_slug_by_stock_code[code] = company.slug
                if company.id is not None:
                    company_id_by_stock_code[code] = int(company.id)

    if not candidates:
        return {"registered": False, "reason": "no_asx_candidate_rows", "rows_scanned": len(rows), "candidate_count": 0}
    if not stock_codes:
        return {
            "registered": False,
            "reason": "no_asx_stock_codes",
            "rows_scanned": len(rows),
            "candidate_count": candidates,
            "missing_stock_codes": missing,
            "examples": examples,
        }

    source = session.query(Source).filter_by(name=_ASX_ACTIVE_SOURCE_NAME).first()
    if source is None:
        source = Source(
            name=_ASX_ACTIVE_SOURCE_NAME,
            category="exchange",
            priority=1,
            polling_interval_seconds=21600,
            config={},
            created_at=utcnow(),
        )
        session.add(source)
        session.flush()
    cfg = dict(source.config or {})

    def _merge_unique(existing: object, additions: set[str], *, limit_n: int) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for value in [*(existing if isinstance(existing, list) else []), *sorted(additions)]:
            text = str(value or "").strip().upper()
            if text and text not in seen:
                seen.add(text)
                out.append(text)
            if len(out) >= limit_n:
                break
        return out

    def _merge_map(key: str, updates: dict[str, Any]) -> dict[str, Any]:
        current = dict(cfg.get(key) if isinstance(cfg.get(key), dict) else {})
        for raw_code, value in updates.items():
            normalized = _normalize_asx_stock_code(raw_code)
            if normalized and value is not None and str(value).strip():
                current[normalized] = value
        return current

    existing_codes = {str(value).strip().upper() for value in (cfg.get("stock_codes") or []) if str(value).strip()} if isinstance(cfg.get("stock_codes"), list) else set()
    cfg.update(
        {
            "connector": "asx_announcements",
            "lookback_days": 180,
            "timeframe": "D",
            "period": "M6",
            "stock_codes_per_run": 4,
            "max_items_per_run": 120,
            "fetch_article": False,
            "download_article_images": False,
            "refresh_existing": True,
            "enabled": True,
            "managed_by": "source_pack",
            "coverage_scope": "active_universe",
            "last_registered_reason": reason,
            "stock_codes": _merge_unique(cfg.get("stock_codes"), stock_codes, limit_n=700),
            "include_keywords": _merge_unique(cfg.get("include_keywords"), keywords | stock_codes, limit_n=1200),
            "company_id_by_stock_code": _merge_map("company_id_by_stock_code", company_id_by_stock_code),
            "company_slug_by_stock_code": _merge_map("company_slug_by_stock_code", company_slug_by_stock_code),
            "ticker_by_stock_code": _merge_map("ticker_by_stock_code", ticker_by_stock_code),
            "company_name_by_stock_code": _merge_map("company_name_by_stock_code", company_name_by_stock_code),
            "source_quality": {
                **(cfg.get("source_quality") if isinstance(cfg.get("source_quality"), dict) else {}),
                "authority": "primary_exchange",
                "timeliness": "scheduled",
                "acquisition_fragility": "medium",
                "rights_class": "public",
            },
        }
    )
    source.config = cfg
    session.flush()
    after_codes = {str(value).strip().upper() for value in (cfg.get("stock_codes") or []) if str(value).strip()}
    added_codes = sorted(after_codes - existing_codes)
    if added_codes:
        state = session.query(SourceState).filter_by(source_id=int(source.id)).first()
        if state is not None:
            state.cursor = {}
    session.flush()
    return {
        "registered": True,
        "rows_scanned": len(rows),
        "candidate_count": candidates,
        "stock_codes_registered": len(stock_codes),
        "identifiers_created": identifiers_created,
        "identifiers_existing": identifiers_existing,
        "identifier_conflicts": identifiers_conflicts,
        "missing_stock_codes": missing,
        "asx_announcements_source": {
            "source_id": int(source.id),
            "source_name": source.name,
            "stock_code_count": len(cfg.get("stock_codes") or []),
            "stock_codes_added": len(added_codes),
        },
        "examples": examples,
    }


def ensure_active_universe_bursa_sources(
    session: Session,
    *,
    universe_slug: str | None = None,
    limit: int | None = None,
    reason: str = "active_universe_source_pack",
) -> dict[str, Any]:
    """Register Bursa Malaysia official announcement bindings for active Malaysian universe rows."""
    q = (
        session.query(ResearchUniverseRow)
        .join(ResearchUniverse, ResearchUniverse.id == ResearchUniverseRow.universe_id)
        .filter(ResearchUniverse.status == "active", ResearchUniverseRow.archived_at.is_(None))
        .order_by(ResearchUniverse.sort_order.asc(), ResearchUniverseRow.sort_order.asc(), ResearchUniverseRow.id.asc())
    )
    if universe_slug:
        q = q.filter(ResearchUniverse.slug == str(universe_slug).strip())
    rows = q.all()
    if limit is not None:
        rows = rows[: max(1, int(limit))]

    stock_codes: set[str] = set()
    issuer_names: set[str] = set()
    company_id_by_stock_code: dict[str, int] = {}
    company_slug_by_stock_code: dict[str, str] = {}
    issuer_id_by_stock_code: dict[str, int] = {}
    issuer_name_by_stock_code: dict[str, str] = {}
    ticker_by_stock_code: dict[str, str] = {}
    company_name_by_stock_code: dict[str, str] = {}
    candidates = 0
    identifiers_created = identifiers_existing = identifiers_conflicts = missing = 0
    examples: list[dict[str, Any]] = []
    seen_issuer_code: set[tuple[int, str]] = set()
    for row in rows:
        instrument = row.instrument
        if instrument is None or not bool(getattr(instrument, "is_public", True)):
            continue
        company = session.get(Company, int(instrument.company_id)) if instrument.company_id is not None else None
        issuer = session.get(Issuer, int(instrument.issuer_id)) if instrument.issuer_id is not None else None
        if issuer is None and company is not None and company.issuer_id is not None:
            issuer = session.get(Issuer, int(company.issuer_id))
        exchange = _normalize_exchange_code(getattr(instrument, "exchange", None) or getattr(company, "exchange", None))
        country = str(getattr(instrument, "country", None) or getattr(company, "region", None) or getattr(issuer, "jurisdiction", None) or "").strip().upper()
        ticker = getattr(instrument, "ticker", None) or getattr(company, "ticker", None) or getattr(instrument, "yahoo_symbol", None)
        yahoo = str(getattr(instrument, "yahoo_symbol", None) or "").strip().upper()
        stock_code = _normalize_bursa_stock_code(ticker) or _normalize_bursa_stock_code(yahoo)
        existing_codes = _issuer_identifier_values(issuer, "MY_STOCK") if issuer is not None else []
        in_scope = exchange in {"BURSA", "KLSE", "MYX"} or country in {"MY", "MYS", "MALAYSIA"} or yahoo.endswith(".KL") or bool(existing_codes)
        if not in_scope:
            continue
        candidates += 1
        row_codes = [stock_code] if stock_code else []
        for existing_code in existing_codes:
            normalized = _normalize_bursa_stock_code(existing_code)
            if normalized and normalized not in row_codes:
                row_codes.append(normalized)
        if not row_codes:
            missing += 1
            if len(examples) < 8:
                examples.append(
                    {
                        "instrument_id": int(instrument.id),
                        "display_name": instrument.display_name,
                        "ticker": instrument.ticker,
                        "exchange": instrument.exchange,
                        "reason": "missing_bursa_stock_code",
                    }
                )
            continue
        name = str(
            getattr(company, "name", None)
            or getattr(issuer, "display_name", None)
            or getattr(issuer, "legal_name", None)
            or instrument.display_name
            or ""
        ).strip()
        if name:
            issuer_names.add(name)
        for code in row_codes:
            if issuer is not None:
                pair = (int(issuer.id), code)
                if pair not in seen_issuer_code:
                    seen_issuer_code.add(pair)
                    status, related_id = _upsert_issuer_identifier(
                        session,
                        issuer=issuer,
                        id_type="MY_STOCK",
                        id_value=code,
                        exchange="BURSA",
                    )
                    if status == "created":
                        identifiers_created += 1
                    elif status == "existing":
                        identifiers_existing += 1
                    elif status == "conflict":
                        identifiers_conflicts += 1
                        if len(examples) < 8:
                            examples.append(
                                {
                                    "instrument_id": int(instrument.id),
                                    "display_name": instrument.display_name,
                                    "stock_code": code,
                                    "reason": "my_stock_already_bound_to_other_issuer",
                                    "other_issuer_id": related_id,
                                }
                            )
                        continue
            stock_codes.add(code)
            ticker_by_stock_code[code] = f"{code}.KL"
            if name:
                company_name_by_stock_code[code] = name
            if issuer is not None:
                issuer_id_by_stock_code[code] = int(issuer.id)
                issuer_name_by_stock_code[code] = str(getattr(issuer, "display_name", None) or getattr(issuer, "legal_name", None) or name).strip()
            if company is not None:
                company_slug_by_stock_code[code] = company.slug
                if company.id is not None:
                    company_id_by_stock_code[code] = int(company.id)

    if not candidates:
        return {"registered": False, "reason": "no_bursa_candidate_rows", "rows_scanned": len(rows), "candidate_count": 0}
    if not stock_codes:
        return {
            "registered": False,
            "reason": "no_bursa_stock_codes",
            "rows_scanned": len(rows),
            "candidate_count": candidates,
            "missing_stock_codes": missing,
            "examples": examples,
        }

    source = session.query(Source).filter_by(name=_BURSA_ACTIVE_SOURCE_NAME).first()
    if source is None:
        source = Source(
            name=_BURSA_ACTIVE_SOURCE_NAME,
            category="exchange",
            priority=1,
            polling_interval_seconds=43200,
            config={},
            created_at=utcnow(),
        )
        session.add(source)
        session.flush()
    cfg = dict(source.config or {})

    def _merge_unique(existing: object, additions: set[str], *, limit_n: int) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for value in [*(existing if isinstance(existing, list) else []), *sorted(additions)]:
            text = str(value or "").strip().upper()
            if text and text not in seen:
                seen.add(text)
                out.append(text)
            if len(out) >= limit_n:
                break
        return out

    def _merge_unique_text(existing: object, additions: set[str], *, limit_n: int) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for value in [*(existing if isinstance(existing, list) else []), *sorted(additions)]:
            text = re.sub(r"\s+", " ", str(value or "").strip())
            key = text.upper()
            if text and key not in seen:
                seen.add(key)
                out.append(text)
            if len(out) >= limit_n:
                break
        return out

    def _merge_map(key: str, updates: dict[str, Any]) -> dict[str, Any]:
        current = dict(cfg.get(key) if isinstance(cfg.get(key), dict) else {})
        for raw_code, value in updates.items():
            normalized = _normalize_bursa_stock_code(raw_code)
            if normalized and value is not None and str(value).strip():
                current[normalized] = value
        return current

    try:
        from thoth_terminal.connectors.bursa_announcements import (
            BURSA_COMPANY_ANNOUNCEMENTS_URL,
            BURSA_LISTING_DIRECTORY_URL,
        )
    except Exception:
        BURSA_COMPANY_ANNOUNCEMENTS_URL = "https://www.bursamalaysia.com/market_information/announcements/company_announcement"
        BURSA_LISTING_DIRECTORY_URL = "https://www.bursamalaysia.com/trade/trading_resources/listing_directory/company-profile"

    existing_codes = {str(value).strip().upper() for value in (cfg.get("stock_codes") or []) if str(value).strip()} if isinstance(cfg.get("stock_codes"), list) else set()
    cfg.update(
        {
            "connector": "bursa_announcements",
            "enabled": True,
            "managed_by": "source_pack",
            "coverage_scope": "active_universe",
            "authority": "primary_exchange",
            "filing_system": "Bursa Malaysia",
            "official_search_url": BURSA_COMPANY_ANNOUNCEMENTS_URL,
            "listing_directory_url": BURSA_LISTING_DIRECTORY_URL,
            "stock_codes": _merge_unique(cfg.get("stock_codes"), stock_codes, limit_n=700),
            "my_stock_codes": _merge_unique(cfg.get("my_stock_codes"), stock_codes, limit_n=700),
            "bursa_stock_codes": _merge_unique(cfg.get("bursa_stock_codes"), stock_codes, limit_n=700),
            "issuer_names": _merge_unique_text(cfg.get("issuer_names"), issuer_names, limit_n=700),
            "company_id_by_stock_code": _merge_map("company_id_by_stock_code", company_id_by_stock_code),
            "company_slug_by_stock_code": _merge_map("company_slug_by_stock_code", company_slug_by_stock_code),
            "issuer_id_by_stock_code": _merge_map("issuer_id_by_stock_code", issuer_id_by_stock_code),
            "issuer_name_by_stock_code": _merge_map("issuer_name_by_stock_code", issuer_name_by_stock_code),
            "ticker_by_stock_code": _merge_map("ticker_by_stock_code", ticker_by_stock_code),
            "company_name_by_stock_code": _merge_map("company_name_by_stock_code", company_name_by_stock_code),
            "last_registered_reason": reason,
            "source_quality": {
                **(cfg.get("source_quality") if isinstance(cfg.get("source_quality"), dict) else {}),
                "authority": "primary_exchange",
                "timeliness": "search_binding",
                "acquisition_fragility": "high",
                "rights_class": "public",
                "materialization_status": "official_search_binding_only",
            },
        }
    )
    source.config = cfg
    session.flush()
    after_codes = {str(value).strip().upper() for value in (cfg.get("stock_codes") or []) if str(value).strip()}
    added_codes = sorted(after_codes - existing_codes)
    if added_codes:
        state = session.query(SourceState).filter_by(source_id=int(source.id)).first()
        if state is not None:
            state.cursor = {}
    session.flush()
    return {
        "registered": True,
        "rows_scanned": len(rows),
        "candidate_count": candidates,
        "stock_codes_registered": len(stock_codes),
        "identifiers_created": identifiers_created,
        "identifiers_existing": identifiers_existing,
        "identifier_conflicts": identifiers_conflicts,
        "missing_stock_codes": missing,
        "bursa_announcements_source": {
            "source_id": int(source.id),
            "source_name": source.name,
            "stock_code_count": len(cfg.get("stock_codes") or []),
            "stock_codes_added": len(added_codes),
            "official_search_url": BURSA_COMPANY_ANNOUNCEMENTS_URL,
        },
        "examples": examples,
    }


def ensure_active_universe_kazakhstan_sources(
    session: Session,
    *,
    universe_slug: str | None = None,
    limit: int | None = None,
    reason: str = "active_universe_source_pack",
) -> dict[str, Any]:
    """Register AIX/KASE official announcement bindings for active Kazakhstan universe rows."""
    q = (
        session.query(ResearchUniverseRow)
        .join(ResearchUniverse, ResearchUniverse.id == ResearchUniverseRow.universe_id)
        .filter(ResearchUniverse.status == "active", ResearchUniverseRow.archived_at.is_(None))
        .order_by(ResearchUniverse.sort_order.asc(), ResearchUniverseRow.sort_order.asc(), ResearchUniverseRow.id.asc())
    )
    if universe_slug:
        q = q.filter(ResearchUniverse.slug == str(universe_slug).strip())
    rows = q.all()
    if limit is not None:
        rows = rows[: max(1, int(limit))]

    stock_codes: set[str] = set()
    issuer_names: set[str] = set()
    company_id_by_stock_code: dict[str, int] = {}
    company_slug_by_stock_code: dict[str, str] = {}
    issuer_id_by_stock_code: dict[str, int] = {}
    issuer_name_by_stock_code: dict[str, str] = {}
    ticker_by_stock_code: dict[str, str] = {}
    company_name_by_stock_code: dict[str, str] = {}
    candidates = 0
    identifiers_created = identifiers_existing = identifiers_conflicts = missing = 0
    examples: list[dict[str, Any]] = []
    seen_issuer_code: set[tuple[int, str]] = set()
    for row in rows:
        instrument = row.instrument
        if instrument is None or not bool(getattr(instrument, "is_public", True)):
            continue
        company = session.get(Company, int(instrument.company_id)) if instrument.company_id is not None else None
        issuer = session.get(Issuer, int(instrument.issuer_id)) if instrument.issuer_id is not None else None
        if issuer is None and company is not None and company.issuer_id is not None:
            issuer = session.get(Issuer, int(company.issuer_id))
        exchange = _normalize_exchange_code(getattr(instrument, "exchange", None) or getattr(company, "exchange", None))
        country = str(getattr(instrument, "country", None) or getattr(company, "region", None) or getattr(issuer, "jurisdiction", None) or "").strip().upper()
        ticker = getattr(instrument, "ticker", None) or getattr(company, "ticker", None) or getattr(instrument, "yahoo_symbol", None)
        yahoo = str(getattr(instrument, "yahoo_symbol", None) or "").strip().upper()
        stock_code = _normalize_kz_stock_code(ticker) or _normalize_kz_stock_code(yahoo)
        existing_codes = _issuer_identifier_values(issuer, "KZ_STOCK") if issuer is not None else []
        in_scope = exchange in {"AIX", "KASE", "KASEKZ", "KZ"} or country in {"KZ", "KAZ", "KAZAKHSTAN"} or yahoo.endswith(".KZ") or bool(existing_codes)
        if not in_scope:
            continue
        candidates += 1
        row_codes = [stock_code] if stock_code else []
        for existing_code in existing_codes:
            normalized = _normalize_kz_stock_code(existing_code)
            if normalized and normalized not in row_codes:
                row_codes.append(normalized)
        if not row_codes:
            missing += 1
            if len(examples) < 8:
                examples.append(
                    {
                        "instrument_id": int(instrument.id),
                        "display_name": instrument.display_name,
                        "ticker": instrument.ticker,
                        "exchange": instrument.exchange,
                        "reason": "missing_kazakhstan_stock_code",
                    }
                )
            continue
        name = str(
            getattr(company, "name", None)
            or getattr(issuer, "display_name", None)
            or getattr(issuer, "legal_name", None)
            or instrument.display_name
            or ""
        ).strip()
        if name:
            issuer_names.add(name)
        for code in row_codes:
            if issuer is not None:
                pair = (int(issuer.id), code)
                if pair not in seen_issuer_code:
                    seen_issuer_code.add(pair)
                    status, related_id = _upsert_issuer_identifier(
                        session,
                        issuer=issuer,
                        id_type="KZ_STOCK",
                        id_value=code,
                        exchange="AIX_KASE",
                    )
                    if status == "created":
                        identifiers_created += 1
                    elif status == "existing":
                        identifiers_existing += 1
                    elif status == "conflict":
                        identifiers_conflicts += 1
                        if len(examples) < 8:
                            examples.append(
                                {
                                    "instrument_id": int(instrument.id),
                                    "display_name": instrument.display_name,
                                    "stock_code": code,
                                    "reason": "kz_stock_already_bound_to_other_issuer",
                                    "other_issuer_id": related_id,
                                }
                            )
                        continue
            stock_codes.add(code)
            ticker_by_stock_code[code] = f"{code}.KZ"
            if name:
                company_name_by_stock_code[code] = name
            if issuer is not None:
                issuer_id_by_stock_code[code] = int(issuer.id)
                issuer_name_by_stock_code[code] = str(getattr(issuer, "display_name", None) or getattr(issuer, "legal_name", None) or name).strip()
            if company is not None:
                company_slug_by_stock_code[code] = company.slug
                if company.id is not None:
                    company_id_by_stock_code[code] = int(company.id)

    if not candidates:
        return {"registered": False, "reason": "no_kazakhstan_candidate_rows", "rows_scanned": len(rows), "candidate_count": 0}
    if not stock_codes:
        return {
            "registered": False,
            "reason": "no_kazakhstan_stock_codes",
            "rows_scanned": len(rows),
            "candidate_count": candidates,
            "missing_stock_codes": missing,
            "examples": examples,
        }

    source = session.query(Source).filter_by(name=_KAZAKHSTAN_ACTIVE_SOURCE_NAME).first()
    if source is None:
        source = Source(
            name=_KAZAKHSTAN_ACTIVE_SOURCE_NAME,
            category="exchange",
            priority=1,
            polling_interval_seconds=43200,
            config={},
            created_at=utcnow(),
        )
        session.add(source)
        session.flush()
    cfg = dict(source.config or {})

    def _merge_unique(existing: object, additions: set[str], *, limit_n: int) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for value in [*(existing if isinstance(existing, list) else []), *sorted(additions)]:
            text = str(value or "").strip().upper()
            if text and text not in seen:
                seen.add(text)
                out.append(text)
            if len(out) >= limit_n:
                break
        return out

    def _merge_unique_text(existing: object, additions: set[str], *, limit_n: int) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for value in [*(existing if isinstance(existing, list) else []), *sorted(additions)]:
            text = re.sub(r"\s+", " ", str(value or "").strip())
            key = text.upper()
            if text and key not in seen:
                seen.add(key)
                out.append(text)
            if len(out) >= limit_n:
                break
        return out

    def _merge_map(key: str, updates: dict[str, Any]) -> dict[str, Any]:
        current = dict(cfg.get(key) if isinstance(cfg.get(key), dict) else {})
        for raw_code, value in updates.items():
            normalized = _normalize_kz_stock_code(raw_code)
            if normalized and value is not None and str(value).strip():
                current[normalized] = value
        return current

    try:
        from thoth_terminal.connectors.kazakhstan_announcements import (
            AIX_ISSUERS_URL,
            KASE_ISSUERS_URL,
            KASE_NEWS_URL,
        )
    except Exception:
        AIX_ISSUERS_URL = "https://aix.kz/issuers/"
        KASE_ISSUERS_URL = "https://kase.kz/en/issuers/"
        KASE_NEWS_URL = "https://kase.kz/en/information/news/"

    existing_codes = {str(value).strip().upper() for value in (cfg.get("stock_codes") or []) if str(value).strip()} if isinstance(cfg.get("stock_codes"), list) else set()
    cfg.update(
        {
            "connector": "kazakhstan_announcements",
            "enabled": True,
            "managed_by": "source_pack",
            "coverage_scope": "active_universe",
            "authority": "primary_exchange",
            "filing_system": "AIX/KASE",
            "aix_issuers_url": AIX_ISSUERS_URL,
            "kase_issuers_url": KASE_ISSUERS_URL,
            "kase_news_url": KASE_NEWS_URL,
            "stock_codes": _merge_unique(cfg.get("stock_codes"), stock_codes, limit_n=700),
            "kz_stock_codes": _merge_unique(cfg.get("kz_stock_codes"), stock_codes, limit_n=700),
            "aix_symbols": _merge_unique(cfg.get("aix_symbols"), stock_codes, limit_n=700),
            "issuer_names": _merge_unique_text(cfg.get("issuer_names"), issuer_names, limit_n=700),
            "company_id_by_stock_code": _merge_map("company_id_by_stock_code", company_id_by_stock_code),
            "company_slug_by_stock_code": _merge_map("company_slug_by_stock_code", company_slug_by_stock_code),
            "issuer_id_by_stock_code": _merge_map("issuer_id_by_stock_code", issuer_id_by_stock_code),
            "issuer_name_by_stock_code": _merge_map("issuer_name_by_stock_code", issuer_name_by_stock_code),
            "ticker_by_stock_code": _merge_map("ticker_by_stock_code", ticker_by_stock_code),
            "company_name_by_stock_code": _merge_map("company_name_by_stock_code", company_name_by_stock_code),
            "last_registered_reason": reason,
            "source_quality": {
                **(cfg.get("source_quality") if isinstance(cfg.get("source_quality"), dict) else {}),
                "authority": "primary_exchange",
                "timeliness": "search_binding",
                "acquisition_fragility": "high",
                "rights_class": "public",
                "materialization_status": "official_search_binding_only",
            },
        }
    )
    source.config = cfg
    session.flush()
    after_codes = {str(value).strip().upper() for value in (cfg.get("stock_codes") or []) if str(value).strip()}
    added_codes = sorted(after_codes - existing_codes)
    if added_codes:
        state = session.query(SourceState).filter_by(source_id=int(source.id)).first()
        if state is not None:
            state.cursor = {}
    session.flush()
    return {
        "registered": True,
        "rows_scanned": len(rows),
        "candidate_count": candidates,
        "stock_codes_registered": len(stock_codes),
        "identifiers_created": identifiers_created,
        "identifiers_existing": identifiers_existing,
        "identifier_conflicts": identifiers_conflicts,
        "missing_stock_codes": missing,
        "kazakhstan_announcements_source": {
            "source_id": int(source.id),
            "source_name": source.name,
            "stock_code_count": len(cfg.get("stock_codes") or []),
            "stock_codes_added": len(added_codes),
            "aix_issuers_url": AIX_ISSUERS_URL,
            "kase_issuers_url": KASE_ISSUERS_URL,
        },
        "examples": examples,
    }


def ensure_active_universe_sgx_sources(
    session: Session,
    *,
    universe_slug: str | None = None,
    limit: int | None = None,
    reason: str = "active_universe_source_pack",
) -> dict[str, Any]:
    """Register SGXNet official announcement bindings for active Singapore universe rows."""
    q = (
        session.query(ResearchUniverseRow)
        .join(ResearchUniverse, ResearchUniverse.id == ResearchUniverseRow.universe_id)
        .filter(ResearchUniverse.status == "active", ResearchUniverseRow.archived_at.is_(None))
        .order_by(ResearchUniverse.sort_order.asc(), ResearchUniverseRow.sort_order.asc(), ResearchUniverseRow.id.asc())
    )
    if universe_slug:
        q = q.filter(ResearchUniverse.slug == str(universe_slug).strip())
    rows = q.all()
    if limit is not None:
        rows = rows[: max(1, int(limit))]

    stock_codes: set[str] = set()
    issuer_names: set[str] = set()
    company_id_by_stock_code: dict[str, int] = {}
    company_slug_by_stock_code: dict[str, str] = {}
    issuer_id_by_stock_code: dict[str, int] = {}
    issuer_name_by_stock_code: dict[str, str] = {}
    ticker_by_stock_code: dict[str, str] = {}
    company_name_by_stock_code: dict[str, str] = {}
    candidates = 0
    identifiers_created = identifiers_existing = identifiers_conflicts = missing = 0
    examples: list[dict[str, Any]] = []
    seen_issuer_code: set[tuple[int, str]] = set()
    for row in rows:
        instrument = row.instrument
        if instrument is None or not bool(getattr(instrument, "is_public", True)):
            continue
        company = session.get(Company, int(instrument.company_id)) if instrument.company_id is not None else None
        issuer = session.get(Issuer, int(instrument.issuer_id)) if instrument.issuer_id is not None else None
        if issuer is None and company is not None and company.issuer_id is not None:
            issuer = session.get(Issuer, int(company.issuer_id))
        exchange = _normalize_exchange_code(getattr(instrument, "exchange", None) or getattr(company, "exchange", None))
        country = str(getattr(instrument, "country", None) or getattr(company, "region", None) or getattr(issuer, "jurisdiction", None) or "").strip().upper()
        ticker = getattr(instrument, "ticker", None) or getattr(company, "ticker", None) or getattr(instrument, "yahoo_symbol", None)
        yahoo = str(getattr(instrument, "yahoo_symbol", None) or "").strip().upper()
        stock_code = _normalize_sgx_stock_code(ticker) or _normalize_sgx_stock_code(yahoo)
        existing_codes = _issuer_identifier_values(issuer, "SGX_STOCK") if issuer is not None else []
        in_scope = exchange == "SGX" or country in {"SG", "SGP", "SINGAPORE"} or yahoo.endswith(".SI") or bool(existing_codes)
        if not in_scope:
            continue
        candidates += 1
        row_codes = [stock_code] if stock_code else []
        for existing_code in existing_codes:
            normalized = _normalize_sgx_stock_code(existing_code)
            if normalized and normalized not in row_codes:
                row_codes.append(normalized)
        if not row_codes:
            missing += 1
            if len(examples) < 8:
                examples.append(
                    {
                        "instrument_id": int(instrument.id),
                        "display_name": instrument.display_name,
                        "ticker": instrument.ticker,
                        "exchange": instrument.exchange,
                        "reason": "missing_sgx_stock_code",
                    }
                )
            continue
        name = str(
            getattr(company, "name", None)
            or getattr(issuer, "display_name", None)
            or getattr(issuer, "legal_name", None)
            or instrument.display_name
            or ""
        ).strip()
        if name:
            issuer_names.add(name)
        for code in row_codes:
            if issuer is not None:
                pair = (int(issuer.id), code)
                if pair not in seen_issuer_code:
                    seen_issuer_code.add(pair)
                    status, related_id = _upsert_issuer_identifier(
                        session,
                        issuer=issuer,
                        id_type="SGX_STOCK",
                        id_value=code,
                        exchange="SGX",
                    )
                    if status == "created":
                        identifiers_created += 1
                    elif status == "existing":
                        identifiers_existing += 1
                    elif status == "conflict":
                        identifiers_conflicts += 1
                        if len(examples) < 8:
                            examples.append(
                                {
                                    "instrument_id": int(instrument.id),
                                    "display_name": instrument.display_name,
                                    "stock_code": code,
                                    "reason": "sgx_stock_already_bound_to_other_issuer",
                                    "other_issuer_id": related_id,
                                }
                            )
                        continue
            stock_codes.add(code)
            ticker_by_stock_code[code] = f"{code}.SI"
            if name:
                company_name_by_stock_code[code] = name
            if issuer is not None:
                issuer_id_by_stock_code[code] = int(issuer.id)
                issuer_name_by_stock_code[code] = str(getattr(issuer, "display_name", None) or getattr(issuer, "legal_name", None) or name).strip()
            if company is not None:
                company_slug_by_stock_code[code] = company.slug
                if company.id is not None:
                    company_id_by_stock_code[code] = int(company.id)

    if not candidates:
        return {"registered": False, "reason": "no_sgx_candidate_rows", "rows_scanned": len(rows), "candidate_count": 0}
    if not stock_codes:
        return {
            "registered": False,
            "reason": "no_sgx_stock_codes",
            "rows_scanned": len(rows),
            "candidate_count": candidates,
            "missing_stock_codes": missing,
            "examples": examples,
        }

    source = session.query(Source).filter_by(name=_SGX_ACTIVE_SOURCE_NAME).first()
    if source is None:
        source = Source(
            name=_SGX_ACTIVE_SOURCE_NAME,
            category="exchange",
            priority=1,
            polling_interval_seconds=43200,
            config={},
            created_at=utcnow(),
        )
        session.add(source)
        session.flush()
    cfg = dict(source.config or {})

    def _merge_unique(existing: object, additions: set[str], *, limit_n: int) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for value in [*(existing if isinstance(existing, list) else []), *sorted(additions)]:
            text = str(value or "").strip().upper()
            if text and text not in seen:
                seen.add(text)
                out.append(text)
            if len(out) >= limit_n:
                break
        return out

    def _merge_unique_text(existing: object, additions: set[str], *, limit_n: int) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for value in [*(existing if isinstance(existing, list) else []), *sorted(additions)]:
            text = re.sub(r"\s+", " ", str(value or "").strip())
            key = text.upper()
            if text and key not in seen:
                seen.add(key)
                out.append(text)
            if len(out) >= limit_n:
                break
        return out

    def _merge_map(key: str, updates: dict[str, Any]) -> dict[str, Any]:
        current = dict(cfg.get(key) if isinstance(cfg.get(key), dict) else {})
        for raw_code, value in updates.items():
            normalized = _normalize_sgx_stock_code(raw_code)
            if normalized and value is not None and str(value).strip():
                current[normalized] = value
        return current

    try:
        from thoth_terminal.connectors.sgx_announcements import (
            SGX_COMPANY_ANNOUNCEMENTS_URL,
            SGX_LINKS_CORPORATE_ANNOUNCEMENTS_URL,
        )
    except Exception:
        SGX_COMPANY_ANNOUNCEMENTS_URL = "https://www.sgx.com/securities/company-announcements"
        SGX_LINKS_CORPORATE_ANNOUNCEMENTS_URL = "https://links.sgx.com/1.0.0/corporate-announcements"

    existing_codes = {str(value).strip().upper() for value in (cfg.get("stock_codes") or []) if str(value).strip()} if isinstance(cfg.get("stock_codes"), list) else set()
    cfg.update(
        {
            "connector": "sgx_announcements",
            "enabled": True,
            "managed_by": "source_pack",
            "coverage_scope": "active_universe",
            "authority": "primary_exchange",
            "filing_system": "SGXNet",
            "official_search_url": SGX_COMPANY_ANNOUNCEMENTS_URL,
            "links_endpoint_url": SGX_LINKS_CORPORATE_ANNOUNCEMENTS_URL,
            "stock_codes": _merge_unique(cfg.get("stock_codes"), stock_codes, limit_n=700),
            "sgx_stock_codes": _merge_unique(cfg.get("sgx_stock_codes"), stock_codes, limit_n=700),
            "issuer_names": _merge_unique_text(cfg.get("issuer_names"), issuer_names, limit_n=700),
            "company_id_by_stock_code": _merge_map("company_id_by_stock_code", company_id_by_stock_code),
            "company_slug_by_stock_code": _merge_map("company_slug_by_stock_code", company_slug_by_stock_code),
            "issuer_id_by_stock_code": _merge_map("issuer_id_by_stock_code", issuer_id_by_stock_code),
            "issuer_name_by_stock_code": _merge_map("issuer_name_by_stock_code", issuer_name_by_stock_code),
            "ticker_by_stock_code": _merge_map("ticker_by_stock_code", ticker_by_stock_code),
            "company_name_by_stock_code": _merge_map("company_name_by_stock_code", company_name_by_stock_code),
            "last_registered_reason": reason,
            "source_quality": {
                **(cfg.get("source_quality") if isinstance(cfg.get("source_quality"), dict) else {}),
                "authority": "primary_exchange",
                "timeliness": "search_binding",
                "acquisition_fragility": "high",
                "rights_class": "public",
                "materialization_status": "official_search_binding_only",
            },
        }
    )
    source.config = cfg
    session.flush()
    after_codes = {str(value).strip().upper() for value in (cfg.get("stock_codes") or []) if str(value).strip()}
    added_codes = sorted(after_codes - existing_codes)
    if added_codes:
        state = session.query(SourceState).filter_by(source_id=int(source.id)).first()
        if state is not None:
            state.cursor = {}
    session.flush()
    return {
        "registered": True,
        "rows_scanned": len(rows),
        "candidate_count": candidates,
        "stock_codes_registered": len(stock_codes),
        "identifiers_created": identifiers_created,
        "identifiers_existing": identifiers_existing,
        "identifier_conflicts": identifiers_conflicts,
        "missing_stock_codes": missing,
        "sgx_announcements_source": {
            "source_id": int(source.id),
            "source_name": source.name,
            "stock_code_count": len(cfg.get("stock_codes") or []),
            "stock_codes_added": len(added_codes),
            "official_search_url": SGX_COMPANY_ANNOUNCEMENTS_URL,
        },
        "examples": examples,
    }


def ensure_active_universe_nse_sources(
    session: Session,
    *,
    universe_slug: str | None = None,
    limit: int | None = None,
    reason: str = "active_universe_source_pack",
) -> dict[str, Any]:
    """Refresh NSE official corporate-announcements source from active Indian universe rows."""
    q = (
        session.query(ResearchUniverseRow)
        .join(ResearchUniverse, ResearchUniverse.id == ResearchUniverseRow.universe_id)
        .filter(ResearchUniverse.status == "active", ResearchUniverseRow.archived_at.is_(None))
        .order_by(ResearchUniverse.sort_order.asc(), ResearchUniverseRow.sort_order.asc(), ResearchUniverseRow.id.asc())
    )
    if universe_slug:
        q = q.filter(ResearchUniverse.slug == str(universe_slug).strip())
    rows = q.all()
    if limit is not None:
        rows = rows[: max(1, int(limit))]

    symbols: set[str] = set()
    keywords: set[str] = set()
    company_id_by_symbol: dict[str, int] = {}
    company_slug_by_symbol: dict[str, str] = {}
    issuer_id_by_symbol: dict[str, int] = {}
    issuer_name_by_symbol: dict[str, str] = {}
    ticker_by_symbol: dict[str, str] = {}
    company_name_by_symbol: dict[str, str] = {}
    candidates = 0
    identifiers_created = identifiers_existing = identifiers_conflicts = missing = 0
    examples: list[dict[str, Any]] = []
    seen_issuer_symbol: set[tuple[int, str]] = set()
    for row in rows:
        instrument = row.instrument
        if instrument is None or not bool(getattr(instrument, "is_public", True)):
            continue
        company = session.get(Company, int(instrument.company_id)) if instrument.company_id is not None else None
        issuer = session.get(Issuer, int(instrument.issuer_id)) if instrument.issuer_id is not None else None
        if issuer is None and company is not None and company.issuer_id is not None:
            issuer = session.get(Issuer, int(company.issuer_id))
        exchange = _normalize_exchange_code(getattr(instrument, "exchange", None) or getattr(company, "exchange", None))
        country = str(getattr(instrument, "country", None) or getattr(company, "region", None) or getattr(issuer, "jurisdiction", None) or "").strip().upper()
        ticker = getattr(instrument, "ticker", None) or getattr(company, "ticker", None) or getattr(instrument, "yahoo_symbol", None)
        yahoo = str(getattr(instrument, "yahoo_symbol", None) or "").strip().upper()
        symbol = _normalize_nse_symbol(ticker) or _normalize_nse_symbol(yahoo)
        existing_symbols = _issuer_identifier_values(issuer, "NSE_STOCK") if issuer is not None else []
        in_scope = exchange in {"NSE", "NSEI"} or country in {"IN", "INDIA"} or str(ticker or "").strip().upper().endswith(".NS") or yahoo.endswith(".NS") or bool(existing_symbols)
        if not in_scope:
            continue
        candidates += 1
        row_symbols = [symbol] if symbol else []
        for existing_symbol in existing_symbols:
            normalized = _normalize_nse_symbol(existing_symbol)
            if normalized and normalized not in row_symbols:
                row_symbols.append(normalized)
        if not row_symbols:
            missing += 1
            if len(examples) < 8:
                examples.append(
                    {
                        "instrument_id": int(instrument.id),
                        "display_name": instrument.display_name,
                        "ticker": instrument.ticker,
                        "exchange": instrument.exchange,
                        "reason": "missing_nse_symbol",
                    }
                )
            continue
        name = str(
            getattr(company, "name", None)
            or getattr(issuer, "display_name", None)
            or getattr(issuer, "legal_name", None)
            or instrument.display_name
            or ""
        ).strip()
        for symbol_value in row_symbols:
            if issuer is not None:
                pair = (int(issuer.id), symbol_value)
                if pair not in seen_issuer_symbol:
                    seen_issuer_symbol.add(pair)
                    status, related_id = _upsert_issuer_identifier(
                        session,
                        issuer=issuer,
                        id_type="NSE_STOCK",
                        id_value=symbol_value,
                        exchange="NSE",
                    )
                    if status == "created":
                        identifiers_created += 1
                    elif status == "existing":
                        identifiers_existing += 1
                    elif status == "conflict":
                        identifiers_conflicts += 1
                        if len(examples) < 8:
                            examples.append(
                                {
                                    "instrument_id": int(instrument.id),
                                    "display_name": instrument.display_name,
                                    "symbol": symbol_value,
                                    "reason": "nse_symbol_already_bound_to_other_issuer",
                                    "other_issuer_id": related_id,
                                }
                            )
                        continue
            symbols.add(symbol_value)
            ticker_by_symbol[symbol_value] = f"{symbol_value}.NS"
            if name:
                company_name_by_symbol[symbol_value] = name
                keywords.add(name[:120])
            keywords.add(symbol_value)
            if issuer is not None:
                issuer_id_by_symbol[symbol_value] = int(issuer.id)
                issuer_name_by_symbol[symbol_value] = str(getattr(issuer, "display_name", None) or getattr(issuer, "legal_name", None) or name).strip()
            if company is not None:
                company_slug_by_symbol[symbol_value] = company.slug
                if company.id is not None:
                    company_id_by_symbol[symbol_value] = int(company.id)

    if not candidates:
        return {"registered": False, "reason": "no_nse_candidate_rows", "rows_scanned": len(rows), "candidate_count": 0}
    if not symbols:
        return {
            "registered": False,
            "reason": "no_nse_symbols",
            "rows_scanned": len(rows),
            "candidate_count": candidates,
            "missing_symbols": missing,
            "examples": examples,
        }

    source = session.query(Source).filter_by(name=_NSE_ACTIVE_SOURCE_NAME).first()
    if source is None:
        source = Source(
            name=_NSE_ACTIVE_SOURCE_NAME,
            category="exchange",
            priority=1,
            polling_interval_seconds=21600,
            config={},
            created_at=utcnow(),
        )
        session.add(source)
        session.flush()
    cfg = dict(source.config or {})

    def _merge_unique(existing: object, additions: set[str], *, limit_n: int) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for value in [*(existing if isinstance(existing, list) else []), *sorted(additions)]:
            text = str(value or "").strip().upper()
            if text and text not in seen:
                seen.add(text)
                out.append(text)
            if len(out) >= limit_n:
                break
        return out

    def _merge_map(key: str, updates: dict[str, Any]) -> dict[str, Any]:
        current = dict(cfg.get(key) if isinstance(cfg.get(key), dict) else {})
        for raw_symbol, value in updates.items():
            normalized = _normalize_nse_symbol(raw_symbol)
            if normalized and value is not None and str(value).strip():
                current[normalized] = value
        return current

    try:
        from thoth_terminal.connectors.nse_corporate_announcements import NSE_CORPORATE_ANNOUNCEMENTS_PAGE
    except Exception:
        NSE_CORPORATE_ANNOUNCEMENTS_PAGE = "https://www.nseindia.com/companies-listing/corporate-filings-announcements"

    existing_symbols = {str(value).strip().upper() for value in (cfg.get("symbols") or []) if str(value).strip()} if isinstance(cfg.get("symbols"), list) else set()
    cfg.update(
        {
            "connector": "nse_corporate_announcements",
            "lookback_days": 365,
            "symbols_per_run": 3,
            "max_items_per_run": 120,
            "fetch_article": False,
            "download_article_images": False,
            "refresh_existing": True,
            "enabled": True,
            "managed_by": "source_pack",
            "coverage_scope": "active_universe",
            "authority": "primary_exchange",
            "filing_system": "NSE corporate filings",
            "official_search_url": NSE_CORPORATE_ANNOUNCEMENTS_PAGE,
            "last_registered_reason": reason,
            "symbols": _merge_unique(cfg.get("symbols"), symbols, limit_n=700),
            "nse_symbols": _merge_unique(cfg.get("nse_symbols"), symbols, limit_n=700),
            "include_keywords": _merge_unique(cfg.get("include_keywords"), keywords | symbols, limit_n=1200),
            "company_id_by_symbol": _merge_map("company_id_by_symbol", company_id_by_symbol),
            "company_slug_by_symbol": _merge_map("company_slug_by_symbol", company_slug_by_symbol),
            "issuer_id_by_symbol": _merge_map("issuer_id_by_symbol", issuer_id_by_symbol),
            "issuer_name_by_symbol": _merge_map("issuer_name_by_symbol", issuer_name_by_symbol),
            "ticker_by_symbol": _merge_map("ticker_by_symbol", ticker_by_symbol),
            "company_name_by_symbol": _merge_map("company_name_by_symbol", company_name_by_symbol),
            "source_quality": {
                **(cfg.get("source_quality") if isinstance(cfg.get("source_quality"), dict) else {}),
                "authority": "primary_exchange",
                "timeliness": "scheduled",
                "acquisition_fragility": "medium",
                "rights_class": "public",
            },
        }
    )
    source.config = cfg
    session.flush()
    after_symbols = {str(value).strip().upper() for value in (cfg.get("symbols") or []) if str(value).strip()}
    added_symbols = sorted(after_symbols - existing_symbols)
    if added_symbols:
        state = session.query(SourceState).filter_by(source_id=int(source.id)).first()
        if state is not None:
            state.cursor = {}
    session.flush()
    return {
        "registered": True,
        "rows_scanned": len(rows),
        "candidate_count": candidates,
        "symbols_registered": len(symbols),
        "identifiers_created": identifiers_created,
        "identifiers_existing": identifiers_existing,
        "identifier_conflicts": identifiers_conflicts,
        "missing_symbols": missing,
        "nse_announcements_source": {
            "source_id": int(source.id),
            "source_name": source.name,
            "symbol_count": len(cfg.get("symbols") or []),
            "symbols_added": len(added_symbols),
            "official_search_url": NSE_CORPORATE_ANNOUNCEMENTS_PAGE,
        },
        "examples": examples,
    }


def ensure_active_universe_opendart_sources(
    session: Session,
    *,
    universe_slug: str | None = None,
    limit: int | None = None,
    reason: str = "active_universe_source_pack",
    corp_code_map: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Register OpenDART official filing and financial sources for active Korean universe rows."""
    q = (
        session.query(ResearchUniverseRow)
        .join(ResearchUniverse, ResearchUniverse.id == ResearchUniverseRow.universe_id)
        .filter(ResearchUniverse.status == "active", ResearchUniverseRow.archived_at.is_(None))
        .order_by(ResearchUniverse.sort_order.asc(), ResearchUniverseRow.sort_order.asc(), ResearchUniverseRow.id.asc())
    )
    if universe_slug:
        q = q.filter(ResearchUniverse.slug == str(universe_slug).strip())
    rows = q.all()
    if limit is not None:
        rows = rows[: max(1, int(limit))]

    candidates: list[tuple[ResearchInstrument, Company | None, Issuer, str]] = []
    kr_stock_codes: set[str] = set()
    company_names: set[str] = set()
    company_id_by_company_name: dict[str, int] = {}
    company_slug_by_company_name: dict[str, str] = {}
    ticker_by_company_name: dict[str, str] = {}
    company_name_by_stock_code: dict[str, str] = {}
    company_id_by_stock_code: dict[str, int] = {}
    company_slug_by_stock_code: dict[str, str] = {}
    ticker_by_stock_code: dict[str, str] = {}
    for row in rows:
        instrument = row.instrument
        if instrument is None or not bool(getattr(instrument, "is_public", True)):
            continue
        company = session.get(Company, int(instrument.company_id)) if instrument.company_id is not None else None
        issuer = session.get(Issuer, int(instrument.issuer_id)) if instrument.issuer_id is not None else None
        if issuer is None and company is not None and company.issuer_id is not None:
            issuer = session.get(Issuer, int(company.issuer_id))
        if issuer is None:
            continue
        exchange = _normalize_exchange_code(getattr(instrument, "exchange", None) or getattr(company, "exchange", None))
        country = str(getattr(instrument, "country", None) or getattr(company, "region", None) or getattr(issuer, "jurisdiction", None) or "").strip().upper()
        ticker = getattr(instrument, "ticker", None) or getattr(company, "ticker", None)
        stock_code = _normalize_kr_stock_code(ticker)
        if not stock_code:
            continue
        if exchange in {"KRX", "KOSPI", "KOSDAQ"} or country in {"KR", "KOREA", "SOUTH KOREA", "REPUBLIC OF KOREA"} or str(ticker or "").upper().endswith((".KS", ".KQ")):
            candidates.append((instrument, company, issuer, stock_code))
            kr_stock_codes.add(stock_code)
            if company is not None:
                if company.id is not None:
                    company_id_by_stock_code[stock_code] = int(company.id)
                if company.slug:
                    company_slug_by_stock_code[stock_code] = company.slug
                if company.name:
                    company_name_by_stock_code[stock_code] = str(company.name).strip()
                if company.ticker:
                    ticker_by_stock_code[stock_code] = str(company.ticker).strip()
            for value in [
                getattr(instrument, "display_name", None),
                getattr(instrument, "legal_name", None),
                getattr(company, "name", None) if company is not None else None,
                getattr(issuer, "display_name", None),
                getattr(issuer, "legal_name", None),
            ]:
                text = re.sub(r"\s+", " ", str(value or "").strip())
                if not text or len(text) < 2:
                    continue
                key = text[:120]
                company_names.add(key)
                if company is not None and company.id is not None:
                    company_id_by_company_name[key] = int(company.id)
                if company is not None and company.slug:
                    company_slug_by_company_name[key] = company.slug
                if ticker:
                    ticker_by_company_name[key] = str(ticker).strip()

    if not candidates:
        return {"registered": False, "reason": "no_korean_candidate_rows", "rows_scanned": len(rows), "candidate_count": 0}

    mapping = corp_code_map

    def _corp_code_map() -> dict[str, dict[str, str]]:
        nonlocal mapping
        if mapping is None:
            mapping = fetch_opendart_corp_code_map()
        return mapping

    corp_codes: list[str] = []
    company_id_by_corp_code: dict[str, int] = {}
    company_slug_by_corp_code: dict[str, str] = {}
    ticker_by_corp_code: dict[str, str] = {}
    company_name_by_corp_code: dict[str, str] = {}
    created = existing = conflicts = missing = 0
    examples: list[dict[str, Any]] = []
    seen_issuer_corp: set[tuple[int, str]] = set()
    for instrument, company, issuer, stock_code in candidates:
        existing_corp_codes = _issuer_identifier_values(issuer, "DART_CORP")
        if existing_corp_codes:
            matches = [
                {
                    "corp_code": corp_code,
                    "stock_code": stock_code,
                    "corp_eng_name": getattr(issuer, "display_name", None) or getattr(company, "name", "") or instrument.display_name,
                }
                for corp_code in existing_corp_codes
            ]
        else:
            match = _corp_code_map().get(stock_code)
            if not match:
                missing += 1
                if len(examples) < 8:
                    examples.append(
                        {
                            "instrument_id": int(instrument.id),
                            "display_name": instrument.display_name,
                            "stock_code": stock_code,
                            "reason": "stock_code_not_in_opendart_corp_code",
                        }
                    )
                continue
            matches = [match]
        for match in matches:
            corp_code = str(match.get("corp_code") or "").strip()
            if not corp_code:
                missing += 1
                continue
            pair = (int(issuer.id), corp_code)
            if pair in seen_issuer_corp:
                continue
            seen_issuer_corp.add(pair)
            for id_type, id_value, exchange in [("DART_CORP", corp_code, None), ("KR_STOCK", stock_code, "KRX")]:
                status, related_id = _upsert_issuer_identifier(session, issuer=issuer, id_type=id_type, id_value=id_value, exchange=exchange)
                if status == "created":
                    created += 1
                elif status == "existing":
                    existing += 1
                elif status == "conflict":
                    conflicts += 1
                    if len(examples) < 8:
                        examples.append(
                            {
                                "instrument_id": int(instrument.id),
                                "display_name": instrument.display_name,
                                "stock_code": stock_code,
                                "corp_code": corp_code,
                                "id_type": id_type,
                                "reason": "identifier_already_bound_to_other_issuer",
                                "other_issuer_id": related_id,
                            }
                        )
            corp_codes.append(corp_code)
            ticker_by_corp_code[corp_code] = stock_code
            matched_company_name = str(match.get("corp_eng_name") or match.get("corp_name") or instrument.display_name).strip()
            company_name_by_corp_code[corp_code] = matched_company_name
            if matched_company_name:
                key = matched_company_name[:120]
                company_names.add(key)
                if company is not None and company.id is not None:
                    company_id_by_company_name[key] = int(company.id)
                if company is not None and company.slug:
                    company_slug_by_company_name[key] = company.slug
                ticker_by_company_name[key] = stock_code
            if company is not None:
                company_slug_by_corp_code[corp_code] = company.slug
                if company.id is not None:
                    company_id_by_corp_code[corp_code] = int(company.id)

    normalized = sorted(set(corp_codes))
    if not normalized:
        return {
            "registered": False,
            "reason": "no_corp_codes",
            "rows_scanned": len(rows),
            "candidate_count": len(candidates),
            "missing_stock_matches": missing,
            "examples": examples,
        }

    def _merge_source(name: str, connector: str, defaults: dict[str, Any]) -> dict[str, Any]:
        source = session.query(Source).filter_by(name=name).first()
        if source is None:
            source = Source(
                name=name,
                category="filings",
                priority=1,
                polling_interval_seconds=43200,
                config={},
                created_at=utcnow(),
            )
            session.add(source)
            session.flush()
        cfg = dict(source.config or {})
        previous_cursor_plan_key = str(cfg.get("cursor_plan_key") or "").strip()
        existing_codes = [str(value).strip() for value in (cfg.get("corp_codes") or []) if str(value).strip()]
        merged_codes = sorted(set(existing_codes).union(normalized))
        added = [code for code in normalized if code not in set(existing_codes)]
        cfg.update(
            {
                **defaults,
                "connector": connector,
                "corp_codes": merged_codes,
                "company_id_by_corp_code": {**(cfg.get("company_id_by_corp_code") if isinstance(cfg.get("company_id_by_corp_code"), dict) else {}), **company_id_by_corp_code},
                "company_slug_by_corp_code": {**(cfg.get("company_slug_by_corp_code") if isinstance(cfg.get("company_slug_by_corp_code"), dict) else {}), **company_slug_by_corp_code},
                "ticker_by_corp_code": {**(cfg.get("ticker_by_corp_code") if isinstance(cfg.get("ticker_by_corp_code"), dict) else {}), **ticker_by_corp_code},
                "company_name_by_corp_code": {**(cfg.get("company_name_by_corp_code") if isinstance(cfg.get("company_name_by_corp_code"), dict) else {}), **company_name_by_corp_code},
                "enabled": True,
                "managed_by": "source_pack",
                "coverage_scope": "active_universe",
                "last_registered_reason": reason,
                "source_quality": {
                    **(cfg.get("source_quality") if isinstance(cfg.get("source_quality"), dict) else {}),
                    "authority": "primary_regulator",
                    "timeliness": "scheduled",
                    "acquisition_fragility": "low",
                    "rights_class": "public",
                },
            }
        )
        source.config = cfg
        session.flush()
        state = session.query(SourceState).filter_by(source_id=int(source.id)).first()
        cursor_reset = False
        if state is not None and cfg.get("cursor_plan_key") and previous_cursor_plan_key != str(cfg.get("cursor_plan_key") or "").strip():
            state.cursor = {}
            cursor_reset = True
            session.flush()
        return {
            "registered": True,
            "source_id": int(source.id),
            "source_name": source.name,
            "corp_codes_added": added,
            "corp_code_count": len(merged_codes),
            "cursor_reset": cursor_reset,
            "should_enqueue": bool(added) or cursor_reset or state is None or state.last_success_at is None,
        }

    disclosures = _merge_source(
        _OPENDART_DISCLOSURES_SOURCE_NAME,
        "opendart_disclosures",
        {
            "lookback_days": 730,
            "rotate_corp_codes": True,
            "max_corp_codes_per_run": 8,
            "max_items_per_run": 120,
            "page_count": 100,
            "max_pages": 3,
            "fetch_article": False,
            "download_article_images": False,
            "prefetch_outside_transaction": True,
            "refresh_existing": True,
        },
    )
    financials = _merge_source(
        _OPENDART_FINANCIALS_SOURCE_NAME,
        "opendart_financials",
        {
            "year_count": 3,
            "report_codes": ["1q", "half", "3q", "annual"],
            "fs_divs": ["CFS", "OFS"],
            "requests_per_run": 40,
            "max_items_per_run": 40,
            "source_pack_priority_mode": True,
            "config_version": "source-pack-v4",
            "cursor_plan_key": "source-pack-opendart-financials-v4:priority-current-quarter-prior-annual:years3:requests40:deferred-sync-period-meta",
            "fetch_article": False,
            "download_article_images": False,
            "prefetch_outside_transaction": True,
            "defer_ingest_derived_sync": True,
            "refresh_existing": True,
        },
    )

    def _merge_unique(existing: object, additions: set[str], *, limit_n: int) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for value in [*(existing if isinstance(existing, list) else []), *sorted(additions)]:
            text = str(value or "").strip()
            key = text.casefold()
            if text and key not in seen:
                seen.add(key)
                out.append(text)
            if len(out) >= limit_n:
                break
        return out

    krx_source = session.query(Source).filter_by(name=_KRX_ACTIVE_SOURCE_NAME).first()
    if krx_source is None:
        krx_source = Source(
            name=_KRX_ACTIVE_SOURCE_NAME,
            category="exchange",
            priority=1,
            polling_interval_seconds=21600,
            config={},
            created_at=utcnow(),
        )
        session.add(krx_source)
        session.flush()
    krx_cfg = dict(krx_source.config or {})
    existing_keywords = {str(value).casefold() for value in (krx_cfg.get("include_keywords") or []) if str(value).strip()} if isinstance(krx_cfg.get("include_keywords"), list) else set()
    existing_names = {str(value).casefold() for value in (krx_cfg.get("company_names") or []) if str(value).strip()} if isinstance(krx_cfg.get("company_names"), list) else set()
    active_krx_keywords = company_names | kr_stock_codes
    krx_cfg.update(
        {
            "connector": "krx_kind_disclosure",
            "market_type": "0",
            "lookback_days": 730,
            "dates_per_run": 7,
            "max_items_per_run": 120,
            "include_keywords": _merge_unique(
                [],
                active_krx_keywords,
                limit_n=1000,
            ),
            "company_names": _merge_unique(krx_cfg.get("company_names"), company_names, limit_n=700),
            "company_name_exact_match": True,
            "company_names_per_run": 0,
            "company_id_by_company_name": {**(krx_cfg.get("company_id_by_company_name") if isinstance(krx_cfg.get("company_id_by_company_name"), dict) else {}), **company_id_by_company_name},
            "company_slug_by_company_name": {**(krx_cfg.get("company_slug_by_company_name") if isinstance(krx_cfg.get("company_slug_by_company_name"), dict) else {}), **company_slug_by_company_name},
            "ticker_by_company_name": {**(krx_cfg.get("ticker_by_company_name") if isinstance(krx_cfg.get("ticker_by_company_name"), dict) else {}), **ticker_by_company_name},
            "company_id_by_stock_code": {**(krx_cfg.get("company_id_by_stock_code") if isinstance(krx_cfg.get("company_id_by_stock_code"), dict) else {}), **company_id_by_stock_code},
            "company_slug_by_stock_code": {**(krx_cfg.get("company_slug_by_stock_code") if isinstance(krx_cfg.get("company_slug_by_stock_code"), dict) else {}), **company_slug_by_stock_code},
            "company_name_by_stock_code": {**(krx_cfg.get("company_name_by_stock_code") if isinstance(krx_cfg.get("company_name_by_stock_code"), dict) else {}), **company_name_by_stock_code},
            "ticker_by_stock_code": {**(krx_cfg.get("ticker_by_stock_code") if isinstance(krx_cfg.get("ticker_by_stock_code"), dict) else {}), **ticker_by_stock_code},
            "fetch_article": False,
            "download_article_images": False,
            "store_raw_html_on_failure": True,
            "refresh_existing": True,
            "enabled": True,
            "managed_by": "source_pack",
            "coverage_scope": "active_universe",
            "last_registered_reason": reason,
            "source_quality": {
                **(krx_cfg.get("source_quality") if isinstance(krx_cfg.get("source_quality"), dict) else {}),
                "authority": "primary_exchange",
                "timeliness": "scheduled",
                "acquisition_fragility": "medium",
                "rights_class": "public",
            },
        }
    )
    krx_source.config = krx_cfg
    session.flush()
    updated_keywords = {str(value).casefold() for value in (krx_cfg.get("include_keywords") or []) if str(value).strip()}
    updated_names = {str(value).casefold() for value in (krx_cfg.get("company_names") or []) if str(value).strip()}
    added_keywords = sorted(updated_keywords - existing_keywords)
    added_names = sorted(updated_names - existing_names)
    krx_tracking_changed = updated_keywords != existing_keywords or updated_names != existing_names
    krx_state = session.query(SourceState).filter_by(source_id=int(krx_source.id)).first()
    if krx_tracking_changed:
        if krx_state is not None:
            krx_state.cursor = {}
    return {
        "registered": True,
        "rows_scanned": len(rows),
        "candidate_count": len(candidates),
        "corp_codes_registered": len(normalized),
        "identifiers_created": created,
        "identifiers_existing": existing,
        "identifier_conflicts": conflicts,
        "missing_stock_matches": missing,
        "opendart_disclosures_source": disclosures,
        "opendart_financials_source": financials,
        "krx_kind_source": {
            "source_id": int(krx_source.id),
            "source_name": krx_source.name,
            "include_keywords_added": len(added_keywords),
            "company_names_added": len(added_names),
            "company_name_count": len(krx_cfg.get("company_names") or []),
            "should_enqueue": krx_tracking_changed or krx_state is None or krx_state.last_success_at is None,
        },
        "examples": examples,
    }


def ensure_active_universe_japan_sources(
    session: Session,
    *,
    universe_slug: str | None = None,
    limit: int | None = None,
    reason: str = "active_universe_source_pack",
    refresh_official_edinet_code_list: bool = False,
) -> dict[str, Any]:
    """Refresh EDINETDB and TDnet tracked query lists from active Japanese universe rows."""
    q = (
        session.query(ResearchUniverseRow)
        .join(ResearchUniverse, ResearchUniverse.id == ResearchUniverseRow.universe_id)
        .filter(ResearchUniverse.status == "active", ResearchUniverseRow.archived_at.is_(None))
        .order_by(ResearchUniverse.sort_order.asc(), ResearchUniverseRow.sort_order.asc(), ResearchUniverseRow.id.asc())
    )
    if universe_slug:
        q = q.filter(ResearchUniverse.slug == str(universe_slug).strip())
    rows = q.all()
    if limit is not None:
        rows = rows[: max(1, int(limit))]

    issuer_ids_with_explicit_japan_context: set[int] = set()
    for row in rows:
        instrument = row.instrument
        if instrument is None or not bool(getattr(instrument, "is_public", True)):
            continue
        company = session.get(Company, int(instrument.company_id)) if instrument.company_id is not None else None
        issuer = session.get(Issuer, int(instrument.issuer_id)) if instrument.issuer_id is not None else None
        if issuer is None and company is not None and company.issuer_id is not None:
            issuer = session.get(Issuer, int(company.issuer_id))
        if issuer is None or issuer.id is None:
            continue
        exchange_codes = {
            _normalize_exchange_code(value)
            for value in [getattr(instrument, "exchange", None), getattr(company, "exchange", None) if company is not None else None]
            if str(value or "").strip()
        }
        country_codes = {
            str(value or "").strip().upper()
            for value in [
                getattr(instrument, "country", None),
                getattr(company, "region", None) if company is not None else None,
                getattr(issuer, "jurisdiction", None),
            ]
            if str(value or "").strip()
        }
        ticker_values = [
            getattr(company, "ticker", None) if company is not None else None,
            getattr(instrument, "ticker", None),
            getattr(instrument, "yahoo_symbol", None),
        ]
        jp_ticker_suffix = any(re.search(r"\.(T|JP)$", str(value or "").strip(), flags=re.IGNORECASE) for value in ticker_values)
        depositary_like = bool(
            re.search(
                r"\b(ADR|ADS|GDR|DEPOSITARY|DEPOSITORY|SPONSORED|UNSPONSORED|REGS)\b",
                " ".join(
                    str(value or "")
                    for value in [
                        getattr(instrument, "security_type", None),
                        getattr(instrument, "display_name", None),
                        getattr(instrument, "legal_name", None),
                    ]
                ).upper(),
            )
        )
        jp_exchange = bool(
            exchange_codes & {"TSEJ", "JPX", "TYO", "XTKS", "TOKYOSTOCKEXCHANGE"}
            or any("TOKYO" in code for code in exchange_codes)
            or ("TSE" in exchange_codes and (country_codes & {"JP", "JAPAN"} or jp_ticker_suffix))
        )
        explicit_non_japan_country = bool(country_codes and not (country_codes & {"JP", "JAPAN"}))
        explicit_non_japan_exchange = bool(
            exchange_codes
            & {
                "TWSE",
                "TPEX",
                "TWO",
                "TAIWAN",
                "SEHK",
                "HKEX",
                "HONGKONG",
                "SSE",
                "SZSE",
                "SHSE",
                "SHENZHEN",
                "SHANGHAI",
                "TADAWUL",
                "NASDAQ",
                "NYSE",
                "PINK",
                "OTC",
            }
        )
        explicit_foreign_primary_exchange = explicit_non_japan_exchange and not jp_exchange and not depositary_like
        explicit_non_japan_market = explicit_non_japan_country or (
            explicit_non_japan_exchange and not (country_codes & {"JP", "JAPAN"})
        ) or explicit_foreign_primary_exchange
        if not (explicit_non_japan_market and not jp_exchange) and (
            jp_exchange or country_codes & {"JP", "JAPAN"} or jp_ticker_suffix
        ):
            issuer_ids_with_explicit_japan_context.add(int(issuer.id))

    stock_codes: set[str] = set()
    edinet_codes: set[str] = set()
    queries: set[str] = set()
    priority_queries: set[str] = set()
    keywords: set[str] = set()
    company_id_by_stock_code: dict[str, int] = {}
    company_slug_by_stock_code: dict[str, str] = {}
    issuer_id_by_stock_code: dict[str, int] = {}
    issuer_name_by_stock_code: dict[str, str] = {}
    ticker_by_stock_code: dict[str, str] = {}
    company_name_by_stock_code: dict[str, str] = {}
    company_id_by_edinet_code: dict[str, int] = {}
    candidates = 0
    identifiers_created = identifiers_existing = identifiers_conflicts = 0
    document_edinet_examined = 0
    document_edinet_harvested = 0
    document_edinet_identifiers_created = 0
    document_edinet_identifiers_existing = 0
    document_edinet_identifier_conflicts = 0
    document_edinet_examples: list[dict[str, Any]] = []
    official_code_list_examined = 0
    official_code_list_matched = 0
    official_code_list_identifiers_created = 0
    official_code_list_identifiers_existing = 0
    official_code_list_identifier_conflicts = 0
    official_code_list_error: str | None = None
    official_code_list_examples: list[dict[str, Any]] = []
    stale_jp_identifiers_removed = 0
    stale_jp_identifier_ids_removed: set[int] = set()
    stale_jp_identifier_examples: list[dict[str, Any]] = []
    examples: list[dict[str, Any]] = []
    for row in rows:
        instrument = row.instrument
        if instrument is None or not bool(getattr(instrument, "is_public", True)):
            continue
        company = session.get(Company, int(instrument.company_id)) if instrument.company_id is not None else None
        issuer = session.get(Issuer, int(instrument.issuer_id)) if instrument.issuer_id is not None else None
        if issuer is None and company is not None and company.issuer_id is not None:
            issuer = session.get(Issuer, int(company.issuer_id))
        exchange_values = [
            getattr(instrument, "exchange", None),
            getattr(company, "exchange", None) if company is not None else None,
        ]
        country_values = [
            getattr(instrument, "country", None),
            getattr(company, "region", None) if company is not None else None,
            getattr(issuer, "jurisdiction", None) if issuer is not None else None,
        ]
        exchange_codes = {_normalize_exchange_code(value) for value in exchange_values if str(value or "").strip()}
        country_codes = {str(value or "").strip().upper() for value in country_values if str(value or "").strip()}
        existing_jp_stock_codes = _issuer_identifier_values(issuer, "JP_STOCK") if issuer is not None else []
        existing_edinet_codes = _issuer_identifier_values(issuer, "EDINET") if issuer is not None else []
        ticker_values = [
            *existing_jp_stock_codes,
            getattr(company, "ticker", None) if company is not None else None,
            getattr(instrument, "ticker", None),
            getattr(instrument, "yahoo_symbol", None),
        ]
        stock_code = next((code for code in (_normalize_jp_stock_code(value) for value in ticker_values) if code), None)
        jp_ticker_suffix = any(re.search(r"\.(T|JP)$", str(value or "").strip(), flags=re.IGNORECASE) for value in ticker_values)
        depositary_like = bool(
            re.search(
                r"\b(ADR|ADS|GDR|DEPOSITARY|DEPOSITORY|SPONSORED|UNSPONSORED|REGS)\b",
                " ".join(
                    str(value or "")
                    for value in [
                        getattr(instrument, "security_type", None),
                        getattr(instrument, "display_name", None),
                        getattr(instrument, "legal_name", None),
                    ]
                ).upper(),
            )
        )
        jp_exchange = bool(
            exchange_codes & {"TSEJ", "JPX", "TYO", "XTKS", "TOKYOSTOCKEXCHANGE"}
            or any("TOKYO" in code for code in exchange_codes)
            or ("TSE" in exchange_codes and (country_codes & {"JP", "JAPAN"} or jp_ticker_suffix))
        )
        explicit_non_japan_country = bool(country_codes and not (country_codes & {"JP", "JAPAN"}))
        explicit_non_japan_exchange = bool(
            exchange_codes
            & {
                "TWSE",
                "TPEX",
                "TWO",
                "TAIWAN",
                "SEHK",
                "HKEX",
                "HONGKONG",
                "SSE",
                "SZSE",
                "SHSE",
                "SHENZHEN",
                "SHANGHAI",
                "TADAWUL",
                "NASDAQ",
                "NYSE",
                "PINK",
                "OTC",
            }
        )
        explicit_foreign_primary_exchange = explicit_non_japan_exchange and not jp_exchange and not depositary_like
        explicit_non_japan_market = explicit_non_japan_country or (
            explicit_non_japan_exchange and not (country_codes & {"JP", "JAPAN"})
        ) or explicit_foreign_primary_exchange
        issuer_has_explicit_japan_context = issuer is not None and int(issuer.id) in issuer_ids_with_explicit_japan_context
        if (
            issuer is not None
            and not issuer_has_explicit_japan_context
            and explicit_non_japan_market
            and not jp_exchange
        ):
            stale_identifiers = [
                identifier
                for identifier in (getattr(issuer, "identifiers", []) or [])
                if str(identifier.id_type or "").strip().upper() == "JP_STOCK"
            ]
            for identifier in stale_identifiers:
                identifier_id = int(identifier.id)
                if identifier_id in stale_jp_identifier_ids_removed:
                    continue
                if len(stale_jp_identifier_examples) < 8:
                    stale_jp_identifier_examples.append(
                        {
                            "issuer_id": int(issuer.id),
                            "identifier_id": identifier_id,
                            "id_value": str(identifier.id_value or "").strip(),
                            "country_context": sorted(country_codes),
                            "exchange_context": sorted(exchange_codes),
                        }
                    )
                session.delete(identifier)
                stale_jp_identifier_ids_removed.add(identifier_id)
                stale_jp_identifiers_removed += 1
            if stale_identifiers:
                session.flush()
                existing_jp_stock_codes = []
        jp_context = bool(
            not (explicit_non_japan_market and not jp_exchange)
            and (
                jp_exchange
                or country_codes & {"JP", "JAPAN"}
                or existing_jp_stock_codes
                or existing_edinet_codes
                or jp_ticker_suffix
            )
        )
        if not jp_context:
            continue
        candidates += 1
        if stock_code:
            stock_codes.add(stock_code)
            queries.add(stock_code)
            if not existing_edinet_codes:
                priority_queries.add(stock_code)
            ticker_text = str(getattr(instrument, "ticker", None) or getattr(company, "ticker", None) or "").strip()
            if ticker_text:
                ticker_by_stock_code[stock_code] = ticker_text
            if company is not None:
                company_name = str(company.name or "").strip()
                if company_name:
                    company_name_by_stock_code[stock_code] = company_name
                if company.slug:
                    company_slug_by_stock_code[stock_code] = company.slug
                if company.id is not None:
                    company_id_by_stock_code[stock_code] = int(company.id)
            if issuer is not None:
                issuer_name = str(issuer.display_name or issuer.legal_name or "").strip()
                if issuer_name and stock_code not in company_name_by_stock_code:
                    company_name_by_stock_code[stock_code] = issuer_name
                issuer_id_by_stock_code[stock_code] = int(issuer.id)
                issuer_name_by_stock_code[stock_code] = issuer_name
                status, related_id = _upsert_issuer_identifier(session, issuer=issuer, id_type="JP_STOCK", id_value=stock_code, exchange="TSEJ")
                if status == "created":
                    identifiers_created += 1
                elif status == "existing":
                    identifiers_existing += 1
                elif status == "conflict":
                    identifiers_conflicts += 1
                    if len(examples) < 8:
                        examples.append(
                            {
                                "instrument_id": int(instrument.id),
                                "display_name": instrument.display_name,
                                "stock_code": stock_code,
                                "reason": "jp_stock_already_bound_to_other_issuer",
                                "other_issuer_id": related_id,
                            }
                        )
        if issuer is not None:
            for value in existing_edinet_codes:
                edinet_code = _normalize_edinet_code(value)
                if edinet_code:
                    edinet_codes.add(edinet_code)
                    if company is not None and company.id is not None:
                        company_id_by_edinet_code[edinet_code] = int(company.id)
        for value in [
            getattr(instrument, "display_name", None),
            getattr(instrument, "legal_name", None),
            getattr(company, "name", None) if company is not None else None,
            getattr(issuer, "display_name", None) if issuer is not None else None,
            getattr(issuer, "legal_name", None) if issuer is not None else None,
        ]:
            text = re.sub(r"\s+", " ", str(value or "").strip())
            if text and len(text) >= 3:
                queries.add(text[:120])
                if not existing_edinet_codes:
                    priority_queries.add(text[:120])
                keywords.add(text[:80])

    if not candidates:
        return {
            "registered": False,
            "reason": "no_japanese_candidate_rows",
            "rows_scanned": len(rows),
            "candidate_count": 0,
            "stale_jp_identifiers_removed": stale_jp_identifiers_removed,
            "stale_jp_identifier_examples": stale_jp_identifier_examples,
        }

    if refresh_official_edinet_code_list and stock_codes and issuer_id_by_stock_code:
        try:
            code_list = fetch_edinet_code_list({})
            code_list_rows = [row for row in code_list.get("rows") or [] if isinstance(row, dict)]
            official_code_list_examined = len(code_list_rows)
            seen_stock_codes: set[str] = set()
            for code_row in code_list_rows:
                stock_code = _normalize_jp_stock_code(code_row.get("stock_code") or code_row.get("security_code"))
                edinet_code = _normalize_edinet_code(code_row.get("edinet_code"))
                if not stock_code or not edinet_code or stock_code not in issuer_id_by_stock_code or stock_code in seen_stock_codes:
                    continue
                seen_stock_codes.add(stock_code)
                issuer = session.get(Issuer, int(issuer_id_by_stock_code[stock_code]))
                if issuer is None:
                    continue
                official_code_list_matched += 1
                status, related_id = _upsert_issuer_identifier(
                    session,
                    issuer=issuer,
                    id_type="EDINET",
                    id_value=edinet_code,
                    exchange="JP",
                )
                if status == "created":
                    identifiers_created += 1
                    official_code_list_identifiers_created += 1
                elif status == "existing":
                    identifiers_existing += 1
                    official_code_list_identifiers_existing += 1
                elif status == "conflict":
                    identifiers_conflicts += 1
                    official_code_list_identifier_conflicts += 1
                    if len(official_code_list_examples) < 8:
                        official_code_list_examples.append(
                            {
                                "stock_code": stock_code,
                                "edinet_code": edinet_code,
                                "reason": "edinet_code_already_bound_to_other_issuer",
                                "other_issuer_id": related_id,
                            }
                        )
                    continue
                edinet_codes.add(edinet_code)
                if stock_code in company_id_by_stock_code:
                    company_id_by_edinet_code[edinet_code] = int(company_id_by_stock_code[stock_code])
                name = str(code_row.get("filer_name_en") or code_row.get("filer_name") or "").strip()
                if name and stock_code not in company_name_by_stock_code:
                    company_name_by_stock_code[stock_code] = name
                if len(official_code_list_examples) < 8:
                    official_code_list_examples.append(
                        {
                            "stock_code": stock_code,
                            "edinet_code": edinet_code,
                            "status": status,
                            "filer_name": code_row.get("filer_name"),
                            "filer_name_en": code_row.get("filer_name_en"),
                        }
                    )
        except Exception as exc:
            official_code_list_error = str(exc)[:500]

    if issuer_id_by_stock_code:
        edinet_source_ids = [
            int(source.id)
            for source in session.query(Source)
            .filter(Source.name.in_([_EDINETDB_ACTIVE_SOURCE_NAME, _EDINET_OFFICIAL_SOURCE_NAME]))
            .all()
            if source.id is not None
        ]
        seen_doc_pairs: set[tuple[str, str]] = set()
        if edinet_source_ids:
            docs = (
                session.query(Document)
                .filter(Document.source_id.in_(edinet_source_ids))
                .order_by(Document.id.desc())
                .limit(5000)
                .all()
            )
            for doc in docs:
                extra = _doc_extra(doc)
                payload = _payload(extra)
                data = payload if isinstance(payload, dict) else {}
                edinet_code = _normalize_edinet_code(
                    extra.get("edinet_code")
                    or data.get("edinet_code")
                    or data.get("edinetCode")
                    or _source_identifier_value(extra, "EDINET")
                )
                raw_stock_code = (
                    extra.get("stock_code")
                    or _source_identifier_value(extra, "JP_STOCK")
                    or extra.get("sec_code")
                    or data.get("stock_code")
                    or data.get("stockCode")
                    or data.get("sec_code")
                    or data.get("secCode")
                    or ""
                )
                stock_code = _normalize_jp_stock_code(raw_stock_code)
                if not edinet_code or not stock_code:
                    continue
                document_edinet_examined += 1
                if stock_code not in issuer_id_by_stock_code:
                    continue
                pair = (stock_code, edinet_code)
                if pair in seen_doc_pairs:
                    continue
                seen_doc_pairs.add(pair)
                issuer = session.get(Issuer, int(issuer_id_by_stock_code[stock_code]))
                if issuer is None:
                    continue
                status, related_id = _upsert_issuer_identifier(
                    session,
                    issuer=issuer,
                    id_type="EDINET",
                    id_value=edinet_code,
                    exchange="JP",
                )
                if status == "created":
                    identifiers_created += 1
                    document_edinet_identifiers_created += 1
                elif status == "existing":
                    identifiers_existing += 1
                    document_edinet_identifiers_existing += 1
                elif status == "conflict":
                    identifiers_conflicts += 1
                    document_edinet_identifier_conflicts += 1
                    if len(document_edinet_examples) < 8:
                        document_edinet_examples.append(
                            {
                                "document_id": int(doc.id),
                                "stock_code": stock_code,
                                "edinet_code": edinet_code,
                                "reason": "edinet_code_already_bound_to_other_issuer",
                                "other_issuer_id": related_id,
                            }
                        )
                    continue
                edinet_codes.add(edinet_code)
                if stock_code in company_id_by_stock_code:
                    company_id_by_edinet_code[edinet_code] = int(company_id_by_stock_code[stock_code])
                document_edinet_harvested += 1
                if len(document_edinet_examples) < 8:
                    document_edinet_examples.append(
                        {
                            "document_id": int(doc.id),
                            "stock_code": stock_code,
                            "edinet_code": edinet_code,
                            "status": status,
                        }
                    )

    def _merge_unique(existing: object, additions: set[str], *, limit_n: int) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for value in [*(existing if isinstance(existing, list) else []), *sorted(additions)]:
            text = str(value or "").strip()
            key = text.casefold()
            if text and key not in seen:
                seen.add(key)
                out.append(text)
            if len(out) >= limit_n:
                break
        return out

    def _managed_map(updates: dict[str, Any]) -> dict[str, Any]:
        return {str(key): updates[key] for key in sorted(updates)}

    def _source(name: str, connector: str, defaults: dict[str, Any]) -> tuple[Source, dict[str, Any], list[str]]:
        row = session.query(Source).filter_by(name=name).first()
        if row is None:
            row = Source(
                name=name,
                category="filings" if connector in {"edinetdb_financials", "edinet_disclosures"} else "exchange",
                priority=1,
                polling_interval_seconds=43200,
                config={},
                created_at=utcnow(),
            )
            session.add(row)
            session.flush()
        cfg = dict(row.config or {})
        before = {str(value).casefold() for value in (cfg.get("tracked_queries") or []) if str(value).strip()} if isinstance(cfg.get("tracked_queries"), list) else set()
        if connector in {"edinetdb_financials", "tdnet_company_announcements"}:
            if isinstance(cfg.get("priority_queries"), list):
                before.update(str(value).casefold() for value in cfg.get("priority_queries") or [] if str(value).strip())
        if connector == "edinetdb_financials":
            if isinstance(cfg.get("tracked_edinet_codes"), list):
                before.update(str(value).casefold() for value in cfg.get("tracked_edinet_codes") or [] if str(value).strip())
        cfg.update(defaults)
        cfg["connector"] = connector
        cfg["tracked_queries"] = _merge_unique([], queries, limit_n=700)
        if connector == "tdnet_company_announcements":
            cfg["priority_queries"] = _merge_unique([], stock_codes, limit_n=500)
            cfg["stock_codes"] = _merge_unique([], stock_codes, limit_n=500)
        if connector == "edinetdb_financials":
            cfg["tracked_edinet_codes"] = _merge_unique([], edinet_codes, limit_n=350)
            cfg["priority_queries"] = _merge_unique([], priority_queries, limit_n=500)
            cfg["company_id_by_edinet_code"] = _managed_map(company_id_by_edinet_code)
        cfg["company_id_by_stock_code"] = _managed_map(company_id_by_stock_code)
        cfg["company_slug_by_stock_code"] = _managed_map(company_slug_by_stock_code)
        cfg["issuer_id_by_stock_code"] = _managed_map(issuer_id_by_stock_code)
        cfg["issuer_name_by_stock_code"] = _managed_map(issuer_name_by_stock_code)
        cfg["ticker_by_stock_code"] = _managed_map(ticker_by_stock_code)
        cfg["company_name_by_stock_code"] = _managed_map(company_name_by_stock_code)
        source_keywords = keywords | stock_codes if connector == "tdnet_company_announcements" else keywords
        cfg["include_keywords"] = _merge_unique([], source_keywords, limit_n=900)
        cfg["enabled"] = bool(defaults.get("enabled", True))
        if cfg["enabled"]:
            cfg.pop("disabled_reason", None)
        elif defaults.get("disabled_reason"):
            cfg["disabled_reason"] = str(defaults.get("disabled_reason"))
        cfg["managed_by"] = "source_pack"
        cfg["coverage_scope"] = "active_universe"
        cfg["last_registered_reason"] = reason
        cfg["source_quality"] = {
            **(cfg.get("source_quality") if isinstance(cfg.get("source_quality"), dict) else {}),
            "authority": "primary_regulator" if connector in {"edinetdb_financials", "edinet_disclosures"} else "primary_exchange",
            "timeliness": "scheduled",
            "acquisition_fragility": "low",
            "rights_class": "public",
        }
        row.config = cfg
        session.flush()
        after = {str(value).casefold() for value in (cfg.get("tracked_queries") or []) if str(value).strip()}
        if connector == "tdnet_company_announcements":
            after.update(str(value).casefold() for value in cfg.get("priority_queries") or [] if str(value).strip())
        if connector == "edinetdb_financials":
            after.update(str(value).casefold() for value in cfg.get("priority_queries") or [] if str(value).strip())
            after.update(str(value).casefold() for value in cfg.get("tracked_edinet_codes") or [] if str(value).strip())
        return row, cfg, sorted(after - before)

    tdnet_source, tdnet_cfg, tdnet_added = _source(
        _TDNET_ACTIVE_SOURCE_NAME,
        "tdnet_company_announcements",
        {
            "lookback_days": 730,
            "priority_queries_per_run": 8,
            "queries_per_run": 32,
            "max_pages_per_query": 2,
            "max_items_per_run": 240,
            "prefetch_outside_transaction": True,
            "fetch_article": False,
            "download_article_images": False,
            "store_raw_html_on_failure": True,
            "refresh_existing": True,
            "auto_translate": True,
            "universe_tracking_enabled": True,
            "universe_backfill_days": 730,
        },
    )
    edinet_source, edinet_cfg, edinet_added = _source(
        _EDINETDB_ACTIVE_SOURCE_NAME,
        "edinetdb_financials",
        {
            "companies_per_run": 8,
            "tracked_companies_per_run": 8,
            "priority_companies_per_run": 4,
            "priority_queries_per_run": 4,
            "tracked_codes_per_run": 4,
            "tracked_queries_per_run": 0,
            "years": 2,
            "period": "annual",
            "earnings_limit": 8,
            "search_limit": 3,
            "max_items_per_run": 60,
            "rate_limit_backoff_seconds": 21600,
            "prefetch_outside_transaction": True,
            "fetch_article": False,
            "download_article_images": False,
            "refresh_existing": True,
            "auto_translate": True,
            "include_qualitative_text": True,
            "universe_tracking_enabled": True,
            "universe_backfill_years": 2,
        },
    )
    official_edinet_key = str(getattr(settings, "edinet_subscription_key", None) or "").strip()
    official_edinet_enabled = bool(official_edinet_key and not official_edinet_key.startswith("edb_"))
    official_edinet_source, official_edinet_cfg, official_edinet_added = _source(
        _EDINET_OFFICIAL_SOURCE_NAME,
        "edinet_disclosures",
        {
            "document_list_type": "2",
            "lookback_days": 14,
            "max_items_per_run": 120,
            "prefetch_outside_transaction": True,
            "fetch_article": False,
            "download_article_images": False,
            "refresh_existing": True,
            "auto_translate": True,
            "universe_tracking_enabled": True,
            "enabled": official_edinet_enabled,
            **({} if official_edinet_enabled else {"disabled_reason": "requires THOTH_EDINET_SUBSCRIPTION_KEY"}),
        },
    )
    for source, added in [(tdnet_source, tdnet_added), (edinet_source, edinet_added), (official_edinet_source, official_edinet_added)]:
        if added:
            state = session.query(SourceState).filter_by(source_id=int(source.id)).first()
            if state is not None:
                state.cursor = {}
    session.flush()
    return {
        "registered": True,
        "rows_scanned": len(rows),
        "candidate_count": candidates,
        "stock_codes_registered": len(stock_codes),
        "edinet_codes_registered": len(edinet_codes),
        "identifiers_created": identifiers_created,
        "identifiers_existing": identifiers_existing,
        "identifier_conflicts": identifiers_conflicts,
        "stale_jp_identifiers_removed": stale_jp_identifiers_removed,
        "stale_jp_identifier_examples": stale_jp_identifier_examples,
        "document_edinet_identifier_harvest": {
            "documents_examined": document_edinet_examined,
            "pairs_harvested": document_edinet_harvested,
            "identifiers_created": document_edinet_identifiers_created,
            "identifiers_existing": document_edinet_identifiers_existing,
            "identifier_conflicts": document_edinet_identifier_conflicts,
            "examples": document_edinet_examples,
        },
        "official_edinet_code_list_harvest": {
            "enabled": bool(refresh_official_edinet_code_list),
            "rows_examined": official_code_list_examined,
            "pairs_harvested": official_code_list_matched,
            "identifiers_created": official_code_list_identifiers_created,
            "identifiers_existing": official_code_list_identifiers_existing,
            "identifier_conflicts": official_code_list_identifier_conflicts,
            "error": official_code_list_error,
            "examples": official_code_list_examples,
        },
        "tdnet_source": {
            "source_id": int(tdnet_source.id),
            "source_name": tdnet_source.name,
            "tracked_query_count": len(tdnet_cfg.get("tracked_queries") or []),
            "tracked_queries_added": len(tdnet_added),
            "priority_query_count": len(tdnet_cfg.get("priority_queries") or []),
        },
        "edinetdb_source": {
            "source_id": int(edinet_source.id),
            "source_name": edinet_source.name,
            "tracked_query_count": len(edinet_cfg.get("tracked_queries") or []),
            "tracked_queries_added": len(edinet_added),
            "tracked_edinet_code_count": len(edinet_cfg.get("tracked_edinet_codes") or []),
            "priority_query_count": len(edinet_cfg.get("priority_queries") or []),
        },
        "edinet_official_source": {
            "source_id": int(official_edinet_source.id),
            "source_name": official_edinet_source.name,
            "enabled": bool(official_edinet_cfg.get("enabled", True)),
            "disabled_reason": official_edinet_cfg.get("disabled_reason"),
            "tracked_query_count": len(official_edinet_cfg.get("tracked_queries") or []),
            "tracked_queries_added": len(official_edinet_added),
        },
        "examples": examples,
    }


def ensure_active_universe_taiwan_sources(
    session: Session,
    *,
    universe_slug: str | None = None,
    limit: int | None = None,
    reason: str = "active_universe_source_pack",
) -> dict[str, Any]:
    """Refresh official TWSE/MOPS sources from active Taiwanese universe rows."""
    q = (
        session.query(ResearchUniverseRow)
        .join(ResearchUniverse, ResearchUniverse.id == ResearchUniverseRow.universe_id)
        .filter(ResearchUniverse.status == "active", ResearchUniverseRow.archived_at.is_(None))
        .order_by(ResearchUniverse.sort_order.asc(), ResearchUniverseRow.sort_order.asc(), ResearchUniverseRow.id.asc())
    )
    if universe_slug:
        q = q.filter(ResearchUniverse.slug == str(universe_slug).strip())
    rows = q.all()
    if limit is not None:
        rows = rows[: max(1, int(limit))]

    stock_codes: set[str] = set()
    keywords: set[str] = set()
    company_id_by_stock_code: dict[str, int] = {}
    company_slug_by_stock_code: dict[str, str] = {}
    ticker_by_stock_code: dict[str, str] = {}
    company_name_by_stock_code: dict[str, str] = {}
    exchange_by_stock_code: dict[str, str] = {}
    candidates = 0
    identifiers_created = identifiers_existing = identifiers_conflicts = 0
    identity_updates = 0
    profile_updates = 0
    examples: list[dict[str, Any]] = []
    for row in rows:
        instrument = row.instrument
        if instrument is None or not bool(getattr(instrument, "is_public", True)):
            continue
        company = session.get(Company, int(instrument.company_id)) if instrument.company_id is not None else None
        issuer = session.get(Issuer, int(instrument.issuer_id)) if instrument.issuer_id is not None else None
        if issuer is None and company is not None and company.issuer_id is not None:
            issuer = session.get(Issuer, int(company.issuer_id))
        exchange = _normalize_exchange_code(getattr(instrument, "exchange", None) or getattr(company, "exchange", None))
        country = str(getattr(instrument, "country", None) or getattr(company, "region", None) or getattr(issuer, "jurisdiction", None) or "").strip().upper()
        ticker = getattr(instrument, "ticker", None) or getattr(company, "ticker", None) or getattr(instrument, "yahoo_symbol", None)
        ticker_text = str(ticker or "").strip().upper()
        detected_stock_code = _normalize_tw_stock_code(ticker)
        existing_stock_codes = _issuer_identifier_values(issuer, "TW_STOCK") if issuer is not None else []
        has_taiwan_suffix = bool(re.search(r"\.(TW|TWO|TPE)$", ticker_text))
        known_taiwan_code = bool(
            (detected_stock_code and detected_stock_code in TAIWAN_COMPANY_EN)
            or any(_normalize_tw_stock_code(value) in TAIWAN_COMPANY_EN for value in existing_stock_codes)
        )
        taiwan_context = (
            exchange in {"TWSE", "TPEX", "TWO", "ROCO"}
            or country in {"TW", "TAIWAN"}
            or has_taiwan_suffix
            or known_taiwan_code
            or bool(existing_stock_codes)
        )
        if not taiwan_context:
            continue
        if issuer is not None and not str(issuer.jurisdiction or "").strip():
            issuer.jurisdiction = "TW"
            issuer.updated_at = utcnow()
            identity_updates += 1
        if company is not None:
            if not str(company.region or "").strip():
                company.region = "TW"
                identity_updates += 1
            if not str(company.exchange or "").strip() and (detected_stock_code or existing_stock_codes):
                company.exchange = "TPEX" if exchange == "TPEX" else "TWSE"
                identity_updates += 1
        selected_codes = existing_stock_codes or ([detected_stock_code] if detected_stock_code else [])
        candidates += 1
        if not selected_codes:
            if len(examples) < 8:
                examples.append(
                    {
                        "instrument_id": int(instrument.id),
                        "display_name": instrument.display_name,
                        "ticker": ticker,
                        "exchange": instrument.exchange,
                        "reason": "missing_tw_stock_code",
                    }
                )
            continue
        for stock_code in selected_codes:
            normalized = _normalize_tw_stock_code(stock_code)
            if not normalized:
                continue
            stock_codes.add(normalized)
            keywords.add(normalized)
            if issuer is not None:
                status, related_id = _upsert_issuer_identifier(
                    session,
                    issuer=issuer,
                    id_type="TW_STOCK",
                    id_value=normalized,
                    exchange="TWSE" if exchange != "TPEX" else "TPEX",
                )
                if status == "created":
                    identifiers_created += 1
                elif status == "existing":
                    identifiers_existing += 1
                elif status == "conflict":
                    identifiers_conflicts += 1
                    if len(examples) < 8:
                        examples.append(
                            {
                                "instrument_id": int(instrument.id),
                                "display_name": instrument.display_name,
                                "stock_code": normalized,
                                "reason": "tw_stock_already_bound_to_other_issuer",
                                "other_issuer_id": related_id,
                            }
                        )
                    continue
            if company is not None:
                company_slug_by_stock_code[normalized] = company.slug
                if company.id is not None:
                    company_id_by_stock_code[normalized] = int(company.id)
                if company.ticker:
                    ticker_by_stock_code[normalized] = str(company.ticker).strip()
                if company.name:
                    company_name_by_stock_code[normalized] = str(company.name).strip()
                profile_override = _TAIWAN_SOURCE_PROFILE_OVERRIDES.get(normalized)
                if profile_override:
                    for field in ("ir_url", "reports_url", "calendar_url"):
                        value = str(profile_override.get(field) or "").strip()
                        if value and not str(getattr(company, field, None) or "").strip():
                            setattr(company, field, value)
                            profile_updates += 1
                    if issuer is not None:
                        issuer_website = str(profile_override.get("issuer_website") or "").strip()
                        issuer_ir_website = str(profile_override.get("issuer_ir_website") or "").strip()
                        if issuer_website and not str(issuer.website or "").strip():
                            issuer.website = issuer_website
                            profile_updates += 1
                        if issuer_ir_website and not str(issuer.ir_website or "").strip():
                            issuer.ir_website = issuer_ir_website
                            profile_updates += 1
                        issuer.updated_at = utcnow()
            exchange_by_stock_code[normalized] = "TPEX" if exchange == "TPEX" else "TWSE"
            if issuer is not None and normalized not in company_name_by_stock_code:
                company_name_by_stock_code[normalized] = str(issuer.display_name or issuer.legal_name or "").strip()
        for value in [
            getattr(instrument, "display_name", None),
            getattr(instrument, "legal_name", None),
            getattr(company, "name", None) if company is not None else None,
            getattr(issuer, "display_name", None) if issuer is not None else None,
            getattr(issuer, "legal_name", None) if issuer is not None else None,
        ]:
            text = re.sub(r"\s+", " ", str(value or "").strip())
            if text and len(text) >= 3:
                keywords.add(text[:120])

    if not candidates:
        return {"registered": False, "reason": "no_taiwan_candidate_rows", "rows_scanned": len(rows), "candidate_count": 0}

    def _merge_unique(existing: object, additions: set[str], *, limit_n: int) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for value in [*(existing if isinstance(existing, list) else []), *sorted(additions)]:
            text = str(value or "").strip()
            key = text.casefold()
            if text and key not in seen:
                seen.add(key)
                out.append(text)
            if len(out) >= limit_n:
                break
        return out

    def _merge_map(cfg: dict[str, Any], key: str, updates: dict[str, Any]) -> dict[str, Any]:
        current = dict(cfg.get(key) if isinstance(cfg.get(key), dict) else {})
        for stock_code, value in updates.items():
            normalized = _normalize_tw_stock_code(stock_code)
            if normalized and value is not None and str(value).strip():
                current[normalized] = value
        return current

    def _source(
        name: str,
        connector: str,
        defaults: dict[str, Any],
        *,
        selected_stock_codes: set[str] | None = None,
        selected_keywords: set[str] | None = None,
    ) -> tuple[Source, dict[str, Any], list[str], list[str]]:
        source_stock_codes = set(stock_codes if selected_stock_codes is None else selected_stock_codes)
        source_keywords = set((keywords | source_stock_codes) if selected_keywords is None else selected_keywords | source_stock_codes)
        source_category = "filings" if connector == "taiwan_mops_financial_reports" else "exchange"
        row = session.query(Source).filter_by(name=name).first()
        if row is None:
            row = Source(
                name=name,
                category=source_category,
                priority=1,
                polling_interval_seconds=21600,
                config={},
                created_at=utcnow(),
            )
            session.add(row)
            session.flush()
        elif row.category != source_category:
            row.category = source_category
        cfg = dict(row.config or {})
        existing_keywords = {str(value).casefold() for value in (cfg.get("include_keywords") or []) if str(value).strip()} if isinstance(cfg.get("include_keywords"), list) else set()
        existing_codes = {str(value).strip() for value in (cfg.get("company_codes") or []) if str(value).strip()} if isinstance(cfg.get("company_codes"), list) else set()
        cfg.update(defaults)
        cfg["connector"] = connector
        cfg["include_keywords"] = _merge_unique(cfg.get("include_keywords"), source_keywords, limit_n=1000)
        cfg["company_codes"] = _merge_unique(cfg.get("company_codes"), source_stock_codes, limit_n=500)
        if connector == "taiwan_mops_monthly_revenue":
            cfg["stats_company_codes"] = _merge_unique(cfg.get("stats_company_codes"), source_stock_codes, limit_n=500)
            cfg["document_company_codes"] = _merge_unique(cfg.get("document_company_codes"), source_stock_codes, limit_n=500)
        cfg["company_id_by_stock_code"] = _merge_map(cfg, "company_id_by_stock_code", company_id_by_stock_code)
        cfg["company_slug_by_stock_code"] = _merge_map(cfg, "company_slug_by_stock_code", company_slug_by_stock_code)
        cfg["ticker_by_stock_code"] = _merge_map(cfg, "ticker_by_stock_code", ticker_by_stock_code)
        cfg["company_name_by_stock_code"] = _merge_map(cfg, "company_name_by_stock_code", company_name_by_stock_code)
        cfg["exchange_by_stock_code"] = _merge_map(cfg, "exchange_by_stock_code", exchange_by_stock_code)
        cfg["enabled"] = True
        cfg["managed_by"] = "source_pack"
        cfg["coverage_scope"] = "active_universe"
        cfg["last_registered_reason"] = reason
        cfg["source_quality"] = {
            **(cfg.get("source_quality") if isinstance(cfg.get("source_quality"), dict) else {}),
            "authority": "primary_exchange",
            "timeliness": "scheduled",
            "acquisition_fragility": "low",
            "rights_class": "public",
        }
        row.config = cfg
        session.flush()
        after_keywords = {str(value).casefold() for value in (cfg.get("include_keywords") or []) if str(value).strip()}
        after_codes = {str(value).strip() for value in (cfg.get("company_codes") or []) if str(value).strip()}
        added_keywords = sorted(after_keywords - existing_keywords)
        added_codes = sorted(after_codes - existing_codes)
        if added_keywords or added_codes:
            state = session.query(SourceState).filter_by(source_id=int(row.id)).first()
            if state is not None:
                state.cursor = {}
        return row, cfg, added_keywords, added_codes

    mops_source, mops_cfg, mops_keywords_added, mops_codes_added = _source(
        _TAIWAN_MOPS_MONTHLY_SOURCE_NAME,
        "taiwan_mops_monthly_revenue",
        {
            "markets": ["listed", "public"],
            "max_items_per_run": 120,
            "prefetch_outside_transaction": True,
            "fetch_article": False,
            "download_article_images": False,
            "refresh_existing": True,
        },
    )
    financial_source, financial_cfg, financial_keywords_added, financial_codes_added = _source(
        _TAIWAN_MOPS_FINANCIAL_SOURCE_NAME,
        "taiwan_mops_financial_reports",
        {
            "markets": ["listed", "otc"],
            "max_items_per_run": 80,
            "max_financial_items_per_run": 60,
            "annual_company_limit_per_run": 8,
            "annual_report_years_back": 3,
            "prefetch_outside_transaction": True,
            "defer_ingest_derived_sync": True,
            "fetch_article": False,
            "download_article_images": False,
            "refresh_existing": False,
        },
    )
    conference_source, conference_cfg, conference_keywords_added, conference_codes_added = _source(
        _TAIWAN_MOPS_CONFERENCE_SOURCE_NAME,
        "taiwan_mops_investor_conferences",
        {
            "markets": ["listed", "otc"],
            "months_back": 8,
            "max_items_per_run": 500,
            "requests_per_run": 4,
            "prefetch_outside_transaction": True,
            "fetch_article": False,
            "download_article_images": False,
            "store_raw_html_on_failure": True,
            "refresh_existing": True,
        },
    )
    twse_source, twse_cfg, twse_keywords_added, twse_codes_added = _source(
        _TWSE_MATERIAL_SOURCE_NAME,
        "twse_openapi",
        {
            "data_url": "https://openapi.twse.com.tw/v1/opendata/t187ap04_L",
            "max_items_per_run": 120,
            "fetch_article": False,
            "download_article_images": False,
            "store_raw_html_on_failure": True,
            "refresh_existing": True,
        },
    )
    tpex_stock_codes = {code for code, exchange in exchange_by_stock_code.items() if exchange == "TPEX"}
    tpex_keywords = {
        value
        for code in tpex_stock_codes
        for value in (code, company_name_by_stock_code.get(code, ""))
        if str(value or "").strip()
    }
    tpex_source, tpex_cfg, tpex_keywords_added, tpex_codes_added = _source(
        _TPEX_INFOHUB_SOURCE_NAME,
        "tpex_infohub_company",
        {
            "sections": ["conferences"],
            "max_items_per_run": 120,
            "max_company_codes_per_run": 120,
            "fetch_article": False,
            "download_article_images": False,
            "store_raw_html_on_failure": True,
            "refresh_existing": True,
        },
        selected_stock_codes=tpex_stock_codes,
        selected_keywords=tpex_keywords,
    )
    session.flush()
    return {
        "registered": True,
        "rows_scanned": len(rows),
        "candidate_count": candidates,
        "stock_codes_registered": len(stock_codes),
        "identifiers_created": identifiers_created,
        "identifiers_existing": identifiers_existing,
        "identifier_conflicts": identifiers_conflicts,
        "identity_updates": identity_updates,
        "profile_updates": profile_updates,
        "mops_monthly_source": {
            "source_id": int(mops_source.id),
            "source_name": mops_source.name,
            "company_code_count": len(mops_cfg.get("company_codes") or []),
            "company_codes_added": len(mops_codes_added),
            "include_keywords_added": len(mops_keywords_added),
        },
        "mops_financial_source": {
            "source_id": int(financial_source.id),
            "source_name": financial_source.name,
            "company_code_count": len(financial_cfg.get("company_codes") or []),
            "company_codes_added": len(financial_codes_added),
            "include_keywords_added": len(financial_keywords_added),
        },
        "mops_conference_source": {
            "source_id": int(conference_source.id),
            "source_name": conference_source.name,
            "company_code_count": len(conference_cfg.get("company_codes") or []),
            "company_codes_added": len(conference_codes_added),
            "include_keywords_added": len(conference_keywords_added),
        },
        "twse_material_source": {
            "source_id": int(twse_source.id),
            "source_name": twse_source.name,
            "company_code_count": len(twse_cfg.get("company_codes") or []),
            "company_codes_added": len(twse_codes_added),
            "include_keywords_added": len(twse_keywords_added),
        },
        "tpex_infohub_source": {
            "source_id": int(tpex_source.id),
            "source_name": tpex_source.name,
            "company_code_count": len(tpex_cfg.get("company_codes") or []),
            "company_codes_added": len(tpex_codes_added),
            "include_keywords_added": len(tpex_keywords_added),
        },
        "examples": examples,
    }


def ensure_active_universe_hong_kong_sources(
    session: Session,
    *,
    universe_slug: str | None = None,
    limit: int | None = None,
    reason: str = "active_universe_source_pack",
    active_stock_id_by_code: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Refresh official HKEXnews title-search source from active Hong Kong universe rows."""
    q = (
        session.query(ResearchUniverseRow)
        .join(ResearchUniverse, ResearchUniverse.id == ResearchUniverseRow.universe_id)
        .filter(ResearchUniverse.status == "active", ResearchUniverseRow.archived_at.is_(None))
        .order_by(ResearchUniverse.sort_order.asc(), ResearchUniverseRow.sort_order.asc(), ResearchUniverseRow.id.asc())
    )
    if universe_slug:
        q = q.filter(ResearchUniverse.slug == str(universe_slug).strip())
    rows = q.all()
    if limit is not None:
        rows = rows[: max(1, int(limit))]

    stock_codes: set[str] = set()
    keywords: set[str] = set()
    company_id_by_stock_code: dict[str, int] = {}
    company_slug_by_stock_code: dict[str, str] = {}
    ticker_by_stock_code: dict[str, str] = {}
    company_name_by_stock_code: dict[str, str] = {}
    stock_query_id_by_stock_code: dict[str, str] = {}
    stock_code_by_stock_query_id: dict[str, str] = {}
    candidates = 0
    identifiers_created = identifiers_existing = identifiers_conflicts = 0
    examples: list[dict[str, Any]] = []
    for row in rows:
        instrument = row.instrument
        if instrument is None or not bool(getattr(instrument, "is_public", True)):
            continue
        company = session.get(Company, int(instrument.company_id)) if instrument.company_id is not None else None
        issuer = session.get(Issuer, int(instrument.issuer_id)) if instrument.issuer_id is not None else None
        if issuer is None and company is not None and company.issuer_id is not None:
            issuer = session.get(Issuer, int(company.issuer_id))
        exchange = _normalize_exchange_code(getattr(instrument, "exchange", None) or getattr(company, "exchange", None))
        country = str(getattr(instrument, "country", None) or getattr(company, "region", None) or getattr(issuer, "jurisdiction", None) or "").strip().upper()
        ticker = getattr(instrument, "ticker", None) or getattr(company, "ticker", None) or getattr(instrument, "yahoo_symbol", None)
        ticker_text = str(ticker or "").strip().upper()
        yahoo_text = str(getattr(instrument, "yahoo_symbol", None) or "").strip().upper()
        detected_stock_code = _normalize_hk_stock_code(ticker) or _normalize_hk_stock_code(yahoo_text)
        existing_stock_codes = _issuer_identifier_values(issuer, "HK_STOCK") if issuer is not None else []
        hk_context = (
            exchange in {"SEHK", "HKEX"}
            or country in {"HK", "HONG KONG"}
            or ticker_text.endswith(".HK")
            or yahoo_text.endswith(".HK")
            or bool(existing_stock_codes)
        )
        if not hk_context:
            continue
        candidates += 1
        selected_codes = existing_stock_codes or ([detected_stock_code] if detected_stock_code else [])
        if not selected_codes:
            if len(examples) < 8:
                examples.append(
                    {
                        "instrument_id": int(instrument.id),
                        "display_name": instrument.display_name,
                        "ticker": ticker,
                        "exchange": instrument.exchange,
                        "reason": "missing_hk_stock_code",
                    }
                )
            continue
        for stock_code in selected_codes:
            normalized = _normalize_hk_stock_code(stock_code)
            if not normalized:
                continue
            stock_codes.add(normalized)
            keywords.add(normalized)
            keywords.add(normalized.lstrip("0"))
            if issuer is not None:
                status, related_id = _upsert_issuer_identifier(
                    session,
                    issuer=issuer,
                    id_type="HK_STOCK",
                    id_value=normalized,
                    exchange="HKEX",
                )
                if status == "created":
                    identifiers_created += 1
                elif status == "existing":
                    identifiers_existing += 1
                elif status == "conflict":
                    identifiers_conflicts += 1
                    if len(examples) < 8:
                        examples.append(
                            {
                                "instrument_id": int(instrument.id),
                                "display_name": instrument.display_name,
                                "stock_code": normalized,
                                "reason": "hk_stock_already_bound_to_other_issuer",
                                "other_issuer_id": related_id,
                            }
                        )
                    continue
            if company is not None:
                company_slug_by_stock_code[normalized] = company.slug
                if company.id is not None:
                    company_id_by_stock_code[normalized] = int(company.id)
                if company.ticker:
                    ticker_by_stock_code[normalized] = str(company.ticker).strip()
                if company.name:
                    company_name_by_stock_code[normalized] = str(company.name).strip()
            if issuer is not None and normalized not in company_name_by_stock_code:
                company_name_by_stock_code[normalized] = str(issuer.display_name or issuer.legal_name or "").strip()
        for value in [
            getattr(instrument, "display_name", None),
            getattr(instrument, "legal_name", None),
            getattr(company, "name", None) if company is not None else None,
            getattr(issuer, "display_name", None) if issuer is not None else None,
            getattr(issuer, "legal_name", None) if issuer is not None else None,
        ]:
            text = re.sub(r"\s+", " ", str(value or "").strip())
            if text and len(text) >= 3:
                keywords.add(text[:120])

    if not candidates:
        return {"registered": False, "reason": "no_hong_kong_candidate_rows", "rows_scanned": len(rows), "candidate_count": 0}

    hkex_lookup_error: str | None = None
    if stock_codes:
        lookup = dict(active_stock_id_by_code or {})
        if active_stock_id_by_code is None:
            try:
                lookup, official_names = _fetch_hkex_active_stock_id_map()
                for code, name in official_names.items():
                    if code in stock_codes and code not in company_name_by_stock_code:
                        company_name_by_stock_code[code] = name
            except Exception as exc:
                lookup = {}
                hkex_lookup_error = f"{type(exc).__name__}: {exc}"[:240]
        normalized_lookup: dict[str, str] = {}
        for raw_code, raw_query_id in lookup.items():
            code = _normalize_hk_stock_code(raw_code)
            query_id = _normalize_hkex_stock_query_id(raw_query_id)
            if code and query_id:
                normalized_lookup[code] = query_id
        for code in sorted(stock_codes):
            query_id = normalized_lookup.get(code)
            if not query_id:
                continue
            stock_query_id_by_stock_code[code] = query_id
            stock_code_by_stock_query_id[query_id] = code

    source = session.query(Source).filter_by(name=_HKEXNEWS_ACTIVE_SOURCE_NAME).first()
    if source is None:
        source = Source(
            name=_HKEXNEWS_ACTIVE_SOURCE_NAME,
            category="exchange",
            priority=1,
            polling_interval_seconds=21600,
            config={},
            created_at=utcnow(),
        )
        session.add(source)
        session.flush()
    cfg = dict(source.config or {})

    def _merge_unique(existing: object, additions: set[str], *, limit_n: int) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for value in [*(existing if isinstance(existing, list) else []), *sorted(additions)]:
            text = str(value or "").strip()
            key = text.casefold()
            if text and key not in seen:
                seen.add(key)
                out.append(text)
            if len(out) >= limit_n:
                break
        return out

    def _merge_map(key: str, updates: dict[str, Any]) -> dict[str, Any]:
        current = dict(cfg.get(key) if isinstance(cfg.get(key), dict) else {})
        for stock_code, value in updates.items():
            normalized = _normalize_hk_stock_code(stock_code)
            if normalized and value is not None and str(value).strip():
                current[normalized] = value
        return current

    def _merge_plain_map(key: str, updates: dict[str, Any]) -> dict[str, Any]:
        current = dict(cfg.get(key) if isinstance(cfg.get(key), dict) else {})
        for raw_key, value in updates.items():
            text = str(raw_key or "").strip()
            if text and value is not None and str(value).strip():
                current[text] = value
        return current

    existing_codes = {str(value).strip() for value in (cfg.get("stock_ids") or []) if str(value).strip()} if isinstance(cfg.get("stock_ids"), list) else set()
    existing_query_ids = {
        str(value).strip()
        for value in (cfg.get("stock_query_id_by_stock_code") if isinstance(cfg.get("stock_query_id_by_stock_code"), dict) else {}).values()
        if str(value).strip()
    }
    cfg.update(
        {
            "connector": "hkexnews_title_search",
            "market": "SEHK",
            "lookback_days": 730,
            "stock_ids_per_run": 4,
            "max_items_per_run": 120,
            "sort_dir": "0",
            "priority_title_queries": [
                "ANNUAL REPORT",
                "INTERIM REPORT",
                "ANNUAL RESULTS",
                "INTERIM RESULTS",
                "QUARTERLY RESULTS",
            ],
            "priority_title_query_limit": 5,
            "fetch_article": False,
            "download_article_images": False,
            "store_raw_html_on_failure": True,
            "refresh_existing": True,
            "enabled": True,
            "managed_by": "source_pack",
            "coverage_scope": "active_universe",
            "last_registered_reason": reason,
            "stock_ids": _merge_unique(cfg.get("stock_ids"), stock_codes, limit_n=700),
            "include_keywords": _merge_unique(cfg.get("include_keywords"), keywords | {code.lstrip("0") for code in stock_codes}, limit_n=1200),
            "stock_query_id_by_stock_code": _merge_map("stock_query_id_by_stock_code", stock_query_id_by_stock_code),
            "stock_code_by_stock_query_id": _merge_plain_map("stock_code_by_stock_query_id", stock_code_by_stock_query_id),
            "hkex_active_stock_list_url": _HKEX_ACTIVE_STOCK_LIST_URL,
            "hkex_stock_query_id_resolved_count": len(stock_query_id_by_stock_code),
            "company_id_by_stock_code": _merge_map("company_id_by_stock_code", company_id_by_stock_code),
            "company_slug_by_stock_code": _merge_map("company_slug_by_stock_code", company_slug_by_stock_code),
            "ticker_by_stock_code": _merge_map("ticker_by_stock_code", ticker_by_stock_code),
            "company_name_by_stock_code": _merge_map("company_name_by_stock_code", company_name_by_stock_code),
            "source_quality": {
                **(cfg.get("source_quality") if isinstance(cfg.get("source_quality"), dict) else {}),
                "authority": "primary_exchange",
                "timeliness": "scheduled",
                "acquisition_fragility": "medium",
                "rights_class": "public",
            },
        }
    )
    if hkex_lookup_error:
        cfg["hkex_stock_query_id_lookup_error"] = hkex_lookup_error
    else:
        cfg.pop("hkex_stock_query_id_lookup_error", None)
    cfg["hkex_stock_codes_without_query_id"] = sorted(code for code in stock_codes if code not in stock_query_id_by_stock_code)
    source.config = cfg
    session.flush()
    after_codes = {str(value).strip() for value in (cfg.get("stock_ids") or []) if str(value).strip()}
    after_query_ids = {
        str(value).strip()
        for value in (cfg.get("stock_query_id_by_stock_code") if isinstance(cfg.get("stock_query_id_by_stock_code"), dict) else {}).values()
        if str(value).strip()
    }
    added_codes = sorted(after_codes - existing_codes)
    added_query_ids = sorted(after_query_ids - existing_query_ids)
    if added_codes or added_query_ids:
        state = session.query(SourceState).filter_by(source_id=int(source.id)).first()
        if state is not None:
            state.cursor = {}
    session.flush()
    return {
        "registered": True,
        "rows_scanned": len(rows),
        "candidate_count": candidates,
        "stock_codes_registered": len(stock_codes),
        "identifiers_created": identifiers_created,
        "identifiers_existing": identifiers_existing,
        "identifier_conflicts": identifiers_conflicts,
        "hkexnews_source": {
            "source_id": int(source.id),
            "source_name": source.name,
            "stock_id_count": len(cfg.get("stock_ids") or []),
            "stock_ids_added": len(added_codes),
            "stock_query_id_count": len(cfg.get("stock_query_id_by_stock_code") or {}),
            "stock_query_ids_added": len(added_query_ids),
            "stock_query_id_lookup_error": hkex_lookup_error,
        },
        "examples": examples,
    }


def ensure_active_universe_esef_sources(
    session: Session,
    *,
    universe_slug: str | None = None,
    limit: int | None = None,
    reason: str = "active_universe_source_pack",
    resolve_missing_leis: bool = True,
    max_lei_resolution_queries: int = 80,
) -> dict[str, Any]:
    """Refresh LEI-keyed ESEF/UKSEF filing source from active European and UK universe rows."""
    q = (
        session.query(ResearchUniverseRow)
        .join(ResearchUniverse, ResearchUniverse.id == ResearchUniverseRow.universe_id)
        .filter(ResearchUniverse.status == "active", ResearchUniverseRow.archived_at.is_(None))
        .order_by(ResearchUniverse.sort_order.asc(), ResearchUniverseRow.sort_order.asc(), ResearchUniverseRow.id.asc())
    )
    if universe_slug:
        q = q.filter(ResearchUniverse.slug == str(universe_slug).strip())
    rows = q.all()
    if limit is not None:
        rows = rows[: max(1, int(limit))]

    esef_scope_suffixes = set(_GLEIF_SUFFIX_COUNTRY) | {"DU"}
    eu_suffix_re = re.compile(
        rf"\.({'|'.join(sorted((re.escape(key) for key in esef_scope_suffixes), key=len, reverse=True))})\b",
        re.IGNORECASE,
    )
    eu_countries = set(_GLEIF_ESEF_COUNTRIES) | {"UK"}
    eu_exchanges = set(_GLEIF_EXCHANGE_COUNTRY) | {
        "EURONEXT",
        "PARIS",
    }
    leis: set[str] = set()
    countries: set[str] = set()
    keywords: set[str] = set()
    company_id_by_lei: dict[str, int] = {}
    company_slug_by_lei: dict[str, str] = {}
    ticker_by_lei: dict[str, str] = {}
    issuer_name_by_lei: dict[str, str] = {}
    candidates = 0
    missing_lei = 0
    lei_resolution_attempted = 0
    lei_resolution_queries_used = 0
    lei_resolution_statuses: dict[str, int] = {}
    resolved_examples: list[dict[str, Any]] = []
    unresolved_examples: list[dict[str, Any]] = []
    resolved_resolution_issuer_ids: set[int] = set()
    seen_resolution_contexts: set[tuple[int, str, str, str, str]] = set()
    examples: list[dict[str, Any]] = []
    for row in rows:
        instrument = row.instrument
        if instrument is None or not bool(getattr(instrument, "is_public", True)):
            continue
        company = session.get(Company, int(instrument.company_id)) if instrument.company_id is not None else None
        issuer = session.get(Issuer, int(instrument.issuer_id)) if instrument.issuer_id is not None else None
        if issuer is None and company is not None and company.issuer_id is not None:
            issuer = session.get(Issuer, int(company.issuer_id))
        exchange = _normalize_exchange_code(getattr(instrument, "exchange", None) or getattr(company, "exchange", None))
        country_raw = str(getattr(instrument, "country", None) or getattr(company, "region", None) or getattr(issuer, "jurisdiction", None) or "").strip().upper()
        ticker = str(getattr(instrument, "ticker", None) or getattr(company, "ticker", None) or getattr(instrument, "yahoo_symbol", None) or "").strip().upper()
        yahoo = str(getattr(instrument, "yahoo_symbol", None) or "").strip().upper()
        context_text = " ".join([exchange, country_raw, ticker, yahoo])
        depositary_receipt_gleif_candidate = _is_esef_depositary_receipt_gleif_candidate(
            instrument=instrument,
            company=company,
            issuer=issuer,
        )
        in_scope = (
            country_raw in eu_countries
            or exchange in eu_exchanges
            or bool(eu_suffix_re.search(context_text))
            or depositary_receipt_gleif_candidate
        )
        row_leis = _issuer_leis(issuer)
        if not in_scope:
            continue
        if _is_us_primary_esef_false_positive(instrument=instrument, company=company, issuer=issuer, exchange=exchange, country_raw=country_raw):
            continue
        if _is_esef_wrapper_or_fund_candidate(instrument, exchange) and not row_leis and not depositary_receipt_gleif_candidate:
            continue
        candidates += 1
        if not row_leis and resolve_missing_leis and lei_resolution_queries_used < max(0, int(max_lei_resolution_queries or 0)):
            issuer_id = int(issuer.id) if issuer is not None and issuer.id is not None else None
            resolution_context = (
                issuer_id or 0,
                exchange,
                ticker,
                yahoo,
                str(getattr(instrument, "display_name", None) or "")[:120],
            )
            if issuer_id is not None and issuer_id not in resolved_resolution_issuer_ids and resolution_context not in seen_resolution_contexts:
                seen_resolution_contexts.add(resolution_context)
                budget_left = max(0, int(max_lei_resolution_queries or 0)) - lei_resolution_queries_used
                resolution = _resolve_issuer_lei_with_gleif(
                    session,
                    issuer=issuer,
                    company=company,
                    instrument=instrument,
                    exchange=exchange,
                    max_queries=budget_left,
                )
                lei_resolution_attempted += 1
                lei_resolution_queries_used += int(resolution.get("queries_used") or 0)
                res_status = str(resolution.get("status") or "unknown")
                lei_resolution_statuses[res_status] = lei_resolution_statuses.get(res_status, 0) + 1
                if res_status in {"resolved", "existing_identifier"}:
                    resolved_resolution_issuer_ids.add(issuer_id)
                    if len(resolved_examples) < 8:
                        resolved_examples.append(
                            {
                                "instrument_id": int(instrument.id),
                                "display_name": instrument.display_name,
                                "issuer_id": issuer_id,
                                "lei": resolution.get("lei"),
                                "score": resolution.get("score"),
                                "legal_name": resolution.get("legal_name"),
                            }
                        )
                elif res_status not in {"already_has_lei"} and len(unresolved_examples) < 8:
                    unresolved_examples.append(
                        {
                            "instrument_id": int(instrument.id),
                            "display_name": instrument.display_name,
                            "issuer_id": issuer_id,
                            "status": res_status,
                            "best_legal_name": resolution.get("best_legal_name"),
                            "best_score": resolution.get("best_score"),
                            "expected_country": resolution.get("expected_country"),
                        }
                    )
                row_leis = _issuer_leis(issuer)
        if not row_leis:
            missing_lei += 1
            if len(examples) < 8:
                examples.append(
                    {
                        "instrument_id": int(instrument.id),
                        "display_name": instrument.display_name,
                        "ticker": ticker,
                        "exchange": instrument.exchange,
                        "reason": "missing_lei",
                    }
                )
        for raw_country in [country_raw]:
            if len(raw_country) == 2:
                countries.add("GB" if raw_country == "UK" else raw_country)
        for lei in row_leis:
            leis.add(lei)
            if company is not None:
                if company.id is not None:
                    company_id_by_lei[lei] = int(company.id)
                if company.slug:
                    company_slug_by_lei[lei] = company.slug
                if company.ticker:
                    ticker_by_lei[lei] = str(company.ticker).strip()
                if company.name:
                    issuer_name_by_lei[lei] = str(company.name).strip()
            if issuer is not None:
                issuer_name_by_lei.setdefault(lei, str(issuer.display_name or issuer.legal_name or "").strip())
        for value in [
            getattr(instrument, "display_name", None),
            getattr(instrument, "legal_name", None),
            getattr(company, "name", None) if company is not None else None,
            getattr(issuer, "display_name", None) if issuer is not None else None,
            getattr(issuer, "legal_name", None) if issuer is not None else None,
            ticker,
            yahoo,
        ]:
            text = re.sub(r"\s+", " ", str(value or "").strip())
            if text and len(text) >= 3:
                keywords.add(text[:120])

    if not candidates:
        return {"registered": False, "reason": "no_esef_candidate_rows", "rows_scanned": len(rows), "candidate_count": 0}

    source = session.query(Source).filter_by(name=_FILINGS_XBRL_ORG_ACTIVE_SOURCE_NAME).first()
    if source is None:
        source = Source(
            name=_FILINGS_XBRL_ORG_ACTIVE_SOURCE_NAME,
            category="filings",
            priority=1,
            polling_interval_seconds=86400,
            config={},
            created_at=utcnow(),
        )
        session.add(source)
        session.flush()
    cfg = dict(source.config or {})

    def _merge_unique(existing: object, additions: set[str], *, limit_n: int) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for value in [*(existing if isinstance(existing, list) else []), *sorted(additions)]:
            text = str(value or "").strip()
            key = text.casefold()
            if text and key not in seen:
                seen.add(key)
                out.append(text)
            if len(out) >= limit_n:
                break
        return out

    def _merge_map(key: str, updates: dict[str, Any]) -> dict[str, Any]:
        current = dict(cfg.get(key) if isinstance(cfg.get(key), dict) else {})
        for lei, value in updates.items():
            normalized = _normalize_lei(lei)
            if normalized and value is not None and str(value).strip():
                current[normalized] = value
        return current

    existing_leis = {str(value).strip().upper() for value in (cfg.get("leis") or []) if str(value).strip()} if isinstance(cfg.get("leis"), list) else set()
    cfg.update(
        {
            "connector": "filings_xbrl_org",
            "lookback_days": 1460,
            "timeout_s": float(cfg.get("timeout_s") or 12.0),
            "page_size": 20,
            "max_pages": 1,
            "leis_per_run": 8,
            "include_country_fallback": bool(cfg.get("include_country_fallback", False)),
            "max_items_per_run": 120,
            "refresh_existing": True,
            "enabled": True,
            "managed_by": "source_pack",
            "coverage_scope": "active_universe",
            "last_registered_reason": reason,
            "leis": _merge_unique(cfg.get("leis"), leis, limit_n=1200),
            "countries": _merge_unique(cfg.get("countries"), countries, limit_n=80),
            "include_keywords": _merge_unique(cfg.get("include_keywords"), keywords | leis, limit_n=1500),
            "company_id_by_lei": _merge_map("company_id_by_lei", company_id_by_lei),
            "company_slug_by_lei": _merge_map("company_slug_by_lei", company_slug_by_lei),
            "ticker_by_lei": _merge_map("ticker_by_lei", ticker_by_lei),
            "issuer_name_by_lei": _merge_map("issuer_name_by_lei", issuer_name_by_lei),
            "source_quality": {
                **(cfg.get("source_quality") if isinstance(cfg.get("source_quality"), dict) else {}),
                "authority": "official_filing_package_repository",
                "timeliness": "scheduled",
                "acquisition_fragility": "low",
                "rights_class": "public",
            },
        }
    )
    source.config = cfg
    session.flush()
    after_leis = {str(value).strip().upper() for value in (cfg.get("leis") or []) if str(value).strip()}
    added_leis = sorted(after_leis - existing_leis)
    if added_leis:
        state = session.query(SourceState).filter_by(source_id=int(source.id)).first()
        if state is not None:
            state.cursor = {}
    session.flush()
    return {
        "registered": True,
        "rows_scanned": len(rows),
        "candidate_count": candidates,
        "lei_count": len(leis),
        "lei_missing_count": missing_lei,
        "lei_resolution": {
            "enabled": bool(resolve_missing_leis),
            "attempted": int(lei_resolution_attempted),
            "queries_used": int(lei_resolution_queries_used),
            "max_queries": max(0, int(max_lei_resolution_queries or 0)),
            "statuses": lei_resolution_statuses,
            "resolved_examples": resolved_examples,
            "unresolved_examples": unresolved_examples,
        },
        "filings_xbrl_org_source": {
            "source_id": int(source.id),
            "source_name": source.name,
            "lei_count": len(cfg.get("leis") or []),
            "leis_added": len(added_leis),
            "country_count": len(cfg.get("countries") or []),
        },
        "examples": examples,
    }


def ensure_active_universe_china_sources(
    session: Session,
    *,
    universe_slug: str | None = None,
    limit: int | None = None,
    reason: str = "active_universe_source_pack",
    stock_map: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Refresh official CNINFO announcement source from active mainland China A-share rows."""
    q = (
        session.query(ResearchUniverseRow)
        .join(ResearchUniverse, ResearchUniverse.id == ResearchUniverseRow.universe_id)
        .filter(ResearchUniverse.status == "active", ResearchUniverseRow.archived_at.is_(None))
        .order_by(ResearchUniverse.sort_order.asc(), ResearchUniverseRow.sort_order.asc(), ResearchUniverseRow.id.asc())
    )
    if universe_slug:
        q = q.filter(ResearchUniverse.slug == str(universe_slug).strip())
    rows = q.all()
    if limit is not None:
        rows = rows[: max(1, int(limit))]

    if stock_map is None:
        try:
            from thoth_terminal.connectors.cninfo_announcements import fetch_cninfo_stock_map

            stock_map = fetch_cninfo_stock_map()
        except Exception:
            stock_map = {}

    stock_codes: set[str] = set()
    keywords: set[str] = set()
    stock_org_id_by_stock_code: dict[str, str] = {}
    stock_column_by_stock_code: dict[str, str] = {}
    company_id_by_stock_code: dict[str, int] = {}
    company_slug_by_stock_code: dict[str, str] = {}
    ticker_by_stock_code: dict[str, str] = {}
    company_name_by_stock_code: dict[str, str] = {}
    candidates = 0
    identifiers_created = identifiers_existing = identifiers_conflicts = 0
    org_identifiers_created = org_identifiers_existing = org_identifiers_conflicts = 0
    examples: list[dict[str, Any]] = []
    china_exchanges = {"SSE", "SZSE", "SEHKSZSE", "SEHKNTL", "CHINEXT", "STAR", "SEHKSTAR", "SHENZHEN", "SHANGHAI", "SHSE"}
    for row in rows:
        instrument = row.instrument
        if instrument is None or not bool(getattr(instrument, "is_public", True)):
            continue
        company = session.get(Company, int(instrument.company_id)) if instrument.company_id is not None else None
        issuer = session.get(Issuer, int(instrument.issuer_id)) if instrument.issuer_id is not None else None
        if issuer is None and company is not None and company.issuer_id is not None:
            issuer = session.get(Issuer, int(company.issuer_id))
        exchange = _normalize_exchange_code(getattr(instrument, "exchange", None) or getattr(company, "exchange", None))
        country = str(getattr(instrument, "country", None) or getattr(company, "region", None) or getattr(issuer, "jurisdiction", None) or "").strip().upper()
        ticker = getattr(instrument, "ticker", None) or getattr(company, "ticker", None) or getattr(instrument, "yahoo_symbol", None)
        ticker_text = str(ticker or "").strip().upper()
        yahoo_text = str(getattr(instrument, "yahoo_symbol", None) or "").strip().upper()
        existing_stock_codes = _issuer_identifier_values(issuer, "CN_STOCK") if issuer is not None else []
        detected_stock_code = _normalize_cn_stock_code(ticker) or _normalize_cn_stock_code(yahoo_text)
        hk_listing = exchange in {"SEHK", "HKEX"} or ticker_text.endswith(".HK") or yahoo_text.endswith(".HK")
        cn_context = (
            exchange in china_exchanges
            or country in {"CN", "CHINA"}
            or ticker_text.endswith((".SS", ".SH", ".SZ"))
            or yahoo_text.endswith((".SS", ".SH", ".SZ"))
            or bool(existing_stock_codes)
        )
        if not cn_context or (hk_listing and not existing_stock_codes):
            continue
        candidates += 1
        selected_codes = existing_stock_codes or ([detected_stock_code] if detected_stock_code else [])
        if not selected_codes:
            if len(examples) < 8:
                examples.append(
                    {
                        "instrument_id": int(instrument.id),
                        "display_name": instrument.display_name,
                        "ticker": ticker,
                        "exchange": instrument.exchange,
                        "reason": "missing_cn_stock_code",
                    }
                )
            continue
        for stock_code in selected_codes:
            normalized = _normalize_cn_stock_code(stock_code)
            if not normalized:
                continue
            stock_codes.add(normalized)
            keywords.add(normalized)
            mapped = dict(stock_map.get(normalized) or {}) if isinstance(stock_map, dict) else {}
            org_id = str(mapped.get("org_id") or mapped.get("orgId") or "").strip()
            if org_id:
                stock_org_id_by_stock_code[normalized] = org_id
            stock_column_by_stock_code[normalized] = str(mapped.get("column") or _cninfo_column_for_stock_code(normalized)).strip().lower()
            if issuer is not None:
                status, related_id = _upsert_issuer_identifier(
                    session,
                    issuer=issuer,
                    id_type="CN_STOCK",
                    id_value=normalized,
                    exchange=stock_column_by_stock_code[normalized].upper(),
                )
                if status == "created":
                    identifiers_created += 1
                elif status == "existing":
                    identifiers_existing += 1
                elif status == "conflict":
                    identifiers_conflicts += 1
                    if len(examples) < 8:
                        examples.append(
                            {
                                "instrument_id": int(instrument.id),
                                "display_name": instrument.display_name,
                                "stock_code": normalized,
                                "reason": "cn_stock_already_bound_to_other_issuer",
                                "other_issuer_id": related_id,
                            }
                        )
                    continue
                if org_id:
                    org_status, org_related_id = _upsert_issuer_identifier(
                        session,
                        issuer=issuer,
                        id_type="CNINFO_ORG",
                        id_value=org_id,
                        exchange="CNINFO",
                    )
                    if org_status == "created":
                        org_identifiers_created += 1
                    elif org_status == "existing":
                        org_identifiers_existing += 1
                    elif org_status == "conflict":
                        org_identifiers_conflicts += 1
                        if len(examples) < 8:
                            examples.append(
                                {
                                    "instrument_id": int(instrument.id),
                                    "display_name": instrument.display_name,
                                    "stock_code": normalized,
                                    "org_id": org_id,
                                    "reason": "cninfo_org_already_bound_to_other_issuer",
                                    "other_issuer_id": org_related_id,
                                }
                            )
            if company is not None:
                company_slug_by_stock_code[normalized] = company.slug
                if company.id is not None:
                    company_id_by_stock_code[normalized] = int(company.id)
                if company.ticker:
                    ticker_by_stock_code[normalized] = str(company.ticker).strip()
                if company.name:
                    company_name_by_stock_code[normalized] = str(company.name).strip()
            if issuer is not None and normalized not in company_name_by_stock_code:
                company_name_by_stock_code[normalized] = str(issuer.display_name or issuer.legal_name or "").strip()
            if mapped.get("sec_name"):
                keywords.add(str(mapped.get("sec_name")).strip())
        for value in [
            getattr(instrument, "display_name", None),
            getattr(instrument, "legal_name", None),
            getattr(company, "name", None) if company is not None else None,
            getattr(issuer, "display_name", None) if issuer is not None else None,
            getattr(issuer, "legal_name", None) if issuer is not None else None,
        ]:
            text = re.sub(r"\s+", " ", str(value or "").strip())
            if text and len(text) >= 3:
                keywords.add(text[:120])

    if not candidates:
        return {"registered": False, "reason": "no_china_candidate_rows", "rows_scanned": len(rows), "candidate_count": 0}

    source = session.query(Source).filter_by(name=_CNINFO_ACTIVE_SOURCE_NAME).first()
    if source is None:
        source = Source(
            name=_CNINFO_ACTIVE_SOURCE_NAME,
            category="exchange",
            priority=1,
            polling_interval_seconds=21600,
            config={},
            created_at=utcnow(),
        )
        session.add(source)
        session.flush()
    cfg = dict(source.config or {})

    def _merge_unique(existing: object, additions: set[str], *, limit_n: int) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for value in [*(existing if isinstance(existing, list) else []), *sorted(additions)]:
            text = str(value or "").strip()
            key = text.casefold()
            if text and key not in seen:
                seen.add(key)
                out.append(text)
            if len(out) >= limit_n:
                break
        return out

    def _merge_map(key: str, updates: dict[str, Any]) -> dict[str, Any]:
        current = dict(cfg.get(key) if isinstance(cfg.get(key), dict) else {})
        for stock_code, value in updates.items():
            normalized = _normalize_cn_stock_code(stock_code)
            if normalized and value is not None and str(value).strip():
                current[normalized] = value
        return current

    existing_codes = {str(value).strip() for value in (cfg.get("stock_codes") or []) if str(value).strip()} if isinstance(cfg.get("stock_codes"), list) else set()
    cfg.update(
        {
            "connector": "cninfo_announcements",
            "lookback_days": 730,
            "stock_codes_per_run": 4,
            "max_items_per_run": 120,
            "page_size": 30,
            "fetch_article": False,
            "download_article_images": False,
            "store_raw_html_on_failure": True,
            "refresh_existing": True,
            "enabled": True,
            "managed_by": "source_pack",
            "coverage_scope": "active_universe",
            "last_registered_reason": reason,
            "stock_codes": _merge_unique(cfg.get("stock_codes"), stock_codes, limit_n=1000),
            "include_keywords": _merge_unique(cfg.get("include_keywords"), keywords, limit_n=1500),
            "stock_org_id_by_stock_code": _merge_map("stock_org_id_by_stock_code", stock_org_id_by_stock_code),
            "stock_column_by_stock_code": _merge_map("stock_column_by_stock_code", stock_column_by_stock_code),
            "company_id_by_stock_code": _merge_map("company_id_by_stock_code", company_id_by_stock_code),
            "company_slug_by_stock_code": _merge_map("company_slug_by_stock_code", company_slug_by_stock_code),
            "ticker_by_stock_code": _merge_map("ticker_by_stock_code", ticker_by_stock_code),
            "company_name_by_stock_code": _merge_map("company_name_by_stock_code", company_name_by_stock_code),
            "source_quality": {
                **(cfg.get("source_quality") if isinstance(cfg.get("source_quality"), dict) else {}),
                "authority": "official_disclosure_platform",
                "timeliness": "scheduled",
                "acquisition_fragility": "medium",
                "rights_class": "public",
            },
        }
    )
    source.config = cfg
    session.flush()
    after_codes = {str(value).strip() for value in (cfg.get("stock_codes") or []) if str(value).strip()}
    added_codes = sorted(after_codes - existing_codes)
    if added_codes:
        state = session.query(SourceState).filter_by(source_id=int(source.id)).first()
        if state is not None:
            state.cursor = {}
    session.flush()
    return {
        "registered": True,
        "rows_scanned": len(rows),
        "candidate_count": candidates,
        "stock_codes_registered": len(stock_codes),
        "identifiers_created": identifiers_created,
        "identifiers_existing": identifiers_existing,
        "identifier_conflicts": identifiers_conflicts,
        "org_identifiers_created": org_identifiers_created,
        "org_identifiers_existing": org_identifiers_existing,
        "org_identifier_conflicts": org_identifiers_conflicts,
        "cninfo_source": {
            "source_id": int(source.id),
            "source_name": source.name,
            "stock_code_count": len(cfg.get("stock_codes") or []),
            "stock_codes_added": len(added_codes),
            "org_id_count": len(cfg.get("stock_org_id_by_stock_code") or {}),
        },
        "examples": examples,
    }


def ensure_company_sec_companyfacts_source(
    session: Session,
    *,
    company: Company,
    reason: str = "decision_spine_missing_structured_facts",
) -> dict[str, Any]:
    ciks = sec_ciks_from_company_documents(session, company=company)
    if not ciks:
        return {"registered": False, "reason": "no_sec_cik", "ciks": []}
    return ensure_sec_companyfacts_source_for_ciks(
        session,
        ciks=ciks,
        reason=reason,
        company_slug_by_cik={cik: company.slug for cik in ciks},
        ticker_by_cik={cik: company.ticker for cik in ciks},
        company_id_by_cik={cik: int(company.id) for cik in ciks if company.id is not None},
    )


def _filing_form(extra: dict[str, Any]) -> str | None:
    for key in ("filing_type", "form_type", "form"):
        raw = str(extra.get(key) or "").strip()
        if raw:
            return raw.upper()
    return None


def _filing_items(extra: dict[str, Any]) -> list[str]:
    value = extra.get("items") or extra.get("filing_items")
    if isinstance(value, list):
        candidates = [str(item).strip() for item in value]
    else:
        raw = str(value or "").strip()
        candidates = re.findall(r"\b[0-9]{1,2}\.[0-9]{2}\b", raw) if raw else []
        if raw and not candidates:
            candidates = [part.strip() for part in re.split(r"[,;|]", raw)]
    seen: set[str] = set()
    out: list[str] = []
    for item in candidates:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _document_mentions_earnings(doc: Document) -> bool:
    text = "\n".join(
        part
        for part in [
            str(doc.title or ""),
            str(doc.raw_text or "")[:1200],
            str(doc.normalized_text or "")[:1200],
        ]
        if part
    )
    return re.search(r"\b(earnings|financial results?|quarterly results?|annual results?)\b", text, flags=re.IGNORECASE) is not None


def _is_sec_earnings_release(doc: Document, extra: dict[str, Any]) -> bool:
    form = _filing_form(extra)
    return (
        _truthy(extra.get("is_earnings_release"))
        or "2.02" in _filing_items(extra)
        or (form in _SEC_CURRENT_REPORT_FORMS and _document_mentions_earnings(doc))
    )


def _official_doc_type(doc: Document, default_doc_type: str) -> str:
    extra = _doc_extra(doc)
    connector = str(extra.get("connector") or "").strip()
    form = _filing_form(extra)
    if connector in {"sec_edgar", "sec_edgar_rss"}:
        if _is_sec_earnings_release(doc, extra):
            return "earnings_release"
        if form in _SEC_FINANCIAL_REPORT_FORMS:
            return "financial_report"
        if form in {"6-K", "6-K/A"} and _document_mentions_earnings(doc):
            return "earnings_release"
    return default_doc_type


def _should_materialize_filing_ledger(doc: Document, doc_type: str) -> bool:
    extra = _doc_extra(doc)
    connector = str(extra.get("connector") or "").strip()
    if connector in _OFFICIAL_EXCHANGE_DISCLOSURE_CONNECTORS:
        return doc_type in {"earnings_release", "exchange_disclosure"}
    if connector not in _OFFICIAL_FILING_CONNECTORS:
        return False
    if connector == "sec_edgar_rss":
        return doc_type in {"earnings_release", "financial_report"} or _filing_form(extra) in _SEC_FINANCIAL_REPORT_FORMS
    return doc_type in {"earnings_release", "financial_report", "annual_report_text", "filing", "company_profile", "segment_financials", "financial_ratios"}


def _should_sync_securities_filing_rail(doc: Document, *, connector: str, doc_type: str) -> bool:
    if connector not in _OFFICIAL_FILING_CONNECTORS:
        return False
    if str(getattr(doc, "kind", "") or "").strip().lower() in {"securities_filing", "sec_filing"}:
        return True
    return doc_type in {
        "earnings_release",
        "financial_report",
        "annual_report_text",
        "filing",
        "company_profile",
        "segment_financials",
        "financial_ratios",
    }


def _source_identifier_value(extra: dict[str, Any], id_type: str) -> str | None:
    wanted = str(id_type or "").strip().upper()
    for row in extra.get("source_identifiers") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("id_type") or row.get("type") or "").strip().upper() == wanted:
            value = str(row.get("id_value") or row.get("value") or "").strip()
            if value:
                return value
    return None


def _exchange_for_connector(connector: str, extra: dict[str, Any]) -> str | None:
    raw = str(extra.get("exchange") or extra.get("market") or "").strip()
    return raw or _EXCHANGE_CONNECTOR_EXCHANGES.get(connector)


def _jurisdiction_for_connector(connector: str, extra: dict[str, Any]) -> str | None:
    raw = str(extra.get("jurisdiction") or extra.get("country") or "").strip().upper()
    return raw[:2] if raw else _EXCHANGE_CONNECTOR_JURISDICTIONS.get(connector)


def _company_identity_from_doc(doc: Document) -> tuple[str, str | None, str | None, str | None]:
    extra = doc.extra if isinstance(doc.extra, dict) else {}
    connector = str(extra.get("connector") or "").strip()
    if connector in {"edinet_disclosures", "edinetdb_financials"}:
        payload = _payload(extra)
        data = payload if isinstance(payload, dict) else {}
        name = str(
            data.get("name_en")
            or data.get("nameEn")
            or data.get("company_name_en")
            or data.get("companyNameEn")
            or extra.get("company_name_en")
            or data.get("filer_name")
            or data.get("filerName")
            or data.get("company_name")
            or data.get("name")
            or data.get("name_ja")
            or extra.get("company_name")
            or doc.author
            or doc.title
            or "Japanese issuer"
        ).strip()
        code = str(extra.get("edinet_code") or data.get("edinet_code") or data.get("edinetCode") or "").strip() or None
        raw_ticker = (
            extra.get("stock_code")
            or _source_identifier_value(extra, "JP_STOCK")
            or extra.get("sec_code")
            or data.get("stock_code")
            or data.get("sec_code")
            or data.get("secCode")
            or ""
        )
        ticker = _normalize_jp_stock_code(raw_ticker) or str(raw_ticker or "").strip() or None
        return name, ticker, "JP", code
    if connector in {"opendart_disclosures", "opendart_financials"}:
        name = str(extra.get("corp_name") or doc.author or "Korean issuer").strip()
        ticker = str(extra.get("stock_code") or "").strip() or None
        code = str(extra.get("corp_code") or "").strip() or None
        return name, ticker, "KR", code
    if connector in {"sec_edgar", "sec_edgar_rss", "sec_companyfacts"}:
        title_company, title_cik = _sec_title_identity(str(doc.title or ""))
        author = str(doc.author or "").strip()
        author = "" if author.upper() in {"SEC EDGAR", "EDGAR", "SEC"} else author
        name = str(extra.get("company_name") or author or title_company or doc.title or "SEC issuer").strip()
        ticker = str(extra.get("ticker") or "").strip().upper() or None
        code = str(extra.get("cik") or title_cik or "").strip() or None
        return name, ticker, "US", code
    if connector in {"taiwan_mops_financial_reports", "taiwan_mops_investor_conferences", "tpex_infohub_company", "twse_openapi"}:
        name = str(extra.get("company_display_name") or extra.get("company_name_en") or extra.get("company_name") or doc.author or "Taiwan issuer").strip()
        ticker = str(extra.get("stock_code") or _source_identifier_value(extra, "TW_STOCK") or "").strip() or None
        return name, ticker, "TW", ticker
    if connector == "tdnet_company_announcements":
        name = str(extra.get("company_display_name") or extra.get("company_name_en") or extra.get("company_name") or doc.author or "Japanese issuer").strip()
        raw_ticker = extra.get("stock_code") or _source_identifier_value(extra, "JP_STOCK") or ""
        ticker = _normalize_jp_stock_code(raw_ticker) or str(raw_ticker or "").strip() or None
        return name, ticker, "JP", ticker
    if connector == "krx_kind_disclosure":
        name = str(extra.get("company_display_name") or extra.get("company_name") or doc.author or "Korean issuer").strip()
        ticker = str(extra.get("stock_code") or _source_identifier_value(extra, "KR_STOCK") or "").strip() or None
        code = str(extra.get("krx_company_id") or _source_identifier_value(extra, "KRX_KIND") or "").strip() or None
        return name, ticker, "KR", code
    if connector == "hkexnews_title_search":
        name = str(extra.get("company_display_name") or extra.get("stock_name") or extra.get("company_name") or doc.author or "Hong Kong issuer").strip()
        ticker = str(extra.get("stock_code") or _source_identifier_value(extra, "HK_STOCK") or "").strip() or None
        return name, ticker, "HK", ticker
    if connector == "cninfo_announcements":
        name = str(extra.get("company_display_name") or extra.get("sec_name") or extra.get("company_name") or doc.author or "China issuer").strip()
        ticker = str(extra.get("stock_code") or _source_identifier_value(extra, "CN_STOCK") or "").strip() or None
        code = str(extra.get("org_id") or _source_identifier_value(extra, "CNINFO_ORG") or ticker or "").strip() or None
        return name, ticker, "CN", code
    if connector == "asx_announcements":
        name = str(extra.get("company_display_name") or extra.get("company_name") or doc.author or "Australian issuer").strip()
        ticker = str(extra.get("stock_code") or _source_identifier_value(extra, "ASX_STOCK") or "").strip() or None
        return name, ticker, "AU", ticker
    if connector == "bursa_announcements":
        name = str(extra.get("company_display_name") or extra.get("company_name") or doc.author or "Malaysian issuer").strip()
        ticker = str(extra.get("stock_code") or _source_identifier_value(extra, "MY_STOCK") or "").strip() or None
        return name, ticker, "MY", ticker
    if connector == "kazakhstan_announcements":
        name = str(extra.get("company_display_name") or extra.get("company_name") or doc.author or "Kazakhstan issuer").strip()
        ticker = str(extra.get("stock_code") or _source_identifier_value(extra, "KZ_STOCK") or "").strip() or None
        return name, ticker, "KZ", ticker
    if connector == "nse_corporate_announcements":
        name = str(extra.get("company_display_name") or extra.get("company_name") or doc.author or "Indian issuer").strip()
        ticker = str(extra.get("symbol") or _source_identifier_value(extra, "NSE_STOCK") or "").strip() or None
        code = str(extra.get("isin") or _source_identifier_value(extra, "ISIN") or ticker or "").strip() or None
        return name, ticker, "IN", code
    if connector == "filings_xbrl_org":
        name = str(extra.get("company_display_name") or doc.author or doc.title or "ESEF issuer").strip()
        ticker = str(extra.get("ticker") or "").strip() or None
        code = str(extra.get("lei") or _source_identifier_value(extra, "LEI") or "").strip() or None
        country = str(extra.get("country") or "").strip().upper() or None
        return name, ticker, country, code
    return str(doc.author or doc.title or "Company").strip(), None, None, None


def _company_id_from_source_config_candidates(cfg: dict[str, Any], candidates: list[tuple[str, str | None]]) -> int | None:
    for map_key, raw_key in candidates:
        key = str(raw_key or "").strip()
        mapping = cfg.get(map_key)
        if not key or not isinstance(mapping, dict):
            continue
        key_variants = [key, key.upper(), key.lstrip("0") if key.isdigit() else key]
        for variant in key_variants:
            raw_company_id = mapping.get(variant)
            if str(raw_company_id or "").isdigit():
                return int(raw_company_id)
    return None


def _source_config_company_id_for_document(session: Session, doc: Document, source: Source | None = None) -> int | None:
    source_row = source or (session.get(Source, int(doc.source_id)) if doc.source_id is not None else None)
    source_cfg = dict(source_row.config) if source_row is not None and isinstance(source_row.config, dict) else {}
    extra = _doc_extra(doc)
    connector = str(extra.get("connector") or source_cfg.get("connector") or "").strip()
    candidates: list[tuple[str, str | None]] = []
    if connector in {"sec_edgar", "sec_edgar_rss", "sec_companyfacts"}:
        title_company, title_cik = _sec_title_identity(str(doc.title or ""))
        candidates.append(("company_id_by_cik", _normalize_sec_cik(extra.get("cik") or title_cik)))
    elif connector in {"opendart_disclosures", "opendart_financials"}:
        candidates.append(("company_id_by_corp_code", str(extra.get("corp_code") or "").strip()))
    elif connector in {"edinet_disclosures", "edinetdb_financials"}:
        payload = _payload(extra)
        data = payload if isinstance(payload, dict) else {}
        edinet_code = str(extra.get("edinet_code") or data.get("edinet_code") or data.get("edinetCode") or "").strip()
        raw_stock_code = (
            extra.get("stock_code")
            or _source_identifier_value(extra, "JP_STOCK")
            or extra.get("sec_code")
            or data.get("stock_code")
            or data.get("sec_code")
            or data.get("secCode")
            or ""
        )
        normalized_stock_code = _normalize_jp_stock_code(raw_stock_code)
        raw_sec_code = str(extra.get("sec_code") or data.get("sec_code") or data.get("secCode") or "").strip()
        candidates.append(("company_id_by_edinet_code", edinet_code))
        candidates.append(("company_id_by_stock_code", normalized_stock_code or str(raw_stock_code or "").strip()))
        candidates.append(("company_id_by_sec_code", raw_sec_code))
    elif connector in {"taiwan_mops_monthly_revenue", "taiwan_mops_financial_reports", "taiwan_mops_investor_conferences", "tpex_infohub_company", "twse_openapi"}:
        candidates.append(("company_id_by_stock_code", str(extra.get("stock_code") or _source_identifier_value(extra, "TW_STOCK") or "").strip()))
    elif connector == "tdnet_company_announcements":
        raw_stock_code = extra.get("stock_code") or _source_identifier_value(extra, "JP_STOCK") or ""
        normalized_stock_code = _normalize_jp_stock_code(raw_stock_code)
        candidates.append(("company_id_by_stock_code", normalized_stock_code or str(raw_stock_code or "").strip()))
        candidates.append(("company_id_by_stock_code", str(raw_stock_code or "").strip()))
    elif connector == "hkexnews_title_search":
        candidates.append(("company_id_by_stock_code", str(extra.get("stock_code") or _source_identifier_value(extra, "HK_STOCK") or "").strip()))
    elif connector == "cninfo_announcements":
        candidates.append(("company_id_by_stock_code", str(extra.get("stock_code") or _source_identifier_value(extra, "CN_STOCK") or "").strip()))
        candidates.append(("company_id_by_org_id", str(extra.get("org_id") or _source_identifier_value(extra, "CNINFO_ORG") or "").strip()))
    elif connector == "asx_announcements":
        candidates.append(("company_id_by_stock_code", str(extra.get("stock_code") or _source_identifier_value(extra, "ASX_STOCK") or "").strip()))
    elif connector == "bursa_announcements":
        candidates.append(("company_id_by_stock_code", str(extra.get("stock_code") or _source_identifier_value(extra, "MY_STOCK") or "").strip()))
    elif connector == "kazakhstan_announcements":
        candidates.append(("company_id_by_stock_code", str(extra.get("stock_code") or _source_identifier_value(extra, "KZ_STOCK") or "").strip()))
    elif connector == "sgx_announcements":
        candidates.append(("company_id_by_stock_code", str(extra.get("stock_code") or _source_identifier_value(extra, "SGX_STOCK") or "").strip()))
    elif connector == "nse_corporate_announcements":
        candidates.append(("company_id_by_symbol", str(extra.get("symbol") or _source_identifier_value(extra, "NSE_STOCK") or "").strip()))
    elif connector == "filings_xbrl_org":
        candidates.append(("company_id_by_lei", str(extra.get("lei") or _source_identifier_value(extra, "LEI") or "").strip().upper()))
    elif connector == "sedarplus_search":
        candidates.append(("company_id_by_ca_stock_code", str(extra.get("stock_code") or extra.get("ticker") or "").strip().upper()))

    mapped = _company_id_from_source_config_candidates(source_cfg, candidates)
    if mapped is not None:
        return mapped
    for source_name in _ACTIVE_UNIVERSE_MAPPING_SOURCE_NAMES_BY_CONNECTOR.get(connector, []):
        if source_row is not None and source_row.name == source_name:
            continue
        fallback = session.query(Source).filter_by(name=source_name).first()
        if fallback is None or not isinstance(fallback.config, dict):
            continue
        mapped = _company_id_from_source_config_candidates(dict(fallback.config), candidates)
        if mapped is not None:
            return mapped
    return None


def _identity_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _company_identity_score(
    row: Company,
    *,
    name: str,
    ticker: str | None,
    external_code: str | None,
) -> int:
    score = 0
    row_name = _identity_text(row.name)
    name_norm = _identity_text(name)
    row_ticker = str(row.ticker or "").strip().upper()
    ticker_norm = str(ticker or "").strip().upper()
    if ticker_norm and row_ticker == ticker_norm:
        score += 100
    if name_norm and row_name == name_norm:
        score += 60
    elif name_norm and row_name and (name_norm in row_name or row_name in name_norm):
        score += 25
    if row.ticker:
        score += 25
    if row.exchange:
        score += 10
    if row.issuer_id is not None:
        score += 10
    if external_code and str(row.slug or "").strip().lower() == _slugify(external_code) and not row.ticker:
        score -= 35
    return score


def _best_existing_company_for_doc_identity(
    session: Session,
    *,
    name: str,
    ticker: str | None,
    external_code: str | None,
    current: Company | None = None,
) -> Company | None:
    candidates: dict[int, Company] = {}
    if current is not None and current.id is not None:
        candidates[int(current.id)] = current
        if current.issuer_id is not None:
            for row in session.query(Company).filter(Company.issuer_id == int(current.issuer_id)).all():
                candidates[int(row.id)] = row
    name_norm = _identity_text(name)
    if name_norm:
        for row in session.query(Company).filter(Company.name.ilike(name)).limit(20).all():
            candidates[int(row.id)] = row
    ticker_norm = str(ticker or "").strip().upper()
    if ticker_norm:
        for row in session.query(Company).filter(Company.ticker == ticker_norm).limit(20).all():
            candidates[int(row.id)] = row
    external_slug = _slugify(external_code) if external_code else None
    if external_slug:
        row = session.query(Company).filter_by(slug=external_slug).first()
        if row is not None:
            candidates[int(row.id)] = row
    if not candidates:
        return None

    ranked = sorted(
        candidates.values(),
        key=lambda row: (_company_identity_score(row, name=name, ticker=ticker, external_code=external_code), -(int(row.id or 0))),
        reverse=True,
    )
    best = ranked[0]
    best_score = _company_identity_score(best, name=name, ticker=ticker, external_code=external_code)
    if current is None:
        return best if best_score >= 50 else None
    current_score = _company_identity_score(current, name=name, ticker=ticker, external_code=external_code)
    if int(best.id) != int(current.id) and best_score >= current_score + 15:
        return best
    return current


def ensure_company_for_document(session: Session, doc: Document, source: Source | None = None) -> Company | None:
    extra = doc.extra if isinstance(doc.extra, dict) else {}
    connector = str(extra.get("connector") or "").strip()
    if connector not in _OFFICIAL_INVESTOR_CONNECTORS:
        return None
    name, ticker, region, external_code = _company_identity_from_doc(doc)
    if not name:
        return None
    if connector in {"sec_edgar", "sec_edgar_rss", "sec_companyfacts"} and not external_code and name.strip().lower() in {"sec issuer", "sec edgar", "sec current filing"}:
        return None
    slug_seed = ticker or external_code or name
    slug = _slugify(slug_seed)
    mapped_company_id = _source_config_company_id_for_document(session, doc, source=source)
    explicit_company_id = mapped_company_id if mapped_company_id is not None else extra.get("resolved_company_id")
    if explicit_company_id is None and (
        connector not in _OFFICIAL_EXCHANGE_DISCLOSURE_CONNECTORS or isinstance(extra.get("identity_resolution"), dict)
    ):
        explicit_company_id = extra.get("company_id")
    company = session.query(Company).filter_by(id=int(explicit_company_id)).first() if str(explicit_company_id or "").isdigit() else None
    explicit_slug = str(extra.get("company_slug") or "").strip()
    if company is None and explicit_slug:
        company = session.query(Company).filter_by(slug=explicit_slug).first()
    explicit_identity = company is not None and (str(explicit_company_id or "").isdigit() or bool(explicit_slug))
    if company is None:
        company = session.query(Company).filter_by(slug=slug).first()
    if company is None and ticker:
        company = session.query(Company).filter_by(ticker=ticker).first()
    if not explicit_identity:
        company = _best_existing_company_for_doc_identity(
            session,
            name=name,
            ticker=ticker,
            external_code=external_code,
            current=company,
        )
    if company is None:
        exchange = _exchange_for_connector(connector, extra)
        company = Company(
            slug=slug,
            name=name[:255],
            ticker=ticker,
            exchange=exchange,
            region=region,
            sector=None,
            industry=None,
            created_at=utcnow(),
        )
        session.add(company)
        session.flush()
    else:
        company.name = company.name or name[:255]
        company.ticker = company.ticker or ticker
        company.region = company.region or region
        company.exchange = company.exchange or _exchange_for_connector(connector, extra)
    return company


def _doc_type_for_section(section: str | None, connector: str) -> str:
    section_v = str(section or "").strip()
    if connector == "opendart_disclosures":
        return "filing"
    if connector == "opendart_financials":
        return "financial_report"
    if connector == "sec_companyfacts":
        return "financial_report"
    if connector == "filings_xbrl_org":
        return "financial_report"
    return {
        "company": "company_profile",
        "financials": "financial_report",
        "earnings": "earnings_release",
        "segments": "segment_financials",
        "ratios": "financial_ratios",
        "analysis": "company_analysis",
        "text_blocks": "annual_report_text",
        "expenses": "expense_breakdown",
        "facilities": "facility_disclosures",
    }.get(section_v, "filing")


def _ensure_company_document(session: Session, company: Company, doc: Document, doc_type: str) -> CompanyDocument:
    company_id = int(company.id)
    document_id = int(doc.id)
    for pending in session.new:
        if not isinstance(pending, CompanyDocument):
            continue
        if int(pending.company_id or 0) == company_id and int(pending.document_id or 0) == document_id and pending.doc_type == doc_type:
            return pending
    exists = session.query(CompanyDocument).filter_by(company_id=int(company.id), document_id=int(doc.id), doc_type=doc_type).first()
    legacy_untyped = (
        session.query(CompanyDocument)
        .filter_by(company_id=int(company.id), document_id=int(doc.id), doc_type=None)
        .first()
    )
    label = str(doc.title or doc_type or "").strip() or doc_type
    if exists:
        if legacy_untyped is not None and int(legacy_untyped.id) != int(exists.id):
            session.delete(legacy_untyped)
            session.flush()
        return exists
    if legacy_untyped is not None and doc_type:
        legacy_untyped.doc_type = doc_type
        legacy_untyped.label = (legacy_untyped.label or label)[:255]
        session.flush()
        return legacy_untyped
    row = CompanyDocument(
        company_id=int(company.id),
        document_id=int(doc.id),
        doc_type=doc_type,
        label=label[:255],
        created_at=utcnow(),
    )
    session.add(row)
    session.flush()
    return row


def _remove_stale_exchange_company_links(session: Session, company: Company, doc: Document) -> int:
    stale_links = (
        session.query(CompanyDocument)
        .filter(CompanyDocument.document_id == int(doc.id), CompanyDocument.company_id != int(company.id))
        .all()
    )
    removed = 0
    for link in stale_links:
        session.delete(link)
        removed += 1
    if removed:
        session.flush()
    return removed


def _remove_stale_filing_company_links(session: Session, company: Company, doc: Document) -> int:
    stale_links = (
        session.query(CompanyDocument)
        .filter(CompanyDocument.document_id == int(doc.id), CompanyDocument.company_id != int(company.id))
        .all()
    )
    removed = 0
    for link in stale_links:
        session.delete(link)
        removed += 1
    if removed:
        session.flush()
    return removed


def _source_connector(source: Source | None) -> str | None:
    if source is None or not isinstance(source.config, dict):
        return None
    raw = str(source.config.get("connector") or "").strip()
    return raw or None


def _connector_for_document(doc: Document, source: Source | None = None) -> str:
    extra = _doc_extra(doc)
    return str(extra.get("connector") or _source_connector(source) or "").strip()


def _document_day(doc: Document) -> date | None:
    extra = _doc_extra(doc)
    for key in ("filed_date", "filing_date", "accepted_at", "updated"):
        parsed = _date(extra.get(key))
        if parsed:
            return parsed
    if doc.published_at:
        return doc.published_at.date()
    if doc.ingested_at:
        return doc.ingested_at.date()
    return None


def _document_at(doc: Document) -> datetime:
    return doc.published_at or doc.ingested_at or utcnow()


def _document_url(doc: Document) -> str:
    return str(doc.canonical_url or doc.url or f"thoth://documents/{int(doc.id)}").strip()


_EXCHANGE_EARNINGS_RE = re.compile(
    r"(earnings|financial results?|results announcement|quarterly results?|annual results?|interim results?|"
    r"final results?|operating results?|consolidated financial|consolidated results?|financial statements?|"
    r"financial summary|financial report|summary of financial results|summary report on consolidated results?|"
    r"\u8ca1\u52d9|\u8ca1\u5831|\u696d\u7e3e|\u6bcf\u80a1\u76c8\u9918|\u6c7a\u7b97|\uc2e4\uc801|\uc7ac\ubb34)",
    flags=re.IGNORECASE,
)
_EXCHANGE_GUIDANCE_RE = re.compile(r"(guidance|forecast|outlook|revision of forecast|dividend forecast)", flags=re.IGNORECASE)
_EXCHANGE_MATERIAL_RE = re.compile(r"(material|timely disclosure|inside information|announcement|change in)", flags=re.IGNORECASE)
_EXCHANGE_CALL_MATERIAL_RE = re.compile(
    r"\b("
    r"earnings\s+(?:call|presentation|briefing|webcast|audio)"
    r"|earnings\s+(?:release\s+)?conference"
    r"|financial\s+(?:results?\s+)?(?:briefing|presentation|webcast|audio)"
    r"|results?\s+(?:briefing|presentation|webcast|audio)"
    r"|presentation\s*[-:]?\s*results?"
    r"|results?\s*[-:]?\s*presentation"
    r"|q\s*&\s*a|q&a|conference\s+call|analyst\s+(?:meeting|briefing)"
    r")\b"
    r"|法人(?:說明|说明)會|法說會|法说会|業績(?:發布|發佈)?會|业绩(?:发布)?会|簡報|简报|影音|音訊|音频",
    flags=re.IGNORECASE,
)


def _exchange_text(doc: Document, extra: dict[str, Any]) -> str:
    return "\n".join(
        part
        for part in [
            str(doc.title or ""),
            str(extra.get("headline_category") or ""),
            str(extra.get("file_info") or ""),
            str(extra.get("notes") or ""),
            str(doc.normalized_text or "")[:1500],
            str(doc.raw_text or "")[:1500],
        ]
        if part
    )


def _is_exchange_earnings_related(doc: Document, extra: dict[str, Any]) -> bool:
    if _truthy(extra.get("is_earnings_release")) or _truthy(extra.get("is_earnings_related")):
        return True
    return _EXCHANGE_EARNINGS_RE.search(_exchange_text(doc, extra)) is not None


def _is_exchange_call_material(doc: Document, extra: dict[str, Any]) -> bool:
    if _truthy(extra.get("is_call_material")):
        return True
    return _EXCHANGE_CALL_MATERIAL_RE.search(_exchange_text(doc, extra)) is not None


def _exchange_disclosure_type(doc: Document, extra: dict[str, Any], *, earnings_related: bool) -> str:
    text = _exchange_text(doc, extra)
    if earnings_related:
        return "Results"
    if _EXCHANGE_GUIDANCE_RE.search(text):
        return "Guidance"
    if _EXCHANGE_MATERIAL_RE.search(text):
        return "Material Event"
    return "Exchange Disclosure"


def _exchange_disclosure_subtype(extra: dict[str, Any]) -> str | None:
    raw = str(extra.get("headline_category") or extra.get("file_type") or "").strip()
    return raw[:128] or None


def _exchange_attachment_urls(doc: Document, extra: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for raw in [
        extra.get("xbrl_url"),
        extra.get("document_api_url"),
        *(extra.get("attachment_urls") if isinstance(extra.get("attachment_urls"), list) else []),
        doc.url,
        doc.canonical_url,
    ]:
        text = str(raw or "").strip()
        if text and text not in urls:
            urls.append(text)
    return urls


def _exchange_pdf_download_url(doc: Document, extra: dict[str, Any], primary_url: str | None = None) -> str | None:
    urls: list[str] = []
    for raw in [primary_url, *_exchange_attachment_urls(doc, extra)]:
        text = str(raw or "").strip()
        if text and text not in urls:
            urls.append(text)
    for url in urls:
        if ".pdf" in url.lower():
            return url
    return None


def _exchange_disclosure_id(doc: Document, extra: dict[str, Any]) -> str:
    for key in ("news_id", "accept_no", "acpt_no", "doc_id", "disclosure_id"):
        raw = str(extra.get(key) or "").strip()
        if raw:
            return raw[:128]
    raw = str(doc.external_id or "").strip()
    if raw:
        return raw[:128]
    return str(doc.id)


def _period_payload_dict(period: Any) -> dict[str, Any]:
    payload = period.to_dict() if hasattr(period, "to_dict") else {}
    return {key: value for key, value in dict(payload).items() if value is not None}


def _sync_exchange_earnings_event(
    session: Session,
    *,
    issuer_id: int,
    company: Company,
    disclosure: ExchangeDisclosure,
    doc: Document,
    period: Any,
) -> EarningsEvent | None:
    fiscal_year = getattr(period, "fiscal_year", None)
    fiscal_quarter = getattr(period, "fiscal_quarter", None)
    if fiscal_year is None:
        return None
    try:
        fiscal_year_i = int(fiscal_year)
    except Exception:
        return None
    if not is_plausible_earnings_fiscal_year(fiscal_year_i):
        return None
    fiscal_quarter_i = int(fiscal_quarter) if fiscal_quarter is not None else None
    row = session.query(EarningsEvent).filter_by(
        issuer_id=int(issuer_id),
        fiscal_year=fiscal_year_i,
        fiscal_quarter=fiscal_quarter_i,
    ).first()
    period_end = getattr(period, "period_end", None)
    if row is None and period_end is not None:
        row = (
            session.query(EarningsEvent)
            .filter_by(issuer_id=int(issuer_id), period_end=period_end)
            .order_by(EarningsEvent.id.asc())
            .first()
        )
    now = utcnow()
    if row is None:
        row = EarningsEvent(
            issuer_id=int(issuer_id),
            fiscal_year=fiscal_year_i,
            fiscal_quarter=fiscal_quarter_i,
            period_start=getattr(period, "period_start", None),
            period_end=period_end,
            calendar_quarter=getattr(period, "calendar_quarter", None),
            issuer_fiscal_year_label=getattr(period, "issuer_fiscal_year_label", None),
            vendor_fiscal_label=getattr(period, "vendor_fiscal_label", None),
            source_label_raw=getattr(period, "source_label_raw", None),
            scheduled_date=disclosure.announced_at.date() if disclosure.announced_at else None,
            scheduled_time=None,
            scheduled_tz=None,
            date_provenance="official_exchange_disclosure",
            date_source=disclosure.exchange,
            actual_release_at=disclosure.announced_at,
            actual_call_at=None,
            status="completed",
            release_filing_id=None,
            release_disclosure_id=int(disclosure.id),
            release_artifact_id=None,
            presentation_artifact_id=None,
            transcript_artifact_id=None,
            webcast_artifact_id=None,
            guidance_summary=None,
            raw_meta={},
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.flush()
    else:
        row.period_start = getattr(period, "period_start", None) or row.period_start
        row.period_end = period_end or row.period_end
        row.calendar_quarter = getattr(period, "calendar_quarter", None) or row.calendar_quarter
        row.issuer_fiscal_year_label = getattr(period, "issuer_fiscal_year_label", None) or row.issuer_fiscal_year_label
        row.vendor_fiscal_label = getattr(period, "vendor_fiscal_label", None) or row.vendor_fiscal_label
        row.source_label_raw = getattr(period, "source_label_raw", None) or row.source_label_raw
        row.scheduled_date = row.scheduled_date or (disclosure.announced_at.date() if disclosure.announced_at else None)
        row.actual_release_at = row.actual_release_at or disclosure.announced_at
        row.status = "completed"
        row.release_disclosure_id = int(disclosure.id)
    meta = dict(row.raw_meta or {}) if isinstance(row.raw_meta, dict) else {}
    period_payload = _period_payload_dict(period)
    if period_payload:
        meta["period_normalization"] = period_payload
    meta.update(
        {
            "exchange_disclosure_id": int(disclosure.id),
            "exchange": disclosure.exchange,
            "source_document_id": int(doc.id),
            "company_id": int(company.id),
        }
    )
    row.raw_meta = meta
    row.updated_at = now
    session.flush()
    return row


def _int_or_none(value: object) -> int | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        return int(value)
    except Exception:
        return None


def _ensure_exchange_call_material_artifact(
    session: Session,
    *,
    issuer: Issuer,
    doc: Document,
    extra: dict[str, Any],
    period: Any,
    source: Source | None = None,
    ingestion_run_id: int | None = None,
) -> IRArtifact | None:
    if not _is_exchange_call_material(doc, extra):
        return None
    url = _document_url(doc)
    if not url:
        return None
    artifact_type = str(extra.get("artifact_type") or "").strip().lower() or "presentation"
    artifact_subtype = str(extra.get("artifact_subtype") or "").strip().lower() or "earnings"
    fiscal_year = _int_or_none(extra.get("fiscal_year"))
    fiscal_quarter = _int_or_none(extra.get("fiscal_quarter"))
    if fiscal_year is None:
        fiscal_year = getattr(period, "fiscal_year", None)
    if fiscal_quarter is None:
        fiscal_quarter = getattr(period, "fiscal_quarter", None)
    row = (
        session.query(IRArtifact)
        .filter_by(issuer_id=int(issuer.id), artifact_type=artifact_type, canonical_url=url)
        .first()
    )
    if row is None and doc.id is not None:
        row = (
            session.query(IRArtifact)
            .filter_by(issuer_id=int(issuer.id), artifact_type=artifact_type, document_id=int(doc.id))
            .first()
        )
    raw_meta = {
        "connector": str(extra.get("connector") or "").strip() or None,
        "source_id": int(source.id) if source is not None else int(doc.source_id) if doc.source_id else None,
        "source_name": source.name if source is not None else None,
        "ingestion_run_id": ingestion_run_id,
        "document_extra": extra,
        "period_normalization": _period_payload_dict(period),
    }
    now = utcnow()
    pdf_download_url = _exchange_pdf_download_url(doc, extra, url)
    if row is None:
        row = IRArtifact(
            issuer_id=int(issuer.id),
            document_id=int(doc.id),
            artifact_type=artifact_type,
            artifact_subtype=artifact_subtype,
            published_at=_document_at(doc),
            event_date=_document_day(doc),
            fiscal_year=fiscal_year,
            fiscal_quarter=fiscal_quarter,
            title=str(doc.title or "").strip() or None,
            description=None,
            canonical_url=url,
            download_url=pdf_download_url,
            webcast_url=(url if "webcast" in url.lower() or "audio" in url.lower() else None),
            audio_url=(url if "audio" in url.lower() else None),
            is_derived_transcript=False,
            ical_uid=None,
            language=doc.language,
            raw_meta=raw_meta,
            created_at=now,
        )
        session.add(row)
        session.flush()
        return row
    row.document_id = int(doc.id)
    row.artifact_subtype = artifact_subtype or row.artifact_subtype
    row.canonical_url = url or row.canonical_url
    row.published_at = row.published_at or _document_at(doc)
    row.event_date = row.event_date or _document_day(doc)
    row.fiscal_year = fiscal_year if fiscal_year is not None else row.fiscal_year
    row.fiscal_quarter = fiscal_quarter if fiscal_quarter is not None else row.fiscal_quarter
    row.title = str(doc.title or "").strip() or row.title
    row.language = doc.language or row.language
    row.download_url = row.download_url or pdf_download_url
    if "webcast" in url.lower() or "audio" in url.lower():
        row.webcast_url = row.webcast_url or url
    if "audio" in url.lower():
        row.audio_url = row.audio_url or url
    meta = dict(row.raw_meta or {}) if isinstance(row.raw_meta, dict) else {}
    meta.update(raw_meta)
    row.raw_meta = meta
    session.flush()
    return row


def sync_exchange_disclosure_document(
    session: Session,
    *,
    doc: Document,
    source: Source | None = None,
    ingestion_run_id: int | None = None,
) -> dict[str, Any]:
    extra = _doc_extra(doc)
    connector = str(extra.get("connector") or "").strip()
    if connector not in _OFFICIAL_EXCHANGE_DISCLOSURE_CONNECTORS:
        return {"linked": False}
    company = ensure_company_for_document(session, doc, source=source)
    if company is None:
        return {"linked": False}
    issuer = session.get(Issuer, int(company.issuer_id)) if company.issuer_id is not None else None
    if issuer is None:
        from thoth_terminal.foundation_service import ensure_company_issuer

        issuer = ensure_company_issuer(session, company)
    if issuer is None:
        return {"linked": False, "company_id": int(company.id), "reason": "no_issuer"}
    if (
        int(extra.get("resolved_company_id") or 0) != int(company.id)
        or int(extra.get("issuer_id") or 0) != int(issuer.id)
        or int(extra.get("resolved_issuer_id") or 0) != int(issuer.id)
        or str(extra.get("company_slug") or "").strip() != str(company.slug or "").strip()
    ):
        updated_extra = dict(extra)
        updated_extra["resolved_company_id"] = int(company.id)
        updated_extra["company_id"] = int(company.id)
        updated_extra["issuer_id"] = int(issuer.id)
        updated_extra["resolved_issuer_id"] = int(issuer.id)
        updated_extra["company_slug"] = company.slug
        updated_extra["company_display_name"] = company.name
        doc.extra = updated_extra
        extra = updated_extra
    jurisdiction = _jurisdiction_for_connector(connector, extra)
    if jurisdiction and not issuer.jurisdiction:
        issuer.jurisdiction = jurisdiction
    if jurisdiction and not company.region:
        company.region = jurisdiction
    exchange = _exchange_for_connector(connector, extra) or "EXCHANGE"
    company.exchange = company.exchange or exchange

    earnings_related = _is_exchange_earnings_related(doc, extra)
    doc_type = "earnings_release" if earnings_related else "exchange_disclosure"
    stale_company_links_removed = _remove_stale_exchange_company_links(session, company, doc)
    _ensure_company_document(session, company, doc, doc_type)
    period = normalize_period_from_text(
        doc.title,
        _document_url(doc),
        published_at=_document_at(doc),
        issuer=issuer,
        company=company,
        metadata=extra,
        annual_as_q4=True,
    )
    disclosure_id = _exchange_disclosure_id(doc, extra)
    row = session.query(ExchangeDisclosure).filter_by(exchange=exchange, disclosure_id=disclosure_id).first()
    now = utcnow()
    if row is None:
        row = ExchangeDisclosure(
            issuer_id=int(issuer.id),
            document_id=int(doc.id),
            exchange=exchange,
            disclosure_id=disclosure_id,
            disclosure_type=_exchange_disclosure_type(doc, extra, earnings_related=earnings_related),
            disclosure_subtype=_exchange_disclosure_subtype(extra),
            announced_at=_document_at(doc),
            period_end=getattr(period, "period_end", None),
            fiscal_year=getattr(period, "fiscal_year", None),
            fiscal_quarter=getattr(period, "fiscal_quarter", None),
            title=doc.title,
            headline=doc.normalized_text[:2000] if doc.normalized_text else None,
            disclosure_url=_document_url(doc),
            attachment_urls=_exchange_attachment_urls(doc, extra),
            is_earnings_related=earnings_related,
            is_material_event=_EXCHANGE_MATERIAL_RE.search(_exchange_text(doc, extra)) is not None,
            language=doc.language,
            raw_meta={
                "connector": connector,
                "source_id": int(source.id) if source is not None else int(doc.source_id) if doc.source_id else None,
                "source_name": source.name if source is not None else None,
                "ingestion_run_id": ingestion_run_id,
                "document_extra": extra,
                "period_normalization": _period_payload_dict(period),
            },
            created_at=now,
        )
        session.add(row)
        session.flush()
    else:
        row.issuer_id = int(issuer.id)
        row.document_id = int(doc.id)
        row.disclosure_type = _exchange_disclosure_type(doc, extra, earnings_related=earnings_related) or row.disclosure_type
        row.disclosure_subtype = _exchange_disclosure_subtype(extra) or row.disclosure_subtype
        row.announced_at = row.announced_at or _document_at(doc)
        row.period_end = getattr(period, "period_end", None) or row.period_end
        row.fiscal_year = getattr(period, "fiscal_year", None) or row.fiscal_year
        row.fiscal_quarter = getattr(period, "fiscal_quarter", None) if getattr(period, "fiscal_quarter", None) is not None else row.fiscal_quarter
        row.title = doc.title or row.title
        row.headline = doc.normalized_text[:2000] if doc.normalized_text else row.headline
        row.disclosure_url = _document_url(doc) or row.disclosure_url
        row.attachment_urls = _exchange_attachment_urls(doc, extra) or row.attachment_urls
        row.is_earnings_related = bool(row.is_earnings_related or earnings_related)
        row.is_material_event = bool(row.is_material_event or (_EXCHANGE_MATERIAL_RE.search(_exchange_text(doc, extra)) is not None))
        row.language = doc.language or row.language
        meta = dict(row.raw_meta or {}) if isinstance(row.raw_meta, dict) else {}
        meta.update(
            {
                "connector": connector,
                "source_id": int(source.id) if source is not None else int(doc.source_id) if doc.source_id else meta.get("source_id"),
                "source_name": source.name if source is not None else meta.get("source_name"),
                "ingestion_run_id": ingestion_run_id or meta.get("ingestion_run_id"),
                "period_normalization": _period_payload_dict(period) or meta.get("period_normalization"),
            }
        )
        row.raw_meta = meta
        session.flush()

    event = _sync_exchange_earnings_event(
        session,
        issuer_id=int(issuer.id),
        company=company,
        disclosure=row,
        doc=doc,
        period=period,
    ) if earnings_related else None
    call_material_artifact = _ensure_exchange_call_material_artifact(
        session,
        issuer=issuer,
        doc=doc,
        extra=extra,
        period=period,
        source=source,
        ingestion_run_id=ingestion_run_id,
    )
    if event is not None and call_material_artifact is not None:
        event.presentation_artifact_id = event.presentation_artifact_id or int(call_material_artifact.id)
        if call_material_artifact.published_at is not None and event.actual_call_at is None:
            event.actual_call_at = call_material_artifact.published_at
        event.updated_at = utcnow()
    stats: dict[str, Any] = {
        "linked": True,
        "company_id": int(company.id),
        "issuer_id": int(issuer.id),
        "doc_type": doc_type,
        "exchange_disclosure_id": int(row.id),
        "ir_artifact_id": int(call_material_artifact.id) if call_material_artifact is not None else None,
        "earnings_related": earnings_related,
        "earnings_event_id": int(event.id) if event is not None else None,
        "stale_company_links_removed": stale_company_links_removed,
    }
    stats.update(sync_earnings_filing_dataset(session, doc=doc, company=company, doc_type=doc_type, source=source, ingestion_run_id=ingestion_run_id))
    return stats


def _iso_day(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _filing_doc_type_rank(doc_type: str | None) -> int:
    return {
        "earnings_release": 40,
        "annual_report_text": 35,
        "financial_report": 30,
        "segment_financials": 25,
        "financial_ratios": 25,
        "company_profile": 20,
        "filing": 10,
    }.get(str(doc_type or "").strip(), 0)


def _filing_ledger_columns() -> list[dict[str, Any]]:
    return [
        {"key": "document_date", "label": "Document Date", "data_type": "date", "role": "timestamp"},
        {"key": "ingested_date", "label": "Ingested Date", "data_type": "date", "role": "dimension"},
        {"key": "arrival_lag_days", "label": "Arrival Lag Days", "data_type": "number", "role": "metric"},
        {"key": "company", "label": "Company", "data_type": "text", "role": "dimension"},
        {"key": "ticker", "label": "Ticker", "data_type": "text", "role": "dimension"},
        {"key": "document_type", "label": "Document Type", "data_type": "text", "role": "dimension"},
        {"key": "connector", "label": "Connector", "data_type": "text", "role": "dimension"},
        {"key": "form_type", "label": "Form Type", "data_type": "text", "role": "dimension"},
        {"key": "is_earnings_release", "label": "Earnings Release", "data_type": "bool", "role": "dimension"},
        {"key": "period_of_report", "label": "Period Of Report", "data_type": "date", "role": "dimension"},
        {"key": "filed_date", "label": "Filed Date", "data_type": "date", "role": "dimension"},
        {"key": "source_name", "label": "Source", "data_type": "text", "role": "dimension"},
        {"key": "source_document_id", "label": "Source Document ID", "data_type": "number", "role": "dimension"},
        {"key": "source_url", "label": "Source URL", "data_type": "text", "role": "dimension"},
        {"key": "title", "label": "Title", "data_type": "text", "role": "dimension"},
        {"key": "accession_number", "label": "Accession", "data_type": "text", "role": "dimension"},
        {"key": "filing_items", "label": "Filing Items", "data_type": "text", "role": "dimension"},
    ]


def _filing_row(company: Company, company_doc: CompanyDocument, doc: Document, source: Source | None) -> dict[str, Any]:
    extra = _doc_extra(doc)
    doc_type = _official_doc_type(doc, str(company_doc.doc_type or "filing").strip() or "filing")
    document_day = _document_day(doc)
    filed_day = _date(extra.get("filed_date") or extra.get("filing_date"))
    ingested_day = doc.ingested_at.date() if doc.ingested_at else None
    arrival_lag_days = (ingested_day - filed_day).days if ingested_day is not None and filed_day is not None else None
    connector = _connector_for_document(doc, source)
    form = _filing_form(extra)
    return {
        "row_key": f"document:{int(doc.id)}",
        "document_date": _iso_day(document_day),
        "ingested_date": _iso_day(ingested_day),
        "arrival_lag_days": arrival_lag_days,
        "company": company.name,
        "ticker": company.ticker,
        "document_type": doc_type,
        "connector": connector,
        "form_type": form,
        "is_earnings_release": doc_type == "earnings_release" or _is_sec_earnings_release(doc, extra),
        "period_of_report": _iso_day(_date(extra.get("period_of_report"))),
        "filed_date": _iso_day(filed_day),
        "source_name": source.name if source is not None else None,
        "source_document_id": int(doc.id),
        "source_url": doc.url or doc.canonical_url,
        "title": doc.title,
        "accession_number": str(extra.get("accession_number") or "").strip() or None,
        "filing_items": ", ".join(_filing_items(extra)) or None,
    }


def sync_earnings_filing_dataset(
    session: Session,
    *,
    doc: Document,
    company: Company,
    doc_type: str,
    source: Source | None,
    ingestion_run_id: int | None,
) -> dict[str, int]:
    if not _should_materialize_filing_ledger(doc, doc_type):
        return {"filing_dataset_id": 0, "filing_rows": 0}

    lock_managed_company_dataset(
        session, company_id=int(company.id), family=DATASET_FAMILY_EARNINGS_FILINGS,
        source_kind="official_filings",
    )
    rows_by_doc_id: dict[int, tuple[int, dict[str, Any]]] = {}
    company_docs = (
        session.query(CompanyDocument, Document, Source)
        .join(Document, Document.id == CompanyDocument.document_id)
        .join(Source, Source.id == Document.source_id)
        .filter(CompanyDocument.company_id == int(company.id))
        .populate_existing()
        .all()
    )
    for company_doc, linked_doc, linked_source in company_docs:
        candidate_type = _official_doc_type(linked_doc, str(company_doc.doc_type or "filing").strip() or "filing")
        if not _should_materialize_filing_ledger(linked_doc, candidate_type):
            continue
        rank = _filing_doc_type_rank(candidate_type)
        doc_id = int(linked_doc.id)
        row = _filing_row(company, company_doc, linked_doc, linked_source)
        existing = rows_by_doc_id.get(doc_id)
        if existing is None or rank > existing[0]:
            rows_by_doc_id[doc_id] = (rank, row)

    rows = [row for _, row in rows_by_doc_id.values()]
    rows.sort(key=lambda row: (str(row.get("document_date") or ""), int(row.get("source_document_id") or 0)), reverse=True)
    if not rows:
        return {"filing_dataset_id": 0, "filing_rows": 0}

    slug = f"{company.slug}-earnings-filings"
    latest_document_date = next((str(row.get("document_date")) for row in rows if row.get("document_date")), None)
    connector_counts: dict[str, int] = {}
    form_counts: dict[str, int] = {}
    earnings_release_count = 0
    for row in rows:
        connector = str(row.get("connector") or "unknown")
        form = str(row.get("form_type") or "unknown")
        connector_counts[connector] = connector_counts.get(connector, 0) + 1
        form_counts[form] = form_counts.get(form, 0) + 1
        earnings_release_count += 1 if bool(row.get("is_earnings_release")) else 0
    extra = {
        "company_id": int(company.id),
        "company_slug": company.slug,
        "company_name": company.name,
        "ticker": company.ticker,
        "filing_count": len(rows),
        "earnings_release_count": earnings_release_count,
        "latest_document_date": latest_document_date,
        "connector_counts": connector_counts,
        "form_counts": form_counts,
        "source_document_id": int(doc.id),
        "ingestion_run_id": ingestion_run_id,
        "provenance_summary": "Materialized from official/regulatory investor-document ingestion. Use this ledger to audit source arrival before metric extraction.",
        "evidence_tier_summary": "official_regulatory",
    }
    title = f"{company.name} Earnings Filing Ledger"
    description = f"Official filings, releases, and regulator-backed issuer documents for {company.name}."
    dataset, created = ensure_managed_company_dataset(
        session,
        company_id=int(company.id),
        title=title,
        description=description,
        category="earnings",
        family=DATASET_FAMILY_EARNINGS_FILINGS,
        source_kind="official_filings",
        slug=slug,
        columns=_filing_ledger_columns(),
        rows=rows,
        label="official filing sync",
        extra=extra,
        source_event=f"investor_document:{int(doc.id)}",
    )
    if not created:
        dataset.title = title
        dataset.description = description
        dataset.category = "earnings"
        dataset.family = DATASET_FAMILY_EARNINGS_FILINGS
        dataset.source_kind = "official_filings"
        dataset.status = dataset.status or "active"
        dataset.extra = extra
        replace_dataset_draft(
            session,
            dataset,
            columns=_filing_ledger_columns(),
            rows=rows,
            label="official filing sync",
            extra=extra,
            source_event=f"investor_document:{int(doc.id)}",
        )
    return {"filing_dataset_id": int(dataset.id), "filing_rows": len(rows)}


def _iter_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [dict(row) for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("financials", "earnings", "ratios", "segments", "data", "items", "results"):
            val = payload.get(key)
            if isinstance(val, list):
                return [dict(row) for row in val if isinstance(row, dict)]
        return [payload]
    return []


def _currency_for_doc(extra: dict[str, Any]) -> str | None:
    if str(extra.get("connector") or "") == "edinetdb_financials":
        return "JPY"
    if str(extra.get("connector") or "") in {"opendart_disclosures", "opendart_financials"}:
        return "KRW"
    if str(extra.get("connector") or "") == "taiwan_mops_financial_reports":
        return "TWD"
    return None


def _upsert_metric(
    session: Session,
    *,
    company: Company,
    doc: Document,
    source: Source | None,
    ingestion_run_id: int | None,
    family: str,
    row: dict[str, Any],
    metric_key: str,
    metric_label: str,
    unit: str | None,
    value: float,
    period: date | None,
    as_of: date | None,
    statement_type: str,
    currency: str | None,
) -> bool:
    period_label = _period_label(row)
    fact_key = ":".join(
        [
            company.slug,
            family,
            metric_key,
            period.isoformat() if period else (period_label or "undated"),
            str(doc.id),
        ]
    )
    fact, created = upsert_structured_fact(
        session,
        dataset_family=family,
        fact_key=fact_key,
        group_key=f"{company.slug}:{family}",
        company_id=int(company.id),
        document_id=int(doc.id),
        source_id=int(source.id) if source is not None else int(doc.source_id) if doc.source_id else None,
        ingestion_run_id=ingestion_run_id,
        period_date=period,
        period_label=period_label,
        as_of_date=as_of,
        metric_key=metric_key,
        metric_label=metric_label,
        statement_type=statement_type,
        value_number=value,
        unit=unit,
        currency=currency if unit in {"currency", "per_share"} else None,
        evidence_tier="issuer_confirmed",
        provenance_label=doc.title,
        provenance_url=doc.url or doc.canonical_url,
        payload={"source_payload": row, "connector": (doc.extra or {}).get("connector") if isinstance(doc.extra, dict) else None},
    )
    sync_structured_fact_to_institutional(session, fact)
    return created


def sync_edinetdb_document_facts(
    session: Session,
    *,
    doc: Document,
    company: Company,
    source: Source | None,
    ingestion_run_id: int | None,
) -> dict[str, int]:
    extra = doc.extra if isinstance(doc.extra, dict) else {}
    if extra.get("connector") != "edinetdb_financials":
        return {"facts_created": 0, "facts_updated": 0}
    section = str(extra.get("section") or "").strip()
    payload = _payload(extra)
    rows = _iter_rows(payload)
    created = updated = 0
    touched: set[str] = set()
    currency = _currency_for_doc(extra)
    for row in rows:
        period = _period_date(row)
        as_of = _date(row.get("submit_date") or row.get("disclosure_date"))
        if as_of is None:
            as_of = doc.published_at.date() if doc.published_at else doc.ingested_at.date()
        for key, (label, unit) in _ACTUAL_METRICS.items():
            value = _num(row.get(key))
            if value is None:
                continue
            was_created = _upsert_metric(
                session,
                company=company,
                doc=doc,
                source=source,
                ingestion_run_id=ingestion_run_id,
                family=DATASET_FAMILY_EARNINGS_ACTUALS,
                row=row,
                metric_key=key,
                metric_label=label,
                unit=unit,
                value=value,
                period=period,
                as_of=as_of,
                statement_type="segment" if section == "segments" else "actual",
                currency=currency,
            )
            touched.add(DATASET_FAMILY_EARNINGS_ACTUALS)
            created += 1 if was_created else 0
            updated += 0 if was_created else 1
        for key, (label, unit) in _GUIDANCE_METRICS.items():
            value = _num(row.get(key))
            if value is None:
                continue
            was_created = _upsert_metric(
                session,
                company=company,
                doc=doc,
                source=source,
                ingestion_run_id=ingestion_run_id,
                family=DATASET_FAMILY_GUIDANCE_ACTUALS,
                row=row,
                metric_key=key.removeprefix("forecast_"),
                metric_label=label,
                unit=unit,
                value=value,
                period=period,
                as_of=as_of,
                statement_type="guidance",
                currency=currency,
            )
            touched.add(DATASET_FAMILY_GUIDANCE_ACTUALS)
            created += 1 if was_created else 0
            updated += 0 if was_created else 1
    if touched:
        materialize_company_fact_datasets(session, company=company, families=touched)
    return {"facts_created": created, "facts_updated": updated}


def _opendart_metric(row: dict[str, Any]) -> tuple[str, str, str | None] | None:
    account = str(row.get("account_nm") or row.get("account_name") or "").strip().lower()
    if not account:
        return None
    for needle, metric in _OPENDART_ACCOUNT_MAP.items():
        if needle in account:
            return metric
    return None


def sync_opendart_document_facts(
    session: Session,
    *,
    doc: Document,
    company: Company,
    source: Source | None,
    ingestion_run_id: int | None,
) -> dict[str, int]:
    extra = doc.extra if isinstance(doc.extra, dict) else {}
    if extra.get("connector") != "opendart_financials":
        return {"facts_created": 0, "facts_updated": 0}
    rows = _iter_rows(_payload(extra))
    created = updated = 0
    touched = False
    for row in rows:
        metric = _opendart_metric(row)
        if metric is None:
            continue
        metric_key, metric_label, unit = metric
        value = _num(row.get("thstrm_amount") or row.get("amount"))
        if value is None:
            continue
        period = _date(row.get("thstrm_dt")) or _period_date(row)
        if period is None:
            try:
                period = date(int(extra.get("business_year")), 12, 31)
            except Exception:
                period = None
        period_label = str(row.get("thstrm_nm") or extra.get("business_year") or "").strip() or None
        was_created = _upsert_metric(
            session,
            company=company,
            doc=doc,
            source=source,
            ingestion_run_id=ingestion_run_id,
            family=DATASET_FAMILY_EARNINGS_ACTUALS,
            row={**row, "period_label": period_label},
            metric_key=metric_key,
            metric_label=metric_label,
            unit=unit,
            value=value,
            period=period,
            as_of=doc.published_at.date() if doc.published_at else doc.ingested_at.date(),
            statement_type="actual",
            currency="KRW" if unit in {"currency", "per_share"} else None,
        )
        touched = True
        created += 1 if was_created else 0
        updated += 0 if was_created else 1
    if touched:
        materialize_company_fact_datasets(session, company=company, families={DATASET_FAMILY_EARNINGS_ACTUALS})
    return {"facts_created": created, "facts_updated": updated}


def sync_taiwan_mops_financial_document_facts(
    session: Session,
    *,
    doc: Document,
    company: Company,
    source: Source | None,
    ingestion_run_id: int | None,
) -> dict[str, int]:
    extra = doc.extra if isinstance(doc.extra, dict) else {}
    if extra.get("connector") != "taiwan_mops_financial_reports":
        return {"facts_created": 0, "facts_updated": 0}
    metrics = extra.get("financial_metrics")
    if not isinstance(metrics, dict):
        return {"facts_created": 0, "facts_updated": 0}
    period = _date(extra.get("period_of_report"))
    as_of = _date(extra.get("filed_date"))
    if as_of is None:
        as_of = doc.published_at.date() if doc.published_at else doc.ingested_at.date()
    created = updated = 0
    touched = False
    row = {
        "fiscal_year": extra.get("fiscal_year"),
        "fiscal_quarter": extra.get("fiscal_quarter"),
        "period_date": extra.get("period_of_report"),
        "source_rows": extra.get("source_rows"),
    }
    for source_key, (metric_key, metric_label, unit, multiplier) in _TAIWAN_MOPS_FINANCIAL_METRIC_MAP.items():
        value = _num(metrics.get(source_key))
        if value is None:
            continue
        was_created = _upsert_metric(
            session,
            company=company,
            doc=doc,
            source=source,
            ingestion_run_id=ingestion_run_id,
            family=DATASET_FAMILY_EARNINGS_ACTUALS,
            row={**row, "metric_source_key": source_key},
            metric_key=metric_key,
            metric_label=metric_label,
            unit=unit,
            value=value * float(multiplier or 1.0),
            period=period,
            as_of=as_of,
            statement_type="actual",
            currency="TWD" if unit in {"currency", "per_share"} else None,
        )
        touched = True
        created += 1 if was_created else 0
        updated += 0 if was_created else 1
    if touched:
        materialize_company_fact_datasets(session, company=company, families={DATASET_FAMILY_EARNINGS_ACTUALS})
    return {"facts_created": created, "facts_updated": updated}


def sync_sec_companyfacts_document_facts(
    session: Session,
    *,
    doc: Document,
    company: Company,
    source: Source | None,
    ingestion_run_id: int | None,
) -> dict[str, int]:
    extra = doc.extra if isinstance(doc.extra, dict) else {}
    if extra.get("connector") != "sec_companyfacts":
        return {"facts_created": 0, "facts_updated": 0}
    rows = _iter_rows(_payload(extra))
    created = updated = 0
    touched = False
    for row in rows:
        metric_key = str(row.get("metric_key") or "").strip()
        if not metric_key:
            continue
        value = _num(row.get("val"))
        if value is None:
            continue
        unit_raw = str(row.get("unit") or "").strip()
        unit = "per_share" if "shares" in unit_raw.lower() or "/shares" in unit_raw.lower() else "currency" if unit_raw.upper() == "USD" else unit_raw or None
        period = _date(row.get("end"))
        as_of = _date(row.get("filed"))
        if as_of is None:
            as_of = doc.published_at.date() if doc.published_at else doc.ingested_at.date()
        was_created = _upsert_metric(
            session,
            company=company,
            doc=doc,
            source=source,
            ingestion_run_id=ingestion_run_id,
            family=DATASET_FAMILY_EARNINGS_ACTUALS,
            row={**row, "period_label": _sec_companyfacts_period_label(row)},
            metric_key=metric_key,
            metric_label=str(row.get("label") or metric_key),
            unit=unit,
            value=value,
            period=period,
            as_of=as_of,
            statement_type="actual",
            currency="USD" if unit in {"currency", "per_share"} else None,
        )
        touched = True
        created += 1 if was_created else 0
        updated += 0 if was_created else 1
    if touched:
        materialize_company_fact_datasets(session, company=company, families={DATASET_FAMILY_EARNINGS_ACTUALS})
    return {"facts_created": created, "facts_updated": updated}


def sync_investor_document(
    session: Session,
    *,
    doc: Document,
    source: Source | None = None,
    ingestion_run_id: int | None = None,
    sync_filing_dataset: bool = True,
) -> dict[str, Any]:
    extra = doc.extra if isinstance(doc.extra, dict) else {}
    connector = str(extra.get("connector") or "").strip()
    if connector in _OFFICIAL_EXCHANGE_DISCLOSURE_CONNECTORS:
        return sync_exchange_disclosure_document(session, doc=doc, source=source, ingestion_run_id=ingestion_run_id)
    if connector not in _OFFICIAL_FILING_CONNECTORS:
        return {"linked": False}
    company = ensure_company_for_document(session, doc, source=source)
    if company is None:
        return {"linked": False}
    doc_type = _official_doc_type(doc, _doc_type_for_section(extra.get("section"), connector))
    issuer = session.get(Issuer, int(company.issuer_id)) if company.issuer_id is not None else None
    if issuer is None:
        from thoth_terminal.foundation_service import ensure_company_issuer

        issuer = ensure_company_issuer(session, company)
    if issuer is not None and (
        int(extra.get("resolved_company_id") or 0) != int(company.id)
        or int(extra.get("company_id") or 0) != int(company.id)
        or int(extra.get("issuer_id") or 0) != int(issuer.id)
        or int(extra.get("resolved_issuer_id") or 0) != int(issuer.id)
        or str(extra.get("company_slug") or "").strip() != str(company.slug or "").strip()
    ):
        updated_extra = dict(extra)
        updated_extra["resolved_company_id"] = int(company.id)
        updated_extra["company_id"] = int(company.id)
        updated_extra["issuer_id"] = int(issuer.id)
        updated_extra["resolved_issuer_id"] = int(issuer.id)
        updated_extra["company_slug"] = company.slug
        updated_extra["company_display_name"] = company.name
        doc.extra = updated_extra
        extra = updated_extra
    stale_company_links_removed = _remove_stale_filing_company_links(session, company, doc)
    company_doc = _ensure_company_document(session, company, doc, doc_type)
    securities_filing_id = None
    earnings_event_id = None
    if issuer is not None and _should_sync_securities_filing_rail(doc, connector=connector, doc_type=doc_type):
        from thoth_terminal.foundation_service import _ensure_earnings_event, _ensure_securities_filing

        filing = _ensure_securities_filing(session, issuer=issuer, company=company, document=doc)
        securities_filing_id = int(filing.id)
        event = _ensure_earnings_event(
            session,
            issuer=issuer,
            company=company,
            company_doc=company_doc,
            document=doc,
            filing=filing,
        )
        earnings_event_id = int(event.id) if event is not None else None
    stats: dict[str, Any] = {
        "linked": True,
        "company_id": int(company.id),
        "issuer_id": int(issuer.id) if issuer is not None else None,
        "doc_type": doc_type,
        "securities_filing_id": securities_filing_id,
        "earnings_event_id": earnings_event_id,
        "stale_company_links_removed": stale_company_links_removed,
    }
    if connector == "edinetdb_financials":
        stats.update(sync_edinetdb_document_facts(session, doc=doc, company=company, source=source, ingestion_run_id=ingestion_run_id))
    elif connector == "opendart_financials":
        stats.update(sync_opendart_document_facts(session, doc=doc, company=company, source=source, ingestion_run_id=ingestion_run_id))
    elif connector == "taiwan_mops_financial_reports":
        stats.update(sync_taiwan_mops_financial_document_facts(session, doc=doc, company=company, source=source, ingestion_run_id=ingestion_run_id))
    elif connector == "sec_companyfacts":
        stats.update(sync_sec_companyfacts_document_facts(session, doc=doc, company=company, source=source, ingestion_run_id=ingestion_run_id))
    if sync_filing_dataset:
        stats.update(sync_earnings_filing_dataset(session, doc=doc, company=company, doc_type=doc_type, source=source, ingestion_run_id=ingestion_run_id))
    else:
        stats.update({"filing_dataset_id": 0, "filing_rows": 0, "filing_dataset_skipped": True})
    return stats


def _document_materialized(session: Session, *, doc: Document, connector: str) -> bool:
    if connector in _OFFICIAL_EXCHANGE_DISCLOSURE_CONNECTORS:
        return session.query(ExchangeDisclosure.id).filter_by(document_id=int(doc.id)).first() is not None
    if connector in _OFFICIAL_FILING_CONNECTORS:
        linked = session.query(CompanyDocument.id).filter_by(document_id=int(doc.id)).first() is not None
        if not linked:
            return False
        extra = _doc_extra(doc)
        doc_type = _official_doc_type(doc, _doc_type_for_section(extra.get("section"), connector))
        if _should_sync_securities_filing_rail(doc, connector=connector, doc_type=doc_type):
            return session.query(SecuritiesFiling.id).filter_by(document_id=int(doc.id)).first() is not None
        return True
    return True


def _doc_company_scope_match(session: Session, *, doc: Document, company_ids: set[int] | None, company_slugs: set[str] | None) -> bool:
    if not company_ids and not company_slugs:
        return True
    extra = _doc_extra(doc)
    raw = extra.get("resolved_company_id")
    if str(raw or "").isdigit() and company_ids and int(raw) in company_ids:
        return True
    raw = extra.get("company_id") if isinstance(extra.get("identity_resolution"), dict) else None
    if str(raw or "").isdigit() and company_ids and int(raw) in company_ids:
        return True
    slug = str(extra.get("company_slug") or "").strip()
    if slug and company_slugs and slug in company_slugs:
        return True
    mapped_company_id = _source_config_company_id_for_document(session, doc)
    if mapped_company_id is not None and company_ids and int(mapped_company_id) in company_ids:
        return True
    if company_ids:
        linked = (
            session.query(CompanyDocument.id)
            .filter(CompanyDocument.document_id == int(doc.id), CompanyDocument.company_id.in_(sorted(company_ids)))
            .first()
        )
        if linked is not None:
            return True
    return False


def _document_identity_needs_source_config_repair(session: Session, *, doc: Document, company_id: int) -> bool:
    company = session.get(Company, int(company_id))
    if company is None:
        return False
    issuer = session.get(Issuer, int(company.issuer_id)) if company.issuer_id is not None else None
    extra = _doc_extra(doc)
    if int(extra.get("resolved_company_id") or 0) != int(company.id):
        return True
    if int(extra.get("company_id") or 0) != int(company.id):
        return True
    if issuer is not None and int(extra.get("issuer_id") or 0) != int(issuer.id):
        return True
    if issuer is not None and int(extra.get("resolved_issuer_id") or 0) != int(issuer.id):
        return True
    return bool(company.slug) and str(extra.get("company_slug") or "").strip() != str(company.slug or "").strip()


def materialize_official_investor_documents(
    session: Session,
    *,
    limit: int | None = None,
    include_linked: bool = False,
    company_ids: list[int] | set[int] | None = None,
    source_names: list[str] | tuple[str, ...] | set[str] | None = None,
    source_ids: list[int] | tuple[int, ...] | set[int] | None = None,
    max_runtime_seconds: int | float | None = None,
    sync_filing_dataset: bool = True,
) -> dict[str, Any]:
    """Promote already-ingested official/API investor documents into durable rails."""
    started_at = time.monotonic()
    runtime_budget = None if max_runtime_seconds is None else max(0.0, float(max_runtime_seconds))

    def _runtime_exhausted() -> bool:
        return runtime_budget is not None and time.monotonic() - started_at >= runtime_budget

    company_id_set = {int(value) for value in (company_ids or []) if str(value).isdigit() or isinstance(value, int)}
    source_id_set = {int(value) for value in (source_ids or []) if str(value).isdigit() or isinstance(value, int)}
    requested_source_names = [str(value or "").strip() for value in (source_names or []) if str(value or "").strip()]
    if requested_source_names:
        source_id_set.update(
            int(source_id)
            for (source_id,) in session.query(Source.id).filter(Source.name.in_(requested_source_names)).all()
        )
    company_slug_set = {
        str(slug or "").strip()
        for (slug,) in session.query(Company.slug).filter(Company.id.in_(sorted(company_id_set))).all()
        if str(slug or "").strip()
    } if company_id_set else set()
    query = (
        session.query(Document)
        .filter(Document.kind.in_(["exchange_disclosure", "securities_filing", "sec_filing"]))
        .order_by(Document.id.asc())
    )
    if source_id_set:
        query = query.filter(Document.source_id.in_(sorted(source_id_set)))
    scoped_query = query
    scope_filter_fallback_used = False
    if company_id_set:
        scope_filters = [
            Document.id.in_(
                session.query(CompanyDocument.document_id).filter(CompanyDocument.company_id.in_(sorted(company_id_set)))
            ),
            Document.extra["resolved_company_id"].as_integer().in_(sorted(company_id_set)),
            Document.extra["company_id"].as_integer().in_(sorted(company_id_set)),
        ]
        if company_slug_set:
            scope_filters.append(Document.extra["company_slug"].as_string().in_(sorted(company_slug_set)))
        scoped_query = query.filter(or_(*scope_filters))
    seen = linked = skipped = errors = 0
    by_connector: dict[str, int] = {}
    error_examples: list[dict[str, Any]] = []
    exchange_disclosures = earnings_events = ir_artifacts = 0
    stale_company_links_removed = 0
    processed_document_ids: set[int] = set()

    def _materialize_query(document_query: Any) -> None:
        nonlocal seen, linked, skipped, errors, exchange_disclosures, earnings_events, ir_artifacts, stale_company_links_removed
        for doc in document_query.yield_per(100):
            if _runtime_exhausted():
                break
            document_id = int(doc.id)
            if document_id in processed_document_ids:
                continue
            extra = _doc_extra(doc)
            connector = str(extra.get("connector") or "").strip()
            if connector not in _OFFICIAL_INVESTOR_CONNECTORS:
                continue
            if not _doc_company_scope_match(session, doc=doc, company_ids=company_id_set or None, company_slugs=company_slug_set or None):
                continue
            already_materialized = _document_materialized(session, doc=doc, connector=connector)
            if include_linked and already_materialized:
                mapped_company_id = _source_config_company_id_for_document(session, doc)
                if mapped_company_id is None:
                    skipped += 1
                    continue
                if company_id_set and int(mapped_company_id) not in company_id_set:
                    skipped += 1
                    continue
                target_link = (
                    session.query(CompanyDocument.id)
                    .filter_by(document_id=int(doc.id), company_id=int(mapped_company_id))
                    .first()
                )
                stale_link = (
                    session.query(CompanyDocument.id)
                    .filter(CompanyDocument.document_id == int(doc.id), CompanyDocument.company_id != int(mapped_company_id))
                    .first()
                )
                identity_repair_needed = _document_identity_needs_source_config_repair(session, doc=doc, company_id=int(mapped_company_id))
                if target_link is not None and stale_link is None and not identity_repair_needed:
                    skipped += 1
                    continue
            if not include_linked and already_materialized:
                skipped += 1
                continue
            if limit is not None and seen >= max(1, int(limit)):
                break
            processed_document_ids.add(document_id)
            seen += 1
            source = session.get(Source, int(doc.source_id)) if doc.source_id is not None else None
            try:
                with session.begin_nested():
                    result = sync_investor_document(
                        session,
                        doc=doc,
                        source=source,
                        ingestion_run_id=None,
                        sync_filing_dataset=sync_filing_dataset,
                    )
            except Exception as exc:
                if _document_materialized(session, doc=doc, connector=connector):
                    result = {"linked": True}
                else:
                    errors += 1
                    if len(error_examples) < 8:
                        error_examples.append(
                            {
                                "document_id": int(doc.id),
                                "connector": connector,
                                "title": str(doc.title or "")[:180],
                                "error": f"{type(exc).__name__}: {str(exc)[:220]}",
                            }
                        )
                    continue
            if result.get("linked"):
                linked += 1
                by_connector[connector] = by_connector.get(connector, 0) + 1
                if result.get("exchange_disclosure_id"):
                    exchange_disclosures += 1
                if result.get("earnings_event_id"):
                    earnings_events += 1
                if result.get("ir_artifact_id"):
                    ir_artifacts += 1
                stale_company_links_removed += int(result.get("stale_company_links_removed") or 0)

    _materialize_query(scoped_query)
    if company_id_set and not _runtime_exhausted() and (include_linked or (seen == 0 and linked == 0 and errors == 0)):
        # SQLite/json1 availability and backend JSON coercion differ enough that scoped JSON
        # predicates can miss freshly ingested official documents. Existing official links can
        # also point to legacy shadow companies while the source config now has the canonical
        # active-universe company id. Keep the SQL filter as the fast path, but fall back to
        # Python-side scope matching before declaring no work.
        scope_filter_fallback_used = True
        _materialize_query(query)
    session.flush()
    return {
        "seen": seen,
        "linked": linked,
        "skipped_existing": skipped,
        "errors": errors,
        "error_examples": error_examples,
        "scope_filter_fallback_used": bool(scope_filter_fallback_used),
        "source_filter_count": len(source_id_set),
        "by_connector": by_connector,
        "exchange_disclosures": exchange_disclosures,
        "earnings_events": earnings_events,
        "ir_artifacts": ir_artifacts,
        "stale_company_links_removed": stale_company_links_removed,
        "runtime_exhausted": _runtime_exhausted(),
        "sync_filing_dataset": bool(sync_filing_dataset),
    }
