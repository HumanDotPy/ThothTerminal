from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, text
from sqlalchemy.orm import Session

from thoth_terminal.config import settings
from thoth_terminal.browser_ingestion_policy import browser_circuit_retry_at
from thoth_terminal.db import utcnow
from thoth_terminal.models import Job, ResearchPlay, ResearchPlayRun, Source, SourceState


JOB_STATUS_PENDING = "pending"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_SUCCESS = "success"
JOB_STATUS_ERROR = "error"

LOCAL_AGENT_OWNED_JOB_KINDS = {
    "media_transcription",
}


def _now() -> datetime:
    return utcnow()


def _notify_job_available(session: Session, kind: str) -> None:
    """Wake PostgreSQL listeners on commit; failure never affects job durability."""
    try:
        if str(session.get_bind().dialect.name or "").lower() == "postgresql":
            session.execute(text("SELECT pg_notify('thoth_jobs', :payload)"), {"payload": str(kind)[:200]})
    except Exception:
        # The existing polling worker remains the authoritative delivery rail.
        pass


def _retry_delay_s(attempts: int) -> int:
    # 10s, 20s, 40s, ... capped at 10 minutes
    return min(600, 10 * (2 ** max(0, attempts - 1)))


_LATENCY_SENSITIVE_INGEST_CONNECTORS = {
    "gmail_substack",
    "note_creator_browser",
    "rss",
    "sec_edgar_rss",
    "substack_inbox_browser",
    "x_list",
}

_BULK_INGEST_CONNECTORS = {
    "asx_announcements",
    "bursa_announcements",
    "cninfo_announcements",
    "edinet_disclosures",
    "edinetdb_financials",
    "eia_860m",
    "ercot_large_load",
    "ercot_llwg",
    "eu_ted_search",
    "hkexnews_title_search",
    "kazakhstan_announcements",
    "krx_kind_disclosure",
    "opendart_disclosures",
    "opendart_financials",
    "sedarplus_search",
    "sec_companyfacts",
    "sec_edgar",
    "sgx_announcements",
    "taiwan_mops_financial_reports",
    "taiwan_mops_investor_conferences",
    "taiwan_mops_monthly_revenue",
    "tdnet_company_announcements",
    "trade_un_comtrade",
    "trade_usitc_dataweb",
    "tpex_infohub_company",
    "twse_openapi",
    "usitc_dataweb",
}


def _clamp_job_priority(value: object, *, default: int = 5) -> int:
    try:
        priority = int(value)
    except (TypeError, ValueError):
        priority = int(default)
    return max(0, min(priority, 9))


def ingest_job_priority_for_source(source: Source) -> int:
    """Execution priority for source ingest jobs.

    Source.priority is an investment/research importance signal, but the worker
    queue also needs to avoid letting slow bulk feeds starve lightweight fresh
    intake. Lower values run first.
    """
    cfg = dict(source.config or {}) if isinstance(source.config, dict) else {}
    explicit = cfg.get("ingest_job_priority") or cfg.get("job_priority")
    if explicit is not None:
        return _clamp_job_priority(explicit, default=source.priority or 5)

    connector = str(cfg.get("connector") or source.category or "").strip().lower()
    base = _clamp_job_priority(source.priority, default=5)
    try:
        max_items = int(cfg.get("max_items_per_run") or 0)
    except (TypeError, ValueError):
        max_items = 0
    name = str(source.name or "").strip().lower()
    coverage_scope = str(cfg.get("coverage_scope") or "").strip().lower()
    if connector in _LATENCY_SENSITIVE_INGEST_CONNECTORS:
        if connector == "sec_edgar_rss" and (name == "sec_edgar_current_atom" or max_items >= 100):
            return max(base, 7)
        return min(base, 2)
    if (
        connector in _BULK_INGEST_CONNECTORS
        or connector.startswith("trade_")
        or coverage_scope == "active_universe"
        or "_active_universe_" in name
        or max_items > 100
    ):
        return max(base, 7)
    return base


