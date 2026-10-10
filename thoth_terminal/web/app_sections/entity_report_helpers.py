from __future__ import annotations

from thoth_terminal.web.app_sections._shared import bind_app_context


bind_app_context(globals())


def _source_payload(source: Source, state: SourceState | None, last_run: IngestionRun | None) -> dict[str, object]:
    enabled_flag = source.config.get("enabled")
    if enabled_flag is None:
        enabled_flag = not bool(source.config.get("disabled_reason"))
    return {
        "id": source.id,
        "name": source.name,
        "category": source.category,
        "connector": source.config.get("connector"),
        "enabled": bool(enabled_flag),
        "priority": source.priority,
        "interval_s": source.polling_interval_seconds,
        "config": source.config,
        "profile": serialize_source_profile(getattr(source, "profile", None)),
        "last_success_at": state.last_success_at.isoformat() if state and state.last_success_at else None,
        "last_run_at": state.last_run_at.isoformat() if state and state.last_run_at else None,
        "next_run_at": state.next_run_at.isoformat() if state and state.next_run_at else None,
        "backoff_until": state.backoff_until.isoformat() if state and state.backoff_until else None,
        "consecutive_failures": int(state.consecutive_failures or 0) if state else 0,
        "last_status": last_run.status if last_run else None,
        "last_error": last_run.error if last_run and last_run.error else None,
        "last_started": last_run.started_at.isoformat() if last_run and last_run.started_at else None,
        "last_items_new": last_run.items_new if last_run else None,
        "last_items_updated": last_run.items_updated if last_run else None,
    }


def _parse_iso_utc_naive(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value or "").strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(raw)
        except Exception:
            return None
    if dt.tzinfo:
        dt = dt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    return dt


def _company_payload(company: Company) -> dict[str, object]:
    return {
        "id": company.id,
        "slug": company.slug,
        "name": company.name,
        "ticker": company.ticker,
        "exchange": company.exchange,
        "region": company.region,
        "sector": company.sector,
        "industry": company.industry,
        "supply_chain_stage": company.supply_chain_stage,
        "ir_url": company.ir_url,
        "filings_url": company.filings_url,
        "reports_url": company.reports_url,
        "calendar_url": company.calendar_url,
    }


def _primary_company_for_issuer(session: Session, issuer_id: int) -> Company | None:
    return (
        session.query(Company)
        .filter(Company.issuer_id == int(issuer_id))
        .order_by(Company.id.asc())
        .first()
    )


def _issuer_identifiers(session: Session, issuer_id: int) -> list[IssuerIdentifier]:
    return (
        session.query(IssuerIdentifier)
        .filter(IssuerIdentifier.issuer_id == int(issuer_id))
        .order_by(IssuerIdentifier.id_type.asc(), IssuerIdentifier.is_primary.desc(), IssuerIdentifier.id.asc())
        .all()
    )


def _primary_ticker_identifier(session: Session, issuer_id: int) -> IssuerIdentifier | None:
    return (
        session.query(IssuerIdentifier)
        .filter(IssuerIdentifier.issuer_id == int(issuer_id), IssuerIdentifier.id_type == "TICKER")
        .order_by(IssuerIdentifier.is_primary.desc(), IssuerIdentifier.id.asc())
        .first()
    )


