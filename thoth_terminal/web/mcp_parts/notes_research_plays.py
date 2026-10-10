from __future__ import annotations

from thoth_terminal.web.mcp_parts._shared import bind_mcp_context


bind_mcp_context(globals())


def _mcp_tool_list_theses(params: dict[str, object]) -> dict[str, object]:
    status = str(params.get("status") or "").strip() or None
    include_hidden = bool(
        status
        or _request_includes_hidden_theses(params.get("include_hidden"))
        or _request_includes_hidden_theses(params.get("include_archived"))
    )
    limit = params.get("limit")
    limit_val = int(limit) if isinstance(limit, int) or str(limit).isdigit() else 50
    session = _session_read()
    try:
        q = (
            session.query(
                Thesis.id,
                Thesis.title,
                Thesis.kind,
                Thesis.status,
                Thesis.importance,
                Thesis.stale_after_days,
                Thesis.created_at,
                Thesis.updated_at,
                func.count(ThesisEvidence.id).label("evidence_count"),
                func.max(ThesisEvidence.created_at).label("last_evidence_at"),
            )
            .outerjoin(ThesisEvidence, ThesisEvidence.thesis_id == Thesis.id)
        )
        if status:
            q = q.filter(Thesis.status == status)
        else:
            q = _visible_thesis_query(q, include_hidden=include_hidden)
        theses = (
            q.group_by(
                Thesis.id,
                Thesis.title,
                Thesis.kind,
                Thesis.status,
                Thesis.importance,
                Thesis.stale_after_days,
                Thesis.created_at,
                Thesis.updated_at,
            )
            .order_by(Thesis.updated_at.desc(), Thesis.id.desc())
            .limit(limit_val)
            .all()
        )
        out = []
        for th in theses:
            need = compute_evidence_need(
                SimpleNamespace(
                    importance=th.importance,
                    status=th.status,
                    stale_after_days=th.stale_after_days,
                    created_at=th.created_at,
                    evidence=[],
                ),
                evidence_count=int(th.evidence_count or 0),
                last_evidence_at=th.last_evidence_at,
            )
            out.append(
                {
                    "id": th.id,
                    "title": th.title,
                    "kind": normalize_research_signal_kind(th.kind),
                    "kind_profile": thesis_kind_profile(th.kind),
                    "status": th.status,
                    "importance": th.importance,
                    "updated_at": th.updated_at.isoformat(),
                    "evidence_need_score": int(need.get("score") or 0),
                }
            )
        return {"count": len(out), "theses": out, "hidden_statuses_excluded": not include_hidden}
    finally:
        session.close()


