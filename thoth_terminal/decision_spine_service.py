from __future__ import annotations

import hashlib
import re
from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import and_, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload, load_only

from thoth_terminal.config import settings
from thoth_terminal.dataset_service import create_dataset, create_dataset_view, latest_dataset_version, link_dataset, replace_dataset_draft
from thoth_terminal.db import utcnow
from thoth_terminal.discord.yahoo import search_symbols
from thoth_terminal.institutional_model_vocab import metric_definition, metric_value_type_for_key, normalize_metric_key
from thoth_terminal.institutional_service import ensure_entity_identity
from thoth_terminal.investor_data_service import ensure_company_sec_companyfacts_source
from thoth_terminal.jobs import enqueue_ingest_job
from thoth_terminal.instrument_service import (
    REQUEST_STATUS_DRAFT,
    REQUEST_STATUS_REJECTED,
    REQUEST_STATUS_REVIEW,
    build_active_universe_request_scope,
    coverage_gap_scope,
    create_instrument_request,
    serialize_instrument_request,
)
from thoth_terminal.models import (
    AnalystEvidence,
    Company,
    CompanyDocument,
    CoverageGap,
    Dataset,
    DatasetLink,
    DatasetView,
    DecisionEvidenceLink,
    Document,
    EarningsEvent,
    ExchangeDisclosure,
    Entity,
    EntityExternalId,
    InstrumentRequest,
    InstitutionalEvidenceItem,
    InstitutionalObservation,
    InvestmentCatalyst,
    InvestmentMemo,
    IPOEvent,
    IRArtifact,
    Issuer,
    IssuerIdentifier,
    NotificationRule,
    ResearchInstrument,
    ResearchReport,
    ResearchReportVersion,
    ResearchSubject,
    ResearchSubjectAlias,
    ResearchUniverse,
    ResearchUniverseRow,
    SecuritiesFiling,
    Source,
    StructuredDataFact,
    SupplyChain,
    SupplyChainAlertRule,
    SupplyChainMetricBinding,
    SupplyChainSnapshot,
    Thesis,
    TranscriptWatchState,
    thesis_entities,
)
from thoth_terminal.research_report_service import report_memory_quality, report_memory_review_recommendation
from thoth_terminal.source_identity_service import enrich_analyst_source_identity, sync_source_attribution_edges
from thoth_terminal.structured_data_service import EARNINGS_DOC_TYPES, company_structured_fact_scope_ids, sync_company_earnings_document_facts
from thoth_terminal.transcripts import (
    _parse_period_from_text,
    document_is_readable_transcript as _shared_document_is_readable_transcript,
    is_plausible_earnings_fiscal_year,
    transcript_document_text_len,
)
from thoth_terminal.ttl_cache import TTLCache, json_cache_weight
from thoth_terminal.url_utils import clean_url


_INSTRUMENT_DECISION_SECURITY_TYPES = ("ADR", "ADS", "COMMON", "EQUITY", "STK")
_DECISION_PAYLOAD_CACHE: TTLCache[dict[str, Any]] = TTLCache(
    max_entries=64,
    default_ttl_s=30.0,
    max_weight=16 * 1024 * 1024,
    get_weight=json_cache_weight,
)
_DECISION_PAGE_PAYLOAD_CACHE: TTLCache[dict[str, Any]] = TTLCache(
    max_entries=128,
    default_ttl_s=30.0,
    max_weight=8 * 1024 * 1024,
    get_weight=json_cache_weight,
)
_IDENTIFIER_EXCHANGE_ALIASES = {
    "hong kong stock exchange": "HKEX",
    "korea exchange": "KRX",
    "london stock exchange": "LSE",
    "mexican stock exchange": "BMV",
    "nasdaq global market": "NASDAQ",
    "nasdaq global select market": "NASDAQ",
    "nasdaq stock market": "NASDAQ",
    "new york stock exchange": "NYSE",
    "shanghai stock exchange": "SSE",
    "shenzhen stock exchange": "SZSE",
    "sao paulo stock exchange": "B3",
    "taiwan stock exchange": "TWSE",
    "tokyo stock exchange": "TSE",
}


def clear_decision_payload_cache() -> None:
    _DECISION_PAYLOAD_CACHE.clear()
    _DECISION_PAGE_PAYLOAD_CACHE.clear()


def normalize_identifier_exchange(exchange: object) -> str | None:
    text = str(exchange or "").strip()
    if not text:
        return None
    key = re.sub(r"\s+", " ", text.casefold()).strip()
    alias = _IDENTIFIER_EXCHANGE_ALIASES.get(key)
    if alias:
        return alias
    compact = re.sub(r"[^A-Z0-9./_-]+", "", text.upper())
    if not compact:
        return None
    return compact[:16]


def _decision_payload_cache_key(
    session: Session,
    *,
    company: Company | None,
    issuer: Issuer | None,
) -> str:
    bind = session.get_bind()
    company_id = int(company.id) if company is not None and company.id is not None else 0
    issuer_id = int(issuer.id) if issuer is not None and issuer.id is not None else 0
    if issuer_id == 0 and company is not None and company.issuer_id is not None:
        issuer_id = int(company.issuer_id)
    return f"decision:{id(bind)}:{company_id}:{issuer_id}"
_LEGAL_SUFFIX_RE = re.compile(
    r"\b("
    r"incorporated|inc|corp|corporation|co|company|ltd|limited|plc|ag|sa|se|nv|bv|kk|kabushiki|kaisha|"
    r"holdings|holding|group|the|class|ordinary|common|adr|ads|sponsored|/de|de"
    r")\b",
    re.IGNORECASE,
)


def _slugify(value: object, *, default: str = "item") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return slug[:120] or default


