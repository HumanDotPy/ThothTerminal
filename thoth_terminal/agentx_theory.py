"""Evidence-constrained, inference-only resource bounds for architecture scenarios.

This module does not execute, train, or certify an architecture. Its roofline is an
optimistic resource bound, not a performance prediction or a quality comparison.
Quantities accept a finite nonnegative scalar or {"low": ..., "high": ...}.
SI bytes/FLOPs/seconds are used throughout. Inputs are never modified.
"""

from __future__ import annotations

import math
from typing import Any
from urllib.parse import urlparse


_CALCULATORS = {"dense", "moe", "attention_kv", "sparse_lookup", "recurrent_depth"}
_STATUSES = {"documented", "research", "roadmap", "released", "confirmed_release"}
_SOURCE_KINDS = {"paper", "official_technical", "research", "technical_commentary"}
_TIERS = ("hbm", "dram", "nvme")
_MAX_COMPONENTS = 128
_MAX_COUNT = 1_000_000
Range = tuple[float, float]


def _add(a: Range, b: Range) -> Range:
    return a[0] + b[0], a[1] + b[1]


def _mul(a: Range, b: Range) -> Range:
    return a[0] * b[0], a[1] * b[1]


def _div(a: Range, b: Range) -> Range:
    if b[0] <= 0:
        raise ValueError("divisor must be positive across the range")
    return a[0] / b[1], a[1] / b[0]


def _out(value: Range | None, unit: str) -> dict[str, Any] | None:
    return None if value is None else {"low": value[0], "high": value[1], "unit": unit}


def evaluate_agentx_theory(configuration: dict, evidence_catalog: list[dict]) -> dict:
    """Return resource bounds or blocking gaps, including for malformed JSON shapes."""
    try:
        return _evaluate(configuration, evidence_catalog)
    except (TypeError, AttributeError, ValueError, OverflowError) as exc:
        return {"schema_version": 1, "status": "blocked", "economics_ready": False,
                "gaps": [{"code": "invalid_input", "path": "configuration",
                          "message": str(exc), "blocking": True}], "bounds": None,
                "aggregate_flops_per_token": None}


