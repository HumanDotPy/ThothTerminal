from __future__ import annotations

from urllib.parse import urlsplit

from thoth_terminal.local_agent_service import serialize_write_diff
from thoth_terminal.models import LocalAgentWriteDiff
from thoth_terminal.security import is_loopback_host
from thoth_terminal.web.app_sections._shared import bind_app_context


bind_app_context(globals())


@app.get("/settings", response_class=HTMLResponse)
def settings_view(request: Request) -> HTMLResponse:
    from thoth_terminal.cloud_operator_service import cloud_automation_settings_payload
    earnings_rail_enabled = _cookie_enabled(request, _EARNINGS_FEED_COOKIE, default=False)
    markets_tape_enabled = _cookie_enabled(request, _MARKETS_TAPE_COOKIE, default=True)
    ticker_display_mode = _cookie_choice(request, _TICKER_DISPLAY_COOKIE, allowed={"local", "adr"}, default="local")
    discord_guild_id_val = int(getattr(settings, "discord_guild_id", 0) or 0)
    agent_guild_raw, agent_guild_effective = _agent_settings_for_guild(discord_guild_id_val or None)
    desktop_status = _desktop_status_payload(request.app)
    google_error = str(request.query_params.get("google_error") or "").strip() or None
    google_notice = str(request.query_params.get("google_notice") or "").strip() or None
    google_status_payload = _settings_google_status_payload(cached=True)
    openshell_status_payload = _settings_openshell_status_payload(cached=True)
    try:
        ibkr_status_payload = ibkr_service().status(live=False)
    except Exception as exc:
        ibkr_status_payload = {"enabled": False, "connected": False, "error": f"{type(exc).__name__}: {exc}"}
    ibkr_gateway_base = str(getattr(settings, "ibkr_gateway_base_url", "https://localhost:5000/v1/api") or "").rstrip("/")
    ibkr_gateway_login_url = gateway_browser_login_url(ibkr_gateway_base)
    return _template_response(
        "settings.html",
        {
            "request": request,
            "earnings_rail_enabled": earnings_rail_enabled,
            "markets_tape_enabled": markets_tape_enabled,
            "ticker_display_mode": ticker_display_mode,
            "dev_mode": getattr(settings, "dev_mode", True),
            "ai_provider": getattr(settings, "ai_provider", "openai"),
            "openai_model": getattr(settings, "openai_model", "gpt-4o"),
            "anthropic_model": getattr(settings, "anthropic_model", "claude-3-5-sonnet-20241022"),
            "openrouter_model": getattr(settings, "openrouter_model", "openrouter/free"),
            "openrouter_free_model": getattr(settings, "openrouter_free_model", "openrouter/free"),
            "openrouter_free_fallback_models": getattr(settings, "openrouter_free_fallback_models", ""),
            "openrouter_prefer_free_models": bool(getattr(settings, "openrouter_prefer_free_models", True)),
            "deepseek_model": getattr(settings, "deepseek_model", "deepseek-v4-flash"),
            "meta_model": getattr(settings, "meta_model", "muse-spark-1.3-contributor"),
            "has_openai_key": bool(getattr(settings, "openai_api_key", None)),
            "has_anthropic_key": bool(getattr(settings, "anthropic_api_key", None)),
            "has_openrouter_key": bool(getattr(settings, "openrouter_api_key", None)),
            "has_deepseek_key": bool(getattr(settings, "deepseek_api_key", None)),
            "has_meta_key": bool(getattr(settings, "meta_api_key", None)),
            "markets_enabled": getattr(settings, "markets_enabled", True),
            "api_key_health": official_api_key_health(),
            "google_status": google_status_payload,
            "openshell_status": openshell_status_payload,
            "ibkr_status": ibkr_status_payload,
            "ibkr_gateway_login_url": ibkr_gateway_login_url,
            "google_error": google_error,
            "google_notice": google_notice,
            "agent_context_default_lookback_hours": int(getattr(settings, "agent_context_default_lookback_hours", 24) or 24),
            "agent_context_max_items_per_section": int(getattr(settings, "agent_context_max_items_per_section", 25) or 25),
            "agent_context_max_prompt_chars": int(getattr(settings, "agent_context_max_prompt_chars", 24000) or 24000),
            "agent_daily_master_enabled": bool(getattr(settings, "agent_daily_master_enabled", True)),
            "agent_daily_master_hhmm": str(getattr(settings, "agent_daily_master_hhmm", "18:10") or "18:10"),
            "agent_daily_master_tz": str(getattr(settings, "agent_daily_master_tz", "America/New_York") or "America/New_York"),
            "discord_guild_id": discord_guild_id_val,
            "desktop_status": desktop_status,
            "agent_guild_settings_raw": agent_guild_raw,
            "agent_guild_settings_effective": agent_guild_effective,
            "schedule": cloud_automation_settings_payload(),
            "schedule_timezone": str(getattr(settings, "default_active_window_tz", "America/New_York")),
        },
    )


def _settings_redirect_with_notice(*, notice: str | None = None, error: str | None = None) -> RedirectResponse:
    params: dict[str, str] = {}
    if notice:
        params["google_notice"] = str(notice)
    if error:
        params["google_error"] = str(error)[:500]
    query = ("?" + urlencode(params)) if params else ""
    return RedirectResponse(url=f"/settings{query}", status_code=303)


def _upsert_env_values(values: dict[str, str | None]) -> None:
    env_path = Path(".env")
    existing = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    keys = set(values)
    kept = [
        line
        for line in existing
        if not any(line.strip().startswith(f"{key}=") or line.strip().startswith(f"{key} =") for key in keys)
    ]
    for key, value in values.items():
        if value is None:
            continue
        kept.append(f"{key}={value}")
    env_path.write_text("\n".join(kept).rstrip() + "\n", encoding="utf-8")


