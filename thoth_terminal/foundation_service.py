from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from thoth_terminal.connectors.document_fetch import build_fetched_document_payload
from thoth_terminal.db import utcnow
from thoth_terminal.decision_spine_service import canonical_entity_key, name_match_score, normalize_identifier_exchange
from thoth_terminal.knowledge_service import generate_knowledge_asset_draft
from thoth_terminal.net import fetch_with_fallback
from thoth_terminal.models import (
    Company,
    CompanyDocument,
    Document,
    EarningsEvent,
    IRArtifact,
    Issuer,
    IssuerIdentifier,
    KnowledgeAsset,
    ResearchSubject,
    SecuritiesFiling,
    Source,
)
from thoth_terminal.periods import PeriodNormalization, normalize_period_from_text
from thoth_terminal.institutional_service import sync_document_to_institutional
from thoth_terminal.research_report_service import build_research_report
from thoth_terminal.transcripts import (
    TranscriptRecord,
    _parse_period_from_text,
    document_is_readable_transcript,
    earnings_fiscal_year_bounds,
    get_transcript,
    is_plausible_earnings_fiscal_year,
    normalize_ticker,
)
from thoth_terminal.url_utils import clean_url


_OFFICIAL_FILING_CONNECTORS = {
    "edinet_disclosures",
    "edinetdb_financials",
    "filings_xbrl_org",
    "opendart_disclosures",
    "opendart_financials",
    "sec_companyfacts",
    "sec_edgar",
    "sec_edgar_rss",
    "taiwan_mops_financial_reports",
}


_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

_LEADING_DATE_RE = re.compile(r"\b(?P<month>\d{1,2})[/-](?P<day>\d{1,2})[/-](?P<year>\d{4})\b")
_ISO_DATE_RE = re.compile(r"\b(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})\b")
_MONTH_NAME_DATE_RE = re.compile(
    r"\b(?P<month>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
    r"\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?(?:,)?\s+(?P<year>\d{4})\b",
    re.IGNORECASE,
)
_PREANNOUNCE_HINTS = (
    " to report ",
    " sets conference call ",
    " conference call ",
    " earnings date ",
    " to review ",
)
_FILING_TYPE_RE = re.compile(
    r"\b("
    r"10-K/A|10-Q/A|8-K/A|10-K|10-Q|8-K|20-F|6-K|13F-HR|13F-NT|13G/A|13G|SCHEDULE 13G/A|"
    r"SCHEDULE 13G|SCHEDULE 13D/A|SCHEDULE 13D|DEF 14A|SC 13G/A|SC 13G|SC 13D/A|SC 13D"
    r")\b",
    re.IGNORECASE,
)


