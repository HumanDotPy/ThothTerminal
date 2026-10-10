from __future__ import annotations

from thoth_terminal.web.mcp_parts._shared import bind_mcp_context


bind_mcp_context(globals())


def _mcp_tool_list_research_subjects(params: dict[str, object]) -> dict[str, object]:
    session = _session_read()
    try:
        return {
            "subjects": list_research_subjects(
                session,
                query=str(params.get("query") or "").strip() or None,
                kind=str(params.get("kind") or "").strip() or None,
                limit=int(params.get("limit")) if str(params.get("limit") or "").isdigit() else 50,
            )
        }
    finally:
        session.close()


def _mcp_tool_get_research_subject(params: dict[str, object]) -> dict[str, object]:
    subject = params.get("subject")
    raw_id = params.get("id")
    if subject is None and raw_id is None:
        raise ValueError("subject or id is required.")
    session = _session_read()
    try:
        return {
            "subject": get_research_subject(
                session,
                subject=str(subject).strip() if subject is not None else int(raw_id),
            )
        }
    finally:
        session.close()


def _mcp_tool_scan_research_canon_gaps(params: dict[str, object]) -> dict[str, object]:
    with session_scope() as session:
        from thoth_terminal.research_canon_service import run_research_canon_maintenance

        return run_research_canon_maintenance(
            session,
            universe_slug=str(params.get("universe") or params.get("universe_slug") or "").strip() or None,
            create_requests=bool(params.get("create_requests", False)),
            create_cards=bool(params.get("create_cards", False)),
            limit=int(params.get("limit")) if str(params.get("limit") or "").isdigit() else None,
        )


def _mcp_tool_list_research_canon_gaps(params: dict[str, object]) -> dict[str, object]:
    session = _session_read()
    try:
        from thoth_terminal.research_canon_service import list_coverage_gaps

        return list_coverage_gaps(
            session,
            universe_slug=str(params.get("universe") or params.get("universe_slug") or "").strip() or None,
            subject_id=int(params.get("subject_id")) if str(params.get("subject_id") or "").isdigit() else None,
            gap_kind=str(params.get("gap_kind") or "").strip() or None,
            status=str(params.get("status") or "").strip() or None,
            limit=int(params.get("limit")) if str(params.get("limit") or "").isdigit() else 100,
        )
    finally:
        session.close()


def _mcp_tool_get_research_canon_gap(params: dict[str, object]) -> dict[str, object]:
    session = _session_read()
    try:
        from thoth_terminal.research_canon_service import get_coverage_gap

        gap = get_coverage_gap(
            session,
            gap_id=int(params.get("id")) if str(params.get("id") or "").isdigit() else None,
            gap_key=str(params.get("gap_key") or "").strip() or None,
        )
        if gap is None:
            raise ValueError("Coverage gap not found.")
        return {"gap": gap}
    finally:
        session.close()


def _mcp_tool_list_subject_scenarios(params: dict[str, object]) -> dict[str, object]:
    session = _session_read()
    try:
        from thoth_terminal.research_canon_service import list_subject_scenarios

        subject_id = _research_canon_subject_id(session, params)
        rows = list_subject_scenarios(session, subject_id=subject_id, include_archived=bool(params.get("include_archived", False)))
        return {"subject_id": subject_id, "count": len(rows), "scenarios": rows}
    finally:
        session.close()


def _mcp_tool_upsert_subject_scenario(params: dict[str, object]) -> dict[str, object]:
    with session_scope() as session:
        from thoth_terminal.research_canon_service import serialize_scenario, upsert_subject_scenario

        subject_id = _research_canon_subject_id(session, params)
        row = upsert_subject_scenario(
            session,
            subject_id=subject_id,
            slug=str(params.get("slug") or "").strip() or None,
            name=str(params.get("name") or "").strip() or None,
            scenario_kind=str(params.get("scenario_kind") or params.get("kind") or "custom").strip(),
            probability=params.get("probability"),
            confidence=params.get("confidence"),
            selected_canon=bool(params.get("selected_canon", False)),
            thesis_summary=str(params.get("thesis_summary") or "").strip() or None,
            assumptions=dict(params.get("assumptions") or {}) if isinstance(params.get("assumptions"), dict) else None,
            key_metrics=dict(params.get("key_metrics") or {}) if isinstance(params.get("key_metrics"), dict) else None,
            evidence_links=[dict(item) for item in params.get("evidence_links") if isinstance(item, dict)] if isinstance(params.get("evidence_links"), list) else None,
            invalidation_triggers=[str(item) for item in params.get("invalidation_triggers")] if isinstance(params.get("invalidation_triggers"), list) else None,
            open_questions=[str(item) for item in params.get("open_questions")] if isinstance(params.get("open_questions"), list) else None,
            status=str(params.get("status") or "draft").strip() or "draft",
            audit={"source": "mcp"},
        )
        return {"scenario": serialize_scenario(row)}


