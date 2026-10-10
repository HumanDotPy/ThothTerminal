from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from thoth_terminal.db import utcnow
from thoth_terminal.institutional_model_vocab import metric_state_field, normalize_metric_key
from thoth_terminal.jobs import enqueue_job
from thoth_terminal.models import (
    BottleneckAssessment,
    Company,
    CoverageGap,
    Document,
    EntityExternalId,
    EntityIdentity,
    EntityIdentityAlias,
    InstitutionalEvidenceItem,
    InstitutionalObservation,
    Issuer,
    Program,
    ProgramEdge,
    ProgramNode,
    ResearchSubject,
    Site,
    Source,
    SourceProfile,
    StateVectorSnapshot,
    StructuredDataFact,
)


def _slugify(value: str, *, default: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return slug[:120] or default


def _now() -> datetime:
    return utcnow()


def _source_profile_defaults(source: Source | None) -> dict[str, Any]:
    cfg = dict((source.config or {}) if source is not None else {})
    connector = str(cfg.get("connector") or "").strip().lower()
    family = (
        "trade"
        if connector.startswith("trade_")
        else "research"
        if connector in {"local_research_library", "substack_inbox_browser", "note_creator_browser"}
        else "disclosure"
        if connector in {"sec_edgar", "sec_edgar_rss", "sec_companyfacts", "opendart_disclosures", "opendart_financials", "taiwan_mops_monthly_revenue", "taiwan_mops_investor_conferences", "tpex_infohub_company", "twse_openapi", "krx_kind_disclosure", "hkexnews_title_search", "cninfo_announcements", "tdnet_company_announcements", "edinet_disclosures", "edinetdb_financials"}
        else "procurement"
        if connector in {"sam_gov_contracts", "chips_funding"}
        else "technical"
        if connector in {"patentsview_search", "fcc_oet_kdb", "cbp_cross", "usitc_hts", "eu_taric"}
        else "imagery"
        if connector in {"copernicus_browser", "usgs_earthexplorer", "usgs_naip"}
        else "power"
        if connector in {"eia_860m", "pjm_planning_center", "caiso_interconnection_queue", "ercot_large_load", "ercot_llwg"}
        else "general"
    )
    acquisition_mode = (
        "manual_review"
        if connector == "local_research_library"
        else "authenticated_browser"
        if connector in {"substack_inbox_browser", "note_creator_browser"}
        else "manual_template"
        if bool(cfg.get("jurisdiction_template_kind"))
        else "connector"
    )
    cadence = (
        "monthly"
        if connector in {"taiwan_mops_monthly_revenue", "trade_us_census", "trade_taiwan_customs", "trade_korea_customs", "trade_japan_trade_stats", "trade_eurostat_comext", "trade_usitc_dataweb", "trade_un_comtrade", "eia_860m"}
        else "daily"
        if connector in {"sam_gov_contracts", "pjm_planning_center", "caiso_interconnection_queue", "ercot_large_load", "ercot_llwg", "copernicus_browser", "usgs_earthexplorer", "usgs_naip", "cbp_cross", "usitc_hts", "eu_taric", "fcc_oet_kdb", "chips_funding"}
        else "quarterly"
        if connector == "local_research_library"
        else "twice_daily"
        if connector in {"substack_inbox_browser", "note_creator_browser"}
        else "event_driven"
    )
    return {
        "source_family": family,
        "rights_class": "paid" if connector in {"substack_inbox_browser", "note_creator_browser"} else "public",
        "acquisition_mode": acquisition_mode,
        "cadence_label": cadence,
        "review_required": False,
        "subject_binding_kind": "subject",
        "site_binding_kind": "site" if family in {"power", "imagery", "general"} else None,
        "jurisdiction_template_kind": str(cfg.get("jurisdiction_template_kind") or "").strip() or None,
        "vendor_tier": None,
        "notes": None,
    }


def ensure_source_profile(
    session: Session,
    source: Source,
    *,
    existing_row: SourceProfile | None = None,
    **overrides: Any,
) -> SourceProfile:
    row = existing_row
    if row is None:
        row = session.query(SourceProfile).filter_by(source_id=int(source.id)).first()
    now = _now()
    defaults = _source_profile_defaults(source)
    explicit = {k: v for k, v in overrides.items() if v is not None}
    payload = {**defaults, **explicit}
    if row is None:
        row = SourceProfile(
            source_id=int(source.id),
            created_at=now,
            updated_at=now,
            extra={},
            **payload,
        )
        session.add(row)
        session.flush()
        return row
    payload = dict(explicit)
    for key, value in defaults.items():
        current = getattr(row, key)
        if current is None and value is not None:
            payload[key] = value
    changed = False
    for key, value in payload.items():
        if getattr(row, key) != value:
            setattr(row, key, value)
            changed = True
    if changed:
        row.updated_at = now
        session.add(row)
        session.flush()
    return row


def sync_source_profiles(session: Session) -> int:
    sources = session.query(Source).order_by(Source.id.asc()).all()
    if not sources:
        return 0
    profile_map = {
        int(row.source_id): row
        for row in session.query(SourceProfile).all()
        if row.source_id is not None
    }
    count = 0
    for source in sources:
        ensure_source_profile(session, source, existing_row=profile_map.get(int(source.id)))
        count += 1
    return count


def _ensure_subject_for_company(session: Session, company: Company | None, issuer: Issuer | None) -> ResearchSubject | None:
    if issuer is not None:
        subject_key = f"issuer:{int(issuer.id)}"
        row = session.query(ResearchSubject).filter_by(subject_key=subject_key).first()
        if row:
            return row
        now = _now()
        row = ResearchSubject(
            slug=_slugify(str(issuer.display_name or issuer.legal_name or f"issuer-{issuer.id}"), default=f"issuer-{int(issuer.id)}"),
            subject_key=subject_key,
            kind="issuer",
            display_name=str(issuer.display_name or issuer.legal_name or f"Issuer {issuer.id}"),
            legal_name=str(issuer.legal_name or issuer.display_name or "") or None,
            description=None,
            status="active",
            issuer_id=int(issuer.id),
            company_id=int(company.id) if company is not None else None,
            primary_entity_id=None,
            extra={},
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.flush()
        return row
    if company is None:
        return None
    subject_key = f"company:{int(company.id)}"
    row = session.query(ResearchSubject).filter_by(subject_key=subject_key).first()
    if row:
        return row
    now = _now()
    row = ResearchSubject(
        slug=_slugify(str(company.slug or company.name or f"company-{company.id}"), default=f"company-{int(company.id)}"),
        subject_key=subject_key,
        kind="company",
        display_name=str(company.name or f"Company {company.id}"),
        legal_name=str(company.name or "") or None,
        description=None,
        status="active",
        issuer_id=int(company.issuer_id) if company.issuer_id is not None else None,
        company_id=int(company.id),
        primary_entity_id=None,
        extra={},
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    return row


def ensure_entity_identity(
    session: Session,
    *,
    subject_row: ResearchSubject,
    company: Company | None = None,
    issuer: Issuer | None = None,
    aliases: list[str] | None = None,
    external_ids: list[tuple[str, str]] | None = None,
) -> EntityIdentity:
    from thoth_terminal.identity_alias_service import require_consistent_identity, valid_identity_aliases
    require_consistent_identity(company=company, issuer=issuer)
    row = session.query(EntityIdentity).filter_by(subject_id=int(subject_row.id)).first()
    now = _now()
    canonical_name = str(subject_row.display_name or subject_row.legal_name or f"subject-{subject_row.id}")
    if row is None:
        row = EntityIdentity(
            subject_id=int(subject_row.id),
            issuer_id=int(issuer.id) if issuer is not None else (int(subject_row.issuer_id) if subject_row.issuer_id is not None else None),
            company_id=int(company.id) if company is not None else (int(subject_row.company_id) if subject_row.company_id is not None else None),
            canonical_kind=str(subject_row.kind or "entity"),
            canonical_name=canonical_name,
            legal_name=str(subject_row.legal_name or company.name if company is not None else issuer.legal_name if issuer is not None else "") or None,
            display_name=str(subject_row.display_name or canonical_name),
            jurisdiction=str(issuer.jurisdiction or "") if issuer is not None else None,
            status="active",
            extra={},
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.flush()
    else:
        row.canonical_kind = str(subject_row.kind or row.canonical_kind or "entity")
        row.canonical_name = canonical_name
        row.display_name = str(subject_row.display_name or row.display_name or canonical_name)
        if subject_row.legal_name:
            row.legal_name = str(subject_row.legal_name)
        if issuer is not None:
            row.issuer_id = int(issuer.id)
            row.jurisdiction = str(issuer.jurisdiction or "") or row.jurisdiction
        if company is not None:
            row.company_id = int(company.id)
        row.updated_at = now
        session.add(row)
        session.flush()
    alias_values = [canonical_name]
    if subject_row.legal_name:
        alias_values.append(str(subject_row.legal_name))
    if company is not None and company.name:
        alias_values.append(str(company.name))
    if issuer is not None:
        alias_values.extend([str(issuer.display_name or ""), str(issuer.legal_name or "")])
    alias_values.extend([str(alias).strip() for alias in (aliases or []) if str(alias).strip()])
    seen_aliases = {
        (str(existing.alias).strip().lower(), str(existing.alias_type or "name").strip().lower())
        for existing in session.query(EntityIdentityAlias).filter_by(identity_id=int(row.id)).all()
    }
    for alias in valid_identity_aliases(session, issuer_id=row.issuer_id, aliases=alias_values):
        pair = (alias.strip().lower(), "name")
        if not alias.strip() or pair in seen_aliases:
            continue
        session.add(
            EntityIdentityAlias(
                identity_id=int(row.id),
                alias=alias.strip(),
                alias_type="name",
                created_at=now,
            )
        )
        seen_aliases.add(pair)
    existing_ext = {
        (str(existing.provider).strip().lower(), str(existing.external_id).strip())
        for existing in session.query(EntityExternalId).filter_by(identity_id=int(row.id)).all()
    }
    for provider, value in external_ids or []:
        pair = (str(provider).strip().lower(), str(value).strip())
        if not pair[0] or not pair[1] or pair in existing_ext:
            continue
        session.add(
            EntityExternalId(
                identity_id=int(row.id),
                provider=str(provider).strip(),
                external_id=str(value).strip(),
                is_primary=False,
                extra={},
                created_at=now,
                updated_at=now,
            )
        )
        existing_ext.add(pair)
    session.flush()
    return row


def upsert_site(
    session: Session,
    *,
    slug: str,
    name: str,
    subject_row: ResearchSubject | None = None,
    issuer: Issuer | None = None,
    company: Company | None = None,
    **kwargs: Any,
) -> Site:
    row = session.query(Site).filter_by(slug=str(slug).strip()).first()
    now = _now()
    if row is None:
        row = Site(
            slug=str(slug).strip(),
            name=str(name).strip(),
            subject_id=int(subject_row.id) if subject_row is not None else None,
            issuer_id=int(issuer.id) if issuer is not None else None,
            company_id=int(company.id) if company is not None else None,
            created_at=now,
            updated_at=now,
            extra={},
        )
        session.add(row)
        session.flush()
    row.name = str(name).strip() or row.name
    for key, value in kwargs.items():
        if hasattr(row, key) and value is not None:
            setattr(row, key, value)
    row.updated_at = now
    session.add(row)
    session.flush()
    return row


def upsert_program(
    session: Session,
    *,
    slug: str,
    title: str,
    subject_row: ResearchSubject | None = None,
    issuer: Issuer | None = None,
    company: Company | None = None,
    site: Site | None = None,
    category: str | None = None,
    summary: str | None = None,
    extra: dict[str, Any] | None = None,
) -> Program:
    row = session.query(Program).filter_by(slug=str(slug).strip()).first()
    now = _now()
    if row is None:
        row = Program(
            slug=str(slug).strip(),
            title=str(title).strip(),
            subject_id=int(subject_row.id) if subject_row is not None else None,
            issuer_id=int(issuer.id) if issuer is not None else None,
            company_id=int(company.id) if company is not None else None,
            site_id=int(site.id) if site is not None else None,
            category=str(category or "general"),
            summary=str(summary or "").strip() or None,
            status="active",
            extra=dict(extra or {}),
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.flush()
        return row
    row.title = str(title).strip() or row.title
    row.category = str(category or row.category or "general")
    if summary is not None:
        row.summary = str(summary).strip() or None
    if subject_row is not None:
        row.subject_id = int(subject_row.id)
    if issuer is not None:
        row.issuer_id = int(issuer.id)
    if company is not None:
        row.company_id = int(company.id)
    if site is not None:
        row.site_id = int(site.id)
    if extra:
        payload = dict(row.extra or {})
        payload.update(extra)
        row.extra = payload
    row.updated_at = now
    session.add(row)
    session.flush()
    return row


def upsert_program_node(
    session: Session,
    *,
    program: Program,
    node_key: str,
    name: str,
    node_type: str,
    dependency_kind: str | None = None,
    manufacturer: str | None = None,
    attachment_ratio: float | None = None,
    quantity: float | None = None,
    unit: str | None = None,
    constraining_score: float | None = None,
    confidence: float | None = None,
    notes: str | None = None,
    extra: dict[str, Any] | None = None,
) -> ProgramNode:
    row = session.query(ProgramNode).filter_by(program_id=int(program.id), node_key=str(node_key).strip()).first()
    now = _now()
    if row is None:
        row = ProgramNode(
            program_id=int(program.id),
            node_key=str(node_key).strip(),
            name=str(name).strip(),
            node_type=str(node_type).strip() or "component",
            dependency_kind=str(dependency_kind or "").strip() or None,
            manufacturer=str(manufacturer or "").strip() or None,
            attachment_ratio=attachment_ratio,
            quantity=quantity,
            unit=str(unit or "").strip() or None,
            constraining_score=constraining_score,
            confidence=confidence,
            notes=str(notes or "").strip() or None,
            extra=dict(extra or {}),
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.flush()
        return row
    row.name = str(name).strip() or row.name
    row.node_type = str(node_type).strip() or row.node_type
    row.dependency_kind = str(dependency_kind or "").strip() or row.dependency_kind
    row.manufacturer = str(manufacturer or "").strip() or row.manufacturer
    if attachment_ratio is not None:
        row.attachment_ratio = float(attachment_ratio)
    if quantity is not None:
        row.quantity = float(quantity)
    if unit is not None:
        row.unit = str(unit).strip() or None
    if constraining_score is not None:
        row.constraining_score = float(constraining_score)
    if confidence is not None:
        row.confidence = float(confidence)
    if notes is not None:
        row.notes = str(notes).strip() or None
    if extra:
        payload = dict(row.extra or {})
        payload.update(extra)
        row.extra = payload
    row.updated_at = now
    session.add(row)
    session.flush()
    return row


def upsert_program_edge(
    session: Session,
    *,
    program: Program,
    source_node: ProgramNode,
    target_node: ProgramNode,
    edge_kind: str = "depends_on",
    confidence: float | None = None,
    notes: str | None = None,
    extra: dict[str, Any] | None = None,
) -> ProgramEdge:
    row = (
        session.query(ProgramEdge)
        .filter_by(
            program_id=int(program.id),
            source_node_id=int(source_node.id),
            target_node_id=int(target_node.id),
            edge_kind=str(edge_kind).strip() or "depends_on",
        )
        .first()
    )
    now = _now()
    if row is None:
        row = ProgramEdge(
            program_id=int(program.id),
            source_node_id=int(source_node.id),
            target_node_id=int(target_node.id),
            edge_kind=str(edge_kind).strip() or "depends_on",
            confidence=confidence,
            notes=str(notes or "").strip() or None,
            extra=dict(extra or {}),
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.flush()
        return row
    if confidence is not None:
        row.confidence = float(confidence)
    if notes is not None:
        row.notes = str(notes).strip() or None
    if extra:
        payload = dict(row.extra or {})
        payload.update(extra)
        row.extra = payload
    row.updated_at = now
    session.add(row)
    session.flush()
    return row


def upsert_evidence_item(
    session: Session,
    *,
    evidence_key: str,
    title: str,
    evidence_kind: str,
    source: Source | None = None,
    document_id: int | None = None,
    subject_row: ResearchSubject | None = None,
    issuer: Issuer | None = None,
    company: Company | None = None,
    site: Site | None = None,
    program: Program | None = None,
    provenance_summary: str | None = None,
    url: str | None = None,
    canonical_url: str | None = None,
    published_at: datetime | None = None,
    observed_at: datetime | None = None,
    rights_class: str | None = None,
    review_required: bool | None = None,
    confidence: float | None = None,
    payload: dict[str, Any] | None = None,
) -> InstitutionalEvidenceItem:
    row = session.query(InstitutionalEvidenceItem).filter_by(evidence_key=str(evidence_key).strip()).first()
    now = _now()
    profile = ensure_source_profile(session, source) if source is not None else None
    default_rights = str(rights_class or (profile.rights_class if profile is not None else "public"))
    default_review = bool(review_required if review_required is not None else (profile.review_required if profile is not None else False))
    if row is None:
        row = InstitutionalEvidenceItem(
            evidence_key=str(evidence_key).strip(),
            source_id=int(source.id) if source is not None else None,
            source_profile_id=int(profile.source_id) if profile is not None else None,
            document_id=int(document_id) if document_id is not None else None,
            subject_id=int(subject_row.id) if subject_row is not None else None,
            issuer_id=int(issuer.id) if issuer is not None else None,
            company_id=int(company.id) if company is not None else None,
            site_id=int(site.id) if site is not None else None,
            program_id=int(program.id) if program is not None else None,
            title=str(title).strip() or "Institutional evidence",
            evidence_kind=str(evidence_kind).strip() or "document",
            provenance_summary=str(provenance_summary or "").strip() or None,
            url=str(url or "").strip() or None,
            canonical_url=str(canonical_url or url or "").strip() or None,
            published_at=published_at,
            observed_at=observed_at,
            rights_class=default_rights,
            review_required=default_review,
            confidence=confidence,
            payload=dict(payload or {}),
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.flush()
        return row
    row.title = str(title).strip() or row.title
    row.evidence_kind = str(evidence_kind).strip() or row.evidence_kind
    row.source_id = int(source.id) if source is not None else row.source_id
    row.source_profile_id = int(profile.source_id) if profile is not None else row.source_profile_id
    row.document_id = int(document_id) if document_id is not None else row.document_id
    row.subject_id = int(subject_row.id) if subject_row is not None else row.subject_id
    row.issuer_id = int(issuer.id) if issuer is not None else row.issuer_id
    row.company_id = int(company.id) if company is not None else row.company_id
    row.site_id = int(site.id) if site is not None else row.site_id
    row.program_id = int(program.id) if program is not None else row.program_id
    if provenance_summary is not None:
        row.provenance_summary = str(provenance_summary).strip() or None
    if url is not None:
        row.url = str(url).strip() or None
    if canonical_url is not None or url is not None:
        row.canonical_url = str(canonical_url or url or "").strip() or None
    if published_at is not None:
        row.published_at = published_at
    if observed_at is not None:
        row.observed_at = observed_at
    row.rights_class = default_rights
    row.review_required = default_review
    if confidence is not None:
        row.confidence = float(confidence)
    if payload is not None:
        merged = dict(row.payload or {})
        merged.update(payload)
        row.payload = merged
    row.updated_at = now
    session.add(row)
    session.flush()
    return row


def upsert_observation(
    session: Session,
    *,
    observation_key: str,
    kind: str,
    metric_key: str | None,
    evidence: InstitutionalEvidenceItem | None = None,
    source: Source | None = None,
    subject_row: ResearchSubject | None = None,
    issuer: Issuer | None = None,
    company: Company | None = None,
    site: Site | None = None,
    program: Program | None = None,
    dataset_family: str | None = None,
    value_number: float | None = None,
    value_text: str | None = None,
    unit: str | None = None,
    currency: str | None = None,
    confidence: float | None = None,
    period_date: date | None = None,
    as_of_date: date | None = None,
    observed_at: datetime | None = None,
    review_status: str = "accepted",
    payload: dict[str, Any] | None = None,
) -> InstitutionalObservation:
    row = session.query(InstitutionalObservation).filter_by(observation_key=str(observation_key).strip()).first()
    now = _now()
    if row is None:
        row = InstitutionalObservation(
            observation_key=str(observation_key).strip(),
            evidence_id=int(evidence.id) if evidence is not None else None,
            source_id=int(source.id) if source is not None else None,
            subject_id=int(subject_row.id) if subject_row is not None else None,
            issuer_id=int(issuer.id) if issuer is not None else None,
            company_id=int(company.id) if company is not None else None,
            site_id=int(site.id) if site is not None else None,
            program_id=int(program.id) if program is not None else None,
            dataset_family=str(dataset_family or "").strip() or None,
            kind=str(kind).strip(),
            metric_key=str(metric_key or "").strip() or None,
            value_number=value_number,
            value_text=str(value_text or "").strip() or None,
            unit=str(unit or "").strip() or None,
            currency=str(currency or "").strip() or None,
            confidence=confidence,
            period_date=period_date,
            as_of_date=as_of_date,
            observed_at=observed_at,
            review_status=str(review_status or "accepted"),
            payload=dict(payload or {}),
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.flush()
        return row
    row.evidence_id = int(evidence.id) if evidence is not None else row.evidence_id
    row.source_id = int(source.id) if source is not None else row.source_id
    row.subject_id = int(subject_row.id) if subject_row is not None else row.subject_id
    row.issuer_id = int(issuer.id) if issuer is not None else row.issuer_id
    row.company_id = int(company.id) if company is not None else row.company_id
    row.site_id = int(site.id) if site is not None else row.site_id
    row.program_id = int(program.id) if program is not None else row.program_id
    row.dataset_family = str(dataset_family or row.dataset_family or "").strip() or None
    row.kind = str(kind).strip() or row.kind
    row.metric_key = str(metric_key or row.metric_key or "").strip() or None
    row.value_number = value_number
    row.value_text = str(value_text or "").strip() or None
    row.unit = str(unit or "").strip() or None
    row.currency = str(currency or "").strip() or None
    row.confidence = confidence
    row.period_date = period_date
    row.as_of_date = as_of_date
    row.observed_at = observed_at
    row.review_status = str(review_status or row.review_status or "accepted")
    if payload is not None:
        merged = dict(row.payload or {})
        merged.update(payload)
        row.payload = merged
    row.updated_at = now
    session.add(row)
    session.flush()
    return row


def _connector_kind(source: Source | None) -> str:
    connector = str((source.config or {}).get("connector") if source is not None else "").strip().lower()
    if connector.startswith("trade_"):
        return "trade"
    if connector in {"sam_gov_contracts", "eu_ted_search", "chips_funding"}:
        return "procurement"
    if connector in {"eia_860m", "pjm_planning_center", "caiso_interconnection_queue", "ercot_large_load", "ercot_llwg"}:
        return "power"
    if connector in {"copernicus_browser", "usgs_earthexplorer", "usgs_naip"}:
        return "imagery"
    if connector in {"cbp_cross", "usitc_hts", "eu_taric"}:
        return "classification"
    if connector in {"fcc_oet_kdb"}:
        return "certification"
    if connector in {"patentsview_search"}:
        return "technical"
    if connector in {"sec_edgar", "sec_edgar_rss", "sec_companyfacts", "opendart_disclosures", "opendart_financials", "taiwan_mops_monthly_revenue", "taiwan_mops_investor_conferences", "tpex_infohub_company", "twse_openapi", "krx_kind_disclosure", "hkexnews_title_search", "cninfo_announcements", "tdnet_company_announcements", "edinet_disclosures", "edinetdb_financials"}:
        return "estimate_revision"
    return "signal"


def _infer_subject_for_document(session: Session, doc: Document) -> tuple[ResearchSubject | None, Company | None, Issuer | None]:
    company = None
    issuer = None
    if doc.source_id is not None:
        source = session.query(Source).filter_by(id=int(doc.source_id)).first()
        company_slug = None
        extra = doc.extra if isinstance(doc.extra, dict) else {}
        if extra.get("company_slug"):
            company_slug = str(extra.get("company_slug")).strip()
        elif source is not None:
            company_slug = str((source.config or {}).get("company_slug") or "").strip() or None
        if company_slug:
            company = session.query(Company).filter_by(slug=company_slug).first()
    if company is None and isinstance(doc.extra, dict):
        company_name = str(doc.extra.get("company_name") or "").strip()
        if company_name:
            company = session.query(Company).filter_by(name=company_name).first()
    if company is not None and company.issuer_id is not None:
        issuer = session.query(Issuer).filter_by(id=int(company.issuer_id)).first()
    subject_row = _ensure_subject_for_company(session, company, issuer)
    if subject_row is not None:
        external_ids: list[tuple[str, str]] = []
        if issuer is not None and issuer.lei:
            external_ids.append(("lei", str(issuer.lei)))
        if company is not None and company.ticker:
            external_ids.append(("ticker", str(company.ticker)))
        ensure_entity_identity(session, subject_row=subject_row, company=company, issuer=issuer, external_ids=external_ids)
    return subject_row, company, issuer


def sync_document_to_institutional(
    session: Session,
    document_or_id: Document | int,
) -> InstitutionalObservation | None:
    doc = document_or_id if isinstance(document_or_id, Document) else session.query(Document).filter_by(id=int(document_or_id)).first()
    if doc is None or doc.source_id is None:
        return None
    source = session.query(Source).filter_by(id=int(doc.source_id)).first()
    if source is None:
        return None
    subject_row, company, issuer = _infer_subject_for_document(session, doc)
    observed_at = doc.published_at or doc.ingested_at
    evidence = upsert_evidence_item(
        session,
        evidence_key=f"document:{int(doc.id)}",
        title=str(doc.title or doc.url or f"Document {doc.id}"),
        evidence_kind=str(doc.kind or "document"),
        source=source,
        document_id=int(doc.id),
        subject_row=subject_row,
        issuer=issuer,
        company=company,
        provenance_summary=str(source.name or source.config.get('connector') or "source").strip() or None,
        url=str(doc.url or "").strip() or None,
        canonical_url=str(doc.canonical_url or doc.url or "").strip() or None,
        published_at=doc.published_at,
        observed_at=observed_at,
        rights_class=None,
        review_required=None,
        confidence=float(doc.language_confidence) if doc.language_confidence is not None else None,
        payload={
            "document_kind": doc.kind,
            "triage_state": doc.triage_state,
            "saved": bool(doc.saved),
            "language": doc.language,
            "source_name": source.name,
        },
    )
    kind = _connector_kind(source)
    payload = dict(doc.extra or {}) if isinstance(doc.extra, dict) else {}
    obs = upsert_observation(
        session,
        observation_key=f"document_obs:{int(doc.id)}",
        kind=kind,
        metric_key=str(payload.get("metric_key") or kind).strip() or kind,
        evidence=evidence,
        source=source,
        subject_row=subject_row,
        issuer=issuer,
        company=company,
        dataset_family=str(source.config.get("connector") or "").strip() or None,
        value_number=None,
        value_text=str(doc.title or doc.normalized_text or doc.url or "").strip()[:500] or None,
        unit=str(payload.get("unit") or "").strip() or None,
        currency=str(payload.get("currency") or "").strip() or None,
        confidence=float(doc.language_confidence) if doc.language_confidence is not None else None,
        period_date=None,
        as_of_date=doc.published_at.date() if doc.published_at is not None else None,
        observed_at=observed_at,
        review_status="accepted",
        payload={
            "document_id": int(doc.id),
            "document_kind": doc.kind,
            "connector": source.config.get("connector"),
            "language": doc.language,
            "extra": payload,
        },
    )
    if subject_row is not None:
        recompute_state_vectors(session, subject_id=int(subject_row.id))
        refresh_coverage_gaps(session, subject_id=int(subject_row.id))
    return obs


def sync_structured_fact_to_institutional(session: Session, fact: StructuredDataFact) -> InstitutionalObservation:
    source = session.query(Source).filter_by(id=int(fact.source_id)).first() if fact.source_id is not None else None
    company = session.query(Company).filter_by(id=int(fact.company_id)).first() if fact.company_id is not None else None
    issuer = session.query(Issuer).filter_by(id=int(company.issuer_id)).first() if company is not None and company.issuer_id is not None else None
    subject_row = _ensure_subject_for_company(session, company, issuer)
    if subject_row is not None:
        external_ids: list[tuple[str, str]] = []
        if issuer is not None and issuer.lei:
            external_ids.append(("lei", str(issuer.lei)))
        if company is not None and company.ticker:
            external_ids.append(("ticker", str(company.ticker)))
        ensure_entity_identity(session, subject_row=subject_row, company=company, issuer=issuer, external_ids=external_ids)
    evidence = upsert_evidence_item(
        session,
        evidence_key=f"structured_fact:{fact.dataset_family}:{fact.fact_key}",
        title=str(fact.metric_label or fact.metric_key or fact.fact_key),
        evidence_kind="structured_fact",
        source=source,
        document_id=int(fact.document_id) if fact.document_id is not None else None,
        subject_row=subject_row,
        issuer=issuer,
        company=company,
        provenance_summary=str(fact.provenance_label or fact.dataset_family or "").strip() or None,
        url=str(fact.provenance_url or "").strip() or None,
        canonical_url=str(fact.provenance_url or "").strip() or None,
        rights_class="public",
        review_required=False,
        confidence=None,
        payload={
            "dataset_family": fact.dataset_family,
            "group_key": fact.group_key,
            "statement_type": fact.statement_type,
            "fact_id": int(fact.id),
        },
    )
    kind = (
        "trade"
        if fact.dataset_family == "trade_customs"
        else "guidance"
        if fact.dataset_family == "guidance_actuals"
        else "earnings"
        if fact.dataset_family == "earnings_actuals"
        else "metric"
    )
    obs = upsert_observation(
        session,
        observation_key=f"institutional_obs:{fact.dataset_family}:{fact.fact_key}",
        kind=kind,
        metric_key=str(fact.metric_key or "").strip() or None,
        evidence=evidence,
        source=source,
        subject_row=subject_row,
        issuer=issuer,
        company=company,
        dataset_family=str(fact.dataset_family or "").strip() or None,
        value_number=float(fact.value_number) if fact.value_number is not None else None,
        value_text=str(fact.value_text or "").strip() or None,
        unit=str(fact.unit or "").strip() or None,
        currency=str(fact.currency or "").strip() or None,
        confidence=None,
        period_date=fact.period_date,
        as_of_date=fact.as_of_date,
        observed_at=evidence.published_at,
        payload={
            "group_key": fact.group_key,
            "statement_type": fact.statement_type,
            "metric_label": fact.metric_label,
            "provenance_url": fact.provenance_url,
            "evidence_tier": fact.evidence_tier,
        },
    )
    # An unbound structured fact cannot change any subject's institutional state.
    # Passing ``None`` here means "all subjects" to both helpers, so doing that
    # once per unbound fact turns backfills into repeated full-table rescans and
    # keeps the outer transaction alive indefinitely on large stores.
    if subject_row is not None:
        recompute_state_vectors(session, subject_id=int(subject_row.id))
        refresh_coverage_gaps(session, subject_id=int(subject_row.id))
    return obs


def backfill_structured_facts(
    session: Session,
    *,
    dataset_families: list[str] | None = None,
    limit: int | None = None,
) -> dict[str, int]:
    q = session.query(StructuredDataFact).order_by(StructuredDataFact.id.asc())
    families = [str(item).strip() for item in (dataset_families or []) if str(item).strip()]
    if families:
        q = q.filter(StructuredDataFact.dataset_family.in_(families))
    if limit is not None:
        q = q.limit(max(1, int(limit)))
    seen = 0
    synced = 0
    for fact in q.all():
        seen += 1
        sync_structured_fact_to_institutional(session, fact)
        synced += 1
    return {"seen": seen, "synced": synced}


def recompute_state_vectors(
    session: Session,
    *,
    subject_id: int | None = None,
    site_id: int | None = None,
    program_id: int | None = None,
) -> int:
    q = session.query(InstitutionalObservation)
    if subject_id is not None:
        q = q.filter(InstitutionalObservation.subject_id == int(subject_id))
    if site_id is not None:
        q = q.filter(InstitutionalObservation.site_id == int(site_id))
    if program_id is not None:
        q = q.filter(InstitutionalObservation.program_id == int(program_id))
    rows = q.order_by(InstitutionalObservation.updated_at.desc(), InstitutionalObservation.id.desc()).all()
    grouped: dict[tuple[str, str], list[InstitutionalObservation]] = defaultdict(list)
    for row in rows:
        if row.program_id is not None:
            grouped[("program", f"program:{int(row.program_id)}")].append(row)
        elif row.site_id is not None:
            grouped[("site", f"site:{int(row.site_id)}")].append(row)
        elif row.subject_id is not None:
            grouped[("subject", f"subject:{int(row.subject_id)}")].append(row)
    now = _now()
    count = 0
    for (scope_type, scope_key), obs_rows in grouped.items():
        latest_by_metric: dict[str, InstitutionalObservation] = {}
        for row in obs_rows:
            metric = normalize_metric_key(row.metric_key or row.kind) or str(row.metric_key or row.kind or "metric")
            latest_by_metric.setdefault(metric, row)
        snapshot_key = f"state:{scope_key}"
        state = session.query(StateVectorSnapshot).filter_by(snapshot_key=snapshot_key).first()
        if state is None:
            state = StateVectorSnapshot(
                snapshot_key=snapshot_key,
                scope_type=scope_type,
                scope_key=scope_key,
                subject_id=obs_rows[0].subject_id,
                site_id=obs_rows[0].site_id if scope_type == "site" else None,
                program_id=obs_rows[0].program_id if scope_type == "program" else None,
                payload={},
                created_at=now,
                updated_at=now,
            )
            session.add(state)
        installed_capacity = latest_by_metric.get("installed_capacity")
        effective_capacity = latest_by_metric.get("effective_capacity")
        utilization = latest_by_metric.get("utilization")
        yield_rate = latest_by_metric.get("yield_rate")
        lead_time = latest_by_metric.get("lead_time_days") or latest_by_metric.get("lead_time_weeks")
        inventory = latest_by_metric.get("inventory_days")
        state.subject_id = obs_rows[0].subject_id
        state.site_id = obs_rows[0].site_id if scope_type == "site" else state.site_id
        state.program_id = obs_rows[0].program_id if scope_type == "program" else state.program_id
        state.installed_capacity = installed_capacity.value_number if installed_capacity is not None else state.installed_capacity
        state.effective_capacity = effective_capacity.value_number if effective_capacity is not None else state.effective_capacity
        state.utilization = utilization.value_number if utilization is not None else state.utilization
        state.yield_rate = yield_rate.value_number if yield_rate is not None else state.yield_rate
        if lead_time is not None:
            lead_val = float(lead_time.value_number) if lead_time.value_number is not None else None
            if lead_val is not None and str(lead_time.metric_key or "").endswith("_weeks"):
                lead_val = lead_val * 7.0
            state.lead_time_days = lead_val
        state.inventory_days = inventory.value_number if inventory is not None else state.inventory_days
        state.allocation_status = str(latest_by_metric.get("allocation_status").value_text) if latest_by_metric.get("allocation_status") is not None and latest_by_metric.get("allocation_status").value_text else state.allocation_status
        state.qualification_status = str(latest_by_metric.get("qualification_status").value_text) if latest_by_metric.get("qualification_status") is not None and latest_by_metric.get("qualification_status").value_text else state.qualification_status
        state.demand_signal = str(latest_by_metric.get("demand_signal").value_text) if latest_by_metric.get("demand_signal") is not None and latest_by_metric.get("demand_signal").value_text else state.demand_signal
        state.schedule_status = str(latest_by_metric.get("schedule_status").value_text) if latest_by_metric.get("schedule_status") is not None and latest_by_metric.get("schedule_status").value_text else state.schedule_status
        state.logistics_friction = str(latest_by_metric.get("logistics_friction").value_text) if latest_by_metric.get("logistics_friction") is not None and latest_by_metric.get("logistics_friction").value_text else state.logistics_friction
        state.confidence = (
            sum(float(row.confidence or 0.0) for row in obs_rows if row.confidence is not None) / max(1, len([row for row in obs_rows if row.confidence is not None]))
            if any(row.confidence is not None for row in obs_rows)
            else state.confidence
        )
        state.freshness_at = max((row.updated_at for row in obs_rows if row.updated_at is not None), default=state.freshness_at)
        state.contradiction_count = 0
        state.payload = {
            "observation_count": len(obs_rows),
            "latest_metrics": sorted(latest_by_metric.keys()),
            "state_metric_fields": {
                key: metric_state_field(key)
                for key in sorted(latest_by_metric.keys())
                if metric_state_field(key)
            },
        }
        state.updated_at = now
        session.add(state)
        count += 1
    session.flush()
    return count


def _subject_has_institutional_observation_scope(session: Session, subject_row: ResearchSubject) -> bool:
    """Return true only when stale-observation gaps are meaningful for this subject."""
    subject_id = int(subject_row.id)
    if (
        session.query(Site.id)
        .filter(Site.subject_id == subject_id)
        .filter(Site.status != "archived")
        .first()
        is not None
    ):
        return True
    if (
        session.query(Program.id)
        .filter(Program.subject_id == subject_id)
        .filter(Program.status != "archived")
        .first()
        is not None
    ):
        return True
    extra = subject_row.extra if isinstance(subject_row.extra, dict) else {}
    return bool(
        extra.get("institutional_tracking_enabled")
        or extra.get("institutional_observation_required")
        or extra.get("institutional_scope")
    )


def refresh_coverage_gaps(
    session: Session,
    *,
    subject_id: int | None = None,
    site_id: int | None = None,
    program_id: int | None = None,
) -> int:
    now = _now()
    count = 0
    subject_rows: list[ResearchSubject] = []
    if subject_id is not None:
        row = session.query(ResearchSubject).filter_by(id=int(subject_id)).first()
        if row is not None:
            subject_rows = [row]
    elif site_id is None and program_id is None:
        subject_rows = session.query(ResearchSubject).order_by(ResearchSubject.id.asc()).all()
    for subject_row in subject_rows:
        gap_key = f"coverage_gap:subject:{int(subject_row.id)}:stale_observations"
        if not _subject_has_institutional_observation_scope(session, subject_row):
            gap = session.query(CoverageGap).filter_by(gap_key=gap_key).first()
            if gap is not None and gap.status != "resolved":
                gap.status = "resolved"
                gap.last_reviewed_at = now
                gap.updated_at = now
                gap.payload = {
                    **dict(gap.payload or {}),
                    "resolved_by": "institutional_scope_gate",
                    "resolved_at": now.isoformat(),
                    "reason": "Subject has no active institutional site/program scope.",
                }
                session.add(gap)
            continue
        latest_obs = (
            session.query(InstitutionalObservation)
            .filter(InstitutionalObservation.subject_id == int(subject_row.id))
            .order_by(InstitutionalObservation.updated_at.desc(), InstitutionalObservation.id.desc())
            .first()
        )
        stale = latest_obs is None or latest_obs.updated_at < (now - timedelta(days=30))
        gap = session.query(CoverageGap).filter_by(gap_key=gap_key).first()
        if stale:
            if gap is None:
                gap = CoverageGap(
                    gap_key=gap_key,
                    subject_id=int(subject_row.id),
                    title=f"Stale observations for {subject_row.display_name}",
                    gap_kind="stale_observations",
                    status="open",
                    severity=3,
                    summary="Subject has no recent institutional observations.",
                    suggested_rails=["disclosures", "trade", "power", "permits"],
                    payload={},
                    created_at=now,
                    updated_at=now,
                )
                session.add(gap)
            else:
                gap.status = "open"
                gap.updated_at = now
                session.add(gap)
            count += 1
        elif gap is not None and gap.status != "resolved":
            gap.status = "resolved"
            gap.last_reviewed_at = now
            gap.updated_at = now
            session.add(gap)
    session.flush()
    return count


def recompute_program_bottleneck(session: Session, *, program_id: int) -> BottleneckAssessment | None:
    program = session.query(Program).filter_by(id=int(program_id)).first()
    if program is None:
        return None
    nodes = (
        session.query(ProgramNode)
        .filter_by(program_id=int(program.id))
        .order_by(ProgramNode.constraining_score.desc().nullslast(), ProgramNode.id.asc())
        .all()
    )
    if not nodes:
        return None
    active = nodes[0]
    shadow = nodes[1] if len(nodes) > 1 else None
    now = _now()
    row = (
        session.query(BottleneckAssessment)
        .filter_by(program_id=int(program.id), status="active")
        .order_by(BottleneckAssessment.updated_at.desc(), BottleneckAssessment.id.desc())
        .first()
    )
    if row is None:
        row = BottleneckAssessment(
            program_id=int(program.id),
            subject_id=int(program.subject_id) if program.subject_id is not None else None,
            site_id=int(program.site_id) if program.site_id is not None else None,
            created_at=now,
            updated_at=now,
            payload={},
        )
        session.add(row)
    row.active_node_key = str(active.node_key)
    row.shadow_node_key = str(shadow.node_key) if shadow is not None else None
    row.classification = "supply_constrained" if active.constraining_score and active.constraining_score >= 0.5 else "watch"
    row.summary = f"Active bottleneck: {active.name}" + (f"; shadow bottleneck: {shadow.name}" if shadow is not None else "")
    row.confidence = active.confidence
    row.updated_at = now
    row.payload = {
        "active_score": active.constraining_score,
        "shadow_score": shadow.constraining_score if shadow is not None else None,
    }
    session.add(row)
    session.flush()
    return row


def institutional_ops_summary(session: Session) -> dict[str, Any]:
    open_gaps = (
        session.query(CoverageGap)
        .filter(CoverageGap.status != "resolved")
        .order_by(CoverageGap.severity.desc(), CoverageGap.updated_at.desc(), CoverageGap.id.desc())
        .limit(10)
        .all()
    )
    recent_sites = session.query(Site).order_by(Site.updated_at.desc(), Site.id.desc()).limit(8).all()
    recent_programs = session.query(Program).order_by(Program.updated_at.desc(), Program.id.desc()).limit(8).all()
    latest_states = (
        session.query(StateVectorSnapshot)
        .order_by(StateVectorSnapshot.updated_at.desc(), StateVectorSnapshot.id.desc())
        .limit(8)
        .all()
    )
    return {
        "counts": {
            "sources": session.query(Source).count(),
            "source_profiles": session.query(SourceProfile).count(),
            "sites": session.query(Site).count(),
            "programs": session.query(Program).count(),
            "evidence_items": session.query(InstitutionalEvidenceItem).count(),
            "observations": session.query(InstitutionalObservation).count(),
            "open_gaps": session.query(CoverageGap).filter(CoverageGap.status != "resolved").count(),
            "active_bottlenecks": session.query(BottleneckAssessment).filter(BottleneckAssessment.status == "active").count(),
        },
        "recent_sites": [serialize_site(row) for row in recent_sites],
        "recent_programs": [serialize_program(row) for row in recent_programs],
        "open_gaps": [serialize_gap(row) for row in open_gaps],
        "latest_states": [serialize_state_vector(row) for row in latest_states],
    }


def serialize_source_profile(row: SourceProfile | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "source_id": int(row.source_id),
        "source_family": row.source_family,
        "rights_class": row.rights_class,
        "acquisition_mode": row.acquisition_mode,
        "cadence_label": row.cadence_label,
        "review_required": bool(row.review_required),
        "subject_binding_kind": row.subject_binding_kind,
        "site_binding_kind": row.site_binding_kind,
        "jurisdiction_template_kind": row.jurisdiction_template_kind,
        "vendor_tier": row.vendor_tier,
        "notes": row.notes,
        "extra": dict(row.extra or {}),
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def serialize_site(row: Site, *, include_programs: bool = False) -> dict[str, Any]:
    data = {
        "id": int(row.id),
        "slug": row.slug,
        "name": row.name,
        "site_kind": row.site_kind,
        "status": row.status,
        "subject_id": int(row.subject_id) if row.subject_id is not None else None,
        "issuer_id": int(row.issuer_id) if row.issuer_id is not None else None,
        "company_id": int(row.company_id) if row.company_id is not None else None,
        "identity_id": int(row.identity_id) if row.identity_id is not None else None,
        "operator_name": row.operator_name,
        "owner_name": row.owner_name,
        "utility_name": row.utility_name,
        "address_line1": row.address_line1,
        "locality": row.locality,
        "region": row.region,
        "postal_code": row.postal_code,
        "country_code": row.country_code,
        "parcel_id": row.parcel_id,
        "latitude": row.latitude,
        "longitude": row.longitude,
        "extra": dict(row.extra or {}),
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
    if include_programs:
        data["programs"] = [serialize_program(program) for program in row.programs]
    return data


def serialize_program(row: Program, *, include_graph: bool = False) -> dict[str, Any]:
    data = {
        "id": int(row.id),
        "slug": row.slug,
        "title": row.title,
        "category": row.category,
        "status": row.status,
        "summary": row.summary,
        "subject_id": int(row.subject_id) if row.subject_id is not None else None,
        "issuer_id": int(row.issuer_id) if row.issuer_id is not None else None,
        "company_id": int(row.company_id) if row.company_id is not None else None,
        "site_id": int(row.site_id) if row.site_id is not None else None,
        "extra": dict(row.extra or {}),
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
    if include_graph:
        data["nodes"] = [
            {
                "id": int(node.id),
                "node_key": node.node_key,
                "name": node.name,
                "node_type": node.node_type,
                "dependency_kind": node.dependency_kind,
                "manufacturer": node.manufacturer,
                "active": bool(node.active),
                "constraining_score": node.constraining_score,
                "attachment_ratio": node.attachment_ratio,
                "quantity": node.quantity,
                "unit": node.unit,
                "confidence": node.confidence,
                "notes": node.notes,
                "extra": dict(node.extra or {}),
            }
            for node in row.nodes
        ]
        data["edges"] = [
            {
                "id": int(edge.id),
                "source_node_id": int(edge.source_node_id),
                "target_node_id": int(edge.target_node_id),
                "edge_kind": edge.edge_kind,
                "confidence": edge.confidence,
                "notes": edge.notes,
                "extra": dict(edge.extra or {}),
            }
            for edge in row.edges
        ]
    return data


def serialize_observation(row: InstitutionalObservation) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "observation_key": row.observation_key,
        "kind": row.kind,
        "metric_key": row.metric_key,
        "dataset_family": row.dataset_family,
        "evidence_id": int(row.evidence_id) if row.evidence_id is not None else None,
        "source_id": int(row.source_id) if row.source_id is not None else None,
        "subject_id": int(row.subject_id) if row.subject_id is not None else None,
        "issuer_id": int(row.issuer_id) if row.issuer_id is not None else None,
        "company_id": int(row.company_id) if row.company_id is not None else None,
        "site_id": int(row.site_id) if row.site_id is not None else None,
        "program_id": int(row.program_id) if row.program_id is not None else None,
        "value_number": row.value_number,
        "value_text": row.value_text,
        "unit": row.unit,
        "currency": row.currency,
        "confidence": row.confidence,
        "period_date": row.period_date.isoformat() if row.period_date else None,
        "as_of_date": row.as_of_date.isoformat() if row.as_of_date else None,
        "observed_at": row.observed_at.isoformat() if row.observed_at else None,
        "review_status": row.review_status,
        "payload": dict(row.payload or {}),
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def serialize_gap(row: CoverageGap) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "gap_key": row.gap_key,
        "title": row.title,
        "gap_kind": row.gap_kind,
        "status": row.status,
        "severity": int(row.severity or 0),
        "subject_id": int(row.subject_id) if row.subject_id is not None else None,
        "site_id": int(row.site_id) if row.site_id is not None else None,
        "program_id": int(row.program_id) if row.program_id is not None else None,
        "summary": row.summary,
        "suggested_rails": list(row.suggested_rails or []),
        "payload": dict(row.payload or {}),
        "last_reviewed_at": row.last_reviewed_at.isoformat() if row.last_reviewed_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def serialize_bottleneck(row: BottleneckAssessment | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": int(row.id),
        "model_id": int(row.model_id) if row.model_id is not None else None,
        "program_id": int(row.program_id) if row.program_id is not None else None,
        "subject_id": int(row.subject_id) if row.subject_id is not None else None,
        "site_id": int(row.site_id) if row.site_id is not None else None,
        "supply_chain_id": int(row.supply_chain_id) if row.supply_chain_id is not None else None,
        "snapshot_id": int(row.snapshot_id) if row.snapshot_id is not None else None,
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
        "status": row.status,
        "axes": {
            "bindingness": row.bindingness_score,
            "shadow_price": row.shadow_price_score,
            "ramp_resistance": row.ramp_resistance_score,
            "substitutability": row.substitutability_score,
            "persistence": row.persistence_score,
            "latent_demand_absorption": row.latent_demand_absorption_score,
        },
        "qualified_supplier_count": int(row.qualified_supplier_count) if row.qualified_supplier_count is not None else None,
        "marginal_system_leverage": dict(row.marginal_system_leverage or {}),
        "supplier_qualification": dict(row.supplier_qualification_json or {}),
        "evidence": list(row.evidence_json or []),
        "scorecard": dict(row.scorecard_json or {}),
        "payload": dict(row.payload or {}),
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def serialize_state_vector(row: StateVectorSnapshot) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "snapshot_key": row.snapshot_key,
        "scope_type": row.scope_type,
        "scope_key": row.scope_key,
        "subject_id": int(row.subject_id) if row.subject_id is not None else None,
        "site_id": int(row.site_id) if row.site_id is not None else None,
        "program_id": int(row.program_id) if row.program_id is not None else None,
        "installed_capacity": row.installed_capacity,
        "effective_capacity": row.effective_capacity,
        "utilization": row.utilization,
        "lead_time_days": row.lead_time_days,
        "inventory_days": row.inventory_days,
        "demand_signal": row.demand_signal,
        "schedule_status": row.schedule_status,
        "logistics_friction": row.logistics_friction,
        "confidence": row.confidence,
        "freshness_at": row.freshness_at.isoformat() if row.freshness_at else None,
        "contradiction_count": int(row.contradiction_count or 0),
        "payload": dict(row.payload or {}),
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def list_sites(
    session: Session,
    *,
    query: str | None = None,
    subject_id: int | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    q = session.query(Site).order_by(Site.updated_at.desc(), Site.id.desc())
    if subject_id is not None:
        q = q.filter(Site.subject_id == int(subject_id))
    query_text = str(query or "").strip().lower()
    rows = q.limit(max(1, min(int(limit or 100), 500))).all()
    out: list[dict[str, Any]] = []
    for row in rows:
        hay = " ".join(
            [
                str(row.slug or ""),
                str(row.name or ""),
                str(row.operator_name or ""),
                str(row.locality or ""),
                str(row.region or ""),
            ]
        ).lower()
        if query_text and query_text not in hay:
            continue
        out.append(serialize_site(row))
    return out


def get_site_payload(session: Session, *, site_id: int) -> dict[str, Any] | None:
    row = session.query(Site).filter_by(id=int(site_id)).first()
    if row is None:
        return None
    state = session.query(StateVectorSnapshot).filter_by(scope_type="site", scope_key=f"site:{int(row.id)}").first()
    observations = (
        session.query(InstitutionalObservation)
        .filter(InstitutionalObservation.site_id == int(row.id))
        .order_by(InstitutionalObservation.updated_at.desc(), InstitutionalObservation.id.desc())
        .limit(50)
        .all()
    )
    gaps = (
        session.query(CoverageGap)
        .filter(CoverageGap.site_id == int(row.id))
        .order_by(CoverageGap.severity.desc(), CoverageGap.updated_at.desc(), CoverageGap.id.desc())
        .limit(20)
        .all()
    )
    return {
        "site": serialize_site(row, include_programs=True),
        "state_vector": serialize_state_vector(state) if state is not None else None,
        "observations": [serialize_observation(obs) for obs in observations],
        "gaps": [serialize_gap(gap) for gap in gaps],
    }


def list_programs(
    session: Session,
    *,
    query: str | None = None,
    subject_id: int | None = None,
    site_id: int | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    q = session.query(Program).order_by(Program.updated_at.desc(), Program.id.desc())
    if subject_id is not None:
        q = q.filter(Program.subject_id == int(subject_id))
    if site_id is not None:
        q = q.filter(Program.site_id == int(site_id))
    query_text = str(query or "").strip().lower()
    rows = q.limit(max(1, min(int(limit or 100), 500))).all()
    out: list[dict[str, Any]] = []
    for row in rows:
        hay = " ".join([str(row.slug or ""), str(row.title or ""), str(row.category or ""), str(row.summary or "")]).lower()
        if query_text and query_text not in hay:
            continue
        out.append(serialize_program(row))
    return out


def get_program_payload(session: Session, *, program_id: int) -> dict[str, Any] | None:
    row = session.query(Program).filter_by(id=int(program_id)).first()
    if row is None:
        return None
    state = session.query(StateVectorSnapshot).filter_by(scope_type="program", scope_key=f"program:{int(row.id)}").first()
    observations = (
        session.query(InstitutionalObservation)
        .filter(InstitutionalObservation.program_id == int(row.id))
        .order_by(InstitutionalObservation.updated_at.desc(), InstitutionalObservation.id.desc())
        .limit(50)
        .all()
    )
    gaps = (
        session.query(CoverageGap)
        .filter(CoverageGap.program_id == int(row.id))
        .order_by(CoverageGap.severity.desc(), CoverageGap.updated_at.desc(), CoverageGap.id.desc())
        .limit(20)
        .all()
    )
    bottleneck = (
        session.query(BottleneckAssessment)
        .filter(BottleneckAssessment.program_id == int(row.id))
        .order_by(BottleneckAssessment.updated_at.desc(), BottleneckAssessment.id.desc())
        .first()
    )
    return {
        "program": serialize_program(row, include_graph=True),
        "state_vector": serialize_state_vector(state) if state is not None else None,
        "bottleneck": serialize_bottleneck(bottleneck),
        "observations": [serialize_observation(obs) for obs in observations],
        "gaps": [serialize_gap(gap) for gap in gaps],
    }


def list_observations(
    session: Session,
    *,
    subject_id: int | None = None,
    site_id: int | None = None,
    program_id: int | None = None,
    kind: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    q = session.query(InstitutionalObservation).order_by(InstitutionalObservation.updated_at.desc(), InstitutionalObservation.id.desc())
    if subject_id is not None:
        q = q.filter(InstitutionalObservation.subject_id == int(subject_id))
    if site_id is not None:
        q = q.filter(InstitutionalObservation.site_id == int(site_id))
    if program_id is not None:
        q = q.filter(InstitutionalObservation.program_id == int(program_id))
    if kind:
        q = q.filter(InstitutionalObservation.kind == str(kind).strip())
    rows = q.limit(max(1, min(int(limit or 100), 500))).all()
    return [serialize_observation(row) for row in rows]


def list_gaps(
    session: Session,
    *,
    subject_id: int | None = None,
    site_id: int | None = None,
    program_id: int | None = None,
    open_only: bool = True,
    limit: int = 100,
) -> list[dict[str, Any]]:
    q = session.query(CoverageGap).order_by(CoverageGap.severity.desc(), CoverageGap.updated_at.desc(), CoverageGap.id.desc())
    if subject_id is not None:
        q = q.filter(CoverageGap.subject_id == int(subject_id))
    if site_id is not None:
        q = q.filter(CoverageGap.site_id == int(site_id))
    if program_id is not None:
        q = q.filter(CoverageGap.program_id == int(program_id))
    if open_only:
        q = q.filter(CoverageGap.status != "resolved")
    rows = q.limit(max(1, min(int(limit or 100), 500))).all()
    return [serialize_gap(row) for row in rows]


def get_bottleneck(session: Session, *, program_id: int) -> dict[str, Any] | None:
    row = (
        session.query(BottleneckAssessment)
        .filter(BottleneckAssessment.program_id == int(program_id))
        .order_by(BottleneckAssessment.updated_at.desc(), BottleneckAssessment.id.desc())
        .first()
    )
    return serialize_bottleneck(row)


def enqueue_institutional_refresh(
    session: Session,
    *,
    source_id: int | None = None,
    document_id: int | None = None,
    subject_id: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"force": bool(force)}
    if source_id is not None:
        payload["source_id"] = int(source_id)
    if document_id is not None:
        payload["document_id"] = int(document_id)
    if subject_id is not None:
        payload["subject_id"] = int(subject_id)
    dedupe_target = (
        f"source:{int(source_id)}"
        if source_id is not None
        else f"document:{int(document_id)}"
        if document_id is not None
        else f"subject:{int(subject_id)}"
        if subject_id is not None
        else "all"
    )
    job = enqueue_job(
        session,
        kind="institutional_refresh",
        payload=payload,
        dedupe_key=None if force else f"institutional_refresh:{dedupe_target}",
        priority=3,
    )
    return {"job_id": int(job.id), "kind": job.kind, "payload": payload}


def enqueue_closure_pass(
    session: Session,
    *,
    program_id: int | None = None,
    subject_id: int | None = None,
    site_id: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"force": bool(force)}
    if program_id is not None:
        payload["program_id"] = int(program_id)
    if subject_id is not None:
        payload["subject_id"] = int(subject_id)
    if site_id is not None:
        payload["site_id"] = int(site_id)
    dedupe_target = (
        f"program:{int(program_id)}"
        if program_id is not None
        else f"subject:{int(subject_id)}"
        if subject_id is not None
        else f"site:{int(site_id)}"
        if site_id is not None
        else "all"
    )
    job = enqueue_job(
        session,
        kind="institutional_closure_pass",
        payload=payload,
        dedupe_key=None if force else f"institutional_closure_pass:{dedupe_target}",
        priority=4,
    )
    return {"job_id": int(job.id), "kind": job.kind, "payload": payload}


def handle_institutional_refresh_job(payload: dict[str, Any]) -> dict[str, Any]:
    from thoth_terminal.db import session_scope

    with session_scope() as session:
        sync_source_profiles(session)
        processed_docs = 0
        processed_facts = 0
        source_id = payload.get("source_id")
        document_id = payload.get("document_id")
        subject_id = payload.get("subject_id")
        if document_id is not None:
            if sync_document_to_institutional(session, int(document_id)) is not None:
                processed_docs += 1
        elif source_id is not None:
            docs = (
                session.query(Document)
                .filter(Document.source_id == int(source_id))
                .order_by(Document.id.desc())
                .limit(500)
                .all()
            )
            for doc in docs:
                if sync_document_to_institutional(session, doc) is not None:
                    processed_docs += 1
            facts = (
                session.query(StructuredDataFact)
                .filter(StructuredDataFact.source_id == int(source_id))
                .order_by(StructuredDataFact.id.asc())
                .all()
            )
            for fact in facts:
                sync_structured_fact_to_institutional(session, fact)
                processed_facts += 1
        elif subject_id is not None:
            subject_row = session.query(ResearchSubject).filter_by(id=int(subject_id)).first()
            company_id = int(subject_row.company_id) if subject_row is not None and subject_row.company_id is not None else None
            if company_id is not None:
                facts = (
                    session.query(StructuredDataFact)
                    .filter(StructuredDataFact.company_id == company_id)
                    .order_by(StructuredDataFact.id.asc())
                    .all()
                )
                for fact in facts:
                    sync_structured_fact_to_institutional(session, fact)
                    processed_facts += 1
                docs = (
                    session.query(Document)
                    .filter(Document.normalized_text.is_not(None))
                    .order_by(Document.id.desc())
                    .limit(1000)
                    .all()
                )
                company_slug = str(subject_row.company.slug) if subject_row.company is not None else None
                for doc in docs:
                    extra = dict(doc.extra or {}) if isinstance(doc.extra, dict) else {}
                    if company_slug and str(extra.get("company_slug") or "").strip() == company_slug:
                        if sync_document_to_institutional(session, doc) is not None:
                            processed_docs += 1
        else:
            backfill_stats = backfill_structured_facts(session, limit=None)
            processed_facts += int(backfill_stats.get("synced") or 0)
            docs = session.query(Document).order_by(Document.id.desc()).limit(1000).all()
            for doc in docs:
                if sync_document_to_institutional(session, doc) is not None:
                    processed_docs += 1
        return {
            "processed_docs": processed_docs,
            "processed_facts": processed_facts,
            "counts": institutional_ops_summary(session)["counts"],
        }


def handle_institutional_closure_pass_job(payload: dict[str, Any]) -> dict[str, Any]:
    from thoth_terminal.db import session_scope

    with session_scope() as session:
        program_id = payload.get("program_id")
        subject_id = payload.get("subject_id")
        site_id = payload.get("site_id")
        programs_query = session.query(Program)
        if program_id is not None:
            programs_query = programs_query.filter(Program.id == int(program_id))
        if subject_id is not None:
            programs_query = programs_query.filter(Program.subject_id == int(subject_id))
        if site_id is not None:
            programs_query = programs_query.filter(Program.site_id == int(site_id))
        programs = programs_query.order_by(Program.id.asc()).all()
        closed = 0
        for program in programs:
            recompute_program_bottleneck(session, program_id=int(program.id))
            recompute_state_vectors(session, program_id=int(program.id))
            refresh_coverage_gaps(session, program_id=int(program.id))
            closed += 1
        if subject_id is not None:
            recompute_state_vectors(session, subject_id=int(subject_id))
            refresh_coverage_gaps(session, subject_id=int(subject_id))
        if site_id is not None:
            recompute_state_vectors(session, site_id=int(site_id))
            refresh_coverage_gaps(session, site_id=int(site_id))
        return {"programs_closed": closed}
