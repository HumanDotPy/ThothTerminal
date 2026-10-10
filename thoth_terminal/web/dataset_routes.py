from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from fastapi import APIRouter, Body, Depends, File, Form, Request, Response, UploadFile
from fastapi.responses import HTMLResponse
from thoth_terminal.web.json_response import JSONResponse
from sqlalchemy.orm import Session

from thoth_terminal.db import get_session, utcnow
from thoth_terminal.dataset_discovery import discover_datasets
from thoth_terminal.dataset_storage import canonical_dataset

_CONTEXT_PROVIDER: Callable[[], Mapping[str, object]] | None = None


def _refresh_context() -> None:
    if _CONTEXT_PROVIDER is None:
        return
    context = dict(_CONTEXT_PROVIDER())
    for key in list(context):
        if key.startswith("__") and key.endswith("__"):
            context.pop(key, None)
    for key in ("router", "_CONTEXT_PROVIDER", "_refresh_context", "register_dataset_routes"):
        context.pop(key, None)
    globals().update(context)


def _refresh_context_dependency() -> None:
    _refresh_context()


router = APIRouter(dependencies=[Depends(_refresh_context_dependency)])


def register_dataset_routes(app_obj: Any, *, context_provider: Callable[[], Mapping[str, object]] | None = None) -> None:
    global _CONTEXT_PROVIDER
    _CONTEXT_PROVIDER = context_provider
    app_obj.include_router(router)


def _dataset_error_payload(exc: Exception) -> dict[str, object]:
    try:
        data = json.loads(str(exc))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {"error": str(exc)}


def _dataset_universe_scope(db: Session) -> dict[str, object]:
    rows = (
        db.query(
            ResearchInstrument.company_id,
            ResearchInstrument.issuer_id,
            ResearchInstrument.display_name,
            ResearchInstrument.legal_name,
            ResearchInstrument.ticker,
            ResearchInstrument.yahoo_symbol,
            ResearchUniverseRow.display_label,
        )
        .join(ResearchUniverseRow, ResearchUniverseRow.instrument_id == ResearchInstrument.id)
        .join(ResearchUniverse, ResearchUniverse.id == ResearchUniverseRow.universe_id)
        .filter(ResearchUniverse.status == "active", ResearchUniverseRow.archived_at.is_(None))
        .all()
    )
    if not rows:
        return {"active": False}

    company_ids: set[int] = set()
    issuer_ids: set[int] = set()
    slugs: set[str] = set()
    tickers: set[str] = set()
    name_tokens: set[str] = set()

    def add_name(value: object) -> None:
        token = _dataset_text_key(value)
        if len(token) >= 3 and not token.isdigit():
            name_tokens.add(token)

    def add_symbol(value: object) -> None:
        token = str(value or "").strip().upper()
        if token:
            tickers.add(token)

    for row in rows:
        if row.company_id is not None:
            company_ids.add(int(row.company_id))
        if row.issuer_id is not None:
            issuer_ids.add(int(row.issuer_id))
        display_label = row.display_label
        add_name(display_label)
        add_name(row.display_name)
        add_name(row.legal_name)
        add_symbol(display_label)
        add_symbol(row.ticker)
        add_symbol(row.yahoo_symbol)

    company_filters = []
    if company_ids:
        company_filters.append(Company.id.in_(sorted(company_ids)))
    if issuer_ids:
        company_filters.append(Company.issuer_id.in_(sorted(issuer_ids)))
    if company_filters:
        for company in (
            db.query(Company.id, Company.slug, Company.name, Company.ticker, Company.issuer_id)
            .filter(or_(*company_filters))
            .all()
        ):
            company_ids.add(int(company.id))
            slugs.add(_dataset_text_key(company.slug))
            add_name(company.name)
            add_symbol(company.ticker)

    return {
        "active": True,
        "company_ids": company_ids,
        "issuer_ids": issuer_ids,
        "company_slugs": {slug for slug in slugs if slug},
        "tickers": {ticker for ticker in tickers if ticker},
        "name_tokens": {token for token in name_tokens if token},
        "text_tokens": _dataset_scope_text_tokens(tickers, name_tokens),
    }


def _dataset_scope_text_tokens(tickers: set[str], name_tokens: set[str]) -> tuple[str, ...]:
    # These tokens are invariant across the catalog scan. Keep word boundaries,
    # but normalize each universe value once instead of once per candidate row.
    normalized = {_dataset_text_key(token) for token in tickers | name_tokens}
    return tuple(f" {token} " for token in normalized if token)


def _dataset_extra(dataset: Dataset) -> dict[str, Any]:
    return dataset.extra if isinstance(dataset.extra, dict) else {}


def _dataset_text_key(value: object) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9가-힣]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _dataset_data_rails_from_counts(counts: dict[str, int]) -> list[dict[str, object]]:

    rail_order = [
        "trade_customs",
        "earnings_actuals",
        "earnings_filings",
        "ipo_calendar",
        "monthly_revenue",
        "guidance_actuals",
        "product_stage_tracker",
        "instrument_projection",
    ]
    seen = set(rail_order)
    rail_order.extend(sorted(family for family in counts if family not in seen and family != "general"))

    rails: list[dict[str, object]] = []
    for family in rail_order:
        meta = dataset_family_meta(family)
        rails.append(
            {
                "family": family,
                "label": meta.get("label") or family.replace("_", " ").title(),
                "summary": meta.get("summary") or "Structured data family.",
                "count": int(counts.get(family) or 0),
                "href": f"/visualizations/datasets?family={family}",
                "active": int(counts.get(family) or 0) > 0,
                "featured": family == "trade_customs",
            }
        )
    return rails