def _slugify(value: str | None, *, default: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return cleaned or default


def _issuer_slug_base(company: Company) -> str:
    preferred = str(getattr(company, "slug", "") or "").strip().replace("_", "-")
    return _slugify(preferred or getattr(company, "name", None), default=f"issuer-{int(company.id)}")


def _next_unique_issuer_slug(session: Session, *, base: str, issuer_id: int | None = None) -> str:
    slug = base
    counter = 2
    while True:
        row = session.query(Issuer).filter(Issuer.slug == slug).first()
        if row is None or (issuer_id is not None and int(row.id) == int(issuer_id)):
            return slug
        slug = f"{base}-{counter}"
        counter += 1


def _sync_issuer_from_company(issuer: Issuer, company: Company) -> None:
    company_name = str(getattr(company, "name", None) or "").strip()
    company_ticker = normalize_ticker(getattr(company, "ticker", None))
    company_key = canonical_entity_key(company_name)
    legal_key = canonical_entity_key(issuer.legal_name)
    display_key = canonical_entity_key(issuer.display_name)
    if company_name and company_name.upper() != company_ticker:
        if not display_key or len(company_key) >= len(display_key):
            issuer.display_name = company_name
        if not legal_key or len(company_key) >= len(legal_key):
            issuer.legal_name = company_name
    if not issuer.sector and getattr(company, "sector", None):
        issuer.sector = str(company.sector).strip() or issuer.sector
    if not issuer.industry and getattr(company, "industry", None):
        issuer.industry = str(company.industry).strip() or issuer.industry
    if not issuer.ir_website and getattr(company, "ir_url", None):
        issuer.ir_website = str(company.ir_url).strip() or issuer.ir_website
    if not issuer.website:
        for raw in [getattr(company, "ir_url", None), getattr(company, "reports_url", None), getattr(company, "calendar_url", None)]:
            parsed = urlparse(str(raw or "").strip())
            if parsed.scheme and parsed.netloc:
                issuer.website = f"{parsed.scheme}://{parsed.netloc}"
                break
    issuer.entity_status = issuer.entity_status or "ACTIVE"
    issuer.updated_at = utcnow()
    aliases = [str(v).strip() for v in (issuer.extra or {}).get("aliases", []) if str(v).strip()] if isinstance(issuer.extra, dict) else []
    for value in [company_name, getattr(company, "slug", None), company_ticker, canonical_entity_key(company_name)]:
        text = str(value or "").strip()
        if text and text not in aliases:
            aliases.append(text)
    extra = dict(issuer.extra or {})
    extra["aliases"] = aliases[:80]
    issuer.extra = extra


def _issuer_name_match(session: Session, company: Company) -> Issuer | None:
    name = str(getattr(company, "name", "") or "").strip()
    if not name:
        return None
    target_key = canonical_entity_key(name)
    if not target_key or target_key in {"sec edgar", "edgar", "sec"}:
        return None
    best: tuple[float, Issuer] | None = None
    for issuer in session.query(Issuer).order_by(Issuer.id.asc()).limit(5000).all():
        candidates = [
            issuer.display_name,
            issuer.legal_name,
            issuer.slug,
        ]
        if isinstance(issuer.extra, dict):
            candidates.extend([str(v) for v in (issuer.extra.get("aliases") or []) if str(v).strip()])
        score = max((name_match_score(name, candidate) for candidate in candidates if str(candidate or "").strip()), default=0.0)
        if score >= 0.92 and (best is None or score > best[0]):
            best = (score, issuer)
    return best[1] if best is not None else None


def ensure_company_issuer(session: Session, company: Company) -> Issuer | None:
    ticker = normalize_ticker(getattr(company, "ticker", None))
    issuer = None
    if getattr(company, "issuer_id", None) is not None:
        issuer = session.query(Issuer).filter_by(id=int(company.issuer_id)).first()
    if issuer is None and ticker:
        ident = (
            session.query(IssuerIdentifier)
            .filter(IssuerIdentifier.id_type == "TICKER", func.upper(IssuerIdentifier.id_value) == ticker)
            .order_by(IssuerIdentifier.is_primary.desc(), IssuerIdentifier.id.asc())
            .first()
        )
        if ident is not None:
            issuer = session.query(Issuer).filter_by(id=int(ident.issuer_id)).first()
    if issuer is None:
        name = str(getattr(company, "name", "") or "").strip()
        if name:
            issuer = (
                session.query(Issuer)
                .filter(
                    (func.lower(Issuer.display_name) == name.lower())
                    | (func.lower(Issuer.legal_name) == name.lower())
                )
                .order_by(Issuer.id.asc())
                .first()
            )
    if issuer is None:
        issuer = _issuer_name_match(session, company)
    now = utcnow()
    if issuer is None:
        issuer = Issuer(
            slug=_next_unique_issuer_slug(session, base=_issuer_slug_base(company)),
            legal_name=str(getattr(company, "name", "") or "").strip() or f"issuer-{int(company.id)}",
            display_name=str(getattr(company, "name", "") or "").strip() or f"issuer-{int(company.id)}",
            jurisdiction=None,
            entity_status="ACTIVE",
            sector=str(getattr(company, "sector", "") or "").strip() or None,
            industry=str(getattr(company, "industry", "") or "").strip() or None,
            sic_code=None,
            website=None,
            ir_website=str(getattr(company, "ir_url", "") or "").strip() or None,
            created_at=now,
            updated_at=now,
            extra={},
        )
        session.add(issuer)
        session.flush()
    else:
        issuer.slug = _next_unique_issuer_slug(session, base=_issuer_slug_base(company), issuer_id=int(issuer.id))
        _sync_issuer_from_company(issuer, company)
        session.flush()

    company.issuer_id = int(issuer.id)
    session.flush()

    if ticker:
        ident = (
            session.query(IssuerIdentifier)
            .filter(IssuerIdentifier.id_type == "TICKER", func.upper(IssuerIdentifier.id_value) == ticker)
            .order_by(IssuerIdentifier.is_primary.desc(), IssuerIdentifier.id.asc())
            .first()
        )
        if ident is None:
            session.add(
                IssuerIdentifier(
                    issuer_id=int(issuer.id),
                    id_type="TICKER",
                    id_value=ticker,
                    exchange=normalize_identifier_exchange(getattr(company, "exchange", None)),
                    currency=None,
                    is_primary=True,
                    valid_from=None,
                    valid_to=None,
                    created_at=now,
                )
            )
            session.flush()
        else:
            ident.issuer_id = int(issuer.id)
            ident.exchange = normalize_identifier_exchange(getattr(company, "exchange", None)) or ident.exchange
            ident.is_primary = True
            session.flush()

    return issuer


def ensure_company_issuers(session: Session) -> dict[str, int]:
    companies = session.query(Company).order_by(Company.id.asc()).all()
    linked = 0
    identifiers = 0
    for company in companies:
        had_issuer = getattr(company, "issuer_id", None) is not None
        issuer = ensure_company_issuer(session, company)
        if issuer is None:
            continue
        if not had_issuer:
            linked += 1
        ticker = normalize_ticker(getattr(company, "ticker", None))
        if ticker and session.query(IssuerIdentifier).filter(IssuerIdentifier.issuer_id == int(issuer.id), IssuerIdentifier.id_type == "TICKER", func.upper(IssuerIdentifier.id_value) == ticker).first():
            identifiers += 1
    return {"companies": len(companies), "linked": linked, "ticker_identifiers": identifiers}


def _document_url(document: Document) -> str:
    url = str(getattr(document, "canonical_url", None) or getattr(document, "url", None) or "").strip()
    return clean_url(url) or f"thoth://documents/{int(document.id)}"


def _extract_date(value: str | None) -> date | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    for pattern in (_LEADING_DATE_RE, _ISO_DATE_RE):
        match = pattern.search(raw)
        if match:
            try:
                return date(int(match.group("year")), int(match.group("month")), int(match.group("day")))
            except Exception:
                return None
    match = _MONTH_NAME_DATE_RE.search(raw)
    if match:
        month_token = str(match.group("month") or "").strip(".").lower()
        month_v = _MONTHS.get(month_token)
        if month_v is None:
            return None
        try:
            return date(int(match.group("year")), month_v, int(match.group("day")))
        except Exception:
            return None
    return None


def _scheduled_date_for_document(document: Document) -> date | None:
    for candidate in [document.title, document.normalized_text, document.raw_text]:
        parsed = _extract_date(candidate)
        if parsed is not None:
            return parsed
    return None


def _leading_publication_datetime(value: str | None) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    for pattern in (_LEADING_DATE_RE, _ISO_DATE_RE):
        match = pattern.match(raw)
        if not match:
            continue
        try:
            return datetime(int(match.group("year")), int(match.group("month")), int(match.group("day")))
        except Exception:
            return None
    match = _MONTH_NAME_DATE_RE.match(raw)
    if match:
        month_token = str(match.group("month") or "").strip(".").lower()
        month_v = _MONTHS.get(month_token)
        if month_v is None:
            return None
        try:
            return datetime(int(match.group("year")), month_v, int(match.group("day")))
        except Exception:
            return None
    return None


def _document_publication_datetime(document: Document) -> datetime | None:
    if getattr(document, "published_at", None) is not None:
        return document.published_at
    for key in (
        "published_at",
        "published_date",
        "publication_date",
        "released_at",
        "release_date",
        "announced_at",
        "announcement_date",
        "filed_date",
        "filing_date",
        "accepted_at",
        "updated",
    ):
        parsed = _document_extra_datetime(document, key)
        if parsed is not None:
            return parsed
    # Some issuer list pages prefix link labels with the release date. Only trust
    # leading dates here; dates inside titles often describe the fiscal period.
    return _leading_publication_datetime(getattr(document, "title", None))


def _period_for_document(document: Document) -> tuple[int | None, int | None]:
    period = _period_normalization_for_document(document)
    return period.fiscal_year, period.fiscal_quarter


def _period_normalization_for_document(
    document: Document,
    *,
    issuer: Issuer | None = None,
    company: Company | None = None,
) -> PeriodNormalization:
    extra = dict(document.extra or {}) if isinstance(document.extra, dict) else {}
    return normalize_period_from_text(
        document.title,
        _document_url(document),
        published_at=getattr(document, "published_at", None) or getattr(document, "ingested_at", None),
        issuer=issuer,
        company=company,
        metadata=extra,
        annual_as_q4=True,
    )


def _period_payload(period: PeriodNormalization) -> dict[str, Any]:
    return {key: value for key, value in period.to_dict().items() if value is not None}


def _merge_period_meta(meta: dict[str, object] | None, period: PeriodNormalization) -> dict[str, object]:
    payload = dict(meta or {})
    period_dict = _period_payload(period)
    if period_dict:
        payload["period_normalization"] = period_dict
    return payload


def _period_from_meta(meta: dict[str, Any] | None) -> PeriodNormalization | None:
    payload = meta.get("period_normalization") if isinstance(meta, dict) else None
    if not isinstance(payload, dict):
        return None
    try:
        period_start = date.fromisoformat(str(payload["period_start"])) if payload.get("period_start") else None
    except Exception:
        period_start = None
    try:
        period_end = date.fromisoformat(str(payload["period_end"])) if payload.get("period_end") else None
    except Exception:
        period_end = None
    try:
        fiscal_year = int(payload["fiscal_year"]) if payload.get("fiscal_year") is not None else None
    except Exception:
        fiscal_year = None
    try:
        fiscal_quarter = int(payload["fiscal_quarter"]) if payload.get("fiscal_quarter") is not None else None
    except Exception:
        fiscal_quarter = None
    return PeriodNormalization(
        fiscal_year=fiscal_year,
        fiscal_quarter=fiscal_quarter,
        period_start=period_start,
        period_end=period_end,
        calendar_quarter=str(payload.get("calendar_quarter") or "").strip() or None,
        issuer_fiscal_year_label=str(payload.get("issuer_fiscal_year_label") or "").strip() or None,
        vendor_fiscal_label=str(payload.get("vendor_fiscal_label") or "").strip() or None,
        source_label_raw=str(payload.get("source_label_raw") or "").strip() or None,
        basis=str(payload.get("basis") or "").strip() or None,
        confidence=str(payload.get("confidence") or "").strip() or None,
    )


def _artifact_kind(company_doc: CompanyDocument, document: Document) -> tuple[str | None, str | None]:
    doc_type = str(getattr(company_doc, "doc_type", "") or "").strip().lower()
    title_l = str(getattr(document, "title", "") or "").strip().lower()
    if doc_type == "filing":
        return None, None
    if doc_type in {"earnings_transcript", "transcript"}:
        return "transcript", "earnings"
    if doc_type in {"presentation", "earnings_presentation"}:
        return "presentation", "earnings" if "earn" in title_l or "financial result" in title_l else None
    if doc_type in {"calendar_event", "earnings_event"}:
        return "calendar_event", "earnings"
    if doc_type == "earnings_release":
        if "securities report" in title_l:
            return "report", "securities_report"
        if "presentation" in title_l:
            return "presentation", "earnings" if "earn" in title_l or "financial result" in title_l else None
        return "press_release", ("earnings_notice" if _is_preannouncement(document) else "earnings")
    if doc_type == "monthly_sales":
        return "fact_sheet", "monthly_sales"
    if doc_type in {"annual_report", "financial_report"}:
        return "report", doc_type
    return "document", doc_type or None


def _is_preannouncement(document: Document) -> bool:
    title_l = f" {str(getattr(document, 'title', '') or '').strip().lower()} "
    body_l = f" {str(getattr(document, 'normalized_text', '') or '').strip().lower()} "
    if " reports " in title_l and (" financial results " in title_l or " earnings " in title_l):
        return False
    if any(token in title_l for token in _PREANNOUNCE_HINTS):
        return True
    return " earnings date " in body_l or " conference call " in body_l


def _merge_legacy_doc_id(meta: dict[str, object] | None, *, document_id: int) -> dict[str, object]:
    payload = dict(meta or {})
    rows = [int(v) for v in (payload.get("legacy_company_document_ids") or []) if str(v).isdigit()]
    if int(document_id) not in rows:
        rows.append(int(document_id))
    payload["legacy_company_document_ids"] = sorted(rows)
    return payload


def _host_label(document: Document) -> str | None:
    parsed = urlparse(_document_url(document))
    return parsed.netloc.lower() or None


def _ensure_ir_artifact(
    session: Session,
    *,
    issuer: Issuer,
    company: Company | None,
    company_doc: CompanyDocument,
    document: Document,
) -> IRArtifact | None:
    artifact_type, artifact_subtype = _artifact_kind(company_doc, document)
    if not artifact_type:
        return None
    period = _period_normalization_for_document(document, issuer=issuer, company=company)
    fiscal_year, fiscal_quarter = period.fiscal_year, period.fiscal_quarter
    url = _document_url(document)
    row = session.query(IRArtifact).filter_by(issuer_id=int(issuer.id), artifact_type=artifact_type, canonical_url=url).first()
    if row is None:
        row = (
            session.query(IRArtifact)
            .filter_by(issuer_id=int(issuer.id), canonical_url=url)
            .order_by(IRArtifact.id.asc())
            .first()
        )
    if row is None:
        published_at = _document_publication_datetime(document)
        row = IRArtifact(
            issuer_id=int(issuer.id),
            document_id=int(document.id),
            artifact_type=artifact_type,
            artifact_subtype=artifact_subtype,
            published_at=published_at,
            event_date=_scheduled_date_for_document(document),
            fiscal_year=fiscal_year,
            fiscal_quarter=fiscal_quarter,
            title=str(getattr(document, "title", "") or "").strip() or None,
            description=None,
            canonical_url=url,
            download_url=(url if url.lower().endswith(".pdf") else None),
            webcast_url=(url if "webcast" in url.lower() else None),
            audio_url=None,
            is_derived_transcript=False,
            ical_uid=None,
            language=getattr(document, "language", None),
            raw_meta=_merge_legacy_doc_id(
                _merge_period_meta({"source_kind": "legacy_company_document"}, period),
                document_id=int(document.id),
            ),
            created_at=utcnow(),
        )
        session.add(row)
        session.flush()
        return row

    row.document_id = int(document.id)
    row.artifact_type = artifact_type or row.artifact_type
    row.artifact_subtype = artifact_subtype or row.artifact_subtype
    row.published_at = row.published_at or _document_publication_datetime(document)
    row.event_date = row.event_date or _scheduled_date_for_document(document)
    row.fiscal_year = fiscal_year if fiscal_year is not None else row.fiscal_year
    row.fiscal_quarter = fiscal_quarter if fiscal_quarter is not None else row.fiscal_quarter
    row.title = str(getattr(document, "title", "") or "").strip() or row.title
    row.language = getattr(document, "language", None) or row.language
    row.download_url = row.download_url or (url if url.lower().endswith(".pdf") else None)
    if "webcast" in url.lower():
        row.webcast_url = row.webcast_url or url
    row.raw_meta = _merge_legacy_doc_id(
        _merge_period_meta(row.raw_meta if isinstance(row.raw_meta, dict) else {}, period),
        document_id=int(document.id),
    )
    session.flush()
    return row


def _filing_accession(document: Document) -> str:
    extra = document.extra if isinstance(document.extra, dict) else {}
    raw = str(extra.get("accession_number") or "").strip()
    if raw:
        return raw
    external_id = str(getattr(document, "external_id", "") or "").strip()
    if external_id:
        return external_id
    return hashlib.sha1(_document_url(document).encode("utf-8")).hexdigest()


def _filing_regulator(document: Document) -> str:
    extra = document.extra if isinstance(document.extra, dict) else {}
    connector = str(extra.get("connector") or "").strip()
    if connector in {"edinetdb_financials", "edinet_disclosures"}:
        return "EDINET"
    if connector in {"opendart_disclosures", "opendart_financials"}:
        return "DART"
    if connector == "taiwan_mops_financial_reports":
        return "TW_MOPS"
    return str(extra.get("regulator") or "").strip().upper()[:16] or "SEC"


def _filing_type(document: Document) -> str:
    extra = document.extra if isinstance(document.extra, dict) else {}
    for key in ("filing_type", "form_type", "form"):
        raw = str(extra.get(key) or "").strip().upper()
        if raw:
            return raw[:32]
    connector = str(extra.get("connector") or "").strip()
    section = str(extra.get("section") or "").strip()
    if connector == "edinetdb_financials" and section:
        return f"EDINETDB_{section.upper()}"[:32]
    if connector == "edinet_disclosures":
        form_code = str(extra.get("form_code") or extra.get("doc_type_code") or "").strip().upper()
        return f"EDINET_{form_code}"[:32] if form_code else "EDINET_FILING"
    if connector == "opendart_financials":
        report_code = str(extra.get("report_code") or "").strip().upper()
        return f"DART_{report_code}"[:32] if report_code else "DART_FINANCIALS"
    title = str(getattr(document, "title", "") or "").strip()
    match = _FILING_TYPE_RE.search(title)
    if match:
        return match.group(1).upper()
    if " - " in title:
        return str(title.split(" - ", 1)[0] or "").strip().upper()[:32] or "FILING"
    return "FILING"


def _document_extra_datetime(document: Document, key: str) -> datetime | None:
    extra = document.extra if isinstance(document.extra, dict) else {}
    raw = str(extra.get(key) or "").strip()
    if not raw:
        return None
    for value in (raw, raw.replace("Z", "+00:00")):
        try:
            parsed = datetime.fromisoformat(value)
            return parsed.replace(tzinfo=None) if parsed.tzinfo is not None else parsed
        except Exception:
            continue
    try:
        return datetime.combine(date.fromisoformat(raw[:10]), datetime.min.time())
    except Exception:
        return None


def _document_extra_date(document: Document, key: str) -> date | None:
    parsed = _document_extra_datetime(document, key)
    return parsed.date() if parsed is not None else None


def _document_extra_bool(document: Document, key: str) -> bool:
    extra = document.extra if isinstance(document.extra, dict) else {}
    value = extra.get(key)
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _ensure_securities_filing(
    session: Session,
    *,
    issuer: Issuer,
    company: Company | None,
    document: Document,
) -> SecuritiesFiling:
    accession = _filing_accession(document)
    regulator = _filing_regulator(document)
    period = _period_normalization_for_document(document, issuer=issuer, company=company)
    fiscal_year, fiscal_quarter = period.fiscal_year, period.fiscal_quarter
    extra = document.extra if isinstance(document.extra, dict) else {}
    filed_at = _document_extra_datetime(document, "filed_date") or getattr(document, "published_at", None) or getattr(document, "ingested_at", None) or utcnow()
    accepted_at = _document_extra_datetime(document, "accepted_at")
    period_end = _document_extra_date(document, "period_of_report") or period.period_end
    exhibits = extra.get("exhibits") if isinstance(extra.get("exhibits"), dict) else {}
    has_xbrl = _document_extra_bool(document, "is_xbrl")
    is_earnings_release = bool(
        _document_extra_bool(document, "is_earnings_release")
        or "financial result" in str(getattr(document, "title", "") or "").lower()
        or "earnings" in str(getattr(document, "title", "") or "").lower()
    )
    row = session.query(SecuritiesFiling).filter_by(regulator=regulator, accession_number=accession).first()
    if row is None:
        row = SecuritiesFiling(
            issuer_id=int(issuer.id),
            document_id=int(document.id),
            regulator=regulator,
            accession_number=accession,
            filing_type=_filing_type(document),
            filing_subtype=None,
            filed_at=filed_at,
            accepted_at=accepted_at,
            period_end=period_end,
            fiscal_year=fiscal_year,
            fiscal_quarter=fiscal_quarter,
            title=str(getattr(document, "title", "") or "").strip() or None,
            description=None,
            primary_document_url=_document_url(document),
            index_url=str(extra.get("index_url") or "").strip() or _document_url(document),
            filing_url=_document_url(document),
            exhibits=exhibits,
            has_xbrl=has_xbrl,
            xbrl_url=None,
            is_earnings_release=is_earnings_release,
            is_guidance=("guidance" in str(getattr(document, "title", "") or "").lower()),
            raw_meta=_merge_legacy_doc_id(
                _merge_period_meta({"source_kind": "legacy_company_document", "document_extra": extra}, period),
                document_id=int(document.id),
            ),
            created_at=utcnow(),
        )
        session.add(row)
        session.flush()
        return row

    row.issuer_id = int(issuer.id)
    row.document_id = int(document.id)
    row.filing_type = row.filing_type or _filing_type(document)
    row.filed_at = filed_at or row.filed_at
    row.accepted_at = accepted_at or row.accepted_at
    row.period_end = period_end or row.period_end
    row.fiscal_year = fiscal_year if fiscal_year is not None else row.fiscal_year
    row.fiscal_quarter = fiscal_quarter if fiscal_quarter is not None else row.fiscal_quarter
    row.title = str(getattr(document, "title", "") or "").strip() or row.title
    row.primary_document_url = row.primary_document_url or _document_url(document)
    row.index_url = row.index_url or str(extra.get("index_url") or "").strip() or _document_url(document)
    row.filing_url = row.filing_url or _document_url(document)
    row.exhibits = row.exhibits or exhibits
    row.has_xbrl = bool(row.has_xbrl or has_xbrl)
    row.is_earnings_release = bool(row.is_earnings_release or is_earnings_release)
    row.raw_meta = _merge_legacy_doc_id(
        _merge_period_meta({**(row.raw_meta if isinstance(row.raw_meta, dict) else {}), "document_extra": extra}, period),
        document_id=int(document.id),
    )
    session.flush()
    return row


def _ensure_earnings_event(
    session: Session,
    *,
    issuer: Issuer,
    company: Company | None = None,
    company_doc: CompanyDocument | None,
    document: Document | None,
    artifact: IRArtifact | None = None,
    filing: SecuritiesFiling | None = None,
) -> EarningsEvent | None:
    period: PeriodNormalization | None = None
    if artifact is not None:
        period = _period_from_meta(artifact.raw_meta if isinstance(artifact.raw_meta, dict) else None)
        fiscal_year = period.fiscal_year if period is not None and period.fiscal_year is not None else artifact.fiscal_year
        fiscal_quarter = period.fiscal_quarter if period is not None and period.fiscal_quarter is not None else artifact.fiscal_quarter
    elif filing is not None:
        period = _period_from_meta(filing.raw_meta if isinstance(filing.raw_meta, dict) else None)
        fiscal_year = period.fiscal_year if period is not None and period.fiscal_year is not None else filing.fiscal_year
        fiscal_quarter = period.fiscal_quarter if period is not None and period.fiscal_quarter is not None else filing.fiscal_quarter
    elif document is not None:
        period = _period_normalization_for_document(document, issuer=issuer, company=company)
        fiscal_year, fiscal_quarter = period.fiscal_year, period.fiscal_quarter
    else:
        fiscal_year, fiscal_quarter = None, None
    if fiscal_year is None:
        return None
    if not is_plausible_earnings_fiscal_year(fiscal_year):
        return None

    row = session.query(EarningsEvent).filter_by(
        issuer_id=int(issuer.id),
        fiscal_year=int(fiscal_year),
        fiscal_quarter=(int(fiscal_quarter) if fiscal_quarter is not None else None),
    ).first()
    if row is None and period is not None and period.period_end is not None:
        query = session.query(EarningsEvent).filter(
            EarningsEvent.issuer_id == int(issuer.id),
            EarningsEvent.period_end == period.period_end,
        )
        if fiscal_quarter is not None:
            query = query.filter(or_(EarningsEvent.fiscal_quarter == int(fiscal_quarter), EarningsEvent.fiscal_quarter.is_(None)))
        row = query.order_by(EarningsEvent.id.asc()).first()
    if row is None and period is not None and period.calendar_quarter:
        row = (
            session.query(EarningsEvent)
            .filter(
                EarningsEvent.issuer_id == int(issuer.id),
                EarningsEvent.calendar_quarter == period.calendar_quarter,
                EarningsEvent.fiscal_quarter == (int(fiscal_quarter) if fiscal_quarter is not None else None),
            )
            .order_by(EarningsEvent.id.asc())
            .first()
        )
    now = utcnow()
    if row is None:
        row = EarningsEvent(
            issuer_id=int(issuer.id),
            fiscal_year=int(fiscal_year),
            fiscal_quarter=(int(fiscal_quarter) if fiscal_quarter is not None else None),
            period_start=period.period_start if period is not None else None,
            period_end=period.period_end if period is not None else None,
            calendar_quarter=period.calendar_quarter if period is not None else None,
            issuer_fiscal_year_label=period.issuer_fiscal_year_label if period is not None else None,
            vendor_fiscal_label=period.vendor_fiscal_label if period is not None else None,
            source_label_raw=period.source_label_raw if period is not None else None,
            scheduled_date=None,
            scheduled_time=None,
            scheduled_tz=None,
            date_provenance="estimated",
            date_source=None,
            actual_release_at=None,
            actual_call_at=None,
            status="scheduled",
            release_filing_id=None,
            release_disclosure_id=None,
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
    elif period is not None:
        row.fiscal_year = int(fiscal_year)
        row.fiscal_quarter = int(fiscal_quarter) if fiscal_quarter is not None else row.fiscal_quarter
        row.period_start = period.period_start or row.period_start
        row.period_end = period.period_end or row.period_end
        row.calendar_quarter = period.calendar_quarter or row.calendar_quarter
        row.issuer_fiscal_year_label = period.issuer_fiscal_year_label or row.issuer_fiscal_year_label
        row.vendor_fiscal_label = period.vendor_fiscal_label or row.vendor_fiscal_label
        row.source_label_raw = period.source_label_raw or row.source_label_raw

    doc_type = str(getattr(company_doc, "doc_type", "") or "").strip().lower() if company_doc is not None else ""
    if document is not None:
        event_meta = row.raw_meta if isinstance(row.raw_meta, dict) else {}
        if period is not None:
            event_meta = _merge_period_meta(event_meta, period)
        row.raw_meta = _merge_legacy_doc_id(event_meta, document_id=int(document.id))
        scheduled_date = _scheduled_date_for_document(document)
        published_at = _document_publication_datetime(document)
        if filing is not None and bool(filing.is_earnings_release):
            row.release_filing_id = int(filing.id)
            if published_at is not None:
                row.actual_release_at = published_at
                row.scheduled_date = row.scheduled_date or published_at.date()
            row.status = "completed"
        if doc_type in {"calendar_event", "earnings_event"} or _is_preannouncement(document):
            if scheduled_date is not None:
                row.scheduled_date = scheduled_date
                row.date_provenance = "issuer_published"
                row.date_source = _host_label(document) or row.date_source
                if row.status != "completed":
                    row.status = "scheduled"
        elif doc_type == "earnings_release":
            if artifact is not None and artifact.artifact_type == "press_release" and artifact.artifact_subtype == "earnings":
                row.release_artifact_id = int(artifact.id)
            if published_at is not None:
                row.actual_release_at = published_at
                row.scheduled_date = row.scheduled_date or published_at.date()
            row.status = "completed"
        elif doc_type in {"presentation", "earnings_presentation"}:
            if artifact is not None:
                row.presentation_artifact_id = int(artifact.id)
            if published_at is not None and row.actual_call_at is None:
                row.actual_call_at = published_at
        elif doc_type in {"earnings_transcript", "transcript"}:
            if artifact is not None:
                row.transcript_artifact_id = int(artifact.id)
            if published_at is not None:
                row.actual_call_at = published_at
                row.actual_release_at = row.actual_release_at or published_at
            row.status = "completed"

    if artifact is not None:
        if period is not None:
            row.raw_meta = _merge_period_meta(row.raw_meta if isinstance(row.raw_meta, dict) else {}, period)
        if artifact.artifact_type == "transcript":
            row.transcript_artifact_id = int(artifact.id)
            if artifact.published_at is not None:
                row.actual_call_at = artifact.published_at
                row.actual_release_at = row.actual_release_at or artifact.published_at
            row.status = "completed"
        elif artifact.artifact_type == "presentation":
            row.presentation_artifact_id = int(artifact.id)
            if row.release_artifact_id == int(artifact.id):
                row.release_artifact_id = None
        elif artifact.artifact_type == "press_release" and artifact.artifact_subtype == "earnings":
            row.release_artifact_id = int(artifact.id)
            if artifact.published_at is not None:
                row.actual_release_at = artifact.published_at
                row.scheduled_date = row.scheduled_date or artifact.published_at.date()
            row.status = "completed"
        elif artifact.artifact_type == "report":
            if row.release_artifact_id == int(artifact.id):
                row.release_artifact_id = None
        elif artifact.artifact_type == "calendar_event" and artifact.event_date is not None:
            row.scheduled_date = row.scheduled_date or artifact.event_date
            row.date_provenance = "issuer_published"
            row.date_source = row.date_source or (artifact.canonical_url and urlparse(artifact.canonical_url).netloc.lower())
            if row.status != "completed":
                row.status = "scheduled"

    if filing is not None and bool(filing.is_earnings_release):
        if period is not None:
            row.raw_meta = _merge_period_meta(row.raw_meta if isinstance(row.raw_meta, dict) else {}, period)
        row.release_filing_id = int(filing.id)
        row.status = "completed"
        if filing.filed_at is not None:
            row.actual_release_at = row.actual_release_at or filing.filed_at
            row.scheduled_date = row.scheduled_date or filing.filed_at.date()

    row.updated_at = now
    session.flush()
    return row


def quarantine_implausible_earnings_events(
    session: Session,
    *,
    today: date | None = None,
    max_future_years: int = 3,
    limit: int = 5000,
    dry_run: bool = False,
) -> dict[str, int]:
    min_year, max_year = earnings_fiscal_year_bounds(
        today=today,
        max_future_years=max_future_years,
    )
    rows = (
        session.query(EarningsEvent)
        .filter(EarningsEvent.fiscal_year.isnot(None))
        .filter(
            (EarningsEvent.fiscal_year < min_year)
            | (EarningsEvent.fiscal_year > max_year)
        )
        .filter(func.lower(func.coalesce(EarningsEvent.status, "")) != "invalid")
        .order_by(EarningsEvent.updated_at.desc().nullslast(), EarningsEvent.id.asc())
        .limit(max(1, int(limit)))
        .all()
    )
    if dry_run:
        return {"quarantined": 0, "candidates": len(rows), "min_year": min_year, "max_year": max_year}

    now = utcnow()
    for row in rows:
        meta = dict(row.raw_meta if isinstance(row.raw_meta, dict) else {})
        meta["invalid_reason"] = "implausible_earnings_fiscal_year"
        meta["invalid_fiscal_year"] = int(row.fiscal_year) if row.fiscal_year is not None else None
        meta["valid_fiscal_year_min"] = min_year
        meta["valid_fiscal_year_max"] = max_year
        meta["invalid_checked_at"] = now.isoformat()
        row.raw_meta = meta
        row.status = "invalid"
        row.updated_at = now
    session.flush()
    return {"quarantined": len(rows), "candidates": len(rows), "min_year": min_year, "max_year": max_year}


def _should_bootstrap_securities_filing(company_doc: CompanyDocument, document: Document) -> bool:
    doc_type_value = str(getattr(company_doc, "doc_type", "") or "").strip().lower()
    doc_kind_value = str(getattr(document, "kind", "") or "").strip().lower()
    extra = document.extra if isinstance(document.extra, dict) else {}
    connector = str(extra.get("connector") or "").strip()
    if doc_kind_value in {"securities_filing", "sec_filing"} and connector in _OFFICIAL_FILING_CONNECTORS:
        return True
    return doc_type_value in {"filing", "financial_report", "earnings_release"} and connector in _OFFICIAL_FILING_CONNECTORS


def _bootstrap_company_document_rows(
    session: Session,
    rows: list[tuple[CompanyDocument, Company, Document]],
) -> dict[str, int]:
    stats = {"issuer_links": 0, "filings": 0, "ir_artifacts": 0, "earnings_events": 0}
    filing_count_before = session.query(SecuritiesFiling).count()
    artifact_count_before = session.query(IRArtifact).count()
    event_count_before = session.query(EarningsEvent).count()
    for company_doc, company, document in rows:
        issuer = session.get(Issuer, int(company.issuer_id)) if company.issuer_id is not None else ensure_company_issuer(session, company)
        if issuer is None:
            continue
        doc_type_value = str(getattr(company_doc, "doc_type", "") or "").strip().lower()
        if _should_bootstrap_securities_filing(company_doc, document):
            filing = _ensure_securities_filing(session, issuer=issuer, company=company, document=document)
            _ensure_earnings_event(session, issuer=issuer, company=company, company_doc=company_doc, document=document, filing=filing)
            if doc_type_value == "filing":
                continue
        artifact = _ensure_ir_artifact(session, issuer=issuer, company=company, company_doc=company_doc, document=document)
        _ensure_earnings_event(session, issuer=issuer, company=company, company_doc=company_doc, document=document, artifact=artifact)
    stats["filings"] = max(0, session.query(SecuritiesFiling).count() - filing_count_before)
    stats["ir_artifacts"] = max(0, session.query(IRArtifact).count() - artifact_count_before)
    stats["earnings_events"] = max(0, session.query(EarningsEvent).count() - event_count_before)
    session.flush()
    return stats


def bootstrap_company_rails(session: Session) -> dict[str, int]:
    issuer_stats = ensure_company_issuers(session)
    rows = (
        session.query(CompanyDocument, Company, Document)
        .join(Company, Company.id == CompanyDocument.company_id)
        .join(Document, Document.id == CompanyDocument.document_id)
        .order_by(CompanyDocument.id.asc())
        .all()
    )
    stats = _bootstrap_company_document_rows(session, rows)
    stats["issuer_links"] = int(issuer_stats.get("linked", 0))
    return stats


def bootstrap_company_rails_for_company_ids(session: Session, *, company_ids: list[int] | set[int]) -> dict[str, int]:
    ids = sorted({int(value) for value in company_ids if str(value).isdigit() or isinstance(value, int)})
    if not ids:
        return {"issuer_links": 0, "filings": 0, "ir_artifacts": 0, "earnings_events": 0}
    before = {
        int(row.id): getattr(row, "issuer_id", None)
        for row in session.query(Company).filter(Company.id.in_(ids)).all()
    }
    for company in session.query(Company).filter(Company.id.in_(ids), Company.issuer_id.is_(None)).all():
        ensure_company_issuer(session, company)
    rows = (
        session.query(CompanyDocument, Company, Document)
        .join(Company, Company.id == CompanyDocument.company_id)
        .join(Document, Document.id == CompanyDocument.document_id)
        .filter(Company.id.in_(ids))
        .order_by(CompanyDocument.id.asc())
        .all()
    )
    stats = _bootstrap_company_document_rows(session, rows)
    stats["issuer_links"] = sum(
        1
        for company in session.query(Company).filter(Company.id.in_(ids)).all()
        if before.get(int(company.id)) is None and company.issuer_id is not None
    )
    return stats


def bootstrap_company_rails_for_company(session: Session, *, company_slug: str) -> dict[str, int]:
    company = session.query(Company).filter_by(slug=company_slug).first()
    if company is None:
        return {"issuer_links": 0, "filings": 0, "ir_artifacts": 0, "earnings_events": 0}
    had_issuer = getattr(company, "issuer_id", None) is not None
    ensure_company_issuer(session, company)
    rows = (
        session.query(CompanyDocument, Company, Document)
        .join(Company, Company.id == CompanyDocument.company_id)
        .join(Document, Document.id == CompanyDocument.document_id)
        .filter(Company.id == int(company.id))
        .order_by(CompanyDocument.id.asc())
        .all()
    )
    stats = _bootstrap_company_document_rows(session, rows)
    stats["issuer_links"] = 0 if had_issuer or company.issuer_id is None else 1
    return stats


def upsert_transcript_record(session: Session, *, row: TranscriptRecord) -> IRArtifact | None:
    ticker = normalize_ticker(row.ticker)
    if not ticker or not row.url:
        return None
    company = session.query(Company).filter(func.upper(Company.ticker) == ticker).order_by(Company.id.asc()).first()
    issuer = ensure_company_issuer(session, company) if company is not None else None
    if issuer is None:
        ident = session.query(IssuerIdentifier).filter(IssuerIdentifier.id_type == "TICKER", func.upper(IssuerIdentifier.id_value) == ticker).order_by(IssuerIdentifier.is_primary.desc(), IssuerIdentifier.id.asc()).first()
        if ident is not None:
            issuer = session.query(Issuer).filter_by(id=int(ident.issuer_id)).first()
    if issuer is not None and company is None:
        company = session.query(Company).filter_by(issuer_id=int(issuer.id)).order_by(Company.id.asc()).first()
    if issuer is None:
        return None

    display_name = str(getattr(company, "name", None) or getattr(issuer, "display_name", None) or ticker).strip() or ticker
    period_label = str(row.period_label or "").strip()
    title = f"{display_name} {period_label} Earnings Call Transcript".strip() if period_label else f"{display_name} Earnings Call Transcript"
    period = normalize_period_from_text(
        period_label or title,
        row.url,
        published_at=row.published_at,
        issuer=issuer,
        company=company,
        metadata={
            "ticker": ticker,
            "period_label": row.period_label,
            "fiscal_year": row.fiscal_year,
            "fiscal_quarter": row.fiscal_quarter,
        },
        annual_as_q4=True,
    )
    fiscal_year = period.fiscal_year if period.fiscal_year is not None else row.fiscal_year
    fiscal_quarter = period.fiscal_quarter if period.fiscal_quarter is not None else row.fiscal_quarter
    artifact = session.query(IRArtifact).filter_by(issuer_id=int(issuer.id), artifact_type="transcript", canonical_url=row.url).first()
    if artifact is None:
        artifact = IRArtifact(
            issuer_id=int(issuer.id),
            document_id=None,
            artifact_type="transcript",
            artifact_subtype="earnings",
            published_at=row.published_at,
            event_date=(row.published_at.date() if row.published_at else None),
            fiscal_year=fiscal_year,
            fiscal_quarter=fiscal_quarter,
            title=title,
            description=None,
            canonical_url=row.url,
            download_url=row.url,
            webcast_url=None,
            audio_url=None,
            is_derived_transcript=(row.source_kind != "ir_artifact"),
            ical_uid=None,
            language="en",
            raw_meta=_merge_period_meta(
                {
                    "source_kind": row.source_kind,
                    "source_id": row.source_id,
                    "source_period_label": row.period_label,
                    "source_fiscal_year": row.fiscal_year,
                    "source_fiscal_quarter": row.fiscal_quarter,
                },
                period,
            ),
            created_at=utcnow(),
        )
        session.add(artifact)
        session.flush()
    else:
        artifact.published_at = row.published_at or artifact.published_at
        artifact.event_date = artifact.event_date or (row.published_at.date() if row.published_at else None)
        artifact.fiscal_year = fiscal_year if fiscal_year is not None else artifact.fiscal_year
        artifact.fiscal_quarter = fiscal_quarter if fiscal_quarter is not None else artifact.fiscal_quarter
        artifact.title = title or artifact.title
        artifact.download_url = artifact.download_url or row.url
        meta = dict(artifact.raw_meta or {})
        meta["source_kind"] = row.source_kind
        meta["source_id"] = row.source_id
        meta["source_period_label"] = row.period_label
        meta["source_fiscal_year"] = row.fiscal_year
        meta["source_fiscal_quarter"] = row.fiscal_quarter
        artifact.raw_meta = _merge_period_meta(meta, period)
        session.flush()

    transcript_doc = _ensure_transcript_document(session, row=row, company=company, artifact=artifact)
    if transcript_doc is not None and artifact.document_id is None:
        artifact.document_id = int(transcript_doc.id)
        session.flush()

    if fiscal_year is not None:
        event = _ensure_earnings_event(session, issuer=issuer, company=company, company_doc=None, document=None, artifact=artifact)
        if event is not None:
            event.actual_call_at = row.published_at or event.actual_call_at
            event.actual_release_at = event.actual_release_at or row.published_at
            event.transcript_artifact_id = int(artifact.id)
            event.status = "completed"
            event.updated_at = utcnow()
            session.flush()

    return artifact


def _transcript_source_name(source_kind: str | None) -> str:
    raw = str(source_kind or "").strip().lower()
    if raw.startswith("motley_fool"):
        return "motley_fool_transcripts"
    if raw.startswith("kioxia_ir"):
        return "kioxia_ir_call_material_links"
    if raw.startswith("tsmc_investor"):
        return "tsmc_investor_transcripts"
    if raw == "ir_artifact":
        return "issuer_ir_transcripts"
    return "earnings_transcript_links"


def _ensure_transcript_source(session: Session, *, source_kind: str | None) -> Source:
    name = _transcript_source_name(source_kind)
    source = session.query(Source).filter_by(name=name).first()
    if source is not None:
        return source
    source = Source(
        name=name,
        category="transcripts",
        priority=2,
        polling_interval_seconds=None,
        config={"connector": "earnings_transcript_link", "source_kind": source_kind},
        created_at=utcnow(),
    )
    session.add(source)
    session.flush()
    return source


def _metadata_transcript_text(*, title: str, row: TranscriptRecord) -> str:
    return "\n".join(
        [
            title,
            f"Ticker: {row.ticker}",
            f"Period: {row.period_label or 'Latest'}",
            f"Transcript URL: {row.url}",
            f"Source: {row.source_kind}",
        ]
    )


def _should_fetch_transcript_content(row: TranscriptRecord) -> bool:
    source_kind = str(row.source_kind or "").strip().lower()
    url = str(row.url or "").strip().lower()
    if not url or "example.com" in url:
        return False
    if "fool.com/earnings/call-transcripts/" in url:
        return True
    if source_kind in {"stockanalysis_live", "tsmc_investor_transcript", "kioxia_ir_call_material", "issuer_official_call_material_page"}:
        return True
    return False


def _fetch_transcript_content(row: TranscriptRecord) -> tuple[str | None, dict[str, Any]]:
    if not _should_fetch_transcript_content(row):
        return None, {"transcript_content_fetch": "skipped"}
    try:
        fetched = fetch_with_fallback(row.url, timeout_s=45.0)
        payload = build_fetched_document_payload(request_url=row.url, result=fetched)
    except Exception as exc:
        return None, {"transcript_content_fetch": "error", "transcript_content_error": f"{type(exc).__name__}: {exc}"[:500]}
    text = str(payload.get("normalized_text") or "").strip()
    extra = dict(payload.get("extra") or {})
    extra["transcript_content_length"] = len(text)
    if text and len(text) < 500:
        extra["transcript_content_fetch"] = "too_short"
        return None, extra
    extra["transcript_content_fetch"] = "fetched" if text else "empty"
    if payload.get("final_url"):
        extra["resolved_url"] = payload.get("final_url")
    return (text or None), extra


def _ensure_transcript_document(
    session: Session,
    *,
    row: TranscriptRecord,
    company: Company | None,
    artifact: IRArtifact,
) -> Document | None:
    source = _ensure_transcript_source(session, source_kind=row.source_kind)
    title = artifact.title or f"{row.ticker} Earnings Call Transcript"
    existing = (
        session.query(Document)
        .filter(Document.source_id == int(source.id))
        .filter(Document.canonical_url == row.url)
        .first()
    )
    now = utcnow()
    extra = {
        "connector": "earnings_transcript_link",
        "source_kind": row.source_kind,
        "source_id": row.source_id,
        "ticker": row.ticker,
        "company_name": company.name if company is not None else row.company_or_issuer,
        "fiscal_year": artifact.fiscal_year,
        "fiscal_quarter": artifact.fiscal_quarter,
        "period_label": row.period_label,
        "source_fiscal_year": row.fiscal_year,
        "source_fiscal_quarter": row.fiscal_quarter,
        "artifact_id": int(artifact.id),
        "transcript_url": row.url,
    }
    period_payload = (artifact.raw_meta or {}).get("period_normalization") if isinstance(artifact.raw_meta, dict) else None
    if isinstance(period_payload, dict):
        extra["period_normalization"] = period_payload
    if existing is None:
        fetched_text, fetch_extra = _fetch_transcript_content(row)
        text = fetched_text or _metadata_transcript_text(title=title, row=row)
        content_hash = hashlib.sha256(f"{row.url}|{title}|{text}".encode("utf-8")).hexdigest()
        extra.update(fetch_extra)
        existing = Document(
            kind="earnings_transcript",
            source_id=int(source.id),
            external_id=row.source_id or f"transcript:{content_hash[:16]}",
            canonical_url=row.url,
            url=row.url,
            title=title,
            author=company.name if company is not None else row.company_or_issuer,
            published_at=row.published_at,
            ingested_at=now,
            raw_text=text,
            normalized_text=text,
            language="en",
            language_confidence=1.0,
            translation_en=None,
            translation_backend=None,
            translation_model=None,
            translation_glossary_version=None,
            translated_at=None,
            triage_state="NEW",
            saved=False,
            seen_at=None,
            opened_at=None,
            saved_at=None,
            actioned_at=None,
            ignored_at=None,
            triage_updated_at=now,
            content_hash=content_hash,
            extra=extra,
            importance=2,
            storage_tier="hot",
            cold_archive_id=None,
        )
        session.add(existing)
        session.flush()
    else:
        fetched_text, fetch_extra = _fetch_transcript_content(row)
        metadata_text = _metadata_transcript_text(title=title, row=row)
        current_text = existing.raw_text or existing.normalized_text or ""
        text = fetched_text or (current_text if len(current_text) > len(metadata_text) else metadata_text)
        content_hash = hashlib.sha256(f"{row.url}|{title}|{text}".encode("utf-8")).hexdigest()
        existing.title = title or existing.title
        existing.author = existing.author or (company.name if company is not None else row.company_or_issuer)
        existing.published_at = row.published_at or existing.published_at
        existing.url = row.url or existing.url
        existing.canonical_url = row.url or existing.canonical_url
        existing.raw_text = text
        existing.normalized_text = text
        existing.content_hash = content_hash
        merged_extra = dict(existing.extra or {})
        merged_extra.update(extra)
        merged_extra.update(fetch_extra)
        existing.extra = merged_extra
        session.flush()

    if company is not None:
        link = (
            session.query(CompanyDocument)
            .filter_by(company_id=int(company.id), document_id=int(existing.id), doc_type="earnings_transcript")
            .first()
        )
        if link is None:
            session.add(
                CompanyDocument(
                    company_id=int(company.id),
                    document_id=int(existing.id),
                    doc_type="earnings_transcript",
                    label=title[:255],
                    created_at=now,
                )
            )
            session.flush()
    artifact.document_id = int(existing.id)
    session.flush()
    try:
        sync_document_to_institutional(session, existing)
    except Exception:
        pass
    return existing


def _transcript_record_from_document(doc: Document) -> TranscriptRecord | None:
    extra = doc.extra if isinstance(doc.extra, dict) else {}
    url = str(extra.get("transcript_url") or doc.canonical_url or doc.url or "").strip()
    if not url:
        return None
    source_kind = str(extra.get("source_kind") or "").strip()
    if not source_kind or source_kind == "ir_artifact":
        lower_url = url.lower()
        if "stockanalysis.com/" in lower_url:
            source_kind = "stockanalysis_live"
        elif "fool.com/earnings/call-transcripts/" in lower_url:
            source_kind = "motley_fool_live"
        elif "tsmc" in lower_url and "transcript" in lower_url:
            source_kind = "tsmc_investor_transcript"
        else:
            source_kind = "document_fallback"
    fiscal_year = extra.get("fiscal_year")
    fiscal_quarter = extra.get("fiscal_quarter")
    if fiscal_year is None or fiscal_quarter is None:
        parsed_year, parsed_quarter = _parse_period_from_text(doc.title, url)
        fiscal_year = fiscal_year or parsed_year
        fiscal_quarter = fiscal_quarter or parsed_quarter
    try:
        fiscal_year_v = int(fiscal_year) if fiscal_year is not None else None
    except Exception:
        fiscal_year_v = None
    try:
        fiscal_quarter_v = int(fiscal_quarter) if fiscal_quarter is not None else None
    except Exception:
        fiscal_quarter_v = None
    return TranscriptRecord(
        ticker=str(extra.get("ticker") or "").strip().upper(),
        company_or_issuer=str(extra.get("company_name") or doc.author or "").strip(),
        period_label=str(extra.get("period_label") or "").strip() or None,
        fiscal_year=fiscal_year_v,
        fiscal_quarter=fiscal_quarter_v,
        published_at=doc.published_at,
        url=url,
        source_kind=source_kind,
        source_id=str(extra.get("source_id") or f"document:{int(doc.id)}"),
    )


def maybe_hydrate_transcript_document(session: Session, doc: Document) -> dict[str, Any]:
    if str(doc.kind or "").strip().lower() not in {"earnings_transcript", "transcript"}:
        return {"attempted": False, "updated": False, "reason": "not_transcript"}
    extra = dict(doc.extra or {}) if isinstance(doc.extra, dict) else {}
    if str(extra.get("connector") or "").strip() != "earnings_transcript_link":
        return {"attempted": False, "updated": False, "reason": "not_transcript_link"}
    if document_is_readable_transcript(doc):
        return {"attempted": False, "updated": False, "reason": "already_readable"}
    row = _transcript_record_from_document(doc)
    if row is None:
        return {"attempted": False, "updated": False, "reason": "missing_url"}
    fetched_text, fetch_extra = _fetch_transcript_content(row)
    merged_extra = dict(extra)
    merged_extra.update(fetch_extra)
    merged_extra["transcript_hydrated_at"] = utcnow().isoformat()
    doc.extra = merged_extra
    if not fetched_text:
        session.flush()
        return {
            "attempted": True,
            "updated": False,
            "reason": str(merged_extra.get("transcript_content_fetch") or "not_fetched"),
            "text_len": len(str(doc.normalized_text or doc.raw_text or "").strip()),
        }
    title = doc.title or f"{row.ticker} Earnings Call Transcript"
    doc.raw_text = fetched_text
    doc.normalized_text = fetched_text
    doc.content_hash = hashlib.sha256(f"{row.url}|{title}|{fetched_text}".encode("utf-8")).hexdigest()
    doc.ingested_at = utcnow()
    session.flush()
    try:
        sync_document_to_institutional(session, doc)
    except Exception:
        pass
    return {
        "attempted": True,
        "updated": True,
        "reason": "fetched",
        "text_len": len(fetched_text),
    }


def bootstrap_watchlist_transcripts(session: Session, *, limit: int | None = None) -> dict[str, int]:
    from thoth_terminal.discord.models import DiscordWatchlistEntry

    local_tickers = {
        normalize_ticker(row[0])
        for row in session.query(Company.ticker).filter(Company.ticker.isnot(None)).all()
        if normalize_ticker(row[0])
    }
    symbols = sorted(
        {
            normalize_ticker(row[0])
            for row in session.query(DiscordWatchlistEntry.symbol)
            .filter(DiscordWatchlistEntry.removed_at.is_(None))
            .all()
            if normalize_ticker(row[0]) in local_tickers
        }
    )
    if limit is not None:
        symbols = symbols[: max(1, int(limit))]

    created = 0
    reused = 0
    for symbol in symbols:
        before = session.query(IRArtifact.id).filter(IRArtifact.artifact_type == "transcript").count()
        row = get_transcript(session, ticker=symbol, most_recent=True, allow_live_fallback=True)
        if row is None:
            continue
        artifact = upsert_transcript_record(session, row=row)
        if artifact is None:
            continue
        after = session.query(IRArtifact.id).filter(IRArtifact.artifact_type == "transcript").count()
        if after > before:
            created += 1
        else:
            reused += 1
    return {"symbols": len(symbols), "created": created, "reused": reused}


def bootstrap_research_base(session: Session, *, build_reports: bool = True, build_knowledge: bool = True, limit: int | None = None) -> dict[str, int]:
    transcript_stats = bootstrap_watchlist_transcripts(session, limit=limit)
    stats = {
        "subjects": 0,
        "reports_built": 0,
        "reports_reused": 0,
        "knowledge_seeded": 0,
        "transcripts_seeded": int(transcript_stats.get("created", 0)),
        "transcripts_reused": int(transcript_stats.get("reused", 0)),
    }
    candidates = (
        session.query(Company, func.count(CompanyDocument.id).label("doc_count"))
        .outerjoin(CompanyDocument, CompanyDocument.company_id == Company.id)
        .group_by(Company.id)
        .having(func.count(CompanyDocument.id) > 0)
        .order_by(func.count(CompanyDocument.id).desc(), Company.name.asc())
    )
    if limit is not None:
        candidates = candidates.limit(max(1, int(limit)))
    rows = candidates.all()
    stats["subjects"] = len(rows)
    for company, _doc_count in rows:
        if getattr(company, "issuer_id", None) is None:
            continue
        subject_key = f"issuer:{int(company.issuer_id)}"
        subject_ref = normalize_ticker(getattr(company, "ticker", None)) or str(getattr(company, "name", "") or "").strip()
        if not subject_ref:
            continue
        if build_reports:
            result = build_research_report(
                session,
                subject=subject_ref,
                trigger_kind="bootstrap",
                trigger_label="seed",
                force=True,
            )
            if result.created_version:
                stats["reports_built"] += 1
            else:
                stats["reports_reused"] += 1
        if build_knowledge:
            subject_row = session.query(ResearchSubject).filter_by(subject_key=subject_key).first()
            if subject_row is None:
                continue
            existing = session.query(KnowledgeAsset).filter(KnowledgeAsset.subject_id == int(subject_row.id), KnowledgeAsset.kind == "primer").first()
            if existing is None:
                generate_knowledge_asset_draft(session, subject=subject_key, kind="primer", window_days=730)
                stats["knowledge_seeded"] += 1
    session.flush()
    return stats