@app.post("/settings/ibkr/update")
def settings_ibkr_update(
    enable_ibkr: bool = Form(False),
    ibkr_mock_enabled: bool = Form(False),
    ibkr_gateway_base_url: str = Form("https://localhost:5000/v1/api"),
    ibkr_default_account_id: str = Form(""),
    ibkr_symbol_conids_json: str = Form(""),
) -> RedirectResponse:
    if build_security_posture(settings).public:
        return _settings_redirect_with_notice(error="IBKR cannot be enabled in public mode.")
    gateway = (ibkr_gateway_base_url or "https://localhost:5000/v1/api").strip().rstrip("/")
    symbols = (ibkr_symbol_conids_json or "").strip()
    if symbols:
        try:
            parsed = json.loads(symbols)
            if not isinstance(parsed, dict):
                raise ValueError("symbol mapping must be a JSON object")
        except Exception as exc:
            return _settings_redirect_with_notice(error=f"Invalid IBKR symbol mapping JSON: {exc}")
    updates = {
        "THOTH_ENABLE_IBKR": "true" if enable_ibkr else "false",
        "THOTH_IBKR_MOCK_ENABLED": "true" if ibkr_mock_enabled else "false",
        "THOTH_IBKR_GATEWAY_BASE_URL": gateway,
        "THOTH_IBKR_DEFAULT_ACCOUNT_ID": (ibkr_default_account_id or "").strip(),
        "THOTH_IBKR_SYMBOL_CONIDS_JSON": symbols,
    }
    _upsert_env_values(updates)
    settings.enable_ibkr = bool(enable_ibkr)
    settings.ibkr_mock_enabled = bool(ibkr_mock_enabled)
    settings.ibkr_gateway_base_url = gateway
    settings.ibkr_default_account_id = (ibkr_default_account_id or "").strip() or None
    settings.ibkr_symbol_conids_json = symbols or None
    return _settings_redirect_with_notice(
        notice="IBKR settings saved. Authenticate in the local Client Portal Gateway if live mode is enabled."
    )


@app.post("/settings/google/connect")
def settings_google_connect(request: Request) -> RedirectResponse:
    try:
        from thoth_terminal.google_oauth import GoogleIntegrationError, start_web_connect

        configured_base = (getattr(settings, "google_oauth_redirect_base_url", None) or "").strip() or None
        auth_url = start_web_connect(base_url=(configured_base or str(request.base_url).rstrip("/")))
        return RedirectResponse(url=auth_url, status_code=303)
    except GoogleIntegrationError as exc:
        return _settings_redirect_with_notice(error=str(exc))
    except Exception as exc:
        return _settings_redirect_with_notice(error=f"{type(exc).__name__}: {exc}")


@app.get("/settings/google/callback")
def settings_google_callback(state: str = "", code: str = "") -> RedirectResponse:
    try:
        from thoth_terminal.google_oauth import GoogleIntegrationError, finish_web_connect

        finish_web_connect(state=state, code=code)
        _invalidate_google_status_cache()
        return _settings_redirect_with_notice(notice="Google integration connected.")
    except GoogleIntegrationError as exc:
        return _settings_redirect_with_notice(error=str(exc))
    except Exception as exc:
        return _settings_redirect_with_notice(error=f"{type(exc).__name__}: {exc}")


@app.post("/settings/google/disconnect")
def settings_google_disconnect() -> RedirectResponse:
    try:
        from thoth_terminal.google_oauth import GoogleIntegrationError, disconnect as google_disconnect

        removed = bool(google_disconnect())
        _invalidate_google_status_cache()
        return _settings_redirect_with_notice(
            notice="Google integration disconnected." if removed else "Google integration token was already absent."
        )
    except GoogleIntegrationError as exc:
        return _settings_redirect_with_notice(error=str(exc))
    except Exception as exc:
        return _settings_redirect_with_notice(error=f"{type(exc).__name__}: {exc}")


@app.post("/settings/agent/preferences")
def settings_agent_preferences_save(
    daily_master_enabled: bool = Form(False),
    daily_master_hhmm: str | None = Form(None),
    daily_master_tz: str | None = Form(None),
    context_default_lookback_hours: int = Form(24),
    context_max_items_per_section: int = Form(25),
    context_max_prompt_chars: int = Form(24000),
    default_include_sections: str | None = Form(None),
    default_post_to_outbox: bool = Form(False),
) -> RedirectResponse:
    guild_id = int(getattr(settings, "discord_guild_id", 0) or 0)
    if not guild_id:
        return _settings_redirect_with_notice(error="Missing THOTH_DISCORD_GUILD_ID")
    try:
        patch = _normalize_agent_settings_patch(
            {
                "daily_master_enabled": bool(daily_master_enabled),
                "daily_master_hhmm": (daily_master_hhmm or "").strip() or None,
                "daily_master_tz": (daily_master_tz or "").strip() or None,
                "context_default_lookback_hours": int(context_default_lookback_hours),
                "context_max_items_per_section": int(context_max_items_per_section),
                "context_max_prompt_chars": int(context_max_prompt_chars),
                "default_include_sections": (default_include_sections or "").strip(),
                "default_post_to_outbox": bool(default_post_to_outbox),
            }
        )
        from thoth_terminal.discord.desk import update_agent_settings

        update_agent_settings(
            guild_id=guild_id,
            patch=patch,
            user_id=0,
        )
        _invalidate_agent_settings_cache(guild_id)
        return _settings_redirect_with_notice(notice="Per-guild agent settings saved.")
    except Exception as exc:
        return _settings_redirect_with_notice(error=f"{type(exc).__name__}: {exc}")


@app.get("/api/settings/google/status")
def api_settings_google_status() -> JSONResponse:
    try:
        payload = _google_status_payload_live()
        _SETTINGS_GOOGLE_STATUS_CACHE.set(_google_status_cache_key(), dict(payload))
        return JSONResponse({"ok": True, "status": payload})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=200)


@app.get("/api/settings/openshell/status")
def api_settings_openshell_status() -> JSONResponse:
    try:
        payload = _openshell_status_payload_live()
        _OPENSHELL_STATUS_CACHE.set(_openshell_status_cache_key(), dict(payload))
        return JSONResponse({"ok": True, "status": payload})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=200)


