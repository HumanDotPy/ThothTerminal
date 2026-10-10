from __future__ import annotations

import re
from typing import Any

from sqlalchemy.orm import Session

from thoth_terminal.dataset_service import latest_dataset_version
from thoth_terminal.models import Dataset, DatasetColumn, InstrumentRequest, SupplyChain, SupplyChainMetricBinding
from thoth_terminal.supply_chain_service import ensure_draft_snapshot, graph_from_snapshot


_METRIC_HINTS = (
    ("value_usd", ("value", "usd", "trade", "import", "export", "customs")),
    ("volume", ("volume", "quantity", "units", "ton", "kg")),
    (
        "capacity",
        ("capacity", "wpm", "output", "supply", "gpu", "gpus", "accelerator", "accelerators", "rack", "racks", "instance", "instances"),
    ),
    ("price", ("price", "asp", "pricing", "spot", "contract")),
    ("revenue", ("revenue", "sales")),
    ("margin", ("margin", "gross", "operating")),
    ("capex", ("capex", "capital", "spend")),
    ("capital_allocation", ("buyback", "dividend", "payout", "shareholder", "investment", "cash", "return")),
    ("valuation", ("valuation", "multiple", "target", "price", "sotp", "ev", "ebitda", "eps")),
    ("ownership", ("ownership", "holder", "13f", "short", "borrow", "option", "positioning")),
    ("roadmap_status", ("roadmap", "launch", "ramp", "design", "win", "lifecycle", "program")),
    ("governance_status", ("management", "board", "ceo", "cfo", "succession", "governance")),
    ("yield", ("yield", "defect", "quality")),
    ("lead_time", ("lead", "time", "delay")),
    ("utilization", ("utilization", "utilisation", "uptime", "available", "availability", "schedulable", "allocation")),
    ("route_exposure", ("exposure", "route", "lane", "chokepoint", "risk", "buffer", "substitution")),
    ("incident_impact", ("incident", "outage", "disruption", "downtime", "sla", "recovery", "impact")),
    ("provenance_status", ("hydration", "provenance", "rights", "fetch", "hash", "ocr", "readability", "retry", "failure")),
    ("traceability_status", ("traceability", "lot", "batch", "serial", "certificate", "counterfeit", "quarantine", "disposition")),
)

_SUGGESTABLE_DATASET_FAMILIES = {
    "ai_application_usage",
    "ai_capacity",
    "ai_cloud_capacity",
    "ai_cloud_incident",
    "ai_compute_capacity",
    "ai_data_platform",
    "ai_evalops_observability",
    "ai_governance_compliance",
    "ai_workload_compute",
    "bookings_backlog_inventory",
    "capex_delta",
    "capital_allocation",
    "cloud_capacity",
    "cloud_capacity_economics",
    "company_financial_kpi",
    "component_traceability",
    "customer_supplier_exposure",
    "document_hydration_provenance",
    "facility_capex_ramp",
    "future_electronics_product_availability",
    "guidance_actuals",
    "instrument_projection",
    "legal_regulatory_event",
    "management_governance_change",
    "monthly_revenue",
    "online_cluster_capacity",
    "ownership_positioning",
    "physical_logistics",
    "product_roadmap_lifecycle",
    "qualification_ramp_yield",
    "semiconductor_capacity",
    "product_stage_tracker",
    "sell_side_consensus_estimate",
    "shipment_customs",
    "site_program_capacity",
    "supply_chain",
    "supplier_route_exposure",
    "trade",
    "trade_customs",
    "valuation_assumption",
}

