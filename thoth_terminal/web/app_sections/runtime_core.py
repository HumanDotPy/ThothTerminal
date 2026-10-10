from __future__ import annotations

from thoth_terminal.generation_cache import GenerationTTLCache
from thoth_terminal.web.app_sections._shared import bind_app_context
from thoth_terminal.source_health_service import build_adaptive_source_silence_profiles


bind_app_context(globals())


def _request_has_app_auth(request: Request) -> bool:
    expected = str(getattr(settings, "app_auth_token", "") or "").strip()
    if not expected:
        return False
    auth = str(request.headers.get("authorization") or "").strip()
    token = ""
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    if not token:
        token = str(request.headers.get("x-thoth-auth") or "").strip()
    return bool(token and secrets.compare_digest(token, expected))


def _mutation_cache_prefixes(path: str) -> tuple[str, ...] | None:
    if path == "/documents/bulk" or path.startswith("/documents/"):
        return ("feed:", "docnav:")
    if path == "/notes/new" or path.startswith("/notes/") or path.startswith("/api/notes/"):
        return ("home:", "search:", "notes:")
    if path.startswith("/docs/"):
        return ("feed:", "docnav:", "docreview:")
    if path.startswith("/api/universes"):
        return ("universes",)
    if path == "/api/portfolio/targets":
        return ("home:",)
    if not path.startswith("/api/local-agent/"):
        return None
    if path.startswith(("/api/local-agent/runs", "/api/local-agent/claims")):
        return ()
    local_agent_write_prefixes = (
        "/api/local-agent/analyst-evidence",
        "/api/local-agent/catalysts",
        "/api/local-agent/compressions",
        "/api/local-agent/daily-summaries",
        "/api/local-agent/errors",
        "/api/local-agent/evidence-links",
        "/api/local-agent/investment-memos",
        "/api/local-agent/research-docs/write",
        "/api/local-agent/translation-overrides",
        "/api/local-agent/unresolved-entities",
    )
    if path.startswith(local_agent_write_prefixes):
        return ("feed:", "home:", "search:")
    return ()


def _request_skips_terminal_cache_invalidation(request: Request) -> bool:
    state = getattr(request, "state", None)
    return bool(getattr(state, "skip_terminal_cache_invalidation", False))


@app.middleware("http")
async def _security_boundary(request: Request, call_next):
    started = time.perf_counter()
    posture = build_security_posture(settings)
    path = request.url.path
    response = await call_next(request)
    route_ms = max(0.0, (time.perf_counter() - started) * 1000.0)
    response.headers["X-Thoth-Route-Time-Ms"] = f"{route_ms:.1f}"
    server_timing = f"app;dur={route_ms:.1f}"
    existing_server_timing = response.headers.get("Server-Timing")
    response.headers["Server-Timing"] = f"{existing_server_timing}, {server_timing}" if existing_server_timing else server_timing
    if not posture.public:
        runtime_identity = get_runtime_identity()
        response.headers["X-Thoth-Boot-Id"] = str(runtime_identity.get("boot_id") or "")
        response.headers["X-Thoth-Started-At"] = str(runtime_identity.get("started_at_utc") or "")
        response.headers["X-Thoth-Git-Sha"] = str(runtime_identity.get("git_sha") or "")
    if request.method.upper() == "GET" and response.status_code < 400:
        if path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "public, max-age=31536000, immutable")
        elif path.startswith("/omori-mood-runtime/"):
            response.headers.setdefault("Cache-Control", "public, max-age=3600")
        elif _is_navigation_prewarm(request):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Vary"] = "Purpose, Sec-Purpose, X-Thoth-Purpose"
    if (
        request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"}
        and response.status_code < 400
        and not path.startswith("/mcp")
        and not path.endswith("/seen")
        and not _request_skips_terminal_cache_invalidation(request)
    ):
        prefixes = _mutation_cache_prefixes(path)
        if prefixes is None:
            _invalidate_terminal_caches()
        elif prefixes:
            _invalidate_terminal_caches(*prefixes)
    return response

logger = logging.getLogger("thoth")

_HOME_INTELLIGENCE_CACHE: GenerationTTLCache[dict[str, Any]] = GenerationTTLCache(
    max_entries=4,
    default_ttl_s=15.0,
    max_weight=4 * 1024 * 1024,
    get_weight=json_cache_weight,
)
_SOURCE_HEALTH_CACHE: GenerationTTLCache[list[dict[str, object]]] = GenerationTTLCache(max_entries=8, default_ttl_s=20.0)
_UNIVERSE_LIST_CACHE: GenerationTTLCache[list[dict[str, Any]]] = GenerationTTLCache(
    max_entries=4,
    default_ttl_s=45.0,
    max_weight=4 * 1024 * 1024,
    get_weight=json_cache_weight,
)
_UNIVERSE_DETAIL_CACHE: GenerationTTLCache[dict[str, Any]] = GenerationTTLCache(
    max_entries=80,
    default_ttl_s=45.0,
    max_weight=12 * 1024 * 1024,
    get_weight=json_cache_weight,
)
_UNIVERSE_SEARCH_CACHE: GenerationTTLCache[dict[str, Any]] = GenerationTTLCache(
    max_entries=160,
    default_ttl_s=90.0,
    max_weight=16 * 1024 * 1024,
    get_weight=json_cache_weight,
)
_UNIVERSE_LABEL_CACHE: GenerationTTLCache[list[dict[str, Any]]] = GenerationTTLCache(max_entries=80, default_ttl_s=120.0)
_API_SEARCH_CACHE: GenerationTTLCache[dict[str, Any]] = GenerationTTLCache(
    max_entries=256,
    default_ttl_s=45.0,
    max_weight=24 * 1024 * 1024,
    get_weight=json_cache_weight,
)
_HOME_PORTFOLIO_CACHE: GenerationTTLCache[dict[str, Any]] = GenerationTTLCache(max_entries=8, default_ttl_s=75.0)
_TRIAGE_COUNTS_CACHE: GenerationTTLCache[dict[str, int]] = GenerationTTLCache(max_entries=8, default_ttl_s=10.0)
_DATASET_INDEX_CACHE: GenerationTTLCache[dict[str, Any]] = GenerationTTLCache(
    max_entries=32,
    default_ttl_s=15.0,
    max_weight=16 * 1024 * 1024,
    get_weight=json_cache_weight,
)
_DOC_NAV_CACHE: GenerationTTLCache[dict[str, int | None]] = GenerationTTLCache(max_entries=512, default_ttl_s=20.0)
_DOC_REVIEW_CACHE: GenerationTTLCache[dict[str, Any] | None] = GenerationTTLCache(max_entries=128, default_ttl_s=300.0)
_NOTE_FILTER_CACHE: GenerationTTLCache[dict[str, list[SimpleNamespace]]] = GenerationTTLCache(max_entries=8, default_ttl_s=60.0)
_SETTINGS_GOOGLE_STATUS_CACHE: GenerationTTLCache[dict[str, Any]] = GenerationTTLCache(
    max_entries=4,
    default_ttl_s=30.0,
    max_weight=256 * 1024,
    get_weight=json_cache_weight,
)
_OPENSHELL_STATUS_CACHE: GenerationTTLCache[dict[str, Any]] = GenerationTTLCache(
    max_entries=4,
    default_ttl_s=20.0,
    max_weight=256 * 1024,
    get_weight=json_cache_weight,
)
_CLOUD_MODEL_CATALOG_CACHE: GenerationTTLCache[dict[str, Any]] = GenerationTTLCache(
    max_entries=4,
    default_ttl_s=180.0,
    max_weight=4 * 1024 * 1024,
    get_weight=json_cache_weight,
)
_AGENT_SETTINGS_CACHE: GenerationTTLCache[dict[str, Any]] = GenerationTTLCache(
    max_entries=16,
    default_ttl_s=30.0,
    max_weight=512 * 1024,
    get_weight=json_cache_weight,
)
_HOME_INTELLIGENCE_CACHE_TTL_S = 12.0
_HOME_PORTFOLIO_BACKGROUND_TTL_S = 75.0
_DOC_READER_INITIAL_CHARS = 32_000


