from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, selectinload

from thoth_terminal.agent.llm_client import AgentLlmError, ChatMessage, llm_available
from thoth_terminal.agent_request_manifest_service import manifested_chat_json
from thoth_terminal.config import settings
from thoth_terminal.db import utcnow
from thoth_terminal.discord.models import DiscordEvidenceItem, DiscordEvidenceTarget
from thoth_terminal.instrument_service import create_instrument_request
from thoth_terminal.models import (
    AnalystEvidence,
    BottleneckAssessment,
    BottleneckModel,
    BottleneckModelLink,
    Company,
    CompanyDocument,
    CoverageGap,
    Dataset,
    DatasetColumn,
    DatasetLink,
    DatasetRow,
    DatasetVersion,
    Document,
    DocumentEntity,
    EarningsEvent,
    Entity,
    EntityAlias,
    ExchangeDisclosure,
    IRArtifact,
    InstrumentRequest,
    InstitutionalEvidenceItem,
    InstitutionalObservation,
    Issuer,
    IssuerIdentifier,
    Note,
    ProductStageEntry,
    ResearchSubject,
    ResearchSubjectAlias,
    ResearchSubjectRelation,
    ResearchReport,
    ResearchReportArtifact,
    ResearchReportVersion,
    SecuritiesFiling,
    SourceActor,
    StructuredDataFact,
    SupplyChain,
    SupplyChainLink,
    SupplyChainRelation,
    Thesis,
    TranscriptSummary,
    KnowledgeAsset,
    KnowledgeAssetVersion,
    note_entities,
    thesis_entities,
)
from thoth_terminal.object_store import put_bytes
from thoth_terminal.structured_data_service import company_structured_fact_scope_ids, extract_earnings_metric_rows
from thoth_terminal.supply_chain_service import latest_chain_snapshot, serialize_supply_chain
from thoth_terminal.text import clean_whitespace, extract_text_from_pdf
from thoth_terminal.url_utils import clean_url


REPORT_STALE_AFTER = timedelta(days=7)
REPORT_DEFAULT_WINDOW_DAYS = 730
MENTION_CONFIDENCE_MIN = 0.65
DOCUMENT_ENTITY_CONFIDENCE_MIN = 0.75
_REVENUE_SEGMENT_CONTEXT = (
    "segment revenue",
    "business revenue",
    "client revenue",
    "gaming revenue",
    "embedded segment",
    "data center segment",
)
_REVENUE_NON_TOPLINE_CONTEXT = (
    "mid-point",
    "midpoint",
    "year-over-year growth",
    "sequential growth",
    "shipments to china",
    "mi308",
)
SECTION_ORDER = [
    "identity_freshness",
    "canon_scenario",
    "agent_canon_card",
    "published_primers",
    "published_teardowns",
    "thesis_ledger",
    "note_ledger",
    "subject_facts",
    "transcript_archive_summaries",
    "supply_chain_peer_context",
    "linked_datasets_stage_trackers",
    "high_confidence_mention_appendix",
    "artifacts_export_appendix",
    "coverage_panel",
    "official_source_pack",
    "primary_source_timeline",
]
_SUMMARY_KEYWORDS = (
    "revenue",
    "margin",
    "guidance",
    "outlook",
    "demand",
    "orders",
    "inventory",
    "capacity",
    "yield",
    "utilization",
    "pricing",
    "gross margin",
    "bookings",
    "backlog",
)
REPORT_MEMORY_DECISIONS = {"accept", "addendum", "quarantine"}
REPORT_TYPES = (
    "company_report",
    "supply_chain_report",
    "technology_deep_dive",
    "materials_report",
    "cost_model_report",
    "market_structure_report",
    "thesis_support_report",
    "bottleneck_report",
    "imported_report",
)
REPORT_TYPE_LABELS = {
    "company_report": "Company report",
    "supply_chain_report": "Supply chain report",
    "technology_deep_dive": "Technology deep dive",
    "materials_report": "Materials report",
    "cost_model_report": "Cost model report",
    "market_structure_report": "Market structure report",
    "thesis_support_report": "Thesis support report",
    "bottleneck_report": "Bottleneck report",
    "imported_report": "Imported report",
}
REPORT_KNOWLEDGE_ASSET_EXCERPT_CHARS = 1200
REPORT_TRANSCRIPT_SUMMARY_EXCERPT_CHARS = 16000


@dataclass(frozen=True)
class ReportSubject:
    subject_row: ResearchSubject | None
    issuer: Issuer | None
    company: Company | None
    primary_entity: Entity | None
    subject_id: int | None
    issuer_id: int | None
    company_id: int | None
    primary_entity_id: int | None
    kind: str
    slug: str
    subject_key: str
    display_name: str
    legal_name: str | None
    description: str | None
    primary_ticker: str | None
    tickers: list[str]
    aliases: list[str]


@dataclass(frozen=True)
class ResearchReportBuildResult:
    report: ResearchReport
    version: ResearchReportVersion
    created_version: bool
    reason: str
    subject: ReportSubject


