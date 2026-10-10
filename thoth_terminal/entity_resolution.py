"""
Entity Resolution for Issuers

Provides identifier mapping using:
- LEI (Legal Entity Identifier) via GLEIF API
- FIGI (Financial Instrument Global Identifier) via OpenFIGI API
- SEC CIK mapping

This module enables unifying issuer-level filings from regulators,
exchange announcements, and IR artifacts without relying on proprietary IDs.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import requests

from thoth_terminal.config import settings

logger = logging.getLogger("thoth.entity_resolution")

# API endpoints
GLEIF_API_BASE = "https://api.gleif.org/api/v1"
OPENFIGI_API_BASE = "https://api.openfigi.com/v3"

# Rate limiting
GLEIF_MIN_INTERVAL = 0.2  # 5 req/sec
OPENFIGI_MIN_INTERVAL = 0.05  # 20 req/sec with API key, 6/min without

_last_gleif_request: float = 0.0
_last_openfigi_request: float = 0.0


# ==============================================================================
# LEI Resolution via GLEIF
# ==============================================================================


@dataclass
class LEIEntity:
    """Legal Entity Identifier record from GLEIF."""
    lei: str
    legal_name: str
    legal_jurisdiction: str | None
    entity_category: str | None
    entity_status: str  # ACTIVE, INACTIVE, etc.
    registration_status: str  # ISSUED, LAPSED, etc.
    
    # Address
    legal_address_country: str | None
    legal_address_city: str | None
    legal_address_postal_code: str | None
    
    # Registration
    initial_registration_date: datetime | None
    last_update_date: datetime | None
    next_renewal_date: datetime | None
    
    # Additional names
    other_names: list[str]
    
    # Parent relationship (if any)
    direct_parent_lei: str | None
    ultimate_parent_lei: str | None
    
    raw_data: dict[str, Any]


def _rate_limit_gleif() -> None:
    """Ensure we don't exceed GLEIF rate limits."""
    global _last_gleif_request
    now = time.monotonic()
    elapsed = now - _last_gleif_request
    if elapsed < GLEIF_MIN_INTERVAL:
        time.sleep(GLEIF_MIN_INTERVAL - elapsed)
    _last_gleif_request = time.monotonic()


def _parse_gleif_record(data: dict[str, Any]) -> LEIEntity | None:
    """Parse a GLEIF API record into LEIEntity."""
    try:
        attrs = data.get("attributes", {})
        lei_data = attrs.get("lei", "")
        entity = attrs.get("entity", {})
        registration = attrs.get("registration", {})
        
        legal_name_obj = entity.get("legalName", {})
        legal_name = legal_name_obj.get("name", "") if isinstance(legal_name_obj, dict) else str(legal_name_obj)
        
        legal_addr = entity.get("legalAddress", {})
        
        # Other names
        other_names_raw = entity.get("otherNames", []) or []
        other_names = []
        for n in other_names_raw:
            if isinstance(n, dict):
                other_names.append(n.get("name", ""))
            elif isinstance(n, str):
                other_names.append(n)
        
        # Parse dates
        def parse_date(s: str | None) -> datetime | None:
            if not s:
                return None
            try:
                return datetime.fromisoformat(s.replace("Z", "+00:00"))
            except ValueError:
                return None
        
        return LEIEntity(
            lei=lei_data,
            legal_name=legal_name,
            legal_jurisdiction=entity.get("jurisdiction"),
            entity_category=entity.get("category"),
            entity_status=entity.get("status", ""),
            registration_status=registration.get("status", ""),
            legal_address_country=legal_addr.get("country"),
            legal_address_city=legal_addr.get("city"),
            legal_address_postal_code=legal_addr.get("postalCode"),
            initial_registration_date=parse_date(registration.get("initialRegistrationDate")),
            last_update_date=parse_date(registration.get("lastUpdateDate")),
            next_renewal_date=parse_date(registration.get("nextRenewalDate")),
            other_names=other_names,
            direct_parent_lei=None,  # Requires separate relationship lookup
            ultimate_parent_lei=None,
            raw_data=data,
        )
    except Exception as e:
        logger.warning("Failed to parse GLEIF record: %s", e)
        return None


