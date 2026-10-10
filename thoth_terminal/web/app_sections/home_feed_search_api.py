from __future__ import annotations

from thoth_terminal.web.app_sections._shared import bind_app_context
from thoth_terminal.dataset_discovery import discover_datasets


bind_app_context(globals())


@app.get("/api/feed/recent")
def api_feed_recent(
    after_id: int | None = None,
    triage: str | None = None,
    source_id: int | None = None,
    limit: int = 20,
    db: Session = Depends(get_session),
) -> JSONResponse:
    """Fetch recent documents for live feed updates."""
    lim = max(1, min(int(limit), 50))
    triage_norm = (triage or "").upper().strip()
    if triage_norm in ("", "ALL"):
        triage_norm = None

    q = db.query(Document).options(
        joinedload(Document.source),
        joinedload(Document.domains),
        defer(Document.raw_text),
        defer(Document.normalized_text),
        defer(Document.translation_en),
    ).filter(~Document.kind.in_(_FEED_EXCLUDED_DOCUMENT_KINDS))
    q = q.filter(human_feed_visible_filter(db))

    if after_id:
        q = q.filter(Document.id > int(after_id))
    if triage_norm:
        q = q.filter(Document.triage_state == triage_norm)
    if source_id:
        q = q.filter(Document.source_id == int(source_id))

    docs = q.order_by(Document.id.desc()).limit(lim).all()
    doc_ids = [int(d.id) for d in docs]
    _annotate_feed_document_excerpts(db, docs)

    thumb_paths: dict[int, str] = {}
    if doc_ids:
        for attachment in (
            db.query(Attachment)
            .filter(Attachment.document_id.in_(doc_ids))
            .order_by(Attachment.document_id.asc(), Attachment.id.asc())
            .all()
        ):
            doc_id = int(attachment.document_id)
            if doc_id in thumb_paths:
                continue
            if attachment.kind == "image" or (attachment.content_type or "").startswith("image/"):
                thumb_paths[doc_id] = attachment.object_path
                continue

    items = []
    for d in docs:
        thumb_url = None
        thumb_path = thumb_paths.get(int(d.id))
        if not thumb_path:
            extra = d.extra or {}
            media = extra.get("media") or extra.get("image_urls") or []
            if isinstance(media, str):
                media = [media]
            if not media and isinstance(extra.get("quoted"), dict):
                q_media = extra.get("quoted", {}).get("media") or []
                if isinstance(q_media, str):
                    q_media = [q_media]
                media = q_media
            if isinstance(media, list) and media:
                thumb_url = str(media[0])

        title = d.title or d.url or f"doc {d.id}"
        if (
            getattr(d, "feed_translation_nonempty", False)
            and d.extra
            and d.extra.get("translation_title_en")
        ):
            title = d.extra.get("translation_title_en")

        items.append({
            "id": d.id,
            "title": title[:200],
            "excerpt": str(getattr(d, "feed_excerpt", "") or ""),
            "source_name": d.source.name if d.source else "unknown",
            "url": d.url or d.canonical_url,
            "published_at": (d.published_at or d.ingested_at).isoformat() if (d.published_at or d.ingested_at) else None,
            "language": d.language,
            "has_translation": bool(getattr(d, "feed_has_translation", False)),
            "triage_state": d.triage_state,
            "saved": bool(d.saved),
            "thumb_path": thumb_path,
            "thumb_url": thumb_url,
            "domains": [dom.slug for dom in d.domains] if d.domains else [],
        })

    if doc_ids:
        logger.info(
            "feed_recent after_id=%s triage=%s source_id=%s -> %s items (ids %s..%s)",
            after_id,
            triage_norm or "ANY",
            source_id,
            len(doc_ids),
            min(doc_ids),
            max(doc_ids),
        )
    else:
        logger.info(
            "feed_recent after_id=%s triage=%s source_id=%s -> 0 items",
            after_id,
            triage_norm or "ANY",
            source_id,
        )

    return JSONResponse({"items": items, "count": len(items)})


@app.get("/api/home/intelligence")
def api_home_intelligence(db: Session = Depends(get_session)) -> JSONResponse:
    if build_security_posture(settings).public:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return JSONResponse(
        _HOME_INTELLIGENCE_CACHE.get_or_set(
            f"home:{_cache_scope()}:intelligence",
            lambda: build_home_intelligence(db),
            ttl_s=_HOME_INTELLIGENCE_CACHE_TTL_S,
        )
    )


