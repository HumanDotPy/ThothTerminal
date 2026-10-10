from __future__ import annotations

from thoth_terminal.web.mcp_parts._shared import bind_mcp_context


bind_mcp_context(globals())


def _mcp_tool_list_companies(params: dict[str, object]) -> dict[str, object]:
    query = str(params.get("query") or "").strip().lower()
    limit = params.get("limit")
    limit_val = int(limit) if isinstance(limit, int) or str(limit).isdigit() else 200
    session = _session_read()
    try:
        out: list[dict[str, object]] = []
        seen_keys: set[str] = set()
        issuers_query = session.query(Issuer)
        if query:
            pattern = f"%{query}%"
            issuer_ids = [
                int(row.id)
                for row in (
                    session.query(Issuer.id, Issuer.display_name)
                    .select_from(Issuer)
                    .outerjoin(IssuerIdentifier, IssuerIdentifier.issuer_id == Issuer.id)
                    .outerjoin(Company, Company.issuer_id == Issuer.id)
                    .filter(
                        or_(
                            func.lower(Issuer.display_name).like(pattern),
                            func.lower(Issuer.legal_name).like(pattern),
                            func.lower(Issuer.slug).like(pattern),
                            func.lower(func.coalesce(Issuer.sector, "")).like(pattern),
                            func.lower(func.coalesce(Issuer.industry, "")).like(pattern),
                            func.lower(func.coalesce(IssuerIdentifier.id_value, "")).like(pattern),
                            func.lower(func.coalesce(Company.slug, "")).like(pattern),
                            func.lower(func.coalesce(Company.ticker, "")).like(pattern),
                            func.lower(func.coalesce(Company.sector, "")).like(pattern),
                            func.lower(func.coalesce(Company.industry, "")).like(pattern),
                        )
                    )
                    .group_by(Issuer.id, Issuer.display_name)
                    .order_by(Issuer.display_name.asc(), Issuer.id.asc())
                    .limit(max(limit_val * 8, 40))
                    .all()
                )
            ]
            issuers = (
                issuers_query.filter(Issuer.id.in_(issuer_ids))
                .order_by(Issuer.display_name.asc(), Issuer.id.asc())
                .all()
                if issuer_ids
                else []
            )
        else:
            issuers = issuers_query.order_by(Issuer.display_name.asc(), Issuer.id.asc()).limit(limit_val).all()
        issuer_payloads = _issuer_payloads_bulk(session, issuers)
        for issuer in issuers:
            payload = issuer_payloads.get(int(issuer.id))
            if payload is None:
                continue
            haystacks = [
                str(payload.get("display_name") or "").lower(),
                str(payload.get("legal_name") or "").lower(),
                str(payload.get("slug") or "").lower(),
                str(payload.get("primary_ticker") or "").lower(),
                str(payload.get("company_slug") or "").lower(),
                str(payload.get("sector") or "").lower(),
                str(payload.get("industry") or "").lower(),
            ]
            if query and not any(query in hay for hay in haystacks):
                continue
            key = f"issuer:{payload.get('slug') or payload.get('id')}"
            if key in seen_keys:
                continue
            seen_keys.add(key)
            out.append(payload)
            if len(out) >= limit_val:
                return {"count": len(out), "companies": out}
        legacy_query = session.query(Company).filter(Company.issuer_id.is_(None))
        if query:
            pattern = f"%{query}%"
            legacy_query = legacy_query.filter(
                or_(
                    func.lower(Company.name).like(pattern),
                    func.lower(Company.slug).like(pattern),
                    func.lower(func.coalesce(Company.ticker, "")).like(pattern),
                    func.lower(func.coalesce(Company.sector, "")).like(pattern),
                    func.lower(func.coalesce(Company.industry, "")).like(pattern),
                )
            )
        legacy_companies = legacy_query.order_by(Company.name.asc(), Company.id.asc()).limit(max(limit_val * 8, 40)).all()
        legacy_payloads = _legacy_company_payloads_bulk(session, legacy_companies)
        for company in legacy_companies:
            payload = legacy_payloads.get(int(company.id))
            if payload is None:
                continue
            haystacks = [
                str(payload.get("name") or "").lower(),
                str(payload.get("slug") or "").lower(),
                str(payload.get("primary_ticker") or "").lower(),
                str(payload.get("sector") or "").lower(),
                str(payload.get("industry") or "").lower(),
            ]
            if query and not any(query in hay for hay in haystacks):
                continue
            key = f"legacy:{payload.get('slug') or payload.get('id')}"
            if key in seen_keys:
                continue
            seen_keys.add(key)
            out.append(payload)
            if len(out) >= limit_val:
                break
        return {"count": len(out), "companies": out}
    finally:
        session.close()


