from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from thoth_terminal.db import utcnow
from thoth_terminal.models import BottleneckAssessment, BottleneckModel, SupplyChain, SupplyChainSnapshot
from thoth_terminal.supply_chain_service import ensure_draft_snapshot, graph_from_snapshot, import_legacy_chain_if_needed


BOTTLENECK_AXES = (
    "bindingness",
    "shadow_price",
    "ramp_resistance",
    "substitutability",
    "persistence",
    "latent_demand_absorption",
)

BOTTLENECK_REGIMES = {
    "tech_complexity_gate": "Frontier process, yield, qualification, or integration capacity gates system output.",
    "volume_allocation_crunch": "Mature or broad-volume component pressure where allocation and lead time dominate.",
    "qualification_constraint": "Qualified supply exists only after customer, reliability, or process approval clears.",
    "architectural_capacity_gate": "The constrained input is embedded deeply enough that routing around it changes the system architecture.",
    "logistics_constraint": "Physical flow, customs, power, site, or delivery friction binds more than production capability.",
    "mixed_constraint": "Multiple mechanisms are plausible and the evidence does not justify a narrower regime.",
    "watch": "Constraint evidence is directional but not yet system-binding.",
}

_AXIS_COLUMN_MAP = {
    "bindingness": "bindingness_score",
    "shadow_price": "shadow_price_score",
    "ramp_resistance": "ramp_resistance_score",
    "substitutability": "substitutability_score",
    "persistence": "persistence_score",
    "latent_demand_absorption": "latent_demand_absorption_score",
}

_AXIS_WEIGHTS = {
    "bindingness": 0.28,
    "shadow_price": 0.16,
    "ramp_resistance": 0.18,
    "substitutability": 0.14,
    "persistence": 0.14,
    "latent_demand_absorption": 0.10,
}


def _slugify(value: str | None, *, default: str = "bottleneck") -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return cleaned or default