def _invalidate_terminal_caches(*prefixes: str) -> None:
    if not prefixes:
        for cache in (
            _HOME_INTELLIGENCE_CACHE,
            _SOURCE_HEALTH_CACHE,
            _UNIVERSE_LIST_CACHE,
            _UNIVERSE_DETAIL_CACHE,
            _UNIVERSE_SEARCH_CACHE,
            _UNIVERSE_LABEL_CACHE,
            _API_SEARCH_CACHE,
            _HOME_PORTFOLIO_CACHE,
            _TRIAGE_COUNTS_CACHE,
            _DATASET_INDEX_CACHE,
            _DOC_NAV_CACHE,
            _DOC_REVIEW_CACHE,
            _NOTE_FILTER_CACHE,
            _SETTINGS_GOOGLE_STATUS_CACHE,
            _OPENSHELL_STATUS_CACHE,
            _CLOUD_MODEL_CATALOG_CACHE,
            _AGENT_SETTINGS_CACHE,
        ):
            cache.clear()
        return
    caches = (
        _HOME_INTELLIGENCE_CACHE,
        _SOURCE_HEALTH_CACHE,
        _UNIVERSE_LIST_CACHE,
        _UNIVERSE_DETAIL_CACHE,
        _UNIVERSE_SEARCH_CACHE,
        _UNIVERSE_LABEL_CACHE,
        _API_SEARCH_CACHE,
        _HOME_PORTFOLIO_CACHE,
        _TRIAGE_COUNTS_CACHE,
        _DATASET_INDEX_CACHE,
        _DOC_NAV_CACHE,
        _DOC_REVIEW_CACHE,
        _NOTE_FILTER_CACHE,
        _SETTINGS_GOOGLE_STATUS_CACHE,
        _OPENSHELL_STATUS_CACHE,
        _CLOUD_MODEL_CATALOG_CACHE,
        _AGENT_SETTINGS_CACHE,
    )
    for prefix in prefixes:
        for cache in caches:
            cache.invalidate_prefix(prefix)


def _cache_scope() -> str:
    return str(getattr(settings, "db_path", "default"))


def _session_cache_scope(session: Session) -> str:
    try:
        bind = session.get_bind()
        url = getattr(bind, "url", None)
        if url is not None:
            try:
                return str(url.render_as_string(hide_password=False))
            except Exception:
                return str(url)
        return str(id(bind))
    except Exception:
        return _cache_scope()


def _google_status_cache_key() -> str:
    return f"settings:google:{_cache_scope()}"


def _openshell_status_cache_key() -> str:
    return f"settings:openshell:{_cache_scope()}"


def _cloud_model_catalog_cache_key() -> str:
    return f"agents:cloud-models:{_cache_scope()}"


def _google_status_payload_live() -> dict[str, object]:
    from thoth_terminal.google_oauth import status as google_status

    return google_status().as_dict()


def _openshell_status_payload_live() -> dict[str, object]:
    from thoth_terminal.agent.openshell import openshell_runtime_status

    return openshell_runtime_status()


def _settings_google_status_payload(*, cached: bool = True) -> dict[str, object]:
    key = _google_status_cache_key()
    if cached:
        cached_payload = _SETTINGS_GOOGLE_STATUS_CACHE.get(key)
        if cached_payload is not None:
            return dict(cached_payload)
    try:
        payload = _google_status_payload_live()
    except Exception as exc:
        payload = {"connected": False, "error": f"{type(exc).__name__}: {exc}"}
    _SETTINGS_GOOGLE_STATUS_CACHE.set(key, dict(payload))
    return dict(payload)


def _invalidate_google_status_cache() -> None:
    _SETTINGS_GOOGLE_STATUS_CACHE.invalidate(_google_status_cache_key())


def _settings_openshell_status_payload(*, cached: bool = True) -> dict[str, object]:
    key = _openshell_status_cache_key()
    if cached:
        cached_payload = _OPENSHELL_STATUS_CACHE.get(key)
        if cached_payload is not None:
            return dict(cached_payload)
    try:
        payload = _openshell_status_payload_live()
    except Exception as exc:
        payload = {
            "enabled": bool(getattr(settings, "openshell_enabled", True)),
            "installed": False,
            "ready": False,
            "status": "probe_error",
            "detail": f"{type(exc).__name__}: {exc}",
        }
    _OPENSHELL_STATUS_CACHE.set(key, dict(payload))
    return dict(payload)


def _invalidate_openshell_status_cache() -> None:
    _OPENSHELL_STATUS_CACHE.invalidate(_openshell_status_cache_key())


def _cloud_model_catalog_payload(*, cached: bool = True) -> dict[str, Any]:
    key = _cloud_model_catalog_cache_key()
    if cached:
        cached_payload = _CLOUD_MODEL_CATALOG_CACHE.get(key)
        if cached_payload is not None:
            return dict(cached_payload)
    payload = build_model_catalog()
    _CLOUD_MODEL_CATALOG_CACHE.set(key, dict(payload))
    return dict(payload)


NOTE_STATUSES = ["active", "archived"]
THESIS_STATUSES = list(THESIS_WORKFLOW_STATUSES)
THESIS_ARCHIVE_STATUSES = sorted(THESIS_HIDDEN_STATUSES)


def _cached_note_filter_options(db: Session) -> dict[str, list[SimpleNamespace]]:
    cache_key = f"notes:filters:{_session_cache_scope(db)}"
    cached = _NOTE_FILTER_CACHE.get(cache_key)
    if cached is not None:
        return cached
    payload = {
        "domains": [
            SimpleNamespace(id=int(row.id), name=row.name, slug=row.slug)
            for row in db.query(Domain).order_by(Domain.name.asc()).all()
        ],
        "tags": [
            SimpleNamespace(id=int(row.id), name=row.name, slug=row.slug)
            for row in db.query(Tag).order_by(Tag.name.asc()).all()
        ],
    }
    return _NOTE_FILTER_CACHE.set(cache_key, payload)