def enqueue_job(
    session: Session,
    *,
    kind: str,
    payload: dict[str, Any],
    dedupe_key: str | None = None,
    priority: int = 5,
    run_after: datetime | None = None,
    max_attempts: int | None = None,
) -> Job:
    if dedupe_key:
        existing = (
            session.query(Job)
            .filter(Job.dedupe_key == dedupe_key)
            .filter(Job.status.in_([JOB_STATUS_PENDING, JOB_STATUS_RUNNING]))
            .first()
        )
        if existing:
            changed = False
            requested_priority = int(priority)
            if existing.priority > requested_priority:
                existing.priority = requested_priority
                changed = True
            if run_after is not None and (existing.run_after is None or existing.run_after > run_after):
                existing.run_after = run_after
                changed = True
            requested_max_attempts = int(max_attempts if max_attempts is not None else getattr(settings, "worker_job_max_attempts", 5))
            if int(existing.max_attempts or 0) < requested_max_attempts:
                existing.max_attempts = requested_max_attempts
                changed = True
            if changed:
                existing.updated_at = _now()
                session.flush()
                _notify_job_available(session, kind)
            return existing

    now = _now()
    job = Job(
        kind=kind,
        status=JOB_STATUS_PENDING,
        priority=int(priority),
        payload=payload,
        dedupe_key=dedupe_key,
        attempts=0,
        max_attempts=int(max_attempts if max_attempts is not None else getattr(settings, "worker_job_max_attempts", 5)),
        run_after=run_after,
        locked_at=None,
        started_at=None,
        finished_at=None,
        last_error=None,
        created_at=now,
        updated_at=now,
    )
    session.add(job)
    session.flush()
    _notify_job_available(session, kind)
    return job


def enqueue_x_thread_ingest_job(
    session: Session, *, top_url: str, bottom_url: str | None = None, requested_by_user_id: int
) -> Job:
    """Queue a bounded one-off X thread; no recurring source definition is added."""
    from thoth_terminal.discord.link_intake import canonical_x_status_url

    top = canonical_x_status_url(top_url)
    bottom = canonical_x_status_url(bottom_url) if bottom_url else None
    if not top or (bottom_url and not bottom):
        raise ValueError("Provide valid X status URLs for the thread bounds")
    top_handle = top.split("/", 4)[3].lower()
    if bottom and bottom.split("/", 4)[3].lower() != top_handle:
        raise ValueError("Thread bounds must belong to the same X author")
    root_id = top.split("/status/", 1)[1]
    return enqueue_job(
        session,
        kind="ingest_x_thread",
        payload={"top_url": top, "bottom_url": bottom, "requested_by_user_id": int(requested_by_user_id)},
        dedupe_key=f"ingest_x_thread:{root_id}:{bottom.split('/status/', 1)[1] if bottom else 'open'}",
        priority=0,
    )


def _supersede_pending_jobs(
    session: Session,
    *,
    kind: str,
    keep_job_id: int,
    payload_key: str,
    payload_value: str,
    reason: str,
) -> int:
    now = _now()
    rows = (
        session.query(Job)
        .filter(Job.kind == kind)
        .filter(Job.status == JOB_STATUS_PENDING)
        .filter(Job.id != int(keep_job_id))
        .all()
    )
    updated = 0
    for job in rows:
        payload = dict(job.payload or {}) if isinstance(job.payload, dict) else {}
        if str(payload.get(payload_key) or "").strip().lower() != payload_value:
            continue
        payload["superseded_by_job_id"] = int(keep_job_id)
        payload["superseded_reason"] = reason
        job.payload = payload
        job.status = JOB_STATUS_SUCCESS
        job.finished_at = now
        job.updated_at = now
        job.last_error = None
        updated += 1
    if updated:
        session.flush()
    return updated


def enqueue_ingest_job(session: Session, *, source_id: int, priority: int | None = None) -> Job:
    return enqueue_job(
        session,
        kind="ingest_source",
        payload={"source_id": int(source_id)},
        dedupe_key=f"ingest_source:{int(source_id)}",
        priority=int(priority) if priority is not None else 1,
    )


def rebalance_pending_ingest_job_priorities(session: Session) -> int:
    jobs = (
        session.query(Job)
        .filter(Job.kind == "ingest_source")
        .filter(Job.status == JOB_STATUS_PENDING)
        .all()
    )
    source_ids: set[int] = set()
    for job in jobs:
        payload = dict(job.payload or {}) if isinstance(job.payload, dict) else {}
        try:
            source_ids.add(int(payload.get("source_id")))
        except (TypeError, ValueError):
            continue
    sources = {
        int(src.id): src
        for src in session.query(Source).filter(Source.id.in_(source_ids)).all()
    } if source_ids else {}
    updated = 0
    for job in jobs:
        payload = dict(job.payload or {}) if isinstance(job.payload, dict) else {}
        try:
            source_id = int(payload.get("source_id"))
        except (TypeError, ValueError):
            continue
        source = sources.get(source_id)
        if source is None:
            continue
        desired = ingest_job_priority_for_source(source)
        if int(job.priority or 0) == desired:
            continue
        job.priority = desired
        job.updated_at = _now()
        updated += 1
    if updated:
        session.flush()
    return updated