@router.get("/api/datasets/search")
def api_dataset_search(q: str = "", db: Session = Depends(get_session)) -> JSONResponse:
    ensure_dataset_tables(db)
    results = []
    dataset_rows = discover_datasets(db, query=q, limit=50).rows
    for dataset in dataset_rows:
        results.append(
            {
                "id": int(dataset.id),
                "title": dataset.title,
                "category": dataset.category,
                "updated_at": dataset.updated_at.isoformat() if dataset.updated_at else None,
            }
        )
    return JSONResponse({"ok": True, "results": results})


_DATASET_INDEX_DEFAULT_DISPLAY_LIMIT = 120
_DATASET_INDEX_DISPLAY_LIMIT = 240
_DATASET_INDEX_LIMIT_OPTIONS = [50, _DATASET_INDEX_DEFAULT_DISPLAY_LIMIT, _DATASET_INDEX_DISPLAY_LIMIT]


def _dataset_index_payload(
    db: Session,
    *,
    query_text: str = "",
    family_filter: str = "",
    category_filter: str = "",
    display_limit: int | None = None,
) -> dict[str, Any]:
    query = str(query_text or "").strip().lower()
    family_filter = str(family_filter or "").strip().lower()
    category_filter = str(category_filter or "").strip().lower()
    display_limit_val = max(25, min(int(display_limit or _DATASET_INDEX_DISPLAY_LIMIT), _DATASET_INDEX_DISPLAY_LIMIT))
    scope_eligible = not query and not family_filter and not category_filter
    dataset_scope = _dataset_universe_scope(db) if scope_eligible else {"active": False}
    scoped_default = bool(scope_eligible and dataset_scope.get("active"))
    discovery = discover_datasets(
        db, query=query, family=family_filter, category=category_filter,
        scope=dataset_scope if scoped_default else None, limit=display_limit_val,
        include_facets=True,
    )
    deduped_rows = discovery.rows
    display_limited = discovery.match_count > len(deduped_rows)
    summary_map = serialize_dataset_summaries(db, deduped_rows)
    items = [summary_map[int(dataset.id)] for dataset in deduped_rows]
    families = discovery.family_options
    categories = discovery.category_options
    data_rails = _dataset_data_rails_from_counts(discovery.family_counts)
    family_lanes_map: dict[str, dict[str, object]] = {}
    for item in items:
        family_key = str(item.get("family") or "general").strip() or "general"
        lane = family_lanes_map.setdefault(
            family_key,
            {
                "family": family_key,
                "meta": dataset_family_meta(family_key),
                "count": 0,
                "row_count": 0,
                "view_count": 0,
                "items": [],
            },
        )
        lane["count"] = int(lane.get("count") or 0) + 1
        counts = item.get("counts") if isinstance(item.get("counts"), dict) else {}
        lane["row_count"] = int(lane.get("row_count") or 0) + int(counts.get("rows") or 0)
        lane["view_count"] = int(lane.get("view_count") or 0) + int(counts.get("views") or 0)
        lane_items = lane.get("items")
        if isinstance(lane_items, list):
            lane_items.append(item)
    family_lanes = sorted(
        [
            {
                **lane,
                "items": list((lane.get("items") or [])[:4]),
            }
            for lane in family_lanes_map.values()
        ],
        key=lambda lane: (-int(lane.get("count") or 0), str(lane.get("family") or "")),
    )
    return {
        "items": items,
        "family_lanes": family_lanes,
        "family_options": families,
        "category_options": categories,
        "total_count": discovery.total_count,
        "scoped_count": discovery.scoped_count,
        "raw_match_count": discovery.raw_match_count,
        "deduped_match_count": discovery.match_count,
        "scan_limited": False,
        "display_limit": display_limit_val,
        "display_limited": display_limited,
        "duplicate_count": 0,
        "scoped_default": scoped_default,
        "scope_label": "Universe companies" if scoped_default else "Filtered catalog" if (query or family_filter or category_filter) else "Full catalog",
        "data_rails": data_rails,
    }


@router.get("/visualizations/datasets", response_class=HTMLResponse)
def datasets_view(
    request: Request,
    q: str | None = None,
    family: str | None = None,
    category: str | None = None,
    limit: int | None = None,
    db: Session = Depends(get_session),
) -> HTMLResponse:
    ensure_dataset_tables(db)
    query_text = str(q or "").strip()
    query = query_text.lower()
    family_filter = str(family or "").strip().lower()
    category_filter = str(category or "").strip().lower()
    limit_val = max(25, min(int(limit or _DATASET_INDEX_DEFAULT_DISPLAY_LIMIT), _DATASET_INDEX_DISPLAY_LIMIT))
    cache_key = f"datasets:{_cache_scope()}:index:{query}:{family_filter}:{category_filter}:{limit_val}"
    payload = _DATASET_INDEX_CACHE.get_or_set(
        cache_key,
        lambda: _dataset_index_payload(
            db,
            query_text=query_text,
            family_filter=family_filter,
            category_filter=category_filter,
            display_limit=limit_val,
        ),
        ttl_s=15.0,
    )
    return _template_response(
        "datasets.html",
        {
            "request": request,
            **payload,
            "query": query_text,
            "family_filter": str(family or "").strip(),
            "category_filter": str(category or "").strip(),
            "limit_filter": str(limit_val),
            "limit_options": _DATASET_INDEX_LIMIT_OPTIONS,
        },
    )


