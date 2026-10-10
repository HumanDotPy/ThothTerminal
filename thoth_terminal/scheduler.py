from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
import logging
import random
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from thoth_terminal.config import settings
from thoth_terminal.dataset_parts.artificial_analysis import enqueue_artificial_analysis_sync_job
from thoth_terminal.db import session_factory, session_scope, utcnow
from thoth_terminal.fx import ensure_fx_rates
from thoth_terminal.ingest import ingest_source
from thoth_terminal.jobs import (
    enqueue_ingest_job,
    ingest_job_priority_for_source,
    reap_stale_jobs,
    rebalance_pending_ingest_job_priorities,
)
from thoth_terminal.models import Source, SourceState


@dataclass(frozen=True)
class SchedulerTick:
    sources_run: int
    docs_new: int


@dataclass(frozen=True)
class _SourceConfigProxy:
    config: dict[str, object]


def _compute_backoff_seconds(consecutive_failures: int) -> int:
    # 1m, 2m, 4m, ... capped at 1h
    return min(3600, 60 * (2 ** max(0, consecutive_failures - 1)))


def _parse_time(value: object) -> time | None:
    if not value:
        return None
    if isinstance(value, time):
        return value
    raw = str(value).strip()
    if not raw:
        return None
    parts = raw.split(":")
    if len(parts) < 2:
        return None
    try:
        hour = int(parts[0])
        minute = int(parts[1])
        second = int(parts[2]) if len(parts) >= 3 else 0
    except ValueError:
        return None
    if hour < 0 or hour > 23 or minute < 0 or minute > 59 or second < 0 or second > 59:
        return None
    return time(hour=hour, minute=minute, second=second)


def _coerce_positive_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        out = int(value)
    except (TypeError, ValueError):
        return None
    return out if out > 0 else None


def _poll_range_seconds(source: Source) -> tuple[int | None, int | None]:
    cfg = source.config or {}
    min_s = _coerce_positive_int(cfg.get("poll_min_seconds"))
    max_s = _coerce_positive_int(cfg.get("poll_max_seconds"))
    if min_s is None and max_s is None:
        base = _coerce_positive_int(source.polling_interval_seconds)
        return (base, base)
    if min_s is not None and max_s is not None and max_s < min_s:
        min_s, max_s = max_s, min_s
    return (min_s, max_s)


def _choose_interval_seconds(source: Source) -> int | None:
    min_s, max_s = _poll_range_seconds(source)
    if min_s is None and max_s is None:
        return None
    if min_s is None:
        return max_s
    if max_s is None:
        return min_s
    if min_s == max_s:
        return min_s
    return random.randint(min_s, max_s)


def _window_config(source: Source | _SourceConfigProxy) -> tuple[time | None, time | None, ZoneInfo | None]:
    cfg = source.config or {}
    start = _parse_time(cfg.get("active_window_start"))
    end = _parse_time(cfg.get("active_window_end"))
    tz_name = cfg.get("active_window_tz") or getattr(settings, "default_active_window_tz", None)
    if not (start and end and tz_name):
        return (None, None, None)
    try:
        tz = ZoneInfo(str(tz_name))
    except ZoneInfoNotFoundError:
        return (None, None, None)
    return (start, end, tz)


def _scheduled_times_config(source: Source | _SourceConfigProxy) -> tuple[list[time], ZoneInfo | None]:
    cfg = source.config or {}
    raw_times = cfg.get("scheduled_local_times") or cfg.get("scheduled_times")
    if isinstance(raw_times, str):
        values = [part.strip() for part in raw_times.split(",")]
    elif isinstance(raw_times, list):
        values = [str(part).strip() for part in raw_times]
    else:
        values = []
    times = sorted({parsed for value in values if (parsed := _parse_time(value)) is not None})
    if not times:
        return ([], None)
    tz_name = cfg.get("scheduled_tz") or cfg.get("active_window_tz") or getattr(settings, "default_active_window_tz", None)
    if not tz_name:
        return ([], None)
    try:
        tz = ZoneInfo(str(tz_name))
    except ZoneInfoNotFoundError:
        return ([], None)
    return (times, tz)


def _next_scheduled_time_utc(after_utc: datetime, scheduled_times: list[time], tz: ZoneInfo) -> datetime | None:
    if not scheduled_times:
        return None
    after_local = after_utc.astimezone(tz)
    candidates: list[datetime] = []
    for day_offset in (0, 1):
        local_date = after_local.date() + timedelta(days=day_offset)
        for local_time in scheduled_times:
            candidate = datetime.combine(local_date, local_time, tzinfo=tz)
            if candidate > after_local:
                candidates.append(candidate)
    if not candidates:
        return None
    return min(candidates).astimezone(timezone.utc)


