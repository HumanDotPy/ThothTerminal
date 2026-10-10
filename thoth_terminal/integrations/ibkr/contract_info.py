from __future__ import annotations

import re
from typing import Any

import requests
from bs4 import BeautifulSoup

from thoth_terminal.ttl_cache import TTLCache


CONTRACT_INFO_URL = "https://contract.ibkr.info/v3.10/index.php"
_CACHE: TTLCache[dict[str, Any] | None] = TTLCache(max_entries=512, default_ttl_s=86400.0)


def _clean(value: object, *, max_len: int | None = None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return text[:max_len] if max_len is not None else text


def _security_type(value: str | None) -> str:
    normalized = str(value or "").strip().upper()
    if normalized == "STOCK":
        return "STK"
    if normalized == "OPTION":
        return "OPT"
    if normalized == "FUTURE":
        return "FUT"
    return normalized or "IBKR_CONTRACT"


def _currency_code(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"\(([A-Z]{3})\)", value)
    if match:
        return match.group(1)
    text = value.strip().upper()
    return text if len(text) == 3 else None


def normalize_contract_info_payload(payload: dict[str, Any], *, conid: str | int | None = None) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    display_name = _clean(
        payload.get("display_name")
        or payload.get("companyName")
        or payload.get("companyHeader")
        or payload.get("description")
        or payload.get("name")
        or payload.get("fullName"),
        max_len=255,
    )
    symbol = _clean(payload.get("ticker") or payload.get("symbol") or payload.get("localSymbol"), max_len=64)
    exchange = _clean(payload.get("exchange") or payload.get("listingExchange"), max_len=64)
    currency = _clean(payload.get("currency"), max_len=16)
    country = _clean(payload.get("country") or payload.get("countryCode") or payload.get("countryName"), max_len=64)
    contract_type = _security_type(
        _clean(payload.get("security_type") or payload.get("secType") or payload.get("assetClass") or payload.get("contractType"), max_len=32)
    )
    resolved_conid = _clean(payload.get("ibkr_conid") or payload.get("conid") or payload.get("con_id") or conid, max_len=64)
    isin = _clean(payload.get("isin") or payload.get("ISIN"), max_len=32)
    if not (display_name or symbol or exchange):
        return None
    return {
        "display_name": display_name or f"IBKR conid {resolved_conid}",
        "ticker": symbol,
        "exchange": exchange,
        "currency": currency,
        "country": country,
        "security_type": contract_type,
        "ibkr_conid": resolved_conid,
        "isin": isin,
        "source": str(payload.get("source") or "ibkr_client_portal_contract_info"),
        "source_url": payload.get("source_url"),
        "raw_fields": payload,
    }


def parse_contract_info_html(html: str, *, conid: str | int | None = None) -> dict[str, Any] | None:
    soup = BeautifulSoup(html or "", "html.parser")
    fields: dict[str, str] = {}
    for row in soup.find_all("tr"):
        cells = [cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"])]
        if len(cells) < 2:
            continue
        label = re.sub(r"\s+", " ", cells[0]).strip()
        value = re.sub(r"\s+", " ", cells[1]).strip()
        if label and value:
            fields[label] = value
            if label.startswith("CONID "):
                fields["CONID"] = value
            elif label.startswith("ISIN "):
                fields["ISIN"] = value

    symbol = _clean(fields.get("Symbol"), max_len=64)
    exchange = _clean(fields.get("Exchange"), max_len=64)
    display_name = _clean(fields.get("Description/Name"), max_len=255)
    contract_type = _security_type(fields.get("Contract Type"))
    currency = _currency_code(fields.get("Currency"))
    country = _clean(fields.get("Country/Region"), max_len=64)
    isin = _clean(fields.get("ISIN"), max_len=32)
    resolved_conid = _clean(fields.get("CONID") or conid, max_len=64)

    if not fields or not (display_name or symbol or exchange):
        return None
    return {
        "display_name": display_name or f"IBKR conid {resolved_conid}",
        "ticker": symbol,
        "exchange": exchange,
        "currency": currency,
        "country": country,
        "security_type": contract_type,
        "ibkr_conid": resolved_conid,
        "isin": isin,
        "source": "ibkr_contract_info",
        "source_url": CONTRACT_INFO_URL,
        "raw_fields": fields,
    }


def fetch_contract_info(
    conid: str | int,
    *,
    session: requests.Session | None = None,
    timeout_s: float = 8.0,
    ttl_s: float = 86400.0,
) -> dict[str, Any] | None:
    cleaned = str(conid or "").strip()
    if not cleaned:
        return None
    cache_key = f"contract-info:{cleaned}"
    cached = _CACHE.get(cache_key)
    if cached is not None:
        return cached
    http = session or requests.Session()
    response = http.get(
        CONTRACT_INFO_URL,
        params={
            "action": "Conid Info",
            "wlId": "IB",
            "conid": cleaned,
            "lang": "en",
            "ib_entity": "",
        },
        timeout=timeout_s,
    )
    response.raise_for_status()
    payload = parse_contract_info_html(response.text, conid=cleaned)
    return _CACHE.set(cache_key, payload, ttl_s=ttl_s)
