from __future__ import annotations

from thoth_terminal.web.app_sections._shared import bind_app_context


bind_app_context(globals())


@app.get("/visualizations/supply-chains", response_class=HTMLResponse)
def supply_chains_view(request: Request, db: Session = Depends(get_session)) -> HTMLResponse:
    _ensure_supply_chain_tables(db)

    chains = db.query(SupplyChain).order_by(SupplyChain.updated_at.desc()).all()
    drafts = latest_chain_snapshot_map(db, status="draft")
    published_map = latest_chain_snapshot_map(db, status="published")
    items: list[dict[str, object]] = []
    for chain in chains:
        draft = drafts.get(int(chain.id))
        published = published_map.get(int(chain.id))
        data = draft.data if draft and isinstance(draft.data, dict) else (chain.data or {})
        items.append(
            {
                "chain": chain,
                "stats": {
                    "layers": len(data.get("layers") or []),
                    "nodes": len(data.get("nodes") or []),
                    "edges": len(data.get("edges") or []),
                },
                "draft": _version_payload(draft),
                "published": _version_payload(published),
            }
        )

    return _template_response(
        "supply_chains.html",
        {
            "request": request,
            "wrap_class": "wide",
            "items": items,
        },
    )


@app.get("/visualizations/supply-chains/new", response_class=HTMLResponse)
def supply_chain_new(request: Request) -> HTMLResponse:
    template = str(request.query_params.get("template") or "").strip().lower()
    if template in ("vr200", "vera-rubin-vr200", "vr200-nvl72"):
        chain_meta, chain_data = _vr200_supply_chain_template()
    else:
        chain_meta = {
            "title": "Vera Rubin Supply Chain",
            "description": "Layered graph of constraints from inputs → fab → packaging → systems.",
            "category": "semiconductors",
            "product": "NVIDIA Rubin (example)",
            "status": "active",
        }
        chain_data = _default_supply_chain_data()
    version_meta = {"draft": None, "published": None}
    return _template_response(
        "supply_chain_edit.html",
        {
            "request": request,
            "wrap_class": "wide",
            "chain": None,
            "chain_meta_json": json.dumps(chain_meta),
            "chain_data_json": json.dumps(chain_data),
            "chain_version_json": json.dumps(version_meta),
            "chain_parents_json": json.dumps([]),
        },
    )


@app.get("/visualizations/supply-chains/{chain_id}", response_class=HTMLResponse)
def supply_chain_edit(
    request: Request, chain_id: int, db: Session = Depends(get_session)
) -> HTMLResponse:
    _ensure_supply_chain_tables(db)
    chain = db.query(SupplyChain).filter_by(id=chain_id).first()
    if not chain:
        raise HTTPException(status_code=404, detail="Supply chain not found")

    draft = _ensure_draft_version(db, chain, fallback_data=chain.data or {})
    published = _latest_chain_version(db, chain.id, status="published")
    ticker_display_mode = _cookie_choice(request, _TICKER_DISPLAY_COOKIE, allowed={"local", "adr"}, default="local")
    payload = serialize_supply_chain(db, chain, snapshot=draft, include_graph=True, ticker_display=ticker_display_mode)
    data = dict(payload.get("data") or {})
    data.setdefault("about", {})
    data.setdefault("layers", [])
    data.setdefault("nodes", [])
    data.setdefault("edges", [])
    data.setdefault("bindings", [])
    data.setdefault("alerts", [])
    chain_meta = {
        "title": chain.title,
        "description": chain.description or "",
        "category": chain.category or "general",
        "product": chain.product or "",
        "status": chain.status or "active",
    }
    parents = (
        db.query(SupplyChain)
        .join(SupplyChainRelation, SupplyChainRelation.parent_chain_id == SupplyChain.id)
        .filter(SupplyChainRelation.child_chain_id == chain.id)
        .order_by(SupplyChain.title.asc())
        .all()
    )
    parent_payload = [
        {"id": p.id, "title": p.title, "category": p.category, "product": p.product}
        for p in parents
    ]
    version_meta = {"draft": _version_payload(draft), "published": _version_payload(published)}
    return _template_response(
        "supply_chain_edit.html",
        {
            "request": request,
            "wrap_class": "wide",
            "chain": chain,
            "chain_meta_json": json.dumps(chain_meta),
            "chain_data_json": json.dumps(data),
            "chain_version_json": json.dumps(version_meta),
            "chain_parents_json": json.dumps(parent_payload),
        },
    )