@app.get("/api/agents/cloud/models")
def api_cloud_agent_models(refresh: bool = False) -> JSONResponse:
    try:
        catalog = _cloud_model_catalog_payload(cached=not bool(refresh))
        return JSONResponse({"ok": True, "catalog": catalog})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=200)


@app.get("/api/agents/cloud/status")
def api_cloud_operator_status(limit: int = 24, db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    try:
        from thoth_terminal.cloud_operator_service import cloud_operator_status

        return JSONResponse({"ok": True, "status": cloud_operator_status(db, limit=limit, openshell_status=_settings_openshell_status_payload(cached=True))})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=200)


@app.get("/api/agents/requests")
def api_agent_request_manifests(
    run_kind: str | None = None,
    run_id: int | None = None,
    task_kind: str | None = None,
    status: str | None = None,
    limit: int = 100,
    db: Session = Depends(get_session),
) -> JSONResponse:
    _guard_internal_terminal()
    from thoth_terminal.agent_request_manifest_service import list_agent_request_manifests

    return JSONResponse(
        {
            "ok": True,
            "requests": list_agent_request_manifests(
                db,
                run_kind=run_kind,
                run_id=run_id,
                task_kind=task_kind,
                status=status,
                limit=limit,
            ),
        }
    )


@app.get("/api/agents/requests/{manifest_id}")
def api_agent_request_manifest(manifest_id: int, db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    from thoth_terminal.agent_request_manifest_service import get_agent_request_manifest

    payload = get_agent_request_manifest(db, manifest_id, include_payload=True)
    if payload is None:
        return JSONResponse({"ok": False, "error": "request_manifest_not_found"}, status_code=404)
    return JSONResponse({"ok": True, "request": payload})


@app.get("/api/agents/cloud/runs/{run_id}/projections")
def api_cloud_goal_projections(
    run_id: int,
    limit: int = 100,
    include_content: bool = False,
    db: Session = Depends(get_session),
) -> JSONResponse:
    _guard_internal_terminal()
    from thoth_terminal.cloud_goal_run_service import list_cloud_goal_projections

    return JSONResponse(
        {
            "ok": True,
            "projections": list_cloud_goal_projections(
                db,
                run_id,
                limit=limit,
                include_content=include_content,
            ),
        }
    )


@app.get("/api/agents/cloud/runs/{run_id}/replay")
def api_cloud_goal_replay(
    run_id: int,
    mode: str = "projection",
    projection_id: int | None = None,
    db: Session = Depends(get_session),
) -> JSONResponse:
    _guard_internal_terminal()
    from thoth_terminal.cloud_goal_run_service import replay_cloud_goal_context

    try:
        return JSONResponse(
            {
                "ok": True,
                "replay": replay_cloud_goal_context(
                    db,
                    run_id,
                    mode=mode,
                    projection_id=projection_id,
                ),
            }
        )
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=404 if "not found" in str(exc).lower() else 400)


@app.get("/api/agents/cloud/lineage")
def api_cloud_goal_lineage(
    parent_run_id: int | None = None,
    child_run_id: int | None = None,
    status: str | None = None,
    limit: int = 100,
    db: Session = Depends(get_session),
) -> JSONResponse:
    _guard_internal_terminal()
    from thoth_terminal.cloud_goal_lineage_service import list_cloud_goal_lineages

    return JSONResponse(
        {
            "ok": True,
            "lineage": list_cloud_goal_lineages(
                db,
                parent_run_id=parent_run_id,
                child_run_id=child_run_id,
                status=status,
                limit=limit,
                reconcile=False,
            ),
        }
    )


@app.post("/api/agents/cloud/lineage/{lineage_id}/accept")
def api_cloud_goal_lineage_accept(lineage_id: int, db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    from thoth_terminal.cloud_goal_lineage_service import (
        accept_cloud_goal_lineage_output,
        serialize_cloud_goal_lineage,
    )

    try:
        row = accept_cloud_goal_lineage_output(db, lineage_id)
        db.commit()
        return JSONResponse({"ok": True, "lineage": serialize_cloud_goal_lineage(row)})
    except ValueError as exc:
        db.rollback()
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.get("/api/agents/inbox")
def api_agent_inbox_items(
    target_kind: str | None = None,
    target_id: int | None = None,
    status: str | None = None,
    delivery_mode: str | None = None,
    limit: int = 100,
    db: Session = Depends(get_session),
) -> JSONResponse:
    _guard_internal_terminal()
    from thoth_terminal.agent_inbox_service import list_agent_inbox_items

    try:
        rows = list_agent_inbox_items(
            db,
            target_kind=target_kind,
            target_id=target_id,
            status=status,
            delivery_mode=delivery_mode,
            limit=limit,
        )
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "items": rows})


@app.get("/api/agents/desired-state")
def api_agent_desired_state() -> JSONResponse:
    _guard_internal_terminal()
    from thoth_terminal.runtime_desired_state import desired_state_registry, read_desired_state_projection

    registry = desired_state_registry()
    observed = read_desired_state_projection()
    return JSONResponse(
        {
            "ok": True,
            "registry_version": registry.registry_version,
            "observed": observed,
            "components": [
                {
                    "name": row.name,
                    "owner": row.owner,
                    "kind": row.kind,
                    "desired": row.desired,
                    "enabled_when": row.enabled_when,
                    "requires": list(row.requires),
                    "probe": dict(row.probe),
                    "repair": dict(row.repair),
                    "critical": row.critical,
                }
                for row in registry.components
            ],
        }
    )


