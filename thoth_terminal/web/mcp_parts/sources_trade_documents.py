from __future__ import annotations

from thoth_terminal.web.mcp_parts._shared import bind_mcp_context


bind_mcp_context(globals())


def _mcp_tool_list_sources(params: dict[str, object]) -> dict[str, object]:
    query = str(params.get("query") or "").strip().lower()
    category = str(params.get("category") or "").strip() or None
    connector = str(params.get("connector") or "").strip() or None
    enabled = params.get("enabled")
    limit = params.get("limit")
    limit_val = int(limit) if isinstance(limit, int) or str(limit).isdigit() else 200

    session = _session_read()
    try:
        q = session.query(Source).options(joinedload(Source.state), joinedload(Source.profile)).order_by(Source.id.asc())
        if category:
            q = q.filter(Source.category == category)
        if query:
            q = q.filter(func.lower(Source.name).like(f"%{query}%"))
        rows = []
        visible_sources: list[Source] = []
        page_size = max(limit_val * 4, 100)
        offset = 0
        while len(visible_sources) < limit_val:
            sources = q.limit(page_size).offset(offset).all()
            if not sources:
                break
            offset += len(sources)
            for src in sources:
                if (src.config or {}).get("deleted"):
                    continue
                if connector and (src.config.get("connector") or "") != connector:
                    continue
                enabled_flag = src.config.get("enabled")
                if enabled_flag is None:
                    enabled_flag = not bool(src.config.get("disabled_reason"))
                if enabled is not None and bool(enabled) != bool(enabled_flag):
                    continue
                visible_sources.append(src)
                if len(visible_sources) >= limit_val:
                    break
        latest_run_ids = {
            int(source_id): int(run_id)
            for source_id, run_id in (
                session.query(IngestionRun.source_id, func.max(IngestionRun.id))
                .filter(IngestionRun.source_id.in_([int(src.id) for src in visible_sources]))
                .group_by(IngestionRun.source_id)
                .all()
                if visible_sources
                else []
            )
        }
        last_runs_by_source_id = {
            int(row.source_id): row
            for row in (
                session.query(IngestionRun)
                .filter(IngestionRun.id.in_(list(latest_run_ids.values())))
                .all()
                if latest_run_ids
                else []
            )
        }
        for src in visible_sources:
            last_run = last_runs_by_source_id.get(int(src.id))
            rows.append(_source_payload(src, src.state, last_run))
        return {"count": len(rows), "sources": rows}
    finally:
        session.close()