@app.get("/visualizations/supply-chains/{chain_id}/command-center", response_class=HTMLResponse)
def supply_chain_command_center(
    request: Request, chain_id: int, db: Session = Depends(get_session)
) -> HTMLResponse:
    _ensure_supply_chain_tables(db)
    ensure_dataset_tables(db)
    chain = db.query(SupplyChain).filter_by(id=chain_id).first()
    if not chain:
        raise HTTPException(status_code=404, detail="Supply chain not found")
    import_legacy_chain_if_needed(db, chain)
    draft = ensure_draft_snapshot(db, chain)
    published = latest_chain_snapshot(db, chain.id, status="published")
    focus_node = str(request.query_params.get("node") or "").strip() or None
    ticker_display_mode = _cookie_choice(request, _TICKER_DISPLAY_COOKIE, allowed={"local", "adr"}, default="local")
    payload = serialize_supply_chain(
        db,
        chain,
        snapshot=draft,
        include_graph=True,
        focus_node=focus_node,
        ticker_display=ticker_display_mode,
    )
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    nodes = [dict(row) for row in (data.get("nodes") or []) if isinstance(row, dict)]
    edges = [dict(row) for row in (data.get("edges") or []) if isinstance(row, dict)]
    bindings = [dict(row) for row in (data.get("bindings") or []) if isinstance(row, dict)]
    alerts = list_supply_chain_alerts(db, chain.id)
    delta = _select_command_center_delta(
        db,
        chain,
        draft=draft,
        published=published,
        ticker_display=ticker_display_mode,
    )

    top_bottlenecks = sorted(nodes, key=lambda row: float(row.get("bottleneck") or 0), reverse=True)[:8]
    network_payload = _supply_chain_network_payload(data, delta=delta)
    binding_cards = _supply_chain_binding_cards(db, bindings=bindings, focus_node=focus_node, limit=6)
    linked_context = _supply_chain_linked_context_payload(db, int(chain.id))
    exposure_summary = payload.get("exposure_summary") if isinstance(payload.get("exposure_summary"), dict) else {"companies": [], "count": 0}
    single_source_watchlist = _single_source_watchlist(nodes)
    node_payload = (
        _supply_chain_node_api_payload(db, chain, node_key=focus_node, snapshot=draft, ticker_display=ticker_display_mode)
        if focus_node
        else None
    )

    return _template_response(
        "supply_chain_command_center.html",
        {
            "request": request,
            "wrap_class": "wide",
            "chain": chain,
            "chain_payload": payload,
            "command_center_json": json.dumps(
                {
                    "chain": {
                        "id": int(chain.id),
                        "title": chain.title,
                        "category": chain.category,
                        "product": chain.product,
                    },
                    "about": payload.get("about"),
                    "legend": payload.get("legend"),
                    "lints": payload.get("lints"),
                    "network": network_payload,
                    "binding_cards": binding_cards,
                    "delta": delta,
                    "top_bottlenecks": top_bottlenecks,
                    "exposure_summary": exposure_summary,
                    "single_source_watchlist": single_source_watchlist,
                    "ticker_display_mode": ticker_display_mode,
                    "selected_node": payload.get("selected_node"),
                    "dossier": payload.get("dossier"),
                    "linked_context": linked_context,
                    "node_payload": node_payload,
                    "focus_node": focus_node,
                }
            ),
            "top_bottlenecks": top_bottlenecks,
            "exposure_summary": exposure_summary,
            "single_source_watchlist": single_source_watchlist,
            "binding_cards": binding_cards,
            "alerts": alerts,
            "delta": delta,
            "linked_context": linked_context,
        },
    )


