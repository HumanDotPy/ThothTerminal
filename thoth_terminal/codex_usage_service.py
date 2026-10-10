from __future__ import annotations

import json
import os
import threading
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


PRICING_SOURCE_URL = "https://developers.openai.com/api/docs/pricing"
PRICING_OBSERVED_AT = "2026-09-22"
LONG_CONTEXT_INPUT_THRESHOLD = 272_000
RATE_LIMIT_STALE_AFTER_SECONDS = 24 * 60 * 60
DEFAULT_DAYS = 30
MAX_DAYS = 90

# The values are USD per one million tokens for the standard API tier. This
# snapshot is deliberately local and bounded; a dashboard request never calls
# the pricing page or transmits session contents.
MODEL_PRICING: dict[str, dict[str, float]] = {
    "gpt-6-astra": {"input": 10.0, "cached_input": 1.0, "cache_write_input": 12.5, "output": 50.0},
    "gpt-6-sol": {"input": 2.0, "cached_input": 0.2, "cache_write_input": 2.5, "output": 10.0},
    "gpt-6-luna": {"input": 0.1, "cached_input": 0.01, "cache_write_input": 0.125, "output": 0.5},
    "gpt-5.6-sol": {"input": 4.0, "cached_input": 0.4, "cache_write_input": 5.0, "output": 20.0},
    "gpt-5.6-terra": {"input": 2.0, "cached_input": 0.2, "cache_write_input": 2.5, "output": 12.0},
    "gpt-5.6-luna": {"input": 0.2, "cached_input": 0.02, "cache_write_input": 0.25, "output": 1.2},
}

_TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)
_CACHE_LOCK = threading.RLock()


