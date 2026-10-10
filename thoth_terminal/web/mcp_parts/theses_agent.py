from __future__ import annotations

from thoth_terminal.web.mcp_parts._shared import bind_mcp_context


bind_mcp_context(globals())


def _mcp_tool_create_thesis(params: dict[str, object]) -> dict[str, object]:
    title = str(params.get("title") or "").strip()
    statement = str(params.get("statement") or "").strip()
    if not title or not statement:
        raise ValueError("title and statement are required.")
    entities = params.get("entities")
    if not isinstance(entities, list) or not entities:
        raise ValueError("entities is required and must be a list.")
    kind = normalize_research_signal_kind(params.get("kind") or "claim")
    status = str(params.get("status") or "watching")
    importance = params.get("importance")
    importance_val = int(importance) if str(importance).isdigit() else 3
    stale_after_days = params.get("stale_after_days")
    stale_val = int(stale_after_days) if str(stale_after_days).isdigit() else None
    domains = params.get("domains") if isinstance(params.get("domains"), list) else None
    tags = params.get("tags") if isinstance(params.get("tags"), list) else None
    event_types = params.get("event_types") if isinstance(params.get("event_types"), list) else None
    client_request_id = str(params.get("client_request_id") or "").strip() or None

    def _create() -> dict[str, object]:
        with session_scope() as session:
            th = create_thesis(
                session,
                title=title,
                statement=statement,
                kind=kind,
                status=status,
                importance=importance_val,
                stale_after_days=stale_val,
                entity_tokens=[str(e) for e in entities],
                domain_slugs=[str(d) for d in (domains or [])],
                tag_slugs=[str(t) for t in (tags or [])],
                event_type_slugs=[str(e) for e in (event_types or [])],
                client_request_id=client_request_id,
            )
            return {
                "id": th.id,
                "title": th.title,
                "kind": normalize_research_signal_kind(th.kind),
                "kind_profile": thesis_kind_profile(th.kind),
                "status": th.status,
                "importance": th.importance,
                "updated_at": th.updated_at.isoformat(),
            }

    return _with_db_retry(_create)


def _mcp_tool_list_house_view_theses(params: dict[str, object]) -> dict[str, object]:
    subject_id = params.get("subject_id")
    subject_id_val = int(subject_id) if str(subject_id or "").isdigit() else None
    limit = params.get("limit")
    limit_val = int(limit) if str(limit or "").isdigit() else 50
    include_inactive = _boolish_value(params.get("include_inactive"), default=False)
    from thoth_terminal.house_view_service import list_house_view_theses

    session = _session_read()
    try:
        rows = list_house_view_theses(
            session,
            subject_id=subject_id_val,
            include_inactive=include_inactive,
            limit=limit_val,
        )
        return {"count": len(rows), "house_views": rows}
    finally:
        session.close()


def _mcp_tool_house_view_context(params: dict[str, object]) -> dict[str, object]:
    raw_subject = params.get("subject_id")
    if raw_subject is None or not str(raw_subject).isdigit():
        raise ValueError("subject_id is required.")
    limit = params.get("limit")
    limit_val = int(limit) if str(limit or "").isdigit() else 8
    mode = str(params.get("mode") or "both").strip() or "both"
    from thoth_terminal.house_view_service import build_research_mode_context

    session = _session_read()
    try:
        return build_research_mode_context(session, subject_id=int(raw_subject), mode=mode, limit=limit_val)
    finally:
        session.close()


