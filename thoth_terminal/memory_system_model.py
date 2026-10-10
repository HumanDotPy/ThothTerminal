from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from math import isfinite
from typing import Any


MODEL_VERSION = "memory-system-v2-fab-node-product-allocation"
BITS_PER_GB = 8_000_000_000.0
ALLOWED_LINEAGES = {
    "reported_actual",
    "company_guidance",
    "analyst_estimate",
    "sell_side_aggregate",
    "house_assumption",
    "model_derived",
}


def _number(value: object, *, default: float | None = None) -> float | None:
    if value in (None, ""):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if isfinite(result) else default


def _non_negative(value: object, *, default: float | None = None) -> float | None:
    result = _number(value, default=default)
    if result is None:
        return None
    return result if result >= 0 else default


def _rate(value: object, *, default: float | None = None) -> float | None:
    result = _number(value, default=default)
    if result is None:
        return None
    if 1 < result <= 100:
        result /= 100.0
    return result if 0 <= result <= 1 else default


def _rows(payload: dict[str, object], key: str) -> list[dict[str, Any]]:
    value = payload.get(key)
    if not isinstance(value, list):
        return []
    return [dict(row) for row in value if isinstance(row, dict)]


def _text(value: object, *, default: str = "") -> str:
    result = str(value or "").strip()
    return result or default


def _lineage(row: dict[str, Any], audit: dict[str, Any], path: str) -> dict[str, Any]:
    lineage = _text(row.get("lineage"))
    if lineage not in ALLOWED_LINEAGES:
        audit["missing_evidence"].append(f"{path}.lineage")
    return {
        "lineage": lineage or None,
        "source_document_id": row.get("source_document_id"),
        "source_owner": _text(row.get("source_owner")) or None,
        "estimate_owner": _text(row.get("estimate_owner")) or None,
        "assumption_id": _text(row.get("assumption_id")) or None,
        "confidence": _number(row.get("confidence")),
    }


def _period_months(row: dict[str, Any], audit: dict[str, Any], path: str) -> float:
    raw = row.get("period_months")
    if raw in (None, ""):
        audit["defaults_used"][f"{path}.period_months"] = 1.0
        return 1.0
    months = _number(raw)
    if months is None or months <= 0:
        audit["errors"].append(f"{path}.period_months must be a positive finite number")
        return 1.0
    return months


def _stage_value(
    row: dict[str, Any],
    *,
    explicit_key: str,
    prior: float,
    fraction_key: str,
    default_fraction: float,
    audit: dict[str, Any],
    path: str,
) -> tuple[float, float, str]:
    explicit = _non_negative(row.get(explicit_key))
    if explicit is not None:
        if explicit > prior + 1e-9:
            audit["errors"].append(f"{path}.{explicit_key} exceeds its upstream capacity state")
        fraction = explicit / prior if prior > 0 else 0.0
        return explicit, fraction, "explicit"
    fraction = _rate(row.get(fraction_key))
    if fraction is None:
        fraction = default_fraction
        audit["defaults_used"][f"{path}.{fraction_key}"] = default_fraction
    audit["derived_fields"].append(f"{path}.{explicit_key}")
    return prior * fraction, fraction, "derived"