def _request_includes_hidden_theses(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "all", "hidden"}


def _visible_thesis_query(query: Any, *, include_hidden: bool = False) -> Any:
    if include_hidden:
        return query
    return query.filter(~func.lower(Thesis.status).in_(THESIS_ARCHIVE_STATUSES))


def _latest_job_status(session: Session, *, kind: str) -> dict[str, object] | None:
    row = (
        session.query(Job)
        .filter(Job.kind == kind)
        .order_by(Job.created_at.desc(), Job.id.desc())
        .first()
    )
    if not row:
        return None
    return {
        "id": int(row.id),
        "status": str(row.status),
        "attempts": int(row.attempts or 0),
        "last_error": str(row.last_error or "")[:500],
        "updated_at": str(row.updated_at),
        "created_at": str(row.created_at),
    }


def _discord_pipeline_health(session: Session) -> dict[str, object]:
    from thoth_terminal.db import utcnow
    from thoth_terminal.discord.models import DiscordOutboxItem, DiscordWatchlistEntry

    now = utcnow()
    quote_stats = (
        session.query(
            func.count(DiscordWatchlistEntry.id),
            func.coalesce(
                func.sum(case((DiscordWatchlistEntry.last_quote_price.isnot(None), 1), else_=0)),
                0,
            ),
            func.coalesce(
                func.sum(case((DiscordWatchlistEntry.last_quote_at.is_(None), 1), else_=0)),
                0,
            ),
            func.coalesce(
                func.sum(
                    case(
                        (DiscordWatchlistEntry.last_quote_at >= (now - timedelta(hours=1)), 1),
                        else_=0,
                    )
                ),
                0,
            ),
            func.coalesce(
                func.sum(
                    case(
                        (
                            and_(
                                DiscordWatchlistEntry.last_quote_at < (now - timedelta(hours=1)),
                                DiscordWatchlistEntry.last_quote_at >= (now - timedelta(hours=6)),
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ),
                0,
            ),
            func.coalesce(
                func.sum(
                    case(
                        (
                            DiscordWatchlistEntry.last_quote_at < (now - timedelta(hours=6)),
                            1,
                        ),
                        else_=0,
                    )
                ),
                0,
            ),
        )
        .filter(DiscordWatchlistEntry.removed_at.is_(None))
        .one()
    )
    total = int(quote_stats[0] or 0)
    quoted = int(quote_stats[1] or 0)
    no_timestamp = int(quote_stats[2] or 0)
    age_lt_1h = int(quote_stats[3] or 0)
    age_1h_6h = int(quote_stats[4] or 0)
    age_gt_6h = int(quote_stats[5] or 0)
    guild_ids = sorted(
        {
            int(row[0])
            for row in session.query(DiscordWatchlistEntry.guild_id)
            .filter(DiscordWatchlistEntry.removed_at.is_(None))
            .distinct()
            .all()
            if row and row[0] is not None
        }
    )

    watchlist_refresh_job = _latest_job_status(session, kind="discord_watchlist_refresh")
    transcript_scan_job = _latest_job_status(session, kind="watchlist_transcript_scan")

    transcript_outbox_24h = (
        session.query(DiscordOutboxItem)
        .filter(DiscordOutboxItem.dedupe_key.like("transcript:%"))
        .filter(DiscordOutboxItem.created_at >= (now - timedelta(hours=24)))
        .count()
    )
    transcript_outbox_failed_24h = (
        session.query(DiscordOutboxItem)
        .filter(DiscordOutboxItem.dedupe_key.like("transcript:%"))
        .filter(DiscordOutboxItem.status == "failed")
        .filter(DiscordOutboxItem.created_at >= (now - timedelta(hours=24)))
        .count()
    )
    transcript_watch_states = session.query(TranscriptWatchState).count()
    transcript_watch_missing = session.query(TranscriptWatchState).filter_by(status="missing").count()
    transcript_watch_found = session.query(TranscriptWatchState).filter_by(status="found").count()

    likely_causes: list[str] = []
    if total > 0 and quoted == 0:
        likely_causes.append("No active quotes cached; Yahoo bulk fetch may be failing or symbols may not map.")
    if total > 0 and (watchlist_refresh_job and str(watchlist_refresh_job.get("status")) in {"error"}):
        likely_causes.append("Latest watchlist refresh job ended in error; inspect `last_error`.")
    if transcript_scan_job and str(transcript_scan_job.get("status")) in {"error"}:
        likely_causes.append("Latest transcript scan job ended in error; inspect `last_error`.")
    link_stats = discord_link_intake.inbox_stats()

    return {
        "watchlist_entries_total": total,
        "watchlist_entries_with_quotes": quoted,
        "watchlist_guild_ids": guild_ids,
        "quote_freshness": {
            "lt_1h": age_lt_1h,
            "h1_to_h6": age_1h_6h,
            "gt_6h": age_gt_6h,
            "missing_quote_at": no_timestamp,
        },
        "latest_watchlist_refresh_job": watchlist_refresh_job,
        "latest_transcript_scan_job": transcript_scan_job,
        "transcript_watch": {
            "states": int(transcript_watch_states),
            "missing": int(transcript_watch_missing),
            "found": int(transcript_watch_found),
        },
        "transcript_outbox_24h": {
            "created": int(transcript_outbox_24h),
            "failed": int(transcript_outbox_failed_24h),
        },
        "likely_causes": likely_causes,
        "link_intake": link_stats,
    }


def _ops_health_payload() -> dict[str, object]:
    global _ops_health_cache
    now = time.time()
    db_key = str(settings.db_path)
    cached_payload = _ops_health_cache.get("payload")
    if cached_payload and _ops_health_cache.get("db_path") == db_key and (
        now - float(_ops_health_cache.get("ts", 0.0))
    ) < _OPS_HEALTH_TTL_S:
        return cached_payload  # type: ignore[return-value]
    payload: dict[str, object] = {
        "runtime": get_runtime_identity(),
        "ok": True,
    }
    try:
        from thoth_terminal.html_native import native_available as html_native_available
        from thoth_terminal.search_native import native_available as search_native_available

        payload["native_backends"] = {
            "html": {"configured": "native", "available": html_native_available()},
            "search_rank": {"configured": "native", "available": search_native_available()},
        }
        if not html_native_available() or not search_native_available():
            payload["ok"] = False
    except Exception as exc:
        payload["native_backends"] = {"error": f"{type(exc).__name__}: {exc}"}
        payload["ok"] = False
    api_key_health = official_api_key_health()
    payload["api_keys"] = api_key_health
    if not bool(api_key_health.get("ok", True)):
        payload["ok"] = False
    session = _session_read()
    try:
        payload["discord_pipeline"] = _discord_pipeline_health(session)
    except Exception as exc:
        payload["ok"] = False
        payload["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        session.close()
    _ops_health_cache = {"ts": now, "db_path": db_key, "payload": payload}
    return payload


def _pid_alive(pid: int | None) -> bool:
    if pid is None or int(pid) <= 0:
        return False
    try:
        import os

        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def _format_duration_compact(seconds: int | None) -> str | None:
    if seconds is None:
        return None
    if seconds < 60:
        return "now"
    mins = seconds // 60
    if mins < 60:
        return f"{mins}m"
    hours = mins // 60
    if hours < 48:
        return f"{hours}h"
    days = hours // 24
    return f"{days}d"


def _source_sla_seconds(source: Source) -> int | None:
    cfg = source.config or {}
    sla_raw = cfg.get("freshness_sla_minutes")
    if sla_raw is not None and str(sla_raw).strip():
        try:
            return int(float(sla_raw)) * 60
        except Exception:
            return None
    min_s = cfg.get("poll_min_seconds")
    max_s = cfg.get("poll_max_seconds")
    base = max_s or min_s or source.polling_interval_seconds
    if not base:
        return None
    return max(1, int(base) * 3)


def _ns(**kwargs: object) -> SimpleNamespace:
    return SimpleNamespace(**kwargs)


def _source_health_rows(session: Session, *, include_deprecated: bool = False) -> list[dict[str, object]]:
    cache_key = f"sources:{_cache_scope()}:health:{1 if include_deprecated else 0}"
    cached = _SOURCE_HEALTH_CACHE.get(cache_key)
    if cached is not None:
        return cached
    srcs = (
        session.query(Source)
        .options(
            load_only(
                Source.id,
                Source.name,
                Source.category,
                Source.polling_interval_seconds,
                Source.config,
                Source.created_at,
            ),
            joinedload(Source.state).load_only(
                SourceState.source_id,
                SourceState.last_success_at,
                SourceState.next_run_at,
                SourceState.consecutive_failures,
                SourceState.backoff_until,
            ),
        )
        .order_by(Source.name.asc())
        .all()
    )
    srcs = [s for s in srcs if not (s.config or {}).get("deleted")]
    if not include_deprecated:
        srcs = [s for s in srcs if not is_source_deprecated_config(s.config or {})]

    ids = [s.id for s in srcs]
    latest_runs: dict[int, IngestionRun] = {}
    if ids:
        sub = (
            session.query(IngestionRun.source_id, func.max(IngestionRun.id).label("max_id"))
            .filter(IngestionRun.source_id.in_(ids))
            .group_by(IngestionRun.source_id)
            .subquery()
        )
        rows = (
            session.query(IngestionRun)
            .options(
                load_only(
                    IngestionRun.id,
                    IngestionRun.source_id,
                    IngestionRun.started_at,
                    IngestionRun.status,
                    IngestionRun.error,
                    IngestionRun.items_seen,
                    IngestionRun.items_new,
                    IngestionRun.items_updated,
                )
            )
            .join(sub, IngestionRun.id == sub.c.max_id)
            .all()
        )
        latest_runs = {r.source_id: r for r in rows}

    now = utcnow()
    silence_profiles = build_adaptive_source_silence_profiles(session, sources=srcs, now=now)
    health_rows: list[dict[str, object]] = []
    for source in srcs:
        cfg = dict(source.config or {})
        source_enabled = cfg.get("enabled") is not False and not bool(cfg.get("disabled_reason"))
        state = source.state
        last_run = latest_runs.get(source.id)
        sla_s = _source_sla_seconds(source)
        last_success = state.last_success_at if state else None
        age_s = int((now - last_success).total_seconds()) if last_success else None
        stale = False
        if source_enabled and sla_s:
            if last_success:
                stale = age_s is not None and age_s > sla_s
            else:
                age_created = int((now - source.created_at).total_seconds())
                stale = age_created > sla_s
        backoff_s = None
        if source_enabled and state and state.backoff_until and state.backoff_until > now:
            backoff_s = int((state.backoff_until - now).total_seconds())

        last_success_local, last_success_utc = _format_dt_pair(last_success)
        last_run_at = last_run.started_at if last_run else None
        last_run_local, last_run_utc = _format_dt_pair(last_run_at)
        next_run_local, next_run_utc = _format_dt_pair(state.next_run_at if state else None)
        failing = bool(
            source_enabled
            and ((state and int(state.consecutive_failures or 0) > 0) or (last_run and str(last_run.status) == "error"))
        )
        silence = silence_profiles.get(int(source.id), {})
        content_silent = bool(source_enabled and silence.get("silent"))
        content_critical = bool(source_enabled and silence.get("critical"))
        latest_new_at = silence.get("latest_new_at")
        latest_new_local, latest_new_utc = _format_dt_pair(latest_new_at if isinstance(latest_new_at, datetime) else None)

        source_payload = _ns(
            id=source.id,
            name=source.name,
            category=source.category,
            polling_interval_seconds=source.polling_interval_seconds,
            config=cfg,
            created_at=source.created_at,
        )
        state_payload = (
            _ns(
                source_id=state.source_id,
                last_success_at=state.last_success_at,
                next_run_at=state.next_run_at,
                consecutive_failures=state.consecutive_failures,
                backoff_until=state.backoff_until,
            )
            if state
            else None
        )
        run_payload = (
            _ns(
                id=last_run.id,
                source_id=last_run.source_id,
                started_at=last_run.started_at,
                status=last_run.status,
                error=last_run.error,
                items_seen=last_run.items_seen,
                items_new=last_run.items_new,
                items_updated=last_run.items_updated,
            )
            if last_run
            else None
        )
        health_rows.append(
            {
                "source": source_payload,
                "state": state_payload,
                "last_run": run_payload,
                "sla_s": sla_s,
                "age_s": age_s,
                "enabled": source_enabled,
                "disabled_reason": str(cfg.get("disabled_reason") or "").strip() or None,
                "stale": stale,
                "failing": failing,
                "content_silent": content_silent,
                "content_critical": content_critical,
                "content_common": bool(silence.get("common")),
                "latest_new_at": latest_new_at,
                "new_age_s": silence.get("new_age_s"),
                "new_age_label": _format_duration_compact(silence.get("new_age_s")),
                "expected_silence_s": silence.get("warning_s"),
                "expected_silence_label": _format_duration_compact(silence.get("warning_s")),
                "median_new_interval_s": silence.get("median_interval_s"),
                "median_new_interval_label": _format_duration_compact(silence.get("median_interval_s")),
                "productive_run_count": int(silence.get("productive_run_count") or 0),
                "zero_success_runs": int(silence.get("zero_success_runs") or 0),
                "latest_new_local": latest_new_local,
                "latest_new_utc": latest_new_utc,
                "backoff_s": backoff_s,
                "age_label": _format_duration_compact(age_s),
                "sla_label": _format_duration_compact(sla_s),
                "backoff_label": _format_duration_compact(backoff_s),
                "last_success_local": last_success_local,
                "last_success_utc": last_success_utc,
                "last_run_local": last_run_local,
                "last_run_utc": last_run_utc,
                "next_run_local": next_run_local,
                "next_run_utc": next_run_utc,
            }
        )
    return _SOURCE_HEALTH_CACHE.set(cache_key, health_rows, ttl_s=20.0)


def _ops_redirect_with_notice(*, notice: str | None = None, error: str | None = None) -> RedirectResponse:
    query = urlencode(
        {
            key: value[:240]
            for key, value in {"notice": notice, "error": error}.items()
            if isinstance(value, str) and value.strip()
        }
    )
    return RedirectResponse(url=f"/ops{('?' + query) if query else ''}", status_code=303)


def _agent_review_redirect_with_notice(
    *,
    notice: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    query = urlencode(
        {
            key: value[:240]
            for key, value in {"notice": notice, "error": error}.items()
            if isinstance(value, str) and value.strip()
        }
    )
    return RedirectResponse(url=f"/agent-review{('?' + query) if query else ''}", status_code=303)


def _sources_redirect_with_notice(*, notice: str | None = None, error: str | None = None) -> RedirectResponse:
    query = urlencode(
        {
            key: value[:240]
            for key, value in {"notice": notice, "error": error}.items()
            if isinstance(value, str) and value.strip()
        }
    )
    return RedirectResponse(url=f"/sources{('?' + query) if query else ''}", status_code=303)


def _ops_view_model(session: Session, *, guild_id: int | None = None) -> dict[str, object]:
    from thoth_terminal.discord.desk import get_desk_settings
    from thoth_terminal.discord.models import DiscordOutboxItem, DiscordQueueItem

    guild_val = int(guild_id) if guild_id else None
    health = _ops_health_payload()
    runtime = health.get("runtime") if isinstance(health.get("runtime"), dict) else get_runtime_identity()
    desk_cfg = get_desk_settings(guild_id=guild_val) if guild_val else None
    agent_raw, agent_effective = _agent_settings_for_guild(guild_val) if guild_val else ({}, {})

    runtime_services: list[dict[str, object]] = []
    dead_services: list[str] = []
    for state in list_runtime_states():
        service = str(state.get("service") or Path(str(state.get("_path") or "")).stem or "unknown")
        pid_raw = state.get("pid")
        try:
            pid = int(pid_raw) if pid_raw is not None else None
        except Exception:
            pid = None
        alive = _pid_alive(pid)
        if pid is not None and not alive:
            dead_services.append(service)
        runtime_services.append(
            {
                "service": service,
                "pid": pid,
                "alive": alive,
                "started_at": state.get("started_at_utc") or state.get("started_at"),
                "python_executable": state.get("python_executable") or state.get("python"),
                "path": state.get("_path"),
            }
        )
    runtime_services.sort(key=lambda row: str(row.get("service") or "").lower())

    job_counts: dict[str, int] = {}
    for status_name in ("pending", "running", "error", "success"):
        job_counts[status_name] = session.query(Job).filter_by(status=status_name).count()

    recent_job_rows = (
        session.query(Job)
        .order_by(Job.updated_at.desc(), Job.id.desc())
        .limit(20)
        .all()
    )
    recent_jobs: list[dict[str, object]] = []
    for row in recent_job_rows:
        payload = row.payload if isinstance(row.payload, dict) else {}
        details: list[str] = []
        if payload.get("guild_id") is not None:
            details.append(f"guild {payload.get('guild_id')}")
        if payload.get("source_id") is not None:
            details.append(f"source {payload.get('source_id')}")
        if payload.get("ticker"):
            details.append(str(payload.get("ticker")).upper())
        if payload.get("mode"):
            details.append(str(payload.get("mode")))
        if payload.get("lookback_hours") is not None:
            details.append(f"{payload.get('lookback_hours')}h")
        updated_local, updated_utc = _format_dt_pair(row.updated_at)
        recent_jobs.append(
            {
                "id": int(row.id),
                "kind": str(row.kind),
                "status": str(row.status),
                "priority": int(row.priority or 0),
                "attempts": int(row.attempts or 0),
                "details": " • ".join(details) if details else "-",
                "updated_local": updated_local,
                "updated_utc": updated_utc,
                "last_error": str(row.last_error or "")[:240],
            }
        )

    outbox_base = session.query(DiscordOutboxItem)
    queue_base = session.query(DiscordQueueItem)
    if guild_val:
        outbox_base = outbox_base.filter(DiscordOutboxItem.guild_id == int(guild_val))
        queue_base = queue_base.filter(DiscordQueueItem.guild_id == int(guild_val))

    outbox_counts: dict[str, int] = {}
    for status_name in ("pending", "sending", "failed", "posted", "canceled"):
        outbox_counts[status_name] = outbox_base.filter_by(status=status_name).count()

    queue_counts: dict[str, int] = {}
    for status_name in ("pending", "approved", "rejected"):
        queue_counts[status_name] = queue_base.filter_by(status=status_name).count()

    action_base = (
        session.query(AgentAction)
        .options(
            joinedload(AgentAction.thesis),
            joinedload(AgentAction.document).joinedload(Document.source),
            joinedload(AgentAction.note),
        )
    )
    if guild_val is not None:
        action_base = action_base.filter(or_(AgentAction.guild_id == int(guild_val), AgentAction.guild_id.is_(None)))

    action_counts: dict[str, int] = {}
    for status_name in (ACTION_STATUS_PENDING, ACTION_STATUS_RUNNING, ACTION_STATUS_COMPLETED, ACTION_STATUS_SKIPPED):
        action_counts[status_name] = action_base.filter(AgentAction.status.in_(action_status_filter_values(status_name))).count()

    recent_outbox: list[dict[str, object]] = []
    for item in (
        outbox_base.order_by(DiscordOutboxItem.created_at.desc(), DiscordOutboxItem.id.desc())
        .limit(12)
        .all()
    ):
        created_local, created_utc = _format_dt_pair(item.created_at)
        embed = item.embed_json if isinstance(item.embed_json, dict) else {}
        summary = str(
            item.dedupe_key
            or embed.get("title")
            or (item.content or "")
            or f"outbox {item.id}"
        )[:110]
        recent_outbox.append(
            {
                "id": int(item.id),
                "status": str(item.status),
                "summary": summary,
                "channel_id": int(item.channel_id),
                "thread_id": (int(item.thread_id) if item.thread_id else None),
                "created_local": created_local,
                "created_utc": created_utc,
                "last_error": str(item.last_error or "")[:180],
            }
        )

    recent_queue: list[dict[str, object]] = []
    for item in (
        queue_base.order_by(DiscordQueueItem.created_at.desc(), DiscordQueueItem.id.desc())
        .limit(12)
        .all()
    ):
        created_local, created_utc = _format_dt_pair(item.created_at)
        payload = item.payload if isinstance(item.payload, dict) else {}
        subject = (
            str(payload.get("ticker") or payload.get("symbol") or payload.get("title") or payload.get("url") or "")
            or f"queue {item.id}"
        )[:110]
        recent_queue.append(
            {
                "id": int(item.id),
                "kind": str(item.kind),
                "status": str(item.status),
                "subject": subject,
                "created_local": created_local,
                "created_utc": created_utc,
                "decision_note": str(item.decision_note or "")[:160],
            }
        )

    recent_actions: list[dict[str, object]] = []
    for item in action_base.order_by(AgentAction.priority.asc(), AgentAction.updated_at.desc(), AgentAction.id.desc()).limit(12).all():
        updated_local, updated_utc = _format_dt_pair(item.updated_at)
        ref_parts: list[str] = []
        if item.thesis_id:
            ref_parts.append(f"thesis {item.thesis_id}")
        if item.document_id:
            ref_parts.append(f"doc {item.document_id}")
        if item.note_id:
            ref_parts.append(f"note {item.note_id}")
        recent_actions.append(
            {
                "id": int(item.id),
                "kind": str(item.kind),
                "status": canonical_action_status(item.status),
                "priority": int(item.priority or 0),
                "title": str(item.title or f"action {item.id}"),
                "rationale": str(item.rationale or "")[:180],
                "thesis_id": int(item.thesis_id) if item.thesis_id is not None else None,
                "thesis_title": item.thesis.title if item.thesis else None,
                "document_id": int(item.document_id) if item.document_id is not None else None,
                "document_title": _doc_display_title(item.document) if item.document else None,
                "note_id": int(item.note_id) if item.note_id is not None else None,
                "note_title": item.note.title if item.note else None,
                "updated_local": updated_local,
                "updated_utc": updated_utc,
                "refs": " • ".join(ref_parts) if ref_parts else "—",
            }
        )

    change_base = (
        session.query(AgentChangeRun)
        .options(joinedload(AgentChangeRun.note), joinedload(AgentChangeRun.changes))
    )
    if guild_val is not None:
        change_base = change_base.filter(or_(AgentChangeRun.guild_id == int(guild_val), AgentChangeRun.guild_id.is_(None)))
    change_counts: dict[str, int] = {}
    for status_name in (RUN_STATUS_RUNNING, RUN_STATUS_SUCCESS, RUN_STATUS_ERROR):
        change_counts[status_name] = change_base.filter(AgentChangeRun.status == status_name).count()
    recent_change_runs: list[dict[str, object]] = []
    for item in change_base.order_by(AgentChangeRun.updated_at.desc(), AgentChangeRun.id.desc()).limit(12).all():
        updated_local, updated_utc = _format_dt_pair(item.updated_at)
        recent_change_runs.append(
            {
                "id": int(item.id),
                "task_kind": str(item.task_kind),
                "status": str(item.status),
                "engine_kind": str(item.engine_kind),
                "summary": str(item.summary or "")[:220],
                "change_count": len(item.changes or []),
                "source_job_id": int(item.source_job_id) if item.source_job_id is not None else None,
                "note_id": int(item.note_id) if item.note_id is not None else None,
                "note_title": item.note.title if item.note else None,
                "note_href": f"/notes/{int(item.note_id)}" if item.note_id is not None else None,
                "updated_local": updated_local,
                "updated_utc": updated_utc,
            }
        )

    proposal_base = (
        session.query(ThesisProposal)
        .options(joinedload(ThesisProposal.note), joinedload(ThesisProposal.approved_thesis))
    )
    if guild_val is not None:
        proposal_base = proposal_base.filter(or_(ThesisProposal.guild_id == int(guild_val), ThesisProposal.guild_id.is_(None)))
    proposal_counts: dict[str, int] = {}
    for status_name in (PROPOSAL_STATUS_PENDING, PROPOSAL_STATUS_PROMOTED, PROPOSAL_STATUS_REJECTED):
        proposal_counts[status_name] = proposal_base.filter(ThesisProposal.status == status_name).count()
    recent_proposals: list[dict[str, object]] = []
    for item in proposal_base.order_by(ThesisProposal.updated_at.desc(), ThesisProposal.id.desc()).limit(12).all():
        updated_local, updated_utc = _format_dt_pair(item.updated_at)
        recent_proposals.append(
            {
                "id": int(item.id),
                "status": str(item.status),
                "title": str(item.title),
                "novelty_score": round(float(item.novelty_score), 3) if item.novelty_score is not None else None,
                "confidence": round(float(item.confidence), 3) if item.confidence is not None else None,
                "rationale": str(item.rationale or "")[:220],
                "note_id": int(item.note_id) if item.note_id is not None else None,
                "note_href": f"/notes/{int(item.note_id)}" if item.note_id is not None else None,
                "approved_thesis_id": int(item.approved_thesis_id) if item.approved_thesis_id is not None else None,
                "approved_thesis_href": f"/theses/{int(item.approved_thesis_id)}" if item.approved_thesis_id is not None else None,
                "updated_local": updated_local,
                "updated_utc": updated_utc,
            }
        )

    source_rows = _source_health_rows(session)
    source_issues = [
        row
        for row in source_rows
        if bool(row.get("stale"))
        or bool(row.get("failing"))
        or bool(row.get("backoff_s"))
        or bool(row.get("content_silent"))
    ]
    source_issues.sort(
        key=lambda row: (
            0 if bool(row.get("content_critical")) else 1,
            0 if bool(row.get("content_silent")) else 1,
            0 if bool(row.get("stale")) else 1,
            0 if bool(row.get("failing")) else 1,
            -int((row.get("state").consecutive_failures if row.get("state") else 0) or 0),
            -(int(row.get("age_s") or 0)),
            str(getattr(row.get("source"), "name", "")).lower(),
        )
    )
    source_summary = {
        "total": len(source_rows),
        "stale": sum(1 for row in source_rows if bool(row.get("stale"))),
        "failing": sum(1 for row in source_rows if bool(row.get("failing"))),
        "backoff": sum(1 for row in source_rows if bool(row.get("backoff_s"))),
        "content_silent": sum(1 for row in source_rows if bool(row.get("content_silent"))),
        "content_critical": sum(1 for row in source_rows if bool(row.get("content_critical"))),
    }

    recent_agent_docs: list[dict[str, object]] = []
    for doc in (
        session.query(Document)
        .filter(Document.kind.in_(["agent_digest", "agent_context_compression"]))
        .order_by(func.coalesce(Document.published_at, Document.ingested_at).desc(), Document.id.desc())
        .limit(10)
        .all()
    ):
        published_local, published_utc = _format_dt_pair(doc.published_at or doc.ingested_at)
        recent_agent_docs.append(
            {
                "id": int(doc.id),
                "kind": str(doc.kind),
                "title": str(doc.title or f"doc {doc.id}"),
                "href": f"/docs/{int(doc.id)}",
                "published_local": published_local,
                "published_utc": published_utc,
            }
        )

    issues: list[str] = []
    if not bool(health.get("ok", False)):
        issues.append(str(health.get("error") or "Ops health check failed."))
    if dead_services:
        issues.append(f"Runtime state files reference dead services: {', '.join(dead_services[:6])}.")
    if int(job_counts.get("error") or 0) > 0:
        issues.append(f"{job_counts['error']} job(s) are in error.")
    if int(outbox_counts.get("failed") or 0) > 0:
        issues.append(f"{outbox_counts['failed']} Discord outbox item(s) failed.")
    if int(source_summary.get("stale") or 0) > 0:
        issues.append(f"{source_summary['stale']} source(s) are stale against their freshness window.")
    if int(source_summary.get("content_critical") or 0) > 0:
        issues.append(f"{source_summary['content_critical']} historically active source(s) have produced no new documents for at least seven days.")
    elif int(source_summary.get("content_silent") or 0) > 0:
        issues.append(f"{source_summary['content_silent']} historically active source(s) are unusually quiet.")
    if int(action_counts.get(ACTION_STATUS_PENDING) or 0) > 0:
        issues.append(f"{action_counts[ACTION_STATUS_PENDING]} agent action(s) are pending execution.")
    if int(proposal_counts.get(PROPOSAL_STATUS_PENDING) or 0) > 0:
        issues.append(f"{proposal_counts[PROPOSAL_STATUS_PENDING]} thesis proposal(s) await approval.")
    if guild_val and desk_cfg and not desk_cfg.desk_channel_id:
        issues.append("Desk channel is not configured for the primary Discord guild.")
    if guild_val and desk_cfg and bool(desk_cfg.outbox_paused):
        issues.append("Discord outbox is paused.")

    return {
        "runtime": runtime,
        "health": health,
        "scheduler_enabled": bool(getattr(settings, "web_scheduler_enabled", False)),
        "guild_id": guild_val,
        "desk": desk_cfg,
        "agent_settings_raw": agent_raw,
        "agent_settings_effective": agent_effective,
        "runtime_services": runtime_services,
        "service_summary": {
            "total": len(runtime_services),
            "alive": sum(1 for row in runtime_services if bool(row.get("alive"))),
            "dead": sum(1 for row in runtime_services if not bool(row.get("alive"))),
        },
        "job_counts": job_counts,
        "recent_jobs": recent_jobs,
        "outbox_counts": outbox_counts,
        "recent_outbox": recent_outbox,
        "queue_counts": queue_counts,
        "recent_queue": recent_queue,
        "action_counts": action_counts,
        "recent_actions": recent_actions,
        "change_counts": change_counts,
        "recent_change_runs": recent_change_runs,
        "proposal_counts": proposal_counts,
        "recent_proposals": recent_proposals,
        "source_summary": source_summary,
        "source_issues": source_issues[:10],
        "recent_agent_docs": recent_agent_docs,
        "issues": issues,
    }


def _wants_json(request: Request) -> bool:
    if request.url.path.startswith("/api/"):
        return True
    accept = (request.headers.get("accept") or "").lower()
    content_type = (request.headers.get("content-type") or "").lower()
    return "application/json" in accept or "application/json" in content_type


@app.exception_handler(DatabaseBusyError)
async def _db_busy_handler(request: Request, exc: DatabaseBusyError) -> Response:
    message = (
        "Published research is temporarily unavailable. Please try again later."
        if getattr(request.state, "public_surface", False)
        else str(exc) or "Database is busy. Please retry in a moment."
    )
    if _wants_json(request):
        return JSONResponse(status_code=503, content={"error": "database_busy", "detail": message})
    html = f"""
    <html>
      <head><title>Thoth · Busy</title></head>
      <body style="font-family:Arial, sans-serif; padding:32px;">
        <h2>Thoth is busy</h2>
        <p>{message}</p>
        <p><a href="javascript:history.back()">Go back</a></p>
      </body>
    </html>
    """
    return HTMLResponse(status_code=503, content=html)


def _split_lines(value: str | None) -> list[str]:
    if not value:
        return []
    parts = [p.strip() for p in value.split("\n")]
    return [p for p in parts if p]


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    parts = [p.strip() for p in value.split(",")]
    return [p for p in parts if p]


def _doc_display_title(doc: Document | None) -> str:
    if not doc:
        return ""
    extra = doc.extra if isinstance(doc.extra, dict) else {}
    translated = extra.get("translation_title_en")
    return str(translated or doc.title or doc.url or f"doc {doc.id}")


def _query_string(params: dict[str, object]) -> str:
    clean: dict[str, object] = {}
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, bool):
            if value:
                clean[key] = "1"
            continue
        raw = str(value).strip()
        if not raw:
            continue
        clean[key] = value
    return urlencode(clean)


_EARNINGS_KEYWORDS = ("earnings", "financial results", "financial result")
_EARNINGS_FEED_COOKIE = "thoth_earnings_rail"
_EARNINGS_FEED_LEAD_DAYS = 5
_EARNINGS_FEED_GRACE_DAYS = 2
_EARNINGS_FEED_LOOKAHEAD_DAYS = 75
_EARNINGS_FEED_SEASON_DAYS = 45


def _earnings_title_filter():
    clauses = []
    for kw in _EARNINGS_KEYWORDS:
        like = f"%{kw}%"
        clauses.append(Document.title.ilike(like))
        clauses.append(Document.normalized_text.ilike(like))
    # Catch titles like "Q4 FY26 Results" without "earnings" phrasing.
    results_like = Document.title.ilike("%results%")
    quarter_like = or_(
        Document.title.ilike("%quarter%"),
        Document.title.ilike("%q1%"),
        Document.title.ilike("%q2%"),
        Document.title.ilike("%q3%"),
        Document.title.ilike("%q4%"),
        Document.title.ilike("%fiscal%"),
        Document.title.ilike("%fy%"),
    )
    clauses.append(and_(results_like, quarter_like))
    return or_(*clauses) if clauses else None


def _is_earnings_season(dt: datetime | None = None) -> bool:
    ref = (dt or utcnow()).date()
    windows = [
        (1, 15, 2, 15),
        (4, 10, 5, 15),
        (7, 10, 8, 15),
        (10, 10, 11, 15),
    ]
    for m1, d1, m2, d2 in windows:
        start = date(ref.year, m1, d1)
        end = date(ref.year, m2, d2)
        if start <= ref <= end:
            return True
    return False


def _cookie_enabled(request: Request, key: str, *, default: bool = False) -> bool:
    raw = request.cookies.get(key)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _cookie_choice(request: Request, key: str, *, allowed: set[str], default: str) -> str:
    raw = str(request.cookies.get(key) or "").strip().lower()
    if raw in allowed:
        return raw
    return default


def _parse_minutes(value: str | None) -> int | None:
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        minutes = int(raw)
    except ValueError:
        return None
    return minutes * 60 if minutes > 0 else None


def _clean_optional_text(value: str | None) -> str | None:
    raw = str(value or "").strip()
    return raw or None


def _source_profile_payload(
    *,
    source_family: str | None,
    rights_class: str | None,
    acquisition_mode: str | None,
    cadence_label: str | None,
    review_required: bool,
    subject_binding_kind: str | None,
    site_binding_kind: str | None,
    jurisdiction_template_kind: str | None,
    vendor_tier: str | None,
    profile_notes: str | None,
) -> dict[str, object]:
    return {
        "source_family": _clean_optional_text(source_family),
        "rights_class": _clean_optional_text(rights_class) or "public",
        "acquisition_mode": _clean_optional_text(acquisition_mode) or "connector",
        "cadence_label": _clean_optional_text(cadence_label),
        "review_required": bool(review_required),
        "subject_binding_kind": _clean_optional_text(subject_binding_kind),
        "site_binding_kind": _clean_optional_text(site_binding_kind),
        "jurisdiction_template_kind": _clean_optional_text(jurisdiction_template_kind),
        "vendor_tier": _clean_optional_text(vendor_tier),
        "notes": _clean_optional_text(profile_notes),
    }


def _doc_payload(doc: Document) -> dict[str, object]:
    return {
        "id": doc.id,
        "triage_state": doc.triage_state,
        "saved": bool(doc.saved),
        "opened_at": doc.opened_at.isoformat() if doc.opened_at else None,
        "saved_at": doc.saved_at.isoformat() if doc.saved_at else None,
        "actioned_at": doc.actioned_at.isoformat() if doc.actioned_at else None,
        "ignored_at": doc.ignored_at.isoformat() if doc.ignored_at else None,
        "seen_at": doc.seen_at.isoformat() if doc.seen_at else None,
        "triage_updated_at": doc.triage_updated_at.isoformat() if doc.triage_updated_at else None,
    }


def _is_x_url(url: str | None) -> bool:
    if not url:
        return False
    u = url.strip().lower()
    return "://x.com/" in u or "://twitter.com/" in u


def _is_db_locked(exc: Exception) -> bool:
    return isinstance(exc, OperationalError) and "database is locked" in str(exc).lower()


def _with_db_retry(fn, *, attempts: int = 4, delay_s: float = 0.15):
    for attempt in range(attempts):
        try:
            return fn()
        except (DatabaseBusyError, OperationalError) as exc:
            if isinstance(exc, OperationalError) and not _is_db_locked(exc):
                raise
            time.sleep(delay_s * (2**attempt))
    return fn()


def _format_dt_pair(dt: datetime | None, *, tz_name: str | None = None) -> tuple[str | None, str | None]:
    if not dt:
        return None, None
    tz_key = tz_name or getattr(settings, "default_active_window_tz", None) or "UTC"
    try:
        local_tz = ZoneInfo(tz_key)
    except Exception:
        local_tz = ZoneInfo("UTC")
    dt_utc = dt if dt.tzinfo else dt.replace(tzinfo=ZoneInfo("UTC"))
    dt_local = dt_utc.astimezone(local_tz)
    return (
        f"{dt_local.strftime('%Y-%m-%d %H:%M')} {local_tz.key}",
        f"{dt_utc.strftime('%Y-%m-%d %H:%M')} UTC",
    )


def _delete_doc_batch(session: Session, doc_ids: list[int]) -> None:
    if not doc_ids:
        return
    session.query(DocumentMention).filter(
        DocumentMention.source_document_id.in_(doc_ids)
    ).delete(synchronize_session=False)
    session.query(DocumentMention).filter(
        DocumentMention.target_document_id.in_(doc_ids)
    ).delete(synchronize_session=False)
    session.query(DocumentEvent).filter(
        DocumentEvent.document_id.in_(doc_ids)
    ).delete(synchronize_session=False)
    session.query(Attachment).filter(
        Attachment.document_id.in_(doc_ids)
    ).delete(synchronize_session=False)
    session.query(DocumentEntity).filter(
        DocumentEntity.document_id.in_(doc_ids)
    ).delete(synchronize_session=False)
    session.query(NoteDocument).filter(
        NoteDocument.document_id.in_(doc_ids)
    ).delete(synchronize_session=False)
    session.query(ThesisEvidence).filter(
        ThesisEvidence.document_id.in_(doc_ids)
    ).delete(synchronize_session=False)
    session.query(Observation).filter(
        Observation.document_id.in_(doc_ids)
    ).delete(synchronize_session=False)

    session.execute(
        document_domains.delete().where(document_domains.c.document_id.in_(doc_ids))
    )
    session.execute(
        document_tags.delete().where(document_tags.c.document_id.in_(doc_ids))
    )
    session.execute(
        document_event_types.delete().where(document_event_types.c.document_id.in_(doc_ids))
    )

    session.query(Document).filter(Document.id.in_(doc_ids)).delete(
        synchronize_session=False
    )


def _purge_source_docs(source_id: int, *, batch_size: int = 200) -> None:
    while True:
        def _fetch_batch():
            with session_scope() as session:
                rows = (
                    session.query(Document.id)
                    .filter_by(source_id=source_id)
                    .limit(batch_size)
                    .all()
                )
                return [int(r[0]) for r in rows]

        ids = _with_db_retry(_fetch_batch)
        if not ids:
            break

        def _delete_batch():
            with session_scope() as session:
                _delete_doc_batch(session, ids)

        _with_db_retry(_delete_batch)


def _seen_with_retry(doc_id: int, *, attempts: int = 4) -> Document | None:
    delay_s = 0.05
    for attempt in range(attempts):
        try:
            with session_scope() as session:
                doc = mark_seen(session, document_id=int(doc_id), meta={"source": "api:seen"})
            return doc
        except (DatabaseBusyError, OperationalError) as exc:
            if isinstance(exc, OperationalError) and not _is_db_locked(exc):
                raise
            time.sleep(delay_s * (2**attempt))
    return None


def _bulk_apply_with_retry(
    doc_ids: list[int],
    action: str,
    *,
    attempts: int = 4,
) -> tuple[bool, dict[str, int], list[int]]:
    delay_s = 0.1
    ids = [int(i) for i in doc_ids]
    for attempt in range(attempts):
        try:
            with session_scope() as session:
                counts = bulk_apply(
                    session,
                    document_ids=ids,
                    action=action,
                    meta={"source": "api:bulk"},
                )
            return (True, counts, [])
        except (DatabaseBusyError, OperationalError) as exc:
            if isinstance(exc, OperationalError) and not _is_db_locked(exc):
                raise
            time.sleep(min(delay_s * (2**attempt), 2.0))
    return (False, {}, ids)


def _bulk_seen_with_retry(doc_ids: list[int], *, attempts: int = 4) -> tuple[bool, dict[str, int], list[int]]:
    return _bulk_apply_with_retry(doc_ids, "seen", attempts=attempts)


def _normalize_source_policy_settings() -> None:
    with session_scope() as session:
        rows = session.query(Source).all()
        for row in rows:
            cfg = dict(row.config or {})
            cfg, changed_a = apply_noninteractive_browser_policy(row.name, cfg)
            cfg, changed_b = apply_source_deprecation_policy(row.name, cfg, category=row.category)
            changed = changed_a or changed_b
            if changed:
                row.config = cfg
        cleanup_legacy_company_sources(session)
