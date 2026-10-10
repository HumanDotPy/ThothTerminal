"""Read-only causal impact previews for sourced product-model changes."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from typing import Any

from sqlalchemy.orm import Session

from thoth_terminal.models import Document, ProductConfigurationRevision, ProductModelRevision, ResearchSubject
from thoth_terminal.product_model_service import (
    _key, _latest_configuration, evaluate_product_configuration, validate_configuration_inputs,
)


def _latest_configurations(session: Session) -> tuple[list[ProductConfigurationRevision], bool]:
    rows = session.query(ProductConfigurationRevision).order_by(ProductConfigurationRevision.id.desc()).limit(3001).all()
    latest: dict[str, ProductConfigurationRevision] = {}
    for row in rows[:3000]:
        latest.setdefault(row.configuration_key, row)
    return list(latest.values()), len(rows) > 3000


def compare_product_outputs(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Compare points and intervals without replacing an unresolved range with a midpoint."""
    changes: dict[str, dict[str, Any]] = {}
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key), after.get(key)
        if old == new:
            continue
        if old is None or new is None or old.get("unit") != new.get("unit"):
            status = "definition_changed"
        elif old.get("low") is None and new.get("low") is not None:
            status = "resolved"
        elif old.get("low") is not None and new.get("low") is None:
            status = "became_unresolved"
        elif old.get("low") is None and new.get("low") is None:
            status = ("missing_dependencies_changed" if old.get("missing_inputs") != new.get("missing_inputs")
                      else "unresolved_lineage_changed")
        elif any(old.get(field) != new.get(field) for field in ("value", "low", "high")):
            status = "value_changed"
        else:
            status = "lineage_changed"
        delta = None
        if old is not None and new is not None and old.get("unit") == new.get("unit"):
            delta = {field: new[field] - old[field] if old.get(field) is not None and new.get(field) is not None else None
                     for field in ("value", "low", "high")}
        changes[key] = {"status": status, "before": old, "after": new, "delta": delta}
    return changes


def _dependent_previews(
    session: Session, changed_revision_id: int, proposed_result: dict[str, Any], *, limit: int = 80,
) -> dict[str, Any]:
    latest, scan_truncated = _latest_configurations(session)
    parents: dict[int, list[tuple[ProductConfigurationRevision, list[str]]]] = {}
    for row in latest:
        for key, binding in (row.inputs_json or {}).items():
            if isinstance(binding, dict) and binding.get("from_configuration_revision_id") is not None:
                child_id = int(binding["from_configuration_revision_id"])
                parent_rows = parents.setdefault(child_id, [])
                entry = next((item for item in parent_rows if item[0].id == row.id), None)
                if entry is None:
                    parent_rows.append((row, [key]))
                else:
                    entry[1].append(key)
    overrides = {changed_revision_id: proposed_result}
    pending = [changed_revision_id]
    reports: dict[int, dict[str, Any]] = {}
    steps = 0
    while pending and steps < limit:
        child_id = pending.pop(0)
        for parent, binding_keys in parents.get(child_id, []):
            steps += 1
            before = evaluate_product_configuration(session, parent.id)
            after = evaluate_product_configuration(session, parent.id, _child_result_overrides=overrides)
            changes = compare_product_outputs(before["outputs"], after["outputs"])
            reports[parent.id] = {
                "configuration_key": parent.configuration_key, "configuration_revision_id": parent.id,
                "world_key": parent.world_key, "product_key": parent.product_key,
                "pinned_child_revision_id": child_id, "binding_keys": binding_keys,
                "projected_output_changes": changes,
                "action": "revise this configuration's child binding to propagate the change",
            }
            overrides[parent.id] = after
            if parent.id not in pending:
                pending.append(parent.id)
            if steps >= limit:
                break
    return {"dependent_configurations": list(reports.values()),
            "truncated": scan_truncated or bool(pending), "scanned_latest_configurations": len(latest)}


