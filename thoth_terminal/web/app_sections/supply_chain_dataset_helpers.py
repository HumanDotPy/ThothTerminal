from __future__ import annotations

from thoth_terminal.web.app_sections._shared import bind_app_context


bind_app_context(globals())


def _version_payload(version: SupplyChainSnapshot | SupplyChainVersion | None) -> dict[str, object] | None:
    if isinstance(version, SupplyChainSnapshot):
        return supply_chain_version_payload(version)
    if not version:
        return None
    return {
        "id": version.id,
        "version": version.version,
        "status": version.status,
        "label": getattr(version, "label", None),
        "parent_version_id": getattr(version, "parent_version_id", None),
        "created_at": version.created_at.isoformat() if version.created_at else None,
        "updated_at": version.updated_at.isoformat() if version.updated_at else None,
    }


def _supply_chain_payload(
    chain: SupplyChain,
    *,
    data: dict[str, object] | None = None,
    include_data: bool = True,
    version: SupplyChainSnapshot | SupplyChainVersion | None = None,
) -> dict[str, object]:
    with session_scope() as session:
        bound = session.query(SupplyChain).filter_by(id=int(chain.id)).first()
        if not bound:
            resolved = data if isinstance(data, dict) else (chain.data if isinstance(chain.data, dict) else {})
            return {
                "id": chain.id,
                "slug": chain.slug,
                "title": chain.title,
                "description": chain.description,
                "category": chain.category,
                "product": chain.product,
                "status": chain.status,
                "created_at": chain.created_at.isoformat() if chain.created_at else None,
                "updated_at": chain.updated_at.isoformat() if chain.updated_at else None,
                "counts": {
                    "layers": len(resolved.get("layers") or []),
                    "nodes": len(resolved.get("nodes") or []),
                    "edges": len(resolved.get("edges") or []),
                },
                "data": resolved if include_data else None,
            }
        snapshot = version if isinstance(version, SupplyChainSnapshot) else None
        payload = serialize_supply_chain(session, bound, snapshot=snapshot, include_graph=include_data)
        if data is not None and include_data:
            payload["data"] = data
        return payload


def _supply_chain_state_summary(
    chain: SupplyChain,
    *,
    data: dict[str, object] | None = None,
    version: SupplyChainSnapshot | SupplyChainVersion | None = None,
) -> str:
    with session_scope() as session:
        bound = session.query(SupplyChain).filter_by(id=int(chain.id)).first()
        if not bound:
            return str(chain.title or f"chain {chain.id}")
        snapshot = version if isinstance(version, SupplyChainSnapshot) else None
        return supply_chain_state_summary(session, bound, snapshot=snapshot)


def _ensure_supply_chain_tables(session: Session) -> None:
    ensure_supply_chain_snapshot_tables(session)


def _supply_chain_linked_context_payload(session: Session, chain_id: int) -> dict[str, list[dict[str, object]]]:
    link_rows = session.query(SupplyChainLink).filter_by(chain_id=int(chain_id)).all()
    note_ids = [int(row.object_id) for row in link_rows if row.object_type == "note"]
    thesis_ids = [int(row.object_id) for row in link_rows if row.object_type == "thesis"]
    linked_notes = (
        session.query(Note).filter(Note.id.in_(note_ids)).order_by(Note.updated_at.desc()).all()
        if note_ids
        else []
    )
    linked_theses = (
        session.query(Thesis).filter(Thesis.id.in_(thesis_ids)).order_by(Thesis.updated_at.desc()).all()
        if thesis_ids
        else []
    )
    return {
        "notes": [
            {"id": int(note.id), "title": note.title, "href": f"/notes/{int(note.id)}"}
            for note in linked_notes
        ],
        "theses": [
            {"id": int(thesis.id), "title": thesis.title, "href": f"/theses/{int(thesis.id)}"}
            for thesis in linked_theses
        ],
    }