@app.get("/api/agents/cloud/write-diffs/{write_diff_id}")
def api_cloud_agent_write_diff(write_diff_id: int, db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    row = db.get(LocalAgentWriteDiff, int(write_diff_id))
    if row is None:
        return JSONResponse({"ok": False, "error": "write_diff_not_found"}, status_code=404)
    return JSONResponse({"ok": True, "write": serialize_write_diff(row)})


@app.get("/api/agents/cloud/memory/context")
def api_cloud_agent_memory_context(
    agent_key: str = "cloud_research_heartbeat",
    max_chars: int = 160000,
    db: Session = Depends(get_session),
) -> JSONResponse:
    _guard_internal_terminal()
    try:
        from thoth_terminal.cloud_agent_memory_service import render_cloud_agent_memory_context

        content_md, meta = render_cloud_agent_memory_context(
            db,
            agent_key=agent_key,
            max_chars=max(2000, min(int(max_chars or 160000), 600000)),
            mark_accessed=False,
        )
        return JSONResponse({"ok": True, "content_md": content_md, "meta": meta})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=200)


@app.get("/api/agents/cloud/memory/segments")
def api_cloud_agent_memory_segments(
    agent_key: str = "cloud_research_heartbeat",
    status: str = "active",
    query: str | None = None,
    segment_type: str | None = None,
    cache_scope: str | None = None,
    limit: int = 200,
    db: Session = Depends(get_session),
) -> JSONResponse:
    _guard_internal_terminal()
    try:
        from thoth_terminal.cloud_agent_memory_service import (
            list_cloud_agent_memory_segments,
            serialize_cloud_agent_memory_segment,
        )

        rows = list_cloud_agent_memory_segments(
            db,
            agent_key=agent_key,
            status=status,
            query=query,
            segment_type=segment_type,
            cache_scope=cache_scope,
            limit=max(1, min(int(limit or 200), 1000)),
        )
        return JSONResponse(
            {
                "ok": True,
                "count": len(rows),
                "segments": [serialize_cloud_agent_memory_segment(row, include_content=True) for row in rows],
            }
        )
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=200)


@app.post("/api/agents/cloud/memory/segments")
def api_cloud_agent_memory_segment_upsert(payload: dict[str, Any] | None = Body(default=None), db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    body = dict(payload or {})
    try:
        from thoth_terminal.cloud_agent_memory_service import (
            serialize_cloud_agent_memory_segment,
            upsert_cloud_agent_memory_segment,
        )

        row, changed = upsert_cloud_agent_memory_segment(
            db,
            agent_key=body.get("agent_key") or "cloud_research_heartbeat",
            segment_key=body.get("segment_key"),
            segment_type=str(body.get("segment_type") or "working_model"),
            cache_scope=str(body.get("cache_scope") or ""),
            title=str(body.get("title") or ""),
            summary=str(body.get("summary") or "").strip() or None,
            content_md=str(body.get("content_md") or ""),
            priority=int(body.get("priority") or 50),
            importance=int(body.get("importance") or 3),
            entity_refs=body.get("entity_refs"),
            theme_refs=body.get("theme_refs"),
            metadata=body.get("metadata") if isinstance(body.get("metadata"), dict) else {},
            change_summary=str(body.get("change_summary") or "Manual cloud-agent memory upsert.").strip(),
        )
        db.commit()
        return JSONResponse({"ok": True, "changed": changed, "segment": serialize_cloud_agent_memory_segment(row, include_content=True)})
    except Exception as exc:
        db.rollback()
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=200)


@app.post("/api/agents/cloud/memory/segments/archive")
def api_cloud_agent_memory_segment_archive(payload: dict[str, Any] | None = Body(default=None), db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    body = dict(payload or {})
    try:
        from thoth_terminal.cloud_agent_memory_service import archive_cloud_agent_memory_segment

        ok = archive_cloud_agent_memory_segment(
            db,
            agent_key=body.get("agent_key") or "cloud_research_heartbeat",
            segment_key=str(body.get("segment_key") or ""),
            reason=str(body.get("reason") or "Manual cloud-agent memory archive.").strip(),
        )
        db.commit()
        return JSONResponse({"ok": True, "archived": ok})
    except Exception as exc:
        db.rollback()
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=200)


@app.post("/api/agents/cloud/automation/run-now")
def api_cloud_automation_run_now(payload: dict[str, Any] | None = Body(default=None), db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    body = dict(payload or {})
    try:
        from thoth_terminal.cloud_operator_service import enqueue_cloud_automation_run_now

        job = enqueue_cloud_automation_run_now(
            db,
            mode=body.get("mode") or "heartbeat",
            provider=body.get("provider"),
            model=body.get("model"),
            lookback_hours=int(body["lookback_hours"]) if body.get("lookback_hours") else None,
            objective=body.get("objective"),
        )
        db.commit()
        return JSONResponse({"ok": True, "job": job})
    except Exception as exc:
        db.rollback()
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=200)


@app.post("/api/agents/cloud/runs/{run_id}/stop")
def api_cloud_run_stop(run_id: int, payload: dict[str, Any] | None = Body(default=None), db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    body = dict(payload or {})
    try:
        from thoth_terminal.cloud_operator_service import cancel_cloud_run

        run = cancel_cloud_run(db, int(run_id), reason=body.get("reason"))
        db.commit()
        return JSONResponse({"ok": True, "run": run})
    except Exception as exc:
        db.rollback()
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=200)


@app.post("/api/agents/cloud/jobs/{job_id}/retry")
def api_cloud_job_retry(job_id: int, db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    from thoth_terminal.cloud_operator_service import retry_cloud_job

    try:
        job = retry_cloud_job(db, job_id)
        db.commit()
        return JSONResponse({"ok": True, "job": job})
    except ValueError as exc:
        db.rollback()
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)


@app.post("/api/agents/cloud/jobs/{job_id}/stop")
def api_cloud_job_stop(job_id: int, payload: dict[str, Any] | None = Body(default=None), db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    body = dict(payload or {})
    try:
        from thoth_terminal.cloud_operator_service import cancel_cloud_job

        job = cancel_cloud_job(db, int(job_id), reason=body.get("reason"))
        db.commit()
        return JSONResponse({"ok": True, "job": job})
    except Exception as exc:
        db.rollback()
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=200)


def _bool_payload(value: object, default: bool) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


