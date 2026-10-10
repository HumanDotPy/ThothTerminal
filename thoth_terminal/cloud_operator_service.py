from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from thoth_terminal.agent.provider_broker import select_provider
from thoth_terminal.cloud_research_automation import CLOUD_RESEARCH_AUTOMATION_JOB
from thoth_terminal.cloud_task_catalog import list_task_capabilities
from thoth_terminal.cloud_goal_run_service import list_cloud_goal_events, serialize_cloud_goal_run, utcnow
from thoth_terminal.cloud_write_policy import cloud_write_policy_status
from thoth_terminal.config import settings
from thoth_terminal.jobs import enqueue_cloud_research_automation_job
from thoth_terminal.models import CloudGoalRun, Job, LocalAgentDailySummary


CLOUD_TASK_KINDS = {
    "cloud_orchestrator",
    "cloud_research_nightly",
    "cloud_research_heartbeat",
    "cloud_research_inquisitive",
    "gap_hunter",
    "red_team",
    "report_body_builder",
    "source_pack_builder",
    "bottleneck_constraint",
}


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _int(value: object, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _text(value: object) -> str:
    return str(value or "").strip()


def _serialize_job(job: Job) -> dict[str, Any]:
    return {
        "id": int(job.id),
        "kind": job.kind,
        "status": job.status,
        "priority": int(job.priority or 0),
        "payload": dict(job.payload or {}) if isinstance(job.payload, dict) else {},
        "dedupe_key": job.dedupe_key,
        "attempts": int(job.attempts or 0),
        "max_attempts": int(job.max_attempts or 0),
        "run_after": _dt(job.run_after),
        "started_at": _dt(job.started_at),
        "finished_at": _dt(job.finished_at),
        "last_error": job.last_error,
        "created_at": _dt(job.created_at),
        "updated_at": _dt(job.updated_at),
    }


def _serialize_dispatch(row: LocalAgentDailySummary) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "run_id": int(row.run_id) if row.run_id is not None else None,
        "summary_date": row.summary_date.isoformat(),
        "summary_kind": row.summary_kind,
        "title": row.title,
        "content_md": row.content_md,
        "payload": dict(row.payload or {}) if isinstance(row.payload, dict) else {},
        "created_at": _dt(row.created_at),
        "updated_at": _dt(row.updated_at),
    }


def cloud_automation_settings_payload() -> dict[str, Any]:
    return {
        "enabled": bool(getattr(settings, "cloud_research_automation_enabled", True)),
        "provider": _text(getattr(settings, "cloud_research_provider", "openrouter")) or "openrouter",
        "model": _text(getattr(settings, "cloud_research_model", "openrouter/free")) or "openrouter/free",
        "reasoning_effort": settings.cloud_research_reasoning_effort,
        "timeout_s": float(getattr(settings, "cloud_research_timeout_s", 1200.0) or 1200.0),
        "max_input_chars": None,
        "max_output_tokens": None,
        "nightly": {
            "enabled": bool(getattr(settings, "cloud_research_nightly_enabled", False)),
            "local_times": _text(getattr(settings, "cloud_research_nightly_local_times", "12:10")),
            "lookback_hours": int(getattr(settings, "cloud_research_nightly_lookback_hours", 72) or 72),
        },
        "heartbeat": {
            "enabled": bool(getattr(settings, "cloud_research_heartbeat_enabled", True)),
            "local_times": _text(getattr(settings, "cloud_research_heartbeat_local_times", "08:00,20:00")),
            "interval_hours": int(getattr(settings, "cloud_research_heartbeat_interval_hours", 12) or 12),
            "lookback_hours": int(getattr(settings, "cloud_research_heartbeat_lookback_hours", 168) or 168),
        },
        "memory": {
            "enabled": bool(getattr(settings, "cloud_agent_memory_enabled", True)),
            "context_chars": int(getattr(settings, "cloud_agent_memory_context_chars", 420000) or 420000),
        },
        "inquisitive": {
            "enabled": bool(getattr(settings, "cloud_research_inquisitive_enabled", False)),
            "local_times": _text(getattr(settings, "cloud_research_inquisitive_local_times", "12:45,18:30")),
            "lookback_hours": int(getattr(settings, "cloud_research_inquisitive_lookback_hours", 120) or 120),
        },
        "no_change_gate": {
            "enabled": bool(getattr(settings, "cloud_research_no_change_gate_enabled", True)),
            "max_staleness_hours": int(getattr(settings, "cloud_research_no_change_max_staleness_hours", 8) or 8),
        },
    }