_FAMILY_SCORE_BONUS = {
    "supply_chain": 18,
    "supplier_route_exposure": 18,
    "component_traceability": 16,
    "shipment_customs": 16,
    "physical_logistics": 15,
    "trade_customs": 14,
    "trade": 12,
    "ai_cloud_incident": 12,
    "ai_cloud_capacity": 12,
    "ai_capacity": 12,
    "ai_compute_capacity": 12,
    "cloud_capacity": 12,
    "cloud_capacity_economics": 12,
    "online_cluster_capacity": 12,
    "site_program_capacity": 12,
    "semiconductor_capacity": 12,
    "facility_capex_ramp": 12,
    "qualification_ramp_yield": 12,
    "ai_workload_compute": 12,
    "product_stage_tracker": 12,
    "monthly_revenue": 10,
    "capex_delta": 10,
    "bookings_backlog_inventory": 10,
    "capital_allocation": 10,
    "company_financial_kpi": 10,
    "customer_supplier_exposure": 10,
    "product_roadmap_lifecycle": 10,
    "sell_side_consensus_estimate": 10,
    "ai_application_usage": 10,
    "ai_data_platform": 10,
    "ai_evalops_observability": 10,
    "ai_governance_compliance": 10,
    "legal_regulatory_event": 8,
    "management_governance_change": 8,
    "ownership_positioning": 8,
    "document_hydration_provenance": 8,
    "future_electronics_product_availability": 8,
    "instrument_projection": 6,
    "valuation_assumption": 6,
    "earnings_actuals": 4,
    "guidance_actuals": 4,
}

_FAMILY_METRIC_KIND_DEFAULTS = {
    "ai_application_usage": "usage",
    "ai_capacity": "capacity",
    "ai_cloud_capacity": "capacity",
    "ai_cloud_incident": "incident_impact",
    "ai_compute_capacity": "capacity",
    "ai_data_platform": "usage",
    "ai_evalops_observability": "quality",
    "ai_governance_compliance": "compliance_status",
    "ai_workload_compute": "capacity",
    "cloud_capacity": "capacity",
    "cloud_capacity_economics": "capacity",
    "component_traceability": "traceability_status",
    "capital_allocation": "capital_allocation",
    "company_financial_kpi": "revenue",
    "customer_supplier_exposure": "route_exposure",
    "document_hydration_provenance": "provenance_status",
    "facility_capex_ramp": "capex",
    "legal_regulatory_event": "incident_impact",
    "management_governance_change": "governance_status",
    "online_cluster_capacity": "capacity",
    "ownership_positioning": "ownership",
    "physical_logistics": "lead_time",
    "product_roadmap_lifecycle": "roadmap_status",
    "qualification_ramp_yield": "yield",
    "semiconductor_capacity": "capacity",
    "shipment_customs": "value_usd",
    "site_program_capacity": "capacity",
    "supplier_route_exposure": "route_exposure",
    "valuation_assumption": "valuation",
}

_STOPWORDS = {
    "about",
    "active",
    "and",
    "are",
    "before",
    "chain",
    "company",
    "corp",
    "corporation",
    "custom",
    "customs",
    "data",
    "dataset",
    "electronic",
    "electronics",
    "export",
    "exports",
    "for",
    "from",
    "global",
    "group",
    "holdings",
    "hs",
    "inc",
    "import",
    "imports",
    "in",
    "level",
    "linked",
    "llc",
    "ltd",
    "official",
    "plc",
    "product",
    "research",
    "source",
    "supply",
    "system",
    "systems",
    "table",
    "the",
    "this",
    "to",
    "tracker",
    "us",
    "usd",
    "value",
    "with",
}

_ALIASES = {
    "a16": {"advanced", "node"},
    "cowos": {"packaging", "advanced-packaging"},
    "cpo": {"optics", "optical", "photonics", "photonic"},
    "hbm": {"memory"},
    "hbm3": {"hbm", "memory"},
    "hbm3e": {"hbm", "memory"},
    "hbm4": {"hbm", "memory"},
    "inp": {"indium", "phosphide"},
    "n2": {"advanced", "node"},
    "osat": {"packaging"},
    "wfe": {"wafer", "fab", "equipment"},
}