def enqueue_postprocess_job(session: Session, *, doc_id: int, source_id: int | None = None) -> Job:
    payload = {"doc_id": int(doc_id)}
    if source_id is not None:
        payload["source_id"] = int(source_id)
    return enqueue_job(
        session,
        kind="postprocess_document",
        payload=payload,
        dedupe_key=f"postprocess_document:{int(doc_id)}",
        priority=5,
    )


def enqueue_agent_digest_job(
    session: Session,
    *,
    guild_id: int,
    mode: str = "manual",
    lookback_hours: int | None = None,
    post_channel_id: int | None = None,
    post_thread_id: int | None = None,
    date_key: str | None = None,
    title_override: str | None = None,
    engine: str | None = None,
    dedupe_key: str | None = None,
    run_after: datetime | None = None,
    max_attempts: int = 3,
) -> Job:
    payload: dict[str, Any] = {
        "guild_id": int(guild_id),
        "mode": (mode or "manual").strip().lower(),
    }
    if lookback_hours is not None:
        payload["lookback_hours"] = int(lookback_hours)
    if post_channel_id is not None:
        payload["post_channel_id"] = int(post_channel_id)
    if post_thread_id is not None:
        payload["post_thread_id"] = int(post_thread_id)
    if date_key:
        payload["date_key"] = str(date_key)
    if title_override and str(title_override).strip():
        payload["title_override"] = str(title_override).strip()
    if engine and str(engine).strip():
        payload["engine"] = str(engine).strip()
    return enqueue_job(
        session,
        kind="agent_desk_digest",
        payload=payload,
        dedupe_key=(dedupe_key.strip() if isinstance(dedupe_key, str) and dedupe_key.strip() else None),
        priority=5,
        run_after=run_after,
        max_attempts=int(max_attempts),
    )


def enqueue_agent_ticker_job(
    session: Session,
    *,
    guild_id: int,
    ticker: str,
    days: int = 30,
    post_to_outbox: bool = False,
    post_channel_id: int | None = None,
    post_thread_id: int | None = None,
    dedupe_key: str | None = None,
    run_after: datetime | None = None,
    max_attempts: int = 3,
) -> Job:
    payload: dict[str, Any] = {
        "guild_id": int(guild_id),
        "ticker": str(ticker),
        "days": int(days),
        "post_to_outbox": bool(post_to_outbox),
    }
    if post_channel_id is not None:
        payload["post_channel_id"] = int(post_channel_id)
    if post_thread_id is not None:
        payload["post_thread_id"] = int(post_thread_id)
    return enqueue_job(
        session,
        kind="agent_ticker_timeline",
        payload=payload,
        dedupe_key=(dedupe_key.strip() if isinstance(dedupe_key, str) and dedupe_key.strip() else None),
        priority=6,
        run_after=run_after,
        max_attempts=int(max_attempts),
    )


def enqueue_agent_context_compression_job(
    session: Session,
    *,
    guild_id: int,
    mode: str = "custom",
    lookback_hours: int | None = None,
    focus_query: str | None = None,
    tickers: list[str] | None = None,
    include_sections: list[str] | None = None,
    post_to_outbox: bool = False,
    post_channel_id: int | None = None,
    post_thread_id: int | None = None,
    persist_note: bool = True,
    title_override: str | None = None,
    client_request_id: str | None = None,
    dedupe_key: str | None = None,
    run_after: datetime | None = None,
    max_attempts: int = 3,
) -> Job:
    mode_v = (mode or "custom").strip().lower()
    if mode_v not in {"daily_master", "topic", "decision_brief", "custom"}:
        raise ValueError("mode must be daily_master|topic|decision_brief|custom")
    payload: dict[str, Any] = {
        "guild_id": int(guild_id),
        "mode": mode_v,
        "post_to_outbox": bool(post_to_outbox),
        "persist_note": bool(persist_note),
    }
    if lookback_hours is not None:
        payload["lookback_hours"] = int(lookback_hours)
    if focus_query and str(focus_query).strip():
        payload["focus_query"] = str(focus_query).strip()
    if isinstance(tickers, list):
        payload["tickers"] = [str(t).strip().upper() for t in tickers if str(t).strip()]
    if isinstance(include_sections, list):
        payload["include_sections"] = [str(s).strip() for s in include_sections if str(s).strip()]
    if post_channel_id is not None:
        payload["post_channel_id"] = int(post_channel_id)
    if post_thread_id is not None:
        payload["post_thread_id"] = int(post_thread_id)
    if title_override and str(title_override).strip():
        payload["title_override"] = str(title_override).strip()
    if client_request_id and str(client_request_id).strip():
        payload["client_request_id"] = str(client_request_id).strip()
    return enqueue_job(
        session,
        kind="agent_context_compression",
        payload=payload,
        dedupe_key=(dedupe_key.strip() if isinstance(dedupe_key, str) and dedupe_key.strip() else None),
        priority=2,
        run_after=run_after,
        max_attempts=int(max_attempts),
    )


