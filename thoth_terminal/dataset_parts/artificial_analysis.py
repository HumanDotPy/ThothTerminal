from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from thoth_terminal.agent.model_catalog import _artificial_analysis_model_rows
from thoth_terminal.dataset_parts.core import ensure_dataset_tables
from thoth_terminal.dataset_parts.mutations import (
    create_dataset,
    create_dataset_view,
    latest_dataset_version,
    publish_dataset_snapshot,
    replace_dataset_draft,
)
from thoth_terminal.config import settings
from thoth_terminal.db import session_factory, session_scope, utcnow
from thoth_terminal.jobs import enqueue_job
from thoth_terminal.models import Dataset, DatasetVersion, Job


ARTIFICIAL_ANALYSIS_DATASET_SLUG = "artificial-analysis-model-economics"
ARTIFICIAL_ANALYSIS_PUBLIC_URL = "https://artificialanalysis.ai/#price-and-cost"
ARTIFICIAL_ANALYSIS_API_SOURCE = "https://artificialanalysis.ai/api/v2/language/models/free"
DEFAULT_ARTIFICIAL_ANALYSIS_MIN_REFRESH_DAYS = 1
ARTIFICIAL_ANALYSIS_SYNC_JOB = "artificial_analysis_sync"


def _aa_source_hash(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _aa_number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except Exception:
        return None


def _aa_row_key(values: dict[str, Any]) -> str:
    basis = json.dumps(
        {
            "aa_model_id": values.get("aa_model_id"),
            "slug": values.get("slug"),
            "model": values.get("model"),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"artificial-analysis:{hashlib.sha256(basis.encode('utf-8')).hexdigest()[:18]}"


def artificial_analysis_columns() -> list[dict[str, Any]]:
    specs: list[tuple[str, str, str, str | None]] = [
        ("observed_at", "Observed At", "text", "timestamp"),
        ("aa_model_id", "AA Model ID", "text", "dimension"),
        ("slug", "Slug", "text", "dimension"),
        ("model", "Model", "text", "dimension"),
        ("creator", "Creator", "text", "dimension"),
        ("release_date", "Release Date", "text", "dimension"),
        ("openrouter_api_id", "OpenRouter API ID", "text", "dimension"),
        ("intelligence_index_version", "Intelligence Index Version", "number", "metric"),
        ("artificial_analysis_intelligence_index", "AA Intelligence Index", "number", "metric"),
        ("artificial_analysis_coding_index", "AA Coding Index", "number", "metric"),
        ("artificial_analysis_agentic_index", "AA Agentic Index", "number", "metric"),
        ("intelligence_index_total_cost_usd", "Intelligence Index Cost USD", "number", "metric"),
        ("intelligence_index_input_cost_usd", "Intelligence Index Input Cost USD", "number", "metric"),
        ("intelligence_index_reasoning_cost_usd", "Intelligence Index Reasoning Cost USD", "number", "metric"),
        ("intelligence_index_answer_cost_usd", "Intelligence Index Answer Cost USD", "number", "metric"),
        ("input_usd_per_mtok", "Input USD / MTok", "number", "metric"),
        ("output_usd_per_mtok", "Output USD / MTok", "number", "metric"),
        ("cache_hit_usd_per_mtok", "Cache Hit USD / MTok", "number", "metric"),
        ("cache_write_usd_per_mtok", "Cache Write USD / MTok", "number", "metric"),
        ("median_output_tokens_per_second", "Median Output tok/s", "number", "metric"),
        ("median_time_to_first_token_seconds", "Median TTFT Seconds", "number", "metric"),
        ("median_time_to_first_answer_token_seconds", "Median First Answer Token Seconds", "number", "metric"),
        ("median_end_to_end_response_time_seconds", "Median E2E Response Seconds", "number", "metric"),
        ("context_window_tokens", "Context Window Tokens", "number", "metric"),
        ("reasoning_model", "Reasoning Model", "boolean", "dimension"),
        ("evaluations", "Evaluations JSON", "json", "dimension"),
        ("pricing", "Pricing JSON", "json", "dimension"),
        ("performance", "Performance JSON", "json", "dimension"),
        ("raw_model", "Raw Model JSON", "json", "dimension"),
        ("source_kind", "Source Kind", "text", "dimension"),
        ("source_url", "Source URL", "text", "dimension"),
        ("api_url", "API URL", "text", "dimension"),
        ("source_hash", "Source Hash", "text", "dimension"),
    ]
    return [{"key": key, "label": label, "data_type": data_type, "role": role, "config": {}} for key, label, data_type, role in specs]


def normalize_artificial_analysis_rows(
    upstream_rows: list[dict[str, Any]],
    *,
    observed_at: str,
    intelligence_index_version: float | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in upstream_rows:
        if not isinstance(row, dict):
            continue
        creator = row.get("model_creator") if isinstance(row.get("model_creator"), dict) else {}
        evaluations = row.get("evaluations") if isinstance(row.get("evaluations"), dict) else {}
        costs = row.get("artificial_analysis_intelligence_index_cost")
        costs = costs if isinstance(costs, dict) else {}
        pricing = row.get("pricing") if isinstance(row.get("pricing"), dict) else {}
        performance = row.get("performance") if isinstance(row.get("performance"), dict) else {}
        values = {
            "observed_at": observed_at,
            "aa_model_id": str(row.get("id") or ""),
            "slug": str(row.get("slug") or ""),
            "model": str(row.get("name") or ""),
            "creator": str(creator.get("name") or row.get("creator") or ""),
            "release_date": row.get("release_date"),
            "openrouter_api_id": row.get("openrouter_api_id"),
            "intelligence_index_version": _aa_number(row.get("intelligence_index_version") or intelligence_index_version),
            "artificial_analysis_intelligence_index": _aa_number(evaluations.get("artificial_analysis_intelligence_index")),
            "artificial_analysis_coding_index": _aa_number(evaluations.get("artificial_analysis_coding_index")),
            "artificial_analysis_agentic_index": _aa_number(evaluations.get("artificial_analysis_agentic_index")),
            "intelligence_index_total_cost_usd": _aa_number(costs.get("total_cost")),
            "intelligence_index_input_cost_usd": _aa_number(costs.get("input_cost")),
            "intelligence_index_reasoning_cost_usd": _aa_number(costs.get("reasoning_cost")),
            "intelligence_index_answer_cost_usd": _aa_number(costs.get("answer_cost")),
            "input_usd_per_mtok": _aa_number(pricing.get("price_1m_input_tokens")),
            "output_usd_per_mtok": _aa_number(pricing.get("price_1m_output_tokens")),
            "cache_hit_usd_per_mtok": _aa_number(pricing.get("price_1m_cache_hit_tokens")),
            "cache_write_usd_per_mtok": _aa_number(pricing.get("price_1m_cache_write_tokens")),
            "median_output_tokens_per_second": _aa_number(performance.get("median_output_tokens_per_second")),
            "median_time_to_first_token_seconds": _aa_number(performance.get("median_time_to_first_token_seconds")),
            "median_time_to_first_answer_token_seconds": _aa_number(performance.get("median_time_to_first_answer_token_seconds")),
            "median_end_to_end_response_time_seconds": _aa_number(performance.get("median_end_to_end_response_time_seconds")),
            "context_window_tokens": _aa_number(row.get("context_window_tokens")),
            "reasoning_model": row.get("reasoning_model"),
            "evaluations": evaluations,
            "pricing": pricing,
            "performance": performance,
            "raw_model": row,
            "source_kind": "artificial_analysis_api_v2_free",
            "source_url": ARTIFICIAL_ANALYSIS_PUBLIC_URL,
            "api_url": ARTIFICIAL_ANALYSIS_API_SOURCE,
            "source_hash": _aa_source_hash(row),
        }
        rows.append({"row_key": _aa_row_key(values), "values": values})
    return rows


def _aa_parse_observed_at(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed
    except Exception:
        return None


def _aa_fresh_enough(session: Session, *, min_refresh_days: int) -> tuple[bool, dict[str, Any]]:
    dataset = session.query(Dataset).filter_by(slug=ARTIFICIAL_ANALYSIS_DATASET_SLUG).first()
    if dataset is None:
        return False, {}
    latest = latest_dataset_version(session, int(dataset.id), status="published")
    if latest is None:
        return False, {}
    extra = dict(latest.extra or {})
    observed_at = _aa_parse_observed_at((dataset.extra or {}).get("last_successful_check_at") or extra.get("observed_at"))
    if observed_at is None:
        return False, extra
    age = utcnow().replace(tzinfo=None) - observed_at
    return age < timedelta(days=max(1, int(min_refresh_days))), extra


def fetch_artificial_analysis_rows(*, observed_at: str | None = None, limit: int | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    observed = observed_at or utcnow().replace(microsecond=0).isoformat()
    upstream_rows, error = _artificial_analysis_model_rows(timeout_s=15.0, limit=limit, force_refresh=True)
    if error:
        raise RuntimeError(error)
    if not upstream_rows:
        raise RuntimeError("Artificial Analysis returned no models; retaining the previous snapshot")
    version_values = [
        _aa_number(row.get("intelligence_index_version"))
        for row in upstream_rows
        if isinstance(row, dict) and row.get("intelligence_index_version") is not None
    ]
    intelligence_version = version_values[0] if version_values else None
    rows = normalize_artificial_analysis_rows(upstream_rows, observed_at=observed, intelligence_index_version=intelligence_version)
    # Observation time and upstream ordering do not change the source content.
    snapshot_digest = _aa_source_hash(sorted(row["values"]["source_hash"] for row in rows))
    return rows, {
        "observed_at": observed,
        "snapshot_digest": snapshot_digest,
        "row_count": len(rows),
        "upstream_error": error,
        "intelligence_index_version": intelligence_version,
        "api_url": ARTIFICIAL_ANALYSIS_API_SOURCE,
        "source_url": ARTIFICIAL_ANALYSIS_PUBLIC_URL,
    }


def upsert_artificial_analysis_dataset(
    session: Session,
    *,
    rows: list[dict[str, Any]],
    summary: dict[str, Any],
    publish: bool = True,
    min_refresh_days: int = DEFAULT_ARTIFICIAL_ANALYSIS_MIN_REFRESH_DAYS,
) -> dict[str, Any]:
    ensure_dataset_tables(session)
    columns = artificial_analysis_columns()
    refresh_days = max(1, int(min_refresh_days))
    refresh_day_unit = "day" if refresh_days == 1 else "days"
    extra = {
        "source": "Artificial Analysis Data API",
        "source_owner": "Artificial Analysis",
        "source_urls": [ARTIFICIAL_ANALYSIS_PUBLIC_URL, ARTIFICIAL_ANALYSIS_API_SOURCE],
        "upstream_kind": "artificial_analysis_api_v2_free",
        "observed_at": summary.get("observed_at"),
        "snapshot_digest": summary.get("snapshot_digest"),
        "intelligence_index_version": summary.get("intelligence_index_version"),
        "upstream_error": summary.get("upstream_error"),
        "updated_by": "artificial_analysis_sync",
        "refresh_policy": f"at most once every {refresh_days} {refresh_day_unit} unless forced",
    }
    dataset = session.query(Dataset).filter_by(slug=ARTIFICIAL_ANALYSIS_DATASET_SLUG).first()
    created = dataset is None
    latest_published = None if created else latest_dataset_version(session, int(dataset.id), status="published")
    previous_digest = (latest_published.extra or {}).get("snapshot_digest") if isinstance(latest_published, DatasetVersion) else None
    changed = previous_digest != summary.get("snapshot_digest")
    if dataset is None:
        dataset = create_dataset(
            session,
            title="Artificial Analysis Model Economics",
            description="Artificial Analysis model benchmark, pricing, and cost-per-task snapshots for AI serving economics.",
            category="ai_infrastructure",
            family="ai_model_economics",
            status="active",
            source_kind="artificial_analysis_api",
            slug=ARTIFICIAL_ANALYSIS_DATASET_SLUG,
            columns=columns,
            rows=[],
            label="Artificial Analysis upstream shell",
            extra=extra,
            source_event="artificial_analysis_sync",
        )
    else:
        dataset.title = "Artificial Analysis Model Economics"
        dataset.description = "Artificial Analysis model benchmark, pricing, and cost-per-task snapshots for AI serving economics."
        dataset.category = "ai_infrastructure"
        dataset.family = "ai_model_economics"
        dataset.source_kind = "artificial_analysis_api"
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
        label=str(summary.get("observed_at") or "artificial analysis snapshot"),
        extra=extra,
        source_event="artificial_analysis_sync",
    )
    create_dataset_view(
        session,
        dataset,
        name="Cost vs Intelligence",
        slug="cost-vs-intelligence",
        description="Headline model quality and Artificial Analysis Intelligence Index cost by model.",
        filters={},
        columns=[
            "observed_at",
            "creator",
            "model",
            "artificial_analysis_intelligence_index",
            "artificial_analysis_coding_index",
            "artificial_analysis_agentic_index",
            "intelligence_index_total_cost_usd",
            "input_usd_per_mtok",
            "output_usd_per_mtok",
            "median_output_tokens_per_second",
            "source_url",
        ],
        sort_spec=[{"key": "artificial_analysis_intelligence_index", "direction": "desc"}],
        extra={"source": "artificial_analysis_sync"},
    )
    create_dataset_view(
        session,
        dataset,
        name="Evaluation Breakdown",
        slug="evaluation-breakdown",
        description="Raw Artificial Analysis evaluation, pricing, and performance objects for deeper model comparisons.",
        filters={},
        columns=["observed_at", "creator", "model", "slug", "evaluations", "pricing", "performance", "raw_model"],
        sort_spec=[{"key": "creator", "direction": "asc"}, {"key": "model", "direction": "asc"}],
        extra={"source": "artificial_analysis_sync"},
    )
    published_version_id = None
    if publish and changed:
        published = publish_dataset_snapshot(session, dataset, label=str(summary.get("observed_at") or "artificial analysis snapshot"))
        published_version_id = int(published.id)
    if publish:
        dataset.extra = {**dict(dataset.extra or {}), "last_successful_check_at": summary.get("observed_at")}
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


def sync_artificial_analysis_dataset(
    session: Session,
    *,
    force: bool = False,
    min_refresh_days: int = DEFAULT_ARTIFICIAL_ANALYSIS_MIN_REFRESH_DAYS,
    publish: bool = True,
    limit: int | None = None,
) -> dict[str, Any]:
    fresh, previous = _aa_fresh_enough(session, min_refresh_days=min_refresh_days)
    if fresh and not force:
        dataset = session.query(Dataset).filter_by(slug=ARTIFICIAL_ANALYSIS_DATASET_SLUG).first()
        return {
            "skipped": True,
            "reason": f"Artificial Analysis was successfully checked within {max(1, int(min_refresh_days))} days",
            "dataset_id": int(dataset.id) if dataset is not None else None,
            "slug": ARTIFICIAL_ANALYSIS_DATASET_SLUG,
            "observed_at": previous.get("observed_at"),
            "snapshot_digest": previous.get("snapshot_digest"),
        }
    rows, summary = fetch_artificial_analysis_rows(limit=limit)
    result = upsert_artificial_analysis_dataset(session, rows=rows, summary=summary, publish=publish, min_refresh_days=min_refresh_days)
    result["skipped"] = False
    return result


def enqueue_artificial_analysis_sync_job(session: Session) -> Job | None:
    """Maintain one durable daily refresh, preserving worker retry backoff."""
    if not settings.artificial_analysis_enabled or not settings.artificial_analysis_api_key:
        return None
    # Scheduler and manual callers share the queue; serialize this dedupe on Postgres.
    if session.get_bind().dialect.name == "postgresql":
        from sqlalchemy import text
        session.execute(text("SELECT pg_advisory_xact_lock(741206, 1)"))
    active = session.query(Job).filter(
        Job.dedupe_key == ARTIFICIAL_ANALYSIS_SYNC_JOB,
        Job.status.in_(["pending", "running"]),
    ).first()
    if active is not None:
        return active
    dataset = session.query(Dataset).filter_by(slug=ARTIFICIAL_ANALYSIS_DATASET_SLUG).first()
    checked_at = None
    if dataset is not None:
        published = latest_dataset_version(session, int(dataset.id), status="published")
        if published is not None:
            checked_at = _aa_parse_observed_at(
                (dataset.extra or {}).get("last_successful_check_at") or (published.extra or {}).get("observed_at")
            )
    run_after = max(utcnow(), checked_at + timedelta(days=1)) if checked_at else utcnow()
    failed = session.query(Job).filter_by(kind=ARTIFICIAL_ANALYSIS_SYNC_JOB, status="error").order_by(Job.id.desc()).first()
    if failed is not None:
        # After the worker exhausts normal retries, try again in an hour.
        run_after = max(run_after, (failed.finished_at or failed.updated_at) + timedelta(hours=1))
    return enqueue_job(
        session, kind=ARTIFICIAL_ANALYSIS_SYNC_JOB, payload={},
        dedupe_key=ARTIFICIAL_ANALYSIS_SYNC_JOB, priority=2, run_after=run_after,
    )


def handle_artificial_analysis_sync_job(payload: dict[str, Any]) -> dict[str, Any]:
    # Fetch outside the database write transaction; only commit complete downloads.
    if not settings.artificial_analysis_enabled:
        return {"skipped": True, "reason": "Artificial Analysis disabled"}
    SessionLocal = session_factory()
    with SessionLocal() as session:
        fresh, _ = _aa_fresh_enough(session, min_refresh_days=1)
    if fresh:
        return {"skipped": True}
    rows, summary = fetch_artificial_analysis_rows()
    with session_scope() as session:
        return upsert_artificial_analysis_dataset(session, rows=rows, summary=summary)