@app.get("/api/home/portfolio")
def api_home_portfolio(
    account_id: str | None = None,
    live: bool = True,
    include_market: bool = True,
    db: Session = Depends(get_session),
) -> JSONResponse:
    if build_security_posture(settings).public:
        return JSONResponse({"error": "Not found"}, status_code=404)
    live_refresh = bool(live)

    def build_payload() -> dict[str, Any]:
        status = ibkr_service().status(live=live_refresh)
        mood = ibkr_service().portfolio_mood(
            db,
            account_id=account_id,
            settings=settings,
            allow_live_refresh=bool(live_refresh and status.get("connected")),
            persist_mood=live_refresh,
        )
        if live_refresh:
            db.commit()
            _HOME_INTELLIGENCE_CACHE.invalidate_prefix("home:")
            _HOME_PORTFOLIO_CACHE.invalidate_prefix("home:")
        payload = {
            "ok": True,
            "ibkr_status": status,
            "portfolio_mood": mood,
            "portfolio_state": derive_home_portfolio_state(mood),
            "portfolio_targets": read_portfolio_target_profile(),
        }
        if include_market:
            payload["market_watchlist"] = build_market_watchlist(
                db,
                allow_live_refresh=bool(live_refresh and status.get("connected")),
                portfolio_mood=mood,
            )
        return payload

    if not live_refresh and not include_market:
        key = f"home:{_session_cache_scope(db)}:portfolio:background:{account_id or ''}"
        return JSONResponse(
            _HOME_PORTFOLIO_CACHE.get_or_set(
                key,
                build_payload,
                ttl_s=_HOME_PORTFOLIO_BACKGROUND_TTL_S,
            )
        )
    return JSONResponse(build_payload())


@app.get("/api/portfolio/targets")
def api_portfolio_targets_get() -> JSONResponse:
    if build_security_posture(settings).public:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return JSONResponse({"ok": True, "portfolio_targets": read_portfolio_target_profile()})


@app.put("/api/portfolio/targets")
def api_portfolio_targets_put(payload: dict[str, Any] | None = Body(default=None)) -> JSONResponse:
    if build_security_posture(settings).public:
        return JSONResponse({"error": "Not found"}, status_code=404)
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "error": "Expected JSON object."}, status_code=400)
    profile = write_portfolio_target_profile(payload)
    _HOME_INTELLIGENCE_CACHE.invalidate_prefix("home:")
    _HOME_PORTFOLIO_CACHE.invalidate_prefix("home:")
    return JSONResponse({"ok": True, "portfolio_targets": profile})