def _tokens(value: object) -> set[str]:
    out: set[str] = set()
    for token in re.split(r"[^a-z0-9]+", str(value or "").lower()):
        if len(token) < 2 or token in _STOPWORDS:
            continue
        out.add(token)
        singular = token[:-1] if token.endswith("s") and len(token) > 4 else ""
        if singular and singular not in _STOPWORDS:
            out.add(singular)
        if re.match(r"^hbm\d", token):
            out.add("hbm")
        for alias in _ALIASES.get(token, set()):
            if alias not in _STOPWORDS:
                out.add(alias)
    return out


def _dataset_text(dataset: Dataset) -> str:
    extra = dataset.extra if isinstance(dataset.extra, dict) else {}
    tags = extra.get("tags")
    tag_text = " ".join(str(item or "") for item in tags) if isinstance(tags, list) else str(tags or "")
    return " ".join(
        str(value or "")
        for value in (
            dataset.title,
            dataset.slug,
            dataset.description,
            dataset.category,
            dataset.family,
            dataset.source_kind,
            extra.get("reporter_iso"),
            extra.get("partner_iso"),
            extra.get("hs_code"),
            extra.get("theme"),
            extra.get("ticker"),
            extra.get("symbol"),
            tag_text,
        )
    )


def _dataset_dedupe_key(dataset: Dataset) -> tuple[str, str, str, str, str]:
    extra = dataset.extra if isinstance(dataset.extra, dict) else {}
    title_key = re.sub(r"[^a-z0-9]+", " ", str(dataset.title or dataset.slug or "").lower()).strip()
    return (
        str(dataset.family or "").strip().lower(),
        str(dataset.category or "").strip().lower(),
        title_key,
        str(extra.get("hs_code") or "").strip().lower(),
        str(extra.get("theme") or "").strip().lower(),
    )


def _dataset_is_suggestable(dataset: Dataset) -> bool:
    extra = dataset.extra if isinstance(dataset.extra, dict) else {}
    if extra.get("is_test") is True:
        return False
    if extra.get("analyst_visible") is False:
        return False
    return True


def _dedupe_datasets(datasets: list[Dataset]) -> list[Dataset]:
    seen: set[tuple[str, str, str, str, str]] = set()
    out: list[Dataset] = []
    for dataset in datasets:
        if not _dataset_is_suggestable(dataset):
            continue
        key = _dataset_dedupe_key(dataset)
        if key in seen:
            continue
        seen.add(key)
        out.append(dataset)
    return out


def _node_text(node: dict[str, Any]) -> str:
    pieces: list[str] = []
    for key in ("id", "name", "layer", "type", "manufacturer", "location", "notes", "definition", "why_it_matters"):
        pieces.append(str(node.get(key) or ""))
    for key in ("who_matters", "watchpoints"):
        value = node.get(key)
        if isinstance(value, list):
            pieces.extend(str(item or "") for item in value)
    return " ".join(pieces)


def _chain_text(chain: SupplyChain) -> str:
    return " ".join(str(value or "") for value in (chain.title, chain.slug, chain.description, chain.category, chain.product))


def _metric_kind(dataset: Dataset, column: dict[str, Any] | None) -> str:
    column_text = f"{column.get('key') if column else ''} {column.get('label') if column else ''}".lower()
    for metric, hints in _METRIC_HINTS:
        if column_text and any(hint in column_text for hint in hints):
            return metric
    text = f"{_dataset_text(dataset)} {column_text}".lower()
    for metric, hints in _METRIC_HINTS:
        if any(hint in text for hint in hints):
            return metric
    family = str(dataset.family or "").strip().lower()
    if family == "trade_customs":
        return "value_usd"
    if family in _FAMILY_METRIC_KIND_DEFAULTS:
        return _FAMILY_METRIC_KIND_DEFAULTS[family]
    if family in {"earnings_actuals", "guidance_actuals"}:
        return "issuer_metric"
    return "custom_metric"


