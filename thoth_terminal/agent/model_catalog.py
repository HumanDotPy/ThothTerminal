from __future__ import annotations

import json
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import requests

from thoth_terminal.config import settings
from thoth_terminal.agent.model_policy import annotate_model_catalog_for_tasks


OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
NOUS_DEFAULT_BASE_URL = "https://inference-api.nousresearch.com/v1"
ARTIFICIAL_ANALYSIS_CACHE_TTL_S = 24 * 60 * 60
ARTIFICIAL_ANALYSIS_MODEL_LIMIT = 1_000
META_PRICING_SOURCE_URL = "https://dev.meta.ai/docs/pricing-rate-limits"
_KNOWN_TOKEN_PRICING = {
    ("meta", "muse-spark-1.3-contributor"): ("0.10", "0.002", "0.20", META_PRICING_SOURCE_URL),
    ("meta", "muse-spark-1.2-contributor"): ("0.10", "0.002", "0.20", META_PRICING_SOURCE_URL),
    ("meta", "muse-spark-1.3"): ("1.25", "0.15", "4.25", META_PRICING_SOURCE_URL),
    ("meta", "muse-spark-1.2"): ("1.25", "0.15", "4.25", META_PRICING_SOURCE_URL),
    ("deepseek", "deepseek-v4-flash"): ("0.14", "0.0028", "0.28", "DeepSeek pricing"),
}


def known_token_pricing(provider: str, model: str) -> dict[str, object] | None:
    """Public list-rate snapshot; no provider call or account-billing claim."""
    values = _KNOWN_TOKEN_PRICING.get((str(provider).lower(), str(model).lower()))
    if values is None:
        return None
    uncached, cached, output, source = values
    return {
        "uncached_input_per_million": Decimal(uncached),
        "cached_input_per_million": Decimal(cached),
        "output_per_million": Decimal(output),
        "source": source,
        "observed_on": "2026-09-26",
    }


def _decimal(value: object) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _per_million(value: object) -> Decimal | None:
    parsed = _decimal(value)
    if parsed is None:
        return None
    return parsed * Decimal("1000000")


def _money_label(value: Decimal | None) -> str:
    if value is None:
        return "n/a"
    if value == 0:
        return "free"
    if value < Decimal("0.001"):
        return f"${value:.6f}/M"
    if value < Decimal("0.01"):
        return f"${value:.4f}/M"
    if value < Decimal("1"):
        return f"${value:.3f}/M"
    return f"${value:.2f}/M"


def _price_float(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _is_zero(value: Decimal | None) -> bool:
    return value is not None and value == 0


def _string_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip().lower() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip().lower()]
    return []


def _model_modalities(row: dict[str, Any]) -> tuple[list[str], list[str]]:
    architecture = row.get("architecture") if isinstance(row.get("architecture"), dict) else {}
    modalities = row.get("modalities") if isinstance(row.get("modalities"), dict) else {}
    input_values = _string_list(architecture.get("input_modalities") or modalities.get("input_modalities"))
    output_values = _string_list(architecture.get("output_modalities") or modalities.get("output_modalities"))
    if not input_values:
        input_values = ["text"]
    if not output_values:
        output_values = ["text"]
    return input_values, output_values


def _media_kind(input_modalities: list[str], output_modalities: list[str]) -> str:
    values = set(input_modalities) | set(output_modalities)
    if "image" in values:
        return "image"
    if "video" in values:
        return "video"
    if "audio" in values or "speech" in values:
        return "audio"
    return "text"


def _context_tier(context_length: int | None) -> str:
    try:
        value = int(context_length or 0)
    except Exception:
        value = 0
    if value >= 1_000_000:
        return "1m+"
    if value >= 200_000:
        return "200k+"
    if value >= 100_000:
        return "100k+"
    if value >= 64_000:
        return "64k+"
    if value > 0:
        return "short"
    return "unknown"


