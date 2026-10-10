from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from thoth_terminal.agent.llm_client import ChatMessage, llm_metadata
from thoth_terminal.agent_request_manifest_service import manifested_chat_json
from thoth_terminal.agent.model_catalog import build_model_catalog
from thoth_terminal.agent.model_selection import select_openrouter_free_model
from thoth_terminal.config import settings
from thoth_terminal.models import (
    AgentRequestManifest,
    CloudGoalEvent,
    CloudGoalLineage,
    CloudGoalProjection,
    CloudGoalRun,
)


CLOUD_GOAL_RUNNING = "running"
CLOUD_GOAL_WAITING = "waiting"
CLOUD_GOAL_COMPLETE = "complete"
CLOUD_GOAL_FAILED = "failed"
CLOUD_GOAL_PROJECTION_VERSION = "cloud-goal-projection-v1"

DEFAULT_TOKEN_POLICY = {
    # DeepSeek V4 exposes a 1,048,576-token window. Keep a 5% reserve for
    # instructions, tool results, token-estimation error, and the next answer.
    "target_context_tokens": 900_000,
    "soft_compact_at": 930_000,
    "hard_compact_at": 950_000,
    "allow_one_step_grace": True,
}

TRANSIENT_CLOUD_MODEL_FAILURE_TOKENS = (
    "408",
    "429",
    "500",
    "502",
    "503",
    "504",
    "rate-limit",
    "rate limit",
    "rate limited",
    "quota",
    "throttle",
    "throttled",
    "temporarily",
    "timeout",
    "timed out",
    "missing text content",
    "provider returned error",
    "service unavailable",
    "gateway",
    "upstream",
    "overloaded",
    "capacity",
    "no endpoints",
    "no endpoint",
    "routing",
    "route",
    "try again",
    "connection reset",
    "connection aborted",
    "remote end closed",
)