def _mcp_tool_select_subject_canon_scenario(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("scenario_id")
    if raw_id is None or not str(raw_id).isdigit():
        raise ValueError("scenario_id is required.")
    with session_scope() as session:
        from thoth_terminal.research_canon_service import select_canon_scenario, serialize_scenario

        row = select_canon_scenario(session, scenario_id=int(raw_id), supersede_previous=bool(params.get("supersede_previous", True)))
        return {"scenario": serialize_scenario(row)}


def _mcp_tool_supersede_subject_scenario(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("scenario_id")
    if raw_id is None or not str(raw_id).isdigit():
        raise ValueError("scenario_id is required.")
    with session_scope() as session:
        from thoth_terminal.research_canon_service import serialize_scenario, supersede_subject_scenario

        row = supersede_subject_scenario(
            session,
            scenario_id=int(raw_id),
            replacement_scenario_id=int(params.get("replacement_scenario_id")) if str(params.get("replacement_scenario_id") or "").isdigit() else None,
            audit={"source": "mcp"},
        )
        return {"scenario": serialize_scenario(row)}


def _mcp_tool_draft_subject_scenarios(params: dict[str, object]) -> dict[str, object]:
    with session_scope() as session:
        from thoth_terminal.research_canon_service import draft_subject_scenario_set

        subject_id = _research_canon_subject_id(session, params)
        rows = draft_subject_scenario_set(
            session,
            subject_id=subject_id,
            scenarios=[dict(item) for item in params.get("scenarios") if isinstance(item, dict)] if isinstance(params.get("scenarios"), list) else None,
            audit={"source": "mcp"},
        )
        return {"subject_id": subject_id, "count": len(rows), "scenarios": rows}


def _mcp_tool_refresh_subject_source_pack(params: dict[str, object]) -> dict[str, object]:
    with session_scope() as session:
        from thoth_terminal.research_canon_service import discover_source_pack_items, serialize_source_pack, upsert_source_pack

        subject_id = _research_canon_subject_id(session, params)
        if bool(params.get("discover", False)):
            return discover_source_pack_items(session, subject_id=subject_id, create_requests=bool(params.get("create_requests", True)), audit={"source": "mcp"})
        return {"source_pack": serialize_source_pack(upsert_source_pack(session, subject_id=subject_id, audit={"source": "mcp"}))}


def _mcp_tool_refresh_active_universe_source_packs(params: dict[str, object]) -> dict[str, object]:
    with session_scope() as session:
        from thoth_terminal.research_canon_service import discover_active_universe_source_pack_items

        limit_raw = params.get("limit")
        stale_raw = params.get("stale_after_days")
        return discover_active_universe_source_pack_items(
            session,
            universe_slug=str(params.get("universe") or "").strip() or None,
            create_requests=bool(params.get("create_requests", True)),
            create_gap_requests=bool(params.get("create_gap_requests", True)),
            create_cards=bool(params.get("create_cards", False)),
            materialize_official_documents=bool(params.get("materialize_official_documents", True)),
            register_official_sources=bool(params.get("register_official_sources", True)),
            run_coverage_scan=bool(params.get("run_coverage_scan", False)),
            stale_after_days=int(stale_raw) if str(stale_raw or "").isdigit() else 120,
            limit=int(limit_raw) if str(limit_raw or "").isdigit() else None,
            audit={"source": "mcp"},
        )


def _mcp_tool_audit_active_universe_source_pack_coverage(params: dict[str, object]) -> dict[str, object]:
    with session_scope() as session:
        from thoth_terminal.research_canon_service import audit_active_universe_source_pack_coverage

        limit_raw = params.get("limit")
        examples_raw = params.get("examples_limit")
        return audit_active_universe_source_pack_coverage(
            session,
            universe_slug=str(params.get("universe") or "").strip() or None,
            ensure_subjects=bool(params.get("ensure_subjects", False)),
            refresh_source_packs=bool(params.get("refresh_source_packs", False)),
            limit=int(limit_raw) if str(limit_raw or "").isdigit() else None,
            examples_limit=int(examples_raw) if str(examples_raw or "").isdigit() else 12,
        )


def _mcp_tool_backfill_active_universe_official_source_packs(params: dict[str, object]) -> dict[str, object]:
    with session_scope() as session:
        from thoth_terminal.research_canon_service import backfill_active_universe_official_source_packs

        limit_raw = params.get("limit")
        runs_raw = params.get("max_runs_per_source")
        runtime_raw = params.get("max_runtime_seconds")
        rails_raw = params.get("rails")
        source_names_raw = params.get("source_names")
        return backfill_active_universe_official_source_packs(
            session,
            universe_slug=str(params.get("universe") or "").strip() or None,
            rails=[str(item).strip() for item in rails_raw if str(item).strip()] if isinstance(rails_raw, list) else None,
            source_names=[str(item).strip() for item in source_names_raw if str(item).strip()] if isinstance(source_names_raw, list) else None,
            max_runs_per_source=int(runs_raw) if str(runs_raw or "").lstrip("-").isdigit() else 1,
            register_official_sources=bool(params.get("register_official_sources", True)),
            materialize_official_documents=bool(params.get("materialize_official_documents", True)),
            refresh_source_packs=bool(params.get("refresh_source_packs", True)),
            include_audit=bool(params.get("include_audit", True)),
            limit=int(limit_raw) if str(limit_raw or "").isdigit() else None,
            max_runtime_seconds=int(runtime_raw) if str(runtime_raw or "").isdigit() else None,
        )


def _mcp_tool_get_subject_source_pack(params: dict[str, object]) -> dict[str, object]:
    session = _session_read()
    try:
        from thoth_terminal.research_canon_service import serialize_source_pack

        subject_id = _research_canon_subject_id(session, params)
        row = session.query(ResearchSourcePack).filter_by(subject_id=subject_id).first()
        return {"source_pack": serialize_source_pack(row)}
    finally:
        session.close()


def _mcp_tool_refresh_subject_report_quality(params: dict[str, object]) -> dict[str, object]:
    with session_scope() as session:
        from thoth_terminal.research_canon_service import serialize_quality, upsert_report_quality

        subject_id = _research_canon_subject_id(session, params)
        row = upsert_report_quality(
            session,
            subject_id=subject_id,
            stale_after_days=int(params.get("stale_after_days")) if str(params.get("stale_after_days") or "").isdigit() else 120,
        )
        return {"quality": serialize_quality(row)}


def _mcp_tool_get_subject_report_quality(params: dict[str, object]) -> dict[str, object]:
    session = _session_read()
    try:
        from thoth_terminal.research_canon_service import serialize_quality

        subject_id = _research_canon_subject_id(session, params)
        row = session.query(ResearchReportQuality).filter_by(subject_id=subject_id).first()
        return {"quality": serialize_quality(row)}
    finally:
        session.close()


def _mcp_tool_generate_agent_canon_card(params: dict[str, object]) -> dict[str, object]:
    with session_scope() as session:
        from thoth_terminal.research_canon_service import generate_agent_canon_card, get_agent_canon_card

        subject_id = _research_canon_subject_id(session, params)
        generate_agent_canon_card(session, subject_id=subject_id)
        return {"agent_canon_card": get_agent_canon_card(session, subject_id=subject_id)}


def _mcp_tool_get_agent_canon_card(params: dict[str, object]) -> dict[str, object]:
    session = _session_read()
    try:
        from thoth_terminal.research_canon_service import get_agent_canon_card

        subject_id = _research_canon_subject_id(session, params)
        return {"agent_canon_card": get_agent_canon_card(session, subject_id=subject_id)}
    finally:
        session.close()


def _mcp_tool_preview_research_campaign_from_universe(params: dict[str, object]) -> dict[str, object]:
    universe = str(params.get("universe") or params.get("universe_slug") or "").strip()
    if not universe:
        raise ValueError("universe is required.")
    session = _session_read()
    try:
        from thoth_terminal.research_canon_service import create_campaign_from_universe

        return create_campaign_from_universe(session, universe_slug=universe, limit=int(params.get("limit")) if str(params.get("limit") or "").isdigit() else None, preview=True)
    finally:
        session.close()


def _mcp_tool_create_research_campaign_from_universe(params: dict[str, object]) -> dict[str, object]:
    universe = str(params.get("universe") or params.get("universe_slug") or "").strip()
    if not universe:
        raise ValueError("universe is required.")
    with session_scope() as session:
        from thoth_terminal.research_canon_service import create_campaign_from_universe

        return create_campaign_from_universe(session, universe_slug=universe, name=str(params.get("name") or "").strip() or None, limit=int(params.get("limit")) if str(params.get("limit") or "").isdigit() else None, preview=False)


def _mcp_tool_get_research_campaign(params: dict[str, object]) -> dict[str, object]:
    session = _session_read()
    try:
        from thoth_terminal.research_canon_service import get_campaign

        row = get_campaign(
            session,
            campaign_id=int(params.get("id")) if str(params.get("id") or "").isdigit() else None,
            campaign_key=str(params.get("campaign_key") or "").strip() or None,
            include_items=bool(params.get("include_items", False)),
            limit=int(params.get("limit")) if str(params.get("limit") or "").isdigit() else 100,
        )
        if row is None:
            raise ValueError("Research campaign not found.")
        return {"campaign": row}
    finally:
        session.close()


def _mcp_tool_list_next_research_campaign_items(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("campaign_id")
    if raw_id is None or not str(raw_id).isdigit():
        raise ValueError("campaign_id is required.")
    with session_scope() as session:
        from thoth_terminal.research_canon_service import next_campaign_items

        rows = next_campaign_items(session, campaign_id=int(raw_id), limit=int(params.get("limit")) if str(params.get("limit") or "").isdigit() else 10)
        return {"count": len(rows), "items": rows}


def _mcp_tool_run_research_campaign_items(params: dict[str, object]) -> dict[str, object]:
    with session_scope() as session:
        from thoth_terminal.research_canon_service import execute_campaign_items

        return execute_campaign_items(
            session,
            campaign_id=int(params.get("campaign_id")) if str(params.get("campaign_id") or "").isdigit() else None,
            campaign_key=str(params.get("campaign_key") or "").strip() or None,
            limit=int(params.get("limit")) if str(params.get("limit") or "").isdigit() else 3,
            dry_run=bool(params.get("dry_run", True)),
            force=bool(params.get("force", False)),
        )


def _mcp_tool_mark_research_campaign_item_status(params: dict[str, object]) -> dict[str, object]:
    if not str(params.get("item_id") or "").isdigit():
        raise ValueError("item_id is required.")
    with session_scope() as session:
        from thoth_terminal.research_canon_service import mark_campaign_item_status, serialize_campaign_item

        row = mark_campaign_item_status(
            session,
            item_id=int(params.get("item_id")),
            status=str(params.get("status") or "").strip(),
            last_error=str(params.get("last_error") or "").strip() or None,
            output_report_id=int(params.get("output_report_id")) if str(params.get("output_report_id") or "").isdigit() else None,
            output_report_version_id=int(params.get("output_report_version_id")) if str(params.get("output_report_version_id") or "").isdigit() else None,
            quality=dict(params.get("quality") or {}) if isinstance(params.get("quality"), dict) else None,
        )
        return {"item": serialize_campaign_item(row)}


def _mcp_tool_retry_research_campaign_item(params: dict[str, object]) -> dict[str, object]:
    if not str(params.get("item_id") or "").isdigit():
        raise ValueError("item_id is required.")
    with session_scope() as session:
        from thoth_terminal.research_canon_service import retry_campaign_item, serialize_campaign_item

        return {"item": serialize_campaign_item(retry_campaign_item(session, item_id=int(params.get("item_id"))))}


def _mcp_tool_unblock_research_campaign_items(params: dict[str, object]) -> dict[str, object]:
    with session_scope() as session:
        from thoth_terminal.research_canon_service import unblock_campaign_items

        return unblock_campaign_items(
            session,
            campaign_id=int(params.get("campaign_id")) if str(params.get("campaign_id") or "").isdigit() else None,
            limit=int(params.get("limit")) if str(params.get("limit") or "").isdigit() else 500,
        )


def _mcp_tool_start_bottleneck_report(params: dict[str, object]) -> dict[str, object]:
    model_key = str(params.get("model_key") or "").strip()
    title = str(params.get("title") or "").strip()
    if not model_key or not title:
        raise ValueError("model_key and title are required.")
    with session_scope() as session:
        from thoth_terminal.research_canon_service import bottleneck_startup_packet, execute_campaign_items, serialize_campaign_item, start_bottleneck_report
        from thoth_terminal.models import BottleneckModel, ResearchCampaign, ResearchCampaignItem

        result = start_bottleneck_report(
            session,
            model_key=model_key,
            title=title,
            scope_type=str(params.get("scope_type") or "theme").strip() or "theme",
            scope_key=str(params.get("scope_key") or "").strip() or None,
            constrained_input=str(params.get("constrained_input") or "").strip() or None,
            output_unit=str(params.get("output_unit") or "").strip() or None,
            supply_chain_id=int(params.get("supply_chain_id")) if str(params.get("supply_chain_id") or "").isdigit() else None,
            subject_id=int(params.get("subject_id")) if str(params.get("subject_id") or "").isdigit() else None,
            summary=str(params.get("summary") or "").strip() or None,
            thesis=str(params.get("thesis") or "").strip() or None,
            payload=dict(params.get("payload") or {}) if isinstance(params.get("payload"), dict) else None,
            links=[dict(item) for item in params.get("links") if isinstance(item, dict)] if isinstance(params.get("links"), list) else None,
            campaign_key=str(params.get("campaign_key") or "").strip() or None,
            campaign_name=str(params.get("campaign_name") or "").strip() or None,
            priority=int(params.get("priority")) if str(params.get("priority") or "").isdigit() else 8,
        )
        result["executed"] = False
        if bool(params.get("execute", False)):
            campaign_id = int((result.get("campaign") or {}).get("id") or 0)
            execute_result = execute_campaign_items(
                session,
                campaign_id=campaign_id or None,
                campaign_key=str(params.get("campaign_key") or "").strip() or None,
                limit=1,
                dry_run=False,
                force=bool(params.get("force", False)),
            )
            result["executed"] = True
            result["execute_result"] = execute_result
            item_id = int((result.get("item") or {}).get("id") or 0)
            item = session.get(ResearchCampaignItem, item_id) if item_id else None
            campaign = session.get(ResearchCampaign, campaign_id) if campaign_id else None
            model_id = int((result.get("bottleneck_model") or {}).get("id") or 0)
            model = session.get(BottleneckModel, model_id) if model_id else None
            if model is not None:
                result["startup_packet"] = bottleneck_startup_packet(session, model=model, campaign=campaign, item=item)
                if item is not None:
                    result["item"] = serialize_campaign_item(item)
        return result


def _mcp_tool_upsert_research_bottleneck_object(params: dict[str, object]) -> dict[str, object]:
    with session_scope() as session:
        from thoth_terminal.research_canon_service import upsert_bottleneck_object

        row = upsert_bottleneck_object(
            session,
            model_key=str(params.get("model_key") or "").strip(),
            title=str(params.get("title") or "").strip(),
            scope_type=str(params.get("scope_type") or "theme").strip() or "theme",
            scope_key=str(params.get("scope_key") or "").strip() or None,
            constrained_input=str(params.get("constrained_input") or "").strip() or None,
            supply_chain_id=int(params.get("supply_chain_id")) if str(params.get("supply_chain_id") or "").isdigit() else None,
            subject_id=int(params.get("subject_id")) if str(params.get("subject_id") or "").isdigit() else None,
            summary=str(params.get("summary") or "").strip() or None,
            thesis=str(params.get("thesis") or "").strip() or None,
            payload=dict(params.get("payload") or {}) if isinstance(params.get("payload"), dict) else None,
            links=[dict(item) for item in params.get("links") if isinstance(item, dict)] if isinstance(params.get("links"), list) else None,
        )
        return serialize_bottleneck_model(row, include_assessments=True)


def _mcp_tool_refresh_research_bottleneck_gaps(params: dict[str, object]) -> dict[str, object]:
    with session_scope() as session:
        from thoth_terminal.research_canon_service import refresh_bottleneck_coverage_gaps

        return refresh_bottleneck_coverage_gaps(session)