def _supply_chain_binding_cards(
    session: Session,
    *,
    bindings: list[dict[str, object]],
    focus_node: str | None = None,
    limit: int = 6,
) -> list[dict[str, object]]:
    focus_key = str(focus_node or "").strip()
    rows: list[dict[str, object]] = []
    for binding in bindings:
        if not isinstance(binding, dict):
            continue
        scope_type = str(binding.get("scope_type") or "chain").strip().lower() or "chain"
        scope_key = str(binding.get("scope_key") or "chain").strip() or "chain"
        if focus_key:
            if scope_type == "node" and scope_key == focus_key:
                scope_rank = 0
            elif scope_type == "chain":
                scope_rank = 1
            else:
                continue
        else:
            scope_rank = 0 if scope_type == "chain" else 1
        dataset = (
            session.query(Dataset).filter_by(id=int(binding["dataset_id"])).first()
            if str(binding.get("dataset_id") or "").isdigit()
            else None
        )
        if not dataset:
            continue
        view = (
            session.query(DatasetView)
            .filter_by(id=int(binding["dataset_view_id"]), dataset_id=int(dataset.id))
            .first()
            if str(binding.get("dataset_view_id") or "").isdigit()
            else None
        )
        dataset_payload = serialize_dataset(session, dataset, include_rows=True, view=view, limit=60)
        suggestions = _dataset_chart_suggestions(dataset_payload)
        chart = suggestions[0] if suggestions else None
        series = list(chart.get("series") or []) if isinstance(chart, dict) else []
        numeric_points = [
            {"date": row.get("date"), "value": float(row.get("value"))}
            for row in series
            if isinstance(row, dict) and row.get("value") not in (None, "")
        ]
        latest_point = numeric_points[-1] if numeric_points else None
        previous_point = numeric_points[-2] if len(numeric_points) > 1 else None
        latest_value = latest_point["value"] if latest_point else None
        previous_value = previous_point["value"] if previous_point else None
        delta_abs = (latest_value - previous_value) if latest_value is not None and previous_value is not None else None
        delta_pct = (
            (delta_abs / previous_value)
            if delta_abs is not None and previous_value not in (None, 0)
            else None
        )
        dataset_summary = serialize_dataset_summary(session, dataset)
        rows.append(
            {
                "binding": binding,
                "dataset": dataset_summary,
                "chart": chart,
                "scope_rank": scope_rank,
                "is_focus_scope": bool(focus_key and scope_type == "node" and scope_key == focus_key),
                "latest_value": latest_value,
                "previous_value": previous_value,
                "delta_abs": delta_abs,
                "delta_pct": delta_pct,
                "latest_date": latest_point["date"] if latest_point else None,
                "observation_count": len(numeric_points),
                "row_count": int(((dataset_summary.get("counts") or {}).get("rows") or 0)),
            }
        )
    rows.sort(
        key=lambda card: (
            int(card.get("scope_rank") or 99),
            str((((card.get("binding") or {}) if isinstance(card.get("binding"), dict) else {}).get("metric_kind") or "")),
        )
    )
    return rows[: max(1, int(limit or 6))]


def _supply_chain_network_payload(
    data: dict[str, object],
    *,
    delta: dict[str, object] | None = None,
) -> dict[str, object]:
    nodes = [dict(row) for row in (data.get("nodes") or []) if isinstance(row, dict)]
    edges = [dict(row) for row in (data.get("edges") or []) if isinstance(row, dict)]
    layers = [dict(row) for row in (data.get("layers") or []) if isinstance(row, dict)]
    layer_order = [str(layer.get("id") or layer.get("name") or f"layer_{idx}") for idx, layer in enumerate(layers)]
    category_map = {layer_id: idx for idx, layer_id in enumerate(layer_order)}
    delta_nodes_added = {
        str(row.get("id") or "")
        for row in (delta.get("nodes_added") or [])
        if isinstance(row, dict) and str(row.get("id") or "").strip()
    } if isinstance(delta, dict) else set()
    delta_nodes_changed = {
        str((row.get("after") or {}).get("id") or row.get("node_id") or "")
        for row in (delta.get("nodes_changed") or [])
        if isinstance(row, dict) and str((row.get("after") or {}).get("id") or row.get("node_id") or "").strip()
    } if isinstance(delta, dict) else set()
    return {
        "categories": [
            {
                "name": str(layer.get("name") or layer.get("id") or "Layer"),
                "itemStyle": {"color": str(layer.get("color") or "#64748b")},
            }
            for layer in layers
        ]
        or [{"name": "Chain", "itemStyle": {"color": "#64748b"}}],
        "nodes": [
            {
                "id": str(node.get("id") or ""),
                "name": str(node.get("name") or node.get("id") or ""),
                "category": category_map.get(str(node.get("layer") or ""), 0),
                "symbolSize": [
                    max(132, min(228, 96 + (len(str(node.get("name") or "")) * 5))),
                    42,
                ],
                "value": float(node.get("bottleneck") or 0),
                "leadTime": int(node.get("lead_time_months") or 0),
                "bottleneck": float(node.get("bottleneck") or 0),
                "pricingPower": float(node.get("pricing_power") or 0),
                "shape_key": str(node.get("shape_key") or ""),
                "stage_family": str(node.get("stage_family") or ""),
                "layer_name": str(node.get("layer_name") or node.get("layer") or ""),
                "lint_count": len(node.get("lint") or []),
                "has_child_chain": bool(node.get("has_child_chain")),
                "exposure_preview": [dict(row) for row in (node.get("exposure_preview") or []) if isinstance(row, dict)],
                "exposure_counts": dict(node.get("exposure", {}).get("counts") or {}) if isinstance(node.get("exposure"), dict) else {},
                "delta_state": (
                    "added"
                    if str(node.get("id") or "") in delta_nodes_added
                    else "changed" if str(node.get("id") or "") in delta_nodes_changed else None
                ),
            }
            for node in nodes
        ],
        "links": [
            {
                "source": str(edge.get("source") or ""),
                "target": str(edge.get("target") or ""),
                "value": float(edge.get("criticality") or 0),
                "type": str(edge.get("type") or "materials"),
                "lint_count": len(edge.get("lint") or []),
            }
            for edge in edges
        ],
    }