def _int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _iso_timestamp(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _timestamp_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _model_name(value: object) -> str:
    text = str(value or "").strip()
    return text or "unknown"


def _usage_values(raw: object) -> dict[str, int]:
    payload = raw if isinstance(raw, dict) else {}
    return {field: _int(payload.get(field)) for field in _TOKEN_FIELDS}


def _delta(current: dict[str, int], previous: dict[str, int] | None) -> dict[str, int]:
    if previous is None:
        return dict(current)
    return {field: current[field] - previous.get(field, 0) if current[field] >= previous.get(field, 0) else current[field] for field in _TOKEN_FIELDS}


def _event_identity(timestamp: str | None, model: str, values: dict[str, int]) -> tuple[object, ...]:
    return (timestamp or "", model, *(values.get(field, 0) for field in _TOKEN_FIELDS))


def _rate_limit_identity(timestamp: str | None, rate_limits: object) -> tuple[object, ...]:
    try:
        encoded = json.dumps(rate_limits, sort_keys=True, separators=(",", ":"), default=str)
    except TypeError:
        encoded = repr(rate_limits)
    return (timestamp or "", encoded)


@dataclass
class _UsageEvent:
    timestamp: str
    model: str
    effort: str
    totals: dict[str, int]
    values: dict[str, int]
    rate_limits: dict[str, Any] | None = None


@dataclass
class _FileState:
    offset: int = 0
    size: int = 0
    mtime_ns: int = 0
    session_id: str | None = None
    active_model: str = "unknown"
    active_effort: str = "unknown"
    previous_totals: dict[str, int] | None = None
    events: list[_UsageEvent] = field(default_factory=list)


_FILE_CACHE: dict[str, _FileState] = {}


def _default_roots() -> tuple[Path, Path]:
    codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    return codex_home / "sessions", codex_home / "archived_sessions"


def _selected_files(roots: Iterable[Path], *, cutoff: datetime, now: datetime) -> list[Path]:
    selected: list[Path] = []
    for root in roots:
        try:
            paths = root.rglob("*.jsonl")
        except OSError:
            continue
        for path in paths:
            try:
                stat = path.stat()
            except OSError:
                continue
            modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            if modified >= cutoff or modified > now:
                selected.append(path)
    return sorted(set(selected), key=lambda path: str(path).casefold())


def _model_from_payload(payload: dict[str, Any], current: str) -> str:
    model = payload.get("model")
    if model:
        return _model_name(model)
    settings = payload.get("thread_settings")
    if isinstance(settings, dict) and settings.get("model"):
        return _model_name(settings["model"])
    return current


def _effort_from_payload(payload: dict[str, Any], current: str) -> str:
    effort = payload.get("effort") or payload.get("reasoning_effort")
    settings = payload.get("thread_settings")
    if isinstance(settings, dict):
        effort = effort or settings.get("reasoning_effort") or settings.get("effort")
    text = str(effort or "").strip().lower()
    return text or current


def _read_incremental(path: Path, state: _FileState) -> None:
    try:
        stat = path.stat()
    except OSError:
        return
    if stat.st_size < state.offset or stat.st_mtime_ns < state.mtime_ns:
        state.offset = 0
        state.size = 0
        state.session_id = None
        state.active_model = "unknown"
        state.active_effort = "unknown"
        state.previous_totals = None
        state.events.clear()
    try:
        handle = path.open("rb")
    except OSError:
        return
    consumed = 0
    with handle:
        handle.seek(state.offset)
        for line in handle:
            if not line.endswith((b"\n", b"\r")):
                break
            consumed += len(line)
            # Session files also contain large prompts, responses, and tool
            # output. Avoid decoding/materializing those for counter accounting.
            if not any(marker in line for marker in (b'"token_count"', b'"turn_context"', b'"session_meta"', b'"thread_settings_applied"')):
                continue
            try:
                item = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(item, dict):
                continue
            timestamp = _iso_timestamp(item.get("timestamp"))
            kind = str(item.get("type") or "").strip()
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            if kind == "session_meta":
                state.session_id = str(payload.get("session_id") or payload.get("id") or "").strip() or state.session_id
            elif kind == "turn_context":
                previous_model = state.active_model
                state.active_model = _model_from_payload(payload, previous_model)
                effort_fallback = "unknown" if state.active_model != previous_model else state.active_effort
                state.active_effort = _effort_from_payload(payload, effort_fallback)
            elif kind == "event_msg":
                payload_type = str(payload.get("type") or "").strip()
                if payload_type == "thread_settings_applied":
                    previous_model = state.active_model
                    state.active_model = _model_from_payload(payload, previous_model)
                    effort_fallback = "unknown" if state.active_model != previous_model else state.active_effort
                    state.active_effort = _effort_from_payload(payload, effort_fallback)
                if payload_type != "token_count":
                    continue
                info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
                has_usage = isinstance(info.get("total_token_usage"), dict)
                totals = _usage_values(info["total_token_usage"]) if has_usage else (state.previous_totals or _usage_values(None))
                if timestamp is None:
                    continue
                model = state.active_model
                rate_limits = payload.get("rate_limits") if isinstance(payload.get("rate_limits"), dict) else None
                state.events.append(
                    _UsageEvent(
                        timestamp=timestamp,
                        model=model,
                        effort=state.active_effort,
                        totals=totals,
                        values=_delta(totals, state.previous_totals) if has_usage else _usage_values(None),
                        rate_limits=rate_limits,
                    )
                )
                if has_usage:
                    state.previous_totals = totals
    state.offset += consumed
    state.size = stat.st_size
    state.mtime_ns = stat.st_mtime_ns


def _read_files(paths: list[Path]) -> list[_UsageEvent]:
    with _CACHE_LOCK:
        selected = {str(path): path for path in paths}
        for stale in set(_FILE_CACHE) - set(selected):
            _FILE_CACHE.pop(stale, None)
        for key, path in selected.items():
            state = _FILE_CACHE.setdefault(key, _FileState())
            _read_incremental(path, state)
        events: list[_UsageEvent] = []
        seen: set[tuple[object, ...]] = set()
        seen_usage: set[tuple[object, ...]] = set()
        usage_event_indexes: dict[tuple[object, ...], int] = {}
        for state in _FILE_CACHE.values():
            for event in state.events:
                identity = (*_event_identity(event.timestamp, event.model, event.totals), _rate_limit_identity(event.timestamp, event.rate_limits))
                usage_identity = _event_identity(event.timestamp, event.model, event.totals)
                previous_index = usage_event_indexes.get(usage_identity)
                if previous_index is not None and events[previous_index].effort == "unknown" and event.effort != "unknown":
                    events[previous_index] = replace(events[previous_index], effort=event.effort)
                if identity in seen:
                    continue
                seen.add(identity)
                if usage_identity in seen_usage:
                    events.append(replace(event, values=_usage_values(None)))
                else:
                    usage_event_indexes[usage_identity] = len(events)
                    events.append(event)
                seen_usage.add(usage_identity)
        return events


def _latest_rate_limits(events: Iterable[_UsageEvent], *, now: datetime) -> list[dict[str, Any]]:
    latest: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}
    seen: set[tuple[object, ...]] = set()
    for event in events:
        if not event.rate_limits:
            continue
        identity = _rate_limit_identity(event.timestamp, event.rate_limits)
        if identity in seen:
            continue
        seen.add(identity)
        raw_limits = event.rate_limits
        if "primary" in raw_limits or "secondary" in raw_limits:
            limit_id = str(raw_limits.get("limit_id") or "unknown")
            limits = {limit_id: raw_limits}
        else:
            limits = raw_limits
        for limit_id, value in limits.items():
            if not isinstance(value, dict):
                continue
            for scope in ("primary", "secondary"):
                limit = value.get(scope)
                if not isinstance(limit, dict) or limit.get("used_percent") is None:
                    continue
                key = (str(limit_id), scope)
                previous = latest.get(key)
                if previous is None or event.timestamp >= previous[0]:
                    latest[key] = (event.timestamp, dict(limit))
    grouped: dict[str, dict[str, Any]] = {}
    for (limit_id, scope), (observed_at, value) in latest.items():
        row = grouped.setdefault(limit_id, {"limit_id": limit_id, "scope": "account", "observed_at": observed_at})
        if observed_at > row["observed_at"]:
            row["observed_at"] = observed_at
        row[scope] = {
            "used_percent": float(value["used_percent"]),
            "window_minutes": _int(value.get("window_minutes")) or None,
            "resets_at": value.get("resets_at"),
        }
    output: list[dict[str, Any]] = []
    for row in grouped.values():
        observed = _timestamp_datetime(row.get("observed_at")) or now
        age = max(0.0, (now - observed).total_seconds())
        row["age_seconds"] = round(age, 3)
        row["stale"] = age > RATE_LIMIT_STALE_AFTER_SECONDS
        output.append(row)
    return sorted(output, key=lambda row: str(row.get("limit_id")))


