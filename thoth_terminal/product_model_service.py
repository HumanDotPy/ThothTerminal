"""Versioned product calculations and configurations with explicit dependencies."""

from __future__ import annotations

import math
import json
import re
from copy import deepcopy
from typing import Any

from sqlalchemy.orm import Session

from thoth_terminal.cloud_goal_run_service import append_cloud_goal_event
from thoth_terminal.db import utcnow
from thoth_terminal.models import (
    Document, ProductConfigurationRevision, ProductModelRevision, ResearchSubject,
)
from thoth_terminal.worldview_write_service import _authorized_run, _replayed_receipt


_KEY = re.compile(r"^[a-z][a-z0-9_]{1,79}$")
_UNIT = re.compile(r"^[A-Za-z0-9_$%.-]+(?:[*/][A-Za-z0-9_$%.-]+)*$")
_BOUNDARIES = {"chip", "package", "board", "node", "rack", "scale_up_domain", "campus", "market", "other"}
_OPERATIONS = {"sum", "subtract", "multiply", "divide", "minimum", "maximum"}
_COUNT_UNITS = {"stacks": "stack", "accelerators": "accelerator", "systems": "system",
                "chips": "chip", "racks": "rack", "nodes": "node", "packages": "package",
                "dies": "die", "domains": "domain"}


def _key(value: object, label: str) -> str:
    result = str(value or "").strip().lower()
    if not _KEY.fullmatch(result):
        raise ValueError(f"{label} must use 2-80 lowercase letters, digits, or underscores")
    return result


def _source_ids(session: Session, values: object) -> list[int]:
    if not isinstance(values, list) or len(values) > 100 or any(type(value) is not int or value < 1 for value in values):
        raise ValueError("source_document_ids must be a list of at most 100 stored document IDs")
    ids = sorted(set(values))
    if ids:
        found = {int(row[0]) for row in session.query(Document.id).filter(Document.id.in_(ids)).all()}
        if found != set(ids):
            raise ValueError("source_document_ids must resolve to stored documents")
    return ids


def _unit_signature(unit: object) -> dict[str, int]:
    label = str(unit or "").strip()
    if label == "1":
        return {}
    if not _UNIT.fullmatch(label):
        raise ValueError(f"Invalid unit {label!r}; use explicit products and ratios such as GB/system")
    signature: dict[str, int] = {}
    sign = 1
    for part in re.split(r"([*/])", label):
        if part == "*":
            sign = 1
        elif part == "/":
            sign = -1
        elif part:
            if part != "1":
                canonical = _COUNT_UNITS.get(part, part)
                signature[canonical] = signature.get(canonical, 0) + sign
    return {name: power for name, power in signature.items() if power}


def _combine_units(left: dict[str, int], right: dict[str, int], sign: int = 1) -> dict[str, int]:
    combined = dict(left)
    for name, power in right.items():
        combined[name] = combined.get(name, 0) + sign * power
    return {name: power for name, power in combined.items() if power}