CLOUD_CONTEXT_COMPACTION_SYSTEM = """Return json only.
You are compacting a Thoth cloud goal ledger so the task can continue.

Preserve:
- objective and current task state
- decisions already made
- child runs and their outcomes
- source/evidence pointers and report ids
- open gaps, assumptions, risks, and next steps
- model/routing/token metadata when available

Remove:
- duplicated raw context
- repeated prompt boilerplate
- long source excerpts after their useful claims are preserved
- local runtime internals, private URLs, operator-only details, API keys, and portfolio state

Return exactly one JSON object:
{
  "compacted_context_md": "markdown working context that can replace the old current_context_md",
  "preserved_decisions": [],
  "open_questions": [],
  "next_steps": [],
  "discarded": []
}
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def is_transient_cloud_model_failure(exc: Exception | str) -> bool:
    text = str(exc or "").lower()
    return any(token in text for token in TRANSIENT_CLOUD_MODEL_FAILURE_TOKENS)


def cooldown_seconds_for_cloud_model_failure(exc: Exception | str) -> int:
    text = str(exc or "").lower()
    if "429" in text or "rate" in text:
        return 15 * 60
    if "quota" in text or "throttle" in text:
        return 15 * 60
    if "timeout" in text or "timed out" in text:
        return 10 * 60
    if "overloaded" in text or "capacity" in text:
        return 10 * 60
    if any(token in text for token in ("500", "502", "503", "504", "service unavailable", "gateway", "upstream")):
        return 10 * 60
    return 5 * 60


def resume_cloud_goal_after_cooldown(
    session: Session,
    run: CloudGoalRun,
    *,
    summary: str = "Cloud model cooldown elapsed; retrying.",
) -> bool:
    if run.status != CLOUD_GOAL_WAITING:
        return True
    stats = dict(run.stats_json or {})
    retry_after = stats.get("retry_after")
    retry_at = None
    if retry_after:
        try:
            retry_at = datetime.fromisoformat(str(retry_after))
        except Exception:
            retry_at = None
    if retry_at is not None and retry_at > utcnow():
        return False
    run.status = CLOUD_GOAL_RUNNING
    stats.pop("retry_after", None)
    stats.pop("cooldown_reason", None)
    run.stats_json = stats
    run.error = None
    append_cloud_goal_event(session, run, event_kind="cooldown_resumed", summary=summary)
    session.flush()
    return True


def mark_cloud_goal_model_cooldown(
    session: Session,
    run: CloudGoalRun,
    exc: Exception | str,
    *,
    step_index: int | None = None,
    fallback_available: bool = False,
    summary_prefix: str = "Transient cloud model failure",
) -> dict[str, Any]:
    cooldown_s = cooldown_seconds_for_cloud_model_failure(exc)
    retry_at = utcnow() + timedelta(seconds=cooldown_s)
    stats = dict(run.stats_json or {})
    stats["retry_after"] = retry_at.isoformat()
    stats["cooldown_seconds"] = cooldown_s
    stats["cooldown_reason"] = str(exc)[:500]
    stats["fallback_available"] = bool(fallback_available)
    run.stats_json = stats
    run.status = CLOUD_GOAL_WAITING
    run.error = None
    run.updated_at = utcnow()
    append_cloud_goal_event(
        session,
        run,
        event_kind="cooldown",
        summary=f"{summary_prefix}; retry scheduled at {retry_at.isoformat()}.",
        payload={"error": str(exc), "fallback_available": bool(fallback_available), "retry_after": retry_at.isoformat()},
        step_index=step_index,
    )
    session.flush()
    return {"retry_after": retry_at.isoformat(), "cooldown_seconds": cooldown_s}


def estimate_tokens(text: object) -> int:
    # Fast conservative-enough estimate for routing/compaction decisions.
    return max(1, int(len(str(text or "")) / 4))


def create_cloud_goal_run(
    session: Session,
    *,
    task_kind: str,
    objective: str,
    provider: str = "deepseek",
    model: str = "deepseek-v4-flash",
    token_policy: dict[str, Any] | None = None,
    initial_context_md: str | None = None,
) -> CloudGoalRun:
    now = utcnow()
    policy = dict(DEFAULT_TOKEN_POLICY)
    if token_policy:
        policy.update({key: value for key, value in token_policy.items() if value is not None})
    provider_v = str(provider or "deepseek").strip().lower()
    requested_model = str(model or "deepseek-v4-flash").strip()
    model_v = requested_model
    model_resolution: dict[str, Any] | None = None
    if provider_v == "deepseek" and requested_model != "deepseek-v4-flash":
        model_v = "deepseek-v4-flash"
        model_resolution = {
            "requested_model": requested_model,
            "resolved_model": model_v,
            "selected_by": "deepseek_v4_flash_collapse_policy",
        }
    if provider_v == "openrouter" and requested_model == "openrouter/free":
        catalog = build_model_catalog()
        selected = select_openrouter_free_model(catalog, task_kind=str(task_kind or "cloud_goal").strip() or "cloud_goal")
        if selected is not None:
            model_v = str(selected.get("model") or requested_model).strip()
            model_resolution = {
                "requested_model": requested_model,
                "resolved_model": model_v,
                "selected_by": "aa_policy_openrouter_free_resolver",
                "intelligence_score": selected.get("intelligence_score"),
                "routing_bias": selected.get("routing_bias"),
            }
    row = CloudGoalRun(
        task_kind=str(task_kind or "cloud_goal").strip() or "cloud_goal",
        status=CLOUD_GOAL_RUNNING,
        objective=str(objective or "").strip() or "Run cloud goal.",
        provider=provider_v,
        model=model_v,
        current_context_md=str(initial_context_md or "").strip(),
        token_policy=policy,
        stats_json={"steps": 0, "compactions": 0, "total_tokens": 0},
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    append_cloud_goal_event(
        session,
        row,
        event_kind="goal_started",
        summary=row.objective,
        content_md=initial_context_md,
        payload={"provider": row.provider, "model": row.model, "token_policy": policy, "model_resolution": model_resolution},
    )
    return row


def get_cloud_goal_run(session: Session, run_id: int) -> CloudGoalRun | None:
    return session.query(CloudGoalRun).filter_by(id=int(run_id)).first()


def append_cloud_goal_event(
    session: Session,
    run: CloudGoalRun,
    *,
    event_kind: str,
    summary: str | None = None,
    content_md: str | None = None,
    payload: dict[str, Any] | None = None,
    step_index: int | None = None,
) -> CloudGoalEvent:
    stats = dict(run.stats_json or {})
    idx = int(step_index if step_index is not None else stats.get("steps", 0) or 0)
    event = CloudGoalEvent(
        run_id=int(run.id),
        step_index=idx,
        event_kind=str(event_kind or "event").strip() or "event",
        summary=str(summary or "").strip() or None,
        content_md=str(content_md or "").strip() or None,
        payload=dict(payload or {}),
        token_estimate=estimate_tokens(content_md or summary or payload or ""),
        created_at=utcnow(),
    )
    session.add(event)
    run.updated_at = utcnow()
    session.flush()
    if event.event_kind in {"final", "error", "worker_interruption_recovered"}:
        lineages = (
            session.query(CloudGoalLineage)
            .filter(
                CloudGoalLineage.child_run_id == int(run.id),
                CloudGoalLineage.terminal_disposition.in_(["pending", "ready_for_acceptance"]),
            )
            .all()
        )
        for lineage in lineages:
            if event.event_kind == "final":
                lineage.status = "complete"
                lineage.terminal_disposition = "ready_for_acceptance"
            else:
                lineage.status = "failed"
                lineage.terminal_disposition = "failed"
            lineage.completed_at = run.completed_at or utcnow()
            lineage.updated_at = utcnow()
        if lineages:
            session.flush()
    return event


def fail_stale_cloud_goal_runs(
    session: Session,
    *,
    task_kinds: set[str] | None = None,
    stale_after_seconds: int = 1800,
    reason: str = "Recovered abandoned cloud run after worker interruption.",
) -> list[int]:
    """Close cloud runs that cannot still have an owning worker."""
    cutoff = utcnow() - timedelta(seconds=max(60, int(stale_after_seconds or 1800)))
    query = session.query(CloudGoalRun).filter(
        CloudGoalRun.status == CLOUD_GOAL_RUNNING,
        CloudGoalRun.updated_at < cutoff,
    )
    if task_kinds:
        query = query.filter(CloudGoalRun.task_kind.in_(sorted(task_kinds)))
    rows = query.order_by(CloudGoalRun.updated_at.asc(), CloudGoalRun.id.asc()).all()
    recovered: list[int] = []
    now = utcnow()
    for run in rows:
        run.status = CLOUD_GOAL_FAILED
        run.error = str(reason)[:2000]
        run.completed_at = now
        append_cloud_goal_event(
            session,
            run,
            event_kind="worker_interruption_recovered",
            summary=run.error,
            payload={"stale_cutoff": cutoff.isoformat()},
        )
        recovered.append(int(run.id))
    session.flush()
    return recovered


def list_cloud_goal_events(session: Session, run_id: int, *, limit: int = 200) -> list[CloudGoalEvent]:
    return (
        session.query(CloudGoalEvent)
        .filter_by(run_id=int(run_id))
        .order_by(CloudGoalEvent.step_index.asc(), CloudGoalEvent.id.asc())
        .limit(max(1, min(10_000, int(limit or 200))))
        .all()
    )


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: object) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _event_projection_payload(event: CloudGoalEvent) -> dict[str, Any]:
    return {
        "id": int(event.id),
        "run_id": int(event.run_id),
        "step_index": int(event.step_index),
        "event_kind": event.event_kind,
        "summary": event.summary,
        "content_md": event.content_md,
        "payload": dict(event.payload or {}),
        "token_estimate": int(event.token_estimate or 0),
        "created_at": event.created_at.isoformat() if event.created_at else None,
    }


def _events_sha256(events: list[CloudGoalEvent]) -> str:
    return _sha256(_canonical_json([_event_projection_payload(event) for event in events]))


def latest_cloud_goal_projection(session: Session, run_id: int) -> CloudGoalProjection | None:
    return (
        session.query(CloudGoalProjection)
        .filter(CloudGoalProjection.run_id == int(run_id))
        .order_by(CloudGoalProjection.sequence.desc(), CloudGoalProjection.id.desc())
        .first()
    )


def serialize_cloud_goal_projection(
    projection: CloudGoalProjection,
    *,
    include_content: bool = True,
) -> dict[str, Any]:
    payload = {
        "id": int(projection.id),
        "projection_key": projection.projection_key,
        "run_id": int(projection.run_id),
        "sequence": int(projection.sequence),
        "projection_version": projection.projection_version,
        "status": projection.status,
        "previous_projection_id": projection.previous_projection_id,
        "source_event_from_id": projection.source_event_from_id,
        "source_event_to_id": projection.source_event_to_id,
        "source_event_count": int(projection.source_event_count or 0),
        "source_event_ids": [int(value) for value in projection.source_event_ids_json or []],
        "source_events_sha256": projection.source_events_sha256,
        "content_sha256": projection.content_sha256,
        "structured_state": dict(projection.structured_state_json or {}),
        "request_manifest_id": projection.request_manifest_id,
        "created_at": projection.created_at.isoformat() if projection.created_at else None,
    }
    if include_content:
        payload["content_md"] = projection.content_md
    return payload


def list_cloud_goal_projections(
    session: Session,
    run_id: int,
    *,
    limit: int = 100,
    include_content: bool = False,
) -> list[dict[str, Any]]:
    rows = (
        session.query(CloudGoalProjection)
        .filter(CloudGoalProjection.run_id == int(run_id))
        .order_by(CloudGoalProjection.sequence.desc(), CloudGoalProjection.id.desc())
        .limit(max(1, min(1000, int(limit or 100))))
        .all()
    )
    return [serialize_cloud_goal_projection(row, include_content=include_content) for row in rows]


def verify_cloud_goal_projection(
    session: Session,
    projection: CloudGoalProjection | int,
) -> dict[str, Any]:
    row = session.get(CloudGoalProjection, int(projection)) if isinstance(projection, int) else projection
    if row is None:
        raise ValueError("Cloud goal projection not found.")
    event_ids = [int(value) for value in row.source_event_ids_json or []]
    events = (
        session.query(CloudGoalEvent)
        .filter(CloudGoalEvent.run_id == int(row.run_id), CloudGoalEvent.id.in_(event_ids or [-1]))
        .order_by(CloudGoalEvent.id.asc())
        .all()
    )
    actual_ids = [int(event.id) for event in events]
    issues: list[str] = []
    if actual_ids != event_ids:
        issues.append("source event id set/order does not match")
    actual_events_sha256 = _events_sha256(events)
    if actual_events_sha256 != row.source_events_sha256:
        issues.append("source event digest does not match")
    actual_content_sha256 = _sha256(row.content_md)
    if actual_content_sha256 != row.content_sha256:
        issues.append("projection content digest does not match")
    if len(events) != int(row.source_event_count or 0):
        issues.append("source event count does not match")
    return {
        "ok": not issues,
        "projection_id": int(row.id),
        "run_id": int(row.run_id),
        "issues": issues,
        "expected_source_events_sha256": row.source_events_sha256,
        "actual_source_events_sha256": actual_events_sha256,
        "expected_content_sha256": row.content_sha256,
        "actual_content_sha256": actual_content_sha256,
    }


def replay_cloud_goal_context(
    session: Session,
    run_id: int,
    *,
    mode: str = "projection",
    projection_id: int | None = None,
) -> dict[str, Any]:
    run = get_cloud_goal_run(session, int(run_id))
    if run is None:
        raise ValueError("Cloud goal run not found.")
    mode_v = str(mode or "projection").strip().lower()
    if mode_v not in {"projection", "full"}:
        raise ValueError("mode must be projection|full")
    all_events = (
        session.query(CloudGoalEvent)
        .filter(CloudGoalEvent.run_id == int(run.id))
        .order_by(CloudGoalEvent.id.asc())
        .all()
    )
    if mode_v == "full":
        lines = ["# Cloud Goal Full Event Replay", "", "## Objective", run.objective, "", "## Immutable Event Ledger"]
        for event in all_events:
            lines.extend(["", f"### Event {int(event.id)} / Step {int(event.step_index)}: {event.event_kind}"])
            if event.summary:
                lines.append(event.summary)
            if event.content_md:
                lines.append(event.content_md)
            if event.payload:
                lines.extend(["", "```json", json.dumps(event.payload, ensure_ascii=False, sort_keys=True, indent=2, default=str), "```"])
        return {
            "mode": mode_v,
            "run_id": int(run.id),
            "projection": None,
            "verification": None,
            "event_count": len(all_events),
            "replayed_context_md": "\n".join(lines).strip(),
        }
    projection = (
        session.get(CloudGoalProjection, int(projection_id))
        if projection_id is not None
        else latest_cloud_goal_projection(session, int(run.id))
    )
    if projection is None or int(projection.run_id) != int(run.id):
        raise ValueError("Cloud goal projection not found for this run.")
    verification = verify_cloud_goal_projection(session, projection)
    if not verification["ok"]:
        raise ValueError("Cloud goal projection failed lineage verification: " + "; ".join(verification["issues"]))
    later_events = [
        event
        for event in all_events
        if int(event.id) > int(projection.source_event_to_id or 0)
        and not (
            event.event_kind == "context_compaction"
            and int(dict(dict(event.payload or {}).get("projection") or {}).get("id") or 0) == int(projection.id)
        )
    ]
    lines = [
        "# Cloud Goal Projection Replay",
        "",
        "## Objective",
        run.objective,
        "",
        f"## Verified Projection {int(projection.sequence)}",
        projection.content_md,
        "",
        "## Events Since Projection",
    ]
    if not later_events:
        lines.append("No later events.")
    for event in later_events:
        lines.extend(["", f"### Event {int(event.id)} / Step {int(event.step_index)}: {event.event_kind}"])
        if event.summary:
            lines.append(event.summary)
        if event.content_md:
            lines.append(event.content_md)
    return {
        "mode": mode_v,
        "run_id": int(run.id),
        "projection": serialize_cloud_goal_projection(projection, include_content=False),
        "verification": verification,
        "event_count": len(later_events),
        "replayed_context_md": "\n".join(lines).strip(),
    }


def render_cloud_goal_context(run: CloudGoalRun, events: list[CloudGoalEvent]) -> str:
    lines = [
        "# Cloud Goal Working Context",
        "",
        "## Objective",
        run.objective,
        "",
        "## Current Working State",
        (run.current_context_md or "No compacted working state yet. Use the ledger below."),
        "",
        "## Recent Ledger",
    ]
    for event in events[-24:]:
        lines.append("")
        lines.append(f"### Step {event.step_index}: {event.event_kind}")
        if event.summary:
            lines.append(event.summary)
        if event.content_md:
            lines.append(event.content_md[:80_000])
    return "\n".join(lines).strip()


def should_compact_context(run: CloudGoalRun, rendered_context: str, *, upcoming_tokens: int = 0) -> tuple[bool, str]:
    policy = dict(DEFAULT_TOKEN_POLICY)
    policy.update(dict(run.token_policy or {}))
    total = estimate_tokens(rendered_context) + max(0, int(upcoming_tokens or 0))
    hard = int(policy.get("hard_compact_at") or DEFAULT_TOKEN_POLICY["hard_compact_at"])
    soft = int(policy.get("soft_compact_at") or DEFAULT_TOKEN_POLICY["soft_compact_at"])
    stats = dict(run.stats_json or {})
    if total >= hard:
        return True, f"hard limit {hard} would be exceeded by {total} tokens"
    if total >= soft and not bool(stats.get("used_compaction_grace")) and bool(policy.get("allow_one_step_grace", True)):
        stats["used_compaction_grace"] = True
        run.stats_json = stats
        return False, f"soft limit {soft} reached; one-step grace allowed"
    if total >= soft:
        return True, f"soft limit {soft} exceeded after grace by {total} tokens"
    return False, f"under compaction limits at {total} tokens"


def _int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def accumulate_llm_usage_stats(stats: dict[str, Any], metadata: dict[str, Any], *, include_reasoning: bool = True) -> dict[str, Any]:
    out = dict(stats or {})
    keys = [
        "input_tokens",
        "effective_input_tokens",
        "output_tokens",
        "total_tokens",
        "prompt_cache_hit_tokens",
        "prompt_cache_miss_tokens",
        "prompt_cache_write_tokens",
    ]
    if include_reasoning:
        keys.append("reasoning_tokens")
    for key in keys:
        raw = metadata.get(key)
        if str(raw or "").isdigit():
            out[key] = int(out.get(key) or 0) + int(raw)
    hit = int(out.get("prompt_cache_hit_tokens") or 0)
    miss = int(out.get("prompt_cache_miss_tokens") or 0)
    if hit + miss > 0:
        out["cache_hit_rate"] = round(hit / (hit + miss), 6)
    if metadata.get("reported_cost_usd") is not None:
        try:
            out["reported_cost_usd"] = round(
                float(out.get("reported_cost_usd") or 0.0) + float(metadata["reported_cost_usd"]),
                8,
            )
        except (TypeError, ValueError):
            pass
    if str(metadata.get("latency_ms") or "").isdigit():
        out["llm_latency_ms"] = int(out.get("llm_latency_ms") or 0) + int(metadata["latency_ms"])
    usage_reported = any(metadata.get(key) is not None for key in ("input_tokens", "output_tokens", "total_tokens"))
    status_key = "usage_reported_calls" if usage_reported else "usage_unavailable_calls"
    out[status_key] = int(out.get(status_key) or 0) + 1
    out["llm_calls"] = int(out.get("llm_calls") or 0) + 1
    return out


def compact_cloud_goal_context(
    session: Session,
    run_id: int,
    *,
    provider: str | None = None,
    model: str | None = None,
    reason: str | None = None,
    force: bool = False,
    max_input_chars: int | None = None,
    max_output_tokens: int | None = None,
) -> dict[str, Any]:
    run = get_cloud_goal_run(session, int(run_id))
    if run is None:
        raise ValueError("Cloud goal run not found.")
    events = list_cloud_goal_events(session, int(run_id), limit=1000)
    rendered = render_cloud_goal_context(run, events)
    needed, why = should_compact_context(run, rendered)
    if not force and not needed:
        return {
            "compacted": False,
            "reason": why,
            "run": serialize_cloud_goal_run(run, events=events),
        }

    call_provider = str(provider or run.provider or getattr(settings, "cloud_research_provider", "openrouter") or "openrouter").strip().lower()
    call_model = str(model or run.model or getattr(settings, "cloud_research_model", "openrouter/free") or "openrouter/free").strip()
    session.flush()
    session.commit()
    result = manifested_chat_json(
        session,
        run_kind="cloud_goal",
        run_id=int(run.id),
        task_kind="cloud_goal_context_compaction",
        messages=[
            ChatMessage(role="system", content=CLOUD_CONTEXT_COMPACTION_SYSTEM),
            ChatMessage(
                role="user",
                content=(
                    "# Compaction Request\n\n"
                    f"Reason: {reason or why}\n\n"
                    f"{rendered}"
                ),
            ),
        ],
        provider=call_provider,
        model=call_model,
        timeout_s=max(30.0, float(getattr(settings, "cloud_research_timeout_s", 1200.0) or 1200.0)),
        max_input_chars=None,
        max_output_tokens=None,
        prompt_cache_key="thoth-cloud-goal-compaction-v1",
        permission_profile="cloud-goal-model-no-direct-tools",
    )

    run = get_cloud_goal_run(session, int(run_id))
    if run is None:
        raise ValueError("Cloud goal run disappeared during compaction.")
    metadata = llm_metadata(result)
    compacted_md = str(result.get("compacted_context_md") or "").strip()
    if not compacted_md:
        compacted_md = "# Compacted Cloud Goal Context\n\nCompaction returned no markdown body; preserve recent ledger."
    previous_projection = latest_cloud_goal_projection(session, int(run.id))
    sequence = int(previous_projection.sequence if previous_projection is not None else 0) + 1
    source_event_ids = [int(event.id) for event in events]
    source_events_sha256 = _events_sha256(events)
    request_manifest = (
        session.query(AgentRequestManifest)
        .filter(
            AgentRequestManifest.run_kind == "cloud_goal",
            AgentRequestManifest.run_id == int(run.id),
            AgentRequestManifest.task_kind == "cloud_goal_context_compaction",
        )
        .order_by(AgentRequestManifest.id.desc())
        .first()
    )
    structured_state = {
        "objective": run.objective,
        "reason": reason or why,
        "preserved_decisions": result.get("preserved_decisions") if isinstance(result.get("preserved_decisions"), list) else [],
        "open_questions": result.get("open_questions") if isinstance(result.get("open_questions"), list) else [],
        "next_steps": result.get("next_steps") if isinstance(result.get("next_steps"), list) else [],
        "discarded": result.get("discarded") if isinstance(result.get("discarded"), list) else [],
    }
    projection = CloudGoalProjection(
        projection_key=f"cloud-goal:{int(run.id)}:projection:{sequence}:{source_events_sha256[:16]}",
        run_id=int(run.id),
        sequence=sequence,
        projection_version=CLOUD_GOAL_PROJECTION_VERSION,
        status="committed",
        previous_projection_id=int(previous_projection.id) if previous_projection is not None else None,
        source_event_from_id=source_event_ids[0] if source_event_ids else None,
        source_event_to_id=source_event_ids[-1] if source_event_ids else None,
        source_event_count=len(source_event_ids),
        source_event_ids_json=source_event_ids,
        source_events_sha256=source_events_sha256,
        content_md=compacted_md,
        content_sha256=_sha256(compacted_md),
        structured_state_json=structured_state,
        request_manifest_id=int(request_manifest.id) if request_manifest is not None else None,
        created_at=utcnow(),
    )
    session.add(projection)
    session.flush()
    stats = dict(run.stats_json or {})
    usage = {
        key: _int(metadata.get(key), 0)
        for key in ("input_tokens", "output_tokens", "total_tokens", "reasoning_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens")
        if metadata.get(key) is not None
    }
    stats["compactions"] = int(stats.get("compactions") or 0) + 1
    stats["last_compaction"] = {
        "reason": reason or why,
        "provider": metadata.get("provider") or call_provider,
        "requested_model": metadata.get("requested_model") or call_model,
        "routed_model": metadata.get("model") or metadata.get("response_model"),
        "usage": usage,
        "projection_id": int(projection.id),
        "projection_sequence": sequence,
        "source_events_sha256": source_events_sha256,
    }
    stats = accumulate_llm_usage_stats(stats, metadata)
    run.stats_json = stats
    run.current_context_md = compacted_md
    run.updated_at = utcnow()
    append_cloud_goal_event(
        session,
        run,
        event_kind="context_compaction",
        summary=reason or why,
        content_md=compacted_md,
        payload={
            "llm": metadata,
            "projection": serialize_cloud_goal_projection(projection, include_content=False),
            **structured_state,
        },
    )
    events = list_cloud_goal_events(session, int(run.id), limit=1000)
    return {
        "compacted": True,
        "reason": reason or why,
        "projection": serialize_cloud_goal_projection(projection),
        "verification": verify_cloud_goal_projection(session, projection),
        "run": serialize_cloud_goal_run(run, events=events),
    }


def serialize_cloud_goal_event(event: CloudGoalEvent) -> dict[str, Any]:
    return {
        "id": int(event.id),
        "run_id": int(event.run_id),
        "step_index": int(event.step_index),
        "event_kind": event.event_kind,
        "summary": event.summary,
        "content_md": event.content_md,
        "payload": dict(event.payload or {}),
        "token_estimate": int(event.token_estimate or 0),
        "created_at": event.created_at.isoformat() if event.created_at else None,
    }


def serialize_cloud_goal_run(run: CloudGoalRun, *, events: list[CloudGoalEvent] | None = None) -> dict[str, Any]:
    return {
        "id": int(run.id),
        "task_kind": run.task_kind,
        "status": run.status,
        "objective": run.objective,
        "provider": run.provider,
        "model": run.model,
        "routed_model": run.routed_model,
        "current_context_md": run.current_context_md,
        "final_result": dict(run.final_result or {}),
        "token_policy": dict(run.token_policy or {}),
        "stats": dict(run.stats_json or {}),
        "error": run.error,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "updated_at": run.updated_at.isoformat() if run.updated_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "events": [serialize_cloud_goal_event(event) for event in (events or [])],
    }