def _issuer_resolution_requests(
    session: Session,
    *,
    issuer: Issuer,
    company: Company | None = None,
    primary_ticker: str | None = None,
) -> list[InstrumentRequest]:
    name_tokens = {
        str(getattr(issuer, "display_name", "") or "").strip().lower(),
        str(getattr(issuer, "legal_name", "") or "").strip().lower(),
        str(getattr(company, "name", "") or "").strip().lower(),
        str(primary_ticker or "").strip().lower(),
    }
    name_tokens = {token for token in name_tokens if token}
    if not name_tokens:
        return []
    rows = (
        session.query(InstrumentRequest)
        .options(
            load_only(
                InstrumentRequest.id,
                InstrumentRequest.title,
                InstrumentRequest.summary,
                InstrumentRequest.target_entities,
                InstrumentRequest.provenance_summary,
                InstrumentRequest.status,
                InstrumentRequest.updated_at,
            )
        )
        .filter(InstrumentRequest.intent == "company_resolution")
        .filter(InstrumentRequest.status.in_(["draft", "review"]))
        .order_by(InstrumentRequest.updated_at.desc())
        .all()
    )
    matched: list[InstrumentRequest] = []
    for row in rows:
        haystacks = [
            str(row.title or "").strip().lower(),
            str(row.summary or "").strip().lower(),
            str(row.provenance_summary or "").strip().lower(),
        ]
        haystacks.extend([str(v or "").strip().lower() for v in (row.target_entities or []) if str(v or "").strip()])
        if any(token and any(token in hay for hay in haystacks) for token in name_tokens):
            matched.append(row)
    return matched


def _issuer_payload(
    session: Session,
    issuer: Issuer,
    company: Company | None = None,
    *,
    include_resolution_request_count: bool = True,
) -> dict[str, object]:
    company_row = company or _primary_company_for_issuer(session, int(issuer.id))
    identifiers = _issuer_identifiers(session, int(issuer.id))
    primary_ticker_ident = next((row for row in identifiers if row.id_type == "TICKER" and bool(row.is_primary)), None)
    if primary_ticker_ident is None:
        primary_ticker_ident = next((row for row in identifiers if row.id_type == "TICKER"), None)
    primary_ticker = str(getattr(primary_ticker_ident, "id_value", "") or "").strip() or None
    primary_exchange = str(getattr(primary_ticker_ident, "exchange", "") or "").strip() or getattr(company_row, "exchange", None)

    filing_count = int(session.query(func.count(SecuritiesFiling.id)).filter(SecuritiesFiling.issuer_id == int(issuer.id)).scalar() or 0)
    disclosure_count = int(session.query(func.count(ExchangeDisclosure.id)).filter(ExchangeDisclosure.issuer_id == int(issuer.id)).scalar() or 0)
    ir_count = int(session.query(func.count(IRArtifact.id)).filter(IRArtifact.issuer_id == int(issuer.id)).scalar() or 0)
    evidence_q = session.query(func.count(func.distinct(DiscordEvidenceTarget.evidence_id)))
    if company_row is not None:
        evidence_q = evidence_q.filter(
            or_(
                DiscordEvidenceTarget.issuer_id == int(issuer.id),
                DiscordEvidenceTarget.company_id == int(company_row.id),
            )
        )
    else:
        evidence_q = evidence_q.filter(DiscordEvidenceTarget.issuer_id == int(issuer.id))
    evidence_count = int(evidence_q.scalar() or 0)
    open_request_count = 0
    if include_resolution_request_count:
        open_request_count = len(
            _issuer_resolution_requests(
                session,
                issuer=issuer,
                company=company_row,
                primary_ticker=primary_ticker,
            )
        )
    company_doc_counts = {}
    if company_row is not None:
        company_doc_counts = {
            str(doc_type): int(count or 0)
            for doc_type, count in (
                session.query(CompanyDocument.doc_type, func.count(CompanyDocument.id))
                .filter(CompanyDocument.company_id == int(company_row.id))
                .group_by(CompanyDocument.doc_type)
                .all()
            )
        }

    return {
        "id": int(issuer.id),
        "slug": issuer.slug,
        "name": issuer.display_name,
        "display_name": issuer.display_name,
        "legal_name": issuer.legal_name,
        "ticker": primary_ticker,
        "primary_ticker": primary_ticker,
        "exchange": primary_exchange,
        "jurisdiction": issuer.jurisdiction,
        "sector": issuer.sector or getattr(company_row, "sector", None),
        "industry": issuer.industry or getattr(company_row, "industry", None),
        "supply_chain_stage": getattr(company_row, "supply_chain_stage", None),
        "website": issuer.website,
        "ir_url": getattr(company_row, "ir_url", None) or issuer.ir_website or issuer.website,
        "filings_url": getattr(company_row, "filings_url", None),
        "reports_url": getattr(company_row, "reports_url", None),
        "calendar_url": getattr(company_row, "calendar_url", None),
        "company_id": int(company_row.id) if company_row is not None else None,
        "company_slug": getattr(company_row, "slug", None),
        "is_private": bool((issuer.extra or {}).get("private")) or str(issuer.entity_status or "").strip().upper() == "PRIVATE",
        "identifiers": [
            {
                "type": row.id_type,
                "value": row.id_value,
                "exchange": row.exchange,
                "is_primary": bool(row.is_primary),
            }
            for row in identifiers
        ],
        "filing_count": filing_count,
        "disclosure_count": disclosure_count,
        "ir_count": ir_count,
        "evidence_count": evidence_count,
        "open_company_resolution_request_count": open_request_count,
        "doc_counts": company_doc_counts,
    }


