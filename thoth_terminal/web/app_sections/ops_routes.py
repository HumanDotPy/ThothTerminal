from __future__ import annotations

from thoth_terminal.web.app_sections._shared import bind_app_context


bind_app_context(globals())


def _agent_pagination_model(
    *,
    base_path: str,
    total: int,
    page: int,
    page_size: int,
    hours: int,
) -> dict[str, Any]:
    total = max(0, int(total or 0))
    page_size = max(1, int(page_size or 20))
    page_count = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(int(page or 1), page_count))

    def href(target: int) -> str:
        return f"{base_path}?{urlencode({'page': target, 'page_size': page_size, 'hours': hours})}"

    visible = {1, page_count}
    visible.update(range(max(1, page - 2), min(page_count, page + 2) + 1))
    page_tokens: list[dict[str, Any] | None] = []
    previous_number: int | None = None
    for number in sorted(visible):
        if previous_number is not None and number - previous_number > 1:
            page_tokens.append(None)
        page_tokens.append({"number": number, "href": href(number), "current": number == page})
        previous_number = number

    start = 0 if total == 0 else ((page - 1) * page_size) + 1
    end = min(total, page * page_size)
    return {
        "base_path": base_path,
        "page": page,
        "page_size": page_size,
        "page_count": page_count,
        "total": total,
        "start": start,
        "end": end,
        "hours": hours,
        "pages": page_tokens,
        "previous_href": href(page - 1) if page > 1 else None,
        "next_href": href(page + 1) if page < page_count else None,
        "back_ten_href": href(max(1, page - 10)) if page > 1 else None,
        "forward_ten_href": href(min(page_count, page + 10)) if page < page_count else None,
    }


@app.get("/ops/version")
def ops_version() -> JSONResponse:
    return JSONResponse(get_runtime_identity())


@app.get("/ops/live")
def ops_live() -> JSONResponse:
    """Constant-time process liveness probe; never touches databases or integrations."""
    runtime = get_runtime_identity()
    return JSONResponse(
        {
            "ok": True,
            "service": "web",
            "pid": runtime.get("pid"),
            "boot_id": runtime.get("boot_id"),
            "version": runtime.get("version"),
        }
    )


@app.get("/ops/health")
def ops_health() -> JSONResponse:
    return JSONResponse(_ops_health_payload())


@app.get("/ops", response_class=HTMLResponse)
def ops_view(request: Request, db: Session = Depends(get_session)) -> HTMLResponse:
    notice = str(request.query_params.get("notice") or "").strip() or None
    error = str(request.query_params.get("error") or "").strip() or None
    guild_id_val = int(getattr(settings, "discord_guild_id", 0) or 0) or None
    context = _ops_view_model(db, guild_id=guild_id_val)
    context.update(
        {
            "request": request,
            "notice": notice,
            "error": error,
            "wrap_class": "wide",
        }
    )
    return _template_response("ops.html", context)


@app.get("/agents", response_class=HTMLResponse)
def agents_workspace_entry(request: Request) -> HTMLResponse:
    return RedirectResponse("/agents/harness", status_code=307)


