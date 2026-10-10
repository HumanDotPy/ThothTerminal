from __future__ import annotations

from sqlalchemy import or_

from thoth_terminal.web.pagination import build_traditional_pagination
from thoth_terminal.web.app_sections._shared import bind_app_context


bind_app_context(globals())


@app.get("/companies", response_class=HTMLResponse)
def companies_list(
    request: Request,
    region: str | None = None,
    q: str | None = None,
    page: int = 1,
    page_size: int = 50,
    db: Session = Depends(get_session),
) -> HTMLResponse:
    """List issuers with three-rail coverage indicators."""
    region_map = {
        "US": {"US", "CA"},
        "APAC": {"JP", "KR", "TW", "CN", "HK", "SG", "IN", "AU"},
        "EU": {"GB", "DE", "FR", "NL", "CH", "SE", "FI", "IT", "ES"},
    }
    region_key = str(region or "").strip().upper()
    query_text = str(q or "").strip()
    page_size = max(25, min(int(page_size or 50), 100))
    issuers_query = db.query(Issuer).order_by(Issuer.display_name.asc())
    if region_key in region_map:
        issuers_query = issuers_query.filter(Issuer.jurisdiction.in_(region_map[region_key]))
    if query_text:
        like = f"%{query_text}%"
        issuers_query = issuers_query.filter(
            or_(
                Issuer.display_name.ilike(like),
                Issuer.slug.ilike(like),
                Issuer.sector.ilike(like),
                Issuer.jurisdiction.ilike(like),
            )
        )
    total_issuers = int(issuers_query.order_by(None).count())
    pagination = build_traditional_pagination(
        base_path="/companies",
        page=page,
        page_size=page_size,
        total=total_issuers,
        params={"region": region_key if region_key in region_map else "", "q": query_text},
    )
    issuers_raw = (
        issuers_query
        .offset((int(pagination["page"]) - 1) * page_size)
        .limit(page_size)
        .all()
    )
    issuer_ids = [int(row.id) for row in issuers_raw]

    primary_tickers: dict[int, IssuerIdentifier] = {}
    if issuer_ids:
        for ident in (
            db.query(IssuerIdentifier)
            .filter(IssuerIdentifier.issuer_id.in_(issuer_ids))
            .filter(IssuerIdentifier.id_type == "TICKER")
            .order_by(IssuerIdentifier.issuer_id.asc(), IssuerIdentifier.is_primary.desc(), IssuerIdentifier.id.asc())
            .all()
        ):
            issuer_id = int(ident.issuer_id)
            if issuer_id not in primary_tickers:
                primary_tickers[issuer_id] = ident

    def _count_by_issuer(model: Any, column: Any) -> dict[int, int]:
        if not issuer_ids:
            return {}
        return {
            int(issuer_id): int(count or 0)
            for issuer_id, count in (
                db.query(column, func.count(model.id))
                .filter(column.in_(issuer_ids))
                .group_by(column)
                .all()
            )
        }

    filing_counts = _count_by_issuer(SecuritiesFiling, SecuritiesFiling.issuer_id)
    disclosure_counts = _count_by_issuer(ExchangeDisclosure, ExchangeDisclosure.issuer_id)
    ir_counts = _count_by_issuer(IRArtifact, IRArtifact.issuer_id)

    next_earnings_by_issuer: dict[int, EarningsEvent] = {}
    if issuer_ids:
        for event in (
            db.query(EarningsEvent)
            .filter(EarningsEvent.issuer_id.in_(issuer_ids))
            .filter(EarningsEvent.scheduled_date >= calendar_today())
            .order_by(EarningsEvent.issuer_id.asc(), EarningsEvent.scheduled_date.asc())
            .all()
        ):
            issuer_id = int(event.issuer_id)
            if issuer_id not in next_earnings_by_issuer:
                next_earnings_by_issuer[issuer_id] = event

    issuers_data: list[dict] = []
    for issuer in issuers_raw:
        issuer_id = int(issuer.id)
        ident = primary_tickers.get(issuer_id)
        next_earnings_event = next_earnings_by_issuer.get(issuer_id)
        payload = {
            "id": issuer_id,
            "slug": issuer.slug,
            "display_name": issuer.display_name,
            "primary_ticker": str(getattr(ident, "id_value", "") or "").strip() or None,
            "jurisdiction": issuer.jurisdiction,
            "sector": issuer.sector,
            "lei": issuer.lei,
            "filing_count": filing_counts.get(issuer_id, 0),
            "disclosure_count": disclosure_counts.get(issuer_id, 0),
            "ir_count": ir_counts.get(issuer_id, 0),
            "next_earnings": next_earnings_event.scheduled_date if next_earnings_event else None,
            "earnings_provenance": next_earnings_event.date_provenance if next_earnings_event else None,
            "href": f"/companies/{issuer.slug}",
        }
        issuers_data.append(payload)

    # Get recent filings across all issuers
    recent_filings: list[dict] = []
    seven_days_ago = utcnow() - timedelta(days=7)

    # Rail 1: Securities Filings
    for filing in (
        db.query(SecuritiesFiling, Issuer)
        .join(Issuer, Issuer.id == SecuritiesFiling.issuer_id)
        .filter(SecuritiesFiling.filed_at >= seven_days_ago)
        .order_by(SecuritiesFiling.filed_at.desc())
        .limit(10)
        .all()
    ):
        recent_filings.append({
            "rail": "rail1",
            "type_label": filing[0].filing_type,
            "issuer_id": filing[1].id,
            "issuer_name": filing[1].display_name,
            "title": filing[0].title or filing[0].filing_type,
            "filed_at": filing[0].filed_at,
            "url": clean_url(filing[0].primary_document_url or filing[0].filing_url),
        })

    # Rail 2: Exchange Disclosures
    for disclosure in (
        db.query(ExchangeDisclosure, Issuer)
        .join(Issuer, Issuer.id == ExchangeDisclosure.issuer_id)
        .filter(ExchangeDisclosure.announced_at >= seven_days_ago)
        .order_by(ExchangeDisclosure.announced_at.desc())
        .limit(10)
        .all()
    ):
        recent_filings.append({
            "rail": "rail2",
            "type_label": disclosure[0].disclosure_type[:20],
            "issuer_id": disclosure[1].id,
            "issuer_name": disclosure[1].display_name,
            "title": disclosure[0].title or disclosure[0].disclosure_type,
            "filed_at": disclosure[0].announced_at,
            "url": disclosure[0].disclosure_url,
        })

    # Rail 3: IR Artifacts
    for artifact in (
        db.query(IRArtifact, Issuer)
        .join(Issuer, Issuer.id == IRArtifact.issuer_id)
        .filter(IRArtifact.published_at >= seven_days_ago)
        .order_by(IRArtifact.published_at.desc())
        .limit(10)
        .all()
    ):
        recent_filings.append({
            "rail": "rail3",
            "type_label": artifact[0].artifact_type,
            "issuer_id": artifact[1].id,
            "issuer_name": artifact[1].display_name,
            "title": artifact[0].title or artifact[0].artifact_type,
            "filed_at": artifact[0].published_at,
            "url": artifact[0].canonical_url,
        })

    # Sort all recent filings by date
    recent_filings.sort(key=lambda x: x["filed_at"] or datetime.min, reverse=True)

    return _template_response(
        "companies.html",
        {
            "request": request,
            "issuers": issuers_data,
            "total_issuers": total_issuers,
            "region_filter": region,
            "query": query_text,
            "pagination": pagination,
            "recent_filings": recent_filings[:20],
        },
    )


