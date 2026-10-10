from __future__ import annotations

from thoth_terminal.web.app_sections._shared import bind_app_context


bind_app_context(globals())


def _research_latest_run(session: Session, *, play_id: int) -> ResearchPlayRun | None:
    return (
        session.query(ResearchPlayRun)
        .filter(ResearchPlayRun.play_id == int(play_id))
        .order_by(ResearchPlayRun.started_at.desc(), ResearchPlayRun.id.desc())
        .first()
    )


@app.get("/research/plays")
def research_plays_view(
    request: Request,
    db: Session = Depends(get_session),
) -> HTMLResponse:
    query = str(request.query_params.get("query") or "").strip() or None
    owner = str(request.query_params.get("owner") or "").strip() or None
    notice = str(request.query_params.get("notice") or "").strip() or None
    error = str(request.query_params.get("error") or "").strip() or None
    plays = list_research_plays(db, query=query, owner=owner, limit=100)
    skills = list_research_skills(db, query=query, owner=owner, limit=200)
    play_rows = [
        serialize_research_play(play, include_skills=True, latest_run=_research_latest_run(db, play_id=int(play.id)))
        for play in plays
    ]
    skill_rows = [serialize_research_skill(skill) for skill in skills]
    return _template_response(
        "research_plays.html",
        {
            "request": request,
            "title": "Research Plays",
            "notice": notice,
            "error": error,
            "query": query or "",
            "owner": owner or "",
            "plays": play_rows,
            "skills": skill_rows,
        },
    )


@app.get("/research/plays/{play_id}")
def research_play_detail_view(
    request: Request,
    play_id: int,
    db: Session = Depends(get_session),
) -> HTMLResponse:
    play = get_research_play(db, int(play_id))
    if play is None:
        raise HTTPException(status_code=404, detail="Research play not found.")
    runs = list_recent_play_runs(db, play_id=int(play.id), limit=20)
    signals = list_recent_play_signals(db, play_id=int(play.id), limit=50)
    signal_ids = [int(row.id) for row in signals]
    feedback_rows = (
        db.query(AlignmentFeedback)
        .filter(AlignmentFeedback.target_type == "play_signal")
        .filter(AlignmentFeedback.target_id.in_(signal_ids))
        .order_by(AlignmentFeedback.created_at.desc(), AlignmentFeedback.id.desc())
        .all()
        if signal_ids
        else []
    )
    feedback_by_signal: dict[int, list[dict[str, object]]] = {}
    for row in feedback_rows:
        if row.target_id is None:
            continue
        feedback_by_signal.setdefault(int(row.target_id), []).append(serialize_alignment_feedback(row))
    skill_rows = [serialize_research_skill(skill) for skill in list_research_skills(db, limit=200)]
    return _template_response(
        "research_play_detail.html",
        {
            "request": request,
            "title": f"Research Play · {play.name}",
            "notice": str(request.query_params.get("notice") or "").strip() or None,
            "error": str(request.query_params.get("error") or "").strip() or None,
            "play": serialize_research_play(play, include_skills=True, latest_run=(runs[0] if runs else None)),
            "runs": [serialize_research_play_run(run) for run in runs],
            "signals": [serialize_research_play_signal(signal) for signal in signals],
            "feedback_by_signal": feedback_by_signal,
            "available_skills": skill_rows,
        },
    )