def cloud_operator_status(session: Session, *, limit: int = 20, openshell_status: dict[str, Any] | None = None) -> dict[str, Any]:
    lim = max(1, min(int(limit or 20), 100))
    runs = (
        session.query(CloudGoalRun)
        .filter(CloudGoalRun.task_kind.in_(sorted(CLOUD_TASK_KINDS)))
        .order_by(CloudGoalRun.updated_at.desc(), CloudGoalRun.id.desc())
        .limit(lim)
        .all()
    )
    jobs = (
        session.query(Job)
        .filter(
            Job.kind.in_(
                [
                    CLOUD_RESEARCH_AUTOMATION_JOB,
                    "research_play_run",
                    "agent_context_compression",
                    "agent_desk_digest",
                ]
            )
        )
        .order_by(Job.updated_at.desc(), Job.id.desc())
        .limit(lim)
        .all()
    )
    dispatches = (
        session.query(LocalAgentDailySummary)
        .filter(LocalAgentDailySummary.summary_kind.in_(["cloud_research_nightly", "cloud_research_heartbeat", "cloud_research_inquisitive"]))
        .order_by(LocalAgentDailySummary.updated_at.desc(), LocalAgentDailySummary.id.desc())
        .limit(8)
        .all()
    )
    running = [row for row in runs if row.status == "running"]
    failed = [row for row in runs if row.status == "failed"]
    pending_jobs = [row for row in jobs if row.status == "pending"]
    running_jobs = [row for row in jobs if row.status == "running"]
    total_tokens = 0
    input_tokens = 0
    output_tokens = 0
    cache_hit_tokens = 0
    cache_write_tokens = 0
    llm_latency_ms = 0
    llm_calls = 0
    usage_reported_calls = 0
    usage_unavailable_calls = 0
    reported_cost_usd = 0.0
    for row in runs:
        stats = dict(row.stats_json or {})
        total_tokens += _int(stats.get("total_tokens"), 0)
        input_tokens += _int(stats.get("effective_input_tokens") or stats.get("input_tokens"), 0)
        output_tokens += _int(stats.get("output_tokens"), 0)
        cache_hit_tokens += _int(stats.get("prompt_cache_hit_tokens"), 0)
        cache_write_tokens += _int(stats.get("prompt_cache_write_tokens"), 0)
        llm_latency_ms += _int(stats.get("llm_latency_ms"), 0)
        llm_calls += _int(stats.get("llm_calls"), 0)
        usage_reported_calls += _int(stats.get("usage_reported_calls"), 0)
        usage_unavailable_calls += _int(stats.get("usage_unavailable_calls"), 0)
        try:
            reported_cost_usd += float(stats.get("reported_cost_usd") or 0.0)
        except (TypeError, ValueError):
            pass
    cache_hit_rate = round(cache_hit_tokens / input_tokens, 6) if input_tokens > 0 else None
    memory_status: dict[str, Any] = {"enabled": bool(getattr(settings, "cloud_agent_memory_enabled", True))}
    if memory_status["enabled"]:
        try:
            from thoth_terminal.cloud_agent_memory_service import (
                CLOUD_RESEARCH_HEARTBEAT_AGENT_KEY,
                list_cloud_agent_memory_segments,
            )

            memory_rows = list_cloud_agent_memory_segments(
                session,
                agent_key=CLOUD_RESEARCH_HEARTBEAT_AGENT_KEY,
                status="active",
                limit=1000,
            )
            token_estimate = sum(int(row.token_estimate or 0) for row in memory_rows)
            char_estimate = sum(len(row.content_md or "") + len(row.summary or "") + len(row.title or "") for row in memory_rows)
            context_chars = int(getattr(settings, "cloud_agent_memory_context_chars", 420000) or 420000)
            memory_status.update(
                {
                    "agent_key": CLOUD_RESEARCH_HEARTBEAT_AGENT_KEY,
                    "active_segments": len(memory_rows),
                    "token_estimate": token_estimate,
                    "char_estimate": char_estimate,
                    "context_chars": context_chars,
                    "requires_compression": char_estimate > context_chars,
                    "cache_scopes": sorted({str(row.cache_scope) for row in memory_rows}),
                }
            )
        except Exception as exc:
            memory_status["error"] = f"{type(exc).__name__}: {exc}"
    broker_status = select_provider(
        session,
        task_kind="cloud_research_heartbeat",
        requested_provider=_text(getattr(settings, "cloud_research_provider", "meta")) or "meta",
        requested_model=_text(getattr(settings, "cloud_research_model", "")) or None,
        scheduled=True,
        now=datetime.now(timezone.utc),
    ).as_dict()
    return {
        "generated_at": utcnow().isoformat(),
        "settings": cloud_automation_settings_payload(),
        "write_policy": cloud_write_policy_status(probe_openshell=True, openshell_status=openshell_status),
        "memory": memory_status,
        "provider_broker": broker_status,
        "metrics": {
            "recent_runs": len(runs),
            "running_runs": len(running),
            "failed_runs": len(failed),
            "pending_jobs": len(pending_jobs),
            "running_jobs": len(running_jobs),
            "recent_total_tokens": total_tokens,
            "recent_input_tokens": input_tokens,
            "recent_output_tokens": output_tokens,
            "recent_cache_hit_tokens": cache_hit_tokens,
            "recent_cache_write_tokens": cache_write_tokens,
            "recent_cache_hit_rate": cache_hit_rate,
            "average_llm_latency_ms": round(llm_latency_ms / llm_calls) if llm_calls > 0 else None,
            "reported_cost_usd": round(reported_cost_usd, 8) if reported_cost_usd > 0 else None,
            "usage_reported_calls": usage_reported_calls,
            "usage_unavailable_calls": usage_unavailable_calls,
        },
        "runs": [serialize_cloud_goal_run(row, events=list_cloud_goal_events(session, int(row.id), limit=12)) for row in runs],
        "jobs": [_serialize_job(row) for row in jobs],
        "dispatches": [_serialize_dispatch(row) for row in dispatches],
        "capabilities": list_task_capabilities(),
    }


