from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload, load_only

from thoth_terminal.config import settings
from thoth_terminal.db import utcnow
from thoth_terminal.decision_spine_service import cached_company_decision_page_payload, promote_universe_instrument_dataset_links, universe_population_directory
from thoth_terminal.discord.yahoo import YahooFinanceError, fetch_bulk_returns, fetch_quotes, fetch_quotes_and_returns, search_symbols
from thoth_terminal.models import (
    Company,
    EarningsEvent,
    Entity,
    Issuer,
    IssuerIdentifier,
    ResearchInstrument,
    ResearchInstrumentIdentifier,
    ResearchUniverse,
    ResearchUniverseRow,
    ResearchUniverseSection,
)
from thoth_terminal.ttl_cache import TTLCache, json_cache_weight

def _quote_cache_weight(payload: dict[str, Any]) -> int:
    # Quote dataclasses contain bounded scalar fields. Account for them instead
    # of the generic opaque-object charge used for arbitrary Python objects.
    return json_cache_weight(payload) + json_cache_weight(getattr(payload, "errors", [])) + sum(512 for value in payload.values() if hasattr(value, "price"))


_QUOTE_CACHE: TTLCache[dict[str, Any]] = TTLCache(
    max_entries=128, default_ttl_s=60.0, max_weight=8 * 1024 * 1024, get_weight=_quote_cache_weight,
)


_THEME_ENTITY_KINDS = {"technology", "component", "theme", "concept", "process", "domain", "product"}
_SECURITY_GROUP_BY_SYMBOL = {
    "TSM": "taiwan-semiconductor-manufacturing",
    "2330.TW": "taiwan-semiconductor-manufacturing",
    "ASML": "asml",
    "ASML.AS": "asml",
    "BABA": "alibaba",
    "9988.HK": "alibaba",
    "GOOG": "alphabet",
    "GOOGL": "alphabet",
}

_INSTRUMENT_DETAIL_COLUMNS = (
    ResearchInstrument.id,
    ResearchInstrument.instrument_key,
    ResearchInstrument.display_name,
    ResearchInstrument.legal_name,
    ResearchInstrument.ticker,
    ResearchInstrument.exchange,
    ResearchInstrument.currency,
    ResearchInstrument.country,
    ResearchInstrument.security_type,
    ResearchInstrument.is_public,
    ResearchInstrument.issuer_id,
    ResearchInstrument.company_id,
    ResearchInstrument.ibkr_conid,
    ResearchInstrument.yahoo_symbol,
    ResearchInstrument.primary_provider,
    ResearchInstrument.metadata_json,
)


def _instrument_detail_load_options():
    return (
        load_only(*_INSTRUMENT_DETAIL_COLUMNS),
        joinedload(ResearchInstrument.company).load_only(
            Company.id,
            Company.slug,
            Company.name,
            Company.ticker,
            Company.exchange,
            Company.issuer_id,
        ),
        joinedload(ResearchInstrument.issuer).load_only(
            Issuer.id,
            Issuer.slug,
            Issuer.legal_name,
            Issuer.display_name,
        ),
    )


def _instrument_metadata_text(session: Session, key: str):
    try:
        dialect = str(session.get_bind().dialect.name or "").lower()
    except Exception:
        dialect = ""
    if dialect.startswith("postgres"):
        return ResearchInstrument.metadata_json.op("->>")(key)
    return func.json_extract(ResearchInstrument.metadata_json, f"$.{key}")


@dataclass(frozen=True)
class InstrumentCandidate:
    source: str
    display_name: str
    ticker: str | None = None
    exchange: str | None = None
    currency: str | None = None
    security_type: str = "STK"
    is_public: bool = True
    instrument_id: int | None = None
    issuer_id: int | None = None
    company_id: int | None = None
    ibkr_conid: str | None = None
    yahoo_symbol: str | None = None
    reason: str | None = None
    metadata: dict[str, Any] | None = None


