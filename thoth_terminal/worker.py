from __future__ import annotations

import gc
import hashlib
import logging
import os
from pathlib import Path
import threading
import time
from collections import defaultdict
from typing import Any

from thoth_terminal.config import settings
from thoth_terminal.dataset_parts.artificial_analysis import (
    ARTIFICIAL_ANALYSIS_SYNC_JOB, enqueue_artificial_analysis_sync_job, handle_artificial_analysis_sync_job,
)
from thoth_terminal.connectors import fetch_items_result
from thoth_terminal.db import DatabaseBusyError, session_factory, session_scope, utcnow
from thoth_terminal.ingest import ingest_source
from thoth_terminal.institutional_service import (
    handle_institutional_closure_pass_job,
    handle_institutional_refresh_job,
)
from thoth_terminal.jobs import (
    claim_next_job,
    enqueue_due_research_play_jobs,
    enqueue_discord_watchlist_refresh_hourly,
    enqueue_postprocess_job,
    enqueue_watchlist_transcript_scan_gap_check,
    enqueue_watchlist_transcript_scan_regular,
    finish_job,
    next_pending_job_delay_s,
    recover_interrupted_running_jobs,
    reap_stale_jobs,
    rebalance_pending_ingest_job_priorities,
)
from thoth_terminal.job_notifications import JobNotificationListener
from thoth_terminal.cloud_research_automation import (
    CLOUD_RESEARCH_AUTOMATION_JOB,
    enqueue_due_cloud_research_automation_jobs,
    run_cloud_research_automation_job,
)
from thoth_terminal.cloud_goal_run_service import fail_stale_cloud_goal_runs
from thoth_terminal.models import Job, Source, SourceState
from thoth_terminal.postprocess import postprocess_document
from thoth_terminal.scheduler import enqueue_scheduler_tick_no_lock

from thoth_terminal.discord.agent_jobs import (
    enqueue_daily_digest_if_due,
    handle_agent_context_compression,
    handle_agent_desk_digest,
    handle_agent_ticker_timeline,
)
from thoth_terminal.foundation_service import upsert_transcript_record
from thoth_terminal.transcript_watchlist import handle_watchlist_transcript_scan_job
from thoth_terminal.agent.thesis_autopilot import run_autonomous_thesis_cycle
from thoth_terminal.report_jobs import (
    enqueue_report_freshness_scan,
    handle_research_report_build,
    handle_research_report_freshness_scan,
)
from thoth_terminal.knowledge_jobs import (
    enqueue_due_freshness_reviews,
    handle_freshness_review,
    handle_knowledge_asset_draft,
)
from thoth_terminal.runtime_info import runtime_fingerprint
from thoth_terminal.runtime_health import write_service_heartbeat
from thoth_terminal.research_play_service import execute_research_play
from thoth_terminal.newsletter_service import (
    NEWSLETTER_DELIVERY_JOB,
    handle_newsletter_delivery_job,
)


logger = logging.getLogger("thoth.worker")
_CLOUD_WORKER_JOB_KINDS = {CLOUD_RESEARCH_AUTOMATION_JOB, ARTIFICIAL_ANALYSIS_SYNC_JOB}


def _current_private_memory_mb() -> float | None:
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class _ProcessMemoryCountersEx(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                    ("PrivateUsage", ctypes.c_size_t),
                ]

            counters = _ProcessMemoryCountersEx()
            counters.cb = ctypes.sizeof(counters)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            psapi.GetProcessMemoryInfo.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(_ProcessMemoryCountersEx),
                wintypes.DWORD,
            ]
            psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
            ok = psapi.GetProcessMemoryInfo(
                kernel32.GetCurrentProcess(),
                ctypes.byref(counters),
                counters.cb,
            )
            if ok:
                return float(counters.PrivateUsage) / (1024 * 1024)
        except Exception:
            return None
        return None

    status_path = Path("/proc/self/status")
    try:
        for line in status_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("VmRSS:"):
                parts = line.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    return float(parts[1]) / 1024
    except Exception:
        return None
    return None


def _worker_memory_recycle_requested(*, context: str, process_limit_mb: int | None = None) -> bool:
    limit_mb = int(getattr(settings, "worker_memory_recycle_mb", 0) or 0)
    if limit_mb <= 0:
        return False
    hard_limit_mb = max(256, int(process_limit_mb or getattr(settings, "worker_process_memory_limit_mb", 3072) or 3072))
    # Leave room for the next job and reclaim before the supervisor's hard cap.
    # A recycle threshold above the hard cap can never provide graceful exit.
    limit_mb = min(limit_mb, int(hard_limit_mb * 0.8))
    private_mb = _current_private_memory_mb()
    if private_mb is None or private_mb < float(limit_mb):
        return False

    gc.collect()
    private_after_gc_mb = _current_private_memory_mb() or private_mb
    if private_after_gc_mb < float(limit_mb):
        logger.info(
            "worker memory recovered after gc context=%s private_mb=%.1f limit_mb=%s",
            context,
            private_after_gc_mb,
            limit_mb,
        )
        return False

    logger.warning(
        "worker memory recycle requested context=%s private_mb=%.1f limit_mb=%s",
        context,
        private_after_gc_mb,
        limit_mb,
    )
    return True


