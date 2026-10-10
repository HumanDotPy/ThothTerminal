from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from sqlalchemy.orm import Session

from thoth_terminal.dataset_parts.core import ensure_dataset_tables
from thoth_terminal.dataset_parts.mutations import (
    create_dataset,
    create_dataset_view,
    latest_dataset_version,
    publish_dataset_snapshot,
    replace_dataset_draft,
)
from thoth_terminal.agentx_economics import evaluate_agentx_economics
from thoth_terminal.db import utcnow
from thoth_terminal.models import Dataset, DatasetRow, DatasetVersion


SERVING_ASSUMPTIONS_DATASET_SLUG = "ai-serving-scenario-assumptions"
MODEL_COST_QUALITY_FRONTIER_DATASET_SLUG = "ai-model-cost-quality-frontier"
SERVING_MARGIN_SCENARIOS_DATASET_SLUG = "ai-serving-margin-scenarios"


def _serving_hash(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _serving_number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except Exception:
        return None


def _serving_text(value: Any) -> str:
    return str(value or "").strip()


def _serving_norm(value: Any) -> str:
    text = _serving_text(value).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _serving_row_key(prefix: str, values: dict[str, Any]) -> str:
    digest = _serving_hash(values)[:18]
    return f"{prefix}:{digest}"


def _serving_columns(specs: list[tuple[str, str, str, str | None]]) -> list[dict[str, Any]]:
    return [{"key": key, "label": label, "data_type": data_type, "role": role, "config": {}} for key, label, data_type, role in specs]


def serving_assumption_columns() -> list[dict[str, Any]]:
    return _serving_columns(
        [
            ("scenario_id", "Scenario ID", "text", "id"),
            ("scenario_label", "Scenario Label", "text", "dimension"),
            ("utilization_rate", "Utilization Rate", "number", "metric"),
            ("cache_hit_rate", "Cache Hit Rate", "number", "metric"),
            ("input_token_share", "Input Token Share", "number", "metric"),
            ("output_token_share", "Output Token Share", "number", "metric"),
            ("price_realization_multiplier", "Price Realization Multiplier", "number", "metric"),
            ("electricity_usd_per_kwh", "Electricity USD / kWh", "number", "metric"),
            ("pue", "PUE", "number", "metric"),
            ("gpu_capex_usd", "GPU Capex USD", "number", "metric"),
            ("depreciation_years", "Depreciation Years", "number", "metric"),
            ("assumption_status", "Assumption Status", "text", "dimension"),
            ("confidence", "Confidence", "number", "metric"),
            ("notes", "Notes", "text", "dimension"),
            ("min_p90_interactivity_tok_s_user", "P90 Interactivity Floor tok/s/user", "number", "metric"),
            ("max_ttft_ms", "P90 TTFT Ceiling ms", "number", "metric"),
            ("availability", "Availability", "number", "metric"),
            ("annual_hours", "Hours / Year", "number", "metric"),
            ("utility_power_w_per_gpu", "All-in Utility Power / GPU W", "number", "metric"),
            ("royalty_revenue_share", "Royalty Revenue Share", "number", "metric"),
            ("royalty_fixed_usd_per_gpu_hour", "Fixed Royalty USD / GPU Hour", "number", "metric"),
            ("royalty_basis", "Royalty Basis", "text", "dimension"),
            ("override_basis", "Override Basis", "text", "dimension"),
            ("tco", "Explicit All-in / Component TCO", "json", "dimension"),
        ]
    )


def default_serving_assumption_rows() -> list[dict[str, Any]]:
    defaults = [
        {
            "scenario_id": "bear_low_util_no_cache",
            "scenario_label": "Bear: low utilization, no cache",
            "utilization_rate": 0.35,
            "cache_hit_rate": 0.0,
            "input_token_share": 0.55,
            "output_token_share": 0.45,
            "price_realization_multiplier": 0.70,
            "electricity_usd_per_kwh": 0.08,
            "pue": 1.25,
            "gpu_capex_usd": None,
            "depreciation_years": 3.0,
            "assumption_status": "operator_assumption",
            "confidence": 0.35,
            "notes": "Stress case. GPU capex is deliberately blank until sourced from cloud pricing, disclosures, channel checks, or operator input.",
        },
        {
            "scenario_id": "base_moderate_cache",
            "scenario_label": "Base: moderate utilization/cache",
            "utilization_rate": 0.60,
            "cache_hit_rate": 0.35,
            "input_token_share": 0.55,
            "output_token_share": 0.45,
            "price_realization_multiplier": 1.00,
            "electricity_usd_per_kwh": 0.08,
            "pue": 1.25,
            "gpu_capex_usd": None,
            "depreciation_years": 3.0,
            "assumption_status": "operator_assumption",
            "confidence": 0.45,
            "notes": "Base serving economics case. Utilization and cache hit rate are not observed facts.",
        },
        {
            "scenario_id": "bull_high_cache",
            "scenario_label": "Bull: high utilization/cache",
            "utilization_rate": 0.85,
            "cache_hit_rate": 0.65,
            "input_token_share": 0.60,
            "output_token_share": 0.40,
            "price_realization_multiplier": 1.00,
            "electricity_usd_per_kwh": 0.08,
            "pue": 1.20,
            "gpu_capex_usd": None,
            "depreciation_years": 3.0,
            "assumption_status": "operator_assumption",
            "confidence": 0.30,
            "notes": "Optimistic cache/reuse and fleet-fill case.",
        },
        {
            "scenario_id": "compression_case",
            "scenario_label": "Price compression sensitivity",
            "utilization_rate": 0.60,
            "cache_hit_rate": 0.35,
            "input_token_share": 0.55,
            "output_token_share": 0.45,
            "price_realization_multiplier": 0.50,
            "electricity_usd_per_kwh": 0.08,
            "pue": 1.25,
            "gpu_capex_usd": None,
            "depreciation_years": 3.0,
            "assumption_status": "operator_assumption",
            "confidence": 0.40,
            "notes": "Sensitivity for API price cuts, promotions, routing discounts, or weaker realized pricing.",
        },
    ]
    for row in defaults:
        row.update(min_p90_interactivity_tok_s_user=30.0, availability=1.0, annual_hours=8760.0,
                   override_basis="Operator scenario token mix/cache assumptions; not measured contract or workload")
    return [{"row_key": row["scenario_id"], "values": row} for row in defaults]


def cost_quality_frontier_columns() -> list[dict[str, Any]]:
    return _serving_columns(
        [
            ("observed_at", "Observed At", "text", "timestamp"),
            ("creator", "Creator", "text", "dimension"),
            ("model", "Model", "text", "dimension"),
            ("slug", "Slug", "text", "dimension"),
            ("release_date", "Release Date", "text", "dimension"),
            ("intelligence_index", "AA Intelligence Index", "number", "metric"),
            ("coding_index", "AA Coding Index", "number", "metric"),
            ("agentic_index", "AA Agentic Index", "number", "metric"),
            ("intelligence_index_total_cost_usd", "Intelligence Index Cost USD", "number", "metric"),
            ("cost_per_intelligence_point_usd", "Cost / Intelligence Point USD", "number", "metric"),
            ("input_usd_per_mtok", "Input USD / MTok", "number", "metric"),
            ("output_usd_per_mtok", "Output USD / MTok", "number", "metric"),
            ("blended_text_usd_per_mtok", "Blended 50/50 Text USD / MTok", "number", "metric"),
            ("median_output_tokens_per_second", "Median Output tok/s", "number", "metric"),
            ("frontier_score", "Frontier Score", "number", "metric"),
            ("source_dataset_slug", "Source Dataset Slug", "text", "dimension"),
            ("source_url", "Source URL", "text", "dimension"),
        ]
    )


def serving_margin_columns() -> list[dict[str, Any]]:
    return _serving_columns(
        [
            ("observed_at", "Observed At", "text", "timestamp"),
            ("scenario_id", "Scenario ID", "text", "dimension"),
            ("scenario_label", "Scenario Label", "text", "dimension"),
            ("hardware", "Hardware", "text", "dimension"),
            ("framework", "Framework", "text", "dimension"),
            ("benchmark_model", "Benchmark Model", "text", "dimension"),
            ("benchmark_type", "Workload Type", "text", "dimension"),
            ("benchmark_run_url", "Benchmark Run", "text", "dimension"),
            ("recipe_fingerprint", "Recipe Fingerprint", "text", "dimension"),
            ("offload_mode", "KV Offload Mode", "text", "dimension"),
            ("priced_provider", "Priced Provider", "text", "dimension"),
            ("priced_model", "Priced Model", "text", "dimension"),
            ("pricing_basis", "Pricing Basis", "text", "dimension"),
            ("median_interactivity_tok_s_user", "Median Interactivity tok/s/user", "number", "metric"),
            ("tput_per_gpu_tok_s", "Throughput / GPU tok/s", "number", "metric"),
            ("utilization_rate", "Utilization Rate", "number", "metric"),
            ("cache_hit_rate", "Cache Hit Rate", "number", "metric"),
            ("input_token_share", "Input Token Share", "number", "metric"),
            ("output_token_share", "Output Token Share", "number", "metric"),
            ("effective_input_usd_per_mtok", "Effective Input USD / MTok", "number", "metric"),
            ("output_usd_per_mtok", "Output USD / MTok", "number", "metric"),
            ("blended_revenue_usd_per_mtok", "Blended Revenue USD / MTok", "number", "metric"),
            ("tokens_per_day_per_gpu", "Tokens / Day / GPU", "number", "metric"),
            ("gross_revenue_usd_per_day_per_gpu", "Gross Revenue USD / Day / GPU", "number", "metric"),
            ("avg_power_w", "Average GPU Power W", "number", "metric"),
            ("electricity_usd_per_kwh", "Electricity USD / kWh", "number", "metric"),
            ("pue", "PUE", "number", "metric"),
            ("power_cost_usd_per_day_per_gpu", "Power Cost USD / Day / GPU", "number", "metric"),
            ("gpu_capex_usd", "GPU Capex USD", "number", "metric"),
            ("depreciation_usd_per_day_per_gpu", "Depreciation USD / Day / GPU", "number", "metric"),
            ("gross_margin_before_capex_usd_per_day_per_gpu", "Gross Margin Before Capex USD / Day / GPU", "number", "metric"),
            ("net_margin_after_depreciation_usd_per_day_per_gpu", "Net Margin After Depreciation USD / Day / GPU", "number", "metric"),
            ("payback_days", "Payback Days", "number", "metric"),
            ("aa_intelligence_index", "AA Intelligence Index", "number", "metric"),
            ("assumption_status", "Assumption Status", "text", "dimension"),
            ("match_confidence", "Match Confidence", "number", "metric"),
            ("formula", "Formula", "text", "dimension"),
            ("source_dataset_slugs", "Source Dataset Slugs", "json", "dimension"),
            ("availability", "Availability", "number", "metric"),
            ("p90_interactivity_tok_s_user", "P90 Interactivity tok/s/user", "number", "metric"),
            ("tco_usd_per_day_per_gpu", "TCO USD / Day / GPU", "number", "metric"),
            ("profit_usd_per_day_per_gpu", "Profit USD / Day / GPU", "number", "metric"),
            ("profit_margin", "Profit Margin", "number", "metric"),
            ("cost_usd_per_mtok", "Cost USD / MTok", "number", "metric"),
            ("break_even_billable_utilization", "Break-even Billable Utilization", "number", "metric"),
            ("revenue_usd_per_gw_year", "Revenue USD / GW / Year", "number", "metric"),
            ("profit_usd_per_gw_year", "Profit USD / GW / Year", "number", "metric"),
            ("status", "Evaluation Status", "text", "dimension"),
            ("gaps", "Missing Inputs", "json", "dimension"),
            ("provenance", "Provenance", "json", "dimension"),
            ("lineage", "Benchmark Lineage", "json", "dimension"),
            ("selected_operating_point", "Selected Measured Point", "json", "dimension"),
            ("price_lineage", "Price Lineage", "json", "dimension"),
            ("assumption_lineage", "Assumption Lineage", "json", "dimension"),
            ("scenario_inputs", "Pinned Scenario Inputs", "json", "dimension"),
            ("price_snapshot", "Pinned Price Inputs", "json", "dimension"),
        ]
    )


def _serving_active_rows(session: Session, slug: str, *, limit: int = 10000, benchmark_type: str | None = None) -> tuple[Dataset | None, list[dict[str, Any]], dict[str, Any]]:
    dataset = session.query(Dataset).filter_by(slug=slug).first()
    if dataset is None:
        return None, [], {}
    published = latest_dataset_version(session, int(dataset.id), status="published")
    draft = latest_dataset_version(session, int(dataset.id), status="draft")
    version = (draft or published) if slug == SERVING_ASSUMPTIONS_DATASET_SLUG else (published or draft)
    if version is None:
        return dataset, [], dict(dataset.extra or {})
    query = session.query(DatasetRow).filter_by(dataset_version_id=int(version.id))
    if benchmark_type is not None:
        query = query.filter(DatasetRow.values["benchmark_type"].as_string() == benchmark_type)
    total_rows = query.count()
    rows = (
        query
        .order_by(DatasetRow.sort_order.asc(), DatasetRow.id.asc())
        .limit(max(1, int(limit or 10000)))
        .all()
    )
    values: list[dict[str, Any]] = []
    for row in rows:
        raw = dict(row.values or {})
        if isinstance(raw.get("values"), dict):
            raw = dict(raw["values"])
        raw.update(source_dataset_slug=slug, source_dataset_version_id=int(version.id),
                   source_row_id=int(row.id), source_row_key=row.row_key)
        values.append(raw)
    extra = dict(version.extra or dataset.extra or {})
    extra.update(source_dataset_version_id=int(version.id), rows_available=total_rows,
                 rows_loaded=len(values), truncated=total_rows > len(values))
    return dataset, values, extra


def build_cost_quality_frontier_rows(aa_rows: list[dict[str, Any]], *, observed_at: str | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for source in aa_rows:
        intelligence = _serving_number(source.get("artificial_analysis_intelligence_index"))
        total_cost = _serving_number(source.get("intelligence_index_total_cost_usd"))
        input_price = _serving_number(source.get("input_usd_per_mtok"))
        output_price = _serving_number(source.get("output_usd_per_mtok"))
        blended_price = None
        if input_price is not None and output_price is not None:
            blended_price = (input_price + output_price) / 2.0
        cost_per_point = total_cost / intelligence if total_cost is not None and intelligence and intelligence > 0 else None
        frontier_score = intelligence / blended_price if intelligence is not None and blended_price and blended_price > 0 else None
        values = {
            "observed_at": observed_at or source.get("observed_at"),
            "creator": source.get("creator"),
            "model": source.get("model"),
            "slug": source.get("slug"),
            "release_date": source.get("release_date"),
            "intelligence_index": intelligence,
            "coding_index": _serving_number(source.get("artificial_analysis_coding_index")),
            "agentic_index": _serving_number(source.get("artificial_analysis_agentic_index")),
            "intelligence_index_total_cost_usd": total_cost,
            "cost_per_intelligence_point_usd": cost_per_point,
            "input_usd_per_mtok": input_price,
            "output_usd_per_mtok": output_price,
            "blended_text_usd_per_mtok": blended_price,
            "median_output_tokens_per_second": _serving_number(source.get("median_output_tokens_per_second")),
            "frontier_score": frontier_score,
            "source_dataset_slug": "artificial-analysis-model-economics",
            "source_url": source.get("source_url"),
        }
        out.append({"row_key": _serving_row_key("cost-quality", values), "values": values})
    return out


def _serving_price_rows(aa_rows: list[dict[str, Any]], provider_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for source in aa_rows:
        input_price = _serving_number(source.get("input_usd_per_mtok"))
        output_price = _serving_number(source.get("output_usd_per_mtok"))
        if input_price is None or output_price is None:
            continue
        rows.append(
            {
                "provider": _serving_text(source.get("creator")) or "artificial_analysis",
                "model": source.get("model"),
                "input_usd_per_mtok": input_price,
                "cached_input_usd_per_mtok": source.get("cache_hit_usd_per_mtok"),
                "cache_hit_usd_per_mtok": source.get("cache_hit_usd_per_mtok"),
                "output_usd_per_mtok": output_price,
                "pricing_basis": "artificial_analysis_model_pricing",
                "aa_intelligence_index": source.get("artificial_analysis_intelligence_index"),
                "norm_model": _serving_norm(source.get("model")),
                **{k: source.get(k) for k in ("source_url", "source_dataset_slug", "source_dataset_version_id", "source_row_id", "source_row_key")},
            }
        )
    for source in provider_rows:
        input_price = _serving_number(source.get("input_usd_per_mtok"))
        output_price = _serving_number(source.get("output_usd_per_mtok"))
        if input_price is None or output_price is None:
            continue
        rows.append(
            {
                "provider": source.get("provider"),
                "model": source.get("model"),
                "input_usd_per_mtok": input_price,
                "cached_input_usd_per_mtok": source.get("cached_input_usd_per_mtok"),
                "cache_hit_usd_per_mtok": source.get("cache_hit_usd_per_mtok"),
                "output_usd_per_mtok": output_price,
                "pricing_basis": "first_party_api_price_equivalent",
                "aa_intelligence_index": None,
                "norm_model": _serving_norm(source.get("model")),
                **{k: source.get(k) for k in ("source_url", "source_dataset_slug", "source_dataset_version_id", "source_row_id", "source_row_key")},
            }
        )
    return rows


def build_serving_margin_rows(
    inferencex_rows: list[dict[str, Any]],
    price_rows: list[dict[str, Any]],
    assumption_rows: list[dict[str, Any]],
    *,
    observed_at: str,
    benchmark_limit: int = 5000,
    price_limit: int = 1000,
) -> list[dict[str, Any]]:
    # Caps are caller-controlled samples, disclosed in sync metadata. Selection
    # happens within recipes in the canonical evaluator, never across models.
    benchmarks = [r for r in inferencex_rows if r.get("benchmark_type") in {"agentic_traces", "agentx", "AgentX"}][:max(1, benchmark_limit)]
    out: list[dict[str, Any]] = []
    for price in price_rows[:max(1, price_limit)]:
        def exact_model(row: dict[str, Any]) -> bool:
            raw_model = row.get("source_model") or row.get("model")
            if _serving_norm(raw_model) == _serving_norm(price.get("model")):
                return True
            # An upstream display option can collapse several model versions.
            # It is a verified alias only when its raw-key membership is unique.
            keys = row.get("model_db_keys")
            return bool(isinstance(keys, list) and len(keys) == 1 and str(keys[0]) == str(raw_model)
                        and _serving_norm(row.get("display_model")) == _serving_norm(price.get("model")))
        matching = [r for r in benchmarks if exact_model(r)]
        if not matching:
            continue
        for assumption in assumption_rows:
            scenario = {
                "scenario_id": assumption.get("scenario_id"),
                "min_p90_interactivity_tok_s_user": assumption.get("min_p90_interactivity_tok_s_user"),
                "max_ttft_ms": assumption.get("max_ttft_ms"),
                "billable_utilization": assumption.get("utilization_rate"),
                "availability": assumption.get("availability"),
                "royalty_revenue_share": assumption.get("royalty_revenue_share"),
                "royalty_fixed_usd_per_gpu_hour": assumption.get("royalty_fixed_usd_per_gpu_hour"),
                "royalty_basis": assumption.get("royalty_basis"),
                "annual_hours": assumption.get("annual_hours"),
                "utility_power_w_per_gpu": assumption.get("utility_power_w_per_gpu"),
                "tco": assumption.get("tco") or {},
                "prices": {
                    "input_usd_per_mtok": price.get("input_usd_per_mtok"),
                    "cached_input_usd_per_mtok": price.get("cached_input_usd_per_mtok") if price.get("cached_input_usd_per_mtok") is not None else price.get("cache_hit_usd_per_mtok"),
                    "output_usd_per_mtok": price.get("output_usd_per_mtok"),
                    "realization_multiplier": assumption.get("price_realization_multiplier"),
                    "basis": price.get("pricing_basis"), "source_url": price.get("source_url"),
                },
                "overrides": {k: assumption[k] for k in ("input_token_share", "cache_hit_rate") if assumption.get(k) is not None},
                "override_basis": assumption.get("override_basis") or assumption.get("notes"),
            }
            evaluated = evaluate_agentx_economics(matching, scenario)
            for result in evaluated["results"]:
                metrics = result["metrics"]
                recipe = result["recipe"]
                selected = result["selected_operating_point"] or {}
                values = {
                    "observed_at": observed_at, "scenario_id": assumption.get("scenario_id"),
                    "scenario_label": assumption.get("scenario_label"),
                    "hardware": recipe.get("hardware"), "framework": recipe.get("framework"),
                    "benchmark_model": recipe.get("model"), "benchmark_type": "agentic_traces",
                    "benchmark_run_url": selected.get("run_url"), "recipe_fingerprint": recipe.get("recipe_fingerprint"),
                    "offload_mode": recipe.get("offload_mode"), "priced_provider": price.get("provider"),
                    "priced_model": price.get("model"), "pricing_basis": price.get("pricing_basis"),
                    "p90_interactivity_tok_s_user": selected.get("p90_intvty"),
                    "tput_per_gpu_tok_s": metrics.get("total_tok_s_per_gpu"),
                    "utilization_rate": scenario["billable_utilization"], "availability": scenario["availability"],
                    "cache_hit_rate": metrics.get("cache_hit_rate"), "input_token_share": metrics.get("input_token_share"),
                    "output_token_share": 1 - metrics["input_token_share"] if metrics.get("input_token_share") is not None else None,
                    "effective_input_usd_per_mtok": metrics.get("effective_input_usd_per_mtok"),
                    "output_usd_per_mtok": price.get("output_usd_per_mtok"),
                    "blended_revenue_usd_per_mtok": metrics.get("blended_revenue_usd_per_mtok"),
                    "tokens_per_day_per_gpu": metrics.get("tokens_per_day_per_gpu"),
                    "gross_revenue_usd_per_day_per_gpu": metrics.get("revenue_usd_per_day_per_gpu"),
                    "tco_usd_per_day_per_gpu": metrics.get("tco_usd_per_day_per_gpu"),
                    "profit_usd_per_day_per_gpu": metrics.get("profit_usd_per_day_per_gpu"),
                    "net_margin_after_depreciation_usd_per_day_per_gpu": metrics.get("profit_usd_per_day_per_gpu"),
                    "profit_margin": metrics.get("profit_margin"), "cost_usd_per_mtok": metrics.get("cost_usd_per_mtok"),
                    "break_even_billable_utilization": metrics.get("break_even_billable_utilization"),
                    "revenue_usd_per_gw_year": metrics.get("revenue_usd_per_gw_year"),
                    "profit_usd_per_gw_year": metrics.get("profit_usd_per_gw_year"),
                    "assumption_status": assumption.get("assumption_status"), "match_confidence": 1.0,
                    "status": result["status"], "gaps": result["gaps"], "provenance": result.get("provenance", {}),
                    "lineage": result["lineage"], "selected_operating_point": result["selected_operating_point"],
                    "price_lineage": {k: price.get(k) for k in ("source_dataset_slug", "source_dataset_version_id", "source_row_id", "source_row_key")},
                    "assumption_lineage": {k: assumption.get(k) for k in ("source_dataset_slug", "source_dataset_version_id", "source_row_id", "source_row_key")},
                    "scenario_inputs": scenario, "price_snapshot": price,
                    "formula": evaluated["method"],
                    "source_dataset_slugs": ["inferencex-upstream-benchmarks", "artificial-analysis-model-economics", "ai-provider-api-pricing", SERVING_ASSUMPTIONS_DATASET_SLUG],
                }
                out.append({"row_key": _serving_row_key("serving-margin", {"recipe_id": result["recipe_id"], "scenario_id": values["scenario_id"], "provider": values["priced_provider"], "model": values["priced_model"]}), "values": values})
    return out


def _serving_upsert_dataset(
    session: Session,
    *,
    slug: str,
    title: str,
    description: str,
    family: str,
    source_kind: str,
    columns: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    extra: dict[str, Any],
    source_event: str,
    publish: bool,
    views: list[dict[str, Any]],
) -> dict[str, Any]:
    ensure_dataset_tables(session)
    dataset = session.query(Dataset).filter_by(slug=slug).first()
    created = dataset is None
    latest_published = None if created else latest_dataset_version(session, int(dataset.id), status="published")
    snapshot_digest = _serving_hash([row["values"] for row in rows])
    previous_digest = (latest_published.extra or {}).get("snapshot_digest") if isinstance(latest_published, DatasetVersion) else None
    changed = previous_digest != snapshot_digest
    dataset_extra = dict(extra)
    dataset_extra["snapshot_digest"] = snapshot_digest
    dataset_extra["updated_by"] = source_event
    if dataset is None:
        dataset = create_dataset(
            session,
            title=title,
            description=description,
            category="ai_infrastructure",
            family=family,
            status="active",
            source_kind=source_kind,
            slug=slug,
            columns=columns,
            rows=[],
            label=str(dataset_extra.get("observed_at") or utcnow().date()),
            extra=dataset_extra,
            source_event=source_event,
        )
    else:
        dataset.title = title
        dataset.description = description
        dataset.category = "ai_infrastructure"
        dataset.family = family
        dataset.source_kind = source_kind
        merged = dict(dataset.extra or {})
        merged.update(dataset_extra)
        dataset.extra = merged
        dataset.updated_at = utcnow()
        session.add(dataset)
        session.flush()
    replace_dataset_draft(
        session,
        dataset,
        columns=columns,
        rows=rows,
        label=str(dataset_extra.get("observed_at") or utcnow().date()),
        extra=dataset_extra,
        source_event=source_event,
    )
    for view in views:
        create_dataset_view(session, dataset, extra={"source": source_event}, **view)
    published_version_id = None
    if publish and changed:
        published = publish_dataset_snapshot(session, dataset, label=str(dataset_extra.get("observed_at") or utcnow().date()))
        published_version_id = int(published.id)
    session.flush()
    return {
        "dataset_id": int(dataset.id),
        "slug": dataset.slug,
        "created": created,
        "changed": changed,
        "rows": len(rows),
        "columns": len(columns),
        "published_version_id": published_version_id,
        "snapshot_digest": snapshot_digest,
    }


def sync_serving_assumptions_dataset(session: Session, *, publish: bool = True) -> dict[str, Any]:
    existing, rows, extra = _serving_active_rows(session, SERVING_ASSUMPTIONS_DATASET_SLUG)
    if existing is not None:
        # Sync owns initial defaults, never an operator's saved draft or snapshot.
        return {"dataset_id": int(existing.id), "slug": existing.slug, "created": False,
                "changed": False, "rows": len(rows), "preserved_operator_assumptions": True,
                "published_version_id": None, "source_dataset_version_id": extra.get("source_dataset_version_id")}
    return _serving_upsert_dataset(
        session,
        slug=SERVING_ASSUMPTIONS_DATASET_SLUG,
        title="AI Serving Scenario Assumptions",
        description="Editable scenario assumptions for utilization, cache hit rate, token mix, price realization, power, PUE, and capex gaps.",
        family="ai_serving_economics",
        source_kind="operator_assumption_grid",
        columns=serving_assumption_columns(),
        rows=default_serving_assumption_rows(),
        extra={
            "observed_at": utcnow().replace(microsecond=0).isoformat(),
            "provenance_summary": "Operator assumption grid. Utilization and cache hit rate are not observed facts.",
            "open_gaps": ["GPU/server capex or rental economics", "realized utilization", "customer/workload cache hit rate", "cloud price realization"],
        },
        source_event="ai_serving_assumptions_sync",
        publish=publish,
        views=[
            {
                "name": "Assumption Grid",
                "slug": "assumption-grid",
                "description": "Scenario assumptions that should be edited or replaced as sourced data arrives.",
                "filters": {},
                "columns": [col["key"] for col in serving_assumption_columns()],
                "sort_spec": [{"key": "scenario_id", "direction": "asc"}],
            }
        ],
    )


def sync_model_cost_quality_frontier_dataset(session: Session, *, publish: bool = True) -> dict[str, Any]:
    _aa_dataset, aa_rows, aa_extra = _serving_active_rows(session, "artificial-analysis-model-economics", limit=2000)
    rows = build_cost_quality_frontier_rows(aa_rows, observed_at=str(aa_extra.get("observed_at") or utcnow().replace(microsecond=0).isoformat()))
    return _serving_upsert_dataset(
        session,
        slug=MODEL_COST_QUALITY_FRONTIER_DATASET_SLUG,
        title="AI Model Cost / Quality Frontier",
        description="Artificial Analysis intelligence, coding, agentic, cost-per-task, and token-price frontier for model economics.",
        family="ai_model_economics",
        source_kind="derived_artificial_analysis_frontier",
        columns=cost_quality_frontier_columns(),
        rows=rows,
        extra={
            "observed_at": aa_extra.get("observed_at") or utcnow().replace(microsecond=0).isoformat(),
            "source_dataset_slugs": ["artificial-analysis-model-economics"],
            "provenance_summary": "Derived directly from Artificial Analysis model economics snapshots.",
        },
        source_event="ai_model_cost_quality_frontier_sync",
        publish=publish,
        views=[
            {
                "name": "Cost Quality Frontier",
                "slug": "cost-quality-frontier",
                "description": "Models ranked by intelligence and cost efficiency.",
                "filters": {},
                "columns": [
                    "creator",
                    "model",
                    "intelligence_index",
                    "intelligence_index_total_cost_usd",
                    "cost_per_intelligence_point_usd",
                    "blended_text_usd_per_mtok",
                    "frontier_score",
                    "median_output_tokens_per_second",
                ],
                "sort_spec": [{"key": "frontier_score", "direction": "desc"}],
            }
        ],
    )


def sync_serving_margin_scenarios_dataset(
    session: Session,
    *,
    publish: bool = True,
    benchmark_limit: int = 5000,
    price_limit: int = 1000,
) -> dict[str, Any]:
    assumptions_dataset = session.query(Dataset).filter_by(slug=SERVING_ASSUMPTIONS_DATASET_SLUG).first()
    if assumptions_dataset is None:
        sync_serving_assumptions_dataset(session, publish=publish)
    _assumptions_dataset, assumption_rows, assumptions_extra = _serving_active_rows(session, SERVING_ASSUMPTIONS_DATASET_SLUG, limit=100)
    _inferencex_dataset, inferencex_rows, inferencex_extra = _serving_active_rows(session, "inferencex-upstream-benchmarks", limit=max(1, benchmark_limit), benchmark_type="agentic_traces")
    _aa_dataset, aa_rows, aa_extra = _serving_active_rows(session, "artificial-analysis-model-economics", limit=2000)
    _provider_dataset, provider_rows, provider_extra = _serving_active_rows(session, "ai-provider-api-pricing", limit=500)
    price_rows = _serving_price_rows(aa_rows, provider_rows)
    observed_at = utcnow().replace(microsecond=0).isoformat()
    rows = build_serving_margin_rows(
        inferencex_rows,
        price_rows,
        assumption_rows,
        observed_at=observed_at,
        benchmark_limit=benchmark_limit,
        price_limit=price_limit,
    )
    return _serving_upsert_dataset(
        session,
        slug=SERVING_MARGIN_SCENARIOS_DATASET_SLUG,
        title="AI Serving Margin Scenarios",
        description="Measured AgentX recipes selected against P90 SLA, exact matching API-equivalent prices, explicit assumptions, and nullable all-in economics where TCO/royalty inputs are missing.",
        family="ai_serving_economics",
        source_kind="derived_serving_economics_model",
        columns=serving_margin_columns(),
        rows=rows,
        extra={
            "observed_at": observed_at,
            "benchmark_type": "agentic_traces",
            "price_join_policy": "exact_normalized_model",
            "price_join_identity_gaps": [{"source_model": r.get("source_model") or r.get("model"),
                                          "display_model": r.get("display_model"), "model_db_keys": r.get("model_db_keys"),
                                          "reason": "display_alias_not_unique"}
                                         for r in inferencex_rows if r.get("display_model") and r.get("display_model") != r.get("model")
                                         and not (isinstance(r.get("model_db_keys"), list) and len(r["model_db_keys"]) == 1)],
            "benchmark_rows_available": inferencex_extra.get("rows_available", len(inferencex_rows)),
            "coverage_complete": not any((inferencex_extra.get("truncated"), aa_extra.get("truncated"), provider_extra.get("truncated"), assumptions_extra.get("truncated"), len(price_rows) > price_limit)),
            "coverage": {
                "benchmark": {k: inferencex_extra.get(k) for k in ("rows_available", "rows_loaded", "truncated")},
                "aa_prices": {k: aa_extra.get(k) for k in ("rows_available", "rows_loaded", "truncated")},
                "provider_prices": {k: provider_extra.get(k) for k in ("rows_available", "rows_loaded", "truncated")},
                "price_rows_available": len(price_rows), "price_rows_evaluated": min(len(price_rows), price_limit),
                "price_truncated": len(price_rows) > price_limit,
            },
            "source_dataset_version_ids": {
                "inferencex": inferencex_extra.get("source_dataset_version_id"),
                "artificial_analysis": aa_extra.get("source_dataset_version_id"),
                "provider_pricing": provider_extra.get("source_dataset_version_id"),
                "assumptions": assumptions_extra.get("source_dataset_version_id"),
            },
            "priced_benchmark_models": sorted({row["values"]["benchmark_model"] for row in rows}),
            "source_dataset_slugs": [
                "inferencex-upstream-benchmarks",
                "artificial-analysis-model-economics",
                "ai-provider-api-pricing",
                SERVING_ASSUMPTIONS_DATASET_SLUG,
            ],
            "source_observed_at": {
                "inferencex": inferencex_extra.get("release_date") or inferencex_extra.get("observed_at"),
                "artificial_analysis": aa_extra.get("observed_at"),
                "provider_pricing": provider_extra.get("observed_at"),
                "assumptions": assumptions_extra.get("observed_at"),
            },
            "provenance_summary": "Derived scenario model. Utilization, cache hit rate, price realization, and capex are assumptions unless populated by sourced rows.",
            "open_gaps": ["realized utilization", "cache hit rate by workload", "GPU/server capex or rental rate", "provider discounts and promotions"],
        },
        source_event="ai_serving_margin_scenarios_sync",
        publish=publish,
        views=[
            {
                "name": "Margin Leaderboard",
                "slug": "margin-leaderboard",
                "description": "Scenario rows ranked by profit only where complete TCO and royalty inputs are available.",
                "filters": {},
                "columns": [
                    "scenario_id",
                    "hardware",
                    "framework",
                    "benchmark_model",
                    "priced_provider",
                    "priced_model",
                    "tput_per_gpu_tok_s",
                    "blended_revenue_usd_per_mtok",
                    "gross_revenue_usd_per_day_per_gpu",
                    "profit_usd_per_day_per_gpu",
                    "status",
                    "gaps",
                    "match_confidence",
                ],
                "sort_spec": [{"key": "profit_usd_per_day_per_gpu", "direction": "desc"}],
            },
            {
                "name": "Assumption Sensitivity",
                "slug": "assumption-sensitivity",
                "description": "Utilization, cache, and price-realization sensitivity by hardware and price basis.",
                "filters": {},
                "columns": [
                    "scenario_id",
                    "hardware",
                    "priced_provider",
                    "priced_model",
                    "utilization_rate",
                    "cache_hit_rate",
                    "price_realization_multiplier",
                    "gross_revenue_usd_per_day_per_gpu",
                    "profit_usd_per_day_per_gpu",
                ],
                "sort_spec": [{"key": "scenario_id", "direction": "asc"}],
            },
        ],
    )


def sync_ai_serving_economics_datasets(
    session: Session,
    *,
    publish: bool = True,
    benchmark_limit: int = 5000,
    price_limit: int = 1000,
) -> dict[str, Any]:
    assumptions = sync_serving_assumptions_dataset(session, publish=publish)
    frontier = sync_model_cost_quality_frontier_dataset(session, publish=publish)
    scenarios = sync_serving_margin_scenarios_dataset(
        session,
        publish=publish,
        benchmark_limit=benchmark_limit,
        price_limit=price_limit,
    )
    return {"assumptions": assumptions, "frontier": frontier, "scenarios": scenarios}
