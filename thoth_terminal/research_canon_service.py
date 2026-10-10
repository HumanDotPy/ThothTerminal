from __future__ import annotations

import hashlib
import json
import re
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import or_, text
from sqlalchemy.orm import Session

from thoth_terminal.db import utcnow
from thoth_terminal.earnings_event_policy import earnings_event_is_subsidiary_only_result
from thoth_terminal.instrument_service import create_instrument_request
from thoth_terminal.models import (
    AgentCanonCard,
    BottleneckModel,
    BottleneckModelLink,
    Company,
    CompanyDocument,
    CoverageGap,
    Document,
    EarningsEvent,
    ExchangeDisclosure,
    IRArtifact,
    Issuer,
    IssuerIdentifier,
    IngestionRun,
    InstrumentRequest,
    IPOEvent,
    ResearchCampaign,
    ResearchCampaignItem,
    ResearchInstrument,
    ResearchReport,
    ResearchReportQuality,
    ResearchReportVersion,
    ResearchSourcePack,
    ResearchSubject,
    ResearchSubjectScenario,
    ResearchUniverse,
    ResearchUniverseRow,
    SecuritiesFiling,
    Source,
    SourceState,
    StructuredDataFact,
    TranscriptSummary,
    TranscriptWatchState,
)


SOURCE_PACK_REQUIRED_ITEMS = (
    "ir_website",
    "filings_source",
    "latest_annual_report",
    "latest_quarterly_or_earnings_release",
    "earnings_transcript_or_call_material",
    "investor_presentation",
    "product_pages",
    "official_press_news_page",
    "regulatory_or_exchange_disclosures",
)
SOURCE_PACK_API_FIRST_ITEMS = {
    "filings_source",
    "latest_annual_report",
    "latest_quarterly_or_earnings_release",
    "regulatory_or_exchange_disclosures",
}
SOURCE_PACK_TRANSCRIPT_GATED_ITEMS = {"earnings_transcript_or_call_material"}
TRANSCRIPT_GATE_EXHAUSTED_MISS_THRESHOLD = 8
ANNUAL_FIRST_REPORT_NOT_YET_DUE_DAYS = 210
SEC_ANNUAL_EQUIVALENT_FORMS = {"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"}
SEC_TERMINATION_FORMS = {"15-12B", "15-12G", "15-15D", "25-NSE"}
ANNUAL_FILING_REQUIREMENT_BY_RAIL = {
    "sec": "SEC annual report equivalent: Form 10-K, 20-F, or 40-F depending on issuer status",
    "canada": "SEDAR+ annual filing or annual financial report equivalent",
    "asx": "ASX annual report or annual financial report equivalent",
    "bursa": "Bursa annual report or annual financial statement equivalent",
    "kazakhstan": "Kazakhstan exchange annual report or annual financial statement equivalent",
    "sgx": "SGX annual report or annual financial statement equivalent",
    "nse": "NSE/BSE annual report or annual financial statement equivalent",
    "opendart": "OpenDART annual business report or annual financial statement equivalent",
    "japan": "EDINET annual securities report or full-year financial results equivalent",
    "taiwan": "Taiwan MOPS annual report or annual financial statement equivalent",
    "hkex": "HKEX annual report or annual results equivalent",
    "china": "CNINFO/CSRC annual report equivalent",
    "esef": "ESEF annual financial report equivalent",
}
ACTIVE_UNIVERSE_OFFICIAL_RAIL_SOURCES = {
    "sec": ("sec_edgar_active_universe_filings", "sec_companyfacts_active_universe"),
    "canada": ("sedarplus_active_universe_filings",),
    "asx": ("asx_active_universe_announcements",),
    "bursa": ("bursa_active_universe_announcements",),
    "kazakhstan": ("kazakhstan_active_universe_announcements",),
    "sgx": ("sgx_active_universe_announcements",),
    "nse": ("nse_active_universe_announcements",),
    "opendart": ("opendart_official_filings", "opendart_korea_financials", "krx_active_universe_disclosures"),
    "japan": ("edinet_official_filings", "edinetdb_japan_company_financials", "tdnet_semiconductor_announcements"),
    "taiwan": (
        "taiwan_mops_monthly_revenue",
        "taiwan_mops_financial_reports",
        "taiwan_mops_investor_conferences",
        "twse_material_announcements",
        "tpex_infohub_company_pages",
    ),
    "hkex": ("hkexnews_active_universe_disclosures",),
    "china": ("cninfo_active_universe_announcements",),
    "esef": ("filings_xbrl_org_active_universe_esef",),
}
_SOURCE_BACKFILL_RETRY_ATTEMPTS = 4
_SOURCE_BACKFILL_FETCH_MIN_RUNTIME_SECONDS = 30.0
_SOURCE_BACKFILL_FETCH_TIMEOUT_BUFFER_SECONDS = 5.0
_SOURCE_BACKFILL_PREFETCH_MIN_RUNTIME_SECONDS = _SOURCE_BACKFILL_FETCH_MIN_RUNTIME_SECONDS
_SOURCE_BACKFILL_PREFETCH_TIMEOUT_BUFFER_SECONDS = _SOURCE_BACKFILL_FETCH_TIMEOUT_BUFFER_SECONDS
SCENARIO_STATUSES = {"draft", "active", "superseded", "archived"}
SCENARIO_KINDS = {"bear", "base", "bull", "custom", "canon"}
QUALITY_STATES = {
    "missing",
    "skeleton",
    "minimum_viable",
    "analyst_grade",
    "canon_ready",
    "stale",
    "failed",
    "error",
}
ACTIVE_GAP_STATUSES = {"open", "draft", "review"}
CAMPAIGN_ITEM_STATUSES = {"queued", "blocked", "running", "workspace_ready", "completed", "failed", "skipped"}
ACTIVE_UNIVERSE_GAP_KINDS = {
    "unresolved_universe_identity",
    "adr_local_duplicate_candidate",
    "same_company_separate_issuers",
    "official_identifier_conflict",
    "private_company_public_listing_review",
    "missing_research_report",
    "skeleton_or_short_report",
    "stale_research_report",
    "missing_official_source_pack",
    "missing_canon_scenario",
    "missing_agent_canon_card",
}
REPORT_RAIL_NONBLOCKING_GAP_KINDS = {
    "missing_research_report",
    "skeleton_or_short_report",
    "stale_research_report",
    "missing_agent_canon_card",
    "missing_investment_memo",
    "missing_analyst_evidence",
}
BOTTLENECK_COVERAGE_GAP_KINDS = (
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
_BOTTLENECK_SCOPE_INPUT_KEYS = {
    "constrained_input",
    "constrained_upstream_input",
    "suspected_constrained_input",
}
_BOTTLENECK_SCOPE_OUTPUT_KEYS = {
    "output_unit",
    "downstream_output_unit",
    "capacity_metric",
    "accepted_output_unit",
}
_BOTTLENECK_QUALIFIED_SUPPLY_KEYS = {
    "qualified_input_supply",
    "qualified_supplier_set",
    "qualified_suppliers",
    "supplier_qualification",
    "suppliers",
}
_BOTTLENECK_ALLOCATION_MODEL_KEYS = {
    "closed_allocation_model",
    "allocation_model",
    "chain_map",
    "conversion_graph",
    "demand_supply_bridge",
    "model_tables",
    "scenario_table",
    "sensitivity_table",
    "shortfall_by_demand_case",
    "supported_output_by_supply_case",
    "topline_to_input_bridge",
}
_BOTTLENECK_DATASET_FAMILY_KEYS = {
    "market_topline_visibility",
    "architecture_component_demand",
    "qualified_input_supply",
    "intermediate_capacity",
    "conversion_assumptions",
    "pricing_shadow_price",
}
_BOTTLENECK_METRIC_KEYS = {
    "asp",
    "backlog",
    "capex",
    "capacity",
    "contract_price",
    "demand",
    "die",
    "lead_time",
    "lta",
    "margin",
    "metric",
    "metric_name",
    "price",
    "quantity",
    "scrap",
    "shipment",
    "shipments",
    "spot_price",
    "tonnes",
    "unit",
    "units",
    "utilization",
    "value",
    "value_number",
    "wafer",
    "wafers",
    "yield",
}
_BOTTLENECK_ATTRIBUTION_KEYS = {
    "access_status",
    "citation",
    "citations",
    "document_id",
    "observed_source",
    "provenance",
    "source_attribution_chain",
    "source_document_id",
    "source_id",
    "underlying_source",
}


def _slugify(value: object, *, default: str = "item") -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return cleaned or default


def _stable_hash(value: object, *, length: int = 16) -> str:
    return hashlib.sha1(json.dumps(value, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:length]


def _unique_slug(session: Session, model: type[Any], value: object, *, default: str = "item") -> str:
    base = _slugify(value, default=default)[:120].strip("-") or default
    slug = base
    idx = 2
    while session.query(model).filter_by(slug=slug).first() is not None:
        suffix = f"-{idx}"
        slug = f"{base[: max(1, 120 - len(suffix))]}{suffix}"
        idx += 1
    return slug


def _short(value: object, limit: int = 255) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    return text[:limit]


def _norm_name(value: object) -> str:
    text = str(value or "").lower()
    text = re.sub(
        r"\b(inc|corp|corporation|co|company|ltd|limited|plc|holdings|holding|sa|ag|nv|ab|spa|kk|kabushiki kaisha|gmbh|se|adr|ads)\b",
        " ",
        text,
    )
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _base_symbol(value: object) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    return text.split(".", 1)[0]


def _json_list(value: object) -> list[Any]:
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    if value in (None, ""):
        return []
    return [value]


def _json_dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _norm_json_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def _has_nonempty_json_value(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, bool):
        return bool(value)
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, dict):
        return any(_has_nonempty_json_value(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_has_nonempty_json_value(item) for item in value)
    return True


def _json_tree_has_key(value: object, keys: set[str]) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if _norm_json_key(key) in keys and _has_nonempty_json_value(item):
                return True
            if _json_tree_has_key(item, keys):
                return True
    elif isinstance(value, (list, tuple, set)):
        return any(_json_tree_has_key(item, keys) for item in value)
    return False


def _json_tree_has_metric_binding(value: object) -> bool:
    if isinstance(value, dict):
        normalized_keys = {_norm_json_key(key) for key in value}
        has_metric_key = bool(normalized_keys & _BOTTLENECK_METRIC_KEYS)
        if has_metric_key and any(_has_nonempty_json_value(item) for item in value.values()):
            return True
        return any(_json_tree_has_metric_binding(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_json_tree_has_metric_binding(item) for item in value)
    return False


def _nested_counter_payload(counters: dict[str, Counter[str]]) -> dict[str, dict[str, int]]:
    return {str(key): {str(inner_key): int(count) for inner_key, count in counter.items()} for key, counter in counters.items()}


def _dt(value: Any) -> str | None:
    return value.isoformat() if value else None


def _active_universe_rows(session: Session, *, universe_slug: str | None = None) -> list[ResearchUniverseRow]:
    q = (
        session.query(ResearchUniverseRow)
        .join(ResearchUniverse, ResearchUniverse.id == ResearchUniverseRow.universe_id)
        .filter(ResearchUniverse.status == "active", ResearchUniverseRow.archived_at.is_(None))
    )
    if universe_slug:
        q = q.filter(ResearchUniverse.slug == str(universe_slug).strip())
    return q.order_by(ResearchUniverse.sort_order.asc(), ResearchUniverseRow.sort_order.asc(), ResearchUniverseRow.id.asc()).all()


def _subject_for_instrument(session: Session, instrument: ResearchInstrument, *, ensure: bool = False) -> ResearchSubject | None:
    if instrument.issuer_id is not None:
        subject = session.query(ResearchSubject).filter_by(subject_key=f"issuer:{int(instrument.issuer_id)}").first()
        if subject is not None:
            return subject
    if instrument.company_id is not None:
        subject = session.query(ResearchSubject).filter_by(subject_key=f"company:{int(instrument.company_id)}").first()
        if subject is not None:
            return subject
    private_key = f"private_instrument:{int(instrument.id)}"
    private_subject = session.query(ResearchSubject).filter_by(subject_key=private_key).first()
    if private_subject is not None:
        return private_subject
    if not ensure:
        return None
    issuer = session.get(Issuer, int(instrument.issuer_id)) if instrument.issuer_id is not None else None
    company = session.get(Company, int(instrument.company_id)) if instrument.company_id is not None else None
    if issuer is None and company is None:
        if not instrument.is_public:
            now = utcnow()
            row = ResearchSubject(
                slug=f"{_slugify(instrument.display_name, default='private-company')}-{int(instrument.id)}",
                subject_key=private_key,
                kind="private_company",
                display_name=instrument.display_name,
                legal_name=instrument.legal_name,
                description=None,
                status="active",
                issuer_id=None,
                company_id=None,
                primary_entity_id=None,
                extra={
                    "source": "research_canon_private_instrument",
                    "instrument_id": int(instrument.id),
                    "instrument_key": instrument.instrument_key,
                },
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            return row
        return None
    from thoth_terminal.decision_spine_service import ensure_decision_subject

    return ensure_decision_subject(session, company=company, issuer=issuer)


def ensure_subject_for_instrument(session: Session, *, instrument_id: int) -> ResearchSubject | None:
    instrument = session.get(ResearchInstrument, int(instrument_id))
    if instrument is None:
        return None
    return _subject_for_instrument(session, instrument, ensure=True)


def _issuer_payload(row: Issuer | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": int(row.id),
        "slug": row.slug,
        "display_name": row.display_name,
        "legal_name": row.legal_name,
        "jurisdiction": row.jurisdiction,
        "website": row.website,
        "ir_website": row.ir_website,
    }


def _company_payload(row: Company | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": int(row.id),
        "slug": row.slug,
        "name": row.name,
        "ticker": row.ticker,
        "exchange": row.exchange,
        "region": row.region,
        "issuer_id": int(row.issuer_id) if row.issuer_id is not None else None,
        "ir_url": row.ir_url,
    }


def _subject_payload(row: ResearchSubject | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": int(row.id),
        "subject_key": row.subject_key,
        "kind": row.kind,
        "display_name": row.display_name,
        "issuer_id": int(row.issuer_id) if row.issuer_id is not None else None,
        "company_id": int(row.company_id) if row.company_id is not None else None,
    }


def _identity_repair_gap(session: Session, *, gap_id: int | None = None, gap_key: str | None = None) -> CoverageGap | None:
    if gap_id is not None:
        return session.get(CoverageGap, int(gap_id))
    if gap_key:
        return session.query(CoverageGap).filter_by(gap_key=str(gap_key).strip()).first()
    return None


def _find_target_issuer(
    session: Session,
    *,
    issuer_id: int | None = None,
    issuer_name: str | None = None,
    ticker: str | None = None,
    exchange: str | None = None,
) -> Issuer | None:
    if issuer_id is not None:
        return session.get(Issuer, int(issuer_id))
    name = str(issuer_name or "").strip()
    if name:
        row = (
            session.query(Issuer)
            .filter(or_(Issuer.legal_name.ilike(name), Issuer.display_name.ilike(name), Issuer.slug == _slugify(name)))
            .first()
        )
        if row is not None:
            return row
    if ticker and not name:
        id_types = ["TICKER"]
        if exchange:
            id_types.append(f"TICKER_{str(exchange).strip().upper()}"[:16])
        q = session.query(IssuerIdentifier).filter(IssuerIdentifier.id_value == str(ticker).strip(), IssuerIdentifier.id_type.in_(id_types))
        if exchange:
            q = q.filter(or_(IssuerIdentifier.exchange == str(exchange).strip().upper(), IssuerIdentifier.id_type == id_types[-1]))
        ident = q.order_by(IssuerIdentifier.is_primary.desc(), IssuerIdentifier.id.asc()).first()
        if ident is not None:
            return session.get(Issuer, int(ident.issuer_id))
    return None


def _find_target_company(
    session: Session,
    *,
    company_id: int | None = None,
    company_name: str | None = None,
    ticker: str | None = None,
    exchange: str | None = None,
    issuer_id: int | None = None,
) -> Company | None:
    if company_id is not None:
        return session.get(Company, int(company_id))
    if issuer_id is not None:
        row = session.query(Company).filter_by(issuer_id=int(issuer_id)).first()
        if row is not None:
            return row
    filters = []
    if company_name:
        filters.append(Company.name.ilike(str(company_name).strip()))
    if ticker:
        filters.append(Company.ticker == str(ticker).strip())
    if not filters:
        return None
    q = session.query(Company).filter(or_(*filters))
    if exchange:
        q = q.filter(Company.exchange == str(exchange).strip().upper())
    return q.order_by(Company.id.asc()).first()


def _ensure_target_identity(
    session: Session,
    *,
    apply: bool,
    issuer_id: int | None = None,
    company_id: int | None = None,
    subject_id: int | None = None,
    issuer_name: str | None = None,
    company_name: str | None = None,
    ticker: str | None = None,
    exchange: str | None = None,
    jurisdiction: str | None = None,
    update_existing_company_listing: bool = True,
    identifier_ticker: str | None = None,
    identifier_exchange: str | None = None,
    website: str | None = None,
    ir_website: str | None = None,
) -> tuple[Issuer | None, Company | None, ResearchSubject | None, list[dict[str, Any]]]:
    planned: list[dict[str, Any]] = []
    subject = session.get(ResearchSubject, int(subject_id)) if subject_id is not None else None
    if subject is not None:
        issuer_id = issuer_id if issuer_id is not None else subject.issuer_id
        company_id = company_id if company_id is not None else subject.company_id
    issuer = _find_target_issuer(session, issuer_id=issuer_id, issuer_name=issuer_name, ticker=ticker, exchange=exchange)
    company = _find_target_company(
        session,
        company_id=company_id,
        company_name=company_name,
        ticker=ticker,
        exchange=exchange,
        issuer_id=int(issuer.id) if issuer is not None else None,
    )

    target_name = str(issuer_name or company_name or (company.name if company else "") or (issuer.display_name if issuer else "")).strip()
    if issuer is None:
        if not target_name:
            raise ValueError("Target issuer is missing. Provide an existing issuer/subject or issuer/company name.")
        planned.append({"action": "create_issuer", "legal_name": target_name, "jurisdiction": jurisdiction})
        if apply:
            now = utcnow()
            issuer = Issuer(
                slug=_unique_slug(session, Issuer, f"{target_name}-{exchange or jurisdiction or ticker or 'issuer'}", default="issuer"),
                lei=None,
                legal_name=target_name,
                display_name=target_name,
                jurisdiction=str(jurisdiction or "").strip().upper() or None,
                entity_status="ACTIVE",
                sector=None,
                industry=None,
                sic_code=None,
                website=str(website or "").strip() or None,
                ir_website=str(ir_website or "").strip() or None,
                extra={"source": "research_identity_repair", "created_by": "codex"},
                created_at=now,
                updated_at=now,
            )
            session.add(issuer)
            session.flush()
    else:
        planned.append({"action": "use_issuer", "issuer_id": int(issuer.id)})
        if apply:
            issuer.website = str(website or issuer.website or "").strip() or None
            issuer.ir_website = str(ir_website or issuer.ir_website or "").strip() or None
            if jurisdiction:
                issuer.jurisdiction = str(jurisdiction).strip().upper()
            issuer.updated_at = utcnow()

    if company is None:
        if not target_name:
            raise ValueError("Target company is missing. Provide an existing company/subject or company name.")
        planned.append({"action": "create_company", "name": target_name, "ticker": ticker, "exchange": exchange})
        if apply:
            company = Company(
                slug=_unique_slug(session, Company, f"{target_name}-{exchange or ticker or 'company'}", default="company"),
                name=target_name,
                ticker=str(ticker or "").strip() or None,
                exchange=str(exchange or "").strip().upper() or None,
                region=str(jurisdiction or "").strip().upper() or None,
                sector=None,
                industry=None,
                supply_chain_stage=None,
                ir_url=str(ir_website or "").strip() or None,
                filings_url=None,
                reports_url=None,
                calendar_url=None,
                created_at=utcnow(),
                issuer_id=int(issuer.id) if issuer is not None else None,
            )
            session.add(company)
            session.flush()
    else:
        planned.append({"action": "use_company", "company_id": int(company.id)})
        if apply:
            if issuer is not None:
                company.issuer_id = int(issuer.id)
            if company_name and str(company_name).strip():
                company.name = str(company_name).strip()
            company.ir_url = str(ir_website or company.ir_url or "").strip() or None
            if ticker and update_existing_company_listing:
                company.ticker = str(ticker).strip()
            if exchange and update_existing_company_listing:
                company.exchange = str(exchange).strip().upper()
            if jurisdiction and update_existing_company_listing:
                company.region = str(jurisdiction).strip().upper()

    id_ticker = str(identifier_ticker or ticker or "").strip()
    if id_ticker:
        exchange_code = str(identifier_exchange or exchange or "").strip().upper() or None
        id_type = "TICKER"
        existing_plain = session.query(IssuerIdentifier).filter_by(id_type="TICKER", id_value=id_ticker).first()
        if existing_plain is not None and (issuer is None or int(existing_plain.issuer_id) != int(issuer.id)):
            id_type = f"TICKER_{exchange_code}"[:16] if exchange_code else "TICKER_ALT"
        planned.append({"action": "ensure_issuer_identifier", "id_type": id_type, "value": id_ticker, "exchange": exchange_code})
        if apply:
            if issuer is None:
                raise ValueError("Cannot create ticker identifier without a target issuer.")
            ident = (
                session.query(IssuerIdentifier)
                .filter_by(id_type=id_type, id_value=id_ticker)
                .first()
            )
            if ident is None:
                ident = IssuerIdentifier(
                    issuer_id=int(issuer.id),
                    id_type=id_type,
                    id_value=id_ticker,
                    exchange=exchange_code,
                    currency=None,
                    is_primary=True,
                    valid_from=None,
                    valid_to=None,
                    created_at=utcnow(),
                )
                session.add(ident)
            elif int(ident.issuer_id) != int(issuer.id):
                raise ValueError(f"Ticker identifier {id_ticker}/{exchange_code or ''} already belongs to issuer {ident.issuer_id}.")

    if apply and (issuer is not None or company is not None):
        from thoth_terminal.decision_spine_service import ensure_decision_subject

        subject = ensure_decision_subject(session, company=company, issuer=issuer)
    elif subject is None:
        planned.append({"action": "ensure_research_subject", "subject_key": f"issuer:<created>" if issuer is None else f"issuer:{int(issuer.id)}"})
    session.flush()
    return issuer, company, subject, planned


def _identity_repair_text_blob(*values: Any) -> str:
    parts: list[str] = []
    for value in values:
        if value in (None, ""):
            continue
        if isinstance(value, (dict, list, tuple)):
            parts.append(json.dumps(value, sort_keys=True, default=str))
        else:
            parts.append(str(value))
    return " ".join(parts).lower()


def _identity_repair_cik_variants(value: object) -> set[str]:
    normalized = _normalize_audit_sec_cik(value)
    if not normalized:
        return set()
    stripped = normalized.lstrip("0") or "0"
    return {normalized, stripped}


def _identity_repair_identifier_hints(evidence: list[dict[str, Any]]) -> dict[str, set[str]]:
    hints: dict[str, set[str]] = defaultdict(set)
    for row in evidence:
        raw_type = str(row.get("id_type") or row.get("identifier_type") or "").strip().upper()
        raw_value = str(row.get("id_value") or row.get("identifier_value") or "").strip()
        if raw_type and raw_value:
            if raw_type == "CIK":
                hints["CIK"].update(_identity_repair_cik_variants(raw_value))
            elif raw_type == "LEI":
                lei = _normalize_audit_lei(raw_value)
                if lei:
                    hints["LEI"].add(lei)
            else:
                hints[raw_type].add(raw_value.upper())
        text_value = " ".join(str(row.get(key) or "") for key in ("url", "label", "note", "title"))
        text_upper = text_value.upper()
        if "SEC.GOV" in text_upper or "CIK" in text_upper:
            for match in re.finditer(r"CIK0*([0-9]{1,10})", text_upper):
                hints["CIK"].update(_identity_repair_cik_variants(match.group(1)))
            for match in re.finditer(r"/(?:DATA|CIK)(?:/)?0*([0-9]{1,10})(?:[/.#?]|$)", text_upper):
                hints["CIK"].update(_identity_repair_cik_variants(match.group(1)))
        if "GLEIF" in text_upper or "LEI" in text_upper or "FILINGS.XBRL.ORG" in text_upper:
            for match in re.finditer(r"(?<![A-Z0-9])([A-Z0-9]{20})(?![A-Z0-9])", text_upper):
                lei = _normalize_audit_lei(match.group(1))
                if lei:
                    hints["LEI"].add(lei)
    return hints


def _identity_repair_identifier_values(row: IssuerIdentifier) -> set[str]:
    id_type = str(row.id_type or "").strip().upper()
    value = str(row.id_value or "").strip()
    if not value:
        return set()
    if id_type == "CIK":
        return _identity_repair_cik_variants(value)
    if id_type == "LEI":
        lei = _normalize_audit_lei(value)
        return {lei} if lei else set()
    return {value.upper(), value.lower()}


def _identity_repair_ticker_identifier_matches(
    row: IssuerIdentifier,
    *,
    ticker: str | None,
    exchange: str | None,
) -> bool:
    ticker_value = str(ticker or "").strip()
    if not ticker_value:
        return False
    row_type = str(row.id_type or "").strip().upper()
    if row_type != "TICKER" and not row_type.startswith("TICKER_") and row_type not in {"TW_STOCK", "HK_STOCK"}:
        return False
    if str(row.id_value or "").strip().upper() != ticker_value.upper():
        return False
    expected_exchange = str(exchange or "").strip().upper()
    row_exchange = str(row.exchange or "").strip().upper()
    if expected_exchange and row_exchange and expected_exchange != row_exchange:
        return False
    if expected_exchange and row_type.startswith("TICKER_") and row_type != f"TICKER_{expected_exchange}"[:16]:
        return False
    return True


def _identity_repair_record_identity_match(blob: str, *, identifier_values: set[str]) -> bool:
    blob_upper = blob.upper()
    for value in identifier_values:
        raw = str(value or "").strip()
        if not raw:
            continue
        if raw.upper() in blob_upper:
            return True
    return False


def _identity_repair_record_name_match(blob: str, *, names: set[str], tickers: set[str]) -> bool:
    blob_lower = blob.lower()
    strong_name = any(name and len(name) >= 5 and name.lower() in blob_lower for name in names)
    if not strong_name:
        return False
    if not tickers:
        return True
    blob_upper = blob.upper()
    return any(ticker and ticker.upper() in blob_upper for ticker in tickers)


def _identity_repair_move_company_documents(
    session: Session,
    *,
    document_ids: set[int],
    target_company_id: int | None,
    old_company_ids: set[int],
    audit_note: dict[str, Any],
) -> dict[str, Any]:
    if target_company_id is None or not document_ids:
        return {"moved": [], "merged": [], "skipped": []}
    moved: list[dict[str, Any]] = []
    merged: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for document_id in sorted(document_ids):
        rows = (
            session.query(CompanyDocument)
            .filter(CompanyDocument.document_id == int(document_id))
            .order_by(CompanyDocument.id.asc())
            .all()
        )
        for row in rows:
            if int(row.company_id) == int(target_company_id):
                continue
            if old_company_ids and int(row.company_id) not in old_company_ids:
                skipped.append({"company_document_id": int(row.id), "document_id": int(document_id), "company_id": int(row.company_id)})
                continue
            existing = (
                session.query(CompanyDocument)
                .filter_by(company_id=int(target_company_id), document_id=int(document_id), doc_type=row.doc_type)
                .first()
            )
            before = {"company_document_id": int(row.id), "document_id": int(document_id), "old_company_id": int(row.company_id), "doc_type": row.doc_type}
            if existing is not None:
                merged.append({**before, "target_company_document_id": int(existing.id)})
                session.delete(row)
                continue
            row.company_id = int(target_company_id)
            moved.append({**before, "new_company_id": int(target_company_id)})
    return {"moved": moved, "merged": merged, "skipped": skipped}


def _annotate_identity_repaired_meta(
    current: dict[str, Any] | None,
    *,
    audit_note: dict[str, Any],
) -> dict[str, Any]:
    payload = _json_dict(current)
    history = list(payload.get("identity_repair_history") or [])
    history.append(dict(audit_note))
    payload["identity_repair_history"] = history[-10:]
    payload["last_identity_repair"] = dict(audit_note)
    return payload


def _migrate_identity_repair_attribution(
    session: Session,
    *,
    apply: bool,
    old_issuer_id: int | None,
    old_company_id: int | None,
    target_issuer: Issuer | None,
    target_company: Company | None,
    instrument: ResearchInstrument,
    evidence: list[dict[str, Any]],
    note: str | None,
) -> dict[str, Any]:
    """Move evidence-matched stale identifiers and official rows during identity repair."""
    if old_issuer_id is None or target_issuer is None or int(old_issuer_id) == int(target_issuer.id):
        return {
            "identifier_hints": {},
            "moved_identifiers": [],
            "moved_filings": [],
            "moved_disclosures": [],
            "moved_artifacts": [],
            "moved_earnings_events": [],
            "moved_transcript_watch_states": [],
            "moved_transcript_summaries": [],
            "company_documents": {"moved": [], "merged": [], "skipped": []},
            "conflicts": [],
        }

    now = utcnow()
    target_issuer_id = int(target_issuer.id)
    target_company_id = int(target_company.id) if target_company is not None else None
    hints = _identity_repair_identifier_hints(evidence)
    ticker = str(instrument.ticker or "").strip() or None
    exchange = str(instrument.exchange or "").strip().upper() or None
    moved_identifiers: list[dict[str, Any]] = []
    identifier_values: set[str] = set()
    official_identifier_values: set[str] = set()
    conflicts: list[dict[str, Any]] = []
    audit_note = {
        "repaired_at": now.isoformat(),
        "actor": "codex",
        "old_issuer_id": int(old_issuer_id),
        "new_issuer_id": target_issuer_id,
        "target_company_id": target_company_id,
        "instrument_id": int(instrument.id),
        "note": note,
    }

    old_identifiers = (
        session.query(IssuerIdentifier)
        .filter_by(issuer_id=int(old_issuer_id))
        .order_by(IssuerIdentifier.id.asc())
        .all()
    )
    for ident in old_identifiers:
        id_type = str(ident.id_type or "").strip().upper()
        values = _identity_repair_identifier_values(ident)
        reason = None
        if _identity_repair_ticker_identifier_matches(ident, ticker=ticker, exchange=exchange):
            reason = "instrument_listing"
        elif values and values.intersection(hints.get(id_type, set())):
            reason = "official_evidence_identifier"
        if not reason:
            continue
        moved_identifiers.append(
            {
                "id": int(ident.id),
                "id_type": id_type,
                "id_value": ident.id_value,
                "exchange": ident.exchange,
                "old_issuer_id": int(old_issuer_id),
                "new_issuer_id": target_issuer_id,
                "reason": reason,
            }
        )
        identifier_values.update(values)
        if id_type not in {"TICKER", "TW_STOCK", "HK_STOCK"} and not id_type.startswith("TICKER_"):
            official_identifier_values.update(values)
        if apply:
            ident.issuer_id = target_issuer_id
            if id_type == "LEI":
                lei = _normalize_audit_lei(ident.id_value)
                if lei and not target_issuer.lei:
                    existing_lei = session.query(Issuer).filter(Issuer.lei == lei, Issuer.id != target_issuer_id).first()
                    if existing_lei is None:
                        target_issuer.lei = lei
            target_issuer.updated_at = now

    names = {
        str(value or "").strip().lower()
        for value in (
            instrument.display_name,
            instrument.legal_name,
            target_issuer.display_name if target_issuer is not None else None,
            target_issuer.legal_name if target_issuer is not None else None,
            target_company.name if target_company is not None else None,
        )
        if str(value or "").strip()
    }
    tickers = {
        str(value or "").strip().upper()
        for value in (
            ticker,
            target_company.ticker if target_company is not None else None,
        )
        if str(value or "").strip()
    }
    document_ids: set[int] = set()
    moved_filings: list[dict[str, Any]] = []
    moved_disclosures: list[dict[str, Any]] = []
    moved_artifacts: list[dict[str, Any]] = []
    moved_events: list[dict[str, Any]] = []
    moved_watch_states: list[dict[str, Any]] = []
    moved_summaries: list[dict[str, Any]] = []

    def move_row(row: Any, bucket: list[dict[str, Any]], row_type: str, reason: str) -> bool:
        before = {"id": int(row.id), "old_issuer_id": int(row.issuer_id), "new_issuer_id": target_issuer_id, "reason": reason}
        bucket.append(before)
        if getattr(row, "document_id", None) is not None:
            document_ids.add(int(row.document_id))
        if not apply:
            return True
        row.issuer_id = target_issuer_id
        if hasattr(row, "raw_meta"):
            row.raw_meta = _annotate_identity_repaired_meta(row.raw_meta, audit_note={**audit_note, "row_type": row_type, "row_id": int(row.id), "reason": reason})
        return True

    for filing in session.query(SecuritiesFiling).filter_by(issuer_id=int(old_issuer_id)).order_by(SecuritiesFiling.id.asc()).all():
        blob = _identity_repair_text_blob(
            filing.regulator,
            filing.accession_number,
            filing.title,
            filing.primary_document_url,
            filing.index_url,
            filing.filing_url,
            filing.raw_meta,
        )
        if _identity_repair_record_identity_match(blob, identifier_values=official_identifier_values):
            move_row(filing, moved_filings, "securities_filing", "matched_moved_identifier")

    for disclosure in session.query(ExchangeDisclosure).filter_by(issuer_id=int(old_issuer_id)).order_by(ExchangeDisclosure.id.asc()).all():
        blob = _identity_repair_text_blob(
            disclosure.exchange,
            disclosure.disclosure_id,
            disclosure.title,
            disclosure.headline,
            disclosure.disclosure_url,
            disclosure.attachment_urls,
            disclosure.raw_meta,
        )
        if _identity_repair_record_identity_match(blob, identifier_values=official_identifier_values):
            move_row(disclosure, moved_disclosures, "exchange_disclosure", "matched_moved_identifier")
        elif _identity_repair_record_name_match(blob, names=names, tickers=tickers):
            move_row(disclosure, moved_disclosures, "exchange_disclosure", "matched_target_name_and_ticker")

    moved_artifact_ids: set[int] = set()
    for artifact in session.query(IRArtifact).filter_by(issuer_id=int(old_issuer_id)).order_by(IRArtifact.id.asc()).all():
        blob = _identity_repair_text_blob(
            artifact.artifact_type,
            artifact.artifact_subtype,
            artifact.title,
            artifact.description,
            artifact.canonical_url,
            artifact.download_url,
            artifact.webcast_url,
            artifact.audio_url,
            artifact.raw_meta,
        )
        reason = None
        if artifact.document_id is not None and int(artifact.document_id) in document_ids:
            reason = "matched_moved_document"
        elif (
            artifact.document_id is not None
            and target_company_id is not None
            and session.query(CompanyDocument.id)
            .filter_by(document_id=int(artifact.document_id), company_id=int(target_company_id))
            .first()
            is not None
        ):
            reason = "matched_target_company_document"
        elif _identity_repair_record_identity_match(blob, identifier_values=official_identifier_values):
            reason = "matched_moved_identifier"
        elif _identity_repair_record_name_match(blob, names=names, tickers=tickers):
            reason = "matched_target_name_and_ticker"
        if not reason:
            continue
        existing = (
            session.query(IRArtifact)
            .filter_by(issuer_id=target_issuer_id, artifact_type=artifact.artifact_type, canonical_url=artifact.canonical_url)
            .first()
        )
        if existing is not None and int(existing.id) != int(artifact.id):
            conflicts.append({"row_type": "ir_artifact", "id": int(artifact.id), "existing_target_id": int(existing.id), "canonical_url": artifact.canonical_url})
            continue
        move_row(artifact, moved_artifacts, "ir_artifact", reason)
        moved_artifact_ids.add(int(artifact.id))

    moved_filing_ids = {int(row["id"]) for row in moved_filings}
    moved_disclosure_ids = {int(row["id"]) for row in moved_disclosures}
    for event in session.query(EarningsEvent).filter_by(issuer_id=int(old_issuer_id)).order_by(EarningsEvent.id.asc()).all():
        linked = {
            int(value)
            for value in (
                event.release_filing_id,
                event.release_disclosure_id,
                event.release_artifact_id,
                event.presentation_artifact_id,
                event.transcript_artifact_id,
                event.webcast_artifact_id,
            )
            if value is not None
        }
        if not (linked.intersection(moved_artifact_ids) or (event.release_filing_id and int(event.release_filing_id) in moved_filing_ids) or (event.release_disclosure_id and int(event.release_disclosure_id) in moved_disclosure_ids)):
            continue
        existing = (
            session.query(EarningsEvent)
            .filter_by(issuer_id=target_issuer_id, fiscal_year=event.fiscal_year, fiscal_quarter=event.fiscal_quarter)
            .first()
        )
        if existing is not None and int(existing.id) != int(event.id):
            conflicts.append({"row_type": "earnings_event", "id": int(event.id), "existing_target_id": int(existing.id), "fiscal_year": event.fiscal_year, "fiscal_quarter": event.fiscal_quarter})
            continue
        move_row(event, moved_events, "earnings_event", "matched_moved_event_link")
        if apply:
            event.updated_at = now

    for watch in session.query(TranscriptWatchState).filter_by(issuer_id=int(old_issuer_id)).order_by(TranscriptWatchState.id.asc()).all():
        if not ticker:
            continue
        watch_ticker = str(watch.ticker or "").strip().upper()
        if watch_ticker and watch_ticker != ticker.upper():
            continue
        moved_watch_states.append({"id": int(watch.id), "old_issuer_id": int(old_issuer_id), "new_issuer_id": target_issuer_id, "ticker": watch.ticker})
        if apply:
            watch.issuer_id = target_issuer_id
            watch.company_id = target_company_id
            watch.updated_at = now

    for summary in session.query(TranscriptSummary).filter_by(issuer_id=int(old_issuer_id)).order_by(TranscriptSummary.id.asc()).all():
        summary_ticker = str(summary.ticker or "").strip().upper()
        if summary.document_id not in document_ids and (not ticker or (summary_ticker and summary_ticker != ticker.upper())):
            continue
        moved_summaries.append({"id": int(summary.id), "old_issuer_id": int(old_issuer_id), "new_issuer_id": target_issuer_id, "document_id": int(summary.document_id)})
        if apply:
            summary.issuer_id = target_issuer_id
            summary.company_id = target_company_id
            summary.updated_at = now

    old_company_ids = {
        int(row[0])
        for row in session.query(ResearchInstrument.company_id)
        .filter(ResearchInstrument.issuer_id == int(old_issuer_id), ResearchInstrument.company_id.isnot(None))
        .all()
    }
    old_company_ids.update(
        int(row[0])
        for row in session.query(Company.id)
        .filter(Company.issuer_id == int(old_issuer_id))
        .all()
    )
    if old_company_id is not None:
        old_company_ids.add(int(old_company_id))
    company_document_result = _identity_repair_move_company_documents(
        session,
        document_ids=document_ids,
        target_company_id=target_company_id,
        old_company_ids=old_company_ids,
        audit_note=audit_note,
    ) if apply else {"moved": [], "merged": [], "skipped": []}

    if apply:
        session.flush()

    return {
        "identifier_hints": {key: sorted(values) for key, values in hints.items()},
        "moved_identifiers": moved_identifiers,
        "moved_filings": moved_filings,
        "moved_disclosures": moved_disclosures,
        "moved_artifacts": moved_artifacts,
        "moved_earnings_events": moved_events,
        "moved_transcript_watch_states": moved_watch_states,
        "moved_transcript_summaries": moved_summaries,
        "company_documents": company_document_result,
        "conflicts": conflicts,
    }


def research_identity_repair(
    session: Session,
    *,
    instrument_id: int,
    apply: bool = False,
    issuer_id: int | None = None,
    company_id: int | None = None,
    subject_id: int | None = None,
    issuer_name: str | None = None,
    company_name: str | None = None,
    ticker: str | None = None,
    exchange: str | None = None,
    jurisdiction: str | None = None,
    website: str | None = None,
    ir_website: str | None = None,
    evidence_links: list[dict[str, Any]] | None = None,
    gap_id: int | None = None,
    gap_key: str | None = None,
    resolve_gap: bool = True,
    note: str | None = None,
) -> dict[str, Any]:
    """Preview or apply a bounded issuer/company/subject relink for one instrument."""
    instrument = session.get(ResearchInstrument, int(instrument_id))
    if instrument is None:
        raise ValueError("Research instrument not found.")
    evidence = [dict(row) for row in evidence_links or [] if isinstance(row, dict) and str(row.get("url") or "").strip()]
    if apply and not evidence:
        raise ValueError("Applying identity repair requires at least one official evidence link.")
    gap = _identity_repair_gap(session, gap_id=gap_id, gap_key=gap_key)
    previous_subject = _subject_for_instrument(session, instrument, ensure=False)
    before = {
        "instrument": _instrument_payload(instrument),
        "issuer": _issuer_payload(session.get(Issuer, int(instrument.issuer_id)) if instrument.issuer_id is not None else None),
        "company": _company_payload(session.get(Company, int(instrument.company_id)) if instrument.company_id is not None else None),
        "subject": _subject_payload(previous_subject),
    }

    has_existing_target_ref = issuer_id is not None or company_id is not None or subject_id is not None
    explicit_ticker = str(ticker).strip() if ticker is not None and str(ticker).strip() else None
    explicit_exchange = str(exchange).strip().upper() if exchange is not None and str(exchange).strip() else None
    explicit_jurisdiction = str(jurisdiction).strip().upper() if jurisdiction is not None and str(jurisdiction).strip() else None
    use_instrument_listing_as_target = not has_existing_target_ref
    target_ticker = explicit_ticker or (str(instrument.ticker).strip() if use_instrument_listing_as_target and instrument.ticker else None)
    target_exchange = explicit_exchange or (
        str(instrument.exchange).strip().upper() if use_instrument_listing_as_target and instrument.exchange else None
    )
    target_jurisdiction = explicit_jurisdiction or (
        str(instrument.country).strip().upper() if use_instrument_listing_as_target and instrument.country else None
    )
    identifier_ticker = str(instrument.ticker or target_ticker or "").strip() or None
    identifier_exchange = str(instrument.exchange or target_exchange or "").strip().upper() or None

    issuer, company, subject, planned = _ensure_target_identity(
        session,
        apply=apply,
        issuer_id=issuer_id,
        company_id=company_id,
        subject_id=subject_id,
        issuer_name=issuer_name,
        company_name=company_name,
        ticker=target_ticker,
        exchange=target_exchange,
        jurisdiction=target_jurisdiction,
        update_existing_company_listing=bool(use_instrument_listing_as_target or explicit_ticker or explicit_exchange or explicit_jurisdiction),
        identifier_ticker=identifier_ticker,
        identifier_exchange=identifier_exchange,
        website=website,
        ir_website=ir_website,
    )
    target = {
        "issuer": _issuer_payload(issuer),
        "company": _company_payload(company),
        "subject": _subject_payload(subject),
        "planned_actions": planned,
    }
    warnings: list[str] = []
    if issuer is not None and company is not None and company.issuer_id is not None and int(company.issuer_id) != int(issuer.id):
        raise ValueError(f"Target company {company.id} is linked to issuer {company.issuer_id}, not target issuer {issuer.id}.")
    if instrument.ticker and ticker and _base_symbol(instrument.ticker) != _base_symbol(ticker):
        warnings.append("Instrument ticker differs from target ticker; verify this is not an ADR/local or stale symbol case.")
    if instrument.exchange and exchange and str(instrument.exchange).strip().upper() != str(exchange).strip().upper():
        warnings.append("Instrument exchange differs from target exchange; verify before apply.")

    affected_campaign_items: list[dict[str, Any]] = []
    attribution_repair: dict[str, Any] = {
        "identifier_hints": {},
        "moved_identifiers": [],
        "moved_filings": [],
        "moved_disclosures": [],
        "moved_artifacts": [],
        "moved_earnings_events": [],
        "moved_transcript_watch_states": [],
        "moved_transcript_summaries": [],
        "company_documents": {"moved": [], "merged": [], "skipped": []},
        "conflicts": [],
    }
    if apply:
        now = utcnow()
        old_issuer_id = instrument.issuer_id
        old_company_id = instrument.company_id
        instrument.issuer_id = int(issuer.id) if issuer is not None else None
        instrument.company_id = int(company.id) if company is not None else None
        metadata = _json_dict(instrument.metadata_json)
        history = list(metadata.get("identity_repair_history") or [])
        history.append(
            {
                "repaired_at": now.isoformat(),
                "actor": "codex",
                "old_issuer_id": int(old_issuer_id) if old_issuer_id is not None else None,
                "old_company_id": int(old_company_id) if old_company_id is not None else None,
                "new_issuer_id": int(instrument.issuer_id) if instrument.issuer_id is not None else None,
                "new_company_id": int(instrument.company_id) if instrument.company_id is not None else None,
                "gap_id": int(gap.id) if gap is not None else None,
                "evidence_links": evidence,
                "note": note,
            }
        )
        metadata["identity_repair_history"] = history[-20:]
        metadata["last_identity_repair_at"] = now.isoformat()
        instrument.metadata_json = metadata
        instrument.updated_at = now
        if gap is not None and resolve_gap:
            gap.status = "resolved"
            gap.last_reviewed_at = now
            gap.updated_at = now
            payload = _json_dict(gap.payload)
            payload.update(
                {
                    "resolved_by": "codex",
                    "resolved_at": now.isoformat(),
                    "resolution_note": note or "Resolved by research identity repair.",
                    "target_issuer_id": int(issuer.id) if issuer is not None else None,
                    "target_company_id": int(company.id) if company is not None else None,
                    "target_subject_id": int(subject.id) if subject is not None else None,
                    "evidence_links": evidence,
                }
            )
            gap.payload = payload
            for request in session.query(InstrumentRequest).all():
                linked = _json_dict(request.linked_objects)
                if int(linked.get("gap_id") or 0) == int(gap.id):
                    request.status = "resolved"
                    request.reviewed_at = now
                    request.updated_at = now
                    request.review_note = note or "Resolved by research identity repair."
        if subject is not None:
            for item in session.query(ResearchCampaignItem).all():
                payload = _json_dict(item.payload_json)
                if int(payload.get("instrument_id") or 0) != int(instrument.id):
                    continue
                item.target_type = "subject"
                item.target_id = int(subject.id)
                item.subject_id = int(subject.id)
                item.target_key = subject.subject_key
                item.item_key = f"campaign:{int(item.campaign_id)}:subject:{subject.subject_key}"
                payload["identity_repair"] = {
                    "repaired_at": now.isoformat(),
                    "instrument_id": int(instrument.id),
                    "old_subject_id": int(previous_subject.id) if previous_subject is not None else None,
                    "new_subject_id": int(subject.id),
                }
                item.payload_json = payload
                item.updated_at = now
                affected_campaign_items.append({"id": int(item.id), "campaign_id": int(item.campaign_id), "target_key": item.target_key})
        attribution_repair = _migrate_identity_repair_attribution(
            session,
            apply=True,
            old_issuer_id=int(old_issuer_id) if old_issuer_id is not None else None,
            old_company_id=int(old_company_id) if old_company_id is not None else None,
            target_issuer=issuer,
            target_company=company,
            instrument=instrument,
            evidence=evidence,
            note=note,
        )
        session.flush()
    elif issuer is not None:
        attribution_repair = _migrate_identity_repair_attribution(
            session,
            apply=False,
            old_issuer_id=int(instrument.issuer_id) if instrument.issuer_id is not None else None,
            old_company_id=int(instrument.company_id) if instrument.company_id is not None else None,
            target_issuer=issuer,
            target_company=company,
            instrument=instrument,
            evidence=evidence,
            note=note,
        )

    return {
        "applied": bool(apply),
        "instrument_id": int(instrument.id),
        "gap": serialize_coverage_gap(gap) if gap is not None else None,
        "before": before,
        "target": target,
        "after": {
            "instrument": _instrument_payload(instrument),
            "issuer": _issuer_payload(issuer),
            "company": _company_payload(company),
            "subject": _subject_payload(subject),
        }
        if apply
        else None,
        "evidence_links": evidence,
        "warnings": warnings,
        "attribution_repair": attribution_repair,
        "affected_campaign_items": affected_campaign_items,
        "next_steps": [
            "Run research-canon-maintenance for the affected universe.",
            "Refresh or recreate the campaign item so the new subject receives current source/scenario/report blockers.",
        ],
    }


def active_universe_subjects(
    session: Session,
    *,
    universe_slug: str | None = None,
    ensure: bool = False,
    rows: list[ResearchUniverseRow] | None = None,
) -> dict[int, dict[str, Any]]:
    rows = list(rows) if rows is not None else _active_universe_rows(session, universe_slug=universe_slug)
    out: dict[int, dict[str, Any]] = {}
    for row in rows:
        instrument = row.instrument
        if instrument is None:
            continue
        subject = _subject_for_instrument(session, instrument, ensure=ensure)
        if subject is None or subject.id is None:
            continue
        entry = out.setdefault(
            int(subject.id),
            {
                "subject": subject,
                "instruments": {},
                "universes": {},
                "rows": [],
            },
        )
        entry["instruments"][int(instrument.id)] = instrument
        entry["universes"][int(row.universe_id)] = row.universe
        entry["rows"].append(row)
    return out


def serialize_scenario(row: ResearchSubjectScenario | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": int(row.id),
        "subject_id": int(row.subject_id),
        "slug": row.slug,
        "name": row.name,
        "scenario_kind": row.scenario_kind,
        "probability": float(row.probability) if row.probability is not None else None,
        "confidence": float(row.confidence) if row.confidence is not None else None,
        "selected_canon": bool(row.selected_canon),
        "thesis_summary": row.thesis_summary,
        "assumptions": _json_dict(row.assumptions_json),
        "key_metrics": _json_dict(row.key_metrics_json),
        "evidence_links": list(row.evidence_links_json or []),
        "invalidation_triggers": list(row.invalidation_triggers_json or []),
        "open_questions": list(row.open_questions_json or []),
        "status": row.status,
        "supersedes_scenario_id": int(row.supersedes_scenario_id) if row.supersedes_scenario_id is not None else None,
        "audit": _json_dict(row.audit_json),
        "created_at": _dt(row.created_at),
        "updated_at": _dt(row.updated_at),
    }


def stable_scenario_payload(row: ResearchSubjectScenario | None) -> dict[str, Any] | None:
    """Scenario payload for content fingerprints; excludes timestamps and audit metadata."""
    if row is None:
        return None
    return {
        "subject_id": int(row.subject_id),
        "slug": row.slug,
        "name": row.name,
        "scenario_kind": row.scenario_kind,
        "probability": float(row.probability) if row.probability is not None else None,
        "confidence": float(row.confidence) if row.confidence is not None else None,
        "selected_canon": bool(row.selected_canon),
        "thesis_summary": row.thesis_summary,
        "assumptions": _json_dict(row.assumptions_json),
        "key_metrics": _json_dict(row.key_metrics_json),
        "evidence_links": list(row.evidence_links_json or []),
        "invalidation_triggers": list(row.invalidation_triggers_json or []),
        "open_questions": list(row.open_questions_json or []),
        "status": row.status,
        "supersedes_scenario_id": int(row.supersedes_scenario_id) if row.supersedes_scenario_id is not None else None,
    }


def repair_selected_canon_scenarios(
    session: Session,
    *,
    subject_id: int | None = None,
    keep_scenario_id: int | None = None,
    supersede_previous: bool = False,
) -> dict[str, int]:
    """Enforce one active selected canon scenario per subject at the service layer."""
    q = session.query(ResearchSubjectScenario).filter(
        ResearchSubjectScenario.selected_canon.is_(True),
        ResearchSubjectScenario.status.in_(["draft", "active"]),
    )
    if subject_id is not None:
        q = q.filter(ResearchSubjectScenario.subject_id == int(subject_id))
    rows = q.order_by(
        ResearchSubjectScenario.subject_id.asc(),
        ResearchSubjectScenario.updated_at.desc(),
        ResearchSubjectScenario.id.desc(),
    ).all()
    by_subject: dict[int, list[ResearchSubjectScenario]] = defaultdict(list)
    for row in rows:
        by_subject[int(row.subject_id)].append(row)
    repaired = 0
    checked = 0
    now = utcnow()
    for sid, candidates in by_subject.items():
        checked += 1
        keep = None
        if keep_scenario_id is not None:
            keep = next((row for row in candidates if int(row.id) == int(keep_scenario_id)), None)
        if keep is None:
            keep = candidates[0]
        for row in candidates:
            if int(row.id) == int(keep.id):
                continue
            row.selected_canon = False
            if supersede_previous and row.status in {"draft", "active"}:
                row.status = "superseded"
            row.updated_at = now
            repaired += 1
    session.flush()
    return {"subjects_checked": checked, "scenarios_repaired": repaired}


def upsert_subject_scenario(
    session: Session,
    *,
    subject_id: int,
    slug: str | None = None,
    name: str | None = None,
    scenario_kind: str = "custom",
    probability: float | None = None,
    confidence: float | None = None,
    selected_canon: bool = False,
    thesis_summary: str | None = None,
    assumptions: dict[str, Any] | None = None,
    key_metrics: dict[str, Any] | None = None,
    evidence_links: list[dict[str, Any]] | None = None,
    invalidation_triggers: list[str] | None = None,
    open_questions: list[str] | None = None,
    status: str = "draft",
    audit: dict[str, Any] | None = None,
) -> ResearchSubjectScenario:
    subject = session.get(ResearchSubject, int(subject_id))
    if subject is None:
        raise ValueError("Research subject not found.")
    clean_name = _short(name or slug or "Canon scenario")
    clean_slug = _slugify(slug or clean_name, default="scenario")
    kind = str(scenario_kind or "custom").strip().lower()
    if kind not in SCENARIO_KINDS:
        kind = "custom"
    status_norm = str(status or "draft").strip().lower()
    if status_norm not in SCENARIO_STATUSES:
        status_norm = "draft"
    row = session.query(ResearchSubjectScenario).filter_by(subject_id=int(subject.id), slug=clean_slug).first()
    now = utcnow()
    if row is None:
        row = ResearchSubjectScenario(
            subject_id=int(subject.id),
            slug=clean_slug,
            name=clean_name,
            scenario_kind=kind,
            probability=probability,
            confidence=confidence,
            selected_canon=False,
            thesis_summary=_short(thesis_summary, 4000) or None,
            assumptions_json=dict(assumptions or {}),
            key_metrics_json=dict(key_metrics or {}),
            evidence_links_json=[dict(item) for item in (evidence_links or []) if isinstance(item, dict)],
            invalidation_triggers_json=[str(item).strip() for item in (invalidation_triggers or []) if str(item).strip()],
            open_questions_json=[str(item).strip() for item in (open_questions or []) if str(item).strip()],
            status=status_norm,
            audit_json=dict(audit or {}),
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.flush()
    else:
        row.name = clean_name
        row.scenario_kind = kind
        row.probability = probability
        row.confidence = confidence
        row.thesis_summary = _short(thesis_summary, 4000) or row.thesis_summary
        row.assumptions_json = dict(assumptions or row.assumptions_json or {})
        row.key_metrics_json = dict(key_metrics or row.key_metrics_json or {})
        if evidence_links is not None:
            row.evidence_links_json = [dict(item) for item in evidence_links if isinstance(item, dict)]
        if invalidation_triggers is not None:
            row.invalidation_triggers_json = [str(item).strip() for item in invalidation_triggers if str(item).strip()]
        if open_questions is not None:
            row.open_questions_json = [str(item).strip() for item in open_questions if str(item).strip()]
        row.status = status_norm
        if audit:
            merged = _json_dict(row.audit_json)
            merged.update(dict(audit))
            row.audit_json = merged
        row.updated_at = now
        session.flush()
    if selected_canon:
        select_canon_scenario(session, scenario_id=int(row.id), supersede_previous=False)
        session.flush()
    return row


def get_selected_canon_scenario(session: Session, *, subject_id: int) -> ResearchSubjectScenario | None:
    repair_selected_canon_scenarios(session, subject_id=int(subject_id))
    return (
        session.query(ResearchSubjectScenario)
        .filter(
            ResearchSubjectScenario.subject_id == int(subject_id),
            ResearchSubjectScenario.selected_canon.is_(True),
            ResearchSubjectScenario.status.in_(["draft", "active"]),
        )
        .order_by(ResearchSubjectScenario.updated_at.desc(), ResearchSubjectScenario.id.desc())
        .first()
    )


def select_canon_scenario(session: Session, *, scenario_id: int, supersede_previous: bool = False) -> ResearchSubjectScenario:
    row = session.get(ResearchSubjectScenario, int(scenario_id))
    if row is None:
        raise ValueError("Research subject scenario not found.")
    now = utcnow()
    previous_rows = (
        session.query(ResearchSubjectScenario)
        .filter(
            ResearchSubjectScenario.subject_id == int(row.subject_id),
            ResearchSubjectScenario.id != int(row.id),
            ResearchSubjectScenario.selected_canon.is_(True),
        )
        .all()
    )
    for previous in previous_rows:
        previous.selected_canon = False
        if supersede_previous and previous.status in {"draft", "active"}:
            previous.status = "superseded"
        previous.updated_at = now
    row.selected_canon = True
    if row.status == "draft":
        row.status = "active"
    row.updated_at = now
    audit = _json_dict(row.audit_json)
    audit["selected_canon_at"] = now.isoformat()
    audit["superseded_previous_count"] = len(previous_rows) if supersede_previous else 0
    row.audit_json = audit
    repair_selected_canon_scenarios(
        session,
        subject_id=int(row.subject_id),
        keep_scenario_id=int(row.id),
        supersede_previous=supersede_previous,
    )
    session.flush()
    return row


def list_subject_scenarios(session: Session, *, subject_id: int, include_archived: bool = False) -> list[dict[str, Any]]:
    q = session.query(ResearchSubjectScenario).filter(ResearchSubjectScenario.subject_id == int(subject_id))
    if not include_archived:
        q = q.filter(ResearchSubjectScenario.status != "archived")
    rows = q.order_by(ResearchSubjectScenario.selected_canon.desc(), ResearchSubjectScenario.updated_at.desc(), ResearchSubjectScenario.id.desc()).all()
    return [serialize_scenario(row) for row in rows]


def supersede_subject_scenario(
    session: Session,
    *,
    scenario_id: int,
    replacement_scenario_id: int | None = None,
    audit: dict[str, Any] | None = None,
) -> ResearchSubjectScenario:
    row = session.get(ResearchSubjectScenario, int(scenario_id))
    if row is None:
        raise ValueError("Research subject scenario not found.")
    now = utcnow()
    row.selected_canon = False
    row.status = "superseded"
    if replacement_scenario_id is not None:
        row.supersedes_scenario_id = int(replacement_scenario_id)
    merged = _json_dict(row.audit_json)
    merged.update({"superseded_at": now.isoformat(), **dict(audit or {})})
    row.audit_json = merged
    row.updated_at = now
    session.flush()
    return row


def draft_subject_scenario_set(
    session: Session,
    *,
    subject_id: int,
    scenarios: list[dict[str, Any]] | None = None,
    audit: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    defaults = [
        {"slug": "bear", "name": "Bear case", "scenario_kind": "bear"},
        {"slug": "base", "name": "Base case", "scenario_kind": "base"},
        {"slug": "bull", "name": "Bull case", "scenario_kind": "bull"},
        {"slug": "canon", "name": "Canon scenario", "scenario_kind": "canon"},
    ]
    created = []
    for payload in scenarios or defaults:
        if not isinstance(payload, dict):
            continue
        row = upsert_subject_scenario(
            session,
            subject_id=int(subject_id),
            slug=str(payload.get("slug") or payload.get("scenario_kind") or "scenario"),
            name=str(payload.get("name") or payload.get("slug") or "Scenario"),
            scenario_kind=str(payload.get("scenario_kind") or payload.get("kind") or "custom"),
            probability=payload.get("probability"),
            confidence=payload.get("confidence"),
            selected_canon=bool(payload.get("selected_canon", False)),
            thesis_summary=str(payload.get("thesis_summary") or "").strip() or None,
            assumptions=payload.get("assumptions") if isinstance(payload.get("assumptions"), dict) else None,
            key_metrics=payload.get("key_metrics") if isinstance(payload.get("key_metrics"), dict) else None,
            evidence_links=payload.get("evidence_links") if isinstance(payload.get("evidence_links"), list) else None,
            invalidation_triggers=payload.get("invalidation_triggers") if isinstance(payload.get("invalidation_triggers"), list) else None,
            open_questions=payload.get("open_questions") if isinstance(payload.get("open_questions"), list) else None,
            status=str(payload.get("status") or "draft"),
            audit={"source": "scenario_set", **dict(audit or {})},
        )
        created.append(serialize_scenario(row))
    return created


def serialize_source_pack(row: ResearchSourcePack | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": int(row.id),
        "subject_id": int(row.subject_id),
        "status": row.status,
        "checklist": list(row.checklist_json or []),
        "items": _json_dict(row.items_json),
        "missing_items": list(row.missing_items_json or []),
        "official_links": list(row.official_links_json or []),
        "source_counts": _json_dict(row.source_counts_json),
        "reviewed_at": _dt(row.reviewed_at),
        "updated_at": _dt(row.updated_at),
    }


def stable_source_pack_payload(row: ResearchSourcePack | None) -> dict[str, Any] | None:
    """Source-pack payload for report fingerprints; excludes review timestamps."""
    if row is None:
        return None
    return {
        "subject_id": int(row.subject_id),
        "status": row.status,
        "checklist": list(row.checklist_json or []),
        "items": _json_dict(row.items_json),
        "missing_items": list(row.missing_items_json or []),
        "official_links": list(row.official_links_json or []),
        "source_counts": _json_dict(row.source_counts_json),
    }


def _latest_row_payload(rows: list[Any], *, date_attr: str, title_attr: str, url_attrs: tuple[str, ...]) -> dict[str, Any] | None:
    if not rows:
        return None
    rows_sorted = sorted(rows, key=lambda item: getattr(item, date_attr, None) or getattr(item, "created_at", None), reverse=True)
    row = rows_sorted[0]
    url = None
    for attr in url_attrs:
        raw = getattr(row, attr, None)
        if raw:
            url = raw
            break
    return {
        "id": int(row.id),
        "title": getattr(row, title_attr, None),
        "url": url,
        "date": _dt(getattr(row, date_attr, None)),
    }


def _latest_row_payload_from_specs(specs: list[tuple[list[Any], str, str, tuple[str, ...]]]) -> dict[str, Any] | None:
    candidates: list[tuple[Any, str, str, tuple[str, ...]]] = []
    for rows, date_attr, title_attr, url_attrs in specs:
        for row in rows:
            candidates.append((row, date_attr, title_attr, url_attrs))
    if not candidates:
        return None
    row, date_attr, title_attr, url_attrs = sorted(
        candidates,
        key=lambda item: getattr(item[0], item[1], None) or getattr(item[0], "created_at", None),
        reverse=True,
    )[0]
    return _latest_row_payload([row], date_attr=date_attr, title_attr=title_attr, url_attrs=url_attrs)


def _subject_public_instruments(session: Session, subject: ResearchSubject) -> list[ResearchInstrument]:
    filters = []
    if subject.issuer_id is not None:
        filters.append(ResearchInstrument.issuer_id == int(subject.issuer_id))
    if subject.company_id is not None:
        filters.append(ResearchInstrument.company_id == int(subject.company_id))
    if not filters:
        return []
    return session.query(ResearchInstrument).filter(or_(*filters), ResearchInstrument.is_public.is_(True)).all()


def _subject_company_and_issuer(session: Session, subject: ResearchSubject) -> tuple[Company | None, Issuer | None]:
    company = session.get(Company, int(subject.company_id)) if subject.company_id is not None else None
    issuer = session.get(Issuer, int(subject.issuer_id)) if subject.issuer_id is not None else None
    if issuer is None and company is not None and company.issuer_id is not None:
        issuer = session.get(Issuer, int(company.issuer_id))
    return company, issuer


def _source_pack_not_required_for_subject(subject: ResearchSubject, public_instruments: list[ResearchInstrument]) -> bool:
    if subject.kind not in {"company", "issuer"} and not public_instruments:
        return True
    return bool(public_instruments) and all(_is_fund_like_instrument(instrument) for instrument in public_instruments)


def _parse_source_pack_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    text_value = value.strip()
    if not text_value:
        return None
    for candidate in (text_value, text_value[:10]):
        try:
            return date.fromisoformat(candidate)
        except ValueError:
            continue
    return None


def _known_public_listing_dates_for_subject(
    session: Session,
    *,
    subject: ResearchSubject,
    public_instruments: list[ResearchInstrument],
) -> list[date]:
    dates: list[date] = []
    for instrument in public_instruments:
        meta = _json_dict(instrument.metadata_json)
        for key in ("listing_date", "listed_at", "first_trading_date", "ipo_date", "ipo_listing_date"):
            parsed = _parse_source_pack_date(meta.get(key))
            if parsed is not None:
                dates.append(parsed)
    filters = []
    if subject.issuer_id is not None:
        filters.append(IPOEvent.issuer_id == int(subject.issuer_id))
    if subject.company_id is not None:
        filters.append(IPOEvent.company_id == int(subject.company_id))
    if filters:
        for event in session.query(IPOEvent).filter(or_(*filters)).all():
            if str(event.status or "").strip().lower() in {"cancelled", "withdrawn", "rejected"}:
                continue
            if event.listing_date is not None:
                dates.append(event.listing_date)
    return sorted(set(dates))


def _annual_filing_requirement_payload(
    session: Session,
    *,
    subject: ResearchSubject,
    public_instruments: list[ResearchInstrument],
    rail: str,
    rail_status: dict[str, Any],
) -> dict[str, Any]:
    rail_key = str(rail or "").strip().lower()
    rail_status_v = str(rail_status.get("status") or "").strip().lower()
    authority = ANNUAL_FILING_REQUIREMENT_BY_RAIL.get(rail_key, "annual report or annual financial statement equivalent")
    if not public_instruments:
        return {
            "status": "not_required",
            "required_now": False,
            "authority": authority,
            "reason": "no public operating instrument is present, so an official public-company annual filing is not expected",
        }
    if _source_pack_not_required_for_subject(subject, public_instruments):
        return {
            "status": "not_required",
            "required_now": False,
            "authority": authority,
            "reason": "subject is private/non-operating for source-pack purposes or only fund-like public instruments are present",
        }
    if rail_key == "not_required_public_fund":
        return {
            "status": "not_required_public_fund",
            "required_now": False,
            "authority": authority,
            "reason": "public instrument is fund-like and should not block operating-company source-pack readiness",
        }
    if rail_key == "not_required_private":
        return {
            "status": "not_required_private",
            "required_now": False,
            "authority": authority,
            "reason": "private instrument is not expected to publish public-company annual filings through official market rails",
        }
    if rail_key == "unimplemented" and any(_is_adr_like_instrument(instrument) for instrument in public_instruments):
        return {
            "status": "primary_or_parent_listing_resolution_needed",
            "required_now": None,
            "authority": authority,
            "reason": "annual filing likely exists at the parent or primary listing, but this subject is currently tied to an unresolved depositary/secondary listing rail",
        }
    if rail_key == "hkex" and rail_status_v == "identifier_not_in_source":
        return {
            "status": "inactive_listing_resolution_needed",
            "required_now": False,
            "authority": authority,
            "reason": "HKEX stock code is not present in the active HKEXnews stock-id map; resolve listing status before treating this as an annual-report backfill gap",
        }
    if rail_key == "sec" and subject.issuer_id is not None:
        termination = (
            session.query(SecuritiesFiling)
            .filter(
                SecuritiesFiling.issuer_id == int(subject.issuer_id),
                SecuritiesFiling.filing_type.in_(SEC_TERMINATION_FORMS),
            )
            .order_by(SecuritiesFiling.filed_at.desc().nullslast(), SecuritiesFiling.id.desc())
            .first()
        )
        latest_annual = (
            session.query(SecuritiesFiling)
            .filter(
                SecuritiesFiling.issuer_id == int(subject.issuer_id),
                SecuritiesFiling.filing_type.in_(SEC_ANNUAL_EQUIVALENT_FORMS),
            )
            .order_by(SecuritiesFiling.filed_at.desc().nullslast(), SecuritiesFiling.id.desc())
            .first()
        )
        if termination is not None and (
            latest_annual is None
            or latest_annual.filed_at is None
            or (termination.filed_at is not None and termination.filed_at > latest_annual.filed_at)
        ):
            return {
                "status": "inactive_listing_resolution_needed",
                "required_now": False,
                "authority": authority,
                "termination_filing_id": int(termination.id),
                "termination_form": termination.filing_type,
                "termination_filed_at": termination.filed_at.isoformat() if termination.filed_at is not None else None,
                "reason": "latest SEC annual-equivalent obligation is superseded by an SEC termination/delisting filing; resolve listing status before treating this as an annual-report backfill gap",
            }
    listing_dates = _known_public_listing_dates_for_subject(session, subject=subject, public_instruments=public_instruments)
    today = utcnow().date()
    if listing_dates:
        earliest_listing = listing_dates[0]
        age_days = (today - earliest_listing).days
        if age_days < 0:
            return {
                "status": "future_listing_not_required_yet",
                "required_now": False,
                "authority": authority,
                "listing_date": earliest_listing.isoformat(),
                "reason": "known listing date is in the future, so a public annual filing is not due yet",
            }
        if age_days <= ANNUAL_FIRST_REPORT_NOT_YET_DUE_DAYS and rail_status_v == "bound":
            return {
                "status": "first_annual_not_yet_due",
                "required_now": False,
                "authority": authority,
                "listing_date": earliest_listing.isoformat(),
                "listing_age_days": age_days,
                "reason": "known public listing is recent enough that the first annual-report-equivalent may not be due yet",
            }
    return {
        "status": "annual_equivalent_required",
        "required_now": True,
        "authority": authority,
        "reason": "public operating issuer should have an annual-report-equivalent on its official regulator, exchange, or issuer rail",
    }


def _source_pack_status(missing_items: list[str], *, not_required: bool = False) -> str:
    if not_required:
        return "not_required"
    if len(missing_items) == len(SOURCE_PACK_REQUIRED_ITEMS):
        return "missing"
    if missing_items:
        return "partial"
    return "complete"


def _is_rejected_source_pack_artifact(row: IRArtifact) -> bool:
    return str(row.artifact_type or "").lower().startswith("rejected") or str(_json_dict(row.raw_meta).get("research_canon_status") or "") == "rejected_for_source_pack"


def _is_earnings_result_artifact(row: IRArtifact) -> bool:
    if _is_rejected_source_pack_artifact(row):
        return False
    artifact_type = str(row.artifact_type or "").lower()
    if artifact_type in {"calendar_event", "webcast", "audio", "transcript"}:
        return False
    title = str(row.title or "").lower()
    subtype = str(row.artifact_subtype or "").lower()
    if "conference call" in title:
        return False
    if subtype in {"results", "guidance"}:
        return True
    if subtype == "earnings":
        return "earnings" in title or "result" in title or "quarter" in title or "year ended" in title
    return "announces financial results" in title or ("reports" in title and "results" in title)


_SOURCE_PACK_ESG_RE = re.compile(
    r"\b(?:esg|environmental,\s*social|environmental\s+social|sustainability)\b|"
    r"環境、?社會及管治|环境、?社会及管治|環境社會及管治|环境社会及管治",
    re.IGNORECASE,
)
_SOURCE_PACK_ANNUAL_REPORT_RE = re.compile(
    r"\bannual\s+report\b|\bintegrated\s*report\b|integratedreport|年度報告|年度报告",
    re.IGNORECASE,
)
_SOURCE_PACK_REPORT_WRAPPER_RE = re.compile(
    r"\b(?:notification\s+letter|reply\s+form|letter\s+to\s+(?:existing\s+)?(?:registered\s+)?shareholders?|letter\s+to\s+non-registered\s+holders?)\b|"
    r"\bnew\s+arrangements?\s+for\s+the\s+dissemination\s+of\s+corporate\s+communications\b|"
    r"提示性公告|"
    r"關於.*(?:年度報告|半年度報告|季度報告).*(?:披露|延期|延遲|時間|日期|變更|提示|公告|通知)|"
    r"关于.*(?:年度报告|半年度报告|季度报告).*(?:披露|延期|延迟|时间|日期|变更|提示|公告|通知)",
    re.IGNORECASE,
)
_SOURCE_PACK_FINANCIAL_RESULT_DISCLOSURE_RE = re.compile(
    r"\b(?:annual|final|interim|half[-\s]?year|quarterly)\s+(?:results?|report)\b|"
    r"\bhalf[-\s]?year\s+accounts?\b|"
    r"\bappendix\s+4[de]\b|"
    r"\bfinancial\s+summary\s+for\s+(?:the\s+)?fiscal\s+year\s+ended\b|"
    r"\bfy\s?\d{2,4}\s+financial\s+report\b|"
    r"\bsummary\s+report\s+on\s+(?:the\s+)?consolidated\s+results?\b|"
    r"\b(?:consolidated\s+)?results?\s+for\s+(?:the\s+)?(?:fiscal\s+year|year)\s+ended\b|"
    r"\bfinancial\s+statements?\b|"
    r"決算短信|四半期決算|通期決算|年度決算|決算説明資料|"
    r"年度業績|年度业绩|全年業績|全年业绩|中期報告|中期报告|半年度報告|半年度报告|"
    r"季度業績|季度业绩|(?:第一|第二|第三|第四|一|二|三|四|1|2|3|4)季度報告|"
    r"(?:第一|第二|第三|第四|一|二|三|四|1|2|3|4)季度报告",
    re.IGNORECASE,
)
_SOURCE_PACK_FULL_YEAR_RESULT_DISCLOSURE_RE = re.compile(
    r"\bappendix\s+4e\b|"
    r"\bannual\s+financial\s+results?\b|"
    r"\bfull[-\s]?year\s+(?:(?:consolidated\s+)?financial\s+|consolidated\s+)?results?\b|"
    r"\bfinal\s+results?.*\b(?:fiscal\s+year|year)\s+ended\b|"
    r"\b(?:consolidated\s+)?financial\s+results?\s*(?:\([^)]{1,80}\)\s*)?for\s+(?:the\s+)?(?:fiscal\s+year|year)\s+(?:ended|ending)\b|"
    r"\b(?:consolidated\s+)?financial\s+results?\s+of\s+fiscal\s+year\b|"
    r"\b(?:consolidated\s+)?financial\s+results?\s+for\s+fy\b|"
    r"\bfy\s?\d{2,4}\s+(?:consolidated\s+)?financial\s+results?\b|"
    r"\b(?:summary|outline).*financial\s+results?.*(?:fiscal\s+year|year)\s+ended\b|"
    r"\bfinancial\s+summary\s+for\s+(?:the\s+)?fiscal\s+year\s+ended\b|"
    r"\bfinancial\s+statements?\s+for\s+(?:the\s+)?(?:12[-\s]*month|twelve[-\s]*month|fiscal\s+year|year)(?:\s+period)?\s+ended\b|"
    r"\d{4}年\d{1,2}月期\s*決算短信|通期決算|年度決算|本決算",
    re.IGNORECASE,
)
_SOURCE_PACK_CONTEXTUAL_ANNUAL_RESULT_DISCLOSURE_RE = re.compile(
    r"\bannual\s+results?\b|"
    r"年度業績|年度业绩|全年業績|全年业绩",
    re.IGNORECASE,
)
_SOURCE_PACK_RESULT_CONTEXT_RE = re.compile(r"\b(?:results?|financial\s+statements?)\b", re.IGNORECASE)
_SOURCE_PACK_NON_ANNUAL_PERIOD_RESULT_RE = re.compile(
    r"\b(?:three|six|nine)\s+months?\s+ended\b|"
    r"\b(?:first|second|third)\s+quarter\b|"
    r"\bq[1-3]\b|"
    r"\b(?:interim|half[-\s]?year)\s+results?\b|"
    r"四半期決算|第(?:1|2|3|一|二|三)四半期決算|"
    r"中期報告|中期报告|半年度報告|半年度报告|季度業績|季度业绩|"
    r"(?:第一|第二|第三|一|二|三|1|2|3)季度報告|(?:第一|第二|第三|一|二|三|1|2|3)季度报告",
    re.IGNORECASE,
)


def _source_pack_disclosure_blob(row: ExchangeDisclosure) -> str:
    return " ".join(
        str(value or "")
        for value in (
            row.title,
            row.headline,
            row.disclosure_type,
            row.disclosure_subtype,
            row.disclosure_url,
        )
    )


def _is_esg_source_pack_disclosure(row: ExchangeDisclosure) -> bool:
    title_blob = " ".join(str(value or "") for value in (row.title, row.headline))
    if _SOURCE_PACK_ANNUAL_REPORT_RE.search(title_blob):
        return False
    return bool(_SOURCE_PACK_ESG_RE.search(title_blob))


def _has_source_pack_financial_result_context(row: ExchangeDisclosure) -> bool:
    if row.is_earnings_related:
        return True
    type_blob = " ".join(str(value or "") for value in (row.disclosure_type, row.disclosure_subtype))
    return bool(_SOURCE_PACK_RESULT_CONTEXT_RE.search(type_blob))


def _is_annual_source_pack_disclosure(row: ExchangeDisclosure) -> bool:
    blob = _source_pack_disclosure_blob(row)
    title_blob = " ".join(str(value or "") for value in (row.title, row.headline))
    if _SOURCE_PACK_REPORT_WRAPPER_RE.search(title_blob):
        return False
    if _is_esg_source_pack_disclosure(row):
        return False
    if _SOURCE_PACK_ANNUAL_REPORT_RE.search(blob):
        return True
    if _SOURCE_PACK_NON_ANNUAL_PERIOD_RESULT_RE.search(title_blob):
        return False
    if _SOURCE_PACK_FULL_YEAR_RESULT_DISCLOSURE_RE.search(title_blob):
        return True
    if not _has_source_pack_financial_result_context(row):
        return False
    return bool(_SOURCE_PACK_CONTEXTUAL_ANNUAL_RESULT_DISCLOSURE_RE.search(title_blob))


def _is_quarterly_or_earnings_source_pack_disclosure(row: ExchangeDisclosure) -> bool:
    if _is_esg_source_pack_disclosure(row):
        return False
    blob = _source_pack_disclosure_blob(row)
    title_blob = " ".join(str(value or "") for value in (row.title, row.headline))
    if _SOURCE_PACK_REPORT_WRAPPER_RE.search(title_blob):
        return False
    if _SOURCE_PACK_ANNUAL_REPORT_RE.search(blob):
        return False
    return bool(row.is_earnings_related or _SOURCE_PACK_FINANCIAL_RESULT_DISCLOSURE_RE.search(blob))


def _is_annual_source_pack_artifact(row: IRArtifact) -> bool:
    if _is_rejected_source_pack_artifact(row):
        return False
    title_blob = str(row.title or "")
    if _SOURCE_PACK_REPORT_WRAPPER_RE.search(title_blob):
        return False
    blob = " ".join(str(value or "") for value in (row.title, row.artifact_type, row.artifact_subtype, row.canonical_url, row.download_url))
    return bool(_SOURCE_PACK_ANNUAL_REPORT_RE.search(blob)) and not bool(_SOURCE_PACK_ESG_RE.search(blob))


_SOURCE_PACK_CALL_MATERIAL_RE = re.compile(
    r"\b("
    r"earnings\s+(?:call|presentation|briefing|webcast|audio)"
    r"|earnings\s+(?:release\s+)?conference"
    r"|financial\s+(?:results?\s+)?(?:briefing|presentation|webcast|audio)"
    r"|results?\s+(?:briefing|presentation|webcast|audio)"
    r"|presentation\s*[-:]?\s*results?"
    r"|results?\s*[-:]?\s*presentation"
    r"|q\s*&\s*a|q&a|qa\s+summary|questions?\s+and\s+answers?"
    r"|conference\s+call|analyst\s+(?:meeting|briefing)"
    r")\b"
    r"|法人(?:說明|说明)會|法說會|法说会|業績(?:發布|發佈)?會|业绩(?:发布)?会|簡報|简报|影音|音訊|音频",
    re.IGNORECASE,
)


def _is_earnings_call_material_artifact(row: IRArtifact | None) -> bool:
    if row is None:
        return False
    if _is_rejected_source_pack_artifact(row):
        return False
    artifact_type = str(row.artifact_type or "").strip().lower()
    subtype = str(row.artifact_subtype or "").strip().lower()
    title = str(row.title or "").strip()
    url = str(row.canonical_url or row.download_url or row.webcast_url or row.audio_url or "").strip()
    blob = " ".join([title, url]).lower()
    if artifact_type in {"transcript", "webcast", "audio"}:
        return True
    if subtype in {"call", "earnings_call", "earnings_webcast", "earnings_audio"}:
        return True
    if artifact_type in {"presentation", "fact_sheet"} and subtype == "earnings":
        return True
    if artifact_type in {"document", "exchange_disclosure", "presentation", "fact_sheet", "press_release"} and _SOURCE_PACK_CALL_MATERIAL_RE.search(blob):
        return True
    return False


def _filing_type_upper(row: SecuritiesFiling) -> str:
    return str(row.filing_type or "").strip().upper()


def _is_annual_source_pack_filing(row: SecuritiesFiling) -> bool:
    filing_type = _filing_type_upper(row)
    return (
        filing_type in {"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A", "ANNUAL", "ANNUAL_REPORT", "ESEF"}
        or filing_type == "DART_11011"
        or filing_type in {"EDINETDB_FINANCIALS", "EDINETDB_TEXT_BLOCKS", "EDINET_FILING", "EDINET_030000"}
        or filing_type in {"TW_MOPS_ANNUAL_REPORT", "TW_MOPS_ANNUAL_FINANCIALS"}
    )


def _is_quarterly_or_earnings_source_pack_filing(row: SecuritiesFiling) -> bool:
    filing_type = _filing_type_upper(row)
    return (
        bool(row.is_earnings_release)
        or filing_type in {"10-Q", "10-Q/A", "6-K", "6-K/A", "QUARTERLY", "RESULTS"}
        or filing_type
        in {
            "DART_11012",
            "DART_11013",
            "DART_11014",
            "EDINETDB_EARNINGS",
            "EDINETDB_FINANCIALS",
            "EDINET_043000",
            "EDINET_050000",
        }
        or filing_type in {"TW_MOPS_QUARTERLY_FINANCIALS", "TW_MOPS_ANNUAL_FINANCIALS"}
    )


def _is_campaign_blocking_gap(gap_kind: str | None, *, assigned_rail: str | None = "reports") -> bool:
    kind = str(gap_kind or "").strip()
    rail = str(assigned_rail or "").strip().lower()
    if rail == "reports" and kind in REPORT_RAIL_NONBLOCKING_GAP_KINDS:
        return False
    return True


def evaluate_source_pack(session: Session, *, subject: ResearchSubject) -> dict[str, Any]:
    company, issuer = _subject_company_and_issuer(session, subject)
    public_instruments = _subject_public_instruments(session, subject)
    not_required = _source_pack_not_required_for_subject(subject, public_instruments)
    if not_required:
        return {
            "status": "not_required",
            "checklist": [],
            "items": {},
            "missing_items": [],
            "official_links": [],
            "source_counts": {},
        }

    filings = session.query(SecuritiesFiling).filter_by(issuer_id=int(issuer.id)).all() if issuer is not None else []
    disclosures = session.query(ExchangeDisclosure).filter_by(issuer_id=int(issuer.id)).all() if issuer is not None else []
    artifacts = session.query(IRArtifact).filter_by(issuer_id=int(issuer.id)).all() if issuer is not None else []
    earnings_events = session.query(EarningsEvent).filter_by(issuer_id=int(issuer.id)).all() if issuer is not None else []
    annual_filings = [row for row in filings if _is_annual_source_pack_filing(row)]
    annual_disclosures = [row for row in disclosures if _is_annual_source_pack_disclosure(row)]
    annual_artifacts = [row for row in artifacts if _is_annual_source_pack_artifact(row)]
    quarterly_filings = [
        row
        for row in filings
        if _is_quarterly_or_earnings_source_pack_filing(row)
    ]
    earnings_disclosures = [row for row in disclosures if _is_quarterly_or_earnings_source_pack_disclosure(row)]
    transcript_artifacts = [row for row in artifacts if _is_earnings_call_material_artifact(row)]
    presentation_artifacts = [
        row for row in artifacts if str(row.artifact_type or "").lower() in {"presentation", "fact_sheet"}
    ]
    earnings_artifacts = [
        row
        for row in artifacts
        if _is_earnings_result_artifact(row)
    ]
    official_source_latest = _latest_row_payload_from_specs(
        [
            (filings, "filed_at", "title", ("primary_document_url", "filing_url", "index_url")),
            (disclosures, "announced_at", "title", ("disclosure_url",)),
        ]
    )
    def event_has_call_material(event: EarningsEvent) -> bool:
        for artifact_id in (event.transcript_artifact_id, event.webcast_artifact_id, event.presentation_artifact_id):
            if artifact_id is None:
                continue
            artifact = session.get(IRArtifact, int(artifact_id))
            if _is_earnings_call_material_artifact(artifact):
                return True
        return False

    items: dict[str, Any] = {
        "ir_website": {"present": bool((issuer and issuer.ir_website) or (company and company.ir_url)), "url": (issuer.ir_website if issuer else None) or (company.ir_url if company else None)},
        "filings_source": {
            "present": bool(filings or disclosures or (company and company.filings_url)),
            "url": company.filings_url if company else None,
            "latest": official_source_latest,
        },
        "latest_annual_report": {
            "present": bool(annual_filings or annual_disclosures or annual_artifacts),
            "latest": _latest_row_payload_from_specs(
                [
                    (annual_filings, "filed_at", "title", ("primary_document_url", "filing_url", "index_url")),
                    (annual_disclosures, "announced_at", "title", ("disclosure_url",)),
                    (annual_artifacts, "published_at", "title", ("canonical_url", "download_url")),
                ]
            ),
        },
        "latest_quarterly_or_earnings_release": {
            "present": bool(quarterly_filings or earnings_disclosures or earnings_artifacts),
            "latest": _latest_row_payload_from_specs(
                [
                    (quarterly_filings, "filed_at", "title", ("primary_document_url", "filing_url", "index_url")),
                    (earnings_disclosures, "announced_at", "title", ("disclosure_url",)),
                    (earnings_artifacts, "published_at", "title", ("canonical_url", "download_url", "webcast_url")),
                ]
            ),
        },
        "earnings_transcript_or_call_material": {
            "present": bool(transcript_artifacts or any(event_has_call_material(event) for event in earnings_events)),
            "latest": _latest_row_payload(transcript_artifacts, date_attr="published_at", title_attr="title", url_attrs=("canonical_url", "download_url", "webcast_url")),
        },
        "investor_presentation": {
            "present": bool(presentation_artifacts or (company and company.reports_url)),
            "latest": _latest_row_payload(presentation_artifacts, date_attr="published_at", title_attr="title", url_attrs=("canonical_url", "download_url")),
            "url": company.reports_url if company else None,
        },
        "product_pages": {"present": bool(issuer and issuer.website), "url": issuer.website if issuer else None},
        "official_press_news_page": {"present": bool((issuer and issuer.ir_website) or (issuer and issuer.website)), "url": (issuer.ir_website or issuer.website) if issuer else None},
        "regulatory_or_exchange_disclosures": {"present": bool(filings or disclosures), "count": len(filings) + len(disclosures)},
    }
    checklist = [
        {
            "key": key,
            "label": key.replace("_", " ").title(),
            "present": bool(_json_dict(items.get(key)).get("present")),
        }
        for key in SOURCE_PACK_REQUIRED_ITEMS
    ]
    missing_items = [row["key"] for row in checklist if not row["present"]]
    links = []
    for key, payload in items.items():
        if not isinstance(payload, dict):
            continue
        latest = payload.get("latest") if isinstance(payload.get("latest"), dict) else {}
        url = payload.get("url") or latest.get("url")
        if url:
            links.append({"item": key, "url": url})
    source_counts = {
        "filings": len(filings),
        "exchange_disclosures": len(disclosures),
        "ir_artifacts": len(artifacts),
        "earnings_events": len(earnings_events),
        "public_instruments": len(public_instruments),
    }
    return {
        "status": _source_pack_status(missing_items, not_required=False),
        "checklist": checklist,
        "items": items,
        "missing_items": missing_items,
        "official_links": links,
        "source_counts": source_counts,
    }


def upsert_source_pack(session: Session, *, subject_id: int, audit: dict[str, Any] | None = None) -> ResearchSourcePack:
    subject = session.get(ResearchSubject, int(subject_id))
    if subject is None:
        raise ValueError("Research subject not found.")
    evaluated = evaluate_source_pack(session, subject=subject)
    row = session.query(ResearchSourcePack).filter_by(subject_id=int(subject.id)).first()
    now = utcnow()
    if row is None:
        row = ResearchSourcePack(
            subject_id=int(subject.id),
            status=evaluated["status"],
            checklist_json=list(evaluated["checklist"]),
            items_json=dict(evaluated["items"]),
            missing_items_json=list(evaluated["missing_items"]),
            official_links_json=list(evaluated["official_links"]),
            source_counts_json=dict(evaluated["source_counts"]),
            audit_json=dict(audit or {}),
            reviewed_at=now,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    else:
        row.status = evaluated["status"]
        row.checklist_json = list(evaluated["checklist"])
        row.items_json = dict(evaluated["items"])
        row.missing_items_json = list(evaluated["missing_items"])
        row.official_links_json = list(evaluated["official_links"])
        row.source_counts_json = dict(evaluated["source_counts"])
        row.reviewed_at = now
        if audit:
            merged = _json_dict(row.audit_json)
            merged.update(dict(audit))
            row.audit_json = merged
        row.updated_at = now
    session.flush()
    return row


def _source_pack_discovery_paths(subject: ResearchSubject, item_key: str) -> list[str]:
    jurisdiction = ""
    if subject.issuer_id is not None:
        issuer = subject.issuer
        jurisdiction = str(getattr(issuer, "jurisdiction", "") or "").upper()
    exchange = ""
    if subject.company_id is not None:
        company = subject.company
        exchange = str(getattr(company, "exchange", "") or "").upper()
    api_paths = {
        "US": ["SEC EDGAR submissions/companyfacts APIs", "SEC current-report filings API"],
        "JP": ["EDINETDB/API securities reports", "TDnet timely-disclosure feed"],
        "KR": ["OpenDART filings API", "KRX/KIND disclosures feed"],
        "HK": ["HKEXnews issuer filings feed"],
        "CN": ["SSE/SZSE/BSE official disclosure portals"],
        "TW": ["TWSE/MOPS filings and material-information feed"],
        "CA": ["SEDAR+ public company-filings search", "SEC EDGAR 40-F/6-K rail when dual-listed"],
        "AU": ["ASX company-announcements feed", "ASIC/company registry material where required"],
        "ASX": ["ASX company-announcements feed"],
        "SG": ["SGXNet/company-announcements feed"],
        "SGX": ["SGXNet/company-announcements feed"],
        "IN": ["NSE corporate-filings announcements API", "BSE corporate announcements where NSE is not the primary listing"],
        "NSE": ["NSE corporate-filings announcements API"],
        "EU_UK": [
            "GLEIF LEI resolution",
            "filings.xbrl.org ESEF/UKSEF filing API",
            "UK FCA NSM or national OAM disclosure search when ESEF index is incomplete",
        ],
    }
    eu_exchange = exchange in {"IBIS", "XETRA", "FWB", "FWB2", "FRA", "FRANKFURT", "SBF", "LSE", "SIX", "AMS", "MIL", "BVME", "EBS", "EURONEXT", "PARIS"}
    official_paths = (
        api_paths.get(jurisdiction)
        or api_paths.get(exchange)
        or (api_paths["EU_UK"] if jurisdiction in {"NL", "DE", "FR", "IT", "ES", "GB", "UK", "CH", "SE", "FI"} or eu_exchange else None)
        or ["official regulator/exchange filings API or feed"]
    )
    issuer_paths = ["issuer IR library after official/API period material exists"]
    if item_key == "ir_website":
        base = ["issuer/company registry metadata", "official company website or exchange issuer profile"]
    elif item_key in SOURCE_PACK_API_FIRST_ITEMS:
        base = list(official_paths) + list(issuer_paths)
    elif item_key in SOURCE_PACK_TRANSCRIPT_GATED_ITEMS:
        base = [f"{path} gated earnings-event/material record" for path in official_paths] + [
            "issuer-specific official transcript or call-material adapter after the earnings-material gate",
            *issuer_paths,
        ]
    else:
        base = ["official issuer IR/library page", "official company website", "exchange issuer profile"]
    suffix = {
        "ir_website": "find official investor-relations homepage",
        "filings_source": "resolve durable API/feed-backed regulator or exchange filing source",
        "latest_annual_report": "import latest annual report through official API/feed first",
        "latest_quarterly_or_earnings_release": "import latest quarterly results or earnings release through official API/feed first",
        "earnings_transcript_or_call_material": "wait for official earnings material, then import transcript, webcast, audio, or call deck",
        "investor_presentation": "import latest investor presentation",
        "product_pages": "capture official product/business pages",
        "official_press_news_page": "capture official press/news page",
        "regulatory_or_exchange_disclosures": "import recent regulatory/exchange disclosures through official API/feed first",
    }.get(item_key, f"find {item_key}")
    return [f"{path}: {suffix}" for path in base]


def _source_pack_request_policy(subject: ResearchSubject, item_key: str) -> dict[str, Any]:
    jurisdiction = ""
    if subject.issuer_id is not None and subject.issuer is not None:
        jurisdiction = str(getattr(subject.issuer, "jurisdiction", "") or "").upper()
    exchange = ""
    if subject.company_id is not None and subject.company is not None:
        exchange = str(getattr(subject.company, "exchange", "") or "").upper()
    is_transcript = item_key in SOURCE_PACK_TRANSCRIPT_GATED_ITEMS
    is_api_first = item_key in SOURCE_PACK_API_FIRST_ITEMS or is_transcript
    eu_exchange = exchange in {"IBIS", "XETRA", "FWB", "FWB2", "FRA", "FRANKFURT", "SBF", "LSE", "SIX", "AMS", "MIL", "BVME", "EBS", "EURONEXT", "PARIS"}
    jurisdiction_family = (
        "EU_UK"
        if jurisdiction in {"NL", "DE", "FR", "IT", "ES", "GB", "UK", "CH", "SE", "FI"} or eu_exchange
        else "AU"
        if jurisdiction in {"AU", "AUS"} or exchange == "ASX"
        else "SG"
        if jurisdiction in {"SG", "SGP", "SINGAPORE"} or exchange == "SGX"
        else "IN"
        if jurisdiction in {"IN", "INDIA"} or exchange in {"NSE", "NSEI"} or str(getattr(subject.company, "ticker", "") if subject.company is not None else "").upper().endswith(".NS")
        else jurisdiction or None
    )
    preferred_source_order = [
        "durable_issuer_identifier_resolution",
        "official_regulator_or_exchange_api",
        "official_regulator_or_exchange_feed",
        "issuer_ir_page_after_official_material",
        "operator_uploaded_official_material",
    ]
    if is_transcript:
        preferred_source_order = [
            "completed_earnings_event_with_official_release_material",
            "local_ir_artifact_or_company_document",
            "issuer_specific_official_transcript_adapter",
            "issuer_official_call_material_page_after_gate",
            "derived_from_official_webcast_or_audio_when_available",
            "operator_uploaded_official_transcript_or_audio",
            "non_official_backup_provider_after_gate",
        ]
    return {
        "api_first": bool(is_api_first),
        "jurisdiction": jurisdiction or None,
        "jurisdiction_family": jurisdiction_family,
        "exchange": exchange or None,
        "preferred_source_order": preferred_source_order,
        "challenge_page_policy": "avoid challenge-prone issuer pages until an official API/feed or imported official material establishes the period; do not make issuer IR the primary earnings-filing rail when a regulator/exchange/API rail exists",
        "transcript_gate": {
            "enabled": bool(is_transcript),
            "required_precondition": "completed EarningsEvent with release_artifact_id, filing_id, or webcast/call material from official/API source",
            "do_not_search_generic_transcript_sources_before_gate": bool(is_transcript),
            "official_first_after_gate": [
                "local_ir_artifact_or_company_document",
                "issuer_specific_official_transcript_adapter",
                "issuer_official_call_material_page_after_gate",
                "derived_from_official_webcast_or_audio_when_available",
                "operator_uploaded_official_transcript_or_audio",
            ],
            "blocked_providers": ["quartr_api", "marketscreener_scrape"],
            "fallback_providers_after_gate": ["stockanalysis_live", "motley_fool_live"],
        },
    }


def discover_source_pack_items(
    session: Session,
    *,
    subject_id: int,
    create_requests: bool = True,
    audit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate a source pack and convert missing items into stable actionable requests."""
    subject = session.get(ResearchSubject, int(subject_id))
    if subject is None:
        raise ValueError("Research subject not found.")
    pack = upsert_source_pack(session, subject_id=int(subject.id), audit={"source": "source_pack_discovery", **dict(audit or {})})
    missing = [str(item) for item in (pack.missing_items_json or []) if str(item)]
    requests: list[dict[str, Any]] = []
    resolved_requests: list[dict[str, Any]] = []
    for item_key in missing:
        suggested_paths = _source_pack_discovery_paths(subject, item_key)
        request_policy = _source_pack_request_policy(subject, item_key)
        transcript_gate = _json_dict(request_policy.get("transcript_gate"))
        if transcript_gate.get("enabled"):
            summary = (
                f"Subject source pack is missing `{item_key}`. Do not probe generic transcript providers or challenge-prone "
                "issuer pages until an official/API earnings material record exists for the period."
            )
        else:
            summary = (
                f"Subject source pack is missing `{item_key}`. Resolve through official regulator/exchange API or feed first; "
                "use issuer IR pages only as a fallback or for official material already identified by the API rail."
            )
        request_key = f"source-pack:{int(subject.id)}:{item_key}"
        existing = session.query(InstrumentRequest).filter_by(request_key=request_key).first()
        row = None
        if create_requests:
            row = create_instrument_request(
                session,
                request_key=request_key,
                title=f"Find official source-pack item: {subject.display_name} - {item_key}",
                intent="official_source_pack_item",
                summary=summary,
                target_entities=[subject.display_name],
                target_themes=["official_source_pack", item_key],
                desired_outputs=[item_key, "source_url", "source_kind", "import_target", "api_or_feed_path"],
                evidence_bundle=[{"subject_id": int(subject.id), "source_pack_id": int(pack.id), "missing_item": item_key}],
                provenance_summary="Created by research canon source-pack discovery.",
                evidence_tier="system_gap",
                status="draft",
                priority=8,
                automation_source="research_canon",
                source_kind="official_source_pack",
                draft_payload={
                    "subject_id": int(subject.id),
                    "subject_key": subject.subject_key,
                    "missing_item": item_key,
                    "suggested_source_paths": suggested_paths,
                    "source_policy": request_policy,
                    "accepted_destinations": ["issuer", "company", "securities_filings", "exchange_disclosures", "ir_artifacts", "documents"],
                },
                linked_objects={"subject_id": int(subject.id), "source_pack_id": int(pack.id), "missing_item": item_key},
            )
        requests.append(
            {
                "missing_item": item_key,
                "request_id": int(row.id) if row is not None else int(existing.id) if existing is not None else None,
                "request_key": request_key,
                "created": bool(create_requests and existing is None and row is not None),
                "suggested_source_paths": suggested_paths,
                "source_policy": request_policy,
            }
        )
    if create_requests:
        now = utcnow()
        for item_key in SOURCE_PACK_REQUIRED_ITEMS:
            if item_key in missing:
                continue
            existing = session.query(InstrumentRequest).filter_by(request_key=f"source-pack:{int(subject.id)}:{item_key}").first()
            if existing is None or existing.status not in {"draft", "review"}:
                continue
            existing.status = "live"
            existing.review_note = "Resolved by source-pack refresh; the item is now present."
            live_payload = dict(existing.live_payload or {})
            live_payload.update(
                {
                    "resolved_by_source_pack_refresh": True,
                    "subject_id": int(subject.id),
                    "source_pack_id": int(pack.id),
                    "missing_item": item_key,
                    "source_pack_item": _json_dict(pack.items_json).get(item_key),
                }
            )
            existing.live_payload = live_payload
            existing.reviewed_at = now
            existing.updated_at = now
            resolved_requests.append(
                {
                    "missing_item": item_key,
                    "request_id": int(existing.id),
                    "request_key": existing.request_key,
                    "status": existing.status,
                }
            )
    session.flush()
    return {
        "subject_id": int(subject.id),
        "source_pack": serialize_source_pack(pack),
        "missing_items": missing,
        "requests": requests,
        "request_count": len(requests),
        "resolved_requests": resolved_requests,
        "resolved_request_count": len(resolved_requests),
    }


def discover_active_universe_source_pack_items(
    session: Session,
    *,
    universe_slug: str | None = None,
    create_requests: bool = True,
    create_gap_requests: bool = True,
    create_cards: bool = False,
    stale_after_days: int = 120,
    limit: int | None = None,
    materialize_official_documents: bool = True,
    register_official_sources: bool = True,
    run_coverage_scan: bool = True,
    audit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Refresh active-universe source packs and create API-first missing-item requests."""
    rows = _active_universe_rows(session, universe_slug=universe_slug)
    if limit is not None:
        rows = rows[: max(1, int(limit))]
    official_materialization: dict[str, Any] = {}
    if materialize_official_documents:
        from thoth_terminal.foundation_service import bootstrap_company_rails_for_company_ids
        from thoth_terminal.investor_data_service import materialize_official_investor_documents

        active_company_ids = {
            int(row.instrument.company_id)
            for row in rows
            if row.instrument is not None and row.instrument.company_id is not None
        }
        official_materialization["investor_documents"] = materialize_official_investor_documents(session, company_ids=active_company_ids)
        official_materialization["company_rails"] = bootstrap_company_rails_for_company_ids(session, company_ids=active_company_ids)
    official_source_registration: dict[str, Any] = {}
    if register_official_sources:
        from thoth_terminal.investor_data_service import (
            ensure_active_universe_asx_sources,
            ensure_active_universe_bursa_sources,
            ensure_active_universe_canada_sources,
            ensure_active_universe_china_sources,
            ensure_active_universe_esef_sources,
            ensure_active_universe_hong_kong_sources,
            ensure_active_universe_japan_sources,
            ensure_active_universe_kazakhstan_sources,
            ensure_active_universe_nse_sources,
            ensure_active_universe_opendart_sources,
            ensure_active_universe_sec_sources,
            ensure_active_universe_sgx_sources,
            ensure_active_universe_taiwan_sources,
        )

        official_source_registration["sec"] = ensure_active_universe_sec_sources(
            session,
            universe_slug=universe_slug,
            limit=limit,
            reason="active_universe_source_pack_discovery",
        )
        official_source_registration["canada"] = ensure_active_universe_canada_sources(
            session,
            universe_slug=universe_slug,
            limit=limit,
            reason="active_universe_source_pack_discovery",
        )
        official_source_registration["asx"] = ensure_active_universe_asx_sources(
            session,
            universe_slug=universe_slug,
            limit=limit,
            reason="active_universe_source_pack_discovery",
        )
        official_source_registration["bursa"] = ensure_active_universe_bursa_sources(
            session,
            universe_slug=universe_slug,
            limit=limit,
            reason="active_universe_source_pack_discovery",
        )
        official_source_registration["kazakhstan"] = ensure_active_universe_kazakhstan_sources(
            session,
            universe_slug=universe_slug,
            limit=limit,
            reason="active_universe_source_pack_discovery",
        )
        official_source_registration["sgx"] = ensure_active_universe_sgx_sources(
            session,
            universe_slug=universe_slug,
            limit=limit,
            reason="active_universe_source_pack_discovery",
        )
        official_source_registration["nse"] = ensure_active_universe_nse_sources(
            session,
            universe_slug=universe_slug,
            limit=limit,
            reason="active_universe_source_pack_discovery",
        )
        official_source_registration["opendart"] = ensure_active_universe_opendart_sources(
            session,
            universe_slug=universe_slug,
            limit=limit,
            reason="active_universe_source_pack_discovery",
        )
        official_source_registration["japan"] = ensure_active_universe_japan_sources(
            session,
            universe_slug=universe_slug,
            limit=limit,
            reason="active_universe_source_pack_discovery",
        )
        official_source_registration["taiwan"] = ensure_active_universe_taiwan_sources(
            session,
            universe_slug=universe_slug,
            limit=limit,
            reason="active_universe_source_pack_discovery",
        )
        official_source_registration["hkex"] = ensure_active_universe_hong_kong_sources(
            session,
            universe_slug=universe_slug,
            limit=limit,
            reason="active_universe_source_pack_discovery",
        )
        official_source_registration["china"] = ensure_active_universe_china_sources(
            session,
            universe_slug=universe_slug,
            limit=limit,
            reason="active_universe_source_pack_discovery",
        )
        official_source_registration["esef"] = ensure_active_universe_esef_sources(
            session,
            universe_slug=universe_slug,
            limit=limit,
            reason="active_universe_source_pack_discovery",
        )
    coverage = (
        scan_active_universe_coverage(
            session,
            universe_slug=universe_slug,
            ensure_subjects=True,
            create_requests=create_gap_requests,
            create_cards=create_cards,
            stale_after_days=stale_after_days,
            limit=limit,
        )
        if run_coverage_scan
        else {"skipped": True, "reason": "source_pack_refresh_only"}
    )
    subject_entries = active_universe_subjects(session, universe_slug=universe_slug, ensure=True, rows=rows)
    source_pack_counts: Counter[str] = Counter()
    missing_item_counts: Counter[str] = Counter()
    created_count = 0
    request_count = 0
    resolved_request_count = 0
    gated_transcript_request_count = 0
    api_first_request_count = 0
    examples: list[dict[str, Any]] = []
    audit_payload = {"source": "active_universe_source_pack_discovery", "api_first": True, **dict(audit or {})}
    for subject_id in sorted(subject_entries):
        result = discover_source_pack_items(
            session,
            subject_id=int(subject_id),
            create_requests=create_requests,
            audit=audit_payload,
        )
        pack_payload = _json_dict(result.get("source_pack"))
        status = str(pack_payload.get("status") or "missing")
        source_pack_counts[status] += 1
        for item in result.get("missing_items") or []:
            missing_item_counts[str(item)] += 1
        for request in result.get("requests") or []:
            if not isinstance(request, dict):
                continue
            request_count += 1
            if bool(request.get("created")):
                created_count += 1
            policy = _json_dict(request.get("source_policy"))
            if bool(policy.get("api_first")):
                api_first_request_count += 1
            transcript_gate = _json_dict(policy.get("transcript_gate"))
            if bool(transcript_gate.get("enabled")):
                gated_transcript_request_count += 1
            if len(examples) < 8:
                examples.append(
                    {
                        "subject_id": int(subject_id),
                        "subject": subject_entries[subject_id]["subject"].display_name,
                        "missing_item": request.get("missing_item"),
                        "request_key": request.get("request_key"),
                        "created": bool(request.get("created")),
                    }
                )
        for request in result.get("resolved_requests") or []:
            if isinstance(request, dict):
                resolved_request_count += 1
    session.flush()
    return {
        "ok": True,
        "universe_slug": universe_slug,
        "rows_scanned": len(rows),
        "subjects_scanned": len(subject_entries),
        "source_pack_counts": dict(source_pack_counts),
        "missing_item_counts": dict(missing_item_counts),
        "source_pack_item_request_count": int(request_count),
        "source_pack_item_requests_created": int(created_count),
        "source_pack_item_requests_resolved": int(resolved_request_count),
        "api_first_request_count": int(api_first_request_count),
        "gated_transcript_request_count": int(gated_transcript_request_count),
        "coverage_scan": coverage,
        "official_materialization": official_materialization,
        "official_source_registration": official_source_registration,
        "examples": examples,
    }


def _source_tracking_counts(config: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for key in (
        "ciks",
        "corp_codes",
        "tracked_queries",
        "tracked_edinet_codes",
        "company_codes",
        "stock_codes",
        "stock_ids",
        "stock_query_ids",
        "symbols",
        "nse_symbols",
        "ca_stock_codes",
        "leis",
        "countries",
        "stats_company_codes",
        "document_company_codes",
        "include_keywords",
        "company_names",
    ):
        value = config.get(key)
        if isinstance(value, list):
            counts[key] = len([item for item in value if str(item or "").strip()])
    for key in (
        "company_id_by_cik",
        "company_id_by_corp_code",
        "company_id_by_stock_code",
        "stock_query_id_by_stock_code",
        "stock_code_by_stock_query_id",
        "company_id_by_symbol",
        "company_id_by_ca_stock_code",
        "company_id_by_lei",
        "company_id_by_company_name",
    ):
        value = config.get(key)
        if isinstance(value, dict):
            counts[key] = len(value)
    return counts


def _official_source_state(session: Session, source_name: str) -> dict[str, Any]:
    source = session.query(Source).filter_by(name=source_name).first()
    if source is None:
        return {"source_name": source_name, "present": False}
    state = session.query(SourceState).filter_by(source_id=int(source.id)).first()
    run = session.query(IngestionRun).filter_by(source_id=int(source.id)).order_by(IngestionRun.id.desc()).first()
    config = _json_dict(source.config)
    now = utcnow()
    payload: dict[str, Any] = {
        "source_name": source.name,
        "source_id": int(source.id),
        "present": True,
        "category": source.category,
        "connector": config.get("connector"),
        "enabled": bool(config.get("enabled", True)),
        "managed_by": config.get("managed_by"),
        "coverage_scope": config.get("coverage_scope"),
        "tracking_counts": _source_tracking_counts(config),
        "last_success_at": _dt(state.last_success_at) if state is not None else None,
        "consecutive_failures": int(state.consecutive_failures) if state is not None else None,
        "backoff_until": _dt(state.backoff_until) if state is not None else None,
    }
    if state is not None and state.backoff_until is not None and state.backoff_until > now:
        payload["health_status"] = "backoff"
    elif state is not None and int(state.consecutive_failures or 0) > 0:
        payload["health_status"] = "degraded"
    else:
        payload["health_status"] = "ok"
    if run is not None:
        run_extra = _json_dict(run.extra)
        connector_stats = _json_dict(run_extra.get("connector_stats"))
        connector_stats_summary = {
            key: connector_stats[key]
            for key in (
                "source_kind",
                "rate_limited",
                "cursor_advanced",
                "tracked_selected",
                "market_selected",
                "company_count",
                "count",
                "retry_after_seconds",
                "checked_dates",
                "selected_company_names",
                "selected_stock_query_ids",
                "selected_stock_codes",
                "per_stock_limit",
                "date_offset",
            )
            if key in connector_stats
        }
        payload["latest_run"] = {
            "id": int(run.id),
            "status": run.status,
            "finished_at": _dt(run.finished_at),
            "items_seen": int(run.items_seen or 0),
            "items_new": int(run.items_new or 0),
            "items_updated": int(run.items_updated or 0),
            "error": _short(run.error, 240) if run.error else None,
            "connector_stats": connector_stats_summary,
        }
        if connector_stats.get("rate_limited"):
            payload["health_status"] = "rate_limited"
    return payload


def _issuer_identifier_values_for_audit(session: Session, issuer_id: int | None, id_type: str) -> list[str]:
    if issuer_id is None:
        return []
    return [
        str(row.id_value or "").strip()
        for row in session.query(IssuerIdentifier)
        .filter_by(issuer_id=int(issuer_id), id_type=str(id_type).strip().upper())
        .order_by(IssuerIdentifier.is_primary.desc(), IssuerIdentifier.id.asc())
        .all()
        if str(row.id_value or "").strip()
    ]


def _normalize_audit_lei(value: object) -> str | None:
    raw = str(value or "").strip().upper()
    if len(raw) == 20 and raw.isalnum():
        return raw
    return None


def _issuer_leis_for_audit(session: Session, issuer_id: int | None) -> list[str]:
    if issuer_id is None:
        return []
    issuer = session.get(Issuer, int(issuer_id))
    seen: set[str] = set()
    out: list[str] = []
    for raw in [
        getattr(issuer, "lei", None) if issuer is not None else None,
        *_issuer_identifier_values_for_audit(session, issuer_id, "LEI"),
    ]:
        lei = _normalize_audit_lei(raw)
        if lei and lei not in seen:
            seen.add(lei)
            out.append(lei)
    return out


def _normalize_audit_sec_cik(value: object) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    digits = re.sub(r"\D+", "", raw)
    if 1 <= len(digits) <= 10:
        return digits.zfill(10)
    return None


def _normalize_audit_exchange_stock(value: object, *, suffixes: tuple[str, ...], pattern: str) -> str | None:
    raw = str(value or "").strip().upper()
    if not raw:
        return None
    if suffixes:
        raw = re.sub(rf"\.({'|'.join(re.escape(suffix) for suffix in suffixes)})$", "", raw)
    raw = re.sub(r"[^0-9A-Z]+", "", raw)
    return raw if re.fullmatch(pattern, raw) else None


def _normalize_audit_hk_stock(value: object) -> str | None:
    raw = str(value or "").strip().upper()
    if not raw:
        return None
    raw = re.sub(r"\.HK$", "", raw)
    digits = re.sub(r"\D+", "", raw)
    if 1 <= len(digits) <= 5:
        return digits.zfill(5)
    return None


def _normalize_audit_cn_stock(value: object) -> str | None:
    raw = str(value or "").strip().upper()
    if not raw:
        return None
    raw = re.sub(r"\.(SS|SH|SZ|CN)$", "", raw)
    digits = re.sub(r"\D+", "", raw)
    return digits if re.fullmatch(r"[0-9]{6}", digits) else None


def _normalize_audit_ca_stock(value: object) -> str | None:
    raw = str(value or "").strip().upper()
    if not raw:
        return None
    if re.search(r"\.(T|JP|TW|TWO|TPE|HK|SS|SH|SZ|KS|KQ|L|DE|F|PA|MI|SW)$", raw):
        return None
    raw = re.sub(r"\.(TO|TSX|V|TSXV|CN|NE|CA)$", "", raw)
    raw = re.sub(r"[^0-9A-Z.]+", "", raw)
    return raw if re.fullmatch(r"[A-Z0-9][A-Z0-9.]{0,14}", raw) else None


def _normalize_audit_asx_stock(value: object) -> str | None:
    raw = str(value or "").strip().upper()
    if not raw:
        return None
    raw = re.sub(r"\.AX$", "", raw)
    raw = re.sub(r"[^0-9A-Z]+", "", raw)
    return raw if re.fullmatch(r"[0-9A-Z]{2,6}", raw) else None


def _normalize_audit_bursa_stock(value: object) -> str | None:
    raw = str(value or "").strip().upper()
    if not raw:
        return None
    raw = re.sub(r"\.(KL|KLS|MY)$", "", raw)
    raw = re.sub(r"^BURSA[:\s]+", "", raw)
    raw = re.sub(r"[^0-9A-Z]+", "", raw)
    return raw if re.fullmatch(r"[0-9A-Z]{3,8}", raw) else None


def _normalize_audit_kz_stock(value: object) -> str | None:
    raw = str(value or "").strip().upper()
    if not raw:
        return None
    raw = re.sub(r"\.(KZ|KASE|AIX)$", "", raw)
    raw = re.sub(r"^(AIX|KASE)[:\s]+", "", raw)
    raw = re.sub(r"[^0-9A-Z.]+", "", raw)
    return raw if re.fullmatch(r"[0-9A-Z][0-9A-Z.]{0,9}", raw) else None


def _normalize_audit_sgx_stock(value: object) -> str | None:
    raw = str(value or "").strip().upper()
    if not raw:
        return None
    raw = re.sub(r"\.SI$", "", raw)
    raw = re.sub(r"[^0-9A-Z]+", "", raw)
    return raw if re.fullmatch(r"[0-9A-Z]{1,10}", raw) else None


def _normalize_audit_nse_symbol(value: object) -> str | None:
    raw = str(value or "").strip().upper()
    if not raw:
        return None
    raw = re.sub(r"\.(NS|NSE|IN)$", "", raw)
    raw = re.sub(r"[^0-9A-Z&-]+", "", raw)
    return raw if re.fullmatch(r"[0-9A-Z][0-9A-Z&-]{0,19}", raw) else None


def _official_source_identifier_mapping(session: Session, *, rail: str, identifier: str) -> dict[str, Any]:
    if rail == "sec":
        source_name = "sec_edgar_active_universe_filings"
        company_key = "company_id_by_cik"
        slug_key = "company_slug_by_cik"
        ticker_key = "ticker_by_cik"
    elif rail == "opendart":
        source_name = "opendart_official_filings"
        company_key = "company_id_by_corp_code"
        slug_key = "company_slug_by_corp_code"
        ticker_key = "ticker_by_corp_code"
    elif rail == "taiwan":
        source_name = "taiwan_mops_monthly_revenue"
        company_key = "company_id_by_stock_code"
        slug_key = "company_slug_by_stock_code"
        ticker_key = "ticker_by_stock_code"
    elif rail == "hkex":
        source_name = "hkexnews_active_universe_disclosures"
        company_key = "company_id_by_stock_code"
        slug_key = "company_slug_by_stock_code"
        ticker_key = "ticker_by_stock_code"
    elif rail == "china":
        source_name = "cninfo_active_universe_announcements"
        company_key = "company_id_by_stock_code"
        slug_key = "company_slug_by_stock_code"
        ticker_key = "ticker_by_stock_code"
    elif rail == "canada":
        source_name = "sedarplus_active_universe_filings"
        company_key = "company_id_by_ca_stock_code"
        slug_key = "company_slug_by_ca_stock_code"
        ticker_key = ""
    elif rail == "asx":
        source_name = "asx_active_universe_announcements"
        company_key = "company_id_by_stock_code"
        slug_key = "company_slug_by_stock_code"
        ticker_key = "ticker_by_stock_code"
    elif rail == "bursa":
        source_name = "bursa_active_universe_announcements"
        company_key = "company_id_by_stock_code"
        slug_key = "company_slug_by_stock_code"
        ticker_key = "ticker_by_stock_code"
    elif rail == "kazakhstan":
        source_name = "kazakhstan_active_universe_announcements"
        company_key = "company_id_by_stock_code"
        slug_key = "company_slug_by_stock_code"
        ticker_key = "ticker_by_stock_code"
    elif rail == "sgx":
        source_name = "sgx_active_universe_announcements"
        company_key = "company_id_by_stock_code"
        slug_key = "company_slug_by_stock_code"
        ticker_key = "ticker_by_stock_code"
    elif rail == "nse":
        source_name = "nse_active_universe_announcements"
        company_key = "company_id_by_symbol"
        slug_key = "company_slug_by_symbol"
        ticker_key = "ticker_by_symbol"
    elif rail == "esef":
        source_name = "filings_xbrl_org_active_universe_esef"
        company_key = "company_id_by_lei"
        slug_key = "company_slug_by_lei"
        ticker_key = "ticker_by_lei"
    else:
        source_name = "edinetdb_japan_company_financials"
        company_key = ""
        slug_key = ""
        ticker_key = ""
    source = session.query(Source).filter_by(name=source_name).first()
    cfg = _json_dict(source.config) if source is not None else {}
    payload: dict[str, Any] = {"source_name": source_name, "source_id": int(source.id) if source is not None else None}
    for out_key, cfg_key in [("company_id", company_key), ("company_slug", slug_key), ("ticker", ticker_key)]:
        values = cfg.get(cfg_key) if cfg_key else None
        if isinstance(values, dict) and identifier in values:
            payload[out_key] = values.get(identifier)
    return payload


def _row_official_identifier_candidates(
    session: Session,
    *,
    rail: str,
    instrument: ResearchInstrument,
    company: Company | None,
    issuer: Issuer | None,
) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(id_type: str, value: object, source: str) -> None:
        normalized: str | None
        if id_type == "CIK":
            normalized = _normalize_audit_sec_cik(value)
        elif id_type == "TW_STOCK":
            normalized = _normalize_audit_exchange_stock(value, suffixes=("TW", "TWO", "TPE"), pattern=r"[0-9]{4}[A-Z]?")
        elif id_type == "JP_STOCK":
            normalized = _normalize_audit_exchange_stock(value, suffixes=("T", "JP"), pattern=r"[0-9]{4}[A-Z]?|[0-9]{3}[A-Z]")
        elif id_type == "KR_STOCK":
            stock = _normalize_audit_exchange_stock(value, suffixes=("KS", "KQ", "KR"), pattern=r"[0-9]{1,6}")
            normalized = stock.zfill(6)[-6:] if stock else None
        elif id_type == "HK_STOCK":
            normalized = _normalize_audit_hk_stock(value)
        elif id_type == "CN_STOCK":
            normalized = _normalize_audit_cn_stock(value)
        elif id_type == "CA_STOCK":
            normalized = _normalize_audit_ca_stock(value)
        elif id_type == "ASX_STOCK":
            normalized = _normalize_audit_asx_stock(value)
        elif id_type == "MY_STOCK":
            normalized = _normalize_audit_bursa_stock(value)
        elif id_type == "KZ_STOCK":
            normalized = _normalize_audit_kz_stock(value)
        elif id_type == "SGX_STOCK":
            normalized = _normalize_audit_sgx_stock(value)
        elif id_type == "NSE_STOCK":
            normalized = _normalize_audit_nse_symbol(value)
        elif id_type == "LEI":
            normalized = _normalize_audit_lei(value)
        else:
            normalized = str(value or "").strip()
        if not normalized:
            return
        key = (id_type, normalized)
        if key in seen:
            return
        seen.add(key)
        candidates.append({"id_type": id_type, "id_value": normalized, "source": source})

    if rail == "sec":
        for value in _issuer_identifier_values_for_audit(session, int(issuer.id) if issuer is not None else None, "CIK"):
            add("CIK", value, "issuer_identifier")
        for source, value in [
            ("issuer_slug", issuer.slug if issuer is not None else None),
            ("company_slug", company.slug if company is not None else None),
        ]:
            add("CIK", value, source)
    elif rail == "taiwan":
        for value in _issuer_identifier_values_for_audit(session, int(issuer.id) if issuer is not None else None, "TW_STOCK"):
            add("TW_STOCK", value, "issuer_identifier")
        for source, value in [
            ("instrument_ticker", instrument.ticker),
            ("instrument_yahoo_symbol", instrument.yahoo_symbol),
            ("company_ticker", company.ticker if company is not None else None),
        ]:
            add("TW_STOCK", value, source)
    elif rail == "japan":
        for value in _issuer_identifier_values_for_audit(session, int(issuer.id) if issuer is not None else None, "JP_STOCK"):
            add("JP_STOCK", value, "issuer_identifier")
        for source, value in [
            ("instrument_ticker", instrument.ticker),
            ("instrument_yahoo_symbol", instrument.yahoo_symbol),
            ("company_ticker", company.ticker if company is not None else None),
        ]:
            add("JP_STOCK", value, source)
    elif rail == "opendart":
        for value in _issuer_identifier_values_for_audit(session, int(issuer.id) if issuer is not None else None, "DART_CORP"):
            add("DART_CORP", value, "issuer_identifier")
        for value in _issuer_identifier_values_for_audit(session, int(issuer.id) if issuer is not None else None, "KR_STOCK"):
            add("KR_STOCK", value, "issuer_identifier")
        for source, value in [
            ("instrument_ticker", instrument.ticker),
            ("instrument_yahoo_symbol", instrument.yahoo_symbol),
            ("company_ticker", company.ticker if company is not None else None),
        ]:
            add("KR_STOCK", value, source)
    elif rail == "hkex":
        for value in _issuer_identifier_values_for_audit(session, int(issuer.id) if issuer is not None else None, "HK_STOCK"):
            add("HK_STOCK", value, "issuer_identifier")
        for source, value in [
            ("instrument_ticker", instrument.ticker),
            ("instrument_yahoo_symbol", instrument.yahoo_symbol),
            ("company_ticker", company.ticker if company is not None else None),
        ]:
            add("HK_STOCK", value, source)
    elif rail == "china":
        for value in _issuer_identifier_values_for_audit(session, int(issuer.id) if issuer is not None else None, "CN_STOCK"):
            add("CN_STOCK", value, "issuer_identifier")
        for source, value in [
            ("instrument_ticker", instrument.ticker),
            ("instrument_yahoo_symbol", instrument.yahoo_symbol),
            ("company_ticker", company.ticker if company is not None else None),
        ]:
            add("CN_STOCK", value, source)
    elif rail == "canada":
        for value in _issuer_identifier_values_for_audit(session, int(issuer.id) if issuer is not None else None, "CA_STOCK"):
            add("CA_STOCK", value, "issuer_identifier")
        for source, value in [
            ("instrument_ticker", instrument.ticker),
            ("instrument_yahoo_symbol", instrument.yahoo_symbol),
            ("company_ticker", company.ticker if company is not None else None),
        ]:
            add("CA_STOCK", value, source)
    elif rail == "asx":
        for value in _issuer_identifier_values_for_audit(session, int(issuer.id) if issuer is not None else None, "ASX_STOCK"):
            add("ASX_STOCK", value, "issuer_identifier")
        for source, value in [
            ("instrument_ticker", instrument.ticker),
            ("instrument_yahoo_symbol", instrument.yahoo_symbol),
            ("company_ticker", company.ticker if company is not None else None),
        ]:
            add("ASX_STOCK", value, source)
    elif rail == "bursa":
        for value in _issuer_identifier_values_for_audit(session, int(issuer.id) if issuer is not None else None, "MY_STOCK"):
            add("MY_STOCK", value, "issuer_identifier")
        for source, value in [
            ("instrument_ticker", instrument.ticker),
            ("instrument_yahoo_symbol", instrument.yahoo_symbol),
            ("company_ticker", company.ticker if company is not None else None),
        ]:
            add("MY_STOCK", value, source)
    elif rail == "kazakhstan":
        for value in _issuer_identifier_values_for_audit(session, int(issuer.id) if issuer is not None else None, "KZ_STOCK"):
            add("KZ_STOCK", value, "issuer_identifier")
        for source, value in [
            ("instrument_ticker", instrument.ticker),
            ("instrument_yahoo_symbol", instrument.yahoo_symbol),
            ("company_ticker", company.ticker if company is not None else None),
        ]:
            add("KZ_STOCK", value, source)
    elif rail == "sgx":
        for value in _issuer_identifier_values_for_audit(session, int(issuer.id) if issuer is not None else None, "SGX_STOCK"):
            add("SGX_STOCK", value, "issuer_identifier")
        for source, value in [
            ("instrument_ticker", instrument.ticker),
            ("instrument_yahoo_symbol", instrument.yahoo_symbol),
            ("company_ticker", company.ticker if company is not None else None),
        ]:
            add("SGX_STOCK", value, source)
    elif rail == "nse":
        for value in _issuer_identifier_values_for_audit(session, int(issuer.id) if issuer is not None else None, "NSE_STOCK"):
            add("NSE_STOCK", value, "issuer_identifier")
        for source, value in [
            ("instrument_ticker", instrument.ticker),
            ("instrument_yahoo_symbol", instrument.yahoo_symbol),
            ("company_ticker", company.ticker if company is not None else None),
        ]:
            add("NSE_STOCK", value, source)
    elif rail == "esef":
        for value in _issuer_leis_for_audit(session, int(issuer.id) if issuer is not None else None):
            add("LEI", value, "issuer_identifier")
    return candidates


def _official_identifier_conflict_status(
    session: Session,
    *,
    rail: str,
    instrument: ResearchInstrument,
    company: Company | None,
    issuer: Issuer | None,
) -> dict[str, Any] | None:
    if rail not in {"sec", "opendart", "japan", "taiwan", "hkex", "china", "canada", "asx", "bursa", "kazakhstan", "sgx", "nse", "esef"}:
        return None
    current_issuer_id = int(issuer.id) if issuer is not None and issuer.id is not None else None
    candidates = _row_official_identifier_candidates(
        session,
        rail=rail,
        instrument=instrument,
        company=company,
        issuer=issuer,
    )
    conflicts: list[dict[str, Any]] = []
    for candidate in candidates:
        id_type = candidate["id_type"]
        id_value = candidate["id_value"]
        holders = (
            session.query(IssuerIdentifier)
            .filter_by(id_type=id_type, id_value=id_value)
            .order_by(IssuerIdentifier.is_primary.desc(), IssuerIdentifier.id.asc())
            .all()
        )
        for holder in holders:
            if current_issuer_id is not None and int(holder.issuer_id) == current_issuer_id:
                continue
            holder_issuer = session.get(Issuer, int(holder.issuer_id))
            holder_company = session.query(Company).filter_by(issuer_id=int(holder.issuer_id)).order_by(Company.id.asc()).first()
            conflicts.append(
                {
                    "id_type": id_type,
                    "id_value": id_value,
                    "candidate_source": candidate.get("source"),
                    "holder_identifier_id": int(holder.id),
                    "holder_issuer": _issuer_payload(holder_issuer),
                    "holder_company": _company_payload(holder_company),
                    "source_mapping": _official_source_identifier_mapping(session, rail=rail, identifier=id_value),
                }
            )
    if not conflicts:
        return None
    return {
        "rail": rail,
        "status": "identifier_conflict",
        "identifiers": sorted({row["id_value"] for row in conflicts}),
        "conflicts": conflicts[:8],
    }


def _issuer_object_identifier_values(issuer: Issuer | None, id_type: str) -> list[str]:
    wanted = str(id_type or "").strip().upper()
    if issuer is None or not wanted:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for identifier in getattr(issuer, "identifiers", []) or []:
        if str(identifier.id_type or "").strip().upper() != wanted:
            continue
        value = str(identifier.id_value or "").strip()
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _identity_has_identifier(issuer: Issuer | None, id_type: str) -> bool:
    return bool(_issuer_object_identifier_values(issuer, id_type))


_EU_UK_AUDIT_COUNTRIES = {
    "AT",
    "BE",
    "CH",
    "DE",
    "DK",
    "ES",
    "FI",
    "FR",
    "GB",
    "GR",
    "IE",
    "IT",
    "NL",
    "NO",
    "PT",
    "SE",
    "UK",
}
_EU_UK_AUDIT_EXCHANGES = {
    "AEB",
    "AMS",
    "ATH",
    "BVME",
    "EBS",
    "EURONEXT",
    "FRA",
    "FRANKFURT",
    "FWB",
    "FWB2",
    "IBIS",
    "LISBON",
    "LSE",
    "MIL",
    "PARIS",
    "SBF",
    "SIX",
    "VIENNA",
    "XETRA",
}
_EU_UK_AUDIT_SUFFIX_RE = re.compile(r"\.(DE|DU|F|PA|MI|L|LS|AS|BR|SW|MC|ST|HE|OL|CO|VI|IR|AT)\b")
_SEC_TICKER_LOOKUP_FALLBACK_EXCHANGES = {"MEXI", "VALUE"}


def _is_adr_like_instrument(instrument: ResearchInstrument) -> bool:
    text = " ".join(
        str(value or "")
        for value in [
            instrument.security_type,
            instrument.display_name,
            instrument.legal_name,
            instrument.ticker,
            instrument.yahoo_symbol,
        ]
    ).upper()
    return bool(re.search(r"\b(ADR|ADS|GDR|DEPOSITARY|DEPOSITORY|SPONSORED|UNSPONSORED|REGS)\b", text))


def _is_fund_like_instrument(instrument: ResearchInstrument) -> bool:
    text = " ".join(
        str(value or "")
        for value in [
            instrument.security_type,
            instrument.display_name,
            instrument.legal_name,
            instrument.exchange,
        ]
    ).upper()
    return bool(re.search(r"\b(ETF|ETN|FUND|TRUST|UCITS|BOND|TREASURY|NOTE)\b", text)) or "ETF" in text


_ESEF_PUBLIC_COMPANY_FORM_RE = re.compile(
    r"\b(AB|ABP|AG|AKTIENGESELLSCHAFT|AS|ASA|KGAA|N\.?V\.?|NV|OYJ|P\.?L\.?C\.?|PLC|S\.?A\.?|SA|SE|S\.?P\.?A\.?|SPA)\b",
    re.IGNORECASE,
)
_DEPOSITARY_RECEIPT_TERMS_RE = re.compile(
    r"\b(SPONSORED|UNSPONSORED|SPONS|SPON|UNSPON|ADR|ADS|GDR|DEPOSITARY|DEPOSITORY|REGS?)\b",
    re.IGNORECASE,
)


def _is_esef_depositary_receipt_home_candidate(
    instrument: ResearchInstrument,
    company: Company | None,
    issuer: Issuer | None,
) -> bool:
    if not _is_adr_like_instrument(instrument) or _is_fund_like_instrument(instrument):
        return False
    values = [
        getattr(issuer, "legal_name", None) if issuer is not None else None,
        getattr(issuer, "display_name", None) if issuer is not None else None,
        getattr(company, "name", None) if company is not None else None,
        getattr(instrument, "legal_name", None),
        getattr(instrument, "display_name", None),
    ]
    for value in values:
        text = _DEPOSITARY_RECEIPT_TERMS_RE.sub(" ", str(value or ""))
        text = re.sub(r"[-_/]+", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        if text and _ESEF_PUBLIC_COMPANY_FORM_RE.search(text):
            return True
    return False


def _is_sec_ticker_lookup_fallback_candidate(instrument: ResearchInstrument, company: Company | None) -> bool:
    if _is_adr_like_instrument(instrument) or _is_fund_like_instrument(instrument):
        return False
    exchange = str(instrument.exchange or (company.exchange if company else "") or "").strip().upper()
    ticker = str(instrument.ticker or (company.ticker if company else "") or instrument.yahoo_symbol or "").strip().upper()
    if not ticker or "." in ticker or not re.fullmatch(r"[A-Z][A-Z0-9-]{0,9}", ticker):
        return False
    if exchange in _SEC_TICKER_LOOKUP_FALLBACK_EXCHANGES:
        return True
    return not exchange and bool(re.fullmatch(r"[A-Z]{1,4}", ticker))


def _home_bucket_from_existing_identity(
    instrument: ResearchInstrument,
    company: Company | None,
    issuer: Issuer | None,
) -> tuple[str, str] | None:
    exchange = str(company.exchange if company is not None else "").strip().upper()
    ticker = str(company.ticker if company is not None else "").strip().upper()
    yahoo = str(instrument.yahoo_symbol or "").strip().upper()
    company_region = str(company.region if company is not None else "").strip().upper()
    issuer_jurisdiction = str(issuer.jurisdiction if issuer is not None else "").strip().upper()
    home_country = company_region or issuer_jurisdiction
    ticker_identifiers = [
        identifier
        for identifier in (getattr(issuer, "identifiers", []) or [])
        if str(identifier.id_type or "").strip().upper() == "TICKER"
    ]
    identifier_exchanges = {
        str(identifier.exchange or "").strip().upper()
        for identifier in ticker_identifiers
        if str(identifier.exchange or "").strip()
    }
    identifier_hay = " ".join(
        f"{str(identifier.id_value or '').strip().upper()} {str(identifier.exchange or '').strip().upper()}"
        for identifier in ticker_identifiers
    )
    hay = " ".join([exchange, ticker, yahoo, identifier_hay])
    japan_exchange = exchange in {"TSEJ", "JPX", "TYO", "XTKS", "TOKYOSTOCKEXCHANGE"} or "TOKYO" in exchange
    if (
        home_country in {"CA", "CANADA"}
        or ticker.endswith(".TO")
        or yahoo.endswith(".TO")
        or exchange in {"TSX", "TSXV"}
        or (exchange == "TSE" and home_country in {"CA", "CANADA"})
    ):
        return "CA", "canada"
    if (
        japan_exchange
        or ticker.endswith((".T", ".JP"))
        or (exchange == "TSE" and home_country in {"JP", "JAPAN"})
    ):
        return "JP", "japan"
    if exchange in {"TWSE", "TPEX", "TWO"} or ticker.endswith((".TW", ".TWO", ".TPE")):
        return "TW", "taiwan"
    if exchange in {"SEHK", "HKEX"} or ticker.endswith(".HK"):
        return "HK", "hkex"
    if exchange in {"SSE", "SZSE", "SEHKSZSE", "SEHKNTL", "CHINEXT", "STAR", "SEHKSTAR"} or ticker.endswith((".SS", ".SH", ".SZ")):
        return "CN", "china"
    if exchange in {"KRX", "KOSPI", "KOSDAQ"} or ticker.endswith((".KS", ".KQ")):
        return "KR", "opendart"
    if exchange == "ASX" or ticker.endswith(".AX") or yahoo.endswith(".AX") or home_country in {"AU", "AUS", "AUSTRALIA"}:
        return "AU", "asx"
    if exchange in {"BURSA", "KLSE", "MYX"} or ticker.endswith(".KL") or yahoo.endswith(".KL") or home_country in {"MY", "MYS", "MALAYSIA"}:
        return "MY", "bursa"
    if exchange in {"AIX", "KASE", "KASEKZ", "KZ"} or ticker.endswith(".KZ") or yahoo.endswith(".KZ") or home_country in {"KZ", "KAZ", "KAZAKHSTAN"}:
        return "KZ", "kazakhstan"
    if exchange == "SGX" or ticker.endswith(".SI") or yahoo.endswith(".SI") or home_country in {"SG", "SGP", "SINGAPORE"}:
        return "SG", "sgx"
    if exchange in {"NSE", "NSEI"} or ticker.endswith(".NS") or yahoo.endswith(".NS") or home_country in {"IN", "INDIA"}:
        return "IN", "nse"
    if (
        exchange in _EU_UK_AUDIT_EXCHANGES
        or identifier_exchanges.intersection(_EU_UK_AUDIT_EXCHANGES)
    ):
        return "EU_UK", "esef"
    if _identity_has_identifier(issuer, "JP_STOCK"):
        return "JP", "japan"
    if _identity_has_identifier(issuer, "TW_STOCK"):
        return "TW", "taiwan"
    if _identity_has_identifier(issuer, "HK_STOCK"):
        return "HK", "hkex"
    if _identity_has_identifier(issuer, "CN_STOCK"):
        return "CN", "china"
    if _identity_has_identifier(issuer, "ASX_STOCK"):
        return "AU", "asx"
    if _identity_has_identifier(issuer, "MY_STOCK"):
        return "MY", "bursa"
    if _identity_has_identifier(issuer, "KZ_STOCK"):
        return "KZ", "kazakhstan"
    if _identity_has_identifier(issuer, "SGX_STOCK"):
        return "SG", "sgx"
    if _identity_has_identifier(issuer, "NSE_STOCK"):
        return "IN", "nse"
    if _identity_has_identifier(issuer, "DART_CORP") or _identity_has_identifier(issuer, "KR_STOCK"):
        return "KR", "opendart"
    if getattr(issuer, "lei", None) or _identity_has_identifier(issuer, "LEI"):
        return "EU_UK", "esef"
    if _identity_has_identifier(issuer, "CIK"):
        return "US", "sec"
    return None


def _active_universe_market_bucket(instrument: ResearchInstrument, company: Company | None, issuer: Issuer | None) -> tuple[str, str]:
    exchange = str(instrument.exchange or (company.exchange if company else "") or "").strip().upper()
    country = str(instrument.country or (company.region if company else "") or (issuer.jurisdiction if issuer else "") or "").strip().upper()
    ticker = str(instrument.ticker or (company.ticker if company else "") or instrument.yahoo_symbol or "").strip().upper()
    yahoo = str(instrument.yahoo_symbol or "").strip().upper()
    currency = str(instrument.currency or "").strip().upper()
    hay = " ".join([exchange, country, ticker, yahoo])
    name_hay = " ".join([instrument.display_name or "", instrument.legal_name or ""]).upper()
    if country in {"CA", "CANADA"} or ticker.endswith(".TO") or yahoo.endswith(".TO") or exchange in {"TSX", "TSXV"} or (exchange == "TSE" and currency == "CAD"):
        return "CA", "canada"
    adr_like = _is_adr_like_instrument(instrument)
    if adr_like and _identity_has_identifier(issuer, "CIK"):
        return "US", "sec"
    if adr_like:
        home_bucket = _home_bucket_from_existing_identity(instrument, company, issuer)
        if home_bucket is not None:
            return home_bucket
        if _is_esef_depositary_receipt_home_candidate(instrument, company, issuer):
            return "EU_UK", "esef"
        if exchange in {"PINK", "OTC", "OTCMKTS"} or exchange not in {"NASDAQ", "NYSE", "AMEX", "ARCA", "BATS"}:
            return "DEPOSITORY_RECEIPT", "unimplemented"
    if _is_fund_like_instrument(instrument):
        return exchange or country or "FUND", "not_required_public_fund"
    if country in {"KR", "KOREA", "SOUTH KOREA", "REPUBLIC OF KOREA"} or exchange in {"KRX", "KOSPI", "KOSDAQ"} or ticker.endswith((".KS", ".KQ")):
        return "KR", "opendart"
    japan_exchange = exchange in {"TSEJ", "JPX", "TYO", "XTKS", "TOKYOSTOCKEXCHANGE"} or "TOKYO" in exchange
    foreign_primary_exchange = exchange in {
        "TADAWUL",
        "TWSE",
        "TPEX",
        "TWO",
        "SEHK",
        "HKEX",
        "SSE",
        "SZSE",
        "SHSE",
        "SHENZHEN",
        "SHANGHAI",
        "NASDAQ",
        "NYSE",
        "AMEX",
        "ASX",
        "SGX",
        "NSE",
        "NSEI",
        "BURSA",
        "KLSE",
        "MYX",
    }
    if (
        not (foreign_primary_exchange and not japan_exchange)
        and (
            country in {"JP", "JAPAN"}
            or japan_exchange
            or (exchange == "TSE" and currency == "JPY")
            or ticker.endswith((".T", ".JP"))
        )
    ):
        return "JP", "japan"
    if (
        country in {"TW", "TAIWAN"}
        or exchange in {"TWSE", "TPEX", "TWO", "ROCO"}
        or ticker.endswith((".TW", ".TWO", ".TPE"))
        or "(TWSE)" in name_hay
        or "(TPEX)" in name_hay
    ):
        return "TW", "taiwan"
    if country in {"HK", "HONG KONG"} or exchange in {"SEHK", "HKEX"} or ticker.endswith(".HK") or yahoo.endswith(".HK"):
        return "HK", "hkex"
    if (
        country in {"CN", "CHINA"}
        or exchange in {"SSE", "SZSE", "SEHKSZSE", "SEHKNTL", "CHINEXT", "STAR", "SEHKSTAR", "SHENZHEN", "SHANGHAI", "SHSE"}
        or ".SS" in hay
        or ".SH" in hay
        or ".SZ" in hay
    ):
        return "CN", "china"
    if country in {"AU", "AUS", "AUSTRALIA"} or exchange == "ASX" or ticker.endswith(".AX") or yahoo.endswith(".AX"):
        return "AU", "asx"
    if country in {"MY", "MYS", "MALAYSIA"} or exchange in {"BURSA", "KLSE", "MYX"} or ticker.endswith(".KL") or yahoo.endswith(".KL"):
        return "MY", "bursa"
    if country in {"KZ", "KAZ", "KAZAKHSTAN"} or exchange in {"AIX", "KASE", "KASEKZ", "KZ"} or ticker.endswith(".KZ") or yahoo.endswith(".KZ"):
        return "KZ", "kazakhstan"
    if country in {"SG", "SGP", "SINGAPORE"} or exchange == "SGX" or ticker.endswith(".SI") or yahoo.endswith(".SI"):
        return "SG", "sgx"
    if country in {"IN", "INDIA"} or exchange in {"NSE", "NSEI"} or ticker.endswith(".NS") or yahoo.endswith(".NS"):
        return "IN", "nse"
    if exchange == "TADAWUL":
        return "SA", "unimplemented"
    us_primary_exchange = exchange in {"NASDAQ", "NYSE", "AMEX", "ARCA", "BATS"}
    us_otc_without_home_suffix = exchange in {"PINK", "OTC"} and not _EU_UK_AUDIT_SUFFIX_RE.search(" ".join([ticker, yahoo]))
    if country in {"US", "USA", "UNITED STATES"} or us_primary_exchange or us_otc_without_home_suffix:
        return "US", "sec"
    if _EU_UK_AUDIT_SUFFIX_RE.search(hay):
        return "EU_UK", "esef"
    if country in _EU_UK_AUDIT_COUNTRIES or exchange in _EU_UK_AUDIT_EXCHANGES:
        return "EU_UK", "esef"
    home_bucket = _home_bucket_from_existing_identity(instrument, company, issuer)
    if home_bucket is not None:
        return home_bucket
    if _is_sec_ticker_lookup_fallback_candidate(instrument, company):
        return "US", "sec"
    return country or exchange or "UNKNOWN", "unimplemented"


def _rail_binding_status(
    session: Session,
    *,
    rail: str,
    issuer_id: int | None,
) -> dict[str, Any]:
    if rail == "not_required_public_fund":
        return {"rail": rail, "status": "not_required", "identifiers": []}
    if issuer_id is None:
        return {"rail": rail, "status": "blocked_no_issuer", "identifiers": []}
    if rail == "sec":
        identifiers = _issuer_identifier_values_for_audit(session, issuer_id, "CIK")
        source = session.query(Source).filter_by(name="sec_edgar_active_universe_filings").first()
        tracked = {str(value).strip() for value in (_json_dict(source.config).get("ciks") if source is not None else []) or [] if str(value).strip()}
    elif rail == "opendart":
        identifiers = _issuer_identifier_values_for_audit(session, issuer_id, "DART_CORP")
        source = session.query(Source).filter_by(name="opendart_official_filings").first()
        tracked = {str(value).strip() for value in (_json_dict(source.config).get("corp_codes") if source is not None else []) or [] if str(value).strip()}
    elif rail == "japan":
        edinet_ids = _issuer_identifier_values_for_audit(session, issuer_id, "EDINET")
        jp_ids = _issuer_identifier_values_for_audit(session, issuer_id, "JP_STOCK")
        identifiers = [*edinet_ids, *[value for value in jp_ids if value not in edinet_ids]]
        japan_sources = (
            session.query(Source)
            .filter(Source.name.in_(["edinetdb_japan_company_financials", "edinet_official_filings", "tdnet_semiconductor_announcements"]))
            .all()
        )
        source = japan_sources[0] if japan_sources else None
        tracked = set()
        for candidate_source in japan_sources:
            cfg = _json_dict(candidate_source.config)
            for key in (
                "tracked_edinet_codes",
                "priority_edinet_codes",
                "tracked_queries",
                "priority_queries",
                "stock_codes",
            ):
                tracked.update(str(value).strip() for value in (cfg.get(key) or []) if str(value).strip())
    elif rail == "taiwan":
        identifiers = _issuer_identifier_values_for_audit(session, issuer_id, "TW_STOCK")
        source = session.query(Source).filter_by(name="taiwan_mops_financial_reports").first()
        fallback_source = session.query(Source).filter_by(name="taiwan_mops_monthly_revenue").first()
        source = source or fallback_source
        tracked = set()
        for candidate_source in (source, fallback_source):
            tracked.update(
                str(value).strip()
                for value in (_json_dict(candidate_source.config).get("company_codes") if candidate_source is not None else []) or []
                if str(value).strip()
            )
    elif rail == "hkex":
        identifiers = _issuer_identifier_values_for_audit(session, issuer_id, "HK_STOCK")
        source = session.query(Source).filter_by(name="hkexnews_active_universe_disclosures").first()
        cfg = _json_dict(source.config) if source is not None else {}
        query_map = {
            str(key).strip(): str(value).strip()
            for key, value in (
                cfg.get("stock_query_id_by_stock_code")
                if isinstance(cfg.get("stock_query_id_by_stock_code"), dict)
                else {}
            ).items()
            if str(key).strip() and str(value).strip()
        }
        if query_map or "hkex_stock_codes_without_query_id" in cfg:
            tracked = set(query_map)
        else:
            tracked = {str(value).strip() for value in (cfg.get("stock_ids") or []) if str(value).strip()}
    elif rail == "china":
        identifiers = _issuer_identifier_values_for_audit(session, issuer_id, "CN_STOCK")
        source = session.query(Source).filter_by(name="cninfo_active_universe_announcements").first()
        tracked = {str(value).strip() for value in (_json_dict(source.config).get("stock_codes") if source is not None else []) or [] if str(value).strip()}
    elif rail == "canada":
        identifiers = _issuer_identifier_values_for_audit(session, issuer_id, "CA_STOCK")
        source = session.query(Source).filter_by(name="sedarplus_active_universe_filings").first()
        tracked = {str(value).strip().upper() for value in (_json_dict(source.config).get("ca_stock_codes") if source is not None else []) or [] if str(value).strip()}
    elif rail == "asx":
        identifiers = _issuer_identifier_values_for_audit(session, issuer_id, "ASX_STOCK")
        source = session.query(Source).filter_by(name="asx_active_universe_announcements").first()
        tracked = {str(value).strip().upper() for value in (_json_dict(source.config).get("stock_codes") if source is not None else []) or [] if str(value).strip()}
    elif rail == "bursa":
        identifiers = _issuer_identifier_values_for_audit(session, issuer_id, "MY_STOCK")
        source = session.query(Source).filter_by(name="bursa_active_universe_announcements").first()
        tracked = {str(value).strip().upper() for value in (_json_dict(source.config).get("stock_codes") if source is not None else []) or [] if str(value).strip()}
    elif rail == "kazakhstan":
        identifiers = _issuer_identifier_values_for_audit(session, issuer_id, "KZ_STOCK")
        source = session.query(Source).filter_by(name="kazakhstan_active_universe_announcements").first()
        tracked = {str(value).strip().upper() for value in (_json_dict(source.config).get("stock_codes") if source is not None else []) or [] if str(value).strip()}
    elif rail == "sgx":
        identifiers = _issuer_identifier_values_for_audit(session, issuer_id, "SGX_STOCK")
        source = session.query(Source).filter_by(name="sgx_active_universe_announcements").first()
        tracked = {str(value).strip().upper() for value in (_json_dict(source.config).get("stock_codes") if source is not None else []) or [] if str(value).strip()}
    elif rail == "nse":
        identifiers = _issuer_identifier_values_for_audit(session, issuer_id, "NSE_STOCK")
        source = session.query(Source).filter_by(name="nse_active_universe_announcements").first()
        tracked = {str(value).strip().upper() for value in (_json_dict(source.config).get("symbols") if source is not None else []) or [] if str(value).strip()}
    elif rail == "esef":
        identifiers = _issuer_leis_for_audit(session, issuer_id)
        source = session.query(Source).filter_by(name="filings_xbrl_org_active_universe_esef").first()
        tracked = {str(value).strip().upper() for value in (_json_dict(source.config).get("leis") if source is not None else []) or [] if str(value).strip()}
    else:
        return {"rail": rail, "status": "unimplemented", "identifiers": []}

    if not identifiers:
        return {"rail": rail, "status": "missing_identifier", "identifiers": []}
    if source is None:
        return {"rail": rail, "status": "source_missing", "identifiers": identifiers}
    bound = sorted({value for value in identifiers if value in tracked})
    if bound:
        return {"rail": rail, "status": "bound", "identifiers": identifiers, "bound_identifiers": bound}
    return {"rail": rail, "status": "identifier_not_in_source", "identifiers": identifiers, "tracked_count": len(tracked)}


def _source_pack_payload_from_evaluation(subject_id: int, evaluated: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": None,
        "subject_id": int(subject_id),
        "status": str(evaluated.get("status") or "missing"),
        "checklist": list(evaluated.get("checklist") or []),
        "items": _json_dict(evaluated.get("items")),
        "missing_items": list(evaluated.get("missing_items") or []),
        "official_links": list(evaluated.get("official_links") or []),
        "source_counts": _json_dict(evaluated.get("source_counts")),
        "reviewed_at": None,
        "updated_at": None,
    }


def _classify_source_pack_gap_cause(
    *,
    item_key: str,
    rail: str,
    rail_status: dict[str, Any],
    items: dict[str, Any],
    source_counts: dict[str, Any],
    annual_requirement: dict[str, Any] | None = None,
) -> str:
    filings = int(source_counts.get("filings") or 0)
    disclosures = int(source_counts.get("exchange_disclosures") or 0)
    artifacts = int(source_counts.get("ir_artifacts") or 0)
    events = int(source_counts.get("earnings_events") or 0)
    official_records = filings + disclosures + artifacts + events
    filing_records = filings + disclosures
    rail_status_v = str(rail_status.get("status") or "").strip().lower()
    if item_key == "latest_annual_report":
        requirement = _json_dict(annual_requirement)
        requirement_status = str(requirement.get("status") or "").strip().lower()
        if requirement_status in {"not_required", "not_required_public_fund", "not_required_private"}:
            return "annual_equivalent_not_required"
        if requirement_status in {"first_annual_not_yet_due", "future_listing_not_required_yet"}:
            return "annual_equivalent_not_yet_due"
        if requirement_status == "inactive_listing_resolution_needed":
            return "annual_equivalent_inactive_listing_resolution_needed"
        if requirement_status == "primary_or_parent_listing_resolution_needed":
            return "annual_equivalent_primary_or_parent_listing_resolution_needed"
        if rail_status_v not in {"bound", "not_required"}:
            return "annual_equivalent_rail_binding_gap"
        if official_records == 0:
            return "required_annual_equivalent_zero_official_records"
        return "required_annual_equivalent_classification_or_backfill_gap"
    if rail_status_v not in {"bound", "not_required"}:
        return "rail_binding_gap"
    if item_key == "earnings_transcript_or_call_material":
        if not bool(_json_dict(items.get("latest_quarterly_or_earnings_release")).get("present")):
            return "waiting_for_official_earnings_material"
        return "transcript_or_call_material_missing"
    if item_key in {"ir_website", "product_pages", "official_press_news_page"}:
        return "issuer_profile_metadata_missing"
    if item_key == "investor_presentation":
        return "presentation_or_ir_library_missing"
    if item_key == "latest_quarterly_or_earnings_release" and rail == "esef" and bool(
        _json_dict(items.get("latest_annual_report")).get("present")
    ):
        return "provider_scope_missing_quarterly"
    if official_records == 0:
        return "zero_official_records"
    if item_key in {"filings_source", "regulatory_or_exchange_disclosures"} and filing_records == 0:
        return "zero_filing_or_exchange_records"
    if item_key in SOURCE_PACK_API_FIRST_ITEMS:
        return "classification_or_backfill_gap"
    return "classification_policy"


def _event_has_official_release_material(event: EarningsEvent) -> bool:
    return bool(event.release_filing_id or event.release_disclosure_id or event.release_artifact_id or event.presentation_artifact_id or event.webcast_artifact_id)


def _event_has_linked_call_material(session: Session, event: EarningsEvent) -> bool:
    for artifact_id in (event.transcript_artifact_id, event.webcast_artifact_id, event.presentation_artifact_id):
        if artifact_id is None:
            continue
        artifact = session.get(IRArtifact, int(artifact_id))
        if _is_earnings_call_material_artifact(artifact):
            return True
    return False


def _datetimes_close(left: datetime | None, right: datetime | None, *, tolerance_seconds: int = 2) -> bool:
    if left is None or right is None:
        return False
    return abs((left - right).total_seconds()) <= tolerance_seconds


def _event_has_untrusted_ingest_time(session: Session, event: EarningsEvent, value: datetime | None) -> bool:
    if value is None or event.period_end is None or value <= datetime.combine(event.period_end, datetime.min.time()) + timedelta(days=120):
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


def _event_scheduled_date_is_untrusted(session: Session, event: EarningsEvent) -> bool:
    if event.scheduled_date is None or event.period_end is None:
        return False
    scheduled_at = datetime.combine(event.scheduled_date, datetime.min.time())
    if scheduled_at <= datetime.combine(event.period_end, datetime.min.time()) + timedelta(days=120):
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


def _event_material_at_for_transcript_gate(session: Session, event: EarningsEvent) -> datetime | None:
    values: list[datetime] = []
    if event.release_filing_id is not None:
        filing = session.get(SecuritiesFiling, int(event.release_filing_id))
        if filing is not None and filing.filed_at is not None:
            values.append(filing.filed_at)
    if event.release_disclosure_id is not None:
        disclosure = session.get(ExchangeDisclosure, int(event.release_disclosure_id))
        if disclosure is not None and disclosure.announced_at is not None:
            values.append(disclosure.announced_at)
    for artifact_id in (event.release_artifact_id, event.presentation_artifact_id, event.webcast_artifact_id):
        if artifact_id is None:
            continue
        artifact = session.get(IRArtifact, int(artifact_id))
        if artifact is None or artifact.published_at is None:
            continue
        if not _event_has_untrusted_ingest_time(session, event, artifact.published_at):
            values.append(artifact.published_at)
    return max(values) if values else None


def _event_has_untrusted_transcript_gate_timing(session: Session, event: EarningsEvent) -> bool:
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


def _event_is_completed_for_transcript_gate(session: Session, event: EarningsEvent) -> bool:
    if str(event.status or "").strip().lower() == "completed":
        material_at = _event_material_at_for_transcript_gate(session, event)
        if material_at is not None:
            return True
        if (
            event.scheduled_date is not None
            and not _event_scheduled_date_is_untrusted(session, event)
        ):
            return True
    if event.actual_release_at is not None and not _event_has_untrusted_ingest_time(session, event, event.actual_release_at):
        return True
    if event.actual_call_at is not None and not _event_has_untrusted_ingest_time(session, event, event.actual_call_at):
        return True
    return False


def _transcript_watch_state_payload(session: Session, *, event: EarningsEvent) -> dict[str, Any] | None:
    state = (
        session.query(TranscriptWatchState)
        .filter_by(watch_key=f"earnings_event:{int(event.id)}")
        .order_by(TranscriptWatchState.updated_at.desc(), TranscriptWatchState.id.desc())
        .first()
    )
    if state is None:
        return None
    payload = _json_dict(state.payload)
    return {
        "id": int(state.id),
        "status": state.status,
        "miss_count": int(state.miss_count or 0),
        "last_checked_at": state.last_checked_at.isoformat() if state.last_checked_at else None,
        "next_check_at": state.next_check_at.isoformat() if state.next_check_at else None,
        "transcript_source_kind": payload.get("transcript_source_kind"),
        "transcript_url": payload.get("transcript_url"),
        "mode": payload.get("mode"),
    }


def _transcript_watch_state_is_exhausted(watch_state: dict[str, Any] | None) -> bool:
    if not watch_state:
        return False
    status = str(watch_state.get("status") or "").strip().lower()
    if status in {"exhausted", "blocked_no_public_transcript"}:
        return True
    if status != "missing":
        return False
    try:
        miss_count = int(watch_state.get("miss_count") or 0)
    except Exception:
        return False
    return miss_count >= TRANSCRIPT_GATE_EXHAUSTED_MISS_THRESHOLD


def audit_active_universe_source_pack_coverage(
    session: Session,
    *,
    universe_slug: str | None = None,
    ensure_subjects: bool = False,
    refresh_source_packs: bool = False,
    limit: int | None = None,
    examples_limit: int = 12,
) -> dict[str, Any]:
    """Read or refresh a bottleneck-readiness audit for active-universe official source coverage.

    This deliberately separates general source-pack completeness from stricter official/API rail
    coverage so an IR URL cannot hide missing SEC/OpenDART/EDINET/TWSE/MOPS records.
    """
    rows = _active_universe_rows(session, universe_slug=universe_slug)
    if limit is not None:
        rows = rows[: max(1, int(limit))]
    examples_n = max(1, int(examples_limit or 12))
    subjects = active_universe_subjects(session, universe_slug=universe_slug, ensure=ensure_subjects, rows=rows)
    source_states = {
        rail: [_official_source_state(session, source_name) for source_name in source_names]
        for rail, source_names in ACTIVE_UNIVERSE_OFFICIAL_RAIL_SOURCES.items()
    }

    market_counts: Counter[str] = Counter()
    rail_counts: Counter[str] = Counter()
    rail_status_counts: Counter[str] = Counter()
    source_pack_counts: Counter[str] = Counter()
    stored_source_pack_counts: Counter[str] = Counter()
    missing_item_counts: Counter[str] = Counter()
    missing_item_cause_counts: dict[str, Counter[str]] = defaultdict(Counter)
    missing_item_rail_counts: dict[str, Counter[str]] = defaultdict(Counter)
    annual_filing_requirement_counts: Counter[str] = Counter()
    annual_gap_requirement_counts: Counter[str] = Counter()
    backfill_candidate_subjects_by_rail: dict[str, set[int]] = defaultdict(set)
    strict_api_missing_counts: Counter[str] = Counter()
    identity_counts: Counter[str] = Counter()
    transcript_gate_counts: Counter[str] = Counter()
    transcript_gate_cause_counts: Counter[str] = Counter()
    official_record_counts: Counter[str] = Counter()
    rows_without_company = 0
    rows_without_issuer = 0
    subjectless_rows = 0
    unique_instruments: set[int] = set()
    company_ids: set[int] = set()
    issuer_ids: set[int] = set()
    examples: dict[str, list[dict[str, Any]]] = {
        "identity_blockers": [],
        "missing_source_pack_items": [],
        "official_rail_gaps": [],
        "official_identifier_conflicts": [],
        "inactive_listing_resolution": [],
        "url_only_filings_source": [],
        "transcript_gate_open": [],
        "transcript_gate_exhausted": [],
        "transcript_gate_subsidiary_only": [],
        "transcript_gate_untrusted_event_date": [],
        "source_pack_gap_causes": [],
        "unimplemented_official_rails": [],
    }

    def _append_example(key: str, payload: dict[str, Any]) -> None:
        bucket = examples.setdefault(key, [])
        if len(bucket) < examples_n:
            bucket.append(payload)

    subject_id_by_instrument_id: dict[int, int] = {}
    for subject_id, entry in subjects.items():
        for instrument_id in entry.get("instruments", {}):
            subject_id_by_instrument_id[int(instrument_id)] = int(subject_id)
    subject_audit_context: dict[int, dict[str, Any]] = {}

    for row in rows:
        instrument = row.instrument
        if instrument is None:
            identity_counts["missing_instrument"] += 1
            continue
        unique_instruments.add(int(instrument.id))
        company = session.get(Company, int(instrument.company_id)) if instrument.company_id is not None else None
        issuer = session.get(Issuer, int(instrument.issuer_id)) if instrument.issuer_id is not None else None
        if issuer is None and company is not None and company.issuer_id is not None:
            issuer = session.get(Issuer, int(company.issuer_id))
        if company is not None and company.id is not None:
            company_ids.add(int(company.id))
        if issuer is not None and issuer.id is not None:
            issuer_ids.add(int(issuer.id))
        if instrument.company_id is None:
            rows_without_company += 1
            identity_counts["missing_company_id"] += 1
        if instrument.issuer_id is None and (company is None or company.issuer_id is None):
            rows_without_issuer += 1
            identity_counts["missing_issuer_id"] += 1
        if int(instrument.id) not in subject_id_by_instrument_id:
            subjectless_rows += 1
            identity_counts["missing_subject"] += 1
            _append_example(
                "identity_blockers",
                {
                    "universe": row.universe.slug if row.universe is not None else None,
                    "instrument": _instrument_payload(instrument),
                    "reason": "no_research_subject_for_instrument",
                },
            )

        subject_id = subject_id_by_instrument_id.get(int(instrument.id))
        if not bool(instrument.is_public):
            market_counts["PRIVATE"] += 1
            rail_counts["not_required_private"] += 1
            rail_status_counts["not_required_private:not_required"] += 1
            if subject_id is not None and int(subject_id) not in subject_audit_context:
                subject_audit_context[int(subject_id)] = {
                    "market": "PRIVATE",
                    "rail": "not_required_private",
                    "rail_status": {"rail": "not_required_private", "status": "not_required"},
                }
            continue
        market, rail = _active_universe_market_bucket(instrument, company, issuer)
        market_counts[market] += 1
        rail_counts[rail] += 1
        rail_status = _rail_binding_status(session, rail=rail, issuer_id=int(issuer.id) if issuer is not None else None)
        identifier_conflict = _official_identifier_conflict_status(
            session,
            rail=rail,
            instrument=instrument,
            company=company,
            issuer=issuer,
        )
        if identifier_conflict is not None and rail_status["status"] in {"missing_identifier", "blocked_no_issuer", "identifier_not_in_source"}:
            rail_status = identifier_conflict
        rail_status_counts[f"{rail}:{rail_status['status']}"] += 1
        if subject_id is not None and (
            int(subject_id) not in subject_audit_context
            or str(subject_audit_context[int(subject_id)].get("rail") or "") == "not_required_private"
        ):
            subject_audit_context[int(subject_id)] = {
                "market": market,
                "rail": rail,
                "rail_status": rail_status,
            }
        if rail_status["status"] == "identifier_conflict":
            _append_example(
                "official_identifier_conflicts",
                {
                    "universe": row.universe.slug if row.universe is not None else None,
                    "market": market,
                    "rail": rail,
                    "rail_status": rail_status,
                    "instrument": _instrument_payload(instrument),
                    "issuer": _issuer_payload(issuer),
                    "company": _company_payload(company),
                    "recommended_next_step": "preview research-identity-repair with official evidence before applying any relink",
                },
            )
        elif rail == "hkex" and rail_status["status"] == "identifier_not_in_source":
            _append_example(
                "inactive_listing_resolution",
                {
                    "universe": row.universe.slug if row.universe is not None else None,
                    "market": market,
                    "rail": rail,
                    "rail_status": rail_status,
                    "instrument": _instrument_payload(instrument),
                    "issuer": _issuer_payload(issuer),
                    "company": _company_payload(company),
                    "recommended_next_step": "verify listing status and archive or relink the universe instrument before treating this as an official filing ingestion failure",
                },
            )
        elif rail_status["status"] in {"missing_identifier", "source_missing", "identifier_not_in_source", "blocked_no_issuer"}:
            _append_example(
                "official_rail_gaps",
                {
                    "universe": row.universe.slug if row.universe is not None else None,
                    "market": market,
                    "rail": rail,
                    "rail_status": rail_status,
                    "instrument": _instrument_payload(instrument),
                    "issuer": _issuer_payload(issuer),
                    "company": _company_payload(company),
                },
            )
        elif rail == "unimplemented":
            _append_example(
                "unimplemented_official_rails",
                {
                    "universe": row.universe.slug if row.universe is not None else None,
                    "market": market,
                    "instrument": _instrument_payload(instrument),
                    "issuer": _issuer_payload(issuer),
                    "company": _company_payload(company),
                },
            )

    for subject_id, entry in subjects.items():
        subject: ResearchSubject = entry["subject"]
        if refresh_source_packs:
            pack_payload = serialize_source_pack(upsert_source_pack(session, subject_id=int(subject_id), audit={"source": "active_universe_source_pack_audit"})) or {}
        else:
            pack_payload = _source_pack_payload_from_evaluation(int(subject_id), evaluate_source_pack(session, subject=subject))
        pack_status = str(pack_payload.get("status") or "missing")
        source_pack_counts[pack_status] += 1
        stored_pack = session.query(ResearchSourcePack).filter_by(subject_id=int(subject_id)).first()
        stored_source_pack_counts[str(stored_pack.status if stored_pack is not None else "not_created")] += 1
        items = _json_dict(pack_payload.get("items"))
        source_counts = _json_dict(pack_payload.get("source_counts"))
        official_record_counts["filings"] += int(source_counts.get("filings") or 0)
        official_record_counts["exchange_disclosures"] += int(source_counts.get("exchange_disclosures") or 0)
        official_record_counts["ir_artifacts"] += int(source_counts.get("ir_artifacts") or 0)
        official_record_counts["earnings_events"] += int(source_counts.get("earnings_events") or 0)
        audit_context = subject_audit_context.get(int(subject_id), {})
        subject_market = str(audit_context.get("market") or "UNKNOWN")
        subject_rail = str(audit_context.get("rail") or "unimplemented")
        subject_rail_status = _json_dict(audit_context.get("rail_status")) or {"rail": subject_rail, "status": "unknown"}
        subject_public_instruments = _subject_public_instruments(session, subject)
        annual_requirement = _annual_filing_requirement_payload(
            session,
            subject=subject,
            public_instruments=subject_public_instruments,
            rail=subject_rail,
            rail_status=subject_rail_status,
        )
        annual_requirement_status = str(annual_requirement.get("status") or "unknown")
        official_rail_not_required = subject_rail in {"not_required_private", "not_required_public_fund"} or annual_requirement_status in {
            "not_required",
            "not_required_private",
            "not_required_public_fund",
        }
        annual_filing_requirement_counts[annual_requirement_status] += 1
        for item in pack_payload.get("missing_items") or []:
            item_key = str(item)
            cause = _classify_source_pack_gap_cause(
                item_key=item_key,
                rail=subject_rail,
                rail_status=subject_rail_status,
                items=items,
                source_counts=source_counts,
                annual_requirement=annual_requirement if item_key == "latest_annual_report" else None,
            )
            missing_item_counts[item_key] += 1
            missing_item_cause_counts[item_key][cause] += 1
            missing_item_rail_counts[item_key][subject_rail] += 1
            if item_key == "latest_annual_report":
                annual_gap_requirement_counts[annual_requirement_status] += 1
            if not official_rail_not_required and item_key in SOURCE_PACK_API_FIRST_ITEMS and cause in {
                "zero_official_records",
                "zero_filing_or_exchange_records",
                "classification_or_backfill_gap",
                "required_annual_equivalent_zero_official_records",
                "required_annual_equivalent_classification_or_backfill_gap",
            }:
                backfill_candidate_subjects_by_rail[subject_rail].add(int(subject_id))
            missing_payload = {
                "subject_id": int(subject_id),
                "subject": subject.display_name,
                "missing_item": item_key,
                "gap_cause": cause,
                "market": subject_market,
                "rail": subject_rail,
                "source_counts": source_counts,
            }
            gap_payload = {
                "subject_id": int(subject_id),
                "subject": subject.display_name,
                "missing_item": item_key,
                "cause": cause,
                "market": subject_market,
                "rail": subject_rail,
                "rail_status": subject_rail_status,
                "source_counts": source_counts,
            }
            if item_key == "latest_annual_report":
                missing_payload["annual_requirement"] = annual_requirement
                gap_payload["annual_requirement"] = annual_requirement
            _append_example(
                "missing_source_pack_items",
                missing_payload,
            )
            _append_example(
                "source_pack_gap_causes",
                gap_payload,
        )
        if pack_status == "not_required" or official_rail_not_required:
            transcript_gate_counts["not_required"] += 1
            transcript_gate_cause_counts["not_required"] += 1
            continue
        for item in SOURCE_PACK_API_FIRST_ITEMS:
            payload = _json_dict(items.get(item))
            if not bool(payload.get("present")):
                if item == "latest_annual_report" and annual_requirement.get("required_now") is False:
                    continue
                strict_api_missing_counts[item] += 1
        if bool(_json_dict(items.get("filings_source")).get("present")) and (
            int(source_counts.get("filings") or 0) + int(source_counts.get("exchange_disclosures") or 0)
        ) == 0:
            strict_api_missing_counts["filings_source_url_only_no_filing_records"] += 1
            _append_example(
                "url_only_filings_source",
                {
                    "subject_id": int(subject_id),
                    "subject": subject.display_name,
                    "filings_source": _json_dict(items.get("filings_source")).get("url"),
                    "source_counts": source_counts,
                },
            )

        _subject_company, subject_issuer = _subject_company_and_issuer(session, subject)
        if subject_issuer is None or subject_issuer.id is None:
            transcript_gate_counts["blocked_no_issuer"] += 1
            transcript_gate_cause_counts["blocked_no_issuer"] += 1
            continue
        events = session.query(EarningsEvent).filter_by(issuer_id=int(subject_issuer.id)).all()
        transcript_item = _json_dict(items.get("earnings_transcript_or_call_material"))
        has_transcript_or_call = bool(transcript_item.get("present"))
        official_material_events_all = [event for event in events if _event_has_official_release_material(event)]
        subsidiary_only_events = [
            event for event in official_material_events_all if earnings_event_is_subsidiary_only_result(session, event)
        ]
        official_material_events = [
            event for event in official_material_events_all if not earnings_event_is_subsidiary_only_result(session, event)
        ]
        completed_official_events = [event for event in official_material_events if _event_is_completed_for_transcript_gate(session, event)]
        latest_completed_event = (
            sorted(completed_official_events, key=lambda item: item.actual_release_at or item.updated_at or item.created_at, reverse=True)[0]
            if completed_official_events
            else None
        )
        if has_transcript_or_call:
            if latest_completed_event is not None and not _event_has_linked_call_material(session, latest_completed_event):
                transcript_gate_counts["satisfied_historical_latest_missing"] += 1
                transcript_gate_cause_counts["transcript_latest_event_missing"] += 1
                _append_example(
                    "transcript_gate_latest_event_missing",
                    {
                        "subject_id": int(subject_id),
                        "subject": subject.display_name,
                        "event_id": int(latest_completed_event.id),
                        "fiscal_year": latest_completed_event.fiscal_year,
                        "fiscal_quarter": latest_completed_event.fiscal_quarter,
                        "status": latest_completed_event.status,
                        "release_filing_id": int(latest_completed_event.release_filing_id) if latest_completed_event.release_filing_id else None,
                        "release_disclosure_id": int(latest_completed_event.release_disclosure_id) if latest_completed_event.release_disclosure_id else None,
                        "release_artifact_id": int(latest_completed_event.release_artifact_id) if latest_completed_event.release_artifact_id else None,
                        "watch_state": _transcript_watch_state_payload(session, event=latest_completed_event),
                        "next_step": "source pack has historical transcript/call material, but the latest completed official earnings event still lacks linked call material",
                    },
                )
            else:
                transcript_gate_counts["satisfied"] += 1
                transcript_gate_cause_counts["satisfied_current_or_no_completed_event"] += 1
        elif completed_official_events:
            event = latest_completed_event
            assert event is not None
            watch_state = _transcript_watch_state_payload(session, event=event)
            payload = {
                "subject_id": int(subject_id),
                "subject": subject.display_name,
                "event_id": int(event.id),
                "fiscal_year": event.fiscal_year,
                "fiscal_quarter": event.fiscal_quarter,
                "status": event.status,
                "release_filing_id": int(event.release_filing_id) if event.release_filing_id else None,
                "release_disclosure_id": int(event.release_disclosure_id) if event.release_disclosure_id else None,
                "release_artifact_id": int(event.release_artifact_id) if event.release_artifact_id else None,
                "watch_state": watch_state,
            }
            if _transcript_watch_state_is_exhausted(watch_state):
                transcript_gate_counts["missing_after_allowed_sources_exhausted"] += 1
                transcript_gate_cause_counts["manual_operator_needed"] += 1
                payload["next_step"] = (
                    "allowed transcript/call-material sources have been checked repeatedly after official earnings material; "
                    "operator should upload official transcript/audio, mark no-public-call-material after manual review, or add a new durable provider"
                )
                _append_example("transcript_gate_exhausted", payload)
            else:
                transcript_gate_counts["missing_after_official_material"] += 1
                transcript_gate_cause_counts["transcript_scan_pending"] += 1
                payload["next_step"] = "run_or_wait_for_watchlist_transcript_scan; official issuer adapters and call-material pages are tried before allowed backup providers"
                _append_example("transcript_gate_open", payload)
        elif subsidiary_only_events:
            transcript_gate_counts["closed_subsidiary_only_material"] += 1
            transcript_gate_cause_counts["subsidiary_only_result"] += 1
            event = sorted(
                subsidiary_only_events,
                key=lambda item: item.actual_release_at or item.updated_at or item.created_at,
                reverse=True,
            )[0]
            _append_example(
                "transcript_gate_subsidiary_only",
                {
                    "subject_id": int(subject_id),
                    "subject": subject.display_name,
                    "event_id": int(event.id),
                    "fiscal_year": event.fiscal_year,
                    "fiscal_quarter": event.fiscal_quarter,
                    "status": event.status,
                    "release_disclosure_id": int(event.release_disclosure_id) if event.release_disclosure_id else None,
                    "release_artifact_id": int(event.release_artifact_id) if event.release_artifact_id else None,
                    "watch_state": _transcript_watch_state_payload(session, event=event),
                    "next_step": "no parent transcript required; official material is a listed-subsidiary result, not the issuer's own earnings call",
                },
            )
        elif any(_event_has_untrusted_transcript_gate_timing(session, event) for event in official_material_events):
            transcript_gate_counts["blocked_untrusted_event_date"] += 1
            transcript_gate_cause_counts["untrusted_event_date"] += 1
            event = sorted(official_material_events, key=lambda item: item.updated_at or item.created_at, reverse=True)[0]
            _append_example(
                "transcript_gate_untrusted_event_date",
                {
                    "subject_id": int(subject_id),
                    "subject": subject.display_name,
                    "event_id": int(event.id),
                    "fiscal_year": event.fiscal_year,
                    "fiscal_quarter": event.fiscal_quarter,
                    "status": event.status,
                    "release_artifact_id": int(event.release_artifact_id) if event.release_artifact_id else None,
                    "next_step": "refresh or repair the official material timestamp before transcript search; do not use document ingest time as an earnings-call date",
                },
            )
        else:
            transcript_gate_counts["closed_no_official_material"] += 1
            transcript_gate_cause_counts["no_official_material"] += 1

    session.flush()
    return {
        "ok": True,
        "universe_slug": universe_slug,
        "partial_scan": limit is not None,
        "rows_scanned": len(rows),
        "distinct_instruments": len(unique_instruments),
        "active_subjects": len(subjects),
        "distinct_companies": len(company_ids),
        "distinct_issuers": len(issuer_ids),
        "rows_without_company": rows_without_company,
        "rows_without_issuer": rows_without_issuer,
        "subjectless_rows": subjectless_rows,
        "market_counts": dict(market_counts),
        "official_rail_counts": dict(rail_counts),
        "official_rail_status_counts": dict(rail_status_counts),
        "source_pack_status_counts": dict(source_pack_counts),
        "stored_source_pack_status_counts": dict(stored_source_pack_counts),
        "annual_filing_requirement_counts": dict(annual_filing_requirement_counts),
        "annual_gap_requirement_counts": dict(annual_gap_requirement_counts),
        "missing_item_counts": dict(missing_item_counts),
        "missing_item_cause_counts": _nested_counter_payload(missing_item_cause_counts),
        "missing_item_rail_counts": _nested_counter_payload(missing_item_rail_counts),
        "backfill_candidate_counts_by_rail": {
            str(rail): len(subject_ids) for rail, subject_ids in backfill_candidate_subjects_by_rail.items()
        },
        "strict_api_missing_counts": dict(strict_api_missing_counts),
        "identity_blocker_counts": dict(identity_counts),
        "official_record_counts": dict(official_record_counts),
        "transcript_gate_counts": dict(transcript_gate_counts),
        "transcript_gate_cause_counts": dict(transcript_gate_cause_counts),
        "official_sources": source_states,
        "examples": examples,
    }


def _active_company_ids_from_rows(rows: list[ResearchUniverseRow]) -> set[int]:
    return {
        int(row.instrument.company_id)
        for row in rows
        if row.instrument is not None and row.instrument.company_id is not None
    }


def _company_ids_from_source_configs(session: Session, *, source_names: list[str] | tuple[str, ...] | None = None) -> set[int]:
    names = [str(name or "").strip() for name in source_names or [] if str(name or "").strip()]
    if not names:
        return set()
    company_ids: set[int] = set()
    rows = session.query(Source.config).filter(Source.name.in_(names)).all()
    for (config,) in rows:
        cfg = _json_dict(config)
        for key, value in cfg.items():
            if not str(key or "").startswith("company_id_by_") or not isinstance(value, dict):
                continue
            for raw_company_id in value.values():
                try:
                    company_id = int(raw_company_id)
                except Exception:
                    continue
                if company_id > 0:
                    company_ids.add(company_id)
    return company_ids


def _target_identifier_values_for_rail(session: Session, *, rail: str, issuer_id: int | None) -> dict[str, list[str]]:
    if issuer_id is None:
        return {}
    if rail == "sec":
        return {"CIK": _issuer_identifier_values_for_audit(session, issuer_id, "CIK")}
    if rail == "opendart":
        return {
            "DART_CORP": _issuer_identifier_values_for_audit(session, issuer_id, "DART_CORP"),
            "KR_STOCK": _issuer_identifier_values_for_audit(session, issuer_id, "KR_STOCK"),
        }
    if rail == "japan":
        return {
            "EDINET": _issuer_identifier_values_for_audit(session, issuer_id, "EDINET"),
            "JP_STOCK": _issuer_identifier_values_for_audit(session, issuer_id, "JP_STOCK"),
        }
    if rail == "taiwan":
        return {"TW_STOCK": _issuer_identifier_values_for_audit(session, issuer_id, "TW_STOCK")}
    if rail == "hkex":
        return {"HK_STOCK": _issuer_identifier_values_for_audit(session, issuer_id, "HK_STOCK")}
    if rail == "china":
        return {"CN_STOCK": _issuer_identifier_values_for_audit(session, issuer_id, "CN_STOCK")}
    if rail == "canada":
        return {"CA_STOCK": _issuer_identifier_values_for_audit(session, issuer_id, "CA_STOCK")}
    if rail == "asx":
        return {"ASX_STOCK": _issuer_identifier_values_for_audit(session, issuer_id, "ASX_STOCK")}
    if rail == "bursa":
        return {"MY_STOCK": _issuer_identifier_values_for_audit(session, issuer_id, "MY_STOCK")}
    if rail == "kazakhstan":
        return {"KZ_STOCK": _issuer_identifier_values_for_audit(session, issuer_id, "KZ_STOCK")}
    if rail == "sgx":
        return {"SGX_STOCK": _issuer_identifier_values_for_audit(session, issuer_id, "SGX_STOCK")}
    if rail == "nse":
        return {"NSE_STOCK": _issuer_identifier_values_for_audit(session, issuer_id, "NSE_STOCK")}
    if rail == "esef":
        return {"LEI": _issuer_leis_for_audit(session, issuer_id)}
    return {}


def _merge_target_identifiers(target: dict[str, Any], identifiers_by_type: dict[str, list[str]]) -> None:
    current = target.setdefault("identifiers_by_type", {})
    for id_type, values in identifiers_by_type.items():
        if not isinstance(current.get(id_type), list):
            current[id_type] = []
        seen = {str(value) for value in current[id_type]}
        for value in values:
            text = str(value or "").strip()
            if text and text not in seen:
                current[id_type].append(text)
                seen.add(text)


def _source_pack_backfill_target_scopes(
    session: Session,
    *,
    universe_slug: str | None,
    source_names: list[str],
    limit: int | None,
) -> dict[str, dict[str, Any]]:
    names = [str(name or "").strip() for name in source_names or [] if str(name or "").strip()]
    if not names:
        return {}
    source_names_by_rail = {rail: set(source_names_v) for rail, source_names_v in ACTIVE_UNIVERSE_OFFICIAL_RAIL_SOURCES.items()}
    selected_names = set(names)
    rows = _active_universe_rows(session, universe_slug=universe_slug)
    if limit is not None:
        rows = rows[: max(1, int(limit))]
    subjects = active_universe_subjects(session, universe_slug=universe_slug, ensure=True, rows=rows)
    subject_id_by_instrument_id = {
        int(instrument_id): int(subject_id)
        for subject_id, entry in subjects.items()
        for instrument_id in entry.get("instruments", {})
    }
    subject_context: dict[int, dict[str, Any]] = {}
    for row in rows:
        instrument = row.instrument
        if instrument is None:
            continue
        subject_id = subject_id_by_instrument_id.get(int(instrument.id))
        if subject_id is None or subject_id in subject_context:
            continue
        company = session.get(Company, int(instrument.company_id)) if instrument.company_id is not None else None
        issuer = session.get(Issuer, int(instrument.issuer_id)) if instrument.issuer_id is not None else None
        if issuer is None and company is not None and company.issuer_id is not None:
            issuer = session.get(Issuer, int(company.issuer_id))
        _market, rail = _active_universe_market_bucket(instrument, company, issuer)
        subject_context[subject_id] = {
            "rail": rail,
            "rail_status": _rail_binding_status(session, rail=rail, issuer_id=int(issuer.id) if issuer is not None else None),
        }

    targets: dict[str, dict[str, Any]] = {}
    for subject_id, entry in subjects.items():
        subject: ResearchSubject = entry["subject"]
        context = subject_context.get(int(subject_id), {})
        rail = str(context.get("rail") or "")
        rail_source_names = source_names_by_rail.get(rail, set())
        wanted_source_names = sorted(selected_names.intersection(rail_source_names))
        if not wanted_source_names:
            continue
        evaluated = evaluate_source_pack(session, subject=subject)
        items = _json_dict(evaluated.get("items"))
        source_counts = _json_dict(evaluated.get("source_counts"))
        rail_status = _json_dict(context.get("rail_status")) or {"rail": rail, "status": "unknown"}
        annual_requirement = _annual_filing_requirement_payload(
            session,
            subject=subject,
            public_instruments=_subject_public_instruments(session, subject),
            rail=rail,
            rail_status=rail_status,
        )
        missing_api_items: list[str] = []
        for item_key in evaluated.get("missing_items") or []:
            key = str(item_key)
            if key not in SOURCE_PACK_API_FIRST_ITEMS:
                continue
            cause = _classify_source_pack_gap_cause(
                item_key=key,
                rail=rail,
                rail_status=rail_status,
                items=items,
                source_counts=source_counts,
                annual_requirement=annual_requirement if key == "latest_annual_report" else None,
            )
            if cause in {
                "zero_official_records",
                "zero_filing_or_exchange_records",
                "classification_or_backfill_gap",
                "required_annual_equivalent_zero_official_records",
                "required_annual_equivalent_classification_or_backfill_gap",
            }:
                missing_api_items.append(key)
        if not missing_api_items:
            continue
        company, issuer = _subject_company_and_issuer(session, subject)
        identifiers_by_type = _target_identifier_values_for_rail(session, rail=rail, issuer_id=int(issuer.id) if issuer is not None else None)
        if not any(identifiers_by_type.values()):
            continue
        company_id = int(company.id) if company is not None and company.id is not None else None
        for source_name in wanted_source_names:
            target = targets.setdefault(
                source_name,
                {
                    "rail": rail,
                    "company_ids": [],
                    "subject_ids": [],
                    "missing_items": [],
                    "identifiers_by_type": {},
                },
            )
            if company_id is not None and company_id not in target["company_ids"]:
                target["company_ids"].append(company_id)
            if int(subject_id) not in target["subject_ids"]:
                target["subject_ids"].append(int(subject_id))
            for missing_item in missing_api_items:
                if missing_item not in target["missing_items"]:
                    target["missing_items"].append(missing_item)
            _merge_target_identifiers(target, identifiers_by_type)
    return targets


def _source_pack_target_values(target: dict[str, Any], id_type: str) -> list[str]:
    values = _json_dict(target.get("identifiers_by_type")).get(id_type) or []
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            out.append(text)
            seen.add(text)
    return out


def _source_pack_target_jp_stock_codes(target: dict[str, Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in _source_pack_target_values(target, "JP_STOCK"):
        raw = re.sub(r"\.(T|JP)$", "", str(value or "").strip().upper())
        raw = re.sub(r"[^0-9A-Z]+", "", raw)
        if raw.endswith("0"):
            candidate = raw[:-1]
            if re.fullmatch(r"[0-9]{4}[A-Z]?|[0-9]{3}[A-Z]", candidate):
                raw = candidate
        if not re.fullmatch(r"[0-9]{4}[A-Z]?|[0-9]{3}[A-Z]", raw):
            continue
        if raw not in seen:
            out.append(raw)
            seen.add(raw)
    return out


def _source_pack_target_company_ids(target: dict[str, Any]) -> set[int]:
    company_ids: set[int] = set()
    for value in target.get("company_ids") or []:
        try:
            company_id = int(value)
        except Exception:
            continue
        if company_id > 0:
            company_ids.add(company_id)
    return company_ids


def _source_pack_known_edinet_codes_for_target(source_cfg: dict[str, Any], target: dict[str, Any]) -> list[str]:
    company_ids = _source_pack_target_company_ids(target)
    if not company_ids:
        return []
    company_id_by_edinet_code = source_cfg.get("company_id_by_edinet_code")
    if not isinstance(company_id_by_edinet_code, dict):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for code, raw_company_id in company_id_by_edinet_code.items():
        try:
            company_id = int(raw_company_id)
        except Exception:
            continue
        text = str(code or "").strip()
        if text and company_id in company_ids and text not in seen:
            out.append(text)
            seen.add(text)
    return out


def _merge_source_pack_values(*groups: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for group in groups:
        for value in group:
            text = str(value or "").strip()
            if text and text not in seen:
                out.append(text)
                seen.add(text)
    return out


def _append_source_pack_keywords(cfg: dict[str, Any], values: list[object]) -> None:
    keywords = [str(value or "").strip() for value in (cfg.get("include_keywords") or []) if str(value or "").strip()]
    seen = {value.casefold() for value in keywords}
    for value in values:
        text = str(value or "").strip()
        if text and text.casefold() not in seen:
            keywords.append(text)
            seen.add(text.casefold())
    cfg["include_keywords"] = keywords


def _apply_source_pack_target_scope(source_name: str, source_cfg: dict[str, Any], target: dict[str, Any] | None) -> dict[str, Any]:
    if not target:
        return source_cfg
    cfg = dict(source_cfg)
    cfg["_source_cursor_override"] = {}
    cfg["defer_company_entity_link"] = True
    if source_name in {"sec_edgar_active_universe_filings", "sec_companyfacts_active_universe"}:
        values = _source_pack_target_values(target, "CIK")
        if values:
            cfg["ciks"] = values
            cfg["rotate_ciks"] = False
            cfg["max_ciks_per_run"] = len(values)
            missing_items = {str(item) for item in target.get("missing_items") or []}
            per_cik = max(1, int(cfg.get("source_pack_max_items_per_cik") or 6))
            if source_name == "sec_edgar_active_universe_filings" and "latest_annual_report" in missing_items:
                cfg["since_days"] = max(int(cfg.get("since_days") or 0), int(cfg.get("source_pack_annual_lookback_days") or 3650))
                per_cik = max(per_cik, int(cfg.get("source_pack_annual_max_items_per_cik") or 30))
            cfg["source_pack_max_items_per_cik"] = per_cik
            cfg["max_items_per_run"] = max(int(cfg.get("max_items_per_run") or 0), len(values) * per_cik)
            cfg["prioritize_source_pack_forms"] = True
            cfg["source_pack_targeted_backfill"] = True
    elif source_name in {"opendart_official_filings", "opendart_korea_financials"}:
        values = _source_pack_target_values(target, "DART_CORP")
        if values:
            cfg["corp_codes"] = values
            cfg["rotate_corp_codes"] = False
            cfg["max_corp_codes_per_run"] = len(values)
            cfg["source_pack_priority_mode"] = True
            cfg["source_pack_targeted_backfill"] = True
            name_map = cfg.get("company_name_by_corp_code") if isinstance(cfg.get("company_name_by_corp_code"), dict) else {}
            ticker_map = cfg.get("ticker_by_corp_code") if isinstance(cfg.get("ticker_by_corp_code"), dict) else {}
            _append_source_pack_keywords(cfg, [*(name_map.get(code) for code in values), *(ticker_map.get(code) for code in values), *values])
            if source_name == "opendart_official_filings":
                missing_items = {str(item) for item in target.get("missing_items") or []}
                if missing_items.intersection({"latest_annual_report", "latest_quarterly_or_earnings_release"}):
                    cfg["pblntf_ty"] = ["A"]
                    periodic_items_per_company = max(1, int(cfg.get("source_pack_periodic_items_per_company") or 8))
                    cfg["max_items_per_run"] = max(int(cfg.get("max_items_per_run") or 0), len(values) * periodic_items_per_company)
            if source_name == "opendart_korea_financials":
                cfg["report_codes"] = ["annual", "1q", "half", "3q"]
                if "latest_annual_report" in set(target.get("missing_items") or []):
                    cfg["source_pack_annual_first"] = True
                fs_divs = cfg.get("fs_divs") if isinstance(cfg.get("fs_divs"), list) else ["CFS", "OFS"]
                desired_requests = max(1, len(values) * len(fs_divs) * 2)
                cfg["requests_per_run"] = max(int(cfg.get("requests_per_run") or 0), desired_requests)
                cfg["max_items_per_run"] = max(int(cfg.get("max_items_per_run") or 0), desired_requests)
    elif source_name == "krx_active_universe_disclosures":
        values = _source_pack_target_values(target, "KR_STOCK")
        if values:
            cfg["stock_codes"] = values
            cfg["source_pack_targeted_backfill"] = True
    elif source_name in {
        "taiwan_mops_monthly_revenue",
        "taiwan_mops_financial_reports",
        "taiwan_mops_investor_conferences",
        "twse_material_announcements",
        "tpex_infohub_company_pages",
    }:
        values = _source_pack_target_values(target, "TW_STOCK")
        if values:
            cfg["company_codes"] = values
            cfg["annual_company_limit_per_run"] = max(int(cfg.get("annual_company_limit_per_run") or 0), len(values))
            cfg["source_pack_targeted_backfill"] = True
    elif source_name == "hkexnews_active_universe_disclosures":
        values = _source_pack_target_values(target, "HK_STOCK")
        if values:
            cfg["stock_ids"] = values
            cfg["stock_codes"] = values
            cfg["stock_ids_per_run"] = max(
                len(values),
                int(cfg.get("stock_ids_per_run") or cfg.get("max_stock_ids_per_run") or 4),
            )
            per_stock = max(1, int(cfg.get("max_items_per_stock") or cfg.get("max_items_per_stock_id") or 30))
            cfg["max_items_per_run"] = max(int(cfg.get("max_items_per_run") or 0), int(cfg["stock_ids_per_run"]) * per_stock)
            cfg["source_pack_targeted_backfill"] = True
    elif source_name == "cninfo_active_universe_announcements":
        values = _source_pack_target_values(target, "CN_STOCK")
        if values:
            cfg["stock_codes"] = values
            cfg["stock_codes_per_run"] = max(
                len(values),
                int(cfg.get("stock_codes_per_run") or cfg.get("max_stock_codes_per_run") or 4),
            )
            per_stock = max(1, int(cfg.get("page_size") or 30))
            cfg["max_items_per_run"] = max(int(cfg.get("max_items_per_run") or 0), int(cfg["stock_codes_per_run"]) * per_stock)
            missing_items = {str(item) for item in target.get("missing_items") or []}
            if "latest_annual_report" in missing_items:
                cfg["searchkey"] = str(cfg.get("source_pack_annual_searchkey") or "年度报告")
                cfg["recent_days"] = max(int(cfg.get("recent_days") or 0), int(cfg.get("source_pack_annual_lookback_days") or 430))
                cfg["source_pack_target_query"] = "annual_report"
            elif "latest_quarterly_or_earnings_release" in missing_items:
                cfg["searchkey"] = str(cfg.get("source_pack_quarterly_searchkey") or "季度报告")
                cfg["recent_days"] = max(int(cfg.get("recent_days") or 0), int(cfg.get("source_pack_quarterly_lookback_days") or 210))
                cfg["source_pack_target_query"] = "quarterly_report"
            cfg["source_pack_targeted_backfill"] = True
    elif source_name == "sedarplus_active_universe_filings":
        values = _source_pack_target_values(target, "CA_STOCK")
        if values:
            cfg["ca_stock_codes"] = values
            cfg["source_pack_targeted_backfill"] = True
    elif source_name == "asx_active_universe_announcements":
        values = _source_pack_target_values(target, "ASX_STOCK")
        if values:
            cfg["stock_codes"] = values
            cfg["stock_codes_per_run"] = max(1, int(cfg.get("stock_codes_per_run") or cfg.get("asx_codes_per_run") or 4))
            cfg["source_pack_targeted_backfill"] = True
    elif source_name == "bursa_active_universe_announcements":
        values = _source_pack_target_values(target, "MY_STOCK")
        if values:
            cfg["stock_codes"] = values
            cfg["source_pack_targeted_backfill"] = True
    elif source_name == "kazakhstan_active_universe_announcements":
        values = _source_pack_target_values(target, "KZ_STOCK")
        if values:
            cfg["stock_codes"] = values
            cfg["source_pack_targeted_backfill"] = True
    elif source_name == "sgx_active_universe_announcements":
        values = _source_pack_target_values(target, "SGX_STOCK")
        if values:
            cfg["stock_codes"] = values
            cfg["source_pack_targeted_backfill"] = True
    elif source_name == "nse_active_universe_announcements":
        values = _source_pack_target_values(target, "NSE_STOCK")
        if values:
            cfg["symbols"] = values
            cfg["nse_symbols"] = values
            cfg["symbols_per_run"] = max(1, int(cfg.get("symbols_per_run") or 3))
            cfg["source_pack_targeted_backfill"] = True
    elif source_name == "edinetdb_japan_company_financials":
        edinet_codes = _merge_source_pack_values(
            _source_pack_target_values(target, "EDINET"),
            _source_pack_known_edinet_codes_for_target(source_cfg, target),
        )
        stock_codes = _source_pack_target_values(target, "JP_STOCK")
        cfg["tracked_edinet_codes"] = edinet_codes
        cfg["priority_edinet_codes"] = edinet_codes
        cfg["resolve_queries_via_company_list"] = True
        cfg["fallback_query_search"] = False
        cfg["company_list_per_page"] = max(int(cfg.get("company_list_per_page") or 0), 5000)
        if stock_codes:
            cfg["tracked_queries"] = stock_codes
            cfg["priority_queries"] = stock_codes
            targeted_query_quota = min(
                len(stock_codes),
                int(cfg.get("source_pack_targeted_queries_per_run") or cfg.get("source_pack_targeted_companies_per_run") or 16),
            )
            cfg["priority_queries_per_run"] = max(int(cfg.get("priority_queries_per_run") or 0), targeted_query_quota)
            cfg["priority_companies_per_run"] = max(int(cfg.get("priority_companies_per_run") or 0), targeted_query_quota)
            cfg["companies_per_run"] = max(int(cfg.get("companies_per_run") or 0), targeted_query_quota)
        if edinet_codes or stock_codes:
            cfg["source_pack_targeted_backfill"] = True
    elif source_name == "edinet_official_filings":
        edinet_codes = _merge_source_pack_values(
            _source_pack_target_values(target, "EDINET"),
            _source_pack_known_edinet_codes_for_target(source_cfg, target),
        )
        stock_codes = _source_pack_target_values(target, "JP_STOCK")
        if edinet_codes or stock_codes:
            missing_items = {str(item) for item in target.get("missing_items") or []}
            form_codes: list[str] = []
            if "latest_annual_report" in missing_items:
                form_codes.extend([str(value) for value in cfg.get("source_pack_annual_form_codes") or ["030000"]])
                cfg["lookback_days"] = max(
                    int(cfg.get("lookback_days") or 0),
                    int(cfg.get("source_pack_annual_lookback_days") or 430),
                )
                cfg["source_pack_target_query"] = "annual_report"
            if "latest_quarterly_or_earnings_release" in missing_items:
                form_codes.extend([str(value) for value in cfg.get("source_pack_quarterly_form_codes") or ["043000", "050000"]])
                cfg["lookback_days"] = max(
                    int(cfg.get("lookback_days") or 0),
                    int(cfg.get("source_pack_quarterly_lookback_days") or 210),
                )
                if cfg.get("source_pack_target_query") != "annual_report":
                    cfg["source_pack_target_query"] = "quarterly_report"
            if form_codes:
                cfg["form_codes"] = _merge_source_pack_values(form_codes)
            _append_source_pack_keywords(
                cfg,
                [*edinet_codes, *stock_codes, *(f"{code}0" for code in stock_codes if len(code) == 4)],
            )
            cfg["max_items_per_run"] = max(
                int(cfg.get("max_items_per_run") or 0),
                min(500, max(20, (len(edinet_codes) + len(stock_codes)) * 6)),
            )
            cfg["source_pack_targeted_backfill"] = True
    elif source_name == "tdnet_semiconductor_announcements":
        values = _source_pack_target_jp_stock_codes(target)
        if values:
            cfg["stock_codes"] = values
            cfg["priority_queries"] = values
            cfg["tracked_queries"] = []
            cfg["queries_per_run"] = 0
            cfg["tracked_queries_per_run"] = 0
            missing_items = {str(item) for item in target.get("missing_items") or []}
            if "latest_annual_report" in missing_items:
                cfg["lookback_days"] = max(
                    int(cfg.get("lookback_days") or 0),
                    int(cfg.get("source_pack_annual_lookback_days") or 430),
                )
                cfg["source_pack_target_query"] = "annual_report"
            if "latest_quarterly_or_earnings_release" in missing_items:
                cfg["lookback_days"] = max(
                    int(cfg.get("lookback_days") or 0),
                    int(cfg.get("source_pack_quarterly_lookback_days") or 210),
                )
                if cfg.get("source_pack_target_query") != "annual_report":
                    cfg["source_pack_target_query"] = "quarterly_or_earnings_release"
            cfg["priority_queries_per_run"] = max(int(cfg.get("priority_queries_per_run") or 0), len(values))
            cfg["source_pack_seconds_per_query"] = float(cfg.get("source_pack_seconds_per_query") or 4.0)
            cfg["max_items_per_run"] = max(
                int(cfg.get("max_items_per_run") or 0),
                min(1000, max(120, len(values) * 8)),
            )
            cfg["source_pack_targeted_backfill"] = True
    elif source_name == "filings_xbrl_org_active_universe_esef":
        values = _source_pack_target_values(target, "LEI")
        if values:
            cfg["leis"] = values
            cfg["leis_per_run"] = len(values)
            cfg["entities_per_run"] = len(values)
            cfg["include_country_fallback"] = True
            cfg["restrict_country_fallback_to_selected_leis"] = True
            cfg["country_fallback_pages"] = max(
                int(cfg.get("country_fallback_pages") or cfg.get("max_country_pages") or 0),
                int(cfg.get("source_pack_country_fallback_pages") or 3),
            )
            cfg["page_size"] = max(int(cfg.get("page_size") or 0), 100)
            cfg["source_pack_targeted_backfill"] = True
    return cfg


def _official_source_names_for_rails(rails: list[str] | tuple[str, ...] | None = None) -> list[str]:
    selected = {str(rail or "").strip().lower() for rail in rails or [] if str(rail or "").strip()}
    names: list[str] = []
    for rail, source_names in ACTIVE_UNIVERSE_OFFICIAL_RAIL_SOURCES.items():
        if selected and rail not in selected:
            continue
        for source_name in source_names:
            if source_name not in names:
                names.append(source_name)
    return names


def _official_rails_for_source_names(source_names: list[str] | tuple[str, ...] | None = None) -> list[str]:
    requested = {str(name or "").strip() for name in source_names or [] if str(name or "").strip()}
    if not requested:
        return []
    rails: list[str] = []
    for rail, names in ACTIVE_UNIVERSE_OFFICIAL_RAIL_SOURCES.items():
        if any(name in requested for name in names):
            rails.append(rail)
    return rails


def _source_should_stop_after_one_backfill(source: Source) -> bool:
    cfg = _json_dict(source.config)
    return not bool(
        cfg.get("rotate_ciks")
        or cfg.get("rotate_corp_codes")
        or cfg.get("tracked_companies_per_run")
        or cfg.get("companies_per_run")
        or cfg.get("queries_per_run")
        or cfg.get("annual_company_limit_per_run")
        or cfg.get("stock_codes")
        or cfg.get("stock_ids")
        or cfg.get("stock_query_ids")
        or cfg.get("leis")
    )


def _latest_source_run_payload(session: Session, source_id: int) -> dict[str, Any] | None:
    run = session.query(IngestionRun).filter_by(source_id=int(source_id)).order_by(IngestionRun.id.desc()).first()
    if run is None:
        return None
    return {
        "id": int(run.id),
        "status": run.status,
        "finished_at": _dt(run.finished_at),
        "items_seen": int(run.items_seen or 0),
        "items_new": int(run.items_new or 0),
        "items_updated": int(run.items_updated or 0),
        "error": _short(run.error, 240) if run.error else None,
    }


def _source_cursor_payload(session: Session, source_id: int) -> dict[str, Any]:
    state = session.query(SourceState).filter_by(source_id=int(source_id)).first()
    return _json_dict(state.cursor) if state is not None else {}


def _is_retriable_source_backfill_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return (
        "deadlock detected" in text
        or "deadlockdetected" in text
        or "locknotavailable" in text
        or "lock timeout" in text
        or "canceling statement due to lock timeout" in text
        or "could not serialize access" in text
        or "serialization failure" in text
        or "infailedsqltransaction" in text
        or "current transaction is aborted" in text
        or "pendingrollbackerror" in text
    )


def _source_backfill_retry_delay_seconds(attempt: int) -> float:
    return min(8.0, 1.0 * (2 ** max(0, int(attempt) - 1)))


def _positive_int_config(cfg: dict[str, Any], key: str, default: int) -> int:
    try:
        return max(0, int(cfg.get(key) or default))
    except Exception:
        return max(0, int(default))


def _has_config_items(value: object) -> bool:
    if isinstance(value, (list, tuple, set)):
        return any(str(item or "").strip() for item in value)
    return bool(str(value or "").strip())


def _scale_source_config_to_runtime(connector: str, fetch_cfg: dict[str, Any], *, remaining_runtime: float | None) -> None:
    if remaining_runtime is None:
        return
    timeout_s = max(1.0, float(fetch_cfg.get("timeout_s") or 30.0))
    fetch_cfg["timeout_s"] = max(
        1.0,
        min(
            timeout_s,
            max(1.0, float(remaining_runtime) - _SOURCE_BACKFILL_FETCH_TIMEOUT_BUFFER_SECONDS),
        ),
    )
    timeout_s = max(1.0, float(fetch_cfg.get("timeout_s") or timeout_s))
    call_budget = max(
        1,
        int(max(1.0, float(remaining_runtime) - _SOURCE_BACKFILL_FETCH_TIMEOUT_BUFFER_SECONDS) // timeout_s),
    )
    if connector == "tdnet_company_announcements":
        seconds_per_query = float(fetch_cfg.get("source_pack_seconds_per_query") or 0)
        if seconds_per_query > 0:
            call_budget = max(
                call_budget,
                int(max(1.0, float(remaining_runtime) - _SOURCE_BACKFILL_FETCH_TIMEOUT_BUFFER_SECONDS) // seconds_per_query),
            )
        priority_budget = 0
        if _has_config_items(fetch_cfg.get("priority_queries")):
            existing_priority = _positive_int_config(
                fetch_cfg,
                "priority_queries_per_run",
                _positive_int_config(fetch_cfg, "queries_per_run", 2),
            )
            priority_budget = max(1, min(existing_priority, call_budget))
            fetch_cfg["priority_queries_per_run"] = priority_budget
        remaining_queries = max(0, call_budget - priority_budget)
        if _has_config_items(fetch_cfg.get("tracked_queries")):
            if remaining_queries <= 0:
                fetch_cfg["tracked_queries"] = []
                fetch_cfg["queries_per_run"] = 0
            else:
                existing_queries = _positive_int_config(fetch_cfg, "queries_per_run", 2)
                fetch_cfg["queries_per_run"] = max(1, min(existing_queries, remaining_queries))
    elif connector == "sec_edgar":
        seconds_per_cik = max(1.0, float(fetch_cfg.get("source_pack_seconds_per_cik") or 8.0))
        sec_call_budget = max(
            call_budget,
            int(max(1.0, float(remaining_runtime) - _SOURCE_BACKFILL_FETCH_TIMEOUT_BUFFER_SECONDS) // seconds_per_cik),
        )
        existing_cik_count = _positive_int_config(fetch_cfg, "max_ciks_per_run", sec_call_budget)
        cik_budget = max(1, min(existing_cik_count, sec_call_budget))
        fetch_cfg["max_ciks_per_run"] = cik_budget
        per_cik_limit = _positive_int_config(fetch_cfg, "source_pack_max_items_per_cik", 6)
        existing_items = _positive_int_config(fetch_cfg, "max_items_per_run", cik_budget * per_cik_limit)
        fetch_cfg["max_items_per_run"] = max(1, min(existing_items, cik_budget * max(1, per_cik_limit)))
    elif connector == "edinetdb_financials":
        company_budget = max(1, call_budget // 3)
        for key in (
            "companies_per_run",
            "tracked_companies_per_run",
            "priority_companies_per_run",
            "priority_queries_per_run",
            "tracked_codes_per_run",
        ):
            existing = _positive_int_config(fetch_cfg, key, company_budget)
            fetch_cfg[key] = max(1, min(existing, company_budget))
        for key in ("tracked_queries_per_run", "market_codes_per_run", "market_queries_per_run"):
            if key in fetch_cfg:
                fetch_cfg[key] = min(_positive_int_config(fetch_cfg, key, 0), company_budget)
    elif connector == "opendart_financials":
        existing_requests = _positive_int_config(fetch_cfg, "requests_per_run", call_budget)
        fetch_cfg["requests_per_run"] = max(1, min(existing_requests, call_budget))
        if "max_items_per_run" in fetch_cfg:
            existing_limit = _positive_int_config(fetch_cfg, "max_items_per_run", fetch_cfg["requests_per_run"])
            fetch_cfg["max_items_per_run"] = max(1, min(existing_limit, int(fetch_cfg["requests_per_run"])))
    elif connector == "opendart_disclosures":
        existing_pages = _positive_int_config(fetch_cfg, "max_pages", 1)
        fetch_cfg["max_pages"] = max(1, min(existing_pages, call_budget))
        if bool(fetch_cfg.get("rotate_corp_codes")):
            corp_budget = max(1, call_budget // int(fetch_cfg["max_pages"]))
            existing_corp_count = _positive_int_config(fetch_cfg, "max_corp_codes_per_run", corp_budget)
            fetch_cfg["max_corp_codes_per_run"] = max(1, min(existing_corp_count, corp_budget))


def _scale_prefetch_config_to_runtime(connector: str, fetch_cfg: dict[str, Any], *, remaining_runtime: float | None) -> None:
    _scale_source_config_to_runtime(connector, fetch_cfg, remaining_runtime=remaining_runtime)


def _source_backoff_skip_payload(session: Session, *, source_id: int, source_name: str) -> dict[str, Any] | None:
    state = session.query(SourceState).filter_by(source_id=int(source_id)).first()
    backoff_until = state.backoff_until if state is not None else None
    now = utcnow()
    if backoff_until is None or backoff_until <= now:
        return None
    return {
        "source_name": source_name,
        "source_id": int(source_id),
        "reason": "source_backoff",
        "backoff_until": _dt(backoff_until),
        "seconds_remaining": max(0, int((backoff_until - now).total_seconds())),
    }


def _set_source_backfill_postgres_timeouts(session: Session) -> None:
    bind = session.get_bind()
    if bind is None or bind.dialect.name != "postgresql":
        return
    session.execute(text("SET LOCAL lock_timeout = '15s'"))
    session.execute(text("SET LOCAL idle_in_transaction_session_timeout = '15min'"))
    session.execute(text("SET LOCAL statement_timeout = '240s'"))


def _merge_count_payload(target: dict[str, Any], payload: dict[str, Any]) -> None:
    for key, value in (payload or {}).items():
        if isinstance(value, bool):
            target[key] = int(target.get(key) or 0) + int(value)
            continue
        if isinstance(value, int):
            target[key] = int(target.get(key) or 0) + int(value)
        elif isinstance(value, float) and value.is_integer():
            target[key] = int(target.get(key) or 0) + int(value)
        elif key not in target:
            target[key] = value


def backfill_active_universe_official_source_packs(
    session: Session,
    *,
    universe_slug: str | None = None,
    rails: list[str] | tuple[str, ...] | None = None,
    source_names: list[str] | tuple[str, ...] | None = None,
    max_runs_per_source: int = 1,
    register_official_sources: bool = True,
    materialize_official_documents: bool = True,
    materialize_limit: int | None = None,
    refresh_source_packs: bool = True,
    include_audit: bool = True,
    limit: int | None = None,
    resolve_esef_leis: bool = True,
    esef_lei_query_budget: int = 80,
    max_runtime_seconds: int | None = None,
    run_transcript_scan: bool = False,
    transcript_scan_mode: str = "gap_check",
    transcript_scan_lookback_days: int | None = None,
    transcript_scan_max_candidates: int | None = None,
    transcript_scan_max_runtime_seconds: int | None = None,
) -> dict[str, Any]:
    """Run bounded official/API source backfills and refresh source packs.

    This is intentionally limited to official regulator/exchange/API rails by default. Transcript
    discovery can be run as an explicit post-step, and remains gated by official earnings material.
    """
    started_at = time.monotonic()
    runtime_budget = None if max_runtime_seconds is None else max(0.0, float(max_runtime_seconds))

    def _elapsed_seconds() -> float:
        return time.monotonic() - started_at

    def _runtime_exhausted() -> bool:
        return runtime_budget is not None and _elapsed_seconds() >= runtime_budget

    def _remaining_runtime_seconds() -> float | None:
        if runtime_budget is None:
            return None
        return max(0.0, runtime_budget - _elapsed_seconds())

    requested_names = [str(name or "").strip() for name in source_names or [] if str(name or "").strip()]
    selected_rails = {str(rail or "").strip().lower() for rail in rails or [] if str(rail or "").strip()}
    if requested_names and not selected_rails:
        selected_rails = set(_official_rails_for_source_names(requested_names)) or {"__none__"}

    allowed_names = set(_official_source_names_for_rails(rails))
    if requested_names:
        names = [name for name in requested_names if name in allowed_names]
        rejected_names = [name for name in requested_names if name not in allowed_names]
    else:
        names = [name for name in _official_source_names_for_rails(rails)]
        rejected_names = []

    rows: list[ResearchUniverseRow] | None = None
    active_company_ids: set[int] | None = None
    source_backfill_target_scopes: dict[str, dict[str, Any]] = {}

    def _load_rows() -> list[ResearchUniverseRow]:
        nonlocal rows
        if rows is None:
            loaded_rows = _active_universe_rows(session, universe_slug=universe_slug)
            if limit is not None:
                loaded_rows = loaded_rows[: max(1, int(limit))]
            rows = loaded_rows
        return rows

    def _active_company_ids_for_scope() -> set[int]:
        nonlocal active_company_ids
        if active_company_ids is not None:
            return active_company_ids
        if names:
            target_company_ids = {
                int(company_id)
                for target in source_backfill_target_scopes.values()
                for company_id in target.get("company_ids", [])
                if int(company_id) > 0
            }
            if target_company_ids:
                active_company_ids = target_company_ids
                return active_company_ids
            active_company_ids = _company_ids_from_source_configs(session, source_names=names)
            if active_company_ids:
                return active_company_ids
        active_company_ids = _active_company_ids_from_rows(_load_rows())
        return active_company_ids

    registration: dict[str, Any] = {}
    if register_official_sources:
        from thoth_terminal.investor_data_service import (
            ensure_active_universe_asx_sources,
            ensure_active_universe_bursa_sources,
            ensure_active_universe_canada_sources,
            ensure_active_universe_china_sources,
            ensure_active_universe_esef_sources,
            ensure_active_universe_hong_kong_sources,
            ensure_active_universe_japan_sources,
            ensure_active_universe_kazakhstan_sources,
            ensure_active_universe_nse_sources,
            ensure_active_universe_opendart_sources,
            ensure_active_universe_sec_sources,
            ensure_active_universe_sgx_sources,
            ensure_active_universe_taiwan_sources,
        )

        if (not selected_rails or "sec" in selected_rails) and not _runtime_exhausted():
            registration["sec"] = ensure_active_universe_sec_sources(session, universe_slug=universe_slug, limit=limit, reason="active_universe_source_pack_backfill")
        if (not selected_rails or "canada" in selected_rails) and not _runtime_exhausted():
            registration["canada"] = ensure_active_universe_canada_sources(session, universe_slug=universe_slug, limit=limit, reason="active_universe_source_pack_backfill")
        if (not selected_rails or "asx" in selected_rails) and not _runtime_exhausted():
            registration["asx"] = ensure_active_universe_asx_sources(session, universe_slug=universe_slug, limit=limit, reason="active_universe_source_pack_backfill")
        if (not selected_rails or "bursa" in selected_rails) and not _runtime_exhausted():
            registration["bursa"] = ensure_active_universe_bursa_sources(session, universe_slug=universe_slug, limit=limit, reason="active_universe_source_pack_backfill")
        if (not selected_rails or "kazakhstan" in selected_rails) and not _runtime_exhausted():
            registration["kazakhstan"] = ensure_active_universe_kazakhstan_sources(session, universe_slug=universe_slug, limit=limit, reason="active_universe_source_pack_backfill")
        if (not selected_rails or "sgx" in selected_rails) and not _runtime_exhausted():
            registration["sgx"] = ensure_active_universe_sgx_sources(session, universe_slug=universe_slug, limit=limit, reason="active_universe_source_pack_backfill")
        if (not selected_rails or "nse" in selected_rails) and not _runtime_exhausted():
            registration["nse"] = ensure_active_universe_nse_sources(session, universe_slug=universe_slug, limit=limit, reason="active_universe_source_pack_backfill")
        if (not selected_rails or "opendart" in selected_rails) and not _runtime_exhausted():
            registration["opendart"] = ensure_active_universe_opendart_sources(session, universe_slug=universe_slug, limit=limit, reason="active_universe_source_pack_backfill")
        if (not selected_rails or "japan" in selected_rails) and not _runtime_exhausted():
            registration["japan"] = ensure_active_universe_japan_sources(
                session,
                universe_slug=universe_slug,
                limit=limit,
                reason="active_universe_source_pack_backfill",
                refresh_official_edinet_code_list=True,
            )
        if (not selected_rails or "taiwan" in selected_rails) and not _runtime_exhausted():
            registration["taiwan"] = ensure_active_universe_taiwan_sources(session, universe_slug=universe_slug, limit=limit, reason="active_universe_source_pack_backfill")
        if (not selected_rails or "hkex" in selected_rails) and not _runtime_exhausted():
            registration["hkex"] = ensure_active_universe_hong_kong_sources(session, universe_slug=universe_slug, limit=limit, reason="active_universe_source_pack_backfill")
        if (not selected_rails or "china" in selected_rails) and not _runtime_exhausted():
            registration["china"] = ensure_active_universe_china_sources(session, universe_slug=universe_slug, limit=limit, reason="active_universe_source_pack_backfill")
        if (not selected_rails or "esef" in selected_rails) and not _runtime_exhausted():
            effective_esef_lei_query_budget = int(esef_lei_query_budget or 0)
            remaining_runtime = _remaining_runtime_seconds()
            if remaining_runtime is not None:
                # GLEIF lookups can each consume the entity resolver's 30s HTTP timeout.
                # Keep bounded source-pack backfills bounded by scaling LEI resolution to
                # the remaining runtime budget; existing LEIs still register with zero lookups.
                effective_esef_lei_query_budget = min(effective_esef_lei_query_budget, max(0, int(remaining_runtime // 35.0)))
            registration["esef"] = ensure_active_universe_esef_sources(
                session,
                universe_slug=universe_slug,
                limit=limit,
                reason="active_universe_source_pack_backfill",
                resolve_missing_leis=resolve_esef_leis,
                max_lei_resolution_queries=effective_esef_lei_query_budget,
            )
            registration["esef"]["requested_lei_query_budget"] = int(esef_lei_query_budget or 0)

    if registration and max_runs_per_source:
        session.flush()
        session.commit()

    source_runs: list[dict[str, Any]] = []
    skipped_sources: list[dict[str, Any]] = []
    run_budget = max(0, int(max_runs_per_source or 0))
    if run_budget > 0 and names and not _runtime_exhausted():
        source_backfill_target_scopes = _source_pack_backfill_target_scopes(
            session,
            universe_slug=universe_slug,
            source_names=names,
            limit=limit,
        )

    if run_budget > 0:
        from thoth_terminal.ingest import ingest_source

        for source_name in names:
            if _runtime_exhausted():
                skipped_sources.append({"source_name": source_name, "reason": "runtime_budget_exhausted"})
                continue
            source = session.query(Source).filter_by(name=source_name).first()
            if source is None:
                skipped_sources.append({"source_name": source_name, "reason": "source_missing"})
                continue
            if not _json_dict(source.config).get("connector"):
                skipped_sources.append({"source_name": source_name, "source_id": int(source.id), "reason": "missing_connector"})
                continue
            stop_after_one = _source_should_stop_after_one_backfill(source)
            source_id = int(source.id)
            source_display_name = str(source.name)
            for run_index in range(run_budget):
                if _runtime_exhausted():
                    skipped_sources.append({"source_name": source_display_name, "source_id": source_id, "reason": "runtime_budget_exhausted"})
                    break
                backoff_skip = _source_backoff_skip_payload(session, source_id=source_id, source_name=source_display_name)
                if backoff_skip is not None:
                    skipped_sources.append(backoff_skip)
                    break
                cursor_before = _source_cursor_payload(session, source_id)
                attempt = 0
                stop_source_runs = False
                last_retriable_error: str | None = None
                while True:
                    attempt += 1
                    try:
                        if attempt > 1:
                            # A failed flush can leave SQLAlchemy in pending-rollback state even
                            # after the exception is caught by this retry loop. Retry attempts
                            # start from a clean transaction so transient Postgres lock timeouts
                            # do not cascade into PendingRollbackError retries.
                            session.rollback()
                            session.close()
                        source = session.get(Source, source_id)
                        if source is None:
                            skipped_sources.append({"source_name": source_display_name, "source_id": source_id, "reason": "source_missing"})
                            stop_source_runs = True
                            break
                        prefetched_items = None
                        prefetched_error = None
                        prefetched_cursor = None
                        prefetched_stats = None
                        source_cfg = _json_dict(source.config)
                        target_scope = source_backfill_target_scopes.get(source_display_name)
                        source_cfg = _apply_source_pack_target_scope(
                            source_display_name,
                            source_cfg,
                            target_scope,
                        )
                        source_config_override = dict(source_cfg) if target_scope else None
                        connector = str(source_cfg.get("connector") or "").strip()
                        if bool(source_cfg.get("prefetch_outside_transaction")) or target_scope:
                            from thoth_terminal.connectors import fetch_items_result

                            state = session.query(SourceState).filter_by(source_id=source_id).first()
                            source_cursor = {} if target_scope else (_json_dict(state.cursor) if state is not None else {})
                            fetch_cfg = dict(source_cfg)
                            remaining_runtime = _remaining_runtime_seconds()
                            if (
                                remaining_runtime is not None
                                and remaining_runtime < _SOURCE_BACKFILL_PREFETCH_MIN_RUNTIME_SECONDS
                            ):
                                skipped_sources.append(
                                    {
                                        "source_name": source_display_name,
                                        "source_id": source_id,
                                        "reason": "insufficient_runtime_remaining",
                                        "remaining_runtime_seconds": round(float(remaining_runtime), 3),
                                    }
                                )
                                stop_source_runs = True
                                break
                            if remaining_runtime is not None:
                                existing_timeout = float(fetch_cfg.get("timeout_s") or 30.0)
                                fetch_cfg["timeout_s"] = max(
                                    1.0,
                                    min(
                                        existing_timeout,
                                        float(remaining_runtime) - _SOURCE_BACKFILL_PREFETCH_TIMEOUT_BUFFER_SECONDS,
                                    ),
                                )
                                _scale_prefetch_config_to_runtime(connector, fetch_cfg, remaining_runtime=remaining_runtime)
                            if connector == "web_list" and (state is None or state.last_success_at is None):
                                bootstrap_days = fetch_cfg.get("bootstrap_days")
                                if bootstrap_days and fetch_cfg.get("recent_days") is None:
                                    fetch_cfg["recent_days"] = bootstrap_days
                            session.rollback()
                            try:
                                fetched = fetch_items_result(
                                    connector,
                                    source_config=fetch_cfg,
                                    source_cursor=source_cursor,
                                )
                                prefetched_items = list(fetched.items or [])
                                prefetched_cursor = (
                                    None
                                    if target_scope
                                    else dict(fetched.cursor) if isinstance(fetched.cursor, dict) else None
                                )
                                prefetched_stats = dict(fetched.stats) if isinstance(fetched.stats, dict) else None
                            except Exception as exc:
                                prefetched_items = []
                                prefetched_error = exc
                            source = session.get(Source, source_id)
                            if source is None:
                                skipped_sources.append({"source_name": source_display_name, "source_id": source_id, "reason": "source_missing"})
                                stop_source_runs = True
                                break
                        else:
                            remaining_runtime = _remaining_runtime_seconds()
                            if (
                                remaining_runtime is not None
                                and remaining_runtime < _SOURCE_BACKFILL_FETCH_MIN_RUNTIME_SECONDS
                            ):
                                skipped_sources.append(
                                    {
                                        "source_name": source_display_name,
                                        "source_id": source_id,
                                        "reason": "insufficient_runtime_remaining",
                                        "remaining_runtime_seconds": round(float(remaining_runtime), 3),
                                    }
                                )
                                stop_source_runs = True
                                break
                            if remaining_runtime is not None:
                                source_config_override = dict(source_cfg)
                                _scale_source_config_to_runtime(
                                    connector,
                                    source_config_override,
                                    remaining_runtime=remaining_runtime,
                                )
                        _set_source_backfill_postgres_timeouts(session)
                        ingest_result = ingest_source(
                            session,
                            source,
                            source_config_override=source_config_override,
                            prefetched_items=prefetched_items,
                            prefetched_error=prefetched_error,
                            prefetched_cursor=prefetched_cursor,
                            prefetched_stats=prefetched_stats,
                            defer_derived_sync=bool(source_cfg.get("defer_ingest_derived_sync")),
                            defer_institutional_sync=True,
                        )
                        session.flush()
                        latest_run = _latest_source_run_payload(session, source_id)
                        cursor_after = _source_cursor_payload(session, source_id)
                        session.commit()
                        source_runs.append(
                            {
                                "source_name": source_display_name,
                                "source_id": source_id,
                                "run_index": run_index + 1,
                                "seen": int(getattr(ingest_result, "seen", 0) or 0),
                                "new": int(getattr(ingest_result, "new", 0) or 0),
                                "updated": int(getattr(ingest_result, "updated", 0) or 0),
                                "cursor_before": cursor_before,
                                "cursor_after": cursor_after,
                                "latest_run": latest_run,
                                "attempts": attempt,
                            }
                        )
                        if latest_run is not None and str(latest_run.get("status") or "").lower() != "success":
                            stop_source_runs = True
                            break
                        break
                    except Exception as exc:
                        error_text = f"{type(exc).__name__}: {_short(exc, 240)}"
                        try:
                            session.rollback()
                        finally:
                            session.close()
                        if attempt < _SOURCE_BACKFILL_RETRY_ATTEMPTS and _is_retriable_source_backfill_error(exc) and not _runtime_exhausted():
                            last_retriable_error = error_text
                            time.sleep(_source_backfill_retry_delay_seconds(attempt))
                            continue
                        if "PendingRollbackError:" in error_text and last_retriable_error:
                            error_text = f"{error_text}; previous_retriable_error={last_retriable_error}"
                        source_runs.append(
                            {
                                "source_name": source_display_name,
                                "source_id": source_id,
                                "run_index": run_index + 1,
                                "error": error_text,
                                "attempts": attempt,
                            }
                        )
                        stop_source_runs = True
                        break
                if stop_source_runs:
                    break
                if stop_after_one:
                    break

    official_materialization: dict[str, Any] = {}
    source_name_fetch_requested = bool(names and run_budget > 0)
    source_name_fetch_succeeded = any("error" not in run and "seen" in run for run in source_runs)
    if materialize_official_documents and _runtime_exhausted():
        official_materialization["skipped"] = True
        official_materialization["reason"] = "runtime_budget_exhausted"
    elif materialize_official_documents and source_name_fetch_requested and not source_name_fetch_succeeded:
        official_materialization["skipped"] = True
        official_materialization["reason"] = "source_name_scoped_no_successful_source_run"
    elif materialize_official_documents and not _runtime_exhausted():
        from thoth_terminal.foundation_service import bootstrap_company_rails_for_company_ids
        from thoth_terminal.investor_data_service import materialize_official_investor_documents

        try:
            _set_source_backfill_postgres_timeouts(session)
            official_materialization["investor_documents"] = materialize_official_investor_documents(
                session,
                company_ids=_active_company_ids_for_scope(),
                include_linked=True,
                source_names=names,
                limit=materialize_limit,
                max_runtime_seconds=_remaining_runtime_seconds(),
                sync_filing_dataset=False,
            )
            session.flush()
            session.commit()
        except Exception as exc:
            session.rollback()
            official_materialization["investor_documents"] = {
                "error": f"{type(exc).__name__}: {_short(exc, 240)}",
            }

        if requested_names:
            official_materialization["company_rails"] = {
                "skipped": True,
                "reason": "source_name_scoped_materialization",
            }
        else:
            company_rails_stats: dict[str, Any] = {
                "issuer_links": 0,
                "filings": 0,
                "ir_artifacts": 0,
                "earnings_events": 0,
                "chunks": 0,
                "errors": [],
            }
            company_ids_sorted = sorted(_active_company_ids_for_scope())
            chunk_size = 25
            for index in range(0, len(company_ids_sorted), chunk_size):
                if _runtime_exhausted():
                    company_rails_stats["skipped_reason"] = "runtime_budget_exhausted"
                    break
                chunk = company_ids_sorted[index : index + chunk_size]
                attempt = 0
                while True:
                    attempt += 1
                    try:
                        _set_source_backfill_postgres_timeouts(session)
                        stats = bootstrap_company_rails_for_company_ids(session, company_ids=set(chunk))
                        session.flush()
                        session.commit()
                        _merge_count_payload(company_rails_stats, stats)
                        company_rails_stats["chunks"] = int(company_rails_stats.get("chunks") or 0) + 1
                        break
                    except Exception as exc:
                        session.rollback()
                        if attempt < _SOURCE_BACKFILL_RETRY_ATTEMPTS and _is_retriable_source_backfill_error(exc) and not _runtime_exhausted():
                            time.sleep(_source_backfill_retry_delay_seconds(attempt))
                            continue
                        errors = company_rails_stats.setdefault("errors", [])
                        if isinstance(errors, list):
                            errors.append(
                                {
                                    "company_id_start": int(chunk[0]) if chunk else None,
                                    "company_id_end": int(chunk[-1]) if chunk else None,
                                    "attempts": attempt,
                                    "error": f"{type(exc).__name__}: {_short(exc, 240)}",
                                }
                            )
                        break
            official_materialization["company_rails"] = company_rails_stats
    source_pack_counts: Counter[str] = Counter()
    refreshed_subjects = 0
    source_pack_refresh_errors: list[dict[str, Any]] = []
    if refresh_source_packs and not _runtime_exhausted():
        if names:
            subject_ids = [
                int(subject_id)
                for (subject_id,) in session.query(ResearchSubject.id)
                .filter(ResearchSubject.company_id.in_(sorted(_active_company_ids_for_scope())))
                .all()
            ]
        else:
            subject_entries = active_universe_subjects(session, universe_slug=universe_slug, ensure=True, rows=_load_rows())
            subject_ids = sorted(subject_entries)
        for subject_id in sorted(set(subject_ids)):
            if _runtime_exhausted():
                break
            attempt = 0
            while True:
                attempt += 1
                try:
                    _set_source_backfill_postgres_timeouts(session)
                    pack = upsert_source_pack(
                        session,
                        subject_id=int(subject_id),
                        audit={"source": "active_universe_source_pack_backfill"},
                    )
                    status = str(pack.status or "missing")
                    session.flush()
                    session.commit()
                    source_pack_counts[status] += 1
                    refreshed_subjects += 1
                    break
                except Exception as exc:
                    session.rollback()
                    if attempt < _SOURCE_BACKFILL_RETRY_ATTEMPTS and _is_retriable_source_backfill_error(exc) and not _runtime_exhausted():
                        time.sleep(_source_backfill_retry_delay_seconds(attempt))
                        continue
                    source_pack_refresh_errors.append(
                        {
                            "subject_id": int(subject_id),
                            "attempts": attempt,
                            "error": f"{type(exc).__name__}: {_short(exc, 240)}",
                        }
                    )
                    break

    transcript_scan_result: dict[str, Any] | None = None
    transcript_scan_error: str | None = None
    transcript_scan_skipped_reason: str | None = None
    if run_transcript_scan:
        if _runtime_exhausted():
            transcript_scan_skipped_reason = "runtime_budget_exhausted"
        else:
            from thoth_terminal.transcript_watchlist import handle_watchlist_transcript_scan_job

            payload: dict[str, Any] = {
                "mode": str(transcript_scan_mode or "gap_check").strip().lower() or "gap_check",
                "force": True,
                "require_official_material": True,
            }
            if transcript_scan_lookback_days is not None:
                payload["lookback_days"] = max(1, int(transcript_scan_lookback_days))
            if transcript_scan_max_candidates is not None:
                payload["max_candidates"] = max(1, int(transcript_scan_max_candidates))
            remaining_runtime = _remaining_runtime_seconds()
            if transcript_scan_max_runtime_seconds is not None:
                payload["max_runtime_seconds"] = max(0, int(transcript_scan_max_runtime_seconds))
            if remaining_runtime is not None:
                remaining_s = max(0, int(remaining_runtime))
                if "max_runtime_seconds" in payload:
                    payload["max_runtime_seconds"] = min(int(payload["max_runtime_seconds"]), remaining_s)
                else:
                    payload["max_runtime_seconds"] = remaining_s
            try:
                transcript_scan_result = dict(handle_watchlist_transcript_scan_job(payload) or {})
            except Exception as exc:
                transcript_scan_error = f"{type(exc).__name__}: {_short(exc, 240)}"

    audit = (
        audit_active_universe_source_pack_coverage(
            session,
            universe_slug=universe_slug,
            ensure_subjects=False,
            refresh_source_packs=False,
            limit=limit,
            examples_limit=5,
        )
        if include_audit and not _runtime_exhausted()
        else None
    )
    session.flush()
    return {
        "ok": True,
        "universe_slug": universe_slug,
        "rows_scanned": len(rows) if rows is not None else 0,
        "active_company_count": len(_active_company_ids_for_scope()),
        "rails": list(rails or []),
        "source_names": names,
        "rejected_source_names": rejected_names,
        "max_runs_per_source": run_budget,
        "max_runtime_seconds": runtime_budget,
        "materialize_limit": materialize_limit,
        "runtime_seconds": round(_elapsed_seconds(), 3),
        "official_source_registration": registration,
        "source_runs": source_runs,
        "skipped_sources": skipped_sources,
        "targeted_backfill": {
            source_name: {
                "rail": str(target.get("rail") or ""),
                "company_count": len(target.get("company_ids") or []),
                "subject_count": len(target.get("subject_ids") or []),
                "missing_items": list(target.get("missing_items") or []),
                "identifier_counts": {
                    str(id_type): len(values or [])
                    for id_type, values in _json_dict(target.get("identifiers_by_type")).items()
                },
            }
            for source_name, target in source_backfill_target_scopes.items()
        },
        "official_materialization": official_materialization,
        "source_pack_refresh": {
            "subjects_refreshed": refreshed_subjects,
            "source_pack_counts": dict(source_pack_counts),
            "errors": source_pack_refresh_errors,
        },
        "audit": audit,
        "transcript_policy": {
            "ran_transcript_scan": transcript_scan_result is not None and transcript_scan_error is None,
            "reason": (
                "Transcript discovery ran with require_official_material=true after official source-pack backfill."
                if transcript_scan_result is not None and transcript_scan_error is None
                else "Transcript discovery remains gated until official earnings material exists; run this backfill with --run-transcript-scan or wait for the scheduled watchlist_transcript_scan."
            ),
            "transcript_scan": transcript_scan_result,
            "transcript_scan_error": transcript_scan_error,
            "transcript_scan_skipped_reason": transcript_scan_skipped_reason,
            "official_first_after_gate": [
                "local_ir_artifact_or_company_document",
                "issuer_specific_official_transcript_adapter",
                "issuer_official_call_material_page_after_gate",
                "derived_from_official_webcast_or_audio_when_available",
                "operator_uploaded_official_transcript_or_audio",
            ],
            "allowed_backups_after_gate": ["stockanalysis_live", "motley_fool_live"],
            "blocked_providers": ["quartr_api", "marketscreener_scrape"],
        },
    }


def latest_report_version_for_subject(session: Session, *, subject: ResearchSubject) -> tuple[ResearchReport | None, ResearchReportVersion | None]:
    report = session.query(ResearchReport).filter_by(subject_key=subject.subject_key).first()
    if report is None and subject.id is not None:
        report = session.query(ResearchReport).filter_by(subject_id=int(subject.id)).first()
    if report is None:
        return None, None
    version = (
        session.query(ResearchReportVersion)
        .filter_by(report_id=int(report.id), status="published")
        .order_by(ResearchReportVersion.version.desc(), ResearchReportVersion.id.desc())
        .first()
    )
    return report, version


def _required_sections_present(content_json: dict[str, Any]) -> tuple[bool, list[str]]:
    required = ["identity_freshness", "coverage_panel", "subject_facts", "primary_source_timeline"]
    missing = [key for key in required if not isinstance(content_json.get(key), dict)]
    return not missing, missing


def _operator_review_requires_revision(
    *,
    report: ResearchReport,
    version: ResearchReportVersion,
    coverage: dict[str, Any],
    content: dict[str, Any],
) -> tuple[bool, list[str], dict[str, Any]]:
    review_rows: list[dict[str, Any]] = []
    for raw in (
        _json_dict(getattr(report, "extra", None)).get("operator_review"),
        coverage.get("operator_review"),
        content.get("operator_review"),
    ):
        if isinstance(raw, dict):
            review_rows.append(dict(raw))
    status_values = {
        str(row.get("status") or "").strip().lower()
        for row in review_rows
        if str(row.get("status") or "").strip()
    }
    report_status = str(report.status or "").strip().lower()
    version_status = str(version.status or "").strip().lower()
    revision_statuses = {
        "needs_revision",
        "rejected",
        "rejected_v1_needs_revision",
        "requires_revision",
        "operator_rejected",
    }
    requires_revision = (
        report_status in revision_statuses
        or version_status in revision_statuses
        or bool(status_values.intersection(revision_statuses))
    )
    reasons: list[str] = []
    critique: list[str] = []
    if requires_revision:
        reasons.append("operator_rejected_v1" if "rejected_v1_needs_revision" in status_values else "operator_requires_revision")
        for row in review_rows:
            if row.get("summary"):
                critique.append(str(row.get("summary")))
            for item in row.get("critique") or []:
                if str(item).strip():
                    critique.append(str(item).strip())
        lowered = " ".join(critique).lower()
        if "template" in lowered or "templated" in lowered:
            reasons.append("over_templated")
        if "mechanic" in lowered or "bottleneck" in lowered:
            reasons.append("insufficient_topic_specific_mechanics")
    return requires_revision, reasons, {"reviews": review_rows, "critique": critique[:12]}


def evaluate_report_quality(
    session: Session,
    *,
    subject: ResearchSubject,
    stale_after_days: int = 120,
) -> dict[str, Any]:
    report, version = latest_report_version_for_subject(session, subject=subject)
    now = utcnow()
    source_pack = session.query(ResearchSourcePack).filter_by(subject_id=int(subject.id)).first()
    if source_pack is None:
        source_pack = upsert_source_pack(session, subject_id=int(subject.id), audit={"source": "quality_evaluator"})
    scenario = get_selected_canon_scenario(session, subject_id=int(subject.id))
    canon_card = session.query(AgentCanonCard).filter_by(subject_id=int(subject.id)).first()
    open_critical_gaps = (
        session.query(CoverageGap)
        .filter(
            CoverageGap.subject_id == int(subject.id),
            CoverageGap.status == "open",
            CoverageGap.gap_kind.in_(
                [
                    "missing_official_source_pack",
                    "missing_canon_scenario",
                    "missing_agent_canon_card",
                    "unresolved_universe_identity",
                    "duplicate_identity_candidate",
                ]
            ),
        )
        .count()
    )
    if report is None or version is None:
        return {
            "state": "missing",
            "score": 0,
            "reasons": ["missing_report"],
            "metrics": {
                "content_chars": 0,
                "required_sections_present": False,
                "source_pack_status": source_pack.status,
                "canon_scenario": bool(scenario),
                "agent_canon_card": bool(canon_card),
                "open_critical_gaps": int(open_critical_gaps),
            },
            "report_id": int(report.id) if report is not None else None,
            "report_version_id": None,
        }
    if str(version.status or "").lower() in {"failed", "error"} or version.error:
        return {
            "state": "failed" if str(version.status or "").lower() == "failed" else "error",
            "score": 0,
            "reasons": ["report_version_error"],
            "metrics": {"error": version.error, "content_chars": len(version.content_md or "")},
            "report_id": int(report.id),
            "report_version_id": int(version.id),
        }
    content = _json_dict(version.content_json)
    coverage = _json_dict(version.coverage_json)
    content_chars = len(str(version.content_md or "").strip())
    required_ok, missing_sections = _required_sections_present(content)
    primary_timeline = _json_dict(content.get("primary_source_timeline")).get("timeline") or []
    subject_facts = _json_dict(content.get("subject_facts"))
    financial_present = bool(subject_facts.get("financial_history") or subject_facts.get("metrics"))
    citations = len(primary_timeline)
    stale_basis = version.published_at or version.as_of_at or version.updated_at
    is_stale = bool(stale_basis and stale_basis <= now - timedelta(days=max(1, int(stale_after_days or 120))))
    coverage_gap_count = len(coverage.get("gaps") or []) if isinstance(coverage.get("gaps"), list) else 0
    requires_revision, operator_reasons, operator_review = _operator_review_requires_revision(
        report=report,
        version=version,
        coverage=coverage,
        content=content,
    )
    if requires_revision:
        return {
            "state": "minimum_viable" if content_chars >= 1800 and required_ok else "skeleton",
            "score": 45 if content_chars >= 1800 and required_ok else 20,
            "reasons": sorted(set(operator_reasons or ["operator_requires_revision"])),
            "metrics": {
                "content_chars": content_chars,
                "required_sections_present": required_ok,
                "missing_sections": missing_sections,
                "citation_count": len(primary_timeline),
                "source_pack_status": source_pack.status,
                "financial_snapshot": bool(financial_present),
                "canon_scenario": bool(scenario),
                "agent_canon_card": bool(canon_card),
                "open_critical_gaps": int(open_critical_gaps),
                "coverage_gap_count": int(coverage_gap_count),
                "operator_review": operator_review,
                "stale_after_days": int(stale_after_days),
                "stale_basis": _dt(stale_basis),
            },
            "report_id": int(report.id),
            "report_version_id": int(version.id),
        }
    reasons: list[str] = []
    score = 0
    if content_chars >= 12000:
        score += 25
    elif content_chars >= 6000:
        score += 20
    elif content_chars >= 1800:
        score += 10
    else:
        reasons.append("short_content")
    if required_ok:
        score += 20
    else:
        reasons.extend(f"missing_section:{key}" for key in missing_sections)
    if citations >= 8:
        score += 15
    elif citations >= 3:
        score += 8
    else:
        reasons.append("low_official_source_density")
    if source_pack.status == "complete":
        score += 15
    elif source_pack.status == "partial":
        score += 6
        reasons.append("partial_official_source_pack")
    elif source_pack.status != "not_required":
        reasons.append("missing_official_source_pack")
    else:
        score += 10
    if subject.kind in {"company", "issuer"} and not financial_present:
        reasons.append("missing_financial_snapshot")
    else:
        score += 10
    if scenario is not None:
        score += 10
    else:
        reasons.append("missing_canon_scenario")
    if canon_card is not None:
        score += 5
    else:
        reasons.append("missing_agent_canon_card")
    if open_critical_gaps:
        reasons.append("open_critical_gaps")
        score = max(0, score - min(20, int(open_critical_gaps) * 4))
    if coverage_gap_count:
        reasons.append("report_coverage_gaps")
    score = max(0, min(100, int(score)))
    if is_stale:
        state = "stale"
        reasons.append("stale_report")
    elif content_chars < 1800 or not required_ok:
        state = "skeleton"
    elif score >= 92 and source_pack.status in {"complete", "not_required"} and scenario is not None and canon_card is not None and not open_critical_gaps:
        state = "canon_ready"
    elif score >= 75 and required_ok and content_chars >= 6000:
        state = "analyst_grade"
    elif score >= 45 and content_chars >= 1800:
        state = "minimum_viable"
    else:
        state = "skeleton"
    return {
        "state": state,
        "score": score,
        "reasons": sorted(set(reasons)),
        "metrics": {
            "content_chars": content_chars,
            "required_sections_present": required_ok,
            "missing_sections": missing_sections,
            "citation_count": citations,
            "source_pack_status": source_pack.status,
            "financial_snapshot": bool(financial_present),
            "canon_scenario": bool(scenario),
            "agent_canon_card": bool(canon_card),
            "open_critical_gaps": int(open_critical_gaps),
            "coverage_gap_count": int(coverage_gap_count),
            "stale_after_days": int(stale_after_days),
            "stale_basis": _dt(stale_basis),
        },
        "report_id": int(report.id),
        "report_version_id": int(version.id),
    }


def upsert_report_quality(session: Session, *, subject_id: int, stale_after_days: int = 120) -> ResearchReportQuality:
    subject = session.get(ResearchSubject, int(subject_id))
    if subject is None:
        raise ValueError("Research subject not found.")
    evaluated = evaluate_report_quality(session, subject=subject, stale_after_days=stale_after_days)
    now = utcnow()
    row = session.query(ResearchReportQuality).filter_by(subject_id=int(subject.id)).first()
    if row is None:
        row = ResearchReportQuality(
            subject_id=int(subject.id),
            report_id=evaluated.get("report_id"),
            report_version_id=evaluated.get("report_version_id"),
            state=evaluated["state"],
            score=int(evaluated["score"]),
            reasons_json=list(evaluated["reasons"]),
            metrics_json=dict(evaluated["metrics"]),
            evaluated_at=now,
            stale_as_of_at=now if evaluated["state"] == "stale" else None,
            audit_json={"evaluator": "research_canon_service"},
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    else:
        row.report_id = evaluated.get("report_id")
        row.report_version_id = evaluated.get("report_version_id")
        row.state = evaluated["state"]
        row.score = int(evaluated["score"])
        row.reasons_json = list(evaluated["reasons"])
        row.metrics_json = dict(evaluated["metrics"])
        row.evaluated_at = now
        row.stale_as_of_at = now if evaluated["state"] == "stale" else None
        row.updated_at = now
    if evaluated.get("report_version_id"):
        version = session.get(ResearchReportVersion, int(evaluated["report_version_id"]))
        if version is not None:
            coverage = _json_dict(version.coverage_json)
            coverage["quality"] = {
                "state": evaluated["state"],
                "score": int(evaluated["score"]),
                "reasons": list(evaluated["reasons"]),
                "metrics": dict(evaluated["metrics"]),
                "evaluated_at": now.isoformat(),
            }
            version.coverage_json = coverage
            version.updated_at = now
    session.flush()
    return row


def serialize_quality(row: ResearchReportQuality | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": int(row.id),
        "subject_id": int(row.subject_id),
        "report_id": int(row.report_id) if row.report_id is not None else None,
        "report_version_id": int(row.report_version_id) if row.report_version_id is not None else None,
        "state": row.state,
        "score": int(row.score or 0),
        "reasons": list(row.reasons_json or []),
        "metrics": _json_dict(row.metrics_json),
        "evaluated_at": _dt(row.evaluated_at),
        "stale_as_of_at": _dt(row.stale_as_of_at),
    }


def _upsert_gap(
    session: Session,
    *,
    key: str,
    kind: str,
    title: str,
    summary: str,
    subject: ResearchSubject | None = None,
    severity: int = 5,
    rails: list[str] | None = None,
    payload: dict[str, Any] | None = None,
    open_gap: bool = True,
) -> CoverageGap:
    now = utcnow()
    row = session.query(CoverageGap).filter_by(gap_key=key).first()
    status = "open" if open_gap else "resolved"
    if row is None:
        row = CoverageGap(
            gap_key=key[:255],
            subject_id=int(subject.id) if subject is not None and subject.id is not None else None,
            title=title[:255],
            gap_kind=kind[:64],
            status=status,
            severity=max(1, min(int(severity or 5), 10)),
            summary=summary,
            suggested_rails=list(rails or ["research_canon"]),
            owner_kind="service",
            owner_ref="research_canon",
            payload=dict(payload or {}),
            created_at=now,
            updated_at=now,
            last_reviewed_at=now,
        )
        session.add(row)
    else:
        row.subject_id = int(subject.id) if subject is not None and subject.id is not None else row.subject_id
        row.title = title[:255]
        row.gap_kind = kind[:64]
        row.status = status
        row.severity = max(1, min(int(severity or row.severity or 5), 10))
        row.summary = summary
        row.suggested_rails = list(rails or row.suggested_rails or ["research_canon"])
        if not row.owner_ref or row.owner_ref == "research_canon":
            row.owner_kind = "service"
            row.owner_ref = "research_canon"
        existing = _json_dict(row.payload)
        existing.update(dict(payload or {}))
        row.payload = existing
        row.updated_at = now
        row.last_reviewed_at = now
    session.flush()
    return row


def _ensure_gap_request(session: Session, gap: CoverageGap, *, desired_outputs: list[str], priority: int = 5) -> None:
    payload = _json_dict(gap.payload)
    if payload.get("actionability") not in {None, "actionable"}:
        return
    draft_payload: dict[str, Any] = {"gap": {"id": int(gap.id), "gap_key": gap.gap_key, "gap_kind": gap.gap_kind}}
    if gap.gap_kind == "missing_official_source_pack":
        draft_payload.update(
            {
                "api_first": True,
                "missing_items": list(payload.get("missing_items") or []),
                "challenge_page_policy": "avoid challenge-prone issuer pages until an official API/feed or imported official material establishes the period",
                "transcript_gate": "only search transcript/call material after official/API earnings material exists",
            }
        )
    elif gap.gap_kind == "official_identifier_conflict":
        draft_payload.update(
            {
                "identity_repair_required": True,
                "identifier_conflict": _json_dict(payload.get("identifier_conflict")),
                "required_evidence": list(payload.get("required_evidence") or []),
                "repair_policy": "preview research-identity-repair and require official evidence links before apply",
            }
        )
    create_instrument_request(
        session,
        request_key=f"research-canon-gap:{_stable_hash(gap.gap_key, length=24)}",
        title=gap.title,
        intent="research_canon_gap",
        summary=gap.summary,
        target_entities=[str(payload.get("subject_display_name") or payload.get("instrument_display_name") or gap.title)],
        target_themes=[str(payload.get("universe_slug") or payload.get("gap_kind") or gap.gap_kind)],
        desired_outputs=desired_outputs,
        evidence_bundle=[{"gap_id": int(gap.id), "gap_key": gap.gap_key, "gap_kind": gap.gap_kind}],
        provenance_summary="Created by research canon maintenance scan.",
        evidence_tier="system_gap",
        status="draft",
        priority=priority,
        automation_source="research_canon",
        source_kind=gap.gap_kind,
        draft_payload=draft_payload,
        linked_objects={
            "gap_id": int(gap.id),
            "gap_key": gap.gap_key,
            "subject_id": int(gap.subject_id) if gap.subject_id is not None else None,
            "universe_id": payload.get("universe_id"),
            "instrument_id": payload.get("instrument_id"),
        },
    )


def serialize_coverage_gap(row: CoverageGap) -> dict[str, Any]:
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
        "owner_kind": row.owner_kind,
        "owner_ref": row.owner_ref,
        "payload": _json_dict(row.payload),
        "last_reviewed_at": _dt(row.last_reviewed_at),
        "updated_at": _dt(row.updated_at),
        "created_at": _dt(row.created_at),
    }


def list_coverage_gaps(
    session: Session,
    *,
    universe_slug: str | None = None,
    subject_id: int | None = None,
    gap_kind: str | None = None,
    status: str | None = None,
    owner_ref: str | None = "research_canon",
    limit: int = 100,
) -> dict[str, Any]:
    q = session.query(CoverageGap)
    if owner_ref:
        q = q.filter(CoverageGap.owner_ref == str(owner_ref))
    if subject_id is not None:
        q = q.filter(CoverageGap.subject_id == int(subject_id))
    if gap_kind:
        q = q.filter(CoverageGap.gap_kind == str(gap_kind))
    if status:
        q = q.filter(CoverageGap.status == str(status))
    slug = str(universe_slug or "").strip()
    if universe_slug:
        # Some durable gaps are intentionally keyed by instrument to avoid duplicate repair work
        # across universes. Filter in Python so payload.universe_slugs membership is respected.
        fetch_limit = 5000
    else:
        fetch_limit = max(1, min(int(limit or 100), 1000))
    rows = q.order_by(CoverageGap.status.asc(), CoverageGap.severity.desc(), CoverageGap.updated_at.desc()).limit(fetch_limit).all()
    if slug:
        filtered: list[CoverageGap] = []
        for row in rows:
            payload = _json_dict(row.payload)
            slugs = set(str(value) for value in _json_list(payload.get("universe_slug")) + _json_list(payload.get("universe_slugs")) if str(value).strip())
            if slug in slugs or f":universe:{slug}:" in str(row.gap_key or ""):
                filtered.append(row)
        rows = filtered[: max(1, min(int(limit or 100), 1000))]
    return {"count": len(rows), "gaps": [serialize_coverage_gap(row) for row in rows]}


def get_coverage_gap(session: Session, *, gap_id: int | None = None, gap_key: str | None = None) -> dict[str, Any] | None:
    row = None
    if gap_id is not None:
        row = session.get(CoverageGap, int(gap_id))
    if row is None and gap_key:
        row = session.query(CoverageGap).filter_by(gap_key=str(gap_key)).first()
    return serialize_coverage_gap(row) if row is not None else None


def _instrument_payload(instrument: ResearchInstrument) -> dict[str, Any]:
    return {
        "instrument_id": int(instrument.id),
        "instrument_key": instrument.instrument_key,
        "display_name": instrument.display_name,
        "ticker": instrument.ticker,
        "exchange": instrument.exchange,
        "yahoo_symbol": instrument.yahoo_symbol,
        "issuer_id": int(instrument.issuer_id) if instrument.issuer_id is not None else None,
        "company_id": int(instrument.company_id) if instrument.company_id is not None else None,
        "is_public": bool(instrument.is_public),
        "security_type": instrument.security_type,
    }


def _is_adr_like(instrument: ResearchInstrument) -> bool:
    hay = " ".join([instrument.display_name or "", instrument.legal_name or "", instrument.security_type or "", instrument.exchange or ""]).lower()
    return bool("adr" in hay or "ads" in hay or "pink" in hay or "otc" in hay or str(instrument.ticker or "").endswith("Y"))


def _subject_universe_payload(entry: dict[str, Any]) -> tuple[list[str], list[int]]:
    universes = list(entry.get("universes", {}).values())
    return [u.slug for u in universes if u is not None], [int(u.id) for u in universes if u is not None]


def scan_active_universe_coverage(
    session: Session,
    *,
    universe_slug: str | None = None,
    ensure_subjects: bool = True,
    create_requests: bool = False,
    create_cards: bool = False,
    stale_after_days: int = 120,
    limit: int | None = None,
) -> dict[str, Any]:
    rows = _active_universe_rows(session, universe_slug=universe_slug)
    if limit is not None:
        rows = rows[: max(1, int(limit))]
    now = utcnow()
    observed_gap_keys: set[str] = set()
    summary: dict[str, Any] = {
        "scanned_at": now.isoformat(),
        "universe_slug": universe_slug,
        "rows_scanned": len(rows),
        "unique_instruments": len({int(row.instrument_id) for row in rows}),
        "subjects_scanned": 0,
        "partial_scan": limit is not None,
        "gap_counts": {},
        "actionability_counts": {},
        "universe_counts": {},
        "quality_counts": {},
        "source_pack_counts": {},
        "created_or_updated_gap_ids": [],
        "resolved_gap_count": 0,
    }

    def record_gap(gap: CoverageGap) -> None:
        observed_gap_keys.add(gap.gap_key)
        summary["created_or_updated_gap_ids"].append(int(gap.id))
        payload = _json_dict(gap.payload)
        summary["gap_counts"][gap.gap_kind] = int(summary["gap_counts"].get(gap.gap_kind, 0)) + (1 if gap.status == "open" else 0)
        actionability = str(payload.get("actionability") or ("actionable" if gap.status == "open" else "resolved"))
        summary["actionability_counts"][actionability] = int(summary["actionability_counts"].get(actionability, 0)) + 1
        for slug in _json_list(payload.get("universe_slug")) + _json_list(payload.get("universe_slugs")):
            if not slug:
                continue
            bucket = summary["universe_counts"].setdefault(str(slug), {})
            bucket[gap.gap_kind] = int(bucket.get(gap.gap_kind, 0)) + (1 if gap.status == "open" else 0)

    instruments_by_id: dict[int, ResearchInstrument] = {}
    rows_by_universe: dict[str, list[ResearchUniverseRow]] = defaultdict(list)
    universes_by_instrument: dict[int, dict[int, ResearchUniverse]] = defaultdict(dict)
    for row in rows:
        if row.instrument is None or row.universe is None:
            continue
        instruments_by_id[int(row.instrument.id)] = row.instrument
        rows_by_universe[row.universe.slug].append(row)
        universes_by_instrument[int(row.instrument.id)][int(row.universe.id)] = row.universe

    identifier_conflict_instruments_seen: set[int] = set()
    for row in rows:
        instrument = row.instrument
        universe = row.universe
        if instrument is None or universe is None:
            continue
        missing_issuer = instrument.issuer_id is None
        missing_company = instrument.company_id is None
        gap = _upsert_gap(
            session,
            key=f"canon:identity:universe:{universe.slug}:instrument:{int(instrument.id)}:issuer_company_link",
            kind="unresolved_universe_identity",
            title=f"Resolve issuer/company link for {instrument.display_name}",
            summary="Active universe instrument is missing issuer_id and/or company_id, which blocks report subject reuse.",
            subject=None,
            severity=3 if missing_issuer and missing_company else 4,
            rails=["universes", "issuers", "companies", "instrument_requests"],
            payload={
                "scope": "active_universe",
                "actionability": "actionable",
                "universe_id": int(universe.id),
                "universe_slug": universe.slug,
                "instrument_id": int(instrument.id),
                "instrument_display_name": instrument.display_name,
                "missing_issuer_id": missing_issuer,
                "missing_company_id": missing_company,
                "instrument": _instrument_payload(instrument),
            },
            open_gap=missing_issuer or missing_company,
        )
        record_gap(gap)
        if create_requests and gap.status == "open":
            _ensure_gap_request(session, gap, desired_outputs=["issuer_company_resolution", "listing_dedupe_review"], priority=8)
        if bool(instrument.is_public):
            company = session.get(Company, int(instrument.company_id)) if instrument.company_id is not None else None
            issuer = session.get(Issuer, int(instrument.issuer_id)) if instrument.issuer_id is not None else None
            if issuer is None and company is not None and company.issuer_id is not None:
                issuer = session.get(Issuer, int(company.issuer_id))
            market, rail = _active_universe_market_bucket(instrument, company, issuer)
            identifier_conflict = _official_identifier_conflict_status(
                session,
                rail=rail,
                instrument=instrument,
                company=company,
                issuer=issuer,
            )
            if identifier_conflict is not None and int(instrument.id) not in identifier_conflict_instruments_seen:
                identifier_conflict_instruments_seen.add(int(instrument.id))
                universe_refs = sorted(
                    universes_by_instrument.get(int(instrument.id), {}).values(),
                    key=lambda item: (item.sort_order, item.slug),
                )
                conflict_gap = _upsert_gap(
                    session,
                    key=f"canon:identity:instrument:{int(instrument.id)}:official_identifier_conflict",
                    kind="official_identifier_conflict",
                    title=f"Resolve official identifier conflict for {instrument.display_name}",
                    summary="An active universe instrument's official rail identifier is already bound to a different issuer/company, blocking reliable official source-pack attribution.",
                    subject=_subject_for_instrument(session, instrument, ensure=False),
                    severity=8,
                    rails=["issuer_identity", "official_sources", "instrument_requests"],
                    payload={
                        "scope": "active_universe",
                        "actionability": "actionable",
                        "universe_id": int(universe.id),
                        "universe_slug": universe.slug,
                        "universe_ids": [int(item.id) for item in universe_refs],
                        "universe_slugs": [item.slug for item in universe_refs],
                        "market": market,
                        "rail": rail,
                        "instrument_id": int(instrument.id),
                        "instrument_display_name": instrument.display_name,
                        "instrument": _instrument_payload(instrument),
                        "issuer": _issuer_payload(issuer),
                        "company": _company_payload(company),
                        "identifier_conflict": identifier_conflict,
                        "required_evidence": [
                            "official issuer/exchange page confirming the listing",
                            "official filing/regulator identifier or exchange code page",
                            "operator-reviewed research-identity-repair preview before applying relink",
                        ],
                    },
                    open_gap=True,
                )
                record_gap(conflict_gap)
                if create_requests and conflict_gap.status == "open":
                    _ensure_gap_request(
                        session,
                        conflict_gap,
                        desired_outputs=["identity_repair_preview", "official_identifier_evidence"],
                        priority=9,
                    )

    for slug, universe_rows in rows_by_universe.items():
        instruments = [row.instrument for row in universe_rows if row.instrument is not None]
        by_issuer_or_company: dict[str, list[ResearchInstrument]] = defaultdict(list)
        by_name: dict[str, list[ResearchInstrument]] = defaultdict(list)
        for instrument in instruments:
            if instrument.issuer_id is not None:
                by_issuer_or_company[f"issuer:{int(instrument.issuer_id)}"].append(instrument)
            elif instrument.company_id is not None:
                by_issuer_or_company[f"company:{int(instrument.company_id)}"].append(instrument)
            name_key = _norm_name(instrument.legal_name or instrument.display_name)
            if name_key:
                by_name[name_key].append(instrument)
        for key, group in by_issuer_or_company.items():
            public_group = [item for item in group if item.is_public]
            if len(public_group) < 2 or not any(_is_adr_like(item) for item in public_group):
                continue
            non_adr = [item for item in public_group if not _is_adr_like(item)]
            gap = _upsert_gap(
                session,
                key=f"canon:identity:universe:{slug}:{key}:adr_local_duplicate",
                kind="adr_local_duplicate_candidate",
                title=f"Review ADR/local listing pair for {public_group[0].display_name}",
                summary="Multiple public instruments map to the same issuer/company and include ADR-like/local-listing candidates.",
                severity=4,
                rails=["universes", "issuer_identity", "instrument_requests"],
                payload={
                    "scope": "active_universe",
                    "actionability": "actionable",
                    "universe_slug": slug,
                    "identity_key": key,
                    "adr_like_count": len(public_group) - len(non_adr),
                    "candidates": [_instrument_payload(item) for item in public_group],
                },
                open_gap=True,
            )
            record_gap(gap)
        for name_key, group in by_name.items():
            issuer_ids = sorted({int(item.issuer_id) for item in group if item.issuer_id is not None})
            company_ids = sorted({int(item.company_id) for item in group if item.company_id is not None})
            if len(issuer_ids) > 1:
                gap = _upsert_gap(
                    session,
                    key=f"canon:identity:universe:{slug}:name:{_stable_hash([name_key, issuer_ids])}:separate_issuers",
                    kind="same_company_separate_issuers",
                    title=f"Review separate issuers for {group[0].display_name}",
                    summary="Active universe instruments have the same normalized company name but separate issuer ids.",
                    severity=4,
                    rails=["issuer_identity", "universes"],
                    payload={
                        "scope": "active_universe",
                        "actionability": "actionable",
                        "universe_slug": slug,
                        "normalized_name": name_key,
                        "issuer_ids": issuer_ids,
                        "company_ids": company_ids,
                        "candidates": [_instrument_payload(item) for item in group],
                    },
                    open_gap=True,
                )
                record_gap(gap)
            private_group = [item for item in group if not item.is_public]
            public_group = [item for item in group if item.is_public]
            if private_group and public_group:
                gap = _upsert_gap(
                    session,
                    key=f"canon:identity:universe:{slug}:name:{_stable_hash([name_key, [i.id for i in group]])}:private_public",
                    kind="private_company_public_listing_review",
                    title=f"Review private/public listing overlap for {group[0].display_name}",
                    summary="A private universe instrument appears to overlap with a public listing candidate.",
                    severity=5,
                    rails=["issuer_identity", "universes"],
                    payload={
                        "scope": "active_universe",
                        "actionability": "actionable",
                        "universe_slug": slug,
                        "normalized_name": name_key,
                        "private_candidates": [_instrument_payload(item) for item in private_group],
                        "public_candidates": [_instrument_payload(item) for item in public_group],
                    },
                    open_gap=True,
                )
                record_gap(gap)

    subject_entries = active_universe_subjects(session, universe_slug=universe_slug, ensure=ensure_subjects, rows=rows)
    summary["subjects_scanned"] = len(subject_entries)
    for subject_id, entry in subject_entries.items():
        subject = entry["subject"]
        universe_slugs, universe_ids = _subject_universe_payload(entry)
        if create_cards:
            generate_agent_canon_card(session, subject_id=int(subject_id))
        source_pack = upsert_source_pack(session, subject_id=int(subject_id), audit={"source": "active_universe_scan"})
        summary["source_pack_counts"][source_pack.status] = int(summary["source_pack_counts"].get(source_pack.status, 0)) + 1
        quality = upsert_report_quality(session, subject_id=int(subject_id), stale_after_days=stale_after_days)
        q_state = quality.state
        summary["quality_counts"][q_state] = int(summary["quality_counts"].get(q_state, 0)) + 1
        report, version = latest_report_version_for_subject(session, subject=subject)
        scenario = get_selected_canon_scenario(session, subject_id=int(subject_id))
        card = session.query(AgentCanonCard).filter_by(subject_id=int(subject_id)).first()
        subject_payload = {
            "scope": "active_universe",
            "actionability": "actionable",
            "subject_id": int(subject_id),
            "subject_key": subject.subject_key,
            "subject_display_name": subject.display_name,
            "universe_ids": universe_ids,
            "universe_slugs": universe_slugs,
            "report_id": int(report.id) if report is not None else None,
            "report_version_id": int(version.id) if version is not None else None,
            "quality_state": quality.state,
            "quality_score": int(quality.score),
        }
        gap = _upsert_gap(
            session,
            key=f"coverage_gap:subject:{int(subject_id)}:research_report",
            kind="missing_research_report",
            title=f"Research report missing for {subject.display_name}",
            summary="Active universe subject has no published research report.",
            subject=subject,
            severity=2,
            rails=["reports", "research_campaign", "codex"],
            payload=subject_payload,
            open_gap=report is None or version is None,
        )
        record_gap(gap)
        gap = _upsert_gap(
            session,
            key=f"canon:coverage:subject:{int(subject_id)}:report_quality",
            kind="skeleton_or_short_report",
            title=f"Report quality not ready for {subject.display_name}",
            summary="Latest report exists but is classified as skeleton or only minimum viable.",
            subject=subject,
            severity=4,
            rails=["reports", "research_campaign", "quality_evaluator"],
            payload={**subject_payload, "quality": serialize_quality(quality)},
            open_gap=quality.state in {"skeleton", "minimum_viable"},
        )
        record_gap(gap)
        gap = _upsert_gap(
            session,
            key=f"canon:coverage:subject:{int(subject_id)}:stale_report",
            kind="stale_research_report",
            title=f"Research report stale for {subject.display_name}",
            summary="Latest report is beyond the configured stale window.",
            subject=subject,
            severity=5,
            rails=["reports", "freshness", "research_campaign"],
            payload={**subject_payload, "quality": serialize_quality(quality)},
            open_gap=quality.state == "stale",
        )
        record_gap(gap)
        source_pack_missing = source_pack.status not in {"complete", "not_required"}
        gap = _upsert_gap(
            session,
            key=f"canon:coverage:subject:{int(subject_id)}:official_source_pack",
            kind="missing_official_source_pack",
            title=f"Official source pack incomplete for {subject.display_name}",
            summary="Public company/issuer subject is missing one or more required official source-pack items.",
            subject=subject,
            severity=3,
            rails=["official_sources", "filings", "ir_artifacts", "instrument_requests"],
            payload={**subject_payload, "source_pack": serialize_source_pack(source_pack), "missing_items": list(source_pack.missing_items_json or [])},
            open_gap=source_pack_missing,
        )
        record_gap(gap)
        if create_requests and gap.status == "open":
            _ensure_gap_request(session, gap, desired_outputs=["official_source_pack"], priority=8)
        gap = _upsert_gap(
            session,
            key=f"canon:coverage:subject:{int(subject_id)}:canon_scenario",
            kind="missing_canon_scenario",
            title=f"Canon scenario missing for {subject.display_name}",
            summary="Subject has no selected canon scenario object.",
            subject=subject,
            severity=4,
            rails=["scenarios", "research_campaign"],
            payload={**subject_payload},
            open_gap=scenario is None,
        )
        record_gap(gap)
        gap = _upsert_gap(
            session,
            key=f"canon:coverage:subject:{int(subject_id)}:agent_canon_card",
            kind="missing_agent_canon_card",
            title=f"Agent canon card missing for {subject.display_name}",
            summary="Subject has no compact canonical memory card for repeated local-agent context.",
            subject=subject,
            severity=4,
            rails=["agent_canon_cards", "local_agent"],
            payload={**subject_payload},
            open_gap=card is None,
        )
        record_gap(gap)

    if limit is None:
        stale_q = session.query(CoverageGap).filter(
            CoverageGap.owner_ref == "research_canon",
            CoverageGap.status == "open",
            CoverageGap.gap_kind.in_(sorted(ACTIVE_UNIVERSE_GAP_KINDS)),
        )
        if universe_slug:
            stale_q = stale_q.filter(CoverageGap.gap_key.like(f"canon:%:universe:{universe_slug}:%"))
        for gap in stale_q.limit(5000).all():
            if gap.gap_key not in observed_gap_keys:
                payload = _json_dict(gap.payload)
                payload["resolved_by"] = "research_canon_maintenance_absent_from_latest_complete_scan"
                payload["resolved_at"] = now.isoformat()
                gap.payload = payload
                gap.status = "resolved"
                gap.updated_at = now
                gap.last_reviewed_at = now
                summary["resolved_gap_count"] = int(summary.get("resolved_gap_count", 0)) + 1
    gap_ids = sorted(set(summary["created_or_updated_gap_ids"]))
    summary["created_or_updated_gap_count"] = len(gap_ids)
    summary["created_or_updated_gap_id_sample"] = gap_ids[:20]
    summary.pop("created_or_updated_gap_ids", None)
    summary["open_gap_count"] = int(session.query(CoverageGap).filter(CoverageGap.status == "open", CoverageGap.owner_ref == "research_canon").count())
    session.flush()
    return summary


def generate_agent_canon_card(session: Session, *, subject_id: int) -> AgentCanonCard:
    subject = session.get(ResearchSubject, int(subject_id))
    if subject is None:
        raise ValueError("Research subject not found.")
    report, version = latest_report_version_for_subject(session, subject=subject)
    source_pack = session.query(ResearchSourcePack).filter_by(subject_id=int(subject.id)).first()
    if source_pack is None:
        source_pack = upsert_source_pack(session, subject_id=int(subject.id), audit={"source": "agent_card"})
    scenario = get_selected_canon_scenario(session, subject_id=int(subject.id))
    quality = session.query(ResearchReportQuality).filter_by(subject_id=int(subject.id)).first()
    universe_rows = (
        session.query(ResearchUniverse)
        .join(ResearchUniverseRow, ResearchUniverseRow.universe_id == ResearchUniverse.id)
        .join(ResearchInstrument, ResearchInstrument.id == ResearchUniverseRow.instrument_id)
        .filter(ResearchUniverse.status == "active", ResearchUniverseRow.archived_at.is_(None))
    )
    filters = []
    if subject.issuer_id is not None:
        filters.append(ResearchInstrument.issuer_id == int(subject.issuer_id))
    if subject.company_id is not None:
        filters.append(ResearchInstrument.company_id == int(subject.company_id))
    if filters:
        universe_rows = universe_rows.filter(or_(*filters))
        universes = sorted({row.slug for row in universe_rows.all()})
    else:
        universes = []
    gaps = (
        session.query(CoverageGap)
        .filter(CoverageGap.subject_id == int(subject.id), CoverageGap.status == "open")
        .order_by(CoverageGap.severity.desc(), CoverageGap.updated_at.desc())
        .limit(8)
        .all()
    )
    content = {
        "subject": {
            "id": int(subject.id),
            "subject_key": subject.subject_key,
            "slug": subject.slug,
            "display_name": subject.display_name,
            "kind": subject.kind,
            "issuer_id": int(subject.issuer_id) if subject.issuer_id is not None else None,
            "company_id": int(subject.company_id) if subject.company_id is not None else None,
        },
        "universe_memberships": universes,
        "current_canon_scenario": serialize_scenario(scenario),
        "short_thesis": scenario.thesis_summary if scenario is not None else None,
        "key_metrics": _json_dict(scenario.key_metrics_json) if scenario is not None else {},
        "high_confidence_claims": [],
        "source_pack_status": serialize_source_pack(source_pack),
        "report_quality": serialize_quality(quality),
        "open_gaps": [
            {"gap_key": gap.gap_key, "gap_kind": gap.gap_kind, "severity": int(gap.severity or 0), "summary": gap.summary}
            for gap in gaps
        ],
        "do_not_overwrite_caveats": [
            "Use this card as compact context, not as authority to overwrite source reports or durable notes.",
            "Promote changes through report/scenario/source-pack rails with evidence links.",
        ],
        "last_reviewed_timestamp": _dt((quality.evaluated_at if quality is not None else None) or (version.published_at if version is not None else None)),
    }
    lines = [
        f"# Agent Canon Card - {subject.display_name}",
        f"- Subject: {subject.subject_key} ({subject.kind})",
        f"- Universes: {', '.join(universes) if universes else 'none'}",
        f"- Scenario: {(scenario.name if scenario else 'missing')}",
        f"- Source pack: {source_pack.status}",
        f"- Report quality: {(quality.state if quality else 'unknown')}",
        f"- Open gaps: {len(gaps)}",
    ]
    if scenario is not None and scenario.thesis_summary:
        lines.append(f"- Thesis: {_short(scenario.thesis_summary, 500)}")
    for gap in gaps[:5]:
        lines.append(f"- Gap: {gap.gap_kind} - {_short(gap.summary, 180)}")
    content_md = "\n".join(lines).strip() + "\n"
    now = utcnow()
    row = session.query(AgentCanonCard).filter_by(subject_id=int(subject.id)).first()
    if row is None:
        row = AgentCanonCard(
            subject_id=int(subject.id),
            card_key=f"agent-canon-card:subject:{int(subject.id)}",
            status="active",
            content_json=content,
            content_md=content_md,
            source_report_id=int(report.id) if report is not None else None,
            source_report_version_id=int(version.id) if version is not None else None,
            scenario_id=int(scenario.id) if scenario is not None else None,
            source_pack_id=int(source_pack.id),
            token_estimate=max(1, len(content_md) // 4),
            audit_json={"generator": "research_canon_service"},
            generated_at=now,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    else:
        row.status = "active"
        row.content_json = content
        row.content_md = content_md
        row.source_report_id = int(report.id) if report is not None else None
        row.source_report_version_id = int(version.id) if version is not None else None
        row.scenario_id = int(scenario.id) if scenario is not None else None
        row.source_pack_id = int(source_pack.id)
        row.token_estimate = max(1, len(content_md) // 4)
        row.generated_at = now
        row.updated_at = now
    session.flush()
    return row


def get_agent_canon_card(session: Session, *, subject_id: int) -> dict[str, Any] | None:
    row = session.query(AgentCanonCard).filter_by(subject_id=int(subject_id)).first()
    if row is None:
        return None
    return {
        "id": int(row.id),
        "subject_id": int(row.subject_id),
        "card_key": row.card_key,
        "status": row.status,
        "content": _json_dict(row.content_json),
        "content_md": row.content_md,
        "source_report_id": int(row.source_report_id) if row.source_report_id is not None else None,
        "source_report_version_id": int(row.source_report_version_id) if row.source_report_version_id is not None else None,
        "scenario_id": int(row.scenario_id) if row.scenario_id is not None else None,
        "source_pack_id": int(row.source_pack_id) if row.source_pack_id is not None else None,
        "token_estimate": int(row.token_estimate or 0),
        "generated_at": _dt(row.generated_at),
    }


def research_report_fingerprint_context(session: Session, *, subject_id: int) -> dict[str, Any]:
    """Stable canon context for report fingerprints.

    Agent canon cards are deliberately excluded because report publish regenerates them,
    and including generated card timestamps would create report churn.
    """
    source_pack = session.query(ResearchSourcePack).filter_by(subject_id=int(subject_id)).first()
    scenario = get_selected_canon_scenario(session, subject_id=int(subject_id))
    return {
        "official_source_pack": stable_source_pack_payload(source_pack),
        "canon_scenario": stable_scenario_payload(scenario),
    }


def refresh_subject_canon_state(
    session: Session,
    *,
    subject_id: int,
    stale_after_days: int = 120,
    generate_card: bool = True,
) -> dict[str, Any]:
    source_pack = upsert_source_pack(session, subject_id=int(subject_id), audit={"source": "canon_state_refresh"})
    quality = upsert_report_quality(session, subject_id=int(subject_id), stale_after_days=stale_after_days)
    card = generate_agent_canon_card(session, subject_id=int(subject_id)) if generate_card else None
    # Re-evaluate after card generation so canon_ready gating sees the compact context.
    quality = upsert_report_quality(session, subject_id=int(subject_id), stale_after_days=stale_after_days)
    return {
        "source_pack": serialize_source_pack(source_pack),
        "report_quality": serialize_quality(quality),
        "agent_canon_card": get_agent_canon_card(session, subject_id=int(subject_id)) if card is not None else None,
    }


def post_publish_report_canon_refresh(
    session: Session,
    *,
    report: ResearchReport,
    version: ResearchReportVersion,
    stale_after_days: int = 120,
) -> dict[str, Any]:
    subject_id = int(report.subject_id) if report.subject_id is not None else None
    if subject_id is None:
        return {"skipped": True, "reason": "report_has_no_research_subject"}
    state = refresh_subject_canon_state(session, subject_id=subject_id, stale_after_days=stale_after_days, generate_card=True)
    coverage = _json_dict(version.coverage_json)
    coverage["quality"] = state.get("report_quality")
    coverage["source_pack_status"] = (state.get("source_pack") or {}).get("status")
    coverage["agent_canon_card_id"] = ((state.get("agent_canon_card") or {}).get("id"))
    version.coverage_json = coverage
    version.updated_at = utcnow()
    session.flush()
    return {"skipped": False, **state}


def preview_campaign_from_universe(
    session: Session,
    *,
    universe_slug: str,
    limit: int | None = None,
) -> dict[str, Any]:
    entries = active_universe_subjects(session, universe_slug=universe_slug, ensure=False)
    items = []
    for subject_id, entry in entries.items():
        subject = entry["subject"]
        quality = session.query(ResearchReportQuality).filter_by(subject_id=int(subject_id)).first()
        gaps = (
            session.query(CoverageGap)
            .filter(CoverageGap.subject_id == int(subject_id), CoverageGap.status == "open")
            .order_by(CoverageGap.severity.desc(), CoverageGap.updated_at.desc())
            .limit(10)
            .all()
        )
        priority = 5
        if quality is None or quality.state == "missing":
            priority = 9
        elif quality.state in {"skeleton", "minimum_viable"}:
            priority = 8
        elif quality.state == "stale":
            priority = 7
        elif quality.state == "analyst_grade":
            priority = 4
        elif quality.state == "canon_ready":
            priority = 2
        blocking_gaps = [
            {"gap_key": gap.gap_key, "gap_kind": gap.gap_kind, "severity": int(gap.severity or 0)}
            for gap in gaps
            if _is_campaign_blocking_gap(gap.gap_kind, assigned_rail="reports")
        ]
        items.append(
            {
                "target_type": "subject",
                "target_id": int(subject_id),
                "target_key": subject.subject_key,
                "title": subject.display_name,
                "priority": priority,
                "quality_state": quality.state if quality is not None else "missing",
                "blocking_gaps": blocking_gaps,
            }
        )
    items.sort(key=lambda item: (-int(item["priority"]), str(item["title"]).lower()))
    if limit is not None:
        items = items[: max(1, int(limit))]
    return {"universe_slug": universe_slug, "item_count": len(items), "items": items}


def create_campaign_from_universe(
    session: Session,
    *,
    universe_slug: str,
    name: str | None = None,
    campaign_key: str | None = None,
    limit: int | None = None,
    preview: bool = False,
) -> dict[str, Any]:
    preview_payload = preview_campaign_from_universe(session, universe_slug=universe_slug, limit=limit)
    if preview:
        return {"preview": True, **preview_payload}
    universe = session.query(ResearchUniverse).filter_by(slug=universe_slug).first()
    if universe is None:
        raise ValueError("Universe not found.")
    now = utcnow()
    key = campaign_key or f"campaign:universe:{universe.slug}:analyst-grade"
    campaign = session.query(ResearchCampaign).filter_by(campaign_key=key).first()
    if campaign is None:
        campaign = ResearchCampaign(
            campaign_key=key,
            name=name or f"Cover {universe.name}",
            description=f"Build analyst-grade coverage for active universe {universe.name}.",
            status="draft",
            universe_id=int(universe.id),
            parameters_json={"universe_slug": universe.slug, "limit": limit},
            created_at=now,
            updated_at=now,
        )
        session.add(campaign)
        session.flush()
    else:
        campaign.name = name or campaign.name
        campaign.universe_id = int(universe.id)
        params = _json_dict(campaign.parameters_json)
        params.update({"universe_slug": universe.slug, "limit": limit})
        campaign.parameters_json = params
        campaign.updated_at = now
    created = 0
    updated = 0
    for item in preview_payload["items"]:
        item_key = f"campaign:{int(campaign.id)}:{item['target_type']}:{item['target_key']}"
        row = (
            session.query(ResearchCampaignItem)
            .filter_by(campaign_id=int(campaign.id), target_type=item["target_type"], target_key=item["target_key"])
            .first()
        )
        status = "blocked" if item.get("blocking_gaps") else "queued"
        if row is None:
            row = ResearchCampaignItem(
                campaign_id=int(campaign.id),
                item_key=item_key,
                target_type=item["target_type"],
                target_id=int(item["target_id"]) if item.get("target_id") is not None else None,
                target_key=item["target_key"],
                subject_id=int(item["target_id"]) if item["target_type"] == "subject" and item.get("target_id") is not None else None,
                universe_id=int(universe.id),
                priority=int(item["priority"]),
                status=status,
                blocking_gaps_json=list(item.get("blocking_gaps") or []),
                assigned_rail="reports",
                attempt_count=0,
                quality_json={"state": item.get("quality_state")},
                payload_json={"title": item.get("title")},
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            created += 1
        else:
            row.priority = int(item["priority"])
            if row.status in {"queued", "blocked"}:
                row.status = status
            row.blocking_gaps_json = list(item.get("blocking_gaps") or [])
            row.quality_json = {"state": item.get("quality_state")}
            row.updated_at = now
            updated += 1
    session.flush()
    return {
        "preview": False,
        "campaign": serialize_campaign(campaign),
        "item_count": len(preview_payload["items"]),
        "created_items": created,
        "updated_items": updated,
    }


def serialize_campaign(row: ResearchCampaign) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "campaign_key": row.campaign_key,
        "name": row.name,
        "description": row.description,
        "status": row.status,
        "universe_id": int(row.universe_id) if row.universe_id is not None else None,
        "parameters": _json_dict(row.parameters_json),
        "created_at": _dt(row.created_at),
        "updated_at": _dt(row.updated_at),
    }


def serialize_campaign_item(row: ResearchCampaignItem) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "campaign_id": int(row.campaign_id),
        "item_key": row.item_key,
        "target_type": row.target_type,
        "target_id": int(row.target_id) if row.target_id is not None else None,
        "target_key": row.target_key,
        "subject_id": int(row.subject_id) if row.subject_id is not None else None,
        "universe_id": int(row.universe_id) if row.universe_id is not None else None,
        "priority": int(row.priority or 0),
        "status": row.status,
        "blocking_gaps": list(row.blocking_gaps_json or []),
        "assigned_rail": row.assigned_rail,
        "attempt_count": int(row.attempt_count or 0),
        "last_error": row.last_error,
        "output_report_id": int(row.output_report_id) if row.output_report_id is not None else None,
        "output_report_version_id": int(row.output_report_version_id) if row.output_report_version_id is not None else None,
        "quality": _json_dict(row.quality_json),
        "payload": _json_dict(row.payload_json),
        "updated_at": _dt(row.updated_at),
        "last_attempted_at": _dt(row.last_attempted_at),
    }


def bottleneck_startup_packet(
    session: Session,
    *,
    model: BottleneckModel,
    campaign: ResearchCampaign | None = None,
    item: ResearchCampaignItem | None = None,
    report_version_id: int | None = None,
) -> dict[str, Any]:
    """Return the agent handoff packet for a user-started bottleneck report workspace."""
    version = None
    if report_version_id is not None:
        version = session.get(ResearchReportVersion, int(report_version_id))
    if version is None and item is not None and item.output_report_version_id is not None:
        version = session.get(ResearchReportVersion, int(item.output_report_version_id))
    report = None
    if version is not None:
        report = version.report
    elif item is not None and item.output_report_id is not None:
        report = session.get(ResearchReport, int(item.output_report_id))
    content = _json_dict(version.content_json if version is not None else {})
    startup = _json_dict(content.get("agent_startup_packet"))
    if not startup:
        from thoth_terminal.bottleneck_service import serialize_bottleneck_model

        startup = {
            "workspace_goal": "Start a source-backed bottleneck report workspace and hand it to subagents.",
            "bottleneck_model_id": int(model.id),
            "model_key": model.model_key,
            "open_gap_count": len(_json_list(_json_dict(item.payload_json if item is not None else {}).get("open_gaps"))),
            "search_queries": [
                _short(f"{model.constrained_input or model.title} {model.output_unit or ''}", 400),
                _short(f"{model.title} sell-side channel check bottleneck evidence", 400),
            ],
            "role_prompts": [
                {
                    "role": role,
                    "prompt": f"Work on `{model.title}` using Thoth MCP/API rails; preserve sources, metrics, periods, and open gaps.",
                }
                for role in ["gap_hunter_quick", "evidence_harvester", "model_builder", "gap_hunter_full", "challenger", "report_writer"]
            ],
            "linked_evidence": {"count": 0, "counts_by_type": {}},
            "candidate_sell_side_evidence": {"count": 0, "counts_by_type": {}},
            "runtime_contract": {
                "text_reasoning_lane": "Qwen3.6-35B-A3B-MTP",
                "mmproj_allowed_on_text_lane": False,
                "preferred_llama_cpp_nparallel": 2,
            },
            "bottleneck_model": serialize_bottleneck_model(model, include_assessments=True),
        }
    campaign_key = campaign.campaign_key if campaign is not None else None
    if campaign_key is None and item is not None:
        campaign_row = session.get(ResearchCampaign, int(item.campaign_id))
        campaign_key = campaign_row.campaign_key if campaign_row is not None else None
    report_payload = None
    if report is not None:
        report_payload = {
            "id": int(report.id),
            "title": report.title,
            "slug": report.slug,
            "report_type": _json_dict(report.extra).get("report_type"),
            "url": f"/reports/{report.slug}",
        }
    version_payload = None
    if version is not None:
        version_payload = {
            "id": int(version.id),
            "report_id": int(version.report_id),
            "version": int(version.version or 0),
            "status": version.status,
            "created_at": _dt(version.created_at),
            "published_at": _dt(version.published_at),
            "artifacts": [
                {
                    "id": int(artifact.id),
                    "artifact_type": artifact.artifact_type,
                    "variant": artifact.variant,
                    "label": artifact.label,
                    "object_path": artifact.object_path,
                    "object_url": f"/objects/{artifact.object_path}",
                    "content_type": artifact.content_type,
                }
                for artifact in sorted(version.artifacts, key=lambda row: (row.artifact_type, row.variant, row.id))
            ],
            "workspace_markdown_excerpt": _short(version.content_md, 8000),
        }
    next_calls = []
    for call in _json_list(startup.get("next_mcp_calls")):
        if not isinstance(call, dict):
            continue
        cloned = dict(call)
        args = _json_dict(cloned.get("arguments"))
        if args.get("campaign_key") == "<campaign_key>" and campaign_key:
            args["campaign_key"] = campaign_key
        if args.get("report_id") == "<report_id_after_workspace_build>" and report is not None:
            args["report_id"] = int(report.id)
        cloned["arguments"] = args
        next_calls.append(cloned)
    if next_calls:
        startup["next_mcp_calls"] = next_calls
    startup.update(
        {
            "campaign": serialize_campaign(campaign) if campaign is not None else None,
            "campaign_item": serialize_campaign_item(item) if item is not None else None,
            "report": report_payload,
            "version": version_payload,
            "open_gaps": _json_list(_json_dict(item.payload_json if item is not None else {}).get("open_gaps")) if item is not None else [],
            "linked_evidence_summary": {
                "count": int(_json_dict(content.get("linked_evidence_pack")).get("count") or 0),
                "counts_by_type": _json_dict(_json_dict(content.get("linked_evidence_pack")).get("counts_by_type")),
            },
            "candidate_sell_side_evidence_summary": {
                "count": int(_json_dict(content.get("candidate_sell_side_evidence_pack")).get("count") or 0),
                "counts_by_type": _json_dict(_json_dict(content.get("candidate_sell_side_evidence_pack")).get("counts_by_type")),
            },
        }
    )
    return startup


def get_campaign(
    session: Session,
    *,
    campaign_id: int | None = None,
    campaign_key: str | None = None,
    include_items: bool = False,
    limit: int = 100,
) -> dict[str, Any] | None:
    row = None
    if campaign_id is not None:
        row = session.get(ResearchCampaign, int(campaign_id))
    if row is None and campaign_key:
        row = session.query(ResearchCampaign).filter_by(campaign_key=str(campaign_key)).first()
    if row is None:
        return None
    payload = serialize_campaign(row)
    if include_items:
        items = (
            session.query(ResearchCampaignItem)
            .filter_by(campaign_id=int(row.id))
            .order_by(ResearchCampaignItem.priority.desc(), ResearchCampaignItem.updated_at.asc(), ResearchCampaignItem.id.asc())
            .limit(max(1, min(int(limit or 100), 1000)))
            .all()
        )
        payload["items"] = [serialize_campaign_item(item) for item in items]
        payload["item_count"] = session.query(ResearchCampaignItem.id).filter_by(campaign_id=int(row.id)).count()
    return payload


def _open_gap_keys(session: Session, gap_keys: list[str]) -> set[str]:
    keys = [str(key).strip() for key in gap_keys if str(key).strip()]
    if not keys:
        return set()
    return {
        str(key)
        for (key,) in session.query(CoverageGap.gap_key)
        .filter(CoverageGap.gap_key.in_(keys), CoverageGap.status == "open")
        .all()
    }


def refresh_campaign_item_blockers(session: Session, *, item: ResearchCampaignItem) -> ResearchCampaignItem:
    blockers = [dict(row) for row in (item.blocking_gaps_json or []) if isinstance(row, dict)]
    keys = [str(row.get("gap_key") or "").strip() for row in blockers if str(row.get("gap_key") or "").strip()]
    open_keys = _open_gap_keys(session, keys)
    open_blockers = [
        row
        for row in blockers
        if str(row.get("gap_key") or "").strip() in open_keys
        and _is_campaign_blocking_gap(str(row.get("gap_kind") or ""), assigned_rail=item.assigned_rail)
    ]
    item.blocking_gaps_json = open_blockers
    if open_blockers and item.status in {"queued", "failed"}:
        item.status = "blocked"
    elif not open_blockers and item.status == "blocked":
        item.status = "queued"
    item.updated_at = utcnow()
    session.flush()
    return item


def unblock_campaign_items(session: Session, *, campaign_id: int | None = None, limit: int = 500) -> dict[str, int]:
    q = session.query(ResearchCampaignItem).filter(ResearchCampaignItem.status == "blocked")
    if campaign_id is not None:
        q = q.filter(ResearchCampaignItem.campaign_id == int(campaign_id))
    checked = 0
    unblocked = 0
    still_blocked = 0
    for item in q.order_by(ResearchCampaignItem.updated_at.asc(), ResearchCampaignItem.id.asc()).limit(max(1, min(int(limit or 500), 5000))).all():
        checked += 1
        before = item.status
        refresh_campaign_item_blockers(session, item=item)
        if before == "blocked" and item.status == "queued":
            unblocked += 1
        elif item.status == "blocked":
            still_blocked += 1
    return {"checked": checked, "unblocked": unblocked, "still_blocked": still_blocked}


def next_campaign_items(session: Session, *, campaign_id: int, limit: int = 10) -> list[dict[str, Any]]:
    unblock_campaign_items(session, campaign_id=int(campaign_id), limit=500)
    rows = (
        session.query(ResearchCampaignItem)
        .filter(
            ResearchCampaignItem.campaign_id == int(campaign_id),
            ResearchCampaignItem.status.in_(["queued", "failed"]),
        )
        .order_by(ResearchCampaignItem.priority.desc(), ResearchCampaignItem.updated_at.asc(), ResearchCampaignItem.id.asc())
        .limit(max(1, min(int(limit or 10), 100)))
        .all()
    )
    return [serialize_campaign_item(row) for row in rows]


def mark_campaign_item_status(
    session: Session,
    *,
    item_id: int,
    status: str,
    last_error: str | None = None,
    output_report_id: int | None = None,
    output_report_version_id: int | None = None,
    quality: dict[str, Any] | None = None,
) -> ResearchCampaignItem:
    row = session.get(ResearchCampaignItem, int(item_id))
    if row is None:
        raise ValueError("Campaign item not found.")
    status_norm = str(status or "").strip().lower()
    if status_norm not in CAMPAIGN_ITEM_STATUSES:
        raise ValueError("Unsupported campaign item status.")
    now = utcnow()
    if status_norm == "running" or (status_norm == "failed" and row.status != "running"):
        row.attempt_count = int(row.attempt_count or 0) + 1
        row.last_attempted_at = now
    row.status = status_norm
    row.last_error = _short(last_error, 4000) or None
    if output_report_id is not None:
        row.output_report_id = int(output_report_id)
    if output_report_version_id is not None:
        row.output_report_version_id = int(output_report_version_id)
    if quality is not None:
        row.quality_json = dict(quality)
    row.updated_at = now
    session.flush()
    return row


def retry_campaign_item(session: Session, *, item_id: int) -> ResearchCampaignItem:
    row = session.get(ResearchCampaignItem, int(item_id))
    if row is None:
        raise ValueError("Campaign item not found.")
    row.last_error = None
    row.updated_at = utcnow()
    refresh_campaign_item_blockers(session, item=row)
    if row.status != "blocked":
        row.status = "queued"
    session.flush()
    return row


def execute_campaign_items(
    session: Session,
    *,
    campaign_id: int | None = None,
    campaign_key: str | None = None,
    limit: int = 3,
    dry_run: bool = True,
    force: bool = False,
    stale_after_days: int = 120,
) -> dict[str, Any]:
    campaign = None
    if campaign_id is not None:
        campaign = session.get(ResearchCampaign, int(campaign_id))
    if campaign is None and campaign_key:
        campaign = session.query(ResearchCampaign).filter_by(campaign_key=str(campaign_key)).first()
    if campaign is None:
        raise ValueError("Research campaign not found.")
    unblock = unblock_campaign_items(session, campaign_id=int(campaign.id))
    candidates = (
        session.query(ResearchCampaignItem)
        .filter(
            ResearchCampaignItem.campaign_id == int(campaign.id),
            ResearchCampaignItem.status.in_(["queued", "failed"]),
        )
        .order_by(ResearchCampaignItem.priority.desc(), ResearchCampaignItem.updated_at.asc(), ResearchCampaignItem.id.asc())
        .limit(max(1, min(int(limit or 3), 25)))
        .all()
    )
    if dry_run:
        return {
            "dry_run": True,
            "campaign": serialize_campaign(campaign),
            "unblock": unblock,
            "selected_count": len(candidates),
            "items": [serialize_campaign_item(row) for row in candidates],
        }
    results: list[dict[str, Any]] = []
    for item in candidates:
        refresh_campaign_item_blockers(session, item=item)
        if item.status == "blocked":
            results.append({"item": serialize_campaign_item(item), "status": "blocked"})
            continue
        mark_campaign_item_status(session, item_id=int(item.id), status="running")
        try:
            if item.target_type == "subject":
                subject_id = int(item.subject_id or item.target_id or 0)
                subject = session.get(ResearchSubject, subject_id) if subject_id else None
                if subject is None:
                    mark_campaign_item_status(session, item_id=int(item.id), status="skipped", last_error="subject_not_found")
                    results.append({"item": serialize_campaign_item(item), "status": "skipped", "reason": "subject_not_found"})
                    continue
                from thoth_terminal.research_report_service import build_research_report

                build = build_research_report(
                    session,
                    subject=subject.subject_key,
                    trigger_kind="research_campaign",
                    trigger_label=campaign.name,
                    force=force,
                )
                canon_state = refresh_subject_canon_state(session, subject_id=int(subject.id), stale_after_days=stale_after_days, generate_card=True)
                completed = mark_campaign_item_status(
                    session,
                    item_id=int(item.id),
                    status="completed",
                    output_report_id=int(build.report.id),
                    output_report_version_id=int(build.version.id),
                    quality=canon_state.get("report_quality") if isinstance(canon_state.get("report_quality"), dict) else None,
                )
                results.append(
                    {
                        "item": serialize_campaign_item(completed),
                        "status": "completed",
                        "report_id": int(build.report.id),
                        "report_version_id": int(build.version.id),
                        "created_version": bool(build.created_version),
                        "reason": build.reason,
                    }
                )
            elif item.target_type == "bottleneck":
                model_id = int(item.target_id or 0)
                model = session.get(BottleneckModel, model_id) if model_id else None
                if model is None and item.target_key:
                    model = session.query(BottleneckModel).filter_by(model_key=str(item.target_key)).first()
                if model is None:
                    mark_campaign_item_status(session, item_id=int(item.id), status="skipped", last_error="bottleneck_model_not_found")
                    results.append({"item": serialize_campaign_item(item), "status": "skipped", "reason": "bottleneck_model_not_found"})
                    continue
                from thoth_terminal.research_report_service import build_bottleneck_report

                build = build_bottleneck_report(
                    session,
                    bottleneck_model_id=int(model.id),
                    trigger_kind="research_campaign",
                    trigger_label=campaign.name,
                    force=force,
                )
                item_payload = _json_dict(item.payload_json)
                campaign_params = _json_dict(campaign.parameters_json)
                workspace_flow = bool(item_payload.get("role_order")) or campaign_params.get("source") == "start_bottleneck_report"
                quality = {
                    "state": "workspace_ready" if workspace_flow else "completed",
                    "created_version": bool(build.created_version),
                    "reason": build.reason,
                    "readiness": dict(build.version.coverage_json or {}).get("readiness") if isinstance(build.version.coverage_json, dict) else None,
                }
                if workspace_flow:
                    workspace_ready = mark_campaign_item_status(
                        session,
                        item_id=int(item.id),
                        status="workspace_ready",
                        output_report_id=int(build.report.id),
                        output_report_version_id=int(build.version.id),
                        quality=quality,
                    )
                    results.append(
                        {
                            "item": serialize_campaign_item(workspace_ready),
                            "status": "workspace_ready",
                            "report_id": int(build.report.id),
                            "report_version_id": int(build.version.id),
                            "created_version": bool(build.created_version),
                            "reason": build.reason,
                            "startup_packet": bottleneck_startup_packet(
                                session,
                                model=model,
                                campaign=campaign,
                                item=workspace_ready,
                                report_version_id=int(build.version.id),
                            ),
                        }
                    )
                else:
                    completed = mark_campaign_item_status(
                        session,
                        item_id=int(item.id),
                        status="completed",
                        output_report_id=int(build.report.id),
                        output_report_version_id=int(build.version.id),
                        quality=quality,
                    )
                    results.append(
                        {
                            "item": serialize_campaign_item(completed),
                            "status": "completed",
                            "report_id": int(build.report.id),
                            "report_version_id": int(build.version.id),
                            "created_version": bool(build.created_version),
                            "reason": build.reason,
                        }
                    )
            else:
                mark_campaign_item_status(session, item_id=int(item.id), status="skipped", last_error=f"unsupported_target_type:{item.target_type}")
                results.append({"item": serialize_campaign_item(item), "status": "skipped", "reason": "unsupported_target_type"})
        except Exception as exc:
            failed = mark_campaign_item_status(session, item_id=int(item.id), status="failed", last_error=f"{type(exc).__name__}: {exc}")
            results.append({"item": serialize_campaign_item(failed), "status": "failed", "error": failed.last_error})
    counts = Counter(str(row.get("status") or "unknown") for row in results)
    return {
        "dry_run": False,
        "campaign": serialize_campaign(campaign),
        "unblock": unblock,
        "selected_count": len(candidates),
        "result_counts": dict(counts),
        "results": results,
    }


def upsert_bottleneck_object(
    session: Session,
    *,
    model_key: str,
    title: str,
    scope_type: str = "theme",
    scope_key: str | None = None,
    constrained_input: str | None = None,
    supply_chain_id: int | None = None,
    subject_id: int | None = None,
    summary: str | None = None,
    thesis: str | None = None,
    payload: dict[str, Any] | None = None,
    links: list[dict[str, Any]] | None = None,
) -> BottleneckModel:
    now = utcnow()
    key = _slugify(model_key, default="bottleneck")
    row = session.query(BottleneckModel).filter_by(model_key=key).first()
    merged_payload = dict(payload or {})
    if row is None:
        row = BottleneckModel(
            model_key=key,
            title=_short(title),
            scope_type=_short(scope_type, 64) or "theme",
            scope_key=_short(scope_key),
            subject_id=int(subject_id) if subject_id is not None else None,
            supply_chain_id=int(supply_chain_id) if supply_chain_id is not None else None,
            constrained_input=_short(constrained_input),
            output_unit=merged_payload.get("capacity_metric"),
            regime=merged_payload.get("regime"),
            status="active",
            summary=summary,
            thesis=thesis,
            qualified_supplier_count=merged_payload.get("qualified_supplier_count"),
            supplier_qualification_json=dict(merged_payload.get("suppliers") or {}),
            evidence_json=[dict(item) for item in (merged_payload.get("evidence_links") or []) if isinstance(item, dict)],
            payload=merged_payload,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.flush()
    else:
        row.title = _short(title) or row.title
        row.scope_type = _short(scope_type, 64) or row.scope_type
        row.scope_key = _short(scope_key) or row.scope_key
        row.subject_id = int(subject_id) if subject_id is not None else row.subject_id
        row.supply_chain_id = int(supply_chain_id) if supply_chain_id is not None else row.supply_chain_id
        row.constrained_input = _short(constrained_input) or row.constrained_input
        row.summary = summary if summary is not None else row.summary
        row.thesis = thesis if thesis is not None else row.thesis
        current_payload = _json_dict(row.payload)
        current_payload.update(merged_payload)
        row.payload = current_payload
        if "capacity_metric" in merged_payload:
            row.output_unit = merged_payload.get("capacity_metric")
        if "suppliers" in merged_payload:
            row.supplier_qualification_json = dict(merged_payload.get("suppliers") or {})
        if "evidence_links" in merged_payload:
            row.evidence_json = [dict(item) for item in (merged_payload.get("evidence_links") or []) if isinstance(item, dict)]
        row.updated_at = now
        session.flush()
    for link in links or []:
        if not isinstance(link, dict):
            continue
        object_type = _short(link.get("object_type"), 32)
        try:
            object_id = int(link.get("object_id"))
        except Exception:
            continue
        relation = _short(link.get("relation") or "references", 64)
        existing = (
            session.query(BottleneckModelLink)
            .filter_by(model_id=int(row.id), object_type=object_type, object_id=object_id, relation=relation)
            .first()
        )
        if existing is None:
            session.add(
                BottleneckModelLink(
                    model_id=int(row.id),
                    object_type=object_type,
                    object_id=object_id,
                    relation=relation,
                    payload=dict(link.get("payload") or {}),
                    created_at=now,
                )
            )
    session.flush()
    return row


def refresh_bottleneck_coverage_gaps(session: Session) -> dict[str, Any]:
    counts = Counter()
    rows = session.query(BottleneckModel).filter(BottleneckModel.status == "active").order_by(BottleneckModel.id.asc()).all()
    for model in rows:
        payload = _json_dict(model.payload)
        links = session.query(BottleneckModelLink).filter_by(model_id=int(model.id)).all()
        evidence_links = [dict(item) for item in (model.evidence_json or []) if isinstance(item, dict)]
        evidence_links.extend(dict(item) for item in _json_list(payload.get("evidence_links")) if isinstance(item, dict))
        evidence_payload = {"evidence_links": evidence_links}
        scenario_present = bool(payload.get("selected_canon_scenario") or payload.get("scenario_id"))
        evidence_present = bool(evidence_links)
        exposed_present = bool(payload.get("exposed_companies") or any(link.object_type == "subject" for link in links))
        input_present = bool(model.constrained_input) or _json_tree_has_key(payload, _BOTTLENECK_SCOPE_INPUT_KEYS)
        output_present = bool(model.output_unit) or _json_tree_has_key(payload, _BOTTLENECK_SCOPE_OUTPUT_KEYS)
        scope_present = input_present and output_present
        qualified_supply_present = (
            model.qualified_supplier_count is not None
            or _has_nonempty_json_value(model.supplier_qualification_json)
            or _json_tree_has_key(payload, _BOTTLENECK_QUALIFIED_SUPPLY_KEYS)
        )
        allocation_model_present = _json_tree_has_key(payload, _BOTTLENECK_ALLOCATION_MODEL_KEYS)
        metric_bindings_present = (
            _json_tree_has_key(payload, _BOTTLENECK_DATASET_FAMILY_KEYS)
            or _json_tree_has_metric_binding(payload)
            or _json_tree_has_metric_binding(evidence_payload)
        )
        shadow_constraints_present = _has_nonempty_json_value(payload.get("shadow_constraints") or payload.get("candidate_shadow_constraints"))
        falsification_tests_present = _has_nonempty_json_value(payload.get("falsification_tests") or payload.get("falsification_conditions"))
        source_attribution_present = not evidence_present or _json_tree_has_key(
            {"payload": payload, "evidence_links": evidence_links},
            _BOTTLENECK_ATTRIBUTION_KEYS,
        )
        for kind, open_gap, title, summary in [
            ("missing_bottleneck_canon_scenario", not scenario_present, f"Bottleneck scenario missing for {model.title}", "Bottleneck model has no selected canon scenario payload."),
            ("missing_bottleneck_evidence", not evidence_present, f"Bottleneck evidence missing for {model.title}", "Bottleneck model has no evidence links."),
            ("missing_bottleneck_exposed_companies", not exposed_present, f"Bottleneck exposure links missing for {model.title}", "Bottleneck model is not linked to exposed companies or subjects."),
            ("missing_bottleneck_scope_card", not scope_present, f"Bottleneck scope card incomplete for {model.title}", "Bottleneck model must name the constrained input and downstream output unit."),
            ("missing_bottleneck_qualified_supply", not qualified_supply_present, f"Bottleneck qualified supply missing for {model.title}", "Bottleneck model must distinguish qualified supplier capacity from theoretical supply."),
            ("missing_bottleneck_allocation_model", not allocation_model_present, f"Bottleneck allocation model missing for {model.title}", "Bottleneck model has no closed allocation bridge from topline demand through conversion layers to constrained input supply."),
            ("missing_bottleneck_metric_bindings", not metric_bindings_present, f"Bottleneck metric bindings missing for {model.title}", "Bottleneck model has no metric-bearing evidence, dataset-family payload, or conversion assumptions."),
            ("missing_bottleneck_shadow_constraints", not shadow_constraints_present, f"Bottleneck shadow constraints missing for {model.title}", "Bottleneck model must identify substitute bottlenecks that could bind before or after the named constraint."),
            ("missing_bottleneck_falsification_tests", not falsification_tests_present, f"Bottleneck falsification tests missing for {model.title}", "Bottleneck model must state observable conditions that would weaken or kill the thesis."),
            ("missing_bottleneck_source_attribution", not source_attribution_present, f"Bottleneck source attribution incomplete for {model.title}", "Bottleneck evidence must preserve source provenance such as observed source, underlying source, document id, citation, or access status."),
        ]:
            gap = _upsert_gap(
                session,
                key=f"canon:bottleneck:{int(model.id)}:{kind}",
                kind=kind,
                title=title,
                summary=summary,
                severity=4,
                rails=["bottleneck_models", "supply_chains", "reports"],
                payload={
                    "scope": "bottleneck",
                    "actionability": "actionable",
                    "bottleneck_model_id": int(model.id),
                    "model_key": model.model_key,
                },
                open_gap=open_gap,
            )
            if gap.status == "open":
                counts[kind] += 1
    session.flush()
    return {"models_checked": len(rows), "open_gap_counts": dict(counts)}


def create_campaign_from_bottleneck_gaps(
    session: Session,
    *,
    name: str | None = None,
    campaign_key: str | None = None,
    limit: int | None = None,
    preview: bool = False,
) -> dict[str, Any]:
    refresh_bottleneck_coverage_gaps(session)
    gaps = (
        session.query(CoverageGap)
        .filter(
            CoverageGap.owner_ref == "research_canon",
            CoverageGap.status == "open",
            CoverageGap.gap_kind.in_(BOTTLENECK_COVERAGE_GAP_KINDS),
        )
        .order_by(CoverageGap.severity.desc(), CoverageGap.updated_at.asc(), CoverageGap.id.asc())
        .limit(max(1, min(int(limit or 100), 1000)))
        .all()
    )
    items = []
    grouped: dict[int, list[CoverageGap]] = defaultdict(list)
    for gap in gaps:
        model_id = _json_dict(gap.payload).get("bottleneck_model_id")
        if model_id is not None:
            grouped[int(model_id)].append(gap)
    for model_id, model_gaps in grouped.items():
        model = session.get(BottleneckModel, int(model_id))
        if model is None:
            continue
        items.append(
            {
                "target_type": "bottleneck",
                "target_id": int(model.id),
                "target_key": model.model_key,
                "title": model.title,
                "priority": max(4, max(int(gap.severity or 0) for gap in model_gaps)),
                "quality_state": "blocked",
                "blocking_gaps": [{"gap_key": gap.gap_key, "gap_kind": gap.gap_kind, "severity": int(gap.severity or 0)} for gap in model_gaps],
            }
        )
    if preview:
        return {"preview": True, "item_count": len(items), "items": items}
    now = utcnow()
    key = campaign_key or "campaign:bottlenecks:structural-coverage"
    campaign = session.query(ResearchCampaign).filter_by(campaign_key=key).first()
    if campaign is None:
        campaign = ResearchCampaign(
            campaign_key=key,
            name=name or "Bottleneck structural coverage",
            description="Convert open bottleneck model gaps into durable work items.",
            status="draft",
            universe_id=None,
            parameters_json={"source": "bottleneck_gaps", "limit": limit},
            created_at=now,
            updated_at=now,
        )
        session.add(campaign)
        session.flush()
    created = 0
    updated = 0
    for item in items:
        row = (
            session.query(ResearchCampaignItem)
            .filter_by(campaign_id=int(campaign.id), target_type=item["target_type"], target_key=item["target_key"])
            .first()
        )
        if row is None:
            row = ResearchCampaignItem(
                campaign_id=int(campaign.id),
                item_key=f"campaign:{int(campaign.id)}:{item['target_type']}:{item['target_key']}",
                target_type=item["target_type"],
                target_id=int(item["target_id"]),
                target_key=item["target_key"],
                subject_id=None,
                universe_id=None,
                priority=int(item["priority"]),
                status="blocked" if item.get("blocking_gaps") else "queued",
                blocking_gaps_json=list(item.get("blocking_gaps") or []),
                assigned_rail="bottleneck_reports",
                attempt_count=0,
                quality_json={"state": item.get("quality_state")},
                payload_json={"title": item.get("title")},
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            created += 1
        else:
            row.priority = int(item["priority"])
            row.blocking_gaps_json = list(item.get("blocking_gaps") or [])
            row.status = "blocked" if row.blocking_gaps_json and row.status in {"queued", "blocked"} else row.status
            row.updated_at = now
            updated += 1
    session.flush()
    return {
        "preview": False,
        "campaign": serialize_campaign(campaign),
        "item_count": len(items),
        "created_items": created,
        "updated_items": updated,
    }


def _bottleneck_open_gap_context(session: Session, *, model_id: int) -> list[dict[str, Any]]:
    rows = (
        session.query(CoverageGap)
        .filter(
            CoverageGap.owner_ref == "research_canon",
            CoverageGap.status == "open",
            CoverageGap.gap_kind.in_(BOTTLENECK_COVERAGE_GAP_KINDS),
        )
        .order_by(CoverageGap.severity.desc(), CoverageGap.updated_at.desc(), CoverageGap.id.desc())
        .limit(500)
        .all()
    )
    out: list[dict[str, Any]] = []
    for gap in rows:
        payload = _json_dict(gap.payload)
        if int(payload.get("bottleneck_model_id") or -1) != int(model_id):
            continue
        out.append(
            {
                "gap_key": gap.gap_key,
                "gap_kind": gap.gap_kind,
                "title": gap.title,
                "severity": int(gap.severity or 0),
                "summary": gap.summary,
                "suggested_rails": list(gap.suggested_rails or []),
            }
        )
    return out


def start_bottleneck_report(
    session: Session,
    *,
    model_key: str,
    title: str,
    scope_type: str = "theme",
    scope_key: str | None = None,
    constrained_input: str | None = None,
    output_unit: str | None = None,
    supply_chain_id: int | None = None,
    subject_id: int | None = None,
    summary: str | None = None,
    thesis: str | None = None,
    payload: dict[str, Any] | None = None,
    links: list[dict[str, Any]] | None = None,
    campaign_key: str | None = None,
    campaign_name: str | None = None,
    priority: int = 8,
) -> dict[str, Any]:
    if not str(model_key or "").strip() or not str(title or "").strip():
        raise ValueError("model_key and title are required.")
    merged_payload = _json_dict(payload)
    if output_unit and not merged_payload.get("capacity_metric"):
        merged_payload["capacity_metric"] = output_unit
    model = upsert_bottleneck_object(
        session,
        model_key=model_key,
        title=title,
        scope_type=scope_type,
        scope_key=scope_key,
        constrained_input=constrained_input,
        supply_chain_id=supply_chain_id,
        subject_id=subject_id,
        summary=summary,
        thesis=thesis,
        payload=merged_payload,
        links=links,
    )
    refresh_bottleneck_coverage_gaps(session)
    open_gaps = _bottleneck_open_gap_context(session, model_id=int(model.id))
    now = utcnow()
    key = campaign_key or f"campaign:bottleneck:{model.model_key}:startup"
    campaign = session.query(ResearchCampaign).filter_by(campaign_key=key).first()
    if campaign is None:
        campaign = ResearchCampaign(
            campaign_key=key,
            name=campaign_name or f"Bottleneck report: {model.title}",
            description="User-started bottleneck report campaign. Open model gaps are work context, not blockers.",
            status="active",
            universe_id=None,
            parameters_json={"source": "start_bottleneck_report", "bottleneck_model_id": int(model.id)},
            created_at=now,
            updated_at=now,
        )
        session.add(campaign)
        session.flush()
    else:
        campaign.name = campaign_name or campaign.name
        params = _json_dict(campaign.parameters_json)
        params.update({"source": "start_bottleneck_report", "bottleneck_model_id": int(model.id)})
        campaign.parameters_json = params
        campaign.updated_at = now

    item = (
        session.query(ResearchCampaignItem)
        .filter_by(campaign_id=int(campaign.id), target_type="bottleneck", target_key=model.model_key)
        .first()
    )
    if item is None:
        item = ResearchCampaignItem(
            campaign_id=int(campaign.id),
            item_key=f"campaign:{int(campaign.id)}:bottleneck:{model.model_key}",
            target_type="bottleneck",
            target_id=int(model.id),
            target_key=model.model_key,
            subject_id=None,
            universe_id=None,
            priority=max(1, min(int(priority or 8), 10)),
            status="queued",
            blocking_gaps_json=[],
            assigned_rail="bottleneck_reports",
            attempt_count=0,
            quality_json={},
            payload_json={},
            created_at=now,
            updated_at=now,
        )
        session.add(item)
    item.target_id = int(model.id)
    item.priority = max(1, min(int(priority or 8), 10))
    item.status = "queued" if item.status in {"blocked", "failed", "skipped", "queued", "workspace_ready", "completed"} else item.status
    item.blocking_gaps_json = []
    item.assigned_rail = "bottleneck_reports"
    item.quality_json = {
        "state": "ready_to_start",
        "open_gap_count": len(open_gaps),
        "gaps_are_work_context": True,
    }
    item.payload_json = {
        "title": model.title,
        "bottleneck_model_id": int(model.id),
        "model_key": model.model_key,
        "open_gaps": open_gaps,
        "role_order": ["gap_hunter_quick", "evidence_harvester", "model_builder", "gap_hunter_full", "challenger", "report_writer"],
    }
    item.updated_at = now
    session.flush()
    from thoth_terminal.bottleneck_service import serialize_bottleneck_model

    return {
        "bottleneck_model": serialize_bottleneck_model(model, include_assessments=True),
        "campaign": serialize_campaign(campaign),
        "item": serialize_campaign_item(item),
        "open_gaps": open_gaps,
        "startup_packet": bottleneck_startup_packet(session, model=model, campaign=campaign, item=item),
    }


def research_canon_context_for_subject(session: Session, *, subject_id: int) -> dict[str, Any]:
    source_pack = session.query(ResearchSourcePack).filter_by(subject_id=int(subject_id)).first()
    quality = session.query(ResearchReportQuality).filter_by(subject_id=int(subject_id)).first()
    return {
        "selected_canon_scenario": serialize_scenario(get_selected_canon_scenario(session, subject_id=int(subject_id))),
        "source_pack": serialize_source_pack(source_pack),
        "report_quality": serialize_quality(quality),
        "agent_canon_card": get_agent_canon_card(session, subject_id=int(subject_id)),
    }


def run_research_canon_maintenance(
    session: Session,
    *,
    universe_slug: str | None = None,
    create_requests: bool = False,
    create_cards: bool = False,
    stale_after_days: int = 120,
    limit: int | None = None,
) -> dict[str, Any]:
    coverage = scan_active_universe_coverage(
        session,
        universe_slug=universe_slug,
        ensure_subjects=True,
        create_requests=create_requests,
        create_cards=create_cards,
        stale_after_days=stale_after_days,
        limit=limit,
    )
    bottlenecks = refresh_bottleneck_coverage_gaps(session)
    open_gaps = (
        session.query(CoverageGap.gap_kind, CoverageGap.status)
        .filter(CoverageGap.owner_ref == "research_canon")
        .all()
    )
    counts = Counter(kind for kind, status in open_gaps if status == "open")
    campaign_counts = Counter(
        status
        for (status,) in session.query(ResearchCampaignItem.status).all()
    )
    quality_counts = Counter(
        state
        for (state,) in session.query(ResearchReportQuality.state).all()
    )
    return {
        "ok": True,
        "scan": coverage,
        "bottlenecks": bottlenecks,
        "open_gap_counts": dict(counts),
        "quality_state_counts": dict(quality_counts),
        "campaign_readiness_counts": dict(campaign_counts),
        "actionable_open_gaps": sum(counts.values()),
    }
