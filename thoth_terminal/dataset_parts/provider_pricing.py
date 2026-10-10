from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import datetime, timedelta
from typing import Any

import requests
from sqlalchemy.orm import Session

from thoth_terminal.dataset_parts.core import ensure_dataset_tables
from thoth_terminal.dataset_parts.mutations import (
    create_dataset,
    create_dataset_view,
    latest_dataset_version,
    publish_dataset_snapshot,
    replace_dataset_draft,
)
from thoth_terminal.db import utcnow
from thoth_terminal.models import Dataset, DatasetVersion


PROVIDER_PRICING_DATASET_SLUG = "ai-provider-api-pricing"
OPENAI_PRICING_URL = "https://openai.com/api/pricing/"
ANTHROPIC_PRICING_URL = "https://docs.anthropic.com/en/docs/about-claude/pricing"
DEFAULT_PROVIDER_PRICING_MIN_REFRESH_DAYS = 7


def _fetch_text(url: str, *, timeout_s: float = 30.0) -> str:
    response = requests.get(
        url,
        timeout=timeout_s,
        headers={"User-Agent": "ThothTerminal-ProviderPricing-Upstream"},
    )
    response.raise_for_status()
    return response.text


def _strip_tags(value: str) -> str:
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", value, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    return html.unescape(re.sub(r"\s+", " ", text)).strip()


