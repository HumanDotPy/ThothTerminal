from __future__ import annotations

from thoth_terminal.web.mcp_parts._shared import bind_mcp_context
from thoth_terminal.dataset_discovery import discover_datasets
from thoth_terminal.dataset_storage import canonical_dataset


bind_mcp_context(globals())


def _dataset_requested_version(session, dataset, params):
    raw = params.get("version_id")
    if raw is None:
        return canonical_dataset(session, dataset), None
    if not str(raw).isdigit() or not 1 <= int(raw) <= 2_147_483_647:
        raise ValueError("version_id must be a positive dataset version id.")
    version = session.query(DatasetVersion).filter_by(id=int(raw), dataset_id=int(dataset.id)).first()
    if version is None:
        raise ValueError("Dataset version not found.")
    return dataset, version


def _mcp_tool_list_knowledge_assets(params: dict[str, object]) -> dict[str, object]:
    session = _session_read()
    try:
        rows = list_knowledge_assets(
            session,
            query=str(params.get("query") or "").strip() or None,
            subject=str(params.get("subject") or "").strip() or None,
            kind=str(params.get("kind") or "").strip() or None,
            published_only=bool(params.get("published_only", False)),
            include_version_content=bool(params.get("include_content", False)),
            limit=int(params.get("limit")) if str(params.get("limit") or "").isdigit() else 50,
        )
        return {"assets": rows, "count": len(rows)}
    finally:
        session.close()


def _mcp_tool_get_knowledge_asset(params: dict[str, object]) -> dict[str, object]:
    session = _session_read()
    try:
        return {
            "asset": get_knowledge_asset(
                session,
                asset_id=int(params.get("id")) if str(params.get("id") or "").isdigit() else None,
                slug=str(params.get("slug") or "").strip() or None,
                subject=str(params.get("subject") or "").strip() or None,
                kind=str(params.get("kind") or "").strip() or None,
            )
        }
    finally:
        session.close()


