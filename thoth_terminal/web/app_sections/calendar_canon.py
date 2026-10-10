from __future__ import annotations

from thoth_terminal.web.app_sections._shared import bind_app_context


bind_app_context(globals())


def _calendar_iso_date(value: object) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _calendar_status_label(row: dict[str, object], *, today: date) -> str:
    status = str(row.get("status") or "watch").strip().lower()
    event_date = _calendar_iso_date(row.get("event_date"))
    if status == "completed" and event_date is not None and event_date >= today:
        return "confirmed"
    return status or "watch"


def _calendar_view_rows(rows: list[dict[str, object]], *, view: str, today: date) -> list[dict[str, object]]:
    view_key = view if view in {"upcoming", "recent", "all"} else "upcoming"
    normalized: list[dict[str, object]] = []
    for row in rows:
        event_date = _calendar_iso_date(row.get("event_date"))
        if view_key == "upcoming":
            if event_date is not None and event_date < today:
                continue
        elif view_key == "recent":
            if event_date is None or event_date >= today:
                continue
        item = dict(row)
        item["status_label"] = _calendar_status_label(item, today=today)
        normalized.append(item)
    if view_key == "recent":
        normalized.sort(
            key=lambda row: (
                _calendar_iso_date(row.get("event_date")) or date.min,
                int(row.get("priority") or 0),
                str(row.get("updated_at") or ""),
            ),
            reverse=True,
        )
    return normalized


@app.get("/calendar", response_class=HTMLResponse)
def investment_calendar_view(
    request: Request,
    kind: str | None = None,
    view: str = "upcoming",
    gap_scope: str | None = "active_universe",
    horizon: int = 365,
    db: Session = Depends(get_session),
) -> HTMLResponse:
    stats: dict[str, int] = {}
    view_key = str(view or "upcoming").strip().lower()
    if view_key not in {"upcoming", "recent", "all"}:
        view_key = "upcoming"
    days_back = 30 if view_key == "recent" else 180 if view_key == "all" else 1
    days_forward = 1 if view_key == "recent" else max(30, min(int(horizon or 365), 1095))
    catalysts = list_investment_catalysts(
        db,
        days_back=days_back,
        days_forward=days_forward,
        limit=500,
    )
    kind_filter = str(kind or "").strip().lower()
    if kind_filter:
        catalysts = [row for row in catalysts if str(row.get("catalyst_kind") or "").lower() == kind_filter]
    today = calendar_today()
    catalysts = _calendar_view_rows(catalysts, view=view_key, today=today)
    gap_scope_filter = str(gap_scope or "active_universe").strip().lower().replace("-", "_") or "active_universe"
    gap_candidates = (
        db.query(CoverageGap)
        .options(
            load_only(
                CoverageGap.id,
                CoverageGap.gap_key,
                CoverageGap.subject_id,
                CoverageGap.title,
                CoverageGap.gap_kind,
                CoverageGap.status,
                CoverageGap.severity,
                CoverageGap.payload,
                CoverageGap.updated_at,
            )
        )
        .filter(CoverageGap.status != "resolved")
        .order_by(CoverageGap.severity.desc(), CoverageGap.updated_at.desc())
        .limit(1000)
        .all()
    )
    active_scope = build_active_universe_request_scope(db)
    gap_scope_counts = {"active_universe": 0, "off_universe": 0, "manual": 0}
    open_gaps: list[dict[str, object]] = []
    for gap in gap_candidates:
        scope_name = coverage_gap_scope(db, gap, active_scope=active_scope)
        if scope_name in gap_scope_counts:
            gap_scope_counts[scope_name] += 1
        if request_queue_scope_matches(gap_scope_filter, scope_name) and len(open_gaps) < 25:
            open_gaps.append(
                {
                    "id": int(gap.id),
                    "title": gap.title,
                    "gap_kind": gap.gap_kind,
                    "severity": int(gap.severity or 0),
                    "updated_at": gap.updated_at,
                    "queue_scope": scope_name,
                }
            )
    return _template_response(
        "investment_calendar.html",
        {
            "request": request,
            "catalysts": catalysts,
            "kind_filter": kind_filter,
            "view": view_key,
            "gap_scope_filter": gap_scope_filter,
            "gap_scope_counts": gap_scope_counts,
            "sync_stats": stats,
            "open_gaps": open_gaps,
            "memo_count": db.query(InvestmentMemo).filter(InvestmentMemo.status != "archived").count(),
            "catalyst_count": db.query(InvestmentCatalyst).count(),
        },
    )


