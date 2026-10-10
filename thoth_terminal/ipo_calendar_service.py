from __future__ import annotations

import hashlib
import re
from collections import Counter
from datetime import date, datetime
from threading import RLock
from typing import Any
import weakref

from sqlalchemy import func
from sqlalchemy.orm import Session

from thoth_terminal.dataset_service import create_dataset, replace_dataset_draft
from thoth_terminal.db import utcnow
from thoth_terminal.models import (
    Company,
    CompanyDocument,
    Dataset,
    Document,
    IPOEvent,
    Issuer,
    IssuerIdentifier,
    Source,
)


DATASET_FAMILY_IPO_CALENDAR = "ipo_calendar"
GLOBAL_IPO_CALENDAR_SLUG = "global-ipo-calendar"
IPO_CONNECTORS = {
    "sec_ipo_filings",
    "nasdaq_ipo_calendar",
    "asx_ipo_calendar",
    "euronext_ipo_calendar",
    "jpx_ipo_calendar",
    "hkex_ipo_calendar",
    "krx_ipo_calendar",
    "twse_ipo_calendar",
}


_IPO_TABLE_ENSURE_LOCK = RLock()
_IPO_TABLE_ENSURED_ENGINES: "weakref.WeakSet[Any]" = weakref.WeakSet()


def ensure_ipo_tables(session: Session) -> None:
    bind = session.get_bind()
    engine = getattr(bind, "engine", bind)
    if engine in _IPO_TABLE_ENSURED_ENGINES:
        return
    with _IPO_TABLE_ENSURE_LOCK:
        if engine in _IPO_TABLE_ENSURED_ENGINES:
            return
        try:
            IPOEvent.__table__.create(bind=bind, checkfirst=True)
            _IPO_TABLE_ENSURED_ENGINES.add(engine)
        except Exception:
            pass


def _slugify(value: object, *, default: str = "ipo") -> str:
    raw = str(value or "").strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    return (slug[:110] or default).strip("-") or default


def _unique_slug(session: Session, base: str) -> str:
    slug = _slugify(base)
    existing = {row[0] for row in session.query(Issuer.slug).filter(Issuer.slug.like(f"{slug}%")).all()}
    existing.update(row[0] for row in session.query(Company.slug).filter(Company.slug.like(f"{slug}%")).all())
    if slug not in existing:
        return slug
    digest = hashlib.sha1(base.encode("utf-8", errors="ignore")).hexdigest()[:8]
    candidate = f"{slug[:100]}-{digest}"
    if candidate not in existing:
        return candidate
    idx = 2
    while f"{candidate}-{idx}" in existing:
        idx += 1
    return f"{candidate}-{idx}"


def _date(value: Any) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except Exception:
        return None