def _mcp_tool_score_knowledge_asset_readiness(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("id")
    slug = str(params.get("slug") or "").strip()
    session = _session_read()
    try:
        asset = None
        if raw_id is not None and str(raw_id).isdigit():
            asset = session.query(KnowledgeAsset).filter_by(id=int(raw_id)).first()
        if asset is None and slug:
            asset = session.query(KnowledgeAsset).filter_by(slug=slug).first()
        if asset is None:
            raise ValueError("Knowledge asset not found.")
        return score_knowledge_asset(session, asset)
    finally:
        session.close()


def _mcp_tool_list_knowledge_readiness(params: dict[str, object]) -> dict[str, object]:
    limit = params.get("limit")
    session = _session_read()
    try:
        return list_knowledge_readiness(
            session,
            query=str(params.get("query") or "").strip() or None,
            limit=int(limit) if isinstance(limit, int) or str(limit).isdigit() else 50,
        )
    finally:
        session.close()


def _mcp_tool_save_knowledge_asset_draft(params: dict[str, object]) -> dict[str, object]:
    subject = str(params.get("subject") or "").strip()
    kind = str(params.get("kind") or "").strip()
    if not subject or not kind:
        raise ValueError("subject and kind are required.")
    with session_scope() as session:
        return {
            "asset": save_knowledge_asset_draft(
                session,
                subject=subject,
                kind=kind,
                title=str(params.get("title") or "").strip() or None,
                summary=str(params.get("summary") or "").strip() or None,
                content_md=str(params.get("content_md") or "") if params.get("content_md") is not None else None,
                content_json=dict(params.get("content_json") or {}) if isinstance(params.get("content_json"), dict) else None,
                coverage_json=dict(params.get("coverage_json") or {}) if isinstance(params.get("coverage_json"), dict) else None,
                evidence_bundle=[dict(item) for item in (params.get("evidence_bundle") or []) if isinstance(item, dict)] if isinstance(params.get("evidence_bundle"), list) else None,
                ai_metadata=dict(params.get("ai_metadata") or {}) if isinstance(params.get("ai_metadata"), dict) else None,
                links=[dict(item) for item in (params.get("links") or []) if isinstance(item, dict)] if isinstance(params.get("links"), list) else None,
                stale_after_days=int(params.get("stale_after_days")) if str(params.get("stale_after_days") or "").isdigit() else None,
            )
        }


def _mcp_tool_publish_knowledge_asset(params: dict[str, object]) -> dict[str, object]:
    subject = str(params.get("subject") or "").strip()
    kind = str(params.get("kind") or "").strip()
    if not subject or not kind:
        raise ValueError("subject and kind are required.")
    with session_scope() as session:
        return {"asset": publish_knowledge_asset(session, subject=subject, kind=kind)}


def _mcp_tool_generate_knowledge_asset_draft(params: dict[str, object]) -> dict[str, object]:
    subject = str(params.get("subject") or "").strip()
    kind = str(params.get("kind") or "").strip()
    if not subject or not kind:
        raise ValueError("subject and kind are required.")
    with session_scope() as session:
        return {
            "asset": generate_knowledge_asset_draft(
                session,
                subject=subject,
                kind=kind,
                instructions=str(params.get("instructions") or "").strip() or None,
                window_days=int(params.get("window_days")) if str(params.get("window_days") or "").isdigit() else None,
            )
        }


def _mcp_tool_run_freshness_review(params: dict[str, object]) -> dict[str, object]:
    object_type = str(params.get("object_type") or "").strip()
    object_id = params.get("object_id")
    if not object_type or object_id is None or not str(object_id).isdigit():
        raise ValueError("object_type and object_id are required.")
    with session_scope() as session:
        return run_freshness_review(
            session,
            object_type=object_type,
            object_id=int(object_id),
            trigger_kind="mcp",
            allow_live_web=bool(params.get("allow_live_web", True)),
        )


def _mcp_tool_list_freshness_reviews(params: dict[str, object]) -> dict[str, object]:
    session = _session_read()
    try:
        rows = list_freshness_reviews(
            session,
            object_type=str(params.get("object_type") or "").strip() or None,
            object_id=int(params.get("object_id")) if str(params.get("object_id") or "").isdigit() else None,
            subject_id=int(params.get("subject_id")) if str(params.get("subject_id") or "").isdigit() else None,
            limit=int(params.get("limit")) if str(params.get("limit") or "").isdigit() else 50,
        )
        return {"reviews": rows, "count": len(rows)}
    finally:
        session.close()


def _mcp_tool_get_freshness_review(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("id")
    if raw_id is None or not str(raw_id).isdigit():
        raise ValueError("id is required.")
    session = _session_read()
    try:
        return {"review": get_freshness_review(session, review_id=int(raw_id))}
    finally:
        session.close()


def _mcp_tool_list_datasets(params: dict[str, object]) -> dict[str, object]:
    query = str(params.get("query") or "").strip().lower()
    category = str(params.get("category") or "").strip().lower()
    family = str(params.get("family") or "").strip().lower()
    limit = params.get("limit")
    limit_val = int(limit) if str(limit).isdigit() else 50
    include_hidden = bool(params.get("include_hidden") or params.get("include_test") or params.get("include_fixtures"))
    session = _session_read()
    try:
        ensure_dataset_tables(session)
        rows = discover_datasets(
            session, query=query, category=category, family=family,
            limit=limit_val, include_hidden=include_hidden,
        ).rows
        summary_map = serialize_dataset_summaries(session, rows)
        out = [summary_map.get(int(dataset.id)) or serialize_dataset_summary(session, dataset) for dataset in rows]
        return {"count": len(out), "datasets": out}
    finally:
        session.close()


def _mcp_tool_get_dataset(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("id")
    slug = str(params.get("slug") or "").strip() or None
    include_rows = bool(params.get("include_rows", True))
    view_id = int(params.get("view_id")) if str(params.get("view_id") or "").isdigit() else None
    limit = int(params.get("limit")) if str(params.get("limit") or "").isdigit() else None
    session = _session_read()
    try:
        ensure_dataset_tables(session)
        dataset = session.query(Dataset).filter_by(id=int(raw_id)).first() if raw_id is not None and str(raw_id).isdigit() else None
        if dataset is None and slug:
            dataset = session.query(Dataset).filter_by(slug=slug).first()
        if not dataset:
            raise ValueError("Dataset not found.")
        dataset = canonical_dataset(session, dataset)
        view = session.query(DatasetView).filter_by(id=int(view_id), dataset_id=int(dataset.id)).first() if view_id is not None else None
        payload = serialize_dataset(session, dataset, include_rows=include_rows, view=view, limit=limit)
        payload["chart_suggestions"] = _dataset_chart_suggestions(payload)
        return payload
    finally:
        session.close()


def _mcp_tool_create_dataset(params: dict[str, object]) -> dict[str, object]:
    title = str(params.get("title") or "").strip()
    if not title:
        raise ValueError("Title is required.")
    with session_scope() as session:
        dataset = create_dataset(
            session,
            title=title,
            description=str(params.get("description") or "").strip() or None,
            category=str(params.get("category") or "general").strip() or "general",
            family=str(params.get("family") or params.get("category") or "general").strip() or "general",
            status=str(params.get("status") or "active").strip() or "active",
            source_kind=str(params.get("source_kind") or "").strip() or None,
            columns=params.get("columns") if isinstance(params.get("columns"), list) else None,
            rows=params.get("rows") if isinstance(params.get("rows"), list) else None,
        )
        payload = serialize_dataset(session, dataset, include_rows=True)
        _invalidate_terminal_caches("datasets:", "home:", "search:")
        return payload


def _mcp_tool_import_dataset(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("id")
    if raw_id is None or not str(raw_id).isdigit():
        raise ValueError("Missing dataset id.")
    csv_text = str(params.get("csv_text") or "")
    xlsx_base64 = str(params.get("xlsx_base64") or "")
    label = str(params.get("label") or "").strip() or None
    if not csv_text and not xlsx_base64:
        raise ValueError("Provide csv_text or xlsx_base64.")
    with session_scope() as session:
        ensure_dataset_tables(session)
        dataset = session.query(Dataset).filter_by(id=int(raw_id)).first()
        if not dataset:
            raise ValueError("Dataset not found.")
        if csv_text:
            columns, rows = parse_csv_dataset(csv_text)
        else:
            blob = base64.b64decode(xlsx_base64)
            columns, rows = parse_xlsx_dataset(blob)
        draft = replace_dataset_draft(session, dataset, columns=columns, rows=rows, label=label)
        payload = serialize_dataset(session, dataset, version=draft, include_rows=True)
        _invalidate_terminal_caches("datasets:", "home:", "search:")
        return payload


def _mcp_tool_query_dataset(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("id")
    if raw_id is None or not str(raw_id).isdigit():
        raise ValueError("Missing dataset id.")
    session = _session_read()
    try:
        ensure_dataset_tables(session)
        dataset = session.query(Dataset).filter_by(id=int(raw_id)).first()
        if not dataset:
            raise ValueError("Dataset not found.")
        dataset, version = _dataset_requested_version(session, dataset, params)
        version_id = int(version.id) if version is not None else None
        view_id = int(params.get("view_id")) if str(params.get("view_id") or "").isdigit() else None
        limit = int(params.get("limit")) if str(params.get("limit") or "").isdigit() else None
        filters = params.get("filters") if isinstance(params.get("filters"), (dict, list)) else None
        rows = query_dataset(session, dataset_id=int(dataset.id), version_id=version_id, view_id=view_id, filters=filters, limit=limit)
        dataset_payload = serialize_dataset(session, dataset, version=version, include_rows=False)
        payload = dict(dataset_payload)
        payload.update(rows)
        payload["columns"] = dataset_payload.get("columns") or []
        payload["dashboard"] = dataset_payload.get("dashboard") or {}
        payload["capabilities"] = dataset_payload.get("capabilities") or {}
        payload["view_builder"] = dataset_payload.get("view_builder") or {}
        payload["chart_suggestions"] = _dataset_chart_suggestions({"rows": rows.get("rows") or [], "columns": dataset_payload.get("columns") or []})
        return payload
    finally:
        session.close()


def _mcp_tool_update_dataset_rows(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("id")
    if raw_id is None or not str(raw_id).isdigit():
        raise ValueError("Missing dataset id.")
    rows = params.get("rows")
    if not isinstance(rows, list):
        raise ValueError("rows must be an array.")
    with session_scope() as session:
        dataset = session.query(Dataset).filter_by(id=int(raw_id)).first()
        if not dataset:
            raise ValueError("Dataset not found.")
        columns = params.get("columns") if isinstance(params.get("columns"), list) else None
        draft = replace_dataset_draft(session, dataset, columns=columns or [], rows=rows, label=str(params.get("label") or "").strip() or None)
        payload = serialize_dataset(session, dataset, version=draft, include_rows=True)
        _invalidate_terminal_caches("datasets:", "home:", "search:")
        return payload


def _mcp_tool_create_dataset_view(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("id")
    name = str(params.get("name") or "").strip()
    if raw_id is None or not str(raw_id).isdigit():
        raise ValueError("Missing dataset id.")
    if not name:
        raise ValueError("Missing view name.")
    with session_scope() as session:
        dataset = session.query(Dataset).filter_by(id=int(raw_id)).first()
        if not dataset:
            raise ValueError("Dataset not found.")
        dataset_payload = serialize_dataset(session, dataset, include_rows=False)
        normalized = parse_structured_dataset_view_payload(params, dataset_payload)
        view = create_dataset_view(
            session,
            dataset,
            name=name,
            slug=str(params.get("slug") or "").strip() or None,
            description=str(params.get("description") or "").strip() or None,
            filters=normalized.get("filters") if isinstance(normalized.get("filters"), dict) else None,
            columns=normalized.get("columns") if isinstance(normalized.get("columns"), list) else None,
            sort_spec=normalized.get("sort_spec") if isinstance(normalized.get("sort_spec"), list) else None,
            extra={"builder": normalized.get("builder") if isinstance(normalized.get("builder"), dict) else {}},
        )
        payload = {
            "id": int(view.id),
            "dataset_id": int(dataset.id),
            "slug": view.slug,
            "name": view.name,
            "description": view.description,
            "filters": dict(view.filters or {}),
            "columns": list(view.columns or []),
            "sort_spec": list(view.sort_spec or []),
            "extra": dict(view.extra or {}),
        }
        _invalidate_terminal_caches("datasets:", "home:", "search:")
        return payload


def _mcp_tool_preview_dataset_view(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("id")
    if raw_id is None or not str(raw_id).isdigit():
        raise ValueError("Missing dataset id.")
    session = _session_read()
    try:
        dataset = session.query(Dataset).filter_by(id=int(raw_id)).first()
        if not dataset:
            raise ValueError("Dataset not found.")
        dataset_payload = serialize_dataset(session, dataset, include_rows=False)
        normalized = parse_structured_dataset_view_payload(params, dataset_payload)
        return {
            "dataset_id": int(dataset.id),
            "builder": normalized.get("builder") or {},
            "preview": preview_dataset_view(
                session,
                dataset,
                filters=normalized.get("filters"),
                selected_columns=normalized.get("columns") if isinstance(normalized.get("columns"), list) else None,
                sort_spec=normalized.get("sort_spec") if isinstance(normalized.get("sort_spec"), list) else None,
                limit=int(params.get("limit")) if str(params.get("limit") or "").isdigit() else 25,
            ),
        }
    finally:
        session.close()


def _mcp_tool_upsert_dataset_draft_row(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("id")
    values = params.get("values")
    if raw_id is None or not str(raw_id).isdigit():
        raise ValueError("Missing dataset id.")
    if not isinstance(values, dict):
        raise ValueError("values must be an object.")
    with session_scope() as session:
        dataset = session.query(Dataset).filter_by(id=int(raw_id)).first()
        if not dataset:
            raise ValueError("Dataset not found.")
        return upsert_dataset_draft_row(
            session,
            dataset,
            row_key=str(params.get("row_key") or "").strip() or None,
            values=values,
            sort_order=int(params.get("sort_order")) if str(params.get("sort_order") or "").isdigit() else None,
        )


def _mcp_tool_delete_dataset_draft_row(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("id")
    row_key = str(params.get("row_key") or "").strip()
    if raw_id is None or not str(raw_id).isdigit():
        raise ValueError("Missing dataset id.")
    if not row_key:
        raise ValueError("Missing row_key.")
    with session_scope() as session:
        dataset = session.query(Dataset).filter_by(id=int(raw_id)).first()
        if not dataset:
            raise ValueError("Dataset not found.")
        return {"ok": delete_dataset_draft_row(session, dataset, row_key=row_key)}


def _mcp_tool_bulk_paste_dataset_rows(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("id")
    text = str(params.get("text") or "")
    if raw_id is None or not str(raw_id).isdigit():
        raise ValueError("Missing dataset id.")
    if not text.strip():
        raise ValueError("Missing paste text.")
    with session_scope() as session:
        dataset = session.query(Dataset).filter_by(id=int(raw_id)).first()
        if not dataset:
            raise ValueError("Dataset not found.")
        return bulk_paste_dataset_draft_rows(session, dataset, text=text)


def _mcp_tool_export_dataset(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("id")
    if raw_id is None or not str(raw_id).isdigit():
        raise ValueError("Missing dataset id.")
    view_id = int(params.get("view_id")) if str(params.get("view_id") or "").isdigit() else None
    fmt = str(params.get("format") or "csv").strip().lower() or "csv"
    session = _session_read()
    try:
        dataset = session.query(Dataset).filter_by(id=int(raw_id)).first()
        if not dataset:
            raise ValueError("Dataset not found.")
        dataset, version = _dataset_requested_version(session, dataset, params)
        version_id = int(version.id) if version is not None else None
        if fmt == "xlsx":
            blob = export_dataset_xlsx_bytes(session, dataset, view_id=view_id, version_id=version_id)
        else:
            fmt = "csv"
            blob = export_dataset_csv_bytes(session, dataset, view_id=view_id, version_id=version_id)
        return {
            "id": int(dataset.id),
            "format": fmt,
            "filename": f"{dataset.slug}.{fmt}",
            "content_base64": base64.b64encode(blob).decode("ascii"),
            "byte_length": len(blob),
        }
    finally:
        session.close()