def _mcp_tool_get_company(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("id")
    slug = str(params.get("slug") or "").strip()
    session = _session_read()
    try:
        issuer, company = _resolve_company_or_issuer(session, raw_id=raw_id, slug=slug or None)
        payload = _company_or_issuer_payload(session, issuer=issuer, company=company)
        if payload is None:
            raise ValueError("Company not found.")
        return payload
    finally:
        session.close()


def _mcp_tool_list_company_docs(params: dict[str, object]) -> dict[str, object]:
    company_id = params.get("company_id")
    company_slug = str(params.get("company_slug") or "").strip()
    doc_types = params.get("doc_types")
    limit = params.get("limit")
    limit_val = int(limit) if isinstance(limit, int) or str(limit).isdigit() else 50

    session = _session_read()
    try:
        company = None
        if company_id is not None and str(company_id).isdigit():
            company = session.query(Company).filter_by(id=int(company_id)).first()
        if not company and company_slug:
            company = session.query(Company).filter_by(slug=company_slug).first()
        if not company:
            raise ValueError("Company not found.")
        q = (
            session.query(CompanyDocument, Document)
            .join(Document, Document.id == CompanyDocument.document_id)
            .filter(CompanyDocument.company_id == company.id)
            .order_by(Document.published_at.desc().nullslast(), Document.ingested_at.desc())
        )
        if isinstance(doc_types, list) and doc_types:
            q = q.filter(CompanyDocument.doc_type.in_([str(d) for d in doc_types]))
        rows = q.limit(limit_val).all()
        out = []
        for cd, doc in rows:
            out.append(
                {
                    "doc_type": cd.doc_type,
                    "id": doc.id,
                    "title": doc.title,
                    "url": doc.url or doc.canonical_url,
                    "published_at": doc.published_at.isoformat() if doc.published_at else None,
                }
            )
        return {"company": _company_payload(company), "count": len(out), "documents": out}
    finally:
        session.close()


def _mcp_tool_list_earnings_calendar(params: dict[str, object]) -> dict[str, object]:
    days_ahead = params.get("days_ahead")
    days_ahead_val = int(days_ahead) if str(days_ahead).isdigit() else 120
    hours_back = params.get("hours_back")
    hours_back_val = int(hours_back) if str(hours_back).isdigit() else 6
    include_other = bool(params.get("include_other_events", False))
    include_docs = bool(params.get("include_documents", False))
    limit = params.get("limit")
    limit_val = int(limit) if str(limit).isdigit() else 120

    session = _session_read()
    try:
        now = utcnow()
        future_cutoff = now + timedelta(days=days_ahead_val)
        earnings_filter = _earnings_title_filter()

        event_base = (
            session.query(CompanyDocument, Document, Company)
            .join(Document, Document.id == CompanyDocument.document_id)
            .join(Company, Company.id == CompanyDocument.company_id)
        )

        upcoming = (
            event_base.filter(CompanyDocument.doc_type.in_(["earnings_event", "calendar_event"]))
            .filter(Document.published_at.isnot(None))
            .filter(Document.published_at >= now - timedelta(hours=hours_back_val))
            .filter(Document.published_at <= future_cutoff)
        )
        if earnings_filter is not None:
            upcoming = upcoming.filter(earnings_filter)
        upcoming = upcoming.order_by(Document.published_at.asc().nullslast(), Document.ingested_at.desc()).limit(limit_val).all()

        def _row_payload(row):
            cd, doc, company = row
            return {
                "company": _company_payload(company),
                "doc_type": cd.doc_type,
                "id": doc.id,
                "title": doc.title,
                "url": doc.url or doc.canonical_url,
                "published_at": doc.published_at.isoformat() if doc.published_at else None,
            }

        out = {"upcoming": [_row_payload(r) for r in upcoming]}

        if include_other:
            other = event_base.filter(CompanyDocument.doc_type.in_(["earnings_event", "monthly_sales", "calendar_event"]))
            if earnings_filter is not None:
                other = other.filter(~earnings_filter)
            other = other.order_by(Document.published_at.desc().nullslast(), Document.ingested_at.desc()).limit(limit_val).all()
            out["other_events"] = [_row_payload(r) for r in other]

        if include_docs:
            docs = (
                session.query(CompanyDocument, Document, Company)
                .join(Document, Document.id == CompanyDocument.document_id)
                .join(Company, Company.id == CompanyDocument.company_id)
                .filter(CompanyDocument.doc_type.in_(["earnings_release", "earnings_presentation", "earnings_transcript"]))
                .order_by(Document.published_at.desc().nullslast(), Document.ingested_at.desc())
                .limit(limit_val)
                .all()
            )
            out["documents"] = [_row_payload(r) for r in docs]

        return out
    finally:
        session.close()


def _mcp_tool_list_ipo_calendar(params: dict[str, object]) -> dict[str, object]:
    days_back = params.get("days_back")
    days_back_val = int(days_back) if str(days_back).isdigit() else 365
    days_forward = params.get("days_forward")
    days_forward_val = int(days_forward) if str(days_forward).isdigit() else 365
    limit = params.get("limit")
    limit_val = max(1, min(int(limit) if str(limit).isdigit() else 150, 1000))
    exchange = str(params.get("exchange") or "").strip().upper()
    region = str(params.get("region") or "").strip().upper()
    stage = str(params.get("stage") or "").strip().lower()
    include_payload = bool(params.get("include_payload", False))

    session = _session_read()
    try:
        q = session.query(IPOEvent)
        if exchange:
            q = q.filter(func.upper(IPOEvent.exchange) == exchange)
        if region:
            q = q.filter(func.upper(IPOEvent.region) == region)
        if stage:
            q = q.filter(func.lower(IPOEvent.stage) == stage)
        rows = q.order_by(IPOEvent.updated_at.desc()).limit(max(limit_val * 5, 500)).all()
        today = calendar_today()
        start = today - timedelta(days=days_back_val)
        end = today + timedelta(days=days_forward_val)
        filtered = []
        for row in rows:
            event_date = _ipo_event_date(row)
            if event_date is not None and (event_date < start or event_date > end):
                continue
            filtered.append(row)
        filtered.sort(key=lambda row: (_ipo_event_date(row) or date.min, int(row.id or 0)), reverse=True)
        filtered = filtered[:limit_val]
        return {
            "count": len(filtered),
            "filters": {
                "days_back": days_back_val,
                "days_forward": days_forward_val,
                "exchange": exchange or None,
                "region": region or None,
                "stage": stage or None,
            },
            "events": [_ipo_event_payload(row, include_payload=include_payload) for row in filtered],
        }
    finally:
        session.close()


def _mcp_tool_list_transcripts(params: dict[str, object]) -> dict[str, object]:
    ticker = str(params.get("ticker") or "").strip().upper()
    if not ticker:
        raise ValueError("Missing ticker.")
    most_recent = bool(params.get("most_recent", True))
    limit = params.get("limit")
    limit_val = int(limit) if str(limit).isdigit() else 10
    year = params.get("year")
    year_val = int(year) if str(year).isdigit() else None
    quarter = params.get("quarter")
    quarter_val = int(quarter) if str(quarter).isdigit() else None
    lookback_days = params.get("lookback_days")
    lookback_val = int(lookback_days) if str(lookback_days).isdigit() else None

    session = _session_read()
    try:
        rows = query_transcripts(
            session,
            ticker=ticker,
            most_recent=most_recent,
            year=year_val,
            quarter=quarter_val,
            limit=limit_val,
            lookback_days=lookback_val,
        )
        if not rows:
            row = query_transcript(
                session,
                ticker=ticker,
                most_recent=most_recent,
                year=year_val,
                quarter=quarter_val,
                lookback_days=lookback_val,
                allow_live_fallback=True,
            )
            rows = [row] if row else []
        return {
            "ticker": ticker,
            "count": len(rows),
            "transcripts": [row.to_dict() for row in rows],
        }
    finally:
        session.close()


def _mcp_tool_list_research_reports(params: dict[str, object]) -> dict[str, object]:
    query = str(params.get("query") or "").strip() or None
    limit = params.get("limit")
    limit_val = int(limit) if str(limit).isdigit() else 50
    session = _session_read()
    try:
        rows = list_research_reports(
            session,
            query=query,
            report_type=str(params.get("report_type") or "").strip() or None,
            topic=str(params.get("topic") or "").strip() or None,
            company=str(params.get("company") or "").strip() or None,
            supply_chain=str(params.get("supply_chain") or "").strip() or None,
            material=str(params.get("material") or "").strip() or None,
            technology=str(params.get("technology") or "").strip() or None,
            status=str(params.get("status") or "").strip() or None,
            freshness=str(params.get("freshness") or "").strip() or None,
            limit=limit_val,
        )
        return {"count": len(rows), "reports": rows}
    finally:
        session.close()


def _mcp_tool_get_research_report(params: dict[str, object]) -> dict[str, object]:
    raw_subject = params.get("subject")
    raw_report_id = params.get("report_id")
    raw_version_id = params.get("version_id")
    raw_window_days = params.get("window_days")
    if raw_subject is None and raw_report_id is None:
        raise ValueError("subject or report_id is required.")
    subject = str(raw_subject).strip() if raw_subject is not None else int(raw_report_id)
    version_id = int(raw_version_id) if str(raw_version_id).isdigit() else None
    window_days = int(raw_window_days) if str(raw_window_days).isdigit() else None
    session = _session_read()
    try:
        report, version_payload, summary, resolved_subject = _resolve_report_record(session, subject=str(subject), version_id=version_id)
        payload = None
        if version_payload is not None:
            version_row = session.query(ResearchReportVersion).filter_by(id=int(version_payload["id"])).first()
            payload = dict(version_row.content_json or {}) if version_row is not None else None
            if _is_imported_report_payload(payload):
                payload = _normalize_report_payload(payload, resolved_subject)
            else:
                payload = _overlay_report_live_context(
                    session,
                    payload,
                    resolved_subject,
                    report,
                    window_days=window_days,
                )
            payload = _normalize_report_payload(payload, resolved_subject)
        else:
            payload, _meta = assemble_report_payload(session, subject=resolved_subject, report=report, window_days=window_days)
            payload = _normalize_report_payload(payload, resolved_subject)
        versions = list_research_report_versions(session, report_id=int(report.id), limit=25) if report.id is not None else []
        return {
            "report": summary,
            "version": version_payload,
            "content": payload,
            "versions": versions,
        }
    finally:
        session.close()


def _mcp_tool_register_imported_report(params: dict[str, object]) -> dict[str, object]:
    title = str(params.get("title") or "").strip()
    markdown = str(params.get("markdown") or params.get("content_md") or "").strip()
    pdf_base64 = str(params.get("pdf_base64") or "").strip()
    if not title:
        raise ValueError("title is required.")
    if not markdown and not pdf_base64:
        raise ValueError("markdown or pdf_base64 is required.")
    if pdf_base64:
        data = base64.b64decode(pdf_base64)
        content_type = "application/pdf"
        filename = str(params.get("filename") or f"{title}.pdf").strip()
    else:
        data = markdown.encode("utf-8")
        content_type = "text/markdown"
        filename = str(params.get("filename") or f"{title}.md").strip()
    source_url = str(params.get("source_url") or "").strip() or None
    source_label = str(params.get("source_label") or "").strip() or None
    with session_scope() as session:
        return import_research_report_bytes(
            session,
            data=data,
            filename=filename,
            content_type=content_type,
            title=title,
            report_type=str(params.get("report_type") or "imported_report"),
            summary=str(params.get("summary") or "").strip() or None,
            tags=params.get("tags"),
            topics=params.get("topics"),
            linked_objects=params.get("linked_objects"),
            source_metadata={
                "source_url": source_url,
                "source_label": source_label,
                "import_surface": "mcp_register_imported_report",
            },
            report_id=int(params.get("report_id")) if str(params.get("report_id") or "").isdigit() else None,
            actor=str(params.get("actor") or "mcp").strip() or "mcp",
        )


def _mcp_tool_read_report_content(params: dict[str, object]) -> dict[str, object]:
    raw_subject = params.get("subject")
    raw_report_id = params.get("report_id")
    raw_version_id = params.get("version_id")
    raw_artifact_id = params.get("artifact_id")
    format_hint = str(params.get("format") or "").strip().lower() or None
    if raw_subject is None and raw_report_id is None:
        raise ValueError("subject or report_id is required.")
    subject = str(raw_subject).strip() if raw_subject is not None else int(raw_report_id)
    version_id = int(raw_version_id) if str(raw_version_id or "").isdigit() else None
    artifact_id = int(raw_artifact_id) if str(raw_artifact_id or "").isdigit() else None
    session = _session_read()
    try:
        report, version_payload, summary, _resolved_subject = _resolve_report_record(session, subject=str(subject), version_id=version_id)
        if version_payload is None:
            raise ValueError("Report version not found.")
        version = session.query(ResearchReportVersion).filter_by(id=int(version_payload["id"]), report_id=int(report.id)).first()
        if version is None:
            raise ValueError("Report version not found.")
        artifact = _reader_artifact_for_version(version, artifact_id=artifact_id, format_hint=format_hint)
        if artifact is None:
            raise ValueError("Readable report artifact not found.")
        media = str(artifact.content_type or "").split(";", 1)[0].strip().lower()
        reader_format = "pdf" if media == "application/pdf" or artifact.artifact_type == "pdf" else "markdown"
        raw_url = f"/reports/{report.slug}/artifacts/{int(artifact.id)}/raw?version_id={int(version.id)}"
        reader_url = f"/reports/{report.slug}/reader?version_id={int(version.id)}&artifact_id={int(artifact.id)}"
        payload: dict[str, object] = {
            "report": summary,
            "version": version_payload,
            "artifact": _artifact_entries_for_template(artifact),
            "format": reader_format,
            "reader_url": _mcp_absolute_url(reader_url),
            "raw_url": _mcp_absolute_url(raw_url),
            "manual_addenda": summary.get("manual_addenda") or [],
        }
        manual_markdown = _manual_addenda_markdown(summary.get("manual_addenda") if isinstance(summary.get("manual_addenda"), list) else [])
        if reader_format == "markdown":
            text = ""
            path = object_abs_path(str(artifact.object_path or ""))
            if path.exists() and path.is_file():
                text = path.read_text(encoding="utf-8", errors="ignore")
            markdown_text = text or (version.content_md or "")
            if artifact.artifact_type != "manual_addendum" and manual_markdown:
                markdown_text = f"{markdown_text.rstrip()}\n\n{manual_markdown}".strip() + "\n"
            payload["markdown"] = markdown_text
        else:
            payload["text_excerpt"] = str(version.content_md or "")[:2000] or None
            if manual_markdown:
                payload["manual_addenda_markdown"] = manual_markdown
        return payload
    finally:
        session.close()


def _mcp_tool_append_report_addendum(params: dict[str, object]) -> dict[str, object]:
    raw_subject = params.get("subject")
    raw_report_id = params.get("report_id")
    if raw_subject is None and raw_report_id is None:
        raise ValueError("subject or report_id is required.")
    subject = str(raw_subject).strip() if raw_subject is not None else int(raw_report_id)
    body_md = str(params.get("body_md") or params.get("markdown") or "").strip()
    if not body_md:
        raise ValueError("body_md is required.")
    version_id = int(params.get("version_id")) if str(params.get("version_id") or "").isdigit() else None
    with session_scope() as session:
        return append_research_report_addendum(
            session,
            subject=subject,
            version_id=version_id,
            title=str(params.get("title") or "").strip() or None,
            summary=str(params.get("summary") or "").strip() or None,
            body_md=body_md,
            tags=params.get("tags"),
            actor=str(params.get("actor") or "mcp").strip() or "mcp",
        )


def _mcp_tool_build_research_report(params: dict[str, object]) -> dict[str, object]:
    subject = str(params.get("subject") or "").strip()
    if not subject:
        raise ValueError("subject is required.")
    force = bool(params.get("force", False))
    trigger_label = str(params.get("trigger_label") or "").strip() or None
    window_days = int(params.get("window_days")) if str(params.get("window_days") or "").isdigit() else None
    with session_scope() as session:
        result = build_research_report(
            session,
            subject=subject,
            trigger_kind="mcp",
            trigger_label=trigger_label,
            force=force,
            window_days=window_days,
        )
        return {
            "report": serialize_report_summary(result.report, result.version, subject=result.subject),
            "version": serialize_report_version(result.version),
            "created_version": bool(result.created_version),
            "reason": result.reason,
        }


def _mcp_tool_build_bottleneck_report(params: dict[str, object]) -> dict[str, object]:
    raw_model_id = params.get("bottleneck_model_id")
    if not str(raw_model_id or "").isdigit():
        raise ValueError("bottleneck_model_id is required.")
    force = bool(params.get("force", False))
    trigger_label = str(params.get("trigger_label") or "").strip() or None
    with session_scope() as session:
        result = build_bottleneck_report(
            session,
            bottleneck_model_id=int(raw_model_id),
            trigger_kind="mcp",
            trigger_label=trigger_label,
            force=force,
        )
        return {
            "report": serialize_report_summary(result.report, result.version, subject=result.subject),
            "version": serialize_report_version(result.version),
            "created_version": bool(result.created_version),
            "reason": result.reason,
        }


def _mcp_tool_list_research_report_versions(params: dict[str, object]) -> dict[str, object]:
    raw_subject = params.get("subject")
    raw_report_id = params.get("report_id")
    limit = params.get("limit")
    limit_val = int(limit) if str(limit).isdigit() else 20
    session = _session_read()
    try:
        return {
            "versions": list_research_report_versions(
                session,
                subject=str(raw_subject).strip() if raw_subject is not None else None,
                report_id=int(raw_report_id) if str(raw_report_id).isdigit() else None,
                limit=limit_val,
            )
        }
    finally:
        session.close()


def _mcp_tool_export_research_report(params: dict[str, object]) -> dict[str, object]:
    payload = _mcp_tool_get_research_report(params)
    version = payload.get("version") or {}
    return {
        "report": payload.get("report"),
        "version": version,
        "artifacts": list((version.get("artifacts") or [])),
    }


def _mcp_tool_create_research_report_derivative(params: dict[str, object]) -> dict[str, object]:
    raw_subject = params.get("subject")
    raw_version_id = params.get("report_version_id")
    instructions = str(params.get("instructions") or "").strip() or None
    if raw_subject is None and raw_version_id is None:
        raise ValueError("subject or report_version_id is required.")
    with session_scope() as session:
        artifact = create_ai_clean_derivative(
            session,
            subject=str(raw_subject).strip() if raw_subject is not None else None,
            report_version_id=int(raw_version_id) if str(raw_version_id).isdigit() else None,
            instructions=instructions,
        )
        return artifact


def _mcp_tool_apply_report_memory_decision(params: dict[str, object]) -> dict[str, object]:
    raw_subject = params.get("subject")
    raw_report_id = params.get("report_id")
    raw_version_id = params.get("version_id")
    decision = str(params.get("decision") or "").strip()
    if raw_subject is None and raw_report_id is None:
        raise ValueError("subject or report_id is required.")
    if not decision:
        raise ValueError("decision is required.")
    raw_evidence = params.get("evidence")
    evidence: list[str] = []
    if isinstance(raw_evidence, list):
        evidence = [str(row).strip() for row in raw_evidence if str(row).strip()]
    elif raw_evidence is not None:
        evidence = [str(raw_evidence).strip()] if str(raw_evidence).strip() else []
    with session_scope() as session:
        return apply_report_memory_decision(
            session,
            subject=str(raw_subject).strip() if raw_subject is not None else int(raw_report_id),
            version_id=int(raw_version_id) if str(raw_version_id).isdigit() else None,
            decision=decision,
            body_md=str(params.get("body_md") or "").strip() or None,
            reason=str(params.get("reason") or "").strip() or None,
            evidence=evidence,
            actor=str(params.get("actor") or "codex").strip() or "codex",
        )