def _slugify(value: str | None, *, default: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return cleaned or default


def _unique_research_subject_slug(session: Session, slug: object, *, subject_id: int | None = None) -> str:
    base = _slugify(str(slug or ""), default="subject")[:128]
    candidate = base
    suffix = 2
    while True:
        existing = session.query(ResearchSubject).filter_by(slug=candidate).first()
        if existing is None or (subject_id is not None and int(existing.id) == int(subject_id)):
            return candidate
        suffix_text = f"-{suffix}"
        candidate = f"{base[: max(1, 128 - len(suffix_text))]}{suffix_text}"
        suffix += 1


def _clean_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _json_default(value: object) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if value is not None else None


def _dt_iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def normalize_report_type(value: object | None) -> str:
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return text if text in REPORT_TYPES else "imported_report"


def report_type_label(value: object | None) -> str:
    return REPORT_TYPE_LABELS.get(normalize_report_type(value), REPORT_TYPE_LABELS["imported_report"])


def _default_report_type_for_subject(kind: object | None) -> str:
    subject_kind = str(kind or "").strip().lower()
    if subject_kind in {"company", "issuer", "ticker"}:
        return "company_report"
    if subject_kind in {"technology", "component", "product", "process"}:
        return "technology_deep_dive"
    if subject_kind in {"supply_chain", "chain"}:
        return "supply_chain_report"
    return "market_structure_report"


def _split_metadata_values(value: object | None) -> list[str]:
    if value is None:
        return []
    raw_items: list[object]
    if isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raw_items = re.split(r"[,;\n]+", str(value))
    out: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        cleaned = _clean_text(item)
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
    return out


def _normalize_linked_objects(value: object | None) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {
        "companies": [],
        "supply_chains": [],
        "materials": [],
        "technologies": [],
        "theses": [],
        "notes": [],
        "datasets": [],
        "knowledge_assets": [],
        "bottleneck_models": [],
    }
    if isinstance(value, dict):
        for key in list(out):
            out[key] = _split_metadata_values(value.get(key))
        return {key: vals for key, vals in out.items() if vals}
    if isinstance(value, (list, tuple, set)):
        for item in value:
            text = _clean_text(item)
            if not text:
                continue
            if ":" in text:
                kind, ident = text.split(":", 1)
                bucket = kind.strip().lower().replace("-", "_")
                if bucket in out and ident.strip():
                    out[bucket].append(ident.strip())
                    continue
            out.setdefault("knowledge_assets", []).append(text)
    return {key: _split_metadata_values(vals) for key, vals in out.items() if vals}


REPORT_VERSION_INDEX_EXCERPT_CHARS = 6000


def _version_content_chars(version: Any | None) -> int:
    if version is None:
        return 0
    projected = getattr(version, "content_chars", None)
    if projected is not None:
        try:
            return int(projected or 0)
        except (TypeError, ValueError):
            return 0
    return len(str(getattr(version, "content_md", "") or "").strip())


def _version_content_text(version: Any | None) -> str:
    if version is None:
        return ""
    projected = getattr(version, "content_md_excerpt", None)
    if projected is not None:
        return str(projected or "")
    return str(getattr(version, "content_md", "") or "")


def _manual_addenda_for_version(version: Any | None) -> list[dict[str, Any]]:
    if version is None or not isinstance(version.content_json, dict):
        return []
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in version.content_json.get("manual_addenda") or []:
        if not isinstance(raw, dict):
            continue
        addendum_id = str(raw.get("id") or raw.get("addendum_id") or raw.get("artifact_id") or "").strip()
        if not addendum_id:
            addendum_id = _stable_hash(raw)[:16]
        if addendum_id in seen:
            continue
        seen.add(addendum_id)
        body_md = str(raw.get("body_md") or "").strip()
        rows.append(
            {
                "id": addendum_id,
                "title": _clean_text(raw.get("title")) or "Manual addendum",
                "summary": _clean_text(raw.get("summary")) or None,
                "body_md": body_md,
                "body_preview": _clean_text(raw.get("body_preview") or body_md)[:500],
                "tags": _split_metadata_values(raw.get("tags")),
                "actor": _clean_text(raw.get("actor")) or None,
                "created_at": _clean_text(raw.get("created_at")) or None,
                "artifact_id": int(raw["artifact_id"]) if str(raw.get("artifact_id") or "").isdigit() else None,
                "reader_url": _clean_text(raw.get("reader_url")) or None,
                "raw_url": _clean_text(raw.get("raw_url")) or None,
            }
        )
    return rows


def _report_metadata(report: ResearchReport, version: Any | None, subject: ReportSubject | None) -> dict[str, Any]:
    extra = dict(report.extra or {})
    content = dict(version.content_json or {}) if version is not None and isinstance(version.content_json, dict) else {}
    raw_type = extra.get("report_type") or content.get("report_type") or _default_report_type_for_subject(subject.kind if subject else extra.get("subject_kind"))
    report_type = normalize_report_type(raw_type)
    summary = _clean_text(extra.get("summary") or extra.get("description") or content.get("summary") or content.get("description"))
    if not summary and version is not None:
        summary = _clean_text(_version_content_text(version))[:700]
    linked_objects = _normalize_linked_objects(extra.get("linked_objects") or content.get("linked_objects"))
    return {
        "report_type": report_type,
        "report_type_label": report_type_label(report_type),
        "summary": summary or None,
        "description": summary or None,
        "tags": _split_metadata_values(extra.get("tags") or content.get("tags")),
        "topics": _split_metadata_values(extra.get("topics") or content.get("topics")),
        "companies": linked_objects.get("companies") or _split_metadata_values(extra.get("companies") or content.get("companies")),
        "supply_chains": linked_objects.get("supply_chains") or _split_metadata_values(extra.get("supply_chains") or content.get("supply_chains")),
        "materials": linked_objects.get("materials") or _split_metadata_values(extra.get("materials") or content.get("materials")),
        "technologies": linked_objects.get("technologies") or _split_metadata_values(extra.get("technologies") or content.get("technologies")),
        "linked_objects": linked_objects,
        "source_metadata": dict(extra.get("source_metadata") or content.get("source_metadata") or {}),
        "upload_provenance": dict(extra.get("upload_provenance") or content.get("upload_provenance") or {}),
        "imported": bool(extra.get("imported_report") or content.get("imported_report")),
        "manual_addenda": _manual_addenda_for_version(version),
    }


def _report_freshness(version: ResearchReportVersion | None) -> dict[str, Any]:
    if version is None:
        return {"state": "unpublished", "age_days": None, "is_stale": False}
    anchor = version.published_at or version.as_of_at or version.updated_at or version.created_at
    age_days = None
    is_stale = False
    if anchor is not None:
        delta = utcnow() - anchor
        age_days = max(0, int(delta.total_seconds() // 86400))
        is_stale = bool(delta >= REPORT_STALE_AFTER)
    return {"state": "stale" if is_stale else "fresh", "age_days": age_days, "is_stale": is_stale}


def _date_like(value: object) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            return str(value)
    return str(value)


def _stable_hash(payload: object) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=_json_default)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _window_days(value: int | None) -> int:
    parsed = int(value or REPORT_DEFAULT_WINDOW_DAYS)
    return max(30, min(parsed, 3650))


def _window_cutoff(value: int | None) -> datetime:
    return utcnow() - timedelta(days=_window_days(value))


def _primary_tickers(session: Session, issuer_id: int | None, company: Company | None = None) -> list[str]:
    tickers: list[str] = []
    if issuer_id is not None:
        rows = (
            session.query(IssuerIdentifier)
            .filter(IssuerIdentifier.issuer_id == int(issuer_id), IssuerIdentifier.id_type == "TICKER")
            .order_by(IssuerIdentifier.is_primary.desc(), IssuerIdentifier.id.asc())
            .all()
        )
        for row in rows:
            ticker = str(row.id_value or "").strip().upper()
            if ticker and ticker not in tickers:
                tickers.append(ticker)
    if company and company.ticker:
        ticker = str(company.ticker).strip().upper()
        if ticker and ticker not in tickers:
            tickers.append(ticker)
    return tickers


def _bridge_company_for_issuer(session: Session, issuer: Issuer, *, preferred_ticker: str | None = None) -> Company | None:
    rows = (
        session.query(Company)
        .filter(Company.issuer_id == int(issuer.id))
        .order_by(Company.id.asc())
        .all()
    )
    if not rows:
        return None
    pref = str(preferred_ticker or "").strip().upper()
    if pref:
        for row in rows:
            if str(row.ticker or "").strip().upper() == pref:
                return row
    return rows[0]


def _subject_kind_from_entity(entity: Entity | None) -> str:
    kind = str(getattr(entity, "kind", "") or "").strip().lower()
    if kind in {"technology", "process"}:
        return "technology"
    if kind in {"component", "product"}:
        return "component"
    if kind in {"company", "issuer", "ticker"}:
        return "company"
    return "theme"


def _subject_from_rows(
    session: Session,
    *,
    subject_row: ResearchSubject | None = None,
    issuer: Issuer | None = None,
    company: Company | None = None,
    primary_entity: Entity | None = None,
) -> ReportSubject:
    row = subject_row
    issuer_row = issuer or (session.query(Issuer).filter_by(id=int(row.issuer_id)).first() if row and row.issuer_id is not None else None)
    company_row = company or (session.query(Company).filter_by(id=int(row.company_id)).first() if row and row.company_id is not None else None)
    entity_row = primary_entity or (
        session.query(Entity).filter_by(id=int(row.primary_entity_id)).first()
        if row and row.primary_entity_id is not None
        else None
    )
    if issuer_row is None and company_row is not None and company_row.issuer_id is not None:
        issuer_row = session.query(Issuer).filter_by(id=int(company_row.issuer_id)).first()
    if company_row is None and issuer_row is not None:
        preferred_ticker = None
        if row is not None:
            extra = dict(row.extra or {})
            preferred_ticker = str(extra.get("preferred_ticker") or "").strip() or None
        company_row = _bridge_company_for_issuer(session, issuer_row, preferred_ticker=preferred_ticker)

    tickers = _primary_tickers(session, int(issuer_row.id) if issuer_row else None, company=company_row)
    if row is not None:
        extra_tickers = [str(v).strip().upper() for v in (dict(row.extra or {}).get("tickers") or []) if str(v).strip()]
        for ticker in extra_tickers:
            if ticker not in tickers:
                tickers.append(ticker)
    primary_ticker = tickers[0] if tickers else None
    display_name = (
        str((row.display_name if row else "") or "").strip()
        or str((issuer_row.display_name if issuer_row else "") or "").strip()
        or str((company_row.name if company_row else "") or "").strip()
        or str((entity_row.canonical_name if entity_row else "") or "").strip()
        or (primary_ticker or "Unknown")
    )
    legal_name = (
        str((row.legal_name if row else "") or "").strip()
        or str((issuer_row.legal_name if issuer_row else "") or "").strip()
        or (str((company_row.name if company_row else "") or "").strip() or None)
    )
    legacy_subject_key = str((row.subject_key if row else "") or "").strip() or None
    subject_key = _canonical_report_subject_key(
        issuer_id=int(issuer_row.id) if issuer_row is not None else None,
        company_id=int(company_row.id) if company_row is not None else None,
        entity_id=int(entity_row.id) if entity_row is not None else None,
        fallback=legacy_subject_key,
    )
    row_slug = str((row.slug if row else "") or "").strip()
    slug = (
        (row_slug if row_slug and legacy_subject_key == subject_key else "")
        or str((issuer_row.slug if issuer_row else "") or "").strip()
        or str((company_row.slug if company_row else "") or "").strip()
        or row_slug
        or _slugify(display_name, default=f"report-{primary_ticker or 'subject'}")
    )
    alias_values = {
        display_name,
        legal_name or "",
        slug,
        str((company_row.slug if company_row else "") or ""),
        str((company_row.name if company_row else "") or ""),
        str((entity_row.canonical_name if entity_row else "") or ""),
    }
    if row is not None:
        alias_values.add(str(row.subject_key or ""))
        alias_values.update(str(alias.alias or "") for alias in (row.aliases or []))
    alias_values.update(tickers)
    alias_values.update({"$" + ticker for ticker in tickers if ticker})
    aliases = sorted({val for val in (_clean_text(v) for v in alias_values) if val}, key=str.lower)
    kind = str((row.kind if row else "") or "").strip().lower() or (
        "company"
        if issuer_row is not None or company_row is not None
        else _subject_kind_from_entity(entity_row)
    )
    description = (
        str((row.description if row else "") or "").strip()
        or str((entity_row.description if entity_row else "") or "").strip()
        or None
    )
    return ReportSubject(
        subject_row=row,
        issuer=issuer_row,
        company=company_row,
        primary_entity=entity_row,
        subject_id=int(row.id) if row is not None and row.id is not None else None,
        issuer_id=int(issuer_row.id) if issuer_row is not None else None,
        company_id=int(company_row.id) if company_row is not None else None,
        primary_entity_id=int(entity_row.id) if entity_row is not None else None,
        kind=kind,
        slug=slug,
        subject_key=subject_key,
        display_name=display_name,
        legal_name=legal_name,
        description=description,
        primary_ticker=primary_ticker,
        tickers=tickers,
        aliases=aliases,
    )


def _subject_from_prefetched_rows(
    *,
    subject_row: ResearchSubject | None = None,
    issuer: Issuer | None = None,
    company: Company | None = None,
    primary_entity: Entity | None = None,
    tickers_by_issuer_id: dict[int, list[str]] | None = None,
) -> ReportSubject:
    row = subject_row
    issuer_row = issuer
    company_row = company
    entity_row = primary_entity
    ticker_map = tickers_by_issuer_id or {}
    tickers = list(ticker_map.get(int(issuer_row.id), [])) if issuer_row is not None else []
    if company_row is not None and company_row.ticker:
        ticker = str(company_row.ticker).strip().upper()
        if ticker and ticker not in tickers:
            tickers.append(ticker)
    if row is not None:
        extra_tickers = [str(v).strip().upper() for v in (dict(row.extra or {}).get("tickers") or []) if str(v).strip()]
        for ticker in extra_tickers:
            if ticker not in tickers:
                tickers.append(ticker)
    primary_ticker = tickers[0] if tickers else None
    display_name = (
        str((row.display_name if row else "") or "").strip()
        or str((issuer_row.display_name if issuer_row else "") or "").strip()
        or str((company_row.name if company_row else "") or "").strip()
        or str((entity_row.canonical_name if entity_row else "") or "").strip()
        or (primary_ticker or "Unknown")
    )
    legal_name = (
        str((row.legal_name if row else "") or "").strip()
        or str((issuer_row.legal_name if issuer_row else "") or "").strip()
        or (str((company_row.name if company_row else "") or "").strip() or None)
    )
    legacy_subject_key = str((row.subject_key if row else "") or "").strip() or None
    subject_key = _canonical_report_subject_key(
        issuer_id=int(issuer_row.id) if issuer_row is not None else None,
        company_id=int(company_row.id) if company_row is not None else None,
        entity_id=int(entity_row.id) if entity_row is not None else None,
        fallback=legacy_subject_key,
    )
    row_slug = str((row.slug if row else "") or "").strip()
    slug = (
        (row_slug if row_slug and legacy_subject_key == subject_key else "")
        or str((issuer_row.slug if issuer_row else "") or "").strip()
        or str((company_row.slug if company_row else "") or "").strip()
        or row_slug
        or _slugify(display_name, default=f"report-{primary_ticker or 'subject'}")
    )
    alias_values = {
        display_name,
        legal_name or "",
        slug,
        str((company_row.slug if company_row else "") or ""),
        str((company_row.name if company_row else "") or ""),
        str((entity_row.canonical_name if entity_row else "") or ""),
    }
    if row is not None:
        alias_values.add(str(row.subject_key or ""))
        alias_values.update(str(alias.alias or "") for alias in (row.aliases or []))
    alias_values.update(tickers)
    alias_values.update({"$" + ticker for ticker in tickers if ticker})
    aliases = sorted({val for val in (_clean_text(v) for v in alias_values) if val}, key=str.lower)
    kind = str((row.kind if row else "") or "").strip().lower() or (
        "company"
        if issuer_row is not None or company_row is not None
        else _subject_kind_from_entity(entity_row)
    )
    description = (
        str((row.description if row else "") or "").strip()
        or str((entity_row.description if entity_row else "") or "").strip()
        or None
    )
    return ReportSubject(
        subject_row=row,
        issuer=issuer_row,
        company=company_row,
        primary_entity=entity_row,
        subject_id=int(row.id) if row is not None and row.id is not None else None,
        issuer_id=int(issuer_row.id) if issuer_row is not None else None,
        company_id=int(company_row.id) if company_row is not None else None,
        primary_entity_id=int(entity_row.id) if entity_row is not None else None,
        kind=kind,
        slug=slug,
        subject_key=subject_key,
        display_name=display_name,
        legal_name=legal_name,
        description=description,
        primary_ticker=primary_ticker,
        tickers=tickers,
        aliases=aliases,
    )


def _latest_published_versions_by_report_id(
    session: Session,
    reports: list[ResearchReport],
) -> dict[int, Any]:
    report_ids = sorted({int(report.id) for report in reports if report.id is not None})
    if not report_ids:
        return {}
    out: dict[int, Any] = {}
    version_columns = [
        ResearchReportVersion.id,
        ResearchReportVersion.report_id,
        ResearchReportVersion.version,
        ResearchReportVersion.status,
        ResearchReportVersion.trigger_kind,
        ResearchReportVersion.trigger_label,
        ResearchReportVersion.change_fingerprint,
        ResearchReportVersion.as_of_at,
        ResearchReportVersion.created_at,
        ResearchReportVersion.updated_at,
        ResearchReportVersion.published_at,
        ResearchReportVersion.error,
        ResearchReportVersion.coverage_json,
        ResearchReportVersion.content_json,
        func.length(func.trim(ResearchReportVersion.content_md)).label("content_chars"),
        func.substr(ResearchReportVersion.content_md, 1, REPORT_VERSION_INDEX_EXCERPT_CHARS).label("content_md_excerpt"),
    ]
    latest_ids = sorted({int(report.latest_published_version_id) for report in reports if report.latest_published_version_id is not None})
    if latest_ids:
        for row in (
            session.query(*version_columns)
            .filter(ResearchReportVersion.id.in_(latest_ids), ResearchReportVersion.status == "published")
            .all()
        ):
            out[int(row.report_id)] = row
    missing_ids = [report_id for report_id in report_ids if report_id not in out]
    if missing_ids:
        rows = (
            session.query(*version_columns)
            .filter(ResearchReportVersion.report_id.in_(missing_ids), ResearchReportVersion.status == "published")
            .order_by(ResearchReportVersion.report_id.asc(), ResearchReportVersion.version.desc(), ResearchReportVersion.id.desc())
            .all()
        )
        for row in rows:
            report_id = int(row.report_id)
            if report_id not in out:
                out[report_id] = row
    return out


def _report_subjects_by_report_id(
    session: Session,
    reports: list[ResearchReport],
) -> dict[int, ReportSubject]:
    if not reports:
        return {}
    subject_ids = {int(row.subject_id) for row in reports if row.subject_id is not None}
    subjects = {
        int(row.id): row
        for row in (
            session.query(ResearchSubject)
            .options(selectinload(ResearchSubject.aliases))
            .filter(ResearchSubject.id.in_(sorted(subject_ids)))
            .all()
            if subject_ids
            else []
        )
    }

    issuer_ids = {int(row.issuer_id) for row in reports if row.issuer_id is not None}
    company_ids = {int(row.company_id) for row in reports if row.company_id is not None}
    entity_ids: set[int] = set()
    for subject in subjects.values():
        if subject.issuer_id is not None:
            issuer_ids.add(int(subject.issuer_id))
        if subject.company_id is not None:
            company_ids.add(int(subject.company_id))
        if subject.primary_entity_id is not None:
            entity_ids.add(int(subject.primary_entity_id))

    companies = {
        int(row.id): row
        for row in session.query(Company).filter(Company.id.in_(sorted(company_ids))).all()
    } if company_ids else {}
    for company in companies.values():
        if company.issuer_id is not None:
            issuer_ids.add(int(company.issuer_id))
    issuers = {
        int(row.id): row
        for row in session.query(Issuer).filter(Issuer.id.in_(sorted(issuer_ids))).all()
    } if issuer_ids else {}
    bridge_companies_by_issuer: dict[int, list[Company]] = {}
    if issuer_ids:
        bridge_rows = (
            session.query(Company)
            .filter(Company.issuer_id.in_(sorted(issuer_ids)))
            .order_by(Company.issuer_id.asc(), Company.id.asc())
            .all()
        )
        for company in bridge_rows:
            if company.id is not None and int(company.id) not in companies:
                companies[int(company.id)] = company
            if company.issuer_id is not None:
                bridge_companies_by_issuer.setdefault(int(company.issuer_id), []).append(company)
    entities = {
        int(row.id): row
        for row in session.query(Entity).filter(Entity.id.in_(sorted(entity_ids))).all()
    } if entity_ids else {}
    tickers_by_issuer: dict[int, list[str]] = {}
    if issuer_ids:
        ticker_rows = (
            session.query(IssuerIdentifier.issuer_id, IssuerIdentifier.id_value)
            .filter(IssuerIdentifier.issuer_id.in_(sorted(issuer_ids)), IssuerIdentifier.id_type == "TICKER")
            .order_by(IssuerIdentifier.issuer_id.asc(), IssuerIdentifier.is_primary.desc(), IssuerIdentifier.id.asc())
            .all()
        )
        for issuer_id, raw_ticker in ticker_rows:
            ticker = str(raw_ticker or "").strip().upper()
            if ticker and ticker not in tickers_by_issuer.setdefault(int(issuer_id), []):
                tickers_by_issuer[int(issuer_id)].append(ticker)

    out: dict[int, ReportSubject] = {}
    for report in reports:
        subject = subjects.get(int(report.subject_id)) if report.subject_id is not None else None
        issuer = issuers.get(int(report.issuer_id)) if report.issuer_id is not None else None
        if issuer is None and subject is not None and subject.issuer_id is not None:
            issuer = issuers.get(int(subject.issuer_id))
        company = companies.get(int(report.company_id)) if report.company_id is not None else None
        if company is None and subject is not None and subject.company_id is not None:
            company = companies.get(int(subject.company_id))
        if issuer is None and company is not None and company.issuer_id is not None:
            issuer = issuers.get(int(company.issuer_id))
        if company is None and issuer is not None:
            preferred_ticker = None
            if subject is not None:
                preferred_ticker = str((dict(subject.extra or {}).get("preferred_ticker")) or "").strip().upper() or None
            bridge_rows = bridge_companies_by_issuer.get(int(issuer.id), [])
            if preferred_ticker:
                company = next((row for row in bridge_rows if str(row.ticker or "").strip().upper() == preferred_ticker), None)
            if company is None and bridge_rows:
                company = bridge_rows[0]
        entity = entities.get(int(subject.primary_entity_id)) if subject is not None and subject.primary_entity_id is not None else None
        out[int(report.id)] = _subject_from_prefetched_rows(
            subject_row=subject,
            issuer=issuer,
            company=company,
            primary_entity=entity,
            tickers_by_issuer_id=tickers_by_issuer,
        )
    return out


def _ensure_subject_row(session: Session, subject: ReportSubject) -> ResearchSubject:
    row = subject.subject_row
    now = utcnow()
    if row is not None and str(row.subject_key or "").strip() != subject.subject_key:
        canonical_row = session.query(ResearchSubject).filter_by(subject_key=subject.subject_key).first()
        row = canonical_row
    if row is None:
        row = session.query(ResearchSubject).filter_by(subject_key=subject.subject_key).first()
    if row is None:
        slug = _unique_research_subject_slug(session, subject.slug)
        row = ResearchSubject(
            slug=slug,
            subject_key=subject.subject_key,
            kind=subject.kind,
            display_name=subject.display_name,
            legal_name=subject.legal_name,
            description=subject.description,
            status="active",
            issuer_id=subject.issuer_id,
            company_id=subject.company_id,
            primary_entity_id=subject.primary_entity_id,
            extra={"tickers": list(subject.tickers)},
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.flush()
    else:
        slug = _unique_research_subject_slug(session, subject.slug, subject_id=int(row.id))
        row.subject_key = subject.subject_key
        row.slug = slug
        row.kind = subject.kind
        row.display_name = subject.display_name
        row.legal_name = subject.legal_name
        row.description = subject.description
        row.issuer_id = subject.issuer_id
        row.company_id = subject.company_id
        row.primary_entity_id = subject.primary_entity_id
        extra = dict(row.extra or {})
        extra["tickers"] = list(subject.tickers)
        row.extra = extra
        row.updated_at = now
        session.flush()
    existing_aliases = {
        str(alias.alias or "").strip(): alias
        for alias in session.query(ResearchSubjectAlias)
        .filter(ResearchSubjectAlias.subject_id == int(row.id))
        .all()
    }
    from thoth_terminal.identity_alias_service import valid_identity_aliases
    for alias_value in valid_identity_aliases(session, issuer_id=row.issuer_id, aliases=subject.aliases):
        cleaned = _clean_text(alias_value)
        if not cleaned or cleaned in existing_aliases:
            continue
        conflicting_alias = (
            session.query(ResearchSubjectAlias)
            .join(ResearchSubject, ResearchSubject.id == ResearchSubjectAlias.subject_id)
            .filter(func.lower(ResearchSubjectAlias.alias) == cleaned.lower())
            .filter(ResearchSubjectAlias.subject_id != int(row.id))
            .filter(ResearchSubject.status.notin_(["merged", "archived"]))
            .order_by(ResearchSubjectAlias.id.asc())
            .first()
        )
        if conflicting_alias is not None:
            continue
        alias_type = "ticker" if cleaned.upper() in subject.tickers or cleaned.startswith("$") else "name"
        session.add(
            ResearchSubjectAlias(
                subject_id=int(row.id),
                alias=cleaned,
                alias_type=alias_type,
                created_at=now,
            )
        )
    session.flush()
    return row


def _canonical_report_subject_key(
    *,
    issuer_id: int | None = None,
    company_id: int | None = None,
    entity_id: int | None = None,
    fallback: str | None = None,
) -> str:
    if issuer_id is not None:
        return f"issuer:{int(issuer_id)}"
    if company_id is not None:
        return f"company:{int(company_id)}"
    if entity_id is not None:
        return f"entity:{int(entity_id)}"
    return str(fallback or "").strip()


def resolve_report_subject(session: Session, subject: str | int | ResearchReport) -> ReportSubject:
    if isinstance(subject, ResearchReport):
        subject_row = session.query(ResearchSubject).filter_by(id=int(subject.subject_id)).first() if subject.subject_id is not None else None
        issuer = session.query(Issuer).filter_by(id=int(subject.issuer_id)).first() if subject.issuer_id is not None else None
        company = session.query(Company).filter_by(id=int(subject.company_id)).first() if subject.company_id is not None else None
        return _subject_from_rows(session, subject_row=subject_row, issuer=issuer, company=company)

    if isinstance(subject, int) or str(subject).isdigit():
        raw_id = int(subject)
        report = session.query(ResearchReport).filter_by(id=raw_id).first()
        if report:
            return resolve_report_subject(session, report)
        subject_row = session.query(ResearchSubject).filter_by(id=raw_id).first()
        if subject_row:
            return _subject_from_rows(session, subject_row=subject_row)

    raw = str(subject or "").strip()
    if not raw:
        raise ValueError("Missing report subject.")
    raw_sym = raw.lstrip("$").strip()
    raw_sym_upper = raw_sym.upper()
    raw_lower = raw.lower()

    report = (
        session.query(ResearchReport)
        .filter(or_(func.lower(ResearchReport.slug) == raw_lower, func.lower(ResearchReport.subject_key) == raw_lower))
        .first()
    )
    if report:
        return resolve_report_subject(session, report)

    # Canonical names and listing identifiers outrank free-form aliases, which
    # may contain historical cross-company contamination.
    from thoth_terminal.identity_alias_service import canonical_alias_owners
    canonical = canonical_alias_owners()
    canonical_ids = session.scalars(select(canonical.c.issuer_id).where(
        canonical.c.alias == raw_sym.lower().replace("-", " ").replace("_", " ")).distinct().limit(2)).all()
    if len(canonical_ids) > 1:
        raise ValueError("Canonical identity is ambiguous; supply a qualified subject reference.")
    if canonical_ids:
        canonical_issuer = session.get(Issuer, canonical_ids[0])
        canonical_subject = session.query(ResearchSubject).filter(
            ResearchSubject.subject_key == f"issuer:{canonical_issuer.id}",
            ResearchSubject.status.notin_(["merged", "archived"])).first()
        canonical_company = _bridge_company_for_issuer(session, canonical_issuer, preferred_ticker=raw_sym)
        return _subject_from_rows(session, subject_row=canonical_subject,
                                  issuer=canonical_issuer, company=canonical_company)

    subject_row = (
        session.query(ResearchSubject)
        .filter(
            or_(
                func.lower(ResearchSubject.slug) == raw_lower,
                func.lower(ResearchSubject.subject_key) == raw_lower,
                func.lower(ResearchSubject.display_name) == raw_lower,
                func.lower(func.coalesce(ResearchSubject.legal_name, "")) == raw_lower,
            )
        )
        .order_by(ResearchSubject.updated_at.desc(), ResearchSubject.id.desc())
        .first()
    )
    if subject_row:
        return _subject_from_rows(session, subject_row=subject_row)

    alias_row = (
        session.query(ResearchSubjectAlias)
        .join(ResearchSubject, ResearchSubject.id == ResearchSubjectAlias.subject_id)
        .filter(func.lower(ResearchSubjectAlias.alias) == raw_lower)
        .filter(ResearchSubject.status.notin_(["merged", "archived"]))
        .order_by(ResearchSubject.updated_at.desc(), ResearchSubjectAlias.id.asc())
        .first()
    )
    if alias_row:
        subject_row = session.query(ResearchSubject).filter_by(id=int(alias_row.subject_id)).first()
        if subject_row:
            return _subject_from_rows(session, subject_row=subject_row)

    issuer = session.query(Issuer).filter(func.lower(Issuer.slug) == raw_lower).first()
    if issuer:
        return _subject_from_rows(session, issuer=issuer)

    company = session.query(Company).filter(func.lower(Company.slug) == raw_lower).first()
    if company:
        return _subject_from_rows(session, company=company)

    if raw_sym_upper:
        issuer_ident = (
            session.query(IssuerIdentifier)
            .filter(IssuerIdentifier.id_type == "TICKER", func.upper(IssuerIdentifier.id_value) == raw_sym_upper)
            .order_by(IssuerIdentifier.is_primary.desc(), IssuerIdentifier.id.asc())
            .first()
        )
        if issuer_ident:
            issuer = session.query(Issuer).filter_by(id=int(issuer_ident.issuer_id)).first()
            if issuer:
                return _subject_from_rows(session, issuer=issuer)
        company = session.query(Company).filter(func.upper(Company.ticker) == raw_sym_upper).order_by(Company.id.asc()).first()
        if company:
            return _subject_from_rows(session, company=company)
        alias_row = (
            session.query(ResearchSubjectAlias)
            .join(ResearchSubject, ResearchSubject.id == ResearchSubjectAlias.subject_id)
            .filter(func.upper(ResearchSubjectAlias.alias) == raw_sym_upper)
            .filter(ResearchSubject.status.notin_(["merged", "archived"]))
            .order_by(ResearchSubject.updated_at.desc(), ResearchSubjectAlias.id.asc())
            .first()
        )
        if alias_row:
            subject_row = session.query(ResearchSubject).filter_by(id=int(alias_row.subject_id)).first()
            if subject_row:
                return _subject_from_rows(session, subject_row=subject_row)

    issuer = (
        session.query(Issuer)
        .filter(
            or_(
                func.lower(Issuer.display_name) == raw_lower,
                func.lower(Issuer.legal_name) == raw_lower,
                Issuer.display_name.ilike(f"%{raw}%"),
                Issuer.legal_name.ilike(f"%{raw}%"),
            )
        )
        .order_by(Issuer.display_name.asc())
        .first()
    )
    if issuer:
        return _subject_from_rows(session, issuer=issuer)

    company = (
        session.query(Company)
        .filter(or_(func.lower(Company.name) == raw_lower, Company.name.ilike(f"%{raw}%")))
        .order_by(Company.name.asc())
        .first()
    )
    if company:
        return _subject_from_rows(session, company=company)

    entity_rows = (
        session.query(Entity)
        .outerjoin(EntityAlias, EntityAlias.entity_id == Entity.id)
        .filter(
            or_(
                func.lower(Entity.canonical_name) == raw_lower,
                func.lower(func.coalesce(Entity.description, "")).like(f"%{raw_lower}%"),
                func.lower(EntityAlias.alias) == raw_lower,
            )
        )
        .order_by(Entity.id.asc())
        .all()
    )
    if entity_rows:
        entity_row = entity_rows[0]
        subject_row = session.query(ResearchSubject).filter_by(primary_entity_id=int(entity_row.id)).first()
        return _subject_from_rows(session, subject_row=subject_row, primary_entity=entity_row)

    raise ValueError(f"Unknown report subject: {raw}")


def _entity_ids_for_subject(session: Session, subject: ReportSubject) -> list[int]:
    lowered = {alias.lower() for alias in subject.aliases if alias}
    if not lowered and subject.primary_entity_id is None:
        return []
    q = session.query(Entity.id).outerjoin(EntityAlias, EntityAlias.entity_id == Entity.id)
    filters = []
    if lowered:
        filters.extend(
            [
                func.lower(Entity.canonical_name).in_(lowered),
                func.lower(EntityAlias.alias).in_(lowered),
            ]
        )
    if subject.primary_entity_id is not None:
        filters.append(Entity.id == int(subject.primary_entity_id))
    rows = q.filter(or_(*filters)).distinct().all()
    out = [int(row[0]) for row in rows if row and row[0] is not None]
    if subject.primary_entity_id is not None and int(subject.primary_entity_id) not in out:
        out.append(int(subject.primary_entity_id))
    return out


def _entity_labels_by_linked_object_id(
    session: Session,
    *,
    link_table: Any,
    object_id_column: Any,
    object_ids: list[int],
) -> dict[int, list[str]]:
    if not object_ids:
        return {}
    out: dict[int, list[str]] = {int(object_id): [] for object_id in object_ids}
    rows = (
        session.query(object_id_column.label("object_id"), Entity.canonical_name)
        .join(Entity, Entity.id == link_table.c.entity_id)
        .filter(object_id_column.in_(object_ids))
        .order_by(object_id_column.asc(), Entity.canonical_name.asc())
        .all()
    )
    for row in rows:
        object_id = int(row.object_id)
        label = str(row.canonical_name or "").strip()
        if label:
            out.setdefault(object_id, []).append(label)
    return out


def _thesis_rows(session: Session, subject: ReportSubject, *, window_days: int | None = None) -> list[dict[str, Any]]:
    entity_ids = _entity_ids_for_subject(session, subject)
    if not entity_ids:
        return []
    cutoff = _window_cutoff(window_days)
    thesis_ids = [
        int(row[0])
        for row in (
            session.query(Thesis.id)
            .join(Thesis.entities)
            .filter(Entity.id.in_(entity_ids))
            .filter(Thesis.updated_at >= cutoff)
            .distinct()
            .all()
        )
        if row and row[0] is not None
    ]
    if not thesis_ids:
        return []
    entity_labels = _entity_labels_by_linked_object_id(
        session,
        link_table=thesis_entities,
        object_id_column=thesis_entities.c.thesis_id,
        object_ids=thesis_ids,
    )
    rows = (
        session.query(
            Thesis.id,
            Thesis.title,
            Thesis.status,
            Thesis.importance,
            Thesis.stale_after_days,
            Thesis.last_reviewed_at,
            Thesis.last_stale_check_at,
            Thesis.stale_check_state,
            Thesis.updated_at,
        )
        .filter(Thesis.id.in_(thesis_ids))
        .order_by(Thesis.updated_at.desc(), Thesis.id.desc())
        .all()
    )
    return [
        {
            "id": int(row.id),
            "title": row.title,
            "status": row.status,
            "importance": row.importance,
            "stale_after_days": row.stale_after_days,
            "last_reviewed_at": _dt_iso(row.last_reviewed_at),
            "last_stale_check_at": _dt_iso(row.last_stale_check_at),
            "stale_check_state": row.stale_check_state,
            "updated_at": _dt_iso(row.updated_at),
            "href": f"/theses/{int(row.id)}",
            "entities": entity_labels.get(int(row.id), []),
        }
        for row in rows
    ]


def _note_rows(session: Session, subject: ReportSubject, *, window_days: int | None = None) -> list[dict[str, Any]]:
    entity_ids = _entity_ids_for_subject(session, subject)
    if not entity_ids:
        return []
    cutoff = _window_cutoff(window_days)
    note_ids = [
        int(row[0])
        for row in (
            session.query(Note.id)
            .join(Note.entities)
            .filter(Entity.id.in_(entity_ids))
            .filter(Note.updated_at >= cutoff)
            .distinct()
            .all()
        )
        if row and row[0] is not None
    ]
    if not note_ids:
        return []
    entity_labels = _entity_labels_by_linked_object_id(
        session,
        link_table=note_entities,
        object_id_column=note_entities.c.note_id,
        object_ids=note_ids,
    )
    rows = (
        session.query(
            Note.id,
            Note.title,
            Note.note_type,
            Note.status,
            Note.importance,
            Note.stale_after_days,
            Note.last_reviewed_at,
            Note.last_stale_check_at,
            Note.stale_check_state,
            Note.updated_at,
        )
        .filter(Note.id.in_(note_ids))
        .order_by(Note.updated_at.desc(), Note.id.desc())
        .all()
    )
    return [
        {
            "id": int(row.id),
            "title": row.title,
            "note_type": row.note_type,
            "status": row.status,
            "importance": row.importance,
            "stale_after_days": row.stale_after_days,
            "last_reviewed_at": _dt_iso(row.last_reviewed_at),
            "last_stale_check_at": _dt_iso(row.last_stale_check_at),
            "stale_check_state": row.stale_check_state,
            "updated_at": _dt_iso(row.updated_at),
            "href": f"/notes/{int(row.id)}",
            "entities": entity_labels.get(int(row.id), []),
        }
        for row in rows
    ]


def _primary_sources(session: Session, subject: ReportSubject, *, window_days: int | None = None) -> dict[str, Any]:
    filings: list[dict[str, Any]] = []
    disclosures: list[dict[str, Any]] = []
    ir_artifacts: list[dict[str, Any]] = []
    legacy_documents: list[dict[str, Any]] = []
    timeline: list[dict[str, Any]] = []
    cutoff = _window_cutoff(window_days)

    if subject.issuer_id is not None:
        filing_rows = (
            session.query(
                SecuritiesFiling.id,
                SecuritiesFiling.filing_type,
                SecuritiesFiling.title,
                SecuritiesFiling.primary_document_url,
                SecuritiesFiling.filing_url,
                SecuritiesFiling.filed_at,
                SecuritiesFiling.fiscal_year,
                SecuritiesFiling.fiscal_quarter,
            )
            .filter(SecuritiesFiling.issuer_id == int(subject.issuer_id))
            .filter(SecuritiesFiling.filed_at >= cutoff)
            .order_by(SecuritiesFiling.filed_at.desc(), SecuritiesFiling.id.desc())
            .all()
        )
        for row in filing_rows:
            item = {
                "id": int(row.id),
                "rail": "rail1",
                "label": row.filing_type,
                "title": row.title or row.filing_type,
                "url": clean_url(row.primary_document_url or row.filing_url),
                "published_at": _dt_iso(row.filed_at),
                "fiscal_year": row.fiscal_year,
                "fiscal_quarter": row.fiscal_quarter,
            }
            filings.append(item)
            timeline.append(item)

        disclosure_rows = (
            session.query(
                ExchangeDisclosure.id,
                ExchangeDisclosure.disclosure_type,
                ExchangeDisclosure.title,
                ExchangeDisclosure.disclosure_url,
                ExchangeDisclosure.announced_at,
                ExchangeDisclosure.fiscal_year,
                ExchangeDisclosure.fiscal_quarter,
            )
            .filter(ExchangeDisclosure.issuer_id == int(subject.issuer_id))
            .filter(ExchangeDisclosure.announced_at >= cutoff)
            .order_by(ExchangeDisclosure.announced_at.desc(), ExchangeDisclosure.id.desc())
            .all()
        )
        for row in disclosure_rows:
            item = {
                "id": int(row.id),
                "rail": "rail2",
                "label": row.disclosure_type,
                "title": row.title or row.disclosure_type,
                "url": row.disclosure_url,
                "published_at": _dt_iso(row.announced_at),
                "fiscal_year": row.fiscal_year,
                "fiscal_quarter": row.fiscal_quarter,
            }
            disclosures.append(item)
            timeline.append(item)

        artifact_rows = (
            session.query(
                IRArtifact.id,
                IRArtifact.document_id,
                IRArtifact.artifact_type,
                IRArtifact.title,
                IRArtifact.canonical_url,
                IRArtifact.download_url,
                IRArtifact.webcast_url,
                IRArtifact.published_at,
                IRArtifact.created_at,
                IRArtifact.fiscal_year,
                IRArtifact.fiscal_quarter,
            )
            .filter(IRArtifact.issuer_id == int(subject.issuer_id))
            .filter(func.coalesce(IRArtifact.published_at, IRArtifact.created_at) >= cutoff)
            .order_by(IRArtifact.published_at.desc().nullslast(), IRArtifact.id.desc())
            .all()
        )
        for row in artifact_rows:
            item = {
                "id": int(row.id),
                "document_id": int(row.document_id) if row.document_id is not None else None,
                "rail": "rail3",
                "label": row.artifact_type,
                "title": row.title or row.artifact_type,
                "url": row.canonical_url or row.download_url or row.webcast_url,
                "published_at": _dt_iso(row.published_at or row.created_at),
                "fiscal_year": row.fiscal_year,
                "fiscal_quarter": row.fiscal_quarter,
            }
            ir_artifacts.append(item)
            timeline.append(item)

    if subject.company_id is not None:
        doc_rows = (
            session.query(
                CompanyDocument.id.label("link_id"),
                CompanyDocument.doc_type,
                CompanyDocument.label,
                Document.id.label("document_id"),
                Document.title,
                Document.url,
                Document.canonical_url,
                Document.published_at,
                Document.ingested_at,
            )
            .join(Document, Document.id == CompanyDocument.document_id)
            .filter(CompanyDocument.company_id == int(subject.company_id))
            .filter(func.coalesce(Document.published_at, Document.ingested_at) >= cutoff)
            .order_by(Document.published_at.desc().nullslast(), Document.ingested_at.desc())
            .all()
        )
        for row in doc_rows:
            legacy_documents.append(
                {
                    "id": int(row.document_id),
                    "link_id": int(row.link_id),
                    "doc_type": row.doc_type,
                    "title": row.title or row.label or f"doc {row.document_id}",
                    "url": row.url or row.canonical_url,
                    "published_at": _dt_iso(row.published_at or row.ingested_at),
                    "href": f"/docs/{int(row.document_id)}",
                }
            )

    timeline.sort(key=lambda item: item.get("published_at") or "", reverse=True)
    return {
        "filings": filings,
        "disclosures": disclosures,
        "ir_artifacts": ir_artifacts,
        "legacy_documents": legacy_documents,
        "timeline": timeline,
    }


def _structured_fact_source_line(row: StructuredDataFact) -> str:
    projected = getattr(row, "payload_source_line", None)
    if projected is not None:
        return str(projected or "")
    raw_payload = getattr(row, "payload", None)
    payload = raw_payload if isinstance(raw_payload, dict) else {}
    return str(payload.get("source_line") or "")


def _is_suspect_structured_fact(row: StructuredDataFact) -> bool:
    metric_key = str(row.metric_key or "").strip().lower()
    if metric_key != "revenue":
        return False
    unit = str(row.unit or "").strip().lower()
    if unit != "currency":
        return False
    line = _structured_fact_source_line(row).lower()
    if any(token in line for token in _REVENUE_SEGMENT_CONTEXT):
        return True
    if any(token in line for token in _REVENUE_NON_TOPLINE_CONTEXT):
        return True
    try:
        value = float(row.value_number) if row.value_number is not None else None
    except Exception:
        value = None
    if value is not None and value < 1_000_000 and ("%" in line or "growth" in line):
        return True
    return False


def _compact_number(value: float) -> str:
    abs_value = abs(value)
    if abs_value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f}".rstrip("0").rstrip(".") + "B"
    if abs_value >= 1_000_000:
        return f"{value / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M"
    if abs_value >= 1_000:
        return f"{value / 1_000:.1f}".rstrip("0").rstrip(".") + "K"
    return f"{value:g}"


def _format_structured_value(
    *,
    value_number: object,
    value_text: str | None,
    unit: str | None,
    currency: str | None,
) -> str | None:
    if value_number is None:
        return value_text
    try:
        value = float(value_number)
    except Exception:
        return value_text
    unit_norm = str(unit or "").strip().lower()
    currency_norm = str(currency or "").strip().upper()
    symbol = "$" if currency_norm == "USD" else (f"{currency_norm} " if currency_norm else "")
    if unit_norm == "%":
        return f"{value:g}%"
    if unit_norm == "per_share":
        return f"{symbol}{value:g}" if symbol else f"{value:g} per share"
    if unit_norm == "currency":
        return f"{symbol}{_compact_number(value)}"
    return f"{value:g} {unit}".strip() if unit else f"{value:g}"


def _structured_fact_item(
    row: StructuredDataFact,
    *,
    value_number: object | None = None,
    value_text: str | None = None,
    unit: str | None = None,
    currency: str | None = None,
    period_label: str | None = None,
    period_date: object | None = None,
    as_of_date: object | None = None,
    data_quality_note: str | None = None,
) -> dict[str, Any]:
    resolved_value_number = row.value_number if value_number is None else value_number
    resolved_value_text = row.value_text if value_text is None else value_text
    resolved_unit = row.unit if unit is None else unit
    resolved_currency = row.currency if currency is None else currency
    item: dict[str, Any] = {
        "id": int(row.id),
        "dataset_family": row.dataset_family,
        "metric_key": row.metric_key,
        "metric_label": row.metric_label or row.metric_key,
        "statement_type": row.statement_type,
        "period_label": row.period_label if period_label is None else period_label,
        "period_date": _date_like(row.period_date if period_date is None else period_date),
        "as_of_date": _date_like(row.as_of_date if as_of_date is None else as_of_date),
        "value_number": resolved_value_number,
        "value_text": resolved_value_text,
        "display_value": _format_structured_value(
            value_number=resolved_value_number,
            value_text=resolved_value_text,
            unit=resolved_unit,
            currency=resolved_currency,
        ),
        "unit": resolved_unit,
        "currency": resolved_currency,
        "provenance_label": row.provenance_label,
        "provenance_url": row.provenance_url,
        "dataset_id": int(row.dataset_id) if row.dataset_id is not None else None,
        "document_id": int(row.document_id) if row.document_id is not None else None,
    }
    if data_quality_note:
        item["data_quality_note"] = data_quality_note
    return item


def _reparsed_structured_fact_item(session: Session, row: StructuredDataFact) -> dict[str, Any] | None:
    if row.document_id is None:
        return None
    document = session.query(Document).filter_by(id=int(row.document_id)).first()
    if document is None:
        return None
    as_of_date = row.as_of_date or (document.published_at.date() if document.published_at else None) or (
        document.ingested_at.date() if document.ingested_at else None
    )
    doc_type = str(getattr(row, "payload_doc_type", "") or "").strip()
    if not doc_type:
        raw_payload = getattr(row, "payload", None)
        payload = raw_payload if isinstance(raw_payload, dict) else {}
        doc_type = str(payload.get("doc_type") or "").strip()
    extracted_rows = extract_earnings_metric_rows(
        title=document.title,
        text=document.translation_en or document.normalized_text or document.raw_text,
        doc_type=doc_type or "earnings_release",
        as_of_date=as_of_date,
    )
    for candidate in extracted_rows:
        if str(candidate.get("dataset_family") or "") != str(row.dataset_family or ""):
            continue
        if str(candidate.get("metric_key") or "") != str(row.metric_key or ""):
            continue
        return _structured_fact_item(
            row,
            value_number=candidate.get("value_number"),
            value_text=candidate.get("value_text"),
            unit=str(candidate.get("unit") or "") or None,
            currency=str(candidate.get("currency") or "") or None,
            period_label=str(candidate.get("period_label") or "") or row.period_label,
            period_date=row.period_date,
            as_of_date=as_of_date,
            data_quality_note="reparsed_source_document",
        )
    return None


def _structured_rows(session: Session, subject: ReportSubject, *, window_days: int | None = None) -> dict[str, Any]:
    if subject.company_id is None:
        return {"actuals": [], "guidance": [], "metrics": []}
    company_ids = company_structured_fact_scope_ids(session, subject.company)
    if not company_ids:
        return {"actuals": [], "guidance": [], "metrics": []}
    cutoff_date = _window_cutoff(window_days).date()
    rows = (
        session.query(
            StructuredDataFact.id,
            StructuredDataFact.dataset_family,
            StructuredDataFact.metric_key,
            StructuredDataFact.metric_label,
            StructuredDataFact.statement_type,
            StructuredDataFact.period_label,
            StructuredDataFact.period_date,
            StructuredDataFact.as_of_date,
            StructuredDataFact.value_number,
            StructuredDataFact.value_text,
            StructuredDataFact.unit,
            StructuredDataFact.currency,
            StructuredDataFact.provenance_label,
            StructuredDataFact.provenance_url,
            StructuredDataFact.dataset_id,
            StructuredDataFact.document_id,
            StructuredDataFact.payload["source_line"].as_string().label("payload_source_line"),
            StructuredDataFact.payload["doc_type"].as_string().label("payload_doc_type"),
        )
        .filter(StructuredDataFact.company_id.in_(company_ids))
        .filter(
            or_(
                StructuredDataFact.as_of_date.is_(None),
                StructuredDataFact.as_of_date >= cutoff_date,
                StructuredDataFact.period_date >= cutoff_date,
            )
        )
        .order_by(
            StructuredDataFact.as_of_date.desc().nullslast(),
            StructuredDataFact.period_date.desc().nullslast(),
            StructuredDataFact.id.desc(),
        )
        .all()
    )
    actuals: list[dict[str, Any]] = []
    guidance: list[dict[str, Any]] = []
    metric_keys: set[str] = set()
    for row in rows:
        if _is_suspect_structured_fact(row):
            item = _reparsed_structured_fact_item(session, row)
            if item is None:
                continue
        else:
            item = _structured_fact_item(row)
        metric_keys.add(str(row.metric_key or "").strip())
        if "guidance" in str(row.dataset_family or "").lower() or str(row.statement_type or "").lower() == "guidance":
            guidance.append(item)
        else:
            actuals.append(item)
    return {"actuals": actuals, "guidance": guidance, "metrics": sorted(metric for metric in metric_keys if metric)}


def _transcript_rows(session: Session, subject: ReportSubject, *, window_days: int | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    cutoff = _window_cutoff(window_days)
    if subject.issuer_id is not None:
        event_rows = (
            session.query(
                EarningsEvent.actual_release_at,
                EarningsEvent.updated_at,
                EarningsEvent.fiscal_year.label("event_fiscal_year"),
                EarningsEvent.fiscal_quarter.label("event_fiscal_quarter"),
                IRArtifact.id.label("artifact_id"),
                IRArtifact.document_id,
                IRArtifact.title,
                IRArtifact.canonical_url,
                IRArtifact.download_url,
                IRArtifact.webcast_url,
                IRArtifact.published_at,
                IRArtifact.fiscal_year.label("artifact_fiscal_year"),
                IRArtifact.fiscal_quarter.label("artifact_fiscal_quarter"),
            )
            .join(IRArtifact, IRArtifact.id == EarningsEvent.transcript_artifact_id)
            .filter(EarningsEvent.issuer_id == int(subject.issuer_id))
            .filter(func.coalesce(IRArtifact.published_at, EarningsEvent.actual_release_at, EarningsEvent.updated_at) >= cutoff)
            .order_by(IRArtifact.published_at.desc().nullslast(), EarningsEvent.updated_at.desc())
            .all()
        )
        for row in event_rows:
            key = f"artifact:{int(row.artifact_id)}"
            if key in seen_keys:
                continue
            seen_keys.add(key)
            rows.append(
                {
                    "key": key,
                    "document_id": int(row.document_id) if row.document_id is not None else None,
                    "title": row.title or "Transcript",
                    "url": row.canonical_url or row.download_url or row.webcast_url,
                    "published_at": row.published_at or row.actual_release_at,
                    "fiscal_year": row.event_fiscal_year or row.artifact_fiscal_year,
                    "fiscal_quarter": row.event_fiscal_quarter or row.artifact_fiscal_quarter,
                    "period_label": (
                        f"Q{row.event_fiscal_quarter} {row.event_fiscal_year}"
                        if row.event_fiscal_year and row.event_fiscal_quarter
                        else (f"FY {row.event_fiscal_year}" if row.event_fiscal_year else None)
                    ),
                    "source_kind": "earnings_event",
                }
            )
        direct_ir_rows = (
            session.query(
                IRArtifact.id,
                IRArtifact.document_id,
                IRArtifact.title,
                IRArtifact.canonical_url,
                IRArtifact.download_url,
                IRArtifact.webcast_url,
                IRArtifact.published_at,
                IRArtifact.created_at,
                IRArtifact.fiscal_year,
                IRArtifact.fiscal_quarter,
            )
            .filter(IRArtifact.issuer_id == int(subject.issuer_id), IRArtifact.artifact_type == "transcript")
            .filter(func.coalesce(IRArtifact.published_at, IRArtifact.created_at) >= cutoff)
            .order_by(IRArtifact.published_at.desc().nullslast(), IRArtifact.id.desc())
            .all()
        )
        for artifact in direct_ir_rows:
            key = f"artifact:{int(artifact.id)}"
            if key in seen_keys:
                continue
            seen_keys.add(key)
            rows.append(
                {
                    "key": key,
                    "document_id": int(artifact.document_id) if artifact.document_id is not None else None,
                    "title": artifact.title or "Transcript",
                    "url": artifact.canonical_url or artifact.download_url or artifact.webcast_url,
                    "published_at": artifact.published_at or artifact.created_at,
                    "fiscal_year": artifact.fiscal_year,
                    "fiscal_quarter": artifact.fiscal_quarter,
                    "period_label": (
                        f"Q{artifact.fiscal_quarter} {artifact.fiscal_year}"
                        if artifact.fiscal_year and artifact.fiscal_quarter
                        else (f"FY {artifact.fiscal_year}" if artifact.fiscal_year else None)
                    ),
                    "source_kind": "ir_artifact",
                }
            )
    if subject.company_id is not None:
        doc_rows = (
            session.query(
                CompanyDocument.doc_type,
                CompanyDocument.label,
                Document.id.label("document_id"),
                Document.title,
                Document.url,
                Document.canonical_url,
                Document.published_at,
                Document.ingested_at,
            )
            .join(Document, Document.id == CompanyDocument.document_id)
            .filter(CompanyDocument.company_id == int(subject.company_id))
            .filter(func.coalesce(Document.published_at, Document.ingested_at) >= cutoff)
            .filter(
                or_(
                    CompanyDocument.doc_type.in_(["earnings_transcript", "transcript"]),
                    func.lower(func.coalesce(Document.title, "")).like("%transcript%"),
                    func.lower(func.coalesce(Document.url, "")).like("%transcript%"),
                    func.lower(func.coalesce(Document.canonical_url, "")).like("%transcript%"),
                )
            )
            .order_by(Document.published_at.desc().nullslast(), Document.ingested_at.desc())
            .all()
        )
        for row in doc_rows:
            key = f"document:{int(row.document_id)}"
            if key in seen_keys:
                continue
            seen_keys.add(key)
            rows.append(
                {
                    "key": key,
                    "document_id": int(row.document_id),
                    "title": row.title or row.label or "Transcript",
                    "url": row.url or row.canonical_url,
                    "published_at": row.published_at or row.ingested_at,
                    "fiscal_year": None,
                    "fiscal_quarter": None,
                    "period_label": None,
                    "source_kind": f"company_document:{row.doc_type or 'transcript'}",
                }
            )
    rows.sort(key=lambda item: item.get("published_at") or datetime.min, reverse=True)
    return rows


def _extract_transcript_lines(text: str, *, limit: int = 6) -> list[str]:
    candidates = re.split(r"[\r\n]+|(?<=[.!?])\s+", text)
    out: list[str] = []
    seen: set[str] = set()
    for line in candidates:
        cleaned = _clean_text(line)
        if len(cleaned) < 24:
            continue
        lowered = cleaned.lower()
        if not any(token in lowered for token in _SUMMARY_KEYWORDS):
            continue
        if lowered in seen:
            continue
        seen.add(lowered)
        out.append(cleaned[:280])
        if len(out) >= limit:
            break
    if out:
        return out
    fallbacks: list[str] = []
    for line in candidates:
        cleaned = _clean_text(line)
        if len(cleaned) >= 24:
            fallbacks.append(cleaned[:280])
        if len(fallbacks) >= limit:
            break
    return fallbacks


def _llm_provider_model() -> str:
    provider = str(getattr(settings, "ai_provider", "openai") or "openai").strip().lower()
    if provider == "anthropic":
        return f"anthropic:{str(getattr(settings, 'anthropic_model', '') or '').strip() or 'default'}"
    if provider == "openrouter":
        return f"openrouter:{str(getattr(settings, 'openrouter_model', '') or '').strip() or 'default'}"
    if provider == "deepseek":
        return f"deepseek:{str(getattr(settings, 'deepseek_model', '') or '').strip() or 'default'}"
    return f"openai:{str(getattr(settings, 'openai_model', '') or '').strip() or 'default'}"


def _transcript_summary_document_excerpt(
    session: Session,
    document_id: int,
    *,
    max_chars: int = REPORT_TRANSCRIPT_SUMMARY_EXCERPT_CHARS,
) -> Any | None:
    text_expr = func.coalesce(
        func.nullif(Document.translation_en, ""),
        func.nullif(Document.normalized_text, ""),
        Document.raw_text,
        "",
    )
    bounded_chars = max(1, int(max_chars))
    return (
        session.query(
            Document.id.label("id"),
            Document.title.label("title"),
            func.substr(text_expr, 1, bounded_chars).label("text_excerpt"),
            func.length(text_expr).label("text_chars"),
        )
        .filter(Document.id == int(document_id))
        .first()
    )


def ensure_transcript_summary(
    session: Session,
    *,
    document_id: int,
    issuer_id: int | None = None,
    company_id: int | None = None,
    ticker: str | None = None,
    fiscal_year: int | None = None,
    fiscal_quarter: int | None = None,
    period_label: str | None = None,
    source_kind: str | None = None,
) -> TranscriptSummary:
    document = _transcript_summary_document_excerpt(session, int(document_id))
    if not document:
        raise ValueError("Transcript document not found.")
    text = str(document.text_excerpt or "").strip()
    if not text:
        raise ValueError("Transcript document has no text.")
    doc_id = int(document.id)
    title = str(document.title or "").strip()

    now = utcnow()
    highlights = _extract_transcript_lines(text, limit=6)
    overview = _clean_text((title or "") + " " + " ".join(highlights[:2]))[:360]
    payload: dict[str, Any] = {
        "overview": overview or (title or f"Transcript {doc_id}"),
        "highlights": highlights,
        "source_document_id": doc_id,
    }
    summary_md = "\n".join(
        [
            f"### {title or f'Transcript {doc_id}'}",
            "",
            payload["overview"],
            "",
            "Highlights:",
        ]
        + [f"- {line}" for line in highlights]
    ).strip()
    model_name = "heuristic"

    if bool(getattr(settings, "agent_enabled", False)) and llm_available(provider=getattr(settings, "ai_provider", "openai")):
        prompt = (
            "Return JSON only with keys: overview (string), highlights (array of strings), "
            "guidance (array of strings), risks (array of strings). "
            "Keep overview <= 260 chars and each list <= 5 items.\n\n"
            f"Title: {title}\n"
            f"Transcript excerpt:\n{text[:12000]}"
        )
        try:
            parsed = manifested_chat_json(
                session,
                run_kind="research_document",
                run_id=int(doc_id),
                task_kind="transcript_summary",
                messages=[
                    ChatMessage(role="system", content="You extract compact earnings transcript takeaways."),
                    ChatMessage(role="user", content=prompt),
                ],
                provider=getattr(settings, "ai_provider", "openai"),
                timeout_s=60.0,
                max_input_chars=16000,
                prompt_cache_key="thoth-transcript-summary-v1",
                permission_profile="draft-only",
            )
            overview_v = _clean_text(parsed.get("overview"))[:260]
            llm_highlights = [_clean_text(item)[:280] for item in (parsed.get("highlights") or []) if _clean_text(item)][:5]
            guidance = [_clean_text(item)[:280] for item in (parsed.get("guidance") or []) if _clean_text(item)][:5]
            risks = [_clean_text(item)[:280] for item in (parsed.get("risks") or []) if _clean_text(item)][:5]
            payload = {
                "overview": overview_v or payload["overview"],
                "highlights": llm_highlights or highlights,
                "guidance": guidance,
                "risks": risks,
                "source_document_id": doc_id,
            }
            summary_lines = [
                f"### {title or f'Transcript {doc_id}'}",
                "",
                str(payload["overview"]),
                "",
                "Highlights:",
            ]
            summary_lines.extend(f"- {line}" for line in (payload.get("highlights") or []))
            if payload.get("guidance"):
                summary_lines.extend(["", "Guidance / Outlook:"])
                summary_lines.extend(f"- {line}" for line in (payload.get("guidance") or []))
            if payload.get("risks"):
                summary_lines.extend(["", "Risks / Watchpoints:"])
                summary_lines.extend(f"- {line}" for line in (payload.get("risks") or []))
            summary_md = "\n".join(summary_lines).strip()
            model_name = _llm_provider_model()
        except AgentLlmError:
            pass

    row = session.query(TranscriptSummary).filter_by(document_id=doc_id).first()
    if row is None:
        row = TranscriptSummary(
            document_id=doc_id,
            created_at=now,
            updated_at=now,
            summary_md=summary_md,
            payload=payload,
        )
        session.add(row)
    row.issuer_id = int(issuer_id) if issuer_id is not None else None
    row.company_id = int(company_id) if company_id is not None else None
    row.ticker = str(ticker or "").strip().upper() or None
    row.fiscal_year = fiscal_year
    row.fiscal_quarter = fiscal_quarter
    row.period_label = period_label
    row.source_kind = source_kind
    row.model = model_name
    row.summary_md = summary_md
    row.payload = payload
    row.updated_at = now
    session.flush()
    return row


def _apply_transcript_summary_context(
    row: TranscriptSummary,
    *,
    issuer_id: int | None = None,
    company_id: int | None = None,
    ticker: str | None = None,
    fiscal_year: int | None = None,
    fiscal_quarter: int | None = None,
    period_label: str | None = None,
    source_kind: str | None = None,
) -> bool:
    updates = {
        "issuer_id": int(issuer_id) if issuer_id is not None else None,
        "company_id": int(company_id) if company_id is not None else None,
        "ticker": str(ticker or "").strip().upper() or None,
        "fiscal_year": fiscal_year,
        "fiscal_quarter": fiscal_quarter,
        "period_label": period_label,
        "source_kind": source_kind,
    }
    changed = False
    for key, value in updates.items():
        if getattr(row, key) != value:
            setattr(row, key, value)
            changed = True
    if changed:
        row.updated_at = utcnow()
    return changed


def _transcript_archive(session: Session, subject: ReportSubject, *, window_days: int | None = None) -> dict[str, Any]:
    rows = _transcript_rows(session, subject, window_days=window_days)
    document_ids = sorted({int(row["document_id"]) for row in rows if row.get("document_id") is not None})
    summary_by_document_id: dict[int, TranscriptSummary] = {}
    if document_ids:
        summary_by_document_id = {
            int(row.document_id): row
            for row in session.query(TranscriptSummary).filter(TranscriptSummary.document_id.in_(document_ids)).all()
        }
    out_rows: list[dict[str, Any]] = []
    summary_count = 0
    for row in rows:
        document_id = row.get("document_id")
        summary_row = None
        if document_id is not None:
            doc_id = int(document_id)
            summary_row = summary_by_document_id.get(doc_id)
            if summary_row is not None:
                _apply_transcript_summary_context(
                    summary_row,
                    issuer_id=subject.issuer_id,
                    company_id=subject.company_id,
                    ticker=subject.primary_ticker,
                    fiscal_year=row.get("fiscal_year"),
                    fiscal_quarter=row.get("fiscal_quarter"),
                    period_label=row.get("period_label"),
                    source_kind=str(row.get("source_kind") or ""),
                )
            else:
                try:
                    summary_row = ensure_transcript_summary(
                        session,
                        document_id=doc_id,
                        issuer_id=subject.issuer_id,
                        company_id=subject.company_id,
                        ticker=subject.primary_ticker,
                        fiscal_year=row.get("fiscal_year"),
                        fiscal_quarter=row.get("fiscal_quarter"),
                        period_label=row.get("period_label"),
                        source_kind=str(row.get("source_kind") or ""),
                    )
                    summary_by_document_id[doc_id] = summary_row
                except Exception:
                    summary_row = (
                        summary_by_document_id.get(doc_id)
                        or session.query(TranscriptSummary).filter_by(document_id=doc_id).first()
                    )
                    if summary_row is not None:
                        summary_by_document_id[doc_id] = summary_row
        if summary_row is not None:
            summary_count += 1
        out_rows.append(
            {
                "key": row.get("key"),
                "document_id": document_id,
                "title": row.get("title"),
                "url": row.get("url"),
                "published_at": _dt_iso(row.get("published_at")),
                "fiscal_year": row.get("fiscal_year"),
                "fiscal_quarter": row.get("fiscal_quarter"),
                "period_label": row.get("period_label"),
                "source_kind": row.get("source_kind"),
                "summary_id": int(summary_row.id) if summary_row is not None else None,
                "summary_md": summary_row.summary_md if summary_row is not None else None,
                "summary_payload": dict(summary_row.payload or {}) if summary_row is not None else None,
                "summary_model": summary_row.model if summary_row is not None else None,
            }
        )
    return {"rows": out_rows, "count": len(out_rows), "summary_count": summary_count}


def _linked_supply_chains(session: Session, subject: ReportSubject, *, thesis_rows: list[dict[str, Any]], note_rows: list[dict[str, Any]]) -> dict[str, Any]:
    thesis_ids = [int(row["id"]) for row in thesis_rows]
    note_ids = [int(row["id"]) for row in note_rows]
    chain_ids: set[int] = set()
    if thesis_ids or note_ids:
        link_filters = []
        if thesis_ids:
            link_filters.append(and_(SupplyChainLink.object_type == "thesis", SupplyChainLink.object_id.in_(thesis_ids)))
        if note_ids:
            link_filters.append(and_(SupplyChainLink.object_type == "note", SupplyChainLink.object_id.in_(note_ids)))
        link_rows = (
            session.query(SupplyChainLink.chain_id)
            .filter(or_(*link_filters))
            .order_by(SupplyChainLink.id.asc())
            .all()
        )
        for link in link_rows:
            chain_ids.add(int(link.chain_id))

    stage_rows: list[ProductStageEntry] = []
    if subject.company_id is not None:
        stage_rows = (
            session.query(
                ProductStageEntry.id,
                ProductStageEntry.entry_key,
                ProductStageEntry.company_name,
                ProductStageEntry.product_name,
                ProductStageEntry.stage,
                ProductStageEntry.target_date,
                ProductStageEntry.status,
                ProductStageEntry.updated_at,
                ProductStageEntry.linked_supply_chain_ids,
                ProductStageEntry.linked_dataset_ids,
            )
            .filter(ProductStageEntry.company_id == int(subject.company_id))
            .order_by(ProductStageEntry.updated_at.desc(), ProductStageEntry.id.desc())
            .all()
        )
        for row in stage_rows:
            chain_ids.update(int(chain_id) for chain_id in (row.linked_supply_chain_ids or []) if str(chain_id).isdigit())

    chains: list[dict[str, Any]] = []
    if chain_ids:
        for chain in session.query(SupplyChain).filter(SupplyChain.id.in_(sorted(chain_ids))).order_by(SupplyChain.updated_at.desc(), SupplyChain.id.desc()).all():
            snapshot = latest_chain_snapshot(session, int(chain.id), status="published") or latest_chain_snapshot(session, int(chain.id))
            payload = serialize_supply_chain(session, chain, snapshot=snapshot, include_graph=False)
            chains.append(
                {
                    "id": int(chain.id),
                    "slug": chain.slug,
                    "title": chain.title,
                    "description": chain.description,
                    "category": chain.category,
                    "product": chain.product,
                    "status": chain.status,
                    "updated_at": payload.get("updated_at"),
                    "href": f"/visualizations/supply-chains/{int(chain.id)}/command-center",
                    "counts": payload.get("counts") or {},
                    "about": payload.get("about") or {},
                    "version": payload.get("version") or {},
                    "published_snapshot_id": int(snapshot.id) if snapshot is not None else None,
                }
            )

    neighbor_chain_ids: set[int] = set()
    if chain_ids:
        relation_rows = (
            session.query(SupplyChainRelation.parent_chain_id, SupplyChainRelation.child_chain_id)
            .filter(or_(SupplyChainRelation.parent_chain_id.in_(chain_ids), SupplyChainRelation.child_chain_id.in_(chain_ids)))
            .order_by(SupplyChainRelation.id.asc())
            .all()
        )
        for row in relation_rows:
            if int(row.parent_chain_id) not in chain_ids:
                neighbor_chain_ids.add(int(row.parent_chain_id))
            if int(row.child_chain_id) not in chain_ids:
                neighbor_chain_ids.add(int(row.child_chain_id))

    neighbors: list[dict[str, Any]] = []
    if neighbor_chain_ids:
        for chain in (
            session.query(
                SupplyChain.id,
                SupplyChain.slug,
                SupplyChain.title,
                SupplyChain.category,
                SupplyChain.product,
            )
            .filter(SupplyChain.id.in_(sorted(neighbor_chain_ids)))
            .order_by(SupplyChain.title.asc())
            .all()
        ):
            neighbors.append(
                {
                    "id": int(chain.id),
                    "slug": chain.slug,
                    "title": chain.title,
                    "category": chain.category,
                    "product": chain.product,
                    "href": f"/visualizations/supply-chains/{int(chain.id)}/command-center",
                }
            )

    return {
        "chains": chains,
        "neighbors": neighbors,
        "stage_entries": [
            {
                "id": int(row.id),
                "entry_key": row.entry_key,
                "company_name": row.company_name,
                "product_name": row.product_name,
                "stage": row.stage,
                "target_date": _date_like(row.target_date),
                "status": row.status,
                "updated_at": _dt_iso(row.updated_at),
                "href": f"/stage-tracker/{int(row.id)}",
                "linked_supply_chain_ids": [int(chain_id) for chain_id in (row.linked_supply_chain_ids or []) if str(chain_id).isdigit()],
                "linked_dataset_ids": [int(dataset_id) for dataset_id in (row.linked_dataset_ids or []) if str(dataset_id).isdigit()],
            }
            for row in stage_rows
        ],
    }


def _linked_datasets(session: Session, subject: ReportSubject, *, linked_stage_entries: list[dict[str, Any]], thesis_rows: list[dict[str, Any]], note_rows: list[dict[str, Any]], linked_chains: list[dict[str, Any]]) -> list[dict[str, Any]]:
    dataset_ids: set[int] = set()
    if subject.company_id is not None:
        company_ids = company_structured_fact_scope_ids(session, subject.company)
        rows = (
            session.query(StructuredDataFact.dataset_id)
            .filter(StructuredDataFact.company_id.in_(company_ids))
            .all()
            if company_ids
            else []
        )
        dataset_ids.update(int(row[0]) for row in rows if row and row[0] is not None)
    for stage in linked_stage_entries:
        dataset_ids.update(int(dataset_id) for dataset_id in (stage.get("linked_dataset_ids") or []) if str(dataset_id).isdigit())

    thesis_ids = [int(row["id"]) for row in thesis_rows]
    note_ids = [int(row["id"]) for row in note_rows]
    chain_ids = [int(row["id"]) for row in linked_chains]
    link_filters = []
    if thesis_ids:
        link_filters.append((DatasetLink.object_type == "thesis") & (DatasetLink.object_id.in_(thesis_ids)))
    if note_ids:
        link_filters.append((DatasetLink.object_type == "note") & (DatasetLink.object_id.in_(note_ids)))
    if chain_ids:
        link_filters.append((DatasetLink.object_type == "supply_chain") & (DatasetLink.object_id.in_(chain_ids)))
    if subject.company_id is not None:
        link_filters.append((DatasetLink.object_type == "company") & (DatasetLink.object_id == int(subject.company_id)))
    if subject.issuer_id is not None:
        link_filters.append((DatasetLink.object_type == "issuer") & (DatasetLink.object_id == int(subject.issuer_id)))
    if subject.subject_id is not None:
        link_filters.append((DatasetLink.object_type == "research_subject") & (DatasetLink.object_id == int(subject.subject_id)))
    if link_filters:
        for row in session.query(DatasetLink.dataset_id).filter(or_(*link_filters)).order_by(DatasetLink.id.asc()).all():
            if row and row[0] is not None:
                dataset_ids.add(int(row[0]))

    if not dataset_ids:
        return []
    return [
        {
            "id": int(row.id),
            "slug": row.slug,
            "title": row.title,
            "description": row.description,
            "family": row.family,
            "category": row.category,
            "status": row.status,
            "updated_at": _dt_iso(row.updated_at),
            "href": f"/visualizations/datasets/{int(row.id)}",
        }
        for row in (
            session.query(
                Dataset.id,
                Dataset.slug,
                Dataset.title,
                Dataset.description,
                Dataset.family,
                Dataset.category,
                Dataset.status,
                Dataset.updated_at,
            )
            .filter(Dataset.id.in_(sorted(dataset_ids)))
            .order_by(Dataset.updated_at.desc(), Dataset.id.desc())
            .all()
        )
    ]


def _lookup_subject_row(session: Session, subject: ReportSubject) -> ResearchSubject | None:
    if subject.subject_row is not None:
        return subject.subject_row
    if subject.subject_id is not None:
        return session.query(ResearchSubject).filter_by(id=int(subject.subject_id)).first()
    row = session.query(ResearchSubject).filter_by(subject_key=subject.subject_key).first()
    if row is not None:
        return row
    filters = []
    if subject.issuer_id is not None:
        filters.append(ResearchSubject.issuer_id == int(subject.issuer_id))
    if subject.company_id is not None:
        filters.append(ResearchSubject.company_id == int(subject.company_id))
    if subject.primary_entity_id is not None:
        filters.append(ResearchSubject.primary_entity_id == int(subject.primary_entity_id))
    if not filters:
        return None
    return session.query(ResearchSubject).filter(or_(*filters)).order_by(ResearchSubject.id.asc()).first()


def _latest_published_asset_version(session: Session, asset_id: int) -> KnowledgeAssetVersion | None:
    return (
        session.query(KnowledgeAssetVersion)
        .filter(KnowledgeAssetVersion.asset_id == int(asset_id), KnowledgeAssetVersion.status == "published")
        .order_by(KnowledgeAssetVersion.version.desc(), KnowledgeAssetVersion.id.desc())
        .first()
    )


def _knowledge_asset_version_projection(
    *,
    include_content: bool,
    excerpt_chars: int,
) -> list[Any]:
    columns: list[Any] = [
        KnowledgeAssetVersion.id,
        KnowledgeAssetVersion.asset_id,
        KnowledgeAssetVersion.version,
        KnowledgeAssetVersion.status,
        KnowledgeAssetVersion.summary,
        KnowledgeAssetVersion.as_of_at,
        KnowledgeAssetVersion.published_at,
        func.length(KnowledgeAssetVersion.content_md).label("content_chars"),
        func.substr(KnowledgeAssetVersion.content_md, 1, max(1, int(excerpt_chars))).label("content_excerpt"),
    ]
    if include_content:
        columns.extend(
            [
                KnowledgeAssetVersion.content_md,
                KnowledgeAssetVersion.content_json,
                KnowledgeAssetVersion.coverage_json,
            ]
        )
    return columns


def _knowledge_asset_sections(
    session: Session,
    subject: ReportSubject,
    *,
    include_content: bool = False,
    excerpt_chars: int = REPORT_KNOWLEDGE_ASSET_EXCERPT_CHARS,
) -> dict[str, list[dict[str, Any]]]:
    subject_row = _lookup_subject_row(session, subject)
    subject_ids: set[int] = set()
    relation_map: dict[int, str | None] = {}
    if subject_row is not None and subject_row.id is not None:
        subject_ids.add(int(subject_row.id))
        relation_rows = (
            session.query(
                ResearchSubjectRelation.source_subject_id,
                ResearchSubjectRelation.target_subject_id,
                ResearchSubjectRelation.relation_kind,
                ResearchSubjectRelation.reviewed,
            )
            .filter(
                or_(
                    ResearchSubjectRelation.source_subject_id == int(subject_row.id),
                    ResearchSubjectRelation.target_subject_id == int(subject_row.id),
                )
            )
            .order_by(ResearchSubjectRelation.updated_at.desc(), ResearchSubjectRelation.id.desc())
            .all()
        )
        for relation in relation_rows:
            if not bool(relation.reviewed):
                continue
            other_id = (
                int(relation.target_subject_id)
                if int(relation.source_subject_id) == int(subject_row.id)
                else int(relation.source_subject_id)
            )
            subject_ids.add(other_id)
            relation_map.setdefault(other_id, str(relation.relation_kind or "").strip() or None)
    if not subject_ids:
        return {"primers": [], "teardowns": []}

    out: dict[str, list[dict[str, Any]]] = {"primers": [], "teardowns": []}
    seen_assets: set[int] = set()
    assets = (
        session.query(
            KnowledgeAsset.id,
            KnowledgeAsset.subject_id,
            KnowledgeAsset.slug,
            KnowledgeAsset.kind,
            KnowledgeAsset.title,
            KnowledgeAsset.summary,
            KnowledgeAsset.latest_published_version_id,
        )
        .filter(KnowledgeAsset.subject_id.in_(sorted(subject_ids)))
        .order_by(KnowledgeAsset.updated_at.desc(), KnowledgeAsset.id.desc())
        .all()
    )
    asset_ids = [int(row.id) for row in assets]
    version_by_asset_id: dict[int, Any] = {}
    latest_version_ids = [
        int(row.latest_published_version_id)
        for row in assets
        if row.latest_published_version_id is not None
    ]
    if latest_version_ids:
        for version in (
            session.query(*_knowledge_asset_version_projection(include_content=include_content, excerpt_chars=excerpt_chars))
            .filter(
                KnowledgeAssetVersion.id.in_(sorted(set(latest_version_ids))),
                KnowledgeAssetVersion.status == "published",
            )
            .all()
        ):
            version_by_asset_id[int(version.asset_id)] = version
    missing_asset_ids = [asset_id for asset_id in asset_ids if asset_id not in version_by_asset_id]
    if missing_asset_ids:
        for version in (
            session.query(*_knowledge_asset_version_projection(include_content=include_content, excerpt_chars=excerpt_chars))
            .filter(
                KnowledgeAssetVersion.asset_id.in_(missing_asset_ids),
                KnowledgeAssetVersion.status == "published",
            )
            .order_by(KnowledgeAssetVersion.asset_id.asc(), KnowledgeAssetVersion.version.desc(), KnowledgeAssetVersion.id.desc())
            .all()
        ):
            version_by_asset_id.setdefault(int(version.asset_id), version)
    subject_rows = {
        int(row.id): row
        for row in (
            session.query(ResearchSubject.id, ResearchSubject.display_name, ResearchSubject.slug, ResearchSubject.kind)
            .filter(ResearchSubject.id.in_(sorted({int(row.subject_id) for row in assets})))
            .all()
        )
    }
    for asset in assets:
        if int(asset.id) in seen_assets:
            continue
        version = version_by_asset_id.get(int(asset.id))
        if version is None:
            continue
        seen_assets.add(int(asset.id))
        asset_subject = subject_rows.get(int(asset.subject_id))
        item = {
            "id": int(asset.id),
            "slug": asset.slug,
            "kind": asset.kind,
            "title": asset.title,
            "summary": asset.summary,
            "subject_id": int(asset.subject_id),
            "subject_display_name": asset_subject.display_name if asset_subject is not None else subject.display_name,
            "subject_slug": asset_subject.slug if asset_subject is not None else subject.slug,
            "subject_kind": asset_subject.kind if asset_subject is not None else subject.kind,
            "relation_kind": relation_map.get(int(asset.subject_id)),
            "version_id": int(version.id),
            "version": int(version.version),
            "published_at": _dt_iso(version.published_at),
            "as_of_at": _dt_iso(version.as_of_at),
            "summary_md": version.summary,
            "content_chars": int(version.content_chars or 0),
            "content_excerpt": str(version.content_excerpt or ""),
            "content_omitted": not include_content,
            "href": f"/knowledge/{asset.slug}",
        }
        if include_content:
            item["content_md"] = str(version.content_md or "")
            item["content_json"] = dict(version.content_json or {})
            item["coverage_json"] = dict(version.coverage_json or {})
        bucket = "primers" if str(asset.kind or "").strip().lower() == "primer" else "teardowns"
        out[bucket].append(item)
    return out


def _subject_facts(
    session: Session,
    subject: ReportSubject,
    *,
    structured: dict[str, Any],
) -> dict[str, Any]:
    subject_row = _lookup_subject_row(session, subject)
    related_companies: list[dict[str, Any]] = []
    taxonomy: list[dict[str, Any]] = []
    if subject_row is not None and subject_row.id is not None:
        relation_rows = (
            session.query(
                ResearchSubjectRelation.source_subject_id,
                ResearchSubjectRelation.target_subject_id,
                ResearchSubjectRelation.relation_kind,
                ResearchSubject.id.label("related_subject_id"),
                ResearchSubject.slug,
                ResearchSubject.display_name,
                ResearchSubject.kind,
            )
            .join(
                ResearchSubject,
                or_(
                    ResearchSubject.id == ResearchSubjectRelation.source_subject_id,
                    ResearchSubject.id == ResearchSubjectRelation.target_subject_id,
                ),
            )
            .filter(
                or_(
                    ResearchSubjectRelation.source_subject_id == int(subject_row.id),
                    ResearchSubjectRelation.target_subject_id == int(subject_row.id),
                )
            )
            .all()
        )
        seen_related: set[int] = set()
        for row in relation_rows:
            related_id = int(row.related_subject_id)
            if related_id == int(subject_row.id) or related_id in seen_related:
                continue
            seen_related.add(related_id)
            item = {
                "subject_id": related_id,
                "slug": row.slug,
                "display_name": row.display_name,
                "kind": row.kind,
                "relation_kind": row.relation_kind,
                "href": f"/reports/{row.slug}",
            }
            if row.kind == "company":
                related_companies.append(item)
            else:
                taxonomy.append(item)

    if subject.kind == "company":
        issuer = subject.issuer
        company = subject.company
        latest_actuals = (structured.get("actuals") or [])[:12]
        latest_guidance = (structured.get("guidance") or [])[:12]
        return {
            "kind": "company",
            "definition": subject.description,
            "sector": getattr(issuer, "sector", None) or getattr(company, "sector", None),
            "industry": getattr(issuer, "industry", None) or getattr(company, "industry", None),
            "jurisdiction": getattr(issuer, "jurisdiction", None),
            "financial_history": latest_actuals,
            "guidance_history": latest_guidance,
            "metrics": list(structured.get("metrics") or []),
            "related_subjects": taxonomy,
            "related_companies": related_companies,
        }

    related_companies.sort(key=lambda item: (item.get("relation_kind") or "", item.get("display_name") or ""))
    taxonomy.sort(key=lambda item: (item.get("relation_kind") or "", item.get("display_name") or ""))
    return {
        "kind": subject.kind,
        "definition": subject.description,
        "taxonomy": taxonomy,
        "exposed_companies": related_companies,
        "metrics": list(structured.get("metrics") or []),
    }


def _mention_appendix(session: Session, subject: ReportSubject) -> dict[str, Any]:
    mentions: list[dict[str, Any]] = []
    seen: set[str] = set()
    evidence_query = (
        session.query(
            DiscordEvidenceItem.id.label("evidence_id"),
            DiscordEvidenceItem.title,
            DiscordEvidenceItem.url_canonical,
            DiscordEvidenceItem.url_raw,
            DiscordEvidenceItem.created_at,
            DiscordEvidenceItem.source_type,
            DiscordEvidenceTarget.confidence,
            DiscordEvidenceTarget.resolution_method,
            DiscordEvidenceTarget.target_label,
        )
        .join(DiscordEvidenceTarget, DiscordEvidenceTarget.evidence_id == DiscordEvidenceItem.id)
        .order_by(DiscordEvidenceItem.created_at.desc(), DiscordEvidenceTarget.confidence.desc().nullslast())
    )
    target_filters = []
    if subject.issuer_id is not None:
        target_filters.append(DiscordEvidenceTarget.issuer_id == int(subject.issuer_id))
    if subject.company_id is not None:
        target_filters.append(DiscordEvidenceTarget.company_id == int(subject.company_id))
    if subject.primary_ticker:
        target_filters.append(func.upper(DiscordEvidenceTarget.symbol) == str(subject.primary_ticker).upper())
    if target_filters:
        rows = (
            evidence_query
            .filter(or_(*target_filters))
            .filter(func.coalesce(DiscordEvidenceTarget.confidence, 0.0) >= MENTION_CONFIDENCE_MIN)
            .all()
        )
        for row in rows:
            confidence = float(row.confidence or 0.0)
            if confidence < MENTION_CONFIDENCE_MIN:
                continue
            key = f"discord:{int(row.evidence_id)}"
            if key in seen:
                continue
            seen.add(key)
            mentions.append(
                {
                    "kind": "discord_evidence",
                    "id": int(row.evidence_id),
                    "title": row.title,
                    "url": row.url_canonical or row.url_raw,
                    "published_at": _dt_iso(row.created_at),
                    "confidence": confidence,
                    "provenance_label": row.source_type,
                    "resolution_method": row.resolution_method,
                    "target_label": row.target_label,
                }
            )

    entity_ids = _entity_ids_for_subject(session, subject)
    if entity_ids:
        rows = (
            session.query(
                Document.id.label("document_id"),
                Document.title,
                Document.url,
                Document.canonical_url,
                Document.published_at,
                Document.ingested_at,
                Entity.id.label("entity_id"),
                Entity.canonical_name,
                DocumentEntity.method,
                DocumentEntity.confidence,
            )
            .join(Document, Document.id == DocumentEntity.document_id)
            .join(Entity, Entity.id == DocumentEntity.entity_id)
            .filter(DocumentEntity.entity_id.in_(entity_ids))
            .filter(func.coalesce(DocumentEntity.confidence, 0.0) >= DOCUMENT_ENTITY_CONFIDENCE_MIN)
            .order_by(Document.published_at.desc().nullslast(), Document.id.desc())
            .all()
        )
        for row in rows:
            confidence = float(row.confidence or 0.0)
            if confidence < DOCUMENT_ENTITY_CONFIDENCE_MIN:
                continue
            key = f"document:{int(row.document_id)}:{int(row.entity_id)}"
            if key in seen:
                continue
            seen.add(key)
            mentions.append(
                {
                    "kind": "document_entity",
                    "id": int(row.document_id),
                    "title": row.title or row.url or f"doc {row.document_id}",
                    "url": row.url or row.canonical_url,
                    "href": f"/docs/{int(row.document_id)}",
                    "published_at": _dt_iso(row.published_at or row.ingested_at),
                    "confidence": confidence,
                    "provenance_label": row.canonical_name,
                    "resolution_method": row.method,
                }
            )

    mentions.sort(key=lambda item: (item.get("published_at") or "", float(item.get("confidence") or 0.0)), reverse=True)
    return {"rows": mentions, "count": len(mentions)}


def _coverage_panel(
    *,
    subject: ReportSubject,
    primary_sources: dict[str, Any],
    knowledge_assets: dict[str, list[dict[str, Any]]],
    theses: list[dict[str, Any]],
    notes: list[dict[str, Any]],
    structured: dict[str, Any],
    transcripts: dict[str, Any],
    supply_chain: dict[str, Any],
    datasets: list[dict[str, Any]],
) -> dict[str, Any]:
    rail1 = len(primary_sources.get("filings") or [])
    rail2 = len(primary_sources.get("disclosures") or [])
    rail3 = len(primary_sources.get("ir_artifacts") or [])
    transcript_count = int(transcripts.get("count") or 0)
    transcript_summary_count = int(transcripts.get("summary_count") or 0)
    actual_count = len(structured.get("actuals") or [])
    guidance_count = len(structured.get("guidance") or [])
    supply_chain_count = len(supply_chain.get("chains") or [])
    stage_count = len(supply_chain.get("stage_entries") or [])
    dataset_count = len(datasets or [])
    primer_count = len(knowledge_assets.get("primers") or [])
    teardown_count = len(knowledge_assets.get("teardowns") or [])
    gaps: list[str] = []
    if subject.kind == "company" and rail1 == 0:
        gaps.append("No rail 1 statutory filings are linked yet.")
    if subject.kind == "company" and rail2 == 0:
        gaps.append("No rail 2 exchange disclosures are linked yet.")
    if subject.kind == "company" and rail3 == 0:
        gaps.append("No rail 3 IR artifacts are linked yet.")
    if primer_count == 0:
        gaps.append("No published primer is linked to this subject or its reviewed neighbors.")
    if teardown_count == 0:
        gaps.append("No published teardown is linked to this subject or its reviewed neighbors.")
    if subject.kind == "company" and actual_count == 0:
        gaps.append("Structured financial actuals are missing.")
    if subject.kind == "company" and guidance_count == 0:
        gaps.append("Structured guidance history is missing.")
    if subject.kind == "company" and transcript_count == 0:
        gaps.append("No transcript archive is linked yet.")
    elif subject.kind == "company" and transcript_summary_count < transcript_count:
        gaps.append(f"Transcript coverage is partial: {transcript_summary_count}/{transcript_count} summaries exist.")
    if supply_chain_count == 0:
        gaps.append("No linked published supply-chain snapshot is attached.")
    if stage_count == 0:
        gaps.append("No product stage tracker entries are linked.")
    if dataset_count == 0:
        gaps.append("No linked datasets are attached.")
    if not theses:
        gaps.append("No thesis ledger rows resolved for this subject.")
    if not notes:
        gaps.append("No note ledger rows resolved for this subject.")
    return {
        "subject_key": subject.subject_key,
        "metrics": [
            {"label": "Rail 1 filings", "value": rail1},
            {"label": "Rail 2 disclosures", "value": rail2},
            {"label": "Rail 3 IR artifacts", "value": rail3},
            {"label": "Primers", "value": primer_count},
            {"label": "Teardowns", "value": teardown_count},
            {"label": "Theses", "value": len(theses)},
            {"label": "Notes", "value": len(notes)},
            {"label": "Structured actuals", "value": actual_count},
            {"label": "Guidance facts", "value": guidance_count},
            {"label": "Transcripts", "value": transcript_count},
            {"label": "Transcript summaries", "value": transcript_summary_count},
            {"label": "Supply chains", "value": supply_chain_count},
            {"label": "Stage entries", "value": stage_count},
            {"label": "Datasets", "value": dataset_count},
        ],
        "gaps": gaps,
    }


def _identity_panel(session: Session, subject: ReportSubject, *, report: ResearchReport | None, latest_version: ResearchReportVersion | None) -> dict[str, Any]:
    next_earnings = None
    if subject.issuer_id is not None:
        today = utcnow().date()
        status_expr = func.lower(func.coalesce(EarningsEvent.status, ""))
        next_event = (
            session.query(EarningsEvent)
            .filter(
                EarningsEvent.issuer_id == int(subject.issuer_id),
                EarningsEvent.scheduled_date.isnot(None),
                EarningsEvent.scheduled_date >= today,
                ~status_expr.in_(("cancelled", "canceled", "postponed")),
                or_(status_expr != "completed", EarningsEvent.scheduled_date > today),
            )
            .order_by(EarningsEvent.scheduled_date.asc(), EarningsEvent.id.asc())
            .first()
        )
        if next_event is not None:
            next_earnings = {
                "scheduled_date": _date_like(next_event.scheduled_date),
                "scheduled_time": next_event.scheduled_time,
                "date_provenance": next_event.date_provenance,
            }
    return {
        "display_name": subject.display_name,
        "legal_name": subject.legal_name,
        "kind": subject.kind,
        "description": subject.description,
        "slug": subject.slug,
        "subject_key": subject.subject_key,
        "subject_id": subject.subject_id,
        "issuer_id": subject.issuer_id,
        "company_id": subject.company_id,
        "primary_entity_id": subject.primary_entity_id,
        "primary_ticker": subject.primary_ticker,
        "tickers": subject.tickers,
        "aliases": subject.aliases,
        "latest_report_version": int(latest_version.version) if latest_version is not None else None,
        "latest_report_published_at": _dt_iso(latest_version.published_at if latest_version is not None else None),
        "report_id": int(report.id) if report is not None and report.id is not None else None,
        "next_earnings": next_earnings,
    }


def _artifact_entries(version: ResearchReportVersion) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for row in sorted(version.artifacts or [], key=lambda item: (item.artifact_type, item.variant, item.id)):
        entries.append(
            {
                "id": int(row.id),
                "artifact_type": row.artifact_type,
                "variant": row.variant,
                "label": row.label,
                "object_path": row.object_path,
                "content_type": row.content_type,
                "object_url": f"/objects/{row.object_path}",
                "payload": dict(row.payload or {}),
                "created_at": _dt_iso(row.created_at),
            }
        )
    return entries


def _render_markdown(payload: dict[str, Any]) -> str:
    subject = payload.get("subject") or {}
    identity = payload.get("identity_freshness") or {}
    coverage = payload.get("coverage_panel") or {}
    source_pack = payload.get("official_source_pack") or {}
    canon_scenario = payload.get("canon_scenario") or {}
    agent_card = payload.get("agent_canon_card") or {}
    primers = payload.get("published_primers") or []
    teardowns = payload.get("published_teardowns") or []
    theses = payload.get("thesis_ledger") or []
    notes = payload.get("note_ledger") or []
    primary_sources = payload.get("primary_source_timeline") or {}
    subject_facts = payload.get("subject_facts") or {}
    transcripts = payload.get("transcript_archive_summaries") or {}
    supply_chain = payload.get("supply_chain_peer_context") or {}
    datasets = payload.get("linked_datasets_stage_trackers") or {}
    mentions = payload.get("high_confidence_mention_appendix") or {}
    artifacts = payload.get("artifacts_export_appendix") or {}

    lines: list[str] = [f"# Research Report — {subject.get('display_name') or subject.get('slug') or 'Subject'}", ""]
    lines.append("## Identity / Freshness")
    lines.append("")
    lines.append(f"- Subject type: `{identity.get('kind') or subject.get('kind') or 'unknown'}`")
    if payload.get("window_days"):
        lines.append(f"- Timeline depth window: `{payload.get('window_days')} days`")
    if identity.get("primary_ticker"):
        lines.append(f"- Primary ticker: `{identity.get('primary_ticker')}`")
    if identity.get("tickers"):
        lines.append(f"- Tickers: `{', '.join(identity.get('tickers') or [])}`")
    if identity.get("description"):
        lines.append(f"- Description: {identity.get('description')}")
    if identity.get("latest_report_version") is not None:
        lines.append(f"- Latest published version: `{identity.get('latest_report_version')}`")
    if identity.get("latest_report_published_at"):
        lines.append(f"- Latest published at: `{identity.get('latest_report_published_at')}`")
    if identity.get("next_earnings"):
        next_earnings = identity.get("next_earnings") or {}
        lines.append(
            f"- Next earnings: `{next_earnings.get('scheduled_date') or 'unknown'}`"
            + (f" ({next_earnings.get('date_provenance')})" if next_earnings.get("date_provenance") else "")
        )
    lines.append("")

    lines.append("## Canon Scenario")
    lines.append("")
    if canon_scenario:
        lines.append(f"- Name: {canon_scenario.get('name')} (`{canon_scenario.get('scenario_kind')}`)")
        if canon_scenario.get("probability") is not None:
            lines.append(f"- Probability: `{canon_scenario.get('probability')}`")
        if canon_scenario.get("confidence") is not None:
            lines.append(f"- Confidence: `{canon_scenario.get('confidence')}`")
        if canon_scenario.get("thesis_summary"):
            lines.append(f"- Thesis: {canon_scenario.get('thesis_summary')}")
        if canon_scenario.get("invalidation_triggers"):
            lines.append("- Invalidation triggers:")
            for item in (canon_scenario.get("invalidation_triggers") or [])[:8]:
                lines.append(f"  - {item}")
    else:
        lines.append("- None selected.")
    lines.append("")

    lines.append("## Agent Canon Card")
    lines.append("")
    if agent_card:
        lines.append(f"- Status: `{agent_card.get('status') or 'unknown'}`")
        lines.append(f"- Token estimate: `{agent_card.get('token_estimate') or 0}`")
        if agent_card.get("generated_at"):
            lines.append(f"- Generated at: `{agent_card.get('generated_at')}`")
    else:
        lines.append("- None.")
    lines.append("")

    lines.append("## Published Primers")
    lines.append("")
    if primers:
        for row in primers:
            qualifier = ""
            if row.get("relation_kind"):
                qualifier = f" [{row.get('relation_kind')}]"
            lines.append(
                f"- {row.get('title')} — `{row.get('subject_display_name')}`{qualifier} "
                f"(v`{row.get('version')}`, as_of=`{row.get('as_of_at')}`)"
            )
            summary = _clean_text(row.get("summary") or row.get("summary_md"))
            if summary:
                lines.append(f"  - {summary[:320]}")
    else:
        lines.append("- None.")
    lines.append("")

    lines.append("## Published Teardowns")
    lines.append("")
    if teardowns:
        for row in teardowns:
            qualifier = ""
            if row.get("relation_kind"):
                qualifier = f" [{row.get('relation_kind')}]"
            lines.append(
                f"- {row.get('title')} — `{row.get('subject_display_name')}`{qualifier} "
                f"(v`{row.get('version')}`, as_of=`{row.get('as_of_at')}`)"
            )
            summary = _clean_text(row.get("summary") or row.get("summary_md"))
            if summary:
                lines.append(f"  - {summary[:320]}")
    else:
        lines.append("- None.")
    lines.append("")

    lines.append("## Thesis Ledger")
    lines.append("")
    if theses:
        for row in theses:
            lines.append(f"- {row.get('updated_at') or 'unknown'} — {row.get('title')} (status=`{row.get('status')}`, importance=`{row.get('importance')}`)")
    else:
        lines.append("- None.")
    lines.append("")

    lines.append("## Note Ledger")
    lines.append("")
    if notes:
        for row in notes:
            lines.append(f"- {row.get('updated_at') or 'unknown'} — {row.get('title')} (type=`{row.get('note_type')}`, status=`{row.get('status')}`)")
    else:
        lines.append("- None.")
    lines.append("")

    lines.append("## Subject Facts")
    lines.append("")
    if subject_facts.get("definition"):
        lines.append(f"- Definition: {subject_facts.get('definition')}")
    if subject_facts.get("sector"):
        lines.append(f"- Sector: {subject_facts.get('sector')}")
    if subject_facts.get("industry"):
        lines.append(f"- Industry: {subject_facts.get('industry')}")
    if subject_facts.get("jurisdiction"):
        lines.append(f"- Jurisdiction: {subject_facts.get('jurisdiction')}")
    if subject_facts.get("metrics"):
        lines.append(f"- Metrics tracked: {', '.join(subject_facts.get('metrics') or [])}")
    if subject_facts.get("financial_history"):
        lines.append("")
        lines.append("Financial history:")
        for row in subject_facts.get("financial_history")[:60]:
            value = row.get("value_number")
            if value is None:
                value = row.get("value_text")
            lines.append(f"- {row.get('period_label') or row.get('as_of_date') or 'unknown'} — {row.get('metric_label')}: `{value}` {(row.get('currency') or row.get('unit') or '').strip()}".rstrip())
    if subject_facts.get("guidance_history"):
        lines.append("")
        lines.append("Guidance history:")
        for row in subject_facts.get("guidance_history")[:60]:
            value = row.get("value_number")
            if value is None:
                value = row.get("value_text")
            lines.append(f"- {row.get('period_label') or row.get('as_of_date') or 'unknown'} — {row.get('metric_label')}: `{value}` {(row.get('currency') or row.get('unit') or '').strip()}".rstrip())
    if subject_facts.get("taxonomy"):
        lines.append("")
        lines.append("Taxonomy / related subjects:")
        for row in subject_facts.get("taxonomy")[:40]:
            lines.append(f"- {row.get('display_name')} ({row.get('kind')}, relation=`{row.get('relation_kind')}`)")
    if subject_facts.get("related_subjects"):
        lines.append("")
        lines.append("Related subjects:")
        for row in subject_facts.get("related_subjects")[:40]:
            lines.append(f"- {row.get('display_name')} ({row.get('kind')}, relation=`{row.get('relation_kind')}`)")
    if subject_facts.get("related_companies"):
        lines.append("")
        lines.append("Related companies:")
        for row in subject_facts.get("related_companies")[:40]:
            lines.append(f"- {row.get('display_name')} (relation=`{row.get('relation_kind')}`)")
    if subject_facts.get("exposed_companies"):
        lines.append("")
        lines.append("Exposed companies:")
        for row in subject_facts.get("exposed_companies")[:40]:
            lines.append(f"- {row.get('display_name')} (relation=`{row.get('relation_kind')}`)")
    if not any(subject_facts.get(key) for key in ["definition", "financial_history", "guidance_history", "taxonomy", "related_subjects", "related_companies", "exposed_companies"]):
        lines.append("- None.")
    lines.append("")

    lines.append("## Transcript Archive + Summaries")
    lines.append("")
    if transcripts.get("rows"):
        for row in transcripts.get("rows") or []:
            lines.append(f"- {row.get('published_at') or 'unknown'} — {row.get('period_label') or row.get('title') or 'Transcript'}")
            summary_payload = row.get("summary_payload") or {}
            overview = _clean_text(summary_payload.get("overview"))
            if overview:
                lines.append(f"  - {overview}")
            for highlight in (summary_payload.get("highlights") or [])[:4]:
                lines.append(f"  - {highlight}")
    else:
        lines.append("- None.")
    lines.append("")

    lines.append("## Supply Chain / Peer Context")
    lines.append("")
    if supply_chain.get("chains"):
        for row in supply_chain.get("chains") or []:
            counts = row.get("counts") or {}
            lines.append(f"- {row.get('title')} — nodes=`{counts.get('nodes', 0)}` edges=`{counts.get('edges', 0)}`")
    else:
        lines.append("- No linked supply chains.")
    if supply_chain.get("neighbors"):
        lines.append("")
        lines.append("Explicit neighbors:")
        for row in supply_chain.get("neighbors") or []:
            lines.append(f"- {row.get('title')}")
    lines.append("")

    lines.append("## Linked Datasets / Stage Trackers")
    lines.append("")
    for row in datasets.get("stage_entries") or []:
        lines.append(f"- Stage: {row.get('company_name')} / {row.get('product_name')} / {row.get('stage')}")
    for row in datasets.get("datasets") or []:
        lines.append(f"- Dataset: {row.get('title')} (`{row.get('family')}`)")
    if not (datasets.get("stage_entries") or []) and not (datasets.get("datasets") or []):
        lines.append("- None.")
    lines.append("")

    lines.append("## High-Confidence Mention Appendix")
    lines.append("")
    if mentions.get("rows"):
        for row in mentions.get("rows") or []:
            lines.append(f"- {row.get('published_at') or 'unknown'} — {row.get('title')} (confidence=`{row.get('confidence')}`, provenance=`{row.get('provenance_label')}`)")
    else:
        lines.append("- None.")
    lines.append("")

    lines.append("## Artifacts / Export Appendix")
    lines.append("")
    if artifacts.get("rows"):
        for row in artifacts.get("rows") or []:
            lines.append(f"- {row.get('artifact_type')} / {row.get('variant')} — `{row.get('object_path')}`")
    else:
        lines.append("- Pending artifact persistence.")
    lines.append("")

    lines.append("## Appendix")
    lines.append("")

    lines.append("### Coverage Panel")
    lines.append("")
    for row in coverage.get("metrics") or []:
        lines.append(f"- {row.get('label')}: `{row.get('value')}`")
    if coverage.get("gaps"):
        lines.append("")
        lines.append("Coverage gaps:")
        for gap in coverage.get("gaps") or []:
            lines.append(f"- {gap}")
    if not coverage.get("metrics") and not coverage.get("gaps"):
        lines.append("- None.")
    lines.append("")

    lines.append("### Official Source Pack")
    lines.append("")
    if source_pack:
        lines.append(f"- Status: `{source_pack.get('status') or 'unknown'}`")
        missing = source_pack.get("missing_items") or []
        if missing:
            lines.append(f"- Missing: {', '.join(missing)}")
        for row in (source_pack.get("checklist") or [])[:12]:
            marker = "present" if row.get("present") else "missing"
            lines.append(f"- {row.get('label') or row.get('key')}: `{marker}`")
    else:
        lines.append("- Not evaluated.")
    lines.append("")

    lines.append("### Primary Source Timeline")
    lines.append("")
    if primary_sources.get("timeline"):
        for row in primary_sources.get("timeline") or []:
            lines.append(f"- {row.get('published_at') or 'unknown'} — {row.get('rail')} / {row.get('label')} — {row.get('title')}")
    else:
        lines.append("- None.")
    lines.append("")
    return "\n".join(lines).strip() + "\n"


def _render_print_html(payload: dict[str, Any], *, image_map: dict[str, bytes] | None = None) -> str:
    image_map = image_map or {}
    subject = payload.get("subject") or {}
    coverage = payload.get("coverage_panel") or {}
    source_pack = payload.get("official_source_pack") or {}
    canon_scenario = payload.get("canon_scenario") or {}
    agent_card = payload.get("agent_canon_card") or {}
    theses = payload.get("thesis_ledger") or []
    notes = payload.get("note_ledger") or []
    primary_sources = payload.get("primary_source_timeline") or {}
    primers = payload.get("published_primers") or []
    teardowns = payload.get("published_teardowns") or []
    subject_facts = payload.get("subject_facts") or {}
    transcripts = payload.get("transcript_archive_summaries") or {}
    supply_chain = payload.get("supply_chain_peer_context") or {}
    datasets = payload.get("linked_datasets_stage_trackers") or {}
    mentions = payload.get("high_confidence_mention_appendix") or {}
    artifacts = payload.get("artifacts_export_appendix") or {}

    def esc(value: object) -> str:
        return str(value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def list_items(rows: list[str]) -> str:
        if not rows:
            return "<p class='muted'>None.</p>"
        return "<ul>" + "".join(f"<li>{esc(row)}</li>" for row in rows) + "</ul>"

    metric_items = [f"{row.get('label')}: {row.get('value')}" for row in coverage.get("metrics") or []]
    source_pack_items = [
        f"{row.get('label') or row.get('key')}: {'present' if row.get('present') else 'missing'}"
        for row in source_pack.get("checklist") or []
    ]
    if source_pack.get("status"):
        source_pack_items.insert(0, f"Status: {source_pack.get('status')}")
    if source_pack.get("missing_items"):
        source_pack_items.append(f"Missing: {', '.join(source_pack.get('missing_items') or [])}")
    scenario_items = []
    if canon_scenario:
        scenario_items.append(f"Name: {canon_scenario.get('name')} ({canon_scenario.get('scenario_kind')})")
        if canon_scenario.get("thesis_summary"):
            scenario_items.append(f"Thesis: {canon_scenario.get('thesis_summary')}")
        for item in (canon_scenario.get("invalidation_triggers") or [])[:6]:
            scenario_items.append(f"Invalidation: {item}")
    card_items = []
    if agent_card:
        card_items.extend(
            [
                f"Status: {agent_card.get('status') or 'unknown'}",
                f"Token estimate: {agent_card.get('token_estimate') or 0}",
                f"Generated at: {agent_card.get('generated_at') or 'unknown'}",
            ]
        )
    primer_items = [
        f"{row.get('title')} · {row.get('subject_display_name')} · v{row.get('version')} · as_of={row.get('as_of_at')}"
        for row in primers
    ]
    teardown_items = [
        f"{row.get('title')} · {row.get('subject_display_name')} · v{row.get('version')} · as_of={row.get('as_of_at')}"
        for row in teardowns
    ]
    thesis_items = [f"{row.get('updated_at') or 'unknown'} · {row.get('title')}" for row in theses]
    note_items = [f"{row.get('updated_at') or 'unknown'} · {row.get('title')}" for row in notes]
    source_items = [f"{row.get('published_at') or 'unknown'} · {row.get('rail')} · {row.get('title')}" for row in primary_sources.get("timeline") or []]
    subject_fact_items = []
    if subject_facts.get("definition"):
        subject_fact_items.append(f"Definition: {subject_facts.get('definition')}")
    if subject_facts.get("sector"):
        subject_fact_items.append(f"Sector: {subject_facts.get('sector')}")
    if subject_facts.get("industry"):
        subject_fact_items.append(f"Industry: {subject_facts.get('industry')}")
    if subject_facts.get("metrics"):
        subject_fact_items.append(f"Metrics: {', '.join(subject_facts.get('metrics') or [])}")
    actual_items = []
    for row in subject_facts.get("financial_history") or []:
        value = row.get("value_number")
        if value is None:
            value = row.get("value_text")
        actual_items.append(f"{row.get('period_label') or row.get('as_of_date') or 'unknown'} · {row.get('metric_label')} · {value}")
    guidance_items = []
    for row in subject_facts.get("guidance_history") or []:
        value = row.get("value_number")
        if value is None:
            value = row.get("value_text")
        guidance_items.append(f"{row.get('period_label') or row.get('as_of_date') or 'unknown'} · {row.get('metric_label')} · {value}")
    transcript_html = []
    for row in transcripts.get("rows") or []:
        summary_payload = row.get("summary_payload") or {}
        highlights = "".join(f"<li>{esc(item)}</li>" for item in (summary_payload.get("highlights") or [])[:4])
        transcript_html.append(
            "<div class='card'>"
            f"<div class='card-title'>{esc(row.get('period_label') or row.get('title') or 'Transcript')}</div>"
            f"<div class='muted'>{esc(row.get('published_at') or '')}</div>"
            f"<p>{esc(summary_payload.get('overview') or '')}</p>"
            + (f"<ul>{highlights}</ul>" if highlights else "")
            + "</div>"
        )
    chain_blocks = []
    for row in supply_chain.get("chains") or []:
        slug = str(row.get("slug") or "").strip()
        image_html = ""
        if slug and slug in image_map:
            encoded = base64.b64encode(image_map[slug]).decode("ascii")
            image_html = f"<img class='chain-image' src='data:image/png;base64,{encoded}' alt='{esc(row.get('title'))}' />"
        counts = row.get("counts") or {}
        chain_blocks.append(
            "<div class='card'>"
            f"<div class='card-title'>{esc(row.get('title'))}</div>"
            f"<div class='muted'>nodes={esc(counts.get('nodes'))} edges={esc(counts.get('edges'))}</div>"
            f"{image_html}"
            "</div>"
        )
    stage_items = [f"{row.get('company_name')} · {row.get('product_name')} · {row.get('stage')}" for row in datasets.get("stage_entries") or []]
    dataset_items = [f"{row.get('title')} ({row.get('family')})" for row in datasets.get("datasets") or []]
    mention_items = [f"{row.get('published_at') or 'unknown'} · {row.get('title')} · conf={row.get('confidence')}" for row in mentions.get("rows") or []]
    artifact_items = [f"{row.get('artifact_type')} / {row.get('variant')} / {row.get('object_path')}" for row in artifacts.get("rows") or []]

    return f"""
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>Research Report - {esc(subject.get("display_name"))}</title>
    <style>
      body {{ font-family: Georgia, "Times New Roman", serif; color: #121212; margin: 32px; line-height: 1.45; }}
      h1 {{ margin: 0 0 8px; font-size: 30px; }}
      h2 {{ margin: 28px 0 10px; font-size: 20px; border-bottom: 1px solid #d9d9d9; padding-bottom: 6px; }}
      h3 {{ margin: 18px 0 8px; font-size: 16px; }}
      .lede {{ color: #555; margin-bottom: 18px; }}
      .grid {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 12px; }}
      .card {{ border: 1px solid #ddd; border-radius: 10px; padding: 12px 14px; margin-bottom: 10px; break-inside: avoid; }}
      .card-title {{ font-weight: 700; margin-bottom: 4px; }}
      .muted {{ color: #666; font-size: 12px; }}
      ul {{ margin: 8px 0 0 18px; padding: 0; }}
      li {{ margin: 4px 0; }}
      .chain-image {{ width: 100%; margin-top: 10px; border: 1px solid #eee; border-radius: 8px; }}
    </style>
  </head>
  <body>
    <h1>{esc(subject.get("display_name") or subject.get("slug") or "Research Report")}</h1>
    <div class="lede">{esc(subject.get("legal_name") or subject.get("description") or "")}{" · " + esc(subject.get("primary_ticker")) if subject.get("primary_ticker") else ""}</div>
    <h2>Identity / Freshness</h2>
    {list_items([f"Subject type: {subject.get('kind')}" if subject.get("kind") else "", f"Primary ticker: {subject.get('primary_ticker')}" if subject.get("primary_ticker") else "", f"Tickers: {', '.join(subject.get('tickers') or [])}" if subject.get("tickers") else "", f"Subject key: {subject.get('subject_key')}" if subject.get("subject_key") else "", f"Window: {payload.get('window_days')} days" if payload.get("window_days") else ""])}
    <h2>Canon Scenario</h2>
    {list_items(scenario_items)}
    <h2>Agent Canon Card</h2>
    {list_items(card_items)}
    <h2>Published Primers</h2>
    {list_items(primer_items)}
    <h2>Published Teardowns</h2>
    {list_items(teardown_items)}
    <h2>Thesis Ledger</h2>
    {list_items(thesis_items)}
    <h2>Note Ledger</h2>
    {list_items(note_items)}
    <h2>Subject Facts</h2>
    <div class="grid">
      <div class="card"><div class="card-title">Overview</div>{list_items(subject_fact_items)}</div>
      <div class="card"><div class="card-title">Actuals</div>{list_items(actual_items)}</div>
      <div class="card"><div class="card-title">Guidance</div>{list_items(guidance_items)}</div>
    </div>
    <h2>Transcript Archive + Summaries</h2>
    {''.join(transcript_html) or "<p class='muted'>None.</p>"}
    <h2>Supply Chain / Peer Context</h2>
    {''.join(chain_blocks) or "<p class='muted'>No linked supply chains.</p>"}
    <h2>Linked Datasets / Stage Trackers</h2>
    <div class="grid">
      <div class="card"><div class="card-title">Stage Entries</div>{list_items(stage_items)}</div>
      <div class="card"><div class="card-title">Datasets</div>{list_items(dataset_items)}</div>
    </div>
    <h2>High-Confidence Mention Appendix</h2>
    {list_items(mention_items)}
    <h2>Artifacts / Export Appendix</h2>
    {list_items(artifact_items)}
    <h2>Appendix</h2>
    <h3>Coverage Panel</h3>
    {list_items(metric_items + [f"GAP: {gap}" for gap in (coverage.get("gaps") or [])])}
    <h3>Official Source Pack</h3>
    {list_items(source_pack_items)}
    <h3>Primary Source Timeline</h3>
    {list_items(source_items)}
  </body>
</html>
    """.strip()


def _exports_dir() -> Path:
    path = settings.data_dir / "exports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _persist_export_bundle(version: ResearchReportVersion, *, payload: dict[str, Any], markdown_text: str) -> str:
    report = version.report
    ts = utcnow().strftime("%Y%m%d_%H%M%S")
    subject_slug = _slugify(report.slug or f"report-{report.id}", default=f"report-{report.id}")
    out_dir = _exports_dir() / f"research_report_{subject_slug}_v{int(version.version)}_{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)
    index_payload = {
        "kind": "research_report",
        "report_id": int(report.id),
        "report_version_id": int(version.id),
        "slug": report.slug,
        "version": int(version.version),
        "published_at": _dt_iso(version.published_at),
        "artifacts": payload.get("artifacts_export_appendix", {}).get("rows", []),
        "subject": payload.get("subject"),
    }
    (out_dir / "index.json").write_text(json.dumps(index_payload, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / "bundle.md").write_text(markdown_text, encoding="utf-8")
    return out_dir.name


def _upsert_artifact(
    session: Session,
    *,
    version: ResearchReportVersion,
    artifact_type: str,
    variant: str,
    label: str | None,
    data_bytes: bytes,
    content_type: str,
    payload: dict[str, Any] | None = None,
) -> ResearchReportArtifact:
    ext = {"application/json": ".json", "text/markdown": ".md", "application/pdf": ".pdf", "image/png": ".png"}.get(content_type, "")
    object_path, _sha = put_bytes(data_bytes, ext=ext)
    row = (
        session.query(ResearchReportArtifact)
        .filter_by(report_version_id=int(version.id), artifact_type=str(artifact_type), variant=str(variant))
        .first()
    )
    if row is None:
        row = ResearchReportArtifact(
            report_version_id=int(version.id),
            artifact_type=str(artifact_type),
            variant=str(variant),
            label=label,
            object_path=object_path,
            content_type=content_type,
            payload=dict(payload or {}),
            created_at=utcnow(),
        )
        row.report_version = version
        session.add(row)
    else:
        row.label = label
        row.object_path = object_path
        row.content_type = content_type
        row.payload = dict(payload or {})
    session.flush()
    return row


def _unique_import_slug(
    session: Session,
    base_value: str,
    *,
    existing_report_id: int | None = None,
    existing_subject_id: int | None = None,
) -> str:
    base_slug = _slugify(base_value, default="imported-report")[:118].strip("-") or "imported-report"
    candidate = base_slug
    suffix = 2
    while True:
        report_row = session.query(ResearchReport).filter(ResearchReport.slug == candidate).first()
        subject_row = session.query(ResearchSubject).filter(ResearchSubject.slug == candidate).first()
        report_ok = report_row is None or (existing_report_id is not None and int(report_row.id) == int(existing_report_id))
        subject_ok = subject_row is None or (existing_subject_id is not None and int(subject_row.id) == int(existing_subject_id))
        if report_ok and subject_ok:
            return candidate
        suffix_text = f"-{suffix}"
        candidate = f"{base_slug[: max(1, 128 - len(suffix_text))]}{suffix_text}"
        suffix += 1


def _title_from_markdown(markdown_text: str, *, fallback: str) -> str:
    for line in str(markdown_text or "").splitlines():
        cleaned = _clean_text(line).lstrip("#").strip()
        if cleaned:
            return cleaned[:240]
    return fallback[:240]


def _summary_from_text(text: str, *, fallback: str | None = None) -> str | None:
    cleaned = clean_whitespace(text or "")
    if not cleaned:
        return _clean_text(fallback) or None
    paragraphs = [part.strip() for part in cleaned.split("\n\n") if part.strip()]
    summary = paragraphs[0] if paragraphs else cleaned
    return summary[:900]


def _content_kind_from_upload(*, filename: str | None, content_type: str | None, data: bytes) -> tuple[str, str, str]:
    suffix = Path(filename or "").suffix.lower()
    ctype = str(content_type or "").split(";", 1)[0].strip().lower()
    if suffix == ".pdf" or ctype == "application/pdf" or data[:5] == b"%PDF-":
        return "pdf", ".pdf", "application/pdf"
    if suffix in {".md", ".markdown"} or ctype in {"text/markdown", "text/x-markdown"}:
        return "markdown", suffix or ".md", "text/markdown"
    raise ValueError("Only PDF and Markdown reports are supported.")


def import_research_report_bytes(
    session: Session,
    *,
    data: bytes,
    filename: str | None = None,
    content_type: str | None = None,
    title: str | None = None,
    report_type: str | None = None,
    summary: str | None = None,
    tags: object | None = None,
    topics: object | None = None,
    linked_objects: object | None = None,
    source_metadata: dict[str, Any] | None = None,
    report_id: int | None = None,
    subject: str | int | ResearchReport | None = None,
    slug: str | None = None,
    actor: str = "browser",
) -> dict[str, Any]:
    raw = bytes(data or b"")
    if not raw:
        raise ValueError("Report upload is empty.")

    source_filename = str(filename or "").strip() or "imported-report"
    content_kind, ext, normalized_content_type = _content_kind_from_upload(
        filename=source_filename,
        content_type=content_type,
        data=raw,
    )
    file_sha = hashlib.sha256(raw).hexdigest()
    report_type_norm = normalize_report_type(report_type)
    linked = _normalize_linked_objects(linked_objects)
    tags_list = _split_metadata_values(tags)
    topics_list = _split_metadata_values(topics)
    source_meta = dict(source_metadata or {})
    source_meta.update(
        {
            "file_name": source_filename,
            "file_sha256": file_sha,
            "content_type": normalized_content_type,
            "file_extension": ext,
        }
    )

    markdown_text = ""
    extracted_text = ""
    if content_kind == "markdown":
        markdown_text = raw.decode("utf-8", errors="ignore")
        detected_title = _title_from_markdown(markdown_text, fallback=Path(source_filename).stem or "Imported Report")
        text_for_summary = markdown_text
    else:
        extracted_text = extract_text_from_pdf(raw)
        detected_title = Path(source_filename).stem.replace("_", " ").replace("-", " ").strip() or "Imported PDF Report"
        text_for_summary = extracted_text

    resolved_title = _clean_text(title) or detected_title
    resolved_summary = _clean_text(summary) or _summary_from_text(text_for_summary, fallback=resolved_title)

    report: ResearchReport | None = None
    resolved_subject: ReportSubject | None = None
    if report_id is not None:
        report = session.query(ResearchReport).filter_by(id=int(report_id)).first()
        if report is None:
            raise ValueError("Research report not found.")
        resolved_subject = resolve_report_subject(session, report)
    elif subject is not None and str(subject).strip():
        resolved_subject = resolve_report_subject(session, subject)
        report = session.query(ResearchReport).filter_by(subject_key=resolved_subject.subject_key).first()
    else:
        requested_slug = _slugify(slug or resolved_title, default="imported-report")
        report = session.query(ResearchReport).filter_by(slug=requested_slug).first()
        if report is not None:
            resolved_subject = resolve_report_subject(session, report)

    now = utcnow()
    if report is None:
        base_slug = _slugify(slug or resolved_title, default="imported-report")
        unique_slug = _unique_import_slug(session, base_slug)
        subject_key = f"report:{unique_slug}"
        subject_row = ResearchSubject(
            slug=unique_slug,
            subject_key=subject_key,
            kind="report",
            display_name=resolved_title,
            legal_name=None,
            description=resolved_summary,
            status="active",
            issuer_id=None,
            company_id=None,
            primary_entity_id=None,
            extra={
                "report_type": report_type_norm,
                "tags": tags_list,
                "topics": topics_list,
                "linked_objects": linked,
            },
            created_at=now,
            updated_at=now,
        )
        session.add(subject_row)
        session.flush()
        resolved_subject = _subject_from_rows(session, subject_row=subject_row)
        report = ResearchReport(
            subject_id=int(subject_row.id),
            slug=unique_slug,
            subject_key=subject_key,
            issuer_id=None,
            company_id=None,
            title=resolved_title,
            status="active",
            latest_published_version_id=None,
            latest_subject_slug=unique_slug,
            extra={},
            created_at=now,
            updated_at=now,
        )
        session.add(report)
        session.flush()
    else:
        if resolved_subject is None:
            resolved_subject = resolve_report_subject(session, report)
        report.title = resolved_title or report.title
        report.updated_at = now
        if report.subject_id is not None:
            subject_row = session.query(ResearchSubject).filter_by(id=int(report.subject_id)).first()
            if subject_row is not None:
                subject_row.display_name = resolved_title or subject_row.display_name
                subject_row.description = resolved_summary or subject_row.description
                subject_extra = dict(subject_row.extra or {})
                subject_extra.update({"report_type": report_type_norm, "tags": tags_list, "topics": topics_list, "linked_objects": linked})
                subject_row.extra = subject_extra
                subject_row.updated_at = now
                resolved_subject = _subject_from_rows(session, subject_row=subject_row)

    assert report is not None
    assert resolved_subject is not None
    report_slug = _unique_import_slug(
        session,
        slug or report.slug or resolved_title,
        existing_report_id=int(report.id),
        existing_subject_id=int(report.subject_id) if report.subject_id is not None else None,
    )
    report.slug = report_slug
    report.latest_subject_slug = report_slug
    report.subject_key = resolved_subject.subject_key
    extra = dict(report.extra or {})
    extra.update(
        {
            "report_type": report_type_norm,
            "report_type_label": report_type_label(report_type_norm),
            "subject_kind": resolved_subject.kind,
            "summary": resolved_summary,
            "description": resolved_summary,
            "tags": tags_list,
            "topics": topics_list,
            "linked_objects": linked,
            "source_metadata": source_meta,
            "imported_report": True,
            "upload_provenance": {
                "actor": str(actor or "browser"),
                "uploaded_at": now.isoformat(),
                "file_name": source_filename,
                "file_sha256": file_sha,
            },
        }
    )
    report.extra = extra

    max_version = (
        session.query(func.max(ResearchReportVersion.version))
        .filter(ResearchReportVersion.report_id == int(report.id))
        .scalar()
    )
    next_version = int(max_version or 0) + 1
    content_md = markdown_text
    if content_kind == "pdf":
        parts = [f"# {resolved_title}", ""]
        if resolved_summary:
            parts.extend([resolved_summary, ""])
        if extracted_text:
            parts.extend(["## Extracted Text", "", extracted_text])
        else:
            parts.extend(["The uploaded PDF is preserved as the source artifact. Text extraction did not return readable content."])
        content_md = "\n".join(parts).strip() + "\n"

    version = ResearchReportVersion(
        report_id=int(report.id),
        version=next_version,
        status="published",
        trigger_kind="import",
        trigger_label=source_filename,
        trigger_payload={
            "source": "report_import",
            "content_kind": content_kind,
            "file_name": source_filename,
            "file_sha256": file_sha,
            "report_type": report_type_norm,
            "actor": str(actor or "browser"),
        },
        change_fingerprint=file_sha,
        content_json={},
        content_md=content_md,
        coverage_json={
            "metrics": [
                {"label": "Source artifacts", "value": 1},
                {"label": "Topics", "value": len(topics_list)},
                {"label": "Tags", "value": len(tags_list)},
            ],
            "gaps": [],
        },
        as_of_at=now,
        error=None,
        created_at=now,
        updated_at=now,
        published_at=now,
    )
    session.add(version)
    session.flush()

    artifact_type = "pdf" if content_kind == "pdf" else "markdown"
    artifact = _upsert_artifact(
        session,
        version=version,
        artifact_type=artifact_type,
        variant="source",
        label=f"{resolved_title} source {content_kind}",
        data_bytes=raw,
        content_type=normalized_content_type,
        payload={
            "source_role": "uploaded_source",
            "file_name": source_filename,
            "file_sha256": file_sha,
            "report_type": report_type_norm,
            "source_metadata": source_meta,
        },
    )
    reader_format = "pdf" if content_kind == "pdf" else "markdown"
    source_row = {
        "kind": "uploaded_report",
        "title": resolved_title,
        "url": source_meta.get("source_url"),
        "published_at": now.isoformat(),
        "href": f"/reports/{report_slug}/reader?version_id={int(version.id)}&artifact_id={int(artifact.id)}",
        "rail": "import",
        "label": report_type_label(report_type_norm),
    }
    content_payload = {
        "imported_report": True,
        "report_type": report_type_norm,
        "report_type_label": report_type_label(report_type_norm),
        "summary": resolved_summary,
        "description": resolved_summary,
        "tags": tags_list,
        "topics": topics_list,
        "linked_objects": linked,
        "source_metadata": source_meta,
        "upload_provenance": extra["upload_provenance"],
        "subject": {
            "display_name": resolved_title,
            "legal_name": resolved_subject.legal_name,
            "kind": "report",
            "description": resolved_summary,
            "slug": report_slug,
            "subject_key": resolved_subject.subject_key,
            "subject_id": resolved_subject.subject_id,
            "issuer_id": resolved_subject.issuer_id,
            "company_id": resolved_subject.company_id,
            "primary_ticker": resolved_subject.primary_ticker,
            "tickers": list(resolved_subject.tickers),
            "aliases": list(resolved_subject.aliases),
        },
        "identity_freshness": {
            "display_name": resolved_title,
            "kind": "report",
            "description": resolved_summary,
            "slug": report_slug,
            "subject_key": resolved_subject.subject_key,
            "latest_report_version": int(version.version),
            "latest_report_published_at": now.isoformat(),
        },
        "coverage_panel": dict(version.coverage_json or {}),
        "published_primers": [],
        "published_teardowns": [],
        "thesis_ledger": [],
        "note_ledger": [],
        "subject_facts": {
            "kind": "report",
            "definition": resolved_summary,
            "metrics": [],
            "taxonomy": [],
            "related_subjects": [],
            "related_companies": [],
            "exposed_companies": [],
        },
        "structured_financial_history_guidance": {"actuals": [], "guidance": [], "metrics": []},
        "primary_source_timeline": {"timeline": [source_row]},
        "transcript_archive_summaries": {"rows": [], "count": 0, "summary_count": 0},
        "supply_chain_peer_context": {"chains": [], "stage_entries": [], "neighbors": []},
        "linked_datasets_stage_trackers": {"datasets": [], "stage_entries": []},
        "high_confidence_mention_appendix": {"rows": [], "count": 0},
        "artifacts_export_appendix": {"rows": _artifact_entries(version)},
        "reader": {
            "format": reader_format,
            "artifact_id": int(artifact.id),
            "reader_url": f"/reports/{report_slug}/reader?version_id={int(version.id)}&artifact_id={int(artifact.id)}",
        },
        "section_order": list(SECTION_ORDER),
        "window_days": None,
        "computed_at": now.isoformat(),
    }
    version.content_json = content_payload
    report.latest_published_version_id = int(version.id)
    report.updated_at = now
    session.flush()
    return {
        "report": serialize_report_summary(report, version, subject=resolve_report_subject(session, report)),
        "version": serialize_report_version(version),
        "artifact": _artifact_entries(version)[0] if _artifact_entries(version) else None,
    }


def append_research_report_addendum(
    session: Session,
    *,
    subject: str | int | ResearchReport,
    version_id: int | None = None,
    title: str | None = None,
    body_md: str,
    summary: str | None = None,
    tags: object | None = None,
    actor: str = "browser",
) -> dict[str, Any]:
    resolved_subject = resolve_report_subject(session, subject)
    report = _find_report_for_subject(session, resolved_subject)
    if report is None or report.id is None:
        raise ValueError("Research report not found for this subject.")

    if version_id is not None:
        version = (
            session.query(ResearchReportVersion)
            .filter_by(report_id=int(report.id), id=int(version_id))
            .first()
        )
    else:
        version = _latest_published_version(session, int(report.id))
    if version is None:
        raise ValueError("Research report version not found for this subject.")

    cleaned_body = str(body_md or "").strip()
    if not cleaned_body:
        raise ValueError("body_md is required.")
    now = utcnow()
    cleaned_title = _clean_text(title) or "Manual addendum"
    cleaned_summary = _clean_text(summary) or _summary_from_text(cleaned_body, fallback=cleaned_title)
    tags_list = _split_metadata_values(tags)
    actor_label = _clean_text(actor) or "browser"
    addendum_id = f"manual-{now.strftime('%Y%m%d%H%M%S')}-{_stable_hash({'title': cleaned_title, 'body_md': cleaned_body, 'actor': actor_label, 'now': now.isoformat()})[:10]}"
    addendum_markdown_parts = [f"# {cleaned_title}", ""]
    if cleaned_summary:
        addendum_markdown_parts.extend([cleaned_summary, ""])
    addendum_markdown_parts.extend([cleaned_body, "", f"_Manual addendum recorded by {actor_label} at {now.isoformat()}._"])
    addendum_markdown = "\n".join(addendum_markdown_parts).strip() + "\n"

    artifact = _upsert_artifact(
        session,
        version=version,
        artifact_type="manual_addendum",
        variant=addendum_id[:64],
        label=cleaned_title,
        data_bytes=addendum_markdown.encode("utf-8"),
        content_type="text/markdown",
        payload={
            "manual_addendum": True,
            "addendum_id": addendum_id,
            "title": cleaned_title,
            "summary": cleaned_summary,
            "tags": tags_list,
            "actor": actor_label,
            "created_at": now.isoformat(),
        },
    )
    reader_url = f"/reports/{report.slug}/reader?version_id={int(version.id)}&artifact_id={int(artifact.id)}"
    raw_url = f"/reports/{report.slug}/artifacts/{int(artifact.id)}/raw?version_id={int(version.id)}"
    record = {
        "id": addendum_id,
        "title": cleaned_title,
        "summary": cleaned_summary,
        "body_md": cleaned_body,
        "body_preview": _clean_text(cleaned_body)[:500],
        "tags": tags_list,
        "actor": actor_label,
        "created_at": now.isoformat(),
        "artifact_id": int(artifact.id),
        "reader_url": reader_url,
        "raw_url": raw_url,
    }

    payload = dict(version.content_json or {}) if isinstance(version.content_json, dict) else {}
    addenda = _manual_addenda_for_version(version)
    addenda.append(record)
    payload["manual_addenda"] = addenda
    payload["manual_layer"] = {
        "updated_at": now.isoformat(),
        "count": len(addenda),
        "latest_addendum_id": addendum_id,
    }
    version.content_json = payload
    coverage_json = dict(version.coverage_json or {}) if isinstance(version.coverage_json, dict) else {}
    coverage_json["manual_addenda_count"] = len(addenda)
    coverage_json["manual_addenda_updated_at"] = now.isoformat()
    version.coverage_json = coverage_json
    version.updated_at = now
    report_extra = dict(report.extra or {})
    report_extra["manual_addenda_count"] = len(addenda)
    report_extra["manual_addenda_updated_at"] = now.isoformat()
    report.extra = report_extra
    report.updated_at = now
    session.flush()
    return {
        "report": serialize_report_summary(report, version, subject=resolved_subject),
        "version": serialize_report_version(version),
        "addendum": record,
    }


async def _playwright_pdf_bytes(html: str) -> bytes | None:
    try:
        from playwright.async_api import async_playwright
    except Exception:
        return None
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            page = await browser.new_page(viewport={"width": 1280, "height": 1810})
            await page.set_content(html, wait_until="load")
            pdf_bytes = await page.pdf(format="Letter", print_background=True, margin={"top": "18mm", "bottom": "18mm", "left": "14mm", "right": "14mm"})
            await browser.close()
            return pdf_bytes
    except Exception:
        return None


async def _playwright_png_bytes(html: str) -> bytes | None:
    try:
        from playwright.async_api import async_playwright
    except Exception:
        return None
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            page = await browser.new_page(viewport={"width": 1200, "height": 900})
            await page.set_content(html, wait_until="load")
            png_bytes = await page.screenshot(full_page=True, type="png")
            await browser.close()
            return png_bytes
    except Exception:
        return None


def _chain_png_html(chain: dict[str, Any]) -> str:
    about = chain.get("about") or {}
    counts = chain.get("counts") or {}
    lints = about.get("lint_summary") or []
    return f"""
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <style>
      body {{ font-family: Arial, sans-serif; background: #f7f5ef; color: #1e1a16; padding: 24px; }}
      .card {{ background: white; border: 1px solid #d9d2c4; border-radius: 18px; padding: 22px; width: 980px; }}
      h1 {{ margin: 0 0 6px; font-size: 32px; }}
      p {{ margin: 6px 0; color: #4a433b; }}
      .grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin: 18px 0; }}
      .metric {{ border: 1px solid #ece5d8; border-radius: 12px; padding: 12px; background: #faf8f2; }}
      .metric strong {{ display: block; font-size: 22px; margin-bottom: 2px; }}
      ul {{ margin: 14px 0 0 18px; }}
    </style>
  </head>
  <body>
    <div class="card">
      <h1>{chain.get("title") or "Supply Chain"}</h1>
      <p>{chain.get("description") or ""}</p>
      <div class="grid">
        <div class="metric"><strong>{counts.get("layers", 0)}</strong>Layers</div>
        <div class="metric"><strong>{counts.get("nodes", 0)}</strong>Nodes</div>
        <div class="metric"><strong>{counts.get("edges", 0)}</strong>Edges</div>
        <div class="metric"><strong>{counts.get("bindings", 0)}</strong>Bindings</div>
      </div>
      <p>Category: {chain.get("category") or "general"}{(" · Product: " + str(chain.get("product"))) if chain.get("product") else ""}</p>
      <ul>{''.join(f"<li>{str(item)}</li>" for item in lints[:6])}</ul>
    </div>
  </body>
</html>
    """.strip()


def _persist_supply_chain_pngs(session: Session, *, version: ResearchReportVersion, chains: list[dict[str, Any]]) -> dict[str, bytes]:
    image_map: dict[str, bytes] = {}
    for chain in chains[:4]:
        slug = str(chain.get("slug") or "").strip()
        if not slug:
            continue
        png_bytes = asyncio.run(_playwright_png_bytes(_chain_png_html(chain)))
        if not png_bytes:
            continue
        image_map[slug] = png_bytes
        _upsert_artifact(
            session,
            version=version,
            artifact_type="supply_chain_png",
            variant=slug,
            label=f"{chain.get('title') or slug} snapshot",
            data_bytes=png_bytes,
            content_type="image/png",
            payload={"chain_id": chain.get("id"), "chain_slug": slug},
        )
    return image_map


def _ensure_report_container(session: Session, subject: ReportSubject) -> ResearchReport:
    subject_row = _ensure_subject_row(session, subject)
    report = session.query(ResearchReport).filter_by(subject_key=subject.subject_key).first()
    now = utcnow()
    if report is None and subject.issuer_id is not None:
        report = (
            session.query(ResearchReport)
            .filter(ResearchReport.issuer_id == int(subject.issuer_id))
            .order_by(ResearchReport.updated_at.desc(), ResearchReport.id.desc())
            .first()
        )
    if report is None and subject.company_id is not None:
        report = (
            session.query(ResearchReport)
            .filter(ResearchReport.company_id == int(subject.company_id))
            .order_by(ResearchReport.updated_at.desc(), ResearchReport.id.desc())
            .first()
        )
    if report is None and subject.subject_id is not None:
        report = (
            session.query(ResearchReport)
            .filter(ResearchReport.subject_id == int(subject.subject_id))
            .order_by(ResearchReport.updated_at.desc(), ResearchReport.id.desc())
            .first()
        )
    if report is None:
        report = ResearchReport(
            subject_id=int(subject_row.id),
            slug=subject.slug,
            subject_key=subject.subject_key,
            issuer_id=subject.issuer_id,
            company_id=subject.company_id,
            title=f"Research Report — {subject.display_name}",
            status="active",
            latest_published_version_id=None,
            latest_subject_slug=subject.slug,
            extra={"subject_kind": subject.kind},
            created_at=now,
            updated_at=now,
        )
        session.add(report)
        session.flush()
        return report
    report.subject_id = int(subject_row.id)
    report.slug = subject.slug
    report.subject_key = subject.subject_key
    report.title = f"Research Report — {subject.display_name}"
    report.issuer_id = subject.issuer_id
    report.company_id = subject.company_id
    report.latest_subject_slug = subject.slug
    extra = dict(report.extra or {})
    extra["subject_kind"] = subject.kind
    report.extra = extra
    report.updated_at = now
    session.flush()
    return report


def _latest_published_version(session: Session, report_id: int) -> ResearchReportVersion | None:
    return (
        session.query(ResearchReportVersion)
        .filter_by(report_id=int(report_id), status="published")
        .order_by(ResearchReportVersion.version.desc(), ResearchReportVersion.id.desc())
        .first()
    )


def _find_report_for_subject(session: Session, subject: ReportSubject) -> ResearchReport | None:
    report = session.query(ResearchReport).filter_by(subject_key=subject.subject_key).first()
    if report is None and subject.issuer_id is not None:
        report = (
            session.query(ResearchReport)
            .filter(ResearchReport.issuer_id == int(subject.issuer_id))
            .order_by(ResearchReport.updated_at.desc(), ResearchReport.id.desc())
            .first()
        )
    if report is None and subject.company_id is not None:
        report = (
            session.query(ResearchReport)
            .filter(ResearchReport.company_id == int(subject.company_id))
            .order_by(ResearchReport.updated_at.desc(), ResearchReport.id.desc())
            .first()
        )
    if report is None and subject.subject_id is not None:
        report = (
            session.query(ResearchReport)
            .filter(ResearchReport.subject_id == int(subject.subject_id))
            .order_by(ResearchReport.updated_at.desc(), ResearchReport.id.desc())
            .first()
        )
    return report


def assemble_report_payload(
    session: Session,
    *,
    subject: ReportSubject,
    report: ResearchReport | None = None,
    window_days: int | None = None,
    include_knowledge_asset_content: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    resolved_window_days = _window_days(window_days)
    latest_version = _latest_published_version(session, int(report.id)) if report is not None and report.id is not None else None
    primary_sources = _primary_sources(session, subject, window_days=resolved_window_days)
    theses = _thesis_rows(session, subject, window_days=resolved_window_days)
    notes = _note_rows(session, subject, window_days=resolved_window_days)
    structured = _structured_rows(session, subject, window_days=resolved_window_days)
    transcripts = _transcript_archive(session, subject, window_days=resolved_window_days)
    supply_chain = _linked_supply_chains(session, subject, thesis_rows=theses, note_rows=notes)
    datasets = _linked_datasets(
        session,
        subject,
        linked_stage_entries=list(supply_chain.get("stage_entries") or []),
        thesis_rows=theses,
        note_rows=notes,
        linked_chains=list(supply_chain.get("chains") or []),
    )
    knowledge_assets = _knowledge_asset_sections(
        session,
        subject,
        include_content=include_knowledge_asset_content,
    )
    subject_facts = _subject_facts(session, subject, structured=structured)
    mentions = _mention_appendix(session, subject)
    coverage = _coverage_panel(
        subject=subject,
        primary_sources=primary_sources,
        knowledge_assets=knowledge_assets,
        theses=theses,
        notes=notes,
        structured=structured,
        transcripts=transcripts,
        supply_chain=supply_chain,
        datasets=datasets,
    )
    source_pack_payload: dict[str, Any] | None = None
    canon_scenario_payload: dict[str, Any] | None = None
    agent_card_payload: dict[str, Any] | None = None
    if subject.subject_id is not None:
        from thoth_terminal.research_canon_service import (
            get_agent_canon_card,
            get_selected_canon_scenario,
            research_report_fingerprint_context,
            serialize_scenario,
            serialize_source_pack,
            upsert_source_pack,
        )

        source_pack_payload = serialize_source_pack(
            upsert_source_pack(session, subject_id=int(subject.subject_id), audit={"source": "report_build"})
        )
        canon_scenario_payload = serialize_scenario(get_selected_canon_scenario(session, subject_id=int(subject.subject_id)))
        agent_card_payload = get_agent_canon_card(session, subject_id=int(subject.subject_id))
        fingerprint_canon_payload = research_report_fingerprint_context(session, subject_id=int(subject.subject_id))
    else:
        fingerprint_canon_payload = {}

    payload = {
        "subject": {
            "display_name": subject.display_name,
            "legal_name": subject.legal_name,
            "kind": subject.kind,
            "description": subject.description,
            "slug": subject.slug,
            "subject_key": subject.subject_key,
            "subject_id": subject.subject_id,
            "issuer_id": subject.issuer_id,
            "company_id": subject.company_id,
            "primary_entity_id": subject.primary_entity_id,
            "primary_ticker": subject.primary_ticker,
            "tickers": subject.tickers,
            "aliases": subject.aliases,
        },
        "identity_freshness": _identity_panel(session, subject, report=report, latest_version=latest_version),
        "coverage_panel": coverage,
        "official_source_pack": source_pack_payload,
        "canon_scenario": canon_scenario_payload,
        "agent_canon_card": agent_card_payload,
        "published_primers": knowledge_assets.get("primers") or [],
        "published_teardowns": knowledge_assets.get("teardowns") or [],
        "thesis_ledger": theses,
        "note_ledger": notes,
        "primary_source_timeline": primary_sources,
        "subject_facts": subject_facts,
        "structured_financial_history_guidance": structured,
        "transcript_archive_summaries": transcripts,
        "supply_chain_peer_context": supply_chain,
        "linked_datasets_stage_trackers": {"datasets": datasets, "stage_entries": list(supply_chain.get("stage_entries") or [])},
        "high_confidence_mention_appendix": mentions,
        "artifacts_export_appendix": {"rows": []},
        "section_order": list(SECTION_ORDER),
        "window_days": resolved_window_days,
        "computed_at": _dt_iso(utcnow()),
    }
    fingerprint_payload = {
        "subject_key": subject.subject_key,
        "subject_kind": subject.kind,
        "window_days": resolved_window_days,
        "primary_sources": {
            "filings": [(row.get("id"), row.get("published_at")) for row in primary_sources.get("filings") or []],
            "disclosures": [(row.get("id"), row.get("published_at")) for row in primary_sources.get("disclosures") or []],
            "ir_artifacts": [(row.get("id"), row.get("published_at")) for row in primary_sources.get("ir_artifacts") or []],
            "legacy_documents": [(row.get("id"), row.get("published_at")) for row in primary_sources.get("legacy_documents") or []],
        },
        "primers": [(row.get("id"), row.get("version"), row.get("as_of_at")) for row in knowledge_assets.get("primers") or []],
        "teardowns": [(row.get("id"), row.get("version"), row.get("as_of_at")) for row in knowledge_assets.get("teardowns") or []],
        "theses": [(row.get("id"), row.get("updated_at")) for row in theses],
        "notes": [(row.get("id"), row.get("updated_at")) for row in notes],
        "structured": [(row.get("id"), row.get("metric_key"), row.get("period_label"), row.get("value_number"), row.get("value_text")) for row in (structured.get("actuals") or []) + (structured.get("guidance") or [])],
        "subject_facts": subject_facts,
        "transcripts": [(row.get("document_id"), row.get("published_at"), row.get("summary_id")) for row in transcripts.get("rows") or []],
        "supply_chains": [(row.get("id"), row.get("updated_at"), row.get("published_snapshot_id")) for row in supply_chain.get("chains") or []],
        "stage_entries": [(row.get("id"), row.get("updated_at")) for row in supply_chain.get("stage_entries") or []],
        "datasets": [(row.get("id"), row.get("updated_at")) for row in datasets],
        "mentions": [(row.get("kind"), row.get("id"), row.get("published_at")) for row in mentions.get("rows") or []],
        "coverage": coverage,
        "official_source_pack": fingerprint_canon_payload.get("official_source_pack"),
        "canon_scenario": fingerprint_canon_payload.get("canon_scenario"),
    }
    return payload, {"change_fingerprint": _stable_hash(fingerprint_payload), "coverage": coverage}


def report_payload_has_detail_sections(payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    return all(
        isinstance(payload.get(key), dict) or isinstance(payload.get(key), list)
        for key in ("identity_freshness", "coverage_panel", "subject_facts")
    )


def report_memory_quality(version: Any | None) -> dict[str, Any]:
    if version is None:
        return {
            "state": "missing",
            "label": "Missing memory",
            "needs_review": True,
            "content_chars": 0,
            "structured_payload": False,
            "agent_direct_write": False,
            "readable": False,
        }
    content_json = dict(version.content_json or {}) if isinstance(version.content_json, dict) else {}
    content_chars = _version_content_chars(version)
    structured = report_payload_has_detail_sections(content_json)
    direct_write = bool(content_json.get("agent_direct_write"))
    latest_decision = content_json.get("report_memory_decision") if isinstance(content_json.get("report_memory_decision"), dict) else {}
    accepted = bool(content_json.get("report_memory_review_accepted") or latest_decision.get("decision") in {"accept", "addendum"})
    quarantined = str(version.status or "").strip().lower() == "quarantined" or bool(content_json.get("report_memory_quarantined"))
    readable = content_chars >= 200
    if quarantined:
        state = "quarantined"
        label = "Quarantined memory"
        needs_review = False
    elif accepted and structured:
        state = "reviewed_agent_memory" if direct_write else "reviewed_memory"
        label = "Reviewed agent memory" if direct_write else "Reviewed memory"
        needs_review = False
    elif accepted and readable:
        state = "accepted_unstructured_memory"
        label = "Accepted, still unstructured"
        needs_review = True
    elif structured and readable:
        if direct_write:
            state = "structured_agent_write"
            label = "Structured agent write"
            needs_review = True
        else:
            state = "structured"
            label = "Structured memory"
            needs_review = False
    elif readable:
        state = "readable_unstructured"
        label = "Readable, needs structuring"
        needs_review = True
    elif content_chars > 0:
        state = "thin"
        label = "Thin memory"
        needs_review = True
    else:
        state = "empty"
        label = "Empty memory"
        needs_review = True
    return {
        "state": state,
        "label": label,
        "needs_review": needs_review,
        "content_chars": content_chars,
        "structured_payload": structured,
        "agent_direct_write": direct_write,
        "readable": readable,
        "review_accepted": accepted,
        "quarantined": quarantined,
        "latest_decision": latest_decision or None,
    }


def _report_memory_mentions_social_evidence(version: Any | None) -> bool:
    if version is None:
        return False
    payload = dict(version.content_json or {}) if isinstance(version.content_json, dict) else {}
    candidates: list[str] = [_version_content_text(version)]
    coverage = payload.get("coverage_panel") if isinstance(payload.get("coverage_panel"), dict) else {}
    candidates.extend(str(coverage.get(key) or "") for key in ("evidence_tier", "source_kind", "latest_agent_update"))
    for fact in payload.get("subject_facts") or []:
        if isinstance(fact, dict):
            candidates.extend(str(fact.get(key) or "") for key in ("summary", "evidence_tier", "source_kind"))
    text = " ".join(candidates).lower()
    return any(token in text for token in ("tweet", "x.com", "social", "@paurooteri", "@dnystedt", "@"))


def report_memory_review_recommendation(version: Any | None) -> dict[str, Any]:
    quality = report_memory_quality(version)
    state = str(quality.get("state") or "")
    content_chars = int(quality.get("content_chars") or 0)
    direct_write = bool(quality.get("agent_direct_write"))
    social_evidence = _report_memory_mentions_social_evidence(version)
    if state in {"structured", "reviewed_agent_memory", "reviewed_memory", "quarantined"} and (state != "structured" or not direct_write):
        return {
            "action": "none",
            "label": "No memory follow-up",
            "summary": "Report memory has a durable reviewed or canonical disposition.",
            "desired_outputs": [],
            "priority": 1,
            "needs_follow_up": False,
        }
    if state == "missing":
        return {
            "action": "build_report",
            "label": "Build report memory",
            "summary": "No report memory version exists for this subject.",
            "desired_outputs": ["research_report"],
            "priority": 6,
            "needs_follow_up": True,
        }
    if state == "readable_unstructured":
        return {
            "action": "structure_report_memory",
            "label": "Structure readable memory",
            "summary": "Readable markdown exists, but the payload is not structured enough for decision-spine reuse.",
            "desired_outputs": ["report_memory", "structured_payload", "review"],
            "priority": 7,
            "needs_follow_up": True,
        }
    if state == "thin":
        if social_evidence:
            return {
                "action": "corroborate_or_quarantine",
                "label": "Corroborate or quarantine",
                "summary": "Thin report memory appears to rely on social or analyst-commentary evidence and should not be treated as final report memory without corroboration.",
                "desired_outputs": ["source_corroboration", "report_review", "quarantine_decision"],
                "priority": 8,
                "needs_follow_up": True,
            }
        return {
            "action": "write_report_addendum",
            "label": "Write report addendum",
            "summary": "Structured but thin report memory needs a fuller source-backed addendum before analyst or agent reuse.",
            "desired_outputs": ["report_addendum", "source_review", "report_memory"],
            "priority": 7,
            "needs_follow_up": True,
        }
    if state == "structured_agent_write":
        if content_chars >= 5000:
            return {
                "action": "review_direct_write",
                "label": "Review direct agent write",
                "summary": "A structured local-agent write is present and substantial; keep it review-visible until a human or higher-confidence automation accepts it.",
                "desired_outputs": ["report_review", "accept_or_amend"],
                "priority": 5,
                "needs_follow_up": True,
            }
        if social_evidence:
            return {
                "action": "corroborate_or_addendum",
                "label": "Corroborate or addendum",
                "summary": "A structured local-agent write is present, but it is short and appears commentary-led; corroborate it or write a source-backed addendum.",
                "desired_outputs": ["source_corroboration", "report_addendum", "report_review"],
                "priority": 8,
                "needs_follow_up": True,
            }
        return {
            "action": "review_or_addendum",
            "label": "Review or addendum",
            "summary": "A structured local-agent write is present, but it is short enough that the report should remain review-visible until accepted or expanded.",
            "desired_outputs": ["report_review", "report_addendum"],
            "priority": 6,
            "needs_follow_up": True,
        }
    return {
        "action": "review_report_memory",
        "label": "Review report memory",
        "summary": "Report memory is incomplete or empty and should be reviewed before analyst or agent reuse.",
        "desired_outputs": ["report_review"],
        "priority": 6,
        "needs_follow_up": True,
    }


def _report_memory_request_key(*, report: ResearchReport, version: ResearchReportVersion | None, action: str) -> str:
    if version is not None and version.id is not None:
        return f"report-memory-review:version:{int(version.id)}:{action}"
    return f"report-memory-review:report:{int(report.id)}:{action}"


def create_report_memory_review_request(
    session: Session,
    *,
    subject: str | int | ResearchReport,
    version_id: int | None = None,
    status: str = "draft",
) -> InstrumentRequest:
    resolved_subject = resolve_report_subject(session, subject)
    report = session.query(ResearchReport).filter_by(subject_key=resolved_subject.subject_key).first()
    if report is None:
        report = _transient_report_for_subject(resolved_subject)
        session.add(report)
        session.flush()

    version: ResearchReportVersion | None = None
    if version_id is not None:
        version = (
            session.query(ResearchReportVersion)
            .filter_by(report_id=int(report.id), id=int(version_id))
            .first()
        )
        if version is None:
            raise ValueError("Report version not found for this subject.")
    elif report.id is not None:
        version = _latest_published_version(session, int(report.id))

    review = report_memory_review_recommendation(version)
    quality = report_memory_quality(version)
    action = str(review.get("action") or "review_report_memory").strip() or "review_report_memory"
    label = str(review.get("label") or "Review report memory").strip() or "Review report memory"
    summary = str(review.get("summary") or "Report memory needs analyst follow-up before reuse.").strip()
    desired_outputs = [str(v).strip() for v in (review.get("desired_outputs") or []) if str(v).strip()]
    if not desired_outputs:
        desired_outputs = ["report_review"]
    report_url = f"/reports/{report.slug}"
    if version is not None and version.id is not None:
        report_url += f"?version_id={int(version.id)}"
    linked_objects: dict[str, Any] = {
        "report_id": int(report.id),
        "report_url": report_url,
        "subject_key": resolved_subject.subject_key,
    }
    if version is not None and version.id is not None:
        linked_objects["report_version_id"] = int(version.id)
    if resolved_subject.subject_id is not None:
        linked_objects["subject_id"] = int(resolved_subject.subject_id)
    if resolved_subject.company_id is not None:
        linked_objects["company_id"] = int(resolved_subject.company_id)
    if resolved_subject.issuer_id is not None:
        linked_objects["issuer_id"] = int(resolved_subject.issuer_id)

    version_label = f" v{int(version.version)}" if version is not None else ""
    return create_instrument_request(
        session,
        title=f"{label} for {resolved_subject.display_name}{version_label}"[:255],
        intent="report_memory_review",
        summary=summary,
        target_entities=[resolved_subject.display_name] if resolved_subject.kind in {"company", "issuer"} else None,
        target_themes=[resolved_subject.display_name] if resolved_subject.kind not in {"company", "issuer"} else None,
        desired_outputs=desired_outputs,
        evidence_bundle=[
            {
                "label": f"Report memory quality: {quality.get('label') or quality.get('state') or 'unknown'}",
                "source": "research_report",
                "object_type": "research_report_version" if version is not None else "research_report",
                "object_id": int(version.id) if version is not None and version.id is not None else int(report.id),
                "url": report_url,
                "memory_state": quality.get("state"),
                "memory_action": action,
            }
        ],
        provenance_summary="Created from a report-memory quality recommendation on the research report surface.",
        evidence_tier="internal_report_memory",
        status=status,
        priority=max(1, min(10, int(review.get("priority") or 6) + 1)),
        automation_source="report_memory_review",
        source_kind=action,
        draft_payload={
            "memory_review": review,
            "memory_quality": quality,
            "report": {
                "id": int(report.id),
                "slug": report.slug,
                "title": report.title,
                "url": report_url,
            },
            "version": {
                "id": int(version.id),
                "version": int(version.version),
                "status": version.status,
            }
            if version is not None and version.id is not None
            else None,
        },
        linked_objects=linked_objects,
        request_key=_report_memory_request_key(report=report, version=version, action=action),
    )


def _clean_evidence_list(values: list[str] | tuple[str, ...] | None) -> list[str]:
    out: list[str] = []
    for value in values or []:
        cleaned = _clean_text(value)
        if cleaned and cleaned not in out:
            out.append(cleaned)
    return out[:25]


def _report_memory_decision_record(
    *,
    decision: str,
    version: ResearchReportVersion,
    actor: str,
    reason: str | None,
    evidence: list[str],
    now: datetime,
    body_md: str | None = None,
    parent_version_id: int | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "decision": decision,
        "actor": _clean_text(actor) or "codex",
        "reason": _clean_text(reason) or None,
        "evidence": list(evidence),
        "decided_at": _dt_iso(now),
        "report_id": int(version.report_id),
        "version_id": int(version.id),
        "version": int(version.version),
    }
    if parent_version_id is not None:
        record["parent_version_id"] = int(parent_version_id)
    if body_md is not None:
        record["body_md"] = str(body_md).strip()
    return record


def _append_report_memory_decision(content_json: dict[str, Any] | None, record: dict[str, Any]) -> dict[str, Any]:
    payload = dict(content_json or {})
    history = [dict(row) for row in (payload.get("report_memory_decisions") or []) if isinstance(row, dict)]
    history.append(dict(record))
    payload["report_memory_decision"] = dict(record)
    payload["report_memory_decisions"] = history
    decision = str(record.get("decision") or "").strip().lower()
    if decision in {"accept", "addendum"}:
        payload["report_memory_review_accepted"] = True
        payload["report_memory_review_accepted_at"] = record.get("decided_at")
        payload["report_memory_review_actor"] = record.get("actor")
    if decision == "quarantine":
        payload["report_memory_quarantined"] = True
        payload["report_memory_quarantined_at"] = record.get("decided_at")
        payload["report_memory_quarantine_reason"] = record.get("reason")
    return payload


def _format_report_memory_addendum(*, body_md: str, reason: str | None, evidence: list[str], now: datetime) -> str:
    lines: list[str] = ["", f"## Report Memory Addendum - {now.date().isoformat()}", ""]
    cleaned_reason = _clean_text(reason)
    if cleaned_reason:
        lines.extend([f"Reason: {cleaned_reason}", ""])
    lines.append(str(body_md or "").strip())
    if evidence:
        lines.extend(["", "Evidence:"])
        lines.extend(f"- {item}" for item in evidence)
    return "\n".join(lines).strip() + "\n"


def _write_report_memory_artifacts(
    session: Session,
    *,
    version: ResearchReportVersion,
    payload: dict[str, Any],
    markdown_text: str,
) -> dict[str, Any]:
    report = version.report or session.query(ResearchReport).filter_by(id=int(version.report_id)).first()
    report_label = str(getattr(report, "title", "") or "Research report").replace("Research Report — ", "", 1)
    payload_for_artifact = dict(payload)
    payload_for_artifact["artifacts_export_appendix"] = {"rows": _artifact_entries(version)}
    _upsert_artifact(
        session,
        version=version,
        artifact_type="json",
        variant="default",
        label=f"{report_label} research report JSON",
        data_bytes=json.dumps(payload_for_artifact, indent=2, ensure_ascii=False, default=_json_default).encode("utf-8"),
        content_type="application/json",
        payload={"report_memory_decision": (payload_for_artifact.get("report_memory_decision") or {}).get("decision")},
    )
    _upsert_artifact(
        session,
        version=version,
        artifact_type="markdown",
        variant="default",
        label=f"{report_label} research report Markdown",
        data_bytes=markdown_text.encode("utf-8"),
        content_type="text/markdown",
        payload={"report_memory_decision": (payload_for_artifact.get("report_memory_decision") or {}).get("decision")},
    )
    payload_for_artifact["artifacts_export_appendix"] = {"rows": _artifact_entries(version)}
    return payload_for_artifact


def apply_report_memory_decision(
    session: Session,
    *,
    subject: str | int | ResearchReport,
    version_id: int | None = None,
    decision: str,
    body_md: str | None = None,
    reason: str | None = None,
    evidence: list[str] | tuple[str, ...] | None = None,
    actor: str = "codex",
) -> dict[str, Any]:
    decision_value = str(decision or "").strip().lower()
    if decision_value not in REPORT_MEMORY_DECISIONS:
        raise ValueError(f"Unsupported report-memory decision: {decision}")
    resolved_subject = resolve_report_subject(session, subject)
    report = _find_report_for_subject(session, resolved_subject)
    if report is None or report.id is None:
        raise ValueError("Research report not found for this subject.")

    if version_id is not None:
        version = (
            session.query(ResearchReportVersion)
            .filter_by(report_id=int(report.id), id=int(version_id))
            .first()
        )
    else:
        version = _latest_published_version(session, int(report.id))
    if version is None:
        raise ValueError("Research report version not found for this subject.")

    now = utcnow()
    evidence_list = _clean_evidence_list(list(evidence or []))
    cleaned_reason = _clean_text(reason) or None
    cleaned_body = str(body_md or "").strip()

    if decision_value == "addendum" and not cleaned_body:
        raise ValueError("body_md is required when applying a report-memory addendum.")

    if decision_value == "accept":
        record = _report_memory_decision_record(
            decision=decision_value,
            version=version,
            actor=actor,
            reason=cleaned_reason,
            evidence=evidence_list,
            now=now,
        )
        version.content_json = _append_report_memory_decision(
            dict(version.content_json or {}) if isinstance(version.content_json, dict) else {},
            record,
        )
        version.updated_at = now
        report.updated_at = now
        session.flush()
        payload_with_artifacts = _write_report_memory_artifacts(
            session,
            version=version,
            payload=dict(version.content_json or {}),
            markdown_text=str(version.content_md or ""),
        )
        version.content_json = payload_with_artifacts
        session.flush()
        return {
            "decision": decision_value,
            "report": serialize_report_summary(report, version, subject=resolved_subject),
            "version": serialize_report_version(version),
        }

    if decision_value == "quarantine":
        record = _report_memory_decision_record(
            decision=decision_value,
            version=version,
            actor=actor,
            reason=cleaned_reason,
            evidence=evidence_list,
            now=now,
        )
        version.status = "quarantined"
        version.error = f"quarantined: {cleaned_reason}" if cleaned_reason else "quarantined"
        version.content_json = _append_report_memory_decision(
            dict(version.content_json or {}) if isinstance(version.content_json, dict) else {},
            record,
        )
        coverage_json = dict(version.coverage_json or {}) if isinstance(version.coverage_json, dict) else {}
        decisions = [dict(row) for row in (coverage_json.get("report_memory_decisions") or []) if isinstance(row, dict)]
        decisions.append(record)
        coverage_json["report_memory_decisions"] = decisions
        coverage_json["report_memory_quarantined"] = True
        version.coverage_json = coverage_json
        version.updated_at = now
        report.updated_at = now
        if report.latest_published_version_id == int(version.id):
            session.flush()
            replacement = _latest_published_version(session, int(report.id))
            report.latest_published_version_id = int(replacement.id) if replacement is not None else None
        session.flush()
        payload_with_artifacts = _write_report_memory_artifacts(
            session,
            version=version,
            payload=dict(version.content_json or {}),
            markdown_text=str(version.content_md or ""),
        )
        version.content_json = payload_with_artifacts
        session.flush()
        return {
            "decision": decision_value,
            "report": serialize_report_summary(report, _latest_published_version(session, int(report.id)), subject=resolved_subject),
            "version": serialize_report_version(version),
        }

    addendum_markdown = _format_report_memory_addendum(
        body_md=cleaned_body,
        reason=cleaned_reason,
        evidence=evidence_list,
        now=now,
    )
    max_version = (
        session.query(func.max(ResearchReportVersion.version))
        .filter(ResearchReportVersion.report_id == int(report.id))
        .scalar()
    )
    next_version = int(max_version or version.version or 0) + 1
    new_version = ResearchReportVersion(
        report_id=int(report.id),
        version=next_version,
        status="published",
        trigger_kind="report_memory_addendum",
        trigger_label=(cleaned_reason or "Report memory addendum")[:255],
        trigger_payload={
            "decision": decision_value,
            "parent_version_id": int(version.id),
            "actor": _clean_text(actor) or "codex",
            "reason": cleaned_reason,
            "evidence": evidence_list,
        },
        change_fingerprint=_stable_hash(
            {
                "decision": decision_value,
                "parent_version_id": int(version.id),
                "body_md": cleaned_body,
                "evidence": evidence_list,
                "reason": cleaned_reason,
            }
        ),
        content_json={},
        content_md="",
        coverage_json=dict(version.coverage_json or {}) if isinstance(version.coverage_json, dict) else {},
        as_of_at=now,
        error=None,
        created_at=now,
        updated_at=now,
        published_at=now,
    )
    session.add(new_version)
    session.flush()

    record = _report_memory_decision_record(
        decision=decision_value,
        version=new_version,
        actor=actor,
        reason=cleaned_reason,
        evidence=evidence_list,
        now=now,
        body_md=cleaned_body,
        parent_version_id=int(version.id),
    )
    payload = dict(version.content_json or {}) if isinstance(version.content_json, dict) else {}
    addenda = [dict(row) for row in (payload.get("report_memory_addenda") or []) if isinstance(row, dict)]
    addenda.append(record)
    payload["report_memory_addenda"] = addenda
    payload = _append_report_memory_decision(payload, record)

    base_markdown = str(version.content_md or "").rstrip()
    markdown_text = f"{base_markdown}\n\n{addendum_markdown}".strip() + "\n"
    payload_with_artifacts = _write_report_memory_artifacts(
        session,
        version=new_version,
        payload=payload,
        markdown_text=markdown_text,
    )
    new_version.content_json = payload_with_artifacts
    new_version.content_md = markdown_text
    new_version.coverage_json = dict(new_version.coverage_json or {})
    decisions = [dict(row) for row in (new_version.coverage_json.get("report_memory_decisions") or []) if isinstance(row, dict)]
    decisions.append(record)
    new_version.coverage_json["report_memory_decisions"] = decisions
    report.latest_published_version_id = int(new_version.id)
    report.updated_at = now
    session.flush()
    return {
        "decision": decision_value,
        "report": serialize_report_summary(report, new_version, subject=resolved_subject),
        "version": serialize_report_version(new_version),
        "parent_version": serialize_report_version(version),
    }


def serialize_report_version(version: ResearchReportVersion) -> dict[str, Any]:
    return {
        "id": int(version.id),
        "report_id": int(version.report_id),
        "version": int(version.version),
        "status": version.status,
        "trigger_kind": version.trigger_kind,
        "trigger_label": version.trigger_label,
        "change_fingerprint": version.change_fingerprint,
        "as_of_at": _dt_iso(version.as_of_at),
        "created_at": _dt_iso(version.created_at),
        "updated_at": _dt_iso(version.updated_at),
        "published_at": _dt_iso(version.published_at),
        "error": version.error,
        "coverage_json": dict(version.coverage_json or {}),
        "memory_quality": report_memory_quality(version),
        "memory_review": report_memory_review_recommendation(version),
        "artifacts": _artifact_entries(version),
    }


def serialize_report_summary(report: ResearchReport, version: Any | None, *, subject: ReportSubject | None = None) -> dict[str, Any]:
    resolved_subject = subject
    if resolved_subject is None:
        resolved_subject = ReportSubject(
            subject_row=None,
            issuer=None,
            company=None,
            primary_entity=None,
            subject_id=int(report.subject_id) if report.subject_id is not None else None,
            issuer_id=int(report.issuer_id) if report.issuer_id is not None else None,
            company_id=int(report.company_id) if report.company_id is not None else None,
            primary_entity_id=None,
            kind=str((report.extra or {}).get("subject_kind") or "company"),
            slug=report.slug,
            subject_key=report.subject_key,
            display_name=report.title.replace("Research Report — ", "", 1),
            legal_name=None,
            description=None,
            primary_ticker=None,
            tickers=[],
            aliases=[],
        )
    metadata = _report_metadata(report, version, resolved_subject)
    freshness = _report_freshness(version)
    return {
        "id": int(report.id) if report.id is not None else None,
        "slug": report.slug,
        "subject_key": report.subject_key,
        "subject_id": resolved_subject.subject_id,
        "subject_kind": resolved_subject.kind,
        "title": report.title,
        "status": report.status,
        "issuer_id": report.issuer_id,
        "company_id": report.company_id,
        "display_name": resolved_subject.display_name,
        "primary_ticker": resolved_subject.primary_ticker,
        "report_type": metadata["report_type"],
        "report_type_label": metadata["report_type_label"],
        "summary": metadata["summary"],
        "description": metadata["description"],
        "tags": metadata["tags"],
        "topics": metadata["topics"],
        "companies": metadata["companies"],
        "supply_chains": metadata["supply_chains"],
        "materials": metadata["materials"],
        "technologies": metadata["technologies"],
        "linked_objects": metadata["linked_objects"],
        "source_metadata": metadata["source_metadata"],
        "upload_provenance": metadata["upload_provenance"],
        "imported": metadata["imported"],
        "manual_addenda": metadata["manual_addenda"],
        "freshness": freshness,
        "latest_published_version_id": int(version.id) if version is not None else None,
        "latest_version": int(version.version) if version is not None else None,
        "latest_published_at": _dt_iso(version.published_at if version is not None else None),
        "latest_as_of_at": _dt_iso(version.as_of_at if version is not None else None),
        "coverage_gaps": list((version.coverage_json or {}).get("gaps") or []) if version is not None else [],
        "memory_quality": report_memory_quality(version),
        "memory_review": report_memory_review_recommendation(version),
        "href": f"/reports/{report.slug}",
    }


def _transient_report_for_subject(subject: ReportSubject) -> ResearchReport:
    now = utcnow()
    return ResearchReport(
        subject_id=subject.subject_id,
        slug=subject.slug,
        subject_key=subject.subject_key,
        issuer_id=subject.issuer_id,
        company_id=subject.company_id,
        title=f"Research Report — {subject.display_name}",
        status="active",
        latest_published_version_id=None,
        latest_subject_slug=subject.slug,
        extra={"subject_kind": subject.kind},
        created_at=now,
        updated_at=now,
    )


def _report_public_base_url() -> str:
    base = (getattr(settings, "public_base_url", None) or "").strip()
    if not base:
        base = f"http://{settings.host}:{settings.port}"
    return base.rstrip("/")


def get_research_report_state(
    session: Session,
    *,
    subject: str | int | ResearchReport,
    version_id: int | None = None,
) -> dict[str, Any]:
    resolved_subject = resolve_report_subject(session, subject)
    report = session.query(ResearchReport).filter_by(subject_key=resolved_subject.subject_key).first()
    if report is None:
        report = _transient_report_for_subject(resolved_subject)

    version: ResearchReportVersion | None = None
    if version_id is not None and report.id is not None:
        version = (
            session.query(ResearchReportVersion)
            .filter_by(report_id=int(report.id), id=int(version_id))
            .first()
        )
    if version is None and report.id is not None:
        version = _latest_published_version(session, int(report.id))

    report_summary = serialize_report_summary(report, version, subject=resolved_subject)
    version_payload = serialize_report_version(version) if version is not None else None
    coverage_json = dict(version.coverage_json or {}) if version is not None else {}
    metrics = [dict(row) for row in (coverage_json.get("metrics") or []) if isinstance(row, dict)]
    gaps = [str(row).strip() for row in (coverage_json.get("gaps") or []) if str(row).strip()]
    artifacts = _artifact_entries(version) if version is not None else []
    artifact_links: dict[str, str] = {}
    artifact_labels: dict[str, str] = {}
    for artifact in artifacts:
        artifact_type = str(artifact.get("artifact_type") or "").strip()
        object_url = str(artifact.get("object_url") or "").strip()
        if not artifact_type or not object_url or artifact_type in artifact_links:
            continue
        artifact_links[artifact_type] = (
            object_url if object_url.startswith("http://") or object_url.startswith("https://") else f"{_report_public_base_url()}{object_url}"
        )
        label = str(artifact.get("label") or "").strip()
        if label:
            artifact_labels[artifact_type] = label

    freshness = _report_freshness(version)

    return {
        "report": report_summary,
        "version": version_payload,
        "subject": {
            "display_name": resolved_subject.display_name,
            "kind": resolved_subject.kind,
            "slug": resolved_subject.slug,
            "subject_key": resolved_subject.subject_key,
            "primary_ticker": resolved_subject.primary_ticker,
            "tickers": list(resolved_subject.tickers),
            "aliases": list(resolved_subject.aliases),
            "description": resolved_subject.description,
        },
        "coverage": {
            "metrics": metrics,
            "gaps": gaps,
        },
        "artifacts": {
            "rows": artifacts,
            "links": artifact_links,
            "labels": artifact_labels,
        },
        "freshness": freshness,
        "report_url": f"{_report_public_base_url()}/reports/{report.slug}",
    }


def _summary_matches_text(summary: dict[str, Any], text: str) -> bool:
    needle = str(text or "").strip().lower()
    if not needle:
        return True
    values: list[object] = [
        summary.get("slug"),
        summary.get("subject_key"),
        summary.get("title"),
        summary.get("display_name"),
        summary.get("primary_ticker"),
        summary.get("report_type"),
        summary.get("report_type_label"),
        summary.get("summary"),
        summary.get("description"),
    ]
    for key in ("tags", "topics", "companies", "supply_chains", "materials", "technologies"):
        values.extend(summary.get(key) or [])
    for addendum in summary.get("manual_addenda") or []:
        if not isinstance(addendum, dict):
            continue
        values.extend(
            [
                addendum.get("title"),
                addendum.get("summary"),
                addendum.get("body_md"),
                addendum.get("body_preview"),
            ]
        )
        values.extend(addendum.get("tags") or [])
    linked = summary.get("linked_objects")
    if isinstance(linked, dict):
        for vals in linked.values():
            values.extend(vals or [])
    return any(needle in str(value or "").lower() for value in values)


def _summary_matches_filter(summary: dict[str, Any], key: str, value: str | None) -> bool:
    needle = str(value or "").strip().lower()
    if not needle:
        return True
    if key == "report_type":
        return normalize_report_type(summary.get("report_type")) == normalize_report_type(needle)
    if key == "status":
        return str(summary.get("status") or "").strip().lower() == needle
    if key == "freshness":
        freshness = summary.get("freshness") if isinstance(summary.get("freshness"), dict) else {}
        return str(freshness.get("state") or "").strip().lower() == needle
    values = summary.get(key) or []
    if not isinstance(values, list):
        values = [values]
    return any(needle in str(item or "").lower() for item in values)


def list_research_reports(
    session: Session,
    *,
    query: str | None = None,
    report_type: str | None = None,
    topic: str | None = None,
    company: str | None = None,
    supply_chain: str | None = None,
    material: str | None = None,
    technology: str | None = None,
    status: str | None = None,
    freshness: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    q = session.query(ResearchReport).order_by(ResearchReport.updated_at.desc(), ResearchReport.id.desc())
    limit_val = max(1, min(int(limit or 50), 200))
    has_filters = any([query, report_type, topic, company, supply_chain, material, technology, status, freshness])
    if has_filters:
        max_candidates = 1000
        chunk_size = max(100, limit_val)
    else:
        max_candidates = min(limit_val * 3, 600)
        chunk_size = limit_val
    out: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    query_text = str(query or "").strip()
    for offset in range(0, max_candidates, chunk_size):
        report_chunk = q.limit(chunk_size).offset(offset).all()
        if not report_chunk:
            break
        latest_by_report_id = _latest_published_versions_by_report_id(session, report_chunk)
        subject_by_report_id = _report_subjects_by_report_id(session, report_chunk)
        for report in report_chunk:
            report_id = int(report.id)
            latest = latest_by_report_id.get(report_id)
            subject = subject_by_report_id.get(report_id) or resolve_report_subject(session, report)
            canonical_key = _canonical_report_subject_key(
                issuer_id=subject.issuer_id,
                company_id=subject.company_id,
                entity_id=subject.primary_entity_id,
                fallback=subject.subject_key,
            )
            if canonical_key in seen_keys:
                continue
            seen_keys.add(canonical_key)
            summary = serialize_report_summary(report, latest, subject=subject)
            if not _summary_matches_text(summary, query_text):
                continue
            if not _summary_matches_filter(summary, "report_type", report_type):
                continue
            if not _summary_matches_filter(summary, "topics", topic):
                continue
            if not _summary_matches_filter(summary, "companies", company):
                continue
            if not _summary_matches_filter(summary, "supply_chains", supply_chain):
                continue
            if not _summary_matches_filter(summary, "materials", material):
                continue
            if not _summary_matches_filter(summary, "technologies", technology):
                continue
            if not _summary_matches_filter(summary, "status", status):
                continue
            if not _summary_matches_filter(summary, "freshness", freshness):
                continue
            out.append(summary)
            if len(out) >= limit_val:
                break
        if len(out) >= limit_val:
            break
        if len(report_chunk) < chunk_size:
            break
    return out


def list_research_report_versions(session: Session, *, subject: str | int | ResearchReport | None = None, report_id: int | None = None, limit: int = 20) -> list[dict[str, Any]]:
    if report_id is None:
        if subject is None:
            raise ValueError("subject or report_id is required")
        resolved = resolve_report_subject(session, subject)
        report = session.query(ResearchReport).filter_by(subject_key=resolved.subject_key).first()
        if report is None or report.id is None:
            return []
        report_id = int(report.id)
    rows = (
        session.query(ResearchReportVersion)
        .filter(ResearchReportVersion.report_id == int(report_id))
        .order_by(ResearchReportVersion.version.desc(), ResearchReportVersion.id.desc())
        .limit(max(1, min(int(limit or 20), 100)))
        .all()
    )
    return [serialize_report_version(row) for row in rows]


def build_research_report(
    session: Session,
    *,
    subject: str | int | ResearchReport,
    trigger_kind: str = "manual",
    trigger_label: str | None = None,
    force: bool = False,
    window_days: int | None = None,
) -> ResearchReportBuildResult:
    resolved_subject = resolve_report_subject(session, subject)
    report = _ensure_report_container(session, resolved_subject)
    latest = _latest_published_version(session, int(report.id))
    payload, meta = assemble_report_payload(
        session,
        subject=resolved_subject,
        report=report,
        window_days=window_days,
    )
    fingerprint = str(meta.get("change_fingerprint") or "")
    now = utcnow()

    if latest is not None and not force:
        if latest.change_fingerprint == fingerprint:
            return ResearchReportBuildResult(report=report, version=latest, created_version=False, reason="unchanged", subject=resolved_subject)
        if latest.published_at is not None and (now - latest.published_at) < REPORT_STALE_AFTER:
            return ResearchReportBuildResult(report=report, version=latest, created_version=False, reason="fresh", subject=resolved_subject)

    max_version = (
        session.query(func.max(ResearchReportVersion.version))
        .filter(ResearchReportVersion.report_id == int(report.id))
        .scalar()
    )
    next_version = int(max_version or 0) + 1
    version = ResearchReportVersion(
        report_id=int(report.id),
        version=next_version,
        status="building",
        trigger_kind=str(trigger_kind or "manual"),
        trigger_label=_clean_text(trigger_label) or None,
        trigger_payload={"subject": resolved_subject.subject_key, "window_days": int(payload.get("window_days") or REPORT_DEFAULT_WINDOW_DAYS)},
        change_fingerprint=fingerprint,
        content_json=payload,
        content_md="",
        coverage_json=dict(meta.get("coverage") or {}),
        as_of_at=now,
        error=None,
        created_at=now,
        updated_at=now,
        published_at=None,
    )
    session.add(version)
    session.flush()

    try:
        base_payload = dict(payload)
        image_map = _persist_supply_chain_pngs(session, version=version, chains=list((payload.get("supply_chain_peer_context") or {}).get("chains") or []))
        pdf_bytes = asyncio.run(_playwright_pdf_bytes(_render_print_html(base_payload, image_map=image_map)))
        if pdf_bytes:
            _upsert_artifact(session, version=version, artifact_type="pdf", variant="default", label=f"{resolved_subject.display_name} research report PDF", data_bytes=pdf_bytes, content_type="application/pdf", payload={"subject": resolved_subject.subject_key})

        payload_with_artifacts = dict(base_payload)
        payload_with_artifacts["artifacts_export_appendix"] = {"rows": _artifact_entries(version)}
        markdown_text = _render_markdown(payload_with_artifacts)
        _upsert_artifact(session, version=version, artifact_type="json", variant="default", label=f"{resolved_subject.display_name} research report JSON", data_bytes=json.dumps(payload_with_artifacts, indent=2, ensure_ascii=False, default=_json_default).encode("utf-8"), content_type="application/json", payload={"subject": resolved_subject.subject_key})
        markdown_artifact = _upsert_artifact(session, version=version, artifact_type="markdown", variant="default", label=f"{resolved_subject.display_name} research report Markdown", data_bytes=markdown_text.encode("utf-8"), content_type="text/markdown", payload={"subject": resolved_subject.subject_key})

        export_path = _persist_export_bundle(version, payload=payload_with_artifacts, markdown_text=markdown_text)
        payload_with_artifacts["artifacts_export_appendix"] = {"rows": _artifact_entries(version), "export_bundle": export_path}
        payload_with_artifacts["reader"] = {
            "format": "markdown",
            "artifact_id": int(markdown_artifact.id),
            "reader_url": f"/reports/{report.slug}/reader?version_id={int(version.id)}&artifact_id={int(markdown_artifact.id)}",
        }
        version.status = "published"
        version.content_json = payload_with_artifacts
        version.content_md = markdown_text
        version.coverage_json = dict(meta.get("coverage") or {})
        version.as_of_at = now
        version.updated_at = utcnow()
        version.published_at = utcnow()
        version.error = None
        report.latest_published_version_id = int(version.id)
        report.updated_at = utcnow()
        session.flush()
        if report.subject_id is not None:
            from thoth_terminal.research_canon_service import post_publish_report_canon_refresh

            post_publish_report_canon_refresh(session, report=report, version=version)
        return ResearchReportBuildResult(report=report, version=version, created_version=True, reason="published", subject=resolved_subject)
    except Exception as exc:
        version.status = "failed"
        version.error = f"{type(exc).__name__}: {exc}"
        version.updated_at = utcnow()
        report.updated_at = utcnow()
        session.flush()
        raise


BOTTLENECK_REPORT_GAP_KINDS = (
    "missing_bottleneck_canon_scenario",
    "missing_bottleneck_evidence",
    "missing_bottleneck_exposed_companies",
    "missing_bottleneck_scope_card",
    "missing_bottleneck_qualified_supply",
    "missing_bottleneck_allocation_model",
    "missing_bottleneck_metric_bindings",
    "missing_bottleneck_shadow_constraints",
    "missing_bottleneck_falsification_tests",
    "missing_bottleneck_source_attribution",
)


def _bottleneck_report_slug(model: BottleneckModel) -> str:
    return _slugify(f"bottleneck-{model.model_key or model.title}", default=f"bottleneck-{int(model.id or 0)}")[:128]


def _ensure_bottleneck_report_subject(session: Session, model: BottleneckModel) -> ReportSubject:
    now = utcnow()
    slug = _bottleneck_report_slug(model)
    subject_key = f"bottleneck_model:{int(model.id)}"
    description = _clean_text(model.summary or model.thesis or f"Bottleneck report workspace for {model.title}") or None
    row = session.query(ResearchSubject).filter_by(subject_key=subject_key).first()
    if row is None:
        row = ResearchSubject(
            slug=slug,
            subject_key=subject_key,
            kind="bottleneck_model",
            display_name=model.title,
            legal_name=None,
            description=description,
            status="active",
            issuer_id=None,
            company_id=None,
            primary_entity_id=None,
            extra={},
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    row.slug = slug
    row.kind = "bottleneck_model"
    row.display_name = model.title
    row.description = description
    extra = dict(row.extra or {})
    extra.update(
        {
            "report_type": "bottleneck_report",
            "bottleneck_model_id": int(model.id),
            "model_key": model.model_key,
            "constrained_input": model.constrained_input,
            "output_unit": model.output_unit,
        }
    )
    row.extra = extra
    row.updated_at = now
    session.flush()
    return _subject_from_rows(session, subject_row=row)


def _ensure_bottleneck_report_container(session: Session, model: BottleneckModel, subject: ReportSubject) -> ResearchReport:
    now = utcnow()
    slug = _bottleneck_report_slug(model)
    report = session.query(ResearchReport).filter_by(subject_key=subject.subject_key).first()
    if report is None:
        report = ResearchReport(
            subject_id=subject.subject_id,
            slug=slug,
            subject_key=subject.subject_key,
            issuer_id=None,
            company_id=None,
            title=f"Bottleneck Report - {model.title}",
            status="active",
            latest_published_version_id=None,
            latest_subject_slug=slug,
            extra={},
            created_at=now,
            updated_at=now,
        )
        session.add(report)
    report.subject_id = subject.subject_id
    report.slug = slug
    report.subject_key = subject.subject_key
    report.title = f"Bottleneck Report - {model.title}"
    report.status = "active"
    report.latest_subject_slug = slug
    report.extra = {
        **dict(report.extra or {}),
        "report_type": "bottleneck_report",
        "report_type_label": report_type_label("bottleneck_report"),
        "subject_kind": "bottleneck_model",
        "summary": _clean_text(model.summary or model.thesis or model.title),
        "description": _clean_text(model.summary or model.thesis or model.title),
        "topics": _split_metadata_values(["bottleneck", model.constrained_input, model.output_unit, model.regime]),
        "linked_objects": {"bottleneck_models": [str(model.model_key or model.id)]},
        "bottleneck_model_id": int(model.id),
        "model_key": model.model_key,
    }
    report.updated_at = now
    session.flush()
    return report


def _serialize_bottleneck_assessment(row: BottleneckAssessment) -> dict[str, Any]:
    axes = {
        "bindingness": row.bindingness_score,
        "shadow_price": row.shadow_price_score,
        "ramp_resistance": row.ramp_resistance_score,
        "substitutability": row.substitutability_score,
        "persistence": row.persistence_score,
        "latent_demand_absorption": row.latent_demand_absorption_score,
    }
    return {
        "id": int(row.id),
        "model_id": int(row.model_id) if row.model_id is not None else None,
        "scope_type": row.scope_type,
        "scope_key": row.scope_key,
        "active_node_key": row.active_node_key,
        "shadow_node_key": row.shadow_node_key,
        "constrained_input": row.constrained_input,
        "output_unit": row.output_unit,
        "regime": row.regime,
        "classification": row.classification,
        "summary": row.summary,
        "confidence": row.confidence,
        "axes": {key: value for key, value in axes.items() if value is not None},
        "qualified_supplier_count": int(row.qualified_supplier_count) if row.qualified_supplier_count is not None else None,
        "marginal_system_leverage": dict(row.marginal_system_leverage or {}),
        "supplier_qualification": dict(row.supplier_qualification_json or {}),
        "evidence": list(row.evidence_json or []),
        "scorecard": dict(row.scorecard_json or {}),
        "payload": dict(row.payload or {}),
        "updated_at": _dt_iso(row.updated_at),
    }


def _bottleneck_model_open_gaps(session: Session, model_id: int) -> list[dict[str, Any]]:
    rows = (
        session.query(CoverageGap)
        .filter(CoverageGap.owner_ref == "research_canon", CoverageGap.status == "open", CoverageGap.gap_kind.in_(BOTTLENECK_REPORT_GAP_KINDS))
        .order_by(CoverageGap.severity.desc(), CoverageGap.updated_at.desc(), CoverageGap.id.desc())
        .limit(500)
        .all()
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        payload = dict(row.payload or {})
        if int(payload.get("bottleneck_model_id") or -1) != int(model_id):
            continue
        out.append(
            {
                "id": int(row.id),
                "gap_key": row.gap_key,
                "gap_kind": row.gap_kind,
                "title": row.title,
                "severity": int(row.severity or 0),
                "summary": row.summary,
                "suggested_rails": list(row.suggested_rails or []),
                "payload": payload,
                "updated_at": _dt_iso(row.updated_at),
            }
        )
    return out


def _brief_text(value: object, *, limit: int = 700) -> str | None:
    text = _clean_text(value)
    if not text:
        return None
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _brief_json(value: object, *, limit: int = 1200) -> dict[str, Any] | list[Any] | str | None:
    if value in (None, "", [], {}):
        return None
    try:
        rendered = json.dumps(value, ensure_ascii=False, default=_json_default, sort_keys=True)
    except Exception:
        rendered = str(value)
    if len(rendered) <= limit:
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, list):
            return list(value)
        return value if isinstance(value, str) else rendered
    return {"excerpt": rendered[: max(0, limit - 3)].rstrip() + "...", "truncated": True}


def _dateish_iso(value: object) -> str | None:
    if value is None:
        return None
    iso = getattr(value, "isoformat", None)
    if callable(iso):
        return str(iso())
    return str(value)


def _link_object_type(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def _bottleneck_link_meta(link: dict[str, Any]) -> dict[str, Any]:
    return {
        "link_id": int(link.get("id") or 0) or None,
        "object_type": link.get("object_type"),
        "object_id": int(link.get("object_id") or 0) or None,
        "relation": link.get("relation"),
        "payload": _brief_json(link.get("payload"), limit=800) or {},
    }


def _source_actor_card(row: SourceActor | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": int(row.id),
        "actor_key": row.actor_key,
        "display_name": row.display_name,
        "actor_type": row.actor_type,
        "side": row.side,
        "trust_label": row.trust_label,
        "status": row.status,
        "notes": _brief_text(row.notes, limit=300),
        "payload": _brief_json(row.payload, limit=600) or {},
    }


def _bottleneck_analyst_evidence_row(row: AnalystEvidence, link: dict[str, Any]) -> dict[str, Any]:
    payload = dict(row.payload or {})
    forecast = payload.get("forecast") if isinstance(payload.get("forecast"), dict) else {}
    price = payload.get("price_target_analysis") if isinstance(payload.get("price_target_analysis"), dict) else {}
    return {
        "type": "analyst_evidence",
        "title": _brief_text(row.summary, limit=180) or row.evidence_key,
        "id": int(row.id),
        "evidence_key": row.evidence_key,
        "evidence_kind": row.evidence_kind,
        "status": row.status,
        "source_name": row.source_name,
        "author": row.author,
        "outlet": row.outlet,
        "published_at": _dateish_iso(row.published_at),
        "source_url": row.source_url,
        "summary": _brief_text(row.summary),
        "subject_id": int(row.subject_id) if row.subject_id is not None else None,
        "issuer_id": int(row.issuer_id) if row.issuer_id is not None else None,
        "company_id": int(row.company_id) if row.company_id is not None else None,
        "source_document_id": int(row.source_document_id) if row.source_document_id is not None else None,
        "metric": {
            "estimate_metric": row.estimate_metric,
            "estimate_period": row.estimate_period,
            "estimate_value": row.estimate_value,
            "estimate_unit": row.estimate_unit,
            "rating": row.rating,
            "prior_rating": row.prior_rating,
            "price_target": row.price_target,
            "prior_price_target": row.prior_price_target,
            "currency": row.currency,
        },
        "source_attribution": _brief_json(payload.get("source_attribution"), limit=1000) or {},
        "source_relations": _brief_json(payload.get("source_relations"), limit=900) or [],
        "forecast": _brief_json(forecast, limit=1000) or {},
        "price_target_analysis": _brief_json(price, limit=700) or {},
        "observed_source_actor": _source_actor_card(row.observed_source_actor),
        "underlying_source_actor": _source_actor_card(row.underlying_source_actor),
        "rights_class": row.rights_class,
        "trust_label": row.trust_label,
        "confidence": row.confidence,
        "link": _bottleneck_link_meta(link),
    }


def _bottleneck_institutional_observation_row(row: InstitutionalObservation, link: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "institutional_observation",
        "title": _brief_text(row.metric_key or row.kind or row.observation_key, limit=180) or row.observation_key,
        "id": int(row.id),
        "observation_key": row.observation_key,
        "evidence_id": int(row.evidence_id) if row.evidence_id is not None else None,
        "dataset_family": row.dataset_family,
        "kind": row.kind,
        "metric_key": row.metric_key,
        "value_number": row.value_number,
        "value_text": _brief_text(row.value_text, limit=500),
        "unit": row.unit,
        "currency": row.currency,
        "confidence": row.confidence,
        "period_date": _dateish_iso(row.period_date),
        "as_of_date": _dateish_iso(row.as_of_date),
        "observed_at": _dateish_iso(row.observed_at),
        "review_status": row.review_status,
        "subject_id": int(row.subject_id) if row.subject_id is not None else None,
        "issuer_id": int(row.issuer_id) if row.issuer_id is not None else None,
        "company_id": int(row.company_id) if row.company_id is not None else None,
        "site_id": int(row.site_id) if row.site_id is not None else None,
        "program_id": int(row.program_id) if row.program_id is not None else None,
        "payload": _brief_json(row.payload, limit=1000) or {},
        "link": _bottleneck_link_meta(link),
    }


def _bottleneck_institutional_evidence_row(session: Session, row: InstitutionalEvidenceItem, link: dict[str, Any]) -> dict[str, Any]:
    observations = (
        session.query(InstitutionalObservation)
        .filter(InstitutionalObservation.evidence_id == int(row.id))
        .order_by(InstitutionalObservation.updated_at.desc(), InstitutionalObservation.id.desc())
        .limit(10)
        .all()
    )
    return {
        "type": "institutional_evidence",
        "title": row.title,
        "id": int(row.id),
        "evidence_key": row.evidence_key,
        "evidence_kind": row.evidence_kind,
        "provenance_summary": _brief_text(row.provenance_summary),
        "url": row.url,
        "canonical_url": row.canonical_url,
        "published_at": _dateish_iso(row.published_at),
        "observed_at": _dateish_iso(row.observed_at),
        "rights_class": row.rights_class,
        "review_required": bool(row.review_required),
        "confidence": row.confidence,
        "subject_id": int(row.subject_id) if row.subject_id is not None else None,
        "issuer_id": int(row.issuer_id) if row.issuer_id is not None else None,
        "company_id": int(row.company_id) if row.company_id is not None else None,
        "site_id": int(row.site_id) if row.site_id is not None else None,
        "program_id": int(row.program_id) if row.program_id is not None else None,
        "payload": _brief_json(row.payload, limit=1200) or {},
        "observations": [_bottleneck_institutional_observation_row(obs, link) for obs in observations],
        "link": _bottleneck_link_meta(link),
    }


def _bottleneck_dataset_row(session: Session, row: Dataset, link: dict[str, Any]) -> dict[str, Any]:
    version = (
        session.query(DatasetVersion)
        .filter(DatasetVersion.dataset_id == int(row.id))
        .order_by(DatasetVersion.status.desc(), DatasetVersion.updated_at.desc(), DatasetVersion.id.desc())
        .first()
    )
    columns: list[dict[str, Any]] = []
    sample_rows: list[dict[str, Any]] = []
    if version is not None:
        columns = [
            {
                "key": col.key,
                "label": col.label,
                "data_type": col.data_type,
                "role": col.role,
            }
            for col in session.query(DatasetColumn)
            .filter(DatasetColumn.dataset_version_id == int(version.id))
            .order_by(DatasetColumn.sort_order.asc(), DatasetColumn.id.asc())
            .limit(40)
            .all()
        ]
        sample_rows = [
            {
                "row_key": data_row.row_key,
                "values": _brief_json(data_row.values, limit=1200) or {},
            }
            for data_row in session.query(DatasetRow)
            .filter(DatasetRow.dataset_version_id == int(version.id))
            .order_by(DatasetRow.sort_order.asc(), DatasetRow.id.asc())
            .limit(8)
            .all()
        ]
    return {
        "type": "dataset",
        "title": row.title,
        "id": int(row.id),
        "slug": row.slug,
        "description": _brief_text(row.description),
        "category": row.category,
        "family": row.family,
        "status": row.status,
        "source_kind": row.source_kind,
        "latest_version": {
            "id": int(version.id) if version is not None else None,
            "version": int(version.version) if version is not None else None,
            "status": version.status if version is not None else None,
            "label": version.label if version is not None else None,
        },
        "columns": columns,
        "sample_rows": sample_rows,
        "extra": _brief_json(row.extra, limit=1000) or {},
        "link": _bottleneck_link_meta(link),
    }


def _bottleneck_document_row(row: Document, link: dict[str, Any]) -> dict[str, Any]:
    text = row.translation_en or row.normalized_text or row.raw_text or ""
    return {
        "type": "document",
        "title": row.title,
        "id": int(row.id),
        "source_id": int(row.source_id) if row.source_id is not None else None,
        "url": row.url,
        "canonical_url": row.canonical_url,
        "author": row.author,
        "published_at": _dateish_iso(row.published_at),
        "language": row.language,
        "text_excerpt": _brief_text(text, limit=1000),
        "extra": _brief_json(row.extra, limit=900) or {},
        "link": _bottleneck_link_meta(link),
    }


def _bottleneck_linked_evidence_pack(session: Session, links: list[dict[str, Any]]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for link in links:
        object_type = _link_object_type(link.get("object_type"))
        try:
            object_id = int(link.get("object_id"))
        except Exception:
            continue
        hydrated: dict[str, Any] | None = None
        canonical_type = object_type
        if object_type in {"analyst_evidence", "analyst_evidence_item", "sell_side_evidence", "consensus_evidence", "analyst"}:
            row = session.get(AnalystEvidence, object_id)
            if row is not None:
                canonical_type = "analyst_evidence"
                hydrated = _bottleneck_analyst_evidence_row(row, link)
        elif object_type in {"institutional_evidence", "institutional_evidence_item", "institutional_evidence_items", "evidence_item"}:
            row = session.get(InstitutionalEvidenceItem, object_id)
            if row is not None:
                canonical_type = "institutional_evidence"
                hydrated = _bottleneck_institutional_evidence_row(session, row, link)
        elif object_type in {"institutional_observation", "institutional_observations", "observation"}:
            row = session.get(InstitutionalObservation, object_id)
            if row is not None:
                canonical_type = "institutional_observation"
                hydrated = _bottleneck_institutional_observation_row(row, link)
        elif object_type in {"dataset", "datasets"}:
            row = session.get(Dataset, object_id)
            if row is not None:
                canonical_type = "dataset"
                hydrated = _bottleneck_dataset_row(session, row, link)
        elif object_type in {"document", "documents", "source_document"}:
            row = session.get(Document, object_id)
            if row is not None:
                canonical_type = "document"
                hydrated = _bottleneck_document_row(row, link)
        elif object_type in {"source_actor", "source_actors", "observed_source_actor", "underlying_source_actor"}:
            row = session.get(SourceActor, object_id)
            if row is not None:
                canonical_type = "source_actor"
                hydrated = {
                    "type": "source_actor",
                    "title": row.display_name,
                    "source_actor": _source_actor_card(row),
                    "link": _bottleneck_link_meta(link),
                }
        if hydrated is None:
            missing.append(_bottleneck_link_meta(link))
            continue
        counts[canonical_type] = counts.get(canonical_type, 0) + 1
        rows.append(hydrated)
    return {
        "rows": rows,
        "count": len(rows),
        "counts_by_type": counts,
        "missing": missing[:50],
        "provenance_rule": (
            "Rows are hydrated from BottleneckModelLink targets. Preserve the linked object id, observed source, "
            "underlying source, document id, and relation when using them in report reasoning."
        ),
    }


_BOTTLENECK_CANDIDATE_STOPWORDS = {
    "and",
    "are",
    "for",
    "from",
    "into",
    "may",
    "model",
    "report",
    "supply",
    "that",
    "the",
    "this",
    "unit",
    "with",
}


def _candidate_term_strings(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, dict):
        out: list[str] = []
        for key, item in value.items():
            out.extend(_candidate_term_strings(key))
            out.extend(_candidate_term_strings(item))
        return out
    if isinstance(value, (list, tuple, set)):
        out = []
        for item in value:
            out.extend(_candidate_term_strings(item))
        return out
    if isinstance(value, (str, int, float)):
        return [str(value)]
    return []


def _bottleneck_candidate_search_terms(model: BottleneckModel, subject: ReportSubject) -> list[str]:
    raw_values: list[object] = [
        model.title,
        model.scope_key,
        model.constrained_input,
        model.output_unit,
        model.regime,
        model.summary,
        model.thesis,
        subject.display_name,
        subject.subject_key,
        subject.slug,
        subject.description,
        subject.aliases,
        model.payload,
        model.supplier_qualification_json,
        model.evidence_json,
    ]
    terms: list[str] = []

    def add(value: object) -> None:
        text = re.sub(r"\s+", " ", str(value or "").replace("_", " ").strip().lower())
        text = re.sub(r"[^a-z0-9$%.+\-/ ]+", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) < 3 or text in _BOTTLENECK_CANDIDATE_STOPWORDS:
            return
        if text not in terms:
            terms.append(text)

    for raw in raw_values:
        for text in _candidate_term_strings(raw):
            add(text)
            for token in re.split(r"[^A-Za-z0-9$%.+\-/]+", text):
                token_norm = token.strip().lower().replace("_", " ")
                if len(token_norm) >= 4:
                    add(token_norm)
    return terms[:32]


def _candidate_match_terms(text: object, terms: list[str]) -> list[str]:
    blob = re.sub(r"\s+", " ", str(text or "").replace("_", " ").lower())
    matched: list[str] = []
    for term in terms:
        if term and term in blob and term not in matched:
            matched.append(term)
    return matched[:12]


def _candidate_link(object_type: str, object_id: int, *, match_terms: list[str], score: int) -> dict[str, Any]:
    return {
        "id": None,
        "object_type": object_type,
        "object_id": int(object_id),
        "relation": "candidate_supports",
        "payload": {
            "candidate": True,
            "match_terms": match_terms[:8],
            "match_score": int(score),
            "promotion_rule": "Review before creating a BottleneckModelLink; this candidate pack is read-only.",
        },
    }


def _candidate_query_filter(columns: list[Any], terms: list[str]) -> Any | None:
    conditions = []
    for term in terms[:18]:
        like = f"%{term}%"
        for column in columns:
            conditions.append(column.ilike(like))
    if not conditions:
        return None
    return or_(*conditions)


def _linked_bottleneck_keys(links: list[dict[str, Any]]) -> set[tuple[str, int]]:
    aliases = {
        "analyst_evidence_item": "analyst_evidence",
        "sell_side_evidence": "analyst_evidence",
        "consensus_evidence": "analyst_evidence",
        "institutional_evidence_item": "institutional_evidence",
        "institutional_evidence_items": "institutional_evidence",
        "evidence_item": "institutional_evidence",
        "institutional_observations": "institutional_observation",
        "observation": "institutional_observation",
        "datasets": "dataset",
        "documents": "document",
        "source_document": "document",
        "source_actors": "source_actor",
    }
    keys: set[tuple[str, int]] = set()
    for link in links:
        object_type = aliases.get(_link_object_type(link.get("object_type")), _link_object_type(link.get("object_type")))
        try:
            object_id = int(link.get("object_id"))
        except Exception:
            continue
        keys.add((object_type, object_id))
    return keys


def _bottleneck_candidate_sell_side_evidence_pack(
    session: Session,
    *,
    model: BottleneckModel,
    subject: ReportSubject,
    links: list[dict[str, Any]],
    limit: int = 16,
) -> dict[str, Any]:
    terms = _bottleneck_candidate_search_terms(model, subject)
    linked_keys = _linked_bottleneck_keys(links)
    rows: list[dict[str, Any]] = []
    suggestions: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    seen: set[tuple[str, int]] = set()

    def add_row(object_type: str, object_id: int, row_payload: dict[str, Any], match_terms: list[str]) -> None:
        key = (object_type, int(object_id))
        if key in seen or key in linked_keys or len(rows) >= limit:
            return
        seen.add(key)
        counts[object_type] = counts.get(object_type, 0) + 1
        rows.append(row_payload)
        suggestions.append(
            {
                "object_type": object_type,
                "object_id": int(object_id),
                "relation": "candidate_supports",
                "payload": {
                    "candidate": True,
                    "match_terms": match_terms[:8],
                    "source": "bottleneck_candidate_sell_side_evidence_pack",
                },
            }
        )

    subject_ids = [int(subject.subject_id)] if subject.subject_id is not None else []
    issuer_ids = [int(subject.issuer.id)] if subject.issuer is not None else []
    company_ids = [int(subject.company.id)] if subject.company is not None else []
    text_filter = _candidate_query_filter(
        [
            AnalystEvidence.evidence_key,
            AnalystEvidence.source_name,
            AnalystEvidence.author,
            AnalystEvidence.outlet,
            AnalystEvidence.estimate_metric,
            AnalystEvidence.estimate_period,
            AnalystEvidence.summary,
            AnalystEvidence.source_url,
        ],
        terms,
    )
    analyst_filters = [AnalystEvidence.status == "active"]
    identity_filters = []
    if subject_ids:
        identity_filters.append(AnalystEvidence.subject_id.in_(subject_ids))
    if issuer_ids:
        identity_filters.append(AnalystEvidence.issuer_id.in_(issuer_ids))
    if company_ids:
        identity_filters.append(AnalystEvidence.company_id.in_(company_ids))
    if text_filter is not None:
        identity_filters.append(text_filter)
    if identity_filters:
        analyst_filters.append(or_(*identity_filters))
    for row in (
        session.query(AnalystEvidence)
        .filter(*analyst_filters)
        .order_by(AnalystEvidence.updated_at.desc(), AnalystEvidence.id.desc())
        .limit(60)
        .all()
    ):
        haystack = " ".join(
            str(part or "")
            for part in [
                row.evidence_key,
                row.source_name,
                row.author,
                row.outlet,
                row.estimate_metric,
                row.estimate_period,
                row.summary,
                row.source_url,
                row.payload,
            ]
        )
        matched = _candidate_match_terms(haystack, terms)
        if not matched and not (row.subject_id in subject_ids or row.issuer_id in issuer_ids or row.company_id in company_ids):
            continue
        link = _candidate_link("analyst_evidence", int(row.id), match_terms=matched, score=len(matched))
        add_row("analyst_evidence", int(row.id), _bottleneck_analyst_evidence_row(row, link), matched)

    text_filter = _candidate_query_filter(
        [
            InstitutionalEvidenceItem.evidence_key,
            InstitutionalEvidenceItem.title,
            InstitutionalEvidenceItem.evidence_kind,
            InstitutionalEvidenceItem.provenance_summary,
            InstitutionalEvidenceItem.url,
            InstitutionalEvidenceItem.canonical_url,
        ],
        terms,
    )
    institutional_filters = []
    identity_filters = []
    if subject_ids:
        identity_filters.append(InstitutionalEvidenceItem.subject_id.in_(subject_ids))
    if issuer_ids:
        identity_filters.append(InstitutionalEvidenceItem.issuer_id.in_(issuer_ids))
    if company_ids:
        identity_filters.append(InstitutionalEvidenceItem.company_id.in_(company_ids))
    if model.site_id is not None:
        identity_filters.append(InstitutionalEvidenceItem.site_id == int(model.site_id))
    if model.program_id is not None:
        identity_filters.append(InstitutionalEvidenceItem.program_id == int(model.program_id))
    if text_filter is not None:
        identity_filters.append(text_filter)
    if identity_filters:
        institutional_filters.append(or_(*identity_filters))
    for row in (
        session.query(InstitutionalEvidenceItem)
        .filter(*institutional_filters)
        .order_by(InstitutionalEvidenceItem.updated_at.desc(), InstitutionalEvidenceItem.id.desc())
        .limit(60)
        .all()
    ):
        haystack = " ".join(str(part or "") for part in [row.evidence_key, row.title, row.evidence_kind, row.provenance_summary, row.url, row.payload])
        matched = _candidate_match_terms(haystack, terms)
        if not matched and not (row.subject_id in subject_ids or row.issuer_id in issuer_ids or row.company_id in company_ids):
            continue
        link = _candidate_link("institutional_evidence", int(row.id), match_terms=matched, score=len(matched))
        add_row("institutional_evidence", int(row.id), _bottleneck_institutional_evidence_row(session, row, link), matched)

    text_filter = _candidate_query_filter(
        [
            InstitutionalObservation.observation_key,
            InstitutionalObservation.dataset_family,
            InstitutionalObservation.kind,
            InstitutionalObservation.metric_key,
            InstitutionalObservation.value_text,
            InstitutionalObservation.unit,
        ],
        terms,
    )
    observation_filters = [InstitutionalObservation.review_status != "rejected"]
    identity_filters = []
    if subject_ids:
        identity_filters.append(InstitutionalObservation.subject_id.in_(subject_ids))
    if issuer_ids:
        identity_filters.append(InstitutionalObservation.issuer_id.in_(issuer_ids))
    if company_ids:
        identity_filters.append(InstitutionalObservation.company_id.in_(company_ids))
    if model.site_id is not None:
        identity_filters.append(InstitutionalObservation.site_id == int(model.site_id))
    if model.program_id is not None:
        identity_filters.append(InstitutionalObservation.program_id == int(model.program_id))
    if text_filter is not None:
        identity_filters.append(text_filter)
    if identity_filters:
        observation_filters.append(or_(*identity_filters))
    for row in (
        session.query(InstitutionalObservation)
        .filter(*observation_filters)
        .order_by(InstitutionalObservation.updated_at.desc(), InstitutionalObservation.id.desc())
        .limit(80)
        .all()
    ):
        haystack = " ".join(
            str(part or "")
            for part in [row.observation_key, row.dataset_family, row.kind, row.metric_key, row.value_text, row.unit, row.payload]
        )
        matched = _candidate_match_terms(haystack, terms)
        if not matched and not (row.subject_id in subject_ids or row.issuer_id in issuer_ids or row.company_id in company_ids):
            continue
        link = _candidate_link("institutional_observation", int(row.id), match_terms=matched, score=len(matched))
        add_row("institutional_observation", int(row.id), _bottleneck_institutional_observation_row(row, link), matched)

    text_filter = _candidate_query_filter(
        [
            Dataset.slug,
            Dataset.title,
            Dataset.description,
            Dataset.category,
            Dataset.family,
            Dataset.source_kind,
        ],
        terms,
    )
    dataset_filters = [Dataset.status == "active"]
    if text_filter is not None:
        dataset_filters.append(text_filter)
    for row in (
        session.query(Dataset)
        .filter(*dataset_filters)
        .order_by(Dataset.updated_at.desc(), Dataset.id.desc())
        .limit(60)
        .all()
    ):
        haystack = " ".join(str(part or "") for part in [row.slug, row.title, row.description, row.category, row.family, row.source_kind, row.extra])
        matched = _candidate_match_terms(haystack, terms)
        if not matched:
            continue
        link = _candidate_link("dataset", int(row.id), match_terms=matched, score=len(matched))
        add_row("dataset", int(row.id), _bottleneck_dataset_row(session, row, link), matched)

    return {
        "rows": rows,
        "count": len(rows),
        "counts_by_type": counts,
        "suggested_links": suggestions,
        "query_terms": terms[:24],
        "provenance_rule": (
            "Candidate rows are discovered by bottleneck scope/search terms and are not curated links yet. "
            "Promote useful rows by creating BottleneckModelLink records through service, MCP, or API rails."
        ),
    }


def _bottleneck_startup_search_queries(payload: dict[str, Any]) -> list[str]:
    scope = payload.get("scope_card") or {}
    model = payload.get("bottleneck_model") or {}
    subject = payload.get("subject") or {}
    candidates = [
        f"{scope.get('constrained_input') or model.get('constrained_input') or ''} {scope.get('output_unit') or model.get('output_unit') or ''}",
        f"{model.get('title') or ''} sell-side channel check estimate capacity qualification",
        f"{subject.get('display_name') or ''} {scope.get('constrained_input') or ''} bottleneck",
        f"{scope.get('constrained_input') or ''} qualified supplier capacity yield lead time",
    ]
    cleaned: list[str] = []
    for value in candidates:
        text = re.sub(r"\s+", " ", str(value or "").strip())
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned[:6]


def _bottleneck_agent_startup_packet(payload: dict[str, Any]) -> dict[str, Any]:
    scope = payload.get("scope_card") or {}
    model = payload.get("bottleneck_model") or {}
    subject = payload.get("subject") or {}
    open_gaps = (payload.get("coverage_panel") or {}).get("open_gaps") or []
    search_queries = _bottleneck_startup_search_queries(payload)
    model_id = (scope.get("durable_anchors") or {}).get("bottleneck_model_id") or model.get("id")
    report_title = model.get("title") or subject.get("display_name") or "bottleneck report"
    role_prompts = [
        {
            "role": "gap_hunter_quick",
            "prompt": (
                f"Audit the startup workspace for `{report_title}`. Identify the smallest set of missing variables "
                "that would stop a useful bottleneck model, using the open gaps and candidate evidence pack first."
            ),
        },
        {
            "role": "evidence_harvester",
            "prompt": (
                "Use object_search, research library, datasets, linked evidence, and candidate sell-side evidence to collect "
                "source-backed facts. Preserve observed source, underlying source, document id, metric key, period, unit, and confidence."
            ),
        },
        {
            "role": "model_builder",
            "prompt": (
                "Build the closed allocation bridge from demand to constrained input to downstream output. Separate theoretical "
                "capacity from qualified/usable capacity, and keep shadow constraints explicit."
            ),
        },
        {
            "role": "gap_hunter_full",
            "prompt": (
                "After the first model pass, rerun sensitivity-first gap discovery. Rank the unknowns that can move the conclusion "
                "most, then turn them into concrete research requests or dataset requirements."
            ),
        },
        {
            "role": "challenger",
            "prompt": (
                "Attack the bottleneck thesis. Look for stale, duplicate, circular, single-source, or non-comparable evidence, and "
                "write the strongest falsification path before any report promotion."
            ),
        },
        {
            "role": "report_writer",
            "prompt": (
                "Write the bottleneck memo only after evidence and model checks are complete. Separate sourced facts, assumptions, "
                "inferences, scenario math, open gaps, and recommended next measurements."
            ),
        },
    ]
    next_mcp_calls = [
        {"tool": "get_bottleneck_model", "arguments": {"id": model_id}},
        {"tool": "read_report_content", "arguments": {"report_id": "<report_id_after_workspace_build>"}},
        {"tool": "object_search", "arguments": {"query": search_queries[0] if search_queries else str(report_title), "kind": "all", "limit": 12}},
        {"tool": "run_research_campaign_items", "arguments": {"campaign_key": "<campaign_key>", "limit": 1, "dry_run": False, "force": True}},
    ]
    return {
        "workspace_goal": (
            "Prepare a source-backed bottleneck report workspace that subagents can continue without relying on chat memory."
        ),
        "subject": subject,
        "bottleneck_model_id": model_id,
        "model_key": model.get("model_key"),
        "open_gap_count": len(open_gaps),
        "search_queries": search_queries,
        "role_prompts": role_prompts,
        "next_mcp_calls": next_mcp_calls,
        "candidate_sell_side_evidence": {
            "count": int((payload.get("candidate_sell_side_evidence_pack") or {}).get("count") or 0),
            "counts_by_type": dict((payload.get("candidate_sell_side_evidence_pack") or {}).get("counts_by_type") or {}),
        },
        "linked_evidence": {
            "count": int((payload.get("linked_evidence_pack") or {}).get("count") or 0),
            "counts_by_type": dict((payload.get("linked_evidence_pack") or {}).get("counts_by_type") or {}),
        },
        "runtime_contract": {
            "ocr_first": True,
            "vision_lane": "Use GLM-OCR/Qwen VLM sidecars only before text reasoning; the report workspace is text-only.",
            "text_reasoning_lane": "Qwen3.6-35B-A3B-MTP",
            "mmproj_allowed_on_text_lane": False,
            "preferred_llama_cpp_nparallel": 2,
        },
    }


def _bottleneck_report_payload(session: Session, *, model: BottleneckModel, subject: ReportSubject) -> tuple[dict[str, Any], dict[str, Any]]:
    from thoth_terminal.bottleneck_service import serialize_bottleneck_model

    model_payload = serialize_bottleneck_model(model, include_assessments=True)
    assessments = (
        session.query(BottleneckAssessment)
        .filter(BottleneckAssessment.model_id == int(model.id), BottleneckAssessment.status == "active")
        .order_by(BottleneckAssessment.updated_at.desc(), BottleneckAssessment.id.desc())
        .limit(10)
        .all()
    )
    assessment_rows = [_serialize_bottleneck_assessment(row) for row in assessments]
    latest_assessment = assessment_rows[0] if assessment_rows else None
    links = [
        {
            "id": int(row.id),
            "object_type": row.object_type,
            "object_id": int(row.object_id),
            "relation": row.relation,
            "payload": dict(row.payload or {}),
        }
        for row in session.query(BottleneckModelLink)
        .filter(BottleneckModelLink.model_id == int(model.id))
        .order_by(BottleneckModelLink.relation.asc(), BottleneckModelLink.id.asc())
        .all()
    ]
    open_gaps = _bottleneck_model_open_gaps(session, int(model.id))
    evidence = list(model.evidence_json or [])
    if latest_assessment:
        evidence.extend(latest_assessment.get("evidence") or [])
    linked_evidence = _bottleneck_linked_evidence_pack(session, links)
    candidate_evidence = _bottleneck_candidate_sell_side_evidence_pack(session, model=model, subject=subject, links=links)
    hydrated_evidence_rows = [row for row in linked_evidence.get("rows") or [] if isinstance(row, dict)]
    evidence.extend(hydrated_evidence_rows)
    dataset_families = [
        "market_topline_visibility",
        "architecture_component_demand",
        "qualified_input_supply",
        "intermediate_capacity",
        "conversion_assumptions",
        "pricing_shadow_price",
        "source_attribution_chain",
        "open_gaps",
    ]
    readiness_state = "blocked" if open_gaps else "ready_to_start"
    coverage = {
        "metrics": [
            {"label": "Open bottleneck gaps", "value": len(open_gaps)},
            {"label": "Evidence rows", "value": len(evidence)},
            {"label": "Assessments", "value": len(assessment_rows)},
            {"label": "Linked objects", "value": len(links)},
            {"label": "Hydrated linked evidence", "value": len(hydrated_evidence_rows)},
            {"label": "Candidate sell-side evidence", "value": int(candidate_evidence.get("count") or 0)},
        ],
        "gaps": [f"{row['gap_kind']}: {row['title']}" for row in open_gaps],
        "open_gaps": open_gaps,
        "readiness": {
            "state": readiness_state,
            "same_pass_max_label": "analyst_grade_candidate",
            "reason": "Open structural gaps remain." if open_gaps else "Bottleneck model has enough durable structure to start a report run.",
        },
    }
    payload = {
        "report_type": "bottleneck_report",
        "report_type_label": report_type_label("bottleneck_report"),
        "summary": _clean_text(model.summary or model.thesis or model.title),
        "description": _clean_text(model.summary or model.thesis or model.title),
        "tags": ["bottleneck", "research-canon"],
        "topics": _split_metadata_values(["bottleneck", model.constrained_input, model.output_unit, model.regime]),
        "linked_objects": {"bottleneck_models": [str(model.model_key or model.id)]},
        "subject": {
            "display_name": subject.display_name,
            "kind": subject.kind,
            "slug": subject.slug,
            "subject_key": subject.subject_key,
            "subject_id": subject.subject_id,
            "description": subject.description,
            "aliases": list(subject.aliases),
        },
        "identity_freshness": {
            "display_name": subject.display_name,
            "kind": subject.kind,
            "description": subject.description,
            "slug": subject.slug,
            "subject_key": subject.subject_key,
        },
        "coverage_panel": coverage,
        "bottleneck_model": model_payload,
        "latest_assessment": latest_assessment,
        "bottleneck_assessments": assessment_rows,
        "bottleneck_links": links,
        "linked_evidence_pack": linked_evidence,
        "candidate_sell_side_evidence_pack": candidate_evidence,
        "evidence_pack": {
            "rows": evidence,
            "count": len(evidence),
            "provenance_rule": "Preserve observed source and underlying source separately; do not promote screenshot-only claims without access status.",
        },
        "scope_card": {
            "thesis": model.thesis or model.summary,
            "constrained_input": model.constrained_input,
            "output_unit": model.output_unit,
            "visible_future_horizon": (model.payload or {}).get("visible_future_horizon"),
            "chain_map": (model.payload or {}).get("chain_map"),
            "candidate_shadow_constraints": (model.payload or {}).get("shadow_constraints") or (latest_assessment or {}).get("scorecard", {}).get("ranked_candidates", [])[1:],
            "durable_anchors": {
                "bottleneck_model_id": int(model.id),
                "model_key": model.model_key,
                "subject_id": int(model.subject_id) if model.subject_id is not None else None,
                "supply_chain_id": int(model.supply_chain_id) if model.supply_chain_id is not None else None,
                "program_id": int(model.program_id) if model.program_id is not None else None,
            },
        },
        "required_dataset_families": dataset_families,
        "workflow_contract": {
            "role_order": ["gap_hunter_quick", "evidence_harvester", "model_builder", "gap_hunter_full", "challenger", "report_writer"],
            "closed_allocation_model": "topline demand -> architecture units -> component attach -> qualified manufacturing capacity -> upstream input supply -> accepted downstream output",
            "mutation_rule": "Use this report as a workspace input; durable mutations still go through Thoth MCP/API rails.",
        },
        "computed_at": utcnow().isoformat(),
    }
    payload["agent_startup_packet"] = _bottleneck_agent_startup_packet(payload)
    fingerprint_payload = {
        "model": model_payload,
        "assessments": assessment_rows,
        "links": links,
        "open_gaps": open_gaps,
        "evidence": evidence,
        "linked_evidence": linked_evidence,
        "candidate_evidence": candidate_evidence,
        "startup_packet": payload["agent_startup_packet"],
    }
    return payload, {"coverage": coverage, "change_fingerprint": _stable_hash(fingerprint_payload)}


def _render_bottleneck_markdown(payload: dict[str, Any]) -> str:
    subject = payload.get("subject") or {}
    model = payload.get("bottleneck_model") or {}
    coverage = payload.get("coverage_panel") or {}
    scope = payload.get("scope_card") or {}
    latest = payload.get("latest_assessment") or {}
    evidence = payload.get("evidence_pack") or {}
    candidate_evidence = payload.get("candidate_sell_side_evidence_pack") or {}
    startup = payload.get("agent_startup_packet") or {}
    lines = [f"# Bottleneck Report - {subject.get('display_name') or model.get('title') or 'Bottleneck'}", ""]
    lines.extend(
        [
            "## Scope Card",
            "",
            f"- Constrained input: `{scope.get('constrained_input') or model.get('constrained_input') or 'unknown'}`",
            f"- Output unit: `{scope.get('output_unit') or model.get('output_unit') or 'unknown'}`",
            f"- Thesis: {scope.get('thesis') or model.get('thesis') or model.get('summary') or 'Not stated.'}",
            "",
            "## Readiness",
            "",
            f"- State: `{(coverage.get('readiness') or {}).get('state') or 'unknown'}`",
            f"- Same-pass ceiling: `{(coverage.get('readiness') or {}).get('same_pass_max_label') or 'analyst_grade_candidate'}`",
        ]
    )
    for gap in coverage.get("open_gaps") or []:
        lines.append(f"- Gap: `{gap.get('gap_kind')}` - {gap.get('title')}")
    lines.extend(["", "## Latest Assessment", ""])
    if latest:
        lines.append(f"- Regime: `{latest.get('regime') or latest.get('classification') or 'unknown'}`")
        lines.append(f"- Summary: {latest.get('summary') or 'Not stated.'}")
        axes = latest.get("axes") or {}
        if axes:
            lines.append("- Axes:")
            for key, value in axes.items():
                lines.append(f"  - {key}: `{value}`")
    else:
        lines.append("- No active assessment has been recorded.")
    lines.extend(["", "## Evidence Pack", ""])
    rows = evidence.get("rows") or []
    if rows:
        for row in rows[:20]:
            if isinstance(row, dict):
                label = row.get("title") or row.get("source") or row.get("label") or row.get("url") or "Evidence"
                detail = (
                    row.get("summary")
                    or row.get("provenance_summary")
                    or row.get("claim")
                    or row.get("value_text")
                    or row.get("text")
                    or row.get("text_excerpt")
                    or row.get("description")
                    or row.get("url")
                    or ""
                )
                lines.append(f"- {label}: {detail}")
            else:
                lines.append(f"- {row}")
    else:
        lines.append("- No evidence rows attached yet.")
    lines.extend(["", "## Candidate Sell-Side Evidence", ""])
    candidate_rows = candidate_evidence.get("rows") or []
    if candidate_rows:
        for row in candidate_rows[:12]:
            if not isinstance(row, dict):
                continue
            label = row.get("title") or row.get("source_name") or row.get("metric_key") or row.get("slug") or "Candidate evidence"
            detail = (
                row.get("summary")
                or row.get("provenance_summary")
                or row.get("value_text")
                or row.get("description")
                or row.get("source_url")
                or row.get("url")
                or ""
            )
            lines.append(f"- {label}: {detail}")
    else:
        lines.append("- No unlinked candidate evidence was discovered.")
    lines.extend(["", "## Startup Packet", ""])
    search_queries = startup.get("search_queries") or []
    if search_queries:
        lines.append("- Search queries:")
        for query in search_queries[:6]:
            lines.append(f"  - `{query}`")
    role_prompts = startup.get("role_prompts") or []
    if role_prompts:
        lines.append("- Role prompts:")
        for prompt in role_prompts:
            if isinstance(prompt, dict):
                lines.append(f"  - `{prompt.get('role')}`: {prompt.get('prompt')}")
    lines.extend(["", "## Required Workflow", ""])
    for role in (payload.get("workflow_contract") or {}).get("role_order") or []:
        lines.append(f"- {role}")
    lines.append("")
    return "\n".join(lines).strip() + "\n"


def build_bottleneck_report(
    session: Session,
    *,
    bottleneck_model_id: int,
    trigger_kind: str = "manual",
    trigger_label: str | None = None,
    force: bool = False,
) -> ResearchReportBuildResult:
    model = session.query(BottleneckModel).filter_by(id=int(bottleneck_model_id)).first()
    if model is None:
        raise ValueError("Bottleneck model not found.")
    subject = _ensure_bottleneck_report_subject(session, model)
    report = _ensure_bottleneck_report_container(session, model, subject)
    latest = _latest_published_version(session, int(report.id))
    payload, meta = _bottleneck_report_payload(session, model=model, subject=subject)
    fingerprint = str(meta.get("change_fingerprint") or "")
    now = utcnow()
    if latest is not None and not force:
        if latest.change_fingerprint == fingerprint:
            return ResearchReportBuildResult(report=report, version=latest, created_version=False, reason="unchanged", subject=subject)
        if latest.published_at is not None and (now - latest.published_at) < REPORT_STALE_AFTER:
            return ResearchReportBuildResult(report=report, version=latest, created_version=False, reason="fresh", subject=subject)

    max_version = session.query(func.max(ResearchReportVersion.version)).filter(ResearchReportVersion.report_id == int(report.id)).scalar()
    next_version = int(max_version or 0) + 1
    markdown_text = _render_bottleneck_markdown(payload)
    version = ResearchReportVersion(
        report_id=int(report.id),
        version=next_version,
        status="published",
        trigger_kind=str(trigger_kind or "manual"),
        trigger_label=_clean_text(trigger_label) or None,
        trigger_payload={"bottleneck_model_id": int(model.id), "model_key": model.model_key},
        change_fingerprint=fingerprint,
        content_json=payload,
        content_md=markdown_text,
        coverage_json=dict(meta.get("coverage") or {}),
        as_of_at=now,
        error=None,
        created_at=now,
        updated_at=now,
        published_at=now,
    )
    session.add(version)
    session.flush()
    _upsert_artifact(
        session,
        version=version,
        artifact_type="json",
        variant="default",
        label=f"{model.title} bottleneck report JSON",
        data_bytes=json.dumps(payload, indent=2, ensure_ascii=False, default=_json_default).encode("utf-8"),
        content_type="application/json",
        payload={"bottleneck_model_id": int(model.id), "model_key": model.model_key},
    )
    _upsert_artifact(
        session,
        version=version,
        artifact_type="markdown",
        variant="default",
        label=f"{model.title} bottleneck report Markdown",
        data_bytes=markdown_text.encode("utf-8"),
        content_type="text/markdown",
        payload={"bottleneck_model_id": int(model.id), "model_key": model.model_key},
    )
    payload_with_artifacts = dict(payload)
    payload_with_artifacts["artifacts_export_appendix"] = {"rows": _artifact_entries(version)}
    export_path = _persist_export_bundle(version, payload=payload_with_artifacts, markdown_text=markdown_text)
    payload_with_artifacts["artifacts_export_appendix"] = {"rows": _artifact_entries(version), "export_bundle": export_path}
    startup_packet = dict(payload_with_artifacts.get("agent_startup_packet") or {})
    startup_calls: list[dict[str, Any]] = []
    for call in startup_packet.get("next_mcp_calls") or []:
        if not isinstance(call, dict):
            continue
        cloned = dict(call)
        args = dict(cloned.get("arguments") or {}) if isinstance(cloned.get("arguments"), dict) else {}
        if args.get("report_id") == "<report_id_after_workspace_build>":
            args["report_id"] = int(report.id)
        cloned["arguments"] = args
        startup_calls.append(cloned)
    if startup_calls:
        startup_packet["next_mcp_calls"] = startup_calls
    startup_packet["report"] = {
        "id": int(report.id),
        "title": report.title,
        "slug": report.slug,
        "url": f"/reports/{report.slug}",
        "report_type": "bottleneck_report",
    }
    startup_packet["version"] = {
        "id": int(version.id),
        "report_id": int(report.id),
        "version": int(version.version or 0),
        "status": version.status,
        "artifacts": _artifact_entries(version),
    }
    payload_with_artifacts["agent_startup_packet"] = startup_packet
    version.content_json = payload_with_artifacts
    report.latest_published_version_id = int(version.id)
    report.updated_at = utcnow()
    session.flush()
    return ResearchReportBuildResult(report=report, version=version, created_version=True, reason="published", subject=subject)


def create_ai_clean_derivative(session: Session, *, subject: str | int | ResearchReport | None = None, report_version_id: int | None = None, instructions: str | None = None) -> dict[str, Any]:
    if report_version_id is not None:
        version = session.query(ResearchReportVersion).filter_by(id=int(report_version_id)).first()
        if version is None:
            raise ValueError("Research report version not found.")
    else:
        if subject is None:
            raise ValueError("subject or report_version_id is required")
        resolved_subject = resolve_report_subject(session, subject)
        report = _ensure_report_container(session, resolved_subject)
        version = _latest_published_version(session, int(report.id))
        if version is None:
            raise ValueError("No published research report exists yet.")
    if not llm_available(provider=getattr(settings, "ai_provider", "openai")):
        raise ValueError("AI provider is not configured.")

    payload = dict(version.content_json or {})
    prompt = (
        "Return JSON only with keys: title (string), executive_summary (array of strings), markdown (string). "
        "Rewrite the canonical auto-generated research report into cleaner analyst prose while preserving all source-dense facts and caveats. "
        "Do not invent new facts, numbers, dates, or sources. Keep markdown under 7000 chars.\n\n"
        + (f"Additional instructions: {instructions}\n\n" if instructions and str(instructions).strip() else "")
        + json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default)
    )
    parsed = manifested_chat_json(
        session,
        run_kind="research_report_version",
        run_id=int(version.id),
        task_kind="ai_clean_derivative",
        messages=[
            ChatMessage(role="system", content="You improve auto-generated equity research reports without fabricating facts."),
            ChatMessage(role="user", content=prompt),
        ],
        provider=getattr(settings, "ai_provider", "openai"),
        timeout_s=90.0,
        max_input_chars=22000,
        prompt_cache_key="thoth-ai-clean-report-v1",
        permission_profile="artifact-draft-only",
    )
    markdown_text = str(parsed.get("markdown") or "").strip()
    if not markdown_text:
        raise ValueError("AI clean derivative returned empty markdown.")
    artifact = _upsert_artifact(
        session,
        version=version,
        artifact_type="ai_clean_markdown",
        variant="default",
        label=str(parsed.get("title") or f"{version.report.title} (AI Clean)"),
        data_bytes=markdown_text.encode("utf-8"),
        content_type="text/markdown",
        payload={"model": _llm_provider_model(), "executive_summary": list(parsed.get("executive_summary") or []), "instructions": str(instructions or "").strip() or None},
    )
    session.flush()
    return {
        "artifact_id": int(artifact.id),
        "artifact_type": artifact.artifact_type,
        "label": artifact.label,
        "object_path": artifact.object_path,
        "object_url": f"/objects/{artifact.object_path}",
        "payload": dict(artifact.payload or {}),
        "report_version_id": int(version.id),
    }


def run_report_freshness_scan(session: Session) -> list[dict[str, Any]]:
    now = utcnow()
    out: list[dict[str, Any]] = []
    for report in session.query(ResearchReport).order_by(ResearchReport.updated_at.asc(), ResearchReport.id.asc()).all():
        latest = _latest_published_version(session, int(report.id))
        if latest is None:
            out.append({"report_id": int(report.id), "slug": report.slug, "reason": "missing"})
            continue
        if latest.published_at is None or (now - latest.published_at) >= REPORT_STALE_AFTER:
            out.append({"report_id": int(report.id), "slug": report.slug, "reason": "stale"})
    return out