@app.post("/api/agents/cloud/settings")
def api_cloud_automation_settings_update(payload: dict[str, Any] | None = Body(default=None)) -> JSONResponse:
    _guard_internal_terminal()
    body = dict(payload or {})
    try:
        enabled = _bool_payload(body.get("enabled"), bool(getattr(settings, "cloud_research_automation_enabled", True)))
        provider = str(body.get("provider") or getattr(settings, "cloud_research_provider", "openrouter") or "openrouter").strip()
        model = str(body.get("model") or getattr(settings, "cloud_research_model", "openrouter/free") or "openrouter/free").strip()
        if provider.lower() == "deepseek":
            model = "deepseek-v4-flash"
        elif provider.lower() == "meta":
            model = "muse-spark-1.3-contributor"
        nightly = body.get("nightly") if isinstance(body.get("nightly"), dict) else {}
        heartbeat = body.get("heartbeat") if isinstance(body.get("heartbeat"), dict) else {}
        inquisitive = body.get("inquisitive") if isinstance(body.get("inquisitive"), dict) else {}
        memory = body.get("memory") if isinstance(body.get("memory"), dict) else {}

        def schedule_times(mode: dict[str, Any], setting_name: str, default: str) -> str:
            # An explicit blank selects the heartbeat interval fallback. Omitted
            # fields preserve the current cadence, including an existing blank.
            if "local_times" not in mode:
                return str(getattr(settings, setting_name, default)).strip()
            raw = str(mode["local_times"] or "").strip()
            from thoth_terminal.cloud_research_automation import _parse_local_times

            if raw and any(
                not re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2})?", part.strip())
                or not _parse_local_times(part)
                for part in raw.split(",")
            ):
                raise ValueError("Schedule times must be comma-separated HH:MM or HH:MM:SS values.")
            return raw

        updates = {
            "THOTH_CLOUD_RESEARCH_AUTOMATION_ENABLED": "true" if enabled else "false",
            "THOTH_CLOUD_RESEARCH_PROVIDER": provider,
            "THOTH_CLOUD_RESEARCH_MODEL": model,
            "THOTH_CLOUD_RESEARCH_NIGHTLY_ENABLED": "true" if _bool_payload(nightly.get("enabled"), bool(getattr(settings, "cloud_research_nightly_enabled", False))) else "false",
            "THOTH_CLOUD_RESEARCH_NIGHTLY_LOCAL_TIMES": schedule_times(nightly, "cloud_research_nightly_local_times", "12:10"),
            "THOTH_CLOUD_RESEARCH_NIGHTLY_LOOKBACK_HOURS": str(max(1, min(int(nightly.get("lookback_hours") or getattr(settings, "cloud_research_nightly_lookback_hours", 72) or 72), 720))),
            "THOTH_CLOUD_RESEARCH_HEARTBEAT_ENABLED": "true" if _bool_payload(heartbeat.get("enabled"), bool(getattr(settings, "cloud_research_heartbeat_enabled", True))) else "false",
            "THOTH_CLOUD_RESEARCH_HEARTBEAT_LOCAL_TIMES": schedule_times(heartbeat, "cloud_research_heartbeat_local_times", "08:00,20:00"),
            "THOTH_CLOUD_RESEARCH_HEARTBEAT_INTERVAL_HOURS": str(max(1, min(int(heartbeat.get("interval_hours") or getattr(settings, "cloud_research_heartbeat_interval_hours", 12) or 12), 168))),
            "THOTH_CLOUD_RESEARCH_HEARTBEAT_LOOKBACK_HOURS": str(max(1, min(int(heartbeat.get("lookback_hours") or getattr(settings, "cloud_research_heartbeat_lookback_hours", 168) or 168), 720))),
            "THOTH_CLOUD_RESEARCH_INQUISITIVE_ENABLED": "true" if _bool_payload(inquisitive.get("enabled"), bool(getattr(settings, "cloud_research_inquisitive_enabled", False))) else "false",
            "THOTH_CLOUD_RESEARCH_INQUISITIVE_LOCAL_TIMES": schedule_times(inquisitive, "cloud_research_inquisitive_local_times", "12:45,18:30"),
            "THOTH_CLOUD_RESEARCH_INQUISITIVE_LOOKBACK_HOURS": str(max(1, min(int(inquisitive.get("lookback_hours") or getattr(settings, "cloud_research_inquisitive_lookback_hours", 120) or 120), 720))),
            "THOTH_CLOUD_AGENT_MEMORY_ENABLED": "true" if _bool_payload(memory.get("enabled"), bool(getattr(settings, "cloud_agent_memory_enabled", True))) else "false",
            "THOTH_CLOUD_AGENT_MEMORY_CONTEXT_CHARS": str(max(16000, min(int(memory.get("context_chars") or getattr(settings, "cloud_agent_memory_context_chars", 420000) or 420000), 800000))),
        }
        _upsert_env_values(updates)
        settings.cloud_research_automation_enabled = enabled
        settings.cloud_research_provider = provider
        settings.cloud_research_model = model
        settings.cloud_research_nightly_enabled = updates["THOTH_CLOUD_RESEARCH_NIGHTLY_ENABLED"] == "true"
        settings.cloud_research_nightly_local_times = updates["THOTH_CLOUD_RESEARCH_NIGHTLY_LOCAL_TIMES"]
        settings.cloud_research_nightly_lookback_hours = int(updates["THOTH_CLOUD_RESEARCH_NIGHTLY_LOOKBACK_HOURS"])
        settings.cloud_research_heartbeat_enabled = updates["THOTH_CLOUD_RESEARCH_HEARTBEAT_ENABLED"] == "true"
        settings.cloud_research_heartbeat_local_times = updates["THOTH_CLOUD_RESEARCH_HEARTBEAT_LOCAL_TIMES"]
        settings.cloud_research_heartbeat_interval_hours = int(updates["THOTH_CLOUD_RESEARCH_HEARTBEAT_INTERVAL_HOURS"])
        settings.cloud_research_heartbeat_lookback_hours = int(updates["THOTH_CLOUD_RESEARCH_HEARTBEAT_LOOKBACK_HOURS"])
        settings.cloud_research_inquisitive_enabled = updates["THOTH_CLOUD_RESEARCH_INQUISITIVE_ENABLED"] == "true"
        settings.cloud_research_inquisitive_local_times = updates["THOTH_CLOUD_RESEARCH_INQUISITIVE_LOCAL_TIMES"]
        settings.cloud_research_inquisitive_lookback_hours = int(updates["THOTH_CLOUD_RESEARCH_INQUISITIVE_LOOKBACK_HOURS"])
        settings.cloud_agent_memory_enabled = updates["THOTH_CLOUD_AGENT_MEMORY_ENABLED"] == "true"
        settings.cloud_agent_memory_context_chars = int(updates["THOTH_CLOUD_AGENT_MEMORY_CONTEXT_CHARS"])
        from thoth_terminal.cloud_operator_service import cloud_automation_settings_payload

        return JSONResponse({"ok": True, "settings": cloud_automation_settings_payload()})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=200)