def _count_by_key(session: Session, model: Any, column: Any, keys: list[int]) -> dict[int, int]:
    if not keys:
        return {}
    return {
        int(key): int(count or 0)
        for key, count in (
            session.query(column, func.count(model.id))
            .filter(column.in_(keys))
            .group_by(column)
            .all()
        )
    }


def _primary_companies_by_issuer_id(session: Session, issuer_ids: list[int]) -> dict[int, Company]:
    if not issuer_ids:
        return {}
    out: dict[int, Company] = {}
    for company in (
        session.query(Company)
        .filter(Company.issuer_id.in_(issuer_ids))
        .order_by(Company.issuer_id.asc(), Company.id.asc())
        .all()
    ):
        issuer_id = int(company.issuer_id or 0)
        if issuer_id and issuer_id not in out:
            out[issuer_id] = company
    return out


def _identifiers_by_issuer_id(session: Session, issuer_ids: list[int]) -> dict[int, list[IssuerIdentifier]]:
    if not issuer_ids:
        return {}
    out: dict[int, list[IssuerIdentifier]] = {}
    for ident in (
        session.query(IssuerIdentifier)
        .filter(IssuerIdentifier.issuer_id.in_(issuer_ids))
        .order_by(IssuerIdentifier.issuer_id.asc(), IssuerIdentifier.id_type.asc(), IssuerIdentifier.is_primary.desc(), IssuerIdentifier.id.asc())
        .all()
    ):
        out.setdefault(int(ident.issuer_id), []).append(ident)
    return out


def _company_doc_counts_by_company_id(session: Session, company_ids: list[int]) -> dict[int, dict[str, int]]:
    if not company_ids:
        return {}
    out: dict[int, dict[str, int]] = {}
    for company_id, doc_type, count in (
        session.query(CompanyDocument.company_id, CompanyDocument.doc_type, func.count(CompanyDocument.id))
        .filter(CompanyDocument.company_id.in_(company_ids))
        .group_by(CompanyDocument.company_id, CompanyDocument.doc_type)
        .all()
    ):
        out.setdefault(int(company_id), {})[str(doc_type)] = int(count or 0)
    return out