def validate_product_model(variables: object, outputs: object) -> dict[str, list[str]]:
    """Reject cycles and dimensional mistakes before a model revision becomes active."""
    if not isinstance(variables, list) or not 1 <= len(variables) <= 120:
        raise ValueError("variables must contain 1-120 nodes")
    if not isinstance(outputs, list) or not 1 <= len(outputs) <= 40:
        raise ValueError("outputs must contain 1-40 variable keys")
    nodes: dict[str, dict[str, Any]] = {}
    for raw in variables:
        if not isinstance(raw, dict):
            raise ValueError("Each variable must be an object")
        key = _key(raw.get("key"), "variable key")
        if key in nodes:
            raise ValueError(f"Duplicate variable {key}")
        _unit_signature(raw.get("unit"))
        nodes[key] = raw
    dependencies: dict[str, list[str]] = {}
    for key, node in nodes.items():
        formula = node.get("formula")
        if formula is None:
            dependencies[key] = []
            continue
        if not isinstance(formula, dict) or formula.get("op") not in _OPERATIONS:
            raise ValueError(f"{key} needs a supported formula operation")
        args = formula.get("inputs")
        if not isinstance(args, list) or not 2 <= len(args) <= 12:
            raise ValueError(f"{key} needs 2-12 input variables")
        refs = [_key(value, "formula input") for value in args]
        if any(ref not in nodes for ref in refs):
            raise ValueError(f"{key} references a variable absent from this model")
        if formula["op"] in {"subtract", "divide"} and len(refs) != 2:
            raise ValueError(f"{key} {formula['op']} requires exactly two inputs")
        dependencies[key] = refs
        signatures = [_unit_signature(nodes[ref]["unit"]) for ref in refs]
        op = formula["op"]
        if op in {"sum", "subtract", "minimum", "maximum"}:
            if any(item != signatures[0] for item in signatures[1:]):
                raise ValueError(f"{key} combines incompatible units")
            computed_unit = signatures[0]
        else:
            computed_unit = signatures[0]
            for signature in signatures[1:]:
                computed_unit = _combine_units(computed_unit, signature, -1 if op == "divide" else 1)
        if computed_unit != _unit_signature(node["unit"]):
            raise ValueError(f"{key} output unit does not match its formula")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(key: str) -> None:
        if key in visiting:
            raise ValueError(f"Circular dependency involving {key}")
        if key in visited:
            return
        visiting.add(key)
        for ref in dependencies[key]:
            visit(ref)
        visiting.remove(key)
        visited.add(key)

    for key in nodes:
        visit(key)
    for value in outputs:
        if _key(value, "output") not in nodes:
            raise ValueError(f"Unknown model output {value}")
    if len(set(outputs)) != len(outputs):
        raise ValueError("Model outputs must be unique")
    return dependencies


