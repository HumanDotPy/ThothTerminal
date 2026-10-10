from __future__ import annotations

from typing import Any, Iterable


_CAPACITY_FIELDS = {
    "gross_nameplate_wpm": "gross_wpm",
    "equipped_wpm": "equipped_wpm",
    "eligible_wpm": "eligible_wpm",
    "qualified_wpm": "qualified_wpm",
    "utilized_wpm": "utilized_wpm",
}


def _values(row: object) -> dict[str, Any]:
    if isinstance(row, dict):
        nested = row.get("values")
        return dict(nested) if isinstance(nested, dict) else dict(row)
    nested = getattr(row, "values", None)
    return dict(nested) if isinstance(nested, dict) else {}


def _text(value: object) -> str:
    return str(value or "").strip()


def _derived_pool_id(values: dict[str, Any]) -> str:
    parts = [
        _text(values.get("supplier") or values.get("company")),
        _text(values.get("site")),
        _text(values.get("fab") or values.get("facility")),
        _text(values.get("phase")),
        _text(values.get("process_node") or values.get("technology")),
        _text(values.get("period_date") or values.get("period_label")),
    ]
    return "|".join(part.lower().replace(" ", "-") for part in parts if part)


def capacity_pools_from_memory_ledger(rows: Iterable[object]) -> dict[str, Any]:
    pools: dict[str, dict[str, Any]] = {}
    node_allocations: list[dict[str, Any]] = []
    audit: dict[str, list[str]] = {"errors": [], "warnings": [], "derived_fields": []}
    for index, source_row in enumerate(rows):
        values = _values(source_row)
        is_node_allocation = any(
            values.get(key) not in (None, "")
            for key in ("node_pool_id", "node_allocation_wpm", "node_allocation_share")
        )
        if is_node_allocation:
            parent_pool_id = _text(values.get("parent_pool_id") or values.get("physical_pool_id"))
            node_pool_id = _text(values.get("node_pool_id") or values.get("pool_id"))
            process_node = _text(values.get("process_node"))
            if not parent_pool_id or not node_pool_id or not process_node:
                audit["errors"].append(
                    f"rows[{index}] node allocation requires parent_pool_id, node_pool_id, and process_node"
                )
                continue
            node_allocations.append(
                {
                    "parent_pool_id": parent_pool_id,
                    "node_pool_id": node_pool_id,
                    "process_node": process_node,
                    "node_allocation_wpm": values.get("node_allocation_wpm"),
                    "node_allocation_share": values.get("node_allocation_share"),
                    "eligible_node_set": values.get("eligible_node_set"),
                    "node_eligibility_status": values.get("node_eligibility_status"),
                    "node_conversion_start_period": values.get("node_conversion_start_period"),
                    "node_conversion_completion_period": values.get("node_conversion_completion_period"),
                    "period": values.get("period_date") or values.get("period_label"),
                    "lineage": values.get("assumption_lineage"),
                    "source_document_id": values.get("source_document_id"),
                    "source_owner": values.get("source_owner"),
                    "estimate_owner": values.get("estimate_owner"),
                    "confidence": values.get("confidence"),
                }
            )
            continue
        pool_id = _text(values.get("pool_id"))
        if not pool_id:
            pool_id = _derived_pool_id(values)
            if pool_id:
                audit["derived_fields"].append(f"rows[{index}].pool_id")
        if not pool_id:
            audit["errors"].append(f"rows[{index}] has no pool_id or sufficient identity to derive one")
            continue
        target = pools.setdefault(
            pool_id,
            {
                "pool_id": pool_id,
                "supplier": values.get("supplier") or values.get("company"),
                "site": values.get("site"),
                "fab": values.get("fab") or values.get("facility"),
                "phase": values.get("phase"),
                "process_node": values.get("process_node") or values.get("technology"),
                "period": values.get("period_date") or values.get("period_label"),
                "wafer_size_mm": values.get("wafer_size_mm"),
                "lineage": values.get("assumption_lineage"),
                "source_document_id": values.get("source_document_id"),
                "source_owner": values.get("source_owner"),
                "estimate_owner": values.get("estimate_owner"),
                "confidence": values.get("confidence"),
            },
        )
        for ledger_key, model_key in _CAPACITY_FIELDS.items():
            value = values.get(ledger_key)
            if value in (None, ""):
                continue
            existing = target.get(model_key)
            if existing not in (None, "") and existing != value:
                audit["errors"].append(f"pool {pool_id} has conflicting {ledger_key} values: {existing} and {value}")
            else:
                target[model_key] = value
        for key in ("utilization", "ramp_factor"):
            if values.get(key) not in (None, ""):
                target[key] = values[key]
    for pool_id, pool in pools.items():
        if pool.get("gross_wpm") in (None, ""):
            audit["errors"].append(f"pool {pool_id} has no gross_nameplate_wpm")
    return {"capacity_pools": list(pools.values()), "node_allocations": node_allocations, "audit": audit}


def memory_model_payload_from_ledger(
    rows: Iterable[object],
    *,
    product_conversions: list[dict[str, Any]],
    allocations: list[dict[str, Any]],
    demand: list[dict[str, Any]] | None = None,
    scenario_id: str = "base",
    assumption_set_id: str | None = None,
) -> dict[str, Any]:
    adapted = capacity_pools_from_memory_ledger(rows)
    if adapted["audit"]["errors"]:
        raise ValueError("; ".join(adapted["audit"]["errors"]))
    return {
        "scenario_id": scenario_id,
        "assumption_set_id": assumption_set_id,
        "capacity_pools": adapted["capacity_pools"],
        "node_allocations": adapted["node_allocations"],
        "product_conversions": product_conversions,
        "allocations": allocations,
        "demand": demand or [],
        "adapter_audit": adapted["audit"],
    }