async def _parse_supply_chain_payload(request: Request) -> dict[str, object]:
    payload: dict[str, object] = {}
    try:
        payload = await request.json()
    except Exception:
        try:
            form = await request.form()
            payload = dict(form)
        except Exception:
            payload = {}
    return payload or {}


@app.post("/api/supply-chains")
async def api_supply_chain_create(
    request: Request, db: Session = Depends(get_session)
) -> JSONResponse:
    payload = await _parse_supply_chain_payload(request)
    title = str(payload.get("title") or "Untitled supply chain").strip()
    slug = _slugify_title(title)[:100]
    _ensure_supply_chain_tables(db)
    existing = db.query(SupplyChain).filter(SupplyChain.slug.like(f"{slug}%")).count()
    if existing:
        slug = f"{slug}-{existing + 1}"

    data = payload.get("data") or {}
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            data = {}

    chain = create_supply_chain(
        db,
        title=title,
        description=(payload.get("description") or "") or None,
        category=str(payload.get("category") or "general"),
        product=str(payload.get("product") or "") or None,
        status=str(payload.get("status") or "active"),
        slug=slug,
        data=data if isinstance(data, dict) else {},
    )
    draft = ensure_draft_snapshot(db, chain)
    db.commit()
    return JSONResponse(
        {
            "ok": True,
            "id": chain.id,
            "redirect": f"/visualizations/supply-chains/{chain.id}",
            "version": _version_payload(draft),
        }
    )


@app.post("/api/supply-chains/{chain_id}")
async def api_supply_chain_update(
    chain_id: int, request: Request, db: Session = Depends(get_session)
) -> JSONResponse:
    _ensure_supply_chain_tables(db)
    chain = db.query(SupplyChain).filter_by(id=chain_id).first()
    if not chain:
        return JSONResponse({"ok": False, "error": "Supply chain not found"}, status_code=404)

    payload = await _parse_supply_chain_payload(request)
    title = str(payload.get("title") or chain.title or "Supply chain").strip()
    data = payload.get("data") or {}
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            data = {}

    chain.title = title
    chain.description = (payload.get("description") or "") or None
    chain.category = str(payload.get("category") or chain.category or "general")
    chain.product = (payload.get("product") or "") or None
    chain.status = str(payload.get("status") or chain.status or "active")
    draft = replace_draft_snapshot_graph(db, chain, data if isinstance(data, dict) else {})
    db.commit()
    return JSONResponse({"ok": True, "id": chain.id, "version": _version_payload(draft)})


@app.post("/api/supply-chains/{chain_id}/duplicate")
def api_supply_chain_duplicate(
    chain_id: int, db: Session = Depends(get_session)
) -> JSONResponse:
    _ensure_supply_chain_tables(db)
    chain = db.query(SupplyChain).filter_by(id=chain_id).first()
    if not chain:
        return JSONResponse({"ok": False, "error": "Supply chain not found"}, status_code=404)

    base_title = f"{chain.title} Copy"
    slug = _slugify_title(base_title)[:100]
    existing = db.query(SupplyChain).filter(SupplyChain.slug.like(f"{slug}%")).count()
    if existing:
        slug = f"{slug}-{existing + 1}"

    import_legacy_chain_if_needed(db, chain)
    draft = ensure_draft_snapshot(db, chain)
    cloned_data = graph_from_snapshot(db, draft, chain_id=int(chain.id))
    new_chain = create_supply_chain(
        db,
        title=base_title,
        description=chain.description,
        category=chain.category,
        product=chain.product,
        status=chain.status,
        slug=slug,
        data=cloned_data,
    )
    db.commit()
    return JSONResponse(
        {"ok": True, "id": new_chain.id, "redirect": f"/visualizations/supply-chains/{new_chain.id}"}
    )


