from __future__ import annotations

from thoth_terminal.web.app_sections._shared import bind_app_context
from thoth_terminal.agent.model_selection import select_openrouter_free_model


bind_app_context(globals())


def _instrument_payload_from_request(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    instrument_payload = payload.get("instrument") if isinstance(payload.get("instrument"), dict) else {}
    instrument_id = payload.get("instrument_id") or instrument_payload.get("instrument_id") or instrument_payload.get("id")
    if instrument_id:
        inst = db.get(ResearchInstrument, int(instrument_id))
        if inst is None:
            raise HTTPException(status_code=404, detail="Instrument not found.")
        return {
            "display_name": inst.display_name,
            "ticker": inst.ticker,
            "exchange": inst.exchange,
            "currency": inst.currency,
            "country": inst.country,
            "security_type": inst.security_type,
            "is_public": bool(inst.is_public),
            "issuer_id": inst.issuer_id,
            "company_id": inst.company_id,
            "ibkr_conid": inst.ibkr_conid,
            "yahoo_symbol": inst.yahoo_symbol,
            "primary_provider": inst.primary_provider,
            "metadata": inst.metadata_json or {},
        }

    candidate = payload.get("candidate") if isinstance(payload.get("candidate"), dict) else instrument_payload
    display_name = str(candidate.get("display_name") or candidate.get("name") or candidate.get("ticker") or "").strip()
    if not display_name:
        raise HTTPException(status_code=400, detail="Missing instrument display name.")
    return {
        "display_name": display_name,
        "ticker": candidate.get("ticker") or candidate.get("symbol") or candidate.get("yahoo_symbol"),
        "exchange": candidate.get("exchange"),
        "currency": candidate.get("currency"),
        "country": candidate.get("country"),
        "security_type": candidate.get("security_type") or candidate.get("secType") or ("STK" if candidate.get("is_public", True) else "PRIVATE"),
        "is_public": bool(candidate.get("is_public", True)),
        "issuer_id": candidate.get("issuer_id"),
        "company_id": candidate.get("company_id"),
        "ibkr_conid": candidate.get("ibkr_conid") or candidate.get("conid"),
        "yahoo_symbol": candidate.get("yahoo_symbol"),
        "primary_provider": candidate.get("primary_provider") or candidate.get("source") or "local",
        "metadata": candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else {},
    }


def _serialize_universe_section_delta(section: ResearchUniverseSection) -> dict[str, Any]:
    return {
        "id": int(section.id),
        "section_key": section.section_key,
        "name": section.name,
        "description": section.description,
        "sort_order": section.sort_order,
        "source_provider": section.source_provider,
        "source_external_id": section.source_external_id,
        "metadata": section.metadata_json or {},
        "rows": [],
    }


def _serialize_universe_row_delta(row: ResearchUniverseRow) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "display_label": row.display_label,
        "role": row.role,
        "exposure_type": row.exposure_type,
        "exposure_strength": row.exposure_strength,
        "conviction": row.conviction,
        "notes": row.notes,
        "sort_order": row.sort_order,
        "source_provider": row.source_provider,
        "source_external_id": row.source_external_id,
        "metadata": row.metadata_json or {},
        "instrument": serialize_instrument(row.instrument, quote={}) if row.instrument else None,
    }


def _payload_id_list(payload: dict[str, Any], key: str) -> list[int]:
    raw = payload.get(key)
    if not isinstance(raw, list):
        raise HTTPException(status_code=400, detail=f"Missing {key}.")
    ids: list[int] = []
    for value in raw:
        try:
            ids.append(int(value))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid {key}.") from exc
    return ids


@app.get("/universes", response_class=HTMLResponse)
def universes_view(
    request: Request,
    slug: str | None = None,
    db: Session = Depends(get_session),
) -> HTMLResponse:
    _guard_internal_terminal()
    universes = _UNIVERSE_LIST_CACHE.get_or_set(f"universes:{_cache_scope()}:list", lambda: list_universes(db), ttl_s=45.0)
    selected_slug = slug or (universes[0]["slug"] if universes else None)
    selected = None
    if selected_slug:
        detail_key = f"universes:{_cache_scope()}:detail:{selected_slug}:quotes0"
        selected = _UNIVERSE_DETAIL_CACHE.get(detail_key)
        if selected is None:
            row = db.query(ResearchUniverse).filter_by(slug=str(selected_slug)).first()
            if row is not None:
                selected = _UNIVERSE_DETAIL_CACHE.set(
                    detail_key,
                    serialize_universe(db, row, include_rows=True, include_quotes=False),
                    ttl_s=45.0,
                )
    ibkr_status = ibkr_service().status(live=False)
    return _template_response(
        "universes.html",
        {
            "request": request,
            "title": "Universes",
            "body_class": "universes-page",
            "wrap_class": "wide",
            "universes": universes,
            "selected": selected,
            "ibkr_status": ibkr_status,
        },
    )


@app.get("/api/universes")
def api_universes_list(db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    return JSONResponse({"items": _UNIVERSE_LIST_CACHE.get_or_set(f"universes:{_cache_scope()}:list", lambda: list_universes(db), ttl_s=45.0)})


@app.get("/api/agents/gap-hunter/universe-index")
def api_gap_hunter_universe_index(
    keyword: str | None = None,
    universe_id: int | None = None,
    section_id: int | None = None,
    limit: int = 200,
    db: Session = Depends(get_session),
) -> JSONResponse:
    _guard_internal_terminal()
    payload = universe_coverage_index(
        db,
        keyword=keyword,
        universe_id=universe_id,
        section_id=section_id,
        limit=limit,
    )
    return JSONResponse({"ok": True, "index": payload})


def _gap_cloud_catalog_model(catalog: dict[str, Any], *, provider: str, model: str) -> dict[str, Any] | None:
    for row in catalog.get("rows") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("provider") or "") == provider and str(row.get("model") or "") == model:
            return row
    return None