def lookup_lei(lei: str) -> LEIEntity | None:
    """Look up a single LEI from GLEIF."""
    _rate_limit_gleif()
    url = f"{GLEIF_API_BASE}/lei-records/{lei}"
    
    try:
        resp = requests.get(url, timeout=30.0)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json().get("data", {})
        return _parse_gleif_record(data)
    except Exception as e:
        logger.warning("GLEIF lookup failed for %s: %s", lei, e)
        return None


def search_lei_by_name(
    name: str,
    *,
    jurisdiction: str | None = None,
    limit: int = 10,
) -> list[LEIEntity]:
    """Search GLEIF by legal name."""
    _rate_limit_gleif()
    
    params: dict[str, Any] = {
        "filter[entity.legalName]": name,
        "page[size]": str(limit),
    }
    if jurisdiction:
        params["filter[entity.jurisdiction]"] = jurisdiction
    
    url = f"{GLEIF_API_BASE}/lei-records"
    
    try:
        resp = requests.get(url, params=params, timeout=30.0)
        resp.raise_for_status()
        records = resp.json().get("data", [])
        results = []
        for rec in records:
            entity = _parse_gleif_record(rec)
            if entity:
                results.append(entity)
        return results
    except Exception as e:
        logger.warning("GLEIF search failed for '%s': %s", name, e)
        return []


def get_lei_relationships(lei: str) -> dict[str, Any]:
    """Get parent/child relationships for an LEI."""
    _rate_limit_gleif()
    
    url = f"{GLEIF_API_BASE}/lei-records/{lei}/direct-parent-relationship"
    
    try:
        resp = requests.get(url, timeout=30.0)
        if resp.status_code == 404:
            return {}
        resp.raise_for_status()
        return resp.json().get("data", {})
    except Exception as e:
        logger.debug("GLEIF relationship lookup failed for %s: %s", lei, e)
        return {}


# ==============================================================================
# FIGI Resolution via OpenFIGI
# ==============================================================================


@dataclass
class FIGIResult:
    """OpenFIGI mapping result."""
    figi: str
    security_type: str | None
    market_sector: str | None
    ticker: str | None
    name: str | None
    exchange_code: str | None
    share_class_figi: str | None
    composite_figi: str | None
    
    raw_data: dict[str, Any]


def _rate_limit_openfigi() -> None:
    """Ensure we don't exceed OpenFIGI rate limits."""
    global _last_openfigi_request
    api_key = getattr(settings, "openfigi_api_key", None)
    min_interval = 0.05 if api_key else 10.0  # 6/min without key
    
    now = time.monotonic()
    elapsed = now - _last_openfigi_request
    if elapsed < min_interval:
        time.sleep(min_interval - elapsed)
    _last_openfigi_request = time.monotonic()


def _get_openfigi_headers() -> dict[str, str]:
    """Get headers for OpenFIGI requests."""
    headers = {
        "Content-Type": "application/json",
    }
    api_key = getattr(settings, "openfigi_api_key", None)
    if api_key:
        headers["X-OPENFIGI-APIKEY"] = api_key
    return headers