def _columns(session: Session, dataset: Dataset) -> list[dict[str, Any]]:
    version = latest_dataset_version(session, int(dataset.id), status="draft") or latest_dataset_version(session, int(dataset.id), status="published")
    if version is None:
        return []
    rows = (
        session.query(DatasetColumn)
        .filter(DatasetColumn.dataset_version_id == int(version.id))
        .order_by(DatasetColumn.sort_order.asc(), DatasetColumn.id.asc())
        .all()
    )
    return [
        {"key": row.key, "label": row.label, "data_type": row.data_type, "role": row.role}
        for row in rows
    ]


def _preferred_metric_column(columns: list[dict[str, Any]]) -> dict[str, Any] | None:
    for col in columns:
        if str(col.get("role") or "").lower() == "metric":
            return col
    for col in columns:
        if str(col.get("data_type") or "").lower() == "number":
            return col
    return None


def _existing_keys(session: Session, snapshot_id: int) -> set[tuple[str, str, str]]:
    return {
        (str(row.scope_type), str(row.scope_key), str(row.metric_kind))
        for row in session.query(SupplyChainMetricBinding).filter_by(snapshot_id=int(snapshot_id)).all()
    }


def _score(left: str, right: str) -> tuple[int, list[str]]:
    left_tokens = _tokens(left)
    right_tokens = _tokens(right)
    overlap = sorted(left_tokens & right_tokens)
    if not overlap:
        return 0, []
    score = len(overlap) * 22
    score += sum(8 for token in overlap if len(token) >= 5)
    return min(100, score), overlap


def _boosted_score(dataset: Dataset, score: int) -> int:
    family = str(dataset.family or "").strip().lower()
    return min(100, int(score) + int(_FAMILY_SCORE_BONUS.get(family, 0)))


def list_supply_chain_metric_bindings(session: Session, chain: SupplyChain) -> dict[str, Any]:
    snapshot = ensure_draft_snapshot(session, chain)
    graph = graph_from_snapshot(session, snapshot, chain_id=int(chain.id))
    bindings = [dict(row) for row in graph.get("bindings") or [] if isinstance(row, dict)]
    return {
        "id": int(chain.id),
        "title": chain.title,
        "snapshot_id": int(snapshot.id),
        "count": len(bindings),
        "bindings": bindings,
    }