def _float_or_none(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except Exception:
        return None


def _catalog_row(
    *,
    service: str,
    provider: str,
    model: str,
    model_label: str | None = None,
    input_per_million: Decimal | None,
    output_per_million: Decimal | None,
    cache_input_per_million: Decimal | None = None,
    context_length: int | None = None,
    auto: bool = False,
    source: str = "",
    note: str = "",
    supports_json: bool | None = None,
    input_modalities: list[str] | None = None,
    output_modalities: list[str] | None = None,
) -> dict[str, Any]:
    free = bool(_is_zero(input_per_million) and _is_zero(output_per_million))
    in_modalities = input_modalities or ["text"]
    out_modalities = output_modalities or ["text"]
    media_kind = _media_kind(in_modalities, out_modalities)
    return {
        "service": service,
        "provider": provider,
        "model": model,
        "model_label": model_label or model,
        "price_input": _money_label(input_per_million),
        "price_output": _money_label(output_per_million),
        "price_cache_input": _money_label(cache_input_per_million) if cache_input_per_million is not None else None,
        "input_per_million": _price_float(input_per_million),
        "output_per_million": _price_float(output_per_million),
        "cache_input_per_million": _price_float(cache_input_per_million),
        "price_unit": "USD / 1M tokens",
        "free": free,
        "auto": bool(auto),
        "context_length": context_length,
        "context_tier": _context_tier(context_length),
        "input_modalities": in_modalities,
        "output_modalities": out_modalities,
        "media_kind": media_kind,
        "text_output": "text" in out_modalities,
        "source": source,
        "note": note,
        "supports_json": supports_json,
    }


def _openrouter_headers() -> dict[str, str]:
    key = str(getattr(settings, "openrouter_api_key", None) or "").strip()
    headers = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _parse_context_length(row: dict[str, Any]) -> int | None:
    for key in ("context_length", "contextLength", "max_context_length"):
        value = row.get(key)
        try:
            return int(value) if value is not None else None
        except Exception:
            continue
    return None


def _openrouter_model_rows(*, timeout_s: float = 8.0, limit: int = 350) -> tuple[list[dict[str, Any]], str | None]:
    rows: list[dict[str, Any]] = []
    error: str | None = None
    try:
        resp = requests.get(
            OPENROUTER_MODELS_URL,
            headers=_openrouter_headers(),
            timeout=max(2.0, float(timeout_s or 8.0)),
        )
        if resp.status_code >= 400:
            error = f"OpenRouter HTTP {resp.status_code}"
        else:
            payload = resp.json()
            raw_rows = payload.get("data") if isinstance(payload, dict) else []
            if isinstance(raw_rows, list):
                for item in raw_rows[: max(1, int(limit or 350))]:
                    if not isinstance(item, dict):
                        continue
                    model_id = str(item.get("id") or "").strip()
                    if not model_id:
                        continue
                    pricing = item.get("pricing") if isinstance(item.get("pricing"), dict) else {}
                    input_price = _per_million(pricing.get("prompt"))
                    output_price = _per_million(pricing.get("completion"))
                    request_price = _per_million(pricing.get("request"))
                    if request_price == 0 and (input_price is None or output_price is None):
                        input_price = input_price if input_price is not None else Decimal("0")
                        output_price = output_price if output_price is not None else Decimal("0")
                    supported = item.get("supported_parameters")
                    supports_json = None
                    if isinstance(supported, list):
                        supports_json = "response_format" in supported or "structured_outputs" in supported
                    input_modalities, output_modalities = _model_modalities(item)
                    rows.append(
                        _catalog_row(
                            service="OpenRouter",
                            provider="openrouter",
                            model=model_id,
                            model_label=str(item.get("name") or model_id),
                            input_per_million=input_price,
                            output_per_million=output_price,
                            context_length=_parse_context_length(item),
                            source="OpenRouter /models",
                            note="free route" if model_id.endswith(":free") else "",
                            supports_json=supports_json,
                            input_modalities=input_modalities,
                            output_modalities=output_modalities,
                        )
                    )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    return rows, error


def _configured_openrouter_rows() -> list[dict[str, Any]]:
    fallback_models = [
        part.strip()
        for part in str(getattr(settings, "openrouter_free_fallback_models", "") or "").split(",")
        if part.strip()
    ]
    rows = [
        _catalog_row(
            service="OpenRouter",
            provider="openrouter",
            model="openrouter/free",
            model_label="Auto free route",
            input_per_million=Decimal("0"),
            output_per_million=Decimal("0"),
            auto=True,
            source="OpenRouter free router",
            note="free-only route; OpenRouter chooses an available free model",
            supports_json=True,
        )
    ]
    for model in fallback_models:
        rows.append(
            _catalog_row(
                service="OpenRouter",
                provider="openrouter",
                model=model,
                model_label=model,
                input_per_million=Decimal("0") if model.endswith(":free") else None,
                output_per_million=Decimal("0") if model.endswith(":free") else None,
                source="Thoth fallback list",
                note="free fallback" if model.endswith(":free") else "configured fallback",
                supports_json=True,
            )
        )
    return rows


def _deepseek_rows() -> list[dict[str, Any]]:
    pricing = known_token_pricing("deepseek", "deepseek-v4-flash")
    assert pricing is not None
    row = _catalog_row(
        service="DeepSeek",
        provider="deepseek",
        model="deepseek-v4-flash",
        model_label="DeepSeek V4 Flash 0731",
        input_per_million=pricing["uncached_input_per_million"],
        output_per_million=pricing["output_per_million"],
        cache_input_per_million=pricing["cached_input_per_million"],
        context_length=1_048_576,
        source="DeepSeek pricing",
        note="0731 checkpoint; thinking enabled at max effort",
        supports_json=True,
    )
    row.update(
        {
            "checkpoint": "0731",
            "max_output_tokens": 384_000,
            "reasoning_efforts": ["low", "high", "max"],
            "default_reasoning_effort": "max",
            "supports_responses_api": True,
            "supports_tool_calls": True,
            "supports_parallel_tool_calls": True,
        }
    )
    return [row]


def _meta_rows() -> list[dict[str, Any]]:
    model = str(getattr(settings, "meta_model", "muse-spark-1.3-contributor") or "").strip()
    if not model:
        return []
    pricing = known_token_pricing("meta", model)
    return [
        _catalog_row(
            service="Meta",
            provider="meta",
            model=model,
            model_label=model,
            input_per_million=pricing["uncached_input_per_million"] if pricing else None,
            output_per_million=pricing["output_per_million"] if pricing else None,
            cache_input_per_million=pricing["cached_input_per_million"] if pricing else None,
            context_length=1_048_576 if pricing else None,
            source=str(pricing["source"]) if pricing else "Meta Developer API configuration",
            note="Public token list rate; projected cost is not a provider invoice" if pricing else "Pricing unavailable",
            supports_json=True,
        )
    ]


def _nous_base_url() -> str:
    return str(getattr(settings, "nous_base_url", NOUS_DEFAULT_BASE_URL) or NOUS_DEFAULT_BASE_URL).strip().rstrip("/")


def _nous_headers() -> dict[str, str]:
    key = str(getattr(settings, "nous_api_key", None) or "").strip()
    headers = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _nous_model_rows(*, timeout_s: float = 8.0, limit: int = 100) -> tuple[list[dict[str, Any]], str | None]:
    rows: list[dict[str, Any]] = []
    error: str | None = None
    try:
        resp = requests.get(
            f"{_nous_base_url()}/models",
            headers=_nous_headers(),
            timeout=max(2.0, float(timeout_s or 8.0)),
        )
        if resp.status_code >= 400:
            error = f"Nous HTTP {resp.status_code}"
        else:
            payload = resp.json()
            raw_rows = payload.get("data") if isinstance(payload, dict) else []
            if isinstance(raw_rows, list):
                for item in raw_rows[: max(1, int(limit or 100))]:
                    if not isinstance(item, dict):
                        continue
                    model_id = str(item.get("id") or item.get("model") or "").strip()
                    if not model_id:
                        continue
                    pricing = item.get("pricing") if isinstance(item.get("pricing"), dict) else {}
                    input_price = _per_million(pricing.get("prompt") or pricing.get("input"))
                    output_price = _per_million(pricing.get("completion") or pricing.get("output"))
                    request_price = _per_million(pricing.get("request"))
                    if request_price == 0 and (input_price is None or output_price is None):
                        input_price = input_price if input_price is not None else Decimal("0")
                        output_price = output_price if output_price is not None else Decimal("0")
                    rows.append(
                        _catalog_row(
                            service="Nous",
                            provider="nous",
                            model=model_id,
                            model_label=str(item.get("name") or model_id),
                            input_per_million=input_price,
                            output_per_million=output_price,
                            context_length=_parse_context_length(item),
                            source="Nous /models",
                            note="portal free route" if input_price == 0 and output_price == 0 else "portal-priced model",
                            supports_json=True,
                        )
                    )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    return rows, error


def _configured_nous_rows() -> list[dict[str, Any]]:
    configured = str(getattr(settings, "nous_model", "Hermes-4.3-36B") or "Hermes-4.3-36B").strip()
    model_ids = [configured, "Hermes-4.3-36B", "Hermes-4-70B", "Hermes-4-405B"]
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for model in model_ids:
        if not model or model in seen:
            continue
        seen.add(model)
        rows.append(
            _catalog_row(
                service="Nous",
                provider="nous",
                model=model,
                model_label=model,
                input_per_million=None,
                output_per_million=None,
                context_length=128_000,
                source="Nous Portal API docs",
                note="OpenAI-compatible; pricing/free status comes from Nous portal when available",
                supports_json=True,
            )
        )
    return rows


def _dedupe_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (str(row.get("provider") or ""), str(row.get("model") or ""))
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = dict(row)
            continue
        if str(existing.get("source") or "").startswith("Thoth") and row.get("source") == "OpenRouter /models":
            merged = dict(row)
            merged["note"] = existing.get("note") or row.get("note")
            by_key[key] = merged
        elif row.get("source") == "Nous /models":
            merged = dict(row)
            merged["note"] = existing.get("note") or row.get("note")
            by_key[key] = merged
    return list(by_key.values())


def _artificial_analysis_base_url() -> str:
    return str(
        getattr(settings, "artificial_analysis_base_url", "https://artificialanalysis.ai/api/v2")
        or "https://artificialanalysis.ai/api/v2"
    ).strip().rstrip("/")


def _artificial_analysis_cache_path() -> Path:
    return Path(settings.runtime_state_dir) / "artificial-analysis-llm-models.json"


def _read_artificial_analysis_cache() -> dict[str, Any] | None:
    path = _artificial_analysis_cache_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    fetched_at = float(payload.get("fetched_at") or 0)
    if fetched_at <= 0 or time.time() - fetched_at > ARTIFICIAL_ANALYSIS_CACHE_TTL_S:
        return None
    return payload


def _write_artificial_analysis_cache(payload: dict[str, Any]) -> None:
    path = _artificial_analysis_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _artificial_analysis_model_rows(
    *, timeout_s: float = 8.0, limit: int | None = 500, force_refresh: bool = False,
) -> tuple[list[dict[str, Any]], str | None]:
    if not bool(getattr(settings, "artificial_analysis_enabled", True)):
        return [], "Artificial Analysis disabled"
    requested_limit = None if limit is None else max(1, int(limit or 500))
    cached = None if force_refresh else _read_artificial_analysis_cache()
    if cached is not None:
        data = cached.get("data")
        complete_cache = bool(cached.get("complete"))
        if isinstance(data, list) and (complete_cache or (requested_limit is not None and len(data) >= requested_limit)):
            return data[:requested_limit], None
    key = str(getattr(settings, "artificial_analysis_api_key", None) or "").strip()
    if not key:
        return [], "Artificial Analysis API key is not configured"
    rows: list[dict[str, Any]] = []
    error: str | None = None
    page = 1
    completed = False
    try:
        while requested_limit is None or len(rows) < requested_limit:
            resp = requests.get(
                f"{_artificial_analysis_base_url()}/language/models/free",
                headers={"x-api-key": key, "Accept": "application/json"},
                params={"page": page},
                timeout=max(2.0, float(timeout_s or 8.0)),
            )
            if resp.status_code >= 400:
                error = f"Artificial Analysis HTTP {resp.status_code}"
                break
            payload = resp.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, list):
                error = "Artificial Analysis returned invalid model data"
                break
            rows.extend([item for item in data if isinstance(item, dict)])
            pagination = payload.get("pagination") if isinstance(payload, dict) else {}
            has_more = bool(pagination.get("has_more")) if isinstance(pagination, dict) else False
            if not has_more:
                completed = True
                break
            if not data:
                error = "Artificial Analysis pagination returned an empty page with more pages remaining"
                break
            page += 1
        if rows and completed:
            _write_artificial_analysis_cache(
                {
                    "fetched_at": time.time(),
                    "source": "Artificial Analysis /language/models/free",
                    "complete": True,
                    "data": rows,
                }
            )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    return rows[:requested_limit], error