def _pricing(model: str) -> dict[str, Any] | None:
    rates = MODEL_PRICING.get(model)
    if rates is None:
        return None
    return {**rates, "currency": "USD", "unit": "USD per 1M tokens", "source_url": PRICING_SOURCE_URL, "observed_at": PRICING_OBSERVED_AT}


def _cost(model: str, values: dict[str, int]) -> tuple[float | None, bool, dict[str, Any] | None]:
    rates = MODEL_PRICING.get(model)
    if rates is None:
        return None, False, None
    input_tokens = values["input_tokens"]
    cached_tokens = min(input_tokens, values["cached_input_tokens"])
    write_tokens = min(input_tokens - cached_tokens, values["cache_write_input_tokens"])
    uncached_tokens = max(0, input_tokens - cached_tokens - write_tokens)
    long_context = input_tokens > LONG_CONTEXT_INPUT_THRESHOLD
    input_rate = rates["input"] * (2.0 if long_context else 1.0)
    cached_rate = rates["cached_input"] * (2.0 if long_context else 1.0)
    output_rate = rates["output"] * (1.5 if long_context else 1.0)
    amount = (
        uncached_tokens * input_rate
        + cached_tokens * cached_rate
        + write_tokens * rates["cache_write_input"] * (2.0 if long_context else 1.0)
        + values["output_tokens"] * output_rate
    ) / 1_000_000
    return amount, long_context, _pricing(model)


def _metric_row(day: str, model: str, values: dict[str, int]) -> dict[str, Any]:
    cost, long_context, pricing = _cost(model, values)
    input_tokens = values["input_tokens"]
    cached_tokens = min(input_tokens, values["cached_input_tokens"])
    row: dict[str, Any] = {
        "date": day,
        "model": model,
        **values,
        "cache_hit_ratio": round(cached_tokens / input_tokens, 6) if input_tokens else 0.0,
        "cache_hit_percent": round((cached_tokens / input_tokens) * 100.0, 3) if input_tokens else 0.0,
        "long_context": long_context,
        "standard_api_equivalent_usd": round(cost, 8) if cost is not None else None,
        "pricing_known": pricing is not None,
        "pricing": pricing,
    }
    return row