def _evaluate(configuration: dict, evidence_catalog: list[dict]) -> dict:
    """Evaluate a documented-primitive composition under explicit resource assumptions.

    Required configuration scope is component or serving_architecture. Workload
    phase is decode; batch_tokens is the number of concurrent decode sequences,
    each retaining context_tokens. Precision separates weight_bytes storage from
    compute_format; hardware evidence lists supported_compute_formats for its
    supplied compute rate. Dense/MoE use two FLOPs per active parameter per forward
    token plus explicit ancillary FLOPs/traffic (zero requires a rationale).
    Attention and lookup costs must be supplied with source locations or explicit
    derivation/assumption rationale; there is no universal MLA/DSA/CSA2 discount.
    Recurrent-depth wrappers multiply target compute and traffic; weights remain
    shared only under a documented weight-sharing condition. KV sharing is admitted
    only when the evidence lists the exact condition
    and the configuration supplies its satisfaction rationale. All tier bandwidths
    are per GPU, including a GPU's allocated share of host/storage bandwidth.
    Interconnect bandwidth/traffic are per replica, with partition placement and
    workload provenance supplied explicitly. Efficiency never exceeds one.

    Evidence records: id/name/version/calculators/status/sources. Sources require
    url and kind (paper, official_technical, research, technical_commentary).
    Hardware resource_bounds map exact configuration paths to {value: scalar/range,
    locator: source section/table, rationale: technical derivation}; entries are
    required for compute, tier capacity/BW/transfer, link BW and GPU power. A roadmap
    identity requires future_work_rationale; it never becomes a released product.
    Replaced evidence remains in returned ancestry but blocks a current estimate.
    Full-serving architecture_accounting requires full_forward_path: true,
    unresolved_gaps: [], and coverage for input_output_embeddings, attention,
    ffn_or_experts, kv_cache, activation_temporary_memory, routing_communication.
    Coverage declares cost_included: true and rationale, then component_ids or the
    exact resource_path hardware.fixed_memory_bytes_per_replica / hardware.interconnect.
    Source coefficients use cost_provenance[field] with basis, rationale and pinned
    evidence_id/locator; assumption coefficients need basis and rationale only.
    Component scaling_rationale and composition interfaces are explicit conditional
    engineering claims, not evidence of successful composition or model quality.
    Numerical assumptions do not establish that a composition works or preserves
    quality. Neither returned status nor a citation verifies the supplied source.
    """
    gaps: list[dict] = []
    assumptions: list[dict] = []
    ancestry: list[dict] = []

    def gap(code: str, path: str, message: str) -> None:
        gaps.append({"code": code, "path": path, "message": message, "blocking": True})

    def quantity(obj: dict, key: str, path: str, *, positive: bool = False,
                 count: bool = False, efficiency: bool = False) -> Range:
        value = obj.get(key)
        try:
            if isinstance(value, dict):
                low, high = value["low"], value["high"]
            else:
                low = high = value
            if isinstance(low, bool) or isinstance(high, bool):
                raise ValueError("boolean is not a quantity")
            low, high = float(low), float(high)
            if not all(math.isfinite(x) for x in (low, high)) or low < 0 or high < low or high > 1e30:
                raise ValueError("requires finite ordered nonnegative values")
            if positive and low <= 0:
                raise ValueError("requires a positive lower bound")
            if efficiency and (low <= 0 or high > 1):
                raise ValueError("efficiency must lie in (0, 1]")
            if count and (high > _MAX_COUNT or not low.is_integer() or not high.is_integer()):
                raise ValueError(f"count must be integral and at most {_MAX_COUNT}")
            return low, high
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            gap("invalid_or_missing_quantity", f"{path}.{key}", str(exc))
            return 0.0, 0.0

    if not isinstance(configuration, dict) or not isinstance(evidence_catalog, list):
        return {"schema_version": 1, "status": "blocked", "economics_ready": False,
                "gaps": [{"code": "invalid_input", "path": "configuration",
                          "message": "configuration must be a dict and catalog a list",
                          "blocking": True}], "bounds": None}
    if len(evidence_catalog) > 4096:
        evidence_catalog = []
        gap("work_limit", "evidence_catalog", "catalog exceeds 4096 entries")
    catalog: dict[str, dict] = {}
    for index, entry in enumerate(evidence_catalog):
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            gap("invalid_evidence", f"evidence_catalog.{index}", "entry needs a string id")
            continue
        if entry["id"] in catalog:
            gap("ambiguous_evidence", entry["id"], "duplicate evidence id")
        catalog[entry["id"]] = entry

    def admit(item: dict, path: str, calculator: str) -> dict:
        entry = catalog.get(item.get("evidence_id"))
        if entry is None:
            gap("unknown_identity", path, "an exact pinned evidence id is required")
            return {}
        sources = entry.get("sources", [])
        ancestry.append({"configuration_path": path, "evidence_id": entry["id"],
                         "name": entry.get("name"), "version": entry.get("version"),
                         "status": entry.get("status"), "sources": sources,
                         "superseded_by": entry.get("superseded_by"),
                         "derived_from": entry.get("derived_from", []),
                         "supersedes": entry.get("supersedes", []),
                         "promotion": entry.get("promotion"),
                         "verified_at": entry.get("verified_at")})
        for field in ("name", "version"):
            if not isinstance(item.get(field), str) or item.get(field) != entry.get(field):
                gap("identity_mismatch", f"{path}.{field}", "identity must match pinned evidence")
        if calculator not in entry.get("calculators", []):
            gap("unsupported_calculator", path, "evidence does not support this calculator")
        if entry.get("status") not in _STATUSES:
            gap("unsupported_scope", path, "evidence status is not an admitted technical scope")
        if entry.get("superseded_by"):
            gap("superseded_evidence", path,
                f"evidence replaced by {entry['superseded_by']}; history retained")
        if not isinstance(sources, list) or not sources or not all(
            isinstance(source, dict) and source.get("kind") in _SOURCE_KINDS
            and isinstance(source.get("url"), str)
            and urlparse(source["url"]).scheme in ("http", "https")
            and bool(urlparse(source["url"]).netloc) for source in sources
        ):
            gap("missing_technical_source", path, "technical source URLs and kinds are required")
        if calculator != "hardware" and not (isinstance(sources, list) and any(
            isinstance(source, dict) and source.get("kind") in {"paper", "research"}
            for source in sources
        )):
            gap("missing_primary_research", path,
                "architecture mechanisms require primary research; commentary alone cannot establish a mechanism")
        if entry.get("status") == "roadmap" and not item.get("future_work_rationale"):
            gap("missing_future_rationale", path, "roadmap inputs require a proposal rationale")
        return entry

    def provenance(item: dict, field: str, path: str) -> None:
        record = item.get("cost_provenance", {}).get(field)
        if not isinstance(record, dict) or record.get("basis") not in {
            "paper", "derivation", "assumption", "official_technical", "technical_commentary"
        } or not record.get("rationale"):
            gap("missing_cost_provenance", f"{path}.{field}",
                "cost requires basis and a technical derivation/assumption rationale")
            return
        if record["basis"] != "assumption":
            if record.get("evidence_id") != item.get("evidence_id") or not record.get("locator"):
                gap("unpinned_cost", f"{path}.{field}",
                    "source-derived costs need matching evidence id and section/table locator")
        assumptions.append({"path": f"{path}.{field}", **record})

    workload = configuration.get("workload", {})
    scope = configuration.get("scope")
    if scope not in ("component", "serving_architecture"):
        gap("missing_scope", "scope", "explicit component or serving_architecture scope required")
    precision = configuration.get("precision", {})
    if not isinstance(workload, dict):
        workload = {}
    if workload.get("phase") != "decode":
        gap("unsupported_workload_phase", "workload.phase", "this calculator requires explicit steady-state decode phase")
    if not isinstance(precision, dict):
        precision = {}
    context = quantity(workload, "context_tokens", "workload", positive=True, count=True)
    batch = quantity(workload, "batch_tokens", "workload", positive=True, count=True)
    weight_bytes = quantity(precision, "weight_bytes", "precision", positive=True)
    compute_format = precision.get("compute_format")
    if not isinstance(compute_format, str) or not compute_format:
        gap("missing_compute_format", "precision.compute_format", "explicit compute precision required")
    if weight_bytes[1] > 8:
        gap("unsupported_precision", "precision.weight_bytes", "weight storage exceeds 64 bits")
    components = configuration.get("components", [])
    if not isinstance(components, list) or not components or len(components) > _MAX_COMPONENTS:
        components = []
        gap("work_limit", "components", f"requires 1..{_MAX_COMPONENTS} components")
    costs: dict[str, dict] = {}
    wrappers: list[tuple[dict, dict, str]] = []
    zero: Range = (0.0, 0.0)

    for index, raw in enumerate(components):
        path = f"components.{index}"
        before = len(gaps)
        if not isinstance(raw, dict):
            gap("invalid_component", path, "component must be an object")
            continue
        ident = raw.get("id")
        if not isinstance(ident, str) or not ident or ident in costs:
            gap("duplicate_component", path, "component id must be nonempty and unique")
            continue
        calculator = raw.get("calculator")
        if calculator not in _CALCULATORS:
            gap("unsupported_mechanism", path, "no supported resource calculator for mechanism")
            continue
        evidence = admit(raw, path, calculator)
        params = raw.get("parameters", {})
        if not isinstance(params, dict):
            params = {}
        if not raw.get("scaling_rationale"):
            gap("missing_scaling_rationale", path, "configuration must explain parameter scaling")
        count = quantity(params, "count", path, positive=True, count=True)
        item = {"id": ident, "name": raw.get("name"), "calculator": calculator,
                "flops": zero, "memory": {tier: zero for tier in _TIERS},
                "traffic": {tier: zero for tier in _TIERS}, "kv": zero,
                "kv_tier": "hbm", "complete": True}
        if calculator in ("dense", "moe"):
            total = quantity(params, "total_parameters", path, positive=True)
            active = total if calculator == "dense" else quantity(
                params, "active_parameters", path, positive=True)
            if calculator == "moe" and active[1] > total[0]:
                gap("invalid_active_parameters", path,
                    "active parameter upper bound must not exceed total lower bound")
            tier = params.get("weights_tier", "hbm")
            if tier not in _TIERS:
                gap("invalid_tier", path, "weights tier must be hbm, dram or nvme")
                tier = "hbm"
            item["flops"] = _mul(_mul(active, count), (2.0, 2.0))
            item["memory"][tier] = _mul(_mul(total, weight_bytes), count)
            reuse = batch
            if calculator == "moe":
                provenance(raw, "weight_reuse_tokens", path)
                reuse = quantity(params, "weight_reuse_tokens", path, positive=True)
                if reuse[1] > batch[0]:
                    gap("invalid_expert_reuse", path, "guaranteed weight reuse cannot exceed batch lower bound")
            if reuse[0] > 0:
                item["traffic"][tier] = _div(_mul(_mul(active, weight_bytes), count), reuse)
            for field in ("overhead_flops_per_token", "overhead_bytes_per_token"):
                provenance(raw, field, path)
            item["flops"] = _add(item["flops"], _mul(quantity(params, "overhead_flops_per_token", path), count))
            item["traffic"]["hbm"] = _add(item["traffic"]["hbm"], _mul(quantity(params, "overhead_bytes_per_token", path), count))
        elif calculator == "attention_kv":
            if params.get("attention_kind") not in evidence.get("attention_kinds", []):
                gap("unsupported_attention", path, "attention identity must be supported by evidence")
            for field in ("flops_per_token", "kv_bytes_per_context_token", "read_bytes_per_token"):
                provenance(raw, field, path)
            item["flops"] = _mul(quantity(params, "flops_per_token", path), count)
            item["kv"] = _mul(_mul(_mul(quantity(params, "kv_bytes_per_context_token", path),
                                         context), batch), count)
            item["traffic"]["hbm"] = _mul(quantity(params, "read_bytes_per_token", path), count)
            # Supplied read traffic must include KV writes and attention projection weights.
            if not params.get("traffic_includes_kv_writes_and_weights"):
                gap("incomplete_attention_traffic", path, "include projection weights and KV writes")
            for field in ("weight_storage_bytes",):
                provenance(raw, field, path)
                item["memory"]["hbm"] = _mul(quantity(params, field, path), count)
        elif calculator == "sparse_lookup":
            for field in ("table_storage_bytes", "lookup_bytes_per_token", "flops_per_token"):
                provenance(raw, field, path)
            tier = params.get("table_tier")
            if tier not in _TIERS:
                gap("invalid_tier", path, "lookup table needs a supported storage tier")
                tier = "hbm"
            item["memory"][tier] = _mul(quantity(params, "table_storage_bytes", path), count)
            item["traffic"][tier] = _mul(quantity(params, "lookup_bytes_per_token", path), count)
            item["flops"] = _mul(quantity(params, "flops_per_token", path), count)
        else:
            if count != (1.0, 1.0):
                gap("ambiguous_recurrence", path, "recurrent wrapper count must equal one")
            for field in ("overhead_flops_per_token", "overhead_bytes_per_token", "overhead_storage_bytes"):
                provenance(raw, field, path)
            item["flops"] = quantity(params, "overhead_flops_per_token", path)
            item["traffic"]["hbm"] = quantity(params, "overhead_bytes_per_token", path)
            item["memory"]["hbm"] = quantity(params, "overhead_storage_bytes", path)
            wrappers.append((raw, evidence, path))
        item["complete"] = len(gaps) == before
        costs[ident] = item

    wrapped: set[str] = set()
    for raw, evidence, path in wrappers:
        params = raw.get("parameters", {})
        steps = quantity(params, "steps", path, positive=True, count=True)
        targets = params.get("base_component_ids", [])
        if not isinstance(targets, list) or not targets or len(targets) > _MAX_COMPONENTS:
            gap("invalid_recurrence_targets", path, "wrapper requires bounded base component ids")
            continue
        share_kv = params.get("share_kv", False)
        if params.get("shared_weights") is not True or params.get("weight_sharing_condition") not in evidence.get(
            "weight_sharing_conditions", []
        ) or not params.get("weight_condition_satisfaction_rationale"):
            gap("unsupported_weight_sharing", path,
                "recurrent shared weights require exact documented condition and satisfaction rationale")
        if not isinstance(share_kv, bool):
            gap("invalid_sharing", path, "share_kv must be boolean")
        if share_kv:
            condition = params.get("kv_sharing_condition")
            if condition not in evidence.get("kv_sharing_conditions", []) or not params.get(
                "condition_satisfaction_rationale"
            ):
                gap("unsupported_kv_sharing", path,
                    "KV sharing requires exact paper condition and satisfaction rationale")
        for target in targets:
            if not isinstance(target, str) or target not in costs or target in wrapped or costs[
                target
            ]["calculator"] == "recurrent_depth":
                gap("invalid_recurrence_targets", path, "unknown, repeated or nested wrapper target")
                continue
            wrapped.add(target)
            item = costs[target]
            item["flops"] = _mul(item["flops"], steps)
            item["traffic"] = {tier: _mul(value, steps) for tier, value in item["traffic"].items()}
            if not share_kv:
                item["kv"] = _mul(item["kv"], steps)

    composition = configuration.get("composition", {})
    if len(costs) > 1:
        if not isinstance(composition, dict) or not composition.get("rationale"):
            gap("missing_composition_rationale", "composition", "composition needs a technical rationale")
            composition = {}
        interfaces = composition.get("interfaces", [])
        graph = {ident: set() for ident in costs}
        if not isinstance(interfaces, list) or len(interfaces) > _MAX_COMPONENTS * 4:
            interfaces = []
            gap("work_limit", "composition.interfaces", "interfaces must be a bounded list")
        for index, edge in enumerate(interfaces):
            if not isinstance(edge, dict):
                gap("invalid_interface", f"composition.interfaces.{index}", "interface must be an object")
                continue
            start, end = edge.get("from"), edge.get("to")
            if start not in graph or end not in graph or start == end or edge.get(
                "compatible"
            ) is not True or not edge.get("rationale"):
                gap("incompatible_interface", f"composition.interfaces.{index}",
                    "interface needs existing distinct endpoints and compatibility rationale")
                continue
            graph[start].add(end)
            graph[end].add(start)
        reached: set[str] = set()
        pending = [next(iter(graph))]
        while pending:
            ident = pending.pop()
            if ident not in reached:
                reached.add(ident)
                pending.extend(graph[ident] - reached)
        if len(reached) != len(graph):
            gap("disconnected_composition", "composition.interfaces", "every primitive must be connected")

    if scope == "serving_architecture":
        accounting = configuration.get("architecture_accounting", {})
        if not isinstance(accounting, dict) or accounting.get("full_forward_path") is not True:
            gap("incomplete_forward_path", "architecture_accounting", "full forward-path accounting is required")
            accounting = {}
        if accounting.get("unresolved_gaps") != []:
            gap("unresolved_architecture_gaps", "architecture_accounting.unresolved_gaps",
                "explicit empty unresolved_gaps required for economic capacity")
        coverage = accounting.get("coverage", {})
        if not isinstance(coverage, dict):
            coverage = {}
        for category in ("input_output_embeddings", "attention", "ffn_or_experts", "kv_cache",
                         "activation_temporary_memory", "routing_communication"):
            covered = coverage.get(category, {})
            if not isinstance(covered, dict) or covered.get("cost_included") is not True or not covered.get("rationale"):
                gap("missing_architecture_cost", f"architecture_accounting.coverage.{category}",
                    "category requires explicit cost inclusion and rationale")
                continue
            if category in ("activation_temporary_memory", "routing_communication"):
                required_path = "hardware.fixed_memory_bytes_per_replica" if category == "activation_temporary_memory" else "hardware.interconnect"
                if covered.get("resource_path") != required_path:
                    gap("missing_architecture_cost", f"architecture_accounting.coverage.{category}",
                        f"must account for {required_path}")
            else:
                ids = covered.get("component_ids", [])
                if not isinstance(ids, list) or not ids or any(ident not in costs for ident in ids):
                    gap("missing_architecture_cost", f"architecture_accounting.coverage.{category}",
                        "category requires valid costed component ids")
                elif category in ("attention", "kv_cache") and not any(
                    costs[ident]["calculator"] == "attention_kv" for ident in ids
                ):
                    gap("missing_architecture_cost", f"architecture_accounting.coverage.{category}",
                        "attention/KV accounting requires a supported attention calculator")

    hardware = configuration.get("hardware", {})
    if not isinstance(hardware, dict):
        hardware = {}
    hardware_evidence = admit(hardware, "hardware", "hardware")
    if compute_format not in hardware_evidence.get("supported_compute_formats", []):
        gap("unsupported_compute_format", "precision.compute_format", "hardware compute rate must match supported precision")
    provenance(hardware, "resources", "hardware")
    # Resource values are admitted only within catalogued technical constraints;
    # a valid URL alone cannot certify arbitrary future bandwidth/capacity.
    resource_bounds = hardware_evidence.get("resource_bounds", {})
    if not isinstance(resource_bounds, dict) or not resource_bounds:
        gap("missing_physical_constraints", "hardware", "hardware evidence needs pinned resource_bounds")
        resource_bounds = {}

    def constrained(obj: dict, key: str, path: str, *, positive: bool = False) -> Range:
        value = quantity(obj, key, path, positive=positive)
        constraint = resource_bounds.get(f"{path}.{key}")
        if not isinstance(constraint, dict) or not constraint.get("rationale") or not constraint.get("locator"):
            gap("missing_physical_constraint", f"{path}.{key}", "technical range, locator and rationale required")
        else:
            bound = quantity(constraint, "value", f"evidence.{hardware_evidence.get('id')}.resource_bounds.{path}.{key}")
            if value[0] < bound[0] or value[1] > bound[1]:
                gap("outside_evidence_constraints", f"{path}.{key}", "scenario exceeds source-supported physical range")
        return value
    gpus = quantity(hardware, "gpus_per_replica", "hardware", positive=True, count=True)
    replicas = quantity(hardware, "replicas", "hardware", positive=True, count=True)
    compute = constrained(hardware, "compute_flops_per_second_per_gpu", "hardware", positive=True)
    compute_eff = quantity(hardware, "compute_efficiency", "hardware", efficiency=True)
    effective_compute = _mul(_mul(compute, compute_eff), gpus)
    fixed = hardware.get("fixed_memory_bytes_per_replica", {})
    if not isinstance(fixed, dict):
        fixed = {}
    tiers = hardware.get("tiers", {})
    if not isinstance(tiers, dict):
        tiers = {}
    memory = {tier: zero for tier in _TIERS}
    traffic = {tier: zero for tier in _TIERS}
    flops = zero
    for item in costs.values():
        flops = _add(flops, item["flops"])
        for tier in _TIERS:
            memory[tier] = _add(memory[tier], item["memory"][tier])
            traffic[tier] = _add(traffic[tier], item["traffic"][tier])
        memory["hbm"] = _add(memory["hbm"], item["kv"])
    limits: list[Range] = []
    if flops[0] <= 0:
        gap("missing_compute", "components", "aggregate forward compute must be positive")
    elif effective_compute[0] > 0:
        limits.append(_div(flops, effective_compute))
    resources: dict[str, dict] = {}
    for tier in _TIERS:
        if tier in fixed:
            memory[tier] = _add(memory[tier], quantity(fixed, tier, "hardware.fixed_memory_bytes_per_replica"))
        elif memory[tier][1] > 0:
            gap("missing_runtime_memory", f"hardware.fixed_memory_bytes_per_replica.{tier}",
                "explicit runtime/activation/allocator allowance is required, including zero")
        spec = tiers.get(tier)
        if memory[tier][1] == 0 and traffic[tier][1] == 0 and spec is None:
            continue
        if not isinstance(spec, dict):
            spec = {}
        capacity = _mul(constrained(spec, "capacity_bytes_per_gpu", f"hardware.tiers.{tier}"), gpus)
        bandwidth = _mul(_mul(constrained(spec, "bandwidth_bytes_per_second_per_gpu",
                                          f"hardware.tiers.{tier}", positive=True),
                              quantity(spec, "efficiency", f"hardware.tiers.{tier}", efficiency=True)), gpus)
        if memory[tier][1] > capacity[0]:
            gap("insufficient_capacity", f"hardware.tiers.{tier}",
                "worst-case per-replica demand exceeds guaranteed capacity")
        if bandwidth[0] > 0 and traffic[tier][1] > 0:
            limits.append(_div(traffic[tier], bandwidth))
        if tier != "hbm" and traffic[tier][1] > 0:
            transfer_bw = _mul(constrained(spec, "transfer_to_gpu_bandwidth_bytes_per_second_per_replica",
                                            f"hardware.tiers.{tier}", positive=True),
                               quantity(spec, "transfer_efficiency", f"hardware.tiers.{tier}", efficiency=True))
            if transfer_bw[0] > 0:
                limits.append(_div(traffic[tier], transfer_bw))
        resources[tier] = {"required_bytes_per_replica": _out(memory[tier], "bytes"),
                           "capacity_bytes_per_replica": _out(capacity, "bytes"),
                           "bytes_per_token": _out(traffic[tier], "bytes/token"),
                           "effective_bandwidth_per_replica": _out(bandwidth, "bytes/second")}
    interconnect = hardware.get("interconnect", {})
    if not isinstance(interconnect, dict):
        interconnect = {}
    link_traffic = quantity(interconnect, "bytes_per_token", "hardware.interconnect")
    if gpus[1] > 1 and not interconnect.get("topology_rationale"):
        gap("missing_topology", "hardware.interconnect", "multi-GPU placement and communication rationale required")
    if link_traffic[1] > 0:
        link_bw = _mul(constrained(interconnect, "bandwidth_bytes_per_second_per_replica",
                                   "hardware.interconnect", positive=True),
                       quantity(interconnect, "efficiency", "hardware.interconnect", efficiency=True))
        if link_bw[0] > 0:
            limits.append(_div(link_traffic, link_bw))
    elif gpus[1] > 1 and not interconnect.get("zero_traffic_rationale"):
        gap("missing_interconnect_traffic", "hardware.interconnect", "zero traffic needs a justified partition")
    utilities = hardware.get("utilities", {})
    if not isinstance(utilities, dict):
        utilities = {}
    watts = constrained(utilities, "device_watts_per_gpu", "hardware.utilities", positive=True)
    other_watts = quantity(utilities, "other_watts_per_replica", "hardware.utilities")
    pue = quantity(utilities, "pue", "hardware.utilities", positive=True)
    if pue[0] < 1:
        gap("invalid_pue", "hardware.utilities.pue", "PUE cannot be below one")
    it_power = _mul(_add(_mul(watts, gpus), other_watts), replicas)
    facility_power = _mul(it_power, pue)
    power_budget = quantity(utilities, "facility_power_watts", "hardware.utilities", positive=True)
    cooling = quantity(utilities, "cooling_watts", "hardware.utilities", positive=True)
    if facility_power[1] > power_budget[0]:
        gap("power_budget", "hardware.utilities", "facility power budget below worst-case demand")
    if it_power[1] > cooling[0]:
        gap("thermal_budget", "hardware.utilities", "cooling capacity below worst-case IT heat")
    if gpus[0] != gpus[1] or replicas[0] != replicas[1]:
        gap("uncertain_topology", "hardware", "replica topology counts must be exact")
    if gpus[1] > 1 and not hardware.get("memory_partition_rationale"):
        gap("missing_memory_partition", "hardware", "aggregate capacity needs a feasible shard/placement rationale")
    if not configuration.get("assumptions"):
        gap("missing_assumptions", "assumptions", "scenario/workload/precision assumptions must be explicit")
    bounds = None
    if not gaps and limits:
        latency = max(value[0] for value in limits), max(value[1] for value in limits)
        fleet_rate = _div(replicas, latency)
        if all(math.isfinite(x) for x in (*latency, *fleet_rate)):
            bounds = {"tokens_per_second_upper": _out(fleet_rate, "tokens/second"),
                      "seconds_per_token_lower": _out(latency, "seconds/token/replica"),
                      "gpus_per_replica": gpus[0], "replicas": replicas[0],
                      "per_gpu_tokens_per_second_upper": _out(_div(fleet_rate, _mul(gpus, replicas)), "tokens/second/GPU"),
                      "scope": "fleet aggregate steady-state decode token flow",
                      "latency_interpretation": "seconds_per_token_lower is inverse replica throughput, not per-stream token latency",
                      "interpretation": "optimistic steady-state resource roofline; no measured performance",
                      "excludes": ["quality equivalence", "training", "queueing", "kernel scheduling",
                                   "non-overlapped serial latency", "successful architecture integration"]}
        else:
            gap("numeric_overflow", "bounds", "resource arithmetic exceeds finite representable range")
    ready = not gaps and bounds is not None
    if not ready:
        # Diagnostics retain capacities, but uncosted primitives never become
        # apparent zero-cost aggregate memory or bandwidth demand.
        for resource in resources.values():
            resource["required_bytes_per_replica"] = None
            resource["bytes_per_token"] = None
    for resource in resources.values():
        resource["complete"] = ready
    return {"schema_version": 1, "status": "ready" if ready else "blocked",
            "scope": scope, "economics_ready": ready and scope == "serving_architecture",
            "gpus_per_replica": _out(gpus, "GPUs/replica"), "replicas": _out(replicas, "replicas"),
            "gaps": gaps, "bounds": bounds,
            "assumptions": assumptions, "scenario_assumptions": configuration.get("assumptions"),
            "provenance": ancestry, "components": [
                {"id": item["id"], "name": item["name"], "calculator": item["calculator"],
                 "flops_per_token": _out(item["flops"], "FLOPs/token") if item["complete"] else None,
                 "kv_bytes_per_replica": _out(item["kv"], "bytes") if item["complete"] else None} for item in costs.values()],
            "resources": resources,
            "aggregate_flops_per_token": _out(flops, "FLOPs/token") if ready else None,
            "facility_power_watts": _out(facility_power, "watts"),
            "quality_equivalence": "not established",
            "evidence_validation": "catalog declarations and pinned identity checked; source content not independently verified"}
