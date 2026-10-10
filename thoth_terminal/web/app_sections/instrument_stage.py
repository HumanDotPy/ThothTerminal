from __future__ import annotations

from thoth_terminal.web.app_sections._shared import bind_app_context


bind_app_context(globals())


def _dataset_numeric_columns(payload: dict[str, object]) -> list[str]:
    cols = payload.get("columns") or []
    out = []
    for col in cols:
        if isinstance(col, dict) and str(col.get("data_type") or "") == "number":
            out.append(str(col.get("key") or ""))
    return [key for key in out if key]


def _dataset_time_column(payload: dict[str, object]) -> str | None:
    cols = payload.get("columns") or []
    for preferred in ("period_date", "date", "period", "as_of_date"):
        for col in cols:
            if isinstance(col, dict) and str(col.get("key") or "") == preferred:
                return preferred
    for col in cols:
        if isinstance(col, dict) and str(col.get("data_type") or "") == "date":
            return str(col.get("key") or "")
    return None


def _dataset_column_label(payload: dict[str, object], key: str | None) -> str:
    target = str(key or "").strip()
    if not target:
        return "Metric"
    for col in payload.get("columns") or []:
        if isinstance(col, dict) and str(col.get("key") or "").strip() == target:
            return str(col.get("label") or target.replace("_", " ").title())
    return target.replace("_", " ").title()


def _dataset_chart_columns(db: Session, dataset: Dataset) -> list[dict[str, object]]:
    version = latest_dataset_version(db, int(dataset.id), status="draft") or latest_dataset_version(db, int(dataset.id), status="published")
    if not version:
        return []
    return [
        {
            "key": row.key,
            "label": row.label,
            "data_type": row.data_type,
            "role": row.role,
            "config": dict(row.config or {}),
        }
        for row in db.query(DatasetColumn).filter_by(dataset_version_id=int(version.id)).order_by(DatasetColumn.sort_order.asc()).all()
    ]


def _dataset_column_unit(columns: list[dict[str, object]], key: str | None) -> str:
    target = str(key or "").strip()
    for col in columns:
        if str(col.get("key") or "").strip() != target:
            continue
        config = col.get("config") if isinstance(col.get("config"), dict) else {}
        for unit_key in ("unit", "units", "display_unit", "axis_unit", "currency"):
            value = config.get(unit_key)
            if value not in (None, ""):
                return str(value)
        label = f"{col.get('key') or ''} {col.get('label') or ''}".lower()
        if "%" in label or "percent" in label or "margin" in label or "rate" in label:
            return "%"
        if "usd" in label or "dollar" in label:
            return "USD"
    return ""


def _unique_chart_slug(db: Session, title: str) -> str:
    slug = _slugify_title(title)[:100]
    existing = db.query(Chart).filter(Chart.slug.like(f"{slug}%")).count()
    if existing > 0:
        slug = f"{slug}-{existing + 1}"
    return slug


def _dataset_chart_suggestions(payload: dict[str, object]) -> list[dict[str, object]]:
    rows = payload.get("rows") or []
    if not isinstance(rows, list) or not rows:
        return []
    columns = payload.get("columns") if isinstance(payload.get("columns"), list) else []
    semantic_cards = build_dataset_chart_cards(columns, rows)
    if semantic_cards:
        return semantic_cards
    x_key = _dataset_time_column(payload)
    numeric_keys = _dataset_numeric_columns(payload)
    if not x_key and numeric_keys:
        x_key = str((payload.get("columns") or [{}])[0].get("key") or "row_key")
    suggestions: list[dict[str, object]] = []
    for key in numeric_keys[:3]:
        series_rows = []
        for row in rows[:48]:
            if not isinstance(row, dict):
                continue
            values = row.get("values") or {}
            series_rows.append(
                {
                    "date": values.get(x_key) if x_key else row.get("row_key"),
                    "value": values.get(key),
                }
            )
        suggestions.append(
            {
                "title": _dataset_column_label(payload, key),
                "x_key": x_key,
                "y_key": key,
                "x_label": _dataset_column_label(payload, x_key),
                "y_label": _dataset_column_label(payload, key),
                "series_label": _dataset_column_label(payload, key),
                "subtitle": f"{_dataset_column_label(payload, x_key)} · {len(series_rows)} preview rows",
                "display_type": "line",
                "baseline_zero": False,
                "context": [],
                "stats": {"count": len(series_rows), "unit": ""},
                "series": series_rows,
            }
        )
    return suggestions


