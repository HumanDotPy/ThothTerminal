from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from thoth_terminal.db import utcnow
from thoth_terminal.localized_names import resolve_taiwan_company_name
from thoth_terminal.models import (
    Company,
    Issuer,
    IssuerIdentifier,
    ResearchInstrument,
    ResearchInstrumentIdentifier,
    ResearchSubject,
    ResearchSubjectAlias,
)


_SOURCE_MARKET_EXCHANGES = {
    "listed": "TWSE",
    "public": "TPEx",
    "twse": "TWSE",
    "tpex": "TPEx",
    "jpx": "JPX",
    "tse": "TSE",
    "krx": "KRX",
    "kospi": "KRX",
    "kosdaq": "KRX",
}
_JURISDICTION_REGION = {
    "TW": "Taiwan",
    "KR": "South Korea",
    "JP": "Japan",
}
_GENERIC_ID_TYPES = {
    "lei": "LEI",
    "cik": "CIK",
    "isin": "ISIN",
    "cusip": "CUSIP",
    "figi": "FIGI",
    "sedol": "SEDOL",
}
_CONNECTOR_JURISDICTIONS = {
    "taiwan_mops_monthly_revenue": "TW",
    "taiwan_mops_investor_conferences": "TW",
    "tpex_infohub_company": "TW",
    "twse_openapi": "TW",
    "opendart_disclosures": "KR",
    "opendart_financials": "KR",
    "krx_kind_disclosure": "KR",
    "edinet_disclosures": "JP",
    "edinetdb_financials": "JP",
    "tdnet_company_announcements": "JP",
}


@dataclass(frozen=True)
class IdentifierCandidate:
    id_type: str
    id_value: str
    exchange: str | None = None
    source_key: str | None = None
    is_primary: bool = False


@dataclass(frozen=True)
class IdentityResolution:
    issuer_id: int | None
    company_id: int | None
    security_id: int | None
    display_name: str
    legal_name: str | None
    jurisdiction: str | None
    source: str | None
    market: str | None
    confidence: float
    match_basis: str
    aliases: list[str]
    source_identifiers: list[dict[str, Any]]
    local_company_name: str | None = None
    official_english_name: str | None = None

    def as_metadata(self) -> dict[str, Any]:
        return {
            "issuer_id": self.issuer_id,
            "company_id": self.company_id,
            "security_id": self.security_id,
            "display_name": self.display_name,
            "legal_name": self.legal_name,
            "jurisdiction": self.jurisdiction,
            "source": self.source,
            "market": self.market,
            "confidence": self.confidence,
            "match_basis": self.match_basis,
            "aliases": list(self.aliases),
            "source_identifiers": [dict(row) for row in self.source_identifiers],
            "local_company_name": self.local_company_name,
            "official_english_name": self.official_english_name,
        }


def _clean(value: object) -> str:
    return str(value or "").strip()


