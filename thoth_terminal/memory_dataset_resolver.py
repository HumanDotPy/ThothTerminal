from __future__ import annotations

from copy import deepcopy
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from thoth_terminal.memory_ledger_adapter import capacity_pools_from_memory_ledger
from thoth_terminal.memory_system_model import memory_system_model
from thoth_terminal.models import Dataset, DatasetRow, DatasetVersion


MEMORY_INPUT_FAMILIES = (
    "memory_site_program_ramp",
    "hbm_stack_conversion",
    "hbm_supplier_qualification_allocation",
    "product_bom_content",
    "memory_supply_demand_window",
    "pricing_supply_demand",
)


def list_memory_system_inputs(session: Session, *, limit: int = 100) -> dict[str, Any]:
    """Return bounded dataset choices without loading their potentially large versions or rows."""
    query = (
        session.query(Dataset)
        .filter(Dataset.family.in_(MEMORY_INPUT_FAMILIES), Dataset.status == "active")
    )
    total_count = query.count()
    latest_version_id = (
        select(DatasetVersion.id)
        .where(DatasetVersion.dataset_id == Dataset.id, DatasetVersion.status == "published")
        .order_by(DatasetVersion.version.desc(), DatasetVersion.updated_at.desc(), DatasetVersion.id.desc())
        .limit(1)
        .correlate(Dataset)
        .scalar_subquery()
    )
    datasets = (
        query.with_entities(Dataset.id, Dataset.slug, Dataset.title, Dataset.family, latest_version_id)
        .order_by(Dataset.family.asc(), Dataset.title.asc(), Dataset.id.asc())
        .limit(max(1, min(limit, 100)))
        .all()
    )
    choices = [
        {
            "id": int(row.id),
            "slug": row.slug,
            "name": row.title,
            "family": row.family,
            "latest_published_version_id": row[4],
        }
        for row in datasets
    ]
    return {"datasets": choices, "families": list(MEMORY_INPUT_FAMILIES), "total_count": total_count, "truncated": total_count > len(choices)}


def _text(value: object) -> str:
    return str(value or "").strip()