def _discord_evidence_counts_by_issuer_id(
    session: Session,
    issuer_ids: list[int],
    *,
    company_by_issuer_id: dict[int, Company],
) -> dict[int, int]:
    if not issuer_ids:
        return {}
    issuer_id_set = set(issuer_ids)
    company_to_issuer = {int(company.id): int(issuer_id) for issuer_id, company in company_by_issuer_id.items()}
    clauses = [DiscordEvidenceTarget.issuer_id.in_(issuer_ids)]
    if company_to_issuer:
        clauses.append(DiscordEvidenceTarget.company_id.in_(list(company_to_issuer)))
    evidence_by_issuer: dict[int, set[int]] = {}
    for evidence_id, issuer_id, company_id in (
        session.query(DiscordEvidenceTarget.evidence_id, DiscordEvidenceTarget.issuer_id, DiscordEvidenceTarget.company_id)
        .filter(or_(*clauses))
        .all()
    ):
        evidence_id_int = int(evidence_id)
        if issuer_id is not None and int(issuer_id) in issuer_id_set:
            evidence_by_issuer.setdefault(int(issuer_id), set()).add(evidence_id_int)
        if company_id is not None and int(company_id) in company_to_issuer:
            evidence_by_issuer.setdefault(company_to_issuer[int(company_id)], set()).add(evidence_id_int)
    return {issuer_id: len(evidence_ids) for issuer_id, evidence_ids in evidence_by_issuer.items()}


def _resolution_request_counts_by_issuer_id(
    session: Session,
    issuers: list[Issuer],
    *,
    company_by_issuer_id: dict[int, Company],
    primary_ticker_by_issuer_id: dict[int, str | None],
) -> dict[int, int]:
    if not issuers:
        return {}
    request_haystacks: list[list[str]] = []
    for row in (
        session.query(
            InstrumentRequest.title,
            InstrumentRequest.summary,
            InstrumentRequest.provenance_summary,
            InstrumentRequest.target_entities,
        )
        .filter(InstrumentRequest.intent == "company_resolution")
        .filter(InstrumentRequest.status.in_(["draft", "review"]))
        .all()
    ):
        haystacks = [
            str(row.title or "").strip().lower(),
            str(row.summary or "").strip().lower(),
            str(row.provenance_summary or "").strip().lower(),
        ]
        haystacks.extend([str(v or "").strip().lower() for v in (row.target_entities or []) if str(v or "").strip()])
        request_haystacks.append(haystacks)
    if not request_haystacks:
        return {}
    counts: dict[int, int] = {}
    for issuer in issuers:
        issuer_id = int(issuer.id)
        company = company_by_issuer_id.get(issuer_id)
        name_tokens = {
            str(getattr(issuer, "display_name", "") or "").strip().lower(),
            str(getattr(issuer, "legal_name", "") or "").strip().lower(),
            str(getattr(company, "name", "") or "").strip().lower(),
            str(primary_ticker_by_issuer_id.get(issuer_id) or "").strip().lower(),
        }
        name_tokens = {token for token in name_tokens if token}
        if not name_tokens:
            continue
        counts[issuer_id] = sum(1 for haystacks in request_haystacks if any(token in hay for token in name_tokens for hay in haystacks))
    return counts