@app.post("/research/skills")
def research_skill_create(
    name: str = Form(...),
    slug: str = Form(...),
    owner: str | None = Form(None),
    description: str | None = Form(None),
    query: str | None = Form(None),
    domains: str | None = Form(None),
    source_ids: str | None = Form(None),
    lookback_hours: int = Form(24),
    signal_kind: str = Form("document_match"),
    max_docs: int = Form(8),
    instructions: str | None = Form(None),
    commands: str | None = Form(None),
    keywords: str | None = Form(None),
    modes: str | None = Form(None),
    implicit: bool = Form(False),
) -> RedirectResponse:
    name_v = str(name or "").strip()
    slug_v = str(slug or "").strip()
    if not name_v or not slug_v:
        return _research_redirect("/research/plays", error="Skill name and slug are required.")
    try:
        with session_scope() as session:
            skill = create_research_skill(
                session,
                slug=slug_v,
                name=name_v,
                owner=_clean_optional_text(owner),
                description=_clean_optional_text(description),
                universe_json={
                    "domains": _split_list(domains),
                    "source_ids": _split_int_list(source_ids),
                },
                inputs_json={"query": _clean_optional_text(query)} if _clean_optional_text(query) else {},
                trigger_json={
                    "lookback_hours": max(1, int(lookback_hours or 24)),
                    "signal_kind": str(signal_kind or "document_match").strip() or "document_match",
                    "commands": _split_list(commands),
                    "keywords": _split_list(keywords),
                    "modes": _split_list(modes),
                    "implicit": bool(implicit),
                },
                output_json={"max_docs": max(1, min(int(max_docs or 8), 50))},
                extra={
                    "instructions_md": _clean_optional_text(instructions) or "",
                    "runtime": {"cloud_enabled": True, "progressive_disclosure": True},
                },
            )
            return _research_redirect("/research/plays", notice=f"Research skill saved #{int(skill.id)}.")
    except Exception as exc:
        return _research_redirect("/research/plays", error=f"{type(exc).__name__}: {exc}")


@app.post("/research/plays")
def research_play_create(
    name: str = Form(...),
    slug: str = Form(...),
    owner: str | None = Form(None),
    description: str | None = Form(None),
    query: str | None = Form(None),
    domains: str | None = Form(None),
    source_ids: str | None = Form(None),
    interval_minutes: int = Form(60),
    lookback_hours: int = Form(24),
    alert_guild_id: int | None = Form(None),
    alert_channel_id: int | None = Form(None),
    alert_thread_id: int | None = Form(None),
) -> RedirectResponse:
    name_v = str(name or "").strip()
    slug_v = str(slug or "").strip()
    if not name_v or not slug_v:
        return _research_redirect("/research/plays", error="Play name and slug are required.")
    try:
        with session_scope() as session:
            play = create_research_play(
                session,
                slug=slug_v,
                name=name_v,
                owner=_clean_optional_text(owner),
                description=_clean_optional_text(description),
                universe_json={
                    "query": _clean_optional_text(query),
                    "domains": _split_list(domains),
                    "source_ids": _split_int_list(source_ids),
                },
                schedule_json={
                    "enabled": bool(int(interval_minutes or 0) > 0),
                    "interval_minutes": max(0, int(interval_minutes or 0)),
                    "lookback_hours": max(1, int(lookback_hours or 24)),
                },
                alert_json={
                    "guild_id": int(alert_guild_id) if alert_guild_id else None,
                    "channel_id": int(alert_channel_id) if alert_channel_id else None,
                    "thread_id": int(alert_thread_id) if alert_thread_id else None,
                },
            )
            return _research_redirect(f"/research/plays/{int(play.id)}", notice="Research play saved.")
    except Exception as exc:
        return _research_redirect("/research/plays", error=f"{type(exc).__name__}: {exc}")


@app.post("/research/plays/{play_id}/skills")
def research_play_bind_skill_view(
    play_id: int,
    skill_id: int = Form(...),
    position: int = Form(0),
    required: bool = Form(True),
) -> RedirectResponse:
    try:
        with session_scope() as session:
            play = get_research_play(session, int(play_id))
            skill = get_research_skill(session, int(skill_id))
            if play is None or skill is None:
                return _research_redirect(f"/research/plays/{int(play_id)}", error="Play or skill not found.")
            bind_skill_to_play(
                session,
                play_id=int(play.id),
                skill_id=int(skill.id),
                position=int(position or 0),
                required=bool(required),
            )
        return _research_redirect(f"/research/plays/{int(play_id)}", notice="Skill bound to play.")
    except Exception as exc:
        return _research_redirect(f"/research/plays/{int(play_id)}", error=f"{type(exc).__name__}: {exc}")