def _norm_model_name(value: object) -> str:
    text = str(value or "").strip().lower()
    keep = []
    for ch in text:
        keep.append(ch if ch.isalnum() else " ")
    return " ".join("".join(keep).split())


def _aa_indexes(
    rows: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    by_openrouter: dict[str, dict[str, Any]] = {}
    by_slug: dict[str, dict[str, Any]] = {}
    by_name: dict[str, dict[str, Any]] = {}
    for row in rows:
        openrouter_id = str(row.get("openrouter_api_id") or "").strip()
        if openrouter_id:
            by_openrouter[openrouter_id] = row
        slug = str(row.get("slug") or "").strip().lower()
        if slug:
            by_slug[slug] = row
        name = _norm_model_name(row.get("name"))
        creator = row.get("model_creator") if isinstance(row.get("model_creator"), dict) else {}
        creator_name = _norm_model_name(creator.get("name"))
        if name:
            by_name[name] = row
            if creator_name:
                by_name[f"{creator_name} {name}"] = row
    return by_openrouter, by_slug, by_name


def _aa_match(
    row: dict[str, Any],
    by_openrouter: dict[str, dict[str, Any]],
    by_slug: dict[str, dict[str, Any]],
    by_name: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    model = str(row.get("model") or "").strip()
    if model in by_openrouter:
        return by_openrouter[model]
    slug_candidates = [
        model.lower(),
        model.split("/", 1)[-1].replace(":free", "").lower(),
    ]
    for candidate in slug_candidates:
        if candidate in by_slug:
            return by_slug[candidate]
    candidates = [
        _norm_model_name(row.get("model_label")),
        _norm_model_name(model.split("/", 1)[-1].replace(":free", "")),
        _norm_model_name(model.replace("/", " ").replace(":free", "")),
    ]
    for candidate in candidates:
        if candidate and candidate in by_name:
            return by_name[candidate]
    return None


def _enrich_with_routing_profile(rows: list[dict[str, Any]], aa_rows: list[dict[str, Any]]) -> None:
    by_openrouter, by_slug, by_name = _aa_indexes(aa_rows)
    for row in rows:
        aa = _aa_match(row, by_openrouter, by_slug, by_name)
        evaluations = aa.get("evaluations") if isinstance(aa, dict) and isinstance(aa.get("evaluations"), dict) else {}
        intelligence = evaluations.get("artificial_analysis_intelligence_index")
        coding = evaluations.get("artificial_analysis_coding_index")
        agentic = evaluations.get("artificial_analysis_agentic_index")
        intelligence_score = _float_or_none(intelligence)
        row["artificial_analysis"] = {
            "matched": bool(aa),
            "source": "Artificial Analysis /language/models/free" if aa else None,
            "model_name": aa.get("name") if isinstance(aa, dict) else None,
            "slug": aa.get("slug") if isinstance(aa, dict) else None,
            "intelligence_index": intelligence_score,
            "coding_index": _float_or_none(coding),
            "agentic_index": _float_or_none(agentic),
        }
        row["intelligence_score"] = intelligence_score
        row["routing_bias"] = _routing_bias(row)
        row["routing_lanes"] = _routing_lanes(row)


def _routing_bias(row: dict[str, Any]) -> int:
    score = 0
    if row.get("free"):
        score += 100
    provider = str(row.get("provider") or "").lower()
    model = str(row.get("model") or "").lower()
    if provider == "deepseek" or "deepseek" in model:
        score += 40
    if str(row.get("context_tier") or "") in {"100k+", "200k+", "1m+"}:
        score += 25
    if row.get("intelligence_score") is not None:
        score += min(40, max(0, int(float(row["intelligence_score"]))))
    if not row.get("text_output"):
        score -= 500
    return score


def _routing_lanes(row: dict[str, Any]) -> list[str]:
    if not row.get("text_output"):
        return ["media_only"]
    lanes: list[str] = []
    if row.get("free"):
        lanes.append("free_opportunistic")
    if str(row.get("provider") or "").lower() == "deepseek" or "deepseek" in str(row.get("model") or "").lower():
        lanes.append("deepseek_preferred")
    if str(row.get("context_tier") or "") in {"100k+", "200k+", "1m+"}:
        lanes.append("long_context")
    score = row.get("intelligence_score")
    if score is not None and float(score) >= 40:
        lanes.append("high_intelligence")
    if not lanes:
        lanes.append("general_cloud")
    return lanes


def build_model_catalog(*, include_openrouter_live: bool = True, include_nous_live: bool = True, limit: int = 350) -> dict[str, Any]:
    selected_provider = str(getattr(settings, "ai_provider", "openrouter") or "openrouter").strip().lower()
    selected_model = {
        "openrouter": str(getattr(settings, "openrouter_model", "openrouter/free") or "openrouter/free"),
        "deepseek": str(getattr(settings, "deepseek_model", "deepseek-v4-flash") or "deepseek-v4-flash"),
        "meta": str(getattr(settings, "meta_model", "muse-spark-1.3-contributor") or "muse-spark-1.3-contributor"),
        "nous": str(getattr(settings, "nous_model", "Hermes-4.3-36B") or "Hermes-4.3-36B"),
        "openai": str(getattr(settings, "openai_model", "gpt-4o") or "gpt-4o"),
        "anthropic": str(getattr(settings, "anthropic_model", "") or ""),
        "local": str(getattr(settings, "agent_llm_model", "") or getattr(settings, "local_model_served_model_name", "") or ""),
    }.get(selected_provider, "")
    rows = [*_configured_openrouter_rows(), *_deepseek_rows(), *_meta_rows(), *_configured_nous_rows()]
    openrouter_error: str | None = None
    nous_error: str | None = None
    if include_openrouter_live:
        openrouter_rows, openrouter_error = _openrouter_model_rows(limit=limit)
        rows.extend(openrouter_rows)
    if include_nous_live:
        nous_rows, nous_error = _nous_model_rows(limit=limit)
        rows.extend(nous_rows)
    rows = _dedupe_rows(rows)
    # AA's catalog is larger than the OpenRouter picker limit. Fetch the full
    # benchmark catalog so newly appended checkpoints are not truncated away.
    aa_rows, artificial_analysis_error = _artificial_analysis_model_rows(
        limit=max(ARTIFICIAL_ANALYSIS_MODEL_LIMIT, int(limit or 0))
    )
    _enrich_with_routing_profile(rows, aa_rows)
    for row in rows:
        row["selected"] = row.get("provider") == selected_provider and row.get("model") == selected_model
        output_modalities = {
            str(item).strip().lower()
            for item in row.get("output_modalities", [])
            if str(item).strip()
        }
        row["usable"] = bool(
            row.get("provider") in {"openrouter", "deepseek", "meta", "nous"}
            and row.get("text_output")
            and output_modalities
            and "text" in output_modalities
            and not (output_modalities - {"text"})
        )
    annotate_model_catalog_for_tasks(rows)
    rows.sort(
        key=lambda row: (
            0 if row.get("selected") else 1,
            0 if row.get("provider") == "openrouter" and row.get("model") == "openrouter/free" else 1,
            0 if row.get("free") else 1,
            -int(row.get("routing_bias") or 0),
            str(row.get("service") or ""),
            str(row.get("model_label") or row.get("model") or ""),
        )
    )
    services: dict[str, dict[str, Any]] = {}
    for row in rows:
        service = str(row.get("service") or row.get("provider") or "unknown")
        entry = services.setdefault(service, {"service": service, "models": 0, "free": 0})
        entry["models"] += 1
        if row.get("free"):
            entry["free"] += 1
    return {
        "selected_provider": selected_provider,
        "selected_model": selected_model,
        "rows": rows,
        "services": list(services.values()),
        "openrouter_error": openrouter_error,
        "nous_error": nous_error,
        "artificial_analysis_error": artificial_analysis_error,
        "artificial_analysis_rows": len(aa_rows),
        "artificial_analysis_cache_ttl_s": ARTIFICIAL_ANALYSIS_CACHE_TTL_S,
        "artificial_analysis_source": "Artificial Analysis /language/models/free",
        "free_only": bool(getattr(settings, "openrouter_enforce_free_only", True)),
        "prefer_free": bool(getattr(settings, "openrouter_prefer_free_models", True)),
    }