def _issuer_payloads_bulk(session: Session, issuers: list[Issuer]) -> dict[int, dict[str, object]]:
    issuer_ids = [int(issuer.id) for issuer in issuers]
    if not issuer_ids:
        return {}
    company_by_issuer_id = _primary_companies_by_issuer_id(session, issuer_ids)
    identifiers_by_issuer_id = _identifiers_by_issuer_id(session, issuer_ids)
    filing_counts = _count_by_key(session, SecuritiesFiling, SecuritiesFiling.issuer_id, issuer_ids)
    disclosure_counts = _count_by_key(session, ExchangeDisclosure, ExchangeDisclosure.issuer_id, issuer_ids)
    ir_counts = _count_by_key(session, IRArtifact, IRArtifact.issuer_id, issuer_ids)
    evidence_counts = _discord_evidence_counts_by_issuer_id(session, issuer_ids, company_by_issuer_id=company_by_issuer_id)
    company_doc_counts = _company_doc_counts_by_company_id(session, [int(company.id) for company in company_by_issuer_id.values()])

    primary_ticker_by_issuer_id: dict[int, str | None] = {}
    primary_exchange_by_issuer_id: dict[int, str | None] = {}
    for issuer_id, identifiers in identifiers_by_issuer_id.items():
        primary_ticker_ident = next((row for row in identifiers if row.id_type == "TICKER" and bool(row.is_primary)), None)
        if primary_ticker_ident is None:
            primary_ticker_ident = next((row for row in identifiers if row.id_type == "TICKER"), None)
        primary_ticker_by_issuer_id[issuer_id] = str(getattr(primary_ticker_ident, "id_value", "") or "").strip() or None
        primary_exchange_by_issuer_id[issuer_id] = str(getattr(primary_ticker_ident, "exchange", "") or "").strip() or None

    request_counts = _resolution_request_counts_by_issuer_id(
        session,
        issuers,
        company_by_issuer_id=company_by_issuer_id,
        primary_ticker_by_issuer_id=primary_ticker_by_issuer_id,
    )

    out: dict[int, dict[str, object]] = {}
    for issuer in issuers:
        issuer_id = int(issuer.id)
        company_row = company_by_issuer_id.get(issuer_id)
        identifiers = identifiers_by_issuer_id.get(issuer_id, [])
        primary_ticker = primary_ticker_by_issuer_id.get(issuer_id)
        primary_exchange = primary_exchange_by_issuer_id.get(issuer_id) or getattr(company_row, "exchange", None)
        out[issuer_id] = {
            "id": issuer_id,
            "slug": issuer.slug,
            "name": issuer.display_name,
            "display_name": issuer.display_name,
            "legal_name": issuer.legal_name,
            "ticker": primary_ticker,
            "primary_ticker": primary_ticker,
            "exchange": primary_exchange,
            "jurisdiction": issuer.jurisdiction,
            "sector": issuer.sector or getattr(company_row, "sector", None),
            "industry": issuer.industry or getattr(company_row, "industry", None),
            "supply_chain_stage": getattr(company_row, "supply_chain_stage", None),
            "website": issuer.website,
            "ir_url": getattr(company_row, "ir_url", None) or issuer.ir_website or issuer.website,
            "filings_url": getattr(company_row, "filings_url", None),
            "reports_url": getattr(company_row, "reports_url", None),
            "calendar_url": getattr(company_row, "calendar_url", None),
            "company_id": int(company_row.id) if company_row is not None else None,
            "company_slug": getattr(company_row, "slug", None),
            "is_private": bool((issuer.extra or {}).get("private")) or str(issuer.entity_status or "").strip().upper() == "PRIVATE",
            "identifiers": [
                {
                    "type": row.id_type,
                    "value": row.id_value,
                    "exchange": row.exchange,
                    "is_primary": bool(row.is_primary),
                }
                for row in identifiers
            ],
            "filing_count": filing_counts.get(issuer_id, 0),
            "disclosure_count": disclosure_counts.get(issuer_id, 0),
            "ir_count": ir_counts.get(issuer_id, 0),
            "evidence_count": evidence_counts.get(issuer_id, 0),
            "open_company_resolution_request_count": request_counts.get(issuer_id, 0),
            "doc_counts": company_doc_counts.get(int(company_row.id), {}) if company_row is not None else {},
        }
    return out


def _legacy_company_payloads_bulk(session: Session, companies: list[Company]) -> dict[int, dict[str, object]]:
    company_ids = [int(company.id) for company in companies]
    doc_counts_by_company_id = _company_doc_counts_by_company_id(session, company_ids)
    out: dict[int, dict[str, object]] = {}
    for company in companies:
        payload = _company_payload(company)
        payload.update(
            {
                "display_name": company.name,
                "legal_name": company.name,
                "name": company.name,
                "primary_ticker": company.ticker,
                "jurisdiction": None,
                "website": None,
                "company_id": int(company.id),
                "company_slug": company.slug,
                "is_private": False,
                "identifiers": (
                    [{"type": "TICKER", "value": company.ticker, "exchange": company.exchange, "is_primary": True}]
                    if company.ticker
                    else []
                ),
                "filing_count": 0,
                "disclosure_count": 0,
                "ir_count": 0,
                "evidence_count": 0,
                "open_company_resolution_request_count": 0,
                "doc_counts": doc_counts_by_company_id.get(int(company.id), {}),
            }
        )
        out[int(company.id)] = payload
    return out