def enqueue_agent_thesis_autopilot_job(
    session: Session,
    *,
    guild_id: int | None = None,
    thesis_id: int | None = None,
    lookback_hours: int = 72,
    dedupe_key: str | None = None,
    run_after: datetime | None = None,
    max_attempts: int = 3,
) -> Job:
    payload: dict[str, Any] = {
        "lookback_hours": max(1, int(lookback_hours or 72)),
    }
    if guild_id is not None:
        payload["guild_id"] = int(guild_id)
    if thesis_id is not None:
        payload["thesis_id"] = int(thesis_id)
    return enqueue_job(
        session,
        kind="agent_thesis_autopilot",
        payload=payload,
        dedupe_key=(dedupe_key.strip() if isinstance(dedupe_key, str) and dedupe_key.strip() else None),
        priority=2,
        run_after=run_after,
        max_attempts=int(max_attempts),
    )


def enqueue_cloud_research_automation_job(
    session: Session,
    *,
    mode: str,
    lookback_hours: int | None = None,
    provider: str | None = None,
    model: str | None = None,
    objective: str | None = None,
    promotion_document_id: int | None = None,
    promotion_compression_id: int | None = None,
    dedupe_key: str | None = None,
    run_after: datetime | None = None,
    max_attempts: int = 2,
    extra_payload: dict[str, Any] | None = None,
    delivery_mode: str = "wake",
) -> Job:
    mode_v = str(mode or "nightly").strip().lower().replace("-", "_")
    if mode_v not in {"nightly", "heartbeat", "inquisitive"}:
        raise ValueError("mode must be nightly|heartbeat|inquisitive")
    delivery_mode_v = str(delivery_mode or "").strip().lower().replace("-", "_")
    if delivery_mode_v != "wake":
        raise ValueError("cloud research jobs are execution wakes; persist/inject delivery must use the agent inbox rail")
    payload: dict[str, Any] = {"mode": mode_v, "agent_delivery_mode": "wake"}
    if extra_payload:
        payload.update(dict(extra_payload))
        payload["mode"] = mode_v
        payload["agent_delivery_mode"] = "wake"
    if lookback_hours is not None:
        payload["lookback_hours"] = max(1, int(lookback_hours))
    if provider and str(provider).strip():
        payload["provider"] = str(provider).strip()
    if model and str(model).strip():
        payload["model"] = str(model).strip()
    if objective and str(objective).strip():
        payload["objective"] = str(objective).strip()
    if promotion_document_id is not None:
        payload["promotion_document_id"] = int(promotion_document_id)
    if promotion_compression_id is not None:
        payload["promotion_compression_id"] = int(promotion_compression_id)
    return enqueue_job(
        session,
        kind="cloud_research_automation",
        payload=payload,
        dedupe_key=(dedupe_key.strip() if isinstance(dedupe_key, str) and dedupe_key.strip() else None),
        priority=3 if mode_v == "heartbeat" else 4,
        run_after=run_after,
        max_attempts=int(max_attempts),
    )


def enqueue_discord_watchlist_refresh_job(
    session: Session,
    *,
    guild_id: int,
    mode: str,
    run_after: datetime | None = None,
    dedupe_key: str | None = None,
    max_attempts: int = 3,
) -> Job:
    mode_v = (mode or "").strip().lower()
    if mode_v not in {"hourly", "immediate", "manual"}:
        raise ValueError("mode must be hourly|immediate|manual")
    payload = {
        "guild_id": int(guild_id),
        "mode": mode_v,
    }
    return enqueue_job(
        session,
        kind="discord_watchlist_refresh",
        payload=payload,
        dedupe_key=(dedupe_key.strip() if isinstance(dedupe_key, str) and dedupe_key.strip() else None),
        priority=4,
        run_after=run_after,
        max_attempts=int(max_attempts),
    )


