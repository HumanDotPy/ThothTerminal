"""Pure, auditable economics for measured AgentX operating points.

Prices are USD per million billed tokens; throughput is tokens/second/GPU.
Billable utilization is a fraction of available time, availability a fraction
of calendar time. Observed throughput already contains technical efficiency.
Neither GPU telemetry utilization nor a second efficiency haircut is applied.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from typing import Any


RECIPE_FIELDS = (
    "hardware", "framework", "model", "precision", "spec_method", "disagg",
    "is_multinode", "prefill_tp", "prefill_ep", "prefill_dp_attention",
    "prefill_num_workers", "decode_tp", "decode_ep", "decode_dp_attention",
    "decode_num_workers", "num_prefill_gpu", "num_decode_gpu", "offload_mode",
    "image", "recipe_fingerprint", "isl", "osl",
    "agentx_dataset", "dataset_variant", "dataset_slug", "dataset_name", "revision",
    "workload_trace", "trace_scope", "commit_sha", "runtime_settings",
    "speculative_decoding", "forced_acceptance", "forced_acceptance_rate",
    "kv_offloading", "kv_offload_backend", "kv_offload_backend_version", "router_name", "router_version",
)
LINEAGE_FIELDS = (
    "benchmark_result_id", "workflow_run_id", "github_run_id", "run_url",
    "config_id", "date", "source_dataset_slug", "source_dataset_version_id",
    "source_row_id", "source_row_key",
    "source_dataset_row_id", "upstream_dataset_version_id", "source_model", "display_model",
    "source_url", "run_started_at", "curve_date", "curve_workflow_run_id", "curve_run_started_at",
)


def _number(value: Any, name: str, *, maximum: float | None = None,
            positive: bool = False) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a finite number") from None
    if not math.isfinite(result) or result < 0 or (positive and result <= 0):
        raise ValueError(f"{name} must be finite and {'positive' if positive else 'nonnegative'}")
    if maximum is not None and result > maximum:
        raise ValueError(f"{name} must be <= {maximum}")
    return result


def _get(data: dict, key: str, *, fraction: bool = False, positive: bool = False) -> float | None:
    return _number(data.get(key), key, maximum=1 if fraction else None, positive=positive)


def _metric(row: dict, key: str) -> float | None:
    value = row.get(key)
    if value is None:
        value = (row.get("metrics") or {}).get(key)
    return _number(value, key)


def _lineage(row: dict) -> dict:
    return {key: row[key] for key in LINEAGE_FIELDS if key in row}


def _tco(scenario: dict, utilization: float | None, availability: float | None) -> dict:
    """All component costs are calendar USD/GPU/hour, except active power.