@app.post("/research/plays/{play_id}/run")
def research_play_run_view(
    play_id: int,
    sync: bool = Form(False),
) -> RedirectResponse:
    try:
        with session_scope() as session:
            play = get_research_play(session, int(play_id))
            if play is None:
                return _research_redirect("/research/plays", error="Research play not found.")
            if bool(sync):
                result = execute_research_play(session, play_id=int(play.id), trigger_kind="manual")
                run = result.get("run") if isinstance(result, dict) else {}
                signal_count = len(result.get("signals") or []) if isinstance(result, dict) else 0
                run_id = int((run or {}).get("id") or 0)
                return _research_redirect(
                    f"/research/plays/{int(play.id)}",
                    notice=f"Play executed inline as run #{run_id} with {signal_count} signal(s).",
                )
            job = enqueue_research_play_job(
                session,
                play_id=int(play.id),
                trigger_kind="manual",
                dedupe_key=f"research_play:manual:{int(play.id)}:{int(time.time())}",
            )
        return _research_redirect(f"/research/plays/{int(play_id)}", notice=f"Play run job enqueued #{int(job.id)}.")
    except Exception as exc:
        return _research_redirect(f"/research/plays/{int(play_id)}", error=f"{type(exc).__name__}: {exc}")


@app.post("/research/plays/{play_id}/feedback")
def research_play_feedback_view(
    play_id: int,
    signal_id: int = Form(...),
    feedback_kind: str = Form(...),
    actor: str | None = Form(None),
    note: str | None = Form(None),
) -> RedirectResponse:
    try:
        with session_scope() as session:
            signal = (
                session.query(ResearchPlaySignal)
                .filter(ResearchPlaySignal.play_id == int(play_id))
                .filter(ResearchPlaySignal.id == int(signal_id))
                .first()
            )
            if signal is None:
                return _research_redirect(f"/research/plays/{int(play_id)}", error="Signal not found.")
            record_alignment_feedback(
                session,
                target_type="play_signal",
                target_id=int(signal.id),
                feedback_kind=str(feedback_kind or "").strip() or "comment",
                actor=_clean_optional_text(actor),
                note=_clean_optional_text(note),
            )
        return _research_redirect(f"/research/plays/{int(play_id)}", notice="Alignment feedback recorded.")
    except Exception as exc:
        return _research_redirect(f"/research/plays/{int(play_id)}", error=f"{type(exc).__name__}: {exc}")


def _resolve_play_identifier(params: dict[str, object]) -> int:
    play_id = params.get("play_id")
    raw_id = params.get("id")
    slug = str(params.get("slug") or "").strip()
    candidate = play_id if play_id is not None else raw_id
    if candidate is not None and str(candidate).isdigit():
        return int(candidate)
    if slug:
        session = _session_read()
        try:
            play = get_research_play(session, slug)
            if play is None:
                raise ValueError("Research play not found.")
            return int(play.id)
        finally:
            session.close()
    raise ValueError("play_id or slug is required.")


@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_session)) -> HTMLResponse:
    if getattr(request.state, "public_surface", False) or build_security_posture(settings).public:
        return _template_response(
            "public_landing.html",
            {
                "request": request,
                "title": "Thoth Terminal",
                "body_class": "public-mode",
            },
        )
    intelligence = _HOME_INTELLIGENCE_CACHE.get_or_set(
        f"home:{_cache_scope()}:intelligence",
        lambda: build_home_intelligence(db),
        ttl_s=_HOME_INTELLIGENCE_CACHE_TTL_S,
    )
    return _template_response(
        "home.html",
        {
            "request": request,
            "title": "Thoth Terminal",
            "body_class": "home-cockpit",
            "intelligence": intelligence,
        },
    )


@app.get("/ibkr/auth", response_class=HTMLResponse)
def ibkr_auth_page(request: Request) -> HTMLResponse:
    if build_security_posture(settings).public:
        return _template_response(
            "public_landing.html",
            {"request": request, "title": "Thoth Terminal", "body_class": "public-mode"},
        )
    gateway_start_status: dict[str, object] | None = None
    if bool(getattr(settings, "enable_ibkr", False)):
        try:
            gateway_start_status = launch_gateway_if_needed()
        except Exception as exc:
            gateway_start_status = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return _template_response(
        "ibkr_auth.html",
        {
            "request": request,
            "title": "Authenticate IBKR",
            "body_class": "ibkr-auth",
            "gateway_login_url": gateway_browser_login_url(),
            "gateway_start_status": gateway_start_status,
        },
    )


def _guard_internal_terminal() -> None:
    if build_security_posture(settings).public:
        raise HTTPException(status_code=404, detail="Not found")