def _cloud_catalog_model(catalog: dict[str, Any], *, provider: str, model: str) -> dict[str, Any] | None:
    for row in catalog.get("rows") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("provider") or "") == provider and str(row.get("model") or "") == model:
            return row
    return None


@app.post("/api/agents/cloud/chat")
def api_cloud_agent_chat(payload: dict[str, object] | None = Body(default=None), db: Session = Depends(get_session)) -> JSONResponse:
    body = dict(payload or {})
    prompt = str(body.get("message") or "").strip()
    if not prompt:
        return JSONResponse({"ok": False, "error": "Message is required."}, status_code=400)
    provider = str(body.get("provider") or getattr(settings, "cloud_research_provider", "openrouter") or "openrouter").strip().lower()
    if provider not in {"openrouter", "deepseek", "nous", "meta"}:
        return JSONResponse({"ok": False, "error": "provider must be openrouter, deepseek, nous, or meta"}, status_code=400)
    default_model = {
        "openrouter": getattr(settings, "openrouter_model", "openrouter/free"),
        "deepseek": getattr(settings, "deepseek_model", "deepseek-v4-flash"),
        "nous": getattr(settings, "nous_model", "Hermes-4.3-36B"),
        "meta": getattr(settings, "meta_model", "muse-spark-1.3-contributor"),
    }.get(provider)
    if provider == str(getattr(settings, "cloud_research_provider", "") or "").strip().lower():
        default_model = getattr(settings, "cloud_research_model", None) or default_model
    model = str(body.get("model") or default_model or "").strip()
    if not model:
        return JSONResponse({"ok": False, "error": "Model is required."}, status_code=400)
    catalog = _cloud_model_catalog_payload(cached=True)
    catalog_row = _cloud_catalog_model(catalog, provider=provider, model=model)
    if catalog_row is not None and not bool(catalog_row.get("text_output", True)):
        return JSONResponse({"ok": False, "error": "Cloud analyst chat requires a text-output model."}, status_code=400)
    free_only = bool(body.get("free_only", True))
    if provider == "openrouter" and free_only:
        if catalog_row is not None and not bool(catalog_row.get("free")):
            return JSONResponse({"ok": False, "error": "OpenRouter free-only mode rejected a paid model."}, status_code=400)
        if catalog_row is None and model != "openrouter/free" and not model.endswith(":free"):
            return JSONResponse({"ok": False, "error": "OpenRouter free-only mode requires a free model."}, status_code=400)
    try:
        from thoth_terminal.agent.context_bundle import build_recent_context_bundle, render_recent_context_markdown
        from thoth_terminal.cloud_agent_memory_service import (
            CLOUD_RESEARCH_HEARTBEAT_AGENT_KEY,
            render_cloud_agent_memory_cache_sections,
        )
        from thoth_terminal.local_agent_service import daily_summary_context

        requested_skill_slugs = [str(value) for value in body.get("skill_slugs") or []] if isinstance(body.get("skill_slugs"), list) else []
        matched_skills = resolve_cloud_skills(db, prompt=prompt, explicit_slugs=requested_skill_slugs)
        static_protocol_md, static_protocol_manifest = compile_static_cloud_skill_protocol(db)
        skill_runtime = cloud_skill_runtime_payload(db, active=matched_skills)
        skill_runtime["protocol"] = static_protocol_manifest
        skill_runtime["matched_slugs"] = [row.slug for row in matched_skills]

        lookback_hours = max(1, min(int(body.get("lookback_hours") or getattr(settings, "cloud_research_heartbeat_lookback_hours", 168) or 168), 720))
        recent_bundle: dict[str, object] = {"counts": {}}
        local_context: dict[str, object] = {}
        recent_md = ""
        local_md = ""
        stable_memory_md = ""
        volatile_memory_md = ""
        memory_meta: dict[str, object] = {"enabled": False}
        stable_state_enabled = bool(getattr(settings, "cloud_research_stable_state_enabled", True))
        stable_snapshot = None
        stable_state_created = False
        stable_delta_md = ""
        stable_delta_meta: dict[str, object] = {}
        if stable_state_enabled:
            stable_snapshot, stable_state_created = get_or_create_cloud_prompt_state_snapshot(
                db,
                agent_key=CLOUD_RESEARCH_HEARTBEAT_AGENT_KEY,
            )
            stable_delta_md, stable_delta_meta = render_cloud_prompt_state_delta(db, stable_snapshot)
            memory_meta = {
                "enabled": bool(getattr(settings, "cloud_agent_memory_enabled", True)),
                "frozen_in_stable_state": True,
                "snapshot_id": int(stable_snapshot.id),
                "snapshot_created": stable_state_created,
                "delta": stable_delta_meta,
            }
        elif bool(getattr(settings, "cloud_agent_memory_enabled", True)):
            stable_memory_md, volatile_memory_md, memory_meta = render_cloud_agent_memory_cache_sections(
                db,
                agent_key=CLOUD_RESEARCH_HEARTBEAT_AGENT_KEY,
                max_chars=None,
                mark_accessed=True,
            )
            memory_meta["enabled"] = True

        volatile_state_enabled = bool(getattr(settings, "cloud_research_volatile_state_enabled", True))
        volatile_snapshot = None
        volatile_state_created = False
        volatile_delta_md = ""
        volatile_delta_meta: dict[str, object] = {}
        if volatile_state_enabled:
            volatile_snapshot, volatile_state_created = get_or_create_cloud_prompt_volatile_snapshot(db)
            volatile_delta_md, volatile_delta_meta = render_cloud_prompt_volatile_delta(db, volatile_snapshot)
            recent_bundle = {"counts": dict((volatile_snapshot.manifest_json or {}).get("counts") or {})}
        else:
            recent_bundle = build_recent_context_bundle(
                db,
                lookback_hours=lookback_hours,
                max_items_per_section=35,
                max_items_per_source=8,
                include_full_text=False,
            )
            recent_md = render_recent_context_markdown(recent_bundle, max_chars=None)
            local_context = daily_summary_context(db, hours=lookback_hours, limit=300)
            local_md = json.dumps(local_context, ensure_ascii=False, indent=2)

        context_pack_version = "cloud-chat-context-v4"
        context_parts = [static_protocol_md]
        if stable_snapshot is not None:
            context_parts.append(stable_snapshot.content_md)
        else:
            context_parts.extend(
                [
                    "# Stable Heartbeat Memory\n\n" + (stable_memory_md or "No stable heartbeat memory rendered."),
                    "# Volatile Heartbeat Memory\n\n" + (volatile_memory_md or "No volatile heartbeat memory rendered."),
                ]
            )
        if volatile_snapshot is not None:
            context_parts.append(volatile_snapshot.content_md)
        context_parts.extend([stable_delta_md, volatile_delta_md])
        if recent_md or local_md:
            context_parts.append(
                "# Recent Terminal Context\n\n"
                f"{recent_md}\n\n"
                "# Local Agent Work Context\n\n"
                "```json\n"
                f"{local_md}\n"
                "```"
            )
        context_parts.append(
            "# Task Delta\n\n"
            + json.dumps(
                {"mode": "interactive", "objective": prompt, "requested_lookback_hours": lookback_hours},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        user_content = "\n\n".join(part for part in context_parts if part)
        from thoth_terminal.agent_request_manifest_service import manifested_chat_json

        result = manifested_chat_json(
            db,
            run_kind="interactive_cloud_chat",
            run_id=None,
            task_kind="cloud_agent_chat",
            messages=[
                ChatMessage(role="system", content=CLOUD_RESEARCH_AUTOMATION_SYSTEM),
                ChatMessage(role="user", content=user_content),
            ],
            provider=provider,
            model=model,
            timeout_s=max(60.0, float(getattr(settings, "cloud_research_timeout_s", 1200) or 1200)),
            max_input_chars=None,
            max_output_tokens=None,
            prompt_cache_key=CLOUD_RESEARCH_PROMPT_CACHE_KEY,
            permission_profile="interactive-cloud-model-no-direct-tools",
            source_refs={
                "stable_snapshot_id": int(stable_snapshot.id) if stable_snapshot is not None else None,
                "volatile_snapshot_id": int(volatile_snapshot.id) if volatile_snapshot is not None else None,
            },
        )
        meta = llm_metadata(result)
        routed_model = meta.get("model") or model
        submitted_model = meta.get("submitted_model") or model
        usage = {
            key: int(meta[key])
            for key in ("input_tokens", "output_tokens", "total_tokens", "reasoning_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens")
            if str(meta.get(key) or "").isdigit()
        }
        hit = int(usage.get("prompt_cache_hit_tokens") or 0)
        miss = int(usage.get("prompt_cache_miss_tokens") or 0)
        if hit + miss > 0:
            usage["cache_hit_rate"] = round(hit / (hit + miss), 6)
        return JSONResponse(
            {
                "ok": True,
                "provider": provider,
                "model": model,
                "requested_model": model,
                "submitted_model": submitted_model,
                "routed_model": routed_model,
                "usage": usage,
                "pricing": catalog_row,
                "context": {
                    "lookback_hours": lookback_hours,
                    "recent_counts": dict(recent_bundle.get("counts") or {}),
                    "local_context_counts": {key: len(value) for key, value in local_context.items() if isinstance(value, list)},
                    "cloud_agent_memory": memory_meta,
                    "skills": skill_runtime,
                    "stable_state": {
                        **dict(stable_snapshot.manifest_json or {}),
                        "snapshot_id": int(stable_snapshot.id),
                        "snapshot_key": stable_snapshot.snapshot_key,
                        "epoch_start": stable_snapshot.epoch_start.isoformat(),
                        "epoch_end": stable_snapshot.epoch_end.isoformat(),
                        "created": stable_state_created,
                        "delta": stable_delta_meta,
                    } if stable_snapshot is not None else {"enabled": False},
                    "volatile_state": {
                        **dict(volatile_snapshot.manifest_json or {}),
                        "snapshot_id": int(volatile_snapshot.id),
                        "snapshot_key": volatile_snapshot.snapshot_key,
                        "epoch_start": volatile_snapshot.epoch_start.isoformat(),
                        "epoch_end": volatile_snapshot.epoch_end.isoformat(),
                        "created": volatile_state_created,
                        "delta": volatile_delta_meta,
                    } if volatile_snapshot is not None else {"enabled": False},
                    "context_pack_version": context_pack_version,
                    "cache_stable_prefix_chars": (
                        user_content.find("</thoth_volatile_state>") + len("</thoth_volatile_state>")
                        if volatile_snapshot is not None
                        else user_content.find("</thoth_stable_state>") + len("</thoth_stable_state>")
                        if stable_snapshot is not None
                        else len(static_protocol_md)
                    ),
                    "stable_prefix_chars": user_content.find("# Task Delta"),
                },
                "result": result,
            }
        )
    except ValueError as exc:
        return JSONResponse({"ok": False, "provider": provider, "model": model, "error": str(exc)}, status_code=400)
    except AgentLlmError as exc:
        return JSONResponse({"ok": False, "provider": provider, "model": model, "error": str(exc)}, status_code=200)


@app.post("/api/settings/google/test")
def api_settings_google_test(service: str = Form(...)) -> JSONResponse:
    svc = (service or "").strip().lower()
    try:
        from thoth_terminal.google_oauth import drive_test as google_drive_test
        from thoth_terminal.google_oauth import gmail_test as google_gmail_test

        if svc == "gmail":
            return JSONResponse({"ok": True, "service": "gmail", "result": google_gmail_test()})
        if svc == "drive":
            return JSONResponse({"ok": True, "service": "drive", "result": google_drive_test()})
        return JSONResponse({"ok": False, "error": "service must be gmail|drive"}, status_code=400)
    except Exception as exc:
        return JSONResponse({"ok": False, "service": svc, "error": f"{type(exc).__name__}: {exc}"}, status_code=200)


@app.get("/api/desktop/status")
def api_desktop_status(request: Request) -> JSONResponse:
    payload = _desktop_status_payload(request.app)
    if payload is None:
        return JSONResponse({"ok": False, "error": "Desktop shell is not attached."}, status_code=404)
    sign_in = payload.get("ibkr_sign_in") or {}
    confirmation = sign_in.get("signed_in_at_utc")
    if sign_in.get("state") == "signed_in" and isinstance(confirmation, str) and confirmation:
        ibkr_service().refresh_after_desktop_sign_in(confirmation)
    return JSONResponse({"ok": True, "desktop": payload})


@app.post("/api/desktop/preferences")
def api_desktop_preferences_update(
    request: Request,
    payload: dict[str, object] | None = Body(default=None),
) -> JSONResponse:
    if "ibkr_sign_in_on_launch" in (payload or {}) and not _desktop_ibkr_request_allowed(request):
        return JSONResponse({"ok": False, "error": "IBKR sign-in controls require the local desktop origin."}, status_code=403)
    bridge = _desktop_bridge(request.app)
    updater = getattr(bridge, "update_preferences", None) if bridge is not None else None
    if not callable(updater):
        return JSONResponse({"ok": False, "error": "Desktop shell is not attached."}, status_code=404)
    try:
        result = updater(dict(payload or {}))
        return JSONResponse({"ok": True, "desktop": result})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=400)


@app.post("/api/desktop/action")
def api_desktop_action(
    request: Request,
    payload: dict[str, object] | None = Body(default=None),
) -> JSONResponse:
    bridge = _desktop_bridge(request.app)
    action_fn = getattr(bridge, "perform_action", None) if bridge is not None else None
    if not callable(action_fn):
        return JSONResponse({"ok": False, "error": "Desktop shell is not attached."}, status_code=404)
    action = str((payload or {}).get("action") or "").strip().lower()
    if not action:
        return JSONResponse({"ok": False, "error": "Missing desktop action."}, status_code=400)
    if action in {"manage_ibkr_credentials", "sign_in_ibkr", "open_ibkr_auth"} and not _desktop_ibkr_request_allowed(request):
        return JSONResponse({"ok": False, "error": "IBKR sign-in controls require the local desktop origin."}, status_code=403)
    try:
        result = action_fn(action)
        return JSONResponse({"ok": True, "desktop": result})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=400)