def _mcp_tool_get_thesis(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("id")
    if raw_id is None or not str(raw_id).isdigit():
        raise ValueError("Missing thesis id.")
    thesis_id = int(raw_id)
    session = _session_read()
    try:
        th = (
            session.query(Thesis)
            .options(
                joinedload(Thesis.evidence).joinedload(ThesisEvidence.document),
                joinedload(Thesis.domains),
                joinedload(Thesis.tags),
                joinedload(Thesis.entities),
                joinedload(Thesis.event_types),
            )
            .filter_by(id=thesis_id)
            .first()
        )
        if not th:
            raise ValueError("Thesis not found.")
        evidence = []
        for ev in th.evidence:
            doc = ev.document
            if not doc:
                continue
            text = doc.translation_en or doc.normalized_text or ""
            evidence.append(
                {
                    "id": ev.id,
                    "polarity": ev.polarity,
                    "snippet": ev.snippet,
                    "note": ev.note,
                    "document": {
                        "id": doc.id,
                        "title": doc.title,
                        "url": doc.url or doc.canonical_url,
                        "summary": _truncate_text(text, limit=300),
                    },
                }
            )
        return {
            "id": th.id,
            "title": th.title,
            "statement": th.statement,
            "kind": normalize_research_signal_kind(th.kind),
            "kind_profile": thesis_kind_profile(th.kind),
            "status": th.status,
            "importance": th.importance,
            "domains": [d.slug for d in th.domains],
            "tags": [t.slug for t in th.tags],
            "event_types": [e.slug for e in th.event_types],
            "entities": [e.canonical_name for e in th.entities],
            "evidence": evidence,
            "autopilot": build_thesis_autopilot_snapshot(session, thesis_id=int(th.id), lookback_hours=72, candidate_limit=3),
        }
    finally:
        session.close()


def _mcp_tool_add_thesis_evidence(params: dict[str, object]) -> dict[str, object]:
    raw_thesis = params.get("thesis_id")
    if raw_thesis is None or not str(raw_thesis).isdigit():
        raise ValueError("Missing thesis_id.")
    thesis_id = int(raw_thesis)
    document_id = params.get("document_id")
    document_id_val = int(document_id) if str(document_id).isdigit() else None
    url = str(params.get("url") or "").strip() or None
    title = str(params.get("title") or "").strip() or None
    polarity = str(params.get("polarity") or "support")
    snippet = str(params.get("snippet") or "").strip() or None
    note = str(params.get("note") or "").strip() or None

    if not document_id_val and url:
        def _create_doc() -> int:
            with session_scope() as session:
                existing = session.query(Document).filter_by(canonical_url=url).first()
                if existing:
                    return int(existing.id)
                if _is_x_url(url):
                    payload: dict[str, object] = {"url": url}
                    if title:
                        payload["title"] = title
                    doc = ingest_tweet(
                        session,
                        payload=payload,
                        defer_heavy=True,
                        enqueue_postprocess=enqueue_postprocess_job,
                    )
                else:
                    doc = ingest_note(session, title=title or url, text=url)
                    doc.url = url
                    doc.canonical_url = url
                return int(doc.id)

        document_id_val = int(_with_db_retry(_create_doc))

    if not document_id_val:
        raise ValueError("document_id or url required.")

    def _attach() -> int | None:
        with session_scope() as session:
            th = session.query(Thesis).filter_by(id=thesis_id).first()
            if not th:
                raise ValueError("Thesis not found.")
            ev = add_thesis_evidence(
                session,
                thesis=th,
                document_id=document_id_val,
                polarity=polarity,
                snippet=snippet,
                note=note,
                extra=params.get("change_payload") if isinstance(params.get("change_payload"), dict) else None,
            )
            if ev:
                _maybe_record_agent_change_for_mcp(
                    session,
                    params=params,
                    object_type="thesis",
                    object_id=int(thesis_id),
                    change_kind="add_thesis_evidence",
                    before_summary=f"thesis #{thesis_id} without document #{document_id_val}",
                    after_summary=f"linked document #{document_id_val}",
                )
            return int(ev.id) if ev else None

    ev_id = _with_db_retry(_attach)
    return {"thesis_id": thesis_id, "document_id": document_id_val, "evidence_id": ev_id}


def _mcp_tool_list_research_skills(params: dict[str, object]) -> dict[str, object]:
    session = _session_read()
    try:
        rows = list_research_skills(
            session,
            query=str(params.get("query") or "").strip() or None,
            owner=str(params.get("owner") or "").strip() or None,
            status=str(params.get("status") or "").strip() or None,
            limit=int(params.get("limit")) if str(params.get("limit") or "").isdigit() else 100,
        )
        payload = [serialize_research_skill(row) for row in rows]
        return {"count": len(payload), "skills": payload}
    finally:
        session.close()


def _mcp_tool_create_research_skill(params: dict[str, object]) -> dict[str, object]:
    slug = str(params.get("slug") or "").strip()
    name = str(params.get("name") or "").strip()
    if not slug or not name:
        raise ValueError("slug and name are required.")
    with session_scope() as session:
        row = create_research_skill(
            session,
            slug=slug,
            name=name,
            owner=_clean_optional_text(str(params.get("owner") or "")),
            description=_clean_optional_text(str(params.get("description") or "")),
            universe_json={
                "domains": _split_list(params.get("domains")),
                "source_ids": _split_int_list(params.get("source_ids")),
            },
            inputs_json={"query": _clean_optional_text(str(params.get("query") or ""))}
            if _clean_optional_text(str(params.get("query") or ""))
            else {},
            trigger_json={
                "lookback_hours": max(1, int(params.get("lookback_hours") or 24)),
                "signal_kind": str(params.get("signal_kind") or "document_match").strip() or "document_match",
            },
            output_json={"max_docs": max(1, min(int(params.get("max_docs") or 8), 50))},
        )
        return {"skill": serialize_research_skill(row)}


def _mcp_tool_list_research_plays(params: dict[str, object]) -> dict[str, object]:
    session = _session_read()
    try:
        plays = list_research_plays(
            session,
            query=str(params.get("query") or "").strip() or None,
            owner=str(params.get("owner") or "").strip() or None,
            status=str(params.get("status") or "").strip() or None,
            limit=int(params.get("limit")) if str(params.get("limit") or "").isdigit() else 100,
        )
        payload = [
            serialize_research_play(play, include_skills=True, latest_run=_research_latest_run(session, play_id=int(play.id)))
            for play in plays
        ]
        return {"count": len(payload), "plays": payload}
    finally:
        session.close()


def _mcp_tool_get_research_play(params: dict[str, object]) -> dict[str, object]:
    play_id = _resolve_play_identifier(params)
    session = _session_read()
    try:
        play = get_research_play(session, int(play_id))
        if play is None:
            raise ValueError("Research play not found.")
        runs = list_recent_play_runs(session, play_id=int(play.id), limit=10)
        signals = list_recent_play_signals(session, play_id=int(play.id), limit=20)
        return {
            "play": serialize_research_play(play, include_skills=True, latest_run=(runs[0] if runs else None)),
            "runs": [serialize_research_play_run(row) for row in runs],
            "signals": [serialize_research_play_signal(row) for row in signals],
        }
    finally:
        session.close()


def _mcp_tool_create_research_play(params: dict[str, object]) -> dict[str, object]:
    slug = str(params.get("slug") or "").strip()
    name = str(params.get("name") or "").strip()
    if not slug or not name:
        raise ValueError("slug and name are required.")
    with session_scope() as session:
        play = create_research_play(
            session,
            slug=slug,
            name=name,
            owner=_clean_optional_text(str(params.get("owner") or "")),
            description=_clean_optional_text(str(params.get("description") or "")),
            universe_json={
                "query": _clean_optional_text(str(params.get("query") or "")),
                "domains": _split_list(params.get("domains")),
                "source_ids": _split_int_list(params.get("source_ids")),
            },
            schedule_json={
                "enabled": bool(int(params.get("interval_minutes") or 0) > 0),
                "interval_minutes": max(0, int(params.get("interval_minutes") or 0)),
                "lookback_hours": max(1, int(params.get("lookback_hours") or 24)),
            },
            alert_json={
                "guild_id": int(params.get("alert_guild_id")) if str(params.get("alert_guild_id") or "").isdigit() else None,
                "channel_id": int(params.get("alert_channel_id")) if str(params.get("alert_channel_id") or "").isdigit() else None,
                "thread_id": int(params.get("alert_thread_id")) if str(params.get("alert_thread_id") or "").isdigit() else None,
            },
        )
        return {"play": serialize_research_play(play, include_skills=True, latest_run=None)}


def _mcp_tool_bind_research_skill_to_play(params: dict[str, object]) -> dict[str, object]:
    play_id = params.get("play_id")
    skill_id = params.get("skill_id")
    if not str(play_id or "").isdigit() or not str(skill_id or "").isdigit():
        raise ValueError("play_id and skill_id are required.")
    with session_scope() as session:
        play = get_research_play(session, int(play_id))
        skill = get_research_skill(session, int(skill_id))
        if play is None or skill is None:
            raise ValueError("Play or skill not found.")
        link = bind_skill_to_play(
            session,
            play_id=int(play.id),
            skill_id=int(skill.id),
            position=int(params.get("position") or 0),
            required=_boolish_value(params.get("required"), default=True),
        )
        return {"binding": {"id": int(link.id), "play_id": int(link.play_id), "skill_id": int(link.skill_id)}}


def _mcp_tool_run_research_play(params: dict[str, object]) -> dict[str, object]:
    play_id = _resolve_play_identifier(params)
    sync = _boolish_value(params.get("sync"), default=False)
    with session_scope() as session:
        play = get_research_play(session, int(play_id))
        if play is None:
            raise ValueError("Research play not found.")
        if sync:
            return execute_research_play(session, play_id=int(play.id), trigger_kind="manual")
        job = enqueue_research_play_job(
            session,
            play_id=int(play.id),
            trigger_kind="manual",
            dedupe_key=f"research_play:mcp:{int(play.id)}:{int(time.time())}",
        )
        return {"ok": True, "job_id": int(job.id), "kind": str(job.kind), "play_id": int(play.id)}


def _mcp_tool_record_alignment_feedback(params: dict[str, object]) -> dict[str, object]:
    signal_id = params.get("signal_id")
    feedback_kind = str(params.get("feedback_kind") or "").strip()
    if not str(signal_id or "").isdigit() or not feedback_kind:
        raise ValueError("signal_id and feedback_kind are required.")
    with session_scope() as session:
        signal = session.query(ResearchPlaySignal).filter_by(id=int(signal_id)).first()
        if signal is None:
            raise ValueError("Signal not found.")
        row = record_alignment_feedback(
            session,
            target_type="play_signal",
            target_id=int(signal.id),
            feedback_kind=feedback_kind,
            actor=_clean_optional_text(str(params.get("actor") or "")),
            note=_clean_optional_text(str(params.get("note") or "")),
        )
        return {"feedback": serialize_alignment_feedback(row)}


def _mcp_tool_create_note(params: dict[str, object]) -> dict[str, object]:
    title = str(params.get("title") or "").strip()
    if not title:
        raise ValueError("title is required.")
    body_md = str(params.get("body_md") or "")
    note_type = str(params.get("note_type") or "general")
    status = str(params.get("status") or "active")
    importance = params.get("importance")
    importance_val = int(importance) if str(importance).isdigit() else 3
    stale_after_days = params.get("stale_after_days")
    stale_after_days_val = int(stale_after_days) if str(stale_after_days).isdigit() else None
    pinned = bool(params.get("pinned")) if params.get("pinned") is not None else False
    entities = _split_list(params.get("entities"))
    domains = _split_list(params.get("domains"))
    tags = _split_list(params.get("tags"))
    event_types = _split_list(params.get("event_types"))

    def _create() -> int:
        with session_scope() as session:
            note = create_note(
                session,
                title=title,
                body_md=body_md,
                note_type=note_type,
                status=status,
                importance=importance_val,
                stale_after_days=stale_after_days_val,
                pinned=pinned,
                entity_tokens=entities or None,
                domain_slugs=domains or None,
                tag_slugs=tags or None,
                event_type_slugs=event_types or None,
            )
            return int(note.id)

    note_id = _with_db_retry(_create)
    return {"id": note_id, "title": title}


def _mcp_tool_list_notes(params: dict[str, object]) -> dict[str, object]:
    query = str(params.get("query") or "").strip() or None
    status = str(params.get("status") or "").strip() or "active"
    note_type = str(params.get("note_type") or "").strip() or None
    limit = params.get("limit")
    limit_val = int(limit) if isinstance(limit, int) or str(limit).isdigit() else 50
    session = _session_read()
    try:
        q = session.query(Note.id, Note.title, Note.note_type, Note.status, Note.updated_at, Note.importance)
        if status:
            q = q.filter(Note.status == status)
        if note_type:
            q = q.filter(Note.note_type == note_type)
        if query:
            ids = search_note_ids(session, query, limit=5000)
            if ids:
                q = q.filter(Note.id.in_(ids))
            else:
                like = f"%{query.strip()}%"
                q = q.filter((Note.title.ilike(like)) | (Note.body_md.ilike(like)))
        notes = q.order_by(Note.pinned.desc(), Note.updated_at.desc(), Note.id.desc()).limit(limit_val).all()
        out = []
        for note in notes:
            out.append(
                {
                    "id": note.id,
                    "title": note.title,
                    "note_type": note.note_type,
                    "status": note.status,
                    "updated_at": note.updated_at.isoformat(),
                    "importance": note.importance,
                }
            )
        return {"count": len(out), "notes": out}
    finally:
        session.close()


def _mcp_tool_get_note(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("id")
    if raw_id is None or not str(raw_id).isdigit():
        raise ValueError("Missing note id.")
    note_id = int(raw_id)
    session = _session_read()
    try:
        note = (
            session.query(Note)
            .options(joinedload(Note.entities), joinedload(Note.domains), joinedload(Note.tags))
            .filter_by(id=note_id)
            .first()
        )
        if not note:
            raise ValueError("Note not found.")
        return {
            "id": note.id,
            "title": note.title,
            "body_md": note.body_md,
            "note_type": note.note_type,
            "status": note.status,
            "importance": note.importance,
            "pinned": bool(note.pinned),
            "domains": [d.slug for d in note.domains],
            "tags": [t.slug for t in note.tags],
            "entities": [e.canonical_name for e in note.entities],
            "updated_at": note.updated_at.isoformat(),
        }
    finally:
        session.close()