def _supply_chain_node_api_payload(
    session: Session,
    chain: SupplyChain,
    *,
    node_key: str,
    snapshot: SupplyChainSnapshot | None = None,
    ticker_display: str = "local",
) -> dict[str, object] | None:
    serialized = serialize_supply_chain(
        session,
        chain,
        snapshot=snapshot,
        include_graph=True,
        focus_node=node_key,
        ticker_display=ticker_display,
    )
    selected = serialized.get("selected_node") if isinstance(serialized.get("selected_node"), dict) else None
    dossier = serialized.get("dossier") if isinstance(serialized.get("dossier"), dict) else None
    if not selected or not dossier:
        return None
    graph = serialized.get("data") if isinstance(serialized.get("data"), dict) else {}
    bindings = [dict(row) for row in (graph.get("bindings") or []) if isinstance(row, dict)]
    return {
        "node": selected,
        "dossier": dossier,
        "binding_cards": _supply_chain_binding_cards(session, bindings=bindings, focus_node=node_key, limit=6),
        "linked_context": _supply_chain_linked_context_payload(session, int(chain.id)),
        "legend": serialized.get("legend") if isinstance(serialized.get("legend"), dict) else {"families": supply_chain_stage_family_legend()},
        "lints": [dict(row) for row in (serialized.get("lints") or []) if isinstance(row, dict)],
    }


def _delta_has_changes(delta: dict[str, object] | None) -> bool:
    if not isinstance(delta, dict):
        return False
    keys = (
        "nodes_added",
        "nodes_removed",
        "nodes_changed",
        "edges_added",
        "edges_removed",
        "edges_changed",
        "bindings_added",
        "bindings_removed",
        "bindings_changed",
    )
    for key in keys:
        if len(delta.get(key) or []):
            return True
    exposure_delta = delta.get("exposure_delta") if isinstance(delta.get("exposure_delta"), dict) else {}
    return any(len(exposure_delta.get(key) or []) for key in ("companies_added", "companies_removed", "companies_increased"))


def _single_source_watchlist(nodes: list[dict[str, object]]) -> list[dict[str, object]]:
    rows = []
    for node in nodes:
        options = [dict(row) for row in (node.get("options") or []) if isinstance(row, dict)]
        manufacturer = str(node.get("manufacturer") or "").strip()
        source_count = len(options) if options else (1 if manufacturer else 0)
        if source_count != 1:
            continue
        rows.append(
            {
                "id": str(node.get("id") or ""),
                "name": str(node.get("name") or node.get("id") or ""),
                "manufacturer": manufacturer or str((options[0].get("company") or options[0].get("name") or "")) if options else "",
                "lead_time_months": int(node.get("lead_time_months") or 0),
                "bottleneck": float(node.get("bottleneck") or 0),
                "pricing_power": float(node.get("pricing_power") or 0),
            }
        )
    rows.sort(
        key=lambda row: (
            -float(row.get("bottleneck") or 0),
            -float(row.get("pricing_power") or 0),
            -int(row.get("lead_time_months") or 0),
            str(row.get("name") or "").lower(),
        )
    )
    return rows[:10]