def _value(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def _lineage(row: dict[str, Any]) -> str | None:
    explicit = _text(row.get("assumption_lineage") or row.get("lineage"))
    if explicit:
        return explicit
    role = _text(row.get("source_role")).lower()
    if role in {"company", "company_guidance", "issuer"}:
        return "company_guidance"
    if role in {"sell_side", "analyst", "research_firm"}:
        return "analyst_estimate"
    if role in {"operator", "house", "house_assumption"}:
        return "house_assumption"
    if role in {"official", "reported", "reported_actual"}:
        return "reported_actual"
    return None


def _with_lineage(target: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    target.update(
        {
            "lineage": _lineage(source),
            "source_document_id": source.get("source_document_id"),
            "source_owner": source.get("source_owner"),
            "estimate_owner": source.get("estimate_owner"),
            "confidence": source.get("confidence"),
        }
    )
    return target


def _period_matches(row: dict[str, Any], period: str | None) -> bool:
    if not period:
        return True
    row_period = _text(_value(row, "period", "period_date", "period_label", "ramp_period"))
    if not row_period:
        return True
    requested = period.lower()
    actual = row_period.lower()
    if requested == actual:
        return True
    # A year selection may include dated or quarterly observations. A more
    # specific selection must not silently include a year-wide observation.
    return len(requested) == 4 and requested.isdigit() and (
        actual.startswith(requested + "-") or actual.endswith(" " + requested)
    )


def _row_matches(row: dict[str, Any], *, period: str | None, scenario_id: str, assumption_set_id: str | None) -> bool:
    if not _period_matches(row, period):
        return False
    row_scenario = _text(row.get("scenario_id"))
    if row_scenario and row_scenario != scenario_id:
        return False
    row_assumptions = _text(row.get("assumption_set_id"))
    return not (row_assumptions and assumption_set_id and row_assumptions != assumption_set_id)


def _dataset_ids(params: dict[str, Any], family: str) -> list[int]:
    configured = params.get("dataset_ids")
    if not isinstance(configured, dict):
        return []
    raw = configured.get(family)
    values = raw if isinstance(raw, list) else [raw]
    return [int(value) for value in values if str(value or "").isdigit()]


def _select_datasets(session: Session, family: str, params: dict[str, Any], audit: dict[str, Any]) -> list[Dataset]:
    explicit_ids = _dataset_ids(params, family)
    if explicit_ids:
        rows = session.query(Dataset).filter(Dataset.id.in_(explicit_ids)).order_by(Dataset.id.asc()).all()
        found = {int(row.id) for row in rows}
        for missing in sorted(set(explicit_ids) - found):
            audit["errors"].append(f"dataset_id {missing} does not exist")
        for row in rows:
            if row.family != family:
                audit["errors"].append(f"dataset_id {row.id} is family {row.family}, expected {family}")
        return [row for row in rows if row.family == family]

    candidates = (
        session.query(Dataset)
        .filter(Dataset.family == family, Dataset.status == "active")
        .order_by(Dataset.updated_at.desc(), Dataset.id.desc())
        .all()
    )
    canonical = [
        row
        for row in candidates
        if bool((row.extra or {}).get("canonical")) or str(row.slug or "").startswith("canonical-")
    ]
    if len(canonical) == 1:
        return canonical
    if len(canonical) > 1:
        audit["errors"].append(f"multiple canonical {family} datasets exist: {[int(row.id) for row in canonical]}")
        return []
    if len(candidates) == 1:
        return candidates
    if len(candidates) > 1:
        audit["warnings"].append(
            f"{family} is ambiguous; pass dataset_ids.{family}. Candidates: {[int(row.id) for row in candidates]}"
        )
    return []


def _version_rows(session: Session, dataset: Dataset, *, allow_draft: bool, audit: dict[str, Any]) -> list[dict[str, Any]]:
    version = (
        session.query(DatasetVersion)
        .filter_by(dataset_id=int(dataset.id), status="published")
        .order_by(DatasetVersion.version.desc(), DatasetVersion.updated_at.desc(), DatasetVersion.id.desc())
        .first()
    )
    if version is None and allow_draft:
        version = (
            session.query(DatasetVersion)
            .filter_by(dataset_id=int(dataset.id), status="draft")
            .order_by(DatasetVersion.updated_at.desc(), DatasetVersion.id.desc())
            .first()
        )
    if version is None:
        audit["errors"].append(
            f"dataset {dataset.id} ({dataset.slug}) has no published version"
            + (" or draft" if allow_draft else "; set allow_draft=true to opt in")
        )
        return []
    rows = (
        session.query(DatasetRow)
        .filter_by(dataset_version_id=int(version.id))
        .order_by(DatasetRow.sort_order.asc(), DatasetRow.id.asc())
        .all()
    )
    audit["datasets"].append(
        {
            "dataset_id": int(dataset.id),
            "slug": dataset.slug,
            "family": dataset.family,
            "version_id": int(version.id),
            "version": int(version.version),
            "status": version.status,
            "row_count": len(rows),
        }
    )
    return [dict(row.values or {}) for row in rows]


def _conversion(row: dict[str, Any]) -> dict[str, Any] | None:
    pool_id = _text(_value(row, "node_pool_id", "pool_id"))
    product = _text(_value(row, "product", "memory_product", "hbm_generation"))
    dies = _value(row, "dies_per_wafer", "gross_dies_per_wafer")
    bits = _value(row, "usable_bits_per_die", "bits_per_core_die")
    if not pool_id or not product or dies in (None, "") or bits in (None, ""):
        return None
    return _with_lineage(
        {
            "pool_id": pool_id,
            "product": product,
            "memory_generation": _value(row, "memory_generation", "hbm_generation"),
            "period": _value(row, "period", "period_date", "period_label"),
            "dies_per_wafer": dies,
            "usable_bits_per_die": bits,
            "die_yield": _value(row, "die_yield", "known_good_die_yield"),
            "core_dies_per_stack": _value(row, "core_dies_per_stack", "core_die_count", "kgd_required_per_stack") or 1,
            "stack_yield": _value(row, "stack_yield", "bonding_or_tsv_yield") or 1,
            "package_test_yield": _value(row, "package_test_yield", "final_stack_yield") or 1,
            "base_die_capacity_stacks": row.get("base_die_capacity_stacks"),
            "stacking_capacity_stacks": row.get("stacking_capacity_stacks"),
            "package_test_capacity_stacks": row.get("package_test_capacity_stacks"),
            "customer_qualified_capacity_stacks": _value(
                row, "customer_qualified_capacity_stacks", "qualified_stack_output", "qualified_capacity_units"
            ),
        },
        row,
    )


def _allocation(row: dict[str, Any]) -> dict[str, Any] | None:
    pool_id = _text(_value(row, "node_pool_id", "pool_id"))
    product = _text(_value(row, "product", "memory_product", "hbm_generation"))
    allocated = _value(row, "allocated_wpm", "allocation_amount")
    share = _value(row, "allocation_share", "allocation_share_pct", "product_share_pct")
    unit = _text(row.get("allocation_unit")).lower()
    if allocated not in (None, "") and unit and "wpm" not in unit and "wafer" not in unit:
        allocated = None
    if not pool_id or not product or (allocated in (None, "") and share in (None, "")):
        return None
    return _with_lineage(
        {"pool_id": pool_id, "product": product, "period": _value(row, "period", "period_date", "period_label"), "allocated_wpm": allocated, "allocation_share": share}, row
    )


def _demand(row: dict[str, Any]) -> dict[str, Any] | None:
    product = _text(_value(row, "product", "memory_product", "component_family"))
    platform = _text(_value(row, "system_or_platform", "product_platform", "application_or_end_market"))
    system_units = _value(row, "system_units", "shipment_units", "unit_base")
    memory_gb = _value(row, "memory_gb_per_system")
    if memory_gb in (None, "") and "gb" in _text(row.get("unit")).lower():
        memory_gb = row.get("content_per_unit")
    demand_bits = row.get("demand_bits")
    if not product or (demand_bits in (None, "") and (system_units in (None, "") or memory_gb in (None, ""))):
        return None
    return _with_lineage(
        {
            "product": product,
            "system_or_platform": platform or None,
            "period": _value(row, "period", "period_date", "period_label"),
            "period_months": row.get("period_months"),
            "system_units": system_units,
            "memory_gb_per_system": memory_gb,
            "demand_bits": demand_bits,
        },
        row,
    )


def _economics(row: dict[str, Any]) -> dict[str, Any] | None:
    pool_id = _text(_value(row, "node_pool_id", "pool_id"))
    product = _text(_value(row, "product", "memory_product", "product_or_commodity"))
    asp = _value(row, "asp_per_gb", "asp", "price")
    if not pool_id or not product or asp in (None, ""):
        return None
    fields = (
        "wafer_cost", "stack_package_test_cost", "variable_cost_per_gb", "fixed_cost", "opex", "depreciation",
        "capex", "working_capital_change", "cash_taxes", "subsidy", "invested_capital", "target_gross_margin", "tax_rate",
    )
    return _with_lineage(
        {"pool_id": pool_id, "supplier": row.get("supplier"), "product": product, "period": _value(row, "period", "period_date", "period_label"), "asp_per_gb": asp, **{key: row.get(key) for key in fields}},
        row,
    )


def _apply_overrides(payload: dict[str, Any], overrides: object) -> None:
    if not isinstance(overrides, dict):
        return
    for key, value in overrides.items():
        if key in {"capacity_pools", "node_allocations", "product_conversions", "allocations", "demand", "economics"}:
            if isinstance(value, list):
                payload[key] = deepcopy(value)
        elif key in {"allow_unallocated_capacity", "allow_unallocated_node_capacity"}:
            payload[key] = bool(value)


def evaluate_memory_system_from_datasets(session: Session, params: dict[str, Any]) -> dict[str, Any]:
    scenario_id = _text(params.get("scenario_id")) or "base"
    assumption_set_id = _text(params.get("assumption_set_id")) or None
    period = _text(params.get("period")) or None
    allow_draft = bool(params.get("allow_draft", False))
    audit: dict[str, Any] = {"errors": [], "warnings": [], "datasets": [], "filtered_row_counts": {}}
    rows_by_family: dict[str, list[dict[str, Any]]] = {}
    for family in MEMORY_INPUT_FAMILIES:
        family_rows: list[dict[str, Any]] = []
        for dataset in _select_datasets(session, family, params, audit):
            family_rows.extend(_version_rows(session, dataset, allow_draft=allow_draft, audit=audit))
        filtered = [
            row
            for row in family_rows
            if _row_matches(row, period=period, scenario_id=scenario_id, assumption_set_id=assumption_set_id)
        ]
        rows_by_family[family] = filtered
        audit["filtered_row_counts"][family] = len(filtered)

    ledger_rows = rows_by_family["memory_site_program_ramp"]
    capacity_rows = [
        row
        for row in ledger_rows
        if any(
            row.get(key) not in (None, "")
            for key in (
                "gross_nameplate_wpm", "equipped_wpm", "eligible_wpm", "qualified_wpm", "utilized_wpm",
                "node_pool_id", "node_allocation_wpm", "node_allocation_share",
            )
        )
    ]
    adapted = capacity_pools_from_memory_ledger(capacity_rows)
    audit["errors"].extend(adapted["audit"]["errors"])
    audit["warnings"].extend(adapted["audit"]["warnings"])
    conversions = [row for row in (_conversion(item) for item in ledger_rows + rows_by_family["hbm_stack_conversion"]) if row]
    allocations = [
        row
        for row in (
            _allocation(item)
            for item in ledger_rows + rows_by_family["hbm_supplier_qualification_allocation"]
        )
        if row
    ]
    demand = [
        row
        for row in (
            _demand(item)
            for item in rows_by_family["product_bom_content"] + rows_by_family["memory_supply_demand_window"]
        )
        if row
    ]
    economics = [row for row in (_economics(item) for item in rows_by_family["pricing_supply_demand"]) if row]
    payload: dict[str, Any] = {
        "scenario_id": scenario_id,
        "assumption_set_id": assumption_set_id,
        "period": period,
        "capacity_pools": adapted["capacity_pools"],
        "node_allocations": adapted["node_allocations"],
        "product_conversions": conversions,
        "allocations": allocations,
        "demand": demand,
        "economics": economics,
    }
    _apply_overrides(payload, params.get("overrides"))
    if not payload["capacity_pools"]:
        audit["errors"].append("no canonical fab capacity rows resolved")
    if not payload["product_conversions"]:
        audit["errors"].append("no product conversion rows resolved")
    if not payload["allocations"]:
        audit["errors"].append("no product allocation rows resolved")
    result = memory_system_model(payload)
    if audit["errors"]:
        result["audit"]["errors"] = list(audit["errors"]) + list(result["audit"]["errors"])
        result["audit"]["scenario_valid"] = False
        result["audit"]["completeness"] = "invalid"
        result["scenario"]["status"] = "invalid"
    result["dataset_resolution"] = audit
    result["resolved_input"] = payload
    evidence = []
    for family, rows in rows_by_family.items():
        for row in rows:
            if len(evidence) >= 200:
                break
            evidence.append({**deepcopy(row), "family": family})
    result["source_evidence"] = evidence
    result["source_evidence_truncated"] = sum(len(rows) for rows in rows_by_family.values()) > len(evidence)
    return result