@app.post("/api/supply-chains/{chain_id}/publish")
async def api_supply_chain_publish(
    chain_id: int, request: Request, db: Session = Depends(get_session)
) -> JSONResponse:
    _ensure_supply_chain_tables(db)
    chain = db.query(SupplyChain).filter_by(id=chain_id).first()
    if not chain:
        return JSONResponse({"ok": False, "error": "Supply chain not found"}, status_code=404)
    payload = await _parse_supply_chain_payload(request)
    label = str(payload.get("label") or "").strip() or None
    version = publish_supply_chain_snapshot(db, chain, label=label)
    db.commit()
    return JSONResponse(
        {
            "ok": True,
            "version": _version_payload(version),
            "published_at": version.updated_at.isoformat() if version.updated_at else None,
        }
    )


@app.post("/api/supply-chains/{chain_id}/delete")
def api_supply_chain_delete(
    chain_id: int, db: Session = Depends(get_session)
) -> JSONResponse:
    _ensure_supply_chain_tables(db)
    chain = db.query(SupplyChain).filter_by(id=chain_id).first()
    if not chain:
        return JSONResponse({"ok": False, "error": "Supply chain not found"}, status_code=404)
    snapshot_ids = [
        int(row[0])
        for row in db.query(SupplyChainSnapshot.id).filter_by(chain_id=chain_id).all()
    ]
    db.query(SupplyChainNode).filter(
        SupplyChainNode.snapshot_id.in_(snapshot_ids)
    ).delete(synchronize_session=False)
    db.query(SupplyChainEdge).filter(
        SupplyChainEdge.snapshot_id.in_(snapshot_ids)
    ).delete(synchronize_session=False)
    db.query(SupplyChainMetricBinding).filter(
        SupplyChainMetricBinding.snapshot_id.in_(snapshot_ids)
    ).delete(synchronize_session=False)
    db.query(SupplyChainSnapshot).filter_by(chain_id=chain_id).delete(synchronize_session=False)
    db.query(SupplyChainScenario).filter_by(chain_id=chain_id).delete(synchronize_session=False)
    db.query(SupplyChainAlertRule).filter_by(chain_id=chain_id).delete(synchronize_session=False)
    db.query(SupplyChainLink).filter_by(chain_id=chain_id).delete(synchronize_session=False)
    db.query(SupplyChainVersion).filter_by(chain_id=chain_id).delete(synchronize_session=False)
    db.query(SupplyChainRelation).filter(
        (SupplyChainRelation.child_chain_id == chain_id)
        | (SupplyChainRelation.parent_chain_id == chain_id)
    ).delete(synchronize_session=False)
    db.delete(chain)
    db.commit()
    return JSONResponse({"ok": True})


@app.get("/api/supply-chains/search")
def api_supply_chain_search(
    q: str = "", exclude_id: int | None = None, db: Session = Depends(get_session)
) -> JSONResponse:
    _ensure_supply_chain_tables(db)
    query = (q or "").strip().lower()
    results = []
    chains = db.query(SupplyChain).order_by(SupplyChain.updated_at.desc()).limit(50).all()
    for chain in chains:
        if exclude_id and chain.id == exclude_id:
            continue
        hay = " ".join([chain.title or "", chain.product or "", chain.category or ""]).lower()
        if query and query not in hay:
            continue
        results.append(
            {
                "id": chain.id,
                "title": chain.title,
                "category": chain.category,
                "product": chain.product,
                "updated_at": chain.updated_at.isoformat() if chain.updated_at else None,
            }
        )
    return JSONResponse({"ok": True, "results": results})


@app.get("/api/supply-chains/{chain_id}/parents")
def api_supply_chain_parents(
    chain_id: int, db: Session = Depends(get_session)
) -> JSONResponse:
    _ensure_supply_chain_tables(db)
    parents = (
        db.query(SupplyChain)
        .join(SupplyChainRelation, SupplyChainRelation.parent_chain_id == SupplyChain.id)
        .filter(SupplyChainRelation.child_chain_id == chain_id)
        .order_by(SupplyChain.title.asc())
        .all()
    )
    payload = [
        {"id": p.id, "title": p.title, "category": p.category, "product": p.product}
        for p in parents
    ]
    return JSONResponse({"ok": True, "parents": payload})