def map_to_figi(
    identifiers: list[dict[str, str]],
) -> list[list[FIGIResult]]:
    """
    Map identifiers to FIGIs using OpenFIGI batch API.
    
    Each identifier dict should have:
        - idType: TICKER, ISIN, CUSIP, SEDOL, etc.
        - idValue: The identifier value
        - exchCode (optional): Exchange code
        - micCode (optional): Market identifier code
        - currency (optional): Currency filter
    
    Returns list of results for each input identifier.
    """
    if not identifiers:
        return []
    
    _rate_limit_openfigi()
    
    url = f"{OPENFIGI_API_BASE}/mapping"
    headers = _get_openfigi_headers()
    
    try:
        resp = requests.post(url, json=identifiers, headers=headers, timeout=30.0)
        resp.raise_for_status()
        data = resp.json()
        
        results: list[list[FIGIResult]] = []
        for item in data:
            item_results = []
            if "data" in item:
                for entry in item["data"]:
                    item_results.append(FIGIResult(
                        figi=entry.get("figi", ""),
                        security_type=entry.get("securityType"),
                        market_sector=entry.get("marketSector"),
                        ticker=entry.get("ticker"),
                        name=entry.get("name"),
                        exchange_code=entry.get("exchCode"),
                        share_class_figi=entry.get("shareClassFIGI"),
                        composite_figi=entry.get("compositeFIGI"),
                        raw_data=entry,
                    ))
            results.append(item_results)
        return results
        
    except Exception as e:
        logger.warning("OpenFIGI mapping failed: %s", e)
        return [[] for _ in identifiers]


def lookup_ticker_figi(
    ticker: str,
    exchange: str | None = None,
    currency: str | None = None,
) -> list[FIGIResult]:
    """Look up FIGI by ticker symbol."""
    query: dict[str, str] = {
        "idType": "TICKER",
        "idValue": ticker,
    }
    if exchange:
        query["exchCode"] = exchange
    if currency:
        query["currency"] = currency
    
    results = map_to_figi([query])
    return results[0] if results else []


def lookup_isin_figi(isin: str) -> list[FIGIResult]:
    """Look up FIGI by ISIN."""
    results = map_to_figi([{"idType": "ID_ISIN", "idValue": isin}])
    return results[0] if results else []


def lookup_cusip_figi(cusip: str) -> list[FIGIResult]:
    """Look up FIGI by CUSIP."""
    results = map_to_figi([{"idType": "ID_CUSIP", "idValue": cusip}])
    return results[0] if results else []


# ==============================================================================
# SEC CIK Mapping
# ==============================================================================


def get_cik_ticker_mapping() -> dict[str, str]:
    """
    Fetch SEC CIK to ticker mapping.
    Returns dict of CIK -> ticker.
    
    Note: SEC provides company_tickers.json with this mapping.
    """
    url = "https://www.sec.gov/files/company_tickers.json"
    
    try:
        headers = {"User-Agent": "Thoth-Terminal/1.0"}
        resp = requests.get(url, headers=headers, timeout=30.0)
        resp.raise_for_status()
        data = resp.json()
        
        mapping: dict[str, str] = {}
        for entry in data.values():
            cik = str(entry.get("cik_str", "")).zfill(10)
            ticker = entry.get("ticker", "")
            if cik and ticker:
                mapping[cik] = ticker
        return mapping
        
    except Exception as e:
        logger.warning("Failed to fetch SEC CIK mapping: %s", e)
        return {}


def lookup_cik_by_ticker(ticker: str) -> str | None:
    """Look up SEC CIK by ticker symbol."""
    mapping = get_cik_ticker_mapping()
    # Reverse lookup
    for cik, t in mapping.items():
        if t.upper() == ticker.upper():
            return cik
    return None


# ==============================================================================
# Unified Entity Resolution
# ==============================================================================


@dataclass
class ResolvedIssuer:
    """Unified issuer identity with all resolved identifiers."""
    # Primary identifiers
    lei: str | None
    cik: str | None  # SEC CIK
    
    # Name
    legal_name: str
    display_name: str
    
    # Jurisdiction
    jurisdiction: str | None
    
    # Securities identifiers
    tickers: list[dict[str, str]]  # [{ticker, exchange, currency}, ...]
    isins: list[str]
    cusips: list[str]
    figis: list[str]
    sedols: list[str]
    
    # Status
    entity_status: str | None
    
    # Source info
    resolution_sources: list[str]  # ["gleif", "openfigi", "sec"]