@app.post("/calendar/catalysts")
def investment_calendar_catalyst_create(
    title: str = Form(...),
    catalyst_kind: str = Form("manual"),
    event_date: str | None = Form(None),
    ticker: str | None = Form(None),
    summary: str | None = Form(None),
    evidence_tier: str | None = Form(None),
    source_kind: str | None = Form(None),
    source_url: str | None = Form(None),
    priority: int = Form(5),
    confidence: float | None = Form(None),
    db: Session = Depends(get_session),
) -> RedirectResponse:
    upsert_investment_catalyst(
        db,
        {
            "title": title,
            "catalyst_kind": catalyst_kind,
            "event_date": event_date,
            "ticker": ticker,
            "summary": summary,
            "evidence_tier": evidence_tier,
            "source_kind": source_kind or "manual",
            "source_url": source_url,
            "priority": priority,
            "confidence": confidence,
            "status": "watch",
        },
        actor="manual",
    )
    db.commit()
    _invalidate_terminal_caches("home:", "search:")
    return RedirectResponse("/calendar", status_code=303)


@app.post("/calendar/sync")
def investment_calendar_sync(db: Session = Depends(get_session)) -> RedirectResponse:
    sync_decision_spine(db)
    db.commit()
    return RedirectResponse("/calendar", status_code=303)