@app.get("/companies/{slug}", response_class=HTMLResponse)
def company_detail(request: Request, slug: str, sync: int = 0, db: Session = Depends(get_session)) -> HTMLResponse:
    issuer, company = _resolve_company_or_issuer(db, raw_id=slug, slug=slug)
    if company is None and issuer is not None:
        company = _primary_company_for_issuer(db, int(issuer.id))
    payload = _company_or_issuer_payload(db, issuer=issuer, company=company, include_resolution_request_count=False)
    if payload is None:
        raise HTTPException(status_code=404, detail="Company not found.")

    issuer_row = issuer
    company_row = company
    if issuer_row is None and company_row is not None and getattr(company_row, "issuer_id", None) is not None:
        issuer_row = db.query(Issuer).filter_by(id=int(company_row.issuer_id)).first()
    if company_row is None and payload.get("company_id") is not None:
        company_row = db.query(Company).filter_by(id=int(payload["company_id"])).first()

    filings = []
    disclosures = []
    ir_artifacts = []
    if issuer_row is not None:
        filings = (
            db.query(SecuritiesFiling)
            .options(
                load_only(
                    SecuritiesFiling.id,
                    SecuritiesFiling.document_id,
                    SecuritiesFiling.title,
                    SecuritiesFiling.filing_type,
                    SecuritiesFiling.filed_at,
                    SecuritiesFiling.primary_document_url,
                    SecuritiesFiling.filing_url,
                )
            )
            .filter(SecuritiesFiling.issuer_id == int(issuer_row.id))
            .order_by(SecuritiesFiling.filed_at.desc())
            .limit(25)
            .all()
        )
        disclosures = (
            db.query(ExchangeDisclosure)
            .options(
                load_only(
                    ExchangeDisclosure.id,
                    ExchangeDisclosure.title,
                    ExchangeDisclosure.disclosure_type,
                    ExchangeDisclosure.disclosure_url,
                    ExchangeDisclosure.announced_at,
                )
            )
            .filter(ExchangeDisclosure.issuer_id == int(issuer_row.id))
            .order_by(ExchangeDisclosure.announced_at.desc())
            .limit(25)
            .all()
        )
        ir_artifacts = (
            db.query(IRArtifact)
            .options(
                load_only(
                    IRArtifact.id,
                    IRArtifact.title,
                    IRArtifact.artifact_type,
                    IRArtifact.canonical_url,
                    IRArtifact.published_at,
                    IRArtifact.created_at,
                )
            )
            .filter(IRArtifact.issuer_id == int(issuer_row.id))
            .order_by(IRArtifact.published_at.desc().nullslast(), IRArtifact.created_at.desc())
            .limit(25)
            .all()
        )

    evidence_items: list[dict[str, object]] = []
    if issuer_row is not None or company_row is not None:
        q = (
            db.query(DiscordEvidenceItem, DiscordEvidenceTarget)
            .outerjoin(DiscordEvidenceTarget, DiscordEvidenceTarget.evidence_id == DiscordEvidenceItem.id)
            .order_by(
                DiscordEvidenceItem.created_at.desc(),
                DiscordEvidenceTarget.is_primary.desc(),
                DiscordEvidenceTarget.id.asc(),
            )
        )
        filters = []
        if issuer_row is not None:
            filters.append(DiscordEvidenceTarget.issuer_id == int(issuer_row.id))
        if company_row is not None:
            filters.append(DiscordEvidenceTarget.company_id == int(company_row.id))
        if filters:
            q = q.filter(or_(*filters))
            seen_ids: set[int] = set()
            for evidence, target in q.limit(120).all():
                evidence_id = int(evidence.id)
                if evidence_id in seen_ids:
                    continue
                seen_ids.add(evidence_id)
                evidence_items.append(
                    {
                        "id": evidence_id,
                        "title": evidence.title,
                        "url": evidence.url_canonical or evidence.url_raw,
                        "created_at": evidence.created_at,
                        "sentiment": evidence.sentiment,
                        "source_type": evidence.source_type,
                        "resolution_state": evidence.resolution_state,
                        "target_label": (str(target.target_label) if target and target.target_label else None),
                        "target_kind": (str(target.target_kind) if target else None),
                    }
                )
                if len(evidence_items) >= 25:
                    break

    legacy_documents = []
    if company_row is not None:
        legacy_documents = (
            db.query(CompanyDocument, Document)
            .options(
                Load(CompanyDocument).load_only(
                    CompanyDocument.id,
                    CompanyDocument.company_id,
                    CompanyDocument.document_id,
                    CompanyDocument.doc_type,
                ),
                Load(Document).load_only(
                    Document.id,
                    Document.title,
                    Document.url,
                    Document.published_at,
                    Document.ingested_at,
                ),
            )
            .join(Document, Document.id == CompanyDocument.document_id)
            .filter(CompanyDocument.company_id == int(company_row.id))
            .order_by(Document.published_at.desc().nullslast(), Document.ingested_at.desc())
            .limit(25)
            .all()
        )

    open_requests = (
        _issuer_resolution_requests(
            db,
            issuer=issuer_row,
            company=company_row,
            primary_ticker=str(payload.get("primary_ticker") or "").strip() or None,
        )
        if issuer_row is not None
        else []
    )
    payload["open_company_resolution_request_count"] = len(open_requests)
    latest_report = None
    report_row = None
    if issuer_row is not None:
        report_row = db.query(ResearchReport).filter_by(subject_key=f"issuer:{int(issuer_row.id)}").first()
    elif company_row is not None:
        report_row = db.query(ResearchReport).filter_by(subject_key=f"company:{int(company_row.id)}").first()
    if report_row is not None:
        latest_report_row = None
        latest_version_id = getattr(report_row, "latest_published_version_id", None)
        if latest_version_id is not None:
            latest_report_row = (
                db.query(ResearchReportVersion)
                .filter(ResearchReportVersion.id == int(latest_version_id), ResearchReportVersion.status == "published")
                .first()
            )
        if latest_report_row is None:
            latest_report_row = (
                db.query(ResearchReportVersion)
                .filter_by(report_id=int(report_row.id), status="published")
                .order_by(ResearchReportVersion.version.desc(), ResearchReportVersion.id.desc())
                .first()
            )
        latest_report = serialize_report_summary(report_row, latest_report_row, subject=resolve_report_subject(db, report_row))
    linked_knowledge_assets: list[dict[str, Any]] = []
    if payload.get("slug"):
        try:
            linked_knowledge_assets = list_knowledge_assets(
                db,
                subject=str(payload.get("slug")),
                published_only=True,
                include_version_content=False,
                limit=12,
            )
        except Exception:
            linked_knowledge_assets = []

    decision_payload: dict[str, Any] = {}
    decision_sync_stats: dict[str, int] = {}
    if int(sync or 0):
        try:
            decision_sync_stats = sync_decision_spine(
                db,
                company_id=int(company_row.id) if company_row is not None else None,
                issuer_id=int(issuer_row.id) if issuer_row is not None else None,
            )
            db.commit()
        except Exception:
            db.rollback()
            decision_sync_stats = {}
    try:
        decision_payload = cached_company_decision_page_payload(db, company=company_row, issuer=issuer_row)
    except Exception:
        db.rollback()
        decision_payload = {}

    investor_metrics: list[dict[str, Any]] = []
    investor_timeline: list[dict[str, Any]] = []
    investor_datasets: list[dict[str, Any]] = []
    if company_row is not None:
        fact_company_ids = company_structured_fact_scope_ids(db, company_row)
        fact_rows = (
            db.query(StructuredDataFact)
            .options(
                load_only(
                    StructuredDataFact.id,
                    StructuredDataFact.dataset_id,
                    StructuredDataFact.period_date,
                    StructuredDataFact.period_label,
                    StructuredDataFact.as_of_date,
                    StructuredDataFact.metric_key,
                    StructuredDataFact.metric_label,
                    StructuredDataFact.statement_type,
                    StructuredDataFact.value_number,
                    StructuredDataFact.value_text,
                    StructuredDataFact.unit,
                    StructuredDataFact.currency,
                    StructuredDataFact.provenance_label,
                    StructuredDataFact.provenance_url,
                )
            )
            .filter(StructuredDataFact.company_id.in_(fact_company_ids))
            .order_by(StructuredDataFact.as_of_date.desc().nullslast(), StructuredDataFact.id.desc())
            .limit(500)
            .all()
        ) if fact_company_ids else []
        latest_by_metric: dict[str, StructuredDataFact] = {}
        for fact in fact_rows:
            key = str(fact.metric_key or "").strip()
            if key and key not in latest_by_metric and fact.value_number is not None:
                latest_by_metric[key] = fact
        preferred = [
            "revenue",
            "operating_income",
            "operating_margin",
            "net_income",
            "eps",
            "capex",
            "rnd_expenses",
            "roe",
            "equity_ratio",
        ]
        ordered_metrics = [latest_by_metric[k] for k in preferred if k in latest_by_metric]
        ordered_metrics.extend([v for k, v in latest_by_metric.items() if k not in preferred])
        for fact in ordered_metrics[:12]:
            investor_metrics.append(
                {
                    "metric_key": fact.metric_key,
                    "metric_label": fact.metric_label or fact.metric_key,
                    "value_number": fact.value_number,
                    "value_text": fact.value_text,
                    "unit": fact.unit,
                    "currency": fact.currency,
                    "period_label": fact.period_label,
                    "as_of_date": fact.as_of_date,
                    "statement_type": fact.statement_type,
                    "source_url": fact.provenance_url,
                    "source_label": fact.provenance_label,
                }
            )
        for fact in fact_rows[:40]:
            investor_timeline.append(
                {
                    "kind": "fact",
                    "date": fact.as_of_date or fact.period_date,
                    "title": fact.metric_label or fact.metric_key,
                    "detail": fact.period_label or fact.statement_type,
                    "value_number": fact.value_number,
                    "unit": fact.unit,
                    "currency": fact.currency,
                    "href": fact.provenance_url,
                    "source": fact.provenance_label,
                }
            )
        dataset_ids = sorted({int(f.dataset_id) for f in fact_rows if f.dataset_id is not None})
        if dataset_ids:
            for dataset in (
                db.query(Dataset)
                .options(load_only(Dataset.id, Dataset.title, Dataset.family, Dataset.updated_at))
                .filter(Dataset.id.in_(dataset_ids))
                .order_by(Dataset.updated_at.desc())
                .limit(8)
                .all()
            ):
                investor_datasets.append(
                    {
                        "id": int(dataset.id),
                        "title": dataset.title,
                        "family": dataset.family,
                        "updated_at": dataset.updated_at,
                        "href": f"/datasets/{dataset.id}",
                    }
                )

    return _template_response(
        "company.html",
        {
            "request": request,
            "company": payload,
            "identifiers": list(payload.get("identifiers") or []),
            "evidence_items": evidence_items,
            "filings": filings,
            "disclosures": disclosures,
            "ir_artifacts": ir_artifacts,
            "legacy_documents": legacy_documents,
            "open_requests": open_requests,
            "latest_report": latest_report,
            "linked_knowledge_assets": linked_knowledge_assets,
            "decision_payload": decision_payload,
            "decision_sync_stats": decision_sync_stats,
            "investor_metrics": investor_metrics,
            "investor_timeline": investor_timeline,
            "investor_datasets": investor_datasets,
        },
    )