def _source_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _price(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except Exception:
        return None


def _row_key(values: dict[str, Any]) -> str:
    basis = json.dumps(
        {
            "provider": values.get("provider"),
            "model": values.get("model"),
            "service_tier": values.get("service_tier"),
            "modality": values.get("modality"),
            "source_kind": values.get("source_kind"),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"provider-price:{hashlib.sha256(basis.encode('utf-8')).hexdigest()[:18]}"


def provider_pricing_columns() -> list[dict[str, Any]]:
    specs: list[tuple[str, str, str, str | None]] = [
        ("observed_at", "Observed At", "text", "timestamp"),
        ("provider", "Provider", "text", "dimension"),
        ("model", "Model", "text", "dimension"),
        ("model_family", "Model Family", "text", "dimension"),
        ("service_tier", "Service Tier", "text", "dimension"),
        ("modality", "Modality", "text", "dimension"),
        ("pricing_unit", "Pricing Unit", "text", "dimension"),
        ("input_usd_per_mtok", "Input USD / MTok", "number", "metric"),
        ("cached_input_usd_per_mtok", "Cached Input USD / MTok", "number", "metric"),
        ("cache_write_5m_usd_per_mtok", "5m Cache Write USD / MTok", "number", "metric"),
        ("cache_write_1h_usd_per_mtok", "1h Cache Write USD / MTok", "number", "metric"),
        ("cache_hit_usd_per_mtok", "Cache Hit USD / MTok", "number", "metric"),
        ("output_usd_per_mtok", "Output USD / MTok", "number", "metric"),
        ("batch_input_usd_per_mtok", "Batch Input USD / MTok", "number", "metric"),
        ("batch_output_usd_per_mtok", "Batch Output USD / MTok", "number", "metric"),
        ("fast_input_usd_per_mtok", "Fast Input USD / MTok", "number", "metric"),
        ("fast_output_usd_per_mtok", "Fast Output USD / MTok", "number", "metric"),
        ("data_residency_multiplier", "Data Residency Multiplier", "number", "metric"),
        ("context_window_tokens", "Context Window Tokens", "number", "metric"),
        ("source_kind", "Source Kind", "text", "dimension"),
        ("source_url", "Source URL", "text", "dimension"),
        ("source_hash", "Source Hash", "text", "dimension"),
        ("parse_confidence", "Parse Confidence", "number", "metric"),
        ("notes", "Notes", "text", "dimension"),
    ]
    return [{"key": key, "label": label, "data_type": data_type, "role": role, "config": {}} for key, label, data_type, role in specs]


def parse_openai_pricing_page(text: str, *, observed_at: str, source_url: str = OPENAI_PRICING_URL) -> list[dict[str, Any]]:
    page_hash = _source_hash(text)
    rows: list[dict[str, Any]] = []
    h2_matches = list(re.finditer(r"<h2[^>]*>(.*?)</h2>", text, flags=re.IGNORECASE | re.DOTALL))
    for idx, match in enumerate(h2_matches):
        model = _strip_tags(match.group(1))
        if not model or not model.lower().startswith(("gpt", "o1", "o3", "o4", "codex", "computer-use")):
            continue
        end = h2_matches[idx + 1].start() if idx + 1 < len(h2_matches) else len(text)
        segment = _strip_tags(text[match.end() : end])
        input_match = re.search(r"Input:\s*\$([0-9.]+)\s*/\s*1M tokens", segment, flags=re.IGNORECASE)
        cached_match = re.search(r"Cached input:\s*\$([0-9.]+)\s*/\s*1M tokens", segment, flags=re.IGNORECASE)
        output_match = re.search(r"Output:\s*\$([0-9.]+)\s*/\s*1M tokens", segment, flags=re.IGNORECASE)
        if not (input_match and output_match):
            continue
        values = {
            "observed_at": observed_at,
            "provider": "openai",
            "model": model,
            "model_family": model.split()[0] if " " in model else model.split("-")[0],
            "service_tier": "standard",
            "modality": "text",
            "pricing_unit": "USD per 1M tokens",
            "input_usd_per_mtok": _price(input_match.group(1)),
            "cached_input_usd_per_mtok": _price(cached_match.group(1) if cached_match else None),
            "cache_write_5m_usd_per_mtok": None,
            "cache_write_1h_usd_per_mtok": None,
            "cache_hit_usd_per_mtok": _price(cached_match.group(1) if cached_match else None),
            "output_usd_per_mtok": _price(output_match.group(1)),
            "batch_input_usd_per_mtok": _price(input_match.group(1)) * 0.5 if _price(input_match.group(1)) is not None else None,
            "batch_output_usd_per_mtok": _price(output_match.group(1)) * 0.5 if _price(output_match.group(1)) is not None else None,
            "fast_input_usd_per_mtok": None,
            "fast_output_usd_per_mtok": None,
            "data_residency_multiplier": 1.10,
            "context_window_tokens": 270000 if "Pricing above reflects standard processing rates for context lengths under 270K" in segment else None,
            "source_kind": "first_party_public_html",
            "source_url": source_url,
            "source_hash": page_hash,
            "parse_confidence": 0.82,
            "notes": "OpenAI public pricing page parser. Batch fields apply the page's stated 50% Batch API discount where token prices are available.",
        }
        rows.append({"row_key": _row_key(values), "values": values})
    return rows


def _anthropic_rows_from_table(
    text: str,
    *,
    observed_at: str,
    source_hash: str,
    source_url: str,
) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    normalized = _strip_tags(text)
    pattern = re.compile(
        r"(Claude\s+[A-Za-z0-9 .]+?)(?:\s*\([^)]*\))?"
        r"\$([0-9.]+)\s*/\s*MTok"
        r"\$([0-9.]+)\s*/\s*MTok"
        r"\$([0-9.]+)\s*/\s*MTok"
        r"\$([0-9.]+)\s*/\s*MTok"
        r"\$([0-9.]+)\s*/\s*MTok",
        flags=re.IGNORECASE,
    )
    for match in pattern.finditer(normalized):
        model = re.sub(r"\s+", " ", match.group(1)).strip()
        values = {
            "observed_at": observed_at,
            "provider": "anthropic",
            "model": model,
            "model_family": " ".join(model.split()[:2]) if len(model.split()) >= 2 else model,
            "service_tier": "standard",
            "modality": "text",
            "pricing_unit": "USD per 1M tokens",
            "input_usd_per_mtok": _price(match.group(2)),
            "cached_input_usd_per_mtok": _price(match.group(5)),
            "cache_write_5m_usd_per_mtok": _price(match.group(3)),
            "cache_write_1h_usd_per_mtok": _price(match.group(4)),
            "cache_hit_usd_per_mtok": _price(match.group(5)),
            "output_usd_per_mtok": _price(match.group(6)),
            "batch_input_usd_per_mtok": None,
            "batch_output_usd_per_mtok": None,
            "fast_input_usd_per_mtok": None,
            "fast_output_usd_per_mtok": None,
            "data_residency_multiplier": 1.10 if re.search(r"Opus 4\.[6-9]|Sonnet 4\.[6-9]|Fable|Mythos", model) else None,
            "context_window_tokens": 1000000 if re.search(r"Fable 5|Mythos|Opus 4\.[5-9]|Sonnet 4\.6", model) else None,
            "source_kind": "first_party_public_html",
            "source_url": source_url,
            "source_hash": source_hash,
            "parse_confidence": 0.90,
            "notes": "Anthropic pricing page parser. Cache write/read columns come from the model pricing table.",
        }
        rows[model.lower()] = values
    return rows


def _apply_anthropic_batch_table(values_by_model: dict[str, dict[str, Any]], text: str) -> None:
    normalized = _strip_tags(text)
    pattern = re.compile(
        r"(Claude\s+[A-Za-z0-9 .]+?)(?:\s*\([^)]*\))?"
        r"\$([0-9.]+)\s*/\s*MTok"
        r"\$([0-9.]+)\s*/\s*MTok",
        flags=re.IGNORECASE,
    )
    batch_start = normalized.find("Batch processing")
    if batch_start < 0:
        return
    batch_text = normalized[batch_start : batch_start + 4000]
    for match in pattern.finditer(batch_text):
        model = re.sub(r"\s+", " ", match.group(1)).strip().lower()
        values = values_by_model.get(model)
        if values is None:
            continue
        values["batch_input_usd_per_mtok"] = _price(match.group(2))
        values["batch_output_usd_per_mtok"] = _price(match.group(3))


def _apply_anthropic_fast_table(values_by_model: dict[str, dict[str, Any]], text: str) -> None:
    normalized = _strip_tags(text)
    fast_start = normalized.find("Fast mode pricing")
    if fast_start < 0:
        return
    fast_text = normalized[fast_start : fast_start + 1200]
    opus_46_47 = re.search(r"Claude Opus 4\.6\s*/\s*Claude Opus 4\.7\s*\$([0-9.]+)\s*/\s*MTok\s*\$([0-9.]+)\s*/\s*MTok", fast_text)
    if opus_46_47:
        for model in ("claude opus 4.6", "claude opus 4.7"):
            if model in values_by_model:
                values_by_model[model]["fast_input_usd_per_mtok"] = _price(opus_46_47.group(1))
                values_by_model[model]["fast_output_usd_per_mtok"] = _price(opus_46_47.group(2))
    opus_48 = re.search(r"Claude Opus 4\.8\s*\$([0-9.]+)\s*/\s*MTok\s*\$([0-9.]+)\s*/\s*MTok", fast_text)
    if opus_48 and "claude opus 4.8" in values_by_model:
        values_by_model["claude opus 4.8"]["fast_input_usd_per_mtok"] = _price(opus_48.group(1))
        values_by_model["claude opus 4.8"]["fast_output_usd_per_mtok"] = _price(opus_48.group(2))


def parse_anthropic_pricing_page(text: str, *, observed_at: str, source_url: str = ANTHROPIC_PRICING_URL) -> list[dict[str, Any]]:
    page_hash = _source_hash(text)
    values_by_model = _anthropic_rows_from_table(text, observed_at=observed_at, source_hash=page_hash, source_url=source_url)
    _apply_anthropic_batch_table(values_by_model, text)
    _apply_anthropic_fast_table(values_by_model, text)
    return [{"row_key": _row_key(values), "values": values} for values in values_by_model.values()]


def fetch_provider_pricing_rows(*, observed_at: str | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    observed = observed_at or utcnow().replace(microsecond=0).isoformat()
    source_payloads = {
        "openai": _fetch_text(OPENAI_PRICING_URL),
        "anthropic": _fetch_text(ANTHROPIC_PRICING_URL),
    }
    rows = [
        *parse_openai_pricing_page(source_payloads["openai"], observed_at=observed),
        *parse_anthropic_pricing_page(source_payloads["anthropic"], observed_at=observed),
    ]
    source_hashes = {provider: _source_hash(text) for provider, text in source_payloads.items()}
    snapshot_digest = _source_hash(json.dumps([row["values"] for row in rows], sort_keys=True, default=str))
    return rows, {
        "observed_at": observed,
        "source_hashes": source_hashes,
        "snapshot_digest": snapshot_digest,
        "providers": sorted(source_payloads),
        "row_count": len(rows),
    }


def _parse_observed_at(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def _fresh_enough(session: Session, *, min_refresh_days: int) -> tuple[bool, dict[str, Any]]:
    dataset = session.query(Dataset).filter_by(slug=PROVIDER_PRICING_DATASET_SLUG).first()
    if dataset is None:
        return False, {}
    latest = latest_dataset_version(session, int(dataset.id), status="published")
    if latest is None:
        return False, {}
    extra = dict(latest.extra or {})
    observed_at = _parse_observed_at(extra.get("observed_at"))
    if observed_at is None:
        return False, extra
    age = utcnow().replace(tzinfo=None) - observed_at
    return age < timedelta(days=max(1, int(min_refresh_days))), extra


def upsert_provider_pricing_dataset(
    session: Session,
    *,
    rows: list[dict[str, Any]],
    summary: dict[str, Any],
    publish: bool = True,
) -> dict[str, Any]:
    ensure_dataset_tables(session)
    columns = provider_pricing_columns()
    extra = {
        "source": "Provider public API pricing pages",
        "source_owner": "OpenAI; Anthropic",
        "source_urls": [OPENAI_PRICING_URL, ANTHROPIC_PRICING_URL],
        "upstream_kind": "first_party_public_pricing_pages",
        "observed_at": summary.get("observed_at"),
        "source_hashes": dict(summary.get("source_hashes") or {}),
        "snapshot_digest": summary.get("snapshot_digest"),
        "updated_by": "provider_pricing_sync",
        "refresh_policy": f"at most once every {DEFAULT_PROVIDER_PRICING_MIN_REFRESH_DAYS} days unless forced",
    }
    dataset = session.query(Dataset).filter_by(slug=PROVIDER_PRICING_DATASET_SLUG).first()
    created = dataset is None
    latest_published = None if created else latest_dataset_version(session, int(dataset.id), status="published")
    previous_digest = (latest_published.extra or {}).get("snapshot_digest") if isinstance(latest_published, DatasetVersion) else None
    changed = previous_digest != summary.get("snapshot_digest")
    if dataset is None:
        dataset = create_dataset(
            session,
            title="AI Provider API Pricing",
            description="Public first-party API model pricing normalized for AI serving economics models.",
            category="ai_infrastructure",
            family="ai_provider_pricing",
            status="active",
            source_kind="provider_public_pricing",
            slug=PROVIDER_PRICING_DATASET_SLUG,
            columns=columns,
            rows=[],
            label="Provider pricing upstream shell",
            extra=extra,
            source_event="provider_pricing_sync",
        )
    else:
        dataset.title = "AI Provider API Pricing"
        dataset.description = "Public first-party API model pricing normalized for AI serving economics models."
        dataset.category = "ai_infrastructure"
        dataset.family = "ai_provider_pricing"
        dataset.source_kind = "provider_public_pricing"
        merged = dict(dataset.extra or {})
        merged.update(extra)
        dataset.extra = merged
        dataset.updated_at = utcnow()
        session.add(dataset)
        session.flush()
    replace_dataset_draft(
        session,
        dataset,
        columns=columns,
        rows=rows,
        label=str(summary.get("observed_at") or "provider pricing snapshot"),
        extra=extra,
        source_event="provider_pricing_sync",
    )
    create_dataset_view(
        session,
        dataset,
        name="Token Pricing",
        slug="token-pricing",
        description="Input, cache, output, batch, and fast-mode token prices by provider and model.",
        filters={},
        columns=[
            "observed_at",
            "provider",
            "model",
            "service_tier",
            "input_usd_per_mtok",
            "cached_input_usd_per_mtok",
            "cache_write_5m_usd_per_mtok",
            "cache_hit_usd_per_mtok",
            "output_usd_per_mtok",
            "batch_input_usd_per_mtok",
            "batch_output_usd_per_mtok",
            "fast_input_usd_per_mtok",
            "fast_output_usd_per_mtok",
            "source_url",
        ],
        sort_spec=[{"key": "provider", "direction": "asc"}, {"key": "input_usd_per_mtok", "direction": "desc"}],
        extra={"source": "provider_pricing_sync"},
    )
    published_version_id = None
    if publish and changed:
        published = publish_dataset_snapshot(session, dataset, label=str(summary.get("observed_at") or "provider pricing snapshot"))
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
        "snapshot_digest": summary.get("snapshot_digest"),
        "observed_at": summary.get("observed_at"),
    }


def sync_provider_pricing_dataset(
    session: Session,
    *,
    force: bool = False,
    min_refresh_days: int = DEFAULT_PROVIDER_PRICING_MIN_REFRESH_DAYS,
    publish: bool = True,
) -> dict[str, Any]:
    fresh, previous = _fresh_enough(session, min_refresh_days=min_refresh_days)
    if fresh and not force:
        dataset = session.query(Dataset).filter_by(slug=PROVIDER_PRICING_DATASET_SLUG).first()
        return {
            "skipped": True,
            "reason": f"latest published pricing snapshot is newer than {max(1, int(min_refresh_days))} days",
            "dataset_id": int(dataset.id) if dataset is not None else None,
            "slug": PROVIDER_PRICING_DATASET_SLUG,
            "observed_at": previous.get("observed_at"),
            "snapshot_digest": previous.get("snapshot_digest"),
        }
    rows, summary = fetch_provider_pricing_rows()
    result = upsert_provider_pricing_dataset(session, rows=rows, summary=summary, publish=publish)
    result["skipped"] = False
    return result