def _select_command_center_delta(
    session: Session,
    chain: SupplyChain,
    *,
    draft: SupplyChainSnapshot | None,
    published: SupplyChainSnapshot | None,
    ticker_display: str,
) -> dict[str, object] | None:
    if published and draft:
        draft_delta = compare_snapshots(
            session,
            published,
            draft,
            chain_id=int(chain.id),
            ticker_display=ticker_display,
        )
        if _delta_has_changes(draft_delta):
            draft_delta["comparison_label"] = "Draft vs latest published"
            draft_delta["comparison_mode"] = "draft_vs_published"
            return draft_delta
    published_rows = (
        session.query(SupplyChainSnapshot)
        .filter_by(chain_id=int(chain.id), status="published")
        .order_by(SupplyChainSnapshot.version.desc(), SupplyChainSnapshot.updated_at.desc())
        .limit(2)
        .all()
    )
    if len(published_rows) >= 2:
        release_delta = compare_snapshots(
            session,
            published_rows[1],
            published_rows[0],
            chain_id=int(chain.id),
            ticker_display=ticker_display,
        )
        release_delta["comparison_label"] = "Latest published vs previous published"
        release_delta["comparison_mode"] = "published_vs_previous"
        return release_delta
    return None


def _latest_chain_version(
    session: Session, chain_id: int, *, status: str | None = None
) -> SupplyChainSnapshot | None:
    return latest_chain_snapshot(session, chain_id, status=status)


def _next_chain_version_num(session: Session, chain_id: int) -> int:
    max_ver = (
        session.query(func.max(SupplyChainSnapshot.version))
        .filter_by(chain_id=chain_id, status="published")
        .scalar()
    )
    try:
        return int(max_ver or 0) + 1
    except Exception:
        return 1


def _ensure_draft_version(
    session: Session, chain: SupplyChain, fallback_data: dict[str, object] | None = None
) -> SupplyChainSnapshot:
    draft = ensure_draft_snapshot(session, chain)
    if fallback_data and not graph_from_snapshot(session, draft, chain_id=int(chain.id)).get("nodes"):
        replace_draft_snapshot_graph(session, chain, dict(fallback_data))
        draft = ensure_draft_snapshot(session, chain)
    return draft


def _pin_references_for_publish(session: Session, data: dict[str, object]) -> dict[str, object]:
    if not isinstance(data, dict):
        return {}
    return json.loads(json.dumps(data))


def _normalize_ratio(value: object) -> float | None:
    if value is None:
        return None
    try:
        num = float(value)
    except Exception:
        return None
    if num > 1:
        num = num / 100.0
    return max(0.0, min(1.0, num))


def _unique_chain_id(base: str, existing: set[str]) -> str:
    raw = _slugify_title(base)
    if not raw:
        raw = "node"
    candidate = raw
    idx = 2
    while candidate in existing:
        candidate = f"{raw}-{idx}"
        idx += 1
    return candidate


_DATASET_FIXTURE_TEXT_RE = re.compile(r"\b(smoke|fixture|demo)\b|\btest dataset\b|^route test\b", re.IGNORECASE)


def _truthy_dataset_extra(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "fixture", "smoke", "test"}


def _dataset_extra(dataset: Dataset) -> dict[str, Any]:
    return dict(dataset.extra or {}) if isinstance(dataset.extra, dict) else {}


def _dataset_text_key(value: object) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9가-힣]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _dataset_is_analyst_hidden(dataset: Dataset) -> bool:
    if dataset.canonical_dataset_id is not None:
        return True
    extra = _dataset_extra(dataset)
    if str(extra.get("analyst_visible") or "").strip().lower() in {"0", "false", "no", "hidden"}:
        return True
    for key in ("fixture", "is_fixture", "test_fixture", "is_test", "is_smoke", "smoke"):
        if _truthy_dataset_extra(extra.get(key)):
            return True

    status_key = _dataset_text_key(dataset.status)
    if status_key in {"test", "fixture", "smoke"}:
        return True
    source_key = _dataset_text_key(dataset.source_kind)
    if source_key not in {"", "internal", "test", "fixture", "smoke"}:
        return False
    fixture_text = " ".join(
        [
            str(dataset.title or ""),
            str(dataset.slug or ""),
            str(dataset.description or ""),
            str(dataset.category or ""),
        ]
    ).strip()
    return bool(_DATASET_FIXTURE_TEXT_RE.search(fixture_text))


def _split_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return []


def _split_int_list(value: object) -> list[int]:
    out: list[int] = []
    for item in _split_list(value):
        try:
            out.append(int(item))
        except Exception:
            continue
    return out


def _boolish_value(value: object, *, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _research_redirect(url: str, *, notice: str | None = None, error: str | None = None) -> RedirectResponse:
    params: dict[str, str] = {}
    if notice:
        params["notice"] = str(notice)
    if error:
        params["error"] = str(error)
    suffix = f"?{urlencode(params)}" if params else ""
    return RedirectResponse(url=f"{url}{suffix}", status_code=303)