def _fetch_for_source(
    cfg: dict[str, Any],
    *,
    last_success_at=None,
    source_cursor: dict[str, Any] | None = None,
) -> tuple[list[dict], Exception | None, dict[str, Any] | None, dict[str, Any] | None]:
    try:
        fetch_cfg = dict(cfg)
        if cfg.get("connector") == "web_list" and last_success_at is None:
            bootstrap_days = fetch_cfg.get("bootstrap_days")
            if bootstrap_days and fetch_cfg.get("recent_days") is None:
                fetch_cfg["recent_days"] = bootstrap_days
        result = fetch_items_result(
            cfg.get("connector"),
            source_config=fetch_cfg,
            source_cursor=(dict(source_cursor or {}) if isinstance(source_cursor, dict) else {}),
        )
        return result.items, None, (dict(result.cursor) if isinstance(result.cursor, dict) else None), (
            dict(result.stats) if isinstance(result.stats, dict) else None
        )
    except Exception as exc:
        return [], exc, None, None


def _handle_ingest_job(payload: dict[str, Any]) -> None:
    source_id = payload.get("source_id")
    if not source_id:
        return

    SessionLocal = session_factory()
    with SessionLocal() as session:
        src = session.query(Source).filter_by(id=int(source_id)).first()
        if not src:
            return
        cfg = dict(src.config or {})
        cfg["_source_id"] = int(src.id)
        cfg["_source_name"] = str(src.name or "")
        if payload.get("_job_id") is not None:
            cfg["_job_id"] = payload.get("_job_id")
        state = session.query(SourceState).filter_by(source_id=src.id).first()
        last_success_at = state.last_success_at if state else None
        source_cursor = dict(state.cursor or {}) if state and isinstance(state.cursor, dict) else {}

    items, fetch_error, next_cursor, fetch_stats = _fetch_for_source(
        cfg, last_success_at=last_success_at, source_cursor=source_cursor
    )

    ingest_failure: str | None = None
    with session_scope() as session:
        src = session.query(Source).filter_by(id=int(source_id)).first()
        if not src:
            return
        result = ingest_source(
            session,
            src,
            prefetched_items=items,
            prefetched_error=fetch_error,
            prefetched_cursor=next_cursor,
            prefetched_stats=fetch_stats,
            defer_heavy=True,
            defer_derived_sync=bool(cfg.get("defer_ingest_derived_sync") or cfg.get("defer_derived_sync")),
            defer_institutional_sync=bool(
                cfg.get("defer_ingest_institutional_sync") or cfg.get("defer_institutional_sync")
            ),
            enqueue_postprocess=enqueue_postprocess_job,
        )
        if result.status != "success":
            ingest_failure = result.error or f"ingestion run {result.run_id or 'unknown'} failed"

    if ingest_failure:
        raise RuntimeError(ingest_failure)


def _handle_x_thread_ingest_job(payload: dict[str, Any]) -> None:
    from thoth_terminal.browser_ingestion_runtime import run_browser_ingestion
    from thoth_terminal.connectors.x_list import fetch_x_thread
    from thoth_terminal.ingest import ingest_tweet
    from thoth_terminal.discord.link_intake import canonical_x_status_url

    top_url = canonical_x_status_url(str(payload.get("top_url") or ""))
    bottom_url = canonical_x_status_url(str(payload.get("bottom_url") or "")) if payload.get("bottom_url") else None
    if not top_url or (payload.get("bottom_url") and not bottom_url):
        raise ValueError("Invalid X thread job bounds")
    SessionLocal = session_factory()
    with SessionLocal() as session:
        sources = session.query(Source).all()
        source = next((row for row in sources if (row.config or {}).get("connector") == "x_list"
                       and (row.config or {}).get("list_url")
                       and (row.config or {}).get("enabled", True)
                       and not (row.config or {}).get("deleted")), None)
        browser_config = dict(source.config or {}) if source else {}
    # The isolated browser child imports the callable by module/name; closures
    # cannot cross that boundary. Keep thread bounds in its serialized config.
    browser_config.update(status_url=top_url, bottom_url=bottom_url)
    item = run_browser_ingestion(fetch_x_thread, browser_config)
    extra = dict(item.get("extra") or {})
    published_at = item.get("published_at")
    if hasattr(published_at, "isoformat"):
        published_at = published_at.isoformat()
    root_text = str(extra.get("root_text") or item.get("raw_text") or "")
    capture = extra.get("thread_capture") or {}
    if capture.get("coverage") == "partial_with_unverified_middle":
        root_text += "\n\nCapture note: the requested end post was verified, but the middle of this X thread could not be fully checked."
    tweet_payload = {
        **extra,
        "url": top_url,
        "tweet_id": item.get("external_id"),
        "text": root_text,
        "author": item.get("author"),
        "handle": extra.get("handle"),
        "created_at": published_at,
        "image_urls": item.get("image_urls") or extra.get("media") or [],
        "requested_by_discord_user_id": payload.get("requested_by_user_id"),
    }
    with session_scope() as session:
        doc = ingest_tweet(
            session,
            payload=tweet_payload,
            defer_heavy=True,
            enqueue_postprocess=enqueue_postprocess_job,
        )
        if doc is None:
            raise RuntimeError("X thread did not produce a document")