def _slugify(value: str | None, *, default: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return cleaned or default


def _clean_text(value: object, *, max_len: int | None = None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if max_len is not None:
        return text[:max_len]
    return text


def _clean_symbol(value: object) -> str | None:
    text = str(value or "").strip().upper()
    return text or None


_IBKR_EQUITY_TYPES = {"STK", "ETF", "ADR"}
_IBKR_YAHOO_SAFE_EXCHANGES = {
    "NASDAQ",
    "NYSE",
    "NYSEARCA",
    "NYSEAMERICAN",
    "AMEX",
    "BATS",
    "IEX",
}
_ISSUER_YAHOO_SUFFIX_BY_EXCHANGE = {
    "HKEX": ".HK",
    "KRX": ".KS",
    "KOREA": ".KS",
    "KOREA EXCHANGE": ".KS",
    "KOSPI": ".KS",
    "KOSDAQ": ".KQ",
    "SEHK": ".HK",
    "TSE": ".T",
    "TOKYO": ".T",
    "TOKYO STOCK EXCHANGE": ".T",
    "TWSE": ".TW",
    "TAIWAN": ".TW",
    "TAIWAN STOCK EXCHANGE": ".TW",
}
ContractResolver = Callable[[str], dict[str, Any] | None]
SecuritySearch = Callable[[str], list[dict[str, Any]]]


def _ibkr_conid_and_exchange(item: dict[str, Any]) -> tuple[str | None, str | None]:
    raw = _clean_text(item.get("conid") or item.get("conId") or item.get("C"), max_len=128)
    if not raw:
        return None, None
    if "@" in raw:
        conid, exchange = raw.split("@", 1)
        return _clean_text(conid, max_len=64), _clean_text(exchange, max_len=64)
    return _clean_text(raw, max_len=64), None


def _ibkr_security_type(item: dict[str, Any], *, ticker: str | None = None) -> str:
    sec_type = _clean_text(item.get("secType") or item.get("assetClass") or item.get("ST"), max_len=32)
    has_name = bool(_clean_text(item.get("companyName") or item.get("companyHeader") or item.get("description") or item.get("name") or item.get("fullName"), max_len=255))
    if str(sec_type or "").strip().upper() == "STK" and not ticker and not has_name:
        return "IBKR_CONTRACT"
    return str(sec_type or "IBKR_CONTRACT").strip().upper()


def _ibkr_ticker(item: dict[str, Any]) -> str | None:
    return _clean_symbol(item.get("symbol") or item.get("ticker"))


def _ibkr_is_quoteable_equity(*, security_type: str | None, ticker: str | None) -> bool:
    return bool(ticker and str(security_type or "").strip().upper() in _IBKR_EQUITY_TYPES)


def _ibkr_yahoo_symbol(*, ticker: str | None, exchange: str | None, security_type: str | None) -> str | None:
    if not _ibkr_is_quoteable_equity(security_type=security_type, ticker=ticker):
        return None
    symbol = str(ticker or "").strip().upper()
    if not symbol:
        return None
    if "." in symbol:
        return symbol
    if str(exchange or "").strip().upper() in _IBKR_YAHOO_SAFE_EXCHANGES:
        return symbol
    return None


def _issuer_yahoo_symbol(*, ticker: str | None, exchange: str | None) -> str | None:
    symbol = str(ticker or "").strip().upper()
    if not symbol:
        return None
    if "." in symbol:
        return symbol
    suffix = _ISSUER_YAHOO_SUFFIX_BY_EXCHANGE.get(str(exchange or "").strip().upper())
    if not suffix:
        return symbol
    return f"{symbol}{suffix}"


_EXCHANGE_ALIASES = {
    "AMEX": "NYSEAMERICAN",
    "ARCA": "NYSEARCA",
    "HKG": "HKEX",
    "HK": "HKEX",
    "HONG KONG": "HKEX",
    "HONG KONG STOCK EXCHANGE": "HKEX",
    "KOREA": "KRX",
    "KOREA EXCHANGE": "KRX",
    "KOSDAQ": "KOSDAQ",
    "KOSPI": "KRX",
    "NASDAQ": "NASDAQ",
    "NASD": "NASDAQ",
    "NYSE": "NYSE",
    "NYSE ARCA": "NYSEARCA",
    "NYSE MKT": "NYSEAMERICAN",
    "NYSEAMERICAN": "NYSEAMERICAN",
    "SEHK": "HKEX",
    "TAIWAN": "TWSE",
    "TAIWAN STOCK EXCHANGE": "TWSE",
    "TOKYO": "TSE",
    "TOKYO STOCK EXCHANGE": "TSE",
    "TSE": "TSE",
    "TWSE": "TWSE",
}


def _exchange_code(value: str | None) -> str | None:
    text = re.sub(r"[^A-Z0-9]+", " ", str(value or "").upper()).strip()
    if not text:
        return None
    return _EXCHANGE_ALIASES.get(text, text.replace(" ", ""))


def _exchange_matches(left: str | None, right: str | None) -> bool:
    left_code = _exchange_code(left)
    right_code = _exchange_code(right)
    return bool(left_code and right_code and left_code == right_code)


def _base_symbol(value: str | None) -> str | None:
    symbol = _clean_symbol(value)
    if not symbol:
        return None
    return symbol.split(".", 1)[0]


def _provider_yahoo_symbol(*, ticker: str | None, exchange: str | None, security_type: str | None) -> str | None:
    safe_symbol = _ibkr_yahoo_symbol(ticker=ticker, exchange=exchange, security_type=security_type)
    if safe_symbol:
        return safe_symbol
    symbol = _clean_symbol(ticker)
    if not symbol or "." in symbol:
        return symbol
    suffix = _ISSUER_YAHOO_SUFFIX_BY_EXCHANGE.get(str(exchange or "").strip().upper())
    return f"{symbol}{suffix}" if suffix else None


def _ibkr_exchange(item: dict[str, Any], *, fallback: str | None = None) -> str | None:
    exchange = _clean_text(item.get("exchange") or item.get("listingExchange") or item.get("description"), max_len=64)
    if exchange:
        return exchange
    header = str(item.get("companyHeader") or "").strip()
    if "(" in header and ")" in header:
        maybe = header.rsplit("(", 1)[-1].split(")", 1)[0].strip()
        if maybe:
            return _clean_text(maybe, max_len=64)
    return fallback


def _is_sparse_ibkr_contract(instrument: ResearchInstrument) -> bool:
    if not instrument.ibkr_conid:
        return False
    if instrument.ticker or instrument.yahoo_symbol:
        return False
    name = str(instrument.display_name or "").strip().lower()
    if name.startswith("ibkr conid"):
        return True
    meta = instrument.metadata_json if isinstance(instrument.metadata_json, dict) else {}
    raw = meta.get("ibkr_watchlist_item") or meta.get("ibkr")
    if not isinstance(raw, dict):
        return False
    has_symbol = bool(raw.get("ticker") or raw.get("symbol"))
    has_name = bool(raw.get("companyName") or raw.get("companyHeader") or raw.get("description") or raw.get("name") or raw.get("fullName"))
    return not has_symbol and not has_name


def _should_hide_sparse_contract_for_query(instrument: ResearchInstrument, query: str) -> bool:
    if not _is_sparse_ibkr_contract(instrument):
        return False
    return not any(ch.isdigit() for ch in str(query or ""))


def _should_hide_sparse_issuer_for_query(issuer: Issuer, query: str, *, ticker: str | None) -> bool:
    q = str(query or "").strip()
    if not q or any(ch.isdigit() for ch in q):
        return False
    legal_name = str(issuer.legal_name or "").strip().lower()
    if not legal_name.startswith("ibkr conid"):
        return False
    q_lower = q.lower()
    if q_lower in str(issuer.display_name or "").lower():
        return False
    if str(ticker or "").strip().upper() == q.upper():
        return False
    return q_lower in legal_name


def _resolve_ibkr_contract(
    conid: str | None,
    *,
    contract_resolver: ContractResolver | None,
) -> dict[str, Any] | None:
    if not conid or contract_resolver is None:
        return None
    if not str(conid).strip().isdigit():
        return None
    try:
        payload = contract_resolver(str(conid))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _merge_ibkr_contract_info(item: dict[str, Any], info: dict[str, Any] | None) -> dict[str, Any]:
    if not info:
        return item
    merged = dict(item)
    if info.get("display_name"):
        merged.setdefault("name", info.get("display_name"))
        merged.setdefault("fullName", info.get("display_name"))
    if info.get("ticker"):
        merged.setdefault("ticker", info.get("ticker"))
        merged.setdefault("symbol", info.get("ticker"))
    if info.get("exchange"):
        merged.setdefault("exchange", info.get("exchange"))
        merged.setdefault("listingExchange", info.get("exchange"))
    if info.get("currency"):
        merged.setdefault("currency", info.get("currency"))
    if info.get("country"):
        merged.setdefault("country", info.get("country"))
    if info.get("security_type"):
        merged.setdefault("assetClass", info.get("security_type"))
        merged.setdefault("secType", info.get("security_type"))
        merged.setdefault("ST", info.get("security_type"))
    if info.get("ibkr_conid"):
        merged.setdefault("conid", info.get("ibkr_conid"))
    return merged


def _merge_universe_row_payload(target: ResearchUniverseRow, source: ResearchUniverseRow, *, now) -> None:
    if not target.exposure_type and source.exposure_type:
        target.exposure_type = source.exposure_type
    if not target.exposure_strength and source.exposure_strength:
        target.exposure_strength = source.exposure_strength
    if not target.conviction and source.conviction:
        target.conviction = source.conviction
    if not target.notes and source.notes:
        target.notes = source.notes
    if not target.source_provider and source.source_provider:
        target.source_provider = source.source_provider
    if not target.source_external_id and source.source_external_id:
        target.source_external_id = source.source_external_id
    metadata = dict(source.metadata_json or {}) | dict(target.metadata_json or {})
    merged_row_ids = list(metadata.get("merged_row_ids") or [])
    if int(source.id) not in merged_row_ids:
        merged_row_ids.append(int(source.id))
    metadata["merged_row_ids"] = merged_row_ids
    target.metadata_json = metadata
    target.updated_at = now


def _merge_instrument_into_listing(
    session: Session,
    *,
    source: ResearchInstrument,
    target: ResearchInstrument,
    info: dict[str, Any],
    ticker: str | None,
    exchange: str | None,
    security_type: str,
    yahoo_symbol: str | None,
    now,
) -> None:
    target_metadata = dict(target.metadata_json or {})
    target_metadata["ibkr_contract_info"] = info
    merged_instrument_ids = list(target_metadata.get("merged_instrument_ids") or [])
    if int(source.id) not in merged_instrument_ids:
        merged_instrument_ids.append(int(source.id))
    target_metadata["merged_instrument_ids"] = merged_instrument_ids
    target.display_name = _clean_text(info.get("display_name"), max_len=255) or target.display_name
    target.legal_name = _clean_text(info.get("display_name"), max_len=255) or target.legal_name
    target.ticker = ticker or target.ticker
    target.exchange = exchange or target.exchange
    target.currency = _clean_text(info.get("currency"), max_len=16) or target.currency
    target.country = _clean_text(info.get("country"), max_len=8) or target.country
    target.security_type = security_type or target.security_type
    target.is_public = bool(_ibkr_is_quoteable_equity(security_type=security_type, ticker=ticker))
    target.ibkr_conid = _clean_text(info.get("ibkr_conid") or source.ibkr_conid, max_len=64) or target.ibkr_conid
    target.yahoo_symbol = yahoo_symbol if yahoo_symbol is not None else target.yahoo_symbol
    target.primary_provider = target.primary_provider or "ibkr"
    target.metadata_json = target_metadata
    target.updated_at = now

    for ident in (
        session.query(ResearchInstrumentIdentifier)
        .filter(ResearchInstrumentIdentifier.instrument_id == int(source.id))
        .all()
    ):
        ident.instrument_id = int(target.id)

    for row in (
        session.query(ResearchUniverseRow)
        .filter(ResearchUniverseRow.instrument_id == int(source.id), ResearchUniverseRow.archived_at.is_(None))
        .all()
    ):
        duplicate = (
            session.query(ResearchUniverseRow)
            .filter(
                ResearchUniverseRow.universe_id == int(row.universe_id),
                ResearchUniverseRow.section_id == int(row.section_id),
                ResearchUniverseRow.instrument_id == int(target.id),
                ResearchUniverseRow.role == row.role,
                ResearchUniverseRow.archived_at.is_(None),
            )
            .order_by(ResearchUniverseRow.id.asc())
            .first()
        )
        if duplicate is not None:
            _merge_universe_row_payload(duplicate, row, now=now)
            row_metadata = dict(row.metadata_json or {})
            row_metadata["merged_into_row_id"] = int(duplicate.id)
            row_metadata["merged_into_instrument_id"] = int(target.id)
            row_metadata["merged_at"] = now.isoformat()
            row.metadata_json = row_metadata
            row.archived_at = now
            row.updated_at = now
        else:
            row.instrument_id = int(target.id)
            row_metadata = dict(row.metadata_json or {})
            row_metadata["merged_from_instrument_id"] = int(source.id)
            row.metadata_json = row_metadata
            row.updated_at = now

    source_metadata = dict(source.metadata_json or {})
    source_metadata["merged_into_instrument_id"] = int(target.id)
    source_metadata["merged_at"] = now.isoformat()
    source_metadata["merged_reason"] = "resolved_ibkr_contract_matched_existing_listing"
    source.metadata_json = source_metadata
    source.display_name = f"Merged instrument {source.id}"
    source.legal_name = None
    source.ticker = None
    source.exchange = None
    source.currency = None
    source.country = None
    source.security_type = "MERGED"
    source.is_public = False
    source.ibkr_conid = None
    source.yahoo_symbol = None
    source.primary_provider = "merged"
    source.updated_at = now

    _upsert_identifier(
        session,
        target,
        provider="ibkr",
        id_type="conid",
        id_value=target.ibkr_conid,
        exchange=target.exchange,
        currency=target.currency,
        is_primary=target.primary_provider == "ibkr",
    )
    if target.yahoo_symbol:
        _upsert_identifier(
            session,
            target,
            provider="yahoo",
            id_type="symbol",
            id_value=target.yahoo_symbol,
            exchange=target.exchange,
            currency=target.currency,
            is_primary=target.primary_provider == "yahoo",
        )
    if target.ticker:
        _upsert_identifier(
            session,
            target,
            provider="ticker",
            id_type="symbol",
            id_value=target.ticker,
            exchange=target.exchange,
            currency=target.currency,
            is_primary=False,
        )


def normalize_sparse_ibkr_contracts(
    session: Session,
    *,
    contract_resolver: ContractResolver | None = None,
    limit: int | None = None,
) -> int:
    changed = 0
    query = (
        session.query(ResearchInstrument)
        .filter(
            ResearchInstrument.ibkr_conid.isnot(None),
            or_(ResearchInstrument.ticker.is_(None), ResearchInstrument.ticker == ""),
            or_(ResearchInstrument.yahoo_symbol.is_(None), ResearchInstrument.yahoo_symbol == ""),
        )
        .order_by(ResearchInstrument.updated_at.asc(), ResearchInstrument.id.asc())
    )
    if limit is not None:
        try:
            max_rows = int(limit)
        except (TypeError, ValueError):
            max_rows = 25
        query = query.limit(max(1, min(max_rows, 500)))
    rows = query.all()
    now = utcnow()
    for instrument in rows:
        if not _is_sparse_ibkr_contract(instrument):
            continue
        info = _resolve_ibkr_contract(instrument.ibkr_conid, contract_resolver=contract_resolver)
        if info:
            ticker = _clean_symbol(info.get("ticker"))
            exchange = _clean_text(info.get("exchange"), max_len=64)
            security_type = str(info.get("security_type") or "IBKR_CONTRACT").strip().upper()
            yahoo_symbol = _ibkr_yahoo_symbol(ticker=ticker, exchange=exchange, security_type=security_type)
            issuer = _issuer_for_candidate(
                session,
                ticker=ticker or yahoo_symbol,
                display_name=_clean_text(info.get("display_name"), max_len=255) or instrument.display_name,
            )
            issuer_id = int(issuer.id) if issuer is not None and issuer.id is not None else None
            existing = _find_existing_listing_by_issuer_ticker(session, issuer_id=issuer_id, ticker=ticker or yahoo_symbol)
            if existing is not None and int(existing.id) != int(instrument.id):
                _merge_instrument_into_listing(
                    session,
                    source=instrument,
                    target=existing,
                    info=info,
                    ticker=ticker,
                    exchange=exchange,
                    security_type=security_type,
                    yahoo_symbol=yahoo_symbol,
                    now=now,
                )
                changed += 1
                continue
            metadata = dict(instrument.metadata_json or {})
            metadata["ibkr_contract_info"] = info
            instrument.display_name = _clean_text(info.get("display_name"), max_len=255) or instrument.display_name
            instrument.legal_name = _clean_text(info.get("display_name"), max_len=255) or instrument.legal_name
            instrument.ticker = ticker
            instrument.exchange = exchange
            instrument.currency = _clean_text(info.get("currency"), max_len=16) or instrument.currency
            instrument.country = _clean_text(info.get("country"), max_len=8) or instrument.country
            instrument.security_type = security_type
            instrument.is_public = bool(_ibkr_is_quoteable_equity(security_type=security_type, ticker=ticker))
            instrument.issuer_id = issuer_id if issuer_id is not None else instrument.issuer_id
            instrument.yahoo_symbol = yahoo_symbol
            instrument.metadata_json = metadata
            instrument.updated_at = now
            changed += 1
            continue
        dirty = False
        if instrument.security_type != "IBKR_CONTRACT":
            instrument.security_type = "IBKR_CONTRACT"
            dirty = True
        if instrument.is_public:
            instrument.is_public = False
            dirty = True
        if instrument.yahoo_symbol is not None:
            instrument.yahoo_symbol = None
            dirty = True
        if dirty:
            instrument.updated_at = now
            changed += 1
    if changed:
        session.flush()
    return changed


def _reconciliation_search_symbol(instrument: ResearchInstrument) -> str | None:
    for raw in (instrument.yahoo_symbol, instrument.ticker):
        symbol = _base_symbol(raw)
        if symbol:
            return symbol
    return None


def _ibkr_listing_info(item: dict[str, Any]) -> dict[str, Any] | None:
    conid, conid_exchange = _ibkr_conid_and_exchange(item)
    ticker = _ibkr_ticker(item)
    security_type = _ibkr_security_type(item, ticker=ticker)
    exchange = _ibkr_exchange(item, fallback=conid_exchange)
    if not conid or not ticker or not _ibkr_is_quoteable_equity(security_type=security_type, ticker=ticker):
        return None
    yahoo_symbol = _provider_yahoo_symbol(ticker=ticker, exchange=exchange, security_type=security_type)
    return {
        "display_name": _clean_text(
            item.get("companyName")
            or item.get("companyHeader")
            or item.get("description")
            or item.get("name")
            or item.get("fullName")
            or ticker,
            max_len=255,
        ),
        "ticker": ticker,
        "exchange": _clean_text(exchange, max_len=64),
        "currency": _clean_text(item.get("currency"), max_len=16),
        "country": _clean_text(item.get("country") or item.get("countryName"), max_len=8),
        "security_type": security_type,
        "ibkr_conid": conid,
        "yahoo_symbol": yahoo_symbol,
        "source": item.get("_thoth_source") or "ibkr_security_search",
        "raw": item,
    }


def _score_ibkr_listing_match(instrument: ResearchInstrument, info: dict[str, Any], *, query_symbol: str) -> int:
    score = 0
    target_symbols = {_clean_symbol(instrument.ticker), _clean_symbol(instrument.yahoo_symbol), _base_symbol(instrument.ticker), _base_symbol(instrument.yahoo_symbol)}
    target_symbols.discard(None)
    ticker = _clean_symbol(info.get("ticker"))
    yahoo_symbol = _clean_symbol(info.get("yahoo_symbol"))
    if ticker and ticker in target_symbols:
        score += 6 if ticker == _clean_symbol(query_symbol) else 5
    if yahoo_symbol and yahoo_symbol in target_symbols:
        score += 6
    if _exchange_matches(instrument.exchange, info.get("exchange")):
        score += 4
    if instrument.yahoo_symbol and yahoo_symbol and _clean_symbol(instrument.yahoo_symbol) == yahoo_symbol:
        score += 4
    if info.get("ibkr_conid"):
        score += 1
    return score


def _best_ibkr_listing_match(
    instrument: ResearchInstrument,
    rows: list[dict[str, Any]],
    *,
    query_symbol: str,
) -> tuple[dict[str, Any] | None, str | None]:
    scored: list[tuple[int, dict[str, Any]]] = []
    for item in rows:
        info = _ibkr_listing_info(item)
        if info is None:
            continue
        score = _score_ibkr_listing_match(instrument, info, query_symbol=query_symbol)
        if score >= 7:
            scored.append((score, info))
    if not scored:
        return None, "no_confident_match"
    scored.sort(key=lambda row: row[0], reverse=True)
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None, "ambiguous_match"
    return scored[0][1], None


def reconcile_universe_provider_links(
    session: Session,
    *,
    security_search: SecuritySearch,
    universe_id: int | None = None,
    limit: int = 25,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Enrich existing universe listings with IBKR identifiers when Gateway is available.

    This intentionally runs only when called by an operator, automation, or an API
    route. It avoids live broker calls during ordinary universe navigation.
    """

    max_rows = max(1, min(int(limit or 25), 100))
    query = session.query(ResearchInstrument).filter(
        ResearchInstrument.is_public.is_(True),
        ResearchInstrument.security_type != "MERGED",
        ResearchInstrument.ibkr_conid.is_(None),
        or_(ResearchInstrument.ticker.isnot(None), ResearchInstrument.yahoo_symbol.isnot(None)),
    )
    if universe_id is not None:
        query = query.join(ResearchUniverseRow, ResearchUniverseRow.instrument_id == ResearchInstrument.id).filter(
            ResearchUniverseRow.universe_id == int(universe_id),
            ResearchUniverseRow.archived_at.is_(None),
        )
    instruments = query.order_by(ResearchInstrument.updated_at.desc(), ResearchInstrument.id.asc()).limit(max_rows).all()
    now = utcnow()
    summary: dict[str, Any] = {
        "ok": True,
        "dry_run": bool(dry_run),
        "scanned": 0,
        "linked": 0,
        "merged": 0,
        "skipped": 0,
        "ambiguous": 0,
        "no_match": 0,
        "errors": 0,
        "items": [],
    }
    searched: dict[str, list[dict[str, Any]]] = {}
    for instrument in instruments:
        summary["scanned"] += 1
        symbol = _reconciliation_search_symbol(instrument)
        item_result: dict[str, Any] = {
            "instrument_id": int(instrument.id),
            "display_name": instrument.display_name,
            "ticker": instrument.ticker,
            "yahoo_symbol": instrument.yahoo_symbol,
            "exchange": instrument.exchange,
            "action": "skipped",
        }
        if not symbol:
            summary["skipped"] += 1
            item_result["reason"] = "missing_symbol"
            summary["items"].append(item_result)
            continue
        try:
            rows = searched.get(symbol)
            if rows is None:
                rows = security_search(symbol)
                searched[symbol] = rows
        except Exception as exc:
            summary["errors"] += 1
            item_result["action"] = "error"
            item_result["reason"] = f"{type(exc).__name__}: {exc}"
            summary["items"].append(item_result)
            continue
        match, reason = _best_ibkr_listing_match(instrument, rows, query_symbol=symbol)
        if match is None:
            key = "ambiguous" if reason == "ambiguous_match" else "no_match"
            summary[key] += 1
            item_result["action"] = key
            item_result["reason"] = reason
            summary["items"].append(item_result)
            continue
        conid = _clean_text(match.get("ibkr_conid"), max_len=64)
        if not conid:
            summary["no_match"] += 1
            item_result["action"] = "no_match"
            item_result["reason"] = "missing_conid"
            summary["items"].append(item_result)
            continue
        existing = _find_existing_by_identifier(session, provider="ibkr", id_type="conid", id_value=conid)
        item_result.update(
            {
                "ibkr_conid": conid,
                "matched_ticker": match.get("ticker"),
                "matched_exchange": match.get("exchange"),
                "matched_yahoo_symbol": match.get("yahoo_symbol"),
            }
        )
        if existing is not None and int(existing.id) != int(instrument.id):
            summary["merged"] += 1
            item_result["action"] = "merge_existing_ibkr_listing"
            item_result["merged_instrument_id"] = int(existing.id)
            if not dry_run:
                _merge_instrument_into_listing(
                    session,
                    source=existing,
                    target=instrument,
                    info=match,
                    ticker=_clean_symbol(match.get("ticker")),
                    exchange=_clean_text(match.get("exchange"), max_len=64),
                    security_type=str(match.get("security_type") or "STK").strip().upper(),
                    yahoo_symbol=_clean_symbol(match.get("yahoo_symbol")) or instrument.yahoo_symbol,
                    now=now,
                )
        else:
            summary["linked"] += 1
            item_result["action"] = "linked_ibkr_identifier"
            if not dry_run:
                metadata = dict(instrument.metadata_json or {})
                metadata["provider_reconciliation"] = {
                    "provider": "ibkr",
                    "matched_at": now.isoformat(),
                    "symbol": symbol,
                    "source": match.get("source"),
                    "matched_exchange": match.get("exchange"),
                }
                instrument.ibkr_conid = conid
                instrument.ticker = _clean_symbol(match.get("ticker")) or instrument.ticker
                instrument.exchange = _clean_text(match.get("exchange"), max_len=64) or instrument.exchange
                instrument.currency = _clean_text(match.get("currency"), max_len=16) or instrument.currency
                instrument.country = _clean_text(match.get("country"), max_len=8) or instrument.country
                instrument.security_type = str(match.get("security_type") or instrument.security_type or "STK").strip().upper()
                instrument.yahoo_symbol = _clean_symbol(match.get("yahoo_symbol")) or instrument.yahoo_symbol
                instrument.metadata_json = metadata
                instrument.updated_at = now
                _upsert_identifier(
                    session,
                    instrument,
                    provider="ibkr",
                    id_type="conid",
                    id_value=conid,
                    exchange=instrument.exchange,
                    currency=instrument.currency,
                    is_primary=False,
                )
                if instrument.yahoo_symbol:
                    _upsert_identifier(
                        session,
                        instrument,
                        provider="yahoo",
                        id_type="symbol",
                        id_value=instrument.yahoo_symbol,
                        exchange=instrument.exchange,
                        currency=instrument.currency,
                        is_primary=instrument.primary_provider == "yahoo",
                    )
        summary["items"].append(item_result)
    if not dry_run and (summary["linked"] or summary["merged"]):
        session.flush()
    return summary


def _instrument_key(
    *,
    display_name: str,
    ticker: str | None = None,
    exchange: str | None = None,
    ibkr_conid: str | None = None,
    yahoo_symbol: str | None = None,
    is_public: bool = True,
) -> str:
    if ibkr_conid:
        return f"ibkr:{ibkr_conid.strip()}"
    if yahoo_symbol:
        return f"yahoo:{yahoo_symbol.strip().upper()}"
    if ticker:
        exch = str(exchange or "").strip().upper()
        return f"ticker:{ticker.strip().upper()}:{exch}"
    prefix = "private" if not is_public else "local"
    return f"{prefix}:{_slugify(display_name, default='instrument')}"


def _find_existing_by_identifier(
    session: Session,
    *,
    provider: str,
    id_type: str,
    id_value: str | None,
) -> ResearchInstrument | None:
    value = str(id_value or "").strip()
    if not value:
        return None
    row = (
        session.query(ResearchInstrumentIdentifier)
        .filter(
            ResearchInstrumentIdentifier.provider == provider,
            ResearchInstrumentIdentifier.id_type == id_type,
            func.upper(ResearchInstrumentIdentifier.id_value) == value.upper(),
        )
        .first()
    )
    if row is None:
        if provider == "ibkr" and id_type == "conid":
            return (
                session.query(ResearchInstrument)
                .filter(func.upper(func.coalesce(ResearchInstrument.ibkr_conid, "")) == value.upper())
                .filter(or_(ResearchInstrument.security_type.is_(None), ResearchInstrument.security_type != "MERGED"))
                .order_by(ResearchInstrument.id.asc())
                .first()
            )
        return None
    return session.get(ResearchInstrument, int(row.instrument_id))


def _find_existing_listing_by_issuer_ticker(
    session: Session,
    *,
    issuer_id: int | None,
    ticker: str | None,
) -> ResearchInstrument | None:
    symbol = _clean_symbol(ticker)
    if issuer_id is None or not symbol:
        return None
    clauses = [
        func.upper(func.coalesce(ResearchInstrument.ticker, "")) == symbol,
        func.upper(func.coalesce(ResearchInstrument.yahoo_symbol, "")) == symbol,
    ]
    base_symbol = symbol.split(".", 1)[0]
    if "." in symbol and base_symbol:
        clauses.extend(
            [
                func.upper(func.coalesce(ResearchInstrument.ticker, "")) == base_symbol,
                func.upper(func.coalesce(ResearchInstrument.yahoo_symbol, "")) == base_symbol,
            ]
        )
    elif base_symbol:
        suffix_like = f"{base_symbol}.%"
        clauses.extend(
            [
                func.upper(func.coalesce(ResearchInstrument.ticker, "")).like(suffix_like),
                func.upper(func.coalesce(ResearchInstrument.yahoo_symbol, "")).like(suffix_like),
            ]
        )
    return (
        session.query(ResearchInstrument)
        .filter(
            ResearchInstrument.is_public.is_(True),
            ResearchInstrument.issuer_id == int(issuer_id),
            or_(*clauses),
        )
        .order_by(ResearchInstrument.id.asc())
        .first()
    )


def _issuer_for_candidate(
    session: Session,
    *,
    ticker: str | None,
    display_name: str | None,
) -> Issuer | None:
    symbol = _clean_symbol(ticker)
    if symbol:
        ident = (
            session.query(IssuerIdentifier)
            .filter(IssuerIdentifier.id_type == "TICKER", func.upper(IssuerIdentifier.id_value) == symbol)
            .order_by(IssuerIdentifier.is_primary.desc(), IssuerIdentifier.id.asc())
            .first()
        )
        if ident is not None:
            return session.get(Issuer, int(ident.issuer_id))

    name = str(display_name or "").strip()
    if len(name) < 4:
        return None
    normalized = re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()
    if not normalized:
        return None
    candidates = session.query(Issuer).limit(250).all()
    for issuer in candidates:
        issuer_name = re.sub(r"[^a-z0-9]+", " ", str(issuer.display_name or issuer.legal_name or "").lower()).strip()
        if not issuer_name:
            continue
        if issuer_name in normalized or normalized in issuer_name:
            return issuer
    return None


def _upsert_identifier(
    session: Session,
    instrument: ResearchInstrument,
    *,
    provider: str,
    id_type: str,
    id_value: str | None,
    exchange: str | None = None,
    currency: str | None = None,
    is_primary: bool = False,
    metadata: dict[str, Any] | None = None,
) -> None:
    value = str(id_value or "").strip()
    if not value:
        return
    now = utcnow()
    row = (
        session.query(ResearchInstrumentIdentifier)
        .filter(
            ResearchInstrumentIdentifier.provider == provider,
            ResearchInstrumentIdentifier.id_type == id_type,
            func.upper(ResearchInstrumentIdentifier.id_value) == value.upper(),
        )
        .first()
    )
    if row is None:
        session.add(
            ResearchInstrumentIdentifier(
                instrument_id=int(instrument.id),
                provider=provider,
                id_type=id_type,
                id_value=value,
                exchange=exchange,
                currency=currency,
                is_primary=is_primary,
                metadata_json=metadata or {},
                created_at=now,
            )
        )
        return
    if int(row.instrument_id) != int(instrument.id):
        return
    row.exchange = exchange if exchange is not None else row.exchange
    row.currency = currency if currency is not None else row.currency
    row.is_primary = bool(row.is_primary or is_primary)
    if metadata:
        row.metadata_json = dict(row.metadata_json or {}) | dict(metadata)


def ensure_instrument(
    session: Session,
    *,
    display_name: str,
    ticker: str | None = None,
    exchange: str | None = None,
    currency: str | None = None,
    country: str | None = None,
    security_type: str | None = None,
    is_public: bool = True,
    issuer_id: int | None = None,
    company_id: int | None = None,
    ibkr_conid: str | None = None,
    yahoo_symbol: str | None = None,
    primary_provider: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> ResearchInstrument:
    name = _clean_text(display_name, max_len=255) or _clean_symbol(ticker) or "Unknown instrument"
    ticker_v = _clean_symbol(ticker)
    ibkr_v = _clean_text(ibkr_conid, max_len=64)
    provider_v = _clean_text(primary_provider, max_len=32)
    yahoo_v = _clean_symbol(yahoo_symbol)
    if not yahoo_v and not ibkr_v and (provider_v == "yahoo" or is_public):
        yahoo_v = ticker_v
    security_v = str(security_type or ("STK" if is_public else "PRIVATE")).strip().upper()

    existing = None
    if ibkr_v:
        existing = _find_existing_by_identifier(session, provider="ibkr", id_type="conid", id_value=ibkr_v)
    if existing is None and yahoo_v:
        existing = _find_existing_by_identifier(session, provider="yahoo", id_type="symbol", id_value=yahoo_v)
    if existing is None:
        existing = _find_existing_listing_by_issuer_ticker(session, issuer_id=issuer_id, ticker=ticker_v)

    key = _instrument_key(
        display_name=name,
        ticker=ticker_v,
        exchange=exchange,
        ibkr_conid=ibkr_v,
        yahoo_symbol=yahoo_v if primary_provider == "yahoo" or not ibkr_v else None,
        is_public=is_public,
    )
    if existing is None:
        existing = session.query(ResearchInstrument).filter_by(instrument_key=key).first()

    now = utcnow()
    if issuer_id is None:
        issuer = _issuer_for_candidate(session, ticker=ticker_v or yahoo_v, display_name=name)
        issuer_id = int(issuer.id) if issuer is not None and issuer.id is not None else None
    if existing is None:
        existing = _find_existing_listing_by_issuer_ticker(session, issuer_id=issuer_id, ticker=ticker_v)

    metadata_payload = dict(metadata or {})
    group_key = _SECURITY_GROUP_BY_SYMBOL.get(str(ticker_v or yahoo_v or "").upper())
    if group_key and "security_group" not in metadata_payload:
        metadata_payload["security_group"] = group_key

    if existing is None:
        existing = ResearchInstrument(
            instrument_key=key,
            display_name=name,
            legal_name=_clean_text(metadata.get("legal_name") if metadata else None, max_len=512),
            ticker=ticker_v,
            exchange=_clean_text(exchange, max_len=64),
            currency=_clean_text(currency, max_len=16),
            country=_clean_text(country, max_len=8),
            security_type=security_v,
            is_public=bool(is_public),
            issuer_id=issuer_id,
            company_id=company_id,
            ibkr_conid=ibkr_v,
            yahoo_symbol=yahoo_v,
            primary_provider=provider_v or ("ibkr" if ibkr_v else "yahoo" if yahoo_v else "local"),
            metadata_json=metadata_payload,
            created_at=now,
            updated_at=now,
        )
        session.add(existing)
        session.flush()
    else:
        existing.display_name = name or existing.display_name
        existing.ticker = ticker_v or existing.ticker
        existing.exchange = _clean_text(exchange, max_len=64) or existing.exchange
        existing.currency = _clean_text(currency, max_len=16) or existing.currency
        existing.country = _clean_text(country, max_len=8) or existing.country
        existing.security_type = security_v or existing.security_type
        existing.is_public = bool(is_public)
        existing.issuer_id = issuer_id if issuer_id is not None else existing.issuer_id
        existing.company_id = company_id if company_id is not None else existing.company_id
        existing.ibkr_conid = ibkr_v or existing.ibkr_conid
        existing.yahoo_symbol = yahoo_v if yahoo_v is not None else existing.yahoo_symbol
        existing.primary_provider = provider_v or existing.primary_provider
        existing.metadata_json = dict(existing.metadata_json or {}) | metadata_payload
        existing.updated_at = now
        session.flush()

    _upsert_identifier(
        session,
        existing,
        provider="local",
        id_type="instrument_key",
        id_value=existing.instrument_key,
        is_primary=True,
    )
    _upsert_identifier(
        session,
        existing,
        provider="ibkr",
        id_type="conid",
        id_value=existing.ibkr_conid,
        exchange=existing.exchange,
        currency=existing.currency,
        is_primary=existing.primary_provider == "ibkr",
    )
    _upsert_identifier(
        session,
        existing,
        provider="yahoo",
        id_type="symbol",
        id_value=existing.yahoo_symbol,
        exchange=existing.exchange,
        currency=existing.currency,
        is_primary=existing.primary_provider == "yahoo",
    )
    if existing.ticker:
        _upsert_identifier(
            session,
            existing,
            provider="ticker",
            id_type="symbol",
            id_value=existing.ticker,
            exchange=existing.exchange,
            currency=existing.currency,
            is_primary=False,
        )
    return existing


def ensure_universe(
    session: Session,
    *,
    name: str,
    slug: str | None = None,
    description: str | None = None,
    sort_order: int = 0,
    metadata: dict[str, Any] | None = None,
) -> ResearchUniverse:
    slug_v = _slugify(slug or name, default="universe")
    now = utcnow()
    universe = session.query(ResearchUniverse).filter_by(slug=slug_v).first()
    if universe is None:
        universe = ResearchUniverse(
            slug=slug_v,
            name=name.strip(),
            description=description,
            status="active",
            sort_order=sort_order,
            metadata_json=metadata or {},
            created_at=now,
            updated_at=now,
        )
        session.add(universe)
        session.flush()
    else:
        universe.name = name.strip() or universe.name
        universe.description = description if description is not None else universe.description
        universe.sort_order = sort_order if sort_order is not None else universe.sort_order
        universe.metadata_json = dict(universe.metadata_json or {}) | dict(metadata or {})
        universe.updated_at = now
    return universe


def ensure_section(
    session: Session,
    universe: ResearchUniverse,
    *,
    name: str,
    sort_order: int | None = None,
    description: str | None = None,
    source_provider: str | None = None,
    source_external_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> ResearchUniverseSection:
    section_name = _clean_text(name, max_len=255) or "Unsectioned"
    key = _slugify(section_name, default=f"section-{sort_order or 0}")
    section = (
        session.query(ResearchUniverseSection)
        .filter(ResearchUniverseSection.universe_id == int(universe.id), ResearchUniverseSection.section_key == key)
        .first()
    )
    now = utcnow()
    if section is None:
        if sort_order is None:
            sort_order = (
                session.query(func.coalesce(func.max(ResearchUniverseSection.sort_order), -1))
                .filter(ResearchUniverseSection.universe_id == int(universe.id))
                .scalar()
                or -1
            ) + 1
        section = ResearchUniverseSection(
            universe_id=int(universe.id),
            section_key=key,
            name=section_name,
            description=description,
            sort_order=int(sort_order or 0),
            source_provider=source_provider,
            source_external_id=source_external_id,
            metadata_json=metadata or {},
            created_at=now,
            updated_at=now,
        )
        session.add(section)
        session.flush()
    else:
        section.name = section_name
        section.description = description if description is not None else section.description
        section.source_provider = source_provider if source_provider is not None else section.source_provider
        section.source_external_id = source_external_id if source_external_id is not None else section.source_external_id
        section.metadata_json = dict(section.metadata_json or {}) | dict(metadata or {})
        section.updated_at = now
    return section


def update_universe(
    session: Session,
    universe_id: int,
    *,
    name: str | None = None,
    description: str | None = None,
    status: str | None = None,
) -> ResearchUniverse:
    universe = session.get(ResearchUniverse, int(universe_id))
    if universe is None:
        raise ValueError("Universe not found.")
    if name is not None:
        clean_name = _clean_text(name, max_len=255)
        if not clean_name:
            raise ValueError("Universe name is required.")
        universe.name = clean_name
    if description is not None:
        universe.description = _clean_text(description)
    if status is not None:
        universe.status = _clean_text(status, max_len=32) or universe.status
    universe.updated_at = utcnow()
    session.flush()
    return universe


def delete_universe(session: Session, universe_id: int) -> None:
    universe = session.get(ResearchUniverse, int(universe_id))
    if universe is None:
        raise ValueError("Universe not found.")
    session.delete(universe)
    session.flush()


def _ordered_unique_ids(values: list[int] | tuple[int, ...] | set[int] | None) -> list[int]:
    out: list[int] = []
    seen: set[int] = set()
    for value in values or []:
        try:
            parsed = int(value)
        except Exception:
            continue
        if parsed <= 0 or parsed in seen:
            continue
        seen.add(parsed)
        out.append(parsed)
    return out


def reorder_universes(session: Session, universe_ids: list[int] | tuple[int, ...] | set[int]) -> list[ResearchUniverse]:
    ordered_ids = _ordered_unique_ids(universe_ids)
    if not ordered_ids:
        raise ValueError("Universe order is required.")
    rows = (
        session.query(ResearchUniverse)
        .filter(ResearchUniverse.status == "active")
        .order_by(ResearchUniverse.sort_order.asc(), ResearchUniverse.name.asc(), ResearchUniverse.id.asc())
        .all()
    )
    by_id = {int(row.id): row for row in rows}
    missing = [row_id for row_id in ordered_ids if row_id not in by_id]
    if missing:
        raise ValueError("Universe order contains unknown universe ids.")
    now = utcnow()
    final_ids = ordered_ids + [int(row.id) for row in rows if int(row.id) not in set(ordered_ids)]
    for index, row_id in enumerate(final_ids):
        row = by_id[row_id]
        row.sort_order = index
        row.updated_at = now
    session.flush()
    return [by_id[row_id] for row_id in final_ids]


def update_section(
    session: Session,
    section_id: int,
    *,
    name: str | None = None,
    description: str | None = None,
) -> ResearchUniverseSection:
    section = session.get(ResearchUniverseSection, int(section_id))
    if section is None:
        raise ValueError("Section not found.")
    if name is not None:
        clean_name = _clean_text(name, max_len=255)
        if not clean_name:
            raise ValueError("Section name is required.")
        section.name = clean_name
        section.section_key = _slugify(clean_name, default=f"section-{section.id}")
    if description is not None:
        section.description = _clean_text(description)
    section.updated_at = utcnow()
    session.flush()
    return section


def reorder_universe_sections(
    session: Session,
    universe_id: int,
    section_ids: list[int] | tuple[int, ...] | set[int],
) -> list[ResearchUniverseSection]:
    universe = session.get(ResearchUniverse, int(universe_id))
    if universe is None:
        raise ValueError("Universe not found.")
    ordered_ids = _ordered_unique_ids(section_ids)
    if not ordered_ids:
        raise ValueError("Section order is required.")
    sections = (
        session.query(ResearchUniverseSection)
        .filter(ResearchUniverseSection.universe_id == int(universe.id))
        .order_by(ResearchUniverseSection.sort_order.asc(), ResearchUniverseSection.id.asc())
        .all()
    )
    by_id = {int(section.id): section for section in sections}
    missing = [section_id for section_id in ordered_ids if section_id not in by_id]
    if missing:
        raise ValueError("Section order contains sections outside this universe.")
    now = utcnow()
    final_ids = ordered_ids + [int(section.id) for section in sections if int(section.id) not in set(ordered_ids)]
    for index, section_id in enumerate(final_ids):
        section = by_id[section_id]
        section.sort_order = index
        section.updated_at = now
    universe.updated_at = now
    session.flush()
    return [by_id[section_id] for section_id in final_ids]


def delete_section(session: Session, section_id: int) -> None:
    section = session.get(ResearchUniverseSection, int(section_id))
    if section is None:
        raise ValueError("Section not found.")
    session.delete(section)
    session.flush()


def update_universe_row(
    session: Session,
    row_id: int,
    *,
    display_label: str | None = None,
    role: str | None = None,
    exposure_type: str | None = None,
    exposure_strength: str | None = None,
    conviction: str | None = None,
    notes: str | None = None,
) -> ResearchUniverseRow:
    row = session.get(ResearchUniverseRow, int(row_id))
    if row is None or row.archived_at is not None:
        raise ValueError("Universe row not found.")
    if display_label is not None:
        row.display_label = _clean_text(display_label, max_len=255) or row.display_label
    if role is not None:
        row.role = _clean_text(role, max_len=128) or ""
    if exposure_type is not None:
        row.exposure_type = _clean_text(exposure_type, max_len=128)
    if exposure_strength is not None:
        row.exposure_strength = _clean_text(exposure_strength, max_len=32)
    if conviction is not None:
        row.conviction = _clean_text(conviction, max_len=32)
    if notes is not None:
        row.notes = _clean_text(notes)
    row.updated_at = utcnow()
    session.flush()
    return row


def reorder_universe_rows(
    session: Session,
    section_id: int,
    row_ids: list[int] | tuple[int, ...] | set[int],
) -> list[ResearchUniverseRow]:
    section = session.get(ResearchUniverseSection, int(section_id))
    if section is None:
        raise ValueError("Section not found.")
    ordered_ids = _ordered_unique_ids(row_ids)
    if not ordered_ids:
        raise ValueError("Row order is required.")
    rows = (
        session.query(ResearchUniverseRow)
        .filter(ResearchUniverseRow.section_id == int(section.id), ResearchUniverseRow.archived_at.is_(None))
        .order_by(ResearchUniverseRow.sort_order.asc(), ResearchUniverseRow.id.asc())
        .all()
    )
    by_id = {int(row.id): row for row in rows}
    missing = [row_id for row_id in ordered_ids if row_id not in by_id]
    if missing:
        raise ValueError("Row order contains rows outside this section.")
    now = utcnow()
    final_ids = ordered_ids + [int(row.id) for row in rows if int(row.id) not in set(ordered_ids)]
    for index, row_id in enumerate(final_ids):
        row = by_id[row_id]
        row.sort_order = index
        row.updated_at = now
    section.updated_at = now
    universe = session.get(ResearchUniverse, int(section.universe_id))
    if universe is not None:
        universe.updated_at = now
    session.flush()
    return [by_id[row_id] for row_id in final_ids]


def delete_universe_row(session: Session, row_id: int) -> ResearchUniverseRow:
    row = session.get(ResearchUniverseRow, int(row_id))
    if row is None or row.archived_at is not None:
        raise ValueError("Universe row not found.")
    row.archived_at = utcnow()
    row.updated_at = row.archived_at
    session.flush()
    return row


def add_universe_row(
    session: Session,
    *,
    universe_id: int,
    section_id: int,
    instrument_payload: dict[str, Any],
    display_label: str | None = None,
    role: str | None = None,
    exposure_type: str | None = None,
    exposure_strength: str | None = None,
    conviction: str | None = None,
    notes: str | None = None,
    source_provider: str | None = None,
    source_external_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> ResearchUniverseRow:
    universe = session.get(ResearchUniverse, int(universe_id))
    section = session.get(ResearchUniverseSection, int(section_id))
    if universe is None or section is None or int(section.universe_id) != int(universe.id):
        raise ValueError("Universe section not found.")

    instrument = ensure_instrument(session, **instrument_payload)
    label = _clean_text(display_label, max_len=255) or instrument.ticker or instrument.yahoo_symbol or instrument.display_name
    role_v = _clean_text(role, max_len=128) or ""
    row = (
        session.query(ResearchUniverseRow)
        .filter(
            ResearchUniverseRow.section_id == int(section.id),
            ResearchUniverseRow.instrument_id == int(instrument.id),
            ResearchUniverseRow.role == role_v,
            ResearchUniverseRow.display_label == label,
            ResearchUniverseRow.archived_at.is_(None),
        )
        .first()
    )
    now = utcnow()
    if row is None:
        sort_order = (
            session.query(func.coalesce(func.max(ResearchUniverseRow.sort_order), -1))
            .filter(ResearchUniverseRow.section_id == int(section.id), ResearchUniverseRow.archived_at.is_(None))
            .scalar()
            or -1
        ) + 1
        row = ResearchUniverseRow(
            universe_id=int(universe.id),
            section_id=int(section.id),
            instrument_id=int(instrument.id),
            display_label=label,
            role=role_v,
            exposure_type=_clean_text(exposure_type, max_len=128),
            exposure_strength=_clean_text(exposure_strength, max_len=32),
            conviction=_clean_text(conviction, max_len=32),
            notes=notes,
            sort_order=int(sort_order),
            source_provider=source_provider,
            source_external_id=source_external_id,
            metadata_json=metadata or {},
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.flush()
    else:
        row.exposure_type = _clean_text(exposure_type, max_len=128) or row.exposure_type
        row.exposure_strength = _clean_text(exposure_strength, max_len=32) or row.exposure_strength
        row.conviction = _clean_text(conviction, max_len=32) or row.conviction
        row.notes = notes if notes is not None else row.notes
        row.source_provider = source_provider if source_provider is not None else row.source_provider
        row.source_external_id = source_external_id if source_external_id is not None else row.source_external_id
        row.metadata_json = dict(row.metadata_json or {}) | dict(metadata or {})
        row.updated_at = now
    try:
        promotion = promote_universe_instrument_dataset_links(session, instrument=instrument)
        if any(int(promotion.get(key, 0) or 0) for key in ("identity_resolved", "links", "views", "datasets_matched")):
            row.metadata_json = dict(row.metadata_json or {}) | {"dataset_link_promotion": promotion}
    except Exception as exc:
        row.metadata_json = dict(row.metadata_json or {}) | {
            "dataset_link_promotion": {
                "error": f"{type(exc).__name__}: {exc}",
                "source": "universe_entry",
            }
        }
    return row


def serialize_instrument(instrument: ResearchInstrument, *, quote: dict[str, Any] | None = None) -> dict[str, Any]:
    sparse_ibkr_contract = _is_sparse_ibkr_contract(instrument)
    return {
        "id": int(instrument.id),
        "instrument_key": instrument.instrument_key,
        "display_name": instrument.display_name,
        "legal_name": instrument.legal_name,
        "ticker": instrument.ticker,
        "exchange": instrument.exchange,
        "currency": instrument.currency,
        "country": instrument.country,
        "security_type": "IBKR_CONTRACT" if sparse_ibkr_contract else instrument.security_type,
        "is_public": False if sparse_ibkr_contract else bool(instrument.is_public),
        "issuer_id": instrument.issuer_id,
        "company_id": instrument.company_id,
        "ibkr_conid": instrument.ibkr_conid,
        "yahoo_symbol": None if sparse_ibkr_contract else instrument.yahoo_symbol,
        "primary_provider": instrument.primary_provider,
        "metadata": instrument.metadata_json or {},
        "quote": quote or {},
    }


def _chunked(values: list[str], size: int) -> list[list[str]]:
    chunk_size = max(1, int(size))
    return [values[idx : idx + chunk_size] for idx in range(0, len(values), chunk_size)]


def _ibkr_snapshot_quote(snapshot: dict[str, Any], instrument: ResearchInstrument) -> dict[str, Any]:
    last = snapshot.get("last")
    if last is None:
        last = snapshot.get("mark")
    return {
        "provider": "ibkr",
        "symbol": snapshot.get("symbol") or instrument.ticker,
        "last": last,
        "change": snapshot.get("change"),
        "change_pct": snapshot.get("change_pct"),
        "currency": instrument.currency,
        "exchange": snapshot.get("exchange") or instrument.exchange,
        "market_time": None,
        "market_data": snapshot.get("availability_label"),
        "availability": snapshot.get("availability"),
        "bid": snapshot.get("bid"),
        "ask": snapshot.get("ask"),
        "volume": snapshot.get("volume"),
        "ibkr_conid": snapshot.get("conid") or instrument.ibkr_conid,
    }


def _merge_quote_payload(base: dict[str, Any] | None, live: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base or {})
    existing_provider = str(merged.get("provider") or "").strip()
    for key in ("symbol", "last", "change", "change_pct", "currency", "exchange", "market_time"):
        if live.get(key) is not None:
            merged[key] = live[key]
    for key in ("market_data", "availability", "bid", "ask", "volume", "ibkr_conid"):
        if live.get(key) is not None:
            merged[key] = live[key]
    merged["provider"] = f"{existing_provider}+ibkr" if existing_provider and existing_provider != "ibkr" else "ibkr"
    return merged


def _ibkr_quote_payloads(instruments: list[ResearchInstrument], *, live: bool = True) -> dict[int, dict[str, Any]]:
    conids_by_instrument = {
        int(inst.id): str(inst.ibkr_conid).strip()
        for inst in instruments
        if inst.ibkr_conid and str(inst.ibkr_conid).strip()
    }
    if not conids_by_instrument:
        return {}

    snapshots_by_conid: dict[str, dict[str, Any]] = {}
    unique_conids = list(dict.fromkeys(conids_by_instrument.values()))
    for chunk in _chunked(unique_conids, 100):
        cache_key = f"ibkr-conids:{','.join(chunk)}"
        payload = _QUOTE_CACHE.get(cache_key)
        if payload is None:
            if not live:
                continue
            try:
                from thoth_terminal.integrations.ibkr.routes import ibkr_service

                service = ibkr_service()
                if not service.config.active:
                    continue
                payload = service.marketdata_snapshot(conids=chunk)
                _QUOTE_CACHE.set(cache_key, payload, ttl_s=15.0)
            except Exception:
                continue
        for item in payload.get("items", []) if isinstance(payload, dict) else []:
            if not isinstance(item, dict):
                continue
            conid = str(item.get("conid") or "").strip()
            if conid:
                snapshots_by_conid[conid] = item

    out: dict[int, dict[str, Any]] = {}
    instruments_by_id = {int(inst.id): inst for inst in instruments}
    for instrument_id, conid in conids_by_instrument.items():
        snapshot = snapshots_by_conid.get(conid)
        instrument = instruments_by_id.get(instrument_id)
        if snapshot is None or instrument is None:
            continue
        out[instrument_id] = _ibkr_snapshot_quote(snapshot, instrument)
    return out


def _bulk_return_payloads(symbols: list[str], *, user_agent: str, live: bool = True) -> dict[str, dict[str, float | None]]:
    if not symbols:
        return {}
    cache_key = f"yahoo-returns:{user_agent}:{','.join(symbols)}"
    cached = _QUOTE_CACHE.get(cache_key)
    if cached is not None:
        return cached  # type: ignore[return-value]
    if not live:
        return {}
    try:
        payload = fetch_bulk_returns(symbols, user_agent=user_agent, timeout_s=6.0)
    except Exception as exc:
        payload = {"_error": {"message": str(exc), "error_type": type(exc).__name__}}
    _QUOTE_CACHE.set(cache_key, payload, ttl_s=45.0 if "_error" in payload else 5.0 * 60.0)
    return payload


def _return_decimal_to_pct(value: float | None) -> float | None:
    return (float(value) * 100.0) if value is not None else None


def _normalize_return_pct(value: float | None) -> float | None:
    if value is None:
        return None
    pct = float(value)
    return pct * 100.0 if abs(pct) <= 3.0 else pct


def _quote_payloads(
    instruments: list[ResearchInstrument],
    *,
    user_agent: str | None = None,
    live: bool = True,
) -> dict[int, dict[str, Any]]:
    symbols = [inst.yahoo_symbol or inst.ticker for inst in instruments if inst.is_public and (inst.yahoo_symbol or inst.ticker)]
    symbols = list(dict.fromkeys([str(sym).strip().upper() for sym in symbols if str(sym).strip()]))
    out: dict[int, dict[str, Any]] = {}
    quote_error: dict[str, Any] | None = None
    if symbols:
        ua = user_agent or settings.discord_market_user_agent
        cache_key = f"yahoo:{ua}:{','.join(symbols)}"
        cached_quotes = _QUOTE_CACHE.get(cache_key)
        returns_cache_key = f"yahoo-returns:{ua}:{','.join(symbols)}"
        if live and cached_quotes is None and _QUOTE_CACHE.get(returns_cache_key) is None:
            try:
                quotes, returns = fetch_quotes_and_returns(
                    symbols, user_agent=ua, timeout_s=6.0, quote_timeout_s=5.0,
                    allow_chart_fallback=False,
                )
                if getattr(returns, "complete_failure", False):
                    errors = getattr(returns, "errors", [])
                    returns["_error"] = {
                        "message": "; ".join(dict.fromkeys(error["message"] for error in errors)),
                        "error_type": errors[0].get("error_type") if errors else "YahooFinanceError",
                    }
                _QUOTE_CACHE.set(returns_cache_key, returns, ttl_s=45.0 if "_error" in returns else 5.0 * 60.0)
                if not quotes and getattr(quotes, "errors", []):
                    quotes["_error"] = {
                        "provider": "yahoo",
                        "message": "; ".join(dict.fromkeys(error["message"] for error in quotes.errors)),
                    }
                _QUOTE_CACHE.set(cache_key, quotes, ttl_s=45.0 if "_error" in quotes else 60.0)
                cached_quotes = quotes
            except Exception as exc:
                # An unexpected provider failure keeps the established quote
                # error/backoff behavior; no additional provider pass is made.
                cached_quotes = {"_error": {"provider": "yahoo", "message": f"{type(exc).__name__}: {exc}"}}
                _QUOTE_CACHE.set(cache_key, cached_quotes, ttl_s=45.0)
        if cached_quotes is not None:
            quotes = cached_quotes
        elif not live:
            quotes = {}
        else:
            try:
                quotes = fetch_quotes(
                    symbols,
                    user_agent=ua,
                    timeout_s=5.0,
                    allow_chart_fallback=False,
                )
                _QUOTE_CACHE.set(cache_key, quotes, ttl_s=60.0)
            except YahooFinanceError as exc:
                quotes = {"_error": {"provider": "yahoo", "message": str(exc)}}
                _QUOTE_CACHE.set(cache_key, quotes, ttl_s=45.0)
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                quotes = {"_error": {"provider": "yahoo", "message": message}}
                _QUOTE_CACHE.set(cache_key, quotes, ttl_s=45.0)
        if "_error" in quotes:
            quote_error = quotes["_error"]
        else:
            return_payloads = _bulk_return_payloads(symbols, user_agent=ua, live=live)
            provider_errors = [
                {**error, "phase": phase}
                for phase, payload in (("quotes", quotes), ("returns", return_payloads))
                for error in getattr(payload, "errors", [])
            ]
            if return_payloads.get("_error") and not getattr(return_payloads, "errors", []):
                provider_errors.append({**return_payloads["_error"], "phase": "returns"})
            if provider_errors:
                quote_error = {
                    "provider": "yahoo",
                    "message": "; ".join(dict.fromkeys(f"Yahoo {error['phase']}: {error['message']}" for error in provider_errors)),
                    "chunk_errors": provider_errors[:5],
                    "chunk_error_count": len(provider_errors),
                    "chunk_errors_truncated": len(provider_errors) > 5,
                }
            for inst in instruments:
                symbol = str(inst.yahoo_symbol or inst.ticker or "").strip().upper()
                quote = quotes.get(symbol)
                if quote is None:
                    continue
                returns = return_payloads.get(symbol) or {}
                out[int(inst.id)] = {
                    "provider": "yahoo",
                    "symbol": quote.symbol,
                    "last": quote.price,
                    "change": getattr(quote, "regular_market_change", None),
                    "change_pct": getattr(quote, "regular_market_change_percent", None),
                    "currency": quote.currency,
                    "exchange": quote.full_exchange_name or quote.exchange,
                    "market_time": quote.market_time_utc.isoformat() if quote.market_time_utc else None,
                    "forward_pe": getattr(quote, "forward_pe", None),
                    "trailing_pe": getattr(quote, "trailing_pe", None),
                    "market_cap": getattr(quote, "market_cap", None),
                    "ytd_return": _normalize_return_pct(quote.ytd_return)
                    if quote.ytd_return is not None
                    else _return_decimal_to_pct(returns.get("ytd")),
                    "one_month_return": _return_decimal_to_pct(returns.get("m1")),
                    "one_year_return": _normalize_return_pct(quote.one_year_return)
                    if quote.one_year_return is not None
                    else _return_decimal_to_pct(returns.get("y1")),
                }

    for instrument_id, quote in _ibkr_quote_payloads(instruments, live=live).items():
        out[instrument_id] = _merge_quote_payload(out.get(instrument_id), quote)
    missing_symbols = sorted({
        str(inst.yahoo_symbol or inst.ticker).strip().upper()
        for inst in instruments
        if inst.is_public and (inst.yahoo_symbol or inst.ticker)
        and out.get(int(inst.id), {}).get("last") is None
    })
    if missing_symbols:
        coverage_message = f"No current price returned for {len(missing_symbols)} of {len(symbols)} public symbols."
        quote_error = {
            **(quote_error or {"provider": "market-data"}),
            "message": f"{quote_error['message']} {coverage_message}" if quote_error else coverage_message,
            "missing_symbol_count": len(missing_symbols),
            "requested_symbol_count": len(symbols),
            "missing_symbols": missing_symbols[:20],
            "missing_symbols_truncated": len(missing_symbols) > 20,
        }
    if quote_error is not None:
        out["_error"] = quote_error  # type: ignore[index]
    return out


def serialize_universe(
    session: Session,
    universe: ResearchUniverse,
    *,
    include_rows: bool = True,
    include_quotes: bool = False,
) -> dict[str, Any]:
    sections = (
        session.query(ResearchUniverseSection)
        .filter(ResearchUniverseSection.universe_id == int(universe.id))
        .order_by(ResearchUniverseSection.sort_order.asc(), ResearchUniverseSection.id.asc())
        .all()
    )
    rows = (
        session.query(ResearchUniverseRow)
        .options(joinedload(ResearchUniverseRow.instrument))
        .filter(ResearchUniverseRow.universe_id == int(universe.id), ResearchUniverseRow.archived_at.is_(None))
        .order_by(ResearchUniverseRow.section_id.asc(), ResearchUniverseRow.sort_order.asc(), ResearchUniverseRow.id.asc())
        .all()
        if include_rows
        else []
    )
    instruments = list({int(row.instrument_id): row.instrument for row in rows if row.instrument is not None}.values())
    quotes = _quote_payloads(instruments) if include_quotes else {}
    quote_error = quotes.pop("_error", None) if isinstance(quotes, dict) else None
    rows_by_section: dict[int, list[dict[str, Any]]] = {int(section.id): [] for section in sections}
    for row in rows:
        quote = quotes.get(int(row.instrument_id), {}) if isinstance(quotes, dict) else {}
        rows_by_section.setdefault(int(row.section_id), []).append(
            {
                "id": int(row.id),
                "display_label": row.display_label,
                "role": row.role,
                "exposure_type": row.exposure_type,
                "exposure_strength": row.exposure_strength,
                "conviction": row.conviction,
                "notes": row.notes,
                "sort_order": row.sort_order,
                "source_provider": row.source_provider,
                "source_external_id": row.source_external_id,
                "metadata": row.metadata_json or {},
                "instrument": serialize_instrument(row.instrument, quote=quote) if row.instrument else None,
            }
        )
    section_payloads = []
    for section in sections:
        section_payloads.append(
            {
                "id": int(section.id),
                "section_key": section.section_key,
                "name": section.name,
                "description": section.description,
                "sort_order": section.sort_order,
                "source_provider": section.source_provider,
                "source_external_id": section.source_external_id,
                "metadata": section.metadata_json or {},
                "rows": rows_by_section.get(int(section.id), []),
            }
        )
    row_count = sum(len(section["rows"]) for section in section_payloads)
    if not include_rows:
        row_count = int(
            session.query(func.count(ResearchUniverseRow.id))
            .filter(ResearchUniverseRow.universe_id == int(universe.id), ResearchUniverseRow.archived_at.is_(None))
            .scalar()
            or 0
        )
    return {
        "id": int(universe.id),
        "slug": universe.slug,
        "name": universe.name,
        "description": universe.description,
        "status": universe.status,
        "sort_order": universe.sort_order,
        "metadata": universe.metadata_json or {},
        "sections": section_payloads,
        "row_count": row_count,
        "quote_error": quote_error,
    }


def list_universes(session: Session) -> list[dict[str, Any]]:
    universes = (
        session.query(ResearchUniverse)
        .filter(ResearchUniverse.status == "active")
        .order_by(ResearchUniverse.sort_order.asc(), ResearchUniverse.name.asc())
        .all()
    )
    if not universes:
        return []
    universe_ids = [int(row.id) for row in universes]
    sections_by_universe: dict[int, list[dict[str, Any]]] = {universe_id: [] for universe_id in universe_ids}
    for section in (
        session.query(ResearchUniverseSection)
        .filter(ResearchUniverseSection.universe_id.in_(universe_ids))
        .order_by(
            ResearchUniverseSection.universe_id.asc(),
            ResearchUniverseSection.sort_order.asc(),
            ResearchUniverseSection.id.asc(),
        )
        .all()
    ):
        sections_by_universe.setdefault(int(section.universe_id), []).append(
            {
                "id": int(section.id),
                "section_key": section.section_key,
                "name": section.name,
                "description": section.description,
                "sort_order": section.sort_order,
                "source_provider": section.source_provider,
                "source_external_id": section.source_external_id,
                "metadata": section.metadata_json or {},
                "rows": [],
            }
        )
    row_counts = {
        int(universe_id): int(count or 0)
        for universe_id, count in (
            session.query(ResearchUniverseRow.universe_id, func.count(ResearchUniverseRow.id))
            .filter(ResearchUniverseRow.universe_id.in_(universe_ids), ResearchUniverseRow.archived_at.is_(None))
            .group_by(ResearchUniverseRow.universe_id)
            .all()
        )
    }
    return [
        {
            "id": int(row.id),
            "slug": row.slug,
            "name": row.name,
            "description": row.description,
            "status": row.status,
            "sort_order": row.sort_order,
            "metadata": row.metadata_json or {},
            "sections": sections_by_universe.get(int(row.id), []),
            "row_count": row_counts.get(int(row.id), 0),
            "quote_error": None,
        }
        for row in universes
    ]


def search_instruments(
    session: Session,
    query: str,
    *,
    include_yahoo: bool = True,
    ibkr_results: list[dict[str, Any]] | None = None,
    limit: int = 12,
) -> list[dict[str, Any]]:
    q = str(query or "").strip()
    if not q:
        return []
    limit = max(1, min(int(limit or 12), 30))
    q_like = f"%{q}%"
    q_upper = q.upper()
    seen: set[str] = set()
    out: list[InstrumentCandidate] = []

    local_rows = (
        session.query(ResearchInstrument)
        .filter(
            or_(
                ResearchInstrument.display_name.ilike(q_like),
                ResearchInstrument.ticker.ilike(q_like),
                ResearchInstrument.yahoo_symbol.ilike(q_like),
                ResearchInstrument.ibkr_conid.ilike(q_like),
            )
        )
        .order_by(ResearchInstrument.is_public.desc(), ResearchInstrument.display_name.asc())
        .limit(limit)
        .all()
    )
    for inst in local_rows:
        if _should_hide_sparse_contract_for_query(inst, q):
            continue
        sparse_ibkr_contract = _is_sparse_ibkr_contract(inst)
        key = f"instrument:{int(inst.id)}"
        seen.add(key)
        out.append(
            InstrumentCandidate(
                source="local",
                instrument_id=int(inst.id),
                display_name=inst.display_name,
                ticker=inst.ticker,
                exchange=inst.exchange,
                currency=inst.currency,
                security_type="IBKR_CONTRACT" if sparse_ibkr_contract else inst.security_type,
                is_public=False if sparse_ibkr_contract else bool(inst.is_public),
                issuer_id=inst.issuer_id,
                company_id=inst.company_id,
                ibkr_conid=inst.ibkr_conid,
                yahoo_symbol=None if sparse_ibkr_contract else inst.yahoo_symbol,
                reason="saved locally",
                metadata=inst.metadata_json or {},
            )
        )

    issuer_rows = (
        session.query(Issuer)
        .outerjoin(IssuerIdentifier, IssuerIdentifier.issuer_id == Issuer.id)
        .filter(
            or_(
                Issuer.display_name.ilike(q_like),
                Issuer.legal_name.ilike(q_like),
                func.upper(IssuerIdentifier.id_value) == q_upper,
                IssuerIdentifier.id_value.ilike(q_like),
            )
        )
        .order_by(Issuer.display_name.asc())
        .limit(limit)
        .all()
    )
    for issuer in issuer_rows:
        primary = (
            session.query(IssuerIdentifier)
            .filter(IssuerIdentifier.issuer_id == int(issuer.id), IssuerIdentifier.id_type == "TICKER")
            .order_by(IssuerIdentifier.is_primary.desc(), IssuerIdentifier.id.asc())
            .first()
        )
        ticker = primary.id_value.upper() if primary is not None and primary.id_value else None
        exchange = primary.exchange if primary else None
        if _should_hide_sparse_issuer_for_query(issuer, q, ticker=ticker):
            continue
        key = f"issuer:{int(issuer.id)}:{ticker or issuer.slug}"
        if key in seen:
            continue
        seen.add(key)
        out.append(
            InstrumentCandidate(
                source="issuer",
                display_name=issuer.display_name,
                ticker=ticker,
                exchange=exchange,
                currency=primary.currency if primary else None,
                security_type="STK" if ticker else "ENTITY",
                is_public=bool(ticker),
                issuer_id=int(issuer.id),
                yahoo_symbol=_issuer_yahoo_symbol(ticker=ticker, exchange=exchange),
                reason="issuer registry",
            )
        )

    for raw in ibkr_results or []:
        if not isinstance(raw, dict):
            continue
        conid, conid_exchange = _ibkr_conid_and_exchange(raw)
        ticker = _ibkr_ticker(raw)
        security_type = _ibkr_security_type(raw, ticker=ticker)
        exchange = _ibkr_exchange(raw, fallback=conid_exchange)
        name = _clean_text(
            raw.get("companyName")
            or raw.get("companyHeader")
            or raw.get("description")
            or raw.get("name")
            or raw.get("fullName")
            or ticker,
            max_len=255,
        )
        if not name and conid:
            name = f"IBKR conid {conid}"
        if not name and not ticker:
            continue
        key = f"ibkr:{conid or ticker}"
        if key in seen:
            continue
        seen.add(key)
        out.append(
            InstrumentCandidate(
                source="ibkr",
                display_name=name or ticker or "IBKR instrument",
                ticker=ticker,
                exchange=exchange,
                currency=_clean_text(raw.get("currency"), max_len=16),
                security_type=security_type,
                is_public=_ibkr_is_quoteable_equity(security_type=security_type, ticker=ticker),
                ibkr_conid=conid,
                yahoo_symbol=_ibkr_yahoo_symbol(ticker=ticker, exchange=exchange, security_type=security_type),
                reason="IBKR contract search",
                metadata={"ibkr": raw},
            )
        )

    if include_yahoo and len(out) < limit:
        try:
            yahoo_rows = search_symbols(q, user_agent=settings.discord_market_user_agent, timeout_s=8.0, limit=limit)
        except Exception:
            yahoo_rows = []
        for item in yahoo_rows:
            key = f"yahoo:{item.symbol.upper()}"
            if key in seen:
                continue
            seen.add(key)
            out.append(
                InstrumentCandidate(
                    source="yahoo",
                    display_name=item.name or item.symbol,
                    ticker=item.symbol,
                    exchange=item.exchange,
                    security_type="STK",
                    is_public=True,
                    yahoo_symbol=item.symbol,
                    reason="Yahoo Finance search",
                )
            )
            if len(out) >= limit:
                break

    if len(out) < limit:
        key = f"private:{_slugify(q, default='candidate')}"
        if key not in seen:
            out.append(
                InstrumentCandidate(
                    source="manual_private",
                    display_name=q,
                    security_type="PRIVATE",
                    is_public=False,
                    reason="create private-company instrument",
                    metadata={"manual_candidate": True},
                )
            )

    visible = out[:limit]
    first_ibkr = next((candidate for candidate in out if candidate.source == "ibkr"), None)
    if first_ibkr is not None and all(candidate.source != "ibkr" for candidate in visible):
        replacement_idx = None
        for idx, candidate in enumerate(visible):
            if candidate.source in {"manual_private", "yahoo"}:
                replacement_idx = idx
                break
        if replacement_idx is None and visible:
            replacement_idx = len(visible) - 1
        if replacement_idx is None:
            visible = [first_ibkr]
        else:
            visible = [*visible]
            visible[replacement_idx] = first_ibkr
    exact_ibkr = next(
        (
            candidate
            for candidate in out
            if candidate.source == "ibkr" and str(candidate.ticker or "").strip().upper() == q_upper
        ),
        None,
    )
    if exact_ibkr is not None:
        exact_local = any(
            candidate.source == "local" and str(candidate.ticker or candidate.yahoo_symbol or "").strip().upper() == q_upper
            for candidate in visible
        )
        insert_idx = 1 if exact_local else 0
        promoted = [candidate for candidate in visible if candidate is not exact_ibkr]
        promoted.insert(min(insert_idx, len(promoted)), exact_ibkr)
        visible = promoted[:limit]

    return [candidate.__dict__ for candidate in visible[:limit]]


def label_suggestions(session: Session, *, kind: str = "exposure_type", query: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 30), 100))
    q = str(query or "").strip()
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    if kind == "role":
        values = (
            session.query(ResearchUniverseRow.role, func.count(ResearchUniverseRow.id))
            .filter(ResearchUniverseRow.role != "", ResearchUniverseRow.archived_at.is_(None))
            .group_by(ResearchUniverseRow.role)
            .order_by(func.count(ResearchUniverseRow.id).desc(), ResearchUniverseRow.role.asc())
            .limit(limit)
            .all()
        )
    else:
        values = (
            session.query(ResearchUniverseRow.exposure_type, func.count(ResearchUniverseRow.id))
            .filter(ResearchUniverseRow.exposure_type.isnot(None), ResearchUniverseRow.archived_at.is_(None))
            .group_by(ResearchUniverseRow.exposure_type)
            .order_by(func.count(ResearchUniverseRow.id).desc(), ResearchUniverseRow.exposure_type.asc())
            .limit(limit)
            .all()
        )
    for value, count in values:
        label = str(value or "").strip()
        if not label or (q and q.lower() not in label.lower()):
            continue
        seen.add(label.lower())
        rows.append({"label": label, "source": "universe_rows", "count": int(count or 0)})

    if len(rows) < limit:
        entity_query = session.query(Entity).filter(Entity.kind.in_(sorted(_THEME_ENTITY_KINDS)))
        if q:
            entity_query = entity_query.filter(Entity.canonical_name.ilike(f"%{q}%"))
        for entity in entity_query.order_by(Entity.canonical_name.asc()).limit(limit).all():
            label = str(entity.canonical_name or "").strip()
            if not label or label.lower() in seen:
                continue
            rows.append({"label": label, "source": f"entity:{entity.kind}", "count": 0})
            if len(rows) >= limit:
                break
    return rows[:limit]


def instrument_detail(session: Session, instrument_id: int, *, live_quotes: bool = True) -> dict[str, Any] | None:
    instrument = (
        session.query(ResearchInstrument)
        .options(*_instrument_detail_load_options())
        .filter(ResearchInstrument.id == int(instrument_id))
        .first()
    )
    if instrument is None:
        return None
    quote = _quote_payloads([instrument], live=live_quotes).get(int(instrument.id), {})
    related = []
    if instrument.issuer_id is not None:
        related = [
            serialize_instrument(row)
            for row in (
                session.query(ResearchInstrument)
                .options(load_only(*_INSTRUMENT_DETAIL_COLUMNS))
                .filter(ResearchInstrument.issuer_id == int(instrument.issuer_id), ResearchInstrument.id != int(instrument.id))
                .order_by(ResearchInstrument.display_name.asc())
                .limit(12)
                .all()
            )
        ]
    group_key = None
    if isinstance(instrument.metadata_json, dict):
        group_key = str(instrument.metadata_json.get("security_group") or "").strip()
    if group_key:
        remaining = max(0, 12 - len(related))
        if remaining > 0:
            for row in (
                session.query(ResearchInstrument)
                .options(load_only(*_INSTRUMENT_DETAIL_COLUMNS))
                .filter(ResearchInstrument.id != int(instrument.id))
                .filter(_instrument_metadata_text(session, "security_group") == group_key)
                .order_by(ResearchInstrument.display_name.asc())
                .limit(remaining)
                .all()
            ):
                payload = serialize_instrument(row)
                if not any(existing.get("id") == payload["id"] for existing in related):
                    related.append(payload)
                    if len(related) >= 12:
                        break
    upcoming = []
    if instrument.issuer_id is not None:
        today = utcnow().date()
        upcoming = [
            {
                "scheduled_date": row.scheduled_date.isoformat() if row.scheduled_date else None,
                "period": (
                    f"FY{row.fiscal_year} Q{row.fiscal_quarter}"
                    if row.fiscal_quarter is not None
                    else f"FY{row.fiscal_year}"
                ),
                "date_provenance": row.date_provenance,
            }
            for row in (
                session.query(EarningsEvent)
                .options(
                    load_only(
                        EarningsEvent.id,
                        EarningsEvent.issuer_id,
                        EarningsEvent.fiscal_year,
                        EarningsEvent.fiscal_quarter,
                        EarningsEvent.scheduled_date,
                        EarningsEvent.date_provenance,
                    )
                )
                .filter(EarningsEvent.issuer_id == int(instrument.issuer_id), EarningsEvent.scheduled_date >= today)
                .order_by(EarningsEvent.scheduled_date.asc())
                .limit(4)
                .all()
            )
        ]
    appearances = [
        {
            "universe_id": int(row.universe_id),
            "section_id": int(row.section_id),
            "row_id": int(row.id),
            "display_label": row.display_label,
            "role": row.role,
            "exposure_type": row.exposure_type,
        }
        for row in (
            session.query(ResearchUniverseRow)
            .options(
                load_only(
                    ResearchUniverseRow.id,
                    ResearchUniverseRow.universe_id,
                    ResearchUniverseRow.section_id,
                    ResearchUniverseRow.display_label,
                    ResearchUniverseRow.role,
                    ResearchUniverseRow.exposure_type,
                )
            )
            .filter(ResearchUniverseRow.instrument_id == int(instrument.id), ResearchUniverseRow.archived_at.is_(None))
            .order_by(ResearchUniverseRow.universe_id.asc(), ResearchUniverseRow.section_id.asc())
            .all()
        )
    ]
    decision_payload: dict[str, Any] = {}
    if instrument.company is not None or instrument.issuer is not None:
        try:
            decision_payload = cached_company_decision_page_payload(
                session,
                company=instrument.company,
                issuer=instrument.issuer,
                include_earnings_packets=False,
            )
        except Exception:
            decision_payload = {}
    return {
        "instrument": serialize_instrument(instrument, quote=quote),
        "related_listings": related,
        "upcoming_earnings": upcoming,
        "appearances": appearances,
        "decision": decision_payload,
    }


def _looks_like_separator(item: dict[str, Any]) -> bool:
    conid = item.get("conid") or item.get("conId") or item.get("C")
    ticker = item.get("ticker") or item.get("symbol") or item.get("fullName")
    asset = item.get("assetClass") or item.get("ST")
    if "H" in item and not conid and not ticker and not asset:
        return True
    name = str(item.get("name") or item.get("fullName") or "").strip()
    if conid or ticker or asset:
        return False
    if not name:
        return False
    return True


def import_ibkr_watchlist_payload(
    session: Session,
    *,
    watchlist_payload: dict[str, Any],
    universe_name: str | None = None,
    contract_resolver: ContractResolver | None = None,
) -> dict[str, Any]:
    with universe_population_directory(session):
        watchlist_id = str(watchlist_payload.get("id") or watchlist_payload.get("watchlistId") or "").strip()
        name = _clean_text(universe_name or watchlist_payload.get("name"), max_len=255) or "IBKR Watchlist Import"
        universe = ensure_universe(
            session,
            name=name,
            slug=f"ibkr-{watchlist_id}" if watchlist_id else name,
            description="One-way read-only import from an IBKR watchlist.",
            metadata={"source_provider": "ibkr", "source_watchlist_id": watchlist_id},
        )
        instruments = watchlist_payload.get("instruments") or []
        if not isinstance(instruments, list):
            instruments = []
        current_section = ensure_section(
            session,
            universe,
            name="Imported",
            sort_order=0,
            source_provider="ibkr",
            source_external_id=watchlist_id,
        )
        imported_rows = 0
        section_count = 1
        for item in instruments:
            if not isinstance(item, dict):
                continue
            if _looks_like_separator(item):
                section_name = str(item.get("name") or item.get("fullName") or f"Section {section_count + 1}").strip(" -_=")
                current_section = ensure_section(
                    session,
                    universe,
                    name=section_name or f"Section {section_count + 1}",
                    sort_order=section_count,
                    source_provider="ibkr",
                    source_external_id=watchlist_id,
                )
                section_count += 1
                continue
            conid, conid_exchange = _ibkr_conid_and_exchange(item)
            contract_info = _resolve_ibkr_contract(conid, contract_resolver=contract_resolver)
            item = _merge_ibkr_contract_info(item, contract_info)
            conid, conid_exchange = _ibkr_conid_and_exchange(item)
            ticker = _ibkr_ticker(item)
            security_type = _ibkr_security_type(item, ticker=ticker)
            exchange = _clean_text(item.get("exchange") or item.get("listingExchange"), max_len=64) or conid_exchange
            yahoo_symbol = _ibkr_yahoo_symbol(ticker=ticker, exchange=exchange, security_type=security_type)
            display_name = _clean_text(item.get("name") or item.get("fullName") or ticker, max_len=255)
            if not display_name and conid:
                display_name = f"IBKR conid {conid}"
            if not display_name and not ticker:
                continue
            add_universe_row(
                session,
                universe_id=int(universe.id),
                section_id=int(current_section.id),
                instrument_payload={
                    "display_name": display_name or ticker or "IBKR instrument",
                    "ticker": ticker,
                    "exchange": exchange,
                    "currency": _clean_text(item.get("currency"), max_len=16),
                    "country": _clean_text(item.get("country"), max_len=64),
                    "security_type": security_type,
                    "is_public": _ibkr_is_quoteable_equity(security_type=security_type, ticker=ticker),
                    "ibkr_conid": conid,
                    "yahoo_symbol": yahoo_symbol,
                    "primary_provider": "ibkr",
                    "metadata": {"ibkr_watchlist_item": item, "ibkr_contract_info": contract_info},
                },
                display_label=ticker or display_name,
                source_provider="ibkr",
                source_external_id=watchlist_id,
                metadata={"imported_at": utcnow().isoformat()},
            )
            imported_rows += 1
        return {
            "ok": True,
            "universe": serialize_universe(session, universe, include_rows=True, include_quotes=False),
            "imported_rows": imported_rows,
        }