@app.get("/api/search")
def api_search(
    kind: str,
    q: str,
    limit: int = 10,
    include_neighborhoods: bool = False,
    include_hidden: bool = False,
    db: Session = Depends(get_session),
) -> JSONResponse:
    kind_norm = (kind or "").strip().lower()
    query = (q or "").strip()
    if not query:
        return JSONResponse({"items": []})

    lim = max(1, min(int(limit), 30))
    include_hidden_theses = bool(include_hidden)
    search_cache_key = f"search:{_cache_scope()}:{kind_norm or 'all'}:{query.lower()}:{lim}:{1 if include_neighborhoods else 0}:{1 if include_hidden_theses else 0}"
    cached_search = _API_SEARCH_CACHE.get(search_cache_key)
    if cached_search is not None:
        return JSONResponse(cached_search)

    def _search_response(payload: dict[str, Any], *, ttl_s: float | None = None) -> JSONResponse:
        _API_SEARCH_CACHE.set(search_cache_key, payload, ttl_s=ttl_s or (30.0 if include_neighborhoods else 45.0))
        return JSONResponse(payload)

    if include_neighborhoods or kind_norm in {"institutional_evidence", "institutional_evidence_item", "institutional_evidence_items", "institutional_observation", "institutional_observations"}:
        return _search_response(
            object_aware_search(
                db,
                query=query,
                kind=kind_norm or "all",
                limit=lim,
                include_neighborhoods=include_neighborhoods,
                include_hidden=include_hidden_theses,
            )
        )

    def _doc_items(docs: list[Document]) -> list[dict[str, object]]:
        return [
            {
                "id": d.id,
                "label": (d.title or d.url or f"doc {d.id}")[:160],
                "kind": "doc",
                "href": f"/docs/{d.id}",
            }
            for d in docs
        ]

    def _thesis_items(rows: list[Thesis]) -> list[dict[str, object]]:
        return [
            {
                "id": t.id,
                "label": (t.title or f"thesis {t.id}")[:160],
                "kind": "thesis",
                "href": f"/theses/{t.id}",
            }
            for t in rows
        ]

    def _note_items(rows: list[Note]) -> list[dict[str, object]]:
        return [
            {
                "id": n.id,
                "label": (n.title or f"note {n.id}")[:160],
                "kind": "note",
                "href": f"/notes/{n.id}",
            }
            for n in rows
        ]

    def _dataset_items(rows: list[Dataset]) -> list[dict[str, object]]:
        return [
            {
                "id": d.id,
                "label": (d.title or f"dataset {d.id}")[:160],
                "kind": "dataset",
                "href": f"/visualizations/datasets/{d.id}",
            }
            for d in rows
        ]

    def _chart_items(rows: list[Chart]) -> list[dict[str, object]]:
        return [
            {
                "id": row.id,
                "label": (row.title or f"chart {row.id}")[:160],
                "kind": "chart",
                "href": f"/visualizations/{row.id}",
            }
            for row in rows
        ]

    def _company_items(rows: list[dict[str, object]]) -> list[dict[str, object]]:
        return [
            {
                "id": row.get("id"),
                "label": (str(row.get("display_name") or row.get("name") or row.get("slug") or f"company {row.get('id')}"))[:160],
                "kind": "company",
                "href": f"/companies/{row.get('slug')}",
            }
            for row in rows
        ]

    def _company_results(query_text: str, limit_val: int) -> list[dict[str, object]]:
        query_norm = str(query_text or "").strip().lower()
        pattern = f"%{query_norm}%"
        out: list[dict[str, object]] = []
        seen: set[str] = set()
        issuer_query = (
            db.query(Issuer.id, Issuer.slug, Issuer.display_name)
            .select_from(Issuer)
            .outerjoin(IssuerIdentifier, IssuerIdentifier.issuer_id == Issuer.id)
            .outerjoin(Company, Company.issuer_id == Issuer.id)
        )
        if query_norm:
            issuer_query = issuer_query.filter(
                or_(
                    func.lower(Issuer.display_name).like(pattern),
                    func.lower(Issuer.legal_name).like(pattern),
                    func.lower(Issuer.slug).like(pattern),
                    func.lower(func.coalesce(Issuer.sector, "")).like(pattern),
                    func.lower(func.coalesce(Issuer.industry, "")).like(pattern),
                    func.lower(func.coalesce(IssuerIdentifier.id_value, "")).like(pattern),
                    func.lower(func.coalesce(IssuerIdentifier.exchange, "")).like(pattern),
                    func.lower(func.coalesce(Company.name, "")).like(pattern),
                    func.lower(func.coalesce(Company.slug, "")).like(pattern),
                    func.lower(func.coalesce(Company.ticker, "")).like(pattern),
                    func.lower(func.coalesce(Company.sector, "")).like(pattern),
                    func.lower(func.coalesce(Company.industry, "")).like(pattern),
                )
            )
        issuer_rows = (
            issuer_query.group_by(Issuer.id, Issuer.slug, Issuer.display_name)
            .order_by(Issuer.display_name.asc(), Issuer.id.asc())
            .limit(max(limit_val * 8, 40))
            .all()
        )
        for issuer in issuer_rows:
            key = f"issuer:{issuer.slug or issuer.id}"
            if key in seen:
                continue
            seen.add(key)
            payload = {
                "id": int(issuer.id),
                "slug": issuer.slug,
                "name": issuer.display_name,
                "display_name": issuer.display_name,
            }
            out.append(payload)
            if len(out) >= limit_val:
                return out
        company_query = db.query(Company.id, Company.slug, Company.name).filter(Company.issuer_id.is_(None))
        if query_norm:
            company_query = company_query.filter(
                or_(
                    func.lower(Company.name).like(pattern),
                    func.lower(Company.slug).like(pattern),
                    func.lower(func.coalesce(Company.ticker, "")).like(pattern),
                    func.lower(func.coalesce(Company.sector, "")).like(pattern),
                    func.lower(func.coalesce(Company.industry, "")).like(pattern),
                )
            )
        for company in company_query.order_by(Company.name.asc()).limit(max(limit_val * 8, 40)).all():
            key = f"legacy:{company.slug or company.id}"
            if key in seen:
                continue
            seen.add(key)
            payload = {
                "id": int(company.id),
                "slug": company.slug,
                "name": company.name,
                "display_name": company.name,
            }
            out.append(payload)
            if len(out) >= limit_val:
                break
        return out

    def _instrument_request_items(rows: list[InstrumentRequest]) -> list[dict[str, object]]:
        return [
            {
                "id": row.id,
                "label": (row.title or f"instrument request {row.id}")[:160],
                "kind": "instrument_request",
                "href": f"/instruments/requests/{row.id}",
            }
            for row in rows
        ]

    def _product_stage_items(rows: list[ProductStageEntry]) -> list[dict[str, object]]:
        return [
            {
                "id": row.id,
                "label": f"{row.company_name} · {row.product_name} · {row.stage}"[:160],
                "kind": "product_stage",
                "href": f"/stage-tracker/{row.id}",
            }
            for row in rows
        ]

    def _instrument_request_label_rows(limit_val: int):
        pattern = f"%{query}%"
        return (
            db.query(InstrumentRequest.id, InstrumentRequest.title)
            .filter(
                (InstrumentRequest.title.ilike(pattern))
                | (InstrumentRequest.summary.ilike(pattern))
                | (InstrumentRequest.intent.ilike(pattern))
                | (InstrumentRequest.provenance_summary.ilike(pattern))
            )
            .order_by(InstrumentRequest.priority.desc(), InstrumentRequest.updated_at.desc())
            .limit(max(1, min(int(limit_val or 10), 30)))
            .all()
        )

    def _product_stage_label_rows(limit_val: int):
        pattern = f"%{query}%"
        return (
            db.query(ProductStageEntry.id, ProductStageEntry.company_name, ProductStageEntry.product_name, ProductStageEntry.stage)
            .filter(
                (ProductStageEntry.company_name.ilike(pattern))
                | (ProductStageEntry.product_name.ilike(pattern))
                | (ProductStageEntry.notes.ilike(pattern))
                | (ProductStageEntry.date_text.ilike(pattern))
            )
            .order_by(ProductStageEntry.last_claim_at.desc().nullslast(), ProductStageEntry.updated_at.desc(), ProductStageEntry.id.desc())
            .limit(max(1, min(int(limit_val or 10), 30)))
            .all()
        )

    def _supply_chain_items(rows: list[SupplyChain]) -> list[dict[str, object]]:
        return [
            {
                "id": row.id,
                "label": (row.title or f"supply chain {row.id}")[:160],
                "kind": "supply_chain",
                "href": f"/visualizations/supply-chains/{row.id}/command-center",
            }
            for row in rows
        ]

    def _research_report_items(rows: list[dict[str, object]]) -> list[dict[str, object]]:
        return [
            {
                "id": row.get("id"),
                "label": (str(row.get("display_name") or row.get("title") or row.get("slug") or f"report {row.get('id')}"))[:160],
                "kind": "research_report",
                "href": str(row.get("href") or f"/reports/{row.get('slug')}"),
            }
            for row in rows
        ]

    def _research_subject_items(rows: list[dict[str, object]]) -> list[dict[str, object]]:
        return [
            {
                "id": row.get("id"),
                "label": (str(row.get("display_name") or row.get("slug") or f"subject {row.get('id')}"))[:160],
                "kind": "research_subject",
                "href": str(row.get("href") or f"/reports/{row.get('slug')}"),
            }
            for row in rows
        ]

    def _knowledge_asset_items(rows: list[dict[str, object]]) -> list[dict[str, object]]:
        return [
            {
                "id": row.get("id"),
                "label": (str(row.get("title") or row.get("slug") or f"knowledge {row.get('id')}"))[:160],
                "kind": "knowledge_asset",
                "href": str(row.get("href") or f"/knowledge/{row.get('slug')}"),
            }
            for row in rows
        ]

    def _research_subject_label_rows(limit_val: int) -> list[dict[str, object]]:
        pattern = f"%{query.lower()}%"
        rows = (
            db.query(ResearchSubject.id, ResearchSubject.display_name, ResearchSubject.slug)
            .outerjoin(ResearchSubjectAlias, ResearchSubjectAlias.subject_id == ResearchSubject.id)
            .filter(
                or_(
                    func.lower(ResearchSubject.display_name).like(pattern),
                    func.lower(func.coalesce(ResearchSubject.legal_name, "")).like(pattern),
                    func.lower(ResearchSubject.slug).like(pattern),
                    func.lower(ResearchSubject.subject_key).like(pattern),
                    func.lower(ResearchSubjectAlias.alias).like(pattern),
                )
            )
            .distinct()
            .order_by(ResearchSubject.updated_at.desc(), ResearchSubject.id.desc())
            .limit(max(1, min(int(limit_val or 10), 30)))
            .all()
        )
        return [{"id": int(row.id), "display_name": row.display_name, "slug": row.slug, "href": f"/reports/{row.slug}"} for row in rows]

    def _knowledge_asset_label_rows(limit_val: int) -> list[dict[str, object]]:
        pattern = f"%{query.lower()}%"
        rows = (
            db.query(KnowledgeAsset.id, KnowledgeAsset.title, KnowledgeAsset.slug)
            .join(ResearchSubject, ResearchSubject.id == KnowledgeAsset.subject_id)
            .filter(
                or_(
                    func.lower(KnowledgeAsset.title).like(pattern),
                    func.lower(func.coalesce(KnowledgeAsset.summary, "")).like(pattern),
                    func.lower(KnowledgeAsset.slug).like(pattern),
                    func.lower(ResearchSubject.display_name).like(pattern),
                    func.lower(ResearchSubject.slug).like(pattern),
                )
            )
            .order_by(KnowledgeAsset.updated_at.desc(), KnowledgeAsset.id.desc())
            .limit(max(1, min(int(limit_val or 10), 30)))
            .all()
        )
        return [{"id": int(row.id), "title": row.title, "slug": row.slug, "href": f"/knowledge/{row.slug}"} for row in rows]

    if kind_norm in ("doc", "document", "docs", "documents"):
        docs = list_documents(db, query=query, days=None, defer_text=True, include_domains=False, limit=lim)
        return _search_response({"items": _doc_items(docs)})

    if kind_norm in ("thesis", "theses"):
        thesis_query = db.query(Thesis).options(defer(Thesis.statement)).filter((Thesis.title.ilike(f"%{query}%")) | (Thesis.statement.ilike(f"%{query}%")))
        thesis_query = _visible_thesis_query(thesis_query, include_hidden=include_hidden_theses)
        rows = thesis_query.order_by(Thesis.updated_at.desc()).limit(lim).all()
        return _search_response({"items": _thesis_items(rows)})

    if kind_norm in ("note", "notes"):
        rows = (
            db.query(Note)
            .options(defer(Note.body_md))
            .filter((Note.title.ilike(f"%{query}%")) | (Note.body_md.ilike(f"%{query}%")))
            .order_by(Note.updated_at.desc())
            .limit(lim)
            .all()
        )
        return _search_response({"items": _note_items(rows)})

    if kind_norm in ("dataset", "datasets"):
        ensure_dataset_tables(db)
        rows = discover_datasets(db, query=query, limit=lim).rows
        return _search_response({"items": _dataset_items(rows)})

    if kind_norm in ("chart", "charts", "visualization", "visualizations"):
        rows = (
            db.query(Chart)
            .options(defer(Chart.description_md), defer(Chart.config))
            .filter(
                (Chart.title.ilike(f"%{query}%"))
                | (Chart.subtitle.ilike(f"%{query}%"))
                | (Chart.category.ilike(f"%{query}%"))
                | (Chart.description_md.ilike(f"%{query}%"))
            )
            .order_by(Chart.updated_at.desc())
            .limit(lim)
            .all()
        )
        return _search_response({"items": _chart_items(rows)})

    if kind_norm in ("company", "companies"):
        return _search_response({"items": _company_items(_company_results(query, lim))})

    if kind_norm in ("instrument_request", "instrument-requests", "instrument", "request", "requests"):
        return _search_response({"items": _instrument_request_items(_instrument_request_label_rows(lim))})

    if kind_norm in ("product_stage", "product-stage", "stage", "stages"):
        return _search_response({"items": _product_stage_items(_product_stage_label_rows(lim))})

    if kind_norm in ("supply_chain", "supply-chain", "chain", "chains"):
        _ensure_supply_chain_tables(db)
        rows = (
            db.query(SupplyChain)
            .options(defer(SupplyChain.description), defer(SupplyChain.data))
            .filter(
                (SupplyChain.title.ilike(f"%{query}%"))
                | (SupplyChain.description.ilike(f"%{query}%"))
                | (SupplyChain.category.ilike(f"%{query}%"))
                | (SupplyChain.product.ilike(f"%{query}%"))
            )
            .order_by(SupplyChain.updated_at.desc())
            .limit(lim)
            .all()
        )
        return _search_response({"items": _supply_chain_items(rows)})

    if kind_norm in ("research_report", "research-report", "report", "reports"):
        return _search_response({"items": _research_report_items(list_research_reports(db, query=query, limit=lim))})

    if kind_norm in ("research_subject", "research-subject", "subject", "subjects"):
        return _search_response({"items": _research_subject_items(_research_subject_label_rows(lim))})

    if kind_norm in ("knowledge_asset", "knowledge-assets", "knowledge", "primer", "teardown"):
        return _search_response({"items": _knowledge_asset_items(_knowledge_asset_label_rows(lim))})

    if kind_norm in ("all", "any", "*"):
        docs = list_documents(db, query=query, days=None, defer_text=True, include_domains=False, limit=min(lim, 8))
        thesis_query = db.query(Thesis).options(defer(Thesis.statement)).filter((Thesis.title.ilike(f"%{query}%")) | (Thesis.statement.ilike(f"%{query}%")))
        thesis_query = _visible_thesis_query(thesis_query, include_hidden=include_hidden_theses)
        theses = thesis_query.order_by(Thesis.updated_at.desc()).limit(min(lim, 8)).all()
        notes = (
            db.query(Note)
            .options(defer(Note.body_md))
            .filter((Note.title.ilike(f"%{query}%")) | (Note.body_md.ilike(f"%{query}%")))
            .order_by(Note.updated_at.desc())
            .limit(min(lim, 8))
            .all()
        )
        ensure_dataset_tables(db)
        _ensure_supply_chain_tables(db)
        datasets = discover_datasets(db, query=query, limit=min(lim, 6)).rows
        charts = (
            db.query(Chart)
            .options(defer(Chart.description_md), defer(Chart.config))
            .filter(
                (Chart.title.ilike(f"%{query}%"))
                | (Chart.subtitle.ilike(f"%{query}%"))
                | (Chart.category.ilike(f"%{query}%"))
                | (Chart.description_md.ilike(f"%{query}%"))
            )
            .order_by(Chart.updated_at.desc())
            .limit(min(lim, 6))
            .all()
        )
        companies = _company_results(query, min(lim, 6))
        instrument_requests = _instrument_request_label_rows(min(lim, 6))
        product_stage_rows = _product_stage_label_rows(min(lim, 6))
        chains = (
            db.query(SupplyChain)
            .options(defer(SupplyChain.description), defer(SupplyChain.data))
            .filter(
                (SupplyChain.title.ilike(f"%{query}%"))
                | (SupplyChain.description.ilike(f"%{query}%"))
                | (SupplyChain.category.ilike(f"%{query}%"))
                | (SupplyChain.product.ilike(f"%{query}%"))
            )
            .order_by(SupplyChain.updated_at.desc())
            .limit(min(lim, 6))
            .all()
        )
        research_reports = list_research_reports(db, query=query, limit=min(lim, 6))
        research_subjects = _research_subject_label_rows(min(lim, 6))
        knowledge_assets = _knowledge_asset_label_rows(min(lim, 6))
        items = (
            _doc_items(docs)
            + _thesis_items(theses)
            + _note_items(notes)
            + _dataset_items(datasets)
            + _chart_items(charts)
            + _company_items(companies)
            + _instrument_request_items(instrument_requests)
            + _product_stage_items(product_stage_rows)
            + _supply_chain_items(chains)
            + _research_report_items(research_reports)
            + _research_subject_items(research_subjects)
            + _knowledge_asset_items(knowledge_assets)
        )
        return _search_response({"items": items[:lim]})

    return _search_response({"items": []}, ttl_s=20.0)


@app.get("/api/ingest/updates")
def ingest_updates(after_id: int | None = None, db: Session = Depends(get_session)) -> JSONResponse:
    q = db.query(IngestionRun).filter(IngestionRun.finished_at.isnot(None))
    if after_id:
        q = q.filter(IngestionRun.id > int(after_id))
    runs = q.order_by(IngestionRun.id.asc()).limit(50).all()

    items: list[dict[str, object]] = []
    for r in runs:
        notify = {}
        if isinstance(r.extra, dict):
            notify = r.extra.get("notify") or {}
        items.append(
            {
                "id": r.id,
                "source_id": r.source_id,
                "status": r.status,
                "items_new": r.items_new,
                "items_updated": r.items_updated,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "notify": notify,
            }
        )
    return JSONResponse({"items": items})