def _handle_postprocess_job(payload: dict[str, Any]) -> None:
    doc_id = payload.get("doc_id")
    if not doc_id:
        return
    source_id = payload.get("source_id")
    with session_scope() as session:
        postprocess_document(session, doc_id=int(doc_id), source_id=int(source_id) if source_id else None)


def _handle_discord_watchlist_refresh_job(payload: dict[str, Any]) -> None:
    guild_id = payload.get("guild_id")
    if guild_id is None:
        return
    from thoth_terminal.discord import services as discord_services

    result = discord_services.refresh_guild_watchlists(guild_id=int(guild_id), force_refresh=True)
    if result.error:
        raise RuntimeError(result.error)


def _active_watchlist_symbols(*, guild_id: int) -> list[str]:
    from thoth_terminal.discord.models import DiscordWatchlistEntry

    SessionLocal = session_factory()
    with SessionLocal() as session:
        rows = (
            session.query(DiscordWatchlistEntry.symbol)
            .filter_by(guild_id=int(guild_id), removed_at=None)
            .distinct()
            .all()
        )
    out = sorted(
        {
            str(row[0]).strip().upper()
            for row in rows
            if row and row[0] is not None and str(row[0]).strip()
        }
    )
    return out


def _transcript_marker_key(*, guild_id: int, symbol: str, source_id: int | str | None, url: str) -> str:
    dedupe_source = str(url or source_id or "").strip()
    digest = hashlib.sha1(dedupe_source.encode("utf-8")).hexdigest()[:16] if dedupe_source else "na"
    return f"transcript:{int(guild_id)}:{symbol}:{digest}"