Component mode requires explicit zero for absent cost categories. Capex scope
is either GPU (plus separately supplied node/network per GPU), or node (node
includes GPUs); facilities capex is separately specified. All-in scope means
power, financing, colocation, and operations are included and never re-added.
"""
    data = scenario.get("tco") or {}
    if not isinstance(data, dict):
        raise ValueError("tco must be an object")
    mode = data.get("mode")
    if mode is not None and mode not in {"owned", "rented"}:
        raise ValueError("tco.mode must be owned or rented")
    gaps: list[str] = []
    components: dict[str, float | None] = {}
    all_in = _get(data, "all_in_usd_per_gpu_hour")
    if data.get("scope") == "all_in" and all_in is None:
        return {"mode": mode, "scope": "all_in", "usd_per_gpu_hour": None, "components": {},
                "basis": data.get("basis"), "source_url": data.get("source_url"),
                "gaps": ["tco.all_in_usd_per_gpu_hour"] + (["tco.mode"] if mode is None else [])}
    if all_in is not None:
        if data.get("scope") != "all_in":
            raise ValueError("all-in GPU hourly cost requires tco.scope='all_in'")
        conflicting = set(data) - {"mode", "scope", "all_in_usd_per_gpu_hour", "basis", "source_url", "provenance", "observed_at", "retrieved_at"}
        if conflicting:
            raise ValueError("all-in TCO cannot also contain component costs: " + ", ".join(sorted(conflicting)))
        if mode is None:
            gaps.append("tco.mode")
        if not str(data.get("basis") or "").strip():
            gaps.append("tco.basis")
        return {"mode": mode, "scope": "all_in", "usd_per_gpu_hour": all_in if mode else None,
                "components": {"all_in": all_in}, "gaps": gaps, "basis": data.get("basis"), "source_url": data.get("source_url")}
    if mode is None:
        return {"mode": None, "scope": None, "usd_per_gpu_hour": None,
                "components": {}, "gaps": ["tco.mode", "tco.all_in_or_components"]}
    if data.get("scope") != "components":
        raise ValueError("component TCO requires tco.scope='components'")
    required = ["colocation_usd_per_gpu_hour", "operations_usd_per_gpu_hour",
                "other_usd_per_gpu_hour", "electricity_usd_per_kwh",
                "active_utility_power_w_per_gpu", "idle_utility_power_w_per_gpu"]
    if mode == "rented":
        required.append("rental_usd_per_gpu_hour")
        if any(k in data for k in ("gpu_capex_usd", "node_capex_usd", "network_capex_usd", "capital_rate")):
            raise ValueError("rented component TCO cannot include owned capital costs")
    else:
        required += ["depreciation_years", "capital_rate", "network_capex_usd",
                     "facilities_capex_usd", "gpus_per_node"]
        scope = data.get("capex_scope")
        if scope not in {"gpu", "node"}:
            gaps.append("tco.capex_scope")
        required += ["node_capex_usd"]
        if scope == "gpu":
            required.append("gpu_capex_usd")
        elif data.get("gpu_capex_usd") is not None:
            raise ValueError("node capex includes GPUs; separate GPU capex would double count")
    values = {key: _get(data, key, fraction=key == "capital_rate", positive=key in {"depreciation_years", "gpus_per_node"}) for key in required}
    gaps += ["tco." + key for key, value in values.items() if value is None]
    for key in ("colocation_usd_per_gpu_hour", "operations_usd_per_gpu_hour", "other_usd_per_gpu_hour", "rental_usd_per_gpu_hour"):
        if key in values:
            components[key] = values[key]
    active = utilization * availability if utilization is not None and availability is not None else None
    if active is None:
        gaps.append("tco.power_duty_cycle")
    if active is not None and all(values.get(k) is not None for k in required[:6]):
        watts = values["active_utility_power_w_per_gpu"] * active + values["idle_utility_power_w_per_gpu"] * (1 - active)
        components["power"] = watts / 1000 * values["electricity_usd_per_kwh"]
    else:
        components["power"] = None
    if mode == "owned":
        capital_keys = ["node_capex_usd", "network_capex_usd", "facilities_capex_usd", "gpus_per_node", "depreciation_years", "capital_rate"]
        if data.get("capex_scope") == "gpu":
            capital_keys.append("gpu_capex_usd")
        if all(values.get(k) is not None for k in capital_keys):
            capex = (values["node_capex_usd"] + values["network_capex_usd"] + values["facilities_capex_usd"]) / values["gpus_per_node"]
            if data.get("capex_scope") == "gpu":
                capex += values["gpu_capex_usd"]
            components["depreciation"] = capex / (values["depreciation_years"] * 8760)
            components["capital_cost"] = capex * values["capital_rate"] / 8760
        else:
            components.update(depreciation=None, capital_cost=None)
    hourly = sum(components.values()) if not gaps else None
    if not str(data.get("basis") or "").strip():
        gaps.append("tco.basis")
    return {"mode": mode, "scope": "components", "usd_per_gpu_hour": hourly,
            "components": components, "gaps": gaps, "basis": data.get("basis")}


def _economics(row: dict, scenario: dict) -> dict:
    gaps: list[str] = []
    provenance: dict[str, Any] = {}
    overrides = scenario.get("overrides") or {}
    if not isinstance(overrides, dict):
        raise ValueError("overrides must be an object")
    if "technical_efficiency" in scenario or "serving_efficiency" in scenario:
        raise ValueError("observed rate already includes technical serving efficiency")
    if overrides and not str(scenario.get("override_basis") or "").strip():
        raise ValueError("scenario overrides require override_basis")
    unknown = set(overrides) - {"input_token_share", "cache_hit_rate", "total_tok_s_per_gpu"}
    if unknown:
        raise ValueError("unsupported overrides (observed rate already includes technical efficiency): " + ", ".join(sorted(unknown)))
    total = _metric(row, "tput_per_gpu")
    measured_input = _metric(row, "input_tput_per_gpu")
    measured_output = _metric(row, "output_tput_per_gpu")
    measured_cache = _metric(row, "cached_input_tput_per_gpu")
    prompt_count = _metric(row, "total_prompt_tokens")
    generation_count = _metric(row, "total_generation_tokens")
    cached_count = _metric(row, "total_cached_prompt_tokens")
    uncached_count = _metric(row, "total_uncached_prompt_tokens")
    if cached_count is not None and prompt_count is not None and cached_count > prompt_count:
        raise ValueError("observed cached prompt count exceeds total logical input")
    if None not in (cached_count, uncached_count, prompt_count) and not math.isclose(cached_count + uncached_count, prompt_count, rel_tol=.01, abs_tol=1e-6):
        raise ValueError("observed cached/uncached prompt counts must sum to total input")
    if measured_input is not None and measured_output is not None:
        split_total = measured_input + measured_output
        if total is None:
            total = split_total
        elif not math.isclose(total, split_total, rel_tol=0.01, abs_tol=1e-6):
            raise ValueError("observed input/output rates must sum to total throughput (within 1%)")
    if "total_tok_s_per_gpu" in overrides:
        total = _get(overrides, "total_tok_s_per_gpu")
        provenance["total_throughput"] = {"kind": "scenario_override", "basis": scenario["override_basis"]}
    else:
        provenance["total_throughput"] = {"kind": "measured", "lineage": _lineage(row)}
    share = _get(overrides, "input_token_share", fraction=True) if "input_token_share" in overrides else None
    if share is not None:
        provenance["token_mix"] = {"kind": "scenario_override", "basis": scenario["override_basis"]}
    elif measured_input is not None and measured_output is not None and measured_input + measured_output > 0:
        share = measured_input / (measured_input + measured_output)
        provenance["token_mix"] = {"kind": "measured_input_output_rates"}
    elif None not in (prompt_count, generation_count) and prompt_count + generation_count > 0:
        share = prompt_count / (prompt_count + generation_count)
        provenance["token_mix"] = {"kind": "measured_logical_token_counts"}
    else:
        gaps.append("input_output_token_mix")
    cache = _get(overrides, "cache_hit_rate", fraction=True) if "cache_hit_rate" in overrides else None
    if cache is not None:
        provenance["cache_fraction_of_input"] = {"kind": "scenario_override", "basis": scenario["override_basis"]}
    elif measured_cache is not None and measured_input is not None and measured_input > 0:
        if measured_cache > measured_input:
            raise ValueError("cached input rate exceeds total input rate")
        cache = measured_cache / measured_input
        provenance["cache_fraction_of_input"] = {"kind": "measured_cached_input_rate"}
    elif cached_count is not None and prompt_count is not None and prompt_count > 0:
        cache = cached_count / prompt_count
        provenance["cache_fraction_of_input"] = {"kind": "measured_cached_logical_input_counts"}
    elif share == 0:
        cache = 0.0
    else:
        gaps.append("cache_fraction_of_input")
    utilization = _get(scenario, "billable_utilization", fraction=True)
    availability = _get(scenario, "availability", fraction=True)
    provenance["calendar_billable_fraction"] = {"kind": "scenario_assumption", "billable_utilization": utilization,
                                                "availability": availability, "basis": scenario.get("utilization_basis")}
    royalty = _get(scenario, "royalty_revenue_share", fraction=True)
    fixed_royalty = _get(scenario, "royalty_fixed_usd_per_gpu_hour")
    if (royalty is not None or fixed_royalty is not None) and not str(scenario.get("royalty_basis") or "").strip():
        gaps.append("royalty_basis")
    provenance["royalty"] = {"kind": "scenario_assumption", "basis": scenario.get("royalty_basis"), "source_url": scenario.get("royalty_source_url")}
    provenance["prices"] = {"kind": "api_equivalent_or_scenario", "basis": (scenario.get("prices") or {}).get("basis"), "source_url": (scenario.get("prices") or {}).get("source_url")}
    hours = _get(scenario, "annual_hours", positive=True)
    if hours is not None and hours > 8784:
        raise ValueError("annual_hours must be <= 8784")
    power = _get(scenario, "utility_power_w_per_gpu", positive=True)
    prices = scenario.get("prices") or {}
    if not isinstance(prices, dict):
        raise ValueError("prices must be an object")
    rates = {key: _get(prices, key) for key in ("input_usd_per_mtok", "cached_input_usd_per_mtok", "output_usd_per_mtok", "realization_multiplier")}
    for key, value in (("total_throughput", total), ("billable_utilization", utilization), ("availability", availability), ("royalty_revenue_share", royalty), ("royalty_fixed_usd_per_gpu_hour", fixed_royalty)):
        if value is None:
            gaps.append(key)
    effective_input = None
    if share == 0:
        effective_input = 0.0
    elif cache is not None:
        needed = [("input_usd_per_mtok", 1 - cache), ("cached_input_usd_per_mtok", cache)]
        missing = [key for key, weight in needed if weight > 0 and rates[key] is None]
        gaps += ["prices." + key for key in missing]
        if not missing:
            effective_input = sum((rates[key] or 0) * weight for key, weight in needed)
    if share is not None and share < 1 and rates["output_usd_per_mtok"] is None:
        gaps.append("prices.output_usd_per_mtok")
    if rates["realization_multiplier"] is None:
        gaps.append("prices.realization_multiplier")
    blended = None
    if share is not None and effective_input is not None and rates["realization_multiplier"] is not None and (share == 1 or rates["output_usd_per_mtok"] is not None):
        blended = (share * effective_input + (1 - share) * (rates["output_usd_per_mtok"] or 0)) * rates["realization_multiplier"]
    tokens_day = total * 86400 * utilization * availability if None not in (total, utilization, availability) else None
    revenue_day = tokens_day / 1e6 * blended if None not in (tokens_day, blended) else None
    tco = _tco(scenario, utilization, availability)
    gaps += tco["gaps"]
    cost_day = tco["usd_per_gpu_hour"] * 24 if tco["usd_per_gpu_hour"] is not None else None
    royalty_day = revenue_day * royalty + fixed_royalty * 24 if None not in (revenue_day, royalty, fixed_royalty) else None
    profit_day = revenue_day - cost_day - royalty_day if None not in (revenue_day, cost_day, royalty_day) else None
    cost_token = (cost_day + royalty_day) / tokens_day * 1e6 if tokens_day is not None and tokens_day > 0 and None not in (cost_day, royalty_day) else None
    margin = profit_day / revenue_day if revenue_day is not None and revenue_day > 0 and profit_day is not None else None
    # Solve the affine component power model at u=0 and u=1, keeping the
    # observed operating point fixed. Values >1 are infeasible, not clamped.
    break_even = None
    if None not in (total, blended, availability, royalty, fixed_royalty):
        zero = _tco(scenario, 0, availability)["usd_per_gpu_hour"]
        full = _tco(scenario, 1, availability)["usd_per_gpu_hour"]
        if zero is not None and full is not None:
            contribution = total * 3600 * availability / 1e6 * blended * (1 - royalty) - (full - zero)
            if contribution > 0:
                break_even = (zero + fixed_royalty) / contribution
    gw_gpus = 1e9 / power if power is not None else None
    gw_revenue = revenue_day / 24 * hours * gw_gpus if None not in (revenue_day, hours, gw_gpus) else None
    gw_profit = profit_day / 24 * hours * gw_gpus if None not in (profit_day, hours, gw_gpus) else None
    if power is None:
        gaps.append("utility_power_w_per_gpu (all-in facility allocation, not GPU telemetry)")
    if hours is None:
        gaps.append("annual_hours")
    metrics = {"total_tok_s_per_gpu": total, "input_token_share": share, "cache_hit_rate": cache,
               "effective_input_usd_per_mtok": effective_input, "blended_revenue_usd_per_mtok": blended,
               "tokens_per_day_per_gpu": tokens_day, "revenue_usd_per_day_per_gpu": revenue_day,
               "tco_usd_per_day_per_gpu": cost_day, "royalty_usd_per_day_per_gpu": royalty_day,
               "profit_usd_per_day_per_gpu": profit_day, "profit_margin": margin,
               "cost_usd_per_mtok": cost_token, "break_even_billable_utilization": break_even,
               "break_even_feasible": break_even <= 1 if break_even is not None else None,
               "gpus_per_gw": gw_gpus, "revenue_usd_per_gw_year": gw_revenue,
               "profit_usd_per_gw_year": gw_profit}
    for key, value in metrics.items():
        if isinstance(value, (int, float)) and not math.isfinite(value):
            raise ValueError(f"inputs overflow finite economics output {key}")
    return {"metrics": metrics, "tco": tco, "gaps": list(dict.fromkeys(gaps)), "provenance": provenance}


def evaluate_agentx_economics(benchmark_rows: list[dict], scenario: dict) -> dict:
    """Select measured SLA-compliant points per identical serving recipe.

    Scenario requires an explicit P90 interactivity floor. Optional TTFT ceiling
    uses p90_ttft_ms by default. Legacy raw TTFT requires ttft_unit explicitly;
    AgentX API raw TTFT is seconds. Mix/cache overrides require basis; observed
    throughput cannot be overridden in this measured evaluator. Missing
    economics inputs return nulls and gaps; invalid supplied inputs raise.
    Sensitivity accepts at most 25 explicit {billable_utilization,
    realization_multiplier} cases, fixes the measured operating point and
    recomputes power/TCO. It does not predict a different serving configuration.
    """
    if not isinstance(benchmark_rows, list) or not isinstance(scenario, dict):
        raise ValueError("benchmark_rows must be a list and scenario must be an object")
    if "total_tok_s_per_gpu" in (scenario.get("overrides") or {}):
        raise ValueError("measured economics forbids throughput overrides; use capacity-bound economics")
    floor = _get(scenario, "min_p90_interactivity_tok_s_user")
    ceiling = _get(scenario, "max_ttft_ms")
    ttft_metric = scenario.get("ttft_metric", "p90_ttft_ms")
    if ttft_metric not in {"p90_ttft_ms", "p90_ttft", "p99_ttft", "median_ttft"}:
        raise ValueError("unsupported ttft_metric")
    if ceiling is not None and not ttft_metric.endswith("_ms") and scenario.get("ttft_unit") not in {"seconds", "milliseconds"}:
        raise ValueError("raw TTFT requires explicit ttft_unit")
    ttft_scale = 1000 if not ttft_metric.endswith("_ms") and scenario.get("ttft_unit") == "seconds" else 1
    # Validate scenario even when no benchmark qualifies.
    _economics({}, scenario)
    sensitivities = scenario.get("sensitivity") or []
    if not isinstance(sensitivities, list) or len(sensitivities) > 25:
        raise ValueError("sensitivity must contain at most 25 cases")
    for case in sensitivities:
        if not isinstance(case, dict) or set(case) - {"billable_utilization", "realization_multiplier", "label"}:
            raise ValueError("sensitivity supports billable_utilization and realization_multiplier only")
        _get(case, "billable_utilization", fraction=True)
        _get(case, "realization_multiplier")
    groups: dict[str, dict] = {}
    excluded: list[dict] = []
    for source in benchmark_rows:
        if not isinstance(source, dict):
            raise ValueError("benchmark rows must be objects")
        row = dict(source.get("values") or source)
        reason = None
        if row.get("benchmark_type") not in {"agentic_traces", "agentx", "AgentX"}:
            reason = "not_agentx"
        elif row.get("benchmark_ok") is False or row.get("error"):
            reason = "benchmark_not_successful"
        elif row.get("benchmark_ok") is not True and not (row.get("measurement_valid") is True and row.get("measurement_source") == "public_benchmark_api"):
            reason = "unverified_measurement"
        if reason:
            excluded.append({"lineage": _lineage(row), "reason": reason})
            continue
        # Validate observed numeric fields before selection.
        for key in ("tput_per_gpu", "input_tput_per_gpu", "output_tput_per_gpu", "cached_input_tput_per_gpu", "p90_intvty", "p90_ttft_ms", "p90_ttft", "p99_ttft", "median_ttft"):
            _metric(row, key)
        recipe = {key: row.get(key) for key in RECIPE_FIELDS}
        # An incomplete identity may be evaluated alone but must never imply
        # equivalence with another unknown recipe.
        if not row.get("recipe_fingerprint") and not row.get("config_id"):
            recipe["unidentified_row"] = _lineage(row) or {"input_index": len(excluded) + sum(len(g["rows"]) for g in groups.values())}
        elif not row.get("recipe_fingerprint"):
            recipe["config_id"] = row["config_id"]
        key = hashlib.sha256(json.dumps(recipe, sort_keys=True, default=str).encode()).hexdigest()
        group = groups.setdefault(key, {"recipe": recipe, "rows": [], "eligible": []})
        group["rows"].append(row)
        p90 = _metric(row, "p90_intvty")
        ttft = _metric(row, ttft_metric)
        if ttft is not None and not ttft_metric.endswith("_ms"):
            ttft *= ttft_scale
        total = _metric(row, "tput_per_gpu")
        if total is None:
            inp, out = _metric(row, "input_tput_per_gpu"), _metric(row, "output_tput_per_gpu")
            total = inp + out if None not in (inp, out) else None
        if floor is None:
            reason = "missing_p90_interactivity_sla"
        elif p90 is None:
            reason = "missing_measured_p90_interactivity"
        elif p90 < floor:
            reason = "p90_interactivity_below_sla"
        elif ceiling is not None and (ttft is None or ttft > ceiling):
            reason = "missing_ttft" if ttft is None else "ttft_above_sla"
        elif total is None:
            reason = "missing_measured_throughput"
        if reason:
            excluded.append({"lineage": _lineage(row), "reason": reason, "recipe_id": key})
        else:
            group["eligible"].append((total, row))
    results = []
    for key, group in groups.items():
        lineage = [_lineage(r) for r in group["rows"]]
        if not group["eligible"]:
            results.append({"recipe_id": key, "recipe": group["recipe"], "lineage": lineage,
                            "selected_operating_point": None, "metrics": {}, "status": "no_operating_point",
                            "gaps": ["no_measured_operating_point_meets_sla"], "sensitivity": []})
            continue
        _, selected = max(group["eligible"], key=lambda pair: pair[0])
        result = _economics(selected, scenario)
        result.update(recipe_id=key, recipe=group["recipe"], lineage=lineage,
                      selected_operating_point={**_lineage(selected), "concurrency": selected.get("concurrency"),
                                                "p90_intvty": _metric(selected, "p90_intvty"),
                                                "ttft_metric": ttft_metric, "ttft_ms": (_metric(selected, ttft_metric) * ttft_scale if _metric(selected, ttft_metric) is not None else None),
                                                "benchmark_success_verdict": "successful" if selected.get("benchmark_ok") is True else "unknown_verified_measurement"},
                      status="partial" if result["gaps"] else "complete", sensitivity=[])
        for case in sensitivities:
            alternate = copy.deepcopy(scenario)
            if "billable_utilization" in case:
                alternate["billable_utilization"] = case["billable_utilization"]
            if "realization_multiplier" in case:
                alternate.setdefault("prices", {})["realization_multiplier"] = case["realization_multiplier"]
            evaluated = _economics(selected, alternate)
            result["sensitivity"].append({"case": case, "metrics": evaluated["metrics"], "gaps": evaluated["gaps"],
                                          "basis": "scenario sensitivity at fixed measured operating point"})
        results.append(result)
    top_gaps = ["min_p90_interactivity_tok_s_user"] if floor is None else []
    status = "no_operating_point" if not any(r["selected_operating_point"] for r in results) else "partial" if any(r["status"] != "complete" for r in results) else "complete"
    return {"schema_version": 1, "scenario_id": scenario.get("scenario_id"), "status": status,
            "results": results, "excluded_rows": excluded, "gaps": top_gaps,
            "coverage": {"input_rows": len(benchmark_rows), "recipe_groups": len(groups),
                         "selected_points": sum(r["selected_operating_point"] is not None for r in results),
                         "truncated": False},
            "method": "measured AgentX points only; no interpolation or extrapolation; API prices are equivalents, not observed contracts"}


def evaluate_agentx_capacity_economics(total_tok_s_per_gpu: float, scenario: dict, *,
                                      basis: str = "theoretical_resource_upper_bound") -> dict:
    """Economics at an explicit resource capacity bound, never a forecast.

    This intentionally bypasses measurement/SLA selection. Token mix and cache
    share must be scenario overrides; absent assumptions produce nullable costs
    and revenue with gaps just as measured evaluations do.
    """
    total = _number(total_tok_s_per_gpu, "total_tok_s_per_gpu")
    if not isinstance(scenario, dict) or not str(basis).strip():
        raise ValueError("capacity evaluation requires scenario object and basis")
    bound_scenario = copy.deepcopy(scenario)
    bound_scenario.setdefault("overrides", {})["total_tok_s_per_gpu"] = total
    original_basis = bound_scenario.get("override_basis")
    bound_scenario["override_basis"] = original_basis or basis
    result = _economics({}, bound_scenario)
    result.update(kind="capacity_bound", basis=basis, status="partial" if result["gaps"] else "complete",
                  scenario_id=scenario.get("scenario_id"),
                  method="conditional economics at theoretical resource upper bound; no measured SLA or profit forecast", sensitivity=[])
    result["provenance"]["total_throughput"] = {"kind": "capacity_bound", "basis": basis}
    cases = scenario.get("sensitivity") or []
    if not isinstance(cases, list) or len(cases) > 25:
        raise ValueError("sensitivity must contain at most 25 cases")
    for case in cases:
        if not isinstance(case, dict) or set(case) - {"billable_utilization", "realization_multiplier", "label"}:
            raise ValueError("sensitivity supports billable_utilization and realization_multiplier only")
        alternate = copy.deepcopy(bound_scenario)
        if "billable_utilization" in case:
            alternate["billable_utilization"] = case["billable_utilization"]
        if "realization_multiplier" in case:
            alternate.setdefault("prices", {})["realization_multiplier"] = case["realization_multiplier"]
        evaluated = _economics({}, alternate)
        result["sensitivity"].append({"case": case, "metrics": evaluated["metrics"], "gaps": evaluated["gaps"],
                                      "kind": "capacity_bound", "basis": basis})
    return result