def _float(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return float(raw.replace(",", ""))
    except Exception:
        return None


def _text(value: Any, *, limit: int | None = None) -> str | None:
    raw = re.sub(r"\s+", " ", str(value or "").strip())
    if not raw:
        return None
    return raw[:limit] if limit else raw


def _jurisdiction(region: str | None) -> str | None:
    mapping = {"US": "US", "HK": "HK", "JP": "JP", "AU": "AU", "KR": "KR", "TW": "TW", "EU": None}
    return mapping.get(str(region or "").upper(), str(region or "").upper()[:2] or None)


def _issuer_status(stage: str | None, status: str | None) -> str:
    if str(status or "").lower() == "withdrawn" or str(stage or "").lower() == "withdrawn":
        return "WITHDRAWN"
    if str(stage or "").lower() in {"listed", "priced"}:
        return "ACTIVE"
    return "IPO_CANDIDATE"


def _event_key(event: dict[str, Any]) -> str:
    explicit = _text(event.get("event_key"), limit=255)
    if explicit:
        return explicit
    parts = [
        event.get("region"),
        event.get("exchange"),
        event.get("ticker"),
        event.get("isin"),
        event.get("cik"),
        event.get("company_name"),
        event.get("stage"),
        event.get("listing_date") or event.get("pricing_date") or event.get("application_date") or event.get("announcement_date"),
        event.get("source_url"),
    ]
    payload = "|".join(str(p or "").strip().lower() for p in parts if str(p or "").strip())
    digest = hashlib.sha1(payload.encode("utf-8", errors="ignore")).hexdigest()[:24]
    return f"ipo:{digest}"


def _identifier_value(kind: str, value: Any) -> str | None:
    raw = _text(value, limit=64)
    if not raw:
        return None
    if kind == "CIK":
        digits = "".join(ch for ch in raw if ch.isdigit())
        return digits.zfill(10) if digits else None
    return raw.upper()


def _find_issuer_by_identifier(session: Session, *, kind: str, value: str | None) -> Issuer | None:
    if not value:
        return None
    identifier = session.query(IssuerIdentifier).filter_by(id_type=kind, id_value=value).first()
    return identifier.issuer if identifier else None


def _ensure_identifier(
    session: Session,
    issuer: Issuer,
    *,
    kind: str,
    value: str | None,
    exchange: str | None = None,
    currency: str | None = None,
    is_primary: bool = False,
) -> None:
    normalized = _identifier_value(kind, value)
    if not normalized:
        return
    existing = session.query(IssuerIdentifier).filter_by(id_type=kind, id_value=normalized).first()
    if existing:
        if existing.issuer_id == issuer.id:
            existing.exchange = exchange or existing.exchange
            existing.currency = currency or existing.currency
            existing.is_primary = bool(existing.is_primary or is_primary)
        return
    session.add(
        IssuerIdentifier(
            issuer_id=int(issuer.id),
            id_type=kind,
            id_value=normalized,
            exchange=exchange,
            currency=currency,
            is_primary=is_primary,
            valid_from=None,
            valid_to=None,
            created_at=utcnow(),
        )
    )


def _ensure_issuer(session: Session, event: dict[str, Any]) -> Issuer:
    company_name = _text(event.get("company_name"), limit=512) or "IPO candidate"
    ticker = _identifier_value("TICKER", event.get("ticker"))
    isin = _identifier_value("ISIN", event.get("isin"))
    cik = _identifier_value("CIK", event.get("cik"))
    exchange = _text(event.get("exchange"), limit=32)
    currency = _text(event.get("currency") or event.get("proceeds_currency"), limit=8)

    issuer = (
        _find_issuer_by_identifier(session, kind="CIK", value=cik)
        or _find_issuer_by_identifier(session, kind="ISIN", value=isin)
        or _find_issuer_by_identifier(session, kind="TICKER", value=ticker)
    )
    if issuer is None:
        issuer = (
            session.query(Issuer)
            .filter(
                (func.lower(Issuer.legal_name) == company_name.lower())
                | (func.lower(Issuer.display_name) == company_name.lower())
            )
            .first()
        )
    if issuer is None:
        now = utcnow()
        slug_base = f"ipo-{event.get('region') or 'global'}-{ticker or company_name}"
        issuer = Issuer(
            slug=_unique_slug(session, slug_base),
            lei=None,
            legal_name=company_name,
            display_name=company_name[:255],
            jurisdiction=_jurisdiction(_text(event.get("region"))),
            entity_status=_issuer_status(_text(event.get("stage")), _text(event.get("status"))),
            sector=None,
            industry=None,
            sic_code=None,
            website=None,
            ir_website=None,
            created_at=now,
            updated_at=now,
            extra={
                "origin": "ipo_calendar",
                "provisional": True,
                "first_seen_source": event.get("source_url"),
            },
        )
        session.add(issuer)
        session.flush()
    else:
        issuer.updated_at = utcnow()
        issuer.entity_status = issuer.entity_status or _issuer_status(_text(event.get("stage")), _text(event.get("status")))
        if not issuer.jurisdiction:
            issuer.jurisdiction = _jurisdiction(_text(event.get("region")))
        extra = dict(issuer.extra or {})
        extra.setdefault("origin", "ipo_calendar")
        extra.setdefault("provisional", True)
        issuer.extra = extra

    _ensure_identifier(session, issuer, kind="CIK", value=cik, exchange=exchange, currency=currency)
    _ensure_identifier(session, issuer, kind="ISIN", value=isin, exchange=exchange, currency=currency)
    _ensure_identifier(session, issuer, kind="TICKER", value=ticker, exchange=exchange, currency=currency, is_primary=True)
    return issuer


def _ensure_company(session: Session, issuer: Issuer, event: dict[str, Any]) -> Company:
    company_name = _text(event.get("company_name"), limit=255) or issuer.display_name
    ticker = _text(event.get("ticker"), limit=32)
    exchange = _text(event.get("exchange"), limit=64)
    region = _text(event.get("region"), limit=64)

    company = session.query(Company).filter_by(issuer_id=int(issuer.id)).first()
    if company is None and ticker:
        q = session.query(Company).filter(func.upper(Company.ticker) == ticker.upper())
        if exchange:
            matched = q.filter(func.upper(Company.exchange) == exchange.upper()).first()
            company = matched or q.first()
        else:
            company = q.first()
    if company is None:
        slug_base = f"{exchange or region or 'ipo'}-{ticker or company_name}"
        company = Company(
            slug=_unique_slug(session, slug_base),
            name=company_name,
            ticker=ticker,
            exchange=exchange,
            region=region,
            sector=None,
            industry=None,
            supply_chain_stage=None,
            ir_url=None,
            filings_url=None,
            reports_url=None,
            calendar_url=event.get("source_url"),
            created_at=utcnow(),
            issuer_id=int(issuer.id),
        )
        session.add(company)
        session.flush()
    else:
        company.name = company.name or company_name
        company.ticker = company.ticker or ticker
        company.exchange = company.exchange or exchange
        company.region = company.region or region
        company.calendar_url = company.calendar_url or event.get("source_url")
        if company.issuer_id is None:
            company.issuer_id = int(issuer.id)
    return company


def _ensure_company_document(session: Session, company: Company, doc: Document) -> None:
    exists = (
        session.query(CompanyDocument)
        .filter_by(company_id=int(company.id), document_id=int(doc.id), doc_type="ipo_event")
        .first()
    )
    if exists:
        return
    session.add(
        CompanyDocument(
            company_id=int(company.id),
            document_id=int(doc.id),
            doc_type="ipo_event",
            label="IPO calendar event",
            created_at=utcnow(),
        )
    )


def upsert_ipo_event(
    session: Session,
    *,
    event: dict[str, Any],
    doc: Document | None = None,
    source: Source | None = None,
    ingestion_run_id: int | None = None,
) -> IPOEvent:
    ensure_ipo_tables(session)
    company_name = _text(event.get("company_name"), limit=512) or (doc.author if doc else None) or (doc.title if doc else None) or "IPO candidate"
    event = dict(event)
    event["company_name"] = company_name
    key = _event_key(event)
    issuer = _ensure_issuer(session, event)
    company = _ensure_company(session, issuer, event)
    if doc is not None:
        _ensure_company_document(session, company, doc)

    row = session.query(IPOEvent).filter_by(event_key=key).first()
    now = utcnow()
    if row is None:
        row = IPOEvent(event_key=key, created_at=now, updated_at=now)
        session.add(row)

    row.issuer_id = int(issuer.id)
    row.company_id = int(company.id)
    row.source_id = int(source.id) if source is not None and source.id is not None else (int(doc.source_id) if doc is not None and doc.source_id else row.source_id)
    row.document_id = int(doc.id) if doc is not None and doc.id is not None else row.document_id
    row.ingestion_run_id = ingestion_run_id
    row.region = _text(event.get("region"), limit=32)
    row.exchange = _text(event.get("exchange"), limit=32)
    row.market = _text(event.get("market"), limit=128)
    row.stage = _text(event.get("stage"), limit=32) or "filed"
    row.status = _text(event.get("status"), limit=32) or "active"
    row.source_tier = _text(event.get("source_tier"), limit=64)
    row.confidence = _float(event.get("confidence"))
    row.company_name = company_name
    row.ticker = _text(event.get("ticker"), limit=64)
    row.isin = _text(event.get("isin"), limit=64)
    row.cik = _identifier_value("CIK", event.get("cik"))
    row.security_type = _text(event.get("security_type"), limit=128)
    row.deal_type = _text(event.get("deal_type"), limit=64) or "ipo"
    row.listing_date = _date(event.get("listing_date"))
    row.pricing_date = _date(event.get("pricing_date"))
    row.offer_open_date = _date(event.get("offer_open_date"))
    row.offer_close_date = _date(event.get("offer_close_date"))
    row.application_date = _date(event.get("application_date"))
    row.approval_date = _date(event.get("approval_date"))
    row.announcement_date = _date(event.get("announcement_date"))
    row.price_text = _text(event.get("price_text"), limit=128)
    row.offer_price = _float(event.get("offer_price"))
    row.price_range_low = _float(event.get("price_range_low"))
    row.price_range_high = _float(event.get("price_range_high"))
    row.currency = _text(event.get("currency"), limit=8)
    row.shares_offered = _float(event.get("shares_offered"))
    row.proceeds_amount = _float(event.get("proceeds_amount"))
    row.proceeds_currency = _text(event.get("proceeds_currency"), limit=8)
    row.underwriters = _text(event.get("underwriters"))
    row.title = _text(event.get("title"), limit=512) or (doc.title if doc else None)
    row.source_url = _text(event.get("source_url"), limit=2048) or (doc.url if doc else None)
    row.payload = {
        "event": dict(event),
        "document_extra": dict(doc.extra or {}) if doc is not None and isinstance(doc.extra, dict) else {},
    }
    row.updated_at = now
    session.flush()
    return row


def _event_date(row: IPOEvent) -> date | None:
    return row.listing_date or row.pricing_date or row.offer_close_date or row.approval_date or row.application_date or row.announcement_date


def _date_str(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _ipo_dataset_columns() -> list[dict[str, Any]]:
    return [
        {"key": "event_date", "label": "Event Date", "data_type": "date", "role": "timestamp"},
        {"key": "listing_date", "label": "Listing Date", "data_type": "date", "role": "timestamp"},
        {"key": "pricing_date", "label": "Pricing Date", "data_type": "date", "role": "timestamp"},
        {"key": "application_date", "label": "Application Date", "data_type": "date", "role": "timestamp"},
        {"key": "approval_date", "label": "Approval Date", "data_type": "date", "role": "timestamp"},
        {"key": "stage", "label": "Stage", "data_type": "text", "role": "dimension"},
        {"key": "status", "label": "Status", "data_type": "text", "role": "dimension"},
        {"key": "company", "label": "Company", "data_type": "text", "role": "dimension"},
        {"key": "ticker", "label": "Ticker", "data_type": "text", "role": "id"},
        {"key": "isin", "label": "ISIN", "data_type": "text", "role": "id"},
        {"key": "cik", "label": "CIK", "data_type": "text", "role": "id"},
        {"key": "exchange", "label": "Exchange", "data_type": "text", "role": "dimension"},
        {"key": "market", "label": "Market", "data_type": "text", "role": "dimension"},
        {"key": "region", "label": "Region", "data_type": "text", "role": "dimension"},
        {"key": "source_tier", "label": "Source Tier", "data_type": "text", "role": "dimension"},
        {"key": "confidence", "label": "Confidence", "data_type": "number", "role": "metric"},
        {"key": "offer_price", "label": "Offer Price", "data_type": "number", "role": "metric"},
        {"key": "price_text", "label": "Price Text", "data_type": "text", "role": "dimension"},
        {"key": "currency", "label": "Currency", "data_type": "text", "role": "dimension"},
        {"key": "shares_offered", "label": "Shares Offered", "data_type": "number", "role": "metric"},
        {"key": "proceeds_amount", "label": "Proceeds Amount", "data_type": "number", "role": "metric"},
        {"key": "proceeds_currency", "label": "Proceeds Currency", "data_type": "text", "role": "dimension"},
        {"key": "underwriters", "label": "Underwriters", "data_type": "text", "role": "dimension"},
        {"key": "source_url", "label": "Source URL", "data_type": "text", "role": "id"},
        {"key": "source_document_id", "label": "Source Document ID", "data_type": "number", "role": "id"},
        {"key": "issuer_id", "label": "Issuer ID", "data_type": "number", "role": "id"},
        {"key": "company_id", "label": "Company ID", "data_type": "number", "role": "id"},
    ]


def materialize_global_ipo_calendar_dataset(session: Session) -> Dataset:
    ensure_ipo_tables(session)
    events = session.query(IPOEvent).order_by(IPOEvent.updated_at.desc()).limit(5000).all()
    events.sort(key=lambda row: (_event_date(row) or date.min, int(row.id or 0)), reverse=True)
    rows = []
    for event in events:
        rows.append(
            {
                "row_key": event.event_key[:128],
                "values": {
                    "event_date": _date_str(_event_date(event)),
                    "listing_date": _date_str(event.listing_date),
                    "pricing_date": _date_str(event.pricing_date),
                    "application_date": _date_str(event.application_date),
                    "approval_date": _date_str(event.approval_date),
                    "stage": event.stage,
                    "status": event.status,
                    "company": event.company_name,
                    "ticker": event.ticker,
                    "isin": event.isin,
                    "cik": event.cik,
                    "exchange": event.exchange,
                    "market": event.market,
                    "region": event.region,
                    "source_tier": event.source_tier,
                    "confidence": event.confidence,
                    "offer_price": event.offer_price,
                    "price_text": event.price_text,
                    "currency": event.currency,
                    "shares_offered": event.shares_offered,
                    "proceeds_amount": event.proceeds_amount,
                    "proceeds_currency": event.proceeds_currency,
                    "underwriters": event.underwriters,
                    "source_url": event.source_url,
                    "source_document_id": int(event.document_id) if event.document_id else None,
                    "issuer_id": int(event.issuer_id) if event.issuer_id else None,
                    "company_id": int(event.company_id) if event.company_id else None,
                },
            }
        )

    by_exchange = Counter(str(event.exchange or "unknown") for event in events)
    by_stage = Counter(str(event.stage or "unknown") for event in events)
    extra = {
        "family": DATASET_FAMILY_IPO_CALENDAR,
        "row_count": len(rows),
        "source": "ipo_calendar_service",
        "by_exchange": dict(sorted(by_exchange.items())),
        "by_stage": dict(sorted(by_stage.items())),
        "last_materialized_at": utcnow().isoformat(),
    }
    dataset = session.query(Dataset).filter_by(slug=GLOBAL_IPO_CALENDAR_SLUG).first()
    if dataset is None:
        return create_dataset(
            session,
            title="Global IPO Calendar",
            description="Normalized IPO listings, filings, pricing, and expected-calendar events across US, HK, Japan, Euronext, ASX, Korea, and Taiwan sources.",
            category="capital_markets",
            family=DATASET_FAMILY_IPO_CALENDAR,
            status="active",
            source_kind="official_ipo_calendar",
            slug=GLOBAL_IPO_CALENDAR_SLUG,
            columns=_ipo_dataset_columns(),
            rows=rows,
            label="current",
            extra=extra,
            source_event="ipo_calendar_materialized",
        )
    dataset.title = "Global IPO Calendar"
    dataset.description = "Normalized IPO listings, filings, pricing, and expected-calendar events across US, HK, Japan, Euronext, ASX, Korea, and Taiwan sources."
    dataset.category = "capital_markets"
    dataset.family = DATASET_FAMILY_IPO_CALENDAR
    dataset.status = "active"
    dataset.source_kind = "official_ipo_calendar"
    dataset.extra = extra
    replace_dataset_draft(
        session,
        dataset,
        columns=_ipo_dataset_columns(),
        rows=rows,
        label="current",
        extra=extra,
        source_event="ipo_calendar_materialized",
    )
    return dataset


def sync_ipo_document(
    session: Session,
    *,
    doc: Document,
    source: Source | None = None,
    ingestion_run_id: int | None = None,
) -> dict[str, Any]:
    extra = doc.extra if isinstance(doc.extra, dict) else {}
    connector = str(extra.get("connector") or "").strip()
    ipo_event = extra.get("ipo_event") if isinstance(extra.get("ipo_event"), dict) else None
    if connector not in IPO_CONNECTORS and ipo_event is None:
        return {"linked": False}
    if ipo_event is None:
        return {"linked": False, "reason": "missing_ipo_event_payload"}
    event = upsert_ipo_event(session, event=dict(ipo_event), doc=doc, source=source, ingestion_run_id=ingestion_run_id)
    dataset = materialize_global_ipo_calendar_dataset(session)
    return {
        "linked": True,
        "ipo_event_id": int(event.id),
        "issuer_id": int(event.issuer_id) if event.issuer_id else None,
        "company_id": int(event.company_id) if event.company_id else None,
        "dataset_id": int(dataset.id),
    }