@app.post("/api/decision-spine/sync")
def api_decision_spine_sync(db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    stats = sync_decision_spine(db)
    db.commit()
    return JSONResponse({"ok": True, "stats": stats})


@app.post("/api/research-canon/maintenance")
async def api_research_canon_maintenance(request: Request, db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    payload = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    from thoth_terminal.research_canon_service import run_research_canon_maintenance

    result = run_research_canon_maintenance(
        db,
        universe_slug=str((payload or {}).get("universe") or (payload or {}).get("universe_slug") or "").strip() or None,
        create_requests=bool((payload or {}).get("create_requests", False)),
        create_cards=bool((payload or {}).get("create_cards", False)),
        limit=int((payload or {}).get("limit")) if str((payload or {}).get("limit") or "").isdigit() else None,
    )
    db.commit()
    return JSONResponse({"ok": True, "result": result})


@app.get("/api/research-canon/gaps")
def api_research_canon_gaps(
    universe: str | None = None,
    subject_id: int | None = None,
    gap_kind: str | None = None,
    status: str | None = None,
    limit: int = 100,
    db: Session = Depends(get_session),
) -> JSONResponse:
    _guard_internal_terminal()
    from thoth_terminal.research_canon_service import list_coverage_gaps

    return JSONResponse(
        {
            "ok": True,
            **list_coverage_gaps(
                db,
                universe_slug=universe,
                subject_id=subject_id,
                gap_kind=gap_kind,
                status=status,
                limit=limit,
            ),
        }
    )


@app.get("/api/research-canon/gaps/{gap_id}")
def api_research_canon_gap(gap_id: int, db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    from thoth_terminal.research_canon_service import get_coverage_gap

    gap = get_coverage_gap(db, gap_id=gap_id)
    if gap is None:
        raise HTTPException(status_code=404, detail="Coverage gap not found.")
    return JSONResponse({"ok": True, "gap": gap})


@app.post("/api/research-canon/scenarios")
async def api_research_canon_scenario_upsert(request: Request, db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    payload = await request.json()
    from thoth_terminal.research_canon_service import serialize_scenario, upsert_subject_scenario

    subject_id = _research_canon_subject_id(db, payload if isinstance(payload, dict) else {})
    row = upsert_subject_scenario(
        db,
        subject_id=subject_id,
        slug=str(payload.get("slug") or "").strip() or None,
        name=str(payload.get("name") or "").strip() or None,
        scenario_kind=str(payload.get("scenario_kind") or payload.get("kind") or "custom").strip(),
        probability=payload.get("probability"),
        confidence=payload.get("confidence"),
        selected_canon=bool(payload.get("selected_canon", False)),
        thesis_summary=str(payload.get("thesis_summary") or "").strip() or None,
        assumptions=dict(payload.get("assumptions") or {}) if isinstance(payload.get("assumptions"), dict) else None,
        key_metrics=dict(payload.get("key_metrics") or {}) if isinstance(payload.get("key_metrics"), dict) else None,
        evidence_links=[dict(item) for item in payload.get("evidence_links") if isinstance(item, dict)] if isinstance(payload.get("evidence_links"), list) else None,
        invalidation_triggers=[str(item) for item in payload.get("invalidation_triggers")] if isinstance(payload.get("invalidation_triggers"), list) else None,
        open_questions=[str(item) for item in payload.get("open_questions")] if isinstance(payload.get("open_questions"), list) else None,
        status=str(payload.get("status") or "draft").strip() or "draft",
        audit={"source": "api"},
    )
    db.commit()
    return JSONResponse({"ok": True, "scenario": serialize_scenario(row)})


@app.post("/api/research-canon/scenarios/{scenario_id}/select")
async def api_research_canon_scenario_select(scenario_id: int, request: Request, db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    payload = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    from thoth_terminal.research_canon_service import select_canon_scenario, serialize_scenario

    row = select_canon_scenario(db, scenario_id=scenario_id, supersede_previous=bool((payload or {}).get("supersede_previous", True)))
    db.commit()
    return JSONResponse({"ok": True, "scenario": serialize_scenario(row)})


@app.get("/api/research-canon/source-pack/{subject_id}")
def api_research_canon_source_pack(subject_id: int, db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    from thoth_terminal.research_canon_service import serialize_source_pack

    return JSONResponse({"ok": True, "source_pack": serialize_source_pack(db.query(ResearchSourcePack).filter_by(subject_id=subject_id).first())})


@app.post("/api/research-canon/source-pack/{subject_id}/refresh")
async def api_research_canon_source_pack_refresh(subject_id: int, request: Request, db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    payload = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    from thoth_terminal.research_canon_service import discover_source_pack_items, serialize_source_pack, upsert_source_pack

    if bool((payload or {}).get("discover", False)):
        result = discover_source_pack_items(db, subject_id=subject_id, create_requests=bool((payload or {}).get("create_requests", True)), audit={"source": "api"})
    else:
        result = {"source_pack": serialize_source_pack(upsert_source_pack(db, subject_id=subject_id, audit={"source": "api"}))}
    db.commit()
    return JSONResponse({"ok": True, "result": result})


@app.post("/api/research-canon/source-packs/active-universe/refresh")
async def api_research_canon_active_universe_source_packs_refresh(request: Request, db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    payload = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    from thoth_terminal.research_canon_service import discover_active_universe_source_pack_items

    result = discover_active_universe_source_pack_items(
        db,
        universe_slug=str((payload or {}).get("universe") or "").strip() or None,
        create_requests=bool((payload or {}).get("create_requests", True)),
        create_gap_requests=bool((payload or {}).get("create_gap_requests", True)),
        create_cards=bool((payload or {}).get("create_cards", False)),
        materialize_official_documents=bool((payload or {}).get("materialize_official_documents", True)),
        register_official_sources=bool((payload or {}).get("register_official_sources", True)),
        run_coverage_scan=bool((payload or {}).get("run_coverage_scan", False)),
        stale_after_days=int((payload or {}).get("stale_after_days")) if str((payload or {}).get("stale_after_days") or "").isdigit() else 120,
        limit=int((payload or {}).get("limit")) if str((payload or {}).get("limit") or "").isdigit() else None,
        audit={"source": "api"},
    )
    db.commit()
    return JSONResponse({"ok": True, "result": result})


@app.get("/api/research-canon/quality/{subject_id}")
def api_research_canon_quality(subject_id: int, db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    from thoth_terminal.research_canon_service import serialize_quality

    return JSONResponse({"ok": True, "quality": serialize_quality(db.query(ResearchReportQuality).filter_by(subject_id=subject_id).first())})


@app.post("/api/research-canon/quality/{subject_id}/refresh")
async def api_research_canon_quality_refresh(subject_id: int, request: Request, db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    payload = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    from thoth_terminal.research_canon_service import serialize_quality, upsert_report_quality

    row = upsert_report_quality(db, subject_id=subject_id, stale_after_days=int((payload or {}).get("stale_after_days")) if str((payload or {}).get("stale_after_days") or "").isdigit() else 120)
    db.commit()
    return JSONResponse({"ok": True, "quality": serialize_quality(row)})


@app.get("/api/research-canon/cards/{subject_id}")
def api_research_canon_card(subject_id: int, db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    from thoth_terminal.research_canon_service import get_agent_canon_card

    return JSONResponse({"ok": True, "agent_canon_card": get_agent_canon_card(db, subject_id=subject_id)})


@app.post("/api/research-canon/cards/{subject_id}/generate")
def api_research_canon_card_generate(subject_id: int, db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    from thoth_terminal.research_canon_service import generate_agent_canon_card, get_agent_canon_card

    generate_agent_canon_card(db, subject_id=subject_id)
    db.commit()
    return JSONResponse({"ok": True, "agent_canon_card": get_agent_canon_card(db, subject_id=subject_id)})


@app.post("/api/research-canon/campaigns/from-universe")
async def api_research_canon_campaign_from_universe(request: Request, db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    payload = await request.json()
    from thoth_terminal.research_canon_service import create_campaign_from_universe

    result = create_campaign_from_universe(
        db,
        universe_slug=str(payload.get("universe") or payload.get("universe_slug") or "").strip(),
        name=str(payload.get("name") or "").strip() or None,
        limit=int(payload.get("limit")) if str(payload.get("limit") or "").isdigit() else None,
        preview=bool(payload.get("preview", True)),
    )
    db.commit()
    return JSONResponse({"ok": True, "result": result})


@app.get("/api/research-canon/campaigns/{campaign_id}")
def api_research_canon_campaign(campaign_id: int, include_items: bool = False, limit: int = 100, db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    from thoth_terminal.research_canon_service import get_campaign

    row = get_campaign(db, campaign_id=campaign_id, include_items=include_items, limit=limit)
    if row is None:
        raise HTTPException(status_code=404, detail="Research campaign not found.")
    return JSONResponse({"ok": True, "campaign": row})


@app.get("/api/research-canon/campaigns/{campaign_id}/next")
def api_research_canon_campaign_next(campaign_id: int, limit: int = 10, db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    from thoth_terminal.research_canon_service import next_campaign_items

    rows = next_campaign_items(db, campaign_id=campaign_id, limit=limit)
    db.commit()
    return JSONResponse({"ok": True, "count": len(rows), "items": rows})


@app.post("/api/research-canon/campaigns/{campaign_id}/run")
async def api_research_canon_campaign_run(campaign_id: int, request: Request, db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    payload = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    from thoth_terminal.research_canon_service import execute_campaign_items

    result = execute_campaign_items(
        db,
        campaign_id=campaign_id,
        limit=int((payload or {}).get("limit")) if str((payload or {}).get("limit") or "").isdigit() else 3,
        dry_run=bool((payload or {}).get("dry_run", True)),
        force=bool((payload or {}).get("force", False)),
    )
    db.commit()
    return JSONResponse({"ok": True, "result": result})


@app.post("/api/research-canon/campaign-items/{item_id}/status")
async def api_research_canon_campaign_item_status(item_id: int, request: Request, db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    payload = await request.json()
    from thoth_terminal.research_canon_service import mark_campaign_item_status, serialize_campaign_item

    row = mark_campaign_item_status(
        db,
        item_id=item_id,
        status=str(payload.get("status") or "").strip(),
        last_error=str(payload.get("last_error") or "").strip() or None,
        output_report_id=int(payload.get("output_report_id")) if str(payload.get("output_report_id") or "").isdigit() else None,
        output_report_version_id=int(payload.get("output_report_version_id")) if str(payload.get("output_report_version_id") or "").isdigit() else None,
        quality=dict(payload.get("quality") or {}) if isinstance(payload.get("quality"), dict) else None,
    )
    db.commit()
    return JSONResponse({"ok": True, "item": serialize_campaign_item(row)})


@app.post("/api/research-canon/campaign-items/{item_id}/retry")
def api_research_canon_campaign_item_retry(item_id: int, db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    from thoth_terminal.research_canon_service import retry_campaign_item, serialize_campaign_item

    row = retry_campaign_item(db, item_id=item_id)
    db.commit()
    return JSONResponse({"ok": True, "item": serialize_campaign_item(row)})


@app.post("/api/research-canon/campaigns/{campaign_id}/unblock")
def api_research_canon_campaign_unblock(campaign_id: int, limit: int = 500, db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    from thoth_terminal.research_canon_service import unblock_campaign_items

    result = unblock_campaign_items(db, campaign_id=campaign_id, limit=limit)
    db.commit()
    return JSONResponse({"ok": True, "result": result})


@app.post("/api/research-canon/bottleneck-reports/start")
async def api_research_canon_bottleneck_report_start(request: Request, db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    payload = await request.json()
    from thoth_terminal.research_canon_service import bottleneck_startup_packet, execute_campaign_items, serialize_campaign_item, start_bottleneck_report
    from thoth_terminal.models import BottleneckModel, ResearchCampaign, ResearchCampaignItem

    result = start_bottleneck_report(
        db,
        model_key=str(payload.get("model_key") or "").strip(),
        title=str(payload.get("title") or "").strip(),
        scope_type=str(payload.get("scope_type") or "theme").strip() or "theme",
        scope_key=str(payload.get("scope_key") or "").strip() or None,
        constrained_input=str(payload.get("constrained_input") or "").strip() or None,
        output_unit=str(payload.get("output_unit") or "").strip() or None,
        supply_chain_id=int(payload.get("supply_chain_id")) if str(payload.get("supply_chain_id") or "").isdigit() else None,
        subject_id=int(payload.get("subject_id")) if str(payload.get("subject_id") or "").isdigit() else None,
        summary=str(payload.get("summary") or "").strip() or None,
        thesis=str(payload.get("thesis") or "").strip() or None,
        payload=dict(payload.get("payload") or {}) if isinstance(payload.get("payload"), dict) else None,
        links=[dict(item) for item in payload.get("links") if isinstance(item, dict)] if isinstance(payload.get("links"), list) else None,
        campaign_key=str(payload.get("campaign_key") or "").strip() or None,
        campaign_name=str(payload.get("campaign_name") or "").strip() or None,
        priority=int(payload.get("priority")) if str(payload.get("priority") or "").isdigit() else 8,
    )
    result["executed"] = False
    if bool(payload.get("execute", False)):
        campaign_id = int((result.get("campaign") or {}).get("id") or 0)
        execute_result = execute_campaign_items(
            db,
            campaign_id=campaign_id or None,
            campaign_key=str(payload.get("campaign_key") or "").strip() or None,
            limit=1,
            dry_run=False,
            force=bool(payload.get("force", False)),
        )
        result["executed"] = True
        result["execute_result"] = execute_result
        item_id = int((result.get("item") or {}).get("id") or 0)
        item = db.get(ResearchCampaignItem, item_id) if item_id else None
        campaign = db.get(ResearchCampaign, campaign_id) if campaign_id else None
        model_id = int((result.get("bottleneck_model") or {}).get("id") or 0)
        model = db.get(BottleneckModel, model_id) if model_id else None
        if model is not None:
            result["startup_packet"] = bottleneck_startup_packet(db, model=model, campaign=campaign, item=item)
            if item is not None:
                result["item"] = serialize_campaign_item(item)
    db.commit()
    return JSONResponse({"ok": True, "result": result})


@app.post("/api/research-canon/bottlenecks")
async def api_research_canon_bottleneck_upsert(request: Request, db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    payload = await request.json()
    from thoth_terminal.research_canon_service import upsert_bottleneck_object

    row = upsert_bottleneck_object(
        db,
        model_key=str(payload.get("model_key") or "").strip(),
        title=str(payload.get("title") or "").strip(),
        scope_type=str(payload.get("scope_type") or "theme").strip() or "theme",
        scope_key=str(payload.get("scope_key") or "").strip() or None,
        constrained_input=str(payload.get("constrained_input") or "").strip() or None,
        supply_chain_id=int(payload.get("supply_chain_id")) if str(payload.get("supply_chain_id") or "").isdigit() else None,
        subject_id=int(payload.get("subject_id")) if str(payload.get("subject_id") or "").isdigit() else None,
        summary=str(payload.get("summary") or "").strip() or None,
        thesis=str(payload.get("thesis") or "").strip() or None,
        payload=dict(payload.get("payload") or {}) if isinstance(payload.get("payload"), dict) else None,
        links=[dict(item) for item in payload.get("links") if isinstance(item, dict)] if isinstance(payload.get("links"), list) else None,
    )
    db.commit()
    return JSONResponse({"ok": True, "bottleneck": serialize_bottleneck_model(row, include_assessments=True)})


@app.post("/api/research-canon/bottlenecks/gaps/refresh")
def api_research_canon_bottleneck_gaps_refresh(db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    from thoth_terminal.research_canon_service import refresh_bottleneck_coverage_gaps

    result = refresh_bottleneck_coverage_gaps(db)
    db.commit()
    return JSONResponse({"ok": True, "result": result})


@app.post("/api/investment-catalysts")
async def api_investment_catalyst_upsert(request: Request, db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    row = upsert_investment_catalyst(db, dict(payload or {}), actor="codex")
    db.commit()
    return JSONResponse({"ok": True, "catalyst": serialize_catalyst(row)})


@app.post("/api/investment-memos")
async def api_investment_memo_upsert(request: Request, db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    row = upsert_investment_memo(db, dict(payload or {}), actor="codex")
    db.commit()
    return JSONResponse({"ok": True, "memo": serialize_memo(row)})


@app.get("/api/analyst-evidence")
def api_analyst_evidence_list(
    company_id: int | None = None,
    issuer_id: int | None = None,
    ticker: str | None = None,
    limit: int = 50,
    db: Session = Depends(get_session),
) -> JSONResponse:
    _guard_internal_terminal()
    company, issuer = resolve_company_or_issuer_from_payload(
        db,
        {"company_id": company_id, "issuer_id": issuer_id, "ticker": ticker},
    )
    rows = list_analyst_evidence(
        db,
        company_id=int(company.id) if company is not None else company_id,
        issuer_id=int(issuer.id) if issuer is not None else issuer_id,
        limit=max(1, min(int(limit or 50), 250)),
    )
    return JSONResponse({"ok": True, "analyst_evidence": rows})