def canonical_entity_key(value: object) -> str:
    """Normalize issuer/company names for durable exact and near-exact matching."""

    raw = str(value or "").strip().lower()
    raw = raw.replace("&", " and ")
    raw = re.sub(r"\([^)]*\)", " ", raw)
    raw = re.sub(r"[/,._-]+", " ", raw)
    raw = _LEGAL_SUFFIX_RE.sub(" ", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw


def _name_tokens(value: object) -> set[str]:
    return {token for token in canonical_entity_key(value).split() if len(token) > 1}


def name_match_score(left: object, right: object) -> float:
    left_key = canonical_entity_key(left)
    right_key = canonical_entity_key(right)
    if not left_key or not right_key:
        return 0.0
    if left_key == right_key:
        return 1.0
    left_tokens = _name_tokens(left_key)
    right_tokens = _name_tokens(right_key)
    return _prepared_name_match_score((left_key, left_tokens), (right_key, right_tokens))


def _prepared_name_match_score(left, right) -> float:
    left_key, left_tokens = left
    right_key, right_tokens = right
    if not left_key or not right_key:
        return 0.0
    if left_key == right_key:
        return 1.0
    if not left_tokens or not right_tokens:
        return 0.0
    overlap = len(left_tokens & right_tokens)
    containment = overlap / max(1, min(len(left_tokens), len(right_tokens)))
    jaccard = overlap / max(1, len(left_tokens | right_tokens))
    if containment >= 1.0 and min(len(left_tokens), len(right_tokens)) >= 2:
        return max(0.9, jaccard)
    return max(containment * 0.85, jaccard)


class _UniversePopulationDirectory:
    """One bulk operation's bounded directories and prepared matching inputs."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self._identities: dict[type, list[Any]] = {}
        self._prepared: dict[tuple[type, int], tuple[tuple[str, ...], list[Any]]] = {}
        self._inputs: dict[str, tuple[str, set[str]]] = {}
        self._datasets: dict[int, list[Dataset]] = {}
        self._dataset_identities: dict[int, tuple[dict[str, Any], dict[str, set[Any]]]] = {}

    def identities(self, model: type) -> list[Any]:
        if model not in self._identities:
            columns = (
                (Company.id, Company.name, Company.slug, Company.ticker, Company.issuer_id, Company.exchange, Company.region)
                if model is Company else (Issuer.id, Issuer.display_name, Issuer.legal_name, Issuer.slug, Issuer.extra)
            )
            self._identities[model] = self.session.query(model).options(load_only(*columns)).order_by(model.id.asc()).limit(5000).all()
        return self._identities[model]

    def register(self, *identities: Any) -> None:
        for identity in identities:
            if identity is None or type(identity) not in self._identities:
                continue
            rows = self._identities[type(identity)]
            if any(row.id == identity.id for row in rows):
                continue
            rows.append(identity)
            rows.sort(key=lambda row: int(row.id))
            del rows[5000:]

    def input_name(self, name: str):
        if name not in self._inputs:
            key = canonical_entity_key(name)
            self._inputs[name] = (key, _name_tokens(key))
        return self._inputs[name]

    def candidate_names(self, identity):
        if isinstance(identity, Company):
            values = (identity.name, identity.slug, identity.ticker)
        else:
            values = (identity.display_name, identity.legal_name, identity.slug)
            if isinstance(identity.extra, dict):
                values += tuple(str(value) for value in (identity.extra.get("aliases") or []) if str(value or "").strip())
        signature = tuple(str(value or "") for value in values)
        key = (type(identity), int(identity.id))
        cached = self._prepared.get(key)
        if cached is None or cached[0] != signature:
            cached = (signature, [self.input_name(value) for value in signature if value.strip()])
            self._prepared[key] = cached
        return cached[1]

    def datasets(self, limit: int) -> list[Dataset]:
        limit = max(1, int(limit))
        if limit not in self._datasets:
            self._datasets[limit] = (
                self.session.query(Dataset).filter(Dataset.extra.isnot(None))
                .options(load_only(Dataset.id, Dataset.extra))
                .order_by(Dataset.updated_at.desc(), Dataset.id.desc()).limit(limit).all()
            )
        return self._datasets[limit]

    def dataset_identity(self, dataset: Dataset):
        cached = self._dataset_identities.get(int(dataset.id))
        if cached is None or cached[0] != dataset.extra:
            cached = (deepcopy(dataset.extra), _dataset_identity_payload(dataset))
            self._dataset_identities[int(dataset.id)] = cached
        return cached[1]


@contextmanager
def universe_population_directory(session: Session):
    """Nested population rails share one directory; leave no session cache behind."""
    key = "universe_population_directory"
    if key in session.info:
        yield session.info[key]
        return
    directory = _UniversePopulationDirectory(session)
    session.info[key] = directory
    try:
        yield directory
    finally:
        session.info.pop(key, None)


def _now() -> datetime:
    return utcnow()


def calendar_timezone(tz_name: str | None = None) -> ZoneInfo:
    name = str(tz_name or getattr(settings, "default_active_window_tz", None) or "America/New_York").strip()
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def calendar_today(now: datetime | None = None, *, tz_name: str | None = None) -> date:
    now_v = now or _now()
    if now_v.tzinfo is None:
        now_utc = now_v.replace(tzinfo=timezone.utc)
    else:
        now_utc = now_v.astimezone(timezone.utc)
    return now_utc.astimezone(calendar_timezone(tz_name)).date()


def earnings_event_is_effectively_completed(
    event: EarningsEvent,
    *,
    now: datetime | None = None,
    today: date | None = None,
) -> bool:
    now_v = now or _now()
    today_v = today or calendar_today(now_v)
    if event.actual_release_at is not None:
        return event.actual_release_at <= now_v
    if event.status != "completed":
        return False
    return event.scheduled_date is not None and event.scheduled_date <= today_v


def earnings_event_completed_filter(*, now: datetime | None = None, today: date | None = None):
    now_v = now or _now()
    today_v = today or calendar_today(now_v)
    return or_(
        and_(EarningsEvent.actual_release_at.isnot(None), EarningsEvent.actual_release_at <= now_v),
        and_(
            EarningsEvent.status == "completed",
            EarningsEvent.actual_release_at.is_(None),
            EarningsEvent.scheduled_date.isnot(None),
            EarningsEvent.scheduled_date <= today_v,
        ),
    )


def _earnings_event_primary_date(event: EarningsEvent) -> date | None:
    if event.actual_release_at is not None:
        return event.actual_release_at.date()
    if event.actual_call_at is not None:
        return event.actual_call_at.date()
    return event.scheduled_date


def _event_artifact_period(artifact: IRArtifact | None) -> tuple[int | None, int | None]:
    if artifact is None:
        return None, None
    year = int(artifact.fiscal_year) if artifact.fiscal_year is not None else None
    quarter = int(artifact.fiscal_quarter) if artifact.fiscal_quarter is not None else None
    if year is not None and quarter is not None:
        return year, quarter
    parsed_year, parsed_quarter = _parse_period_from_text(artifact.title, artifact.canonical_url)
    return parsed_year or year, parsed_quarter or quarter


def _event_filing_period(filing: SecuritiesFiling | None) -> tuple[int | None, int | None]:
    if filing is None:
        return None, None
    year = int(filing.fiscal_year) if filing.fiscal_year is not None else None
    quarter = int(filing.fiscal_quarter) if filing.fiscal_quarter is not None else None
    if year is not None and quarter is not None:
        return year, quarter
    parsed_year, parsed_quarter = _parse_period_from_text(filing.title, filing.filing_url or filing.primary_document_url)
    return parsed_year or year, parsed_quarter or quarter


def _earnings_event_effective_period(session: Session, event: EarningsEvent) -> tuple[int | None, int | None]:
    year = int(event.fiscal_year) if event.fiscal_year is not None else None
    quarter = int(event.fiscal_quarter) if event.fiscal_quarter is not None else None
    if year is not None and quarter is not None:
        return year, quarter
    component_ids = [
        event.release_artifact_id,
        event.presentation_artifact_id,
        event.transcript_artifact_id,
        event.webcast_artifact_id,
    ]
    for artifact_id in component_ids:
        if artifact_id is None:
            continue
        parsed_year, parsed_quarter = _event_artifact_period(session.get(IRArtifact, int(artifact_id)))
        if parsed_quarter is not None:
            return parsed_year or year, parsed_quarter
        year = year or parsed_year
    if event.release_filing_id is not None:
        parsed_year, parsed_quarter = _event_filing_period(session.get(SecuritiesFiling, int(event.release_filing_id)))
        if parsed_quarter is not None:
            return parsed_year or year, parsed_quarter
        year = year or parsed_year
    return year, quarter


def _link_matching_transcript_artifact(
    session: Session,
    event: EarningsEvent,
    *,
    fiscal_year: int | None,
    fiscal_quarter: int | None,
) -> bool:
    if event.transcript_artifact_id is not None:
        return _transcript_artifact_has_readable_document(session, int(event.transcript_artifact_id))
    if fiscal_year is None or fiscal_quarter is None:
        return False
    candidates = (
        session.query(IRArtifact)
        .filter(IRArtifact.issuer_id == int(event.issuer_id), IRArtifact.artifact_type == "transcript")
        .order_by(IRArtifact.published_at.desc().nullslast(), IRArtifact.id.desc())
        .limit(100)
        .all()
    )
    for artifact in candidates:
        artifact_year, artifact_quarter = _event_artifact_period(artifact)
        if artifact_year != int(fiscal_year) or artifact_quarter != int(fiscal_quarter):
            continue
        if not _transcript_artifact_has_readable_document(session, int(artifact.id)):
            continue
        event.transcript_artifact_id = int(artifact.id)
        if artifact.published_at is not None:
            event.actual_call_at = event.actual_call_at or artifact.published_at
            event.actual_release_at = event.actual_release_at or artifact.published_at
        event.status = "completed"
        event.updated_at = _now()
        session.flush()
        return True
    return False


def _document_text_len(doc: Document | None) -> int:
    return transcript_document_text_len(doc)


def _document_is_readable_transcript(doc: Document | None) -> bool:
    return _shared_document_is_readable_transcript(doc)


def _transcript_artifact_has_readable_document(session: Session, artifact_id: int | None) -> bool:
    if artifact_id is None:
        return False
    artifact = session.get(IRArtifact, int(artifact_id))
    if artifact is None or str(artifact.artifact_type or "").strip().lower() != "transcript":
        return False
    if artifact.document_id is None:
        return False
    return _document_is_readable_transcript(session.get(Document, int(artifact.document_id)))


def _transcript_artifact_readability_payload(session: Session, artifact_id: int | None) -> dict[str, Any]:
    if artifact_id is None:
        return {"transcript_gap_reason": "missing_transcript_artifact"}
    artifact = session.get(IRArtifact, int(artifact_id))
    if artifact is None:
        return {"transcript_gap_reason": "missing_transcript_artifact", "transcript_artifact_id": int(artifact_id)}
    doc = session.get(Document, int(artifact.document_id)) if artifact.document_id is not None else None
    extra = doc.extra if doc is not None and isinstance(doc.extra, dict) else {}
    if _document_is_readable_transcript(doc):
        reason = "readable_transcript_artifact"
    elif doc is not None:
        reason = "linked_unreadable_transcript"
    else:
        reason = "linked_transcript_without_document"
    return {
        "transcript_gap_reason": reason,
        "transcript_artifact_id": int(artifact.id),
        "transcript_document_id": int(doc.id) if doc is not None else None,
        "transcript_document_text_len": _document_text_len(doc),
        "transcript_content_fetch": str(extra.get("transcript_content_fetch") or "").strip() or None,
    }


def _company_structured_fact_document_scope(session: Session, company: Company | None) -> dict[str, Any]:
    if company is None or company.id is None:
        return {
            "candidate_document_count": 0,
            "candidate_document_ids": [],
            "ignored_unreadable_transcript_count": 0,
            "ignored_unreadable_transcript_ids": [],
        }
    rows = (
        session.query(CompanyDocument, Document)
        .join(Document, Document.id == CompanyDocument.document_id)
        .filter(CompanyDocument.company_id == int(company.id))
        .filter(CompanyDocument.doc_type.in_(sorted(EARNINGS_DOC_TYPES)))
        .order_by(Document.published_at.desc().nullslast(), Document.ingested_at.desc().nullslast(), Document.id.desc())
        .all()
    )
    candidate_ids: list[int] = []
    ignored_transcript_ids: list[int] = []
    for company_doc, doc in rows:
        doc_type = str(company_doc.doc_type or "").strip().lower()
        if doc_type == "earnings_transcript" and not _document_is_readable_transcript(doc):
            ignored_transcript_ids.append(int(doc.id))
            continue
        candidate_ids.append(int(doc.id))
    return {
        "candidate_document_count": len(candidate_ids),
        "candidate_document_ids": candidate_ids[:25],
        "ignored_unreadable_transcript_count": len(ignored_transcript_ids),
        "ignored_unreadable_transcript_ids": ignored_transcript_ids[:25],
    }


def _parse_date(value: object) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except Exception:
        return None


def _parse_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        parsed = _parse_date(raw)
        return datetime.combine(parsed, time.min) if parsed else None


def _parse_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    raw = str(value).strip().replace(",", "")
    if not raw:
        return None
    try:
        return float(raw)
    except Exception:
        return None


def _parse_price_target(value: object) -> float | None:
    parsed = _parse_float(value)
    if parsed is None or parsed <= 0:
        return None
    return parsed


def _doc_url(document: Document | None) -> str | None:
    if document is None:
        return None
    return str(document.canonical_url or document.url or "").strip() or f"/docs/{int(document.id)}"


def _primary_ticker(session: Session, *, issuer: Issuer | None = None, company: Company | None = None) -> str | None:
    if issuer is not None:
        row = (
            session.query(IssuerIdentifier)
            .filter(IssuerIdentifier.issuer_id == int(issuer.id), IssuerIdentifier.id_type == "TICKER")
            .order_by(IssuerIdentifier.is_primary.desc(), IssuerIdentifier.id.asc())
            .first()
        )
        if row is not None and str(row.id_value or "").strip():
            return str(row.id_value).strip().upper()
    if company is not None and str(company.ticker or "").strip():
        return str(company.ticker).strip().upper()
    return None


def _company_for_issuer(session: Session, issuer: Issuer | None) -> Company | None:
    if issuer is None:
        return None
    ticker = _primary_ticker(session, issuer=issuer)
    q = session.query(Company).filter(Company.issuer_id == int(issuer.id))
    if ticker:
        row = q.filter(func.upper(func.coalesce(Company.ticker, "")) == ticker).order_by(Company.id.asc()).first()
        if row is not None:
            return row
    return q.order_by(Company.id.asc()).first()


def _unique_model_slug(session: Session, model: type[Any], base: object, *, default: str) -> str:
    slug = _slugify(base, default=default)
    candidate = slug
    idx = 2
    while session.query(model).filter(getattr(model, "slug") == candidate).first() is not None:
        candidate = f"{slug}-{idx}"
        idx += 1
    return candidate


def _instrument_metadata(instrument: ResearchInstrument) -> dict[str, Any]:
    return instrument.metadata_json if isinstance(instrument.metadata_json, dict) else {}


def _instrument_name_fields(instrument: ResearchInstrument) -> tuple[str, str, list[str]]:
    meta = _instrument_metadata(instrument)
    ibkr = meta.get("ibkr_watchlist_item") if isinstance(meta.get("ibkr_watchlist_item"), dict) else {}
    raw_values = [
        instrument.legal_name,
        ibkr.get("name") if isinstance(ibkr, dict) else None,
        meta.get("legal_name"),
        instrument.display_name,
        meta.get("display_name"),
        instrument.ticker,
    ]
    names = [str(value).strip() for value in raw_values if str(value or "").strip()]
    legal_name = names[0] if names else str(instrument.ticker or "").strip().upper()
    display_name = str(instrument.display_name or "").strip() or legal_name
    candidates: list[str] = []
    for value in [display_name, legal_name, *names]:
        if value and value not in candidates:
            candidates.append(value)
    return display_name, legal_name, candidates


def _instrument_name_match_score(instrument: ResearchInstrument, names: list[str]) -> float:
    if not names:
        return 0.0
    display_name, legal_name, candidates = _instrument_name_fields(instrument)
    values = [display_name, legal_name, instrument.ticker, *candidates]
    return max(
        (name_match_score(name, candidate) for name in names for candidate in values if str(candidate or "").strip()),
        default=0.0,
    )


def _research_instrument_for_ticker(session: Session, ticker: str, *, names: list[str] | None = None) -> ResearchInstrument | None:
    value = str(ticker or "").strip().upper()
    if not value:
        return None
    base_query = (
        session.query(ResearchInstrument)
        .filter(ResearchInstrument.is_public.is_(True))
        .filter(func.upper(func.coalesce(ResearchInstrument.security_type, "")).in_(_INSTRUMENT_DECISION_SECURITY_TYPES))
    )
    exact = (
        base_query
        .filter(
            or_(
                func.upper(func.coalesce(ResearchInstrument.ticker, "")) == value,
                func.upper(func.coalesce(ResearchInstrument.yahoo_symbol, "")) == value,
            )
        )
        .order_by(ResearchInstrument.id.asc())
        .first()
    )
    if exact is not None:
        return exact
    if "." not in value and "/" not in value:
        suffixed = (
            base_query
            .filter(func.upper(func.coalesce(ResearchInstrument.ticker, "")).like(f"{value}.%"))
            .order_by(ResearchInstrument.id.asc())
            .limit(10)
            .all()
        )
        if len(suffixed) == 1:
            return suffixed[0]
        if names:
            scored = [
                (_instrument_name_match_score(instrument, names), instrument)
                for instrument in suffixed
            ]
            matches = [(score, instrument) for score, instrument in scored if score >= 0.9]
            if len(matches) == 1:
                return matches[0][1]
    return None


def _research_instrument_for_yahoo_symbol_key(session: Session, ticker: str) -> ResearchInstrument | None:
    value = str(ticker or "").strip().upper()
    if not value:
        return None
    key = f"yahoo:{value}".lower()
    return (
        session.query(ResearchInstrument)
        .filter(func.lower(func.coalesce(ResearchInstrument.instrument_key, "")) == key)
        .order_by(ResearchInstrument.id.asc())
        .first()
    )


def _refresh_remote_yahoo_instrument(instrument: ResearchInstrument, exact: Any, value: str, now: datetime) -> ResearchInstrument:
    instrument.display_name = str(exact.name or instrument.display_name or value).strip()[:255] or instrument.display_name
    instrument.ticker = instrument.ticker or value
    instrument.exchange = str(exact.exchange or instrument.exchange or "").strip()[:64] or None
    instrument.security_type = str(instrument.security_type or "STK").strip().upper()
    instrument.is_public = True
    instrument.yahoo_symbol = instrument.yahoo_symbol or value
    provider = str(instrument.primary_provider or "").strip().lower()
    if not provider or provider == "local" or str(instrument.instrument_key or "").strip().lower().startswith("yahoo:"):
        instrument.primary_provider = "yahoo"
    instrument.metadata_json = dict(instrument.metadata_json or {}) | {"source": "yahoo_symbol_search_fallback"}
    instrument.updated_at = now
    return instrument


def _remote_symbol_lookup_query(ticker: str) -> bool:
    value = str(ticker or "").strip()
    if not value or len(value) > 24 or any(ch.isspace() for ch in value):
        return False
    if not all(ch.isalnum() or ch in ".-/" for ch in value):
        return False
    return True


def _ensure_research_instrument_from_yahoo_symbol(session: Session, ticker: str) -> ResearchInstrument | None:
    value = str(ticker or "").strip().upper()
    if not _remote_symbol_lookup_query(value):
        return None
    try:
        rows = search_symbols(value, user_agent=settings.discord_market_user_agent, timeout_s=5.0, limit=8)
    except Exception:
        return None
    exact = next((row for row in rows if str(row.symbol or "").strip().upper() == value), None)
    if exact is None:
        return None
    existing = _research_instrument_for_ticker(session, value) or _research_instrument_for_yahoo_symbol_key(session, value)
    if existing is not None:
        now = _now()
        _refresh_remote_yahoo_instrument(existing, exact, value, now)
        session.flush()
        return existing
    key = f"yahoo:{value}"
    now = _now()
    candidate = ResearchInstrument(
        instrument_key=key,
        display_name=str(exact.name or value).strip()[:255] or value,
        legal_name=None,
        ticker=value,
        exchange=str(exact.exchange or "").strip()[:64] or None,
        currency=None,
        country=None,
        security_type="STK",
        is_public=True,
        issuer_id=None,
        company_id=None,
        ibkr_conid=None,
        yahoo_symbol=value,
        primary_provider="yahoo",
        metadata_json={"source": "yahoo_symbol_search_fallback"},
        created_at=now,
        updated_at=now,
    )
    try:
        with session.begin_nested():
            session.add(candidate)
            session.flush()
        existing = candidate
    except IntegrityError:
        if candidate in session:
            session.expunge(candidate)
        existing = _research_instrument_for_ticker(session, value) or _research_instrument_for_yahoo_symbol_key(session, value)
        if existing is None:
            raise
        _refresh_remote_yahoo_instrument(existing, exact, value, now)
        session.flush()
    return existing


def _ensure_company_issuer_for_instrument(
    session: Session,
    instrument: ResearchInstrument,
) -> tuple[Company | None, Issuer | None]:
    ticker = str(instrument.ticker or "").strip().upper()
    if not ticker:
        return None, None
    security_type = str(instrument.security_type or "").strip().upper()
    if not bool(instrument.is_public) or security_type not in _INSTRUMENT_DECISION_SECURITY_TYPES:
        return None, None

    display_name, legal_name, names = _instrument_name_fields(instrument)
    now = _now()
    issuer = session.query(Issuer).filter_by(id=int(instrument.issuer_id)).first() if instrument.issuer_id is not None else None
    company = session.query(Company).filter_by(id=int(instrument.company_id)).first() if instrument.company_id is not None else None

    if issuer is None:
        ident = (
            session.query(IssuerIdentifier)
            .filter(IssuerIdentifier.id_type == "TICKER", func.upper(IssuerIdentifier.id_value) == ticker)
            .order_by(IssuerIdentifier.is_primary.desc(), IssuerIdentifier.id.asc())
            .first()
        )
        if ident is not None:
            issuer = session.query(Issuer).filter_by(id=int(ident.issuer_id)).first()

    if company is None:
        company = session.query(Company).filter(func.upper(func.coalesce(Company.ticker, "")) == ticker).order_by(Company.id.asc()).first()
    if issuer is None and company is not None and company.issuer_id is not None:
        issuer = session.query(Issuer).filter_by(id=int(company.issuer_id)).first()

    if issuer is None or company is None:
        name_company, name_issuer = _resolve_by_name(session, names)
        company = company or name_company
        issuer = issuer or name_issuer

    provenance = {
        "source": "research_instrument_fallback",
        "research_instrument_id": int(instrument.id),
        "instrument_key": instrument.instrument_key,
        "provisional_identity": True,
    }
    if issuer is None:
        issuer = Issuer(
            slug=_unique_model_slug(session, Issuer, legal_name or display_name or ticker, default="issuer"),
            lei=None,
            legal_name=legal_name or display_name or ticker,
            display_name=display_name or legal_name or ticker,
            jurisdiction=str(instrument.country or "").strip().upper() or None,
            entity_status="ACTIVE",
            sector=None,
            industry=None,
            created_at=now,
            updated_at=now,
            extra=provenance,
        )
        session.add(issuer)
        session.flush()

    if company is None:
        company = Company(
            slug=_unique_model_slug(session, Company, display_name or legal_name or ticker, default="company"),
            name=display_name or legal_name or ticker,
            ticker=ticker,
            exchange=str(instrument.exchange or "").strip() or None,
            region=str(instrument.country or "").strip().upper() or None,
            sector=None,
            industry=None,
            supply_chain_stage=None,
            created_at=now,
            issuer_id=int(issuer.id),
        )
        session.add(company)
        session.flush()
    elif company.issuer_id is None:
        company.issuer_id = int(issuer.id)
    if company is not None:
        if not str(company.ticker or "").strip():
            company.ticker = ticker
        if not str(company.exchange or "").strip() and str(instrument.exchange or "").strip():
            company.exchange = str(instrument.exchange or "").strip()[:64]
        if not str(company.region or "").strip() and str(instrument.country or "").strip():
            company.region = str(instrument.country or "").strip().upper()[:16]

    ticker_ident = (
        session.query(IssuerIdentifier)
        .filter(IssuerIdentifier.id_type == "TICKER", func.upper(IssuerIdentifier.id_value) == ticker)
        .order_by(IssuerIdentifier.is_primary.desc(), IssuerIdentifier.id.asc())
        .first()
    )
    if ticker_ident is None:
        session.add(
            IssuerIdentifier(
                issuer_id=int(issuer.id),
                id_type="TICKER",
                id_value=ticker,
                exchange=normalize_identifier_exchange(instrument.exchange),
                currency=str(instrument.currency or "").strip().upper() or None,
                is_primary=True,
                created_at=now,
            )
        )

    instrument.issuer_id = int(issuer.id)
    instrument.company_id = int(company.id)
    instrument.updated_at = now
    session.flush()
    if directory := session.info.get("universe_population_directory"):
        directory.register(company, issuer)
    return company, issuer


def _payload_name_candidates(payload: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in (
        "company_name",
        "issuer_name",
        "entity_name",
        "legal_name",
        "display_name",
        "name",
        "entity",
        "query",
    ):
        value = str(payload.get(key) or "").strip()
        if value:
            values.append(value)
    aliases = payload.get("aliases")
    if isinstance(aliases, list):
        values.extend(str(value).strip() for value in aliases if str(value or "").strip())
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        key = canonical_entity_key(value) or value.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def _resolve_issuer_by_identifier(session: Session, payload: dict[str, Any]) -> Issuer | None:
    for key, id_type in (
        ("lei", "LEI"),
        ("cik", "CIK"),
        ("isin", "ISIN"),
        ("cusip", "CUSIP"),
        ("figi", "FIGI"),
        ("sedol", "SEDOL"),
    ):
        value = str(payload.get(key) or "").strip().upper()
        if not value:
            continue
        if id_type == "LEI":
            row = session.query(Issuer).filter(func.upper(func.coalesce(Issuer.lei, "")) == value).order_by(Issuer.id.asc()).first()
            if row is not None:
                return row
        ident = (
            session.query(IssuerIdentifier)
            .filter(IssuerIdentifier.id_type == id_type, func.upper(IssuerIdentifier.id_value) == value)
            .order_by(IssuerIdentifier.is_primary.desc(), IssuerIdentifier.id.asc())
            .first()
        )
        if ident is not None:
            row = session.query(Issuer).filter_by(id=int(ident.issuer_id)).first()
            if row is not None:
                return row
    return None


def _resolve_subject_company_or_issuer(session: Session, subject: ResearchSubject | None) -> tuple[Company | None, Issuer | None]:
    if subject is None:
        return None, None
    company = session.query(Company).filter_by(id=int(subject.company_id)).first() if subject.company_id is not None else None
    issuer = session.query(Issuer).filter_by(id=int(subject.issuer_id)).first() if subject.issuer_id is not None else None
    if issuer is None and company is not None and company.issuer_id is not None:
        issuer = session.query(Issuer).filter_by(id=int(company.issuer_id)).first()
    if company is None and issuer is not None:
        company = _company_for_issuer(session, issuer)
    return company, issuer


def _resolve_by_name(session: Session, names: list[str]) -> tuple[Company | None, Issuer | None]:
    if not names:
        return None, None
    for name in names:
        raw = str(name or "").strip()
        key = canonical_entity_key(raw)
        if not raw or not key:
            continue
        company = (
            session.query(Company)
            .filter(or_(func.lower(Company.name) == raw.lower(), func.lower(Company.slug) == raw.lower()))
            .order_by(Company.id.asc())
            .first()
        )
        if company is not None:
            issuer = session.query(Issuer).filter_by(id=int(company.issuer_id)).first() if company.issuer_id is not None else None
            return company, issuer
        issuer = (
            session.query(Issuer)
            .filter(or_(func.lower(Issuer.display_name) == raw.lower(), func.lower(Issuer.legal_name) == raw.lower(), func.lower(Issuer.slug) == raw.lower()))
            .order_by(Issuer.id.asc())
            .first()
        )
        if issuer is not None:
            return _company_for_issuer(session, issuer), issuer
        subject = (
            session.query(ResearchSubject)
            .outerjoin(ResearchSubjectAlias, ResearchSubjectAlias.subject_id == ResearchSubject.id)
            .filter(
                or_(
                    func.lower(ResearchSubject.display_name) == raw.lower(),
                    func.lower(ResearchSubject.legal_name) == raw.lower(),
                    func.lower(ResearchSubject.slug) == raw.lower(),
                    func.lower(ResearchSubjectAlias.alias) == raw.lower(),
                )
            )
            .order_by(ResearchSubject.id.asc())
            .first()
        )
        if subject is not None:
            return _resolve_subject_company_or_issuer(session, subject)

    directory = session.info.get("universe_population_directory")
    prepared_inputs = [directory.input_name(str(name or "")) for name in names] if directory else []
    best: tuple[float, Company | None, Issuer | None] | None = None
    companies = directory.identities(Company) if directory else session.query(Company).order_by(Company.id.asc()).limit(5000).all()
    for company in companies:
        candidates = [company.name, company.slug, company.ticker]
        score = (
            max((_prepared_name_match_score(name, candidate) for name in prepared_inputs for candidate in directory.candidate_names(company)), default=0.0)
            if directory else max((name_match_score(name, candidate) for name in names for candidate in candidates if str(candidate or "").strip()), default=0.0)
        )
        if score >= 0.92 and (best is None or score > best[0]):
            issuer = session.query(Issuer).filter_by(id=int(company.issuer_id)).first() if company.issuer_id is not None else None
            best = (score, company, issuer)
    issuers = directory.identities(Issuer) if directory else session.query(Issuer).order_by(Issuer.id.asc()).limit(5000).all()
    for issuer in issuers:
        candidates = [issuer.display_name, issuer.legal_name, issuer.slug]
        if isinstance(issuer.extra, dict):
            candidates.extend(str(value) for value in (issuer.extra.get("aliases") or []) if str(value or "").strip())
        score = (
            max((_prepared_name_match_score(name, candidate) for name in prepared_inputs for candidate in directory.candidate_names(issuer)), default=0.0)
            if directory else max((name_match_score(name, candidate) for name in names for candidate in candidates if str(candidate or "").strip()), default=0.0)
        )
        if score >= 0.92 and (best is None or score > best[0]):
            best = (score, _company_for_issuer(session, issuer), issuer)
    return (best[1], best[2]) if best is not None else (None, None)


def resolve_company_or_issuer_from_payload(
    session: Session,
    payload: dict[str, Any],
) -> tuple[Company | None, Issuer | None]:
    company = None
    issuer = None
    company_id = payload.get("company_id")
    issuer_id = payload.get("issuer_id")
    if str(company_id or "").isdigit():
        company = session.query(Company).filter_by(id=int(company_id)).first()
    if str(issuer_id or "").isdigit():
        issuer = session.query(Issuer).filter_by(id=int(issuer_id)).first()
    ticker = str(payload.get("ticker") or payload.get("symbol") or "").strip().upper()
    if issuer is None and ticker:
        ident = (
            session.query(IssuerIdentifier)
            .filter(IssuerIdentifier.id_type == "TICKER", func.upper(IssuerIdentifier.id_value) == ticker)
            .order_by(IssuerIdentifier.is_primary.desc(), IssuerIdentifier.id.asc())
            .first()
        )
        if ident is not None:
            issuer = session.query(Issuer).filter_by(id=int(ident.issuer_id)).first()
    if company is None and ticker:
        company = session.query(Company).filter(func.upper(func.coalesce(Company.ticker, "")) == ticker).order_by(Company.id.asc()).first()
    if issuer is None:
        issuer = _resolve_issuer_by_identifier(session, payload)
    if company is None and issuer is None:
        name_company, name_issuer = _resolve_by_name(session, _payload_name_candidates(payload))
        company = company or name_company
        issuer = issuer or name_issuer
    if company is None and issuer is None and ticker:
        instrument = _research_instrument_for_ticker(session, ticker, names=_payload_name_candidates(payload))
        if instrument is None and bool(payload.get("allow_remote_symbol_lookup")):
            instrument = _ensure_research_instrument_from_yahoo_symbol(session, ticker)
        if instrument is not None:
            company, issuer = _ensure_company_issuer_for_instrument(session, instrument)
    if issuer is None and company is not None and company.issuer_id is not None:
        issuer = session.query(Issuer).filter_by(id=int(company.issuer_id)).first()
    if company is None and issuer is not None:
        company = _company_for_issuer(session, issuer)
    from thoth_terminal.identity_alias_service import require_consistent_identity
    require_consistent_identity(company=company, issuer=issuer)
    if ticker and company is not None and not str(company.ticker or "").strip():
        instrument = _research_instrument_for_ticker(session, ticker, names=_payload_name_candidates(payload))
        if instrument is not None:
            refreshed_company, refreshed_issuer = _ensure_company_issuer_for_instrument(session, instrument)
            company = refreshed_company or company
            issuer = refreshed_issuer or issuer
    return company, issuer


def _unique_subject_slug(session: Session, base: str, *, subject_id: int | None = None) -> str:
    slug = _slugify(base, default="subject")
    candidate = slug
    idx = 2
    while True:
        row = session.query(ResearchSubject).filter_by(slug=candidate).first()
        if row is None or (subject_id is not None and int(row.id) == int(subject_id)):
            return candidate
        candidate = f"{slug}-{idx}"
        idx += 1


def _add_subject_alias(session: Session, subject: ResearchSubject, alias: object, alias_type: str) -> None:
    value = str(alias or "").strip()
    if not value:
        return
    existing = (
        session.query(ResearchSubjectAlias)
        .filter_by(subject_id=int(subject.id), alias=value)
        .first()
    )
    if existing is None:
        session.add(ResearchSubjectAlias(subject_id=int(subject.id), alias=value[:255], alias_type=alias_type[:32], created_at=_now()))


def _add_subject_aliases(session: Session, subject: ResearchSubject, aliases: list[tuple[object, str]]) -> None:
    from thoth_terminal.identity_alias_service import valid_identity_aliases
    allowed = set(valid_identity_aliases(session, issuer_id=subject.issuer_id, aliases=[alias for alias, _ in aliases]))
    seen = {
        str(row[0] or "").strip().lower()
        for row in session.query(ResearchSubjectAlias.alias).filter_by(subject_id=int(subject.id)).all()
        if str(row[0] or "").strip()
    }
    for alias, alias_type in aliases:
        value = str(alias or "").strip()
        key = value.lower()
        if not value or value[:255] not in allowed or key in seen:
            continue
        seen.add(key)
        session.add(
            ResearchSubjectAlias(
                subject_id=int(subject.id),
                alias=value[:255],
                alias_type=str(alias_type or "name").strip()[:32] or "name",
                created_at=_now(),
            )
        )


def _add_external_id(session: Session, identity_id: int | None, provider: str, value: object, *, primary: bool = False) -> None:
    raw = str(value or "").strip()
    if identity_id is None or not raw:
        return
    row = (
        session.query(EntityExternalId)
        .filter_by(identity_id=int(identity_id), provider=str(provider), external_id=raw)
        .first()
    )
    now = _now()
    if row is None:
        row = EntityExternalId(
            identity_id=int(identity_id),
            provider=str(provider)[:64],
            external_id=raw[:255],
            is_primary=bool(primary),
            extra={},
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    else:
        row.is_primary = bool(primary) or bool(row.is_primary)
        row.updated_at = now
    session.flush()


def _resolve_stale_company_decision_subjects(
    session: Session,
    *,
    canonical_subject: ResearchSubject,
    company: Company | None,
    issuer: Issuer | None,
) -> dict[str, Any]:
    if company is None or company.id is None or canonical_subject.id is None:
        return {"subjects_merged": 0, "gaps_resolved": 0, "requests_rejected": 0}
    expected_issuer_id = int(issuer.id) if issuer is not None else None
    if expected_issuer_id is None and canonical_subject.issuer_id is None:
        return {"subjects_merged": 0, "gaps_resolved": 0, "requests_rejected": 0}
    stale_subjects = (
        session.query(ResearchSubject)
        .filter(ResearchSubject.company_id == int(company.id))
        .filter(ResearchSubject.id != int(canonical_subject.id))
        .filter(ResearchSubject.status.notin_(["merged", "archived"]))
        .order_by(ResearchSubject.id.asc())
        .all()
    )
    now = _now()
    stats = {"subjects_merged": 0, "gaps_resolved": 0, "requests_rejected": 0}
    canonical_names = [
        canonical_subject.display_name,
        canonical_subject.legal_name,
        canonical_subject.subject_key,
        company.name if company is not None else None,
        issuer.display_name if issuer is not None else None,
        issuer.legal_name if issuer is not None else None,
    ]
    for stale in stale_subjects:
        stale_issuer_id = int(stale.issuer_id) if stale.issuer_id is not None else None
        stale_company_id = int(stale.company_id) if stale.company_id is not None else None
        if stale_company_id != int(company.id):
            continue
        if expected_issuer_id is not None and stale_issuer_id == expected_issuer_id:
            continue
        stale_names = [stale.display_name, stale.legal_name, stale.subject_key]
        stale_issuer = session.get(Issuer, stale_issuer_id) if stale_issuer_id is not None else None
        if stale_issuer is not None:
            stale_names.extend([stale_issuer.display_name, stale_issuer.legal_name])
        if not any(name_match_score(left, right) >= 0.82 for left in stale_names for right in canonical_names):
            continue
        payload = dict(stale.extra or {}) if isinstance(stale.extra, dict) else {}
        payload["merged_into_subject_id"] = int(canonical_subject.id)
        payload["merged_into_subject_key"] = canonical_subject.subject_key
        payload["merged_by"] = "decision_subject_company_canonicalization"
        payload["merged_at"] = now.isoformat()
        stale.extra = payload
        stale.status = "merged"
        stale.updated_at = now
        stats["subjects_merged"] += 1

        gaps = (
            session.query(CoverageGap)
            .filter(CoverageGap.subject_id == int(stale.id), CoverageGap.owner_ref == "decision_spine")
            .all()
        )
        stale_gap_ids: set[int] = set()
        stale_gap_keys: set[str] = set()
        stale_request_keys: set[str] = set()
        for gap in gaps:
            if gap.id is not None:
                stale_gap_ids.add(int(gap.id))
            if gap.gap_key:
                stale_gap_keys.add(str(gap.gap_key))
            stale_request_keys.add(_gap_request_key(gap))
            if gap.status == "open":
                gap_payload = dict(gap.payload or {}) if isinstance(gap.payload, dict) else {}
                gap_payload["resolved_by"] = "decision_subject_merged"
                gap_payload["resolved_at"] = now.isoformat()
                gap_payload["merged_into_subject_id"] = int(canonical_subject.id)
                gap.payload = gap_payload
                gap.status = "resolved"
                gap.last_reviewed_at = now
                gap.updated_at = now
                stats["gaps_resolved"] += 1
        candidate_requests = (
            session.query(InstrumentRequest)
            .filter(InstrumentRequest.status.in_([REQUEST_STATUS_DRAFT, REQUEST_STATUS_REVIEW]))
            .filter(
                or_(
                    InstrumentRequest.request_key.in_(sorted(stale_request_keys)) if stale_request_keys else InstrumentRequest.id == -1,
                    InstrumentRequest.automation_source == "decision_spine",
                )
            )
            .all()
        )
        for request in candidate_requests:
            linked = request.linked_objects if isinstance(request.linked_objects, dict) else {}
            request_matches_stale_subject = False
            if request.request_key in stale_request_keys:
                request_matches_stale_subject = True
            try:
                request_matches_stale_subject = request_matches_stale_subject or int(linked.get("subject_id")) == int(stale.id)
            except Exception:
                pass
            try:
                request_matches_stale_subject = request_matches_stale_subject or int(linked.get("gap_id")) in stale_gap_ids
            except Exception:
                pass
            request_matches_stale_subject = request_matches_stale_subject or str(linked.get("gap_key") or "") in stale_gap_keys
            if request_matches_stale_subject:
                request.status = REQUEST_STATUS_REJECTED
                request.review_note = "Closed automatically because the decision subject was merged into the canonical company subject."
                request.reviewed_at = now
                request.updated_at = now
                stats["requests_rejected"] += 1
    if any(int(v or 0) for v in stats.values()):
        session.flush()
    return stats


def ensure_decision_subject(
    session: Session,
    *,
    company: Company | None = None,
    issuer: Issuer | None = None,
) -> ResearchSubject | None:
    if issuer is None and company is not None and company.issuer_id is not None:
        issuer = session.query(Issuer).filter_by(id=int(company.issuer_id)).first()
    if company is None and issuer is not None:
        company = _company_for_issuer(session, issuer)
    from thoth_terminal.identity_alias_service import require_consistent_identity
    require_consistent_identity(company=company, issuer=issuer)
    if issuer is not None:
        subject_key = f"issuer:{int(issuer.id)}"
        row = session.query(ResearchSubject).filter_by(subject_key=subject_key).first()
        display_name = str(issuer.display_name or issuer.legal_name or (company.name if company else "") or f"Issuer {issuer.id}").strip()
        legal_name = str(issuer.legal_name or display_name).strip() or None
        kind = "issuer"
    elif company is not None:
        subject_key = f"company:{int(company.id)}"
        row = session.query(ResearchSubject).filter_by(subject_key=subject_key).first()
        display_name = str(company.name or f"Company {company.id}").strip()
        legal_name = display_name
        kind = "company"
    else:
        return None
    now = _now()
    if row is None:
        row = ResearchSubject(
            slug=_unique_subject_slug(session, display_name),
            subject_key=subject_key,
            kind=kind,
            display_name=display_name,
            legal_name=legal_name,
            description=None,
            status="active",
            issuer_id=int(issuer.id) if issuer is not None else None,
            company_id=int(company.id) if company is not None else None,
            primary_entity_id=None,
            extra={},
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.flush()
    else:
        row.display_name = display_name or row.display_name
        row.legal_name = legal_name or row.legal_name
        row.issuer_id = int(issuer.id) if issuer is not None else row.issuer_id
        row.company_id = int(company.id) if company is not None else row.company_id
        row.updated_at = now
        session.add(row)
        session.flush()
    aliases = [display_name, legal_name, canonical_entity_key(display_name)]
    typed_aliases: list[tuple[object, str]] = []
    if company is not None:
        aliases.extend([company.name, company.slug, company.ticker])
    if issuer is not None:
        aliases.extend([issuer.display_name, issuer.legal_name, issuer.slug])
        for ident in session.query(IssuerIdentifier).filter_by(issuer_id=int(issuer.id)).all():
            aliases.append(ident.id_value)
            if ident.id_type:
                typed_aliases.append((ident.id_value, str(ident.id_type).lower()))
    typed_aliases.extend((alias, "name") for alias in aliases)
    _add_subject_aliases(session, row, typed_aliases)
    merge_stats = _resolve_stale_company_decision_subjects(
        session,
        canonical_subject=row,
        company=company,
        issuer=issuer,
    )
    if any(int(v or 0) for v in merge_stats.values()):
        extra = dict(row.extra or {}) if isinstance(row.extra, dict) else {}
        extra["stale_subject_merge"] = merge_stats
        extra["stale_subject_merge_at"] = now.isoformat()
        row.extra = extra
        row.updated_at = now
    identity = ensure_entity_identity(
        session,
        subject_row=row,
        company=company,
        issuer=issuer,
        aliases=[str(v) for v in aliases if str(v or "").strip()],
        external_ids=[],
    )
    if identity is not None and issuer is not None:
        for ident in session.query(IssuerIdentifier).filter_by(issuer_id=int(issuer.id)).all():
            _add_external_id(session, int(identity.id), str(ident.id_type or "identifier").lower(), ident.id_value, primary=bool(ident.is_primary))
    session.flush()
    return row


def _existing_decision_subject(
    session: Session,
    *,
    company: Company | None = None,
    issuer: Issuer | None = None,
) -> ResearchSubject | None:
    if issuer is None and company is not None and company.issuer_id is not None:
        issuer = session.query(Issuer).filter_by(id=int(company.issuer_id)).first()
    if issuer is not None:
        return session.query(ResearchSubject).filter_by(subject_key=f"issuer:{int(issuer.id)}").first()
    if company is not None:
        return session.query(ResearchSubject).filter_by(subject_key=f"company:{int(company.id)}").first()
    return None


def evidence_tier_for_document(document: Document | None) -> tuple[str, str, float]:
    source = document.source if document is not None and document.source is not None else None
    source_name = str(source.name if source else "").lower()
    category = str(source.category if source else "").lower()
    kind = str(document.kind if document else "").lower()
    extra = dict(document.extra or {}) if document is not None and isinstance(document.extra, dict) else {}
    source_config = source.config if source is not None and isinstance(source.config, dict) else {}
    connector = str(extra.get("connector") or source_config.get("connector") or "").lower()
    hay = " ".join([source_name, category, kind, connector])
    if any(token in hay for token in ("sec", "edgar", "sedar", "edinet", "filing", "opendart", "mops", "twse", "tdnet", "hkex", "krx")):
        return "official_regulatory", "official_feed", 0.96
    if any(token in hay for token in ("ir", "press_releases", "issuer", "company_disclosure", "earnings_release")):
        return "issuer_disclosure", "issuer_artifact", 0.9
    if "transcript" in hay:
        return "call_transcript", "transcript", 0.88
    if any(token in hay for token in ("x_list", "twitter", "discord", "analyst", "research_library", "local_research")):
        return "professional_analyst_media", "curated_media", 0.84
    if any(token in hay for token in ("bloomberg", "nikkei", "reuters", "wsj", "ft.com", "financial times")):
        return "professional_media", "media", 0.8
    return "unclassified", "document", 0.55


def _artifact_payload(session: Session, artifact_id: int | None) -> dict[str, Any] | None:
    if artifact_id is None:
        return None
    row = session.query(IRArtifact).filter_by(id=int(artifact_id)).first()
    if row is None:
        return None
    return {
        "id": int(row.id),
        "document_id": int(row.document_id) if row.document_id is not None else None,
        "artifact_type": row.artifact_type,
        "artifact_subtype": row.artifact_subtype,
        "title": row.title,
        "url": row.canonical_url or row.download_url or row.webcast_url,
        "published_at": row.published_at.isoformat() if row.published_at else None,
        "source_kind": (row.raw_meta or {}).get("source_kind") if isinstance(row.raw_meta, dict) else None,
    }


def earnings_event_packet(session: Session, event: EarningsEvent) -> dict[str, Any]:
    release_filing = session.query(SecuritiesFiling).filter_by(id=int(event.release_filing_id)).first() if event.release_filing_id else None
    release_disclosure = session.query(ExchangeDisclosure).filter_by(id=int(event.release_disclosure_id)).first() if event.release_disclosure_id else None
    release_document = session.query(Document).filter_by(id=int(release_filing.document_id)).first() if release_filing is not None and release_filing.document_id else None
    disclosure_document = (
        session.query(Document).filter_by(id=int(release_disclosure.document_id)).first()
        if release_disclosure is not None and release_disclosure.document_id
        else None
    )
    components = {
        "release_filing": {
            "id": int(release_filing.id),
            "document_id": int(release_filing.document_id) if release_filing.document_id is not None else None,
            "filing_type": release_filing.filing_type,
            "title": release_filing.title,
            "url": clean_url(release_filing.primary_document_url or release_filing.filing_url),
            "filed_at": release_filing.filed_at.isoformat() if release_filing.filed_at else None,
        }
        if release_filing is not None
        else None,
        "release_disclosure": {
            "id": int(release_disclosure.id),
            "document_id": int(release_disclosure.document_id) if release_disclosure.document_id is not None else None,
            "exchange": release_disclosure.exchange,
            "disclosure_type": release_disclosure.disclosure_type,
            "title": release_disclosure.title,
            "url": release_disclosure.disclosure_url,
            "announced_at": release_disclosure.announced_at.isoformat() if release_disclosure.announced_at else None,
        }
        if release_disclosure is not None
        else None,
        "release_artifact": _artifact_payload(session, event.release_artifact_id),
        "presentation": _artifact_payload(session, event.presentation_artifact_id),
        "transcript": _artifact_payload(session, event.transcript_artifact_id),
        "webcast": _artifact_payload(session, event.webcast_artifact_id),
    }
    expected = ["filing_or_exchange_disclosure", "earnings_release", "call_transcript"]
    present = {
        "filing": bool(components["release_filing"]),
        "exchange_disclosure": bool(components["release_disclosure"]),
        "release": bool(components["release_filing"] or components["release_disclosure"] or components["release_artifact"]),
        "presentation": bool(components["presentation"]),
        "transcript": bool(components["transcript"]),
        "webcast": bool(components["webcast"]),
    }
    review_state = {
        "filing_or_exchange_disclosure": bool(components["release_filing"] or components["release_disclosure"]),
        "earnings_release": bool(components["release_filing"] or components["release_disclosure"] or components["release_artifact"]),
        "call_transcript": bool(components["transcript"]),
        "call_access": bool(components["transcript"] or components["webcast"]),
    }
    missing = [key for key in expected if not review_state.get(key)]
    score = sum(1 for key in expected if review_state.get(key)) / max(1, len(expected))
    return {
        "id": int(event.id),
        "issuer_id": int(event.issuer_id),
        "fiscal_year": int(event.fiscal_year),
        "fiscal_quarter": int(event.fiscal_quarter) if event.fiscal_quarter is not None else None,
        "period_label": f"FY{event.fiscal_year} Q{event.fiscal_quarter}" if event.fiscal_quarter is not None else f"FY{event.fiscal_year}",
        "scheduled_date": event.scheduled_date.isoformat() if event.scheduled_date else None,
        "actual_release_at": event.actual_release_at.isoformat() if event.actual_release_at else None,
        "actual_call_at": event.actual_call_at.isoformat() if event.actual_call_at else None,
        "status": event.status,
        "date_provenance": event.date_provenance,
        "date_source": event.date_source,
        "components": components,
        "present": present,
        "review_state": review_state,
        "required_review_steps": expected,
        "missing": missing,
        "completeness_score": score,
        "release_document_evidence_tier": evidence_tier_for_document(release_document or disclosure_document)[0]
        if release_document is not None or disclosure_document is not None
        else None,
    }


def serialize_catalyst(row: InvestmentCatalyst) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "catalyst_key": row.catalyst_key,
        "subject_id": int(row.subject_id) if row.subject_id is not None else None,
        "issuer_id": int(row.issuer_id) if row.issuer_id is not None else None,
        "company_id": int(row.company_id) if row.company_id is not None else None,
        "title": row.title,
        "catalyst_kind": row.catalyst_kind,
        "event_date": row.event_date.isoformat() if row.event_date else None,
        "event_at": row.event_at.isoformat() if row.event_at else None,
        "status": row.status,
        "priority": int(row.priority or 0),
        "confidence": row.confidence,
        "evidence_tier": row.evidence_tier,
        "source_kind": row.source_kind,
        "summary": row.summary,
        "source_url": row.source_url,
        "earnings_event_id": int(row.earnings_event_id) if row.earnings_event_id is not None else None,
        "ipo_event_id": int(row.ipo_event_id) if row.ipo_event_id is not None else None,
        "payload": dict(row.payload or {}),
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def serialize_memo(row: InvestmentMemo) -> dict[str, Any]:
    payload = dict(row.payload or {})
    return {
        "id": int(row.id),
        "memo_key": row.memo_key,
        "subject_id": int(row.subject_id) if row.subject_id is not None else None,
        "issuer_id": int(row.issuer_id) if row.issuer_id is not None else None,
        "company_id": int(row.company_id) if row.company_id is not None else None,
        "title": row.title,
        "status": row.status,
        "rating": row.rating,
        "time_horizon": row.time_horizon,
        "summary_md": row.summary_md,
        "thesis_md": row.thesis_md,
        "valuation_md": row.valuation_md,
        "catalysts_md": row.catalysts_md,
        "risks_md": row.risks_md,
        "evidence_json": list(row.evidence_json or []),
        "conviction": payload.get("conviction"),
        "priority": payload.get("priority"),
        "inclusion_reason": payload.get("inclusion_reason") or payload.get("reason_for_inclusion"),
        "active_thesis": payload.get("active_thesis"),
        "next_catalyst": payload.get("next_catalyst"),
        "missing_data_warnings": list(payload.get("missing_data_warnings") or []) if isinstance(payload.get("missing_data_warnings"), list) else [],
        "payload": payload,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "last_reviewed_at": row.last_reviewed_at.isoformat() if row.last_reviewed_at else None,
    }


def _analyst_trust_label(*, payload: dict[str, Any], evidence_tier: str | None, source_kind: str | None) -> str | None:
    explicit = str(payload.get("trust_label") or payload.get("trust_bucket") or "").strip().lower()
    if explicit:
        return explicit[:64]
    hay = " ".join(
        str(payload.get(key) or "")
        for key in ("source_name", "outlet", "author", "source_url")
    ).lower()
    if "reuters.us" in hay or "reuters us" in hay or "wall street journal" in hay or re.search(r"\bwsj\b", hay):
        return "deprioritized_media"
    tier = str(evidence_tier or "").lower()
    kind = str(source_kind or "").lower()
    if "analyst" in tier or "analyst" in kind or "curated" in kind:
        return "trusted_professional"
    if "media" in tier:
        return "professional_media"
    return None


def serialize_analyst_evidence(row: AnalystEvidence) -> dict[str, Any]:
    payload = dict(row.payload or {})
    return {
        "id": int(row.id),
        "evidence_key": row.evidence_key,
        "subject_id": int(row.subject_id) if row.subject_id is not None else None,
        "issuer_id": int(row.issuer_id) if row.issuer_id is not None else None,
        "company_id": int(row.company_id) if row.company_id is not None else None,
        "source_id": int(row.source_id) if row.source_id is not None else None,
        "source_document_id": int(row.source_document_id) if row.source_document_id is not None else None,
        "evidence_kind": row.evidence_kind,
        "status": row.status,
        "source_name": row.source_name,
        "author": row.author,
        "outlet": row.outlet,
        "published_at": row.published_at.isoformat() if row.published_at else None,
        "action": row.action,
        "rating": row.rating,
        "prior_rating": row.prior_rating,
        "price_target": _parse_price_target(row.price_target),
        "prior_price_target": _parse_price_target(row.prior_price_target),
        "currency": row.currency,
        "target_horizon": row.target_horizon,
        "estimate_metric": row.estimate_metric,
        "estimate_period": row.estimate_period,
        "estimate_value": row.estimate_value,
        "estimate_unit": row.estimate_unit,
        "evidence_tier": row.evidence_tier,
        "source_kind": row.source_kind,
        "trust_label": row.trust_label,
        "rights_class": row.rights_class,
        "entitlement": payload.get("entitlement") or row.rights_class,
        "observed_source_actor_id": int(row.observed_source_actor_id) if row.observed_source_actor_id is not None else None,
        "underlying_source_actor_id": int(row.underlying_source_actor_id) if row.underlying_source_actor_id is not None else None,
        "source_identity_status": row.source_identity_status,
        "source_side": row.source_side,
        "source_role_class": row.source_role_class,
        "confidence": row.confidence,
        "contradiction_count": int(payload.get("contradiction_count") or 0) if str(payload.get("contradiction_count") or "0").lstrip("-").isdigit() else 0,
        "source_attribution": payload.get("source_attribution") or {},
        "forecast": payload.get("forecast") or {},
        "price_target_analysis": payload.get("price_target_analysis") or {},
        "source_relations": payload.get("source_relations") or [],
        "summary": row.summary,
        "source_url": row.source_url,
        "payload": payload,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def list_analyst_evidence(
    session: Session,
    *,
    subject_id: int | None = None,
    company_id: int | None = None,
    issuer_id: int | None = None,
    limit: int = 25,
) -> list[dict[str, Any]]:
    q = session.query(AnalystEvidence).filter(AnalystEvidence.status != "archived")
    filters = []
    if subject_id is not None:
        filters.append(AnalystEvidence.subject_id == int(subject_id))
    if company_id is not None:
        filters.append(AnalystEvidence.company_id == int(company_id))
    if issuer_id is not None:
        filters.append(AnalystEvidence.issuer_id == int(issuer_id))
    if filters:
        q = q.filter(or_(*filters))
    rows = (
        q.order_by(
            AnalystEvidence.published_at.desc().nullslast(),
            AnalystEvidence.updated_at.desc(),
            AnalystEvidence.id.desc(),
        )
        .limit(max(1, min(int(limit), 250)))
        .all()
    )
    return [serialize_analyst_evidence(row) for row in rows]


def _first_text(*values: object, limit: int | None = None) -> str | None:
    for value in values:
        text = " ".join(str(value or "").strip().split())
        if text:
            return text[:limit] if limit is not None else text
    return None


def _first_present(*values: object) -> object | None:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _json_dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _json_list(value: object) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _pct_delta(delta: float | None, base: float | None) -> float | None:
    if delta is None or base is None or base == 0:
        return None
    return delta / abs(base)


def _direction(delta: float | None, *, positive: str = "raised", negative: str = "lowered") -> str | None:
    if delta is None:
        return None
    if delta > 0:
        return positive
    if delta < 0:
        return negative
    return "unchanged"


def _metric_value_type(payload: dict[str, Any], forecast: dict[str, Any], *, metric_key: str | None, unit: str | None) -> str | None:
    explicit = _first_text(payload.get("metric_value_type"), payload.get("value_type"), forecast.get("metric_value_type"), forecast.get("value_type"), limit=64)
    if explicit:
        normalized = explicit.strip().lower().replace("-", "_").replace(" ", "_")
        aliases = {
            "dollar": "money",
            "dollars": "money",
            "usd": "money",
            "revenue": "money",
            "sales": "money",
            "shipments": "unit_count",
            "units": "unit_count",
            "number_shipped": "unit_count",
            "market_share": "percent_bounded",
            "share_percent": "percent_bounded",
            "bounded_percent": "percent_bounded",
            "percentage_bounded": "percent_bounded",
            "growth_percent": "percent_unbounded",
            "unbounded_percent": "percent_unbounded",
            "percentage_unbounded": "percent_unbounded",
        }
        return aliases.get(normalized, normalized)[:64]
    vocabulary_type = metric_value_type_for_key(metric_key)
    if vocabulary_type:
        return vocabulary_type
    unit_key = str(unit or "").strip().lower()
    metric = str(metric_key or "").strip().lower()
    if unit_key in {"usd", "$", "dollars", "dollar", "yen", "jpy", "krw", "eur"}:
        return "money"
    if unit_key in {"units", "pcs", "pieces", "shipments", "devices", "wafers", "servers", "accelerators", "gb", "million_gb"}:
        return "quantity"
    if unit_key in {"%", "percent", "percentage", "pct"}:
        if any(token in metric for token in ("share", "mix", "penetration", "attach", "utilization", "yield", "allocation")):
            return "percent_bounded"
        if any(token in metric for token in ("growth", "yoy", "qoq", "cagr", "increase", "decrease", "delta", "change")):
            return "percent_unbounded"
        return "percentage"
    return None


def _forecast_period_payload(payload: dict[str, Any], forecast: dict[str, Any], *, period: str | None) -> dict[str, Any]:
    raw_basis = _first_text(payload.get("period_basis"), payload.get("period_type"), forecast.get("period_basis"), forecast.get("period_type"), limit=64)
    period_text = str(period or "").strip()
    period_upper = period_text.upper()
    basis = str(raw_basis or "").strip().lower().replace("-", "_").replace(" ", "_")
    if basis in {"fy", "fiscal", "fiscal_year"}:
        basis = "fiscal_year"
    elif basis in {"cy", "calendar", "calendar_year"}:
        basis = "calendar_year"
    elif not basis:
        if re.search(r"\bFY\s*\d{2,4}", period_upper):
            basis = "fiscal_year"
        elif re.search(r"\bCY\s*\d{2,4}", period_upper):
            basis = "calendar_year"
        elif "FULL YEAR" in period_upper or period_upper.startswith("FY"):
            basis = "full_year"
    year = _parse_float(_first_present(payload.get("period_year"), payload.get("fiscal_year"), payload.get("calendar_year"), forecast.get("period_year")))
    if year is None:
        match = re.search(r"(20\d{2}|\d{2})(?:E)?", period_upper)
        if match:
            raw_year = int(match.group(1))
            year = float(2000 + raw_year if raw_year < 100 else raw_year)
    quarter = None
    quarter_value = _first_present(payload.get("period_quarter"), payload.get("fiscal_quarter"), payload.get("calendar_quarter"), forecast.get("period_quarter"))
    if quarter_value is not None:
        quarter = int(_parse_float(quarter_value) or 0) or None
    if quarter is None:
        match = re.search(r"\bQ([1-4])\b", period_upper)
        if match:
            quarter = int(match.group(1))
    half = None
    half_value = _first_present(payload.get("period_half"), payload.get("fiscal_half"), payload.get("calendar_half"), forecast.get("period_half"))
    if half_value is not None:
        half = int(_parse_float(half_value) or 0) or None
    if half is None:
        match = re.search(r"\b(?:H([12])|([12])H)\b", period_upper)
        if match:
            half = int(match.group(1) or match.group(2))
    is_full_year = bool(payload.get("is_full_year") or forecast.get("is_full_year"))
    if not is_full_year:
        is_full_year = basis in {"fiscal_year", "calendar_year", "full_year"} and quarter is None and half is None
    out = {
        "period_basis": basis or None,
        "period_year": int(year) if year is not None else None,
        "period_quarter": quarter,
        "period_half": half,
        "is_full_year": bool(is_full_year),
    }
    return {key: value for key, value in out.items() if value is not None}


def _value_constraint_payload(metric_value_type: str | None, value: float | None) -> dict[str, Any]:
    if metric_value_type != "percent_bounded":
        return {}
    warning = None
    if value is not None and (value < 0 or value > 100):
        warning = "percent_bounded_out_of_range"
    return {"value_min": 0, "value_max": 100, "constraint_warning": warning}


def _analyst_source_attribution(payload: dict[str, Any], source_document: Document | None) -> dict[str, Any]:
    existing = _json_dict(payload.get("source_attribution"))
    existing_observed = _json_dict(existing.get("observed_source"))
    existing_underlying = _json_dict(existing.get("underlying_source"))
    doc_extra = _json_dict(source_document.extra if source_document is not None else None)
    source_row = source_document.source if source_document is not None else None
    handle = _first_text(
        payload.get("observed_source_handle"),
        payload.get("source_handle"),
        existing_observed.get("handle"),
        doc_extra.get("handle"),
        limit=64,
    )
    observed_name = _first_text(
        payload.get("observed_source_name"),
        existing_observed.get("name"),
        payload.get("source_name"),
        payload.get("author"),
        source_document.author if source_document is not None else None,
        f"@{handle}" if handle else None,
        limit=255,
    )
    observed_source = {
        "name": observed_name,
        "handle": handle.lstrip("@") if handle else None,
        "kind": _first_text(
            payload.get("observed_source_kind"),
            existing_observed.get("kind"),
            payload.get("source_kind"),
            source_row.category if source_row is not None else None,
            limit=64,
        ),
        "source_id": int(source_row.id) if source_row is not None and source_row.id is not None else None,
        "source_name": source_row.name if source_row is not None else None,
    }
    underlying_source = {
        "name": _first_text(
            payload.get("underlying_source_name"),
            payload.get("original_source_name"),
            payload.get("research_source_name"),
            payload.get("estimate_owner"),
            payload.get("source_owner"),
            payload.get("institution"),
            existing_underlying.get("name"),
            limit=255,
        ),
        "firm": _first_text(
            payload.get("underlying_firm"),
            payload.get("firm"),
            payload.get("research_firm"),
            payload.get("estimate_owner"),
            payload.get("source_firm"),
            payload.get("estimate_firm"),
            payload.get("institution"),
            payload.get("source_owner"),
            existing_underlying.get("firm"),
            payload.get("outlet"),
            limit=255,
        ),
        "analyst": _first_text(
            payload.get("underlying_analyst"),
            payload.get("analyst"),
            existing_underlying.get("analyst"),
            limit=255,
        ),
        "desk": _first_text(payload.get("desk"), payload.get("desk_region"), existing_underlying.get("desk"), limit=128),
        "region": _first_text(payload.get("region"), existing_underlying.get("region"), limit=64),
    }
    chain = [dict(row) for row in _json_list(payload.get("source_chain") or existing.get("source_chain")) if isinstance(row, dict)]
    if not chain:
        if any(observed_source.values()):
            chain.append({"role": "observed_publisher", **{k: v for k, v in observed_source.items() if v is not None}})
        if any(underlying_source.values()):
            chain.append({"role": "underlying_research", **{k: v for k, v in underlying_source.items() if v is not None}})
        if source_document is not None:
            chain.append(
                {
                    "role": "ingested_document",
                    "document_id": int(source_document.id),
                    "source_id": int(source_document.source_id),
                    "source_name": source_row.name if source_row is not None else None,
                    "url": source_document.url or source_document.canonical_url,
                }
            )
    return {
        "observed_source": {k: v for k, v in observed_source.items() if v is not None},
        "underlying_source": {k: v for k, v in underlying_source.items() if v is not None},
        "source_chain": chain[:8],
        "attribution_status": _first_text(payload.get("attribution_status"), existing.get("attribution_status"), limit=64)
        or ("wrapped" if observed_source.get("name") and any(underlying_source.values()) else "direct"),
        "access_path": _first_text(payload.get("access_path"), existing.get("access_path"), limit=128),
        "language": _first_text(payload.get("language"), source_document.language if source_document is not None else None, limit=16),
        "translated": bool(payload.get("translated") or (source_document is not None and source_document.translation_en)),
    }


def _analyst_forecast_payload(payload: dict[str, Any]) -> dict[str, Any]:
    forecast = _json_dict(payload.get("forecast") or payload.get("projection"))
    value = _parse_float(_first_present(payload.get("forecast_value"), payload.get("estimate_value"), forecast.get("value"), forecast.get("forecast_value")))
    prior_value = _parse_float(
        _first_present(payload.get("prior_forecast_value"), payload.get("prior_estimate_value"), forecast.get("prior_value"), forecast.get("prior_forecast_value"))
    )
    actual_value = _parse_float(_first_present(payload.get("actual_value"), forecast.get("actual_value"), forecast.get("realized_value")))
    delta_vs_prior = value - prior_value if value is not None and prior_value is not None else None
    actual_vs_forecast = actual_value - value if actual_value is not None and value is not None else None
    status = _first_text(payload.get("forecast_status"), forecast.get("status"), forecast.get("outcome"), limit=64)
    if not status and actual_vs_forecast is not None:
        status = "exceeded" if actual_vs_forecast > 0 else "missed" if actual_vs_forecast < 0 else "met"
    metric_key = _first_text(
        payload.get("metric_key"),
        payload.get("estimate_metric"),
        forecast.get("metric_key"),
        forecast.get("metric"),
        forecast.get("metric_name"),
        limit=128,
    )
    metric_key = normalize_metric_key(metric_key) or metric_key
    period = _first_text(
        payload.get("forecast_period"),
        payload.get("estimate_period"),
        payload.get("target_period"),
        forecast.get("period"),
        forecast.get("target_period"),
        limit=64,
    )
    unit = _first_text(payload.get("estimate_unit"), payload.get("unit"), forecast.get("unit"), limit=64)
    metric_value_type = _metric_value_type(payload, forecast, metric_key=metric_key, unit=unit)
    out = {
        "metric_key": metric_key,
        "metric_label": _first_text(payload.get("metric_label"), forecast.get("metric_label"), forecast.get("metric_name"), limit=255),
        "period": period,
        "scenario": _first_text(payload.get("scenario"), forecast.get("scenario"), limit=128),
        "product_family": _first_text(payload.get("product_family"), forecast.get("product_family"), forecast.get("product"), limit=128),
        "segment": _first_text(payload.get("segment"), forecast.get("segment"), forecast.get("product"), limit=128),
        "region": _first_text(payload.get("forecast_region"), forecast.get("region"), limit=64),
        "metric_value_type": metric_value_type,
        "value": value,
        "range_low": _parse_float(forecast.get("range_low")),
        "range_high": _parse_float(forecast.get("range_high")),
        "value_basis": _first_text(forecast.get("value_basis"), limit=32),
        "prior_value": prior_value,
        "delta_vs_prior": delta_vs_prior,
        "delta_vs_prior_pct": _pct_delta(delta_vs_prior, prior_value),
        "revision_direction": _direction(delta_vs_prior),
        "actual_value": actual_value,
        "actual_date": (_parse_date(payload.get("actual_date") or forecast.get("actual_date") or forecast.get("realized_date")) or None),
        "actual_vs_forecast_delta": actual_vs_forecast,
        "actual_vs_forecast_pct": _pct_delta(actual_vs_forecast, value),
        "forecast_status": status,
        "unit": unit,
        "currency": _first_text(payload.get("currency"), forecast.get("currency"), limit=8),
        "basis": _first_text(payload.get("basis"), forecast.get("basis"), limit=128),
        "start_date": _parse_date(payload.get("forecast_start_date") or forecast.get("start_date")),
        "end_date": _parse_date(payload.get("forecast_end_date") or payload.get("target_date") or forecast.get("end_date") or forecast.get("target_date")),
    }
    out.update(_forecast_period_payload(payload, forecast, period=period))
    out.update(_value_constraint_payload(metric_value_type, value))
    normalized: dict[str, Any] = {}
    for key, value_obj in out.items():
        if isinstance(value_obj, date):
            normalized[key] = value_obj.isoformat()
        elif value_obj is not None:
            normalized[key] = value_obj
    return normalized


def _analyst_price_target_payload(payload: dict[str, Any]) -> dict[str, Any]:
    target = _parse_price_target(payload.get("price_target"))
    prior = _parse_price_target(payload.get("prior_price_target"))
    delta = target - prior if target is not None and prior is not None else None
    return {
        "price_target": target,
        "prior_price_target": prior,
        "price_target_delta": delta,
        "price_target_delta_pct": _pct_delta(delta, prior),
        "price_target_direction": _direction(delta),
        "target_horizon": _first_text(payload.get("target_horizon"), limit=64),
        "currency": _first_text(payload.get("currency"), limit=8),
    }


_LOCAL_AGENT_WEAK_ANALYST_KINDS = {
    "media_claim",
    "news_claim",
    "social_claim",
    "wrapper_claim",
}
_LOCAL_AGENT_WEAK_SOURCE_MARKERS = (
    "media",
    "news",
    "social",
    "blog",
    "forum",
    "wire",
)
_ANALYST_RATING_ACTIONS = {
    "upgrade",
    "upgraded",
    "downgrade",
    "downgraded",
    "initiate",
    "initiated",
    "reiterate",
    "reiterated",
    "maintain",
    "maintained",
    "raise",
    "raised",
    "lower",
    "lowered",
    "cut",
    "trim",
    "trimmed",
    "resume",
    "resumed",
}


def _has_rating_or_target_signal(payload: dict[str, Any], price_target_payload: dict[str, Any]) -> bool:
    if any(price_target_payload.get(field) is not None for field in ("price_target", "prior_price_target", "price_target_delta")):
        return True
    if any(bool(str(payload.get(field) or "").strip()) for field in ("rating", "prior_rating", "price_target", "prior_price_target")):
        return True
    return False


def _has_modelable_forecast_signal(payload: dict[str, Any], forecast_payload: dict[str, Any]) -> bool:
    metric_key = _first_text(
        forecast_payload.get("metric_key"),
        payload.get("metric_key"),
        payload.get("estimate_metric"),
        limit=128,
    )
    if not metric_key:
        return False
    if any(
        value is not None and str(value).strip() != ""
        for value in (
            forecast_payload.get("value"),
            forecast_payload.get("actual_value"),
            payload.get("forecast_value"),
            payload.get("estimate_value"),
            forecast_payload.get("start_date"),
            forecast_payload.get("end_date"),
            payload.get("target_date"),
            payload.get("actual_date"),
        )
    ):
        return True
    status = str(forecast_payload.get("forecast_status") or "").strip()
    metric_type = str(forecast_payload.get("metric_value_type") or "").strip().lower()
    metric_text = str(metric_key or "").strip().lower()
    if status:
        if re.search(r"\d", status):
            return True
        if metric_type in {"status", "event", "date"}:
            return True
        if any(token in metric_text for token in ("status", "qualified", "qualification", "delivery", "start", "lead_time", "availability")):
            return True
    return False


def _source_is_weak_for_local_agent(evidence_tier: str | None, source_kind: str | None) -> bool:
    text = f"{evidence_tier or ''} {source_kind or ''}".lower()
    return any(marker in text for marker in _LOCAL_AGENT_WEAK_SOURCE_MARKERS)


def _reject_weak_local_agent_analyst_evidence(
    *,
    actor: str,
    evidence_kind: str,
    evidence_tier: str | None,
    source_kind: str | None,
    payload: dict[str, Any],
    forecast_payload: dict[str, Any],
    price_target_payload: dict[str, Any],
) -> None:
    if str(actor or "").strip().lower() != "local_agent":
        return
    if forecast_payload.get("constraint_warning"):
        raise ValueError(
            "analyst_evidence_invalid_value: bounded percentage/share estimates must stay between 0 and 100. "
            "Fix the value or route the claim to a non-consensus object."
        )
    if _has_rating_or_target_signal(payload, price_target_payload):
        return
    if _has_modelable_forecast_signal(payload, forecast_payload):
        return
    raise ValueError(
        "analyst_evidence_not_modelable: local-agent analyst evidence must include a concrete estimate value, "
        "dated/status forecast vector, rating, or price target. Source-only or direction-only claims such as "
        "'prices rise' or unquantified undersupply should be written as a catalyst/event/fact and linked to the "
        "subject, product, or category instead."
    )


def _reject_incomplete_local_agent_consensus_identity(
    *,
    actor: str,
    subject: ResearchSubject | None,
    company: Company | None,
    issuer: Issuer | None,
    evidence_kind: str,
    payload: dict[str, Any],
    source_attribution: dict[str, Any],
    forecast_payload: dict[str, Any],
    price_target_payload: dict[str, Any],
) -> None:
    if str(actor or "").strip().lower() != "local_agent":
        return
    has_numeric_value = _parse_float(forecast_payload.get("value")) is not None
    has_rating_or_target = _has_rating_or_target_signal(payload, price_target_payload)
    if not has_numeric_value and not has_rating_or_target:
        return
    if subject is None and company is None and issuer is None:
        raise ValueError(
            "analyst_evidence_unresolved_subject: numeric estimates, ratings, and price targets require a resolved "
            "research subject, company, or issuer before they can enter consensus."
        )
    underlying = _json_dict(source_attribution.get("underlying_source"))
    estimate_owner = _first_text(
        payload.get("estimate_owner"),
        payload.get("underlying_firm"),
        payload.get("source_owner"),
        underlying.get("firm"),
        underlying.get("name"),
        payload.get("source_name") if str(source_attribution.get("attribution_status") or "direct") == "direct" else None,
    )
    if not estimate_owner:
        raise ValueError(
            "analyst_evidence_missing_estimate_owner: consensus evidence must identify the firm, issuer, research "
            "organization, or person that owns the estimate. The ingestion source or reposting account is not enough."
        )
    evidence_kind_l = str(evidence_kind or "").strip().lower()
    period = _first_text(forecast_payload.get("period"), payload.get("estimate_period"), payload.get("forecast_period"))
    if has_numeric_value and evidence_kind_l not in {"price_target", "rating", "rating_action"} and not period:
        raise ValueError(
            "analyst_evidence_missing_period: numeric consensus estimates require an explicit fiscal, calendar, "
            "quarterly, or dated period. Do not place undated estimates into the 'latest' aggregate."
        )


def _analyst_dataset_columns() -> list[dict[str, Any]]:
    return [
        {"key": "published_at", "label": "Published", "data_type": "date", "role": "timestamp"},
        {"key": "evidence_id", "label": "Evidence ID", "data_type": "number", "role": "id"},
        {"key": "evidence_key", "label": "Evidence Key", "data_type": "text", "role": "id"},
        {"key": "evidence_kind", "label": "Kind", "data_type": "text", "role": "dimension"},
        {"key": "source_name", "label": "Source", "data_type": "text", "role": "dimension"},
        {"key": "author", "label": "Author", "data_type": "text", "role": "dimension"},
        {"key": "outlet", "label": "Outlet", "data_type": "text", "role": "dimension"},
        {"key": "observed_source_name", "label": "Observed Source", "data_type": "text", "role": "dimension"},
        {"key": "observed_source_handle", "label": "Observed Handle", "data_type": "text", "role": "dimension"},
        {"key": "observed_source_actor_id", "label": "Observed Actor ID", "data_type": "number", "role": "dimension"},
        {"key": "observed_source_actor_type", "label": "Observed Actor Type", "data_type": "text", "role": "dimension"},
        {"key": "underlying_firm", "label": "Underlying Firm", "data_type": "text", "role": "dimension"},
        {"key": "underlying_source_name", "label": "Underlying Source", "data_type": "text", "role": "dimension"},
        {"key": "underlying_source_actor_id", "label": "Underlying Actor ID", "data_type": "number", "role": "dimension"},
        {"key": "underlying_source_actor_type", "label": "Underlying Actor Type", "data_type": "text", "role": "dimension"},
        {"key": "source_identity_status", "label": "Identity Status", "data_type": "text", "role": "dimension"},
        {"key": "source_side", "label": "Source Side", "data_type": "text", "role": "dimension"},
        {"key": "source_role_class", "label": "Source Role", "data_type": "text", "role": "dimension"},
        {"key": "metric_key", "label": "Metric Key", "data_type": "text", "role": "dimension"},
        {"key": "metric_label", "label": "Metric", "data_type": "text", "role": "dimension"},
        {"key": "forecast_period", "label": "Forecast Period", "data_type": "text", "role": "dimension"},
        {"key": "period_basis", "label": "Period Basis", "data_type": "text", "role": "dimension"},
        {"key": "period_year", "label": "Period Year", "data_type": "number", "role": "dimension"},
        {"key": "period_quarter", "label": "Period Quarter", "data_type": "number", "role": "dimension"},
        {"key": "period_half", "label": "Period Half", "data_type": "number", "role": "dimension"},
        {"key": "is_full_year", "label": "Full Year", "data_type": "bool", "role": "dimension"},
        {"key": "scenario", "label": "Scenario", "data_type": "text", "role": "dimension"},
        {"key": "metric_value_type", "label": "Value Type", "data_type": "text", "role": "dimension"},
        {"key": "forecast_value", "label": "Forecast", "data_type": "number", "role": "metric"},
        {"key": "range_low", "label": "Forecast Low", "data_type": "number", "role": "metric"},
        {"key": "range_high", "label": "Forecast High", "data_type": "number", "role": "metric"},
        {"key": "value_basis", "label": "Forecast Value Basis", "data_type": "text", "role": "dimension"},
        {"key": "prior_forecast_value", "label": "Prior Forecast", "data_type": "number", "role": "metric"},
        {"key": "delta_vs_prior", "label": "Delta vs Prior", "data_type": "number", "role": "metric"},
        {"key": "delta_vs_prior_pct", "label": "Delta vs Prior %", "data_type": "number", "role": "metric"},
        {"key": "revision_direction", "label": "Revision", "data_type": "text", "role": "dimension"},
        {"key": "actual_value", "label": "Actual", "data_type": "number", "role": "metric"},
        {"key": "actual_date", "label": "Actual Date", "data_type": "date", "role": "timestamp"},
        {"key": "actual_vs_forecast_delta", "label": "Actual vs Forecast", "data_type": "number", "role": "metric"},
        {"key": "actual_vs_forecast_pct", "label": "Actual vs Forecast %", "data_type": "number", "role": "metric"},
        {"key": "forecast_status", "label": "Forecast Status", "data_type": "text", "role": "dimension"},
        {"key": "value_min", "label": "Value Min", "data_type": "number", "role": "dimension"},
        {"key": "value_max", "label": "Value Max", "data_type": "number", "role": "dimension"},
        {"key": "constraint_warning", "label": "Constraint Warning", "data_type": "text", "role": "dimension"},
        {"key": "unit", "label": "Unit", "data_type": "text", "role": "dimension"},
        {"key": "currency", "label": "Currency", "data_type": "text", "role": "dimension"},
        {"key": "rating", "label": "Rating", "data_type": "text", "role": "dimension"},
        {"key": "prior_rating", "label": "Prior Rating", "data_type": "text", "role": "dimension"},
        {"key": "price_target", "label": "Target", "data_type": "number", "role": "metric"},
        {"key": "prior_price_target", "label": "Prior Target", "data_type": "number", "role": "metric"},
        {"key": "price_target_delta", "label": "Target Delta", "data_type": "number", "role": "metric"},
        {"key": "action", "label": "Action", "data_type": "text", "role": "dimension"},
        {"key": "evidence_tier", "label": "Evidence Tier", "data_type": "text", "role": "dimension"},
        {"key": "trust_label", "label": "Trust Label", "data_type": "text", "role": "dimension"},
        {"key": "rights_class", "label": "Rights", "data_type": "text", "role": "dimension"},
        {"key": "source_document_id", "label": "Source Doc", "data_type": "number", "role": "dimension"},
        {"key": "source_url", "label": "Source URL", "data_type": "text", "role": "dimension"},
        {"key": "summary", "label": "Summary", "data_type": "text", "role": "dimension"},
        {"key": "source_chain", "label": "Source Chain", "data_type": "json", "role": "dimension"},
    ]


def _analyst_dataset_row(row: AnalystEvidence) -> dict[str, Any]:
    payload = dict(row.payload or {})
    attribution = _json_dict(payload.get("source_attribution"))
    observed = _json_dict(attribution.get("observed_source"))
    underlying = _json_dict(attribution.get("underlying_source"))
    identity = _json_dict(attribution.get("identity"))
    forecast = _json_dict(payload.get("forecast"))
    price = _json_dict(payload.get("price_target_analysis"))
    values = {
        "published_at": row.published_at.date().isoformat() if row.published_at else None,
        "evidence_id": int(row.id),
        "evidence_key": row.evidence_key,
        "evidence_kind": row.evidence_kind,
        "source_name": row.source_name,
        "author": row.author,
        "outlet": row.outlet,
        "observed_source_name": observed.get("name"),
        "observed_source_handle": observed.get("handle"),
        "observed_source_actor_id": int(row.observed_source_actor_id) if row.observed_source_actor_id is not None else None,
        "observed_source_actor_type": observed.get("actor_type") or _json_dict(identity.get("observed_actor")).get("actor_type"),
        "underlying_firm": underlying.get("firm"),
        "underlying_source_name": underlying.get("name"),
        "underlying_source_actor_id": int(row.underlying_source_actor_id) if row.underlying_source_actor_id is not None else None,
        "underlying_source_actor_type": underlying.get("actor_type") or _json_dict(identity.get("underlying_actor")).get("actor_type"),
        "source_identity_status": row.source_identity_status,
        "source_side": row.source_side,
        "source_role_class": row.source_role_class,
        "metric_key": forecast.get("metric_key") or row.estimate_metric,
        "metric_label": forecast.get("metric_label"),
        "forecast_period": forecast.get("period") or row.estimate_period,
        "period_basis": forecast.get("period_basis"),
        "period_year": forecast.get("period_year"),
        "period_quarter": forecast.get("period_quarter"),
        "period_half": forecast.get("period_half"),
        "is_full_year": forecast.get("is_full_year"),
        "scenario": forecast.get("scenario"),
        "metric_value_type": forecast.get("metric_value_type"),
        "forecast_value": forecast.get("value") if forecast.get("value") is not None else row.estimate_value,
        "range_low": forecast.get("range_low"),
        "range_high": forecast.get("range_high"),
        "value_basis": forecast.get("value_basis"),
        "prior_forecast_value": forecast.get("prior_value"),
        "delta_vs_prior": forecast.get("delta_vs_prior"),
        "delta_vs_prior_pct": forecast.get("delta_vs_prior_pct"),
        "revision_direction": forecast.get("revision_direction"),
        "actual_value": forecast.get("actual_value"),
        "actual_date": forecast.get("actual_date"),
        "actual_vs_forecast_delta": forecast.get("actual_vs_forecast_delta"),
        "actual_vs_forecast_pct": forecast.get("actual_vs_forecast_pct"),
        "forecast_status": forecast.get("forecast_status"),
        "value_min": forecast.get("value_min"),
        "value_max": forecast.get("value_max"),
        "constraint_warning": forecast.get("constraint_warning"),
        "unit": forecast.get("unit") or row.estimate_unit,
        "currency": forecast.get("currency") or row.currency,
        "rating": row.rating,
        "prior_rating": row.prior_rating,
        "price_target": _parse_price_target(row.price_target),
        "prior_price_target": _parse_price_target(row.prior_price_target),
        "price_target_delta": price.get("price_target_delta"),
        "action": row.action,
        "evidence_tier": row.evidence_tier,
        "trust_label": row.trust_label,
        "rights_class": row.rights_class,
        "source_document_id": int(row.source_document_id) if row.source_document_id is not None else None,
        "source_url": row.source_url,
        "summary": row.summary,
        "source_chain": attribution.get("source_chain") or [],
    }
    return {"row_key": row.evidence_key, "values": values}


def materialize_analyst_evidence_dataset(
    session: Session,
    *,
    subject: ResearchSubject | None = None,
    company: Company | None = None,
    issuer: Issuer | None = None,
) -> Dataset | None:
    filters = []
    if subject is not None:
        filters.append(AnalystEvidence.subject_id == int(subject.id))
    if company is not None:
        filters.append(AnalystEvidence.company_id == int(company.id))
    if issuer is not None:
        filters.append(AnalystEvidence.issuer_id == int(issuer.id))
    if not filters:
        return None
    rows = (
        session.query(AnalystEvidence)
        .filter(AnalystEvidence.status != "archived")
        .filter(or_(*filters))
        .order_by(AnalystEvidence.published_at.desc().nullslast(), AnalystEvidence.updated_at.desc(), AnalystEvidence.id.desc())
        .limit(5000)
        .all()
    )
    display_name = (
        company.name
        if company is not None
        else issuer.display_name
        if issuer is not None
        else subject.display_name
        if subject is not None
        else "Subject"
    )
    slug_seed = (
        company.slug
        if company is not None
        else issuer.slug
        if issuer is not None
        else subject.slug
        if subject is not None
        else display_name
    )
    slug = f"{_slugify(slug_seed, default='subject')}-analyst-evidence"
    dataset = session.query(Dataset).filter_by(slug=slug).first()
    if not rows and dataset is None:
        return None
    extra = {
        "decision_spine": True,
        "dataset_role": "analyst_evidence_spine",
        "subject_id": int(subject.id) if subject is not None else None,
        "subject_key": subject.subject_key if subject is not None else None,
        "company_id": int(company.id) if company is not None else None,
        "issuer_id": int(issuer.id) if issuer is not None else None,
        "provenance_summary": "Materialized from durable AnalystEvidence rows. Use row source chains for wrapper-vs-underlying attribution.",
        "evidence_tier_summary": ", ".join(sorted({str(row.evidence_tier) for row in rows if row.evidence_tier})) or None,
        "rights_note": "Do not store full paid report text here; persist only metadata, numeric claims, attribution, and source document ids.",
        "row_count": len(rows),
    }
    if dataset is None:
        dataset = create_dataset(
            session,
            title=f"{display_name} Analyst Evidence",
            description="Attributed sell-side, specialist analyst, and professional-media evidence normalized into forecast, revision, actual-vs-forecast, rating, and target rows.",
            category="decision_spine",
            family="analyst_evidence",
            status="active",
            source_kind="analyst_evidence",
            slug=slug,
            columns=[],
            rows=[],
            label="Initial analyst evidence materialization",
            extra=extra,
            source_event="analyst_evidence",
        )
    else:
        dataset.title = f"{display_name} Analyst Evidence"
        dataset.description = "Attributed sell-side, specialist analyst, and professional-media evidence normalized into forecast, revision, actual-vs-forecast, rating, and target rows."
        dataset.category = "decision_spine"
        dataset.family = "analyst_evidence"
        dataset.source_kind = "analyst_evidence"
        dataset.status = "active"
        merged = dict(dataset.extra or {})
        merged.update(extra)
        dataset.extra = merged
        dataset.updated_at = _now()
        session.add(dataset)
        session.flush()
    columns = _analyst_dataset_columns()
    replace_dataset_draft(
        session,
        dataset,
        columns=columns,
        rows=[_analyst_dataset_row(row) for row in rows],
        label="Analyst evidence spine",
        extra=dict(dataset.extra or {}),
        source_event="analyst_evidence",
    )
    visible_columns = [col["key"] for col in columns]
    create_dataset_view(
        session,
        dataset,
        name="Default analyst view",
        slug="default-analyst-view",
        description="Latest analyst evidence, with attribution and forecast deltas.",
        columns=visible_columns,
        sort_spec=[{"column": "published_at", "direction": "desc"}],
        extra={"auto_created": True, "source": "decision_spine"},
    )
    create_dataset_view(
        session,
        dataset,
        name="Forecast and revisions",
        slug="forecast-revisions",
        description="Rows where analysts provided modelable forecast or revision variables.",
        columns=[
            "published_at",
            "source_name",
            "underlying_firm",
            "metric_key",
            "forecast_period",
            "period_basis",
            "period_year",
            "period_quarter",
            "period_half",
            "scenario",
            "metric_value_type",
            "forecast_value",
            "prior_forecast_value",
            "delta_vs_prior",
            "actual_value",
            "actual_vs_forecast_delta",
            "forecast_status",
            "unit",
            "summary",
        ],
        sort_spec=[{"column": "published_at", "direction": "desc"}],
        extra={"auto_created": True, "source": "decision_spine", "view_role": "forecast_revision"},
    )
    for object_type, object_id in (
        ("research_subject", int(subject.id) if subject is not None and subject.id is not None else None),
        ("company", int(company.id) if company is not None and company.id is not None else None),
        ("issuer", int(issuer.id) if issuer is not None and issuer.id is not None else None),
    ):
        if object_id is None:
            continue
        try:
            link_dataset(
                session,
                dataset_id=int(dataset.id),
                object_type=object_type,
                object_id=int(object_id),
                relation="analyst_evidence",
                extra={"source": "decision_spine"},
            )
        except Exception:
            pass
    session.flush()
    return dataset


def _analyst_relation_payloads(payload: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in _json_list(payload.get("related_evidence") or payload.get("source_relations")):
        if isinstance(item, dict):
            out.append(dict(item))
    scalar_relations = {
        "reacts_to_evidence_id": "reacts_to",
        "updates_evidence_id": "updates",
        "supersedes_evidence_id": "supersedes",
    }
    for key, relation in scalar_relations.items():
        if str(payload.get(key) or "").isdigit():
            out.append({"evidence_object_id": int(payload[key]), "relation": relation})
    for value in _json_list(payload.get("contradicts_evidence_ids")):
        if str(value or "").isdigit():
            out.append({"evidence_object_id": int(value), "relation": "contradicts"})
    return out


def _write_analyst_relation_links(
    session: Session,
    row: AnalystEvidence,
    *,
    payload: dict[str, Any],
    actor: str,
) -> None:
    for relation_payload in _analyst_relation_payloads(payload):
        related_id = int(relation_payload.get("evidence_object_id") or relation_payload.get("id") or 0)
        if related_id <= 0 or related_id == int(row.id):
            continue
        try:
            upsert_decision_evidence_link(
                session,
                {
                    "target_type": "analyst_evidence",
                    "target_id": int(row.id),
                    "evidence_object_type": "analyst_evidence",
                    "evidence_object_id": related_id,
                    "relation": str(relation_payload.get("relation") or "related").strip().lower()[:64],
                    "company_id": int(row.company_id) if row.company_id is not None else None,
                    "issuer_id": int(row.issuer_id) if row.issuer_id is not None else None,
                    "evidence_tier": row.evidence_tier,
                    "source_kind": row.source_kind,
                    "confidence": row.confidence,
                    "summary": relation_payload.get("summary") or row.summary,
                    "source_url": row.source_url,
                    "payload": {k: v for k, v in relation_payload.items() if k not in {"id", "evidence_object_id", "relation"}},
                },
                actor=actor,
            )
        except Exception:
            pass


def _forecast_period_date(forecast: dict[str, Any]) -> date | None:
    explicit = _parse_date(forecast.get("end_date") or forecast.get("actual_date") or forecast.get("start_date"))
    if explicit is not None:
        return explicit
    year = int(forecast.get("period_year") or 0)
    if year <= 0:
        return None
    quarter = int(forecast.get("period_quarter") or 0)
    if quarter in {1, 2, 3, 4}:
        return date(year, quarter * 3, 31 if quarter in {1, 4} else 30)
    half = int(forecast.get("period_half") or 0)
    if half == 1:
        return date(year, 6, 30)
    if half == 2:
        return date(year, 12, 31)
    if bool(forecast.get("is_full_year")):
        return date(year, 12, 31)
    return None


def _institutional_metric_key_from_forecast(forecast: dict[str, Any]) -> str | None:
    raw_key = forecast.get("metric_key")
    metric_key = normalize_metric_key(raw_key)
    unit = str(forecast.get("unit") or "").strip().lower()
    if metric_key == "lead_time_days" and unit in {"week", "weeks", "wk", "wks"}:
        return "lead_time_weeks"
    return metric_key


def sync_analyst_evidence_to_institutional(
    session: Session,
    row: AnalystEvidence,
) -> Any | None:
    payload = _json_dict(row.payload)
    forecast = _json_dict(payload.get("forecast"))
    metric_key = _institutional_metric_key_from_forecast(forecast)
    if not metric_key:
        return None
    value_number = _parse_float(forecast.get("value") if forecast.get("value") is not None else row.estimate_value)
    metric_def = metric_definition(metric_key)
    value_type = str(forecast.get("metric_value_type") or (metric_def.value_type if metric_def is not None else "") or "").strip()
    status_value = _first_text(
        forecast.get("forecast_status"),
        forecast.get("value_text"),
        row.summary if value_type == "status" else None,
        limit=255,
    )
    if value_number is None and not status_value:
        return None

    from thoth_terminal.institutional_service import (
        refresh_coverage_gaps,
        recompute_state_vectors,
        upsert_evidence_item,
        upsert_observation,
    )

    subject = session.get(ResearchSubject, int(row.subject_id)) if row.subject_id is not None else None
    company = session.get(Company, int(row.company_id)) if row.company_id is not None else None
    issuer = session.get(Issuer, int(row.issuer_id)) if row.issuer_id is not None else None
    source = session.get(Source, int(row.source_id)) if row.source_id is not None else None
    evidence = upsert_evidence_item(
        session,
        evidence_key=f"analyst_evidence:{int(row.id)}",
        title=str(row.summary or row.evidence_kind or row.evidence_key),
        evidence_kind="analyst_evidence",
        source=source,
        document_id=int(row.source_document_id) if row.source_document_id is not None else None,
        subject_row=subject,
        issuer=issuer,
        company=company,
        provenance_summary=str(row.source_name or row.outlet or row.evidence_kind or "analyst evidence"),
        url=row.source_url,
        canonical_url=row.source_url,
        published_at=row.published_at,
        observed_at=row.published_at or row.created_at,
        rights_class=row.rights_class or "public",
        review_required=bool(forecast.get("constraint_warning")),
        confidence=row.confidence,
        payload={
            "analyst_evidence_id": int(row.id),
            "analyst_evidence_key": row.evidence_key,
            "forecast": forecast,
            "source_attribution": payload.get("source_attribution"),
        },
    )
    observation = upsert_observation(
        session,
        observation_key=f"analyst_evidence_obs:{int(row.id)}:{metric_key}",
        kind="model_delta",
        metric_key=metric_key,
        evidence=evidence,
        source=source,
        subject_row=subject,
        issuer=issuer,
        company=company,
        dataset_family="analyst_evidence",
        value_number=value_number,
        value_text=status_value,
        unit=str(forecast.get("unit") or row.estimate_unit or "").strip() or None,
        currency=str(forecast.get("currency") or row.currency or "").strip().upper()[:16] or None,
        confidence=row.confidence,
        period_date=_forecast_period_date(forecast),
        as_of_date=row.published_at.date() if row.published_at is not None else None,
        observed_at=row.published_at or row.updated_at,
        review_status="needs_review" if forecast.get("constraint_warning") else "accepted",
        payload={
            "metric_value_type": value_type or None,
            "metric_definition": metric_def.key if metric_def is not None else None,
            "scenario": forecast.get("scenario"),
            "product_family": forecast.get("product_family"),
            "segment": forecast.get("segment"),
            "region": forecast.get("region"),
            "period": forecast.get("period") or row.estimate_period,
            "period_basis": forecast.get("period_basis"),
            "period_year": forecast.get("period_year"),
            "period_quarter": forecast.get("period_quarter"),
            "period_half": forecast.get("period_half"),
            "estimate_owner": payload.get("estimate_owner") or payload.get("underlying_firm") or row.source_name,
            "underlying_firm": payload.get("underlying_firm") or payload.get("estimate_owner"),
            "source_name": row.source_name,
            "source_document_id": int(row.source_document_id) if row.source_document_id is not None else None,
            "source_id": int(row.source_id) if row.source_id is not None else None,
            "observed_source_actor_id": int(row.observed_source_actor_id) if row.observed_source_actor_id is not None else None,
            "underlying_source_actor_id": int(row.underlying_source_actor_id) if row.underlying_source_actor_id is not None else None,
            "source_attribution": payload.get("source_attribution"),
            "prior_value": forecast.get("prior_value"),
            "delta_vs_prior": forecast.get("delta_vs_prior"),
            "delta_vs_prior_pct": forecast.get("delta_vs_prior_pct"),
            "constraint_warning": forecast.get("constraint_warning"),
            "analyst_evidence_id": int(row.id),
            "summary": row.summary,
        },
    )
    if subject is not None:
        recompute_state_vectors(session, subject_id=int(subject.id))
        refresh_coverage_gaps(session, subject_id=int(subject.id))
    return observation


def upsert_analyst_evidence(
    session: Session,
    payload: dict[str, Any],
    *,
    actor: str = "manual",
) -> AnalystEvidence:
    payload = dict(payload or {})
    company, issuer = resolve_company_or_issuer_from_payload(session, payload)
    subject = ensure_decision_subject(session, company=company, issuer=issuer)
    requested_subject_id = payload.get("subject_id") or payload.get("canonical_subject_id")
    requested_subject = (
        session.query(ResearchSubject).filter_by(id=int(requested_subject_id), status="active").first()
        if str(requested_subject_id or "").isdigit() else None
    )
    model_delta = payload.get("payload", {}).get("model_delta_candidate") if isinstance(payload.get("payload"), dict) else None
    if requested_subject is None and isinstance(model_delta, dict):
        raw_subject = str(model_delta.get("raw_subject_text") or "").strip()
        words = raw_subject.split()
        for count in range(min(4, len(words)), 0, -1):
            candidate = " ".join(words[:count])
            if len(candidate) < 3:
                continue
            requested_subject = (
                session.query(ResearchSubject)
                .filter(
                    ResearchSubject.status == "active",
                    ResearchSubject.subject_key.like("private_instrument:%"),
                    func.lower(ResearchSubject.display_name) == candidate.lower(),
                )
                .first()
            )
            if requested_subject is not None:
                break
    if requested_subject is not None:
        if (company is not None and requested_subject.company_id != company.id) or (
            issuer is not None and requested_subject.issuer_id != issuer.id
        ):
            raise ValueError("analyst_evidence_subject_conflict: resolved company/issuer differs from the research subject.")
        subject = requested_subject
    source_document_id = int(payload["source_document_id"]) if str(payload.get("source_document_id") or "").isdigit() else None
    source_document = session.query(Document).filter_by(id=source_document_id).first() if source_document_id is not None else None
    source_id = int(payload["source_id"]) if str(payload.get("source_id") or "").isdigit() else None
    if source_id is None and source_document is not None:
        source_id = int(source_document.source_id)
    evidence_tier = str(payload.get("evidence_tier") or "").strip()[:64] or None
    source_kind = str(payload.get("source_kind") or "").strip()[:64] or None
    confidence = _parse_float(payload.get("confidence"))
    if source_document is not None and (not evidence_tier or not source_kind or confidence is None):
        doc_tier, doc_kind, doc_confidence = evidence_tier_for_document(source_document)
        evidence_tier = evidence_tier or doc_tier
        source_kind = source_kind or doc_kind
        confidence = confidence if confidence is not None else doc_confidence
    evidence_kind = str(payload.get("evidence_kind") or payload.get("kind") or "analyst_note").strip().lower()[:64]
    published_at = _parse_datetime(payload.get("published_at")) or (source_document.published_at if source_document is not None else None)
    summary = str(payload.get("summary") or payload.get("claim") or "").strip()
    if not summary and source_document is not None:
        summary = str(source_document.title or "").strip()
    source_attribution = _analyst_source_attribution(payload, source_document)
    identity_payload = enrich_analyst_source_identity(
        session,
        payload=payload,
        source_attribution=source_attribution,
        source_document=source_document,
        published_at=published_at,
        actor=actor,
    )
    source_attribution = identity_payload["source_attribution"]
    forecast_payload = _analyst_forecast_payload(payload)
    price_target_payload = {k: v for k, v in _analyst_price_target_payload(payload).items() if v is not None}
    _reject_weak_local_agent_analyst_evidence(
        actor=actor,
        evidence_kind=evidence_kind,
        evidence_tier=evidence_tier,
        source_kind=source_kind,
        payload=payload,
        forecast_payload=forecast_payload,
        price_target_payload=price_target_payload,
    )
    _reject_incomplete_local_agent_consensus_identity(
        actor=actor,
        subject=subject,
        company=company,
        issuer=issuer,
        evidence_kind=evidence_kind,
        payload=payload,
        source_attribution=source_attribution,
        forecast_payload=forecast_payload,
        price_target_payload=price_target_payload,
    )
    source_relations = _analyst_relation_payloads(payload)
    observed_source = _json_dict(source_attribution.get("observed_source"))
    underlying_source = _json_dict(source_attribution.get("underlying_source"))
    normalized_source_name = _first_text(
        payload.get("source_name"),
        observed_source.get("name"),
        source_document.author if source_document is not None else None,
        limit=255,
    )
    normalized_author = _first_text(payload.get("author"), source_document.author if source_document is not None else None, limit=255)
    normalized_outlet = _first_text(payload.get("outlet"), underlying_source.get("firm"), underlying_source.get("name"), limit=255)
    key = str(payload.get("evidence_key") or "").strip()
    if not key:
        key_src = "|".join(
            [
                evidence_kind,
                str(source_document_id or ""),
                str(subject.id if subject is not None else issuer.id if issuer is not None else company.id if company is not None else ""),
                str(normalized_source_name or normalized_outlet or ""),
                str(normalized_author or ""),
                str(published_at.isoformat() if published_at else ""),
                str(payload.get("rating") or ""),
                str(payload.get("price_target") or ""),
                summary[:160].lower(),
            ]
        )
        key = f"analyst:{hashlib.sha1(key_src.encode('utf-8')).hexdigest()[:24]}"
    now = _now()
    row = session.query(AnalystEvidence).filter_by(evidence_key=key).first()
    values = {
        "subject_id": int(subject.id) if subject is not None else None,
        "issuer_id": int(issuer.id) if issuer is not None else None,
        "company_id": int(company.id) if company is not None else None,
        "source_id": source_id,
        "source_document_id": source_document_id,
        "evidence_kind": evidence_kind,
        "status": str(payload.get("status") or "active").strip().lower()[:32],
        "source_name": normalized_source_name,
        "author": normalized_author,
        "outlet": normalized_outlet,
        "published_at": published_at,
        "action": str(payload.get("action") or "").strip().lower()[:64] or None,
        "rating": str(payload.get("rating") or "").strip()[:64] or None,
        "prior_rating": str(payload.get("prior_rating") or "").strip()[:64] or None,
        "price_target": _parse_price_target(payload.get("price_target")),
        "prior_price_target": _parse_price_target(payload.get("prior_price_target")),
        "currency": str(payload.get("currency") or forecast_payload.get("currency") or "").strip().upper()[:8] or None,
        "target_horizon": str(payload.get("target_horizon") or "").strip()[:64] or None,
        "estimate_metric": str(payload.get("estimate_metric") or forecast_payload.get("metric_key") or "").strip()[:128] or None,
        "estimate_period": str(payload.get("estimate_period") or forecast_payload.get("period") or "").strip()[:64] or None,
        "estimate_value": _parse_float(payload.get("estimate_value") if payload.get("estimate_value") is not None else forecast_payload.get("value")),
        "estimate_unit": str(payload.get("estimate_unit") or forecast_payload.get("unit") or "").strip()[:64] or None,
        "evidence_tier": evidence_tier,
        "source_kind": source_kind,
        "rights_class": str(payload.get("rights_class") or "").strip().lower()[:32] or None,
        "observed_source_actor_id": identity_payload.get("observed_source_actor_id"),
        "underlying_source_actor_id": identity_payload.get("underlying_source_actor_id"),
        "source_identity_status": identity_payload.get("source_identity_status"),
        "source_side": identity_payload.get("source_side"),
        "source_role_class": identity_payload.get("source_role_class"),
        "confidence": confidence,
        "summary": summary or None,
        "source_url": str(payload.get("source_url") or _doc_url(source_document) or "").strip()[:2048] or None,
        "payload": dict(payload.get("payload") or {}),
    }
    values["payload"]["source_attribution"] = source_attribution
    if forecast_payload:
        values["payload"]["forecast"] = forecast_payload
    if price_target_payload:
        values["payload"]["price_target_analysis"] = price_target_payload
    if source_relations:
        values["payload"]["source_relations"] = source_relations
    for field in (
        "entitlement",
        "contradiction_count",
        "contradicts_evidence_ids",
        "provenance_note",
        "source_chain",
        "observed_source_name",
        "observed_source_handle",
        "underlying_source_name",
        "underlying_firm",
        "underlying_analyst",
        "access_path",
        "actual_value",
        "actual_date",
        "prior_forecast_value",
        "forecast_value",
        "metric_key",
        "product_family",
        "scenario",
        "estimate_owner",
        "estimate_owner_type",
        "source_owner",
        "source_firm",
        "estimate_firm",
        "institution",
    ):
        if field in payload and payload.get(field) is not None:
            values["payload"][field] = payload.get(field)
    for raw_field in ("forecast", "projection"):
        if raw_field in payload and isinstance(payload.get(raw_field), dict):
            values["payload"][f"raw_{raw_field}"] = payload.get(raw_field)
    values["trust_label"] = _analyst_trust_label(payload=payload, evidence_tier=values["evidence_tier"], source_kind=values["source_kind"])
    if row is None:
        row = AnalystEvidence(evidence_key=key, created_by=actor[:64], created_at=now, updated_at=now, **values)
        session.add(row)
    else:
        for field, value in values.items():
            if value is not None or field in {"payload", "status", "evidence_kind"}:
                if field == "payload":
                    merged = dict(row.payload or {})
                    merged.update(value or {})
                    setattr(row, field, merged)
                else:
                    setattr(row, field, value)
        row.updated_at = now
    session.flush()
    try:
        sync_source_attribution_edges(session, evidence=row, actor=actor)
    except Exception:
        pass
    if source_document_id is not None:
        try:
            upsert_decision_evidence_link(
                session,
                {
                    "target_type": "analyst_evidence",
                    "target_id": int(row.id),
                    "evidence_object_type": "document",
                    "evidence_object_id": int(source_document_id),
                    "company_id": int(company.id) if company is not None else None,
                    "issuer_id": int(issuer.id) if issuer is not None else None,
                    "relation": "source",
                    "evidence_tier": row.evidence_tier,
                    "source_kind": row.source_kind,
                    "confidence": row.confidence,
                    "summary": row.summary,
                    "source_url": row.source_url,
                },
                actor=actor,
            )
        except Exception:
            pass
    _write_analyst_relation_links(session, row, payload=payload, actor=actor)
    if not bool(payload.get("_dry_run")):
        try:
            sync_analyst_evidence_to_institutional(session, row)
        except Exception:
            pass
        try:
            materialize_analyst_evidence_dataset(session, subject=subject, company=company, issuer=issuer)
        except Exception:
            pass
        _close_subject_gap_request(
            session,
            subject=subject,
            gap_suffix="analyst_evidence",
            reason="Closed automatically after durable analyst evidence was written.",
        )
        clear_decision_payload_cache()
    return row


def relink_analyst_evidence_subject(
    session: Session,
    *,
    evidence_id: int,
    expected_subject_id: int | None,
    expected_company_id: int | None,
    expected_issuer_id: int | None,
    evidence_url: str,
    note: str,
    subject_id: int | None = None,
    subject_name: str | None = None,
    subject_kind: str | None = None,
    estimate_metric: str | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Preview or repair one evidence identity and its derived source/model rows."""
    row = session.get(AnalystEvidence, evidence_id)
    if (subject_id is None) == (not subject_name):
        raise ValueError("Supply exactly one destination subject id or name.")
    subject = session.get(ResearchSubject, subject_id) if subject_id is not None else None
    if subject_name:
        name = " ".join(subject_name.split())
        kind = str(subject_kind or "").strip().lower()
        if kind not in {"market", "technology", "product", "sector", "theme"} or not 3 <= len(name) <= 160:
            raise ValueError("New subject needs a bounded market, technology, product, sector, or theme kind.")
        matches = session.query(ResearchSubject).filter(ResearchSubject.kind == kind, ResearchSubject.display_name.ilike(name)).limit(2).all()
        if len(matches) > 1:
            raise ValueError("Destination subject name is ambiguous; supply subject_id.")
        subject = matches[0] if matches else None
    if row is None or (subject is not None and subject.status != "active") or (subject_id is not None and subject is None):
        raise ValueError("Evidence and an active destination subject are required.")
    before = {"subject_id": row.subject_id, "company_id": row.company_id, "issuer_id": row.issuer_id}
    expected = {"subject_id": expected_subject_id, "company_id": expected_company_id, "issuer_id": expected_issuer_id}
    if before != expected:
        raise ValueError(f"Evidence identity changed since preview: {before}")
    if not str(evidence_url or "").startswith("https://") or not str(note or "").strip():
        raise ValueError("Official identity evidence URL and audit note are required.")
    links = session.query(DecisionEvidenceLink).filter_by(target_type="analyst_evidence", target_id=evidence_id).all()
    institutional = session.query(InstitutionalEvidenceItem).filter_by(evidence_key=f"analyst_evidence:{evidence_id}").all()
    observations = [
        observation
        for item in institutional
        for observation in session.query(InstitutionalObservation).filter_by(evidence_id=int(item.id)).all()
    ]
    after = {"subject_id": int(subject.id) if subject is not None else None,
             "company_id": subject.company_id if subject is not None else None,
             "issuer_id": subject.issuer_id if subject is not None else None}
    metric = str(estimate_metric or "").strip() or None
    if metric is not None and (not metric.replace("_", "").isalnum() or len(metric) > 128):
        raise ValueError("estimate_metric must be a bounded metric key.")
    preview = {
        "evidence_id": evidence_id,
        "source_document_id": row.source_document_id,
        "summary": row.summary,
        "before": before,
        "after": after,
        "create_subject": {"name": name, "kind": kind} if subject_name and subject is None else None,
        "metric_before": row.estimate_metric,
        "metric_after": metric or row.estimate_metric,
        "decision_link_ids": [item.id for item in links],
        "institutional_evidence_ids": [item.id for item in institutional],
        "institutional_observation_ids": [item.id for item in observations],
        "evidence_url": evidence_url,
        "note": note,
        "applied": False,
    }
    if not apply:
        return preview
    if subject is None:
        from thoth_terminal.worldview_write_service import _consensus_subject_from_name

        subject = _consensus_subject_from_name(session, name=name, kind=kind)
        after = {"subject_id": int(subject.id), "company_id": subject.company_id, "issuer_id": subject.issuer_id}
        preview["after"] = after
    old_subject = session.get(ResearchSubject, before["subject_id"]) if before["subject_id"] else None
    old_company = session.get(Company, before["company_id"]) if before["company_id"] else None
    old_issuer = session.get(Issuer, before["issuer_id"]) if before["issuer_id"] else None
    now = _now()
    for item in [row, *links, *institutional, *observations]:
        item.subject_id = int(subject.id)
        item.company_id = subject.company_id
        item.issuer_id = subject.issuer_id
        item.updated_at = now
    payload = dict(row.payload or {})
    candidate = dict(payload.get("model_delta_candidate") or {})
    if candidate:
        candidate["canonical_subject_id"] = int(subject.id)
        candidate["canonical_subject_key"] = subject.subject_key
        payload["model_delta_candidate"] = candidate
    if metric is not None:
        row.estimate_metric = metric
        for key in ("forecast", "raw_forecast", "model_delta_candidate"):
            block = dict(payload.get(key) or {})
            if block:
                block["canonical_metric_key" if key == "model_delta_candidate" else "metric_key"] = metric
                payload[key] = block
        if "metric_key" in payload:
            payload["metric_key"] = metric
        for observation in observations:
            observation.metric_key = metric
            observation.observation_key = f"analyst_evidence_obs:{evidence_id}:{metric}"
        for item in institutional:
            item_payload = dict(item.payload or {})
            item_payload["forecast"] = dict(payload.get("forecast") or {})
            item.payload = item_payload
    history = list(payload.get("identity_repair_history") or [])
    history.append({"at": now.isoformat(), "before": before, "after": after, "evidence_url": evidence_url, "note": note})
    payload["identity_repair_history"] = history
    row.payload = payload
    session.flush()
    new_company = session.get(Company, subject.company_id) if subject.company_id else None
    new_issuer = session.get(Issuer, subject.issuer_id) if subject.issuer_id else None
    materialize_analyst_evidence_dataset(session, subject=subject, company=new_company, issuer=new_issuer)
    if old_subject is not None:
        materialize_analyst_evidence_dataset(session, subject=old_subject, company=old_company, issuer=old_issuer)
    from thoth_terminal.institutional_service import recompute_state_vectors, refresh_coverage_gaps

    for item in (old_subject, subject):
        if item is not None:
            recompute_state_vectors(session, subject_id=int(item.id))
            refresh_coverage_gaps(session, subject_id=int(item.id))
    clear_decision_payload_cache()
    preview["applied"] = True
    return preview


def upsert_investment_catalyst(
    session: Session,
    payload: dict[str, Any],
    *,
    actor: str = "manual",
) -> InvestmentCatalyst:
    company, issuer = resolve_company_or_issuer_from_payload(session, payload)
    subject = ensure_decision_subject(session, company=company, issuer=issuer)
    event_at = _parse_datetime(payload.get("event_at") or payload.get("event_date"))
    event_date = _parse_date(payload.get("event_date") or event_at)
    kind = str(payload.get("catalyst_kind") or payload.get("kind") or "manual").strip().lower()[:64]
    kind = {
        "shortage": "undersupply",
        "supply_shortage": "undersupply",
        "supply_tightness": "undersupply",
        "capacity_shortage": "undersupply",
        "supply_demand_shortage": "undersupply",
    }.get(kind, kind)
    title = str(payload.get("title") or "").strip()[:255]
    if not title:
        raise ValueError("Catalyst title is required.")
    key = str(payload.get("catalyst_key") or "").strip()
    if not key:
        key_src = "|".join(
            [
                kind,
                str(subject.id if subject is not None else issuer.id if issuer is not None else company.id if company is not None else ""),
                title.lower(),
                event_date.isoformat() if event_date else "",
            ]
        )
        key = f"manual:{hashlib.sha1(key_src.encode('utf-8')).hexdigest()[:24]}"
    now = _now()
    row = session.query(InvestmentCatalyst).filter_by(catalyst_key=key).first()
    deduped = False
    if row is None:
        row = _find_semantic_catalyst_match(
            session,
            subject_id=int(subject.id) if subject is not None else None,
            issuer_id=int(issuer.id) if issuer is not None else None,
            company_id=int(company.id) if company is not None else None,
            kind=kind,
            title=title,
            event_date=event_date,
            earnings_event_id=int(payload["earnings_event_id"]) if str(payload.get("earnings_event_id") or "").isdigit() else None,
            ipo_event_id=int(payload["ipo_event_id"]) if str(payload.get("ipo_event_id") or "").isdigit() else None,
        )
        deduped = row is not None
    if row is None:
        row = InvestmentCatalyst(
            catalyst_key=key,
            subject_id=int(subject.id) if subject is not None else None,
            issuer_id=int(issuer.id) if issuer is not None else None,
            company_id=int(company.id) if company is not None else None,
            source_document_id=int(payload["source_document_id"]) if str(payload.get("source_document_id") or "").isdigit() else None,
            earnings_event_id=int(payload["earnings_event_id"]) if str(payload.get("earnings_event_id") or "").isdigit() else None,
            ipo_event_id=int(payload["ipo_event_id"]) if str(payload.get("ipo_event_id") or "").isdigit() else None,
            title=title,
            catalyst_kind=kind,
            event_date=event_date,
            event_at=event_at,
            status=str(payload.get("status") or "watch").strip().lower()[:32],
            priority=int(payload.get("priority") or 5),
            confidence=float(payload["confidence"]) if payload.get("confidence") is not None else None,
            evidence_tier=str(payload.get("evidence_tier") or "").strip()[:64] or None,
            source_kind=str(payload.get("source_kind") or actor).strip()[:64] or None,
            summary=str(payload.get("summary") or "").strip() or None,
            source_url=str(payload.get("source_url") or "").strip()[:2048] or None,
            payload=dict(payload.get("payload") or {}),
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        setattr(row, "_thoth_created", True)
    else:
        row.subject_id = int(subject.id) if subject is not None else row.subject_id
        row.issuer_id = int(issuer.id) if issuer is not None else row.issuer_id
        row.company_id = int(company.id) if company is not None else row.company_id
        row.title = title
        row.catalyst_kind = kind
        row.event_date = event_date
        row.event_at = event_at
        row.status = str(payload.get("status") or row.status or "watch").strip().lower()[:32]
        row.priority = int(payload.get("priority") or row.priority or 5)
        row.confidence = float(payload["confidence"]) if payload.get("confidence") is not None else row.confidence
        row.evidence_tier = str(payload.get("evidence_tier") or row.evidence_tier or "").strip()[:64] or None
        row.source_kind = str(payload.get("source_kind") or row.source_kind or actor).strip()[:64] or None
        row.summary = str(payload.get("summary") or row.summary or "").strip() or None
        row.source_url = str(payload.get("source_url") or row.source_url or "").strip()[:2048] or None
        if str(payload.get("source_document_id") or "").isdigit() and row.source_document_id is None:
            row.source_document_id = int(payload["source_document_id"])
        if str(payload.get("earnings_event_id") or "").isdigit() and row.earnings_event_id is None:
            row.earnings_event_id = int(payload["earnings_event_id"])
        if str(payload.get("ipo_event_id") or "").isdigit() and row.ipo_event_id is None:
            row.ipo_event_id = int(payload["ipo_event_id"])
        merged = dict(row.payload or {})
        merged.update(dict(payload.get("payload") or {}))
        if deduped:
            merged.setdefault("deduped_write_keys", [])
            if isinstance(merged["deduped_write_keys"], list) and key not in merged["deduped_write_keys"]:
                merged["deduped_write_keys"].append(key)
            merged["deduped_latest_key"] = key
        row.payload = merged
        row.updated_at = now
        setattr(row, "_thoth_created", False)
    setattr(row, "_thoth_deduped", deduped)
    session.flush()
    clear_decision_payload_cache()
    return row


def _normalized_catalyst_title(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def _find_semantic_catalyst_match(
    session: Session,
    *,
    subject_id: int | None,
    issuer_id: int | None,
    company_id: int | None,
    kind: str,
    title: str,
    event_date: date | None,
    earnings_event_id: int | None = None,
    ipo_event_id: int | None = None,
) -> InvestmentCatalyst | None:
    """Find an existing catalyst when a worker supplies a new ad-hoc key for the same event."""
    if earnings_event_id is not None:
        existing = session.query(InvestmentCatalyst).filter_by(earnings_event_id=int(earnings_event_id)).first()
        if existing is not None:
            return existing
    if ipo_event_id is not None:
        existing = session.query(InvestmentCatalyst).filter_by(ipo_event_id=int(ipo_event_id)).first()
        if existing is not None:
            return existing
    q = session.query(InvestmentCatalyst).filter(InvestmentCatalyst.catalyst_kind == kind)
    if event_date is None:
        q = q.filter(InvestmentCatalyst.event_date.is_(None))
    else:
        q = q.filter(InvestmentCatalyst.event_date == event_date)
    identity_filters = []
    if subject_id is not None:
        identity_filters.append(InvestmentCatalyst.subject_id == int(subject_id))
    if issuer_id is not None:
        identity_filters.append(InvestmentCatalyst.issuer_id == int(issuer_id))
    if company_id is not None:
        identity_filters.append(InvestmentCatalyst.company_id == int(company_id))
    if identity_filters:
        q = q.filter(or_(*identity_filters))
    target_title = _normalized_catalyst_title(title)
    for candidate in q.order_by(InvestmentCatalyst.id.asc()).limit(250):
        if _normalized_catalyst_title(candidate.title) == target_title:
            return candidate
    return None


def upsert_investment_memo(
    session: Session,
    payload: dict[str, Any],
    *,
    actor: str = "manual",
) -> InvestmentMemo:
    company, issuer = resolve_company_or_issuer_from_payload(session, payload)
    subject = ensure_decision_subject(session, company=company, issuer=issuer)
    subject_key = str(subject.subject_key if subject is not None else payload.get("subject_key") or "").strip()
    key = str(payload.get("memo_key") or "").strip()
    if not key:
        key = f"memo:{subject_key}" if subject_key else f"memo:{hashlib.sha1(str(payload).encode('utf-8')).hexdigest()[:20]}"
    title = str(payload.get("title") or (f"{subject.display_name} Investment Memo" if subject is not None else "Investment Memo")).strip()[:255]
    memo_payload = dict(payload.get("payload") or {})
    for field in ("conviction", "priority", "inclusion_reason", "reason_for_inclusion", "active_thesis", "next_catalyst", "missing_data_warnings"):
        if field in payload and payload.get(field) is not None:
            memo_payload[field] = payload.get(field)
    now = _now()
    row = session.query(InvestmentMemo).filter_by(memo_key=key).first()
    if row is None:
        row = InvestmentMemo(
            memo_key=key,
            subject_id=int(subject.id) if subject is not None else None,
            issuer_id=int(issuer.id) if issuer is not None else None,
            company_id=int(company.id) if company is not None else None,
            title=title,
            status=str(payload.get("status") or "draft").strip().lower()[:32],
            rating=str(payload.get("rating") or "").strip()[:32] or None,
            time_horizon=str(payload.get("time_horizon") or "").strip()[:64] or None,
            summary_md=str(payload.get("summary_md") or "").strip() or None,
            thesis_md=str(payload.get("thesis_md") or "").strip() or None,
            valuation_md=str(payload.get("valuation_md") or "").strip() or None,
            catalysts_md=str(payload.get("catalysts_md") or "").strip() or None,
            risks_md=str(payload.get("risks_md") or "").strip() or None,
            evidence_json=[dict(v) for v in payload.get("evidence_json") or [] if isinstance(v, dict)],
            payload=memo_payload,
            created_by=actor[:64],
            created_at=now,
            updated_at=now,
            last_reviewed_at=_parse_datetime(payload.get("last_reviewed_at")),
        )
        session.add(row)
    else:
        row.subject_id = int(subject.id) if subject is not None else row.subject_id
        row.issuer_id = int(issuer.id) if issuer is not None else row.issuer_id
        row.company_id = int(company.id) if company is not None else row.company_id
        row.title = title
        for field in ("status", "rating", "time_horizon", "summary_md", "thesis_md", "valuation_md", "catalysts_md", "risks_md"):
            if field in payload:
                value = str(payload.get(field) or "").strip()
                setattr(row, field, value or None)
        if isinstance(payload.get("evidence_json"), list):
            row.evidence_json = [dict(v) for v in payload.get("evidence_json") or [] if isinstance(v, dict)]
        merged = dict(row.payload or {})
        merged.update(memo_payload)
        row.payload = merged
        row.updated_at = now
        if payload.get("last_reviewed_at") is not None:
            row.last_reviewed_at = _parse_datetime(payload.get("last_reviewed_at"))
    session.flush()
    _close_subject_gap_request(
        session,
        subject=subject,
        gap_suffix="investment_memo",
        reason="Closed automatically after durable investment memo was written.",
    )
    clear_decision_payload_cache()
    return row


def upsert_decision_evidence_link(
    session: Session,
    payload: dict[str, Any],
    *,
    actor: str = "manual",
) -> DecisionEvidenceLink:
    target_type = str(payload.get("target_type") or "").strip().lower()
    target_id = int(payload.get("target_id")) if str(payload.get("target_id") or "").isdigit() else None
    evidence_type = str(payload.get("evidence_object_type") or payload.get("source_object_type") or "document").strip().lower()
    evidence_id = int(payload.get("evidence_object_id")) if str(payload.get("evidence_object_id") or "").isdigit() else None
    if not target_type or target_id is None:
        raise ValueError("target_type and target_id are required.")
    company, issuer = resolve_company_or_issuer_from_payload(session, payload)
    subject = ensure_decision_subject(session, company=company, issuer=issuer)
    relation = str(payload.get("relation") or "supports").strip().lower()[:64]
    row = (
        session.query(DecisionEvidenceLink)
        .filter_by(
            target_type=target_type,
            target_id=int(target_id),
            evidence_object_type=evidence_type,
            evidence_object_id=evidence_id,
            relation=relation,
        )
        .first()
    )
    now = _now()
    if row is None:
        row = DecisionEvidenceLink(
            target_type=target_type,
            target_id=int(target_id),
            evidence_object_type=evidence_type,
            evidence_object_id=evidence_id,
            subject_id=int(subject.id) if subject is not None else None,
            issuer_id=int(issuer.id) if issuer is not None else None,
            company_id=int(company.id) if company is not None else None,
            relation=relation,
            evidence_tier=str(payload.get("evidence_tier") or "").strip()[:64] or None,
            source_kind=str(payload.get("source_kind") or "").strip()[:64] or None,
            confidence=float(payload["confidence"]) if payload.get("confidence") is not None else None,
            summary=str(payload.get("summary") or "").strip() or None,
            source_url=str(payload.get("source_url") or "").strip()[:2048] or None,
            payload=dict(payload.get("payload") or {}),
            created_by=actor[:64],
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    else:
        row.subject_id = int(subject.id) if subject is not None else row.subject_id
        row.issuer_id = int(issuer.id) if issuer is not None else row.issuer_id
        row.company_id = int(company.id) if company is not None else row.company_id
        row.evidence_tier = str(payload.get("evidence_tier") or row.evidence_tier or "").strip()[:64] or None
        row.source_kind = str(payload.get("source_kind") or row.source_kind or "").strip()[:64] or None
        row.confidence = float(payload["confidence"]) if payload.get("confidence") is not None else row.confidence
        row.summary = str(payload.get("summary") or row.summary or "").strip() or None
        row.source_url = str(payload.get("source_url") or row.source_url or "").strip()[:2048] or None
        merged = dict(row.payload or {})
        merged.update(dict(payload.get("payload") or {}))
        row.payload = merged
        row.updated_at = now
    session.flush()
    clear_decision_payload_cache()
    return row


def _upsert_synced_catalyst(
    session: Session,
    *,
    key: str,
    company: Company | None,
    issuer: Issuer | None,
    subject: ResearchSubject | None,
    title: str,
    catalyst_kind: str,
    event_date: date | None,
    event_at: datetime | None,
    status: str,
    priority: int,
    confidence: float | None,
    evidence_tier: str | None,
    source_kind: str | None,
    source_url: str | None,
    earnings_event_id: int | None = None,
    ipo_event_id: int | None = None,
    payload: dict[str, Any] | None = None,
) -> InvestmentCatalyst:
    now = _now()
    row = session.query(InvestmentCatalyst).filter_by(catalyst_key=key).first()
    if row is None:
        row = InvestmentCatalyst(
            catalyst_key=key,
            subject_id=int(subject.id) if subject is not None else None,
            issuer_id=int(issuer.id) if issuer is not None else None,
            company_id=int(company.id) if company is not None else None,
            source_document_id=None,
            earnings_event_id=earnings_event_id,
            ipo_event_id=ipo_event_id,
            title=title[:255],
            catalyst_kind=catalyst_kind[:64],
            event_date=event_date,
            event_at=event_at,
            status=status[:32],
            priority=int(priority),
            confidence=confidence,
            evidence_tier=evidence_tier,
            source_kind=source_kind,
            summary=None,
            source_url=source_url,
            payload=dict(payload or {}),
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    else:
        row.subject_id = int(subject.id) if subject is not None else row.subject_id
        row.issuer_id = int(issuer.id) if issuer is not None else row.issuer_id
        row.company_id = int(company.id) if company is not None else row.company_id
        row.title = title[:255]
        row.catalyst_kind = catalyst_kind[:64]
        row.event_date = event_date
        row.event_at = event_at
        row.status = status[:32]
        row.priority = int(priority)
        row.confidence = confidence if confidence is not None else row.confidence
        row.evidence_tier = evidence_tier or row.evidence_tier
        row.source_kind = source_kind or row.source_kind
        row.source_url = source_url or row.source_url
        merged = dict(row.payload or {})
        merged.update(dict(payload or {}))
        row.payload = merged
        row.updated_at = now
    session.flush()
    return row


def _earnings_event_is_valid_for_catalyst(event: EarningsEvent, *, today: date) -> bool:
    if str(event.status or "").strip().lower() == "invalid":
        return False
    raw_meta = event.raw_meta if isinstance(event.raw_meta, dict) else {}
    if str(raw_meta.get("invalid_reason") or "").strip() == "implausible_earnings_fiscal_year":
        return False
    return is_plausible_earnings_fiscal_year(event.fiscal_year, today=today)


def _withdraw_invalid_earnings_catalyst(
    session: Session,
    event: EarningsEvent,
    *,
    reason: str,
) -> int:
    row = session.query(InvestmentCatalyst).filter_by(earnings_event_id=int(event.id)).first()
    if row is None or row.status == "withdrawn":
        return 0
    now = _now()
    payload = dict(row.payload or {})
    payload.update(
        {
            "withdrawn_by": "earnings_event_validation",
            "withdrawn_reason": reason,
            "withdrawn_at": now.isoformat(),
            "earnings_event_id": int(event.id),
            "fiscal_year": int(event.fiscal_year) if event.fiscal_year is not None else None,
        }
    )
    row.status = "withdrawn"
    row.priority = 0
    row.payload = payload
    row.updated_at = now
    session.flush()
    return 1


def sync_earnings_catalysts(
    session: Session,
    *,
    issuer_id: int | None = None,
    days_back: int = 540,
    days_forward: int = 540,
    limit: int = 1000,
) -> int:
    today = calendar_today()
    lower = today - timedelta(days=max(1, int(days_back)))
    upper = today + timedelta(days=max(1, int(days_forward)))
    q = session.query(EarningsEvent)
    if issuer_id is not None:
        q = q.filter(EarningsEvent.issuer_id == int(issuer_id))
    q = q.filter(
        or_(
            EarningsEvent.scheduled_date.between(lower, upper),
            EarningsEvent.actual_release_at >= datetime.combine(lower, time.min),
            EarningsEvent.updated_at >= datetime.combine(lower, time.min),
        )
    )
    count = 0
    for event in q.order_by(EarningsEvent.scheduled_date.asc().nullslast(), EarningsEvent.updated_at.desc()).limit(max(1, int(limit))).all():
        if not _earnings_event_is_valid_for_catalyst(event, today=today):
            _withdraw_invalid_earnings_catalyst(
                session,
                event,
                reason="implausible_or_invalid_earnings_event",
            )
            continue
        issuer = session.query(Issuer).filter_by(id=int(event.issuer_id)).first()
        company = _company_for_issuer(session, issuer)
        subject = ensure_decision_subject(session, company=company, issuer=issuer)
        packet = earnings_event_packet(session, event)
        display_name = subject.display_name if subject is not None else issuer.display_name if issuer is not None else f"Issuer {event.issuer_id}"
        event_at = event.actual_release_at or event.actual_call_at or (datetime.combine(event.scheduled_date, time.min) if event.scheduled_date else event.updated_at)
        evidence_tier = (
            "official_regulatory"
            if event.release_filing_id
            else "exchange_disclosure"
            if event.release_disclosure_id
            else "issuer_disclosure"
            if event.release_artifact_id or event.presentation_artifact_id
            else event.date_provenance
        )
        source_url = None
        release = (
            packet.get("components", {}).get("release_filing")
            or packet.get("components", {}).get("release_disclosure")
            or packet.get("components", {}).get("release_artifact")
        )
        if isinstance(release, dict):
            source_url = release.get("url")
        completed = earnings_event_is_effectively_completed(event, today=today)
        priority = 28 if completed and not event.transcript_artifact_id else 18 if completed else 12
        _upsert_synced_catalyst(
            session,
            key=f"earnings:{int(event.id)}",
            company=company,
            issuer=issuer,
            subject=subject,
            title=f"{display_name} {packet['period_label']} earnings",
            catalyst_kind="earnings",
            event_date=event_at.date() if event_at else event.scheduled_date,
            event_at=event_at,
            status="completed" if completed else "watch",
            priority=priority,
            confidence=0.95 if event.release_filing_id or event.release_artifact_id else 0.72,
            evidence_tier=evidence_tier,
            source_kind="earnings_event",
            source_url=str(source_url or "") or None,
            earnings_event_id=int(event.id),
            payload={"packet": packet},
        )
        count += 1
    return count


def sync_ipo_catalysts(
    session: Session,
    *,
    issuer_id: int | None = None,
    days_back: int = 365,
    days_forward: int = 730,
    limit: int = 1000,
) -> int:
    today = calendar_today()
    lower = today - timedelta(days=max(1, int(days_back)))
    upper = today + timedelta(days=max(1, int(days_forward)))
    q = session.query(IPOEvent)
    if issuer_id is not None:
        q = q.filter(IPOEvent.issuer_id == int(issuer_id))
    q = q.filter(
        or_(
            IPOEvent.listing_date.between(lower, upper),
            IPOEvent.pricing_date.between(lower, upper),
            IPOEvent.announcement_date.between(lower, upper),
            IPOEvent.updated_at >= datetime.combine(lower, time.min),
        )
    )
    count = 0
    for event in q.order_by(IPOEvent.listing_date.asc().nullslast(), IPOEvent.updated_at.desc()).limit(max(1, int(limit))).all():
        issuer = event.issuer
        company = event.company or _company_for_issuer(session, issuer)
        subject = ensure_decision_subject(session, company=company, issuer=issuer)
        event_date = event.listing_date or event.pricing_date or event.announcement_date
        title = event.title or f"{event.company_name} IPO"
        _upsert_synced_catalyst(
            session,
            key=f"ipo:{int(event.id)}",
            company=company,
            issuer=issuer,
            subject=subject,
            title=title,
            catalyst_kind="ipo",
            event_date=event_date,
            event_at=datetime.combine(event_date, time.min) if event_date else event.updated_at,
            status=event.status or "active",
            priority=16,
            confidence=event.confidence,
            evidence_tier=event.source_tier,
            source_kind="ipo_calendar",
            source_url=event.source_url,
            ipo_event_id=int(event.id),
            payload={
                "region": event.region,
                "exchange": event.exchange,
                "stage": event.stage,
                "ticker": event.ticker,
                "price_text": event.price_text,
                "proceeds_amount": event.proceeds_amount,
                "currency": event.currency or event.proceeds_currency,
            },
        )
        count += 1
    return count


def list_investment_catalysts(
    session: Session,
    *,
    company_id: int | None = None,
    issuer_id: int | None = None,
    days_back: int = 120,
    days_forward: int = 365,
    limit: int = 250,
) -> list[dict[str, Any]]:
    today = calendar_today()
    lower = today - timedelta(days=max(1, int(days_back)))
    upper = today + timedelta(days=max(1, int(days_forward)))
    q = session.query(InvestmentCatalyst)
    filters = []
    if company_id is not None:
        filters.append(InvestmentCatalyst.company_id == int(company_id))
    if issuer_id is not None:
        filters.append(InvestmentCatalyst.issuer_id == int(issuer_id))
    if filters:
        q = q.filter(or_(*filters))
    q = q.filter(or_(InvestmentCatalyst.event_date.is_(None), InvestmentCatalyst.event_date.between(lower, upper)))
    rows = (
        q.order_by(
            InvestmentCatalyst.event_date.asc().nullslast(),
            InvestmentCatalyst.priority.desc(),
            InvestmentCatalyst.updated_at.desc(),
        )
        .limit(max(1, min(int(limit), 1000)))
        .all()
    )
    return [serialize_catalyst(row) for row in rows]


def _subject_match_terms(
    session: Session,
    *,
    subject: ResearchSubject | None,
    company: Company | None,
    issuer: Issuer | None,
) -> tuple[set[str], set[str]]:
    names: set[str] = set()
    tickers: set[str] = set()
    if subject is not None:
        names.update([subject.display_name, subject.legal_name, subject.slug, subject.subject_key])
        for alias in session.query(ResearchSubjectAlias).filter_by(subject_id=int(subject.id)).limit(50).all():
            if str(alias.alias or "").strip():
                names.add(str(alias.alias).strip())
                if str(alias.alias_type or "").lower() == "ticker":
                    tickers.add(str(alias.alias).strip().upper().lstrip("$"))
    if company is not None:
        names.update([company.name, company.slug])
        if str(company.ticker or "").strip():
            tickers.add(str(company.ticker).strip().upper())
    if issuer is not None:
        names.update([issuer.display_name, issuer.legal_name, issuer.slug])
        for ident in session.query(IssuerIdentifier).filter_by(issuer_id=int(issuer.id)).limit(50).all():
            if str(ident.id_type or "").upper() == "TICKER" and str(ident.id_value or "").strip():
                tickers.add(str(ident.id_value).strip().upper())
            elif str(ident.id_value or "").strip():
                names.add(str(ident.id_value).strip())
    name_keys = {canonical_entity_key(name) for name in names if canonical_entity_key(name)}
    return name_keys, {ticker for ticker in tickers if ticker}


def _matches_subject_terms(thesis: Thesis, *, name_keys: set[str], tickers: set[str]) -> bool:
    hay_parts = [thesis.title, thesis.statement]
    for entity in thesis.entities or []:
        hay_parts.append(entity.canonical_name)
    hay = " ".join(str(part or "") for part in hay_parts)
    hay_upper = hay.upper()
    for ticker in tickers:
        if re.search(rf"(?<![A-Z0-9])\$?{re.escape(ticker)}(?![A-Z0-9])", hay_upper):
            return True
    entity_keys = [canonical_entity_key(getattr(entity, "canonical_name", "")) for entity in thesis.entities or []]
    entity_keys.extend(canonical_entity_key(part) for part in [thesis.title] if canonical_entity_key(part))
    for left in name_keys:
        for right in entity_keys:
            if not right:
                continue
            if left == right or left in right or right in left or name_match_score(left, right) >= 0.74:
                return True
    return False


def _parse_extra_dt(payload: dict[str, Any], *keys: str) -> datetime | None:
    for key in keys:
        value = payload.get(key)
        parsed = _parse_datetime(value)
        if parsed is not None:
            return parsed
    return None


def _thesis_freshness_payload(thesis: Thesis, *, last_evidence_at: datetime | None) -> dict[str, Any]:
    extra = dict(thesis.extra or {}) if isinstance(thesis.extra, dict) else {}
    horizon = str(
        extra.get("validity_horizon")
        or extra.get("time_horizon")
        or extra.get("horizon")
        or ""
    ).strip().lower()
    last_challenged_at = _parse_extra_dt(extra, "last_challenged_at", "challenged_at", "last_challenge_at")
    last_restatement_at = _parse_extra_dt(extra, "last_restatement_at", "last_restated_at", "restated_at")
    basis = last_challenged_at or thesis.last_reviewed_at or last_evidence_at or thesis.updated_at or thesis.created_at
    stale_after = thesis.stale_after_days
    long_horizon = horizon in {"long", "long_term", "long-term", "multi_year", "multi-year", "cycle", "secular"}
    state = "fresh"
    overdue_days = None
    if stale_after is None or long_horizon:
        state = "validity_horizon"
    elif basis is not None:
        overdue_days = (utcnow() - basis).days - int(stale_after)
        if overdue_days > 0:
            state = "needs_challenge" if last_challenged_at is None else "challenge_stale"
    if thesis.stale_check_state:
        state = str(thesis.stale_check_state)
    return {
        "state": state,
        "validity_horizon": horizon or None,
        "stale_after_days": int(stale_after) if stale_after is not None else None,
        "last_challenged_at": last_challenged_at.isoformat() if last_challenged_at else None,
        "last_restatement_at": last_restatement_at.isoformat() if last_restatement_at else None,
        "last_reviewed_at": thesis.last_reviewed_at.isoformat() if thesis.last_reviewed_at else None,
        "last_evidence_at": last_evidence_at.isoformat() if last_evidence_at else None,
        "overdue_days": int(overdue_days) if overdue_days is not None else None,
        "staleness_note": "Long-horizon thesis: challenge evidence without treating lack of recent restatement as stale."
        if stale_after is None or long_horizon
        else None,
    }


def active_thesis_payloads(
    session: Session,
    *,
    subject: ResearchSubject | None,
    company: Company | None,
    issuer: Issuer | None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    name_keys, tickers = _subject_match_terms(session, subject=subject, company=company, issuer=issuer)
    if not name_keys and not tickers:
        return []
    rows: list[dict[str, Any]] = []
    candidates = (
        session.query(Thesis)
        .options(joinedload(Thesis.entities), joinedload(Thesis.evidence))
        .filter(Thesis.status.in_(["watching", "active", "confirmed"]))
        .order_by(Thesis.importance.desc(), Thesis.updated_at.desc(), Thesis.id.desc())
        .limit(500)
        .all()
    )
    for thesis in candidates:
        if not _matches_subject_terms(thesis, name_keys=name_keys, tickers=tickers):
            continue
        evidence = list(thesis.evidence or [])
        last_evidence_at = max((ev.created_at for ev in evidence if ev.created_at), default=None)
        contradict_count = sum(1 for ev in evidence if str(ev.polarity or "").lower() in {"contradict", "against", "negative"})
        rows.append(
            {
                "id": int(thesis.id),
                "title": thesis.title,
                "statement": thesis.statement,
                "kind": thesis.kind,
                "status": thesis.status,
                "importance": int(thesis.importance or 0),
                "href": f"/theses/{int(thesis.id)}",
                "evidence_count": len(evidence),
                "contradiction_count": int(contradict_count),
                "freshness": _thesis_freshness_payload(thesis, last_evidence_at=last_evidence_at),
                "updated_at": thesis.updated_at.isoformat() if thesis.updated_at else None,
            }
        )
        if len(rows) >= max(1, int(limit)):
            break
    return rows


def _source_rail_health(
    session: Session,
    *,
    company: Company | None,
    issuer: Issuer | None,
) -> dict[str, Any]:
    docs: list[Document] = []
    if company is not None:
        docs.extend(
            doc
            for doc in (
                session.query(Document)
                .join(CompanyDocument, CompanyDocument.document_id == Document.id)
                .filter(CompanyDocument.company_id == int(company.id))
                .order_by(Document.ingested_at.desc())
                .limit(200)
                .all()
            )
            if doc is not None
        )
    if issuer is not None:
        filing_docs = [
            doc
            for doc in (
                session.query(Document)
                .join(SecuritiesFiling, SecuritiesFiling.document_id == Document.id)
                .filter(SecuritiesFiling.issuer_id == int(issuer.id))
                .order_by(Document.ingested_at.desc())
                .limit(100)
                .all()
            )
            if doc is not None
        ]
        docs.extend(filing_docs)
    by_tier: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    seen_doc_ids: set[int] = set()
    for doc in docs:
        if int(doc.id) in seen_doc_ids:
            continue
        seen_doc_ids.add(int(doc.id))
        tier, kind, _confidence = evidence_tier_for_document(doc)
        by_tier[tier] = by_tier.get(tier, 0) + 1
        by_kind[kind] = by_kind.get(kind, 0) + 1
    ir_artifacts = session.query(IRArtifact.id).filter(IRArtifact.issuer_id == int(issuer.id)).count() if issuer is not None else 0
    return {
        "document_count": len(seen_doc_ids),
        "by_evidence_tier": by_tier,
        "by_source_kind": by_kind,
        "ir_artifact_count": int(ir_artifacts),
        "policy": "IR pages are incidental. Coverage is scored from filings, exchange/issuer artifacts already captured, transcripts, datasets, analyst evidence, and durable reports.",
    }


def _dataset_context(
    session: Session,
    *,
    company: Company | None,
) -> dict[str, Any]:
    if company is None:
        return {"links": [], "structured_fact_count": 0, "dataset_count": 0}
    company_ids = company_structured_fact_scope_ids(session, company)
    link_rows = (
        session.query(DatasetLink, Dataset)
        .join(Dataset, Dataset.id == DatasetLink.dataset_id)
        .filter(DatasetLink.object_type == "company", DatasetLink.object_id.in_(company_ids))
        .order_by(Dataset.updated_at.desc(), DatasetLink.id.desc())
        .limit(12)
        .all()
    ) if company_ids else []
    fact_dataset_ids = {
        int(row[0])
        for row in session.query(StructuredDataFact.dataset_id)
        .filter(StructuredDataFact.company_id.in_(company_ids), StructuredDataFact.dataset_id.isnot(None))
        .all()
        if row[0] is not None
    } if company_ids else set()
    return {
        "links": [
            {
                "dataset_id": int(dataset.id),
                "title": dataset.title,
                "family": dataset.family,
                "category": dataset.category,
                "relation": link.relation,
                "href": f"/visualizations/datasets/{int(dataset.id)}",
            }
            for link, dataset in link_rows
        ],
        "structured_fact_count": (
            session.query(StructuredDataFact.id).filter(StructuredDataFact.company_id.in_(company_ids)).count()
            if company_ids
            else 0
        ),
        "dataset_count": len(fact_dataset_ids),
    }


def _report_memory_payload(session: Session, *, subject: ResearchSubject) -> dict[str, Any]:
    report = session.query(ResearchReport).filter_by(subject_key=subject.subject_key).first()
    if report is None:
        return {"status": "missing", "report": None, "versions": []}
    versions = (
        session.query(ResearchReportVersion)
        .filter_by(report_id=int(report.id))
        .order_by(ResearchReportVersion.version.desc(), ResearchReportVersion.id.desc())
        .limit(5)
        .all()
    )
    return {
        "status": "available" if versions else "empty",
        "report": {"id": int(report.id), "title": report.title, "href": f"/reports/{report.slug}"},
        "versions": [
            {
                "id": int(version.id),
                "version": int(version.version),
                "status": version.status,
                "content_chars": len(version.content_md or ""),
                "memory_quality": report_memory_quality(version),
                "memory_review": report_memory_review_recommendation(version),
                "coverage_gaps": list((version.coverage_json or {}).get("gaps") or []) if isinstance(version.coverage_json, dict) else [],
                "published_at": version.published_at.isoformat() if version.published_at else None,
                "updated_at": version.updated_at.isoformat() if version.updated_at else None,
            }
            for version in versions
        ],
    }


def _alert_inventory(session: Session, *, subject: ResearchSubject, company: Company | None, issuer: Issuer | None) -> dict[str, Any]:
    notification_rules = session.query(NotificationRule).order_by(NotificationRule.updated_at.desc(), NotificationRule.id.desc()).limit(25).all()
    relevant_notifications: list[dict[str, Any]] = []
    ticker = _primary_ticker(session, issuer=issuer, company=company)
    subject_key = canonical_entity_key(subject.display_name)
    for rule in notification_rules:
        hay = " ".join(
            [
                rule.name,
                rule.keyword_regex or "",
                " ".join(rule.domain_slugs or []),
                " ".join(rule.tag_slugs or []),
            ]
        ).lower()
        if (ticker and ticker.lower() in hay) or (subject_key and subject_key in canonical_entity_key(hay)):
            relevant_notifications.append(
                {
                    "id": int(rule.id),
                    "name": rule.name,
                    "enabled": bool(rule.enabled),
                    "action": rule.action,
                    "kind": "notification_rule",
                }
            )
    return {
        "notification_rule_count": session.query(NotificationRule.id).count(),
        "active_notification_rule_count": session.query(NotificationRule.id).filter(NotificationRule.enabled.is_(True)).count(),
        "supply_chain_alert_rule_count": session.query(SupplyChainAlertRule.id).count(),
        "active_supply_chain_alert_rule_count": session.query(SupplyChainAlertRule.id).filter(SupplyChainAlertRule.status == "active").count(),
        "relevant": relevant_notifications[:8],
        "discoverability": "Alerts live in notification rules and supply-chain alert rules; decision payloads expose inventory so they are not hidden from agents or the side panel.",
    }


def _gap_request_key_for_value(value: str | int | None) -> str:
    key_hash = hashlib.sha1(str(value or "").encode("utf-8")).hexdigest()[:24]
    return f"decision-gap:{key_hash}"


def _gap_request_key(gap: CoverageGap) -> str:
    return _gap_request_key_for_value(gap.gap_key or gap.id)


USER_AUTHORED_GAP_KINDS = {"missing_investment_memo"}
GLOBAL_AUTO_QUEUED_GAP_KINDS = {"missing_supply_chain_metric_bindings"}


def _global_gap_is_auto_queueable(gap: CoverageGap) -> bool:
    payload = dict(gap.payload or {}) if isinstance(gap.payload, dict) else {}
    if "auto_queue" in payload and not bool(payload.get("auto_queue")):
        return False
    if str(payload.get("actionability") or "").strip().lower() in {"archived", "duplicate", "preserved_duplicate", "non_actionable"}:
        return False
    return True


def _should_auto_queue_gap_request(
    session: Session,
    gap: CoverageGap,
    *,
    active_scope: dict[str, set[int]] | None = None,
) -> bool:
    if str(gap.gap_kind or "").strip() in USER_AUTHORED_GAP_KINDS:
        return False
    if str(gap.gap_kind or "").strip() in GLOBAL_AUTO_QUEUED_GAP_KINDS:
        return _global_gap_is_auto_queueable(gap)
    return coverage_gap_scope(session, gap, active_scope=active_scope) == "active_universe"


def _out_of_universe_gap_request_note(gap: CoverageGap) -> str:
    return (
        "Closed automatically because the linked decision-spine gap is outside active universes; "
        f"the {gap.gap_kind} gap remains preserved as evidence."
    )


def _reject_gap_request_if_present(
    session: Session,
    *,
    gap: CoverageGap,
    note: str,
) -> None:
    request = session.query(InstrumentRequest).filter_by(request_key=_gap_request_key(gap)).first()
    if request is None or request.status not in {REQUEST_STATUS_DRAFT, REQUEST_STATUS_REVIEW}:
        return
    now = _now()
    request.status = REQUEST_STATUS_REJECTED
    request.review_note = note
    request.reviewed_at = now
    request.updated_at = now


def _gap_refs_from_request(request: InstrumentRequest) -> tuple[set[int], set[str]]:
    gap_ids: set[int] = set()
    gap_keys: set[str] = set()

    def add_int(value: Any) -> None:
        if isinstance(value, bool):
            return
        if isinstance(value, (list, tuple, set)):
            for item in value:
                add_int(item)
            return
        try:
            text = str(value or "").strip()
            if text:
                gap_ids.add(int(text))
        except Exception:
            return

    def add_key(value: Any) -> None:
        if isinstance(value, (list, tuple, set)):
            for item in value:
                add_key(item)
            return
        text = str(value or "").strip()
        if text:
            gap_keys.add(text)

    linked = request.linked_objects if isinstance(request.linked_objects, dict) else {}
    draft = request.draft_payload if isinstance(request.draft_payload, dict) else {}
    live = request.live_payload if isinstance(request.live_payload, dict) else {}
    for payload in (linked, draft, live):
        add_int(payload.get("gap_id"))
        add_int(payload.get("gap_ids"))
        add_key(payload.get("gap_key"))
        add_key(payload.get("gap_keys"))
        nested = payload.get("gap")
        if isinstance(nested, dict):
            add_int(nested.get("id"))
            add_key(nested.get("gap_key"))
    return gap_ids, gap_keys


def _linked_gap_for_request(session: Session, request: InstrumentRequest) -> CoverageGap | None:
    gap_ids, gap_keys = _gap_refs_from_request(request)
    if gap_ids:
        row = session.query(CoverageGap).filter(CoverageGap.id.in_(sorted(gap_ids))).order_by(CoverageGap.id.asc()).first()
        if row is not None:
            return row
    if gap_keys:
        return (
            session.query(CoverageGap)
            .filter(CoverageGap.gap_key.in_(sorted(gap_keys)))
            .order_by(CoverageGap.id.asc())
            .first()
        )
    return None


def _linked_gap_from_refs(
    refs: tuple[set[int], set[str]],
    *,
    gaps_by_id: dict[int, CoverageGap],
    gaps_by_key: dict[str, CoverageGap],
) -> CoverageGap | None:
    gap_ids, gap_keys = refs
    for gap_id in sorted(gap_ids):
        row = gaps_by_id.get(int(gap_id))
        if row is not None:
            return row
    for gap_key in sorted(gap_keys):
        row = gaps_by_key.get(str(gap_key))
        if row is not None:
            return row
    return None


def cleanup_decision_gap_requests(session: Session, *, limit: int = 2500) -> dict[str, int]:
    """Reject stale decision-spine request rows after their durable gap is closed.

    Rejection preserves the request as audit evidence but removes it from open
    analyst queues. This is intentionally scoped to decision-spine automation
    and draft/review states so live/manual work is not hidden.
    """

    max_limit = max(1, min(int(limit or 2500), 10000))
    rows = (
        session.query(InstrumentRequest)
        .filter(InstrumentRequest.automation_source == "decision_spine")
        .filter(InstrumentRequest.status.in_([REQUEST_STATUS_DRAFT, REQUEST_STATUS_REVIEW]))
        .order_by(InstrumentRequest.updated_at.desc(), InstrumentRequest.id.desc())
        .limit(max_limit)
        .all()
    )
    refs_by_request: dict[int, tuple[set[int], set[str]]] = {}
    all_gap_ids: set[int] = set()
    all_gap_keys: set[str] = set()
    for request in rows:
        refs = _gap_refs_from_request(request)
        refs_by_request[int(request.id)] = refs
        all_gap_ids.update(refs[0])
        all_gap_keys.update(refs[1])
    gaps_by_id = {
        int(gap.id): gap
        for gap in (
            session.query(CoverageGap).filter(CoverageGap.id.in_(sorted(all_gap_ids))).all()
            if all_gap_ids
            else []
        )
    }
    gaps_by_key = {
        str(gap.gap_key): gap
        for gap in (
            session.query(CoverageGap).filter(CoverageGap.gap_key.in_(sorted(all_gap_keys))).all()
            if all_gap_keys
            else []
        )
        if gap.gap_key
    }
    now = _now()
    stats = {
        "requests_checked": len(rows),
        "requests_rejected": 0,
        "resolved_gap_requests": 0,
        "user_authored_gap_requests": 0,
        "out_of_universe_gap_requests": 0,
    }
    active_scope = build_active_universe_request_scope(session)
    for request in rows:
        note = ""
        gap = _linked_gap_from_refs(
            refs_by_request.get(int(request.id), (set(), set())),
            gaps_by_id=gaps_by_id,
            gaps_by_key=gaps_by_key,
        )
        if gap is not None and gap.status == "resolved":
            note = "Closed automatically because the linked decision-spine gap is resolved."
            stats["resolved_gap_requests"] += 1
        elif str(request.source_kind or "").strip() in USER_AUTHORED_GAP_KINDS:
            note = "Closed automatically because formal investment memos are user-authored analyst objects."
            stats["user_authored_gap_requests"] += 1
        elif gap is not None and not _should_auto_queue_gap_request(session, gap, active_scope=active_scope):
            note = _out_of_universe_gap_request_note(gap)
            stats["out_of_universe_gap_requests"] += 1
        if not note:
            continue
        request.status = REQUEST_STATUS_REJECTED
        request.review_note = note
        request.reviewed_at = now
        request.updated_at = now
        stats["requests_rejected"] += 1
    if stats["requests_rejected"]:
        session.flush()
    return stats


def cleanup_transcript_coverage_gaps(session: Session, *, limit: int = 2500) -> dict[str, int]:
    """Resolve stale transcript gaps when the underlying earnings event is closed.

    Some transcript gaps are created before transcript artifacts are linked or
    before future/placeholder earnings rows are corrected. This pass keeps the
    durable gap ledger honest even when no analyst opens that exact company page
    again.
    """

    max_limit = max(1, min(int(limit or 2500), 10000))
    rows = (
        session.query(CoverageGap)
        .filter(CoverageGap.status == "open", CoverageGap.gap_kind == "missing_transcript")
        .order_by(CoverageGap.updated_at.desc(), CoverageGap.id.desc())
        .limit(max_limit)
        .all()
    )
    now_v = _now()
    cutoff = now_v.date() - timedelta(days=180)
    stats = {
        "transcript_gaps_checked": len(rows),
        "transcript_gaps_resolved": 0,
        "transcript_gaps_missing_event": 0,
        "transcript_gaps_with_artifact": 0,
        "transcript_gaps_no_longer_actionable": 0,
    }
    for gap in rows:
        payload = dict(gap.payload or {}) if isinstance(gap.payload, dict) else {}
        try:
            event_id = int(payload.get("earnings_event_id"))
        except Exception:
            continue
        event = session.get(EarningsEvent, event_id)
        if event is None:
            stats["transcript_gaps_missing_event"] += 1
            continue
        fiscal_year, fiscal_quarter = _earnings_event_effective_period(session, event)
        has_transcript = _link_matching_transcript_artifact(
            session,
            event,
            fiscal_year=fiscal_year,
            fiscal_quarter=fiscal_quarter,
        )
        completed = earnings_event_is_effectively_completed(event)
        event_date = _earnings_event_primary_date(event)
        actionable_period = fiscal_quarter is not None and (event_date is None or event_date >= cutoff)
        resolved_by = ""
        if has_transcript:
            resolved_by = "transcript_artifact_linked"
            stats["transcript_gaps_with_artifact"] += 1
        elif not completed or not actionable_period:
            resolved_by = "transcript_gap_no_longer_actionable"
            stats["transcript_gaps_no_longer_actionable"] += 1
        if not resolved_by:
            continue
        payload.update(
            {
                "resolved_by": resolved_by,
                "resolved_at": now_v.isoformat(),
                "earnings_event_id": int(event.id),
                "issuer_id": int(event.issuer_id) if event.issuer_id is not None else payload.get("issuer_id"),
                "fiscal_year": fiscal_year,
                "fiscal_quarter": fiscal_quarter,
                "event_date": event_date.isoformat() if event_date else None,
                "transcript_artifact_id": int(event.transcript_artifact_id) if event.transcript_artifact_id is not None else None,
            }
        )
        gap.payload = payload
        gap.status = "resolved"
        gap.last_reviewed_at = now_v
        gap.updated_at = now_v
        stats["transcript_gaps_resolved"] += 1
    if stats["transcript_gaps_resolved"]:
        session.flush()
    return stats


def _close_subject_gap_request(
    session: Session,
    *,
    subject: ResearchSubject | None,
    gap_suffix: str,
    reason: str,
) -> int:
    if subject is None or subject.id is None:
        return 0
    gap_key = f"coverage_gap:subject:{int(subject.id)}:{str(gap_suffix or '').strip()}"
    now = _now()
    closed = 0
    gap = session.query(CoverageGap).filter_by(gap_key=gap_key).first()
    if gap is not None and gap.status == "open":
        payload = dict(gap.payload or {})
        payload["resolved_by"] = reason
        payload["resolved_at"] = now.isoformat()
        gap.payload = payload
        gap.status = "resolved"
        gap.last_reviewed_at = now
        gap.updated_at = now
        closed += 1
    request = session.query(InstrumentRequest).filter_by(request_key=_gap_request_key_for_value(gap_key)).first()
    if request is not None and request.status in {REQUEST_STATUS_DRAFT, REQUEST_STATUS_REVIEW}:
        request.status = REQUEST_STATUS_REJECTED
        request.review_note = reason
        request.reviewed_at = now
        request.updated_at = now
        closed += 1
    if closed:
        session.flush()
    return closed


def _ensure_research_requests_for_gaps(
    session: Session,
    gaps: list[CoverageGap],
    *,
    subject: ResearchSubject | None,
    company: Company | None,
) -> list[InstrumentRequest]:
    created_or_updated: list[InstrumentRequest] = []
    active_scope = build_active_universe_request_scope(session)
    target_entities = [subject.display_name] if subject is not None and subject.kind in {"company", "issuer"} else None
    target_themes = [subject.display_name] if subject is not None and subject.kind not in {"company", "issuer"} else None
    for gap in gaps:
        request_key = _gap_request_key(gap)
        if gap.status != "open":
            request = session.query(InstrumentRequest).filter_by(request_key=request_key).first()
            if request is not None and request.status in {REQUEST_STATUS_DRAFT, REQUEST_STATUS_REVIEW}:
                request.status = REQUEST_STATUS_REJECTED
                request.review_note = "Closed automatically because the decision-spine gap is resolved."
                request.reviewed_at = _now()
                request.updated_at = request.reviewed_at
            continue
        if gap.gap_kind in USER_AUTHORED_GAP_KINDS:
            _reject_gap_request_if_present(
                session,
                gap=gap,
                note="Not auto-queued because formal investment memos are user-authored analyst objects.",
            )
            continue
        if not _should_auto_queue_gap_request(session, gap, active_scope=active_scope):
            _reject_gap_request_if_present(
                session,
                gap=gap,
                note=_out_of_universe_gap_request_note(gap),
            )
            continue
        gap_payload = dict(gap.payload or {}) if isinstance(gap.payload, dict) else {}
        if subject is None:
            target_entities = None
            target_themes = [str(gap_payload.get("chain_slug") or gap_payload.get("subject") or gap.title).strip()]
        linked_objects = {"gap_id": int(gap.id), "gap_key": gap.gap_key}
        if subject is not None:
            linked_objects["subject_id"] = int(subject.id)
        if gap_payload.get("supply_chain_id") is not None:
            linked_objects["supply_chain_id"] = gap_payload.get("supply_chain_id")
        request = create_instrument_request(
            session,
            title=f"Resolve decision gap: {gap.title}"[:255],
            intent="decision_gap",
            summary=gap.summary,
            target_entities=target_entities,
            target_themes=target_themes,
            desired_outputs=list(gap.suggested_rails or []) or [gap.gap_kind],
            evidence_bundle=[
                {
                    "label": gap.title,
                    "source": "coverage_gap",
                    "object_type": "coverage_gap",
                    "object_id": int(gap.id),
                    "gap_kind": gap.gap_kind,
                }
            ],
            provenance_summary="Auto-created from decision spine missing-data gap.",
            evidence_tier="internal_gap",
            status="draft",
            priority=max(1, min(10, int(gap.severity or 3) + 2)),
            automation_source="decision_spine",
            source_kind=gap.gap_kind,
            draft_payload={"gap": serialize_gap(gap), "company_id": int(company.id) if company is not None else None},
            linked_objects=linked_objects,
            request_key=request_key,
        )
        if request.status == REQUEST_STATUS_REJECTED:
            request.status = REQUEST_STATUS_DRAFT
            request.review_note = None
            request.reviewed_at = None
            request.updated_at = _now()
        created_or_updated.append(request)
    return created_or_updated


def _research_requests_for_subject(session: Session, *, subject: ResearchSubject) -> list[dict[str, Any]]:
    rows = (
        session.query(InstrumentRequest)
        .filter(InstrumentRequest.automation_source == "decision_spine")
        .filter(InstrumentRequest.status != REQUEST_STATUS_REJECTED)
        .order_by(InstrumentRequest.priority.desc(), InstrumentRequest.updated_at.desc())
        .limit(100)
        .all()
    )
    out = []
    for row in rows:
        linked = dict(row.linked_objects or {}) if isinstance(row.linked_objects, dict) else {}
        if str(linked.get("subject_id") or "") != str(subject.id):
            continue
        out.append(serialize_instrument_request(row))
        if len(out) >= 12:
            break
    return out


def _decision_snapshot(
    *,
    memos: list[dict[str, Any]],
    analyst_evidence: list[dict[str, Any]],
    catalysts: list[dict[str, Any]],
    gaps: list[dict[str, Any]],
    active_theses: list[dict[str, Any]],
    research_requests: list[dict[str, Any]],
) -> dict[str, Any]:
    latest_memo = memos[0] if memos else {}
    open_gaps = [gap for gap in gaps if gap.get("status") == "open"]
    what_changed: list[str] = []
    for ev in analyst_evidence[:3]:
        label = ev.get("rating") or ev.get("evidence_kind") or "analyst evidence"
        target = ev.get("price_target")
        suffix = f" to {ev.get('currency') or ''} {target:g}" if isinstance(target, (int, float)) else ""
        summary = ev.get("summary") or ev.get("source_name") or ev.get("outlet") or ""
        what_changed.append(f"{label}{suffix}: {summary}".strip())
    for cat in catalysts[:2]:
        what_changed.append(f"{cat.get('catalyst_kind') or 'catalyst'} on {cat.get('event_date') or 'TBD'}: {cat.get('title')}")
    why_it_matters = []
    if latest_memo.get("summary_md"):
        why_it_matters.append(str(latest_memo.get("summary_md"))[:280])
    if active_theses:
        why_it_matters.append(f"{len(active_theses)} active thesis item(s) are linked; challenge horizon and evidence freshness in the side panel.")
    if open_gaps:
        why_it_matters.append(f"{len(open_gaps)} missing-data warning(s) remain before the decision object is complete.")
    next_actions = [req.get("title") for req in research_requests[:4] if req.get("title")]
    if not next_actions:
        next_actions = [gap.get("title") for gap in open_gaps[:4] if gap.get("title")]
    return {
        "what_changed": what_changed[:6],
        "why_it_matters": why_it_matters[:4],
        "missing_data_warnings": open_gaps[:6],
        "next_actions": next_actions[:6],
        "generated_from": {
            "memos": len(memos),
            "analyst_evidence": len(analyst_evidence),
            "catalysts": len(catalysts),
            "active_theses": len(active_theses),
            "research_requests": len(research_requests),
        },
    }


def _upsert_gap(
    session: Session,
    *,
    key: str,
    title: str,
    kind: str,
    subject: ResearchSubject | None,
    severity: int,
    summary: str,
    rails: list[str],
    payload: dict[str, Any] | None = None,
    open_gap: bool = True,
) -> CoverageGap:
    row = session.query(CoverageGap).filter_by(gap_key=key).first()
    now = _now()
    if row is None:
        row = CoverageGap(
            gap_key=key,
            subject_id=int(subject.id) if subject is not None else None,
            site_id=None,
            program_id=None,
            source_id=None,
            title=title[:255],
            gap_kind=kind[:64],
            status="open" if open_gap else "resolved",
            severity=int(severity),
            summary=summary,
            suggested_rails=list(rails),
            owner_kind="agent",
            owner_ref="decision_spine",
            due_at=None,
            last_reviewed_at=now if not open_gap else None,
            payload=dict(payload or {}),
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    else:
        row.subject_id = int(subject.id) if subject is not None else row.subject_id
        row.title = title[:255]
        row.gap_kind = kind[:64]
        row.status = "open" if open_gap else "resolved"
        row.severity = int(severity)
        row.summary = summary
        row.suggested_rails = list(rails)
        row.payload = dict(payload or {})
        row.last_reviewed_at = now if not open_gap else row.last_reviewed_at
        row.updated_at = now
    session.flush()
    return row


def _extra_values(payload: dict[str, Any], *keys: str) -> list[Any]:
    values: list[Any] = []
    for key in keys:
        raw = payload.get(key)
        if isinstance(raw, (list, tuple, set)):
            values.extend(list(raw))
        elif raw not in (None, ""):
            values.append(raw)
    return values


def _extra_int_values(payload: dict[str, Any], *keys: str) -> set[int]:
    out: set[int] = set()
    for raw in _extra_values(payload, *keys):
        try:
            if isinstance(raw, bool):
                continue
            text = str(raw).strip()
            if not text:
                continue
            out.add(int(text))
        except Exception:
            continue
    return out


def _extra_text_values(payload: dict[str, Any], *keys: str) -> set[str]:
    return {str(raw).strip() for raw in _extra_values(payload, *keys) if str(raw or "").strip()}


_DATASET_BASE_TICKER_SUFFIXES = (".HK", ".KQ", ".KS", ".T", ".TW")


def _ticker_identity_values(*values: object) -> set[str]:
    out: set[str] = set()
    for value in values:
        text = str(value or "").strip().upper()
        if not text:
            continue
        out.add(text)
        if any(text.endswith(suffix) for suffix in _DATASET_BASE_TICKER_SUFFIXES):
            base = text.split(".", 1)[0].strip()
            if len(base) >= 4:
                out.add(base)
    return out


def _dataset_identity_payload(dataset: Dataset) -> dict[str, set[Any]]:
    extras: list[dict[str, Any]] = []
    if isinstance(dataset.extra, dict):
        extras.append(dict(dataset.extra))
        nested = dataset.extra.get("trade_extra")
        if isinstance(nested, dict):
            extras.append(dict(nested))
    company_ids: set[int] = set()
    issuer_ids: set[int] = set()
    tickers: set[str] = set()
    name_keys: set[str] = set()
    for extra in extras:
        company_ids.update(_extra_int_values(extra, "company_id", "company_ids"))
        issuer_ids.update(_extra_int_values(extra, "issuer_id", "issuer_ids"))
        tickers.update(_ticker_identity_values(*_extra_text_values(extra, "ticker", "tickers", "symbol", "symbols", "primary_ticker")))
        name_keys.update(
            canonical_entity_key(token)
            for token in _extra_text_values(extra, "company_name", "company_names", "issuer_name", "issuer_names")
            if canonical_entity_key(token)
        )
    return {"company_ids": company_ids, "issuer_ids": issuer_ids, "tickers": tickers, "name_keys": name_keys}


def _dataset_identity_company_ids(session: Session, dataset: Dataset) -> set[int]:
    identity = _dataset_identity_payload(dataset)
    company_ids: set[int] = set()
    issuer_ids = {int(value) for value in identity["issuer_ids"] if isinstance(value, int)}
    tickers = {str(value) for value in identity["tickers"] if str(value or "").strip()}
    name_keys = {str(value) for value in identity["name_keys"] if str(value or "").strip()}
    company_ids.update(int(value) for value in identity["company_ids"] if isinstance(value, int))
    if company_ids:
        existing_ids = {
            int(row.id)
            for row in session.query(Company.id).filter(Company.id.in_(sorted(company_ids))).all()
            if row.id is not None
        }
        company_ids = existing_ids
    if issuer_ids:
        company_ids.update(
            int(row.id)
            for row in session.query(Company.id).filter(Company.issuer_id.in_(sorted(issuer_ids))).all()
            if row.id is not None
        )
    if tickers:
        company_ids.update(
            int(row.id)
            for row in session.query(Company.id).filter(Company.ticker.in_(sorted(tickers))).all()
            if row.id is not None
        )
        issuer_ticker_rows = (
            session.query(IssuerIdentifier.issuer_id)
            .filter(IssuerIdentifier.id_type == "TICKER", IssuerIdentifier.id_value.in_(sorted(tickers)))
            .all()
        )
        issuer_ids_from_tickers = {int(row.issuer_id) for row in issuer_ticker_rows if row.issuer_id is not None}
        if issuer_ids_from_tickers:
            company_ids.update(
                int(row.id)
                for row in session.query(Company.id).filter(Company.issuer_id.in_(sorted(issuer_ids_from_tickers))).all()
                if row.id is not None
            )
    if name_keys:
        candidates = session.query(Company).order_by(Company.id.asc()).limit(5000).all()
        for company in candidates:
            company_keys = {canonical_entity_key(company.name), canonical_entity_key(company.slug), canonical_entity_key(company.ticker)}
            company_keys = {key for key in company_keys if key}
            if company_keys & name_keys:
                company_ids.add(int(company.id))
    return company_ids


def _ensure_default_analyst_dataset_view(session: Session, dataset: Dataset) -> bool:
    if session.query(DatasetView).filter_by(dataset_id=int(dataset.id)).first() is not None:
        return False
    version = latest_dataset_version(session, int(dataset.id), status="draft") or latest_dataset_version(session, int(dataset.id), status="published")
    if version is None:
        return False
    columns = [col.key for col in sorted(version.columns, key=lambda col: int(col.sort_order or 0)) if str(col.key or "").strip()]
    if not columns:
        return False
    date_key = None
    for col in version.columns:
        if col.role == "timestamp" or col.data_type == "date" or str(col.key).lower() in {"date", "period_date", "as_of_date", "reported_at", "listing_date"}:
            date_key = col.key
            break
    sort_spec = [{"column": date_key, "direction": "desc"}] if date_key else []
    create_dataset_view(
        session,
        dataset,
        name="Default analyst view",
        slug="default-analyst-view",
        description="Auto-created view so dataset rows can be reused in company, calendar, and agent surfaces.",
        columns=columns,
        sort_spec=sort_spec,
        extra={"auto_created": True, "source": "decision_spine"},
    )
    return True


def _ensure_dataset_link_once(
    session: Session,
    *,
    dataset_id: int,
    object_type: str,
    object_id: int,
    relation: str,
    extra: dict[str, Any] | None = None,
) -> bool:
    existing = (
        session.query(DatasetLink.id)
        .filter_by(dataset_id=int(dataset_id), object_type=str(object_type).strip().lower(), object_id=int(object_id))
        .first()
    )
    if existing is not None:
        return False
    link_dataset(
        session,
        dataset_id=int(dataset_id),
        object_type=object_type,
        object_id=int(object_id),
        relation=relation,
        extra=extra,
    )
    return True


def promote_company_dataset_links(
    session: Session,
    *,
    company_id: int | None = None,
    limit: int = 500,
    include_identity: bool = True,
) -> dict[str, int]:
    scoped_company_id = int(company_id) if company_id is not None else None
    fact_company_ids: list[int] | None = None
    if scoped_company_id is not None:
        company = session.get(Company, scoped_company_id)
        fact_company_ids = company_structured_fact_scope_ids(session, company)
    q = session.query(StructuredDataFact.dataset_id, StructuredDataFact.company_id).filter(
        StructuredDataFact.dataset_id.isnot(None),
        StructuredDataFact.company_id.isnot(None),
    )
    if scoped_company_id is not None:
        if not fact_company_ids:
            q = q.filter(StructuredDataFact.company_id == scoped_company_id)
        else:
            q = q.filter(StructuredDataFact.company_id.in_(fact_company_ids))
    pairs = []
    seen: set[tuple[int, int]] = set()
    pair_relation: dict[tuple[int, int], str] = {}
    for dataset_id, fact_company_id in q.order_by(StructuredDataFact.updated_at.desc()).limit(max(1, int(limit))).all():
        target_company_id = scoped_company_id if scoped_company_id is not None else int(fact_company_id)
        pair = (int(dataset_id), int(target_company_id))
        if pair not in seen:
            seen.add(pair)
            pairs.append(pair)
            pair_relation[pair] = "issuer_structured_facts" if scoped_company_id is not None and int(fact_company_id) != scoped_company_id else "structured_facts"
    if include_identity:
        dataset_rows = (
            session.query(Dataset)
            .filter(Dataset.extra.isnot(None))
            .order_by(Dataset.updated_at.desc(), Dataset.id.desc())
            .limit(max(1, int(limit)))
            .all()
        )
        for dataset in dataset_rows:
            for identity_company_id in _dataset_identity_company_ids(session, dataset):
                if scoped_company_id is not None and int(identity_company_id) != scoped_company_id:
                    continue
                pair = (int(dataset.id), int(identity_company_id))
                if pair not in seen:
                    seen.add(pair)
                    pairs.append(pair)
                    pair_relation[pair] = "dataset_identity"
    links = 0
    views = 0
    for dataset_id, fact_company_id in pairs:
        try:
            if _ensure_dataset_link_once(
                session,
                dataset_id=dataset_id,
                object_type="company",
                object_id=fact_company_id,
                relation=pair_relation.get((dataset_id, fact_company_id), "dataset_identity"),
                extra={
                    "source": "decision_spine",
                    "promotion": pair_relation.get((dataset_id, fact_company_id), "dataset_identity"),
                },
            ):
                links += 1
        except Exception:
            pass
        dataset = session.query(Dataset).filter_by(id=int(dataset_id)).first()
        if dataset is None:
            continue
        if _ensure_default_analyst_dataset_view(session, dataset):
            views += 1
    session.flush()
    return {"links": links, "views": views}


def _instrument_identity_targets(
    session: Session,
    instrument: ResearchInstrument,
    *,
    company: Company | None,
    issuer: Issuer | None,
) -> dict[str, set[Any]]:
    tickers = _ticker_identity_values(instrument.ticker, instrument.yahoo_symbol, company.ticker if company is not None else None)
    if issuer is not None:
        for ident in session.query(IssuerIdentifier).filter_by(issuer_id=int(issuer.id), id_type="TICKER").limit(20).all():
            tickers.update(_ticker_identity_values(ident.id_value))
    name_values = [
        instrument.display_name,
        instrument.legal_name,
        company.name if company is not None else None,
        company.slug if company is not None else None,
        issuer.display_name if issuer is not None else None,
        issuer.legal_name if issuer is not None else None,
        issuer.slug if issuer is not None else None,
    ]
    return {
        "company_ids": {int(company.id)} if company is not None and company.id is not None else set(),
        "issuer_ids": {int(issuer.id)} if issuer is not None and issuer.id is not None else set(),
        "tickers": tickers,
        "name_keys": {canonical_entity_key(value) for value in name_values if canonical_entity_key(value)},
    }


def _dataset_identity_matches_targets(dataset_identity: dict[str, set[Any]], targets: dict[str, set[Any]]) -> bool:
    for key in ("company_ids", "issuer_ids", "tickers", "name_keys"):
        if set(dataset_identity.get(key) or set()) & set(targets.get(key) or set()):
            return True
    return False


def promote_universe_instrument_dataset_links(
    session: Session,
    *,
    instrument: ResearchInstrument,
    limit: int = 750,
) -> dict[str, int]:
    """
    Fast, no-network promotion for a newly tracked universe instrument.

    This deliberately covers only deterministic identity matches: explicit
    dataset company/issuer ids, ticker aliases, or exact normalized company
    names. Role/site/theme-specific mappings stay in Codex review requests.
    """
    company = session.query(Company).filter_by(id=int(instrument.company_id)).first() if instrument.company_id is not None else None
    issuer = session.query(Issuer).filter_by(id=int(instrument.issuer_id)).first() if instrument.issuer_id is not None else None
    identity_resolved = 0
    if company is None or issuer is None:
        before = (instrument.company_id, instrument.issuer_id)
        company, issuer = _ensure_company_issuer_for_instrument(session, instrument)
        after = (instrument.company_id, instrument.issuer_id)
        identity_resolved = 1 if after != before and (company is not None or issuer is not None) else 0
    if company is None and issuer is None:
        return {"identity_resolved": identity_resolved, "datasets_examined": 0, "links": 0, "views": 0}

    links = 0
    views = 0
    seen_dataset_ids: set[int] = set()
    company_id = int(company.id) if company is not None and company.id is not None else None
    issuer_id = int(issuer.id) if issuer is not None and issuer.id is not None else None

    if company_id is not None:
        fact_dataset_ids = [
            int(row[0])
            for row in session.query(StructuredDataFact.dataset_id)
            .filter(StructuredDataFact.company_id == company_id, StructuredDataFact.dataset_id.isnot(None))
            .order_by(StructuredDataFact.updated_at.desc())
            .limit(max(1, int(limit)))
            .all()
            if row[0] is not None
        ]
        for dataset_id in fact_dataset_ids:
            dataset = session.query(Dataset).filter_by(id=int(dataset_id)).first()
            if dataset is None:
                continue
            seen_dataset_ids.add(int(dataset.id))
            if _ensure_dataset_link_once(
                session,
                dataset_id=int(dataset.id),
                object_type="company",
                object_id=company_id,
                relation="structured_facts",
                extra={"source": "universe_entry", "promotion": "structured_facts", "research_instrument_id": int(instrument.id)},
            ):
                links += 1
            if _ensure_default_analyst_dataset_view(session, dataset):
                views += 1

    targets = _instrument_identity_targets(session, instrument, company=company, issuer=issuer)
    examined = 0
    directory = session.info.get("universe_population_directory")
    datasets = directory.datasets(limit) if directory else (
        session.query(Dataset)
        .filter(Dataset.extra.isnot(None))
        .order_by(Dataset.updated_at.desc(), Dataset.id.desc())
        .limit(max(1, int(limit)))
        .all()
    )
    for dataset in datasets:
        examined += 1
        identity = directory.dataset_identity(dataset) if directory else _dataset_identity_payload(dataset)
        if not _dataset_identity_matches_targets(identity, targets):
            continue
        seen_dataset_ids.add(int(dataset.id))
        if company_id is not None and _ensure_dataset_link_once(
            session,
            dataset_id=int(dataset.id),
            object_type="company",
            object_id=company_id,
            relation="dataset_identity",
            extra={"source": "universe_entry", "promotion": "dataset_identity", "research_instrument_id": int(instrument.id)},
        ):
            links += 1
        if issuer_id is not None and _ensure_dataset_link_once(
            session,
            dataset_id=int(dataset.id),
            object_type="issuer",
            object_id=issuer_id,
            relation="dataset_identity",
            extra={"source": "universe_entry", "promotion": "dataset_identity", "research_instrument_id": int(instrument.id)},
        ):
            links += 1
        if _ensure_default_analyst_dataset_view(session, dataset):
            views += 1

    session.flush()
    if links or views or identity_resolved:
        clear_decision_payload_cache()
    return {"identity_resolved": identity_resolved, "datasets_examined": examined, "links": links, "views": views, "datasets_matched": len(seen_dataset_ids)}


def refresh_decision_gaps(
    session: Session,
    *,
    company: Company | None = None,
    issuer: Issuer | None = None,
) -> list[dict[str, Any]]:
    subject = ensure_decision_subject(session, company=company, issuer=issuer)
    if issuer is None and company is not None and company.issuer_id is not None:
        issuer = session.query(Issuer).filter_by(id=int(company.issuer_id)).first()
    if company is None and issuer is not None:
        company = _company_for_issuer(session, issuer)
    rows: list[CoverageGap] = []
    if subject is None:
        return []
    memo_exists = session.query(InvestmentMemo.id).filter(InvestmentMemo.subject_id == int(subject.id), InvestmentMemo.status != "archived").first() is not None
    analyst_evidence_exists = (
        session.query(AnalystEvidence.id)
        .filter(AnalystEvidence.status != "archived")
        .filter(
            or_(
                AnalystEvidence.subject_id == int(subject.id),
                AnalystEvidence.company_id == int(company.id) if company is not None else AnalystEvidence.company_id == -1,
                AnalystEvidence.issuer_id == int(issuer.id) if issuer is not None else AnalystEvidence.issuer_id == -1,
            )
        )
        .first()
        is not None
    )
    rows.append(
        _upsert_gap(
            session,
            key=f"coverage_gap:subject:{int(subject.id)}:investment_memo",
            title=f"Investment memo missing for {subject.display_name}",
            kind="missing_investment_memo",
            subject=subject,
            severity=2,
            summary="No structured investment memo exists yet for thesis, valuation, catalysts, and risks.",
            rails=["manual", "codex", "local_agent"],
            payload={"subject_key": subject.subject_key},
            open_gap=not memo_exists,
        )
    )
    rows.append(
        _upsert_gap(
            session,
            key=f"coverage_gap:subject:{int(subject.id)}:analyst_evidence",
            title=f"Analyst evidence missing for {subject.display_name}",
            kind="missing_analyst_evidence",
            subject=subject,
            severity=1,
            summary="No durable analyst/media evidence object is linked yet for ratings, price targets, estimates, or trusted professional claims.",
            rails=["curated_media", "manual", "codex", "local_agent"],
            payload={"subject_key": subject.subject_key},
            open_gap=not analyst_evidence_exists,
        )
    )
    if company is not None:
        doc_count = session.query(CompanyDocument.id).filter_by(company_id=int(company.id)).count()
        structured_doc_scope = _company_structured_fact_document_scope(session, company)
        structured_candidate_count = int(structured_doc_scope.get("candidate_document_count") or 0)
        fact_company_ids = company_structured_fact_scope_ids(session, company)
        fact_count = (
            session.query(StructuredDataFact.id)
            .filter(StructuredDataFact.company_id.in_(fact_company_ids))
            .count()
            if fact_company_ids
            else 0
        )
        fact_backfill: dict[str, Any] = {}
        fact_promotion: dict[str, Any] = {}
        sec_companyfacts_source: dict[str, Any] = {}
        if structured_candidate_count > 0 and fact_count == 0:
            fact_backfill = sync_company_earnings_document_facts(session, company=company, limit=12)
            if int(fact_backfill.get("facts_created") or 0) or int(fact_backfill.get("facts_updated") or 0):
                fact_backfill["promotion"] = promote_company_dataset_links(session, company_id=int(company.id), include_identity=False)
                fact_count = (
                    session.query(StructuredDataFact.id)
                    .filter(StructuredDataFact.company_id.in_(fact_company_ids))
                    .count()
                    if fact_company_ids
                    else 0
                )
            if fact_count == 0:
                sec_companyfacts_source = ensure_company_sec_companyfacts_source(
                    session,
                    company=company,
                    reason="decision_spine_missing_structured_facts",
                )
                if sec_companyfacts_source.get("registered") and sec_companyfacts_source.get("should_enqueue"):
                    job = enqueue_ingest_job(session, source_id=int(sec_companyfacts_source["source_id"]))
                    sec_companyfacts_source["ingest_job_id"] = int(job.id)
        if fact_count > 0:
            fact_promotion = promote_company_dataset_links(session, company_id=int(company.id), include_identity=False)
        rows.append(
            _upsert_gap(
                session,
                key=f"coverage_gap:company:{int(company.id)}:structured_facts",
                title=f"Structured facts missing for {company.name}",
                kind="missing_structured_facts",
                subject=subject,
                severity=3,
                summary="Company has source documents but no normalized structured facts in the investor timeline.",
                rails=["filings", "earnings", "datasets", "local_agent"],
                payload={
                    "company_id": int(company.id),
                    "company_document_count": int(doc_count),
                    "structured_fact_document_scope": structured_doc_scope,
                    "structured_fact_company_ids": fact_company_ids,
                    "backfill": fact_backfill,
                    "promotion": fact_promotion,
                    "sec_companyfacts_source": sec_companyfacts_source,
                },
                open_gap=structured_candidate_count > 0 and fact_count == 0,
            )
        )
        dataset_ids = {
            int(row[0])
            for row in session.query(StructuredDataFact.dataset_id)
            .filter(StructuredDataFact.company_id.in_(fact_company_ids), StructuredDataFact.dataset_id.isnot(None))
            .all()
            if row[0] is not None
        } if fact_company_ids else set()
        unviewed = [
            dataset_id
            for dataset_id in dataset_ids
            if session.query(DatasetView.id).filter_by(dataset_id=int(dataset_id)).first() is None
        ]
        rows.append(
            _upsert_gap(
                session,
                key=f"coverage_gap:company:{int(company.id)}:dataset_views",
                title=f"Dataset views missing for {company.name}",
                kind="unpromoted_datasets",
                subject=subject,
                severity=2,
                summary="Company-linked datasets exist but have not been promoted into reusable saved views.",
                rails=["datasets", "charts", "local_agent"],
                payload={"dataset_ids": sorted(unviewed)[:50], "dataset_count": len(dataset_ids)},
                open_gap=bool(unviewed),
            )
        )
    if issuer is not None:
        transcript_gap_cutoff = _now().date() - timedelta(days=180)
        transcript_gap_keys: set[str] = set()
        recent_events = (
            session.query(EarningsEvent)
            .filter(EarningsEvent.issuer_id == int(issuer.id))
            .filter(
                or_(
                    EarningsEvent.actual_release_at >= datetime.combine(transcript_gap_cutoff, time.min),
                    EarningsEvent.actual_call_at >= datetime.combine(transcript_gap_cutoff, time.min),
                    EarningsEvent.scheduled_date >= transcript_gap_cutoff,
                )
            )
            .order_by(EarningsEvent.updated_at.desc())
            .limit(12)
            .all()
        )
        for event in recent_events:
            completed = earnings_event_is_effectively_completed(event)
            fiscal_year, fiscal_quarter = _earnings_event_effective_period(session, event)
            has_transcript = _link_matching_transcript_artifact(
                session,
                event,
                fiscal_year=fiscal_year,
                fiscal_quarter=fiscal_quarter,
            )
            transcript_payload = _transcript_artifact_readability_payload(session, event.transcript_artifact_id)
            event_date = _earnings_event_primary_date(event)
            actionable_period = fiscal_quarter is not None and (
                event_date is None or event_date >= transcript_gap_cutoff
            )
            period_label = f"FY{fiscal_year or event.fiscal_year}" + (
                f" Q{fiscal_quarter}" if fiscal_quarter is not None else ""
            )
            transcript_reason = str(transcript_payload.get("transcript_gap_reason") or "")
            unreadable = transcript_reason in {"linked_unreadable_transcript", "linked_transcript_without_document"}
            gap_title = (
                f"Transcript unreadable for {period_label}"
                if unreadable
                else f"Transcript missing for {period_label}"
            )
            gap_summary = (
                "A transcript artifact is linked, but its local document is a metadata-only placeholder or failed/short extraction."
                if unreadable
                else "Earnings release/filing is present but no associated transcript artifact is linked."
            )
            gap_key = f"coverage_gap:earnings_event:{int(event.id)}:transcript"
            transcript_gap_keys.add(gap_key)
            rows.append(
                _upsert_gap(
                    session,
                    key=gap_key,
                    title=gap_title,
                    kind="missing_transcript",
                    subject=subject,
                    severity=4,
                    summary=gap_summary,
                    rails=["transcripts", "local_agent", "codex"],
                    payload={
                        "earnings_event_id": int(event.id),
                        "issuer_id": int(issuer.id),
                        "fiscal_year": fiscal_year,
                        "fiscal_quarter": fiscal_quarter,
                        "event_date": event_date.isoformat() if event_date else None,
                        "transcript_gap_cutoff": transcript_gap_cutoff.isoformat(),
                        **transcript_payload,
                    },
                    open_gap=completed and actionable_period and not has_transcript,
                )
            )
        stale_transcript_gaps = (
            session.query(CoverageGap)
            .filter(CoverageGap.status == "open", CoverageGap.gap_kind == "missing_transcript")
            .all()
        )
        now_v = _now()
        for gap in stale_transcript_gaps:
            payload = dict(gap.payload or {}) if isinstance(gap.payload, dict) else {}
            try:
                gap_issuer_id = int(payload.get("issuer_id"))
            except Exception:
                continue
            if gap_issuer_id != int(issuer.id) or gap.gap_key in transcript_gap_keys:
                continue
            payload["resolved_by"] = "transcript_gap_window"
            payload["resolved_at"] = now_v.isoformat()
            payload["transcript_gap_cutoff"] = transcript_gap_cutoff.isoformat()
            gap.payload = payload
            gap.status = "resolved"
            gap.last_reviewed_at = now_v
            gap.updated_at = now_v
            rows.append(gap)
    report = session.query(ResearchReport).filter_by(subject_key=subject.subject_key).first()
    rows.append(
        _upsert_gap(
            session,
            key=f"coverage_gap:subject:{int(subject.id)}:research_report",
            title=f"Research report missing for {subject.display_name}",
            kind="missing_research_report",
            subject=subject,
            severity=2,
            summary="No active research report container is linked to this subject.",
            rails=["reports", "codex", "local_agent"],
            payload={"subject_key": subject.subject_key},
            open_gap=report is None,
        )
    )
    report_version = None
    if report is not None:
        report_version = (
            session.query(ResearchReportVersion)
            .filter_by(report_id=int(report.id), status="published")
            .order_by(ResearchReportVersion.version.desc(), ResearchReportVersion.id.desc())
            .first()
        )
    memory_quality = report_memory_quality(report_version)
    memory_review = report_memory_review_recommendation(report_version)
    report_has_canonical_memory = bool(report_version is not None and not memory_quality.get("needs_review"))
    review_label = str(memory_review.get("label") or "Review report memory")
    review_summary = str(memory_review.get("summary") or "A report container exists but no published, structured report memory version is available for analyst or agent review.")
    rows.append(
        _upsert_gap(
            session,
            key=f"coverage_gap:subject:{int(subject.id)}:report_memory",
            title=f"{review_label} for {subject.display_name}",
            kind="missing_report_memory",
            subject=subject,
            severity=int(memory_review.get("priority") or 6),
            summary=review_summary,
            rails=list(memory_review.get("desired_outputs") or []) or ["reports", "codex", "local_agent"],
            payload={
                "subject_key": subject.subject_key,
                "report_id": int(report.id) if report is not None else None,
                "memory_quality": memory_quality,
                "memory_review": memory_review,
            },
            open_gap=report is not None and not report_has_canonical_memory,
        )
    )
    _ensure_research_requests_for_gaps(session, rows, subject=subject, company=company)
    return [serialize_gap(row) for row in rows]


def _active_supply_chain_duplicate_map(session: Session) -> dict[int, int]:
    """Return active duplicate chain ids mapped to their canonical active chain id."""

    active_chains = session.query(SupplyChain).filter(SupplyChain.status == "active").order_by(SupplyChain.id.asc()).all()
    groups: dict[tuple[str, str], list[SupplyChain]] = {}
    for chain in active_chains:
        title_key = canonical_entity_key(chain.title)
        if not title_key:
            continue
        category_key = canonical_entity_key(chain.category or "")
        groups.setdefault((title_key, category_key), []).append(chain)

    candidate_ids = [int(chain.id) for rows in groups.values() for chain in rows if len(rows) > 1 and chain.id is not None]
    bound_chain_ids: set[int] = set()
    if candidate_ids:
        bound_chain_ids = {
            int(row.chain_id)
            for row in (
                session.query(SupplyChainSnapshot.chain_id)
                .join(SupplyChainMetricBinding, SupplyChainMetricBinding.snapshot_id == SupplyChainSnapshot.id)
                .filter(SupplyChainSnapshot.chain_id.in_(candidate_ids))
                .distinct()
                .all()
            )
            if row.chain_id is not None
        }

    duplicate_map: dict[int, int] = {}
    for rows in groups.values():
        if len(rows) <= 1:
            continue
        sorted_rows = sorted(rows, key=lambda item: int(item.id))
        bound_rows = [row for row in sorted_rows if int(row.id) in bound_chain_ids]
        canonical = bound_rows[0] if bound_rows else sorted_rows[0]
        canonical_id = int(canonical.id)
        for row in sorted_rows:
            row_id = int(row.id)
            if row_id != canonical_id:
                duplicate_map[row_id] = canonical_id
    return duplicate_map


def refresh_supply_chain_metric_gaps(session: Session) -> int:
    count = 0
    rows: list[CoverageGap] = []
    duplicate_map = _active_supply_chain_duplicate_map(session)
    for chain in session.query(SupplyChain).filter(SupplyChain.status == "active").order_by(SupplyChain.id.asc()).all():
        binding_exists = (
            session.query(SupplyChainMetricBinding.id)
            .join(SupplyChainSnapshot, SupplyChainSnapshot.id == SupplyChainMetricBinding.snapshot_id)
            .filter(SupplyChainSnapshot.chain_id == int(chain.id))
            .first()
            is not None
        )
        duplicate_of_chain_id = duplicate_map.get(int(chain.id))
        is_duplicate = duplicate_of_chain_id is not None
        summary = "Supply-chain graph exists but no dataset metric bindings are attached."
        if is_duplicate:
            summary = (
                "Supply-chain graph is an active duplicate without metric bindings; "
                "preserve it as evidence but do not auto-queue analyst follow-up."
            )
        rows.append(
            _upsert_gap(
                session,
                key=f"coverage_gap:supply_chain:{int(chain.id)}:metric_bindings",
                title=f"Metric bindings missing for {chain.title}",
                kind="missing_supply_chain_metric_bindings",
                subject=None,
                severity=7 if is_duplicate else 4,
                summary=summary,
                rails=["trade_customs", "datasets", "supply_chain"],
                payload={
                    "supply_chain_id": int(chain.id),
                    "chain_slug": chain.slug,
                    "chain_title": chain.title,
                    "auto_queue": not is_duplicate,
                    "actionability": "preserved_duplicate" if is_duplicate else "actionable",
                    "duplicate_of_supply_chain_id": duplicate_of_chain_id,
                },
                open_gap=not binding_exists,
            )
        )
        count += 1
    _ensure_research_requests_for_gaps(session, rows, subject=None, company=None)
    return count


def _subject_has_decision_gap_state(session: Session, subject: ResearchSubject | None) -> bool:
    if subject is None or subject.id is None:
        return False
    return (
        session.query(CoverageGap.id)
        .filter(CoverageGap.subject_id == int(subject.id), CoverageGap.owner_ref == "decision_spine")
        .first()
        is not None
    )


def refresh_active_universe_decision_gaps(
    session: Session,
    *,
    limit: int = 250,
    existing_only: bool = True,
) -> int:
    pairs: list[tuple[int | None, int | None]] = []
    seen: set[tuple[int | None, int | None]] = set()
    rows = (
        session.query(ResearchInstrument.company_id, ResearchInstrument.issuer_id)
        .join(ResearchUniverseRow, ResearchUniverseRow.instrument_id == ResearchInstrument.id)
        .join(ResearchUniverse, ResearchUniverse.id == ResearchUniverseRow.universe_id)
        .filter(ResearchUniverse.status == "active")
        .filter(ResearchUniverseRow.archived_at.is_(None))
        .filter(or_(ResearchInstrument.company_id.isnot(None), ResearchInstrument.issuer_id.isnot(None)))
        .order_by(ResearchUniverse.sort_order.asc(), ResearchUniverseRow.sort_order.asc(), ResearchUniverseRow.id.asc())
        .limit(max(1, int(limit or 250)))
        .all()
    )
    for company_id, issuer_id in rows:
        key = (
            int(company_id) if company_id is not None else None,
            int(issuer_id) if issuer_id is not None else None,
        )
        if key in seen:
            continue
        seen.add(key)
        pairs.append(key)
    count = 0
    for company_id, issuer_id in pairs:
        company = session.get(Company, int(company_id)) if company_id is not None else None
        issuer = session.get(Issuer, int(issuer_id)) if issuer_id is not None else None
        if issuer is None and company is not None and company.issuer_id is not None:
            issuer = session.get(Issuer, int(company.issuer_id))
        if company is None and issuer is None:
            continue
        subject = _existing_decision_subject(session, company=company, issuer=issuer)
        if existing_only and not _subject_has_decision_gap_state(session, subject):
            continue
        refresh_decision_gaps(session, company=company, issuer=issuer)
        count += 1
    return count


def serialize_gap(row: CoverageGap) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "gap_key": row.gap_key,
        "subject_id": int(row.subject_id) if row.subject_id is not None else None,
        "title": row.title,
        "gap_kind": row.gap_kind,
        "status": row.status,
        "severity": int(row.severity or 0),
        "summary": row.summary,
        "suggested_rails": list(row.suggested_rails or []),
        "payload": dict(row.payload or {}),
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def sync_decision_spine(
    session: Session,
    *,
    company_id: int | None = None,
    issuer_id: int | None = None,
) -> dict[str, int]:
    company = session.query(Company).filter_by(id=int(company_id)).first() if company_id is not None else None
    issuer = session.query(Issuer).filter_by(id=int(issuer_id)).first() if issuer_id is not None else None
    if issuer is None and company is not None and company.issuer_id is not None:
        issuer = session.query(Issuer).filter_by(id=int(company.issuer_id)).first()
    issuer_filter = int(issuer.id) if issuer is not None else None
    stats = {
        "earnings_catalysts": sync_earnings_catalysts(session, issuer_id=issuer_filter),
        "ipo_catalysts": sync_ipo_catalysts(session, issuer_id=issuer_filter),
        "dataset_links": 0,
        "dataset_views": 0,
        "supply_chain_gap_checks": 0,
        "active_universe_gap_checks": 0,
        "research_requests": 0,
    }
    promotion = promote_company_dataset_links(session, company_id=int(company.id) if company is not None else None)
    stats["dataset_links"] = int(promotion.get("links", 0))
    stats["dataset_views"] = int(promotion.get("views", 0))
    if company is not None or issuer is not None:
        refresh_decision_gaps(session, company=company, issuer=issuer)
    else:
        stats["supply_chain_gap_checks"] = refresh_supply_chain_metric_gaps(session)
        stats["active_universe_gap_checks"] = refresh_active_universe_decision_gaps(session)
    transcript_cleanup = cleanup_transcript_coverage_gaps(session)
    stats["transcript_gaps_checked"] = int(transcript_cleanup.get("transcript_gaps_checked", 0))
    stats["transcript_gaps_resolved"] = int(transcript_cleanup.get("transcript_gaps_resolved", 0))
    cleanup = cleanup_decision_gap_requests(session)
    stats["decision_gap_requests_checked"] = int(cleanup.get("requests_checked", 0))
    stats["decision_gap_requests_rejected"] = int(cleanup.get("requests_rejected", 0))
    stats["decision_gap_requests_rejected_out_of_universe"] = int(cleanup.get("out_of_universe_gap_requests", 0))
    stats["research_requests"] = session.query(InstrumentRequest.id).filter(InstrumentRequest.automation_source == "decision_spine").count()
    session.flush()
    clear_decision_payload_cache()
    return stats


def _query_has_rows(query: Any) -> bool:
    return query.limit(1).first() is not None


def _serialize_decision_page_memo(row: InvestmentMemo) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "title": row.title,
        "status": row.status,
        "rating": row.rating,
        "summary_md": row.summary_md,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _serialize_decision_page_analyst_evidence(row: AnalystEvidence) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "evidence_kind": row.evidence_kind,
        "source_name": row.source_name,
        "author": row.author,
        "outlet": row.outlet,
        "rating": row.rating,
        "price_target": _parse_price_target(row.price_target),
        "currency": row.currency,
        "trust_label": row.trust_label,
        "summary": row.summary,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _serialize_decision_page_catalyst(row: InvestmentCatalyst) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "title": row.title,
        "catalyst_kind": row.catalyst_kind,
        "event_date": row.event_date.isoformat() if row.event_date else None,
        "status": row.status,
        "priority": int(row.priority or 0),
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _serialize_decision_page_gap(row: CoverageGap) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "gap_key": row.gap_key,
        "subject_id": int(row.subject_id) if row.subject_id is not None else None,
        "title": row.title,
        "gap_kind": row.gap_kind,
        "status": row.status,
        "severity": int(row.severity or 0),
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _serialize_decision_page_earnings_packet(row: EarningsEvent) -> dict[str, Any]:
    review_state = {
        "filing_or_exchange_disclosure": bool(row.release_filing_id or row.release_disclosure_id),
        "earnings_release": bool(row.release_filing_id or row.release_disclosure_id or row.release_artifact_id),
        "call_transcript": bool(row.transcript_artifact_id),
        "call_access": bool(row.transcript_artifact_id or row.webcast_artifact_id),
    }
    required = ["filing_or_exchange_disclosure", "earnings_release", "call_transcript"]
    missing = [key for key in required if not review_state.get(key)]
    score = sum(1 for key in required if review_state.get(key)) / max(1, len(required))
    return {
        "id": int(row.id),
        "period_label": f"FY{row.fiscal_year} Q{row.fiscal_quarter}" if row.fiscal_quarter is not None else f"FY{row.fiscal_year}",
        "status": row.status,
        "missing": missing,
        "completeness_score": score,
    }


def _decision_page_active_thesis_exists(
    session: Session,
    *,
    subject: ResearchSubject | None,
    company: Company | None,
    issuer: Issuer | None,
) -> bool:
    name_keys, tickers = _subject_match_terms(session, subject=subject, company=company, issuer=issuer)
    if not name_keys and not tickers:
        return False

    q = session.query(Thesis.id).filter(Thesis.status.in_(["watching", "active", "confirmed"]))
    text_conditions = []
    for ticker in sorted(tickers)[:6]:
        ticker_key = str(ticker or "").strip().lower()
        if len(ticker_key) < 2:
            continue
        pattern = f"%{ticker_key}%"
        text_conditions.append(func.lower(Thesis.title).like(pattern))
        text_conditions.append(func.lower(Thesis.statement).like(pattern))
    for name_key in sorted(name_keys, key=len, reverse=True)[:6]:
        key = str(name_key or "").strip().lower()
        if len(key) < 4:
            continue
        pattern = f"%{key}%"
        text_conditions.append(func.lower(Thesis.title).like(pattern))
        text_conditions.append(func.lower(Thesis.statement).like(pattern))
    if text_conditions and _query_has_rows(q.filter(or_(*text_conditions))):
        return True

    entity_conditions = []
    for term in [*sorted(tickers), *sorted(name_keys, key=len, reverse=True)[:6]]:
        key = str(term or "").strip().lower()
        if len(key) < 2:
            continue
        entity_conditions.append(func.lower(Entity.canonical_name).like(f"%{key}%"))
    if not entity_conditions:
        return False
    return _query_has_rows(
        session.query(Thesis.id)
        .join(thesis_entities, thesis_entities.c.thesis_id == Thesis.id)
        .join(Entity, Entity.id == thesis_entities.c.entity_id)
        .filter(Thesis.status.in_(["watching", "active", "confirmed"]))
        .filter(or_(*entity_conditions))
    )


def company_decision_page_payload(
    session: Session,
    *,
    company: Company | None = None,
    issuer: Issuer | None = None,
    include_earnings_packets: bool = True,
) -> dict[str, Any]:
    """Build the small decision-spine slice rendered by the company detail page."""

    if issuer is None and company is not None and company.issuer_id is not None:
        issuer = session.query(Issuer).filter_by(id=int(company.issuer_id)).first()
    if company is None and issuer is not None:
        company = _company_for_issuer(session, issuer)
    subject = _existing_decision_subject(session, company=company, issuer=issuer)
    if subject is None:
        return {}

    memo_rows = (
        session.query(InvestmentMemo)
        .options(
            load_only(
                InvestmentMemo.id,
                InvestmentMemo.title,
                InvestmentMemo.status,
                InvestmentMemo.rating,
                InvestmentMemo.summary_md,
                InvestmentMemo.updated_at,
            )
        )
        .filter(InvestmentMemo.subject_id == int(subject.id), InvestmentMemo.status != "archived")
        .order_by(InvestmentMemo.updated_at.desc(), InvestmentMemo.id.desc())
        .limit(1)
        .all()
    )
    gap_rows = (
        session.query(CoverageGap)
        .options(
            load_only(
                CoverageGap.id,
                CoverageGap.gap_key,
                CoverageGap.subject_id,
                CoverageGap.title,
                CoverageGap.gap_kind,
                CoverageGap.status,
                CoverageGap.severity,
                CoverageGap.updated_at,
            )
        )
        .filter(CoverageGap.subject_id == int(subject.id), CoverageGap.status != "resolved")
        .order_by(CoverageGap.severity.desc(), CoverageGap.updated_at.desc(), CoverageGap.id.desc())
        .limit(8)
        .all()
    )
    catalyst_filters = []
    if company is not None:
        catalyst_filters.append(InvestmentCatalyst.company_id == int(company.id))
    if issuer is not None:
        catalyst_filters.append(InvestmentCatalyst.issuer_id == int(issuer.id))
    today = calendar_today()
    lower = today - timedelta(days=180)
    upper = today + timedelta(days=365)
    catalyst_q = (
        session.query(InvestmentCatalyst)
        .options(
            load_only(
                InvestmentCatalyst.id,
                InvestmentCatalyst.title,
                InvestmentCatalyst.catalyst_kind,
                InvestmentCatalyst.event_date,
                InvestmentCatalyst.status,
                InvestmentCatalyst.priority,
                InvestmentCatalyst.updated_at,
            )
        )
        .filter(or_(InvestmentCatalyst.event_date.is_(None), InvestmentCatalyst.event_date.between(lower, upper)))
    )
    if catalyst_filters:
        catalyst_q = catalyst_q.filter(or_(*catalyst_filters))
    catalyst_rows = (
        catalyst_q.order_by(
            InvestmentCatalyst.event_date.asc().nullslast(),
            InvestmentCatalyst.priority.desc(),
            InvestmentCatalyst.updated_at.desc(),
        )
        .limit(4)
        .all()
    )
    evidence_filters = [AnalystEvidence.subject_id == int(subject.id)]
    if company is not None:
        evidence_filters.append(AnalystEvidence.company_id == int(company.id))
    if issuer is not None:
        evidence_filters.append(AnalystEvidence.issuer_id == int(issuer.id))
    analyst_rows = (
        session.query(AnalystEvidence)
        .options(
            load_only(
                AnalystEvidence.id,
                AnalystEvidence.evidence_kind,
                AnalystEvidence.source_name,
                AnalystEvidence.author,
                AnalystEvidence.outlet,
                AnalystEvidence.rating,
                AnalystEvidence.price_target,
                AnalystEvidence.currency,
                AnalystEvidence.trust_label,
                AnalystEvidence.summary,
                AnalystEvidence.updated_at,
                AnalystEvidence.published_at,
            )
        )
        .filter(AnalystEvidence.status != "archived")
        .filter(or_(*evidence_filters))
        .order_by(
            AnalystEvidence.published_at.desc().nullslast(),
            AnalystEvidence.updated_at.desc(),
            AnalystEvidence.id.desc(),
        )
        .limit(4)
        .all()
    )

    report_exists = _query_has_rows(session.query(ResearchReport.id).filter_by(subject_key=subject.subject_key))
    fact_company_ids = company_structured_fact_scope_ids(session, company) if company is not None else []
    fact_exists = (
        _query_has_rows(session.query(StructuredDataFact.id).filter(StructuredDataFact.company_id.in_(fact_company_ids)))
        if fact_company_ids
        else False
    )
    dataset_exists = (
        _query_has_rows(
            session.query(StructuredDataFact.dataset_id)
            .filter(StructuredDataFact.company_id.in_(fact_company_ids), StructuredDataFact.dataset_id.isnot(None))
        )
        if fact_company_ids
        else False
    )
    earnings_packets: list[dict[str, Any]] = []
    if include_earnings_packets and issuer is not None:
        earnings_rows = (
            session.query(EarningsEvent)
            .options(
                load_only(
                    EarningsEvent.id,
                    EarningsEvent.fiscal_year,
                    EarningsEvent.fiscal_quarter,
                    EarningsEvent.status,
                    EarningsEvent.release_filing_id,
                    EarningsEvent.release_disclosure_id,
                    EarningsEvent.release_artifact_id,
                    EarningsEvent.transcript_artifact_id,
                    EarningsEvent.webcast_artifact_id,
                    EarningsEvent.updated_at,
                )
            )
            .filter(EarningsEvent.issuer_id == int(issuer.id))
            .order_by(
                EarningsEvent.fiscal_year.desc(),
                EarningsEvent.fiscal_quarter.desc().nullslast(),
                EarningsEvent.updated_at.desc(),
            )
            .limit(6)
            .all()
        )
        earnings_packets = [_serialize_decision_page_earnings_packet(row) for row in earnings_rows]
    score_inputs = {
        "memo": bool(memo_rows),
        "report": report_exists,
        "facts": fact_exists,
        "datasets": dataset_exists,
        "catalysts": bool(catalyst_rows),
        "analyst_evidence": bool(analyst_rows),
        "active_thesis": _decision_page_active_thesis_exists(session, subject=subject, company=company, issuer=issuer),
        "earnings_packets": any(packet.get("completeness_score", 0) >= 1 for packet in earnings_packets),
    }
    coverage_score = round(sum(1 for value in score_inputs.values() if value) / max(1, len(score_inputs)) * 100)
    return {
        "subject": {
            "id": int(subject.id),
            "subject_key": subject.subject_key,
            "slug": subject.slug,
            "display_name": subject.display_name,
            "primary_ticker": _primary_ticker(session, issuer=issuer, company=company),
        },
        "coverage_score": coverage_score,
        "coverage_inputs": score_inputs,
        "memos": [_serialize_decision_page_memo(row) for row in memo_rows],
        "analyst_evidence": [_serialize_decision_page_analyst_evidence(row) for row in analyst_rows],
        "catalysts": [_serialize_decision_page_catalyst(row) for row in catalyst_rows],
        "gaps": [_serialize_decision_page_gap(row) for row in gap_rows],
        "earnings_packets": earnings_packets,
    }


def cached_company_decision_page_payload(
    session: Session,
    *,
    company: Company | None = None,
    issuer: Issuer | None = None,
    include_earnings_packets: bool = True,
    ttl_s: float = 30.0,
) -> dict[str, Any]:
    key = f"decision-page:{_decision_payload_cache_key(session, company=company, issuer=issuer)}:{int(bool(include_earnings_packets))}"
    return _DECISION_PAGE_PAYLOAD_CACHE.get_or_set(
        key,
        lambda: company_decision_page_payload(
            session,
            company=company,
            issuer=issuer,
            include_earnings_packets=include_earnings_packets,
        ),
        ttl_s=ttl_s,
    )


def company_decision_payload(
    session: Session,
    *,
    company: Company | None = None,
    issuer: Issuer | None = None,
    read_only: bool = False,
) -> dict[str, Any]:
    if issuer is None and company is not None and company.issuer_id is not None:
        issuer = session.query(Issuer).filter_by(id=int(company.issuer_id)).first()
    if company is None and issuer is not None:
        company = _company_for_issuer(session, issuer)
    subject = _existing_decision_subject(session, company=company, issuer=issuer) if read_only else ensure_decision_subject(session, company=company, issuer=issuer)
    if subject is None:
        return {}
    if read_only:
        gaps = [
            serialize_gap(row)
            for row in (
                session.query(CoverageGap)
                .filter(CoverageGap.subject_id == int(subject.id))
                .order_by(CoverageGap.severity.desc(), CoverageGap.updated_at.desc(), CoverageGap.id.desc())
                .limit(50)
                .all()
            )
        ]
    else:
        gaps = refresh_decision_gaps(session, company=company, issuer=issuer)
    catalysts = list_investment_catalysts(
        session,
        company_id=int(company.id) if company is not None else None,
        issuer_id=int(issuer.id) if issuer is not None else None,
        days_back=180,
        days_forward=365,
        limit=20,
    )
    memo_rows = (
        session.query(InvestmentMemo)
        .filter(InvestmentMemo.subject_id == int(subject.id), InvestmentMemo.status != "archived")
        .order_by(InvestmentMemo.updated_at.desc(), InvestmentMemo.id.desc())
        .limit(3)
        .all()
    )
    analyst_evidence = list_analyst_evidence(
        session,
        subject_id=int(subject.id),
        company_id=int(company.id) if company is not None else None,
        issuer_id=int(issuer.id) if issuer is not None else None,
        limit=12,
    )
    active_theses = active_thesis_payloads(
        session,
        subject=subject,
        company=company,
        issuer=issuer,
        limit=8,
    )
    report = session.query(ResearchReport).filter_by(subject_key=subject.subject_key).first()
    latest_report = None
    if report is not None:
        version = (
            session.query(ResearchReportVersion)
            .filter_by(report_id=int(report.id), status="published")
            .order_by(ResearchReportVersion.version.desc(), ResearchReportVersion.id.desc())
            .first()
        )
        latest_report = {
            "id": int(report.id),
            "title": report.title,
            "href": f"/reports/{report.slug}",
            "latest_version": int(version.version) if version is not None else None,
            "published_at": version.published_at.isoformat() if version is not None and version.published_at else None,
            "coverage_gaps": list((version.coverage_json or {}).get("gaps") or []) if version is not None else [],
        }
    earnings_packets = []
    if issuer is not None:
        for event in (
            session.query(EarningsEvent)
            .filter(EarningsEvent.issuer_id == int(issuer.id))
            .order_by(EarningsEvent.fiscal_year.desc(), EarningsEvent.fiscal_quarter.desc().nullslast(), EarningsEvent.updated_at.desc())
            .limit(8)
            .all()
        ):
            earnings_packets.append(earnings_event_packet(session, event))
    fact_company_ids = company_structured_fact_scope_ids(session, company) if company is not None else []
    fact_count = (
        session.query(StructuredDataFact.id)
        .filter(StructuredDataFact.company_id.in_(fact_company_ids))
        .count()
        if fact_company_ids
        else 0
    )
    dataset_count = (
        len(
            {
                int(row[0])
                for row in session.query(StructuredDataFact.dataset_id)
                .filter(StructuredDataFact.company_id.in_(fact_company_ids), StructuredDataFact.dataset_id.isnot(None))
                .all()
                if row[0] is not None
            }
        )
        if fact_company_ids
        else 0
    )
    transcript_states = []
    if issuer is not None or company is not None:
        q = session.query(TranscriptWatchState)
        if issuer is not None:
            q = q.filter(TranscriptWatchState.issuer_id == int(issuer.id))
        elif company is not None:
            q = q.filter(TranscriptWatchState.company_id == int(company.id))
        for row in q.order_by(TranscriptWatchState.updated_at.desc()).limit(5).all():
            transcript_states.append(
                {
                    "id": int(row.id),
                    "ticker": row.ticker,
                    "status": row.status,
                    "reason": row.reason,
                    "priority": int(row.priority or 0),
                    "last_checked_at": row.last_checked_at.isoformat() if row.last_checked_at else None,
                    "next_check_at": row.next_check_at.isoformat() if row.next_check_at else None,
                    "miss_count": int(row.miss_count or 0),
                }
            )
    score_inputs = {
        "memo": bool(memo_rows),
        "report": latest_report is not None,
        "facts": fact_count > 0,
        "datasets": dataset_count > 0,
        "catalysts": bool(catalysts),
        "analyst_evidence": bool(analyst_evidence),
        "active_thesis": bool(active_theses),
        "earnings_packets": any(packet.get("completeness_score", 0) >= 1 for packet in earnings_packets),
    }
    coverage_score = round(sum(1 for value in score_inputs.values() if value) / max(1, len(score_inputs)) * 100)
    research_requests = _research_requests_for_subject(session, subject=subject)
    source_rail_health = _source_rail_health(session, company=company, issuer=issuer)
    dataset_context = _dataset_context(session, company=company)
    report_memory = _report_memory_payload(session, subject=subject)
    from thoth_terminal.research_canon_service import research_canon_context_for_subject
    from thoth_terminal.house_view_service import build_research_mode_context

    research_canon = research_canon_context_for_subject(session, subject_id=int(subject.id))
    research_mode_context = build_research_mode_context(session, subject_id=int(subject.id), mode="both", limit=4)
    alert_inventory = _alert_inventory(session, subject=subject, company=company, issuer=issuer)
    decision_snapshot = _decision_snapshot(
        memos=[serialize_memo(row) for row in memo_rows],
        analyst_evidence=analyst_evidence,
        catalysts=catalysts,
        gaps=gaps,
        active_theses=active_theses,
        research_requests=research_requests,
    )
    return {
        "subject": {
            "id": int(subject.id),
            "subject_key": subject.subject_key,
            "slug": subject.slug,
            "display_name": subject.display_name,
            "primary_ticker": _primary_ticker(session, issuer=issuer, company=company),
        },
        "coverage_score": coverage_score,
        "coverage_inputs": score_inputs,
        "memos": [serialize_memo(row) for row in memo_rows],
        "active_theses": active_theses,
        "analyst_evidence": analyst_evidence,
        "latest_report": latest_report,
        "report_memory": report_memory,
        "research_canon": research_canon,
        "house_view": research_mode_context.get("buy_side") or {},
        "research_mode_context": research_mode_context,
        "catalysts": catalysts,
        "earnings_packets": earnings_packets,
        "gaps": gaps,
        "research_requests": research_requests,
        "dataset_count": dataset_count,
        "structured_fact_count": int(fact_count),
        "dataset_context": dataset_context,
        "source_rail_health": source_rail_health,
        "alert_inventory": alert_inventory,
        "decision_snapshot": decision_snapshot,
        "transcript_watch_states": transcript_states,
    }


def cached_company_decision_payload(
    session: Session,
    *,
    company: Company | None = None,
    issuer: Issuer | None = None,
    read_only: bool = True,
    ttl_s: float = 30.0,
) -> dict[str, Any]:
    if not read_only:
        return company_decision_payload(session, company=company, issuer=issuer, read_only=False)
    key = _decision_payload_cache_key(session, company=company, issuer=issuer)
    return _DECISION_PAYLOAD_CACHE.get_or_set(
        key,
        lambda: company_decision_payload(session, company=company, issuer=issuer, read_only=True),
        ttl_s=ttl_s,
    )