def _format_transcript_scan_summary(rows: list[dict[str, str]]) -> str:
    if not rows:
        return ""
    grouped: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in rows:
        ticker = str(row.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        period = str(row.get("period") or "Latest").strip() or "Latest"
        published = str(row.get("published") or "unknown").strip() or "unknown"
        grouped[(period, published)].add(ticker)
    if not grouped:
        return ""
    lines: list[str] = [f"Auto-ingested transcripts for {len(rows)} ticker(s):"]
    for period, published in sorted(grouped.keys(), key=lambda key: (key[1], key[0]), reverse=True):
        ticker_list = ", ".join(f"${ticker}" for ticker in sorted(grouped[(period, published)]))
        lines.append(f"{period} • Published {published}: {ticker_list}")
    max_len = 1800
    kept: list[str] = [lines[0]]
    omitted = 0
    for line in lines[1:]:
        candidate = "\n".join(kept + [line])
        if len(candidate) > max_len:
            omitted += 1
            continue
        kept.append(line)
    if omitted:
        kept.append(f"...and {omitted} more group(s).")
    return "\n".join(kept)


def _handle_discord_watchlist_transcript_scan_job(payload: dict[str, Any]) -> None:
    guild_id = payload.get("guild_id")
    if guild_id is None:
        return
    guild_v = int(guild_id)
    mode_v = str(payload.get("mode") or "hourly").strip().lower()
    if mode_v == "immediate":
        return
    lookback_raw = payload.get("lookback_days")
    if lookback_raw is not None and str(lookback_raw).isdigit():
        lookback_days = max(1, int(lookback_raw))
    else:
        lookback_days = max(
            1,
            int(getattr(settings, "discord_transcript_scan_lookback_days", 120) or 120),
        )

    from thoth_terminal.discord import desk as desk_mod
    from thoth_terminal.discord.models import DiscordOutboxItem
    from thoth_terminal.discord import outbox as outbox_mod
    from thoth_terminal.transcripts import get_transcript

    symbols = _active_watchlist_symbols(guild_id=guild_v)
    if not symbols:
        return

    desk_cfg = desk_mod.get_desk_settings(guild_id=guild_v)
    if not desk_cfg.desk_enabled:
        return
    channel_id = desk_cfg.desk_channel_id or getattr(settings, "discord_channel_id", None)
    if not channel_id:
        return
    thread_id = desk_cfg.desk_thread_id if desk_cfg.desk_thread_id else None

    SessionLocal = session_factory()
    new_rows: list[dict[str, str]] = []
    with SessionLocal() as session:
        existing_marker_keys = {
            str(row[0])
            for row in (
                session.query(DiscordOutboxItem.dedupe_key)
                .filter(DiscordOutboxItem.guild_id == guild_v)
                .filter(DiscordOutboxItem.dedupe_key.isnot(None))
                .filter(DiscordOutboxItem.dedupe_key.like(f"transcript:{guild_v}:%"))
                .all()
            )
            if row and row[0]
        }
        for symbol in symbols:
            row = get_transcript(
                session,
                ticker=symbol,
                most_recent=True,
                lookback_days=lookback_days,
            )
            if not row:
                continue
            try:
                upsert_transcript_record(session, row=row)
            except Exception:
                # Keep notifications alive even if transcript persistence fails for one symbol.
                pass
            marker_key = _transcript_marker_key(
                guild_id=guild_v,
                symbol=str(symbol).strip().upper(),
                source_id=row.source_id,
                url=row.url,
            )
            if marker_key in existing_marker_keys:
                continue
            existing_marker_keys.add(marker_key)
            new_rows.append(
                {
                    "ticker": str(row.ticker or symbol).strip().upper(),
                    "period": str(row.period_label or "Latest"),
                    "published": row.published_at.strftime("%Y-%m-%d") if row.published_at else "unknown",
                    "marker_key": marker_key,
                }
            )
    if not new_rows:
        return

    now = utcnow()
    with session_scope() as session:
        for item in new_rows:
            marker_key = str(item.get("marker_key") or "").strip()
            if not marker_key:
                continue
            session.add(
                DiscordOutboxItem(
                    guild_id=guild_v,
                    channel_id=int(channel_id),
                    thread_id=int(thread_id) if thread_id else None,
                    status="posted",
                    priority=5,
                    dedupe_key=marker_key,
                    content=None,
                    embed_json=None,
                    run_after=None,
                    locked_at=None,
                    attempts=0,
                    max_attempts=1,
                    last_error=None,
                    created_at=now,
                    created_by_kind="system",
                    created_by_user_id=None,
                    posted_at=now,
                    discord_message_id=None,
                )
            )

    summary = _format_transcript_scan_summary(new_rows)
    if not summary:
        return
    marker_seed = "|".join(sorted(str(item.get("marker_key") or "") for item in new_rows))
    summary_sig = hashlib.sha1(marker_seed.encode("utf-8")).hexdigest()[:16]
    outbox_mod.create_outbox_item(
        guild_id=guild_v,
        channel_id=int(channel_id),
        thread_id=int(thread_id) if thread_id else None,
        content=summary,
        dedupe_key=f"transcript-summary:{guild_v}:{summary_sig}",
        created_by_kind="system",
        created_by_user_id=None,
    )


def _handle_agent_thesis_autopilot_job(payload: dict[str, Any]) -> None:
    thesis_id = payload.get("thesis_id")
    lookback_hours = int(payload.get("lookback_hours") or 72)
    guild_id = payload.get("guild_id")
    with session_scope() as session:
        run_autonomous_thesis_cycle(
            session,
            thesis_id=int(thesis_id) if thesis_id is not None else None,
            guild_id=int(guild_id) if guild_id is not None else None,
            lookback_hours=max(1, lookback_hours),
            source_job_id=int(payload.get("_job_id")) if str(payload.get("_job_id")).isdigit() else None,
        )


def _handle_research_play_job(payload: dict[str, Any]) -> None:
    play_id = payload.get("play_id")
    if play_id is None or not str(play_id).isdigit():
        return
    trigger_kind = str(payload.get("trigger_kind") or "manual").strip().lower() or "manual"
    job_id = int(payload.get("_job_id")) if str(payload.get("_job_id") or "").isdigit() else None
    with session_scope() as session:
        execute_research_play(
            session,
            play_id=int(play_id),
            trigger_kind=trigger_kind,
            trigger_job_id=job_id,
        )


def _stop_requested(stop_event: object | None) -> bool:
    return bool(stop_event is not None and getattr(stop_event, "is_set", lambda: False)())


def _sleep_or_stop(seconds: float, stop_event: object | None) -> None:
    if stop_event is None:
        time.sleep(max(0.0, seconds))
        return
    waiter = getattr(stop_event, "wait", None)
    if callable(waiter):
        waiter(max(0.0, seconds))
        return
    time.sleep(max(0.0, seconds))


def _min_positive_delay(*delays: float | None) -> float | None:
    candidates = [float(delay) for delay in delays if delay is not None and float(delay) >= 0.0]
    if not candidates:
        return None
    return min(candidates)


def run_cloud_worker(*, poll_s: float | None = None, stop_event: object | None = None) -> None:
    """Run one cloud API job at a time independently of ingest and local work."""
    logger.info("cloud worker lane startup: %s", runtime_fingerprint())
    poll_s = max(0.25, float(poll_s if poll_s is not None else getattr(settings, "worker_poll_interval_s", 2.0) or 2.0))
    scheduler_interval_s = max(5, int(getattr(settings, "worker_scheduler_interval_s", 60) or 60))
    job_listener = JobNotificationListener()
    next_sched = 0.0
    last_heartbeat = 0.0
    last_scheduler_at: str | None = None
    last_job: dict[str, Any] | None = None
    with session_scope() as session:
        recovered_jobs = sum(
            recover_interrupted_running_jobs(
                session, kind=kind, reason="cloud worker startup recovered interrupted running job",
                resume=kind == CLOUD_RESEARCH_AUTOMATION_JOB,
            )
            for kind in sorted(_CLOUD_WORKER_JOB_KINDS)
        )
        recovered_run_ids = fail_stale_cloud_goal_runs(
            session,
            task_kinds={
                "cloud_research_nightly",
                "cloud_research_heartbeat",
                "cloud_research_inquisitive",
            },
            stale_after_seconds=max(1800, int(getattr(settings, "cloud_research_timeout_s", 1200.0) or 1200.0) * 3),
        )
    if recovered_run_ids:
        logger.warning("cloud worker closed %s abandoned run(s): %s", len(recovered_run_ids), recovered_run_ids)
    if recovered_jobs:
        logger.warning("cloud worker recovered %s interrupted job(s)", recovered_jobs)
    while not _stop_requested(stop_event):
        now = time.time()
        if now >= next_sched:
            try:
                with session_scope() as session:
                    enqueue_due_cloud_research_automation_jobs(session)
                    enqueue_artificial_analysis_sync_job(session)
                last_scheduler_at = utcnow().isoformat()
            except DatabaseBusyError as exc:
                logger.info("cloud worker scheduler delayed because database is busy: %s", exc)
            except Exception:
                logger.exception("cloud worker scheduler tick failed")
            next_sched = now + scheduler_interval_s

        if now - last_heartbeat >= 15.0:
            try:
                write_service_heartbeat(
                    "cloud-worker",
                    status="healthy" if not last_job or bool(last_job.get("ok")) else "degraded",
                    details={
                        "scheduler_last_tick_at": last_scheduler_at,
                        "last_job": last_job,
                        "thread": threading.current_thread().name,
                    },
                )
            except Exception:
                logger.warning("cloud worker heartbeat write failed", exc_info=True)
            last_heartbeat = now

        job_id = None
        kind = None
        payload: dict[str, Any] = {}
        try:
            with session_scope() as session:
                job = claim_next_job(session, kind=ARTIFICIAL_ANALYSIS_SYNC_JOB)
                if job is None:
                    job = claim_next_job(session, kind=CLOUD_RESEARCH_AUTOMATION_JOB)
                if job is not None:
                    job_id = int(job.id)
                    kind = job.kind
                    payload = dict(job.payload or {}) if isinstance(job.payload, dict) else {}
                    payload["_job_id"] = job_id
        except DatabaseBusyError as exc:
            logger.info("cloud worker delayed job claim because database is busy: %s", exc)

        if job_id is None:
            job_listener.wait(min(poll_s, max(0.0, next_sched - time.time())), stop_event)
            continue

        ok = True
        err = None
        job_heartbeat_stop = threading.Event()
        active_job_started_at = utcnow().isoformat()

        def _publish_running_job_heartbeat() -> None:
            while not job_heartbeat_stop.is_set():
                try:
                    write_service_heartbeat(
                        "cloud-worker",
                        status="running",
                        details={
                            "scheduler_last_tick_at": last_scheduler_at,
                            "active_job": {"id": job_id, "started_at": active_job_started_at},
                            "configured_timeout_s": float(getattr(settings, "cloud_research_timeout_s", 1200.0) or 1200.0),
                            "thread": threading.current_thread().name,
                        },
                    )
                except Exception:
                    logger.warning("active cloud job heartbeat write failed", exc_info=True)
                job_heartbeat_stop.wait(15.0)

        job_heartbeat_thread = threading.Thread(
            target=_publish_running_job_heartbeat,
            name="thoth-cloud-job-heartbeat",
            daemon=True,
        )
        job_heartbeat_thread.start()
        try:
            if kind == ARTIFICIAL_ANALYSIS_SYNC_JOB:
                handle_artificial_analysis_sync_job(payload)
            else:
                run_cloud_research_automation_job(payload)
        except Exception as exc:
            ok = False
            err = f"{type(exc).__name__}: {exc}"
        finally:
            job_heartbeat_stop.set()
            job_heartbeat_thread.join(timeout=2.0)

        try:
            with session_scope() as session:
                job = session.query(Job).filter_by(id=job_id).first()
                if job is not None:
                    finish_job(session, job=job, ok=ok, error=err)
                enqueue_artificial_analysis_sync_job(session)
            last_job = {
                "id": job_id,
                "ok": ok,
                "finished_at": utcnow().isoformat(),
                "error": str(err or "")[:500] or None,
            }
            last_heartbeat = 0.0
            # Returning from an embedded cloud thread would silently disable
            # that lane. The main worker owns process recycling in that mode.
            if threading.current_thread() is threading.main_thread() and _worker_memory_recycle_requested(
                context="after_cloud_job",
                process_limit_mb=int(getattr(settings, "cloud_worker_process_memory_limit_mb", 3072) or 3072),
            ):
                return
        except DatabaseBusyError as exc:
            logger.info("cloud worker could not finish job %s because database is busy: %s", job_id, exc)


def run_worker_with_cloud_lane(
    *,
    poll_s: float | None = None,
    scheduler_interval_s: int | None = None,
    max_jobs: int | None = None,
    stop_event: object | None = None,
) -> None:
    # The cloud lane owns its event. Setting an externally supplied desktop
    # stop event in finally made an unexpected worker exit permanent and
    # prevented the desktop from restarting the lane.
    lane_stop_event = threading.Event()
    worker_stop_event = stop_event if stop_event is not None else lane_stop_event
    cloud_thread = threading.Thread(
        target=run_cloud_worker,
        kwargs={"poll_s": poll_s, "stop_event": lane_stop_event},
        name="thoth-cloud-worker",
        daemon=True,
    )
    cloud_thread.start()
    try:
        run_worker(
            poll_s=poll_s,
            scheduler_interval_s=scheduler_interval_s,
            max_jobs=max_jobs,
            stop_event=worker_stop_event,
        )
    finally:
        lane_stop_event.set()
        cloud_thread.join(timeout=5.0)


def run_worker(
    *,
    poll_s: float | None = None,
    scheduler_interval_s: int | None = None,
    max_jobs: int | None = None,
    stop_event: object | None = None,
) -> None:
    logger.info("worker startup: %s", runtime_fingerprint())
    try:
        with session_scope() as session:
            recovered = (
                recover_interrupted_running_jobs(session, exclude_kinds=_CLOUD_WORKER_JOB_KINDS)
                if bool(getattr(settings, "worker_requeue_running_jobs_on_startup", True))
                else 0
            )
            reaped = reap_stale_jobs(session, exclude_kinds=_CLOUD_WORKER_JOB_KINDS)
            rebalanced = rebalance_pending_ingest_job_priorities(session)
    except DatabaseBusyError as exc:
        logger.warning("worker startup skipped stale-job reap because database is busy: %s", exc)
        recovered = 0
        reaped = 0
        rebalanced = 0
    if recovered:
        logger.warning("worker startup recovered %s interrupted running jobs", recovered)
    if reaped:
        logger.warning("worker startup reaped %s stale jobs", reaped)
    if rebalanced:
        logger.info("worker startup rebalanced %s pending ingest job priorities", rebalanced)
    poll_s = float(poll_s if poll_s is not None else getattr(settings, "worker_poll_interval_s", 2.0) or 2.0)
    job_listener = JobNotificationListener()
    scheduler_interval_s = int(
        scheduler_interval_s
        if scheduler_interval_s is not None
        else getattr(settings, "worker_scheduler_interval_s", 60) or 60
    )
    max_jobs = int(max_jobs if max_jobs is not None else getattr(settings, "worker_max_jobs_per_tick", 6) or 6)
    watchlist_refresh_interval_s = max(
        60,
        int(getattr(settings, "discord_watchlist_refresh_interval_minutes", 60) or 60) * 60,
    )
    transcript_scan_interval_s = max(
        60,
        int(getattr(settings, "transcript_scan_interval_minutes", 360) or 360) * 60,
    )
    transcript_gap_scan_interval_s = max(
        24 * 60 * 60,
        int(getattr(settings, "transcript_gap_scan_interval_minutes", 10080) or 10080) * 60,
    )
    research_report_scan_interval_s = max(3600, 12 * 60 * 60)
    freshness_review_scan_interval_s = max(3600, 12 * 60 * 60)

    # Claim already-queued work immediately, but stagger maintenance producers.
    # Starting every scan at zero created a thundering herd after each restart.
    started_at = time.time()
    next_sched = started_at
    next_watchlist_refresh = started_at + min(60.0, watchlist_refresh_interval_s)
    next_transcript_scan = started_at + min(120.0, transcript_scan_interval_s)
    next_report_scan = started_at + min(300.0, research_report_scan_interval_s)
    next_freshness_scan = started_at + min(450.0, freshness_review_scan_interval_s)
    next_transcript_gap_scan = started_at + min(600.0, transcript_gap_scan_interval_s)
    idle_loops = 0
    last_heartbeat = 0.0
    last_job: dict[str, Any] | None = None
    while not _stop_requested(stop_event):
        now = time.time()
        if now - last_heartbeat >= 15.0:
            try:
                write_service_heartbeat(
                    "worker",
                    status="healthy",
                    details={"last_job": last_job, "thread": threading.current_thread().name},
                )
            except Exception:
                logger.warning("worker heartbeat write failed", exc_info=True)
            last_heartbeat = now
        if now >= next_sched:
            try:
                enqueue_scheduler_tick_no_lock()
            except DatabaseBusyError as exc:
                logger.info("worker scheduler delayed because database is busy: %s", exc)
                next_sched = now + max(poll_s, 5.0)
                _sleep_or_stop(poll_s, stop_event)
                continue
            except Exception:
                logger.exception("worker scheduler tick failed")
                next_sched = now + max(poll_s, 5.0)
                _sleep_or_stop(poll_s, stop_event)
                continue
            try:
                enqueue_daily_digest_if_due()
            except Exception:
                # Agent scheduler should never take the worker down.
                pass
            if bool(getattr(settings, "discord_watchlist_refresh_enabled", True)) and now >= next_watchlist_refresh:
                try:
                    from thoth_terminal.discord import services as discord_services

                    guild_ids = discord_services.list_active_watchlist_guild_ids()
                    with session_scope() as session:
                        for guild_id in guild_ids:
                            enqueue_discord_watchlist_refresh_hourly(session, guild_id=int(guild_id))
                except Exception:
                    # Keep worker healthy even when Discord market refresh scheduling fails.
                    pass
                next_watchlist_refresh = now + watchlist_refresh_interval_s
            if bool(getattr(settings, "transcript_scan_enabled", True)) and now >= next_transcript_scan:
                try:
                    lookback_days = max(
                        1,
                        int(getattr(settings, "transcript_scan_lookback_days", 120) or 120),
                    )
                    max_candidates = max(1, int(getattr(settings, "transcript_scan_max_candidates_per_run", 40) or 40))
                    with session_scope() as session:
                        enqueue_watchlist_transcript_scan_regular(
                            session,
                            lookback_days=lookback_days,
                            max_candidates=max_candidates,
                        )
                except Exception:
                    # Keep worker healthy even when transcript scan scheduling fails.
                    pass
                next_transcript_scan = now + transcript_scan_interval_s
            if bool(getattr(settings, "transcript_scan_enabled", True)) and now >= next_transcript_gap_scan:
                try:
                    lookback_days = max(
                        1,
                        int(getattr(settings, "transcript_gap_scan_lookback_days", 420) or 420),
                    )
                    max_candidates = max(1, int(getattr(settings, "transcript_scan_max_candidates_per_run", 40) or 40))
                    with session_scope() as session:
                        enqueue_watchlist_transcript_scan_gap_check(
                            session,
                            lookback_days=lookback_days,
                            max_candidates=max_candidates,
                        )
                except Exception:
                    pass
                next_transcript_gap_scan = now + transcript_gap_scan_interval_s
            if now >= next_report_scan:
                try:
                    with session_scope() as session:
                        enqueue_report_freshness_scan(session)
                except Exception:
                    pass
                next_report_scan = now + research_report_scan_interval_s
            try:
                with session_scope() as session:
                    enqueue_due_research_play_jobs(session)
            except Exception:
                pass
            if now >= next_freshness_scan:
                try:
                    with session_scope() as session:
                        enqueue_due_freshness_reviews(session)
                except Exception:
                    pass
                next_freshness_scan = now + freshness_review_scan_interval_s
            next_sched = now + scheduler_interval_s

        processed = 0
        while processed < max_jobs and (not _stop_requested(stop_event)):
            job_id = None
            kind = None
            payload = {}
            try:
                with session_scope() as session:
                    job = claim_next_job(session, exclude_kinds=_CLOUD_WORKER_JOB_KINDS)
                    if not job:
                        break
                    job_id = job.id
                    kind = job.kind
                    payload = dict(job.payload or {}) if isinstance(job.payload, dict) else {}
                    payload["_job_id"] = int(job.id)
            except DatabaseBusyError as exc:
                logger.info("worker delayed job claim because database is busy: %s", exc)
                _sleep_or_stop(poll_s, stop_event)
                break

            ok = True
            err = None
            job_heartbeat_stop = threading.Event()
            active_job_started_at = utcnow().isoformat()

            def _publish_running_job_heartbeat() -> None:
                while not job_heartbeat_stop.is_set():
                    try:
                        write_service_heartbeat(
                            "worker",
                            status="running",
                            details={
                                "active_job": {
                                    "id": int(job_id) if job_id is not None else None,
                                    "kind": str(kind or "unknown"),
                                    "started_at": active_job_started_at,
                                },
                                "browser_max_runtime_s": int(
                                    getattr(settings, "browser_ingestion_max_runtime_s", 7200) or 7200
                                ),
                                "thread": threading.current_thread().name,
                            },
                        )
                    except Exception:
                        logger.warning("active worker job heartbeat write failed", exc_info=True)
                    job_heartbeat_stop.wait(15.0)

            job_heartbeat_thread = threading.Thread(
                target=_publish_running_job_heartbeat,
                name="thoth-worker-job-heartbeat",
                daemon=True,
            )
            job_heartbeat_thread.start()
            try:
                if kind == "ingest_source":
                    _handle_ingest_job(payload)
                elif kind == "ingest_x_thread":
                    _handle_x_thread_ingest_job(payload)
                elif kind == "postprocess_document":
                    _handle_postprocess_job(payload)
                elif kind == "agent_desk_digest":
                    handle_agent_desk_digest(payload)
                elif kind == "agent_ticker_timeline":
                    handle_agent_ticker_timeline(payload)
                elif kind == "agent_context_compression":
                    handle_agent_context_compression(payload)
                elif kind == "agent_thesis_autopilot":
                    _handle_agent_thesis_autopilot_job(payload)
                elif kind == "research_play_run":
                    _handle_research_play_job(payload)
                elif kind == "discord_watchlist_refresh":
                    _handle_discord_watchlist_refresh_job(payload)
                elif kind == "discord_watchlist_transcript_scan":
                    _handle_discord_watchlist_transcript_scan_job(payload)
                elif kind == "watchlist_transcript_scan":
                    handle_watchlist_transcript_scan_job(payload)
                elif kind == "research_report_build":
                    handle_research_report_build(payload)
                elif kind == "research_report_freshness_scan":
                    handle_research_report_freshness_scan(payload)
                elif kind == "knowledge_asset_draft":
                    handle_knowledge_asset_draft(payload)
                elif kind == "freshness_review":
                    handle_freshness_review(payload)
                elif kind == NEWSLETTER_DELIVERY_JOB:
                    handle_newsletter_delivery_job(payload)
                elif kind == "institutional_refresh":
                    handle_institutional_refresh_job(payload)
                elif kind == "institutional_closure_pass":
                    handle_institutional_closure_pass_job(payload)
                else:
                    ok = False
                    err = f"unknown_job_kind:{kind}"
            except Exception as exc:
                ok = False
                err = f"{type(exc).__name__}: {exc}"
            finally:
                job_heartbeat_stop.set()
                job_heartbeat_thread.join(timeout=2.0)

            if job_id is not None:
                try:
                    with session_scope() as session:
                        job = session.query(Job).filter_by(id=job_id).first()
                        if job:
                            finish_job(session, job=job, ok=ok, error=err)
                except DatabaseBusyError as exc:
                    logger.info("worker could not finish job %s because database is busy: %s", job_id, exc)

            last_job = {
                "id": int(job_id) if job_id is not None else None,
                "kind": str(kind or "unknown"),
                "ok": ok,
                "finished_at": utcnow().isoformat(),
                "error": str(err or "")[:500] or None,
            }
            last_heartbeat = 0.0

            processed += 1
            if _worker_memory_recycle_requested(context=f"after_job:{kind or 'unknown'}"):
                return

        if processed == 0 and (not _stop_requested(stop_event)):
            idle_loops += 1
            idle_cap_s = max(
                poll_s,
                float(getattr(settings, "worker_idle_backoff_cap_s", 10.0) or 10.0),
            )
            idle_delay = min(idle_cap_s, poll_s * (2 ** max(0, idle_loops - 1)))
            SessionLocal = session_factory()
            with SessionLocal() as session:
                next_job_delay = next_pending_job_delay_s(session, exclude_kinds=_CLOUD_WORKER_JOB_KINDS)
            next_maintenance_delay = _min_positive_delay(
                next_sched - time.time() if next_sched else None,
                next_watchlist_refresh - time.time() if next_watchlist_refresh else None,
                next_transcript_scan - time.time() if next_transcript_scan else None,
                next_transcript_gap_scan - time.time() if next_transcript_gap_scan else None,
                next_report_scan - time.time() if next_report_scan else None,
                next_freshness_scan - time.time() if next_freshness_scan else None,
            )
            sleep_s = _min_positive_delay(idle_delay, next_job_delay, next_maintenance_delay)
            job_listener.wait(sleep_s if sleep_s is not None else poll_s, stop_event)
        else:
            idle_loops = 0
