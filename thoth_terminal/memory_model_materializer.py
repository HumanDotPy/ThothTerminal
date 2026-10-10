from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from thoth_terminal.dataset_service import publish_dataset_snapshot, upsert_family_dataset_materialization
from thoth_terminal.db import utcnow


def _reference_text(values: object) -> str | None:
    if not isinstance(values, list) or not values:
        return None
    return json.dumps(values, separators=(",", ":"), default=str)


def _scenario_results(result: dict[str, Any]) -> list[dict[str, Any]]:
    cases = result.get("cases")
    if isinstance(cases, dict):
        return [dict(value) for value in cases.values() if isinstance(value, dict)]
    return [result]


def memory_model_output_rows(
    result: dict[str, Any],
    *,
    source_dataset_ids: list[int] | None = None,
    source_document_ids: list[int] | None = None,
    bottleneck_model_id: int | None = None,
    research_subject_scenario_id: int | None = None,
) -> list[dict[str, Any]]:
    observed_at = utcnow().replace(microsecond=0).isoformat()
    rows: list[dict[str, Any]] = []
    dataset_refs = _reference_text(source_dataset_ids)
    document_refs = _reference_text(source_document_ids)

    def append_metric(
        scenario: dict[str, Any],
        *,
        row_key: str,
        engine: str,
        row_kind: str,
        metric: str,
        value: object,
        unit: str,
        pool: dict[str, Any] | None = None,
        product: object = None,
        active_constraint: object = None,
        notes: object = None,
        period: object = None,
    ) -> None:
        if value is None:
            return
        scenario_meta = scenario.get("scenario") if isinstance(scenario.get("scenario"), dict) else {}
        audit = scenario.get("audit") if isinstance(scenario.get("audit"), dict) else {}
        pool = pool or {}
        scenario_id = str(scenario_meta.get("scenario_id") or "base")
        rows.append(
            {
                "row_key": f"{scenario_id}:{row_key}:{metric}",
                "observed_at": observed_at,
                "model_version": scenario.get("model_version"),
                "scenario_id": scenario_id,
                "assumption_set_id": scenario_meta.get("assumption_set_id"),
                "period": period if period is not None else scenario_meta.get("period"),
                "engine": engine,
                "row_kind": row_kind,
                "pool_id": pool.get("pool_id"),
                "physical_pool_id": pool.get("physical_pool_id"),
                "parent_pool_id": pool.get("parent_pool_id"),
                "pool_kind": pool.get("pool_kind"),
                "supplier": pool.get("supplier"),
                "site": pool.get("site"),
                "fab": pool.get("fab"),
                "process_node": pool.get("process_node"),
                "product": product,
                "metric": metric,
                "value": value,
                "unit": unit,
                "active_constraint": active_constraint,
                "scenario_valid": bool(audit.get("scenario_valid")),
                "source_dataset_ids": dataset_refs,
                "source_document_ids": document_refs,
                "bottleneck_model_id": bottleneck_model_id,
                "research_subject_scenario_id": research_subject_scenario_id,
                "provenance_summary": "Model-derived Memory System output; observed evidence and assumptions remain in their source datasets.",
                "notes": notes,
            }
        )

    for scenario in _scenario_results(result):
        pools = {
            str(pool.get("pool_id")): pool
            for pool in scenario.get("capacity_pools", [])
            if isinstance(pool, dict)
        }
        for pool_id, pool in pools.items():
            states = pool.get("capacity_states_wpm") if isinstance(pool.get("capacity_states_wpm"), dict) else {}
            for state, value in states.items():
                append_metric(
                    scenario,
                    row_key=f"pool:{pool_id}:capacity:{state}",
                    engine="memory_supply",
                    row_kind="capacity_state",
                    metric=f"{state}_wpm",
                    value=value,
                    unit="wafers/month",
                    pool=pool,
                    period=pool.get("period"),
                )
            append_metric(
                scenario,
                row_key=f"pool:{pool_id}:allocation:unallocated",
                engine="closed_allocation",
                row_kind="allocation_closure",
                metric="unallocated_wpm",
                value=pool.get("unallocated_wpm"),
                unit="wafers/month",
                pool=pool,
                period=pool.get("period"),
            )
            if pool.get("pool_kind") == "physical_parent":
                for metric in ("node_allocated_wpm", "node_unallocated_wpm", "node_allocation_closure_ratio"):
                    append_metric(
                        scenario,
                        row_key=f"pool:{pool_id}:node-closure",
                        engine="node_allocation",
                        row_kind="node_allocation_closure",
                        metric=metric,
                        value=pool.get(metric),
                        unit="ratio" if metric.endswith("ratio") else "wafers/month",
                        pool=pool,
                        period=pool.get("period"),
                    )
        for allocation in scenario.get("allocations", []):
            if not isinstance(allocation, dict):
                continue
            pool_id = str(allocation.get("pool_id") or "unknown")
            product = allocation.get("product")
            key = f"allocation:{pool_id}:{product}"
            period = allocation.get("period")
            key = f"{key}:period:{period or 'unspecified'}"
            pool = pools.get(pool_id, {"pool_id": pool_id})
            for metric, unit in (
                ("allocated_wpm", "wafers/month"),
                ("qualified_shipped_stacks", "stacks/month"),
                ("qualified_shipped_bits", "bits/month"),
                ("qualified_shipped_gb", "GB/month"),
            ):
                append_metric(
                    scenario,
                    row_key=key,
                    engine="closed_allocation",
                    row_kind="product_allocation_output",
                    metric=metric,
                    value=allocation.get(metric),
                    unit=unit,
                    pool=pool,
                    product=product,
                    active_constraint=allocation.get("active_constraint"),
                    period=period,
                )
        for demand_index, demand_row in enumerate(scenario.get("demand", [])):
            if not isinstance(demand_row, dict):
                continue
            product = demand_row.get("product")
            platform = demand_row.get("system_or_platform") or "unspecified"
            for metric, unit in (
                ("system_units", "systems/month"),
                ("raw_system_units", "systems/period"),
                ("memory_gb_per_system", "GB/system"),
                ("demand_bits", "bits/month"),
                ("total_demand_bits", "bits/period"),
                ("period_months", "months"),
            ):
                append_metric(
                    scenario,
                    row_key=f"demand:{platform}:{product}:period:{demand_row.get('period') or 'unspecified'}:row:{demand_index}",
                    engine="architecture_demand",
                    row_kind="system_memory_demand",
                    metric=metric,
                    value=demand_row.get(metric),
                    unit=unit,
                    product=product,
                    notes=f"platform={platform}",
                    period=demand_row.get("period"),
                )
        for balance in scenario.get("product_balances", []):
            if not isinstance(balance, dict):
                continue
            product = balance.get("product")
            period = balance.get("period")
            for metric, unit in (
                ("supply_bits", "bits/month"),
                ("demand_bits", "bits/month"),
                ("surplus_deficit_bits", "bits/month"),
                ("demand_fulfillment", "ratio"),
            ):
                append_metric(
                    scenario,
                    row_key=f"balance:{product}:period:{period or 'unspecified'}",
                    engine="demand_allocation_balance",
                    row_kind="product_balance",
                    metric=metric,
                    value=balance.get(metric),
                    unit=unit,
                    product=product,
                    period=period,
                )
        for economics in scenario.get("economics", []):
            if not isinstance(economics, dict):
                continue
            product = economics.get("product")
            period = economics.get("period")
            pool = {
                "pool_id": economics.get("pool_id"),
                "supplier": economics.get("supplier"),
            }
            for metric, unit in (
                ("shipped_gb", "GB/month"),
                ("shipped_revenue", "currency/month"),
                ("notional_unmet_revenue", "currency/month"),
                ("cogs", "currency/month"),
                ("gross_profit", "currency/month"),
                ("gross_margin", "ratio"),
                ("ebitda", "currency/month"),
                ("free_cash_flow", "currency/month"),
                ("roic", "ratio"),
                ("capacity_clearing_asp_per_gb", "currency/GB"),
                ("incremental_value_per_wpm", "currency/WPM"),
            ):
                append_metric(
                    scenario,
                    row_key=f"economics:{economics.get('pool_id') or 'all'}:{product}:period:{period or 'unspecified'}",
                    engine="memory_economics",
                    row_kind="supplier_product_economics",
                    metric=metric,
                    value=economics.get(metric),
                    unit=unit,
                    pool=pool,
                    product=product,
                    period=period,
                )
    return rows