@app.get("/instruments/requests", response_class=HTMLResponse)
def instrument_requests_view(
    request: Request,
    q: str | None = None,
    status: str | None = "open",
    scope: str | None = "active_universe",
    db: Session = Depends(get_session),
) -> HTMLResponse:
    status_filter = str(status or "open").strip() or "open"
    scope_filter = str(scope or "active_universe").strip() or "active_universe"
    items = list_instrument_requests(db, query=q, status=status_filter, scope=scope_filter, limit=200, include_payloads=False)
    active_scope = build_active_universe_request_scope(db)
    queue_scope_by_id = instrument_request_scope_map(db, items, active_scope=active_scope)
    payload_items = []
    for row in items:
        payload = serialize_instrument_request_summary(row)
        payload["queue_scope"] = queue_scope_by_id.get(int(row.id), "manual")
        payload_items.append(payload)
    return _template_response(
        "instrument_requests.html",
        {
            "request": request,
            "items": payload_items,
            "query": str(q or "").strip(),
            "status_filter": status_filter,
            "scope_filter": scope_filter,
            "scope_counts": instrument_request_scope_counts(db, query=q, status=status_filter),
        },
    )


@app.get("/instruments/requests/{request_id}", response_class=HTMLResponse)
def instrument_request_detail(request: Request, request_id: int, db: Session = Depends(get_session)) -> HTMLResponse:
    row = get_instrument_request(db, int(request_id))
    if row is None:
        raise HTTPException(status_code=404, detail="Instrument request not found")
    payload = serialize_instrument_request(row)
    linked = dict(row.linked_objects or {})
    report_ids = sorted(
        {
            int(v)
            for v in ([linked.get("report_id")] + list(linked.get("report_ids") or []))
            if str(v).isdigit()
        }
    )
    company_ids = sorted(
        {
            int(v)
            for v in ([linked.get("company_id")] + list(linked.get("company_ids") or []))
            if str(v).isdigit()
        }
    )
    dataset_ids = [int(v) for v in (linked.get("dataset_ids") or []) if str(v).isdigit()]
    chain_ids = [int(v) for v in (linked.get("supply_chain_ids") or []) if str(v).isdigit()]
    note_ids = [int(v) for v in (linked.get("note_ids") or []) if str(v).isdigit()]
    thesis_ids = [int(v) for v in (linked.get("thesis_ids") or []) if str(v).isdigit()]
    stage_entry_ids = sorted(
        {
            int(v)
            for v in ((linked.get("stage_entry_ids") or []) + (linked.get("product_stage_ids") or []))
            if str(v).isdigit()
        }
    )
    reports = db.query(ResearchReport).filter(ResearchReport.id.in_(report_ids)).all() if report_ids else []
    companies = db.query(Company).filter(Company.id.in_(company_ids)).all() if company_ids else []
    datasets = db.query(Dataset).filter(Dataset.id.in_(dataset_ids)).all() if dataset_ids else []
    chains = db.query(SupplyChain).filter(SupplyChain.id.in_(chain_ids)).all() if chain_ids else []
    notes = db.query(Note).filter(Note.id.in_(note_ids)).all() if note_ids else []
    theses = db.query(Thesis).filter(Thesis.id.in_(thesis_ids)).all() if thesis_ids else []
    stage_entries = (
        [serialize_product_stage_entry(item) for item in db.query(ProductStageEntry).filter(ProductStageEntry.id.in_(stage_entry_ids)).all()]
        if stage_entry_ids
        else []
    )
    return _template_response(
        "instrument_request_detail.html",
        {
            "request": request,
            "item": payload,
            "markdown_export": instrument_request_markdown(row),
            "linked_reports": reports,
            "linked_companies": companies,
            "linked_datasets": datasets,
            "linked_supply_chains": chains,
            "linked_notes": notes,
            "linked_theses": theses,
            "linked_stage_entries": stage_entries,
        },
    )