def _schedule_due_now(
    now_utc: datetime,
    scheduled_times: list[time],
    scheduled_tz: ZoneInfo | None,
    *,
    grace_s: int,
) -> bool:
    if not scheduled_times or scheduled_tz is None:
        return False
    now_local = now_utc.astimezone(scheduled_tz)
    for day_offset in (0, 1):
        local_date = now_local.date() - timedelta(days=day_offset)
        for scheduled_time in scheduled_times:
            due_utc = datetime.combine(local_date, scheduled_time, tzinfo=scheduled_tz).astimezone(timezone.utc)
            if due_utc <= now_utc and (now_utc - due_utc).total_seconds() <= grace_s:
                return True
    return False


def _schedule_next_after(
    after_utc: datetime,
    scheduled_times: list[time],
    scheduled_tz: ZoneInfo | None,
) -> datetime | None:
    if not scheduled_times or scheduled_tz is None:
        return None
    return _next_scheduled_time_utc(after_utc, scheduled_times, scheduled_tz)


def _is_within_window(local_dt: datetime, start: time, end: time) -> bool:
    local_t = local_dt.time()
    if start < end:
        return start <= local_t < end
    # Cross-midnight window (e.g. 07:00 -> 01:00)
    return local_t >= start or local_t < end


def _next_window_start_local(after_local: datetime, start: time) -> datetime:
    candidate = after_local.replace(
        hour=start.hour,
        minute=start.minute,
        second=start.second,
        microsecond=0,
    )
    if candidate <= after_local:
        candidate = candidate + timedelta(days=1)
    return candidate


def _next_window_start_utc(after_utc: datetime, start: time, tz: ZoneInfo) -> datetime:
    after_local = after_utc.astimezone(tz)
    next_local = _next_window_start_local(after_local, start)
    return next_local.astimezone(timezone.utc)


def _get_or_create_state(session: Session, source: Source) -> SourceState:
    state = source.state
    if state:
        return state
    state = SourceState(
        source_id=source.id,
        last_run_at=None,
        last_success_at=None,
        next_run_at=None,
        consecutive_failures=0,
        backoff_until=None,
        cursor={},
        updated_at=utcnow(),
    )
    session.add(state)
    session.flush()
    return state


def run_scheduler_tick(session: Session) -> SchedulerTick:
    now = utcnow()
    now_utc = now.replace(tzinfo=timezone.utc)

    if getattr(settings, "fx_enabled", True):
        try:
            ensure_fx_rates(session, base=str(settings.base_currency or "USD"))
        except Exception:
            pass

    sources = (
        session.query(Source)
        .outerjoin(SourceState, SourceState.source_id == Source.id)
        .filter(Source.polling_interval_seconds.isnot(None))
        .all()
    )

    sources_run = 0
    docs_new = 0

    for source in sources:
        if (source.config or {}).get("deleted"):
            continue
        state = _get_or_create_state(session, source)
        if state.backoff_until and state.backoff_until > now:
            continue
        if state.next_run_at and state.next_run_at > now:
            continue

        start, end, tz = _window_config(source)
        scheduled_times, scheduled_tz = _scheduled_times_config(source)
        if start and end and tz:
            now_local = now_utc.astimezone(tz)
            if not _is_within_window(now_local, start, end):
                state.next_run_at = _next_window_start_utc(now_utc, start, tz).replace(tzinfo=None)
                state.updated_at = utcnow()
                session.commit()
                continue
        if scheduled_times and scheduled_tz:
            grace_s = _coerce_positive_int((source.config or {}).get("scheduled_grace_seconds")) or 20 * 60
            if not _schedule_due_now(now_utc, scheduled_times, scheduled_tz, grace_s=grace_s):
                next_due = _schedule_next_after(now_utc, scheduled_times, scheduled_tz)
                if next_due is not None:
                    state.next_run_at = next_due.replace(tzinfo=None)
                    state.updated_at = utcnow()
                    session.commit()
                continue

        result = ingest_source(session, source)
        sources_run += 1
        docs_new += result.new

        if scheduled_times and scheduled_tz:
            next_due = _schedule_next_after(now_utc + timedelta(seconds=1), scheduled_times, scheduled_tz)
            if next_due is not None:
                state.next_run_at = next_due.replace(tzinfo=None)
        else:
            interval_s = _choose_interval_seconds(source)
            if interval_s:
                next_utc = now_utc + timedelta(seconds=interval_s)
                if start and end and tz:
                    next_local = next_utc.astimezone(tz)
                    if not _is_within_window(next_local, start, end):
                        next_utc = _next_window_start_utc(next_utc, start, tz)
                state.next_run_at = next_utc.replace(tzinfo=None)

        if state.consecutive_failures:
            backoff = _compute_backoff_seconds(int(state.consecutive_failures))
            state.backoff_until = now + timedelta(seconds=backoff)
        state.updated_at = utcnow()

        session.commit()

    return SchedulerTick(sources_run=sources_run, docs_new=docs_new)