def _ratio(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except Exception:
        return None
    if out > 1.0:
        out = out / 100.0
    return max(0.0, min(out, 1.0))


def _as_dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _as_list(value: object) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _json_object(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    return {}


def _json_list(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _qualified_supplier_count(supplier_qualification: dict[str, Any], node: dict[str, Any]) -> int | None:
    explicit = supplier_qualification.get("qualified_supplier_count")
    if explicit is not None and str(explicit).strip().isdigit():
        return int(explicit)
    suppliers = supplier_qualification.get("qualified_suppliers")
    if isinstance(suppliers, list):
        return len([item for item in suppliers if str(item or "").strip()])
    node_suppliers = node.get("qualified_suppliers")
    if isinstance(node_suppliers, list):
        return len([item for item in node_suppliers if str(item or "").strip()])
    return None


def _supplier_concentration_score(count: int | None) -> float | None:
    if count is None:
        return None
    if count <= 1:
        return 1.0
    if count == 2:
        return 0.78
    if count == 3:
        return 0.58
    if count <= 5:
        return 0.38
    return 0.22


def _node_model_payload(node: dict[str, Any]) -> dict[str, Any]:
    direct = _as_dict(node.get("bottleneck_model"))
    extra = _as_dict(node.get("extra"))
    embedded = _as_dict(extra.get("bottleneck_model"))
    return {**embedded, **direct}


def _axis_value(node: dict[str, Any], model_payload: dict[str, Any], axis: str, *, fallback: float | None = None) -> float | None:
    candidates = [
        model_payload.get(f"{axis}_score"),
        model_payload.get(axis),
        node.get(f"{axis}_score"),
        node.get(axis),
    ]
    for candidate in candidates:
        value = _ratio(candidate)
        if value is not None:
            return value
    return fallback


def score_supply_chain_node(node: dict[str, Any]) -> dict[str, Any]:
    model_payload = _node_model_payload(node)
    bottleneck = _ratio(node.get("bottleneck")) or 0.0
    pricing = _ratio(node.get("pricing_power")) or 0.0
    confidence = _ratio(node.get("confidence"))
    lead_months = None
    try:
        lead_months = int(node.get("lead_time_months")) if node.get("lead_time_months") not in (None, "") else None
    except Exception:
        lead_months = None

    supplier_qualification = {
        **_as_dict(node.get("supplier_qualification")),
        **_as_dict(model_payload.get("supplier_qualification")),
    }
    supplier_count = _qualified_supplier_count(supplier_qualification, node)
    supplier_score = _supplier_concentration_score(supplier_count)
    ramp_fallback = max(bottleneck * 0.75, min(float(lead_months or 0) / 12.0, 1.0))
    substitutability_fallback = supplier_score if supplier_score is not None else bottleneck * 0.65
    persistence_fallback = max(ramp_fallback * 0.75, bottleneck * 0.65)

    axes = {
        "bindingness": _axis_value(node, model_payload, "bindingness", fallback=bottleneck),
        "shadow_price": _axis_value(node, model_payload, "shadow_price", fallback=max(pricing, bottleneck * 0.55)),
        "ramp_resistance": _axis_value(node, model_payload, "ramp_resistance", fallback=ramp_fallback),
        "substitutability": _axis_value(node, model_payload, "substitutability", fallback=substitutability_fallback),
        "persistence": _axis_value(node, model_payload, "persistence", fallback=persistence_fallback),
        "latent_demand_absorption": _axis_value(
            node,
            model_payload,
            "latent_demand_absorption",
            fallback=max(bottleneck * 0.55, persistence_fallback * 0.65),
        ),
    }
    clean_axes = {axis: round(float(score or 0.0), 4) for axis, score in axes.items()}
    composite = sum(clean_axes[axis] * _AXIS_WEIGHTS[axis] for axis in BOTTLENECK_AXES)
    regime = str(model_payload.get("regime") or "").strip() or classify_bottleneck_regime(clean_axes)
    return {
        "node_key": str(node.get("id") or node.get("node_key") or "").strip(),
        "node_name": str(node.get("name") or "").strip(),
        "regime": regime,
        "axes": clean_axes,
        "composite_score": round(composite, 4),
        "confidence": confidence if confidence is not None else round(max(clean_axes.values() or [0.0]) * 0.75, 4),
        "qualified_supplier_count": supplier_count,
        "supplier_qualification": supplier_qualification,
        "marginal_system_leverage": _as_dict(node.get("marginal_system_leverage")) or _as_dict(model_payload.get("marginal_system_leverage")),
        "evidence": _json_list(node.get("evidence")) or _json_list(model_payload.get("evidence")),
        "source_node": node,
        "scoring_semantics": {
            "substitutability": "Higher score means harder to route around with qualified alternatives.",
            "latent_demand_absorption": "Higher score means incremental supply is likely consumed without visible easing because demand is supply-censored.",
        },
    }


def classify_bottleneck_regime(axes: dict[str, float]) -> str:
    bindingness = float(axes.get("bindingness") or 0.0)
    shadow_price = float(axes.get("shadow_price") or 0.0)
    ramp = float(axes.get("ramp_resistance") or 0.0)
    substitution_difficulty = float(axes.get("substitutability") or 0.0)
    persistence = float(axes.get("persistence") or 0.0)
    latent = float(axes.get("latent_demand_absorption") or 0.0)
    if bindingness < 0.35:
        return "watch"
    if bindingness >= 0.65 and ramp >= 0.62 and substitution_difficulty >= 0.58 and latent >= 0.55:
        return "tech_complexity_gate"
    if bindingness >= 0.58 and substitution_difficulty >= 0.72 and persistence >= 0.60:
        return "architectural_capacity_gate"
    if ramp >= 0.65 and bindingness < 0.58:
        return "qualification_constraint"
    if bindingness >= 0.50 and shadow_price >= 0.40 and ramp < 0.58 and substitution_difficulty < 0.58:
        return "volume_allocation_crunch"
    return "mixed_constraint"


def serialize_bottleneck_model(row: BottleneckModel, *, include_assessments: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": int(row.id),
        "model_key": row.model_key,
        "title": row.title,
        "scope_type": row.scope_type,
        "scope_key": row.scope_key,
        "subject_id": int(row.subject_id) if row.subject_id is not None else None,
        "site_id": int(row.site_id) if row.site_id is not None else None,
        "program_id": int(row.program_id) if row.program_id is not None else None,
        "supply_chain_id": int(row.supply_chain_id) if row.supply_chain_id is not None else None,
        "snapshot_id": int(row.snapshot_id) if row.snapshot_id is not None else None,
        "constrained_input": row.constrained_input,
        "output_unit": row.output_unit,
        "regime": row.regime,
        "status": row.status,
        "summary": row.summary,
        "thesis": row.thesis,
        "qualified_supplier_count": int(row.qualified_supplier_count) if row.qualified_supplier_count is not None else None,
        "supplier_qualification": dict(row.supplier_qualification_json or {}),
        "evidence": list(row.evidence_json or []),
        "payload": dict(row.payload or {}),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
    if include_assessments:
        latest = sorted(row.assessments or [], key=lambda item: (item.updated_at or datetime.min, int(item.id or 0)), reverse=True)
        payload["assessments"] = [serialize_bottleneck_assessment(item) for item in latest[:10]]
    return payload


def serialize_bottleneck_assessment(row: BottleneckAssessment | None) -> dict[str, Any] | None:
    if row is None:
        return None
    axes = {
        "bindingness": row.bindingness_score,
        "shadow_price": row.shadow_price_score,
        "ramp_resistance": row.ramp_resistance_score,
        "substitutability": row.substitutability_score,
        "persistence": row.persistence_score,
        "latent_demand_absorption": row.latent_demand_absorption_score,
    }
    return {
        "id": int(row.id),
        "model_id": int(row.model_id) if row.model_id is not None else None,
        "program_id": int(row.program_id) if row.program_id is not None else None,
        "subject_id": int(row.subject_id) if row.subject_id is not None else None,
        "site_id": int(row.site_id) if row.site_id is not None else None,
        "supply_chain_id": int(row.supply_chain_id) if row.supply_chain_id is not None else None,
        "snapshot_id": int(row.snapshot_id) if row.snapshot_id is not None else None,
        "scope_type": row.scope_type,
        "scope_key": row.scope_key,
        "active_node_key": row.active_node_key,
        "shadow_node_key": row.shadow_node_key,
        "constrained_input": row.constrained_input,
        "output_unit": row.output_unit,
        "regime": row.regime,
        "classification": row.classification,
        "summary": row.summary,
        "confidence": row.confidence,
        "status": row.status,
        "axes": axes,
        "qualified_supplier_count": int(row.qualified_supplier_count) if row.qualified_supplier_count is not None else None,
        "marginal_system_leverage": dict(row.marginal_system_leverage or {}),
        "supplier_qualification": dict(row.supplier_qualification_json or {}),
        "evidence": list(row.evidence_json or []),
        "scorecard": dict(row.scorecard_json or {}),
        "payload": dict(row.payload or {}),
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def list_bottleneck_models(
    session: Session,
    *,
    query: str | None = None,
    supply_chain_id: int | None = None,
    status: str | None = "active",
    limit: int = 50,
) -> list[dict[str, Any]]:
    q = session.query(BottleneckModel).order_by(BottleneckModel.updated_at.desc(), BottleneckModel.id.desc())
    if supply_chain_id is not None:
        q = q.filter(BottleneckModel.supply_chain_id == int(supply_chain_id))
    if status:
        q = q.filter(BottleneckModel.status == str(status))
    text = str(query or "").strip()
    if text:
        pattern = f"%{text}%"
        q = q.filter(
            or_(
                BottleneckModel.title.ilike(pattern),
                BottleneckModel.summary.ilike(pattern),
                BottleneckModel.thesis.ilike(pattern),
                BottleneckModel.constrained_input.ilike(pattern),
                BottleneckModel.regime.ilike(pattern),
            )
        )
    rows = q.limit(max(1, min(int(limit or 50), 200))).all()
    return [serialize_bottleneck_model(row) for row in rows]


def get_bottleneck_model_payload(session: Session, model_id: int) -> dict[str, Any] | None:
    row = session.query(BottleneckModel).filter_by(id=int(model_id)).first()
    if row is None:
        return None
    return serialize_bottleneck_model(row, include_assessments=True)


def upsert_bottleneck_model(
    session: Session,
    *,
    title: str,
    model_key: str | None = None,
    scope_type: str = "general",
    scope_key: str | None = None,
    subject_id: int | None = None,
    site_id: int | None = None,
    program_id: int | None = None,
    supply_chain_id: int | None = None,
    snapshot_id: int | None = None,
    constrained_input: str | None = None,
    output_unit: str | None = None,
    regime: str | None = None,
    status: str = "active",
    summary: str | None = None,
    thesis: str | None = None,
    qualified_supplier_count: int | None = None,
    supplier_qualification: dict[str, Any] | None = None,
    evidence: list[dict[str, Any]] | None = None,
    payload: dict[str, Any] | None = None,
) -> BottleneckModel:
    now = utcnow()
    key = str(model_key or "").strip()
    if not key:
        scope_part = _slugify(scope_key or scope_type, default="scope")
        key = f"{_slugify(scope_type, default='scope')}:{scope_part}:{_slugify(title, default='bottleneck')}"
    row = session.query(BottleneckModel).filter_by(model_key=key).first()
    if row is None:
        row = BottleneckModel(model_key=key, title=title, scope_type=scope_type, created_at=now, updated_at=now)
        session.add(row)
    row.title = title
    row.scope_type = scope_type
    row.scope_key = scope_key
    row.subject_id = subject_id
    row.site_id = site_id
    row.program_id = program_id
    row.supply_chain_id = supply_chain_id
    row.snapshot_id = snapshot_id
    row.constrained_input = constrained_input
    row.output_unit = output_unit
    row.regime = regime
    row.status = status
    row.summary = summary
    row.thesis = thesis
    row.qualified_supplier_count = qualified_supplier_count
    row.supplier_qualification_json = dict(supplier_qualification or {})
    row.evidence_json = list(evidence or [])
    row.payload = dict(payload or {})
    row.updated_at = now
    session.add(row)
    session.flush()
    return row


def record_bottleneck_assessment(
    session: Session,
    *,
    model: BottleneckModel | None = None,
    model_id: int | None = None,
    scope_type: str | None = None,
    scope_key: str | None = None,
    subject_id: int | None = None,
    site_id: int | None = None,
    program_id: int | None = None,
    supply_chain_id: int | None = None,
    snapshot_id: int | None = None,
    active_node_key: str | None = None,
    shadow_node_key: str | None = None,
    constrained_input: str | None = None,
    output_unit: str | None = None,
    regime: str | None = None,
    classification: str | None = None,
    summary: str | None = None,
    confidence: float | None = None,
    axes: dict[str, float] | None = None,
    qualified_supplier_count: int | None = None,
    marginal_system_leverage: dict[str, Any] | None = None,
    supplier_qualification: dict[str, Any] | None = None,
    evidence: list[dict[str, Any]] | None = None,
    scorecard: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> BottleneckAssessment:
    now = utcnow()
    if model is None and model_id is not None:
        model = session.query(BottleneckModel).filter_by(id=int(model_id)).first()
    q = session.query(BottleneckAssessment).filter(BottleneckAssessment.status == "active")
    if model is not None:
        q = q.filter(BottleneckAssessment.model_id == int(model.id))
    elif model_id is not None:
        q = q.filter(BottleneckAssessment.model_id == int(model_id))
    elif scope_key:
        q = q.filter(BottleneckAssessment.scope_type == scope_type, BottleneckAssessment.scope_key == scope_key)
    elif program_id is not None:
        q = q.filter(BottleneckAssessment.program_id == int(program_id))
    elif supply_chain_id is not None and active_node_key:
        q = q.filter(BottleneckAssessment.supply_chain_id == int(supply_chain_id), BottleneckAssessment.active_node_key == active_node_key)
    else:
        q = q.filter(BottleneckAssessment.id == -1)
    row = q.order_by(BottleneckAssessment.updated_at.desc(), BottleneckAssessment.id.desc()).first()
    if row is None:
        row = BottleneckAssessment(created_at=now, updated_at=now, payload={})
        session.add(row)
    row.model_id = int(model.id) if model is not None else model_id
    row.scope_type = scope_type or (model.scope_type if model is not None else None)
    row.scope_key = scope_key or (model.scope_key if model is not None else None)
    row.subject_id = subject_id if subject_id is not None else (model.subject_id if model is not None else None)
    row.site_id = site_id if site_id is not None else (model.site_id if model is not None else None)
    row.program_id = program_id if program_id is not None else (model.program_id if model is not None else None)
    row.supply_chain_id = supply_chain_id if supply_chain_id is not None else (model.supply_chain_id if model is not None else None)
    row.snapshot_id = snapshot_id if snapshot_id is not None else (model.snapshot_id if model is not None else None)
    row.active_node_key = active_node_key
    row.shadow_node_key = shadow_node_key
    row.constrained_input = constrained_input or (model.constrained_input if model is not None else None)
    row.output_unit = output_unit or (model.output_unit if model is not None else None)
    row.regime = regime or (model.regime if model is not None else None)
    row.classification = classification or row.regime
    row.summary = summary
    row.confidence = confidence
    row.status = "active"
    for axis, column in _AXIS_COLUMN_MAP.items():
        setattr(row, column, _ratio((axes or {}).get(axis)))
    row.qualified_supplier_count = qualified_supplier_count if qualified_supplier_count is not None else (model.qualified_supplier_count if model is not None else None)
    row.marginal_system_leverage = dict(marginal_system_leverage or {})
    row.supplier_qualification_json = dict(supplier_qualification or {})
    row.evidence_json = list(evidence or [])
    row.scorecard_json = dict(scorecard or {})
    row.payload = dict(payload or {})
    row.updated_at = now
    session.add(row)
    session.flush()
    return row


def assess_supply_chain_bottlenecks(
    session: Session,
    chain: SupplyChain,
    *,
    snapshot: SupplyChainSnapshot | None = None,
    output_unit: str | None = None,
    evidence: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    import_legacy_chain_if_needed(session, chain)
    snapshot = snapshot or ensure_draft_snapshot(session, chain)
    graph = graph_from_snapshot(session, snapshot, chain_id=int(chain.id))
    nodes = [dict(row) for row in _as_list(graph.get("nodes")) if isinstance(row, dict)]
    candidates = [score_supply_chain_node(node) for node in nodes]
    candidates = [row for row in candidates if row["node_key"]]
    candidates.sort(key=lambda row: (float(row["composite_score"]), float(row["axes"].get("bindingness") or 0.0)), reverse=True)
    if not candidates:
        return {"supply_chain_id": int(chain.id), "assessment": None, "candidates": []}

    active = candidates[0]
    shadow = candidates[1] if len(candidates) > 1 else None
    node_key = active["node_key"]
    scope_key = f"supply_chain:{int(chain.id)}:node:{node_key}"
    out_unit = output_unit or chain.product or "completed system output"
    title = f"{chain.title}: {active['node_name']} bottleneck"
    evidence_rows = list(active.get("evidence") or []) + list(evidence or [])
    leverage = dict(active.get("marginal_system_leverage") or {})
    if "output_unit" not in leverage:
        leverage["output_unit"] = out_unit
    if "question" not in leverage:
        leverage["question"] = "If one more qualified constrained input unit is added, how much downstream system output unlocks before another constraint binds?"
    scorecard = {
        "axes": dict(active["axes"]),
        "composite_score": active["composite_score"],
        "regime": active["regime"],
        "ranked_candidates": [
            {
                "node_key": row["node_key"],
                "node_name": row["node_name"],
                "regime": row["regime"],
                "composite_score": row["composite_score"],
                "axes": dict(row["axes"]),
                "qualified_supplier_count": row.get("qualified_supplier_count"),
            }
            for row in candidates[:10]
        ],
        "scoring_semantics": active.get("scoring_semantics") or {},
    }
    model = upsert_bottleneck_model(
        session,
        title=title,
        model_key=scope_key,
        scope_type="supply_chain_node",
        scope_key=scope_key,
        supply_chain_id=int(chain.id),
        snapshot_id=int(snapshot.id),
        constrained_input=active["node_name"],
        output_unit=out_unit,
        regime=active["regime"],
        status="active",
        summary=f"{active['node_name']} is the active modeled bottleneck for {chain.title}; "
        + (f"{shadow['node_name']} is the shadow constraint." if shadow else "no shadow constraint is ranked yet."),
        thesis=None,
        qualified_supplier_count=active.get("qualified_supplier_count"),
        supplier_qualification=dict(active.get("supplier_qualification") or {}),
        evidence=evidence_rows,
        payload={"scorecard": scorecard},
    )
    assessment = record_bottleneck_assessment(
        session,
        model=model,
        scope_type="supply_chain_node",
        scope_key=scope_key,
        supply_chain_id=int(chain.id),
        snapshot_id=int(snapshot.id),
        active_node_key=node_key,
        shadow_node_key=shadow["node_key"] if shadow else None,
        constrained_input=active["node_name"],
        output_unit=out_unit,
        regime=active["regime"],
        classification=active["regime"],
        summary=f"Active bottleneck: {active['node_name']}"
        + (f"; shadow bottleneck: {shadow['node_name']}" if shadow else ""),
        confidence=_ratio(active.get("confidence")),
        axes=dict(active["axes"]),
        qualified_supplier_count=active.get("qualified_supplier_count"),
        marginal_system_leverage=leverage,
        supplier_qualification=dict(active.get("supplier_qualification") or {}),
        evidence=evidence_rows,
        scorecard=scorecard,
        payload={"regime_definitions": BOTTLENECK_REGIMES},
    )
    session.flush()
    return {
        "supply_chain_id": int(chain.id),
        "snapshot_id": int(snapshot.id),
        "model": serialize_bottleneck_model(model),
        "assessment": serialize_bottleneck_assessment(assessment),
        "active": scorecard["ranked_candidates"][0],
        "shadow": scorecard["ranked_candidates"][1] if len(scorecard["ranked_candidates"]) > 1 else None,
        "candidates": scorecard["ranked_candidates"],
    }
