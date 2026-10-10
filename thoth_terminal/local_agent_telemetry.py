from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from thoth_terminal.models import LocalAgentAction, LocalAgentRun


LOCAL_AGENT_TELEMETRY_CONTRACT_VERSION = "local-agent-telemetry-v1"
_MAX_TOOL_GROUPS = 50
_MAX_STAGE_GROUPS = 40


def _int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _float(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _duration_ms(started_at: datetime | None, completed_at: datetime | None) -> int | None:
    if started_at is None or completed_at is None:
        return None
    return max(0, int((completed_at - started_at).total_seconds() * 1000))


def _latency_ms(action: LocalAgentAction) -> float | None:
    for payload in (action.response_json, action.request_json):
        if not isinstance(payload, dict):
            continue
        direct = _float(payload.get("latency_ms"))
        if direct is not None:
            return max(0.0, direct)
        seconds = _float(payload.get("elapsed_seconds") or payload.get("duration_seconds"))
        if seconds is not None:
            return max(0.0, seconds * 1000.0)
    return None


def _empty_result(action: LocalAgentAction) -> bool:
    payload = action.response_json if isinstance(action.response_json, dict) else {}
    if payload.get("empty_result") is True:
        return True
    if payload.get("result_count") == 0 or payload.get("count") == 0:
        return True
    return False


def _model_metrics(stats: dict[str, Any]) -> dict[str, Any]:
    rows = [row for row in stats.get("model_calls", []) if isinstance(row, dict)] if isinstance(stats.get("model_calls"), list) else []
    aliases = {
        "input_tokens": ("input_tokens", "prompt_tokens"),
        "cached_input_tokens": ("cached_input_tokens", "prompt_cache_hit_tokens"),
        "output_tokens": ("output_tokens", "completion_tokens"),
        "reasoning_tokens": ("reasoning_tokens",),
        "prompt_eval_ms": ("prompt_eval_ms", "prompt_evaluation_ms"),
        "generation_ms": ("generation_ms", "decode_ms"),
        "time_to_first_token_ms": ("time_to_first_token_ms", "ttft_ms"),
        "latency_ms": ("latency_ms", "model_latency_ms"),
    }

    def first(payload: dict[str, Any], keys: tuple[str, ...]) -> object:
        for key in keys:
            if payload.get(key) is not None:
                return payload.get(key)
        return None

    source_rows = rows or [stats]
    totals: dict[str, float] = defaultdict(float)
    observed: set[str] = set()
    for row in source_rows:
        for metric, keys in aliases.items():
            value = _float(first(row, keys))
            if value is not None:
                totals[metric] += value
                observed.add(metric)
    calls = len(rows) if rows else (_int(stats.get("model_call_count") or stats.get("llm_calls")) or 0)
    input_tokens = int(totals["input_tokens"]) if "input_tokens" in observed else None
    output_tokens = int(totals["output_tokens"]) if "output_tokens" in observed else None
    prompt_eval_ms = totals["prompt_eval_ms"] if "prompt_eval_ms" in observed else None
    generation_ms = totals["generation_ms"] if "generation_ms" in observed else None
    return {
        "available": bool(observed or calls),
        "calls": calls,
        "input_tokens": input_tokens,
        "cached_input_tokens": int(totals["cached_input_tokens"]) if "cached_input_tokens" in observed else None,
        "output_tokens": output_tokens,
        "reasoning_tokens": int(totals["reasoning_tokens"]) if "reasoning_tokens" in observed else None,
        "prompt_eval_ms": round(prompt_eval_ms, 3) if prompt_eval_ms is not None else None,
        "generation_ms": round(generation_ms, 3) if generation_ms is not None else None,
        "latency_ms": round(totals["latency_ms"], 3) if "latency_ms" in observed else None,
        "time_to_first_token_ms": (
            round(totals["time_to_first_token_ms"] / max(1, len(source_rows)), 3)
            if "time_to_first_token_ms" in observed
            else None
        ),
        "prompt_tokens_per_second": (
            round(input_tokens / (prompt_eval_ms / 1000.0), 3)
            if input_tokens is not None and prompt_eval_ms and prompt_eval_ms > 0
            else None
        ),
        "generation_tokens_per_second": (
            round(output_tokens / (generation_ms / 1000.0), 3)
            if output_tokens is not None and generation_ms and generation_ms > 0
            else None
        ),
    }


def _stage_metrics(stats: dict[str, Any]) -> dict[str, Any]:
    stages: dict[str, float] = {}
    for key, raw in stats.items():
        key_v = str(key or "").strip().lower()
        if key_v.endswith("_seconds"):
            value = _float(raw)
            if value is not None:
                stages[key_v[: -len("_seconds")]] = round(max(0.0, value) * 1000.0, 3)
        elif key_v.endswith("_latency_ms"):
            value = _float(raw)
            if value is not None:
                stages[key_v[: -len("_latency_ms")]] = round(max(0.0, value), 3)
        if len(stages) >= _MAX_STAGE_GROUPS:
            break
    return {"available": bool(stages), "duration_ms_by_stage": stages}


def build_local_agent_telemetry(
    session: Session,
    *,
    run: LocalAgentRun,
    stats: dict[str, Any],
) -> dict[str, Any]:
    actions = (
        session.query(LocalAgentAction)
        .filter(LocalAgentAction.run_id == int(run.id))
        .order_by(LocalAgentAction.id.asc())
        .all()
    )
    statuses: dict[str, int] = defaultdict(int)
    tools: dict[str, dict[str, Any]] = {}
    timeout_count = 0
    empty_count = 0
    measured_latencies: list[float] = []
    for action in actions:
        status = str(action.status or "unknown").strip().lower()
        statuses[status] += 1
        error_blob = f"{action.error or ''} {action.summary or ''}".lower()
        is_timeout = "timeout" in error_blob or "timed out" in error_blob
        timeout_count += int(is_timeout)
        empty_count += int(_empty_result(action))
        latency = _latency_ms(action)
        if latency is not None:
            measured_latencies.append(latency)
        tool_name = str(action.tool_name or "").strip()
        if not tool_name or tool_name in {"agent_start_run", "agent_finish_run"}:
            continue
        bucket = tools.setdefault(tool_name, {"calls": 0, "success": 0, "errors": 0, "timeouts": 0, "empty_results": 0})
        bucket["calls"] += 1
        bucket["success"] += int(status == "success")
        bucket["errors"] += int(status == "error" or bool(action.error))
        bucket["timeouts"] += int(is_timeout)
        bucket["empty_results"] += int(_empty_result(action))
    tool_rows = sorted(tools.items(), key=lambda item: (-int(item[1]["calls"]), item[0]))[:_MAX_TOOL_GROUPS]
    tool_calls = sum(int(row["calls"]) for _, row in tool_rows)
    tool_errors = sum(int(row["errors"]) for _, row in tool_rows)
    action_errors = int(statuses.get("error", 0))
    model_calls = [row for row in stats["model_calls"] if isinstance(row, dict)] if isinstance(stats.get("model_calls"), list) else []
    model_calls.extend(action.response_json["model_metrics"] for action in actions
                       if action.action_kind == "harness_inference" and action.status == "success"
                       and isinstance(action.response_json, dict)
                       and isinstance(action.response_json.get("model_metrics"), dict))
    model_stats = {**stats, "model_calls": model_calls} if model_calls else stats
    return {
        "contract_version": LOCAL_AGENT_TELEMETRY_CONTRACT_VERSION,
        "run": {
            "duration_ms": _duration_ms(run.started_at, run.completed_at),
            "status": run.status,
        },
        "actions": {
            "total": len(actions),
            "success": int(statuses.get("success", 0)),
            "errors": action_errors,
            "running": int(statuses.get("running", 0)),
            "error_rate": round(action_errors / len(actions), 6) if actions else None,
            "timeouts": timeout_count,
            "empty_results": empty_count,
            "measured_latency_calls": len(measured_latencies),
            "average_latency_ms": round(sum(measured_latencies) / len(measured_latencies), 3) if measured_latencies else None,
        },
        "tools": {
            "calls": tool_calls,
            "errors": tool_errors,
            "error_rate": round(tool_errors / tool_calls, 6) if tool_calls else None,
            "by_tool": {name: row for name, row in tool_rows},
            "truncated": len(tools) > _MAX_TOOL_GROUPS,
        },
        "model": _model_metrics(model_stats),
        "stages": _stage_metrics(stats),
    }