def _mcp_tool_upsert_house_view_thesis(params: dict[str, object]) -> dict[str, object]:
    title = str(params.get("title") or "").strip()
    statement = str(params.get("statement") or "").strip()
    if not title or not statement:
        raise ValueError("title and statement are required.")
    subject_id = params.get("subject_id")
    subject_id_val = int(subject_id) if str(subject_id or "").isdigit() else None
    entities = params.get("entities") if isinstance(params.get("entities"), list) else []
    confidence = params.get("confidence")
    review_cadence_days = params.get("review_cadence_days")
    review_cadence_val = int(review_cadence_days) if str(review_cadence_days or "").isdigit() else 90
    from thoth_terminal.house_view_service import serialize_house_view_thesis, upsert_house_view_thesis

    def _upsert() -> dict[str, object]:
        with session_scope() as session:
            row = upsert_house_view_thesis(
                session,
                title=title,
                statement=statement,
                subject_id=subject_id_val,
                entity_tokens=[str(v) for v in entities],
                consensus_view=str(params.get("consensus_view") or "").strip() or None,
                variant_view=str(params.get("variant_view") or "").strip() or None,
                model_drivers=_split_list(params.get("model_drivers")),
                technology_assumptions=_split_list(params.get("technology_assumptions")),
                invalidation_triggers=_split_list(params.get("invalidation_triggers")),
                open_questions=_split_list(params.get("open_questions")),
                confidence=float(confidence) if confidence is not None and str(confidence).strip() else None,
                time_horizon=str(params.get("time_horizon") or "").strip() or None,
                review_cadence_days=review_cadence_val,
                idea_origin=str(params.get("idea_origin") or "user").strip() or "user",
                user_confirmed=_boolish_value(params.get("user_confirmed"), default=False),
                client_request_id=str(params.get("client_request_id") or "").strip() or None,
                audit={"source": "mcp", "tool": "upsert_house_view_thesis"},
            )
            _maybe_record_agent_change_for_mcp(
                session,
                params=params,
                object_type="thesis",
                object_id=int(row.id),
                change_kind="upsert_house_view_thesis",
                before_summary=None,
                after_summary=f"{row.title} | house_view",
            )
            return {"house_view": serialize_house_view_thesis(row)}

    return _with_db_retry(_upsert)


def _mcp_tool_generate_house_view_questions(params: dict[str, object]) -> dict[str, object]:
    subject_id = params.get("subject_id")
    subject_id_val = int(subject_id) if str(subject_id or "").isdigit() else None
    limit = params.get("limit")
    limit_val = int(limit) if str(limit or "").isdigit() else 10
    cadence = params.get("cadence_days")
    cadence_val = int(cadence) if str(cadence or "").isdigit() else 90
    create_requests = _boolish_value(params.get("create_requests"), default=False)
    from thoth_terminal.house_view_service import generate_house_view_questions

    def _generate() -> dict[str, object]:
        with session_scope() as session:
            return generate_house_view_questions(
                session,
                subject_id=subject_id_val,
                universe_slug=str(params.get("universe") or "").strip() or None,
                theme=str(params.get("theme") or "").strip() or None,
                limit=limit_val,
                cadence_days=cadence_val,
                create_requests=create_requests,
            )

    if create_requests:
        return _with_db_retry(_generate)
    session = _session_read()
    try:
        return generate_house_view_questions(
            session,
            subject_id=subject_id_val,
            universe_slug=str(params.get("universe") or "").strip() or None,
            theme=str(params.get("theme") or "").strip() or None,
            limit=limit_val,
            cadence_days=cadence_val,
            create_requests=False,
        )
    finally:
        session.close()


def _maybe_record_agent_change_for_mcp(
    session: Session,
    *,
    params: dict[str, object],
    object_type: str,
    object_id: int | None,
    change_kind: str,
    before_summary: str | None,
    after_summary: str | None,
    payload_extra: dict[str, object] | None = None,
) -> None:
    run_id = params.get("agent_change_run_id")
    if run_id is None or not str(run_id).isdigit():
        return
    run = session.query(AgentChangeRun).filter_by(id=int(run_id)).first()
    if not run:
        return
    confidence = params.get("confidence")
    change_payload = params.get("change_payload")
    payload: dict[str, object] = {}
    if isinstance(change_payload, dict):
        payload.update(change_payload)
    if isinstance(payload_extra, dict):
        payload.update(payload_extra)
    record_change_event(
        session,
        run=run,
        object_type=object_type,
        object_id=int(object_id) if object_id is not None else None,
        change_kind=change_kind,
        before_summary=before_summary,
        after_summary=after_summary,
        confidence=float(confidence) if confidence is not None and str(confidence).strip() else None,
        rationale=str(params.get("rationale") or "").strip() or None,
        payload=payload,
    )