def materialize_memory_system_result(
    session: Session,
    result: dict[str, Any],
    *,
    slug: str = "memory-system-model-output",
    publish: bool = False,
    source_dataset_ids: list[int] | None = None,
    source_document_ids: list[int] | None = None,
    bottleneck_model_id: int | None = None,
    research_subject_scenario_id: int | None = None,
) -> dict[str, Any]:
    scenarios = _scenario_results(result)
    invalid = [row for row in scenarios if not bool((row.get("audit") or {}).get("scenario_valid"))]
    if invalid:
        raise ValueError("Invalid Memory System scenarios cannot be materialized.")
    rows = memory_model_output_rows(
        result,
        source_dataset_ids=source_dataset_ids,
        source_document_ids=source_document_ids,
        bottleneck_model_id=bottleneck_model_id,
        research_subject_scenario_id=research_subject_scenario_id,
    )
    materialized = upsert_family_dataset_materialization(
        session,
        family="memory_model_output",
        slug=slug,
        title="Memory System Model Outputs",
        description="Versioned model-derived capacity, good-bit, allocation, constraint, and demand-balance outputs.",
        category="memory_system",
        source_kind="derived_memory_system_model",
        source_event="memory_system_model_materialization",
        rows=rows,
        provenance={
            "model_version": result.get("model_version"),
            "source_dataset_ids": source_dataset_ids or [],
            "source_document_ids": source_document_ids or [],
        },
    )
    published_version_id = None
    if publish:
        published = publish_dataset_snapshot(session, materialized["dataset"], label=utcnow().date().isoformat())
        published_version_id = int(published.id)
    return {
        key: value
        for key, value in materialized.items()
        if key != "dataset"
    } | {"published_version_id": published_version_id}