@router.get("/visualizations/datasets/{dataset_id}", response_class=HTMLResponse)
def dataset_workbench(request: Request, dataset_id: int, db: Session = Depends(get_session)) -> HTMLResponse:
    ensure_dataset_tables(db)
    dataset = db.query(Dataset).filter_by(id=dataset_id).first()
    if not dataset:
        raise HTTPException(status_code=404, detail="Dataset not found")
    canonical = canonical_dataset(db, dataset)
    if canonical.id != dataset.id:
        return RedirectResponse(url=f"/visualizations/datasets/{canonical.id}", status_code=307)
    view_id = int(request.query_params.get("view_id")) if str(request.query_params.get("view_id") or "").isdigit() else None
    view = db.query(DatasetView).filter_by(id=int(view_id), dataset_id=int(dataset.id)).first() if view_id is not None else None
    payload = serialize_dataset(db, dataset, include_rows=True, view=view, limit=250, analysis_limit=1000)
    version_rows = (
        db.query(DatasetVersion)
        .filter_by(dataset_id=int(dataset.id))
        .order_by(DatasetVersion.version.desc(), DatasetVersion.updated_at.desc())
        .all()
    )
    linked_request_rows = [
        {
            "link_id": int(link.id),
            "relation": link.relation,
            "id": int(row.id),
            "title": row.title,
            "href": f"/instruments/requests/{row.id}",
            "meta": f"{row.status} · priority {int(row.priority or 0)}",
        }
        for link, row in (
            db.query(DatasetLink, InstrumentRequest)
            .join(InstrumentRequest, and_(DatasetLink.object_type == "instrument_request", DatasetLink.object_id == InstrumentRequest.id))
            .filter(DatasetLink.dataset_id == int(dataset.id))
            .order_by(InstrumentRequest.updated_at.desc())
            .all()
        )
    ]
    linked_stage_rows: list[dict[str, object]] = []
    for link, row in (
        db.query(DatasetLink, ProductStageEntry)
        .join(ProductStageEntry, and_(DatasetLink.object_type == "product_stage", DatasetLink.object_id == ProductStageEntry.id))
        .filter(DatasetLink.dataset_id == int(dataset.id))
        .order_by(ProductStageEntry.updated_at.desc())
        .all()
    ):
        stage_payload = serialize_product_stage_entry(row)
        linked_stage_rows.append(
            {
                "link_id": int(link.id),
                "relation": link.relation,
                "id": int(row.id),
                "title": f"{row.company_name} · {row.product_name}",
                "href": f"/stage-tracker/{row.id}",
                "meta": (
                    f"reviewed {stage_payload['current_reviewed']['stage'] if stage_payload.get('current_reviewed') else row.stage}"
                    + (
                        f" · candidate {stage_payload['current_candidate']['stage']}"
                        if stage_payload.get("current_candidate")
                        and (
                            not stage_payload.get("current_reviewed")
                            or stage_payload["current_candidate"].get("id") != stage_payload["current_reviewed"].get("id")
                        )
                        else ""
                    )
                    + (f" · {stage_payload['conflict_state']}" if stage_payload.get("conflict_state") else "")
                ),
            }
        )
    linked_supply_chain_rows = [
        {
            "link_id": int(link.id),
            "relation": link.relation,
            "id": int(chain.id),
            "title": chain.title,
            "href": f"/visualizations/supply-chains/{chain.id}/command-center",
            "meta": f"{chain.category}{(' · ' + chain.product) if chain.product else ''}",
        }
        for link, chain in (
            db.query(DatasetLink, SupplyChain)
            .join(SupplyChain, and_(DatasetLink.object_type == "supply_chain", DatasetLink.object_id == SupplyChain.id))
            .filter(DatasetLink.dataset_id == int(dataset.id))
            .order_by(SupplyChain.updated_at.desc())
            .all()
        )
    ]
    linked_note_rows = [
        {
            "link_id": int(link.id),
            "relation": link.relation,
            "id": int(row.id),
            "title": row.title or f"note {row.id}",
            "href": f"/notes/{row.id}",
            "meta": row.note_type or "note",
        }
        for link, row in (
            db.query(DatasetLink, Note)
            .join(Note, and_(DatasetLink.object_type == "note", DatasetLink.object_id == Note.id))
            .filter(DatasetLink.dataset_id == int(dataset.id))
            .order_by(Note.updated_at.desc())
            .all()
        )
    ]
    linked_thesis_rows = [
        {
            "link_id": int(link.id),
            "relation": link.relation,
            "id": int(row.id),
            "title": row.title or f"thesis {row.id}",
            "href": f"/theses/{row.id}",
            "meta": "thesis",
        }
        for link, row in (
            db.query(DatasetLink, Thesis)
            .join(Thesis, and_(DatasetLink.object_type == "thesis", DatasetLink.object_id == Thesis.id))
            .filter(DatasetLink.dataset_id == int(dataset.id))
            .order_by(Thesis.updated_at.desc())
            .all()
        )
    ]
    linked_chart_rows = (
        db.query(Chart, DatasetView)
        .join(ChartDatasetView, ChartDatasetView.chart_id == Chart.id)
        .join(DatasetView, DatasetView.id == ChartDatasetView.dataset_view_id)
        .filter(DatasetView.dataset_id == int(dataset.id))
        .order_by(Chart.updated_at.desc())
        .all()
    )
    fact_rows = (
        db.query(StructuredDataFact)
        .filter(StructuredDataFact.dataset_id == int(dataset.id))
        .order_by(StructuredDataFact.updated_at.desc(), StructuredDataFact.id.desc())
        .limit(24)
        .all()
    )
    chart_suggestions = _dataset_chart_suggestions(payload)
    draft_rows_payload = list_dataset_draft_rows(db, dataset, limit=50, offset=0)
    return _template_response(
        "dataset_workbench.html",
        {
            "request": request,
            "dataset": dataset,
            "dataset_payload_json": json.dumps(payload),
            "dataset_profile_json": json.dumps(payload.get("profile") or {}),
            "dataset_dashboard_json": json.dumps(payload.get("dashboard") or {}),
            "dataset_view_builder_json": json.dumps(payload.get("view_builder") or {}),
            "dataset_draft_rows_json": json.dumps(draft_rows_payload),
            "dataset_profile": payload.get("profile") or {},
            "dataset_dashboard": payload.get("dashboard") or {},
            "dataset_view_builder": payload.get("view_builder") or {},
            "dataset_capabilities": payload.get("capabilities") or {},
            "dataset_summary": serialize_dataset_summary(db, dataset),
            "dataset_views": payload.get("views") or [],
            "dataset_rows": payload.get("rows") or [],
            "dataset_columns": payload.get("columns") or [],
            "dataset_version_rows": version_rows,
            "dataset_draft_rows": draft_rows_payload.get("rows") or [],
            "active_view": view,
            "chart_suggestions_json": json.dumps(chart_suggestions),
            "linked_request_rows": linked_request_rows,
            "linked_stage_rows": linked_stage_rows,
            "linked_supply_chain_rows": linked_supply_chain_rows,
            "linked_note_rows": linked_note_rows,
            "linked_thesis_rows": linked_thesis_rows,
            "linked_chart_rows": linked_chart_rows,
            "fact_rows": fact_rows,
        },
    )