def _mcp_tool_update_thesis(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("id")
    if raw_id is None or not str(raw_id).isdigit():
        raise ValueError("id is required.")
    thesis_id = int(raw_id)
    title = params.get("title")
    statement = params.get("statement")
    kind = params.get("kind")
    status = params.get("status")
    importance = params.get("importance")
    stale_after_days = params.get("stale_after_days")
    entities = params.get("entities")
    domains = params.get("domains")
    tags = params.get("tags")
    event_types = params.get("event_types")

    def _update() -> dict[str, object]:
        with session_scope() as session:
            th = session.query(Thesis).filter_by(id=thesis_id).first()
            if not th:
                raise ValueError("Thesis not found.")
            before_summary = f"{th.title} | status={th.status} | importance={th.importance}"
            changed = False
            if title is not None and str(title).strip() and str(title).strip() != th.title:
                th.title = str(title).strip()
                changed = True
            if statement is not None and str(statement).strip() and str(statement).strip() != th.statement:
                th.statement = str(statement).strip()
                changed = True
            if kind is not None and str(kind).strip():
                kind_norm = normalize_research_signal_kind(kind)
                if kind_norm != th.kind:
                    th.kind = kind_norm
                    changed = True
            if status is not None and str(status).strip() and str(status).strip() != th.status:
                th.status = str(status).strip()
                changed = True
            if importance is not None and str(importance).isdigit() and int(importance) != int(th.importance):
                th.importance = int(importance)
                changed = True
            if stale_after_days is not None and str(stale_after_days).isdigit():
                th.stale_after_days = int(stale_after_days)
                changed = True
            if entities is not None:
                if not isinstance(entities, list) or not entities:
                    raise ValueError("entities must be a non-empty list.")
                th.entities = resolve_entities(session, [str(e) for e in entities])
                changed = True
            if domains is not None and isinstance(domains, list):
                th.domains = resolve_domains(session, [str(d) for d in domains])[:2]
                changed = True
            if tags is not None and isinstance(tags, list):
                th.tags = resolve_tags(session, [str(t) for t in tags])
                changed = True
            if event_types is not None and isinstance(event_types, list):
                th.event_types = resolve_event_types(session, [str(e) for e in event_types])
                changed = True
            if changed:
                th.updated_at = utcnow()
                _maybe_record_agent_change_for_mcp(
                    session,
                    params=params,
                    object_type="thesis",
                    object_id=int(th.id),
                    change_kind="update_thesis",
                    before_summary=before_summary,
                    after_summary=f"{th.title} | status={th.status} | importance={th.importance}",
                )
            return {
                "id": th.id,
                "title": th.title,
                "kind": normalize_research_signal_kind(th.kind),
                "kind_profile": thesis_kind_profile(th.kind),
                "status": th.status,
                "importance": th.importance,
                "updated_at": th.updated_at.isoformat(),
            }

    return _with_db_retry(_update)


def _mcp_tool_list_agent_actions(params: dict[str, object]) -> dict[str, object]:
    guild_id = params.get("guild_id")
    thesis_id = params.get("thesis_id")
    kind = str(params.get("kind") or "").strip() or None
    status = str(params.get("status") or "").strip() or None
    limit = params.get("limit")
    limit_val = int(limit) if isinstance(limit, int) or str(limit).isdigit() else 50
    session = _session_read()
    try:
        rows = list_agent_actions(
            session,
            guild_id=int(guild_id) if str(guild_id).isdigit() else None,
            thesis_id=int(thesis_id) if str(thesis_id).isdigit() else None,
            kind=kind,
            status=status,
            limit=limit_val,
        )
        return {
            "count": len(rows),
            "actions": [serialize_agent_action(row) for row in rows],
        }
    finally:
        session.close()


def _mcp_tool_update_agent_action(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("id")
    if raw_id is None or not str(raw_id).isdigit():
        raise ValueError("id is required.")
    status = str(params.get("status") or "").strip().lower()
    if not status:
        raise ValueError("status is required.")
    resolution_note = str(params.get("resolution_note") or "").strip() or None

    def _update() -> dict[str, object]:
        with session_scope() as session:
            action = update_agent_action_status(
                session,
                action_id=int(raw_id),
                status=status,
                resolution_note=resolution_note,
            )
            if not action:
                raise ValueError("Action not found.")
            session.flush()
            session.refresh(action)
            return serialize_agent_action(action)

    return _with_db_retry(_update)


def _mcp_tool_list_agent_change_runs(params: dict[str, object]) -> dict[str, object]:
    guild_id = params.get("guild_id")
    task_kind = str(params.get("task_kind") or "").strip() or None
    limit = params.get("limit")
    limit_val = int(limit) if isinstance(limit, int) or str(limit).isdigit() else 20
    session = _session_read()
    try:
        rows = list_change_runs(
            session,
            guild_id=int(guild_id) if str(guild_id).isdigit() else None,
            task_kind=task_kind,
            limit=limit_val,
        )
        return {"count": len(rows), "runs": [serialize_change_run(row, include_changes=False) for row in rows]}
    finally:
        session.close()


def _mcp_tool_get_agent_change_run(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("id")
    if raw_id is None or not str(raw_id).isdigit():
        raise ValueError("id is required.")
    session = _session_read()
    try:
        row = get_change_run(session, run_id=int(raw_id))
        if not row:
            raise ValueError("Change run not found.")
        return serialize_change_run(row, include_changes=True)
    finally:
        session.close()


def _mcp_tool_list_thesis_proposals(params: dict[str, object]) -> dict[str, object]:
    guild_id = params.get("guild_id")
    status = str(params.get("status") or "").strip() or None
    limit = params.get("limit")
    limit_val = int(limit) if isinstance(limit, int) or str(limit).isdigit() else 50
    session = _session_read()
    try:
        rows = list_thesis_proposals(
            session,
            guild_id=int(guild_id) if str(guild_id).isdigit() else None,
            status=status,
            limit=limit_val,
        )
        return {"count": len(rows), "proposals": [serialize_thesis_proposal(row) for row in rows]}
    finally:
        session.close()


def _mcp_tool_review_thesis_proposal(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("id")
    if raw_id is None or not str(raw_id).isdigit():
        raise ValueError("id is required.")
    decision = str(params.get("decision") or "").strip().lower()
    if not decision:
        raise ValueError("decision is required.")
    reviewer_note = str(params.get("reviewer_note") or "").strip() or None

    def _review() -> dict[str, object]:
        with session_scope() as session:
            row = review_thesis_proposal(
                session,
                proposal_id=int(raw_id),
                decision=decision,
                reviewer_note=reviewer_note,
            )
            session.flush()
            session.refresh(row)
            return serialize_thesis_proposal(row)

    return _with_db_retry(_review)


def _mcp_tool_run_thesis_autopilot(params: dict[str, object]) -> dict[str, object]:
    guild_id = params.get("guild_id")
    thesis_id = params.get("thesis_id")
    lookback_hours = params.get("lookback_hours")
    lookback_val = int(lookback_hours) if str(lookback_hours).isdigit() else 72
    dedupe_key = str(params.get("dedupe_key") or "").strip() or None

    def _enqueue() -> dict[str, object]:
        from thoth_terminal.jobs import enqueue_agent_thesis_autopilot_job

        with session_scope() as session:
            job = enqueue_agent_thesis_autopilot_job(
                session,
                guild_id=int(guild_id) if str(guild_id).isdigit() else None,
                thesis_id=int(thesis_id) if str(thesis_id).isdigit() else None,
                lookback_hours=max(1, lookback_val),
                dedupe_key=dedupe_key,
            )
            return {
                "job_id": int(job.id),
                "kind": str(job.kind),
                "status": str(job.status),
                "thesis_id": int(thesis_id) if str(thesis_id).isdigit() else None,
                "lookback_hours": max(1, lookback_val),
            }

    return _with_db_retry(_enqueue)


def _mcp_tool_update_note(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("id")
    if raw_id is None or not str(raw_id).isdigit():
        raise ValueError("id is required.")
    note_id = int(raw_id)

    def _update() -> dict[str, object]:
        with session_scope() as session:
            note = update_note(
                session,
                note_id=note_id,
                title=params.get("title"),
                body_md=params.get("body_md"),
                note_type=params.get("note_type"),
                status=params.get("status"),
                importance=int(params.get("importance")) if str(params.get("importance")).isdigit() else None,
                stale_after_days=int(params.get("stale_after_days")) if str(params.get("stale_after_days")).isdigit() else None,
                pinned=bool(params.get("pinned")) if params.get("pinned") is not None else None,
                entities_csv=",".join(params.get("entities") or []) if params.get("entities") is not None else None,
                domains_csv=",".join(params.get("domains") or []) if params.get("domains") is not None else None,
                tags_csv=",".join(params.get("tags") or []) if params.get("tags") is not None else None,
                event_types_csv=",".join(params.get("event_types") or []) if params.get("event_types") is not None else None,
            )
            return {
                "id": note.id,
                "title": note.title,
                "status": note.status,
                "importance": note.importance,
                "updated_at": note.updated_at.isoformat(),
            }

    return _with_db_retry(_update)