def _company_or_issuer_payload(
    session: Session,
    *,
    issuer: Issuer | None = None,
    company: Company | None = None,
    include_resolution_request_count: bool = True,
) -> dict[str, object] | None:
    if issuer is not None:
        return _issuer_payload(session, issuer, company=company, include_resolution_request_count=include_resolution_request_count)
    if company is None:
        return None
    if getattr(company, "issuer_id", None) is not None:
        issuer_row = session.query(Issuer).filter_by(id=int(company.issuer_id)).first()
        if issuer_row is not None:
            return _issuer_payload(
                session,
                issuer_row,
                company=company,
                include_resolution_request_count=include_resolution_request_count,
            )
    payload = _company_payload(company)
    payload.update(
        {
            "display_name": company.name,
            "legal_name": company.name,
            "name": company.name,
            "primary_ticker": company.ticker,
            "jurisdiction": None,
            "website": None,
            "company_id": int(company.id),
            "company_slug": company.slug,
            "is_private": False,
            "identifiers": (
                [{"type": "TICKER", "value": company.ticker, "exchange": company.exchange, "is_primary": True}]
                if company.ticker
                else []
            ),
            "filing_count": 0,
            "disclosure_count": 0,
            "ir_count": 0,
            "evidence_count": 0,
            "open_company_resolution_request_count": 0,
            "doc_counts": {
                str(doc_type): int(count or 0)
                for doc_type, count in (
                    session.query(CompanyDocument.doc_type, func.count(CompanyDocument.id))
                    .filter(CompanyDocument.company_id == int(company.id))
                    .group_by(CompanyDocument.doc_type)
                    .all()
                )
            },
        }
    )
    return payload


def _resolve_company_or_issuer(session: Session, *, raw_id: object | None = None, slug: str | None = None) -> tuple[Issuer | None, Company | None]:
    issuer: Issuer | None = None
    company: Company | None = None
    if raw_id is not None and str(raw_id).isdigit():
        numeric_id = int(raw_id)
        issuer = session.query(Issuer).filter_by(id=numeric_id).first()
        if issuer is None:
            company = session.query(Company).filter_by(id=numeric_id).first()
            if company is not None and getattr(company, "issuer_id", None) is not None:
                issuer = session.query(Issuer).filter_by(id=int(company.issuer_id)).first()
    if issuer is None and slug:
        issuer = session.query(Issuer).filter_by(slug=str(slug)).first()
    if issuer is None and slug:
        company = company or session.query(Company).filter_by(slug=str(slug)).first()
        if company is not None and getattr(company, "issuer_id", None) is not None:
            issuer = session.query(Issuer).filter_by(id=int(company.issuer_id)).first()
    return issuer, company


def _resolve_report_record(
    session: Session,
    *,
    subject: str,
    version_id: int | None = None,
) -> tuple[ResearchReport, dict[str, object] | None, dict[str, object], object]:
    resolved_subject = resolve_report_subject(session, subject)
    report = (
        session.query(ResearchReport)
        .filter_by(subject_key=resolved_subject.subject_key)
        .first()
    )
    if report is None:
        report = ResearchReport(
            subject_id=resolved_subject.subject_id,
            slug=resolved_subject.slug,
            subject_key=resolved_subject.subject_key,
            issuer_id=resolved_subject.issuer_id,
            company_id=resolved_subject.company_id,
            title=f"Research Report — {resolved_subject.display_name}",
            status="active",
            latest_published_version_id=None,
            latest_subject_slug=resolved_subject.slug,
            extra={"subject_kind": resolved_subject.kind},
            created_at=utcnow(),
            updated_at=utcnow(),
        )
    version_row = None
    if version_id is not None and report.id is not None:
        version_row = (
            session.query(ResearchReportVersion)
            .filter_by(report_id=int(report.id), id=int(version_id))
            .first()
        )
    if version_row is None and report.id is not None:
        version_row = (
            session.query(ResearchReportVersion)
            .filter_by(report_id=int(report.id), status="published")
            .order_by(ResearchReportVersion.version.desc(), ResearchReportVersion.id.desc())
            .first()
        )
    summary = serialize_report_summary(report, version_row, subject=resolved_subject)
    version_payload = serialize_report_version(version_row) if version_row is not None else None
    return report, version_payload, summary, resolved_subject