def enqueue_cloud_automation_run_now(
    session: Session,
    *,
    mode: str,
    provider: str | None = None,
    model: str | None = None,
    lookback_hours: int | None = None,
    objective: str | None = None,
) -> dict[str, Any]:
    mode_v = _text(mode).lower().replace("-", "_") or "heartbeat"
    if mode_v not in {"nightly", "heartbeat", "inquisitive"}:
        raise ValueError("mode must be nightly|heartbeat|inquisitive")
    now = datetime.now(timezone.utc)
    job = enqueue_cloud_research_automation_job(
        session,
        mode=mode_v,
        lookback_hours=lookback_hours,
        provider=provider or _text(getattr(settings, "cloud_research_provider", "openrouter")),
        model=model or _text(getattr(settings, "cloud_research_model", "openrouter/free")),
        objective=objective,
        dedupe_key=f"cloud_research:manual:{mode_v}:{now.strftime('%Y%m%d%H%M%S')}",
        max_attempts=2,
    )
    return _serialize_job(job)


def retry_cloud_job(session: Session, job_id: int) -> dict[str, Any]:
    row = session.query(Job).filter_by(id=int(job_id)).with_for_update().first()
    if row is None or row.kind != CLOUD_RESEARCH_AUTOMATION_JOB:
        raise ValueError("Cloud research job not found.")
    if row.status != "error":
        raise ValueError("Only failed cloud research jobs can be retried.")
    run_id = _int((row.payload or {}).get("cloud_goal_run_id"), 0)
    run = session.get(CloudGoalRun, run_id) if run_id else None
    if run is not None and run.status != "failed":
        raise ValueError("The research run must be failed before retrying its job.")
    if run is not None:
        from thoth_terminal.cloud_goal_run_service import append_cloud_goal_event

        append_cloud_goal_event(session, run, event_kind="operator_retry_requested",
                               summary="Operator retried the failed research job.",
                               payload={"job_id": int(row.id), "previous_error": row.last_error})
    row.status = "pending"
    row.max_attempts = int(row.attempts or 0) + 2
    row.run_after = utcnow()
    row.started_at = row.finished_at = row.locked_at = None
    row.updated_at = utcnow()
    session.flush()
    return _serialize_job(row)


def cancel_cloud_run(session: Session, run_id: int, *, reason: str | None = None) -> dict[str, Any]:
    row = session.get(CloudGoalRun, int(run_id))
    if row is None:
        raise ValueError("Cloud run not found.")
    if row.status in {"complete", "failed"}:
        return serialize_cloud_goal_run(row, events=list_cloud_goal_events(session, int(row.id), limit=20))
    row.status = "failed"
    row.error = _text(reason) or "cancelled by operator"
    row.updated_at = utcnow()
    row.completed_at = row.completed_at or utcnow()
    session.flush()
    return serialize_cloud_goal_run(row, events=list_cloud_goal_events(session, int(row.id), limit=20))


def cancel_cloud_job(session: Session, job_id: int, *, reason: str | None = None) -> dict[str, Any]:
    row = session.get(Job, int(job_id))
    if row is None:
        raise ValueError("Job not found.")
    if row.status in {"success", "error"}:
        return _serialize_job(row)
    row.status = "error"
    row.last_error = _text(reason) or "cancelled by operator"
    row.finished_at = utcnow()
    row.updated_at = utcnow()
    session.flush()
    return _serialize_job(row)