def _mcp_tool_get_source(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("id")
    name = str(params.get("name") or "").strip()
    session = _session_read()
    try:
        src = None
        if raw_id is not None and str(raw_id).isdigit():
            src = session.query(Source).options(joinedload(Source.state), joinedload(Source.profile)).filter_by(id=int(raw_id)).first()
        if not src and name:
            src = session.query(Source).options(joinedload(Source.state), joinedload(Source.profile)).filter_by(name=name).first()
        if not src:
            raise ValueError("Source not found.")
        if (src.config or {}).get("deleted"):
            raise ValueError("Source not found.")
        last_run = (
            session.query(IngestionRun)
            .filter_by(source_id=src.id)
            .order_by(IngestionRun.id.desc())
            .first()
        )
        return _source_payload(src, src.state, last_run)
    finally:
        session.close()


def _mcp_tool_run_source(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("id")
    name = str(params.get("name") or "").strip()

    def _run() -> dict[str, object]:
        SessionLocal = session_factory()
        with SessionLocal() as session:
            src = None
            if raw_id is not None and str(raw_id).isdigit():
                src = session.query(Source).filter_by(id=int(raw_id)).first()
            if not src and name:
                src = session.query(Source).filter_by(name=name).first()
            if not src:
                raise ValueError("Source not found.")
            if (src.config or {}).get("deleted"):
                raise ValueError("Source not found.")
            source_id = int(src.id)
            cfg = dict(src.config or {})
            state = src.state
            last_success_at = state.last_success_at if state else None
            source_cursor = dict(state.cursor or {}) if state and isinstance(state.cursor, dict) else {}

        items: list[dict] = []
        fetch_error: Exception | None = None
        next_cursor: dict[str, Any] | None = None
        fetch_stats: dict[str, Any] | None = None
        try:
            fetch_cfg = dict(cfg)
            if cfg.get("connector") == "web_list" and last_success_at is None:
                bootstrap_days = fetch_cfg.get("bootstrap_days")
                if bootstrap_days and fetch_cfg.get("recent_days") is None:
                    fetch_cfg["recent_days"] = bootstrap_days
            fetched = fetch_items_result(
                str(cfg.get("connector") or ""),
                source_config=fetch_cfg,
                source_cursor=source_cursor,
            )
            items = list(fetched.items or [])
            next_cursor = dict(fetched.cursor) if isinstance(fetched.cursor, dict) else None
            fetch_stats = dict(fetched.stats) if isinstance(fetched.stats, dict) else None
        except Exception as exc:
            fetch_error = exc

        with session_scope() as session:
            src = session.query(Source).filter_by(id=source_id).first()
            if not src:
                raise ValueError("Source not found.")
            if (src.config or {}).get("deleted"):
                raise ValueError("Source not found.")
            res = ingest_source(
                session,
                src,
                prefetched_items=items,
                prefetched_error=fetch_error,
                prefetched_cursor=next_cursor,
                prefetched_stats=fetch_stats,
            )
            return {
                "source_id": src.id,
                "source_name": src.name,
                "seen": res.seen,
                "new": res.new,
                "updated": res.updated,
            }

    return _with_db_retry(_run)


def _mcp_tool_update_source_config(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("id")
    name = str(params.get("name") or "").strip()
    config_patch = params.get("config_patch")
    profile_patch = params.get("profile_patch")
    enabled = params.get("enabled")
    polling_interval_minutes = params.get("polling_interval_minutes")
    poll_min_minutes = params.get("poll_min_minutes")
    poll_max_minutes = params.get("poll_max_minutes")
    active_window_start = params.get("active_window_start")
    active_window_end = params.get("active_window_end")
    active_window_tz = params.get("active_window_tz")
    freshness_sla_minutes = params.get("freshness_sla_minutes")

    def _update() -> dict[str, object]:
        with session_scope() as session:
            src = None
            if raw_id is not None and str(raw_id).isdigit():
                src = session.query(Source).filter_by(id=int(raw_id)).first()
            if not src and name:
                src = session.query(Source).filter_by(name=name).first()
            if not src:
                raise ValueError("Source not found.")
            cfg = dict(src.config or {})
            if isinstance(config_patch, dict):
                for key, val in config_patch.items():
                    if val is None:
                        cfg.pop(key, None)
                    else:
                        cfg[key] = val
            if enabled is not None:
                cfg["enabled"] = bool(enabled)
                if not enabled:
                    cfg.setdefault("disabled_reason", "disabled via MCP")
                else:
                    cfg.pop("disabled_reason", None)
            if poll_min_minutes is not None and str(poll_min_minutes).isdigit():
                cfg["poll_min_seconds"] = int(poll_min_minutes) * 60
            if poll_max_minutes is not None and str(poll_max_minutes).isdigit():
                cfg["poll_max_seconds"] = int(poll_max_minutes) * 60
            if active_window_start is not None:
                cfg["active_window_start"] = str(active_window_start)
            if active_window_end is not None:
                cfg["active_window_end"] = str(active_window_end)
            if active_window_tz is not None:
                cfg["active_window_tz"] = str(active_window_tz)
            if freshness_sla_minutes is not None and str(freshness_sla_minutes).isdigit():
                cfg["freshness_sla_minutes"] = int(freshness_sla_minutes)
            if polling_interval_minutes is not None and str(polling_interval_minutes).isdigit():
                src.polling_interval_seconds = int(polling_interval_minutes) * 60

            src.config = cfg
            if isinstance(profile_patch, dict):
                ensure_source_profile(session, src, **profile_patch)
            session.flush()
            last_run = (
                session.query(IngestionRun)
                .filter_by(source_id=src.id)
                .order_by(IngestionRun.id.desc())
                .first()
            )
            return _source_payload(src, src.state, last_run)

    result = _with_db_retry(_update)
    _invalidate_terminal_caches("sources:", "home:", "search:")
    return result


def _mcp_tool_list_source_acquisition_rails(params: dict[str, object]) -> dict[str, object]:
    raw_ids = params.get("rail_ids")
    rail_ids = [int(value) for value in raw_ids if str(value).isdigit()] if isinstance(raw_ids, list) else None
    include_surfaces = params.get("include_surfaces")
    rails = list_source_acquisition_rails(
        rail_ids=rail_ids,
        connector=str(params.get("connector") or "").strip() or None,
        source_family=str(params.get("source_family") or "").strip() or None,
        include_surfaces=True if include_surfaces is None else bool(include_surfaces),
    )
    return {"count": len(rails), "rails": rails}


def _mcp_tool_get_source_acquisition_rail(params: dict[str, object]) -> dict[str, object]:
    identifier = params.get("id")
    if identifier is None or not str(identifier).strip():
        identifier = str(params.get("rail_key") or "").strip()
    if identifier is None or not str(identifier).strip():
        raise ValueError("id or rail_key is required.")
    payload = get_source_acquisition_rail(identifier)
    if payload is None:
        raise ValueError("Source acquisition rail not found.")
    return payload


def _mcp_tool_instrument_request_acquisition_payload(params: dict[str, object]) -> dict[str, object]:
    identifier = params.get("id")
    if identifier is None or not str(identifier).strip():
        identifier = str(params.get("rail_key") or "").strip()
    if identifier is None or not str(identifier).strip():
        raise ValueError("id or rail_key is required.")
    return instrument_request_payload_for_acquisition_rail(identifier)


def _mcp_tool_ensure_source_acquisition_templates(params: dict[str, object]) -> dict[str, object]:
    identifier = params.get("id")
    if identifier is None or not str(identifier).strip():
        identifier = str(params.get("rail_key") or "").strip()
    if identifier is None or not str(identifier).strip():
        raise ValueError("id or rail_key is required.")
    raw_connectors = params.get("connectors")
    connectors = [str(value).strip() for value in raw_connectors if str(value).strip()] if isinstance(raw_connectors, list) else None
    with session_scope() as session:
        sources = ensure_source_acquisition_templates(
            session,
            identifier,
            connectors=connectors,
            enabled=bool(params.get("enabled", False)),
        )
        return {"count": len(sources), "sources": sources}


def _mcp_tool_list_trade_series(params: dict[str, object]) -> dict[str, object]:
    reporter_iso = str(params.get("reporter_iso") or "").strip().upper() or None
    hs_code = str(params.get("hs_code") or "").strip() or None
    theme = str(params.get("theme") or "").strip().lower() or None
    flow = str(params.get("flow") or "").strip().lower() or None
    limit = int(params.get("limit") or 50)
    session = _session_read()
    try:
        rows = list_trade_series_payload(
            session,
            reporter_iso=reporter_iso,
            hs_code=hs_code,
            theme=theme,
            flow=flow,
            limit=max(1, min(limit, 200)),
        )
        return {"count": len(rows), "series": rows}
    finally:
        session.close()


def _mcp_tool_get_trade_series(params: dict[str, object]) -> dict[str, object]:
    slug = str(params.get("slug") or "").strip() or None
    raw_id = params.get("id")
    session = _session_read()
    try:
        payload = get_trade_series_payload(
            session,
            slug=slug,
            series_id=(int(raw_id) if raw_id is not None and str(raw_id).isdigit() else None),
        )
        if not payload:
            raise ValueError("Trade series not found.")
        return payload
    finally:
        session.close()


def _mcp_tool_find_trade_series_by_hs(params: dict[str, object]) -> dict[str, object]:
    hs_code = str(params.get("hs_code") or "").strip()
    if not hs_code:
        raise ValueError("hs_code is required.")
    limit = int(params.get("limit") or 50)
    session = _session_read()
    try:
        rows = find_trade_series_by_hs_payload(session, hs_code=hs_code, limit=max(1, min(limit, 200)))
        return {"count": len(rows), "series": rows}
    finally:
        session.close()


def _mcp_tool_refresh_trade_source(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("id")
    name = str(params.get("name") or "").strip()

    def _run() -> dict[str, object]:
        with session_scope() as session:
            src = None
            if raw_id is not None and str(raw_id).isdigit():
                src = session.query(Source).filter_by(id=int(raw_id)).first()
            if not src and name:
                src = session.query(Source).filter_by(name=name).first()
            if not src or (src.config or {}).get("deleted"):
                raise ValueError("Source not found.")
            connector = str((src.config or {}).get("connector") or "").strip()
            if not (connector.startswith("trade_") or connector == "cloudflare_crawl_discovery"):
                raise ValueError("Source is not a trade/crawl discovery source.")
            res = ingest_source(session, src)
            return {"source_id": int(src.id), "source_name": src.name, "seen": res.seen, "new": res.new, "updated": res.updated}

    return _with_db_retry(_run)


def _mcp_tool_trade_latest_summary(params: dict[str, object]) -> dict[str, object]:
    reporter_iso = str(params.get("reporter_iso") or "").strip().upper() or None
    hs_code = str(params.get("hs_code") or "").strip() or None
    theme = str(params.get("theme") or "").strip().lower() or None
    flow = str(params.get("flow") or "").strip().lower() or None
    limit = int(params.get("limit") or 6)
    session = _session_read()
    try:
        return trade_latest_summary_payload(
            session,
            reporter_iso=reporter_iso,
            hs_code=hs_code,
            theme=theme,
            flow=flow,
            limit=max(1, min(limit, 24)),
        )
    finally:
        session.close()


def _mcp_tool_set_doc_triage(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("document_id")
    action = str(params.get("action") or "").strip().lower()
    if raw_id is None or not str(raw_id).isdigit():
        raise ValueError("document_id is required.")
    doc_id = int(raw_id)

    def _apply() -> dict[str, object]:
        with session_scope() as session:
            if action == "open":
                doc = mark_opened(session, document_id=doc_id, meta={"source": "mcp"})
            elif action == "save":
                doc = mark_saved(session, document_id=doc_id, meta={"source": "mcp"})
            elif action == "unsave":
                doc = mark_unsaved(session, document_id=doc_id, meta={"source": "mcp"})
            elif action == "ignore":
                doc = mark_ignored(session, document_id=doc_id, meta={"source": "mcp"})
            elif action == "archive":
                doc = mark_archived(session, document_id=doc_id, meta={"source": "mcp"})
            elif action == "actioned":
                doc = mark_actioned(session, document_id=doc_id, meta={"source": "mcp"})
            elif action == "reset_to_new":
                doc = reset_to_new(session, document_id=doc_id, meta={"source": "mcp"})
            elif action == "seen":
                doc = mark_seen(session, document_id=doc_id, meta={"source": "mcp"})
            else:
                raise ValueError("Unknown action.")
            return _doc_payload(doc)

    return _with_db_retry(_apply)


def _mcp_tool_bulk_doc_action(params: dict[str, object]) -> dict[str, object]:
    doc_ids = params.get("document_ids")
    action = str(params.get("action") or "").strip().lower()
    if not isinstance(doc_ids, list):
        raise ValueError("document_ids must be a list.")
    ids = [int(d) for d in doc_ids if str(d).isdigit()]
    if not ids:
        raise ValueError("No valid document ids.")

    def _apply() -> dict[str, object]:
        with session_scope() as session:
            counts = bulk_apply(session, document_ids=ids, action=action, meta={"source": "mcp"})
            return {"counts": counts, "action": action, "total": len(ids)}

    return _with_db_retry(_apply)


def _mcp_tool_list_doc_events(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("document_id")
    if raw_id is None or not str(raw_id).isdigit():
        raise ValueError("document_id is required.")
    limit = params.get("limit")
    limit_val = int(limit) if isinstance(limit, int) or str(limit).isdigit() else 50
    doc_id = int(raw_id)

    session = _session_read()
    try:
        events = (
            session.query(DocumentEvent)
            .filter(DocumentEvent.document_id == doc_id)
            .order_by(DocumentEvent.event_at.desc())
            .limit(limit_val)
            .all()
        )
        out = []
        for ev in events:
            out.append(
                {
                    "id": ev.id,
                    "document_id": ev.document_id,
                    "event_type": ev.event_type,
                    "event_at": ev.event_at.isoformat() if ev.event_at else None,
                    "meta": ev.meta or {},
                }
            )
        return {"count": len(out), "events": out}
    finally:
        session.close()
