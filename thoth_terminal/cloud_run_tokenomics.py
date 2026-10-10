"""Compact, provenance-aware token accounting for cloud research dispatches."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from thoth_terminal.agent.model_catalog import known_token_pricing


def _count(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _reported_cost(value: object) -> Decimal | None:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return amount if amount.is_finite() and amount >= 0 else None


def cloud_run_tokenomics(stats: dict[str, Any], *, provider: str, model: str) -> dict[str, Any]:
    """Use reported usage and public rates; leave unpriced or mixed runs unresolved."""
    values = dict(stats or {})
    if (dict(values.get("usage") or {}).get("status") == "unavailable"
            and not any(_count(values.get(key)) for key in (
                "input_tokens", "output_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"))):
        values = {key: value for key, value in values.items() if key not in {
            "input_tokens", "output_tokens", "total_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"}}
    cached = _count(values.get("prompt_cache_hit_tokens"))
    uncached = _count(values.get("prompt_cache_miss_tokens"))
    input_total = _count(values.get("input_tokens"))
    output = _count(values.get("output_tokens"))
    if cached is None and uncached is not None and input_total is not None and uncached <= input_total:
        cached = input_total - uncached
    if uncached is None and cached is not None and input_total is not None and cached <= input_total:
        uncached = input_total - cached
    total = _count(values.get("total_tokens"))
    if total is None and input_total is not None and output is not None:
        total = input_total + output
    reported = _reported_cost(values.get("reported_cost_usd")) if values.get("reported_cost_usd") is not None else None
    price = known_token_pricing(provider, model)
    estimate = None
    if reported is None and price is not None and all(value is not None for value in (uncached, cached, output)):
        # Aggregate tokens may span several models after compaction; a single
        # routed model's list price is valid only for one observed model call.
        calls = _count(values.get("llm_calls"))
        if calls is None or calls == 1:
            estimate = (
                Decimal(uncached) * price["uncached_input_per_million"]
                + Decimal(cached) * price["cached_input_per_million"]
                + Decimal(output) * price["output_per_million"]
            ) / Decimal(1_000_000)
    return {
        "uncached_input_tokens": uncached, "cached_input_tokens": cached,
        "output_tokens": output, "total_tokens": total,
        "spend_usd": reported if reported is not None else estimate,
        "spend_basis": "provider_reported" if reported is not None else "public_list_rate_estimate" if estimate is not None else "unavailable",
        "pricing_source": price["source"] if estimate is not None else None,
        "pricing_observed_on": price["observed_on"] if estimate is not None else None,
    }


def discord_tokenomics_footer(stats: dict[str, Any], *, provider: str, model: str) -> str:
    usage = cloud_run_tokenomics(stats, provider=provider, model=model)
    chunks: list[str] = []
    if all(usage[key] is not None for key in ("uncached_input_tokens", "cached_input_tokens", "output_tokens")):
        chunks.extend((
            f"Uncached {usage['uncached_input_tokens']:,}",
            f"Cached {usage['cached_input_tokens']:,}",
            f"Output {usage['output_tokens']:,}",
        ))
    elif usage["total_tokens"] is not None:
        chunks.append("Token split unreported")
    if usage["total_tokens"] is not None:
        chunks.append(f"Total {usage['total_tokens']:,}")
    if usage["spend_basis"] == "provider_reported":
        chunks.append(f"Reported ${usage['spend_usd']:.4f}")
    elif usage["spend_basis"] == "public_list_rate_estimate":
        chunks.append(f"Est. token spend ${usage['spend_usd']:.4f}")
    elif chunks:
        chunks.append("Spend unavailable")
    return " · ".join(chunks)