@app.post("/api/supply-chains/{chain_id}/component-of")
def api_supply_chain_component_of(
    chain_id: int, parent_id: int = Form(...), db: Session = Depends(get_session)
) -> JSONResponse:
    _ensure_supply_chain_tables(db)
    if chain_id == parent_id:
        return JSONResponse({"ok": False, "error": "Cannot link chain to itself."}, status_code=400)
    chain = db.query(SupplyChain).filter_by(id=chain_id).first()
    parent = db.query(SupplyChain).filter_by(id=parent_id).first()
    if not chain or not parent:
        return JSONResponse({"ok": False, "error": "Supply chain not found"}, status_code=404)
    existing = (
        db.query(SupplyChainRelation)
        .filter_by(parent_chain_id=parent_id, child_chain_id=chain_id, relation_type="component_of")
        .first()
    )
    if existing:
        return JSONResponse({"ok": True, "relation_id": existing.id})
    now = utcnow()
    rel = SupplyChainRelation(
        parent_chain_id=parent_id,
        child_chain_id=chain_id,
        relation_type="component_of",
        created_at=now,
    )
    db.add(rel)
    db.commit()
    return JSONResponse({"ok": True, "relation_id": rel.id})


@app.delete("/api/supply-chains/{chain_id}/component-of/{parent_id}")
def api_supply_chain_component_of_delete(
    chain_id: int, parent_id: int, db: Session = Depends(get_session)
) -> JSONResponse:
    _ensure_supply_chain_tables(db)
    deleted = (
        db.query(SupplyChainRelation)
        .filter_by(parent_chain_id=parent_id, child_chain_id=chain_id, relation_type="component_of")
        .delete(synchronize_session=False)
    )
    db.commit()
    return JSONResponse({"ok": True, "deleted": int(deleted or 0)})


@app.get("/api/supply-chains/{chain_id}/compare")
def api_supply_chain_compare(
    chain_id: int,
    left_version: str = "published",
    right_version: str = "draft",
    db: Session = Depends(get_session),
) -> JSONResponse:
    _ensure_supply_chain_tables(db)
    chain = db.query(SupplyChain).filter_by(id=chain_id).first()
    if not chain:
        return JSONResponse({"ok": False, "error": "Supply chain not found"}, status_code=404)

    def _resolve(arg: str) -> SupplyChainSnapshot | None:
        if str(arg).isdigit():
            return db.query(SupplyChainSnapshot).filter_by(chain_id=chain_id, version=int(arg), status="published").first()
        if str(arg).lower() in {"draft", "latest", "current"}:
            return latest_chain_snapshot(db, chain_id, status="draft")
        if str(arg).lower() in {"published", "release"}:
            return latest_chain_snapshot(db, chain_id, status="published")
        return None

    return JSONResponse({"ok": True, "comparison": compare_snapshots(db, _resolve(left_version), _resolve(right_version), chain_id=chain_id)})


@app.get("/api/supply-chains/{chain_id}/nodes/{node_key}")
def api_supply_chain_node(
    request: Request,
    chain_id: int,
    node_key: str,
    version: str = "draft",
    db: Session = Depends(get_session),
) -> JSONResponse:
    _ensure_supply_chain_tables(db)
    ensure_dataset_tables(db)
    chain = db.query(SupplyChain).filter_by(id=chain_id).first()
    if not chain:
        return JSONResponse({"ok": False, "error": "Supply chain not found"}, status_code=404)

    def _resolve(arg: str) -> SupplyChainSnapshot | None:
        if str(arg).isdigit():
            return db.query(SupplyChainSnapshot).filter_by(chain_id=chain_id, version=int(arg), status="published").first()
        if str(arg).lower() in {"draft", "latest", "current"}:
            return latest_chain_snapshot(db, chain_id, status="draft")
        if str(arg).lower() in {"published", "release"}:
            return latest_chain_snapshot(db, chain_id, status="published")
        return None

    ticker_display_mode = _cookie_choice(request, _TICKER_DISPLAY_COOKIE, allowed={"local", "adr"}, default="local")
    payload = _supply_chain_node_api_payload(
        db,
        chain,
        node_key=node_key,
        snapshot=_resolve(version),
        ticker_display=ticker_display_mode,
    )
    if not payload:
        return JSONResponse({"ok": False, "error": "Supply chain node not found"}, status_code=404)
    return JSONResponse({"ok": True, **payload})