def _desktop_ibkr_request_allowed(request: Request) -> bool:
    """Require a local peer and origin; a tunnel's loopback hop is insufficient."""
    if not request.client or not is_loopback_host(request.client.host):
        return False
    if any(request.headers.get(name) for name in (
        "forwarded", "x-forwarded-host", "x-forwarded-for", "x-real-ip", "cf-connecting-ip",
    )):
        return False
    if request.headers.get("sec-fetch-site") == "cross-site":
        return False
    try:
        host = urlsplit("http://" + request.headers.get("host", ""))
        if host.username is not None or host.path or host.query or host.fragment or not is_loopback_host(host.hostname):
            return False
        host_port = host.port or (443 if request.url.scheme == "https" else 80)
        origin = request.headers.get("origin")
        if origin:
            parsed = urlsplit(origin)
            if (parsed.scheme != request.url.scheme or parsed.hostname != host.hostname
                    or parsed.username is not None or parsed.path or parsed.query or parsed.fragment
                    or (parsed.port or (443 if parsed.scheme == "https" else 80)) != host_port):
                return False
    except ValueError:
        return False
    return True


@app.post("/settings/agent/compress")
def settings_agent_compress_run(
    mode: str = Form("daily_master"),
    lookback_hours: int = Form(24),
    focus_query: str | None = Form(None),
    post_to_outbox: bool = Form(False),
) -> RedirectResponse:
    guild_id = int(getattr(settings, "discord_guild_id", 0) or 0)
    if not guild_id:
        return RedirectResponse(url="/settings?google_error=Missing+THOTH_DISCORD_GUILD_ID", status_code=303)
    _, eff_cfg = _agent_settings_for_guild(guild_id)
    try:
        from thoth_terminal.discord import agent_jobs
        from thoth_terminal.discord.desk import get_desk_settings

        post_channel_id = None
        post_thread_id = None
        if post_to_outbox:
            desk_cfg = get_desk_settings(guild_id=guild_id)
            if not desk_cfg.desk_channel_id:
                return RedirectResponse(url="/settings?google_error=Desk+channel+not+configured", status_code=303)
            post_channel_id = int(desk_cfg.desk_channel_id)
            post_thread_id = int(desk_cfg.desk_thread_id) if desk_cfg.desk_thread_id else None

        agent_jobs.enqueue_context_compression(
            guild_id=guild_id,
            mode=(mode or "daily_master"),
            lookback_hours=int(lookback_hours or eff_cfg.get("context_default_lookback_hours") or 24),
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
            dedupe_key=f"settings_agent_compress:{guild_id}:{int(time.time())}",
        )
        return RedirectResponse(url="/settings?google_notice=Compression+job+enqueued", status_code=303)
    except Exception as exc:
        return _settings_redirect_with_notice(error=str(exc)[:240])