def suggest_supply_chain_metric_bindings(
    session: Session,
    chain: SupplyChain,
    *,
    query: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    snapshot = ensure_draft_snapshot(session, chain)
    graph = graph_from_snapshot(session, snapshot, chain_id=int(chain.id))
    nodes = [dict(row) for row in graph.get("nodes") or [] if isinstance(row, dict)]
    existing = _existing_keys(session, int(snapshot.id))
    query_text = str(query or "").strip()
    dataset_rows = _dedupe_datasets(
        session.query(Dataset)
        .filter(Dataset.status != "archived")
        .filter(Dataset.family.in_(sorted(_SUGGESTABLE_DATASET_FAMILIES)))
        .order_by(Dataset.updated_at.desc(), Dataset.id.desc())
        .limit(1500)
        .all()
    )
    suggestions: list[dict[str, Any]] = []
    chain_scope_text = f"{_chain_text(chain)} {query_text}"

    for dataset in dataset_rows:
        dataset_text = _dataset_text(dataset)
        if query_text and not (_tokens(query_text) & _tokens(dataset_text)):
            continue
        cols = _columns(session, dataset)
        metric_col = _preferred_metric_column(cols)
        if metric_col is None:
            continue
        metric = _metric_kind(dataset, metric_col)
        column_key = str((metric_col or {}).get("key") or "") or None
        chain_score, chain_reasons = _score(chain_scope_text, dataset_text)
        chain_score = min(100, _boosted_score(dataset, chain_score) + 8) if chain_score else 0
        if chain_score >= 18 and ("chain", "chain", metric) not in existing:
            suggestions.append(
                _suggestion(
                    chain=chain,
                    dataset=dataset,
                    scope_type="chain",
                    scope_key="chain",
                    metric_kind=metric,
                    column_key=column_key,
                    score=chain_score,
                    reasons=chain_reasons,
                )
            )
        for node in nodes:
            node_id = str(node.get("id") or "").strip()
            if not node_id:
                continue
            score, reasons = _score(f"{_node_text(node)} {query_text}", dataset_text)
            score = _boosted_score(dataset, score) if score else 0
            if score < 36:
                continue
            if ("node", node_id, metric) in existing:
                continue
            suggestions.append(
                _suggestion(
                    chain=chain,
                    dataset=dataset,
                    scope_type="node",
                    scope_key=node_id,
                    scope_label=str(node.get("name") or node_id),
                    metric_kind=metric,
                    column_key=column_key,
                    score=score,
                    reasons=reasons,
                )
            )

    suggestions.sort(
        key=lambda row: (
            -int(row.get("score") or 0),
            0 if row.get("scope_type") == "chain" else 1,
            str(row.get("scope_label") or ""),
            str(row.get("dataset_title") or ""),
        )
    )
    trimmed = suggestions[: max(1, min(int(limit or 20), 100))]
    for idx, row in enumerate(trimmed, start=1):
        row["suggestion_id"] = f"sugg:{int(chain.id)}:{idx}"
    return {
        "id": int(chain.id),
        "title": chain.title,
        "snapshot_id": int(snapshot.id),
        "count": len(trimmed),
        "suggestions": trimmed,
        "usage": "Pass a suggestion's bind_op to preview_supply_chain_ops, then apply_supply_chain_ops if the preview is correct.",
    }


def _suggestion(
    *,
    chain: SupplyChain,
    dataset: Dataset,
    scope_type: str,
    scope_key: str,
    metric_kind: str,
    column_key: str | None,
    score: int,
    reasons: list[str],
    scope_label: str | None = None,
) -> dict[str, Any]:
    metadata = {
        "service": "supply_chain_metric_service",
        "template_version": 1,
        "reason_tokens": reasons[:8],
        "score": int(score),
    }
    bind_op = {
        "op": "bind_metric",
        "scope_type": scope_type,
        "scope_key": scope_key,
        "metric_kind": metric_kind,
        "dataset_id": int(dataset.id),
        "column_key": column_key,
        "aggregation": "latest",
        "extra": {
            "suggested_by": "supply_chain_metric_service",
            "reason_tokens": reasons[:8],
            "chain_id": int(chain.id),
            "suggestion_metadata": metadata,
        },
    }
    binding_template = {
        "template_kind": "supply_chain_metric_binding",
        "template_version": 1,
        "chain": {"id": int(chain.id), "title": chain.title, "slug": chain.slug},
        "scope": {"type": scope_type, "key": scope_key, "label": scope_label or chain.title},
        "metric": {"kind": metric_kind, "column_key": column_key, "aggregation": "latest"},
        "dataset": {
            "id": int(dataset.id),
            "title": dataset.title,
            "slug": dataset.slug,
            "family": dataset.family,
            "category": dataset.category,
        },
        "apply_tool": "apply_supply_chain_ops",
        "apply_arguments": {"id": int(chain.id), "ops": [bind_op]},
    }
    return {
        "chain_id": int(chain.id),
        "chain_title": chain.title,
        "scope_type": scope_type,
        "scope_key": scope_key,
        "scope_label": scope_label or chain.title,
        "metric_kind": metric_kind,
        "dataset_id": int(dataset.id),
        "dataset_title": dataset.title,
        "dataset_slug": dataset.slug,
        "dataset_family": dataset.family,
        "dataset_category": dataset.category,
        "column_key": column_key,
        "score": int(score),
        "reason_tokens": reasons[:12],
        "suggestion_metadata": metadata,
        "binding_template": binding_template,
        "bind_op": bind_op,
        "apply_arguments": {"id": int(chain.id), "ops": [bind_op]},
    }


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        text = str(value or "").strip()
        return int(text) if text else None
    except Exception:
        return None


def _metric_request_chain_id(request: InstrumentRequest) -> int | None:
    payloads = [
        request.linked_objects if isinstance(request.linked_objects, dict) else {},
        request.draft_payload if isinstance(request.draft_payload, dict) else {},
        request.live_payload if isinstance(request.live_payload, dict) else {},
    ]
    for payload in payloads:
        for key in ("supply_chain_id", "chain_id"):
            value = _coerce_int(payload.get(key))
            if value is not None:
                return value
        for key in ("gap_key", "coverage_gap_key"):
            match = re.search(r"coverage_gap:supply_chain:(\d+):metric_bindings", str(payload.get(key) or ""))
            if match:
                return int(match.group(1))
    return None


def _is_metric_binding_request(request: InstrumentRequest) -> bool:
    desired = {str(value or "").strip().lower() for value in (request.desired_outputs or []) if str(value or "").strip()}
    text = " ".join(
        str(value or "")
        for value in (
            request.title,
            request.summary,
            request.intent,
            request.source_kind,
            request.automation_source,
            " ".join(sorted(desired)),
        )
    ).lower()
    if "supply_chain_metric_binding" in desired or "metric_binding" in desired:
        return True
    if "missing_supply_chain_metric_bindings" in text or "metric bindings" in text:
        return True
    return _metric_request_chain_id(request) is not None and bool(desired & {"datasets", "supply_chain", "trade_customs"})


def _request_query_text(request: InstrumentRequest, chain: SupplyChain) -> str:
    values = [chain.title, chain.slug, chain.description, chain.category, chain.product, request.title, request.summary]
    linked = request.linked_objects if isinstance(request.linked_objects, dict) else {}
    draft = request.draft_payload if isinstance(request.draft_payload, dict) else {}
    for payload in (linked, draft):
        for key in ("query", "metric_query", "chain_query", "chain_title", "chain_slug"):
            values.append(payload.get(key))
    return " ".join(str(value or "") for value in values)


def _request_summary(request: InstrumentRequest) -> dict[str, Any]:
    return {
        "id": int(request.id),
        "title": request.title,
        "status": request.status,
        "automation_source": request.automation_source,
        "source_kind": request.source_kind,
        "desired_outputs": list(request.desired_outputs or []),
    }


def _post_apply_fulfillment_template(
    *,
    request: InstrumentRequest,
    chain: SupplyChain,
    suggestions: list[dict[str, Any]],
    max_ops: int,
) -> dict[str, Any]:
    selected = suggestions[:max(1, min(int(max_ops or 1), len(suggestions) or 1))]
    return {
        "template_kind": "instrument_request_fulfillment",
        "template_version": 1,
        "requires_apply_before_fulfillment": True,
        "requires_verification": ["preview_supply_chain_ops", "apply_supply_chain_ops", "get_supply_chain_metric_bindings"],
        "tool": "fulfill_instrument_request",
        "arguments": {
            "id": int(request.id),
            "fulfillment_summary": f"Applied metric bindings for {chain.title} through the supply-chain metric service.",
            "fulfilled_by": "codex",
            "live_payload_patch": {
                "metric_binding_fulfillment": {
                    "service": "supply_chain_metric_service",
                    "chain_id": int(chain.id),
                    "suggestion_count": len(suggestions),
                    "candidate_binding_templates": [row.get("binding_template") for row in selected],
                }
            },
            "linked_objects_patch": {"supply_chain_id": int(chain.id)},
            "evidence_bundle_patch": [
                {
                    "source": "supply_chain_metric_service",
                    "title": f"Metric binding suggestions for {chain.title}",
                    "chain_id": int(chain.id),
                    "suggestion_count": len(suggestions),
                }
            ],
            "verification": {"commands": ["focused service/test run", "get_supply_chain_metric_bindings after apply"]},
        },
    }


def suggest_metric_binding_request_fulfillments(
    session: Session,
    *,
    chain_ids: list[int] | tuple[int, ...] | set[int],
    limit_per_chain: int = 5,
    max_ops_per_request: int = 2,
    request_limit: int = 100,
) -> dict[str, Any]:
    """Plan bounded supply-chain metric-binding request fulfillment.

    The function intentionally returns bind_metric operations and a post-apply
    fulfillment template instead of applying bindings or closing requests.
    """

    bounded_chain_ids = sorted({int(value) for value in chain_ids if _coerce_int(value) is not None})[:25]
    if not bounded_chain_ids:
        return {"chain_ids": [], "request_count": 0, "candidate_count": 0, "candidates": [], "unmatched_chain_ids": []}

    chains = {
        int(chain.id): chain
        for chain in session.query(SupplyChain).filter(SupplyChain.id.in_(bounded_chain_ids)).order_by(SupplyChain.id.asc()).all()
    }
    requests = (
        session.query(InstrumentRequest)
        .filter(InstrumentRequest.status.in_(["draft", "review"]))
        .order_by(InstrumentRequest.updated_at.desc(), InstrumentRequest.id.desc())
        .limit(max(1, min(int(request_limit or 100), 500)))
        .all()
    )
    candidates: list[dict[str, Any]] = []
    matched_chain_ids: set[int] = set()
    for request in requests:
        if not _is_metric_binding_request(request):
            continue
        chain_id = _metric_request_chain_id(request)
        if chain_id not in chains:
            continue
        chain = chains[chain_id]
        matched_chain_ids.add(int(chain_id))
        query_text = _request_query_text(request, chain)
        suggestion_payload = suggest_supply_chain_metric_bindings(
            session,
            chain,
            query=query_text,
            limit=max(1, min(int(limit_per_chain or 5), 25)),
        )
        suggestions = [dict(row) for row in suggestion_payload.get("suggestions") or [] if isinstance(row, dict)]
        selected = suggestions[: max(1, min(int(max_ops_per_request or 1), len(suggestions) or 1))]
        candidates.append(
            {
                "instrument_request": _request_summary(request),
                "chain": {"id": int(chain.id), "title": chain.title, "slug": chain.slug},
                "status": "ready_for_apply" if suggestions else "blocked_no_metric_suggestions",
                "suggestion_count": len(suggestions),
                "suggestions": suggestions,
                "recommended_apply_arguments": {"id": int(chain.id), "ops": [row["bind_op"] for row in selected if isinstance(row.get("bind_op"), dict)]},
                "post_apply_fulfillment": _post_apply_fulfillment_template(
                    request=request,
                    chain=chain,
                    suggestions=suggestions,
                    max_ops=max_ops_per_request,
                ),
            }
        )
    return {
        "chain_ids": bounded_chain_ids,
        "request_count": len(requests),
        "candidate_count": len(candidates),
        "candidates": candidates,
        "unmatched_chain_ids": [chain_id for chain_id in bounded_chain_ids if chain_id in chains and chain_id not in matched_chain_ids],
        "missing_chain_ids": [chain_id for chain_id in bounded_chain_ids if chain_id not in chains],
        "usage": "Preview/apply recommended_apply_arguments before using post_apply_fulfillment; this service does not write metric bindings or close requests.",
    }


def suggest_dataset_supply_chain_bindings(
    session: Session,
    *,
    dataset_id: int,
    query: str | None = None,
    chain_query: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Suggest supply-chain metric bindings starting from an accepted dataset.

    This is the inverse of ``suggest_supply_chain_metric_bindings``. It gives an
    agent a deterministic service-layer path after materializing a dataset: find
    candidate chains/nodes and return apply-compatible bind operations rather
    than forcing a new instrument request solely to resolve a chain id.
    """

    dataset = session.query(Dataset).filter_by(id=int(dataset_id)).first()
    if dataset is None:
        raise ValueError("Dataset not found.")
    if not _dataset_is_suggestable(dataset):
        return {
            "dataset_id": int(dataset_id),
            "dataset_title": getattr(dataset, "title", None),
            "count": 0,
            "suggestions": [],
            "usage": "Dataset is hidden from analyst metric-binding suggestions.",
        }
    columns = _columns(session, dataset)
    metric_col = _preferred_metric_column(columns)
    if metric_col is None:
        return {
            "dataset_id": int(dataset.id),
            "dataset_title": dataset.title,
            "count": 0,
            "suggestions": [],
            "usage": "Dataset has no metric column to bind.",
        }
    metric = _metric_kind(dataset, metric_col)
    column_key = str((metric_col or {}).get("key") or "") or None
    query_text = str(query or "").strip()
    chain_filter = str(chain_query or "").strip()
    dataset_text = f"{_dataset_text(dataset)} {query_text}"
    chain_filter_tokens = _tokens(chain_filter)
    chains = (
        session.query(SupplyChain)
        .filter(SupplyChain.status != "archived")
        .order_by(SupplyChain.updated_at.desc(), SupplyChain.id.desc())
        .limit(1200)
        .all()
    )
    suggestions: list[dict[str, Any]] = []
    for chain in chains:
        chain_text = _chain_text(chain)
        if chain_filter_tokens and not (chain_filter_tokens & _tokens(chain_text)):
            continue
        snapshot = ensure_draft_snapshot(session, chain)
        graph = graph_from_snapshot(session, snapshot, chain_id=int(chain.id))
        nodes = [dict(row) for row in graph.get("nodes") or [] if isinstance(row, dict)]
        existing = _existing_keys(session, int(snapshot.id))
        chain_score, chain_reasons = _score(f"{chain_text} {chain_filter}", dataset_text)
        chain_score = min(100, _boosted_score(dataset, chain_score) + 8) if chain_score else 0
        if chain_score >= 24 and ("chain", "chain", metric) not in existing:
            suggestions.append(
                _suggestion(
                    chain=chain,
                    dataset=dataset,
                    scope_type="chain",
                    scope_key="chain",
                    metric_kind=metric,
                    column_key=column_key,
                    score=chain_score,
                    reasons=chain_reasons,
                )
            )
        for node in nodes:
            node_id = str(node.get("id") or "").strip()
            if not node_id:
                continue
            score, reasons = _score(f"{_node_text(node)} {chain_filter}", dataset_text)
            score = _boosted_score(dataset, score) if score else 0
            if score < 40:
                continue
            if ("node", node_id, metric) in existing:
                continue
            suggestions.append(
                _suggestion(
                    chain=chain,
                    dataset=dataset,
                    scope_type="node",
                    scope_key=node_id,
                    scope_label=str(node.get("name") or node_id),
                    metric_kind=metric,
                    column_key=column_key,
                    score=score,
                    reasons=reasons,
                )
            )
    suggestions.sort(
        key=lambda row: (
            -int(row.get("score") or 0),
            str(row.get("chain_title") or ""),
            0 if row.get("scope_type") == "chain" else 1,
            str(row.get("scope_label") or ""),
        )
    )
    trimmed = suggestions[: max(1, min(int(limit or 20), 100))]
    for idx, row in enumerate(trimmed, start=1):
        row["suggestion_id"] = f"dataset-sugg:{int(dataset.id)}:{idx}"
    return {
        "dataset_id": int(dataset.id),
        "dataset_title": dataset.title,
        "dataset_family": dataset.family,
        "metric_kind": metric,
        "column_key": column_key,
        "count": len(trimmed),
        "suggestions": trimmed,
        "usage": "Preview a suggestion's apply_arguments with preview_supply_chain_ops, then apply_supply_chain_ops if correct.",
    }