def preview_product_configuration_change(
    session: Session, *, configuration_key: str, source_document_id: int,
    input_changes: dict[str, Any] | None = None, model_revision_id: int | None = None,
) -> dict[str, Any]:
    """Calculate a proposed sourced change and its downstream effect without a write."""
    source = session.get(Document, int(source_document_id))
    if source is None:
        raise ValueError("source_document_id must identify a stored document")
    prior = _latest_configuration(session, _key(configuration_key, "configuration_key"))
    if prior is None:
        raise ValueError("Product configuration not found")
    if not isinstance(input_changes, dict) or len(input_changes) > 40:
        raise ValueError("input_changes must contain at most 40 proposed bindings")
    model = session.get(ProductModelRevision, int(model_revision_id or prior.model_revision_id))
    if model is None or model.model_key != session.get(ProductModelRevision, prior.model_revision_id).model_key:
        raise ValueError("model_revision_id must belong to this configuration's model")
    model_changed = model.id != prior.model_revision_id
    if not input_changes and not model_changed:
        raise ValueError("Preview needs an input change or a new pinned model revision")
    inputs = deepcopy(prior.inputs_json)
    for key, binding in input_changes.items():
        if binding is None:
            inputs.pop(key, None)
        else:
            inputs[key] = binding
    validate_configuration_inputs(session, model, inputs)
    if any(binding is not None and source.id not in (binding.get("source_document_ids") or [])
           for binding in input_changes.values()) or (model_changed and source.id not in (model.source_document_ids_json or [])):
        raise ValueError("Each proposed binding and new model revision must cite source_document_id")
    candidate = SimpleNamespace(
        id=prior.id, model_revision_id=model.id, inputs_json=inputs,
        configuration_key=prior.configuration_key, version=prior.version + 1,
        name=prior.name, product_key=prior.product_key, period=prior.period,
        period_months=prior.period_months, world_key=prior.world_key,
        source_document_ids_json=sorted(set((prior.source_document_ids_json or []) + [source.id])),
        change_summary="Read-only impact preview",
    )
    before = evaluate_product_configuration(session, prior.id)
    after = evaluate_product_configuration(session, prior.id, _configuration_override=candidate)
    downstream = _dependent_previews(session, prior.id, after)
    return {
        "source_document_id": source.id, "configuration_key": prior.configuration_key,
        "base_revision_id": prior.id, "model_revision_id": model.id,
        "proposed_input_keys": sorted(input_changes),
        "output_changes": compare_product_outputs(before["outputs"], after["outputs"]),
        "before_missing_inputs": before["missing_inputs"], "after_missing_inputs": after["missing_inputs"],
        "before_memory_bridges": before["memory_bridges"], "after_memory_bridges": after["memory_bridges"],
        **downstream,
        "applied": False,
    }


def product_configuration_revision_impact(session: Session, revision_id: int) -> dict[str, Any]:
    row = session.get(ProductConfigurationRevision, int(revision_id))
    if row is None:
        raise ValueError("Product configuration revision not found")
    after = evaluate_product_configuration(session, row.id)
    before = evaluate_product_configuration(session, row.supersedes_id) if row.supersedes_id else None
    downstream = _dependent_previews(session, row.supersedes_id, after) if row.supersedes_id else {
        "dependent_configurations": [], "truncated": False, "scanned_latest_configurations": 0,
    }
    return {"configuration_revision_id": row.id, "previous_revision_id": row.supersedes_id,
            "output_changes": compare_product_outputs(before["outputs"] if before else {}, after["outputs"]),
            "before_missing_inputs": before["missing_inputs"] if before else [],
            "after_missing_inputs": after["missing_inputs"], **downstream}


def get_source_product_model_context(
    session: Session, *, source_document_id: int, subject_id: int, limit: int = 40,
) -> dict[str, Any]:
    """Expose plausible destinations, without pretending that subject coverage proves impact."""
    if session.get(Document, int(source_document_id)) is None:
        raise ValueError("source_document_id must identify a stored document")
    if session.get(ResearchSubject, int(subject_id)) is None:
        raise ValueError("subject_id must identify an existing research subject")
    bounded = max(1, min(int(limit), 80))
    model_rows = session.query(ProductModelRevision).filter_by(subject_id=subject_id).order_by(
        ProductModelRevision.id.desc()).limit(500).all()
    latest_models: dict[str, ProductModelRevision] = {}
    for row in model_rows:
        latest_models.setdefault(row.model_key, row)
    model_ids = {row.id: row.model_key for row in model_rows}
    configs, config_truncated = _latest_configurations(session)
    configurations = []
    for row in configs:
        if row.model_revision_id not in model_ids:
            continue
        model = session.get(ProductModelRevision, row.model_revision_id)
        configurations.append({
            "configuration_key": row.configuration_key, "revision_id": row.id,
            "model_key": model.model_key, "model_revision_id": model.id,
            "world_key": row.world_key, "product_key": row.product_key,
            "inputs": [{"key": node["key"], "unit": node["unit"],
                        "current_binding": row.inputs_json.get(node["key"]),
                        "source_already_linked": source_document_id in (
                            (row.inputs_json.get(node["key"]) or {}).get("source_document_ids") or [])}
                       for node in model.variables_json if node.get("formula") is None],
            "outputs": model.outputs_json,
        })
    return {"source_document_id": source_document_id, "subject_id": subject_id,
            "models": [{"model_key": row.model_key, "revision_id": row.id, "boundary": row.boundary,
                        "variables": row.variables_json, "outputs": row.outputs_json}
                       for row in list(latest_models.values())[:bounded]],
            "configurations": configurations[:bounded],
            "truncated": len(model_rows) == 500 or config_truncated or len(configurations) > bounded,
            "interpretation": "These are exact-subject model destinations, not automatic claim matches. Read the source and choose a variable, premise, new model, or no change."}