def enqueue_scheduler_tick_no_lock() -> SchedulerTick:
    now = utcnow()
    now_utc = now.replace(tzinfo=timezone.utc)
    SessionLocal = session_factory()

    # Reap stale running ingest jobs so new runs aren't blocked by dedupe.
    with session_scope() as session:
        reaped = reap_stale_jobs(session, kind="ingest_source")
        rebalance_pending_ingest_job_priorities(session)
        enqueue_artificial_analysis_sync_job(session)
        if reaped:
            logging.getLogger("thoth.scheduler").warning("Reaped %s stale ingest jobs", reaped)

    # Snapshot sources + state without holding the write lock.
    with SessionLocal() as session:
        sources = (
            session.query(Source)
            .outerjoin(SourceState, SourceState.source_id == Source.id)
            .filter(Source.polling_interval_seconds.isnot(None))
            .all()
        )

        snapshots: list[dict[str, object]] = []
        for source in sources:
            state = source.state
            snapshots.append(
                {
                    "id": source.id,
                    "config": dict(source.config or {}),
                    "polling_interval_seconds": source.polling_interval_seconds,
                    "state": {
                        "last_run_at": state.last_run_at if state else None,
                        "last_success_at": state.last_success_at if state else None,
                        "next_run_at": state.next_run_at if state else None,
                        "consecutive_failures": state.consecutive_failures if state else 0,
                        "backoff_until": state.backoff_until if state else None,
                    },
                }
            )

    sources_run = 0
    docs_new = 0

    for snap in snapshots:
        cfg = dict(snap["config"] or {})
        source_id = int(snap["id"])
        state_data = snap["state"] or {}
        backoff_until = state_data.get("backoff_until")
        next_run_at = state_data.get("next_run_at")

        if cfg.get("deleted"):
            continue
        enabled_flag = cfg.get("enabled")
        if enabled_flag is False or cfg.get("disabled_reason"):
            continue

        if backoff_until and backoff_until > now:
            continue
        if next_run_at and next_run_at > now:
            continue

        start, end, tz = _window_config(_SourceConfigProxy(cfg))
        scheduled_times, scheduled_tz = _scheduled_times_config(_SourceConfigProxy(cfg))
        if start and end and tz:
            now_local = now_utc.astimezone(tz)
            if not _is_within_window(now_local, start, end):
                with session_scope() as session:
                    src = session.query(Source).filter_by(id=source_id).first()
                    if src:
                        state = _get_or_create_state(session, src)
                        state.next_run_at = _next_window_start_utc(now_utc, start, tz).replace(tzinfo=None)
                        state.updated_at = utcnow()
                continue
        if scheduled_times and scheduled_tz:
            grace_s = _coerce_positive_int(cfg.get("scheduled_grace_seconds")) or 20 * 60
            if not _schedule_due_now(now_utc, scheduled_times, scheduled_tz, grace_s=grace_s):
                next_due = _schedule_next_after(now_utc, scheduled_times, scheduled_tz)
                if next_due is not None:
                    with session_scope() as session:
                        src = session.query(Source).filter_by(id=source_id).first()
                        if src:
                            state = _get_or_create_state(session, src)
                            state.next_run_at = next_due.replace(tzinfo=None)
                            state.updated_at = utcnow()
                continue

        # Enqueue ingestion under lock.
        with session_scope() as session:
            src = session.query(Source).filter_by(id=source_id).first()
            if not src:
                continue
            enqueue_ingest_job(session, source_id=src.id, priority=ingest_job_priority_for_source(src))
            sources_run += 1

            state = _get_or_create_state(session, src)
            if scheduled_times and scheduled_tz:
                next_due = _schedule_next_after(now_utc + timedelta(seconds=1), scheduled_times, scheduled_tz)
                if next_due is not None:
                    state.next_run_at = next_due.replace(tzinfo=None)
            else:
                interval_s = _choose_interval_seconds(src)
                if interval_s:
                    next_utc = now_utc + timedelta(seconds=interval_s)
                    if start and end and tz:
                        next_local = next_utc.astimezone(tz)
                        if not _is_within_window(next_local, start, end):
                            next_utc = _next_window_start_utc(next_utc, start, tz)
                    state.next_run_at = next_utc.replace(tzinfo=None)
            if state.consecutive_failures:
                backoff = _compute_backoff_seconds(int(state.consecutive_failures))
                state.backoff_until = now + timedelta(seconds=backoff)
            state.updated_at = utcnow()

    return SchedulerTick(sources_run=sources_run, docs_new=docs_new)


def run_scheduler_tick_no_lock() -> SchedulerTick:
    # Backwards-compatible entrypoint: enqueue jobs instead of doing heavy ingest.
    return enqueue_scheduler_tick_no_lock()