@router.post("/api/datasets")
async def api_dataset_create(request: Request, db: Session = Depends(get_session)) -> JSONResponse:
    ensure_dataset_tables(db)
    payload = await _parse_supply_chain_payload(request)
    title = str(payload.get("title") or "").strip()
    if not title:
        return JSONResponse({"ok": False, "error": "Title is required"}, status_code=400)
    dataset = create_dataset(
        db,
        title=title,
        description=str(payload.get("description") or "").strip() or None,
        category=str(payload.get("category") or "general").strip() or "general",
        family=str(payload.get("family") or payload.get("category") or "general").strip() or "general",
        status=str(payload.get("status") or "active").strip() or "active",
        source_kind=str(payload.get("source_kind") or "").strip() or None,
        columns=payload.get("columns") if isinstance(payload.get("columns"), list) else None,
        rows=payload.get("rows") if isinstance(payload.get("rows"), list) else None,
    )
    db.commit()
    _invalidate_terminal_caches("home:", "search:")
    return JSONResponse({"ok": True, "id": int(dataset.id), "redirect": f"/visualizations/datasets/{int(dataset.id)}"})


@router.get("/api/datasets/{dataset_id}/query")
def api_dataset_query(
    dataset_id: int,
    view_id: int | None = None,
    limit: int | None = None,
    version_id: int | None = None,
    db: Session = Depends(get_session),
) -> JSONResponse:
    ensure_dataset_tables(db)
    dataset = db.query(Dataset).filter_by(id=int(dataset_id)).first()
    if not dataset:
        return JSONResponse({"ok": False, "error": "Dataset not found"}, status_code=404)
    version = None
    if version_id is not None:
        version = db.query(DatasetVersion).filter_by(id=int(version_id), dataset_id=int(dataset.id)).first()
        if version is None:
            return JSONResponse({"ok": False, "error": "Dataset version not found"}, status_code=404)
    else:
        dataset = canonical_dataset(db, dataset)
    payload = serialize_dataset(
        db,
        dataset,
        include_rows=True,
        version=version,
        view=(db.query(DatasetView).filter_by(id=int(view_id), dataset_id=int(dataset.id)).first() if view_id else None),
        limit=limit,
    )
    return JSONResponse({"ok": True, "dataset": payload})


@router.get("/api/datasets/{dataset_id}/chart-data")
def api_dataset_chart_data(
    dataset_id: int,
    view_id: int | None = None,
    columns: str | None = None,
    limit: int = 1500,
    db: Session = Depends(get_session),
) -> JSONResponse:
    ensure_dataset_tables(db)
    dataset = db.query(Dataset).filter_by(id=int(dataset_id)).first()
    if not dataset:
        return JSONResponse({"ok": False, "error": "Dataset not found"}, status_code=404)
    dataset = canonical_dataset(db, dataset)
    view = db.query(DatasetView).filter_by(id=int(view_id), dataset_id=int(dataset.id)).first() if view_id else None
    version = latest_dataset_version(db, int(dataset.id), status="draft") or latest_dataset_version(db, int(dataset.id), status="published")
    if not version:
        return JSONResponse(
            {
                "ok": True,
                "dataset": {
                    "id": int(dataset.id),
                    "slug": dataset.slug,
                    "title": dataset.title,
                    "family": dataset.family,
                    "columns": [],
                    "rows": [],
                    "row_limit": max(1, min(int(limit or 1500), 5000)),
                    "rows_returned": 0,
                },
            }
        )
    bounded_limit = max(1, min(int(limit or 1500), 5000))
    column_rows = (
        db.query(DatasetColumn)
        .filter_by(dataset_version_id=int(version.id))
        .order_by(DatasetColumn.sort_order.asc())
        .all()
    )
    column_payload = [
        {
            "key": col.key,
            "label": col.label,
            "data_type": col.data_type,
            "role": col.role,
            "sort_order": int(col.sort_order or 0),
            "config": dict(col.config or {}),
        }
        for col in column_rows
    ]
    valid_keys = {str(col["key"]) for col in column_payload}
    requested_keys = [
        key
        for key in [str(part or "").strip() for part in str(columns or "").split(",")]
        if key and key in valid_keys
    ]
    rows = query_dataset(
        db,
        dataset_id=int(dataset.id),
        version_id=int(version.id),
        view_id=int(view.id) if view else None,
        limit=bounded_limit,
    )["rows"]
    if requested_keys:
        rows = [
            {
                "row_key": row.get("row_key"),
                "values": {key: (row.get("values") or {}).get(key) for key in requested_keys},
            }
            for row in rows
        ]
    return JSONResponse(
        {
            "ok": True,
            "dataset": {
                "id": int(dataset.id),
                "slug": dataset.slug,
                "title": dataset.title,
                "family": dataset.family,
                "version": {"id": int(version.id), "version": int(version.version or 0), "status": version.status},
                "view": {"id": int(view.id), "name": view.name} if view else None,
                "columns": column_payload,
                "rows": rows,
                "row_limit": bounded_limit,
                "rows_returned": len(rows),
                "selected_columns": requested_keys,
            },
        }
    )