def _capacity_pools(payload: dict[str, object], audit: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    normalized: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(_rows(payload, "capacity_pools")):
        path = f"capacity_pools[{index}]"
        pool_id = _text(row.get("pool_id"), default=f"pool-{index + 1}")
        if pool_id in by_id:
            audit["errors"].append(f"duplicate capacity pool_id: {pool_id}")
            continue
        gross_wpm = _non_negative(row.get("gross_wpm"))
        if gross_wpm is None:
            audit["errors"].append(f"{path}.gross_wpm is required and must be non-negative")
            gross_wpm = 0.0
        equipped_wpm, equipped_fraction, equipped_basis = _stage_value(
            row,
            explicit_key="equipped_wpm",
            prior=gross_wpm,
            fraction_key="equipped_fraction",
            default_fraction=1.0,
            audit=audit,
            path=path,
        )
        eligible_wpm, eligible_fraction, eligible_basis = _stage_value(
            row,
            explicit_key="eligible_wpm",
            prior=equipped_wpm,
            fraction_key="eligible_fraction",
            default_fraction=1.0,
            audit=audit,
            path=path,
        )
        qualified_wpm, qualification_fraction, qualified_basis = _stage_value(
            row,
            explicit_key="qualified_wpm",
            prior=eligible_wpm,
            fraction_key="qualification_fraction",
            default_fraction=1.0,
            audit=audit,
            path=path,
        )
        utilization = _rate(row.get("utilization"), default=1.0)
        ramp_factor = _rate(row.get("ramp_factor"), default=1.0)
        if row.get("utilization") in (None, ""):
            audit["defaults_used"][f"{path}.utilization"] = 1.0
        if row.get("ramp_factor") in (None, ""):
            audit["defaults_used"][f"{path}.ramp_factor"] = 1.0
        explicit_utilized = _non_negative(row.get("utilized_wpm"))
        if explicit_utilized is not None:
            utilized_wpm = explicit_utilized
            utilized_basis = "explicit"
            if utilized_wpm > qualified_wpm + 1e-9:
                audit["errors"].append(f"{path}.utilized_wpm exceeds qualified_wpm")
        else:
            utilized_wpm = qualified_wpm * float(utilization or 0.0) * float(ramp_factor or 0.0)
            utilized_basis = "derived"
            audit["derived_fields"].append(f"{path}.utilized_wpm")
        result = {
            "pool_id": pool_id,
            "physical_pool_id": _text(row.get("physical_pool_id"), default=pool_id),
            "parent_pool_id": _text(row.get("parent_pool_id")) or None,
            "pool_kind": _text(row.get("pool_kind"), default="physical_or_flat"),
            "supplier": _text(row.get("supplier") or row.get("company")) or None,
            "site": _text(row.get("site")) or None,
            "fab": _text(row.get("fab") or row.get("facility")) or None,
            "phase": _text(row.get("phase")) or None,
            "process_node": _text(row.get("process_node") or row.get("technology")) or None,
            "period": _text(row.get("period") or row.get("period_label")) or None,
            "wafer_size_mm": _non_negative(row.get("wafer_size_mm"), default=300.0),
            "capacity_states_wpm": {
                "gross": gross_wpm,
                "equipped": equipped_wpm,
                "eligible": eligible_wpm,
                "qualified": qualified_wpm,
                "utilized": utilized_wpm,
            },
            "stage_factors": {
                "equipped_fraction": equipped_fraction,
                "eligible_fraction": eligible_fraction,
                "qualification_fraction": qualification_fraction,
                "utilization": utilization,
                "ramp_factor": ramp_factor,
            },
            "stage_basis": {
                "equipped": equipped_basis,
                "eligible": eligible_basis,
                "qualified": qualified_basis,
                "utilized": utilized_basis,
            },
            "lineage": _lineage(row, audit, path),
            "child_node_pool_ids": [],
        }
        normalized.append(result)
        by_id[pool_id] = result
    if not normalized:
        audit["errors"].append("capacity_pools must contain at least one row")
    return normalized, by_id


def _node_capacity_pools(
    payload: dict[str, object],
    audit: dict[str, Any],
    pools: list[dict[str, Any]],
    pools_by_id: dict[str, dict[str, Any]],
) -> None:
    allocated_by_parent: dict[str, float] = defaultdict(float)
    children_by_parent: dict[str, list[str]] = defaultdict(list)
    for index, row in enumerate(_rows(payload, "node_allocations")):
        path = f"node_allocations[{index}]"
        parent_id = _text(row.get("parent_pool_id") or row.get("physical_pool_id"))
        node_pool_id = _text(row.get("node_pool_id") or row.get("pool_id"))
        process_node = _text(row.get("process_node"))
        parent = pools_by_id.get(parent_id)
        if parent is None:
            audit["errors"].append(f"{path} references unknown parent pool_id {parent_id or '<missing>'}")
            continue
        if not node_pool_id or not process_node:
            audit["errors"].append(f"{path}.node_pool_id and process_node are required")
            continue
        if node_pool_id in pools_by_id:
            audit["errors"].append(f"duplicate node pool_id: {node_pool_id}")
            continue
        available_wpm = float(parent["capacity_states_wpm"]["utilized"])
        explicit_value = row.get("node_allocation_wpm")
        if explicit_value in (None, ""):
            explicit_value = row.get("allocated_wpm")
        share_value = row.get("node_allocation_share")
        if share_value in (None, ""):
            share_value = row.get("allocation_share")
        explicit_wpm = _non_negative(explicit_value)
        share = _rate(share_value)
        if explicit_wpm is not None and share is not None:
            audit["errors"].append(f"{path} must use node_allocation_wpm or node_allocation_share, not both")
        if explicit_wpm is None and share is None:
            audit["errors"].append(f"{path}.node_allocation_wpm or node_allocation_share is required")
            node_wpm = 0.0
        elif explicit_wpm is not None:
            node_wpm = explicit_wpm
            share = node_wpm / available_wpm if available_wpm > 0 else 0.0
        else:
            node_wpm = available_wpm * float(share or 0.0)
        allocated_by_parent[parent_id] += node_wpm
        children_by_parent[parent_id].append(node_pool_id)
        node_pool = {
            "pool_id": node_pool_id,
            "physical_pool_id": parent.get("physical_pool_id") or parent_id,
            "parent_pool_id": parent_id,
            "pool_kind": "node_slice",
            "supplier": _text(row.get("supplier")) or parent.get("supplier"),
            "site": _text(row.get("site")) or parent.get("site"),
            "fab": _text(row.get("fab")) or parent.get("fab"),
            "phase": _text(row.get("phase")) or parent.get("phase"),
            "process_node": process_node,
            "eligible_node_set": _text(row.get("eligible_node_set")) or None,
            "period": _text(row.get("period") or row.get("period_label")) or parent.get("period"),
            "wafer_size_mm": _non_negative(row.get("wafer_size_mm"), default=parent.get("wafer_size_mm")),
            "capacity_states_wpm": {
                "gross": node_wpm,
                "equipped": node_wpm,
                "eligible": node_wpm,
                "qualified": node_wpm,
                "utilized": node_wpm,
            },
            "stage_factors": {
                "node_allocation_share": share,
                "equipped_fraction": 1.0,
                "eligible_fraction": 1.0,
                "qualification_fraction": 1.0,
                "utilization": 1.0,
                "ramp_factor": 1.0,
            },
            "stage_basis": {
                "gross": "parent_utilized_allocation",
                "equipped": "inherited",
                "eligible": "node_allocation",
                "qualified": "node_allocation",
                "utilized": "node_allocation",
            },
            "node_eligibility_status": _text(row.get("node_eligibility_status")) or None,
            "node_conversion_start_period": _text(row.get("node_conversion_start_period")) or None,
            "node_conversion_completion_period": _text(row.get("node_conversion_completion_period")) or None,
            "lineage": _lineage(row, audit, path),
            "child_node_pool_ids": [],
        }
        pools.append(node_pool)
        pools_by_id[node_pool_id] = node_pool

    for parent_id, child_ids in children_by_parent.items():
        parent = pools_by_id[parent_id]
        available = float(parent["capacity_states_wpm"]["utilized"])
        allocated = allocated_by_parent[parent_id]
        parent["pool_kind"] = "physical_parent"
        parent["child_node_pool_ids"] = child_ids
        parent["node_allocated_wpm"] = allocated
        parent["node_unallocated_wpm"] = available - allocated
        parent["node_allocation_closure_ratio"] = allocated / available if available > 0 else None
        if allocated > available + 1e-6:
            audit["errors"].append(
                f"physical pool {parent_id} node allocation exceeds utilized capacity by {allocated - available:.6f} WPM"
            )
        elif not bool(payload.get("allow_unallocated_node_capacity")) and available - allocated > 1e-6:
            audit["errors"].append(
                f"physical pool {parent_id} node allocation leaves {available - allocated:.6f} WPM unassigned"
            )


def _conversions(payload: dict[str, object], audit: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    conversions: dict[tuple[str, str], dict[str, Any]] = {}
    for index, row in enumerate(_rows(payload, "product_conversions")):
        path = f"product_conversions[{index}]"
        pool_id = _text(row.get("pool_id"))
        product = _text(row.get("product") or row.get("memory_product"))
        if not pool_id or not product:
            audit["errors"].append(f"{path}.pool_id and product are required")
            continue
        key = (pool_id, product)
        if key in conversions:
            audit["errors"].append(f"duplicate product conversion: {pool_id}/{product}")
            continue
        dies_per_wafer = _non_negative(row.get("dies_per_wafer"))
        bits_per_die = _non_negative(row.get("usable_bits_per_die"))
        if dies_per_wafer is None or dies_per_wafer <= 0:
            audit["errors"].append(f"{path}.dies_per_wafer is required and must be positive")
            dies_per_wafer = 0.0
        if bits_per_die is None or bits_per_die <= 0:
            die_capacity_gb = _non_negative(row.get("die_capacity_gb"))
            bits_per_die = float(die_capacity_gb or 0.0) * BITS_PER_GB
        if bits_per_die <= 0:
            audit["errors"].append(f"{path}.usable_bits_per_die or die_capacity_gb is required")
        die_yield = _rate(row.get("die_yield"), default=1.0)
        raw_core_dies = row.get("core_dies_per_stack")
        core_dies = 1.0 if raw_core_dies in (None, "") else _number(raw_core_dies)
        if core_dies is None or core_dies <= 0:
            audit["errors"].append(f"{path}.core_dies_per_stack must be positive")
            core_dies = 0.0
        stack_yield = _rate(row.get("stack_yield"), default=1.0)
        package_test_yield = _rate(row.get("package_test_yield"), default=1.0)
        for field in ("die_yield", "stack_yield", "package_test_yield"):
            if row.get(field) in (None, ""):
                audit["defaults_used"][f"{path}.{field}"] = 1.0
        constraint_caps = {
            "base_die": _non_negative(row.get("base_die_capacity_stacks")),
            "stacking_bonding": _non_negative(row.get("stacking_capacity_stacks")),
            "package_test": _non_negative(row.get("package_test_capacity_stacks")),
            "customer_qualification": _non_negative(row.get("customer_qualified_capacity_stacks")),
        }
        if (product.upper().startswith("HBM") or core_dies > 1) and not any(value is not None for value in constraint_caps.values()):
            audit["warnings"].append(f"{path} has no downstream HBM capacity constraint; front-end converted output is unconstrained")
        conversions[key] = {
            "pool_id": pool_id,
            "product": product,
            "memory_generation": _text(row.get("memory_generation")) or None,
            "dies_per_wafer": dies_per_wafer,
            "usable_bits_per_die": bits_per_die,
            "die_yield": die_yield,
            "core_dies_per_stack": core_dies,
            "stack_yield": stack_yield,
            "package_test_yield": package_test_yield,
            "constraint_capacity_stacks": constraint_caps,
            "lineage": _lineage(row, audit, path),
        }
    if not conversions:
        audit["errors"].append("product_conversions must contain at least one row")
    return conversions


def _allocation_rows(
    payload: dict[str, object],
    audit: dict[str, Any],
    pools: dict[str, dict[str, Any]],
    conversions: dict[tuple[str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    allocations: list[dict[str, Any]] = []
    allocated_by_pool: dict[str, float] = defaultdict(float)
    seen: set[tuple[str, str]] = set()
    for index, row in enumerate(_rows(payload, "allocations")):
        path = f"allocations[{index}]"
        pool_id = _text(row.get("pool_id"))
        product = _text(row.get("product") or row.get("memory_product"))
        key = (pool_id, product)
        if key in seen:
            audit["errors"].append(f"duplicate allocation: {pool_id}/{product}")
            continue
        seen.add(key)
        pool = pools.get(pool_id)
        conversion = conversions.get(key)
        if pool is None:
            audit["errors"].append(f"{path} references unknown pool_id {pool_id or '<missing>'}")
            continue
        if conversion is None:
            audit["errors"].append(f"{path} has no matching product conversion for {pool_id}/{product or '<missing>'}")
            continue
        available_wpm = float(pool["capacity_states_wpm"]["utilized"])
        explicit_wpm = _non_negative(row.get("allocated_wpm"))
        share = _rate(row.get("allocation_share"))
        if explicit_wpm is not None and share is not None:
            audit["errors"].append(f"{path} must use allocated_wpm or allocation_share, not both")
        if explicit_wpm is None and share is None:
            audit["errors"].append(f"{path}.allocated_wpm or allocation_share is required")
            allocated_wpm = 0.0
        elif explicit_wpm is not None:
            allocated_wpm = explicit_wpm
            share = allocated_wpm / available_wpm if available_wpm > 0 else 0.0
        else:
            allocated_wpm = available_wpm * float(share or 0.0)
        allocated_by_pool[pool_id] += allocated_wpm

        gross_dies = allocated_wpm * float(conversion["dies_per_wafer"])
        good_dies = gross_dies * float(conversion["die_yield"] or 0.0)
        core_dies = float(conversion["core_dies_per_stack"])
        potential_stacks = good_dies / core_dies if core_dies > 0 else 0.0
        yielded_stacks = potential_stacks * float(conversion["stack_yield"] or 0.0) * float(conversion["package_test_yield"] or 0.0)
        caps = conversion["constraint_capacity_stacks"]
        stage_outputs = {"front_end_yielded": yielded_stacks}
        qualified_stacks = yielded_stacks
        active_constraint = "front_end_yielded"
        for stage in ("base_die", "stacking_bonding", "package_test", "customer_qualification"):
            cap = caps.get(stage)
            if cap is not None:
                stage_outputs[stage] = float(cap)
                if float(cap) < qualified_stacks:
                    qualified_stacks = float(cap)
                    active_constraint = stage
        stack_bits = float(conversion["usable_bits_per_die"]) * core_dies
        allocations.append(
            {
                "pool_id": pool_id,
                "supplier": pool.get("supplier"),
                "site": pool.get("site"),
                "process_node": pool.get("process_node"),
                "period": pool.get("period"),
                "product": product,
                "allocated_wpm": allocated_wpm,
                "allocation_share": share,
                "gross_dies": gross_dies,
                "good_memory_dies": good_dies,
                "potential_stacks": potential_stacks,
                "qualified_shipped_stacks": qualified_stacks,
                "qualified_shipped_bits": qualified_stacks * stack_bits,
                "qualified_shipped_gb": qualified_stacks * stack_bits / BITS_PER_GB,
                "active_constraint": active_constraint,
                "stage_output_stacks": stage_outputs,
                "conversion": conversion,
                "lineage": _lineage(row, audit, path),
            }
        )
    for pool_id, pool in pools.items():
        if pool.get("child_node_pool_ids"):
            continue
        available = float(pool["capacity_states_wpm"]["utilized"])
        allocated = allocated_by_pool.get(pool_id, 0.0)
        pool["allocated_wpm"] = allocated
        pool["unallocated_wpm"] = available - allocated
        pool["allocation_closure_ratio"] = allocated / available if available > 0 else None
        if allocated > available + 1e-6:
            audit["errors"].append(f"pool {pool_id} allocation exceeds utilized capacity by {allocated - available:.6f} WPM")
        elif not bool(payload.get("allow_unallocated_capacity")) and available - allocated > 1e-6:
            audit["errors"].append(f"pool {pool_id} allocation leaves {available - allocated:.6f} WPM unassigned")
    if not allocations:
        audit["errors"].append("allocations must contain at least one row")
    return allocations


def _demand_rows(payload: dict[str, object], audit: dict[str, Any], scenario_period: str | None) -> list[dict[str, Any]]:
    demand: list[dict[str, Any]] = []
    for index, row in enumerate(_rows(payload, "demand")):
        path = f"demand[{index}]"
        product = _text(row.get("product") or row.get("memory_product"))
        system_units = _non_negative(row.get("system_units"))
        memory_gb = _non_negative(row.get("memory_gb_per_system"))
        explicit_bits = _non_negative(row.get("demand_bits"))
        months = _period_months(row, audit, path)
        if not product:
            audit["errors"].append(f"{path}.product is required")
            continue
        if explicit_bits is None and (system_units is None or memory_gb is None):
            audit["errors"].append(f"{path} requires demand_bits or system_units and memory_gb_per_system")
            total_demand_bits = 0.0
            demand_bits = 0.0
        else:
            total_demand_bits = explicit_bits if explicit_bits is not None else float(system_units or 0.0) * float(memory_gb or 0.0) * BITS_PER_GB
            demand_bits = total_demand_bits / months
        demand.append(
            {
                "product": product,
                "system_or_platform": _text(row.get("system_or_platform") or row.get("platform")) or None,
                "period": _text(row.get("period") or row.get("period_label")) or scenario_period,
                "system_units": float(system_units) / months if system_units is not None else None,
                "raw_system_units": system_units,
                "memory_gb_per_system": memory_gb,
                "demand_bits": demand_bits,
                "total_demand_bits": total_demand_bits,
                "period_months": months,
                "demand_gb": demand_bits / BITS_PER_GB,
                "lineage": _lineage(row, audit, path),
            }
        )
    return demand


def _architecture_demand_rows(payload: dict[str, object], audit: dict[str, Any], scenario_period: str | None) -> list[dict[str, Any]]:
    shipments: dict[tuple[str, str], dict[str, Any]] = {}
    for index, row in enumerate(_rows(payload, "system_shipments")):
        path = f"system_shipments[{index}]"
        platform = _text(row.get("system_or_platform") or row.get("platform"))
        period = _text(row.get("period") or row.get("period_label")) or scenario_period or ""
        raw_units = row.get("system_units")
        if raw_units is None or raw_units == "":
            raw_units = row.get("shipment_units")
        units = _non_negative(raw_units)
        if not platform or units is None:
            audit["errors"].append(f"{path}.platform and system_units are required")
            continue
        months = _period_months(row, audit, path)
        key = (platform, period)
        if key in shipments:
            audit["errors"].append(f"duplicate system shipment row: {platform}/{period or '<unspecified>'}")
            continue
        shipments[key] = {
            "system_or_platform": platform,
            "period": period or None,
            "system_units": units / months,
            "raw_system_units": units,
            "period_months": months,
            "lineage": _lineage(row, audit, path),
        }

    demand: list[dict[str, Any]] = []
    for index, row in enumerate(_rows(payload, "memory_bom")):
        path = f"memory_bom[{index}]"
        platform = _text(row.get("system_or_platform") or row.get("platform"))
        period = _text(row.get("period") or row.get("period_label")) or scenario_period or ""
        product = _text(row.get("product") or row.get("memory_product"))
        if not platform or not product:
            audit["errors"].append(f"{path}.platform and product are required")
            continue
        shipment = shipments.get((platform, period))
        if shipment is None and not period:
            candidates = [value for (candidate_platform, _), value in shipments.items() if candidate_platform == platform]
            if len(candidates) == 1:
                shipment = candidates[0]
                period = str(shipment.get("period") or "")
            elif len(candidates) > 1:
                audit["errors"].append(f"{path} has ambiguous shipment period for {platform}; specify period")
                continue
        if shipment is None:
            shipment = shipments.get((platform, ""))
        if shipment is None:
            audit["errors"].append(f"{path} has no matching system shipment row for {platform}/{period or '<unspecified>'}")
            continue
        memory_gb = _non_negative(row.get("memory_gb_per_system"))
        if memory_gb is None:
            units_per_system = _non_negative(row.get("memory_units_per_system"))
            gb_per_unit = _non_negative(row.get("memory_gb_per_unit"))
            if units_per_system is not None and gb_per_unit is not None:
                memory_gb = units_per_system * gb_per_unit
                audit["derived_fields"].append(f"{path}.memory_gb_per_system")
        if memory_gb is None:
            audit["errors"].append(
                f"{path} requires memory_gb_per_system or memory_units_per_system and memory_gb_per_unit"
            )
            memory_gb = 0.0
        system_units = float(shipment["system_units"])
        raw_system_units = float(shipment["raw_system_units"])
        total_demand_bits = raw_system_units * memory_gb * BITS_PER_GB
        demand_bits = total_demand_bits / float(shipment["period_months"])
        demand.append(
            {
                "product": product,
                "system_or_platform": platform,
                "period": period or shipment.get("period") or scenario_period,
                "system_units": system_units,
                "raw_system_units": raw_system_units,
                "memory_gb_per_system": memory_gb,
                "demand_bits": demand_bits,
                "total_demand_bits": total_demand_bits,
                "period_months": shipment["period_months"],
                "demand_gb": demand_bits / BITS_PER_GB,
                "shipment_lineage": shipment["lineage"],
                "lineage": _lineage(row, audit, path),
            }
        )
    return demand


def _economics_rows(
    payload: dict[str, object],
    audit: dict[str, Any],
    allocations: list[dict[str, Any]],
    balances: list[dict[str, Any]],
    scenario_period: str | None,
) -> list[dict[str, Any]]:
    balance_by_product_period = {(str(row["product"]), row.get("period")): row for row in balances}
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None, str | None]] = set()
    for index, row in enumerate(_rows(payload, "economics")):
        path = f"economics[{index}]"
        pool_id = _text(row.get("pool_id"))
        product = _text(row.get("product") or row.get("memory_product"))
        matching = [
            allocation
            for allocation in allocations
            if (not pool_id or allocation["pool_id"] == pool_id) and (not product or allocation["product"] == product)
        ]
        period = _text(row.get("period") or row.get("period_label")) or scenario_period
        matching_periods = {allocation.get("period") for allocation in matching}
        if period is None:
            if len(matching_periods) > 1:
                audit["errors"].append(f"{path}.period is required because matching allocations span multiple periods")
                continue
            period = next(iter(matching_periods), None)
        matching = [allocation for allocation in matching if allocation.get("period") == period]
        identity = (pool_id or "", product or "", period)
        if identity in seen:
            audit["errors"].append(f"duplicate economics row: {pool_id or 'all'}/{product or '<missing>'}/{period or '<unspecified>'}")
            continue
        seen.add(identity)
        if not product or not matching:
            audit["errors"].append(f"{path}.product and period must match at least one allocation")
            continue
        asp_per_gb = _non_negative(row.get("asp_per_gb"))
        if asp_per_gb is None:
            audit["errors"].append(f"{path}.asp_per_gb is required")
            asp_per_gb = 0.0
        defaults = {
            "wafer_cost": 0.0,
            "stack_package_test_cost": 0.0,
            "variable_cost_per_gb": 0.0,
            "fixed_cost": 0.0,
            "opex": 0.0,
            "depreciation": 0.0,
            "capex": 0.0,
            "working_capital_change": 0.0,
            "cash_taxes": 0.0,
            "subsidy": 0.0,
            "invested_capital": 0.0,
        }
        values: dict[str, float] = {}
        for field, default in defaults.items():
            parsed = _number(row.get(field), default=default)
            values[field] = float(parsed if parsed is not None else default)
            if row.get(field) in (None, ""):
                audit["defaults_used"][f"{path}.{field}"] = default
        target_margin = _rate(row.get("target_gross_margin"), default=0.0) or 0.0
        tax_rate = _rate(row.get("tax_rate"), default=0.0) or 0.0
        if row.get("target_gross_margin") in (None, ""):
            audit["defaults_used"][f"{path}.target_gross_margin"] = 0.0
        if row.get("tax_rate") in (None, ""):
            audit["defaults_used"][f"{path}.tax_rate"] = 0.0

        shipped_gb = sum(float(item["qualified_shipped_gb"]) for item in matching)
        shipped_stacks = sum(float(item["qualified_shipped_stacks"]) for item in matching)
        allocated_wpm = sum(float(item["allocated_wpm"]) for item in matching)
        revenue = shipped_gb * float(asp_per_gb)
        variable_cost = (
            allocated_wpm * values["wafer_cost"]
            + shipped_stacks * values["stack_package_test_cost"]
            + shipped_gb * values["variable_cost_per_gb"]
        )
        cogs = variable_cost + values["fixed_cost"]
        gross_profit = revenue - cogs
        ebitda = gross_profit - values["opex"]
        ebit = ebitda - values["depreciation"]
        nopat = ebit * (1.0 - tax_rate)
        free_cash_flow = (
            ebitda
            - values["cash_taxes"]
            - values["capex"]
            - values["working_capital_change"]
            + values["subsidy"]
        )
        balance = balance_by_product_period.get((product, period), {})
        unmet_gb = max(0.0, -float(balance.get("surplus_deficit_gb") or 0.0))
        denominator = shipped_gb * (1.0 - target_margin)
        capacity_clearing_asp = cogs / denominator if denominator > 0 else None
        incremental_gb_per_wpm = shipped_gb / allocated_wpm if allocated_wpm > 0 else None
        incremental_wpm_value = (
            incremental_gb_per_wpm * (float(asp_per_gb) - values["variable_cost_per_gb"])
            - values["wafer_cost"]
            if incremental_gb_per_wpm is not None
            else None
        )
        output.append(
            {
                "pool_id": pool_id or None,
                "supplier": _text(row.get("supplier")) or matching[0].get("supplier"),
                "product": product,
                "period": period,
                "shipped_gb": shipped_gb,
                "shipped_revenue": revenue,
                "unmet_demand_gb": unmet_gb,
                "notional_unmet_revenue": unmet_gb * float(asp_per_gb),
                "cogs": cogs,
                "gross_profit": gross_profit,
                "gross_margin": gross_profit / revenue if revenue > 0 else None,
                "ebitda": ebitda,
                "depreciation": values["depreciation"],
                "ebit": ebit,
                "nopat": nopat,
                "capex": values["capex"],
                "free_cash_flow": free_cash_flow,
                "invested_capital": values["invested_capital"],
                "roic": nopat / values["invested_capital"] if values["invested_capital"] > 0 else None,
                "capacity_clearing_asp_per_gb": capacity_clearing_asp,
                "incremental_value_per_wpm": incremental_wpm_value,
                "lineage": _lineage(row, audit, path),
            }
        )
    return output


def evaluate_memory_system(payload: dict[str, object]) -> dict[str, object]:
    audit: dict[str, Any] = {
        "errors": [],
        "warnings": [],
        "defaults_used": {},
        "derived_fields": [],
        "missing_evidence": [],
    }
    scenario_period = _text(payload.get("period")) or None
    pools, pools_by_id = _capacity_pools(payload, audit)
    if scenario_period:
        for pool in pools:
            if not pool.get("period"):
                pool["period"] = scenario_period
    _node_capacity_pools(payload, audit, pools, pools_by_id)
    conversions = _conversions(payload, audit)
    for pool_id, _product in conversions:
        if pool_id not in pools_by_id:
            audit["errors"].append(f"product conversion references unknown pool_id {pool_id}")
    allocations = _allocation_rows(payload, audit, pools_by_id, conversions)
    demand = _demand_rows(payload, audit, scenario_period) + _architecture_demand_rows(payload, audit, scenario_period)

    supply_by_product_period: dict[tuple[str, str | None], float] = defaultdict(float)
    demand_by_product_period: dict[tuple[str, str | None], float] = defaultdict(float)
    for row in allocations:
        if not row.get("period") and scenario_period:
            row["period"] = scenario_period
        supply_by_product_period[(str(row["product"]), row.get("period"))] += float(row["qualified_shipped_bits"])
    for row in demand:
        demand_by_product_period[(str(row["product"]), row.get("period"))] += float(row["demand_bits"])
    known_periods = {period for _, period in (*supply_by_product_period.keys(), *demand_by_product_period.keys()) if period is not None}
    has_unscoped = any(period is None for _, period in (*supply_by_product_period.keys(), *demand_by_product_period.keys()))
    if has_unscoped:
        if len(known_periods) > 1:
            audit["warnings"].append("unscoped supply or demand is kept in a separate unspecified-period balance")
        else:
            audit["warnings"].append("supply or demand has no period; it is kept in an unspecified-period balance")
    product_periods = sorted(set(supply_by_product_period) | set(demand_by_product_period), key=lambda item: (item[0], item[1] or ""))
    balances = []
    for product, period in product_periods:
        supply_bits = supply_by_product_period.get((product, period), 0.0)
        demand_bits = demand_by_product_period.get((product, period), 0.0)
        balances.append(
            {
                "product": product,
                "period": period,
                "supply_bits": supply_bits,
                "supply_gb": supply_bits / BITS_PER_GB,
                "demand_bits": demand_bits,
                "demand_gb": demand_bits / BITS_PER_GB,
                "surplus_deficit_bits": supply_bits - demand_bits,
                "surplus_deficit_gb": (supply_bits - demand_bits) / BITS_PER_GB,
                "demand_fulfillment": supply_bits / demand_bits if demand_bits > 0 else None,
            }
        )
    economics = _economics_rows(payload, audit, allocations, balances, scenario_period)

    valid = not audit["errors"]
    if audit["errors"]:
        completeness = "invalid"
    elif audit["missing_evidence"] or audit["defaults_used"] or audit["warnings"]:
        completeness = "partial"
    else:
        completeness = "complete"
    audit["scenario_valid"] = valid
    audit["completeness"] = completeness
    return {
        "model_version": MODEL_VERSION,
        "scenario": {
            "scenario_id": _text(payload.get("scenario_id"), default="base"),
            "assumption_set_id": _text(payload.get("assumption_set_id")) or None,
            "period": _text(payload.get("period")) or None,
            "period_basis": _text(payload.get("period_basis")) or None,
            "status": "valid" if valid else "invalid",
        },
        "capacity_pools": pools,
        "product_conversions": list(conversions.values()),
        "allocations": allocations,
        "demand": demand,
        "product_balances": balances,
        "economics": economics,
        "audit": audit,
    }


def memory_system_model(payload: dict[str, object]) -> dict[str, object]:
    cases = payload.get("cases")
    if not isinstance(cases, dict):
        return evaluate_memory_system(payload)
    common = {key: deepcopy(value) for key, value in payload.items() if key != "cases"}
    results: dict[str, dict[str, object]] = {}
    for case_name, overrides in cases.items():
        case_payload = deepcopy(common)
        if isinstance(overrides, dict):
            case_payload.update(deepcopy(overrides))
        case_payload["scenario_id"] = str(case_name)
        results[str(case_name)] = evaluate_memory_system(case_payload)
    return {
        "model_version": MODEL_VERSION,
        "cases": results,
        "all_cases_valid": all(bool(result["audit"]["scenario_valid"]) for result in results.values()),
    }