def _cloud_task_model_request(body: dict[str, Any], *, task_kind: str) -> tuple[str, str, dict[str, Any] | None]:
    provider = str(body.get("provider") or getattr(settings, "cloud_research_provider", "openrouter") or "openrouter").strip().lower()
    if provider not in {"openrouter", "deepseek", "nous", "meta"}:
        raise HTTPException(status_code=400, detail="provider must be openrouter, deepseek, nous, or meta")
    default_model = {
        "openrouter": getattr(settings, "openrouter_model", "openrouter/free"),
        "deepseek": getattr(settings, "deepseek_model", "deepseek-v4-flash"),
        "nous": getattr(settings, "nous_model", "Hermes-4.3-36B"),
        "meta": getattr(settings, "meta_model", "muse-spark-1.3-contributor"),
    }.get(provider)
    model = str(body.get("model") or default_model or "").strip()
    if not model:
        raise HTTPException(status_code=400, detail="Model is required.")
    catalog = _cloud_model_catalog_payload(cached=True)
    catalog_row = _gap_cloud_catalog_model(catalog, provider=provider, model=model)
    if provider == "openrouter" and model == "openrouter/free":
        selected = select_openrouter_free_model(catalog, task_kind=task_kind)
        if selected is not None:
            model = str(selected.get("model") or model).strip()
            catalog_row = selected
    if catalog_row is not None and not bool(catalog_row.get("text_output", True)):
        raise HTTPException(status_code=400, detail="Gap Hunter requires a text-output model.")
    if catalog_row is not None:
        output_modalities = {
            str(item).strip().lower()
            for item in catalog_row.get("output_modalities", [])
            if str(item).strip()
        }
        if output_modalities and ("text" not in output_modalities or output_modalities - {"text"}):
            raise HTTPException(status_code=400, detail="Cloud agents require a text-output model.")
        task_policy = catalog_row.get("task_policy") if isinstance(catalog_row.get("task_policy"), dict) else {}
        task_eval = task_policy.get(task_kind) if isinstance(task_policy.get(task_kind), dict) else {}
        failures = task_eval.get("failures") if isinstance(task_eval.get("failures"), list) else []
        if failures:
            raise HTTPException(
                status_code=400,
                detail=f"Model does not meet {task_kind} policy: {'; '.join(str(item) for item in failures[:4])}",
            )
    free_only = bool(body.get("free_only", True))
    if provider == "openrouter" and free_only:
        if catalog_row is not None and not bool(catalog_row.get("free")):
            raise HTTPException(status_code=400, detail="OpenRouter free-only mode rejected a paid model.")
        if catalog_row is None and model != "openrouter/free" and not model.endswith(":free"):
            raise HTTPException(status_code=400, detail="OpenRouter free-only mode requires a free model.")
    return provider, model, catalog_row


def _gap_hunter_model_request(body: dict[str, Any]) -> tuple[str, str, dict[str, Any] | None]:
    return _cloud_task_model_request(body, task_kind="gap_hunter")


@app.get("/api/agents/tasks/catalog")
def api_cloud_task_catalog() -> JSONResponse:
    _guard_internal_terminal()
    return JSONResponse({"ok": True, "items": list_task_capabilities()})