def _report_subject_payload(resolved_subject: object) -> dict[str, object]:
    return {
        "display_name": getattr(resolved_subject, "display_name", None),
        "kind": getattr(resolved_subject, "kind", None),
        "slug": getattr(resolved_subject, "slug", None),
        "subject_key": getattr(resolved_subject, "subject_key", None),
        "primary_ticker": getattr(resolved_subject, "primary_ticker", None),
        "tickers": list(getattr(resolved_subject, "tickers", []) or []),
        "aliases": list(getattr(resolved_subject, "aliases", []) or []),
        "description": getattr(resolved_subject, "description", None),
    }


def _clean_payload_urls(value: object) -> object:
    if isinstance(value, dict):
        out: dict[str, object] = {}
        for key, nested in value.items():
            if isinstance(nested, str) and str(key).lower() in {"url", "href", "source_url", "filing_url", "primary_document_url", "canonical_url"}:
                out[key] = clean_url(nested)
            else:
                out[key] = _clean_payload_urls(nested)
        return out
    if isinstance(value, list):
        return [_clean_payload_urls(item) for item in value]
    return value


def _normalize_report_payload(payload: dict[str, object] | None, resolved_subject: object) -> dict[str, object] | None:
    if payload is None:
        return None
    normalized = dict(_clean_payload_urls(payload))
    normalized.setdefault("subject", _report_subject_payload(resolved_subject))
    return normalized


_REPORT_LIVE_CONTEXT_KEYS = (
    "subject",
    "identity_freshness",
    "coverage_panel",
    "published_primers",
    "published_teardowns",
    "thesis_ledger",
    "note_ledger",
    "subject_facts",
    "structured_financial_history_guidance",
    "primary_source_timeline",
    "transcript_archive_summaries",
    "supply_chain_peer_context",
    "linked_datasets_stage_trackers",
    "high_confidence_mention_appendix",
    "artifacts_export_appendix",
    "section_order",
    "window_days",
    "computed_at",
)


def _report_version_history_payload(version: ResearchReportVersion) -> dict[str, object]:
    return {
        "id": int(version.id),
        "version": int(version.version),
        "status": version.status,
        "trigger_kind": version.trigger_kind,
        "trigger_label": version.trigger_label,
        "as_of_at": version.as_of_at.isoformat() if version.as_of_at else None,
        "published_at": version.published_at.isoformat() if version.published_at else None,
        "created_at": version.created_at.isoformat() if version.created_at else None,
        "updated_at": version.updated_at.isoformat() if version.updated_at else None,
        "error": version.error,
    }


def _report_version_history(db: Session, report: ResearchReport, *, limit: int = 25) -> list[dict[str, object]]:
    if report.id is None:
        return []
    rows = (
        db.query(ResearchReportVersion)
        .filter(ResearchReportVersion.report_id == int(report.id))
        .order_by(ResearchReportVersion.version.desc(), ResearchReportVersion.id.desc())
        .limit(max(1, min(int(limit or 25), 100)))
        .all()
    )
    return [_report_version_history_payload(row) for row in rows]


def _report_detail_needs_assembled_payload(
    *,
    content: dict[str, object] | None,
) -> bool:
    return not _report_payload_has_detail_sections(content)


def _is_imported_report_payload(content: dict[str, object] | None) -> bool:
    return bool(isinstance(content, dict) and content.get("imported_report"))


