from __future__ import annotations

from thoth_terminal.web.mcp_parts._shared import bind_mcp_context


bind_mcp_context(globals())


def _mcp_tool_list_instrument_requests(params: dict[str, object]) -> dict[str, object]:
    query = str(params.get("query") or "").strip() or None
    status = str(params.get("status") or "").strip() or None
    scope = str(params.get("scope") or "").strip() or None
    limit = int(params.get("limit")) if str(params.get("limit") or "").isdigit() else 50
    session = _session_read()
    try:
        rows = list_instrument_requests(session, query=query, status=status, scope=scope, limit=limit, include_payloads=False)
        active_scope = build_active_universe_request_scope(session)
        items = []
        for row in rows:
            payload = serialize_instrument_request_summary(row)
            payload["queue_scope"] = instrument_request_scope(session, row, active_scope=active_scope)
            items.append(payload)
        return {"count": len(rows), "instrument_requests": items}
    finally:
        session.close()


def _mcp_tool_get_instrument_request(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("id")
    if raw_id is None or not str(raw_id).isdigit():
        raise ValueError("Missing instrument request id.")
    session = _session_read()
    try:
        row = get_instrument_request(session, int(raw_id))
        if row is None:
            raise ValueError("Instrument request not found.")
        payload = serialize_instrument_request(row)
        payload["markdown"] = instrument_request_markdown(row)
        return payload
    finally:
        session.close()


def _mcp_tool_create_instrument_request(params: dict[str, object]) -> dict[str, object]:
    title = str(params.get("title") or "").strip()
    intent = str(params.get("intent") or "instrument").strip() or "instrument"
    if not title:
        raise ValueError("Title is required.")
    with session_scope() as session:
        row = create_instrument_request(
            session,
            title=title,
            intent=intent,
            summary=str(params.get("summary") or "").strip() or None,
            target_entities=[str(v) for v in (params.get("target_entities") or []) if str(v).strip()] if isinstance(params.get("target_entities"), list) else None,
            target_themes=[str(v) for v in (params.get("target_themes") or []) if str(v).strip()] if isinstance(params.get("target_themes"), list) else None,
            desired_outputs=[str(v) for v in (params.get("desired_outputs") or []) if str(v).strip()] if isinstance(params.get("desired_outputs"), list) else None,
            evidence_bundle=[dict(v) for v in (params.get("evidence_bundle") or []) if isinstance(v, dict)] if isinstance(params.get("evidence_bundle"), list) else None,
            provenance_summary=str(params.get("provenance_summary") or "").strip() or None,
            evidence_tier=str(params.get("evidence_tier") or "").strip() or None,
            status=str(params.get("status") or "draft").strip().lower() or "draft",
            priority=int(params.get("priority")) if str(params.get("priority") or "").isdigit() else 5,
            automation_source=str(params.get("automation_source") or "").strip() or None,
            source_kind=str(params.get("source_kind") or "").strip() or None,
            draft_payload=dict(params.get("draft_payload") or {}) if isinstance(params.get("draft_payload"), dict) else None,
            linked_objects=dict(params.get("linked_objects") or {}) if isinstance(params.get("linked_objects"), dict) else None,
        )
        return serialize_instrument_request(row)


def _mcp_tool_review_instrument_request(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("id")
    decision = str(params.get("decision") or "").strip()
    if raw_id is None or not str(raw_id).isdigit():
        raise ValueError("Missing instrument request id.")
    if not decision:
        raise ValueError("Missing review decision.")
    with session_scope() as session:
        row = review_instrument_request(
            session,
            int(raw_id),
            decision=decision,
            reviewer_note=str(params.get("reviewer_note") or "").strip() or None,
            live_payload=dict(params.get("live_payload") or {}) if isinstance(params.get("live_payload"), dict) else None,
        )
        return serialize_instrument_request(row)


def _mcp_tool_run_instrument_request(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("id")
    if raw_id is None or not str(raw_id).isdigit():
        raise ValueError("Missing instrument request id.")
    with session_scope() as session:
        row = run_instrument_request(
            session,
            int(raw_id),
            draft_payload_patch=dict(params.get("draft_payload_patch") or {}) if isinstance(params.get("draft_payload_patch"), dict) else None,
            linked_objects_patch=dict(params.get("linked_objects_patch") or {}) if isinstance(params.get("linked_objects_patch"), dict) else None,
            run_summary=str(params.get("run_summary") or "").strip() or None,
        )
        return serialize_instrument_request(row)


def _mcp_tool_fulfill_instrument_request(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("id")
    summary = str(params.get("fulfillment_summary") or "").strip()
    if raw_id is None or not str(raw_id).isdigit():
        raise ValueError("Missing instrument request id.")
    if not summary:
        raise ValueError("fulfillment_summary is required.")
    with session_scope() as session:
        row = fulfill_instrument_request(
            session,
            int(raw_id),
            fulfillment_summary=summary,
            fulfilled_by=str(params.get("fulfilled_by") or "").strip() or None,
            live_payload_patch=dict(params.get("live_payload_patch") or {}) if isinstance(params.get("live_payload_patch"), dict) else None,
            linked_objects_patch=dict(params.get("linked_objects_patch") or {}) if isinstance(params.get("linked_objects_patch"), dict) else None,
            evidence_bundle_patch=[dict(v) for v in (params.get("evidence_bundle_patch") or []) if isinstance(v, dict)]
            if isinstance(params.get("evidence_bundle_patch"), list)
            else None,
            verification=dict(params.get("verification") or {}) if isinstance(params.get("verification"), dict) else None,
            close_linked_gaps=bool(params.get("close_linked_gaps", True)),
        )
        return serialize_instrument_request(row)


def _mcp_tool_list_product_stage_entries(params: dict[str, object]) -> dict[str, object]:
    session = _session_read()
    try:
        rows = list_product_stage_entries(
            session,
            query=str(params.get("query") or "").strip() or None,
            company=str(params.get("company") or "").strip() or None,
            product=str(params.get("product") or "").strip() or None,
            stage=str(params.get("stage") or "").strip() or None,
            status=str(params.get("status") or "").strip() or None,
            evidence_tier=str(params.get("evidence_tier") or "").strip() or None,
            conflict_state=str(params.get("conflict_state") or "").strip() or None,
            limit=int(params.get("limit")) if str(params.get("limit") or "").isdigit() else 50,
        )
        return {"count": len(rows), "product_stage_entries": [serialize_product_stage_entry(row) for row in rows]}
    finally:
        session.close()


def _mcp_tool_query_product_stage(params: dict[str, object]) -> dict[str, object]:
    session = _session_read()
    try:
        rows = query_product_stage(
            session,
            company=str(params.get("company") or "").strip() or None,
            product=str(params.get("product") or "").strip() or None,
            stage=str(params.get("stage") or "").strip() or None,
            limit=int(params.get("limit")) if str(params.get("limit") or "").isdigit() else 50,
        )
        return {"count": len(rows), "product_stage_entries": [serialize_product_stage_entry(row) for row in rows]}
    finally:
        session.close()


def _mcp_tool_get_product_stage_entry(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("id")
    if raw_id is None or not str(raw_id).isdigit():
        raise ValueError("Missing product stage id.")
    session = _session_read()
    try:
        row = get_product_stage_entry(session, int(raw_id))
        if row is None:
            raise ValueError("Product stage entry not found.")
        payload = serialize_product_stage_entry(row)
        payload["claims"] = [serialize_product_stage_claim(item) for item in list_product_stage_claims(session, entry_id=int(row.id), limit=200)]
        payload["history"] = [serialize_product_stage_history(item) for item in list_product_stage_history(session, int(row.id))]
        return payload
    finally:
        session.close()


def _mcp_tool_list_product_stage_claims(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("entry_id")
    if raw_id is None or not str(raw_id).isdigit():
        raise ValueError("Missing product stage entry id.")
    session = _session_read()
    try:
        rows = list_product_stage_claims(
            session,
            entry_id=int(raw_id),
            review_status=str(params.get("review_status") or "").strip() or None,
            limit=int(params.get("limit")) if str(params.get("limit") or "").isdigit() else 100,
        )
        return {"entry_id": int(raw_id), "claims": [serialize_product_stage_claim(row) for row in rows]}
    finally:
        session.close()


def _mcp_tool_get_product_stage_claim(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("id")
    if raw_id is None or not str(raw_id).isdigit():
        raise ValueError("Missing product stage claim id.")
    session = _session_read()
    try:
        row = get_product_stage_claim(session, int(raw_id))
        if row is None:
            raise ValueError("Product stage claim not found.")
        return serialize_product_stage_claim(row)
    finally:
        session.close()


def _mcp_tool_create_product_stage_claim(params: dict[str, object]) -> dict[str, object]:
    stage = str(params.get("stage") or "").strip()
    if not stage:
        raise ValueError("stage is required.")
    target_date = None
    if str(params.get("target_date") or "").strip():
        try:
            target_date = date.fromisoformat(str(params.get("target_date")))
        except Exception as exc:
            raise ValueError(f"Invalid target_date: {exc}") from exc
    source_date = None
    if str(params.get("source_date") or "").strip():
        try:
            source_date = datetime.fromisoformat(str(params.get("source_date")))
        except Exception as exc:
            raise ValueError(f"Invalid source_date: {exc}") from exc
    with session_scope() as session:
        row = create_product_stage_claim(
            session,
            entry_id=int(params.get("entry_id")) if str(params.get("entry_id") or "").isdigit() else None,
            company_name=str(params.get("company_name") or "").strip() or None,
            product_name=str(params.get("product_name") or "").strip() or None,
            stage=stage,
            target_date=target_date,
            date_precision=str(params.get("date_precision") or "").strip() or None,
            date_text=str(params.get("date_text") or "").strip() or None,
            certainty=str(params.get("certainty") or "").strip() or None,
            evidence_tier=str(params.get("evidence_tier") or "").strip() or None,
            provenance_summary=str(params.get("provenance_summary") or "").strip() or None,
            summary=str(params.get("summary") or "").strip() or None,
            rationale=str(params.get("rationale") or "").strip() or None,
            source_document_id=int(params.get("source_document_id")) if str(params.get("source_document_id") or "").isdigit() else None,
            source_date=source_date,
            linked_dataset_ids=[int(v) for v in (params.get("linked_dataset_ids") or []) if str(v).isdigit()] if isinstance(params.get("linked_dataset_ids"), list) else None,
            linked_supply_chain_ids=[int(v) for v in (params.get("linked_supply_chain_ids") or []) if str(v).isdigit()] if isinstance(params.get("linked_supply_chain_ids"), list) else None,
            linked_note_ids=[int(v) for v in (params.get("linked_note_ids") or []) if str(v).isdigit()] if isinstance(params.get("linked_note_ids"), list) else None,
            linked_thesis_ids=[int(v) for v in (params.get("linked_thesis_ids") or []) if str(v).isdigit()] if isinstance(params.get("linked_thesis_ids"), list) else None,
            automation_source=str(params.get("automation_source") or "").strip() or None,
            source_kind=str(params.get("source_kind") or "").strip() or None,
            review_status=str(params.get("review_status") or "draft").strip().lower() or "draft",
            claim_key=str(params.get("claim_key") or "").strip() or None,
            extra=dict(params.get("extra") or {}) if isinstance(params.get("extra"), dict) else None,
            history_note=str(params.get("history_note") or "").strip() or None,
            entry_status=str(params.get("entry_status") or "").strip() or None,
        )
        return serialize_product_stage_claim(row)


def _mcp_tool_review_product_stage_claim(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("id")
    decision = str(params.get("decision") or "").strip()
    if raw_id is None or not str(raw_id).isdigit():
        raise ValueError("Missing product stage claim id.")
    if not decision:
        raise ValueError("decision is required.")
    with session_scope() as session:
        row = review_product_stage_claim(
            session,
            int(raw_id),
            decision=decision,
            reviewer_note=str(params.get("reviewer_note") or "").strip() or None,
        )
        return serialize_product_stage_claim(row)


def _mcp_tool_update_product_stage_entry(params: dict[str, object]) -> dict[str, object]:
    company_name = str(params.get("company_name") or "").strip()
    product_name = str(params.get("product_name") or "").strip()
    stage = str(params.get("stage") or "").strip()
    if not company_name or not product_name or not stage:
        raise ValueError("company_name, product_name, and stage are required.")
    target_date = None
    if str(params.get("target_date") or "").strip():
        try:
            target_date = date.fromisoformat(str(params.get("target_date")))
        except Exception as exc:
            raise ValueError(f"Invalid target_date: {exc}") from exc
    with session_scope() as session:
        row = update_product_stage_entry(
            session,
            entry_id=int(params.get("id")) if str(params.get("id") or "").isdigit() else None,
            company_name=company_name,
            product_name=product_name,
            stage=stage,
            target_date=target_date,
            date_precision=str(params.get("date_precision") or "").strip() or None,
            date_text=str(params.get("date_text") or "").strip() or None,
            certainty=str(params.get("certainty") or "").strip() or None,
            status=str(params.get("status") or "").strip() or None,
            notes=str(params.get("notes") or "").strip() or None,
            evidence_tier=str(params.get("evidence_tier") or "").strip() or None,
            evidence_bundle=[dict(v) for v in (params.get("evidence_bundle") or []) if isinstance(v, dict)] if isinstance(params.get("evidence_bundle"), list) else None,
            linked_dataset_ids=[int(v) for v in (params.get("linked_dataset_ids") or []) if str(v).isdigit()] if isinstance(params.get("linked_dataset_ids"), list) else None,
            linked_supply_chain_ids=[int(v) for v in (params.get("linked_supply_chain_ids") or []) if str(v).isdigit()] if isinstance(params.get("linked_supply_chain_ids"), list) else None,
            linked_note_ids=[int(v) for v in (params.get("linked_note_ids") or []) if str(v).isdigit()] if isinstance(params.get("linked_note_ids"), list) else None,
            linked_thesis_ids=[int(v) for v in (params.get("linked_thesis_ids") or []) if str(v).isdigit()] if isinstance(params.get("linked_thesis_ids"), list) else None,
            extra=dict(params.get("extra") or {}) if isinstance(params.get("extra"), dict) else None,
            source_document_id=int(params.get("source_document_id")) if str(params.get("source_document_id") or "").isdigit() else None,
            history_note=str(params.get("history_note") or "").strip() or None,
        )
        return serialize_product_stage_entry(row)


def _mcp_tool_list_product_stage_history(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("id")
    if raw_id is None or not str(raw_id).isdigit():
        raise ValueError("Missing product stage id.")
    session = _session_read()
    try:
        history = list_product_stage_history(session, int(raw_id))
        return {"id": int(raw_id), "history": [serialize_product_stage_history(row) for row in history]}
    finally:
        session.close()


def _mcp_tool_list_supply_chains(params: dict[str, object]) -> dict[str, object]:
    query = str(params.get("query") or "").strip().lower()
    limit = params.get("limit")
    limit_val = int(limit) if isinstance(limit, int) or str(limit).isdigit() else 50
    session = _session_read()
    try:
        _ensure_supply_chain_tables(session)
        chains = session.query(SupplyChain).order_by(SupplyChain.updated_at.desc()).all()
        out = []
        for chain in chains:
            if query and query not in (chain.title or "").lower():
                continue
            import_legacy_chain_if_needed(session, chain)
            payload = serialize_supply_chain(session, chain, include_graph=False)
            out.append(payload)
            if len(out) >= limit_val:
                break
        return {"count": len(out), "supply_chains": out}
    finally:
        session.close()


def _mcp_tool_get_supply_chain(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("id")
    if raw_id is None or not str(raw_id).isdigit():
        raise ValueError("Missing supply chain id.")
    version_arg = params.get("version")
    focus_node = str(params.get("focus_node") or "").strip() or None
    version_num: int | None = None
    status: str | None = None
    if version_arg is not None:
        v_str = str(version_arg).strip().lower()
        if v_str.isdigit():
            version_num = int(v_str)
        elif v_str in ("published", "release", "released"):
            status = "published"
        elif v_str in ("draft", "latest", "current"):
            status = "draft"
    session = _session_read()
    try:
        _ensure_supply_chain_tables(session)
        chain = session.query(SupplyChain).filter_by(id=int(raw_id)).first()
        if not chain:
            raise ValueError("Supply chain not found.")
        import_legacy_chain_if_needed(session, chain)
        version = None
        if version_num is not None:
            version = session.query(SupplyChainSnapshot).filter_by(chain_id=chain.id, version=version_num, status="published").first()
        elif status:
            version = _latest_chain_version(session, chain.id, status=status)
        return serialize_supply_chain(session, chain, snapshot=version, include_graph=True, focus_node=focus_node)
    finally:
        session.close()


def _mcp_tool_get_supply_chain_node(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("id")
    if raw_id is None or not str(raw_id).isdigit():
        raise ValueError("Missing supply chain id.")
    node_key = str(params.get("node_key") or "").strip()
    if not node_key:
        raise ValueError("Missing node_key.")
    version_arg = params.get("version")
    version_num: int | None = None
    status: str | None = None
    if version_arg is not None:
        v_str = str(version_arg).strip().lower()
        if v_str.isdigit():
            version_num = int(v_str)
        elif v_str in ("published", "release", "released"):
            status = "published"
        elif v_str in ("draft", "latest", "current"):
            status = "draft"
    session = _session_read()
    try:
        _ensure_supply_chain_tables(session)
        chain = session.query(SupplyChain).filter_by(id=int(raw_id)).first()
        if not chain:
            raise ValueError("Supply chain not found.")
        import_legacy_chain_if_needed(session, chain)
        version = None
        if version_num is not None:
            version = session.query(SupplyChainSnapshot).filter_by(chain_id=chain.id, version=version_num, status="published").first()
        elif status:
            version = _latest_chain_version(session, chain.id, status=status)
        payload = _supply_chain_node_api_payload(session, chain, node_key=node_key, snapshot=version)
        if not payload:
            raise ValueError("Supply chain node not found.")
        return payload
    finally:
        session.close()


def _mcp_tool_list_bottleneck_models(params: dict[str, object]) -> dict[str, object]:
    raw_chain_id = params.get("supply_chain_id")
    supply_chain_id = int(raw_chain_id) if raw_chain_id is not None and str(raw_chain_id).isdigit() else None
    status = str(params.get("status") or "active").strip() or None
    limit = int(params.get("limit")) if str(params.get("limit") or "").isdigit() else 50
    session = _session_read()
    try:
        rows = list_bottleneck_models(
            session,
            query=str(params.get("query") or "").strip() or None,
            supply_chain_id=supply_chain_id,
            status=status,
            limit=limit,
        )
        return {"count": len(rows), "bottleneck_models": rows}
    finally:
        session.close()


def _mcp_tool_get_bottleneck_model(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("id")
    if raw_id is None or not str(raw_id).isdigit():
        raise ValueError("Missing bottleneck model id.")
    session = _session_read()
    try:
        payload = get_bottleneck_model_payload(session, int(raw_id))
        if payload is None:
            raise ValueError("Bottleneck model not found.")
        return payload
    finally:
        session.close()


def _mcp_tool_upsert_bottleneck_model(params: dict[str, object]) -> dict[str, object]:
    title = str(params.get("title") or "").strip()
    if not title:
        raise ValueError("Title is required.")
    with session_scope() as session:
        row = upsert_bottleneck_model(
            session,
            title=title,
            model_key=str(params.get("model_key") or "").strip() or None,
            scope_type=str(params.get("scope_type") or "general").strip() or "general",
            scope_key=str(params.get("scope_key") or "").strip() or None,
            subject_id=int(params.get("subject_id")) if str(params.get("subject_id") or "").isdigit() else None,
            site_id=int(params.get("site_id")) if str(params.get("site_id") or "").isdigit() else None,
            program_id=int(params.get("program_id")) if str(params.get("program_id") or "").isdigit() else None,
            supply_chain_id=int(params.get("supply_chain_id")) if str(params.get("supply_chain_id") or "").isdigit() else None,
            snapshot_id=int(params.get("snapshot_id")) if str(params.get("snapshot_id") or "").isdigit() else None,
            constrained_input=str(params.get("constrained_input") or "").strip() or None,
            output_unit=str(params.get("output_unit") or "").strip() or None,
            regime=str(params.get("regime") or "").strip() or None,
            status=str(params.get("status") or "active").strip() or "active",
            summary=str(params.get("summary") or "").strip() or None,
            thesis=str(params.get("thesis") or "").strip() or None,
            qualified_supplier_count=int(params.get("qualified_supplier_count")) if str(params.get("qualified_supplier_count") or "").isdigit() else None,
            supplier_qualification=dict(params.get("supplier_qualification") or {}) if isinstance(params.get("supplier_qualification"), dict) else None,
            evidence=[dict(item) for item in params.get("evidence") if isinstance(item, dict)] if isinstance(params.get("evidence"), list) else None,
            payload=dict(params.get("payload") or {}) if isinstance(params.get("payload"), dict) else None,
        )
        return serialize_bottleneck_model(row, include_assessments=True)


def _mcp_tool_assess_supply_chain_bottlenecks(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("id")
    if raw_id is None or not str(raw_id).isdigit():
        raise ValueError("Missing supply chain id.")
    version_arg = params.get("version")
    version_num: int | None = None
    status: str | None = None
    if version_arg is not None:
        v_str = str(version_arg).strip().lower()
        if v_str.isdigit():
            version_num = int(v_str)
        elif v_str in ("published", "release", "released"):
            status = "published"
        elif v_str in ("draft", "latest", "current"):
            status = "draft"
    with session_scope() as session:
        _ensure_supply_chain_tables(session)
        chain = session.query(SupplyChain).filter_by(id=int(raw_id)).first()
        if not chain:
            raise ValueError("Supply chain not found.")
        import_legacy_chain_if_needed(session, chain)
        snapshot = None
        if version_num is not None:
            snapshot = session.query(SupplyChainSnapshot).filter_by(chain_id=chain.id, version=version_num, status="published").first()
        elif status:
            snapshot = _latest_chain_version(session, chain.id, status=status)
        return assess_supply_chain_bottlenecks(
            session,
            chain,
            snapshot=snapshot,
            output_unit=str(params.get("output_unit") or "").strip() or None,
            evidence=[dict(item) for item in params.get("evidence") if isinstance(item, dict)] if isinstance(params.get("evidence"), list) else None,
        )


def _mcp_tool_create_supply_chain(params: dict[str, object]) -> dict[str, object]:
    title = str(params.get("title") or "").strip()
    if not title:
        raise ValueError("Title is required.")
    description = str(params.get("description") or "").strip()
    category = str(params.get("category") or "general").strip() or "general"
    product = str(params.get("product") or "").strip()
    status = str(params.get("status") or "active").strip() or "active"
    template = str(params.get("template") or "semiconductors").strip().lower()

    data: dict[str, object]
    if template in ("empty", "blank", "minimal"):
        data = {"layers": [], "nodes": [], "edges": []}
    else:
        data = _default_supply_chain_data()

    slug = _slugify_title(title)[:100]
    with session_scope() as session:
        chain = create_supply_chain(
            session,
            title=title,
            description=description or None,
            category=category,
            product=product or None,
            status=status,
            slug=slug,
            data=data,
        )
        payload = serialize_supply_chain(session, chain, include_graph=True)
    payload["url"] = f"/visualizations/supply-chains/{payload['id']}/studio"
    return payload


def _mcp_tool_apply_supply_chain_ops(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("id")
    if raw_id is None or not str(raw_id).isdigit():
        raise ValueError("Missing supply chain id.")
    ops = params.get("ops")
    if not isinstance(ops, list) or not ops:
        raise ValueError("Ops must be a non-empty list.")

    with session_scope() as session:
        _ensure_supply_chain_tables(session)
        chain = session.query(SupplyChain).filter_by(id=int(raw_id)).first()
        if not chain:
            raise ValueError("Supply chain not found.")
        before_summary = supply_chain_state_summary(session, chain, snapshot=ensure_draft_snapshot(session, chain))
        draft, result = apply_supply_chain_ops(session, chain, [dict(op) for op in ops if isinstance(op, dict)])
        payload = serialize_supply_chain(session, chain, snapshot=draft, include_graph=True)
        _maybe_record_agent_change_for_mcp(
            session,
            params=params,
            object_type="supply_chain",
            object_id=int(chain.id),
            change_kind="apply_ops",
            before_summary=before_summary,
            after_summary=supply_chain_state_summary(session, chain, snapshot=draft),
            payload_extra={
                "applied": int(result["applied"]),
                "warnings": list(result["warnings"]),
                "id_map": dict(result["id_map"]),
                "draft_version_id": int(draft.id),
                "op_names": [
                    str(op.get("op") or op.get("action") or "").strip().lower()
                    for op in ops
                    if isinstance(op, dict) and str(op.get("op") or op.get("action") or "").strip()
                ],
            },
        )

    payload.update({"applied": result["applied"], "warnings": result["warnings"], "id_map": result["id_map"]})
    return payload


def _mcp_tool_preview_supply_chain_ops(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("id")
    if raw_id is None or not str(raw_id).isdigit():
        raise ValueError("Missing supply chain id.")
    ops = params.get("ops")
    if not isinstance(ops, list) or not ops:
        raise ValueError("Ops must be a non-empty list.")

    session = _session_read()
    try:
        _ensure_supply_chain_tables(session)
        chain = session.query(SupplyChain).filter_by(id=int(raw_id)).first()
        if not chain:
            raise ValueError("Supply chain not found.")
        before_summary = supply_chain_state_summary(session, chain, snapshot=ensure_draft_snapshot(session, chain))
        draft, result = apply_supply_chain_ops(session, chain, [dict(op) for op in ops if isinstance(op, dict)])
        payload = serialize_supply_chain(session, chain, snapshot=draft, include_graph=True)
        after_summary = supply_chain_state_summary(session, chain, snapshot=draft)
        session.rollback()
        payload.update(
            {
                "preview": True,
                "applied": result["applied"],
                "warnings": result["warnings"],
                "id_map": result["id_map"],
                "before_summary": before_summary,
                "after_summary": after_summary,
            }
        )
        return payload
    finally:
        session.close()


def _mcp_tool_publish_supply_chain(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("id")
    if raw_id is None or not str(raw_id).isdigit():
        raise ValueError("Missing supply chain id.")
    label = str(params.get("label") or "").strip() or None
    chain_id = int(raw_id)
    with session_scope() as session:
        _ensure_supply_chain_tables(session)
        chain = session.query(SupplyChain).filter_by(id=chain_id).first()
        if not chain:
            raise ValueError("Supply chain not found.")
        draft = ensure_draft_snapshot(session, chain)
        before_summary = supply_chain_state_summary(session, chain, snapshot=draft)
        version = publish_supply_chain_snapshot(session, chain, label=label)
        payload = serialize_supply_chain(session, chain, snapshot=version, include_graph=True)
        _maybe_record_agent_change_for_mcp(
            session,
            params=params,
            object_type="supply_chain",
            object_id=int(chain.id),
            change_kind="publish",
            before_summary=before_summary,
            after_summary=supply_chain_state_summary(session, chain, snapshot=version),
            payload_extra={
                "published_version": _version_payload(version),
                "draft_version": _version_payload(draft),
            },
        )
        return payload


def _mcp_tool_create_supply_chain_snapshot(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("id")
    if raw_id is None or not str(raw_id).isdigit():
        raise ValueError("Missing supply chain id.")
    label = str(params.get("label") or "").strip() or None
    with session_scope() as session:
        chain = session.query(SupplyChain).filter_by(id=int(raw_id)).first()
        if not chain:
            raise ValueError("Supply chain not found.")
        snapshot = create_supply_chain_snapshot(session, chain, label=label)
        return serialize_supply_chain(session, chain, snapshot=snapshot, include_graph=True)


def _mcp_tool_compare_supply_chain_snapshots(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("id")
    if raw_id is None or not str(raw_id).isdigit():
        raise ValueError("Missing supply chain id.")
    chain_id = int(raw_id)
    left_arg = str(params.get("left_version") or "published").strip().lower() or "published"
    right_arg = str(params.get("right_version") or "draft").strip().lower() or "draft"
    session = _session_read()
    try:
        chain = session.query(SupplyChain).filter_by(id=chain_id).first()
        if not chain:
            raise ValueError("Supply chain not found.")

        def resolve(arg: str) -> SupplyChainSnapshot | None:
            if arg.isdigit():
                return session.query(SupplyChainSnapshot).filter_by(chain_id=chain_id, version=int(arg), status="published").first()
            if arg in {"draft", "latest", "current"}:
                return latest_chain_snapshot(session, chain_id, status="draft")
            if arg in {"published", "release"}:
                return latest_chain_snapshot(session, chain_id, status="published")
            return None

        left = resolve(left_arg)
        right = resolve(right_arg)
        return compare_snapshots(session, left, right, chain_id=chain_id)
    finally:
        session.close()


def _mcp_tool_bind_supply_chain_dataset(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("id")
    dataset_id = params.get("dataset_id")
    metric_kind = str(params.get("metric_kind") or "").strip()
    if raw_id is None or not str(raw_id).isdigit():
        raise ValueError("Missing supply chain id.")
    if dataset_id is None or not str(dataset_id).isdigit():
        raise ValueError("Missing dataset_id.")
    if not metric_kind:
        raise ValueError("Missing metric_kind.")
    with session_scope() as session:
        chain = session.query(SupplyChain).filter_by(id=int(raw_id)).first()
        if not chain:
            raise ValueError("Supply chain not found.")
        binding = bind_supply_chain_dataset(
            session,
            chain,
            dataset_id=int(dataset_id),
            dataset_view_id=int(params.get("dataset_view_id")) if str(params.get("dataset_view_id") or "").isdigit() else None,
            scope_type=str(params.get("scope_type") or "chain"),
            scope_key=str(params.get("scope_key") or "chain"),
            metric_kind=metric_kind,
            column_key=str(params.get("column_key") or "") or None,
            aggregation=str(params.get("aggregation") or "") or None,
        )
        return {
            "ok": True,
            "binding": {
                "id": int(binding.id),
                "scope_type": binding.scope_type,
                "scope_key": binding.scope_key,
                "metric_kind": binding.metric_kind,
                "dataset_id": int(binding.dataset_id) if binding.dataset_id is not None else None,
                "dataset_view_id": int(binding.dataset_view_id) if binding.dataset_view_id is not None else None,
                "column_key": binding.column_key,
                "aggregation": binding.aggregation,
            },
        }


def _mcp_tool_get_supply_chain_metric_bindings(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("id")
    if raw_id is None or not str(raw_id).isdigit():
        raise ValueError("Missing supply chain id.")
    session = _session_read()
    try:
        chain = session.query(SupplyChain).filter_by(id=int(raw_id)).first()
        if not chain:
            raise ValueError("Supply chain not found.")
        return list_supply_chain_metric_bindings(session, chain)
    finally:
        session.close()


def _mcp_tool_suggest_supply_chain_metric_bindings(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("id")
    if raw_id is None or not str(raw_id).isdigit():
        raise ValueError("Missing supply chain id.")
    limit = params.get("limit")
    session = _session_read()
    try:
        chain = session.query(SupplyChain).filter_by(id=int(raw_id)).first()
        if not chain:
            raise ValueError("Supply chain not found.")
        return suggest_supply_chain_metric_bindings(
            session,
            chain,
            query=str(params.get("query") or "").strip() or None,
            limit=int(limit) if isinstance(limit, int) or str(limit).isdigit() else 20,
        )
    finally:
        session.close()


def _mcp_tool_suggest_dataset_supply_chain_bindings(params: dict[str, object]) -> dict[str, object]:
    dataset_id = params.get("dataset_id")
    if dataset_id is None or not str(dataset_id).isdigit():
        raise ValueError("Missing dataset_id.")
    limit = params.get("limit")
    session = _session_read()
    try:
        return suggest_dataset_supply_chain_bindings(
            session,
            dataset_id=int(dataset_id),
            query=str(params.get("query") or "").strip() or None,
            chain_query=str(params.get("chain_query") or "").strip() or None,
            limit=int(limit) if isinstance(limit, int) or str(limit).isdigit() else 20,
        )
    finally:
        session.close()


def _mcp_tool_suggest_metric_binding_request_fulfillments(params: dict[str, object]) -> dict[str, object]:
    raw_chain_ids = params.get("chain_ids")
    if not isinstance(raw_chain_ids, list):
        raise ValueError("chain_ids must be a list of supply-chain ids.")
    chain_ids: list[int] = []
    for value in raw_chain_ids:
        if str(value).isdigit():
            chain_ids.append(int(value))
    if not chain_ids:
        raise ValueError("At least one chain id is required.")
    session = _session_read()
    try:
        return suggest_metric_binding_request_fulfillments(
            session,
            chain_ids=chain_ids,
            limit_per_chain=int(params.get("limit_per_chain")) if str(params.get("limit_per_chain") or "").isdigit() else 5,
            max_ops_per_request=int(params.get("max_ops_per_request")) if str(params.get("max_ops_per_request") or "").isdigit() else 2,
            request_limit=int(params.get("request_limit")) if str(params.get("request_limit") or "").isdigit() else 100,
        )
    finally:
        session.close()


def _mcp_tool_list_supply_chain_alerts(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("id")
    if raw_id is None or not str(raw_id).isdigit():
        raise ValueError("Missing supply chain id.")
    session = _session_read()
    try:
        return {"id": int(raw_id), "alerts": list_supply_chain_alerts(session, int(raw_id))}
    finally:
        session.close()


def _mcp_tool_list_supply_chain_lints(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("id")
    if raw_id is None or not str(raw_id).isdigit():
        raise ValueError("Missing supply chain id.")
    version_arg = params.get("version")
    version_num: int | None = None
    status: str | None = None
    if version_arg is not None:
        v_str = str(version_arg).strip().lower()
        if v_str.isdigit():
            version_num = int(v_str)
        elif v_str in ("published", "release", "released"):
            status = "published"
        elif v_str in ("draft", "latest", "current"):
            status = "draft"
    session = _session_read()
    try:
        _ensure_supply_chain_tables(session)
        chain = session.query(SupplyChain).filter_by(id=int(raw_id)).first()
        if not chain:
            raise ValueError("Supply chain not found.")
        import_legacy_chain_if_needed(session, chain)
        version = None
        if version_num is not None:
            version = session.query(SupplyChainSnapshot).filter_by(chain_id=chain.id, version=version_num, status="published").first()
        elif status:
            version = _latest_chain_version(session, chain.id, status=status)
        return {"id": int(raw_id), "lints": list_supply_chain_lints(session, chain, snapshot=version)}
    finally:
        session.close()


def _mcp_tool_link_supply_chain_parent(params: dict[str, object]) -> dict[str, object]:
    raw_child = params.get("child_id")
    raw_parent = params.get("parent_id")
    if raw_child is None or not str(raw_child).isdigit():
        raise ValueError("Missing child_id.")
    if raw_parent is None or not str(raw_parent).isdigit():
        raise ValueError("Missing parent_id.")
    relation_type = str(params.get("relation_type") or "component_of").strip() or "component_of"
    child_id = int(raw_child)
    parent_id = int(raw_parent)
    if child_id == parent_id:
        raise ValueError("child_id cannot equal parent_id.")
    with session_scope() as session:
        _ensure_supply_chain_tables(session)
        child = session.query(SupplyChain).filter_by(id=child_id).first()
        parent = session.query(SupplyChain).filter_by(id=parent_id).first()
        if not child or not parent:
            raise ValueError("Supply chain not found.")
        existing = (
            session.query(SupplyChainRelation)
            .filter_by(parent_chain_id=parent_id, child_chain_id=child_id, relation_type=relation_type)
            .first()
        )
        if existing:
            return {"ok": True, "relation_id": existing.id}
        now = utcnow()
        rel = SupplyChainRelation(
            parent_chain_id=parent_id,
            child_chain_id=child_id,
            relation_type=relation_type,
            created_at=now,
        )
        session.add(rel)
        session.flush()
        return {"ok": True, "relation_id": rel.id}


def _mcp_tool_unlink_supply_chain_parent(params: dict[str, object]) -> dict[str, object]:
    raw_child = params.get("child_id")
    raw_parent = params.get("parent_id")
    if raw_child is None or not str(raw_child).isdigit():
        raise ValueError("Missing child_id.")
    if raw_parent is None or not str(raw_parent).isdigit():
        raise ValueError("Missing parent_id.")
    child_id = int(raw_child)
    parent_id = int(raw_parent)
    with session_scope() as session:
        _ensure_supply_chain_tables(session)
        deleted = (
            session.query(SupplyChainRelation)
            .filter_by(parent_chain_id=parent_id, child_chain_id=child_id, relation_type="component_of")
            .delete(synchronize_session=False)
        )
        return {"ok": True, "deleted": int(deleted or 0)}