def enqueue_discord_watchlist_refresh_hourly(
    session: Session,
    *,
    guild_id: int,
    now: datetime | None = None,
) -> Job:
    now_utc = now or _now()
    bucket = now_utc.strftime("%Y%m%d%H")
    return enqueue_discord_watchlist_refresh_job(
        session,
        guild_id=int(guild_id),
        mode="hourly",
        dedupe_key=f"discord_watchlist_refresh:hourly:{int(guild_id)}:{bucket}",
        max_attempts=3,
    )


def enqueue_discord_watchlist_refresh_debounced(
    session: Session,
    *,
    guild_id: int,
    delay_seconds: int | None = None,
    now: datetime | None = None,
) -> Job:
    now_utc = now or _now()
    delay_s = int(
        delay_seconds
        if delay_seconds is not None
        else getattr(settings, "discord_watchlist_immediate_refresh_delay_seconds", 8) or 8
    )
    delay_s = max(0, delay_s)
    return enqueue_discord_watchlist_refresh_job(
        session,
        guild_id=int(guild_id),
        mode="immediate",
        run_after=(now_utc + timedelta(seconds=delay_s)),
        dedupe_key=f"discord_watchlist_refresh:immediate:{int(guild_id)}",
        max_attempts=3,
    )


def enqueue_discord_watchlist_transcript_scan_job(
    session: Session,
    *,
    guild_id: int,
    mode: str,
    run_after: datetime | None = None,
    dedupe_key: str | None = None,
    lookback_days: int | None = None,
    max_attempts: int = 3,
) -> Job:
    mode_v = (mode or "").strip().lower()
    if mode_v not in {"hourly", "immediate", "manual"}:
        raise ValueError("mode must be hourly|immediate|manual")
    payload: dict[str, Any] = {
        "guild_id": int(guild_id),
        "mode": mode_v,
    }
    if lookback_days is not None:
        payload["lookback_days"] = int(lookback_days)
    return enqueue_job(
        session,
        kind="discord_watchlist_transcript_scan",
        payload=payload,
        dedupe_key=(dedupe_key.strip() if isinstance(dedupe_key, str) and dedupe_key.strip() else None),
        priority=4,
        run_after=run_after,
        max_attempts=int(max_attempts),
    )


def enqueue_discord_watchlist_transcript_scan_hourly(
    session: Session,
    *,
    guild_id: int,
    lookback_days: int | None = None,
    now: datetime | None = None,
) -> Job:
    now_utc = now or _now()
    bucket = now_utc.strftime("%Y%m%d%H")
    return enqueue_discord_watchlist_transcript_scan_job(
        session,
        guild_id=int(guild_id),
        mode="hourly",
        lookback_days=lookback_days,
        dedupe_key=f"discord_transcript_scan:hourly:{int(guild_id)}:{bucket}",
        max_attempts=3,
    )


def enqueue_discord_watchlist_transcript_scan_debounced(
    session: Session,
    *,
    guild_id: int,
    delay_seconds: int | None = None,
    lookback_days: int | None = None,
    now: datetime | None = None,
) -> Job:
    now_utc = now or _now()
    delay_s = int(
        delay_seconds
        if delay_seconds is not None
        else getattr(settings, "discord_watchlist_immediate_refresh_delay_seconds", 8) or 8
    )
    delay_s = max(0, delay_s)
    return enqueue_discord_watchlist_transcript_scan_job(
        session,
        guild_id=int(guild_id),
        mode="immediate",
        lookback_days=lookback_days,
        run_after=(now_utc + timedelta(seconds=delay_s)),
        dedupe_key=f"discord_transcript_scan:immediate:{int(guild_id)}",
        max_attempts=3,
    )


def enqueue_watchlist_transcript_scan_job(
    session: Session,
    *,
    mode: str = "regular",
    run_after: datetime | None = None,
    dedupe_key: str | None = None,
    lookback_days: int | None = None,
    max_candidates: int | None = None,
    force: bool = False,
    max_attempts: int = 3,
) -> Job:
    mode_v = (mode or "regular").strip().lower()
    if mode_v not in {"regular", "gap_check", "weekly", "manual"}:
        raise ValueError("mode must be regular|gap_check|weekly|manual")
    payload: dict[str, Any] = {
        "mode": mode_v,
        "force": bool(force),
    }
    if lookback_days is not None:
        payload["lookback_days"] = int(lookback_days)
    if max_candidates is not None:
        payload["max_candidates"] = int(max_candidates)
    job = enqueue_job(
        session,
        kind="watchlist_transcript_scan",
        payload=payload,
        dedupe_key=(dedupe_key.strip() if isinstance(dedupe_key, str) and dedupe_key.strip() else None),
        priority=0,
        run_after=run_after,
        max_attempts=int(max_attempts),
    )
    if job.status == JOB_STATUS_PENDING:
        _supersede_pending_jobs(
            session,
            kind="watchlist_transcript_scan",
            keep_job_id=int(job.id),
            payload_key="mode",
            payload_value=mode_v,
            reason="newer transcript scan supersedes stale pending bucket",
        )
    return job