def _report_detail_missing_live_context_keys(content: dict[str, object] | None) -> list[str]:
    if not isinstance(content, dict):
        return list(_REPORT_LIVE_CONTEXT_KEYS)
    return [key for key in _REPORT_LIVE_CONTEXT_KEYS if content.get(key) is None]


def _merge_report_live_context(
    payload: dict[str, object],
    live_payload: dict[str, object],
) -> dict[str, object]:
    merged = dict(payload)
    for key in _REPORT_LIVE_CONTEXT_KEYS:
        value = live_payload.get(key)
        if value is not None:
            merged[key] = value
    overlay = dict(merged.get("live_context_overlay") or {}) if isinstance(merged.get("live_context_overlay"), dict) else {}
    overlay.update(
        {
            "applied": True,
            "computed_at": live_payload.get("computed_at"),
            "source": "assemble_report_payload",
        }
    )
    merged["live_context_overlay"] = overlay
    return merged


def _overlay_report_live_context(
    db: Session,
    payload: dict[str, object] | None,
    resolved_subject: object,
    report: ResearchReport | None,
    *,
    window_days: int | None = None,
) -> dict[str, object] | None:
    if payload is None:
        return payload
    live_payload, _meta = assemble_report_payload(
        db,
        subject=resolved_subject,
        report=report,
        window_days=window_days,
    )
    return _merge_report_live_context(payload, live_payload)


def _report_payload_has_detail_sections(payload: dict[str, object] | None) -> bool:
    return service_report_payload_has_detail_sections(payload)


def _ipo_event_date(row: IPOEvent) -> date | None:
    return row.listing_date or row.pricing_date or row.offer_close_date or row.approval_date or row.application_date or row.announcement_date


def _ipo_event_payload(row: IPOEvent, *, include_payload: bool = False) -> dict[str, object]:
    out: dict[str, object] = {
        "id": int(row.id),
        "event_key": row.event_key,
        "event_date": _ipo_event_date(row).isoformat() if _ipo_event_date(row) else None,
        "company": row.company_name,
        "ticker": row.ticker,
        "isin": row.isin,
        "cik": row.cik,
        "exchange": row.exchange,
        "market": row.market,
        "region": row.region,
        "stage": row.stage,
        "status": row.status,
        "source_tier": row.source_tier,
        "confidence": row.confidence,
        "listing_date": row.listing_date.isoformat() if row.listing_date else None,
        "pricing_date": row.pricing_date.isoformat() if row.pricing_date else None,
        "application_date": row.application_date.isoformat() if row.application_date else None,
        "approval_date": row.approval_date.isoformat() if row.approval_date else None,
        "offer_close_date": row.offer_close_date.isoformat() if row.offer_close_date else None,
        "offer_price": row.offer_price,
        "price_text": row.price_text,
        "currency": row.currency,
        "shares_offered": row.shares_offered,
        "proceeds_amount": row.proceeds_amount,
        "proceeds_currency": row.proceeds_currency,
        "underwriters": row.underwriters,
        "source_url": row.source_url,
        "document_id": int(row.document_id) if row.document_id else None,
        "issuer_id": int(row.issuer_id) if row.issuer_id else None,
        "company_id": int(row.company_id) if row.company_id else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
    if include_payload:
        out["payload"] = dict(row.payload or {})
    return out


def _research_canon_subject_id(session: Session, params: dict[str, object]) -> int:
    raw_id = params.get("subject_id") or params.get("id")
    if raw_id is not None and str(raw_id).isdigit():
        return int(raw_id)
    raw_subject = str(params.get("subject") or "").strip()
    if not raw_subject:
        raise ValueError("subject_id or subject is required.")
    resolved = resolve_report_subject(session, raw_subject)
    if resolved.subject_id is None:
        raise ValueError("Resolved object is not backed by a research subject.")
    return int(resolved.subject_id)