@router.post("/api/datasets/{dataset_id}/import")
async def api_dataset_import(
    dataset_id: int,
    csv_text: str = Form(default=""),
    upload: UploadFile | None = File(default=None),
    label: str = Form(default=""),
    publish_after: bool = Form(default=False),
    db: Session = Depends(get_session),
) -> JSONResponse:
    ensure_dataset_tables(db)
    dataset = db.query(Dataset).filter_by(id=int(dataset_id)).first()
    if not dataset:
        return JSONResponse({"ok": False, "error": "Dataset not found"}, status_code=404)
    dataset = canonical_dataset(db, dataset)
    try:
        raw = await upload.read() if upload is not None else b""
        has_upload = upload is not None and bool(str(upload.filename or "").strip()) and bool(raw)
        if has_upload:
            if str(upload.filename or "").lower().endswith(".xlsx"):
                columns, rows = parse_xlsx_dataset(raw)
            else:
                columns, rows = parse_csv_dataset(raw.decode("utf-8"))
        else:
            columns, rows = parse_csv_dataset(csv_text)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    draft = replace_dataset_draft(db, dataset, columns=columns, rows=rows, label=label or None)
    published = publish_dataset_snapshot(db, dataset, label=label or None) if publish_after else None
    db.commit()
    _invalidate_terminal_caches("home:", "search:")
    return JSONResponse(
        {
            "ok": True,
            "draft_version": draft.id,
            "published_version": published.id if published else None,
            "redirect": f"/visualizations/datasets/{dataset.id}",
        }
    )


@router.get("/api/datasets/{dataset_id}/draft-rows")
def api_dataset_draft_rows(
    dataset_id: int,
    limit: int = 50,
    offset: int = 0,
    q: str | None = None,
    db: Session = Depends(get_session),
) -> JSONResponse:
    ensure_dataset_tables(db)
    dataset = db.query(Dataset).filter_by(id=int(dataset_id)).first()
    if not dataset:
        return JSONResponse({"ok": False, "error": "Dataset not found"}, status_code=404)
    dataset = canonical_dataset(db, dataset)
    payload = list_dataset_draft_rows(db, dataset, limit=limit, offset=offset, query=q)
    return JSONResponse({"ok": True, "draft": payload})


@router.post("/api/datasets/{dataset_id}/draft-rows")
async def api_dataset_draft_row_upsert(dataset_id: int, request: Request, db: Session = Depends(get_session)) -> JSONResponse:
    ensure_dataset_tables(db)
    dataset = db.query(Dataset).filter_by(id=int(dataset_id)).first()
    if not dataset:
        return JSONResponse({"ok": False, "error": "Dataset not found"}, status_code=404)
    dataset = canonical_dataset(db, dataset)
    payload = await _parse_supply_chain_payload(request)
    values = payload.get("values") if isinstance(payload.get("values"), dict) else None
    if values is None:
        return JSONResponse({"ok": False, "error": "values is required"}, status_code=400)
    try:
        row = upsert_dataset_draft_row(
            db,
            dataset,
            row_key=str(payload.get("row_key") or "").strip() or None,
            values=values,
            sort_order=int(payload.get("sort_order")) if str(payload.get("sort_order") or "").isdigit() else None,
        )
    except ValueError as exc:
        error_payload = _dataset_error_payload(exc)
        return JSONResponse({"ok": False, **error_payload}, status_code=400)
    db.commit()
    _invalidate_terminal_caches("home:", "search:")
    return JSONResponse({"ok": True, "row": row})


@router.delete("/api/datasets/{dataset_id}/draft-rows/{row_key}")
def api_dataset_draft_row_delete(dataset_id: int, row_key: str, db: Session = Depends(get_session)) -> JSONResponse:
    ensure_dataset_tables(db)
    dataset = db.query(Dataset).filter_by(id=int(dataset_id)).first()
    if not dataset:
        return JSONResponse({"ok": False, "error": "Dataset not found"}, status_code=404)
    dataset = canonical_dataset(db, dataset)
    if not delete_dataset_draft_row(db, dataset, row_key=row_key):
        return JSONResponse({"ok": False, "error": "Draft row not found"}, status_code=404)
    db.commit()
    _invalidate_terminal_caches("home:", "search:")
    return JSONResponse({"ok": True})


