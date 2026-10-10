from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import re
import time
from typing import Any

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from thoth_terminal.config import settings
from thoth_terminal.db import session_factory, session_scope, utcnow
from thoth_terminal.earnings_event_policy import earnings_event_is_subsidiary_only_result
from thoth_terminal.foundation_service import upsert_transcript_record
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
    TranscriptWatchState,
)
from thoth_terminal.transcripts import (
    TranscriptRecord,
    _fetch_official_call_material_rows_from_roots,
    _profile_source_urls,
    _snapshot_profile_company,
    _snapshot_profile_issuer,
    _source_config_call_material_urls,
    document_is_readable_transcript,
    get_transcript,
    normalize_ticker,
)


_FILING_DOC_TYPES = {"earnings_release", "financial_report"}
_TRANSCRIPT_DOC_TYPES = {"earnings_transcript", "transcript"}
_EARNINGS_SEASON_MONTHS = {1, 2, 4, 5, 7, 8, 10, 11}
_URL_QUARTER_RE = re.compile(r"/(?P<year>20\d{2})/q(?P<quarter>[1-4])(?:[/#?]|$)", re.IGNORECASE)
_EVENT_MATERIAL_WINDOW_DAYS = 21
_CALL_MATERIAL_RE = re.compile(
    r"\b("
    r"earnings\s+(?:call|presentation|briefing|webcast|audio)"
    r"|earnings\s+(?:release\s+)?conference"
    r"|financial\s+(?:results?\s+)?(?:briefing|presentation|webcast|audio)"
    r"|results?\s+(?:briefing|presentation|webcast|audio)"
    r"|presentation\s*[-:]?\s*results?"
    r"|results?\s*[-:]?\s*presentation"
    r"|q\s*&\s*a|q&a|qa[\s_-]+summary|questions?\s+and\s+answers?"
    r"|conference\s+call|analyst\s+(?:meeting|briefing)"
    r")\b"
    r"|法人(?:說明|说明)會|法說會|法说会|業績(?:發布|發佈)?會|业绩(?:发布)?会|簡報|简报|影音|音訊|音频",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TranscriptCandidate:
    watch_key: str
    ticker: str
    company_id: int | None
    issuer_id: int | None
    company_name: str | None
    reason: str
    latest_filing_document_id: int | None
    latest_filing_at: datetime | None
    latest_filing_doc_type: str | None
    priority: int
    payload: dict[str, Any]
    fiscal_year: int | None = None
    fiscal_quarter: int | None = None


def _is_earnings_season(now: datetime | None = None) -> bool:
    return (now or utcnow()).month in _EARNINGS_SEASON_MONTHS


def _document_at(doc: Document) -> datetime | None:
    return doc.published_at or doc.ingested_at


def _datetimes_close(left: datetime | None, right: datetime | None, *, tolerance_seconds: int = 2) -> bool:
    if left is None or right is None:
        return False
    return abs((left - right).total_seconds()) <= tolerance_seconds


def _event_period_end(event: EarningsEvent) -> datetime | None:
    if event.period_end is not None:
        return datetime.combine(event.period_end, datetime.min.time())
    return None


def _event_has_untrusted_ingest_time(session: Session, event: EarningsEvent, value: datetime | None) -> bool:
    if value is None:
        return False
    period_end = _event_period_end(event)
    if period_end is None or value <= period_end + timedelta(days=120):
        return False
    for artifact_id in (event.release_artifact_id, event.presentation_artifact_id, event.webcast_artifact_id, event.transcript_artifact_id):
        if artifact_id is None:
            continue
        artifact = session.get(IRArtifact, int(artifact_id))
        if artifact is None or artifact.document_id is None:
            continue
        doc = session.get(Document, int(artifact.document_id))
        if doc is None or doc.published_at is not None:
            continue
        if _datetimes_close(value, doc.ingested_at):
            return True
    return False


def _trusted_event_time(session: Session, event: EarningsEvent, value: datetime | None) -> datetime | None:
    if _event_has_untrusted_ingest_time(session, event, value):
        return None
    return value


def _event_scheduled_date_is_untrusted(session: Session, event: EarningsEvent) -> bool:
    if event.scheduled_date is None:
        return False
    scheduled_at = datetime.combine(event.scheduled_date, datetime.min.time())
    period_end = _event_period_end(event)
    if period_end is None or scheduled_at <= period_end + timedelta(days=120):
        return False
    for artifact_id in (event.release_artifact_id, event.presentation_artifact_id, event.webcast_artifact_id, event.transcript_artifact_id):
        if artifact_id is None:
            continue
        artifact = session.get(IRArtifact, int(artifact_id))
        if artifact is None or artifact.document_id is None:
            continue
        doc = session.get(Document, int(artifact.document_id))
        if doc is None or doc.published_at is not None or doc.ingested_at is None:
            continue
        if doc.ingested_at.date() == event.scheduled_date:
            return True
    return False


def _event_has_untrusted_gate_timing(session: Session, event: EarningsEvent) -> bool:
    if _event_has_untrusted_ingest_time(session, event, event.actual_release_at):
        return True
    if _event_has_untrusted_ingest_time(session, event, event.actual_call_at):
        return True
    if _event_scheduled_date_is_untrusted(session, event):
        return True
    for artifact_id in (event.release_artifact_id, event.presentation_artifact_id, event.webcast_artifact_id):
        if artifact_id is None:
            continue
        artifact = session.get(IRArtifact, int(artifact_id))
        if artifact is not None and _event_has_untrusted_ingest_time(session, event, artifact.published_at):
            return True
    return False


def _document_period(doc: Document | None) -> tuple[int | None, int | None]:
    if doc is None:
        return None, None
    extra = doc.extra if isinstance(doc.extra, dict) else {}
    for key in ("fiscal_year", "source_fiscal_year"):
        if extra.get(key) is None:
            continue
        try:
            year = int(extra[key])
        except Exception:
            continue
        quarter = None
        for q_key in ("fiscal_quarter", "source_fiscal_quarter"):
            if extra.get(q_key) is None:
                continue
            try:
                quarter = int(extra[q_key])
            except Exception:
                quarter = None
            break
        return year, quarter
    period_payload = extra.get("period_normalization")
    if isinstance(period_payload, dict) and period_payload.get("fiscal_year") is not None:
        try:
            year = int(period_payload["fiscal_year"])
        except Exception:
            year = None
        try:
            quarter = int(period_payload["fiscal_quarter"]) if period_payload.get("fiscal_quarter") is not None else None
        except Exception:
            quarter = None
        if year is not None:
            return year, quarter
    blob = " ".join([str(doc.title or ""), str(doc.url or ""), str(doc.canonical_url or "")])
    match = _URL_QUARTER_RE.search(blob)
    if match:
        return int(match.group("year")), int(match.group("quarter"))
    return None, None


def _not_future_document_filter(now: datetime):
    return or_(
        and_(Document.published_at.isnot(None), Document.published_at <= now),
        and_(
            Document.published_at.is_(None),
            or_(Document.ingested_at.is_(None), Document.ingested_at <= now),
        ),
    )


def _company_for_ticker(session: Session, ticker: str) -> Company | None:
    sym = normalize_ticker(ticker)
    if not sym:
        return None
    company = session.query(Company).filter(func.upper(Company.ticker) == sym).order_by(Company.id.asc()).first()
    if company is not None:
        return company
    ident = (
        session.query(IssuerIdentifier)
        .filter(IssuerIdentifier.id_type == "TICKER")
        .filter(func.upper(IssuerIdentifier.id_value) == sym)
        .order_by(IssuerIdentifier.is_primary.desc(), IssuerIdentifier.id.asc())
        .first()
    )
    if ident is None:
        return None
    return session.query(Company).filter(Company.issuer_id == int(ident.issuer_id)).order_by(Company.id.asc()).first()


def _latest_filing_for_company(session: Session, company_id: int, *, lookback_days: int) -> tuple[CompanyDocument, Document] | None:
    now = utcnow()
    cutoff = now - timedelta(days=max(1, int(lookback_days)))
    rows = (
        session.query(CompanyDocument, Document)
        .join(Document, Document.id == CompanyDocument.document_id)
        .filter(CompanyDocument.company_id == int(company_id))
        .filter(CompanyDocument.doc_type.in_(sorted(_FILING_DOC_TYPES)))
        .filter(_not_future_document_filter(now))
        .filter(or_(Document.published_at >= cutoff, Document.ingested_at >= cutoff))
        .order_by(Document.published_at.desc().nullslast(), Document.ingested_at.desc(), Document.id.desc())
        .limit(1)
        .all()
    )
    return rows[0] if rows else None


def _event_primary_at(session: Session, event: EarningsEvent) -> datetime | None:
    values = [
        _trusted_event_time(session, event, event.actual_call_at),
        _trusted_event_time(session, event, event.actual_release_at),
        (
            datetime.combine(event.scheduled_date, datetime.min.time())
            if event.scheduled_date is not None and not _event_scheduled_date_is_untrusted(session, event)
            else None
        ),
    ]
    return next((value for value in values if value is not None), None)


def _event_material_at(session: Session, event: EarningsEvent) -> datetime | None:
    values: list[datetime] = []
    if event.release_filing_id is not None:
        filing = session.get(SecuritiesFiling, int(event.release_filing_id))
        if filing is not None:
            if filing.filed_at is not None:
                values.append(filing.filed_at)
            if filing.document_id is not None:
                doc = session.get(Document, int(filing.document_id))
                if doc is not None:
                    doc_at = _document_at(doc)
                    if doc_at is not None and not _event_has_untrusted_ingest_time(session, event, doc_at):
                        values.append(doc_at)
    if event.release_disclosure_id is not None:
        disclosure = session.get(ExchangeDisclosure, int(event.release_disclosure_id))
        if disclosure is not None:
            if disclosure.announced_at is not None:
                values.append(disclosure.announced_at)
            if disclosure.document_id is not None:
                doc = session.get(Document, int(disclosure.document_id))
                if doc is not None:
                    doc_at = _document_at(doc)
                    if doc_at is not None and not _event_has_untrusted_ingest_time(session, event, doc_at):
                        values.append(doc_at)
    for artifact_id in (event.release_artifact_id, event.presentation_artifact_id, event.webcast_artifact_id):
        if artifact_id is None:
            continue
        artifact = session.get(IRArtifact, int(artifact_id))
        if artifact is None:
            continue
        if artifact.published_at is not None and not _event_has_untrusted_ingest_time(session, event, artifact.published_at):
            values.append(artifact.published_at)
        if artifact.document_id is not None:
            doc = session.get(Document, int(artifact.document_id))
            if doc is not None:
                doc_at = _document_at(doc)
                if doc_at is not None and not _event_has_untrusted_ingest_time(session, event, doc_at):
                    values.append(doc_at)
    return max(values) if values else None


def _event_is_completed(session: Session, event: EarningsEvent, *, now: datetime) -> bool:
    status = str(event.status or "").strip().lower()
    if status == "invalid":
        return False
    actual_release_at = _trusted_event_time(session, event, event.actual_release_at)
    actual_call_at = _trusted_event_time(session, event, event.actual_call_at)
    if actual_release_at is not None and actual_release_at <= now:
        return True
    if actual_call_at is not None and actual_call_at <= now:
        return True
    material_at = _event_material_at(session, event)
    if status == "completed" and material_at is not None and material_at <= now:
        return True
    if (
        status == "completed"
        and event.scheduled_date is not None
        and event.scheduled_date <= now.date()
        and not _event_scheduled_date_is_untrusted(session, event)
    ):
        return True
    return False


def _artifact_payload(session: Session, artifact_id: int | None, *, key: str) -> dict[str, Any] | None:
    if artifact_id is None:
        return None
    artifact = session.get(IRArtifact, int(artifact_id))
    if artifact is None:
        return None
    url = str(artifact.canonical_url or artifact.download_url or artifact.webcast_url or artifact.audio_url or "").strip()
    return {
        "key": key,
        "artifact_id": int(artifact.id),
        "document_id": int(artifact.document_id) if artifact.document_id is not None else None,
        "artifact_type": artifact.artifact_type,
        "artifact_subtype": artifact.artifact_subtype,
        "url": url or None,
        "title": artifact.title,
    }


def _event_material_payload(session: Session, event: EarningsEvent) -> dict[str, Any]:
    surfaces: list[dict[str, Any]] = []
    for key, artifact_id in (
        ("release_artifact", event.release_artifact_id),
        ("presentation_artifact", event.presentation_artifact_id),
        ("webcast_artifact", event.webcast_artifact_id),
    ):
        payload = _artifact_payload(session, artifact_id, key=key)
        if payload is not None:
            surfaces.append(payload)
    if event.release_filing_id is not None:
        filing = session.get(SecuritiesFiling, int(event.release_filing_id))
        surfaces.append(
            {
                "key": "release_filing",
                "filing_id": int(event.release_filing_id),
                "document_id": int(filing.document_id) if filing is not None and filing.document_id is not None else None,
            }
        )
    if event.release_disclosure_id is not None:
        disclosure = session.get(ExchangeDisclosure, int(event.release_disclosure_id))
        surfaces.append(
            {
                "key": "release_disclosure",
                "disclosure_id": int(event.release_disclosure_id),
                "document_id": int(disclosure.document_id) if disclosure is not None and disclosure.document_id is not None else None,
            }
        )
    if event.actual_release_at is not None:
        surfaces.append({"key": "actual_release_at", "at": event.actual_release_at.isoformat()})
    if event.actual_call_at is not None:
        surfaces.append({"key": "actual_call_at", "at": event.actual_call_at.isoformat()})
    return {"available_surfaces": surfaces}


def _event_has_earnings_material(session: Session, event: EarningsEvent) -> bool:
    payload = _event_material_payload(session, event)
    material_keys = {"release_artifact", "presentation_artifact", "webcast_artifact", "release_filing", "release_disclosure"}
    return any(str(row.get("key") or "") in material_keys for row in payload.get("available_surfaces") or [] if isinstance(row, dict))


def _artifact_is_official_call_material(artifact: IRArtifact | None) -> bool:
    if artifact is None:
        return False
    artifact_type = str(artifact.artifact_type or "").strip().lower()
    subtype = str(artifact.artifact_subtype or "").strip().lower()
    title = str(artifact.title or "").strip()
    url = str(artifact.canonical_url or artifact.download_url or artifact.webcast_url or artifact.audio_url or "").strip()
    blob = " ".join([title, url]).lower()
    if artifact_type in {"transcript", "webcast", "audio"}:
        return True
    if subtype in {"call", "earnings_call", "earnings_webcast", "earnings_audio"}:
        return True
    if artifact_type in {"presentation", "fact_sheet"} and subtype == "earnings":
        return True
    if artifact_type in {"document", "exchange_disclosure", "presentation", "fact_sheet", "press_release"} and _CALL_MATERIAL_RE.search(blob):
        return True
    return False


def _event_artifact_period_matches(event: EarningsEvent, artifact: IRArtifact) -> bool:
    if artifact.fiscal_year is not None and event.fiscal_year is not None and int(artifact.fiscal_year) != int(event.fiscal_year):
        return False
    if artifact.fiscal_quarter is None or event.fiscal_quarter is None:
        return True
    return int(artifact.fiscal_quarter) == int(event.fiscal_quarter)


def _event_artifact_time_matches(session: Session, event: EarningsEvent, artifact: IRArtifact) -> bool:
    if (
        artifact.fiscal_year is not None
        and artifact.fiscal_quarter is not None
        and event.fiscal_year is not None
        and event.fiscal_quarter is not None
        and int(artifact.fiscal_year) == int(event.fiscal_year)
        and int(artifact.fiscal_quarter) == int(event.fiscal_quarter)
    ):
        return True
    event_at = _event_primary_at(session, event) or _event_material_at(session, event)
    artifact_at = artifact.published_at
    if artifact_at is None and artifact.document_id is not None:
        doc = session.get(Document, int(artifact.document_id))
        artifact_at = _document_at(doc) if doc is not None else None
    if event_at is None or artifact_at is None:
        return True
    return abs((artifact_at - event_at).total_seconds()) <= (_EVENT_MATERIAL_WINDOW_DAYS * 24 * 60 * 60)


def _find_event_call_material_artifact(session: Session, event: EarningsEvent) -> IRArtifact | None:
    for artifact_id in (event.transcript_artifact_id, event.webcast_artifact_id, event.presentation_artifact_id):
        if artifact_id is None:
            continue
        artifact = session.get(IRArtifact, int(artifact_id))
        if _artifact_is_official_call_material(artifact):
            return artifact
    if event.issuer_id is None or event.fiscal_year is None:
        return None
    rows = (
        session.query(IRArtifact)
        .filter(IRArtifact.issuer_id == int(event.issuer_id))
        .filter(IRArtifact.fiscal_year == int(event.fiscal_year))
        .order_by(IRArtifact.published_at.desc().nullslast(), IRArtifact.id.desc())
        .limit(80)
        .all()
    )
    candidates = [
        artifact
        for artifact in rows
        if _artifact_is_official_call_material(artifact)
        and _event_artifact_period_matches(event, artifact)
        and _event_artifact_time_matches(session, event, artifact)
    ]
    if not candidates:
        return None
    return sorted(candidates, key=lambda artifact: (artifact.published_at or datetime.min, int(artifact.id)), reverse=True)[0]


def _link_event_call_material(session: Session, event: EarningsEvent, artifact: IRArtifact) -> None:
    artifact_type = str(artifact.artifact_type or "").strip().lower()
    if artifact_type == "transcript":
        event.transcript_artifact_id = int(artifact.id)
    elif artifact_type in {"webcast", "audio"}:
        event.webcast_artifact_id = int(artifact.id)
    else:
        event.presentation_artifact_id = int(artifact.id)
    if artifact.published_at is not None and event.actual_call_at is None:
        event.actual_call_at = artifact.published_at
    event.updated_at = utcnow()


def _cover_candidate_with_official_call_material(session: Session, candidate: TranscriptCandidate) -> IRArtifact | None:
    if not str(candidate.watch_key or "").startswith("earnings_event:"):
        return None
    try:
        event_id = int(str(candidate.watch_key).rsplit(":", 1)[-1])
    except Exception:
        return None
    event = session.get(EarningsEvent, event_id)
    if event is None:
        return None
    artifact = _find_event_call_material_artifact(session, event)
    if artifact is None:
        return None
    _link_event_call_material(session, event, artifact)
    return artifact


def _link_candidate_event_call_material(session: Session, candidate: TranscriptCandidate, artifact: IRArtifact) -> bool:
    if not str(candidate.watch_key or "").startswith("earnings_event:"):
        return False
    try:
        event_id = int(str(candidate.watch_key).rsplit(":", 1)[-1])
    except Exception:
        return False
    event = session.get(EarningsEvent, event_id)
    if event is None:
        return False
    if candidate.issuer_id is not None and event.issuer_id is not None and int(event.issuer_id) != int(candidate.issuer_id):
        return False
    if artifact.issuer_id is not None and event.issuer_id is not None and int(artifact.issuer_id) != int(event.issuer_id):
        return False
    artifact_type = str(artifact.artifact_type or "").strip().lower()
    existing_id = (
        event.transcript_artifact_id
        if artifact_type == "transcript"
        else event.webcast_artifact_id
        if artifact_type in {"webcast", "audio"}
        else event.presentation_artifact_id
    )
    if existing_id is not None:
        if int(existing_id) != int(artifact.id):
            return False
        if event.actual_call_at is None and artifact.published_at is not None:
            _link_event_call_material(session, event, artifact)
            return True
        return False
    _link_event_call_material(session, event, artifact)
    return True


def _event_reference_document_id(session: Session, event: EarningsEvent) -> int | None:
    for artifact_id in (event.release_artifact_id, event.presentation_artifact_id, event.webcast_artifact_id):
        if artifact_id is None:
            continue
        artifact = session.get(IRArtifact, int(artifact_id))
        if artifact is not None and artifact.document_id is not None:
            return int(artifact.document_id)
    if event.release_filing_id is not None:
        filing = session.get(SecuritiesFiling, int(event.release_filing_id))
        if filing is not None and filing.document_id is not None:
            return int(filing.document_id)
    if event.release_disclosure_id is not None:
        disclosure = session.get(ExchangeDisclosure, int(event.release_disclosure_id))
        if disclosure is not None and disclosure.document_id is not None:
            return int(disclosure.document_id)
    return None


def _append_http_url(out: list[str], seen: set[str], value: object) -> None:
    raw = str(value or "").strip()
    if not raw or not raw.lower().startswith(("http://", "https://")):
        return
    key = raw.rstrip("/").casefold()
    if key in seen:
        return
    seen.add(key)
    out.append(raw)


def _event_official_material_roots(session: Session, event: EarningsEvent) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()

    def append_document_urls(document_id: int | None) -> None:
        if document_id is None:
            return
        doc = session.get(Document, int(document_id))
        if doc is None:
            return
        _append_http_url(out, seen, doc.canonical_url)
        _append_http_url(out, seen, doc.url)

    for artifact_id in (event.release_artifact_id, event.presentation_artifact_id, event.webcast_artifact_id, event.transcript_artifact_id):
        if artifact_id is None:
            continue
        artifact = session.get(IRArtifact, int(artifact_id))
        if artifact is None:
            continue
        for value in (artifact.canonical_url, artifact.download_url, artifact.webcast_url, artifact.audio_url):
            _append_http_url(out, seen, value)
        append_document_urls(int(artifact.document_id) if artifact.document_id is not None else None)
    if event.release_filing_id is not None:
        filing = session.get(SecuritiesFiling, int(event.release_filing_id))
        if filing is not None:
            for value in (filing.primary_document_url, filing.index_url, filing.filing_url):
                _append_http_url(out, seen, value)
            append_document_urls(int(filing.document_id) if filing.document_id is not None else None)
    if event.release_disclosure_id is not None:
        disclosure = session.get(ExchangeDisclosure, int(event.release_disclosure_id))
        if disclosure is not None:
            _append_http_url(out, seen, disclosure.disclosure_url)
            for value in disclosure.attachment_urls or []:
                _append_http_url(out, seen, value)
            append_document_urls(int(disclosure.document_id) if disclosure.document_id is not None else None)
    return out


def _artifact_type_for_official_call_material_row(row: TranscriptRecord) -> str:
    blob = " ".join([str(row.period_label or ""), str(row.url or ""), str(row.source_kind or "")]).lower()
    if "webcast" in blob:
        return "webcast"
    if "audio" in blob or "mp3" in blob or "wav" in blob or "m4a" in blob:
        return "audio"
    if "transcript" in blob or "script" in blob:
        return "transcript"
    return "presentation"


def _download_url_for_official_call_material(row: TranscriptRecord, artifact_type: str) -> str | None:
    lower_url = str(row.url or "").strip().lower()
    if artifact_type in {"webcast", "audio"}:
        return None
    if lower_url.endswith((".pdf", ".zip", ".xlsx", ".xls", ".doc", ".docx", ".ppt", ".pptx")):
        return row.url
    return None


def _upsert_event_official_call_material_artifact(
    session: Session,
    *,
    event: EarningsEvent,
    candidate: TranscriptCandidate,
    row: TranscriptRecord,
    roots: list[str],
) -> IRArtifact | None:
    stamped = _transcript_row_with_candidate_period(row, candidate)
    artifact_type = _artifact_type_for_official_call_material_row(stamped)
    if artifact_type == "transcript":
        artifact = upsert_transcript_record(session, row=stamped)
        if artifact is not None:
            _link_event_call_material(session, event, artifact)
        return artifact
    published_at = stamped.published_at or _event_primary_at(session, event) or _event_material_at(session, event)
    title_bits = [candidate.company_name or stamped.company_or_issuer or stamped.ticker]
    if stamped.period_label:
        title_bits.append(stamped.period_label)
    title_bits.append("official call material")
    title = " ".join(bit for bit in title_bits if bit).strip()
    artifact = (
        session.query(IRArtifact)
        .filter_by(issuer_id=int(event.issuer_id), artifact_type=artifact_type, canonical_url=stamped.url)
        .order_by(IRArtifact.id.asc())
        .first()
    )
    now = utcnow()
    if artifact is None:
        artifact = IRArtifact(
            issuer_id=int(event.issuer_id),
            document_id=None,
            artifact_type=artifact_type,
            artifact_subtype="earnings",
            published_at=published_at,
            event_date=published_at.date() if published_at is not None else None,
            fiscal_year=stamped.fiscal_year,
            fiscal_quarter=stamped.fiscal_quarter,
            title=title[:512] or None,
            description=None,
            canonical_url=stamped.url,
            download_url=_download_url_for_official_call_material(stamped, artifact_type),
            webcast_url=stamped.url if artifact_type == "webcast" else None,
            audio_url=stamped.url if artifact_type == "audio" else None,
            is_derived_transcript=False,
            ical_uid=None,
            language="en",
            raw_meta={},
            created_at=now,
        )
        session.add(artifact)
        session.flush()
    artifact.artifact_subtype = artifact.artifact_subtype or "earnings"
    artifact.published_at = artifact.published_at or published_at
    artifact.event_date = artifact.event_date or (published_at.date() if published_at is not None else None)
    artifact.fiscal_year = stamped.fiscal_year if stamped.fiscal_year is not None else artifact.fiscal_year
    artifact.fiscal_quarter = stamped.fiscal_quarter if stamped.fiscal_quarter is not None else artifact.fiscal_quarter
    artifact.title = title[:512] or artifact.title
    artifact.download_url = artifact.download_url or _download_url_for_official_call_material(stamped, artifact_type)
    if artifact_type == "webcast":
        artifact.webcast_url = artifact.webcast_url or stamped.url
    if artifact_type == "audio":
        artifact.audio_url = artifact.audio_url or stamped.url
    meta = dict(artifact.raw_meta or {})
    meta.update(
        {
            "source_kind": stamped.source_kind,
            "source_id": stamped.source_id,
            "transcript_url": stamped.url,
            "event_scoped_official_call_material": True,
            "event_id": int(event.id),
            "watch_key": candidate.watch_key,
            "source_roots": roots[:10],
        }
    )
    artifact.raw_meta = meta
    _link_event_call_material(session, event, artifact)
    session.flush()
    return artifact


def _cover_candidate_with_event_official_pages(
    session: Session,
    candidate: TranscriptCandidate,
    *,
    lookback_days: int,
) -> IRArtifact | None:
    if not str(candidate.watch_key or "").startswith("earnings_event:"):
        return None
    try:
        event_id = int(str(candidate.watch_key).rsplit(":", 1)[-1])
    except Exception:
        return None
    event = session.get(EarningsEvent, event_id)
    if event is None:
        return None
    roots = _event_official_material_roots(session, event)
    if not roots:
        return None
    rows = _fetch_official_call_material_rows_from_roots(
        session,
        ticker=candidate.ticker,
        roots=roots,
        year=candidate.fiscal_year,
        quarter=candidate.fiscal_quarter,
        most_recent=True,
        limit=1,
        lookback_days=lookback_days,
    )
    for row in rows:
        if not _transcript_matches_candidate(row, candidate):
            continue
        return _upsert_event_official_call_material_artifact(session, event=event, candidate=candidate, row=row, roots=roots)
    return None


def _source_profile(session: Session | None, company: Company | None, issuer: Issuer | None = None) -> dict[str, Any]:
    urls = {
        "ir_url": str(company.ir_url or "").strip() if company is not None else None,
        "reports_url": str(company.reports_url or "").strip() if company is not None else None,
        "calendar_url": str(company.calendar_url or "").strip() if company is not None else None,
        "filings_url": str(company.filings_url or "").strip() if company is not None else None,
        "issuer_ir_website": str(issuer.ir_website or "").strip() if issuer is not None else None,
        "issuer_website": str(issuer.website or "").strip() if issuer is not None else None,
    }
    profile = {key: value for key, value in urls.items() if value}
    if session is None or company is None:
        return profile
    try:
        company_snapshot = _snapshot_profile_company(company)
        issuer_snapshot = _snapshot_profile_issuer(issuer)
        config_roots = _source_config_call_material_urls(session, company=company_snapshot, issuer=issuer_snapshot)
        profile_roots = _profile_source_urls(company_snapshot, issuer_snapshot)
    except Exception:
        return profile

    if config_roots:
        profile["source_config_call_material_roots"] = config_roots[:20]
    if profile_roots:
        profile["issuer_profile_roots"] = profile_roots[:20]
    combined: list[str] = []
    seen: set[str] = set()
    for root in [*config_roots, *profile_roots]:
        key = root.rstrip("/").casefold()
        if key in seen:
            continue
        seen.add(key)
        combined.append(root)
    if combined:
        profile["official_call_material_roots"] = combined[:40]
    return profile


def _transcript_lookup_policy(
    *,
    session: Session | None = None,
    company: Company | None,
    issuer: Issuer | None = None,
    event: EarningsEvent | None = None,
) -> dict[str, Any]:
    derived_candidate = False
    if event is not None and event.webcast_artifact_id is not None:
        derived_candidate = True
    return {
        "trigger": "after_earnings_material_without_readable_transcript",
        "provider_ladder": [
            "local_ir_artifact_or_company_document",
            "issuer_specific_official_transcript_adapter",
            "issuer_official_call_material_page_after_gate",
            "derived_from_official_webcast_or_audio_when_available",
            "stockanalysis_live_after_official_gate",
            "motley_fool_live_after_official_gate",
        ],
        "blocked_providers": ["quartr_api", "marketscreener_scrape"],
        "source_profile": _source_profile(session, company, issuer),
        "derived_transcript_candidate": derived_candidate,
    }


def _has_local_transcript(
    session: Session,
    *,
    ticker: str,
    company_id: int | None,
    issuer_id: int | None,
    since: datetime | None,
    fiscal_year: int | None = None,
    fiscal_quarter: int | None = None,
) -> bool:
    cutoff = since or (utcnow() - timedelta(days=120))
    if fiscal_year is not None:
        if issuer_id is not None:
            artifacts = (
                session.query(IRArtifact)
                .filter(IRArtifact.issuer_id == int(issuer_id))
                .filter(IRArtifact.artifact_type == "transcript")
                .filter(IRArtifact.fiscal_year == int(fiscal_year))
                .filter(IRArtifact.fiscal_quarter == fiscal_quarter)
                .limit(20)
                .all()
            )
            for artifact in artifacts:
                doc = session.get(Document, int(artifact.document_id)) if artifact.document_id is not None else None
                if document_is_readable_transcript(doc):
                    return True
        if company_id is not None:
            docs = (
                session.query(Document)
                .join(CompanyDocument, CompanyDocument.document_id == Document.id)
                .filter(CompanyDocument.company_id == int(company_id))
                .filter(CompanyDocument.doc_type.in_(sorted(_TRANSCRIPT_DOC_TYPES)))
                .order_by(Document.published_at.desc().nullslast(), Document.ingested_at.desc())
                .limit(50)
                .all()
            )
            for doc in docs:
                if not document_is_readable_transcript(doc):
                    continue
                row_year, row_quarter = _document_period(doc)
                if row_year == int(fiscal_year) and row_quarter == fiscal_quarter:
                    return True
    if company_id is not None:
        docs = (
            session.query(Document)
            .select_from(CompanyDocument)
            .join(Document, Document.id == CompanyDocument.document_id)
            .filter(CompanyDocument.company_id == int(company_id))
            .filter(CompanyDocument.doc_type.in_(sorted(_TRANSCRIPT_DOC_TYPES)))
            .filter(or_(Document.published_at >= cutoff, Document.ingested_at >= cutoff))
            .limit(50)
            .all()
        )
        if any(document_is_readable_transcript(doc) for doc in docs):
            return True
    if issuer_id is not None:
        artifacts = (
            session.query(IRArtifact)
            .filter(IRArtifact.issuer_id == int(issuer_id))
            .filter(IRArtifact.artifact_type == "transcript")
            .filter(or_(IRArtifact.published_at >= cutoff, IRArtifact.created_at >= cutoff))
            .limit(50)
            .all()
        )
        for artifact in artifacts:
            doc = session.get(Document, int(artifact.document_id)) if artifact.document_id is not None else None
            if document_is_readable_transcript(doc):
                return True
    sym = normalize_ticker(ticker)
    if sym:
        docs = (
            session.query(Document)
            .filter(Document.kind.in_(["earnings_transcript", "transcript"]))
            .filter(or_(Document.published_at >= cutoff, Document.ingested_at >= cutoff))
            .filter(
                or_(
                    func.upper(func.coalesce(Document.title, "")).like(f"% {sym} %"),
                    func.upper(func.coalesce(Document.title, "")).like(f"%({sym})%"),
                    func.lower(func.coalesce(Document.url, "")).like(f"%-{sym.lower()}-%"),
                    func.lower(func.coalesce(Document.canonical_url, "")).like(f"%-{sym.lower()}-%"),
                )
            )
            .limit(50)
            .all()
        )
        if any(document_is_readable_transcript(doc) for doc in docs):
            return True
    return False


def _transcript_matches_candidate(row: TranscriptRecord | None, candidate: TranscriptCandidate) -> bool:
    if row is None:
        return False
    if not _transcript_tickers_match(row.ticker, candidate.ticker):
        return False
    if candidate.fiscal_year is not None:
        if row.fiscal_year == candidate.fiscal_year:
            if candidate.fiscal_quarter is None or row.fiscal_quarter == candidate.fiscal_quarter:
                return True
        if row.fiscal_year is not None or row.fiscal_quarter is not None or row.published_at is None:
            return False
    if candidate.latest_filing_at is None or row.published_at is None:
        return True
    return row.published_at >= candidate.latest_filing_at - timedelta(days=21)


def _period_label(year: int | None, quarter: int | None) -> str | None:
    if year is None:
        return None
    if quarter is None:
        return f"FY {int(year)}"
    return f"FY{int(year)} Q{int(quarter)}"


def _transcript_row_with_candidate_period(row: TranscriptRecord, candidate: TranscriptCandidate) -> TranscriptRecord:
    if candidate.fiscal_year is None:
        return row
    if row.fiscal_year is not None or row.fiscal_quarter is not None:
        return row
    return TranscriptRecord(
        ticker=row.ticker,
        company_or_issuer=row.company_or_issuer,
        period_label=row.period_label or _period_label(candidate.fiscal_year, candidate.fiscal_quarter),
        fiscal_year=int(candidate.fiscal_year),
        fiscal_quarter=int(candidate.fiscal_quarter) if candidate.fiscal_quarter is not None else None,
        published_at=row.published_at,
        url=row.url,
        source_kind=row.source_kind,
        source_id=row.source_id,
    )


def _is_safe_exchange_suffix_stem(stem: str) -> bool:
    return len(stem) >= 4 or any(ch.isdigit() for ch in stem)


def _transcript_tickers_match(left: str | None, right: str | None) -> bool:
    left_v = normalize_ticker(left)
    right_v = normalize_ticker(right)
    if not left_v or not right_v:
        return False
    if left_v == right_v:
        return True
    if "." in left_v:
        stem = left_v.split(".", 1)[0]
        if stem == right_v and _is_safe_exchange_suffix_stem(stem):
            return True
    if "." in right_v:
        stem = right_v.split(".", 1)[0]
        if stem == left_v and _is_safe_exchange_suffix_stem(stem):
            return True
    return False


def _filing_gap_candidates(session: Session, *, lookback_days: int) -> list[TranscriptCandidate]:
    now = utcnow()
    cutoff = now - timedelta(days=max(1, int(lookback_days)))
    rows = (
        session.query(Company, CompanyDocument, Document)
        .join(CompanyDocument, CompanyDocument.company_id == Company.id)
        .join(Document, Document.id == CompanyDocument.document_id)
        .filter(Company.ticker.isnot(None))
        .filter(CompanyDocument.doc_type.in_(sorted(_FILING_DOC_TYPES)))
        .filter(_not_future_document_filter(now))
        .filter(or_(Document.published_at >= cutoff, Document.ingested_at >= cutoff))
        .order_by(Company.id.asc(), Document.published_at.desc().nullslast(), Document.ingested_at.desc(), Document.id.desc())
        .all()
    )
    latest_by_company: dict[int, tuple[Company, CompanyDocument, Document]] = {}
    for company, company_doc, document in rows:
        company_id = int(company.id)
        if company_id not in latest_by_company:
            latest_by_company[company_id] = (company, company_doc, document)

    out: list[TranscriptCandidate] = []
    for company, company_doc, document in latest_by_company.values():
        ticker = normalize_ticker(company.ticker)
        if not ticker:
            continue
        filing_at = _document_at(document)
        fiscal_year, fiscal_quarter = _document_period(document)
        transcript_cutoff = filing_at - timedelta(days=21) if filing_at else cutoff
        if _has_local_transcript(
            session,
            ticker=ticker,
            company_id=int(company.id),
            issuer_id=int(company.issuer_id) if company.issuer_id else None,
            since=transcript_cutoff,
            fiscal_year=fiscal_year,
            fiscal_quarter=fiscal_quarter,
        ):
            continue
        issuer = session.get(Issuer, int(company.issuer_id)) if company.issuer_id is not None else None
        payload = {
            "source": "company_document",
            "document_title": document.title,
            "transcript_lookup_policy": _transcript_lookup_policy(session=session, company=company, issuer=issuer),
        }
        if fiscal_year is not None:
            payload["fiscal_year"] = fiscal_year
            payload["fiscal_quarter"] = fiscal_quarter
        out.append(
            TranscriptCandidate(
                watch_key=f"filing:{int(company.id)}:{int(document.id)}",
                ticker=ticker,
                company_id=int(company.id),
                issuer_id=int(company.issuer_id) if company.issuer_id else None,
                company_name=company.name,
                reason="filing_gap",
                latest_filing_document_id=int(document.id),
                latest_filing_at=filing_at,
                latest_filing_doc_type=company_doc.doc_type,
                priority=20 if str(company_doc.doc_type or "") == "earnings_release" else 15,
                payload=payload,
                fiscal_year=fiscal_year,
                fiscal_quarter=fiscal_quarter,
            )
        )
    return out


def _active_universe_rows(session: Session) -> list[tuple[ResearchUniverseRow, ResearchInstrument, ResearchUniverse]]:
    return (
        session.query(ResearchUniverseRow, ResearchInstrument, ResearchUniverse)
        .join(ResearchInstrument, ResearchInstrument.id == ResearchUniverseRow.instrument_id)
        .join(ResearchUniverse, ResearchUniverse.id == ResearchUniverseRow.universe_id)
        .filter(ResearchUniverse.status == "active")
        .filter(ResearchUniverseRow.archived_at.is_(None))
        .filter(ResearchInstrument.is_public.is_(True))
        .filter(or_(ResearchInstrument.ticker.isnot(None), ResearchInstrument.yahoo_symbol.isnot(None)))
        .order_by(ResearchUniverse.sort_order.asc(), ResearchUniverse.name.asc(), ResearchUniverseRow.sort_order.asc())
        .all()
    )


def _event_gap_candidates(session: Session, *, lookback_days: int) -> list[TranscriptCandidate]:
    now = utcnow()
    cutoff = now - timedelta(days=max(1, int(lookback_days)))
    out_by_event: dict[int, TranscriptCandidate] = {}
    for universe_row, instrument, universe in _active_universe_rows(session):
        ticker = normalize_ticker(instrument.ticker or instrument.yahoo_symbol)
        if not ticker:
            continue
        company = instrument.company if instrument.company_id else _company_for_ticker(session, ticker)
        issuer_id = int(company.issuer_id) if company is not None and company.issuer_id else int(instrument.issuer_id) if instrument.issuer_id else None
        if issuer_id is None:
            continue
        issuer = session.get(Issuer, issuer_id)
        events = (
            session.query(EarningsEvent)
            .filter(EarningsEvent.issuer_id == issuer_id)
            .filter(EarningsEvent.fiscal_year.isnot(None))
            .filter(func.lower(func.coalesce(EarningsEvent.status, "")) != "invalid")
            .order_by(EarningsEvent.updated_at.desc(), EarningsEvent.id.desc())
            .limit(20)
            .all()
        )
        for event in events:
            event_at = _event_primary_at(session, event) or _event_material_at(session, event)
            if event_at is None or event_at < cutoff or event_at > now:
                continue
            if not _event_has_earnings_material(session, event):
                continue
            if earnings_event_is_subsidiary_only_result(session, event):
                continue
            status = str(event.status or "").strip().lower()
            if not _event_is_completed(session, event, now=now):
                continue
            transcript_cutoff = event_at - timedelta(days=_EVENT_MATERIAL_WINDOW_DAYS)
            if _has_local_transcript(
                session,
                ticker=ticker,
                company_id=int(company.id) if company is not None else int(instrument.company_id) if instrument.company_id else None,
                issuer_id=issuer_id,
                since=transcript_cutoff,
                fiscal_year=int(event.fiscal_year) if event.fiscal_year is not None else None,
                fiscal_quarter=int(event.fiscal_quarter) if event.fiscal_quarter is not None else None,
            ):
                continue
            material_payload = _event_material_payload(session, event)
            policy = _transcript_lookup_policy(session=session, company=company, issuer=issuer, event=event)
            payload = {
                "source": "earnings_event",
                "event_id": int(event.id),
                "fiscal_year": int(event.fiscal_year) if event.fiscal_year is not None else None,
                "fiscal_quarter": int(event.fiscal_quarter) if event.fiscal_quarter is not None else None,
                "calendar_quarter": event.calendar_quarter,
                "event_status": event.status,
                "universe": universe.name,
                "row_id": int(universe_row.id),
                "earnings_material": material_payload,
                "transcript_lookup_policy": policy,
            }
            priority = 35 if policy.get("derived_transcript_candidate") else 30
            candidate = TranscriptCandidate(
                watch_key=f"earnings_event:{int(event.id)}",
                ticker=ticker,
                company_id=int(company.id) if company is not None else int(instrument.company_id) if instrument.company_id else None,
                issuer_id=issuer_id,
                company_name=company.name if company is not None else instrument.display_name,
                reason="earnings_event_gap",
                latest_filing_document_id=_event_reference_document_id(session, event),
                latest_filing_at=event_at,
                latest_filing_doc_type="earnings_event",
                priority=priority,
                payload=payload,
                fiscal_year=int(event.fiscal_year) if event.fiscal_year is not None else None,
                fiscal_quarter=int(event.fiscal_quarter) if event.fiscal_quarter is not None else None,
            )
            existing = out_by_event.get(int(event.id))
            if existing is None or candidate.priority > existing.priority:
                out_by_event[int(event.id)] = candidate
    return list(out_by_event.values())


def _watchlist_candidates(
    session: Session,
    *,
    lookback_days: int,
    include_seasonal: bool,
) -> list[TranscriptCandidate]:
    out_by_ticker: dict[str, TranscriptCandidate] = {}
    cutoff = utcnow() - timedelta(days=max(1, int(lookback_days)))
    for universe_row, instrument, universe in _active_universe_rows(session):
        ticker = normalize_ticker(instrument.ticker or instrument.yahoo_symbol)
        if not ticker:
            continue
        company = instrument.company if instrument.company_id else _company_for_ticker(session, ticker)
        issuer_id = int(company.issuer_id) if company is not None and company.issuer_id else int(instrument.issuer_id) if instrument.issuer_id else None
        issuer = session.get(Issuer, issuer_id) if issuer_id is not None else None
        latest = _latest_filing_for_company(session, int(company.id), lookback_days=lookback_days) if company is not None else None
        latest_doc: Document | None = latest[1] if latest else None
        latest_company_doc: CompanyDocument | None = latest[0] if latest else None
        filing_at = _document_at(latest_doc) if latest_doc is not None else None
        fiscal_year, fiscal_quarter = _document_period(latest_doc)
        reason = "watchlist_filing_gap" if latest_doc is not None else "watchlist_earnings_season"
        if latest_doc is None and not include_seasonal:
            continue
        transcript_cutoff = filing_at - timedelta(days=21) if filing_at else cutoff
        if _has_local_transcript(
            session,
            ticker=ticker,
            company_id=int(company.id) if company is not None else None,
            issuer_id=issuer_id,
            since=transcript_cutoff,
            fiscal_year=fiscal_year,
            fiscal_quarter=fiscal_quarter,
        ):
            continue
        watch_key = (
            f"filing:{int(company.id)}:{int(latest_doc.id)}"
            if company is not None and latest_doc is not None
            else f"watchlist:{ticker}"
        )
        candidate = TranscriptCandidate(
            watch_key=watch_key,
            ticker=ticker,
            company_id=int(company.id) if company is not None else int(instrument.company_id) if instrument.company_id else None,
            issuer_id=issuer_id,
            company_name=company.name if company is not None else instrument.display_name,
            reason=reason,
            latest_filing_document_id=int(latest_doc.id) if latest_doc is not None else None,
            latest_filing_at=filing_at,
            latest_filing_doc_type=latest_company_doc.doc_type if latest_company_doc is not None else None,
            priority=18 if latest_doc is not None else 5,
            payload={
                "source": "research_universe",
                "universe": universe.name,
                "row_id": int(universe_row.id),
                "transcript_lookup_policy": _transcript_lookup_policy(session=session, company=company, issuer=issuer),
            },
            fiscal_year=fiscal_year,
            fiscal_quarter=fiscal_quarter,
        )
        existing = out_by_ticker.get(ticker)
        if existing is None or candidate.priority > existing.priority:
            out_by_ticker[ticker] = candidate
    return list(out_by_ticker.values())


def select_transcript_candidates(
    session: Session,
    *,
    mode: str,
    lookback_days: int,
    force: bool = False,
    now: datetime | None = None,
    require_official_material: bool | None = None,
) -> list[TranscriptCandidate]:
    now_v = now or utcnow()
    mode_v = str(mode or "regular").strip().lower()
    require_material = (
        bool(getattr(settings, "transcript_scan_require_official_earnings_material", True))
        if require_official_material is None
        else bool(require_official_material)
    )
    include_seasonal = mode_v in {"gap_check", "weekly", "manual"} or _is_earnings_season(now_v)
    combined: dict[str, TranscriptCandidate] = {}
    for candidate in _event_gap_candidates(session, lookback_days=lookback_days):
        combined[candidate.watch_key] = candidate
    if not require_material:
        for candidate in _filing_gap_candidates(session, lookback_days=lookback_days):
            combined[candidate.watch_key] = candidate
        for candidate in _watchlist_candidates(session, lookback_days=lookback_days, include_seasonal=include_seasonal):
            existing = combined.get(candidate.watch_key)
            if existing is None or candidate.priority > existing.priority:
                combined[candidate.watch_key] = candidate

    due: list[TranscriptCandidate] = []
    for candidate in combined.values():
        state = session.query(TranscriptWatchState).filter_by(watch_key=candidate.watch_key).first()
        if state is not None and state.next_check_at is not None and state.next_check_at > now_v and not force:
            continue
        due.append(candidate)
    due.sort(key=lambda item: (item.priority, item.latest_filing_at or datetime.min, item.ticker), reverse=True)
    return due


def _next_miss_check(now: datetime, miss_count: int, *, mode: str) -> datetime:
    if str(mode or "").strip().lower() in {"gap_check", "weekly"}:
        return now + timedelta(days=7)
    retry_hours = max(1, int(getattr(settings, "transcript_scan_retry_hours", 24) or 24))
    if miss_count <= 2:
        return now + timedelta(hours=retry_hours)
    if miss_count <= 6:
        return now + timedelta(hours=retry_hours * 3)
    return now + timedelta(days=7)


def _state_period(row: TranscriptWatchState) -> tuple[int | None, int | None]:
    payload = row.payload if isinstance(row.payload, dict) else {}
    fiscal_year = payload.get("fiscal_year")
    fiscal_quarter = payload.get("fiscal_quarter")
    try:
        fiscal_year_v = int(fiscal_year) if fiscal_year is not None else None
    except Exception:
        fiscal_year_v = None
    try:
        fiscal_quarter_v = int(fiscal_quarter) if fiscal_quarter is not None else None
    except Exception:
        fiscal_quarter_v = None
    return fiscal_year_v, fiscal_quarter_v


def _upsert_watch_state(
    session: Session,
    candidate: TranscriptCandidate,
    *,
    status: str,
    mode: str,
    transcript_artifact_id: int | None = None,
    row: TranscriptRecord | None = None,
) -> TranscriptWatchState:
    now = utcnow()
    state = session.query(TranscriptWatchState).filter_by(watch_key=candidate.watch_key).first()
    if state is None:
        state = TranscriptWatchState(
            watch_key=candidate.watch_key,
            ticker=candidate.ticker,
            company_id=candidate.company_id,
            issuer_id=candidate.issuer_id,
            company_name=candidate.company_name,
            latest_filing_document_id=candidate.latest_filing_document_id,
            latest_filing_at=candidate.latest_filing_at,
            latest_filing_doc_type=candidate.latest_filing_doc_type,
            status=status,
            reason=candidate.reason,
            priority=candidate.priority,
            miss_count=0,
            payload={},
            created_at=now,
            updated_at=now,
        )
        session.add(state)
        session.flush()
    state.ticker = candidate.ticker
    state.company_id = candidate.company_id
    state.issuer_id = candidate.issuer_id
    state.company_name = candidate.company_name
    state.latest_filing_document_id = candidate.latest_filing_document_id
    state.latest_filing_at = candidate.latest_filing_at
    state.latest_filing_doc_type = candidate.latest_filing_doc_type
    state.status = status
    state.reason = candidate.reason
    state.priority = candidate.priority
    state.last_checked_at = now
    state.updated_at = now
    payload = dict(state.payload or {})
    payload.update(candidate.payload)
    payload["mode"] = mode
    if candidate.fiscal_year is not None:
        payload["fiscal_year"] = int(candidate.fiscal_year)
        payload["fiscal_quarter"] = int(candidate.fiscal_quarter) if candidate.fiscal_quarter is not None else None
    payload.setdefault(
        "transcript_lookup_policy",
        _transcript_lookup_policy(session=session, company=state.company, issuer=state.issuer, event=None),
    )
    if row is not None:
        payload["transcript_url"] = row.url
        payload["transcript_source_kind"] = row.source_kind
        payload["period_label"] = row.period_label
    state.payload = payload
    if status == "found":
        state.last_found_at = now
        state.transcript_artifact_id = transcript_artifact_id
        state.next_check_at = now + timedelta(days=30)
        state.miss_count = 0
    elif status == "covered":
        state.miss_count = 0
        state.next_check_at = now + timedelta(days=14)
    elif status == "watching":
        state.miss_count = 0
        state.next_check_at = _next_miss_check(now, 0, mode=mode)
    else:
        state.miss_count = int(state.miss_count or 0) + 1
        state.next_check_at = _next_miss_check(now, int(state.miss_count or 0), mode=mode)
    session.flush()
    return state


def _reconcile_stale_watch_states(session: Session, *, lookback_days: int) -> int:
    now = utcnow()
    rows = (
        session.query(TranscriptWatchState)
        .filter(TranscriptWatchState.status.in_(["missing", "watching"]))
        .filter(TranscriptWatchState.ticker.isnot(None))
        .order_by(TranscriptWatchState.updated_at.desc(), TranscriptWatchState.id.desc())
        .limit(500)
        .all()
    )
    updated = 0
    for row in rows:
        since = row.latest_filing_at - timedelta(days=21) if row.latest_filing_at else now - timedelta(days=max(1, int(lookback_days)))
        fiscal_year, fiscal_quarter = _state_period(row)
        if not _has_local_transcript(
            session,
            ticker=row.ticker,
            company_id=int(row.company_id) if row.company_id is not None else None,
            issuer_id=int(row.issuer_id) if row.issuer_id is not None else None,
            since=since,
            fiscal_year=fiscal_year,
            fiscal_quarter=fiscal_quarter,
        ):
            continue
        row.status = "covered"
        row.miss_count = 0
        row.last_checked_at = now
        row.next_check_at = now + timedelta(days=14)
        row.updated_at = now
        payload = dict(row.payload or {})
        payload["covered_by_local_transcript_reconcile"] = True
        row.payload = payload
        updated += 1
    if updated:
        session.flush()
    return updated


def _reconcile_found_event_watch_states(session: Session) -> int:
    now = utcnow()
    rows = (
        session.query(TranscriptWatchState)
        .filter(TranscriptWatchState.status == "found")
        .filter(TranscriptWatchState.watch_key.like("earnings_event:%"))
        .filter(TranscriptWatchState.transcript_artifact_id.isnot(None))
        .order_by(TranscriptWatchState.updated_at.desc(), TranscriptWatchState.id.desc())
        .limit(500)
        .all()
    )
    linked = 0
    for row in rows:
        artifact = session.get(IRArtifact, int(row.transcript_artifact_id)) if row.transcript_artifact_id is not None else None
        if artifact is None:
            continue
        fiscal_year, fiscal_quarter = _state_period(row)
        candidate = TranscriptCandidate(
            watch_key=str(row.watch_key or ""),
            ticker=str(row.ticker or ""),
            company_id=int(row.company_id) if row.company_id is not None else None,
            issuer_id=int(row.issuer_id) if row.issuer_id is not None else None,
            company_name=row.company_name,
            reason=row.reason or "earnings_event_gap",
            latest_filing_document_id=int(row.latest_filing_document_id) if row.latest_filing_document_id is not None else None,
            latest_filing_at=row.latest_filing_at,
            latest_filing_doc_type=row.latest_filing_doc_type,
            priority=int(row.priority or 0),
            payload=dict(row.payload or {}),
            fiscal_year=fiscal_year,
            fiscal_quarter=fiscal_quarter,
        )
        if not _link_candidate_event_call_material(session, candidate, artifact):
            continue
        payload = dict(row.payload or {})
        payload["linked_event_from_found_state_reconcile"] = True
        payload["linked_event_from_found_state_reconcile_at"] = now.isoformat()
        row.payload = payload
        row.updated_at = now
        linked += 1
    if linked:
        session.flush()
    return linked


def _reconcile_untrusted_event_watch_states(session: Session) -> int:
    now = utcnow()
    rows = (
        session.query(TranscriptWatchState)
        .filter(TranscriptWatchState.status.in_(["missing", "watching"]))
        .filter(TranscriptWatchState.watch_key.like("earnings_event:%"))
        .order_by(TranscriptWatchState.updated_at.desc(), TranscriptWatchState.id.desc())
        .limit(500)
        .all()
    )
    updated = 0
    for row in rows:
        try:
            event_id = int(str(row.watch_key or "").rsplit(":", 1)[-1])
        except Exception:
            continue
        event = session.get(EarningsEvent, event_id)
        if event is None:
            continue
        if not _event_has_earnings_material(session, event):
            continue
        if not _event_has_untrusted_gate_timing(session, event):
            continue
        if _event_is_completed(session, event, now=now):
            continue
        row.status = "blocked"
        row.reason = "untrusted_event_ingest_time"
        row.last_checked_at = now
        row.next_check_at = now + timedelta(days=14)
        row.updated_at = now
        payload = dict(row.payload or {})
        payload["blocked_reason"] = "untrusted_event_ingest_time"
        payload["blocked_by_transcript_gate_reconcile"] = True
        payload["blocked_at"] = now.isoformat()
        row.payload = payload
        updated += 1
    if updated:
        session.flush()
    return updated


def _reconcile_subsidiary_only_event_watch_states(session: Session) -> int:
    now = utcnow()
    rows = (
        session.query(TranscriptWatchState)
        .filter(TranscriptWatchState.status.in_(["missing", "watching", "blocked"]))
        .filter(TranscriptWatchState.watch_key.like("earnings_event:%"))
        .order_by(TranscriptWatchState.updated_at.desc(), TranscriptWatchState.id.desc())
        .limit(500)
        .all()
    )
    updated = 0
    for row in rows:
        try:
            event_id = int(str(row.watch_key or "").rsplit(":", 1)[-1])
        except Exception:
            continue
        event = session.get(EarningsEvent, event_id)
        if event is None or not earnings_event_is_subsidiary_only_result(session, event):
            continue
        changed = False
        if (
            event.actual_call_at is not None
            and event.transcript_artifact_id is None
            and event.presentation_artifact_id is None
            and event.webcast_artifact_id is None
        ):
            event.actual_call_at = None
            event.updated_at = now
            event_meta = dict(event.raw_meta or {})
            cleanup = dict(event_meta.get("transcript_gate_cleanup") or {})
            cleanup["cleared_false_actual_call_at"] = now.isoformat()
            cleanup["reason"] = "subsidiary_only_result_false_parent_call_material"
            event_meta["transcript_gate_cleanup"] = cleanup
            event.raw_meta = event_meta
            changed = True
        if row.status != "blocked" or row.reason != "subsidiary_only_result":
            row.status = "blocked"
            row.reason = "subsidiary_only_result"
            row.last_checked_at = now
            row.next_check_at = now + timedelta(days=30)
            row.updated_at = now
            payload = dict(row.payload or {})
            payload["blocked_reason"] = "subsidiary_only_result"
            payload["blocked_by_transcript_gate_reconcile"] = True
            payload["blocked_at"] = now.isoformat()
            row.payload = payload
            changed = True
        if changed:
            updated += 1
    if updated:
        session.flush()
    return updated


def handle_watchlist_transcript_scan_job(payload: dict[str, Any]) -> dict[str, int]:
    mode = str(payload.get("mode") or "regular").strip().lower()
    force = bool(payload.get("force")) or mode == "manual"
    require_official_material = payload.get("require_official_material")
    if require_official_material is None:
        require_official_material = getattr(settings, "transcript_scan_require_official_earnings_material", True)
    if bool(payload.get("allow_legacy_filing_candidates")):
        require_official_material = False
    if mode in {"gap_check", "weekly"}:
        default_lookback = int(getattr(settings, "transcript_gap_scan_lookback_days", 420) or 420)
    else:
        default_lookback = int(getattr(settings, "transcript_scan_lookback_days", 120) or 120)
    lookback_days = max(1, int(payload.get("lookback_days") or default_lookback))
    max_candidates = max(
        1,
        min(
            int(payload.get("max_candidates") or getattr(settings, "transcript_scan_max_candidates_per_run", 40) or 40),
            250,
        ),
    )

    with session_scope() as session:
        reconciled = _reconcile_stale_watch_states(session, lookback_days=lookback_days)
        linked = _reconcile_found_event_watch_states(session)
        blocked = _reconcile_untrusted_event_watch_states(session)
        blocked += _reconcile_subsidiary_only_event_watch_states(session)
        candidates = select_transcript_candidates(
            session,
            mode=mode,
            lookback_days=lookback_days,
            force=force,
            require_official_material=bool(require_official_material),
        )

    stats = {
        "candidates": len(candidates),
        "checked": 0,
        "deferred": 0,
        "found": 0,
        "created_or_updated": 0,
        "watch_states_upserted": 0,
        "missed": 0,
        "covered": reconciled,
        "linked": linked,
        "blocked": blocked,
        "watching": 0,
        "errors": 0,
        "official_material_gate": 1 if bool(require_official_material) else 0,
    }
    max_runtime_raw = payload.get("max_runtime_seconds")
    max_runtime_s = float(
        max_runtime_raw
        if max_runtime_raw is not None
        else getattr(settings, "transcript_scan_max_runtime_seconds", 240) or 240
    )
    deadline = time.monotonic() + max_runtime_s if max_runtime_s >= 0 else None
    planned_candidates = candidates[:max_candidates]
    for index, candidate in enumerate(planned_candidates):
        if deadline is not None and time.monotonic() >= deadline:
            stats["deferred"] += len(planned_candidates) - index
            break
        transcript_cutoff = candidate.latest_filing_at - timedelta(days=21) if candidate.latest_filing_at else utcnow() - timedelta(days=lookback_days)
        with session_scope() as session:
            if _has_local_transcript(
                session,
                ticker=candidate.ticker,
                company_id=candidate.company_id,
                issuer_id=candidate.issuer_id,
                since=transcript_cutoff,
                fiscal_year=candidate.fiscal_year,
                fiscal_quarter=candidate.fiscal_quarter,
            ):
                _upsert_watch_state(session, candidate, status="covered", mode=mode)
                stats["watch_states_upserted"] += 1
                stats["covered"] += 1
                continue
            call_material = _cover_candidate_with_official_call_material(session, candidate)
            if call_material is not None:
                payload = dict(candidate.payload or {})
                payload["covered_by_official_call_material"] = {
                    "artifact_id": int(call_material.id),
                    "document_id": int(call_material.document_id) if call_material.document_id is not None else None,
                    "artifact_type": call_material.artifact_type,
                    "artifact_subtype": call_material.artifact_subtype,
                    "title": call_material.title,
                    "url": call_material.canonical_url or call_material.download_url or call_material.webcast_url or call_material.audio_url,
                }
                candidate = TranscriptCandidate(
                    watch_key=candidate.watch_key,
                    ticker=candidate.ticker,
                    company_id=candidate.company_id,
                    issuer_id=candidate.issuer_id,
                    company_name=candidate.company_name,
                    reason=candidate.reason,
                    latest_filing_document_id=candidate.latest_filing_document_id,
                    latest_filing_at=candidate.latest_filing_at,
                    latest_filing_doc_type=candidate.latest_filing_doc_type,
                    priority=candidate.priority,
                    payload=payload,
                    fiscal_year=candidate.fiscal_year,
                    fiscal_quarter=candidate.fiscal_quarter,
                )
                _upsert_watch_state(session, candidate, status="covered", mode=mode)
                stats["watch_states_upserted"] += 1
                stats["covered"] += 1
                continue
            event_page_material = _cover_candidate_with_event_official_pages(session, candidate, lookback_days=lookback_days)
            if event_page_material is not None:
                payload = dict(candidate.payload or {})
                payload["covered_by_event_official_call_material_page"] = {
                    "artifact_id": int(event_page_material.id),
                    "document_id": int(event_page_material.document_id) if event_page_material.document_id is not None else None,
                    "artifact_type": event_page_material.artifact_type,
                    "artifact_subtype": event_page_material.artifact_subtype,
                    "title": event_page_material.title,
                    "url": event_page_material.canonical_url
                    or event_page_material.download_url
                    or event_page_material.webcast_url
                    or event_page_material.audio_url,
                }
                candidate = TranscriptCandidate(
                    watch_key=candidate.watch_key,
                    ticker=candidate.ticker,
                    company_id=candidate.company_id,
                    issuer_id=candidate.issuer_id,
                    company_name=candidate.company_name,
                    reason=candidate.reason,
                    latest_filing_document_id=candidate.latest_filing_document_id,
                    latest_filing_at=candidate.latest_filing_at,
                    latest_filing_doc_type=candidate.latest_filing_doc_type,
                    priority=candidate.priority,
                    payload=payload,
                    fiscal_year=candidate.fiscal_year,
                    fiscal_quarter=candidate.fiscal_quarter,
                )
                _upsert_watch_state(session, candidate, status="covered", mode=mode)
                stats["watch_states_upserted"] += 1
                stats["covered"] += 1
                continue
            if candidate.reason == "watchlist_earnings_season" and candidate.latest_filing_document_id is None:
                _upsert_watch_state(session, candidate, status="watching", mode=mode)
                stats["watch_states_upserted"] += 1
                stats["watching"] += 1
                continue
        try:
            SessionLocal = session_factory()
            with SessionLocal() as read_session:
                row = get_transcript(
                    read_session,
                    ticker=candidate.ticker,
                    most_recent=True,
                    year=candidate.fiscal_year,
                    quarter=candidate.fiscal_quarter,
                    lookback_days=lookback_days,
                )
            stats["checked"] += 1
        except Exception:
            row = None
            stats["errors"] += 1
        if _transcript_matches_candidate(row, candidate):
            assert row is not None
            row = _transcript_row_with_candidate_period(row, candidate)
            with session_scope() as session:
                artifact = upsert_transcript_record(session, row=row)
                if artifact is not None:
                    _link_candidate_event_call_material(session, candidate, artifact)
                    _upsert_watch_state(
                        session,
                        candidate,
                        status="found",
                        mode=mode,
                        transcript_artifact_id=int(artifact.id),
                        row=row,
                    )
                    stats["found"] += 1
                    stats["created_or_updated"] += 1
                    stats["watch_states_upserted"] += 1
                    continue
        status = "watching" if candidate.reason == "watchlist_earnings_season" and candidate.latest_filing_document_id is None else "missing"
        with session_scope() as session:
            _upsert_watch_state(session, candidate, status=status, mode=mode)
            stats["watch_states_upserted"] += 1
            if status == "watching":
                stats["watching"] += 1
            else:
                stats["missed"] += 1
    return stats