def _aggregate_rows(events: Iterable[_UsageEvent]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    by_day_model: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: {field: 0 for field in _TOKEN_FIELDS})
    costs: dict[tuple[str, str], float] = defaultdict(float)
    long_context_keys: set[tuple[str, str]] = set()
    for event in events:
        if not any(event.values.values()):
            continue
        day = event.timestamp[:10]
        key = (day, event.model)
        bucket = by_day_model[key]
        for field in _TOKEN_FIELDS:
            bucket[field] += event.values[field]
        cost, long_context, _ = _cost(event.model, event.values)
        if cost is not None:
            costs[key] += cost
        if long_context:
            long_context_keys.add(key)
    rows = [_metric_row(day, model, values) for (day, model), values in sorted(by_day_model.items())]
    for row in rows:
        key = (row["date"], row["model"])
        row["standard_api_equivalent_usd"] = round(costs[key], 8) if row["pricing_known"] else None
        row["long_context"] = key in long_context_keys
    by_day: dict[str, dict[str, int]] = defaultdict(lambda: {field: 0 for field in _TOKEN_FIELDS})
    by_model: dict[str, dict[str, int]] = defaultdict(lambda: {field: 0 for field in _TOKEN_FIELDS})
    for row in rows:
        for target in (by_day[row["date"]], by_model[row["model"]]):
            for field in _TOKEN_FIELDS:
                target[field] += row[field]
    days = [_metric_row(day, "all models", values) for day, values in sorted(by_day.items())]
    models = [_metric_row("all days", model, values) for model, values in sorted(by_model.items())]
    for aggregate in [*days, *models]:
        members = [row for row in rows if (row["date"] == aggregate["date"] if aggregate["model"] == "all models" else row["model"] == aggregate["model"])]
        known = [row["standard_api_equivalent_usd"] for row in members if row["pricing_known"]]
        aggregate["standard_api_equivalent_usd"] = round(sum(known), 8) if known else None
        aggregate["pricing_known"] = all(row["pricing_known"] for row in members)
        aggregate["long_context"] = any(row["long_context"] for row in members)
    return rows, days, models


def _aggregate_model_effort(events: Iterable[_UsageEvent]) -> list[dict[str, Any]]:
    aggregates: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: {field: 0 for field in _TOKEN_FIELDS})
    event_counts: dict[tuple[str, str], int] = defaultdict(int)
    for event in events:
        if not any(event.values.values()):
            continue
        key = (event.model, event.effort or "unknown")
        for field in _TOKEN_FIELDS:
            aggregates[key][field] += event.values[field]
        event_counts[key] += 1
    return [
        {"model": model, "reasoning_effort": effort, **values, "events_counted": event_counts[(model, effort)]}
        for (model, effort), values in sorted(aggregates.items())
    ]


def build_codex_usage_snapshot(
    *,
    days: int = DEFAULT_DAYS,
    now: datetime | None = None,
    sessions_root: Path | str | None = None,
    archived_root: Path | str | None = None,
) -> dict[str, Any]:
    resolved_days = max(1, min(int(days or DEFAULT_DAYS), MAX_DAYS))
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    session_default, archive_default = _default_roots()
    roots = [Path(sessions_root or session_default), Path(archived_root or archive_default)]
    cutoff = current - timedelta(days=resolved_days)
    paths = _selected_files(roots, cutoff=cutoff, now=current)
    events = [event for event in _read_files(paths) if cutoff <= (_timestamp_datetime(event.timestamp) or cutoff) <= current]
    by_model_day, day_totals, model_totals = _aggregate_rows(events)
    by_model_effort = _aggregate_model_effort(events)
    rates = _latest_rate_limits(events, now=current)
    known_costs = [row["standard_api_equivalent_usd"] for row in by_model_day if row["standard_api_equivalent_usd"] is not None]
    return {
        "ok": True,
        "generated_at": current.isoformat().replace("+00:00", "Z"),
        "window": {"days": resolved_days, "start": cutoff.date().isoformat(), "end": current.date().isoformat()},
        "source": {
            "sessions_root": str(roots[0]),
            "archived_sessions_root": str(roots[1]),
            "files_scanned": len(paths),
            "events_counted": len(events),
            "incremental_cache": True,
            "transmitted": False,
        },
        "pricing": {
            "basis": "STANDARD API equivalent",
            "source_url": PRICING_SOURCE_URL,
            "observed_at": PRICING_OBSERVED_AT,
            "long_context_threshold_input_tokens": LONG_CONTEXT_INPUT_THRESHOLD,
            "models": {model: _pricing(model) for model in sorted(MODEL_PRICING)},
        },
        "rate_limits": rates,
        "by_model_day": by_model_day,
        "by_model_effort": by_model_effort,
        "days": day_totals,
        "models": model_totals,
        "totals": {
            **{field: sum(row[field] for row in by_model_day) for field in _TOKEN_FIELDS},
            "standard_api_equivalent_usd": round(sum(known_costs), 8) if known_costs else (0.0 if not by_model_day else None),
            "pricing_known": all(row["pricing_known"] for row in by_model_day),
        },
    }


def get_codex_usage(*, days: int = DEFAULT_DAYS) -> dict[str, Any]:
    """Return the local usage snapshot used by the web and MCP surfaces."""
    return build_codex_usage_snapshot(days=days)


def reset_codex_usage_cache() -> None:
    with _CACHE_LOCK:
        _FILE_CACHE.clear()