def resolve_issuer_by_ticker(
    ticker: str,
    exchange: str | None = None,
) -> ResolvedIssuer | None:
    """
    Attempt to resolve an issuer using ticker as starting point.
    Tries: SEC CIK lookup -> FIGI lookup -> GLEIF search
    """
    resolution_sources: list[str] = []
    lei: str | None = None
    cik: str | None = None
    legal_name = ticker
    display_name = ticker
    jurisdiction: str | None = None
    tickers: list[dict[str, str]] = [{"ticker": ticker, "exchange": exchange or "", "currency": ""}]
    isins: list[str] = []
    cusips: list[str] = []
    figis: list[str] = []
    sedols: list[str] = []
    entity_status: str | None = None
    
    # Step 1: Try SEC CIK lookup
    cik = lookup_cik_by_ticker(ticker)
    if cik:
        resolution_sources.append("sec")
    
    # Step 2: Try OpenFIGI lookup
    figi_results = lookup_ticker_figi(ticker, exchange)
    if figi_results:
        resolution_sources.append("openfigi")
        for fr in figi_results:
            if fr.figi and fr.figi not in figis:
                figis.append(fr.figi)
            if fr.share_class_figi and fr.share_class_figi not in figis:
                figis.append(fr.share_class_figi)
            if fr.composite_figi and fr.composite_figi not in figis:
                figis.append(fr.composite_figi)
            if fr.name:
                display_name = fr.name
    
    # Step 3: Try GLEIF search if we have a name
    if display_name != ticker:
        lei_results = search_lei_by_name(display_name, limit=3)
        if lei_results:
            resolution_sources.append("gleif")
            # Use first active result
            for lr in lei_results:
                if lr.entity_status == "ACTIVE":
                    lei = lr.lei
                    legal_name = lr.legal_name
                    jurisdiction = lr.legal_jurisdiction
                    entity_status = lr.entity_status
                    break
            if not lei and lei_results:
                lr = lei_results[0]
                lei = lr.lei
                legal_name = lr.legal_name
                jurisdiction = lr.legal_jurisdiction
                entity_status = lr.entity_status
    
    if not resolution_sources:
        return None
    
    return ResolvedIssuer(
        lei=lei,
        cik=cik,
        legal_name=legal_name,
        display_name=display_name,
        jurisdiction=jurisdiction,
        tickers=tickers,
        isins=isins,
        cusips=cusips,
        figis=figis,
        sedols=sedols,
        entity_status=entity_status,
        resolution_sources=resolution_sources,
    )


def resolve_issuer_by_lei(lei: str) -> ResolvedIssuer | None:
    """Resolve issuer starting from LEI."""
    lei_entity = lookup_lei(lei)
    if not lei_entity:
        return None
    
    return ResolvedIssuer(
        lei=lei,
        cik=None,
        legal_name=lei_entity.legal_name,
        display_name=lei_entity.legal_name,
        jurisdiction=lei_entity.legal_jurisdiction,
        tickers=[],
        isins=[],
        cusips=[],
        figis=[],
        sedols=[],
        entity_status=lei_entity.entity_status,
        resolution_sources=["gleif"],
    )


def resolve_issuer_by_cik(cik: str) -> ResolvedIssuer | None:
    """Resolve issuer starting from SEC CIK."""
    from thoth_terminal.connectors.sec_edgar import get_company_submissions
    
    try:
        submissions = get_company_submissions(cik)
        company_name = submissions.get("name", "")
        tickers_raw = submissions.get("tickers", [])
        exchanges_raw = submissions.get("exchanges", [])
        
        tickers = []
        for i, t in enumerate(tickers_raw):
            exchange = exchanges_raw[i] if i < len(exchanges_raw) else ""
            tickers.append({"ticker": t, "exchange": exchange, "currency": "USD"})
        
        return ResolvedIssuer(
            lei=None,
            cik=cik.zfill(10),
            legal_name=company_name,
            display_name=company_name,
            jurisdiction="US",
            tickers=tickers,
            isins=[],
            cusips=[],
            figis=[],
            sedols=[],
            entity_status="ACTIVE",
            resolution_sources=["sec"],
        )
    except Exception as e:
        logger.warning("Failed to resolve CIK %s: %s", cik, e)
        return None