def _dedupe(values: list[object]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _clean(value)
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _slugify(value: object, *, default: str = "issuer") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", _clean(value).lower()).strip("-")
    return slug[:120] or default


def _unique_slug(session: Session, model: type[Any], base: str, *, row_id: int | None = None) -> str:
    candidate = _slugify(base, default="issuer")
    idx = 2
    while True:
        row = session.query(model).filter_by(slug=candidate).first()
        if row is None or (row_id is not None and int(row.id) == int(row_id)):
            return candidate
        candidate = f"{_slugify(base, default='issuer')}-{idx}"
        idx += 1


def _normalize_jurisdiction(value: object, source: object = None) -> str | None:
    raw = _clean(value).upper()
    if raw in {"TW", "TWN", "TAIWAN"}:
        return "TW"
    if raw in {"KR", "KOR", "KOREA", "SOUTH KOREA"}:
        return "KR"
    if raw in {"JP", "JPN", "JAPAN"}:
        return "JP"
    return _CONNECTOR_JURISDICTIONS.get(_clean(source).lower()) or (raw[:2] if raw else None)


def _normalize_market(value: object) -> str | None:
    raw = _clean(value)
    if not raw:
        return None
    return _SOURCE_MARKET_EXCHANGES.get(raw.casefold()) or raw[:64]


def _identifier_value(id_type: str, value: object) -> str:
    raw = _clean(value)
    if not raw:
        return ""
    if id_type in {"LEI", "CIK", "ISIN", "CUSIP", "FIGI", "SEDOL"}:
        return raw.upper()
    return raw.upper() if any(ch.isalpha() for ch in raw) else raw


def _append_identifier(
    out: list[IdentifierCandidate],
    *,
    id_type: str,
    value: object,
    exchange: str | None = None,
    source_key: str | None = None,
    primary: bool = False,
) -> None:
    id_type = _clean(id_type).upper()[:16]
    id_value = _identifier_value(id_type, value)
    if not id_type or not id_value:
        return
    pair = (id_type, id_value)
    if any((row.id_type, row.id_value) == pair for row in out):
        return
    out.append(
        IdentifierCandidate(
            id_type=id_type,
            id_value=id_value[:64],
            exchange=_normalize_market(exchange),
            source_key=source_key,
            is_primary=primary,
        )
    )


def _raw_identifier_values(source_identifiers: dict[str, Any], *keys: str) -> list[str]:
    out: list[str] = []
    for key in keys:
        value = source_identifiers.get(key)
        if isinstance(value, (list, tuple, set)):
            out.extend(_clean(item) for item in value if _clean(item))
        elif _clean(value):
            out.append(_clean(value))
    return out


def normalize_source_identifiers(
    *,
    jurisdiction: object = None,
    source: object = None,
    market: object = None,
    source_identifiers: dict[str, Any] | list[Any] | None = None,
) -> list[IdentifierCandidate]:
    source_name = _clean(source).lower()
    jurisdiction_code = _normalize_jurisdiction(jurisdiction, source_name)
    exchange = _normalize_market(market)
    out: list[IdentifierCandidate] = []

    if isinstance(source_identifiers, list):
        for row in source_identifiers:
            if not isinstance(row, dict):
                continue
            _append_identifier(
                out,
                id_type=row.get("id_type") or row.get("type"),
                value=row.get("id_value") or row.get("value"),
                exchange=row.get("exchange") or row.get("market") or exchange,
                source_key=row.get("source_key") or row.get("key"),
                primary=bool(row.get("is_primary") or row.get("primary")),
            )
        raw_map: dict[str, Any] = {}
    else:
        raw_map = dict(source_identifiers or {})

    for key, id_type in _GENERIC_ID_TYPES.items():
        for value in _raw_identifier_values(raw_map, key, key.upper()):
            _append_identifier(out, id_type=id_type, value=value, exchange=exchange, source_key=key)

    if jurisdiction_code == "TW" or source_name in {"taiwan_mops_monthly_revenue", "taiwan_mops_investor_conferences", "tpex_infohub_company", "twse_openapi"}:
        for value in _raw_identifier_values(raw_map, "company_code", "stock_code", "code", "公司代號"):
            _append_identifier(out, id_type="TW_STOCK", value=value, exchange=exchange or "TWSE", source_key="company_code", primary=True)

    if jurisdiction_code == "KR" or source_name in {"opendart_disclosures", "opendart_financials", "krx_kind_disclosure"}:
        for value in _raw_identifier_values(raw_map, "corp_code", "corpCode"):
            _append_identifier(out, id_type="DART_CORP", value=value, exchange=exchange, source_key="corp_code", primary=True)
        for value in _raw_identifier_values(raw_map, "stock_code", "stockCode"):
            _append_identifier(out, id_type="KR_STOCK", value=value, exchange=exchange or "KRX", source_key="stock_code")
        for value in _raw_identifier_values(raw_map, "company_id", "krx_company_id"):
            _append_identifier(out, id_type="KRX_KIND", value=value, exchange=exchange or "KRX", source_key="company_id")

    if jurisdiction_code == "JP" or source_name in {"edinet_disclosures", "edinetdb_financials", "tdnet_company_announcements"}:
        for value in _raw_identifier_values(raw_map, "edinet_code", "edinetCode"):
            _append_identifier(out, id_type="EDINET", value=value, exchange=exchange, source_key="edinet_code", primary=True)
        for value in _raw_identifier_values(raw_map, "sec_code", "secCode", "stock_code", "stockCode"):
            _append_identifier(out, id_type="JP_STOCK", value=value, exchange=exchange or "JPX", source_key="sec_code")
        for value in _raw_identifier_values(raw_map, "jcn", "JCN"):
            _append_identifier(out, id_type="JCN", value=value, exchange=exchange, source_key="jcn")

    if jurisdiction_code == "CN" or source_name in {"cninfo_announcements"}:
        for value in _raw_identifier_values(raw_map, "stock_code", "stockCode", "sec_code", "secCode"):
            _append_identifier(out, id_type="CN_STOCK", value=value, exchange=exchange or "CNINFO", source_key="stock_code", primary=True)
        for value in _raw_identifier_values(raw_map, "org_id", "orgId"):
            _append_identifier(out, id_type="CNINFO_ORG", value=value, exchange=exchange or "CNINFO", source_key="org_id")

    return out


def _identifier_metadata(identifiers: list[IdentifierCandidate], *, source: str | None, market: str | None) -> list[dict[str, Any]]:
    return [
        {
            "id_type": row.id_type,
            "id_value": row.id_value,
            "exchange": row.exchange,
            "source_key": row.source_key,
            "source": source,
            "market": market,
            "is_primary": row.is_primary,
        }
        for row in identifiers
    ]


def _find_issuer_by_identifiers(session: Session, identifiers: list[IdentifierCandidate]) -> tuple[Issuer | None, str | None]:
    for candidate in identifiers:
        if candidate.id_type == "LEI":
            row = (
                session.query(Issuer)
                .filter(func.upper(func.coalesce(Issuer.lei, "")) == candidate.id_value.upper())
                .order_by(Issuer.id.asc())
                .first()
            )
            if row is not None:
                return row, "identifier:LEI"
        ident = (
            session.query(IssuerIdentifier)
            .filter(
                IssuerIdentifier.id_type == candidate.id_type,
                func.upper(IssuerIdentifier.id_value) == candidate.id_value.upper(),
            )
            .order_by(IssuerIdentifier.is_primary.desc(), IssuerIdentifier.id.asc())
            .first()
        )
        if ident is not None:
            row = session.query(Issuer).filter_by(id=int(ident.issuer_id)).first()
            if row is not None:
                return row, f"identifier:{candidate.id_type}"
    return None, None


def _company_for_issuer(session: Session, issuer: Issuer | None) -> Company | None:
    if issuer is None:
        return None
    return session.query(Company).filter_by(issuer_id=int(issuer.id)).order_by(Company.id.asc()).first()


def _company_context_matches(company: Company, *, jurisdiction: str | None, market: str | None) -> bool:
    exchange = _normalize_market(getattr(company, "exchange", None))
    region = _clean(getattr(company, "region", None)).casefold()
    market_norm = _normalize_market(market)
    if market_norm and exchange and exchange.casefold() != market_norm.casefold():
        return False
    region_label = _JURISDICTION_REGION.get(jurisdiction or "", jurisdiction or "")
    if region_label and region and region_label.casefold() not in region and jurisdiction.casefold() not in region:
        return False
    return True


def _find_exact_alias(session: Session, aliases: list[str], *, jurisdiction: str | None, market: str | None) -> tuple[Issuer | None, Company | None]:
    for alias in aliases:
        raw = _clean(alias)
        if not raw:
            continue
        company = (
            session.query(Company)
            .filter(
                or_(
                    func.lower(Company.name) == raw.lower(),
                    func.lower(Company.slug) == raw.lower(),
                    func.upper(func.coalesce(Company.ticker, "")) == raw.upper(),
                )
            )
            .order_by(Company.id.asc())
            .first()
        )
        if company is not None:
            ticker_match = _clean(company.ticker).upper() == raw.upper()
            if ticker_match and not _company_context_matches(company, jurisdiction=jurisdiction, market=market):
                continue
            issuer = session.query(Issuer).filter_by(id=int(company.issuer_id)).first() if company.issuer_id is not None else None
            return issuer, company
        issuer = (
            session.query(Issuer)
            .filter(
                or_(
                    func.lower(Issuer.display_name) == raw.lower(),
                    func.lower(Issuer.legal_name) == raw.lower(),
                    func.lower(Issuer.slug) == raw.lower(),
                )
            )
            .order_by(Issuer.id.asc())
            .first()
        )
        if issuer is not None:
            return issuer, _company_for_issuer(session, issuer)
        subject_alias = (
            session.query(ResearchSubjectAlias)
            .filter(func.lower(ResearchSubjectAlias.alias) == raw.lower())
            .order_by(ResearchSubjectAlias.id.asc())
            .first()
        )
        if subject_alias is not None:
            subject = session.query(ResearchSubject).filter_by(id=int(subject_alias.subject_id)).first()
            if subject is not None:
                issuer = session.query(Issuer).filter_by(id=int(subject.issuer_id)).first() if subject.issuer_id is not None else None
                company = session.query(Company).filter_by(id=int(subject.company_id)).first() if subject.company_id is not None else None
                return issuer, company
    return None, None


def _merge_extra_list(extra: dict[str, Any], key: str, values: list[Any], *, max_items: int = 120) -> None:
    existing = extra.get(key)
    rows = list(existing) if isinstance(existing, list) else []
    seen = {repr(row) for row in rows}
    for value in values:
        marker = repr(value)
        if marker in seen:
            continue
        seen.add(marker)
        rows.append(value)
    extra[key] = rows[-max_items:]


def _ensure_issuer_identifier(
    session: Session,
    *,
    issuer: Issuer,
    candidate: IdentifierCandidate,
    now: Any,
) -> None:
    if candidate.id_type == "LEI":
        issuer.lei = issuer.lei or candidate.id_value[:20]
    row = (
        session.query(IssuerIdentifier)
        .filter(IssuerIdentifier.id_type == candidate.id_type, func.upper(IssuerIdentifier.id_value) == candidate.id_value.upper())
        .order_by(IssuerIdentifier.id.asc())
        .first()
    )
    if row is None:
        session.add(
            IssuerIdentifier(
                issuer_id=int(issuer.id),
                id_type=candidate.id_type,
                id_value=candidate.id_value,
                exchange=(candidate.exchange or "")[:16] or None,
                currency=None,
                is_primary=bool(candidate.is_primary),
                valid_from=None,
                valid_to=None,
                created_at=now,
            )
        )
        return
    if int(row.issuer_id) == int(issuer.id):
        row.exchange = (candidate.exchange or row.exchange or "")[:16] or None
        row.is_primary = bool(row.is_primary or candidate.is_primary)


def _ensure_company(
    session: Session,
    *,
    issuer: Issuer,
    company: Company | None,
    display_name: str,
    jurisdiction: str | None,
    market: str | None,
    industry: str | None,
    security_code: str | None,
    now: Any,
) -> Company:
    if company is None:
        company = _company_for_issuer(session, issuer)
    if company is None:
        slug_base = f"{jurisdiction or 'issuer'}-{security_code or ''}-{display_name or int(issuer.id)}"
        company = Company(
            slug=_unique_slug(session, Company, slug_base),
            name=display_name[:255] or f"Issuer {int(issuer.id)}",
            ticker=(security_code or "")[:32] or None,
            exchange=(market or "")[:64] or None,
            region=_JURISDICTION_REGION.get(jurisdiction or "", jurisdiction),
            sector=None,
            industry=(industry or issuer.industry or "")[:128] or None,
            supply_chain_stage=None,
            ir_url=None,
            filings_url=None,
            reports_url=None,
            calendar_url=None,
            created_at=now,
            issuer_id=int(issuer.id),
        )
        session.add(company)
        session.flush()
        return company
    company.issuer_id = int(issuer.id)
    if display_name and not _clean(company.name):
        company.name = display_name[:255]
    if security_code and not _clean(company.ticker):
        company.ticker = security_code[:32]
    if market and not _clean(company.exchange):
        company.exchange = market[:64]
    if jurisdiction and not _clean(company.region):
        company.region = (_JURISDICTION_REGION.get(jurisdiction, jurisdiction) or "")[:64]
    if industry and not _clean(company.industry):
        company.industry = industry[:128]
    session.flush()
    return company


def _security_candidate(identifiers: list[IdentifierCandidate]) -> IdentifierCandidate | None:
    for id_type in ("TW_STOCK", "KR_STOCK", "JP_STOCK"):
        for candidate in identifiers:
            if candidate.id_type == id_type:
                return candidate
    return None


def _ensure_instrument_identifier(
    session: Session,
    *,
    instrument: ResearchInstrument,
    provider: str,
    candidate: IdentifierCandidate,
    now: Any,
) -> None:
    row = (
        session.query(ResearchInstrumentIdentifier)
        .filter(
            ResearchInstrumentIdentifier.provider == provider[:32],
            ResearchInstrumentIdentifier.id_type == candidate.id_type,
            func.upper(ResearchInstrumentIdentifier.id_value) == candidate.id_value.upper(),
        )
        .order_by(ResearchInstrumentIdentifier.id.asc())
        .first()
    )
    if row is None:
        session.add(
            ResearchInstrumentIdentifier(
                instrument_id=int(instrument.id),
                provider=provider[:32],
                id_type=candidate.id_type,
                id_value=candidate.id_value[:128],
                exchange=(candidate.exchange or "")[:64] or None,
                currency=None,
                is_primary=bool(candidate.is_primary),
                metadata_json={},
                created_at=now,
            )
        )
        return
    if int(row.instrument_id) == int(instrument.id):
        row.exchange = (candidate.exchange or row.exchange or "")[:64] or None
        row.is_primary = bool(row.is_primary or candidate.is_primary)


def _ensure_security_instrument(
    session: Session,
    *,
    issuer: Issuer,
    company: Company,
    display_name: str,
    legal_name: str | None,
    jurisdiction: str | None,
    market: str | None,
    source: str | None,
    identifiers: list[IdentifierCandidate],
    aliases: list[str],
    now: Any,
) -> ResearchInstrument | None:
    security = _security_candidate(identifiers)
    if security is None:
        return None
    provider = _clean(source) or "official"
    instrument = (
        session.query(ResearchInstrument)
        .join(ResearchInstrumentIdentifier, ResearchInstrumentIdentifier.instrument_id == ResearchInstrument.id)
        .filter(
            ResearchInstrumentIdentifier.provider == provider[:32],
            ResearchInstrumentIdentifier.id_type == security.id_type,
            func.upper(ResearchInstrumentIdentifier.id_value) == security.id_value.upper(),
        )
        .order_by(ResearchInstrument.id.asc())
        .first()
    )
    if instrument is None:
        key = f"official:{jurisdiction or 'xx'}:{market or security.exchange or 'market'}:{security.id_type}:{security.id_value}"
        instrument = session.query(ResearchInstrument).filter_by(instrument_key=key[:255]).first()
    if instrument is None:
        instrument = ResearchInstrument(
            instrument_key=f"official:{jurisdiction or 'xx'}:{market or security.exchange or 'market'}:{security.id_type}:{security.id_value}"[:255],
            display_name=display_name[:255],
            legal_name=legal_name[:512] if legal_name else None,
            ticker=security.id_value[:64],
            exchange=(market or security.exchange or "")[:64] or None,
            currency=None,
            country=(jurisdiction or "")[:8] or None,
            security_type="STK",
            is_public=True,
            issuer_id=int(issuer.id),
            company_id=int(company.id),
            ibkr_conid=None,
            yahoo_symbol=None,
            primary_provider=provider[:32],
            metadata_json={},
            created_at=now,
            updated_at=now,
        )
        session.add(instrument)
        session.flush()
    else:
        instrument.display_name = display_name[:255] or instrument.display_name
        instrument.legal_name = (legal_name or instrument.legal_name or "")[:512] or None
        instrument.ticker = instrument.ticker or security.id_value[:64]
        instrument.exchange = instrument.exchange or (market or security.exchange or "")[:64] or None
        instrument.country = instrument.country or (jurisdiction or "")[:8] or None
        instrument.issuer_id = int(issuer.id)
        instrument.company_id = int(company.id)
        instrument.updated_at = now

    meta = dict(instrument.metadata_json or {})
    _merge_extra_list(meta, "aliases", aliases)
    _merge_extra_list(meta, "source_identifiers", _identifier_metadata(identifiers, source=source, market=market))
    meta["identity_resolution"] = {
        "source": source,
        "jurisdiction": jurisdiction,
        "market": market,
        "issuer_id": int(issuer.id),
        "company_id": int(company.id),
    }
    instrument.metadata_json = meta
    for candidate in identifiers:
        _ensure_instrument_identifier(session, instrument=instrument, provider=provider, candidate=candidate, now=now)
    session.flush()
    return instrument


def _ensure_subject_aliases(session: Session, *, issuer: Issuer, company: Company | None, aliases: list[str], identifiers: list[IdentifierCandidate], now: Any) -> None:
    try:
        from thoth_terminal.decision_spine_service import ensure_decision_subject

        subject = ensure_decision_subject(session, company=company, issuer=issuer)
    except Exception:
        subject = None
    if subject is None:
        return
    existing = {
        _clean(row[0]).casefold()
        for row in session.query(ResearchSubjectAlias.alias).filter_by(subject_id=int(subject.id)).all()
        if _clean(row[0])
    }
    typed_aliases: list[tuple[str, str]] = [(alias, "name") for alias in aliases]
    typed_aliases.extend((candidate.id_value, candidate.id_type.lower()) for candidate in identifiers)
    for alias, alias_type in typed_aliases:
        value = _clean(alias)
        key = value.casefold()
        if not value or key in existing:
            continue
        existing.add(key)
        session.add(
            ResearchSubjectAlias(
                subject_id=int(subject.id),
                alias=value[:255],
                alias_type=_clean(alias_type)[:32] or "name",
                created_at=now,
            )
        )
    session.flush()


def resolve_issuer_identity(
    session: Session,
    *,
    jurisdiction: object = None,
    source: object = None,
    market: object = None,
    local_company_name: object = None,
    official_english_name: object = None,
    source_identifiers: dict[str, Any] | list[Any] | None = None,
    aliases: list[object] | None = None,
    industry: object = None,
    create: bool = True,
) -> IdentityResolution | None:
    source_name = _clean(source) or None
    jurisdiction_code = _normalize_jurisdiction(jurisdiction, source_name)
    market_code = _normalize_market(market)
    identifiers = normalize_source_identifiers(
        jurisdiction=jurisdiction_code,
        source=source_name,
        market=market_code,
        source_identifiers=source_identifiers,
    )
    local_name = _clean(local_company_name)
    english_name = _clean(official_english_name)
    if jurisdiction_code == "TW" and not english_name:
        code = next((row.id_value for row in identifiers if row.id_type == "TW_STOCK"), "")
        resolved = resolve_taiwan_company_name(company_code=code, local_name=local_name)
        english_name = _clean(resolved.get("company_name_en"))
        aliases = list(aliases or []) + list(resolved.get("company_aliases") or [])
    display_name = english_name or local_name or next((row.id_value for row in identifiers), "Issuer")
    alias_values = _dedupe(
        [
            display_name,
            english_name,
            local_name,
            *(aliases or []),
            *(row.id_value for row in identifiers),
        ]
    )
    if not identifiers and not any(alias_values):
        return None

    issuer, match_basis = _find_issuer_by_identifiers(session, identifiers)
    company: Company | None = _company_for_issuer(session, issuer) if issuer is not None else None
    if issuer is None:
        issuer, company = _find_exact_alias(session, alias_values, jurisdiction=jurisdiction_code, market=market_code)
        if issuer is not None:
            match_basis = "alias_exact"

    if issuer is None and not create:
        return None

    now = utcnow()
    if issuer is None:
        provisional_key = next((f"{row.id_type.lower()}-{row.id_value}" for row in identifiers), display_name)
        issuer = Issuer(
            slug=_unique_slug(session, Issuer, f"{jurisdiction_code or 'issuer'}-{display_name}-{provisional_key}"),
            lei=None,
            legal_name=(english_name or local_name or display_name)[:512],
            display_name=display_name[:255],
            jurisdiction=jurisdiction_code,
            entity_status="ACTIVE",
            sector=None,
            industry=_clean(industry)[:128] or None,
            sic_code=None,
            website=None,
            ir_website=None,
            created_at=now,
            updated_at=now,
            extra={},
        )
        session.add(issuer)
        session.flush()
        match_basis = "provisional_identifier" if identifiers else "provisional_alias"
    else:
        if jurisdiction_code and not _clean(issuer.jurisdiction):
            issuer.jurisdiction = jurisdiction_code
        if _clean(industry) and not _clean(issuer.industry):
            issuer.industry = _clean(industry)[:128]
        if display_name and _clean(issuer.display_name).lower().startswith("issuer "):
            issuer.display_name = display_name[:255]
        issuer.updated_at = now

    for candidate in identifiers:
        _ensure_issuer_identifier(session, issuer=issuer, candidate=candidate, now=now)
    session.flush()

    security = _security_candidate(identifiers)
    company = _ensure_company(
        session,
        issuer=issuer,
        company=company,
        display_name=display_name,
        jurisdiction=jurisdiction_code,
        market=market_code,
        industry=_clean(industry) or None,
        security_code=security.id_value if security is not None else None,
        now=now,
    )

    from thoth_terminal.identity_alias_service import valid_identity_aliases
    alias_values = valid_identity_aliases(session, issuer_id=int(issuer.id), aliases=alias_values)
    instrument = _ensure_security_instrument(
        session,
        issuer=issuer,
        company=company,
        display_name=display_name,
        legal_name=issuer.legal_name,
        jurisdiction=jurisdiction_code,
        market=market_code,
        source=source_name,
        identifiers=identifiers,
        aliases=alias_values,
        now=now,
    )

    source_identifier_meta = _identifier_metadata(identifiers, source=source_name, market=market_code)
    extra = dict(issuer.extra or {})
    _merge_extra_list(extra, "aliases", alias_values)
    if local_name:
        _merge_extra_list(
            extra,
            "localized_names",
            [{"name": local_name, "source": source_name, "jurisdiction": jurisdiction_code}],
        )
    _merge_extra_list(extra, "source_identifiers", source_identifier_meta)
    extra["identity_resolution"] = {
        "jurisdiction": jurisdiction_code,
        "source": source_name,
        "market": market_code,
        "match_basis": match_basis,
        "confidence": 1.0 if str(match_basis or "").startswith("identifier:") else (0.86 if match_basis == "alias_exact" else 0.68),
        "updated_at": now.isoformat(),
    }
    issuer.extra = extra
    session.flush()

    _ensure_subject_aliases(session, issuer=issuer, company=company, aliases=alias_values, identifiers=identifiers, now=now)
    confidence = 1.0 if str(match_basis or "").startswith("identifier:") else (0.86 if match_basis == "alias_exact" else 0.68)
    return IdentityResolution(
        issuer_id=int(issuer.id) if issuer.id is not None else None,
        company_id=int(company.id) if company is not None and company.id is not None else None,
        security_id=int(instrument.id) if instrument is not None and instrument.id is not None else None,
        display_name=display_name,
        legal_name=issuer.legal_name,
        jurisdiction=jurisdiction_code,
        source=source_name,
        market=market_code,
        confidence=confidence,
        match_basis=match_basis or "unknown",
        aliases=alias_values,
        source_identifiers=source_identifier_meta,
        local_company_name=local_name or None,
        official_english_name=english_name or None,
    )


def _metadata_source_identifiers(metadata: dict[str, Any]) -> dict[str, Any] | list[Any]:
    existing = metadata.get("source_identifiers")
    if isinstance(existing, list):
        return existing
    values = {
        "company_code": metadata.get("company_code"),
        "stock_code": metadata.get("stock_code"),
        "corp_code": metadata.get("corp_code"),
        "corp_cls": metadata.get("corp_cls"),
        "edinet_code": metadata.get("edinet_code"),
        "sec_code": metadata.get("sec_code"),
        "jcn": metadata.get("jcn"),
        "company_id": metadata.get("company_id") or metadata.get("krx_company_id"),
        "lei": metadata.get("lei"),
        "cik": metadata.get("cik"),
        "isin": metadata.get("isin"),
        "cusip": metadata.get("cusip"),
        "figi": metadata.get("figi"),
        "sedol": metadata.get("sedol"),
    }
    return {key: value for key, value in values.items() if _clean(value)}


def resolve_identity_from_metadata(
    session: Session,
    metadata: dict[str, Any],
    *,
    source: str | None = None,
    author: object = None,
    title: object = None,
    create: bool = True,
) -> IdentityResolution | None:
    connector = _clean(source or metadata.get("connector")).lower()
    if connector not in _CONNECTOR_JURISDICTIONS:
        return None
    jurisdiction = metadata.get("country") or metadata.get("jurisdiction") or _CONNECTOR_JURISDICTIONS.get(connector)
    market = metadata.get("market") or metadata.get("exchange")
    local_name = (
        metadata.get("company_name_original")
        or metadata.get("local_company_name")
        or metadata.get("filer_name")
        or metadata.get("filerName")
        or metadata.get("corp_name")
        or metadata.get("company_name")
        or author
    )
    english_name = (
        metadata.get("company_name_en")
        or metadata.get("corp_eng_name")
        or metadata.get("company_display_name")
        or (metadata.get("company_name") if connector in {"tdnet_company_announcements", "krx_kind_disclosure"} else None)
    )
    if connector == "edinetdb_financials":
        english_name = metadata.get("company_name_en") or metadata.get("name_en") or metadata.get("company_name") or english_name
    aliases = []
    company_aliases = metadata.get("company_aliases") or []
    if isinstance(company_aliases, str):
        aliases.extend(part.strip() for part in company_aliases.split(",") if part.strip())
    else:
        for value in company_aliases:
            aliases.append(value)
    return resolve_issuer_identity(
        session,
        jurisdiction=jurisdiction,
        source=connector,
        market=market,
        local_company_name=local_name,
        official_english_name=english_name,
        source_identifiers=_metadata_source_identifiers(metadata),
        aliases=aliases,
        industry=metadata.get("industry") or metadata.get("industry_display_name") or metadata.get("sector"),
        create=create,
    )


def enrich_metadata_with_identity(
    session: Session,
    metadata: dict[str, Any],
    *,
    source: str | None = None,
    author: object = None,
    title: object = None,
    create: bool = True,
) -> IdentityResolution | None:
    resolution = resolve_identity_from_metadata(session, metadata, source=source, author=author, title=title, create=create)
    if resolution is None:
        return None
    payload = resolution.as_metadata()
    metadata["issuer_id"] = resolution.issuer_id
    metadata["company_id"] = resolution.company_id
    metadata["security_id"] = resolution.security_id
    metadata.setdefault("resolved_issuer_id", resolution.issuer_id)
    metadata.setdefault("resolved_company_id", resolution.company_id)
    metadata.setdefault("resolved_security_id", resolution.security_id)
    metadata["identity_resolution"] = payload
    metadata["source_identifiers"] = payload["source_identifiers"]
    if resolution.display_name:
        metadata.setdefault("company_display_name", resolution.display_name)
    aliases = _dedupe([*(metadata.get("company_aliases") or []), *resolution.aliases])
    if aliases:
        metadata["company_aliases"] = aliases
    company = session.query(Company).filter_by(id=int(resolution.company_id)).first() if resolution.company_id is not None else None
    if company is not None:
        metadata.setdefault("company_slug", company.slug)
    return resolution