@app.get("/agents/activity", response_class=HTMLResponse)
def agents_view(
    request: Request,
    hours: int = 72,
    page: int = 1,
    page_size: int = 20,
    db: Session = Depends(get_session),
) -> HTMLResponse:
    resolved_hours = max(1, int(hours or 72))
    resolved_page_size = min(50, max(10, int(page_size or 20)))
    requested_page = max(1, int(page or 1))
    review_state = build_local_agent_review_state(
        db,
        hours=resolved_hours,
        limit=resolved_page_size,
        offset=(requested_page - 1) * resolved_page_size,
    )
    state = build_agent_overview_state(db, hours=resolved_hours, review_state=review_state)
    pagination = _agent_pagination_model(
        base_path="/agents/activity",
        total=review_state.get("total_items", 0),
        page=(int(review_state.get("offset", 0)) // resolved_page_size) + 1,
        page_size=resolved_page_size,
        hours=resolved_hours,
    )
    state["work_items"] = [
        {
            "kind": "review",
            "label": row.get("recommendation") or row.get("document_disposition") or "Needs review",
            "title": row.get("document_title") or f"Document #{row.get('document_id')}",
            "href": row.get("document_href"),
            "summary": row.get("summary_short") or row.get("skip_reason") or "No signal extraction summary.",
            "created_at": row.get("updated_at"),
            "tone": "blocked" if row.get("status") == "error" else "review",
        }
        for row in review_state.get("items", [])
    ]
    state["pagination"] = pagination
    state["metrics"]["review_needed"] = int(review_state.get("total_items", 0))
    state["openshell"] = _settings_openshell_status_payload(cached=True)
    return _template_response(
        "agents.html",
        {
            "request": request,
            "state": state,
            "wrap_class": "wide",
            "title": "Agents",
        },
    )


@app.get("/agents/cloud", response_class=HTMLResponse)
def cloud_agents_view(request: Request) -> HTMLResponse:
    # Always render navigation before DB reads, provider-network catalogs or WSL
    # probes. Status is one explicit asynchronous read with a visible retry path.
    from thoth_terminal.cloud_operator_service import cloud_automation_settings_payload
    cloud_settings = cloud_automation_settings_payload()
    provider, model = cloud_settings["provider"], cloud_settings["model"]
    catalog = {
        "selected_provider": provider, "selected_model": model,
        "free_only": bool(getattr(settings, "openrouter_prefer_free_models", True)),
        "rows": [{"provider": provider, "model": model, "service": provider,
                    "model_label": model, "selected": True, "usable": bool(provider and model),
                    "price_input": "Unknown", "price_output": "Unknown"}],
    }
    cloud_status = {"settings": cloud_settings, "loading": True}
    return _template_response("cloud_agents.html", {
        "request": request, "catalog": catalog, "catalog_bootstrap": catalog,
        "openshell_status": {"ready": None, "status": "unchecked"},
        "cloud_status": cloud_status, "cloud_status_bootstrap": cloud_status,
        "wrap_class": "wide", "title": "Cloud research runs",
    })


@app.get("/agent-review", response_class=HTMLResponse)
def agent_review_view(
    request: Request,
    hours: int = 72,
    page: int = 1,
    page_size: int = 20,
    db: Session = Depends(get_session),
) -> HTMLResponse:
    notice = str(request.query_params.get("notice") or "").strip() or None
    error = str(request.query_params.get("error") or "").strip() or None
    resolved_hours = max(1, int(hours or 72))
    resolved_page_size = min(50, max(10, int(page_size or 20)))
    requested_page = max(1, int(page or 1))
    state = build_local_agent_review_state(
        db,
        hours=resolved_hours,
        limit=resolved_page_size,
        offset=(requested_page - 1) * resolved_page_size,
    )
    state["pagination"] = _agent_pagination_model(
        base_path="/agent-review",
        total=state.get("total_items", 0),
        page=(int(state.get("offset", 0)) // resolved_page_size) + 1,
        page_size=resolved_page_size,
        hours=resolved_hours,
    )
    return _template_response(
        "agent_review.html",
        {
            "request": request,
            "notice": notice,
            "error": error,
            "state": state,
            "wrap_class": "wide",
            "title": "Agentic ingestion",
        },
    )


@app.post("/agent-review/decisions")
def agent_review_decision(
    target_kind: str = Form(...),
    target_id: int = Form(...),
    decision: str = Form(...),
    note: str | None = Form(None),
    db: Session = Depends(get_session),
) -> RedirectResponse:
    try:
        row, action = record_review_decision(
            db,
            target_kind=target_kind,
            target_id=int(target_id),
            decision=decision,
            note=note,
            created_by="operator",
        )
        db.commit()
        suffix = f"; action #{int(action.id)} queued" if action is not None else ""
        return _agent_review_redirect_with_notice(notice=f"Recorded {row.decision} for {row.target_kind} #{row.target_id}{suffix}.")
    except Exception as exc:
        db.rollback()
        return _agent_review_redirect_with_notice(error=f"{type(exc).__name__}: {exc}")


@app.post("/agent-review/evals/seed")
def agent_review_seed_evals(db: Session = Depends(get_session)) -> RedirectResponse:
    try:
        stats = seed_default_eval_cases(db)
        db.commit()
        return _agent_review_redirect_with_notice(
            notice=f"Seeded eval cases: {stats['created']} created, {stats['updated']} updated."
        )
    except Exception as exc:
        db.rollback()
        return _agent_review_redirect_with_notice(error=f"{type(exc).__name__}: {exc}")


@app.post("/agent-review/evals/from-compression")
def agent_review_create_eval_from_compression(
    compression_id: int = Form(...),
    expected_disposition: str | None = Form(None),
    expected_information_gain: str | None = Form(None),
    allow_research_writes: bool = Form(False),
    db: Session = Depends(get_session),
) -> RedirectResponse:
    try:
        expected = {
            "document_disposition": expected_disposition,
            "information_gain": expected_information_gain,
            "allow_research_writes": bool(allow_research_writes),
        }
        row = create_eval_case_from_compression(db, compression_id=int(compression_id), expected=expected)
        db.commit()
        return _agent_review_redirect_with_notice(notice=f"Eval case #{int(row.id)} created for compression #{int(compression_id)}.")
    except Exception as exc:
        db.rollback()
        return _agent_review_redirect_with_notice(error=f"{type(exc).__name__}: {exc}")


@app.post("/agent-review/evals/{case_id}/run")
def agent_review_run_eval(
    case_id: int,
    mode: str = Form("latest_compression"),
    db: Session = Depends(get_session),
) -> RedirectResponse:
    try:
        row = run_eval_case(db, case_id=int(case_id), mode=mode)
        db.commit()
        score = (row.score_json or {}).get("score", 0)
        passed = "passed" if (row.score_json or {}).get("passed") else "failed"
        if row.status == "error":
            return _agent_review_redirect_with_notice(error=f"Eval #{int(row.id)} error: {row.error}")
        return _agent_review_redirect_with_notice(notice=f"Eval #{int(row.id)} {passed} at {score}.")
    except Exception as exc:
        db.rollback()
        return _agent_review_redirect_with_notice(error=f"{type(exc).__name__}: {exc}")


@app.post("/ops/desk")
def ops_desk_update(
    enabled: bool = Form(...),
) -> RedirectResponse:
    guild_id = int(getattr(settings, "discord_guild_id", 0) or 0)
    if not guild_id:
        return _ops_redirect_with_notice(error="Missing THOTH_DISCORD_GUILD_ID")
    try:
        from thoth_terminal.discord.desk import set_desk_enabled

        set_desk_enabled(guild_id=guild_id, enabled=bool(enabled), user_id=0)
        state = "enabled" if bool(enabled) else "disabled"
        return _ops_redirect_with_notice(notice=f"Desk automation {state}.")
    except Exception as exc:
        return _ops_redirect_with_notice(error=f"{type(exc).__name__}: {exc}")


@app.post("/ops/outbox")
def ops_outbox_update(
    paused: bool = Form(...),
) -> RedirectResponse:
    guild_id = int(getattr(settings, "discord_guild_id", 0) or 0)
    if not guild_id:
        return _ops_redirect_with_notice(error="Missing THOTH_DISCORD_GUILD_ID")
    try:
        from thoth_terminal.discord.desk import set_outbox_paused

        set_outbox_paused(guild_id=guild_id, paused=bool(paused), user_id=0)
        state = "paused" if bool(paused) else "resumed"
        return _ops_redirect_with_notice(notice=f"Discord outbox {state}.")
    except Exception as exc:
        return _ops_redirect_with_notice(error=f"{type(exc).__name__}: {exc}")


@app.post("/ops/jobs/watchlist-refresh")
def ops_watchlist_refresh_enqueue(mode: str = Form("manual")) -> RedirectResponse:
    guild_id = int(getattr(settings, "discord_guild_id", 0) or 0)
    if not guild_id:
        return _ops_redirect_with_notice(error="Missing THOTH_DISCORD_GUILD_ID")
    try:
        from thoth_terminal.jobs import enqueue_discord_watchlist_refresh_job

        mode_v = (mode or "manual").strip().lower()
        with session_scope() as session:
            job = enqueue_discord_watchlist_refresh_job(
                session,
                guild_id=guild_id,
                mode=mode_v,
                dedupe_key=f"ops_watchlist_refresh:{guild_id}:{int(time.time())}",
            )
        return _ops_redirect_with_notice(notice=f"Watchlist refresh job enqueued #{int(job.id)}.")
    except Exception as exc:
        return _ops_redirect_with_notice(error=f"{type(exc).__name__}: {exc}")


@app.post("/ops/jobs/transcript-scan")
def ops_transcript_scan_enqueue(
    mode: str = Form("manual"),
    lookback_days: int = Form(120),
) -> RedirectResponse:
    try:
        mode_v = (mode or "manual").strip().lower()
        with session_scope() as session:
            job = enqueue_watchlist_transcript_scan_job(
                session,
                mode=mode_v,
                lookback_days=max(1, int(lookback_days or 120)),
                force=True,
                dedupe_key=f"ops_transcript_scan:{int(time.time())}",
            )
        return _ops_redirect_with_notice(notice=f"Transcript scan job enqueued #{int(job.id)}.")
    except Exception as exc:
        return _ops_redirect_with_notice(error=f"{type(exc).__name__}: {exc}")


@app.post("/ops/jobs/digest")
def ops_digest_enqueue(
    mode: str = Form("manual"),
    lookback_hours: int = Form(24),
    post_to_outbox: bool = Form(True),
) -> RedirectResponse:
    guild_id = int(getattr(settings, "discord_guild_id", 0) or 0)
    if not guild_id:
        return _ops_redirect_with_notice(error="Missing THOTH_DISCORD_GUILD_ID")
    try:
        from thoth_terminal.discord.desk import get_desk_settings
        from thoth_terminal.jobs import enqueue_agent_digest_job

        mode_v = (mode or "manual").strip().lower()
        desk_cfg = get_desk_settings(guild_id=guild_id)
        post_channel_id = 0
        post_thread_id = None
        if post_to_outbox:
            if not desk_cfg.desk_channel_id:
                return _ops_redirect_with_notice(error="Desk channel not configured.")
            post_channel_id = int(desk_cfg.desk_channel_id)
            post_thread_id = int(desk_cfg.desk_thread_id) if desk_cfg.desk_thread_id else None

        tz_name = str(getattr(settings, "discord_desk_digest_tz", "America/New_York") or "America/New_York")
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo("UTC")
        now_local = utcnow().replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
        if mode_v == "daily":
            date_key = now_local.date().isoformat()
            dedupe_key = f"agent_digest:{guild_id}:{date_key}"
        elif mode_v == "midday":
            date_key = f"{now_local.date().isoformat()}-midday"
            dedupe_key = None
        else:
            date_key = now_local.strftime("%Y-%m-%d %H:%M")
            dedupe_key = None

        with session_scope() as session:
            job = enqueue_agent_digest_job(
                session,
                guild_id=guild_id,
                mode=mode_v,
                lookback_hours=max(1, int(lookback_hours or 24)),
                post_channel_id=post_channel_id,
                post_thread_id=post_thread_id,
                date_key=date_key,
                dedupe_key=dedupe_key,
            )
        return _ops_redirect_with_notice(notice=f"Digest job enqueued #{int(job.id)}.")
    except Exception as exc:
        return _ops_redirect_with_notice(error=f"{type(exc).__name__}: {exc}")


@app.post("/ops/jobs/compress")
def ops_compress_enqueue(
    mode: str = Form("daily_master"),
    lookback_hours: int = Form(24),
    focus_query: str | None = Form(None),
    post_to_outbox: bool = Form(False),
) -> RedirectResponse:
    guild_id = int(getattr(settings, "discord_guild_id", 0) or 0)
    if not guild_id:
        return _ops_redirect_with_notice(error="Missing THOTH_DISCORD_GUILD_ID")
    _, eff_cfg = _agent_settings_for_guild(guild_id)
    try:
        from thoth_terminal.discord import agent_jobs
        from thoth_terminal.discord.desk import get_desk_settings

        post_channel_id = None
        post_thread_id = None
        if post_to_outbox:
            desk_cfg = get_desk_settings(guild_id=guild_id)
            if not desk_cfg.desk_channel_id:
                return _ops_redirect_with_notice(error="Desk channel not configured.")
            post_channel_id = int(desk_cfg.desk_channel_id)
            post_thread_id = int(desk_cfg.desk_thread_id) if desk_cfg.desk_thread_id else None

        job = agent_jobs.enqueue_context_compression(
            guild_id=guild_id,
            mode=(mode or "daily_master"),
            lookback_hours=max(1, int(lookback_hours or eff_cfg.get("context_default_lookback_hours") or 24)),
            focus_query=(focus_query or "").strip() or None,
            post_to_outbox=bool(post_to_outbox),
            post_channel_id=post_channel_id,
            post_thread_id=post_thread_id,
            persist_note=True,
            include_sections=(
                [str(s).strip() for s in eff_cfg.get("default_include_sections", []) if str(s).strip()]
                if isinstance(eff_cfg.get("default_include_sections"), list) and not (focus_query or "").strip()
                else None
            ),
            dedupe_key=f"ops_agent_compress:{guild_id}:{int(time.time())}",
        )
        return _ops_redirect_with_notice(notice=f"Compression job enqueued #{int(job.id)}.")
    except Exception as exc:
        return _ops_redirect_with_notice(error=f"{type(exc).__name__}: {exc}")


@app.post("/ops/jobs/thesis-autopilot")
def ops_thesis_autopilot_enqueue(
    thesis_id: int | None = Form(None),
    lookback_hours: int = Form(72),
) -> RedirectResponse:
    guild_id = int(getattr(settings, "discord_guild_id", 0) or 0) or None
    try:
        from thoth_terminal.jobs import enqueue_agent_thesis_autopilot_job

        with session_scope() as session:
            job = enqueue_agent_thesis_autopilot_job(
                session,
                guild_id=guild_id,
                thesis_id=int(thesis_id) if thesis_id else None,
                lookback_hours=max(1, int(lookback_hours or 72)),
                dedupe_key=f"ops_thesis_autopilot:{int(time.time())}",
            )
        return _ops_redirect_with_notice(notice=f"Thesis autopilot job enqueued #{int(job.id)}.")
    except Exception as exc:
        return _ops_redirect_with_notice(error=f"{type(exc).__name__}: {exc}")


@app.post("/ops/actions/{action_id}")
def ops_action_update(
    action_id: int,
    status: str = Form(...),
    resolution_note: str | None = Form(None),
) -> RedirectResponse:
    try:
        status_v = canonical_action_status(status)
        with session_scope() as session:
            action = update_agent_action_status(
                session,
                action_id=int(action_id),
                status=status_v,
                resolution_note=resolution_note,
            )
            if not action:
                return _ops_redirect_with_notice(error="Agent action not found.")
            return _ops_redirect_with_notice(notice=f"Action #{int(action_id)} marked {status_v}.")
    except Exception as exc:
        return _ops_redirect_with_notice(error=f"{type(exc).__name__}: {exc}")


@app.post("/ops/proposals/{proposal_id}")
def ops_proposal_review(
    proposal_id: int,
    decision: str = Form(...),
    reviewer_note: str | None = Form(None),
) -> RedirectResponse:
    try:
        with session_scope() as session:
            proposal = review_thesis_proposal(
                session,
                proposal_id=int(proposal_id),
                decision=decision,
                reviewer_note=reviewer_note,
            )
            if str(proposal.status) == PROPOSAL_STATUS_PROMOTED and proposal.approved_thesis_id is not None:
                return _ops_redirect_with_notice(
                    notice=f"Proposal #{int(proposal_id)} approved as thesis #{int(proposal.approved_thesis_id)}."
                )
        return _ops_redirect_with_notice(notice=f"Proposal #{int(proposal_id)} {str(decision).strip().lower()}ed.")
    except Exception as exc:
        return _ops_redirect_with_notice(error=f"{type(exc).__name__}: {exc}")


@app.get("/manifest.json")
def pwa_manifest() -> Response:
    manifest_path = BASE_DIR / "static" / "manifest.json"
    return FileResponse(
        path=str(manifest_path),
        media_type="application/manifest+json",
    )


@app.get("/service-worker.js")
def service_worker() -> Response:
    sw_path = BASE_DIR / "static" / "service-worker.js"
    return FileResponse(
        path=str(sw_path),
        media_type="application/javascript",
    )


@app.get("/favicon.ico")
def favicon() -> Response:
    icon_path = BASE_DIR / "static" / "icons" / "thoth-terminal.ico"
    return FileResponse(path=str(icon_path), media_type="image/x-icon")


@app.get("/apple-touch-icon.png")
def apple_touch_icon() -> Response:
    icon_path = BASE_DIR / "static" / "icons" / "thoth-terminal-1024.png"
    return FileResponse(path=str(icon_path), media_type="image/png")


@app.get("/apple-touch-icon-precomposed.png")
def apple_touch_icon_precomposed() -> Response:
    icon_path = BASE_DIR / "static" / "icons" / "thoth-terminal-1024.png"
    return FileResponse(path=str(icon_path), media_type="image/png")

async def _scheduler_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(_run_scheduler_tick_safe)
        except Exception as exc:
            logger.exception("Scheduler loop error: %s", exc)
        await asyncio.sleep(60)


def _run_scheduler_tick_safe() -> None:
    try:
        run_scheduler_tick_no_lock()
    except Exception:
        logger.exception("scheduler tick failed")