@router.post("/api/datasets/{dataset_id}/draft-rows/bulk-paste")
async def api_dataset_draft_rows_bulk_paste(dataset_id: int, request: Request, db: Session = Depends(get_session)) -> JSONResponse:
    ensure_dataset_tables(db)
    dataset = db.query(Dataset).filter_by(id=int(dataset_id)).first()
    if not dataset:
        return JSONResponse({"ok": False, "error": "Dataset not found"}, status_code=404)
    dataset = canonical_dataset(db, dataset)
    payload = await _parse_supply_chain_payload(request)
    try:
        result = bulk_paste_dataset_draft_rows(db, dataset, text=str(payload.get("text") or ""))
    except ValueError as exc:
        error_payload = _dataset_error_payload(exc)
        return JSONResponse({"ok": False, **error_payload}, status_code=400)
    db.commit()
    _invalidate_terminal_caches("home:", "search:")
    return JSONResponse({"ok": True, **result})


@router.post("/api/datasets/{dataset_id}/views")
async def api_dataset_view_create(dataset_id: int, request: Request, db: Session = Depends(get_session)) -> JSONResponse:
    ensure_dataset_tables(db)
    dataset = db.query(Dataset).filter_by(id=int(dataset_id)).first()
    if not dataset:
        return JSONResponse({"ok": False, "error": "Dataset not found"}, status_code=404)
    dataset = canonical_dataset(db, dataset)
    payload = await _parse_supply_chain_payload(request)
    name = str(payload.get("name") or "").strip()
    if not name:
        return JSONResponse({"ok": False, "error": "View name is required"}, status_code=400)
    dataset_payload = serialize_dataset(db, dataset, include_rows=False)
    try:
        normalized = parse_structured_dataset_view_payload(payload, dataset_payload)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    view = create_dataset_view(
        db,
        dataset,
        name=name,
        slug=str(payload.get("slug") or "").strip() or None,
        description=str(payload.get("description") or "").strip() or None,
        filters=normalized.get("filters") if isinstance(normalized.get("filters"), dict) else None,
        columns=normalized.get("columns") if isinstance(normalized.get("columns"), list) else None,
        sort_spec=normalized.get("sort_spec") if isinstance(normalized.get("sort_spec"), list) else None,
        extra={"builder": normalized.get("builder") if isinstance(normalized.get("builder"), dict) else {}},
    )
    db.commit()
    _invalidate_terminal_caches("home:", "search:")
    return JSONResponse({"ok": True, "id": int(view.id), "view": serialize_dataset(db, dataset, include_rows=False).get("views", [])})


@router.post("/api/datasets/{dataset_id}/views/preview")
async def api_dataset_view_preview(dataset_id: int, request: Request, db: Session = Depends(get_session)) -> JSONResponse:
    ensure_dataset_tables(db)
    dataset = db.query(Dataset).filter_by(id=int(dataset_id)).first()
    if not dataset:
        return JSONResponse({"ok": False, "error": "Dataset not found"}, status_code=404)
    dataset = canonical_dataset(db, dataset)
    payload = await _parse_supply_chain_payload(request)
    dataset_payload = serialize_dataset(db, dataset, include_rows=False)
    try:
        normalized = parse_structured_dataset_view_payload(payload, dataset_payload)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    preview = preview_dataset_view(
        db,
        dataset,
        filters=normalized.get("filters"),
        selected_columns=normalized.get("columns") if isinstance(normalized.get("columns"), list) else None,
        sort_spec=normalized.get("sort_spec") if isinstance(normalized.get("sort_spec"), list) else None,
        limit=int(payload.get("limit") or 25) if str(payload.get("limit") or "").isdigit() else 25,
    )
    return JSONResponse({"ok": True, "preview": preview, "builder": normalized.get("builder") or {}})


@router.post("/api/datasets/{dataset_id}/views/{view_id}/duplicate")
async def api_dataset_view_duplicate(dataset_id: int, view_id: int, request: Request, db: Session = Depends(get_session)) -> JSONResponse:
    ensure_dataset_tables(db)
    dataset = db.query(Dataset).filter_by(id=int(dataset_id)).first()
    if not dataset:
        return JSONResponse({"ok": False, "error": "Dataset not found"}, status_code=404)
    dataset = canonical_dataset(db, dataset)
    source_view = db.query(DatasetView).filter_by(id=int(view_id), dataset_id=int(dataset.id)).first()
    if source_view is None:
        return JSONResponse({"ok": False, "error": "View not found"}, status_code=404)
    payload = await _parse_supply_chain_payload(request)
    name = str(payload.get("name") or f"{source_view.name} Copy").strip()
    view = create_dataset_view(
        db,
        dataset,
        name=name,
        slug=str(payload.get("slug") or "").strip() or None,
        description=str(payload.get("description") or source_view.description or "").strip() or None,
        filters=dict(source_view.filters or {}),
        columns=list(source_view.columns or []),
        sort_spec=list(source_view.sort_spec or []),
        extra=dict(source_view.extra or {}),
    )
    db.commit()
    _invalidate_terminal_caches("home:", "search:")
    return JSONResponse({"ok": True, "id": int(view.id), "redirect": f"/visualizations/datasets/{dataset.id}?view_id={view.id}"})