def _validate_bridges(bridges: object, variables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(bridges, list) or len(bridges) > 20:
        raise ValueError("bridges must be a list of at most 20 model integrations")
    units = {item["key"]: item["unit"] for item in variables}
    for bridge in bridges:
        if not isinstance(bridge, dict) or bridge.get("kind") != "memory_system_demand":
            raise ValueError("Unknown model bridge kind")
        system_key = bridge.get("system_units_key")
        memory_key = bridge.get("memory_gb_per_system_key")
        if units.get(system_key) != "systems" or units.get(memory_key) != "GB/system":
            raise ValueError("Memory demand bridge requires systems and GB/system variables")
        if not str(bridge.get("memory_product") or "").strip():
            raise ValueError("Memory demand bridge needs a memory_product")
    return bridges


def _number(value: object, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label} must be a finite number")
    return number


def _interval(value: dict[str, Any], label: str) -> tuple[float | None, float, float]:
    point = _number(value["value"], label) if value.get("value") is not None else None
    low = _number(value.get("low", point), f"{label}.low") if value.get("low", point) is not None else None
    high = _number(value.get("high", point), f"{label}.high") if value.get("high", point) is not None else None
    if low is None or high is None or low > high or (point is not None and not low <= point <= high):
        raise ValueError(f"{label} needs a point or a valid low/high range")
    return point, low, high


def _calculated_interval(op: str, values: list[tuple[float | None, float, float]]) -> tuple[float | None, float, float]:
    point = None
    if all(value[0] is not None for value in values):
        points = [float(value[0]) for value in values]
        if op == "sum":
            point = sum(points)
        elif op == "subtract":
            point = points[0] - points[1]
        elif op == "multiply":
            point = math.prod(points)
        elif op == "divide":
            point = points[0] / points[1] if points[1] else None
        elif op == "minimum":
            point = min(points)
        elif op == "maximum":
            point = max(points)
    low, high = values[0][1:]
    for _, next_low, next_high in values[1:]:
        if op == "sum":
            low, high = low + next_low, high + next_high
        elif op == "subtract":
            low, high = low - next_high, high - next_low
        elif op == "multiply":
            products = (low * next_low, low * next_high, high * next_low, high * next_high)
            low, high = min(products), max(products)
        elif op == "divide":
            if next_low <= 0 <= next_high:
                raise ValueError("A divisor range crosses zero")
            quotients = (low / next_low, low / next_high, high / next_low, high / next_high)
            low, high = min(quotients), max(quotients)
        elif op == "minimum":
            low, high = min(low, next_low), min(high, next_high)
        elif op == "maximum":
            low, high = max(low, next_low), max(high, next_high)
    if not math.isfinite(low) or not math.isfinite(high) or (point is not None and not math.isfinite(point)):
        raise ValueError("Calculated value exceeds finite numeric range")
    return point, low, high


def _latest_model(session: Session, model_key: str) -> ProductModelRevision | None:
    return session.query(ProductModelRevision).filter_by(model_key=model_key).order_by(ProductModelRevision.version.desc()).first()


def _latest_configuration(session: Session, configuration_key: str) -> ProductConfigurationRevision | None:
    return session.query(ProductConfigurationRevision).filter_by(configuration_key=configuration_key).order_by(ProductConfigurationRevision.version.desc()).first()


def validate_configuration_inputs(session: Session, model: ProductModelRevision, inputs: object) -> None:
    if not isinstance(inputs, dict) or len(inputs) > 120:
        raise ValueError("inputs must be an object of at most 120 variable bindings")
    nodes = {node["key"]: node for node in model.variables_json}
    for input_key, binding in inputs.items():
        if input_key not in nodes or nodes[input_key].get("formula") is not None:
            raise ValueError(f"{input_key} is not an input variable in the pinned model")
        if not isinstance(binding, dict) or binding.get("unit") != nodes[input_key]["unit"]:
            raise ValueError(f"{input_key} needs the model's exact unit")
        if binding.get("from_configuration_revision_id") is not None:
            if binding.get("value") is not None or binding.get("low") is not None:
                raise ValueError(f"{input_key} cannot be both a child output and a literal value")
            child = session.get(ProductConfigurationRevision, int(binding["from_configuration_revision_id"]))
            if child is None or not binding.get("output_key"):
                raise ValueError(f"{input_key} needs a stored child configuration and output_key")
        else:
            _interval(binding, input_key)
        binding_sources = binding.get("source_document_ids", [])
        _source_ids(session, binding_sources)
        if not binding_sources and not str(binding.get("rationale") or "").strip() and binding.get("from_configuration_revision_id") is None:
            raise ValueError(f"{input_key} needs source_document_ids or a house-assumption rationale")


def revise_product_model(session: Session, payload: dict[str, Any], *, run_id: int | None = None) -> ProductModelRevision:
    if not isinstance(payload, dict) or len(json.dumps(payload, ensure_ascii=False, default=str)) > 100_000:
        raise ValueError("Model revision must be an object under 100,000 characters")
    model_key = _key(payload.get("model_key"), "model_key")
    prior = _latest_model(session, model_key)
    rollback_id = payload.get("rollback_to_revision_id")
    if rollback_id is not None:
        target = session.get(ProductModelRevision, int(rollback_id))
        if prior is None or target is None or target.model_key != model_key or target.version >= prior.version:
            raise ValueError("Rollback target must be a prior revision of this model")
        forbidden = set(payload) - {"model_key", "rollback_to_revision_id", "source_document_ids", "change_summary"}
        if forbidden:
            raise ValueError("Rollback restores the pinned model definition without other changes")
        base = target
    else:
        base = prior
    name = str(payload.get("name") or (base.name if base else "")).strip()
    boundary = str(payload.get("boundary") or (base.boundary if base else "")).strip()
    if not name or len(name) > 255 or boundary not in _BOUNDARIES:
        raise ValueError("A model needs a name and a recognized physical boundary")
    subject_id = payload.get("subject_id", base.subject_id if base else None)
    if subject_id is not None and session.get(ResearchSubject, int(subject_id)) is None:
        raise ValueError("subject_id must identify an existing research subject")
    variables = deepcopy(payload.get("variables", base.variables_json if base else None))
    changes = payload.get("variable_changes")
    if changes is not None:
        if not isinstance(changes, list) or len(changes) > 40 or variables is None:
            raise ValueError("variable_changes needs an existing or supplied model and at most 40 changes")
        by_key = {item["key"]: item for item in variables}
        for change in changes:
            if not isinstance(change, dict):
                raise ValueError("Each variable change must be an object")
            changed_key = _key(change.get("key"), "variable key")
            if change.get("action") == "remove":
                by_key.pop(changed_key, None)
            elif change.get("action", "upsert") == "upsert":
                by_key[changed_key] = {key: value for key, value in change.items() if key != "action"}
            else:
                raise ValueError("Variable change action must be upsert or remove")
        variables = list(by_key.values())
    outputs = deepcopy(payload.get("outputs", base.outputs_json if base else None))
    if outputs is not None:
        outputs = [value for value in outputs if value not in (payload.get("output_remove") or [])]
        outputs.extend(value for value in payload.get("output_add") or [] if value not in outputs)
    validate_product_model(variables, outputs)
    bridges = deepcopy(payload.get("bridges", base.bridges_json if base else []))
    _validate_bridges(bridges, variables)
    premises = deepcopy(payload.get("premises", base.premises_json if base else []))
    if not isinstance(premises, list) or len(premises) > 40:
        raise ValueError("premises must be a list of at most 40 items")
    for premise in premises:
        if not isinstance(premise, dict) or len(str(premise.get("statement") or "").strip()) < 12:
            raise ValueError("Each premise needs a causal statement of at least 12 characters")
        _source_ids(session, premise.get("source_document_ids", []))
    summary = str(payload.get("change_summary") or "").strip()
    if len(summary) < 20:
        raise ValueError("change_summary must explain the model change")
    source_ids = _source_ids(session, [
        *(base.source_document_ids_json if base else []),
        *(payload.get("source_document_ids") or []),
        *(source for premise in premises for source in premise.get("source_document_ids", [])),
    ])
    if not source_ids and not premises:
        raise ValueError("A model without cited documents needs an explicit causal premise")
    row = ProductModelRevision(
        model_key=model_key, version=(prior.version + 1 if prior else 1), name=name,
        boundary=boundary, subject_id=int(subject_id) if subject_id is not None else None,
        variables_json=variables, outputs_json=outputs, bridges_json=bridges, premises_json=premises,
        source_document_ids_json=source_ids, change_summary=summary,
        supersedes_id=prior.id if prior else None, created_by_run_id=run_id, created_at=utcnow(),
    )
    session.add(row)
    session.flush()
    return row


def revise_product_configuration(session: Session, payload: dict[str, Any], *, run_id: int | None = None) -> ProductConfigurationRevision:
    if not isinstance(payload, dict) or len(json.dumps(payload, ensure_ascii=False, default=str)) > 100_000:
        raise ValueError("Configuration revision must be an object under 100,000 characters")
    key = _key(payload.get("configuration_key"), "configuration_key")
    prior = _latest_configuration(session, key)
    fork_id = payload.get("fork_from_revision_id")
    rollback_id = payload.get("rollback_to_revision_id")
    if prior and fork_id is not None:
        raise ValueError("fork_from_revision_id is only valid for a new configuration")
    if fork_id is not None and rollback_id is not None:
        raise ValueError("A revision cannot fork and roll back simultaneously")
    base = prior
    if fork_id is not None or rollback_id is not None:
        base = session.get(ProductConfigurationRevision, int(fork_id or rollback_id))
        if base is None or (rollback_id is not None and (prior is None or base.configuration_key != key or base.version >= prior.version)):
            raise ValueError("Configuration fork or rollback target is invalid")
        if rollback_id is not None:
            forbidden = set(payload) - {"configuration_key", "rollback_to_revision_id", "source_document_ids", "change_summary"}
            if forbidden:
                raise ValueError("Rollback restores the pinned configuration without other changes")
    model_revision_id = payload.get("model_revision_id", base.model_revision_id if base else None)
    model = session.get(ProductModelRevision, int(model_revision_id)) if model_revision_id is not None else None
    if model is None:
        raise ValueError("model_revision_id must pin an existing model revision")
    name = str(payload.get("name") or (base.name if base else "")).strip()
    product_key = _key(payload.get("product_key", base.product_key if base else ""), "product_key")
    if not name or len(name) > 255:
        raise ValueError("A configuration needs a name")
    inputs = deepcopy(payload.get("inputs", base.inputs_json if base else {}))
    input_changes = payload.get("input_changes")
    if input_changes is not None:
        if not isinstance(input_changes, dict) or len(input_changes) > 40:
            raise ValueError("input_changes must be an object of at most 40 bindings")
        for changed_key, binding in input_changes.items():
            if binding is None:
                inputs.pop(changed_key, None)
            else:
                inputs[changed_key] = binding
    validate_configuration_inputs(session, model, inputs)
    summary = str(payload.get("change_summary") or "").strip()
    if len(summary) < 20:
        raise ValueError("change_summary must explain the configuration change")
    source_ids = _source_ids(session, [
        *(base.source_document_ids_json if base else []),
        *(payload.get("source_document_ids") or []),
        *(source for binding in inputs.values() for source in binding.get("source_document_ids", [])),
    ])
    period_months_raw = payload.get("period_months", base.period_months if base else None)
    period_months = _number(period_months_raw, "period_months") if period_months_raw is not None else None
    if period_months is not None and not 0 < period_months <= 120:
        raise ValueError("period_months must be in (0, 120]")
    row = ProductConfigurationRevision(
        configuration_key=key, version=(prior.version + 1 if prior else 1), name=name,
        product_key=product_key, period=str(payload.get("period", base.period if base else "") or "")[:64] or None,
        period_months=period_months,
        world_key=str(payload.get("world_key", base.world_key if base else "") or "")[:128] or None,
        model_revision_id=model.id, inputs_json=inputs, source_document_ids_json=source_ids,
        change_summary=summary, supersedes_id=prior.id if prior else None,
        forked_from_id=base.id if prior is None and base is not None else None,
        created_by_run_id=run_id, created_at=utcnow(),
    )
    session.add(row)
    session.flush()
    # This also validates composed child outputs and catches cross-configuration cycles.
    evaluate_product_configuration(session, row.id)
    return row


def evaluate_product_configuration(
    session: Session, revision_id: int, *, _active: set[int] | None = None,
    memory_context_payload: dict[str, Any] | None = None,
    _configuration_override: object | None = None,
    _child_result_overrides: dict[int, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    row = _configuration_override or session.get(ProductConfigurationRevision, int(revision_id))
    if row is None:
        raise ValueError("Product configuration revision not found")
    active = set(_active or set())
    if row.id in active:
        raise ValueError("Circular product-configuration reference")
    active.add(row.id)
    model = session.get(ProductModelRevision, row.model_revision_id)
    if model is None:
        raise ValueError("Pinned product model revision not found")
    dependencies = validate_product_model(model.variables_json, model.outputs_json)
    nodes = {node["key"]: node for node in model.variables_json}
    values: dict[str, dict[str, Any]] = {}

    def resolve(key: str) -> dict[str, Any]:
        if key in values:
            return values[key]
        node = nodes[key]
        refs = dependencies[key]
        if refs:
            parts = [resolve(ref) for ref in refs]
            missing = sorted({item for part in parts for item in part.get("missing_inputs", [])})
            if missing:
                result = {"value": None, "low": None, "high": None, "unit": node["unit"],
                          "basis": "missing", "depends_on": refs, "missing_inputs": missing,
                          "source_document_ids": sorted({source for part in parts for source in part.get("source_document_ids", [])})}
            else:
                point, low, high = _calculated_interval(node["formula"]["op"],
                                                        [(part["value"], part["low"], part["high"]) for part in parts])
                result = {"value": point, "low": low, "high": high, "unit": node["unit"],
                          "basis": "derived", "depends_on": refs, "missing_inputs": [],
                          "source_document_ids": sorted({source for part in parts for source in part.get("source_document_ids", [])})}
        else:
            binding = row.inputs_json.get(key)
            if binding is None:
                result = {"value": None, "low": None, "high": None, "unit": node["unit"],
                          "basis": "missing", "depends_on": [], "missing_inputs": [key], "source_document_ids": []}
            elif binding.get("from_configuration_revision_id") is not None:
                child_id = int(binding["from_configuration_revision_id"])
                child = (_child_result_overrides or {}).get(child_id)
                if child is None:
                    child = evaluate_product_configuration(session, child_id, _active=active,
                                                           _child_result_overrides=_child_result_overrides)
                output_key = str(binding["output_key"])
                child_value = child["outputs"].get(output_key)
                if child_value is None or child_value["unit"] != node["unit"]:
                    raise ValueError(f"{key} child output is absent or has an incompatible unit")
                result = {**child_value, "basis": "pinned_child_configuration", "depends_on": [],
                          "child_configuration_revision_id": child["configuration"]["id"]}
            else:
                point, low, high = _interval(binding, key)
                result = {"value": point, "low": low, "high": high, "unit": node["unit"],
                          "basis": str(binding.get("lineage") or "house_assumption"), "depends_on": [],
                          "missing_inputs": [], "source_document_ids": list(binding.get("source_document_ids") or []),
                          "rationale": binding.get("rationale")}
        values[key] = result
        return result

    for key in nodes:
        resolve(key)
    outputs = {key: values[key] for key in model.outputs_json}
    memory_bridges: list[dict[str, Any]] = []
    for bridge in model.bridges_json or []:
        systems = values[bridge["system_units_key"]]
        memory = values[bridge["memory_gb_per_system_key"]]
        result_bridge: dict[str, Any] = {
            "kind": "memory_system_demand", "memory_product": bridge["memory_product"],
            "system_units_key": bridge["system_units_key"],
            "memory_gb_per_system_key": bridge["memory_gb_per_system_key"],
            "missing_inputs": sorted(set(systems.get("missing_inputs", []) + memory.get("missing_inputs", []))),
            "demand_gb_range": None,
            "memory_system_payload_patch": None,
        }
        if not result_bridge["missing_inputs"]:
            products = (systems["low"] * memory["low"], systems["low"] * memory["high"],
                        systems["high"] * memory["low"], systems["high"] * memory["high"])
            result_bridge["demand_gb_range"] = {"low": min(products), "high": max(products)}
            if row.period_months is None:
                result_bridge["missing_inputs"].append("period_months")
            if systems["value"] is not None and memory["value"] is not None and row.period_months is not None:
                result_bridge["memory_system_payload_patch"] = {
                    "system_shipments": [{"system_or_platform": row.configuration_key,
                                          "period": row.period, "system_units": systems["value"],
                                          "period_months": row.period_months,
                                          "lineage": "model_derived", "source_document_id": None,
                                          "assumption_id": f"product_configuration_revision:{row.id}"}],
                    "memory_bom": [{"system_or_platform": row.configuration_key,
                                    "period": row.period, "product": bridge["memory_product"],
                                    "memory_gb_per_system": memory["value"], "lineage": "model_derived",
                                    "assumption_id": f"product_configuration_revision:{row.id}"}],
                }
        memory_bridges.append(result_bridge)
    result = {
        "model": {"id": model.id, "key": model.model_key, "version": model.version,
                  "name": model.name, "boundary": model.boundary},
        "configuration": {"id": row.id, "key": row.configuration_key, "version": row.version,
                          "name": row.name, "product_key": row.product_key, "period": row.period,
                          "period_months": row.period_months,
                          "world_key": row.world_key, "source_document_ids": row.source_document_ids_json,
                          "change_summary": row.change_summary},
        "variables": values, "outputs": outputs, "dependencies": dependencies,
        "missing_inputs": sorted({missing for item in outputs.values() for missing in item.get("missing_inputs", [])}),
        "memory_bridges": memory_bridges,
    }
    if memory_context_payload is not None:
        if not isinstance(memory_context_payload, dict) or len(json.dumps(memory_context_payload, default=str)) > 150_000:
            raise ValueError("memory_context_payload must be a bounded object")
        patches = [bridge["memory_system_payload_patch"] for bridge in memory_bridges
                   if bridge["memory_system_payload_patch"] is not None]
        if patches:
            from thoth_terminal.memory_system_model import memory_system_model

            memory_payload = deepcopy(memory_context_payload)
            for key in ("system_shipments", "memory_bom"):
                memory_payload[key] = [*list(memory_payload.get(key) or []),
                                       *(item for patch in patches for item in patch[key])]
            result["memory_system_result"] = memory_system_model(memory_payload)
        else:
            result["memory_system_result"] = {"status": "missing_product_demand_inputs"}
    return result


def list_product_models(
    session: Session, *, query: str = "", subject_id: int | None = None,
    include_history: bool = False, limit: int = 60,
) -> dict[str, Any]:
    models = session.query(ProductModelRevision).order_by(ProductModelRevision.id.desc()).limit(2000).all()
    configurations = session.query(ProductConfigurationRevision).order_by(ProductConfigurationRevision.id.desc()).limit(3000).all()
    latest_models: dict[str, ProductModelRevision] = {}
    latest_configs: dict[str, ProductConfigurationRevision] = {}
    model_keys_by_id = {row.id: row.model_key for row in models}
    for row in models:
        latest_models.setdefault(row.model_key, row)
    for row in configurations:
        latest_configs.setdefault(row.configuration_key, row)
    needle = query.strip().casefold()
    entries = []
    for row in latest_models.values():
        if subject_id is not None and row.subject_id != subject_id:
            continue
        if needle and needle not in f"{row.model_key} {row.name} {row.boundary}".casefold():
            continue
        entry = {"id": row.id, "model_key": row.model_key, "version": row.version,
                 "name": row.name, "boundary": row.boundary, "subject_id": row.subject_id,
                 "outputs": row.outputs_json, "variables": row.variables_json,
                 "bridges": row.bridges_json,
                 "premises": row.premises_json, "change_summary": row.change_summary,
                 "source_document_ids": row.source_document_ids_json,
                 "configurations": [
                     {"id": item.id, "configuration_key": item.configuration_key, "version": item.version,
                      "name": item.name, "product_key": item.product_key, "period": item.period,
                      "period_months": item.period_months,
                      "world_key": item.world_key, "change_summary": item.change_summary}
                     for item in latest_configs.values()
                     if model_keys_by_id.get(item.model_revision_id) == row.model_key
                 ]}
        if include_history:
            entry["history"] = [{"id": item.id, "version": item.version, "change_summary": item.change_summary}
                                for item in models if item.model_key == row.model_key][:30]
        entries.append(entry)
        if len(entries) >= max(1, min(limit, 100)):
            break
    return {"models": entries, "count": len(entries),
            "truncated": len(models) == 2000 or len(configurations) == 3000}


def get_product_configuration(
    session: Session, *, configuration_key: str, include_history: bool = False,
    memory_context_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    key = _key(configuration_key, "configuration_key")
    row = _latest_configuration(session, key)
    if row is None:
        raise ValueError("Product configuration not found")
    result = evaluate_product_configuration(session, row.id, memory_context_payload=memory_context_payload)
    if include_history:
        from thoth_terminal.product_model_impact_service import product_configuration_revision_impact

        result["impact"] = product_configuration_revision_impact(session, row.id)
        history = session.query(ProductConfigurationRevision).filter_by(configuration_key=key).order_by(
            ProductConfigurationRevision.version.desc()).limit(50).all()
        result["history"] = [{"id": item.id, "version": item.version, "model_revision_id": item.model_revision_id,
                              "change_summary": item.change_summary, "source_document_ids": item.source_document_ids_json,
                              "supersedes_id": item.supersedes_id, "forked_from_id": item.forked_from_id}
                             for item in history]
    return result


def list_world_product_configurations(session: Session, *, subject_id: int, world_key: str) -> list[dict[str, Any]]:
    versions = session.query(ProductModelRevision.id).filter_by(subject_id=subject_id).all()
    model_ids = [int(row[0]) for row in versions]
    if not model_ids:
        return []
    rows = session.query(ProductConfigurationRevision).filter(
        ProductConfigurationRevision.model_revision_id.in_(model_ids),
        ProductConfigurationRevision.world_key == world_key,
    ).order_by(ProductConfigurationRevision.id.desc()).limit(500).all()
    latest: dict[str, ProductConfigurationRevision] = {}
    for row in rows:
        latest.setdefault(row.configuration_key, row)
    return [{"key": row.configuration_key, "name": row.name, "version": row.version,
             "product_key": row.product_key, "period": row.period, "model_revision_id": row.model_revision_id}
            for row in latest.values()]


def apply_worldview_product_model(
    session: Session, *, cloud_run_id: int, capability: str, operation_key: str, payload: dict[str, Any],
) -> dict[str, Any]:
    if not 1 <= len(str(operation_key or "").strip()) <= 120:
        raise ValueError("operation_key must be 1-120 characters")
    run = _authorized_run(session, cloud_run_id=cloud_run_id, capability=capability)
    replay = _replayed_receipt(session, cloud_run_id=cloud_run_id,
                               event_kind="worldview_product_model_write", operation_key=operation_key)
    if replay is not None:
        return replay
    row = revise_product_model(session, payload, run_id=cloud_run_id)
    from thoth_terminal.product_model_impact_service import _latest_configurations

    current_configurations, config_scan_truncated = _latest_configurations(session)
    pinned = [item for item in current_configurations if item.model_revision_id == row.supersedes_id]
    receipt = {"model_revision_id": row.id, "model_key": row.model_key, "version": row.version,
               "previous_revision_id": row.supersedes_id, "source_document_ids": row.source_document_ids_json,
               "configurations_pinned_to_previous": [item.configuration_key for item in pinned[:80]],
               "pinned_configurations_truncated": config_scan_truncated or len(pinned) > 80,
               "replayed": False}
    append_cloud_goal_event(session, run, event_kind="worldview_product_model_write",
                            summary=row.change_summary, payload={"operation_key": operation_key, "receipt": receipt})
    return receipt


def apply_worldview_product_configuration(
    session: Session, *, cloud_run_id: int, capability: str, operation_key: str, payload: dict[str, Any],
) -> dict[str, Any]:
    if not 1 <= len(str(operation_key or "").strip()) <= 120:
        raise ValueError("operation_key must be 1-120 characters")
    run = _authorized_run(session, cloud_run_id=cloud_run_id, capability=capability)
    replay = _replayed_receipt(session, cloud_run_id=cloud_run_id,
                               event_kind="worldview_product_configuration_write", operation_key=operation_key)
    if replay is not None:
        return replay
    row = revise_product_configuration(session, payload, run_id=cloud_run_id)
    evaluated = evaluate_product_configuration(session, row.id)
    from thoth_terminal.product_model_impact_service import product_configuration_revision_impact

    impact = product_configuration_revision_impact(session, row.id)
    receipt = {"configuration_revision_id": row.id, "configuration_key": row.configuration_key,
               "version": row.version, "model_revision_id": row.model_revision_id,
               "previous_revision_id": row.supersedes_id, "forked_from_revision_id": row.forked_from_id,
               "outputs": evaluated["outputs"], "missing_inputs": evaluated["missing_inputs"],
               "impact": impact, "replayed": False}
    append_cloud_goal_event(session, run, event_kind="worldview_product_configuration_write",
                            summary=row.change_summary, payload={"operation_key": operation_key, "receipt": receipt})
    return receipt