@app.get("/api/agents/tasks/catalog/{task_id}")
def api_cloud_task_catalog_get(task_id: str) -> JSONResponse:
    _guard_internal_terminal()
    item = get_task_capability(task_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Cloud task capability not found.")
    return JSONResponse({"ok": True, "item": item})


@app.post("/api/agents/orchestrator/runs")
def api_cloud_orchestrator_goal_start(payload: dict[str, Any] | None = Body(default=None), db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    body = dict(payload or {})
    provider, model, _catalog_row = _cloud_task_model_request({**body, "free_only": body.get("free_only", True)}, task_kind="cloud_orchestrator")
    skill = serialize_cloud_overlord_skill(ensure_cloud_overlord_skill(db))
    try:
        run = start_cloud_orchestrator_run(
            db,
            objective=body.get("objective") or body.get("question") or "",
            provider=provider,
            model=model,
            lookback_hours=int(body.get("lookback_hours") or 168),
            token_policy=body.get("token_policy") if isinstance(body.get("token_policy"), dict) else None,
            skill_prompt=skill["system_prompt"],
            strong_provider=body.get("strong_provider") or provider,
            strong_model=body.get("strong_model") or model,
            flash_provider=body.get("flash_provider") or provider,
            flash_model=body.get("flash_model") or model,
            permission_profile=body.get("permission_profile") if isinstance(body.get("permission_profile"), dict) else None,
            subagent_prompts=skill["subagent_prompts"],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return JSONResponse({"ok": True, "run": run})


@app.get("/api/agents/goals")
def api_cloud_goals_list(db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    return JSONResponse({"ok": True, "goals": list_cloud_goals(db), "skill": serialize_cloud_overlord_skill(ensure_cloud_overlord_skill(db))})


@app.post("/api/agents/goals/command")
def api_cloud_goal_command(payload: dict[str, Any] | None = Body(default=None), db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    body = dict(payload or {})
    command = str(body.get("command") or "").strip()
    if not command:
        raise HTTPException(status_code=400, detail="command is required.")
    provider, model, _catalog_row = _cloud_task_model_request({**body, "free_only": body.get("free_only", False)}, task_kind="cloud_orchestrator")
    flash_provider = str(body.get("flash_provider") or provider).strip().lower()
    flash_model = str(body.get("flash_model") or model).strip()
    if body.get("flash_model"):
        flash_provider, flash_model, _flash_row = _cloud_task_model_request(
            {
                "provider": flash_provider,
                "model": flash_model,
                "free_only": body.get("flash_free_only", False),
            },
            task_kind="source_pack_builder",
        )
    try:
        result = execute_cloud_goal_command(
            db,
            command=command,
            provider=provider,
            model=model,
            flash_provider=flash_provider,
            flash_model=flash_model,
            permission_profile=body.get("permission_profile") if isinstance(body.get("permission_profile"), dict) else None,
            skill_slugs=[str(value) for value in body.get("skill_slugs") or []] if isinstance(body.get("skill_slugs"), list) else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return JSONResponse({"ok": True, **result})


@app.get("/api/agents/cloud/skills")
def api_cloud_skills_list(db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    ensure_cloud_overlord_skill(db)
    return JSONResponse({"ok": True, **cloud_skill_runtime_payload(db)})


@app.get("/api/agents/orchestrator/skill")
def api_cloud_overlord_skill_get(db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    return JSONResponse({"ok": True, "skill": serialize_cloud_overlord_skill(ensure_cloud_overlord_skill(db))})


@app.put("/api/agents/orchestrator/skill")
def api_cloud_overlord_skill_update(payload: dict[str, Any] | None = Body(default=None), db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    body = dict(payload or {})
    try:
        skill = update_cloud_overlord_skill(
            db,
            system_prompt=body.get("system_prompt") if "system_prompt" in body else None,
            subagent_prompts=body.get("subagent_prompts") if isinstance(body.get("subagent_prompts"), dict) else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return JSONResponse({"ok": True, "skill": skill})


@app.get("/api/agents/orchestrator/runs/{run_id}")
def api_cloud_orchestrator_goal_get(run_id: int, db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    row = get_cloud_goal_run(db, int(run_id))
    if row is None or row.task_kind != "cloud_orchestrator":
        raise HTTPException(status_code=404, detail="Cloud orchestrator run not found.")
    events = list_cloud_goal_events(db, int(run_id), limit=500)
    return JSONResponse({"ok": True, "run": serialize_cloud_goal_run(row, events=events)})


@app.post("/api/agents/orchestrator/runs/{run_id}/step")
def api_cloud_orchestrator_goal_step(run_id: int, payload: dict[str, Any] | None = Body(default=None), db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    run = step_cloud_orchestrator_run(db, int(run_id))
    db.commit()
    return JSONResponse({"ok": True, "run": run})


@app.post("/api/agents/orchestrator/runs/{run_id}/continue")
def api_cloud_orchestrator_goal_continue(run_id: int, payload: dict[str, Any] | None = Body(default=None), db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    body = dict(payload or {})
    run = continue_cloud_orchestrator_run(
        db,
        int(run_id),
        max_steps=int(body.get("max_steps") or 1),
    )
    db.commit()
    return JSONResponse({"ok": True, "run": run})


@app.post("/api/agents/orchestrator/runs/{run_id}/dispatch")
def api_cloud_orchestrator_goal_dispatch(run_id: int, payload: dict[str, Any] | None = Body(default=None), db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    body = dict(payload or {})
    row = get_cloud_goal_run(db, int(run_id))
    profile = dict((row.stats_json or {}).get("permission_profile") or {}) if row is not None else {}
    run = dispatch_next_cloud_orchestrator_step(
        db,
        int(run_id),
        execute_child=bool(body.get("execute_child", False)),
        child_max_steps=int(body.get("child_max_steps") or 1),
        compact_if_needed=bool(body.get("compact_if_needed", True)),
        allow_durable_writes=bool(body.get("allow_durable_writes", profile.get("allow_durable_writes", False))),
        operator_approved=bool(body.get("operator_approved", profile.get("operator_approved", False))),
    )
    db.commit()
    return JSONResponse({"ok": True, "run": run})


@app.post("/api/agents/orchestrator/runs/{run_id}/dispatch/continue")
def api_cloud_orchestrator_goal_dispatch_continue(run_id: int, payload: dict[str, Any] | None = Body(default=None), db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    body = dict(payload or {})
    run = continue_cloud_orchestrator_handoffs(
        db,
        int(run_id),
        max_handoffs=int(body.get("max_handoffs") or 1),
        execute_child=bool(body.get("execute_child", False)),
        child_max_steps=int(body.get("child_max_steps") or 1),
        compact_if_needed=bool(body.get("compact_if_needed", True)),
        allow_durable_writes=bool(body.get("allow_durable_writes", False)),
        operator_approved=bool(body.get("operator_approved", False)),
    )
    db.commit()
    return JSONResponse({"ok": True, "run": run})


@app.post("/api/agents/runs/{run_id}/compact")
def api_cloud_goal_compact(run_id: int, payload: dict[str, Any] | None = Body(default=None), db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    body = dict(payload or {})
    result = compact_cloud_goal_context(
        db,
        int(run_id),
        provider=body.get("provider"),
        model=body.get("model"),
        reason=body.get("reason"),
        force=bool(body.get("force", False)),
        max_output_tokens=int(body.get("max_output_tokens") or 3500),
    )
    db.commit()
    return JSONResponse({"ok": True, **result})


@app.post("/api/agents/gap-hunter/run")
def api_gap_hunter_run(payload: dict[str, Any] | None = Body(default=None), db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    body = dict(payload or {})
    provider, model, catalog_row = _gap_hunter_model_request(body)
    try:
        result = run_gap_hunter_cloud_task(
            db,
            provider=provider,
            model=model,
            question=body.get("question"),
            keyword=body.get("keyword"),
            universe_id=body.get("universe_id"),
            section_id=body.get("section_id"),
            limit=int(body.get("limit") or 200),
            persist_candidates=bool(body.get("persist_candidates", False)),
            operator_approved=bool(body.get("operator_approved", False)),
            catalog_row=catalog_row,
        )
        if result.get("persisted_candidates"):
            db.commit()
        return JSONResponse({"ok": True, **result})
    except AgentLlmError as exc:
        return JSONResponse({"ok": False, "provider": provider, "model": model, "error": str(exc)}, status_code=200)


@app.post("/api/agents/gap-hunter/context-request")
def api_gap_hunter_context_request(payload: dict[str, Any] | None = Body(default=None), db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    body = dict(payload or {})
    request_payload = body.get("request") if isinstance(body.get("request"), dict) else body
    index = resolve_gap_hunter_context_request(db, request_payload, limit=int(body.get("limit") or 200))
    return JSONResponse({"ok": True, "index": index})


@app.post("/api/agents/gap-hunter/runs")
def api_gap_hunter_goal_start(payload: dict[str, Any] | None = Body(default=None), db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    body = dict(payload or {})
    provider, model, _catalog_row = _gap_hunter_model_request({**body, "free_only": body.get("free_only", False)})
    run = start_gap_hunter_goal_run(
        db,
        objective=body.get("objective") or body.get("question"),
        provider=provider,
        model=model,
        token_policy=body.get("token_policy") if isinstance(body.get("token_policy"), dict) else None,
        keyword=body.get("keyword"),
        section_id=body.get("section_id"),
        universe_id=body.get("universe_id"),
        limit=int(body.get("limit") or 200),
    )
    db.commit()
    return JSONResponse({"ok": True, "run": run})


@app.get("/api/agents/gap-hunter/runs/{run_id}")
def api_gap_hunter_goal_get(run_id: int, db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    row = get_cloud_goal_run(db, int(run_id))
    if row is None:
        raise HTTPException(status_code=404, detail="Gap Hunter run not found.")
    events = list_cloud_goal_events(db, int(run_id), limit=400)
    return JSONResponse({"ok": True, "run": serialize_cloud_goal_run(row, events=events)})


@app.post("/api/agents/gap-hunter/runs/{run_id}/step")
def api_gap_hunter_goal_step(run_id: int, payload: dict[str, Any] | None = Body(default=None), db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    body = dict(payload or {})
    run = step_gap_hunter_goal_run(
        db,
        int(run_id),
        persist_candidates=bool(body.get("persist_candidates", False)),
        operator_approved=bool(body.get("operator_approved", False)),
        max_context_requests=int(body.get("max_context_requests") or 3),
    )
    db.commit()
    return JSONResponse({"ok": True, "run": run})


@app.post("/api/agents/gap-hunter/runs/{run_id}/continue")
def api_gap_hunter_goal_continue(run_id: int, payload: dict[str, Any] | None = Body(default=None), db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    body = dict(payload or {})
    run = continue_gap_hunter_goal_run(
        db,
        int(run_id),
        max_steps=int(body.get("max_steps") or 5),
        persist_candidates=bool(body.get("persist_candidates", False)),
        operator_approved=bool(body.get("operator_approved", False)),
    )
    db.commit()
    return JSONResponse({"ok": True, "run": run})


@app.post("/api/agents/red-team/runs")
def api_red_team_goal_start(payload: dict[str, Any] | None = Body(default=None), db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    body = dict(payload or {})
    provider, model, _catalog_row = _cloud_task_model_request({**body, "free_only": body.get("free_only", True)}, task_kind="red_team")
    run = start_red_team_goal_run(
        db,
        objective=body.get("objective") or body.get("question"),
        provider=provider,
        model=model,
        report_id=int(body["report_id"]) if body.get("report_id") else None,
        subject=body.get("subject"),
        token_policy=body.get("token_policy") if isinstance(body.get("token_policy"), dict) else None,
    )
    db.commit()
    return JSONResponse({"ok": True, "run": run})


@app.get("/api/agents/red-team/runs/{run_id}")
def api_red_team_goal_get(run_id: int, db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    row = get_cloud_goal_run(db, int(run_id))
    if row is None or row.task_kind != "red_team":
        raise HTTPException(status_code=404, detail="Red Team run not found.")
    events = list_cloud_goal_events(db, int(run_id), limit=500)
    return JSONResponse({"ok": True, "run": serialize_cloud_goal_run(row, events=events)})


@app.post("/api/agents/red-team/runs/{run_id}/step")
def api_red_team_goal_step(run_id: int, payload: dict[str, Any] | None = Body(default=None), db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    body = dict(payload or {})
    run = step_red_team_goal_run(
        db,
        int(run_id),
        persist_candidates=bool(body.get("persist_candidates", False)),
        operator_approved=bool(body.get("operator_approved", False)),
        max_context_requests=int(body.get("max_context_requests") or 2),
    )
    db.commit()
    return JSONResponse({"ok": True, "run": run})


@app.post("/api/agents/red-team/runs/{run_id}/continue")
def api_red_team_goal_continue(run_id: int, payload: dict[str, Any] | None = Body(default=None), db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    body = dict(payload or {})
    run = continue_red_team_goal_run(
        db,
        int(run_id),
        max_steps=int(body.get("max_steps") or 3),
        persist_candidates=bool(body.get("persist_candidates", False)),
        operator_approved=bool(body.get("operator_approved", False)),
    )
    db.commit()
    return JSONResponse({"ok": True, "run": run})


@app.post("/api/agents/report-body/runs")
def api_report_body_builder_goal_start(payload: dict[str, Any] | None = Body(default=None), db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    body = dict(payload or {})
    provider, model, _catalog_row = _cloud_task_model_request({**body, "free_only": body.get("free_only", False)}, task_kind="report_body_builder")
    run = start_report_body_builder_goal_run(
        db,
        objective=body.get("objective") or body.get("question"),
        provider=provider,
        model=model,
        report_id=int(body["report_id"]) if body.get("report_id") else None,
        subject=body.get("subject"),
        token_policy=body.get("token_policy") if isinstance(body.get("token_policy"), dict) else None,
    )
    db.commit()
    return JSONResponse({"ok": True, "run": run})


@app.get("/api/agents/report-body/runs/{run_id}")
def api_report_body_builder_goal_get(run_id: int, db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    row = get_cloud_goal_run(db, int(run_id))
    if row is None or row.task_kind != "report_body_builder":
        raise HTTPException(status_code=404, detail="Report Body Builder run not found.")
    events = list_cloud_goal_events(db, int(run_id), limit=500)
    return JSONResponse({"ok": True, "run": serialize_cloud_goal_run(row, events=events)})


@app.post("/api/agents/report-body/runs/{run_id}/step")
def api_report_body_builder_goal_step(run_id: int, payload: dict[str, Any] | None = Body(default=None), db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    body = dict(payload or {})
    run = step_report_body_builder_goal_run(
        db,
        int(run_id),
        persist_addendum=bool(body.get("persist_addendum", False)),
        operator_approved=bool(body.get("operator_approved", False)),
        max_context_requests=int(body.get("max_context_requests") or 3),
    )
    db.commit()
    return JSONResponse({"ok": True, "run": run})


@app.post("/api/agents/report-body/runs/{run_id}/continue")
def api_report_body_builder_goal_continue(run_id: int, payload: dict[str, Any] | None = Body(default=None), db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    body = dict(payload or {})
    run = continue_report_body_builder_goal_run(
        db,
        int(run_id),
        max_steps=int(body.get("max_steps") or 3),
        persist_addendum=bool(body.get("persist_addendum", False)),
        operator_approved=bool(body.get("operator_approved", False)),
    )
    db.commit()
    return JSONResponse({"ok": True, "run": run})


@app.post("/api/agents/source-pack/build")
def api_source_pack_build(payload: dict[str, Any] | None = Body(default=None), db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    body = dict(payload or {})
    pack = build_source_pack(
        db,
        query=body.get("query"),
        report_id=int(body["report_id"]) if body.get("report_id") else None,
        subject=body.get("subject"),
        limit=int(body.get("limit") or 18),
        include_object_search=bool(body.get("include_object_search", True)),
    )
    return JSONResponse({"ok": True, "pack": pack})


@app.post("/api/agents/source-pack/runs")
def api_source_pack_goal_start(payload: dict[str, Any] | None = Body(default=None), db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    body = dict(payload or {})
    provider, model, _catalog_row = _cloud_task_model_request({**body, "free_only": body.get("free_only", False)}, task_kind="source_pack_builder")
    run = start_source_pack_goal_run(
        db,
        objective=body.get("objective") or body.get("question"),
        provider=provider,
        model=model,
        query=body.get("query"),
        report_id=int(body["report_id"]) if body.get("report_id") else None,
        subject=body.get("subject"),
        limit=int(body.get("limit") or 18),
        token_policy=body.get("token_policy") if isinstance(body.get("token_policy"), dict) else None,
    )
    db.commit()
    return JSONResponse({"ok": True, "run": run})


@app.get("/api/agents/source-pack/runs/{run_id}")
def api_source_pack_goal_get(run_id: int, db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    row = get_cloud_goal_run(db, int(run_id))
    if row is None or row.task_kind != "source_pack_builder":
        raise HTTPException(status_code=404, detail="Source Pack Builder run not found.")
    events = list_cloud_goal_events(db, int(run_id), limit=500)
    return JSONResponse({"ok": True, "run": serialize_cloud_goal_run(row, events=events)})


@app.post("/api/agents/source-pack/runs/{run_id}/step")
def api_source_pack_goal_step(run_id: int, payload: dict[str, Any] | None = Body(default=None), db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    body = dict(payload or {})
    run = step_source_pack_goal_run(
        db,
        int(run_id),
        max_context_requests=int(body.get("max_context_requests") or 3),
    )
    db.commit()
    return JSONResponse({"ok": True, "run": run})


@app.post("/api/agents/source-pack/runs/{run_id}/continue")
def api_source_pack_goal_continue(run_id: int, payload: dict[str, Any] | None = Body(default=None), db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    body = dict(payload or {})
    run = continue_source_pack_goal_run(
        db,
        int(run_id),
        max_steps=int(body.get("max_steps") or 3),
    )
    db.commit()
    return JSONResponse({"ok": True, "run": run})


@app.post("/api/agents/bottleneck/runs")
def api_bottleneck_constraint_goal_start(payload: dict[str, Any] | None = Body(default=None), db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    body = dict(payload or {})
    provider, model, _catalog_row = _cloud_task_model_request({**body, "free_only": body.get("free_only", False)}, task_kind="bottleneck_constraint")
    run = start_bottleneck_constraint_goal_run(
        db,
        objective=body.get("objective") or body.get("question"),
        provider=provider,
        model=model,
        query=body.get("query"),
        report_id=int(body["report_id"]) if body.get("report_id") else None,
        subject=body.get("subject"),
        token_policy=body.get("token_policy") if isinstance(body.get("token_policy"), dict) else None,
    )
    db.commit()
    return JSONResponse({"ok": True, "run": run})


@app.get("/api/agents/bottleneck/runs/{run_id}")
def api_bottleneck_constraint_goal_get(run_id: int, db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    row = get_cloud_goal_run(db, int(run_id))
    if row is None or row.task_kind != "bottleneck_constraint":
        raise HTTPException(status_code=404, detail="Bottleneck / constraint run not found.")
    events = list_cloud_goal_events(db, int(run_id), limit=500)
    return JSONResponse({"ok": True, "run": serialize_cloud_goal_run(row, events=events)})


@app.post("/api/agents/bottleneck/runs/{run_id}/step")
def api_bottleneck_constraint_goal_step(run_id: int, payload: dict[str, Any] | None = Body(default=None), db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    run = step_bottleneck_constraint_goal_run(db, int(run_id))
    db.commit()
    return JSONResponse({"ok": True, "run": run})


@app.post("/api/agents/bottleneck/runs/{run_id}/continue")
def api_bottleneck_constraint_goal_continue(run_id: int, payload: dict[str, Any] | None = Body(default=None), db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    body = dict(payload or {})
    run = continue_bottleneck_constraint_goal_run(
        db,
        int(run_id),
        max_steps=int(body.get("max_steps") or 1),
    )
    db.commit()
    return JSONResponse({"ok": True, "run": run})


@app.post("/api/universes")
def api_universe_create(payload: dict[str, Any] = Body(default={}), db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Missing universe name.")
    row = ensure_universe(
        db,
        name=name,
        slug=payload.get("slug"),
        description=payload.get("description"),
        metadata={"created_via": "web"},
    )
    db.commit()
    _invalidate_terminal_caches("universes")
    return JSONResponse({"ok": True, "universe": serialize_universe(db, row, include_rows=True, include_quotes=False)})


@app.patch("/api/universes/{universe_id}")
def api_universe_update(
    universe_id: int,
    payload: dict[str, Any] = Body(default={}),
    db: Session = Depends(get_session),
) -> JSONResponse:
    _guard_internal_terminal()
    try:
        row = update_universe(
            db,
            int(universe_id),
            name=payload.get("name") if "name" in payload else None,
            description=payload.get("description") if "description" in payload else None,
            status=payload.get("status") if "status" in payload else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404 if "not found" in str(exc).lower() else 400, detail=str(exc)) from exc
    db.commit()
    _invalidate_terminal_caches("universes")
    return JSONResponse({"ok": True, "universe": serialize_universe(db, row, include_rows=True, include_quotes=False)})


@app.delete("/api/universes/{universe_id}")
def api_universe_delete(universe_id: int, db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    try:
        delete_universe(db, int(universe_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    db.commit()
    _invalidate_terminal_caches("universes")
    return JSONResponse({"ok": True})


@app.post("/api/universes/reorder")
def api_universes_reorder(payload: dict[str, Any] = Body(default={}), db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    try:
        rows = reorder_universes(db, _payload_id_list(payload, "universe_ids"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    _invalidate_terminal_caches("universes")
    return JSONResponse(
        {
            "ok": True,
            "items": [
                {
                    "id": int(row.id),
                    "slug": row.slug,
                    "name": row.name,
                    "sort_order": row.sort_order,
                }
                for row in rows
            ],
        }
    )


def _ibkr_universe_search_error(exc: Exception) -> str:
    text = str(exc or "").strip()
    lowered = text.lower()
    if (
        isinstance(exc, IbkrClientError)
        or "localhost:5000" in lowered
        or "secdef/search" in lowered
        or "trsrv/stocks" in lowered
        or "client portal" in lowered
    ):
        return "IBKR Client Portal search is degraded; using local/Yahoo candidates."
    if not text:
        return "IBKR search failed; using local/Yahoo candidates."
    return f"{type(exc).__name__}: {text[:160]}"


def _universe_ibkr_ticker_lookup_query(query: str) -> bool:
    text = str(query or "").strip()
    if not text or len(text) > 24 or any(ch.isspace() for ch in text):
        return False
    if not all(ch.isalnum() or ch in ".-/" for ch in text):
        return False
    if any(ch.isdigit() or ch in ".-/" for ch in text):
        return True
    return len(text) <= 5


@app.get("/api/universes/instruments/search")
def api_universe_instrument_search(
    q: str = "",
    limit: int = 12,
    remote: bool = True,
    db: Session = Depends(get_session),
) -> JSONResponse:
    _guard_internal_terminal()
    query = str(q or "").strip()
    lim = max(1, min(int(limit or 12), 30))
    service = ibkr_service()
    ibkr_active = bool(getattr(service.config, "active", False))
    ibkr_mode = "mock" if bool(getattr(service.config, "mock_enabled", False)) and not bool(getattr(service.config, "enabled", False)) else "client_portal"
    ibkr_backoff = bool(getattr(getattr(service, "client", None), "auth_backoff_active", False))
    cache_key = (
        f"universes:{_cache_scope()}:search:{query.lower()}:{lim}:{1 if remote else 0}:"
        f"ibkr{1 if ibkr_active else 0}:{ibkr_mode}:backoff{1 if ibkr_backoff else 0}"
    )
    cached = _UNIVERSE_SEARCH_CACHE.get(cache_key)
    if cached is not None:
        return JSONResponse({**cached, "cached": True})
    ibkr_results: list[dict[str, Any]] = []
    allow_remote = bool(remote and len(query) >= 3)
    ibkr_provider: dict[str, Any] = {
        "enabled": ibkr_active,
        "mode": "disabled" if not ibkr_active else ibkr_mode,
        "queried": False,
        "lookup_mode": "disabled" if not ibkr_active else "ticker_trsrv",
        "skipped_reason": None,
        "connected": None,
        "auth_required": False,
        "count": 0,
        "error": None,
    }
    allow_ibkr_lookup = bool(allow_remote and ibkr_active and _universe_ibkr_ticker_lookup_query(query))
    if allow_remote and ibkr_active and not allow_ibkr_lookup:
        ibkr_provider["skipped_reason"] = "company-name search uses local/Yahoo candidates; IBKR Client Portal lookup is ticker-only here."
    if allow_ibkr_lookup:
        ibkr_provider["queried"] = True
        if ibkr_backoff:
            try:
                status = service.status(live=True)
            except Exception as exc:
                status = {
                    "connected": False,
                    "auth_required": True,
                    "error": _ibkr_universe_search_error(exc),
                }
            ibkr_provider["connected"] = bool(status.get("connected"))
            ibkr_provider["auth_required"] = bool(status.get("auth_required") or not status.get("connected"))
            if not status.get("connected"):
                ibkr_provider["error"] = (
                    status.get("message")
                    or status.get("error")
                    or "IBKR authentication is required."
                )
        try:
            if not ibkr_provider.get("error"):
                ibkr_results = service.security_search(query, name=False, allow_secdef=False)
                ibkr_provider["count"] = len(ibkr_results)
                if ibkr_provider.get("connected") is None:
                    ibkr_provider["connected"] = True
        except IbkrAuthError as exc:
            try:
                status = service.status(live=True)
            except Exception:
                status = {}
            if status.get("connected"):
                try:
                    ibkr_results = service.security_search(query, name=False, allow_secdef=False)
                    ibkr_provider["count"] = len(ibkr_results)
                    ibkr_provider["auth_required"] = False
                    ibkr_provider["connected"] = True
                    ibkr_provider["error"] = None
                except IbkrAuthError as retry_exc:
                    ibkr_provider["auth_required"] = True
                    ibkr_provider["connected"] = False
                    ibkr_provider["error"] = str(retry_exc)
                    ibkr_results = []
                except Exception as retry_exc:
                    ibkr_provider["connected"] = False
                    ibkr_provider["error"] = _ibkr_universe_search_error(retry_exc)
                    ibkr_results = []
            else:
                ibkr_provider["auth_required"] = True
                ibkr_provider["connected"] = False
                ibkr_provider["error"] = str(exc)
                ibkr_results = []
        except Exception as exc:
            ibkr_provider["connected"] = False
            ibkr_provider["error"] = _ibkr_universe_search_error(exc)
            ibkr_results = []
    items = search_instruments(
        db,
        query,
        include_yahoo=allow_remote,
        ibkr_results=ibkr_results,
        limit=lim,
    )
    payload = {"items": items, "providers": {"ibkr": ibkr_provider}}
    if allow_remote and ibkr_active and (ibkr_provider.get("auth_required") or ibkr_provider.get("error")):
        ttl_s = 5.0
    elif allow_remote and ibkr_active:
        ttl_s = 30.0
    else:
        ttl_s = 90.0 if allow_remote else 45.0
    _UNIVERSE_SEARCH_CACHE.set(cache_key, payload, ttl_s=ttl_s)
    return JSONResponse({**payload, "cached": False})


@app.get("/api/universes/labels")
def api_universe_label_suggestions(kind: str = "exposure_type", q: str = "", db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    kind_v = "role" if str(kind or "").strip().lower() == "role" else "exposure_type"
    query = str(q or "").strip().lower()
    cache_key = f"universes:{_cache_scope()}:labels:{kind_v}:{query}"
    items = _UNIVERSE_LABEL_CACHE.get_or_set(
        cache_key,
        lambda: label_suggestions(db, kind=kind_v, query=q),
        ttl_s=120.0,
    )
    return JSONResponse({"items": items})


@app.get("/api/universes/instruments/{instrument_id}")
def api_universe_instrument_detail(instrument_id: int, db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    payload = instrument_detail(db, instrument_id, live_quotes=False)
    if payload is None:
        raise HTTPException(status_code=404, detail="Instrument not found.")
    try:
        if db.new or db.dirty or db.deleted:
            db.commit()
    except Exception:
        db.rollback()
    return JSONResponse(payload)


@app.post("/api/universes/import/ibkr-watchlist")
def api_universe_import_ibkr_watchlist(
    payload: dict[str, Any] = Body(default={}),
    db: Session = Depends(get_session),
) -> JSONResponse:
    _guard_internal_terminal()
    service = ibkr_service()
    if not service.config.active:
        raise HTTPException(status_code=503, detail="IBKR integration is disabled.")
    watchlist_id = str(payload.get("watchlist_id") or payload.get("id") or "").strip()
    if not watchlist_id:
        raise HTTPException(status_code=400, detail="Missing IBKR watchlist id.")
    imported = import_ibkr_watchlist_payload(
        db,
        watchlist_payload=service.watchlist(watchlist_id),
        universe_name=str(payload.get("universe_name") or "").strip() or None,
        contract_resolver=service.contract_info,
    )
    db.commit()
    _invalidate_terminal_caches("universes")
    return JSONResponse(imported)


@app.post("/api/universes/provider-links/reconcile")
def api_universe_provider_links_reconcile(
    payload: dict[str, Any] = Body(default={}),
    db: Session = Depends(get_session),
) -> JSONResponse:
    _guard_internal_terminal()
    service = ibkr_service()
    if not service.config.active:
        raise HTTPException(status_code=503, detail="IBKR integration is disabled.")
    try:
        status = service.status(live=True)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=_ibkr_universe_search_error(exc)) from exc
    if not bool(status.get("connected")):
        detail = status.get("message") or status.get("error") or "IBKR authentication is required."
        raise HTTPException(status_code=503, detail=str(detail))
    universe_id = payload.get("universe_id")
    summary = reconcile_universe_provider_links(
        db,
        security_search=lambda symbol: service.security_search(symbol, name=False, allow_secdef=False),
        universe_id=int(universe_id) if universe_id else None,
        limit=int(payload.get("limit") or 25),
        dry_run=bool(payload.get("dry_run", False)),
    )
    universe = db.get(ResearchUniverse, int(universe_id)) if universe_id else None
    if not summary.get("dry_run"):
        db.commit()
        if summary.get("linked") or summary.get("merged"):
            _invalidate_terminal_caches("universes")
    return JSONResponse(
        {
            **summary,
            "universe": serialize_universe(db, universe, include_rows=True, include_quotes=False) if universe else None,
        }
    )


@app.post("/api/universes/provider-links/normalize-sparse")
def api_universe_provider_links_normalize_sparse(
    payload: dict[str, Any] = Body(default={}),
    db: Session = Depends(get_session),
) -> JSONResponse:
    _guard_internal_terminal()
    service = ibkr_service()
    if not service.config.active:
        raise HTTPException(status_code=503, detail="IBKR integration is disabled.")
    try:
        status = service.status(live=True)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=_ibkr_universe_search_error(exc)) from exc
    if not bool(status.get("connected")):
        detail = status.get("message") or status.get("error") or "IBKR authentication is required."
        raise HTTPException(status_code=503, detail=str(detail))
    try:
        limit = int(payload.get("limit") or 25)
    except (TypeError, ValueError):
        limit = 25
    limit = max(1, min(limit, 100))
    changed = normalize_sparse_ibkr_contracts(db, contract_resolver=service.contract_info, limit=limit)
    universe_id = payload.get("universe_id")
    universe = db.get(ResearchUniverse, int(universe_id)) if universe_id else None
    db.commit()
    if changed:
        _invalidate_terminal_caches("universes")
    return JSONResponse(
        {
            "ok": True,
            "normalized": changed,
            "limit": limit,
            "universe": serialize_universe(db, universe, include_rows=True, include_quotes=False) if universe else None,
        }
    )


@app.get("/api/universes/{slug}")
def api_universe_detail(slug: str, quotes: bool = False, db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    row = db.query(ResearchUniverse).filter_by(slug=slug).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Universe not found.")
    detail_key = f"universes:{_cache_scope()}:detail:{slug}:quotes{1 if quotes else 0}"
    payload = _UNIVERSE_DETAIL_CACHE.get_or_set(
        detail_key,
        lambda: serialize_universe(db, row, include_rows=True, include_quotes=bool(quotes)),
        ttl_s=30.0 if quotes else 45.0,
    )
    return JSONResponse({"universe": payload})


@app.post("/api/universes/{universe_id}/sections")
def api_universe_section_create(
    universe_id: int,
    payload: dict[str, Any] = Body(default={}),
    db: Session = Depends(get_session),
) -> JSONResponse:
    _guard_internal_terminal()
    universe = db.get(ResearchUniverse, int(universe_id))
    if universe is None:
        raise HTTPException(status_code=404, detail="Universe not found.")
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Missing section name.")
    section = ensure_section(
        db,
        universe,
        name=name,
        description=payload.get("description"),
    )
    db.commit()
    _invalidate_terminal_caches("universes")
    return JSONResponse(
        {
            "ok": True,
            "universe_id": int(universe.id),
            "section_id": int(section.id),
            "section": _serialize_universe_section_delta(section),
        }
    )


@app.post("/api/universes/{universe_id}/sections/reorder")
def api_universe_sections_reorder(
    universe_id: int,
    payload: dict[str, Any] = Body(default={}),
    db: Session = Depends(get_session),
) -> JSONResponse:
    _guard_internal_terminal()
    try:
        sections = reorder_universe_sections(db, int(universe_id), _payload_id_list(payload, "section_ids"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    _invalidate_terminal_caches("universes")
    return JSONResponse(
        {
            "ok": True,
            "universe_id": int(universe_id),
            "sections": [_serialize_universe_section_delta(section) for section in sections],
        }
    )


@app.patch("/api/universes/sections/{section_id}")
def api_universe_section_update(
    section_id: int,
    payload: dict[str, Any] = Body(default={}),
    db: Session = Depends(get_session),
) -> JSONResponse:
    _guard_internal_terminal()
    try:
        section = update_section(
            db,
            int(section_id),
            name=payload.get("name") if "name" in payload else None,
            description=payload.get("description") if "description" in payload else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404 if "not found" in str(exc).lower() else 400, detail=str(exc)) from exc
    universe = db.get(ResearchUniverse, int(section.universe_id))
    db.commit()
    _invalidate_terminal_caches("universes")
    return JSONResponse(
        {
            "ok": True,
            "section": {"id": int(section.id), "name": section.name, "description": section.description},
            "universe_id": int(universe.id) if universe else None,
        }
    )


@app.delete("/api/universes/sections/{section_id}")
def api_universe_section_delete(section_id: int, db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    section = db.get(ResearchUniverseSection, int(section_id))
    if section is None:
        raise HTTPException(status_code=404, detail="Section not found.")
    universe_id = int(section.universe_id)
    try:
        delete_section(db, int(section_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    universe = db.get(ResearchUniverse, universe_id)
    db.commit()
    _invalidate_terminal_caches("universes")
    return JSONResponse({"ok": True, "universe_id": int(universe.id) if universe else universe_id, "section_id": int(section_id)})


@app.post("/api/universes/sections/{section_id}/rows/reorder")
def api_universe_rows_reorder(
    section_id: int,
    payload: dict[str, Any] = Body(default={}),
    db: Session = Depends(get_session),
) -> JSONResponse:
    _guard_internal_terminal()
    try:
        rows = reorder_universe_rows(db, int(section_id), _payload_id_list(payload, "row_ids"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    universe_id = int(rows[0].universe_id) if rows else None
    db.commit()
    _invalidate_terminal_caches("universes")
    return JSONResponse(
        {
            "ok": True,
            "section_id": int(section_id),
            "universe_id": universe_id,
            "rows": [_serialize_universe_row_delta(row) for row in rows],
        }
    )


@app.post("/api/universes/{universe_id}/rows")
def api_universe_row_add(
    universe_id: int,
    payload: dict[str, Any] = Body(default={}),
    db: Session = Depends(get_session),
) -> JSONResponse:
    _guard_internal_terminal()
    section_id = payload.get("section_id")
    if not section_id:
        raise HTTPException(status_code=400, detail="Missing section id.")
    instrument_payload = _instrument_payload_from_request(db, payload)
    row = add_universe_row(
        db,
        universe_id=int(universe_id),
        section_id=int(section_id),
        instrument_payload=instrument_payload,
        display_label=payload.get("display_label"),
        role=payload.get("role"),
        exposure_type=payload.get("exposure_type"),
        exposure_strength=payload.get("exposure_strength"),
        conviction=payload.get("conviction"),
        notes=payload.get("notes"),
        source_provider=payload.get("source_provider"),
        source_external_id=payload.get("source_external_id"),
        metadata={"created_via": "web"},
    )
    universe = db.get(ResearchUniverse, int(universe_id))
    db.commit()
    _invalidate_terminal_caches("universes")
    return JSONResponse(
        {
            "ok": True,
            "row_id": int(row.id),
            "universe_id": int(universe.id) if universe else int(universe_id),
            "section_id": int(row.section_id),
            "row": _serialize_universe_row_delta(row),
        }
    )


@app.patch("/api/universes/rows/{row_id}")
def api_universe_row_update(
    row_id: int,
    payload: dict[str, Any] = Body(default={}),
    db: Session = Depends(get_session),
) -> JSONResponse:
    _guard_internal_terminal()
    try:
        row = update_universe_row(
            db,
            int(row_id),
            display_label=payload.get("display_label") if "display_label" in payload else None,
            role=payload.get("role") if "role" in payload else None,
            exposure_type=payload.get("exposure_type") if "exposure_type" in payload else None,
            exposure_strength=payload.get("exposure_strength") if "exposure_strength" in payload else None,
            conviction=payload.get("conviction") if "conviction" in payload else None,
            notes=payload.get("notes") if "notes" in payload else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404 if "not found" in str(exc).lower() else 400, detail=str(exc)) from exc
    universe = db.get(ResearchUniverse, int(row.universe_id))
    db.commit()
    _invalidate_terminal_caches("universes")
    return JSONResponse(
        {
            "ok": True,
            "row_id": int(row.id),
            "universe_id": int(universe.id) if universe else int(row.universe_id),
            "section_id": int(row.section_id),
            "row": _serialize_universe_row_delta(row),
        }
    )


@app.delete("/api/universes/rows/{row_id}")
def api_universe_row_delete(row_id: int, db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    row = db.get(ResearchUniverseRow, int(row_id))
    if row is None or row.archived_at is not None:
        raise HTTPException(status_code=404, detail="Universe row not found.")
    universe_id = int(row.universe_id)
    try:
        delete_universe_row(db, int(row_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    universe = db.get(ResearchUniverse, universe_id)
    db.commit()
    _invalidate_terminal_caches("universes")
    return JSONResponse({"ok": True, "universe_id": int(universe.id) if universe else universe_id, "row_id": int(row_id)})