def enqueue_watchlist_transcript_scan_regular(
    session: Session,
    *,
    lookback_days: int | None = None,
    max_candidates: int | None = None,
    now: datetime | None = None,
) -> Job:
    now_utc = now or _now()
    bucket_minutes = max(60, int(getattr(settings, "transcript_scan_interval_minutes", 360) or 360))
    bucket = int(now_utc.timestamp()) // (bucket_minutes * 60)
    return enqueue_watchlist_transcript_scan_job(
        session,
        mode="regular",
        lookback_days=lookback_days,
        max_candidates=max_candidates,
        dedupe_key=f"watchlist_transcript_scan:regular:{bucket}",
        max_attempts=3,
    )


def enqueue_watchlist_transcript_scan_gap_check(
    session: Session,
    *,
    lookback_days: int | None = None,
    max_candidates: int | None = None,
    now: datetime | None = None,
) -> Job:
    now_utc = now or _now()
    bucket = now_utc.strftime("%Y%W")
    return enqueue_watchlist_transcript_scan_job(
        session,
        mode="gap_check",
        lookback_days=lookback_days,
        max_candidates=max_candidates,
        dedupe_key=f"watchlist_transcript_scan:gap:{bucket}",
        max_attempts=3,
    )


def enqueue_institutional_refresh_job(
    session: Session,
    *,
    source_id: int | None = None,
    document_id: int | None = None,
    subject_id: int | None = None,
    force: bool = False,
    run_after: datetime | None = None,
    max_attempts: int = 3,
) -> Job:
    payload: dict[str, Any] = {"force": bool(force)}
    if source_id is not None:
        payload["source_id"] = int(source_id)
    if document_id is not None:
        payload["document_id"] = int(document_id)
    if subject_id is not None:
        payload["subject_id"] = int(subject_id)
    dedupe_target = (
        f"source:{int(source_id)}"
        if source_id is not None
        else f"document:{int(document_id)}"
        if document_id is not None
        else f"subject:{int(subject_id)}"
        if subject_id is not None
        else "all"
    )
    return enqueue_job(
        session,
        kind="institutional_refresh",
        payload=payload,
        dedupe_key=None if force else f"institutional_refresh:{dedupe_target}",
        priority=3,
        run_after=run_after,
        max_attempts=int(max_attempts),
    )


def enqueue_institutional_closure_pass_job(
    session: Session,
    *,
    program_id: int | None = None,
    subject_id: int | None = None,
    site_id: int | None = None,
    force: bool = False,
    run_after: datetime | None = None,
    max_attempts: int = 3,
) -> Job:
    payload: dict[str, Any] = {"force": bool(force)}
    if program_id is not None:
        payload["program_id"] = int(program_id)
    if subject_id is not None:
        payload["subject_id"] = int(subject_id)
    if site_id is not None:
        payload["site_id"] = int(site_id)
    dedupe_target = (
        f"program:{int(program_id)}"
        if program_id is not None
        else f"subject:{int(subject_id)}"
        if subject_id is not None
        else f"site:{int(site_id)}"
        if site_id is not None
        else "all"
    )
    return enqueue_job(
        session,
        kind="institutional_closure_pass",
        payload=payload,
        dedupe_key=None if force else f"institutional_closure_pass:{dedupe_target}",
        priority=4,
        run_after=run_after,
        max_attempts=int(max_attempts),
    )


def enqueue_research_play_job(
    session: Session,
    *,
    play_id: int,
    trigger_kind: str = "manual",
    dedupe_key: str | None = None,
    run_after: datetime | None = None,
    max_attempts: int = 3,
) -> Job:
    payload: dict[str, Any] = {
        "play_id": int(play_id),
        "trigger_kind": str(trigger_kind or "manual").strip().lower() or "manual",
    }
    return enqueue_job(
        session,
        kind="research_play_run",
        payload=payload,
        dedupe_key=(dedupe_key.strip() if isinstance(dedupe_key, str) and dedupe_key.strip() else None),
        priority=3,
        run_after=run_after,
        max_attempts=int(max_attempts),
    )