@app.post("/instruments/requests/{request_id}/run")
def instrument_request_run_action(
    request_id: int,
    run_summary: str | None = Form(None),
    db: Session = Depends(get_session),
) -> RedirectResponse:
    try:
        run_instrument_request(db, int(request_id), run_summary=str(run_summary or "").strip() or None)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    db.commit()
    return RedirectResponse(url=f"/instruments/requests/{int(request_id)}", status_code=303)


@app.post("/instruments/requests/{request_id}/review")
def instrument_request_review_action(
    request_id: int,
    decision: str = Form(...),
    reviewer_note: str | None = Form(None),
    db: Session = Depends(get_session),
) -> RedirectResponse:
    try:
        review_instrument_request(
            db,
            int(request_id),
            decision=str(decision or "").strip(),
            reviewer_note=str(reviewer_note or "").strip() or None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return RedirectResponse(url=f"/instruments/requests/{int(request_id)}", status_code=303)


@app.post("/instruments/requests/{request_id}/fulfill")
def instrument_request_fulfill_action(
    request_id: int,
    fulfillment_summary: str = Form(...),
    fulfilled_by: str | None = Form(None),
    db: Session = Depends(get_session),
) -> RedirectResponse:
    try:
        fulfill_instrument_request(
            db,
            int(request_id),
            fulfillment_summary=str(fulfillment_summary or "").strip(),
            fulfilled_by=str(fulfilled_by or "").strip() or None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return RedirectResponse(url=f"/instruments/requests/{int(request_id)}", status_code=303)


@app.post("/api/instrument-requests")
async def api_instrument_request_create(request: Request, db: Session = Depends(get_session)) -> JSONResponse:
    payload = await _parse_supply_chain_payload(request)
    title = str(payload.get("title") or "").strip()
    if not title:
        return JSONResponse({"ok": False, "error": "Title is required"}, status_code=400)
    row = create_instrument_request(
        db,
        title=title,
        intent=str(payload.get("intent") or "instrument").strip() or "instrument",
        summary=str(payload.get("summary") or "").strip() or None,
        target_entities=[str(v) for v in (payload.get("target_entities") or []) if str(v).strip()] if isinstance(payload.get("target_entities"), list) else None,
        target_themes=[str(v) for v in (payload.get("target_themes") or []) if str(v).strip()] if isinstance(payload.get("target_themes"), list) else None,
        desired_outputs=[str(v) for v in (payload.get("desired_outputs") or []) if str(v).strip()] if isinstance(payload.get("desired_outputs"), list) else None,
        evidence_bundle=[dict(v) for v in (payload.get("evidence_bundle") or []) if isinstance(v, dict)] if isinstance(payload.get("evidence_bundle"), list) else None,
        provenance_summary=str(payload.get("provenance_summary") or "").strip() or None,
        evidence_tier=str(payload.get("evidence_tier") or "").strip() or None,
        status=str(payload.get("status") or "draft").strip().lower() or "draft",
        priority=int(payload.get("priority")) if str(payload.get("priority") or "").isdigit() else 5,
        automation_source=str(payload.get("automation_source") or "").strip() or None,
        source_kind=str(payload.get("source_kind") or "").strip() or None,
        draft_payload=dict(payload.get("draft_payload") or {}) if isinstance(payload.get("draft_payload"), dict) else None,
        linked_objects=dict(payload.get("linked_objects") or {}) if isinstance(payload.get("linked_objects"), dict) else None,
    )
    db.commit()
    return JSONResponse({"ok": True, "item": serialize_instrument_request(row), "redirect": f"/instruments/requests/{int(row.id)}"})


@app.get("/api/instrument-requests/{request_id}")
def api_instrument_request_get(request_id: int, db: Session = Depends(get_session)) -> JSONResponse:
    row = get_instrument_request(db, int(request_id))
    if row is None:
        return JSONResponse({"ok": False, "error": "Instrument request not found"}, status_code=404)
    payload = serialize_instrument_request(row)
    payload["markdown"] = instrument_request_markdown(row)
    return JSONResponse({"ok": True, "item": payload})


@app.post("/api/instrument-requests/{request_id}/review")
async def api_instrument_request_review(request_id: int, request: Request, db: Session = Depends(get_session)) -> JSONResponse:
    payload = await _parse_supply_chain_payload(request)
    try:
        row = review_instrument_request(
            db,
            int(request_id),
            decision=str(payload.get("decision") or "").strip(),
            reviewer_note=str(payload.get("reviewer_note") or "").strip() or None,
            live_payload=dict(payload.get("live_payload") or {}) if isinstance(payload.get("live_payload"), dict) else None,
        )
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    db.commit()
    return JSONResponse({"ok": True, "item": serialize_instrument_request(row)})


@app.post("/api/instrument-requests/{request_id}/run")
async def api_instrument_request_run(request_id: int, request: Request, db: Session = Depends(get_session)) -> JSONResponse:
    payload = await _parse_supply_chain_payload(request)
    try:
        row = run_instrument_request(
            db,
            int(request_id),
            draft_payload_patch=dict(payload.get("draft_payload_patch") or {}) if isinstance(payload.get("draft_payload_patch"), dict) else None,
            linked_objects_patch=dict(payload.get("linked_objects_patch") or {}) if isinstance(payload.get("linked_objects_patch"), dict) else None,
            run_summary=str(payload.get("run_summary") or "").strip() or None,
        )
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    db.commit()
    return JSONResponse({"ok": True, "item": serialize_instrument_request(row)})


@app.post("/api/instrument-requests/{request_id}/fulfill")
async def api_instrument_request_fulfill(request_id: int, request: Request, db: Session = Depends(get_session)) -> JSONResponse:
    payload = await _parse_supply_chain_payload(request)
    try:
        row = fulfill_instrument_request(
            db,
            int(request_id),
            fulfillment_summary=str(payload.get("fulfillment_summary") or "").strip(),
            fulfilled_by=str(payload.get("fulfilled_by") or "").strip() or None,
            live_payload_patch=dict(payload.get("live_payload_patch") or {}) if isinstance(payload.get("live_payload_patch"), dict) else None,
            linked_objects_patch=dict(payload.get("linked_objects_patch") or {}) if isinstance(payload.get("linked_objects_patch"), dict) else None,
            evidence_bundle_patch=[dict(v) for v in (payload.get("evidence_bundle_patch") or []) if isinstance(v, dict)]
            if isinstance(payload.get("evidence_bundle_patch"), list)
            else None,
            verification=dict(payload.get("verification") or {}) if isinstance(payload.get("verification"), dict) else None,
            close_linked_gaps=bool(payload.get("close_linked_gaps", True)),
        )
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    db.commit()
    return JSONResponse({"ok": True, "item": serialize_instrument_request(row)})


@app.get("/stage-tracker", response_class=HTMLResponse)
def stage_tracker_view(
    request: Request,
    q: str | None = None,
    company: str | None = None,
    product: str | None = None,
    stage: str | None = None,
    status: str | None = None,
    evidence_tier: str | None = None,
    conflict_state: str | None = None,
    db: Session = Depends(get_session),
) -> HTMLResponse:
    items = list_product_stage_entries(
        db,
        query=q,
        company=company,
        product=product,
        stage=stage,
        status=status,
        evidence_tier=evidence_tier,
        conflict_state=conflict_state,
        limit=250,
    )
    return _template_response(
        "stage_tracker.html",
        {
            "request": request,
            "items": [serialize_product_stage_entry(row) for row in items],
            "query": str(q or "").strip(),
            "company_filter": str(company or "").strip(),
            "product_filter": str(product or "").strip(),
            "stage_filter": str(stage or "").strip(),
            "status_filter": str(status or "").strip(),
            "evidence_tier_filter": str(evidence_tier or "").strip(),
            "conflict_state_filter": str(conflict_state or "").strip(),
        },
    )


@app.get("/stage-tracker/{entry_id}", response_class=HTMLResponse)
def stage_tracker_detail(request: Request, entry_id: int, db: Session = Depends(get_session)) -> HTMLResponse:
    row = get_product_stage_entry(db, int(entry_id))
    if row is None:
        raise HTTPException(status_code=404, detail="Product stage entry not found")
    payload = serialize_product_stage_entry(row)
    dataset_ids = [int(v) for v in (payload.get("linked_dataset_ids") or []) if str(v).isdigit()]
    chain_ids = [int(v) for v in (payload.get("linked_supply_chain_ids") or []) if str(v).isdigit()]
    note_ids = [int(v) for v in (payload.get("linked_note_ids") or []) if str(v).isdigit()]
    thesis_ids = [int(v) for v in (payload.get("linked_thesis_ids") or []) if str(v).isdigit()]
    claims = [serialize_product_stage_claim(item) for item in list_product_stage_claims(db, entry_id=int(row.id), limit=250)]
    source_document_ids = sorted(
        {
            int(item.get("source_document_id"))
            for item in claims
            if str(item.get("source_document_id") or "").isdigit()
        }
    )
    datasets = db.query(Dataset).filter(Dataset.id.in_(dataset_ids)).all() if dataset_ids else []
    chains = db.query(SupplyChain).filter(SupplyChain.id.in_(chain_ids)).all() if chain_ids else []
    notes = db.query(Note).filter(Note.id.in_(note_ids)).all() if note_ids else []
    theses = db.query(Thesis).filter(Thesis.id.in_(thesis_ids)).all() if thesis_ids else []
    source_documents = (
        {
            int(doc.id): doc
            for doc in db.query(Document).filter(Document.id.in_(source_document_ids)).all()
        }
        if source_document_ids
        else {}
    )
    return _template_response(
        "stage_tracker_detail.html",
        {
            "request": request,
            "item": payload,
            "claim_rows": claims,
            "history_rows": [serialize_product_stage_history(item) for item in list_product_stage_history(db, int(row.id))],
            "linked_datasets": datasets,
            "linked_supply_chains": chains,
            "linked_notes": notes,
            "linked_theses": theses,
            "source_documents": source_documents,
        },
    )


@app.get("/api/stage-tracker")
def api_stage_tracker_list(
    q: str | None = None,
    company: str | None = None,
    product: str | None = None,
    stage: str | None = None,
    status: str | None = None,
    evidence_tier: str | None = None,
    conflict_state: str | None = None,
    limit: int = 50,
    db: Session = Depends(get_session),
) -> JSONResponse:
    rows = list_product_stage_entries(
        db,
        query=q,
        company=company,
        product=product,
        stage=stage,
        status=status,
        evidence_tier=evidence_tier,
        conflict_state=conflict_state,
        limit=limit,
    )
    return JSONResponse({"ok": True, "items": [serialize_product_stage_entry(row) for row in rows]})


@app.get("/api/stage-tracker/{entry_id}")
def api_stage_tracker_get(entry_id: int, db: Session = Depends(get_session)) -> JSONResponse:
    row = get_product_stage_entry(db, int(entry_id))
    if row is None:
        return JSONResponse({"ok": False, "error": "Product stage entry not found"}, status_code=404)
    return JSONResponse(
        {
            "ok": True,
            "item": serialize_product_stage_entry(row),
            "claims": [serialize_product_stage_claim(item) for item in list_product_stage_claims(db, entry_id=int(row.id), limit=250)],
            "history": [serialize_product_stage_history(item) for item in list_product_stage_history(db, int(row.id))],
        }
    )


@app.post("/api/stage-tracker")
async def api_stage_tracker_update(request: Request, db: Session = Depends(get_session)) -> JSONResponse:
    payload = await _parse_supply_chain_payload(request)
    company_name = str(payload.get("company_name") or "").strip()
    product_name = str(payload.get("product_name") or "").strip()
    stage = str(payload.get("stage") or "").strip()
    if not company_name or not product_name or not stage:
        return JSONResponse({"ok": False, "error": "company_name, product_name, and stage are required"}, status_code=400)
    target_date = None
    if str(payload.get("target_date") or "").strip():
        try:
            target_date = date.fromisoformat(str(payload.get("target_date")))
        except Exception as exc:
            return JSONResponse({"ok": False, "error": f"Invalid target_date: {exc}"}, status_code=400)
    try:
        row = update_product_stage_entry(
            db,
            entry_id=int(payload.get("id")) if str(payload.get("id") or "").isdigit() else None,
            company_name=company_name,
            product_name=product_name,
            stage=stage,
            target_date=target_date,
            date_precision=str(payload.get("date_precision") or "").strip() or None,
            date_text=str(payload.get("date_text") or "").strip() or None,
            certainty=str(payload.get("certainty") or "").strip() or None,
            status=str(payload.get("status") or "").strip() or None,
            notes=str(payload.get("notes") or "").strip() or None,
            evidence_tier=str(payload.get("evidence_tier") or "").strip() or None,
            evidence_bundle=[dict(v) for v in (payload.get("evidence_bundle") or []) if isinstance(v, dict)] if isinstance(payload.get("evidence_bundle"), list) else None,
            linked_dataset_ids=[int(v) for v in (payload.get("linked_dataset_ids") or []) if str(v).isdigit()] if isinstance(payload.get("linked_dataset_ids"), list) else None,
            linked_supply_chain_ids=[int(v) for v in (payload.get("linked_supply_chain_ids") or []) if str(v).isdigit()] if isinstance(payload.get("linked_supply_chain_ids"), list) else None,
            linked_note_ids=[int(v) for v in (payload.get("linked_note_ids") or []) if str(v).isdigit()] if isinstance(payload.get("linked_note_ids"), list) else None,
            linked_thesis_ids=[int(v) for v in (payload.get("linked_thesis_ids") or []) if str(v).isdigit()] if isinstance(payload.get("linked_thesis_ids"), list) else None,
            extra=dict(payload.get("extra") or {}) if isinstance(payload.get("extra"), dict) else None,
            source_document_id=int(payload.get("source_document_id")) if str(payload.get("source_document_id") or "").isdigit() else None,
            history_note=str(payload.get("history_note") or "").strip() or None,
        )
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    db.commit()
    return JSONResponse({"ok": True, "item": serialize_product_stage_entry(row), "redirect": f"/stage-tracker/{int(row.id)}"})


@app.get("/api/stage-tracker/{entry_id}/claims")
def api_stage_tracker_claims(
    entry_id: int,
    review_status: str | None = None,
    limit: int = 100,
    db: Session = Depends(get_session),
) -> JSONResponse:
    row = get_product_stage_entry(db, int(entry_id))
    if row is None:
        return JSONResponse({"ok": False, "error": "Product stage entry not found"}, status_code=404)
    claims = list_product_stage_claims(
        db,
        entry_id=int(entry_id),
        review_status=str(review_status or "").strip() or None,
        limit=limit,
    )
    return JSONResponse({"ok": True, "claims": [serialize_product_stage_claim(item) for item in claims]})


@app.post("/api/stage-tracker/{entry_id}/claims")
async def api_stage_tracker_claim_create(entry_id: int, request: Request, db: Session = Depends(get_session)) -> JSONResponse:
    payload = await _parse_supply_chain_payload(request)
    stage = str(payload.get("stage") or "").strip()
    if not stage:
        return JSONResponse({"ok": False, "error": "stage is required"}, status_code=400)
    target_date = None
    if str(payload.get("target_date") or "").strip():
        try:
            target_date = date.fromisoformat(str(payload.get("target_date")))
        except Exception as exc:
            return JSONResponse({"ok": False, "error": f"Invalid target_date: {exc}"}, status_code=400)
    source_date = None
    if str(payload.get("source_date") or "").strip():
        try:
            source_date = datetime.fromisoformat(str(payload.get("source_date")))
        except Exception as exc:
            return JSONResponse({"ok": False, "error": f"Invalid source_date: {exc}"}, status_code=400)
    try:
        claim = create_product_stage_claim(
            db,
            entry_id=int(entry_id),
            company_name=str(payload.get("company_name") or "").strip() or None,
            product_name=str(payload.get("product_name") or "").strip() or None,
            stage=stage,
            target_date=target_date,
            date_precision=str(payload.get("date_precision") or "").strip() or None,
            date_text=str(payload.get("date_text") or "").strip() or None,
            certainty=str(payload.get("certainty") or "").strip() or None,
            evidence_tier=str(payload.get("evidence_tier") or "").strip() or None,
            provenance_summary=str(payload.get("provenance_summary") or "").strip() or None,
            summary=str(payload.get("summary") or "").strip() or None,
            rationale=str(payload.get("rationale") or "").strip() or None,
            source_document_id=int(payload.get("source_document_id")) if str(payload.get("source_document_id") or "").isdigit() else None,
            source_date=source_date,
            linked_dataset_ids=[int(v) for v in (payload.get("linked_dataset_ids") or []) if str(v).isdigit()] if isinstance(payload.get("linked_dataset_ids"), list) else None,
            linked_supply_chain_ids=[int(v) for v in (payload.get("linked_supply_chain_ids") or []) if str(v).isdigit()] if isinstance(payload.get("linked_supply_chain_ids"), list) else None,
            linked_note_ids=[int(v) for v in (payload.get("linked_note_ids") or []) if str(v).isdigit()] if isinstance(payload.get("linked_note_ids"), list) else None,
            linked_thesis_ids=[int(v) for v in (payload.get("linked_thesis_ids") or []) if str(v).isdigit()] if isinstance(payload.get("linked_thesis_ids"), list) else None,
            automation_source=str(payload.get("automation_source") or "").strip() or None,
            source_kind=str(payload.get("source_kind") or "").strip() or None,
            review_status=str(payload.get("review_status") or "draft").strip().lower() or "draft",
            claim_key=str(payload.get("claim_key") or "").strip() or None,
            extra=dict(payload.get("extra") or {}) if isinstance(payload.get("extra"), dict) else None,
            history_note=str(payload.get("history_note") or "").strip() or None,
            entry_status=str(payload.get("entry_status") or "").strip() or None,
        )
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    db.commit()
    return JSONResponse({"ok": True, "item": serialize_product_stage_claim(claim)})


@app.post("/api/stage-tracker/claims/{claim_id}/review")
async def api_stage_tracker_claim_review(claim_id: int, request: Request, db: Session = Depends(get_session)) -> JSONResponse:
    payload = await _parse_supply_chain_payload(request)
    decision = str(payload.get("decision") or "").strip()
    if not decision:
        return JSONResponse({"ok": False, "error": "decision is required"}, status_code=400)
    try:
        claim = review_product_stage_claim(
            db,
            int(claim_id),
            decision=decision,
            reviewer_note=str(payload.get("reviewer_note") or "").strip() or None,
        )
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    db.commit()
    return JSONResponse({"ok": True, "item": serialize_product_stage_claim(claim)})


@app.get("/api/stage-tracker/{entry_id}/history")
def api_stage_tracker_history(entry_id: int, db: Session = Depends(get_session)) -> JSONResponse:
    row = get_product_stage_entry(db, int(entry_id))
    if row is None:
        return JSONResponse({"ok": False, "error": "Product stage entry not found"}, status_code=404)
    return JSONResponse({"ok": True, "history": [serialize_product_stage_history(item) for item in list_product_stage_history(db, int(row.id))]})