@router.post("/api/datasets/{dataset_id}/metadata")
async def api_dataset_metadata_update(dataset_id: int, request: Request, db: Session = Depends(get_session)) -> JSONResponse:
    ensure_dataset_tables(db)
    dataset = db.query(Dataset).filter_by(id=int(dataset_id)).first()
    if not dataset:
        return JSONResponse({"ok": False, "error": "Dataset not found"}, status_code=404)
    dataset = canonical_dataset(db, dataset)
    payload = await _parse_supply_chain_payload(request)
    title = str(payload.get("title") or dataset.title or "").strip()
    if not title:
        return JSONResponse({"ok": False, "error": "Title is required"}, status_code=400)
    update_dataset_metadata(
        db,
        dataset,
        title=title,
        description=str(payload.get("description") or ""),
        category=str(payload.get("category") or dataset.category or ""),
        family=str(payload.get("family") or dataset.family or ""),
        status=str(payload.get("status") or dataset.status or ""),
        source_kind=str(payload.get("source_kind") or dataset.source_kind or ""),
        provenance_summary=str(payload.get("provenance_summary") or ""),
        evidence_tier_summary=str(payload.get("evidence_tier_summary") or ""),
    )
    db.commit()
    _invalidate_terminal_caches("home:", "search:")
    return JSONResponse({"ok": True, "dataset": serialize_dataset_summary(db, dataset)})


@router.post("/api/datasets/{dataset_id}/publish")
async def api_dataset_publish(dataset_id: int, request: Request, db: Session = Depends(get_session)) -> JSONResponse:
    ensure_dataset_tables(db)
    dataset = db.query(Dataset).filter_by(id=int(dataset_id)).first()
    if not dataset:
        return JSONResponse({"ok": False, "error": "Dataset not found"}, status_code=404)
    dataset = canonical_dataset(db, dataset)
    payload = await _parse_supply_chain_payload(request)
    version = publish_dataset_snapshot(db, dataset, label=str(payload.get("label") or "").strip() or None)
    db.commit()
    _invalidate_terminal_caches("home:", "search:")
    return JSONResponse(
        {
            "ok": True,
            "version": {
                "id": int(version.id),
                "version": int(version.version),
                "status": version.status,
                "label": version.label,
            },
        }
    )


@router.post("/api/datasets/{dataset_id}/links")
async def api_dataset_link_create(dataset_id: int, request: Request, db: Session = Depends(get_session)) -> JSONResponse:
    ensure_dataset_tables(db)
    dataset = db.query(Dataset).filter_by(id=int(dataset_id)).first()
    if not dataset:
        return JSONResponse({"ok": False, "error": "Dataset not found"}, status_code=404)
    dataset = canonical_dataset(db, dataset)
    payload = await _parse_supply_chain_payload(request)
    object_type = str(payload.get("object_type") or "").strip().lower()
    raw_object_id = payload.get("object_id")
    if not object_type:
        return JSONResponse({"ok": False, "error": "object_type is required"}, status_code=400)
    if raw_object_id is None or not str(raw_object_id).isdigit():
        return JSONResponse({"ok": False, "error": "object_id is required"}, status_code=400)
    try:
        link = link_dataset(
            db,
            dataset_id=int(dataset.id),
            object_type=object_type,
            object_id=int(raw_object_id),
            relation=str(payload.get("relation") or "context"),
        )
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    db.commit()
    _invalidate_terminal_caches("home:", "search:")
    return JSONResponse(
        {
            "ok": True,
            "link": {
                "id": int(link.id),
                "object_type": link.object_type,
                "object_id": int(link.object_id),
                "relation": link.relation,
            },
        }
    )


@router.delete("/api/datasets/{dataset_id}/links/{link_id}")
def api_dataset_link_delete(dataset_id: int, link_id: int, db: Session = Depends(get_session)) -> JSONResponse:
    ensure_dataset_tables(db)
    dataset = db.query(Dataset).filter_by(id=int(dataset_id)).first()
    if not dataset:
        return JSONResponse({"ok": False, "error": "Dataset not found"}, status_code=404)
    dataset = canonical_dataset(db, dataset)
    if not unlink_dataset(db, dataset_id=int(dataset.id), link_id=int(link_id)):
        return JSONResponse({"ok": False, "error": "Link not found"}, status_code=404)
    db.commit()
    _invalidate_terminal_caches("home:", "search:")
    return JSONResponse({"ok": True})