def _research_play_interval_minutes(play: ResearchPlay) -> int | None:
    schedule = play.schedule_json if isinstance(play.schedule_json, dict) else {}
    if schedule.get("enabled") is False:
        return None
    if str(schedule.get("interval_minutes") or "").isdigit():
        minutes = int(schedule.get("interval_minutes") or 0)
    elif str(schedule.get("interval_hours") or "").isdigit():
        minutes = int(schedule.get("interval_hours") or 0) * 60
    elif str(schedule.get("cadence") or "").strip().lower() == "daily":
        minutes = 24 * 60
    else:
        minutes = 0
    if minutes <= 0:
        return None
    return max(5, minutes)


def enqueue_due_research_play_jobs(session: Session, *, now: datetime | None = None) -> list[Job]:
    now_utc = now or _now()
    plays = (
        session.query(ResearchPlay)
        .filter(ResearchPlay.status == "active")
        .order_by(ResearchPlay.updated_at.desc(), ResearchPlay.id.desc())
        .all()
    )
    enqueued: list[Job] = []
    for play in plays:
        interval_minutes = _research_play_interval_minutes(play)
        if interval_minutes is None:
            continue
        last_run = (
            session.query(ResearchPlayRun)
            .filter(ResearchPlayRun.play_id == int(play.id))
            .order_by(ResearchPlayRun.started_at.desc(), ResearchPlayRun.id.desc())
            .first()
        )
        if last_run is not None and last_run.started_at is not None:
            elapsed_s = (now_utc - last_run.started_at).total_seconds()
            if elapsed_s < (interval_minutes * 60):
                continue
        bucket = int(now_utc.timestamp()) // (interval_minutes * 60)
        job = enqueue_research_play_job(
            session,
            play_id=int(play.id),
            trigger_kind="scheduled",
            dedupe_key=f"research_play:{int(play.id)}:{bucket}",
            max_attempts=3,
        )
        enqueued.append(job)
    return enqueued


def claim_next_job(
    session: Session,
    *,
    kind: str | None = None,
    exclude_kinds: set[str] | None = None,
) -> Job | None:
    now = _now()
    q = session.query(Job).filter(Job.status == JOB_STATUS_PENDING)
    q = q.filter(or_(Job.run_after.is_(None), Job.run_after <= now))
    q = q.filter(or_(Job.max_attempts.is_(None), Job.attempts < Job.max_attempts))
    if kind:
        q = q.filter(Job.kind == kind)
    else:
        q = q.filter(Job.kind.notin_(LOCAL_AGENT_OWNED_JOB_KINDS))
        if exclude_kinds:
            q = q.filter(Job.kind.notin_(sorted(exclude_kinds)))
    job = None
    for _ in range(64):
        candidate = q.order_by(Job.priority.asc(), Job.created_at.asc()).first()
        if candidate is None:
            return None
        if candidate.kind == "ingest_source":
            payload = dict(candidate.payload or {}) if isinstance(candidate.payload, dict) else {}
            try:
                source_id = int(payload.get("source_id"))
            except (TypeError, ValueError):
                source_id = 0
            source = session.query(Source).filter_by(id=source_id).first() if source_id > 0 else None
            state = session.query(SourceState).filter_by(source_id=source_id).first() if source_id > 0 else None
            retry_at = browser_circuit_retry_at(
                dict(source.config or {}) if source is not None else None,
                failures=int(state.consecutive_failures or 0) if state is not None else 0,
                last_run_at=state.last_run_at if state is not None else None,
                backoff_until=state.backoff_until if state is not None else None,
                now=now,
            )
            if retry_at is not None:
                candidate.run_after = retry_at
                candidate.updated_at = now
                if state is not None:
                    state.backoff_until = retry_at
                    state.updated_at = now
                session.flush()
                continue
        job = candidate
        break
    if job is None:
        return None

    job.status = JOB_STATUS_RUNNING
    job.started_at = now
    job.locked_at = now
    job.attempts = int(job.attempts or 0) + 1
    job.updated_at = now
    session.flush()
    return job