@app.get("/api/supply-chains/{chain_id}/nodes/{node_key}/subchain")
def api_supply_chain_node_subchain(
    chain_id: int,
    node_key: str,
    version: str = "draft",
    db: Session = Depends(get_session),
) -> JSONResponse:
    _ensure_supply_chain_tables(db)
    chain = db.query(SupplyChain).filter_by(id=chain_id).first()
    if not chain:
        return JSONResponse({"ok": False, "error": "Supply chain not found"}, status_code=404)

    def _resolve(arg: str) -> SupplyChainSnapshot | None:
        if str(arg).isdigit():
            return db.query(SupplyChainSnapshot).filter_by(chain_id=chain_id, version=int(arg), status="published").first()
        if str(arg).lower() in {"draft", "latest", "current"}:
            return latest_chain_snapshot(db, chain_id, status="draft")
        if str(arg).lower() in {"published", "release"}:
            return latest_chain_snapshot(db, chain_id, status="published")
        return None

    preview = get_supply_chain_subchain_preview(db, chain, node_key, snapshot=_resolve(version))
    if not preview:
        return JSONResponse({"ok": False, "error": "Linked subchain not found"}, status_code=404)
    if str(preview.get("id") or "").isdigit():
        preview["href"] = f"/visualizations/supply-chains/{int(preview['id'])}/command-center"
    return JSONResponse({"ok": True, "subchain": preview})


@app.post("/api/supply-chains/{chain_id}/bindings")
async def api_supply_chain_bind_dataset(chain_id: int, request: Request, db: Session = Depends(get_session)) -> JSONResponse:
    _ensure_supply_chain_tables(db)
    ensure_dataset_tables(db)
    chain = db.query(SupplyChain).filter_by(id=chain_id).first()
    if not chain:
        return JSONResponse({"ok": False, "error": "Supply chain not found"}, status_code=404)
    payload = await _parse_supply_chain_payload(request)
    dataset_id = payload.get("dataset_id")
    metric_kind = str(payload.get("metric_kind") or "").strip()
    if dataset_id is None or not str(dataset_id).isdigit():
        return JSONResponse({"ok": False, "error": "dataset_id is required"}, status_code=400)
    if not metric_kind:
        return JSONResponse({"ok": False, "error": "metric_kind is required"}, status_code=400)
    binding = bind_supply_chain_dataset(
        db,
        chain,
        dataset_id=int(dataset_id),
        dataset_view_id=int(payload["dataset_view_id"]) if str(payload.get("dataset_view_id") or "").isdigit() else None,
        scope_type=str(payload.get("scope_type") or "chain"),
        scope_key=str(payload.get("scope_key") or "chain"),
        metric_kind=metric_kind,
        column_key=str(payload.get("column_key") or "") or None,
        aggregation=str(payload.get("aggregation") or "") or None,
    )
    db.commit()
    return JSONResponse(
        {
            "ok": True,
            "binding": {
                "id": int(binding.id),
                "dataset_id": int(binding.dataset_id) if binding.dataset_id is not None else None,
                "dataset_view_id": int(binding.dataset_view_id) if binding.dataset_view_id is not None else None,
                "scope_type": binding.scope_type,
                "scope_key": binding.scope_key,
                "metric_kind": binding.metric_kind,
                "column_key": binding.column_key,
                "aggregation": binding.aggregation,
            },
        }
    )


@app.get("/api/supply-chains/{chain_id}/alerts")
def api_supply_chain_alerts(chain_id: int, db: Session = Depends(get_session)) -> JSONResponse:
    _ensure_supply_chain_tables(db)
    chain = db.query(SupplyChain).filter_by(id=chain_id).first()
    if not chain:
        return JSONResponse({"ok": False, "error": "Supply chain not found"}, status_code=404)
    return JSONResponse({"ok": True, "alerts": list_supply_chain_alerts(db, chain_id)})