@router.post("/api/datasets/{dataset_id}/charts")
async def api_dataset_chart_create(dataset_id: int, request: Request, db: Session = Depends(get_session)) -> JSONResponse:
    ensure_dataset_tables(db)
    dataset = db.query(Dataset).filter_by(id=int(dataset_id)).first()
    if not dataset:
        return JSONResponse({"ok": False, "error": "Dataset not found"}, status_code=404)
    dataset = canonical_dataset(db, dataset)
    payload = await _parse_supply_chain_payload(request)
    dataset_payload = serialize_dataset(db, dataset, include_rows=True, limit=500)
    view_id = int(payload.get("view_id")) if str(payload.get("view_id") or "").isdigit() else None
    view = db.query(DatasetView).filter_by(id=view_id, dataset_id=int(dataset.id)).first() if view_id is not None else None
    x_key = str(payload.get("x_key") or "").strip() or _dataset_time_column(dataset_payload)
    y_key = str(payload.get("y_key") or "").strip()
    if not x_key or not y_key:
        return JSONResponse({"ok": False, "error": "x_key and y_key are required"}, status_code=400)
    raw_series_views = payload.get("series_views")
    series_views = [item for item in raw_series_views if isinstance(item, dict)] if isinstance(raw_series_views, list) else []
    if series_views:
        valid_series = [
            item
            for item in series_views
            if str(item.get("x_key") or x_key or "").strip() and str(item.get("y_key") or y_key or "").strip()
        ]
        if not valid_series:
            return JSONResponse({"ok": False, "error": "No valid grouped chart series were supplied"}, status_code=400)
    if view is None and not series_views:
        view = create_dataset_view(
            db,
            dataset,
            name=str(payload.get("view_name") or f"{_dataset_column_label(dataset_payload, y_key)} View").strip(),
            slug=str(payload.get("view_slug") or f"auto-{x_key}-{y_key}").strip() or None,
            description=str(payload.get("view_description") or "").strip() or None,
            filters=payload.get("filters") if isinstance(payload.get("filters"), dict) else None,
            columns=[x_key, y_key],
            sort_spec=payload.get("sort_spec") if isinstance(payload.get("sort_spec"), list) else ([{"column": x_key, "direction": "asc"}] if x_key else []),
        )
    title = str(payload.get("title") or f"{dataset.title} · {_dataset_column_label(dataset_payload, y_key)}").strip()
    default_subtitle = f"{dataset.family} · {len(series_views)} grouped series" if series_views else f"{dataset.family} · {view.name}"
    chart = Chart(
        slug=_unique_chart_slug(db, title),
        title=title,
        subtitle=str(payload.get("subtitle") or default_subtitle).strip() or None,
        chart_style=str(payload.get("chart_style") or "line").strip() or "line",
        category=str(payload.get("category") or dataset.category or "general").strip() or "general",
        description_md=str(payload.get("description_md") or dataset.description or "").strip() or None,
        related_equities=str(payload.get("related_equities") or "").strip() or None,
        pinned=False,
        sort_order=0,
        created_at=utcnow(),
        updated_at=utcnow(),
        config={},
        thesis_id=None,
        note_id=None,
    )
    db.add(chart)
    db.flush()
    if series_views:
        created_links = 0
        for idx, item in enumerate(series_views[:12], start=1):
            series_x_key = str(item.get("x_key") or x_key).strip()
            series_y_key = str(item.get("y_key") or y_key).strip()
            if not series_x_key or not series_y_key:
                continue
            raw_columns = item.get("columns") if isinstance(item.get("columns"), list) else [series_x_key, series_y_key]
            columns = [str(col).strip() for col in raw_columns if str(col or "").strip()]
            for required_key in (series_x_key, series_y_key):
                if required_key not in columns:
                    columns.append(required_key)
            view_name = str(item.get("view_name") or item.get("series_name") or f"{_dataset_column_label(dataset_payload, series_y_key)} Series {idx}").strip()
            view = create_dataset_view(
                db,
                dataset,
                name=view_name,
                slug=str(item.get("view_slug") or f"auto-{x_key}-{y_key}-{idx}").strip() or None,
                description=str(item.get("view_description") or f"Grouped chart series for {title}.").strip() or None,
                filters=item.get("filters") if isinstance(item.get("filters"), dict) else None,
                columns=columns,
                sort_spec=item.get("sort_spec") if isinstance(item.get("sort_spec"), list) else ([{"column": series_x_key, "direction": "asc"}] if series_x_key else []),
            )
            db.add(
                ChartDatasetView(
                    chart_id=int(chart.id),
                    dataset_view_id=int(view.id),
                    x_key=series_x_key,
                    y_key=series_y_key,
                    series_name=str(item.get("series_name") or view.name).strip() or None,
                    y_axis_index=int(item.get("y_axis_index") or 0),
                    display_type=str(item.get("display_type") or payload.get("display_type") or "line").strip() or "line",
                    color=str(item.get("color") or "").strip() or None,
                    sort_order=idx,
                )
            )
            created_links += 1
        if not created_links:
            return JSONResponse({"ok": False, "error": "No grouped chart series could be created"}, status_code=400)
    else:
        db.add(
            ChartDatasetView(
                chart_id=int(chart.id),
                dataset_view_id=int(view.id),
                x_key=x_key,
                y_key=y_key,
                series_name=str(payload.get("series_name") or "").strip() or None,
                y_axis_index=int(payload.get("y_axis_index") or 0),
                display_type=str(payload.get("display_type") or "line").strip() or "line",
                color=str(payload.get("color") or "").strip() or None,
                sort_order=1,
            )
        )
    chart.updated_at = utcnow()
    db.add(chart)
    db.commit()
    _invalidate_terminal_caches("home:", "search:")
    return JSONResponse({"ok": True, "id": int(chart.id), "redirect": f"/visualizations/{int(chart.id)}"})


@router.get("/api/datasets/{dataset_id}/export")
def api_dataset_export(dataset_id: int, format: str = "csv", view_id: int | None = None, version_id: int | None = None, db: Session = Depends(get_session)) -> Response:
    ensure_dataset_tables(db)
    dataset = db.query(Dataset).filter_by(id=int(dataset_id)).first()
    if not dataset:
        return JSONResponse({"ok": False, "error": "Dataset not found"}, status_code=404)
    if version_id is None:
        dataset = canonical_dataset(db, dataset)
    try:
        if str(format).lower() == "xlsx":
            blob = export_dataset_xlsx_bytes(db, dataset, view_id=view_id, version_id=version_id)
            media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            suffix = "xlsx"
        else:
            blob = export_dataset_csv_bytes(db, dataset, view_id=view_id, version_id=version_id)
            media_type = "text/csv; charset=utf-8"
            suffix = "csv"
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return Response(
        content=blob,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{dataset.slug}.{suffix}"'},
    )