def next_pending_job_delay_s(
    session: Session,
    *,
    kind: str | None = None,
    exclude_kinds: set[str] | None = None,
) -> float | None:
    now = _now()
    q = session.query(Job.run_after).filter(Job.status == JOB_STATUS_PENDING)
    q = q.filter(or_(Job.max_attempts.is_(None), Job.attempts < Job.max_attempts))
    if kind:
        q = q.filter(Job.kind == kind)
    else:
        q = q.filter(Job.kind.notin_(LOCAL_AGENT_OWNED_JOB_KINDS))
        if exclude_kinds:
            q = q.filter(Job.kind.notin_(sorted(exclude_kinds)))
    row = q.order_by(Job.run_after.asc().nullsfirst(), Job.priority.asc(), Job.created_at.asc()).first()
    if not row:
        return None
    run_after = row[0]
    if run_after is None:
        return 0.0
    return max(0.0, (run_after - now).total_seconds())


def finish_job(session: Session, *, job: Job, ok: bool, error: str | None = None) -> None:
    now = _now()
    if ok:
        job.status = JOB_STATUS_SUCCESS
        job.finished_at = now
        job.last_error = None
    else:
        job.last_error = error or job.last_error
        if job.attempts < int(job.max_attempts or 0):
            job.status = JOB_STATUS_PENDING
            job.run_after = now + timedelta(seconds=_retry_delay_s(job.attempts))
            job.started_at = None
            job.locked_at = None
        else:
            job.status = JOB_STATUS_ERROR
            job.finished_at = now
    job.updated_at = now
    session.flush()


def reap_stale_jobs(
    session: Session,
    *,
    kind: str | None = None,
    exclude_kinds: set[str] | None = None,
    max_age_s: int | None = None,
) -> int:
    explicit_max_age = max_age_s is not None
    default_max_age_s = int(getattr(settings, "worker_job_stale_seconds", 900) or 900)
    ingest_max_age_s = int(getattr(settings, "worker_ingest_job_stale_seconds", default_max_age_s) or default_max_age_s)
    max_age_s = int(
        max_age_s
        if max_age_s is not None
        else ingest_max_age_s
        if kind == "ingest_source"
        else default_max_age_s
    )
    if max_age_s <= 0:
        return 0
    now = _now()
    cutoff = now - timedelta(seconds=max_age_s)
    q = session.query(Job).filter(Job.status == JOB_STATUS_RUNNING)
    if kind:
        q = q.filter(Job.kind == kind)
    elif exclude_kinds:
        q = q.filter(Job.kind.notin_(sorted(exclude_kinds)))
    if kind:
        q = q.filter(or_(Job.locked_at.is_(None), Job.locked_at <= cutoff))
    elif (not explicit_max_age) and ingest_max_age_s > default_max_age_s:
        default_cutoff = now - timedelta(seconds=default_max_age_s)
        ingest_cutoff = now - timedelta(seconds=ingest_max_age_s)
        q = q.filter(
            or_(
                and_(Job.kind == "ingest_source", or_(Job.locked_at.is_(None), Job.locked_at <= ingest_cutoff)),
                and_(Job.kind != "ingest_source", or_(Job.locked_at.is_(None), Job.locked_at <= default_cutoff)),
            )
        )
    else:
        q = q.filter(or_(Job.locked_at.is_(None), Job.locked_at <= cutoff))
    jobs = q.all()
    for job in jobs:
        finish_job(session, job=job, ok=False, error="stale job reaped")
    return len(jobs)


def recover_interrupted_running_jobs(
    session: Session,
    *,
    kind: str | None = None,
    exclude_kinds: set[str] | None = None,
    reason: str = "worker startup recovered interrupted running job",
    resume: bool = False,
) -> int:
    """Release jobs left running by a clean worker restart or crash.

    Recover only the caller's single-owner lane. Ordinary jobs re-enter the
    retry/error path; resumable work reclaims its existing attempt because a
    process restart does not prove that the external execution failed.
    """
    q = session.query(Job).filter(Job.status == JOB_STATUS_RUNNING)
    if kind:
        q = q.filter(Job.kind == kind)
    elif exclude_kinds:
        q = q.filter(Job.kind.notin_(sorted(exclude_kinds)))
    jobs = q.order_by(Job.locked_at.asc().nullsfirst(), Job.created_at.asc()).all()
    for job in jobs:
        if resume:
            job.status = JOB_STATUS_PENDING
            job.attempts = max(0, int(job.attempts or 0) - 1)
            job.last_error = reason
            job.started_at = job.finished_at = job.locked_at = None
            job.run_after = job.updated_at = _now()
        else:
            finish_job(session, job=job, ok=False, error=reason)
    session.flush()
    return len(jobs)
