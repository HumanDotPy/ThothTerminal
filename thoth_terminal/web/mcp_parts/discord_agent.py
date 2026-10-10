from __future__ import annotations

from thoth_terminal.web.mcp_parts._shared import bind_mcp_context


bind_mcp_context(globals())


def _mcp_tool_discord_outbox_create(params: dict[str, object]) -> dict[str, object]:
    guild_id = params.get("guild_id")
    channel_id = params.get("channel_id")
    if guild_id is None or not str(guild_id).isdigit():
        raise ValueError("guild_id is required.")
    if channel_id is None or not str(channel_id).isdigit():
        raise ValueError("channel_id is required.")
    thread_id = params.get("thread_id")
    thread_val = int(thread_id) if thread_id is not None and str(thread_id).isdigit() else None
    content = params.get("content")
    content_val = str(content) if isinstance(content, str) and content.strip() else None
    embed = params.get("embed")
    embed_val = embed if isinstance(embed, dict) else None
    dedupe_key = str(params.get("dedupe_key") or "").strip() or None
    priority = params.get("priority")
    prio_val = int(priority) if isinstance(priority, int) or str(priority).isdigit() else 5
    run_after = _parse_iso_utc_naive(params.get("run_after"))

    from thoth_terminal.discord import outbox as outbox_mod

    def _create() -> dict[str, object]:
        item = outbox_mod.create_outbox_item(
            guild_id=int(guild_id),
            channel_id=int(channel_id),
            thread_id=thread_val,
            content=content_val,
            embed_json=embed_val,
            dedupe_key=dedupe_key,
            priority=prio_val,
            run_after=run_after,
            created_by_kind="agent",
            created_by_user_id=None,
        )
        return {"id": int(item.id), "status": str(item.status)}

    return _with_db_retry(_create)


def _mcp_tool_discord_outbox_list(params: dict[str, object]) -> dict[str, object]:
    guild_id = params.get("guild_id")
    if guild_id is None or not str(guild_id).isdigit():
        raise ValueError("guild_id is required.")
    status = str(params.get("status") or "").strip().lower() or None
    limit = params.get("limit")
    limit_val = int(limit) if isinstance(limit, int) or str(limit).isdigit() else 20

    from thoth_terminal.discord import outbox as outbox_mod

    def _list() -> dict[str, object]:
        items = outbox_mod.list_outbox(guild_id=int(guild_id), status=status, limit=limit_val)
        out = []
        for it in items:
            out.append(
                {
                    "id": int(it.id),
                    "status": str(it.status),
                    "priority": int(it.priority or 0),
                    "dedupe_key": it.dedupe_key,
                    "channel_id": int(it.channel_id),
                    "thread_id": int(it.thread_id) if it.thread_id else None,
                    "created_at": it.created_at.isoformat() if it.created_at else None,
                    "posted_at": it.posted_at.isoformat() if it.posted_at else None,
                    "attempts": int(it.attempts or 0),
                    "last_error": it.last_error,
                }
            )
        return {"count": len(out), "items": out}

    return _with_db_retry(_list)


def _mcp_tool_discord_outbox_cancel(params: dict[str, object]) -> dict[str, object]:
    guild_id = params.get("guild_id")
    outbox_id = params.get("outbox_id")
    if guild_id is None or not str(guild_id).isdigit():
        raise ValueError("guild_id is required.")
    if outbox_id is None or not str(outbox_id).isdigit():
        raise ValueError("outbox_id is required.")

    from thoth_terminal.discord import outbox as outbox_mod

    def _cancel() -> dict[str, object]:
        ok = outbox_mod.cancel_outbox_item(guild_id=int(guild_id), outbox_id=int(outbox_id))
        return {"ok": bool(ok)}

    return _with_db_retry(_cancel)


def _mcp_tool_discord_queue_create(params: dict[str, object]) -> dict[str, object]:
    guild_id = params.get("guild_id")
    kind = str(params.get("kind") or "").strip().lower()
    payload = params.get("payload")
    if guild_id is None or not str(guild_id).isdigit():
        raise ValueError("guild_id is required.")
    if not kind:
        raise ValueError("kind is required.")
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object.")
    allowed = {"exec_watchlist_add", "source_add", "masterlist_add", "masterlist_remove"}
    if kind not in allowed:
        raise ValueError(f"Unsupported kind. Allowed: {', '.join(sorted(allowed))}")
    created_by_user_id = params.get("created_by_user_id")
    created_by_val = int(created_by_user_id) if created_by_user_id is not None and str(created_by_user_id).isdigit() else 0

    from thoth_terminal.discord.models import DiscordQueueItem
    from thoth_terminal.db import utcnow

    def _create() -> dict[str, object]:
        now = utcnow()
        with session_scope() as session:
            item = DiscordQueueItem(
                guild_id=int(guild_id),
                kind=kind,
                status="pending",
                payload=dict(payload),
                created_at=now,
                created_by_user_id=int(created_by_val),
                decided_at=None,
                decided_by_user_id=None,
                decision_note=None,
            )
            session.add(item)
            session.flush()
            return {"id": int(item.id), "status": str(item.status), "kind": kind}

    return _with_db_retry(_create)


def _mcp_tool_discord_queue_list(params: dict[str, object]) -> dict[str, object]:
    guild_id = params.get("guild_id")
    if guild_id is None or not str(guild_id).isdigit():
        raise ValueError("guild_id is required.")
    status = str(params.get("status") or "pending").strip().lower()
    limit = params.get("limit")
    limit_val = int(limit) if isinstance(limit, int) or str(limit).isdigit() else 20

    from thoth_terminal.discord import services as discord_services

    def _list() -> dict[str, object]:
        items = discord_services.list_queue(guild_id=int(guild_id), status=status, limit=limit_val)
        out = []
        for it in items:
            out.append(
                {
                    "id": int(it.id),
                    "kind": str(it.kind),
                    "status": str(it.status),
                    "payload": dict(it.payload) if isinstance(it.payload, dict) else {},
                    "created_at": it.created_at.isoformat() if it.created_at else None,
                    "created_by_user_id": int(it.created_by_user_id),
                }
            )
        return {"count": len(out), "items": out}

    return _with_db_retry(_list)


def _mcp_tool_discord_watchlist_add(params: dict[str, object]) -> dict[str, object]:
    guild_id = params.get("guild_id")
    ticker = str(params.get("ticker") or "").strip()
    if guild_id is None or not str(guild_id).isdigit():
        raise ValueError("guild_id is required.")
    if not ticker:
        raise ValueError("ticker is required.")
    sector = params.get("sector")
    subgroup = params.get("subgroup")
    sector_v = str(sector) if isinstance(sector, str) and sector.strip() else None
    subgroup_v = str(subgroup) if isinstance(subgroup, str) and subgroup.strip() else None

    from thoth_terminal.discord import services as discord_services
    from thoth_terminal.jobs import (
        enqueue_discord_watchlist_refresh_debounced,
        enqueue_discord_watchlist_transcript_scan_debounced,
    )

    def _add() -> dict[str, object]:
        res = discord_services.watchlist_add(
            guild_id=int(guild_id),
            list_kind="community",
            symbol=ticker,
            sector=sector_v,
            subgroup=subgroup_v,
            user_id=0,
        )
        if res.status in {"added", "reactivated", "updated"}:
            delay_s = int(getattr(settings, "discord_watchlist_immediate_refresh_delay_seconds", 8) or 8)
            transcript_lookback_days = max(
                1,
                int(getattr(settings, "discord_transcript_scan_lookback_days", 120) or 120),
            )
            with session_scope() as session:
                if bool(getattr(settings, "discord_watchlist_refresh_enabled", True)):
                    enqueue_discord_watchlist_refresh_debounced(
                        session,
                        guild_id=int(guild_id),
                        delay_seconds=delay_s,
                    )
                if bool(getattr(settings, "discord_transcript_scan_enabled", True)) and bool(
                    getattr(settings, "discord_transcript_scan_immediate_enabled", False)
                ):
                    enqueue_discord_watchlist_transcript_scan_debounced(
                        session,
                        guild_id=int(guild_id),
                        delay_seconds=delay_s,
                        lookback_days=transcript_lookback_days,
                    )
        return {
            "list_kind": res.list_kind,
            "symbol": res.symbol,
            "status": res.status,
            "sector": res.sector,
            "subgroup": res.subgroup,
        }

    return _with_db_retry(_add)


def _mcp_tool_discord_watchlist_bulk_add(params: dict[str, object]) -> dict[str, object]:
    guild_id = params.get("guild_id")
    items = params.get("items")
    if guild_id is None or not str(guild_id).isdigit():
        raise ValueError("guild_id is required.")
    if not isinstance(items, list):
        raise ValueError("items must be an array.")

    from thoth_terminal.discord import services as discord_services
    from thoth_terminal.jobs import (
        enqueue_discord_watchlist_refresh_debounced,
        enqueue_discord_watchlist_transcript_scan_debounced,
    )

    def _bulk() -> dict[str, object]:
        out: list[dict[str, object]] = []
        changed = 0
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                out.append({"index": idx, "ok": False, "error": "item must be an object"})
                continue
            ticker = str(item.get("ticker") or "").strip()
            sector = item.get("sector")
            subgroup = item.get("subgroup")
            sector_v = str(sector) if isinstance(sector, str) and sector.strip() else None
            subgroup_v = str(subgroup) if isinstance(subgroup, str) and subgroup.strip() else None
            if not ticker:
                out.append({"index": idx, "ok": False, "error": "ticker is required"})
                continue
            try:
                res = discord_services.watchlist_add(
                    guild_id=int(guild_id),
                    list_kind="community",
                    symbol=ticker,
                    sector=sector_v,
                    subgroup=subgroup_v,
                    user_id=0,
                )
                status = str(res.status)
                if status in {"added", "reactivated", "updated"}:
                    changed += 1
                out.append(
                    {
                        "index": idx,
                        "ok": True,
                        "symbol": res.symbol,
                        "status": status,
                        "sector": res.sector,
                        "subgroup": res.subgroup,
                    }
                )
            except Exception as exc:
                out.append({"index": idx, "ok": False, "error": str(exc), "ticker": ticker})

        if changed:
            delay_s = int(getattr(settings, "discord_watchlist_immediate_refresh_delay_seconds", 8) or 8)
            transcript_lookback_days = max(
                1,
                int(getattr(settings, "discord_transcript_scan_lookback_days", 120) or 120),
            )
            with session_scope() as session:
                if bool(getattr(settings, "discord_watchlist_refresh_enabled", True)):
                    enqueue_discord_watchlist_refresh_debounced(
                        session,
                        guild_id=int(guild_id),
                        delay_seconds=delay_s,
                    )
                if bool(getattr(settings, "discord_transcript_scan_enabled", True)) and bool(
                    getattr(settings, "discord_transcript_scan_immediate_enabled", False)
                ):
                    enqueue_discord_watchlist_transcript_scan_debounced(
                        session,
                        guild_id=int(guild_id),
                        delay_seconds=delay_s,
                        lookback_days=transcript_lookback_days,
                    )

        return {
            "count": len(out),
            "ok_count": sum(1 for row in out if bool(row.get("ok"))),
            "error_count": sum(1 for row in out if not bool(row.get("ok"))),
            "changed_count": int(changed),
            "results": out,
        }

    return _with_db_retry(_bulk)


def _mcp_tool_discord_watchlist_remove(params: dict[str, object]) -> dict[str, object]:
    guild_id = params.get("guild_id")
    ticker = str(params.get("ticker") or "").strip()
    if guild_id is None or not str(guild_id).isdigit():
        raise ValueError("guild_id is required.")
    if not ticker:
        raise ValueError("ticker is required.")
    sector = params.get("sector")
    subgroup = params.get("subgroup")
    sector_v = str(sector) if isinstance(sector, str) and sector.strip() else None
    subgroup_v = str(subgroup) if isinstance(subgroup, str) and subgroup.strip() else None
    all_matches = bool(params.get("all_matches")) if params.get("all_matches") is not None else False

    from thoth_terminal.discord import services as discord_services

    def _rm() -> dict[str, object]:
        removed = discord_services.watchlist_remove(
            guild_id=int(guild_id),
            list_kind="community",
            symbol=ticker,
            sector=sector_v,
            subgroup=subgroup_v,
            all_matches=all_matches,
            user_id=0,
        )
        return {"removed": int(removed)}

    return _with_db_retry(_rm)


def _mcp_tool_discord_queue_bulk_create(params: dict[str, object]) -> dict[str, object]:
    guild_id = params.get("guild_id")
    items = params.get("items")
    if guild_id is None or not str(guild_id).isdigit():
        raise ValueError("guild_id is required.")
    if not isinstance(items, list):
        raise ValueError("items must be an array.")

    default_created_by = params.get("created_by_user_id")
    default_created_by_val = (
        int(default_created_by)
        if default_created_by is not None and str(default_created_by).isdigit()
        else 0
    )
    allowed = {"exec_watchlist_add", "source_add", "masterlist_add", "masterlist_remove"}

    from thoth_terminal.discord.models import DiscordQueueItem
    from thoth_terminal.db import utcnow

    def _bulk() -> dict[str, object]:
        out: list[dict[str, object]] = []
        with session_scope() as session:
            now = utcnow()
            for idx, item in enumerate(items):
                if not isinstance(item, dict):
                    out.append({"index": idx, "ok": False, "error": "item must be an object"})
                    continue
                kind = str(item.get("kind") or "").strip().lower()
                payload = item.get("payload")
                created_by = item.get("created_by_user_id")
                created_by_val = (
                    int(created_by) if created_by is not None and str(created_by).isdigit() else default_created_by_val
                )
                if kind not in allowed:
                    out.append(
                        {
                            "index": idx,
                            "ok": False,
                            "error": f"Unsupported kind. Allowed: {', '.join(sorted(allowed))}",
                        }
                    )
                    continue
                if not isinstance(payload, dict):
                    out.append({"index": idx, "ok": False, "error": "payload must be an object"})
                    continue
                row = DiscordQueueItem(
                    guild_id=int(guild_id),
                    kind=kind,
                    status="pending",
                    payload=dict(payload),
                    created_at=now,
                    created_by_user_id=int(created_by_val),
                    decided_at=None,
                    decided_by_user_id=None,
                    decision_note=None,
                )
                session.add(row)
                session.flush()
                out.append(
                    {
                        "index": idx,
                        "ok": True,
                        "id": int(row.id),
                        "kind": str(row.kind),
                        "status": str(row.status),
                    }
                )

        return {
            "count": len(out),
            "ok_count": sum(1 for row in out if bool(row.get("ok"))),
            "error_count": sum(1 for row in out if not bool(row.get("ok"))),
            "results": out,
        }

    return _with_db_retry(_bulk)


def _mcp_tool_discord_link_collect_now(params: dict[str, object]) -> dict[str, object]:
    guild_id = params.get("guild_id")
    if guild_id is None or not str(guild_id).isdigit():
        raise ValueError("guild_id is required.")
    limit = params.get("limit")
    limit_val = int(limit) if isinstance(limit, int) or str(limit).isdigit() else 200

    def _run() -> dict[str, object]:
        reconciled = discord_link_intake.reconcile_collected_links(
            guild_id=int(guild_id),
            limit=max(1, min(limit_val, 500)),
        )
        stats = discord_link_intake.inbox_stats(guild_id=int(guild_id))
        return {
            "collector_mode": "discord_bot_loop",
            "reconciled": reconciled,
            "stats": stats,
        }

    return _with_db_retry(_run)


def _mcp_tool_discord_link_inbox_list(params: dict[str, object]) -> dict[str, object]:
    guild_id = params.get("guild_id")
    if guild_id is None or not str(guild_id).isdigit():
        raise ValueError("guild_id is required.")
    status = str(params.get("status") or "").strip().lower() or None
    limit = params.get("limit")
    limit_val = int(limit) if isinstance(limit, int) or str(limit).isdigit() else 100

    def _list() -> dict[str, object]:
        items = discord_link_intake.list_inbox_items(
            guild_id=int(guild_id),
            status=status,
            limit=max(1, min(limit_val, 500)),
        )
        return {"count": len(items), "items": items}

    return _with_db_retry(_list)


def _mcp_tool_discord_link_inbox_apply_decisions(params: dict[str, object]) -> dict[str, object]:
    guild_id = params.get("guild_id")
    decisions = params.get("decisions")
    if guild_id is None or not str(guild_id).isdigit():
        raise ValueError("guild_id is required.")
    if not isinstance(decisions, list):
        raise ValueError("decisions must be an array.")
    created_by_user_id = params.get("created_by_user_id")
    created_by_val = int(created_by_user_id) if created_by_user_id is not None and str(created_by_user_id).isdigit() else 0
    confidence_min = params.get("confidence_min")
    confidence_min_val = float(confidence_min) if confidence_min is not None else None
    queue_threshold = params.get("queue_threshold")
    queue_threshold_val = float(queue_threshold) if queue_threshold is not None else None

    def _apply() -> dict[str, object]:
        return discord_link_intake.apply_agentic_decisions(
            guild_id=int(guild_id),
            decisions=[dict(row) for row in decisions if isinstance(row, dict)],
            created_by_user_id=int(created_by_val),
            confidence_min=confidence_min_val,
            queue_threshold=queue_threshold_val,
        )

    return _with_db_retry(_apply)


def _mcp_tool_discord_link_inbox_stats(params: dict[str, object]) -> dict[str, object]:
    guild_id = params.get("guild_id")
    guild_val = int(guild_id) if guild_id is not None and str(guild_id).isdigit() else None

    def _stats() -> dict[str, object]:
        return discord_link_intake.inbox_stats(guild_id=guild_val)

    return _with_db_retry(_stats)


def _mcp_tool_agent_enqueue_digest(params: dict[str, object]) -> dict[str, object]:
    guild_id = params.get("guild_id")
    mode = str(params.get("mode") or "").strip().lower()
    if guild_id is None or not str(guild_id).isdigit():
        raise ValueError("guild_id is required.")
    if mode not in {"daily", "manual"}:
        raise ValueError("mode must be daily|manual")
    lookback = params.get("lookback_hours")
    lookback_h = int(lookback) if lookback is not None and (isinstance(lookback, int) or str(lookback).isdigit()) else None

    from thoth_terminal.discord.desk import get_desk_settings
    from thoth_terminal.jobs import enqueue_agent_digest_job

    cfg = get_desk_settings(guild_id=int(guild_id))
    if not cfg.desk_channel_id or not cfg.desk_thread_id:
        raise ValueError("Desk thread not configured. Run `/desk setup` in Discord first.")

    tz_name = str(getattr(settings, "discord_desk_digest_tz", "America/New_York") or "America/New_York")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
    now_local = utcnow().replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
    raw_date_key = str(params.get("date_key") or "").strip()
    date_key = raw_date_key or now_local.date().isoformat()
    raw_dedupe = str(params.get("dedupe_key") or "").strip()
    dedupe = raw_dedupe or (f"agent_digest:{int(guild_id)}:{date_key}" if mode == "daily" else None)
    title_override = str(params.get("title_override") or "").strip() or None
    engine = str(params.get("engine") or "").strip() or None

    def _enqueue() -> dict[str, object]:
        with session_scope() as session:
            job = enqueue_agent_digest_job(
                session,
                guild_id=int(guild_id),
                mode=mode,
                lookback_hours=lookback_h,
                post_channel_id=int(cfg.desk_channel_id),
                post_thread_id=int(cfg.desk_thread_id),
                date_key=date_key,
                title_override=title_override,
                engine=engine,
                dedupe_key=dedupe,
            )
            return {"job_id": int(job.id), "dedupe_key": job.dedupe_key}

    return _with_db_retry(_enqueue)


def _mcp_tool_agent_enqueue_ticker_report(params: dict[str, object]) -> dict[str, object]:
    guild_id = params.get("guild_id")
    ticker = str(params.get("ticker") or "").strip()
    if guild_id is None or not str(guild_id).isdigit():
        raise ValueError("guild_id is required.")
    if not ticker:
        raise ValueError("ticker is required.")
    days = params.get("days")
    days_val = int(days) if days is not None and (isinstance(days, int) or str(days).isdigit()) else 30
    post_to_outbox = bool(params.get("post_to_outbox")) if params.get("post_to_outbox") is not None else False

    from thoth_terminal.discord.desk import get_desk_settings
    from thoth_terminal.jobs import enqueue_agent_ticker_job

    cfg = get_desk_settings(guild_id=int(guild_id))
    post_channel_id = None
    post_thread_id = None
    if post_to_outbox:
        if not cfg.desk_channel_id or not cfg.desk_thread_id:
            raise ValueError("Desk thread not configured. Run `/desk setup` in Discord first.")
        post_channel_id = int(cfg.desk_channel_id)
        post_thread_id = int(cfg.desk_thread_id)

    def _enqueue() -> dict[str, object]:
        with session_scope() as session:
            job = enqueue_agent_ticker_job(
                session,
                guild_id=int(guild_id),
                ticker=ticker,
                days=int(days_val),
                post_to_outbox=bool(post_to_outbox),
                post_channel_id=post_channel_id,
                post_thread_id=post_thread_id,
                dedupe_key=None,
            )
            return {"job_id": int(job.id), "dedupe_key": job.dedupe_key}

    return _with_db_retry(_enqueue)


def _mcp_tool_get_recent_context_bundle(params: dict[str, object]) -> dict[str, object]:
    def _listish(value: object) -> list[str]:
        if isinstance(value, list):
            return [str(x).strip() for x in value if str(x).strip()]
        if isinstance(value, str):
            return [tok.strip() for tok in value.split(",") if tok.strip()]
        return []

    guild_id = params.get("guild_id")
    guild_val = int(guild_id) if guild_id is not None and str(guild_id).isdigit() else None
    _, eff_cfg = _agent_settings_for_guild(guild_val)
    lookback = params.get("lookback_hours")
    lookback_h = (
        int(lookback)
        if lookback is not None and (isinstance(lookback, int) or str(lookback).isdigit())
        else int(eff_cfg.get("context_default_lookback_hours") or 24)
    )
    max_items = params.get("max_items_per_section")
    max_items_val = (
        int(max_items)
        if max_items is not None and (isinstance(max_items, int) or str(max_items).isdigit())
        else int(getattr(settings, "agent_context_max_items_per_section", 25) or 25)
    )
    tickers = [t.upper() for t in _listish(params.get("tickers"))]
    domains = _listish(params.get("domains"))
    include_sections = _listish(params.get("include_sections")) or None
    if not include_sections:
        raw_sections = eff_cfg.get("default_include_sections")
        if isinstance(raw_sections, list):
            include_sections = [str(s).strip() for s in raw_sections if str(s).strip()] or None
    source_ids_raw = params.get("source_ids")
    source_ids: list[int] = []
    if isinstance(source_ids_raw, list):
        source_ids = [int(x) for x in source_ids_raw if str(x).isdigit()]
    include_full_text = bool(params.get("include_full_text")) if params.get("include_full_text") is not None else False

    session = _session_read()
    try:
        bundle = build_recent_context_bundle(
            session,
            guild_id=guild_val,
            lookback_hours=int(lookback_h),
            tickers=tickers or None,
            domains=domains or None,
            source_ids=source_ids or None,
            include_sections=include_sections,
            max_items_per_section=int(max_items_val),
            include_full_text=bool(include_full_text),
        )
        return bundle
    finally:
        session.close()


def _mcp_tool_agent_enqueue_context_compression(params: dict[str, object]) -> dict[str, object]:
    guild_id = params.get("guild_id")
    if guild_id is None or not str(guild_id).isdigit():
        raise ValueError("guild_id is required.")
    _, eff_cfg = _agent_settings_for_guild(int(guild_id))
    mode = str(params.get("mode") or "custom").strip().lower() or "custom"
    lookback = params.get("lookback_hours")
    lookback_h = (
        int(lookback)
        if lookback is not None and (isinstance(lookback, int) or str(lookback).isdigit())
        else int(eff_cfg.get("context_default_lookback_hours") or 24)
    )
    focus_query = str(params.get("focus_query") or "").strip() or None
    tickers = params.get("tickers")
    tickers_val = [str(t).strip().upper() for t in tickers if str(t).strip()] if isinstance(tickers, list) else None
    include_sections = params.get("include_sections")
    include_sections_val = [str(s).strip() for s in include_sections if str(s).strip()] if isinstance(include_sections, list) else None
    if not include_sections_val:
        eff_sections = eff_cfg.get("default_include_sections")
        if isinstance(eff_sections, list):
            include_sections_val = [str(s).strip() for s in eff_sections if str(s).strip()] or None
    post_to_outbox = (
        bool(params.get("post_to_outbox"))
        if params.get("post_to_outbox") is not None
        else bool(eff_cfg.get("default_post_to_outbox", False))
    )
    persist_note = bool(params.get("persist_note")) if params.get("persist_note") is not None else True
    title_override = str(params.get("title_override") or "").strip() or None
    client_request_id = str(params.get("client_request_id") or "").strip() or None
    dedupe_key = str(params.get("dedupe_key") or "").strip() or None

    post_channel_id = None
    post_thread_id = None
    if post_to_outbox:
        from thoth_terminal.discord.desk import get_desk_settings

        cfg = get_desk_settings(guild_id=int(guild_id))
        if not cfg.desk_channel_id:
            raise ValueError("Desk channel not configured. Run `/desk setup` in Discord first.")
        post_channel_id = int(cfg.desk_channel_id)
        post_thread_id = int(cfg.desk_thread_id) if cfg.desk_thread_id else None

    def _enqueue() -> dict[str, object]:
        from thoth_terminal.discord import agent_jobs

        job = agent_jobs.enqueue_context_compression(
            guild_id=int(guild_id),
            mode=mode,
            lookback_hours=lookback_h,
            focus_query=focus_query,
            tickers=tickers_val,
            include_sections=include_sections_val,
            post_to_outbox=bool(post_to_outbox),
            post_channel_id=post_channel_id,
            post_thread_id=post_thread_id,
            persist_note=bool(persist_note),
            title_override=title_override,
            client_request_id=client_request_id,
            dedupe_key=dedupe_key,
        )
        return {"job_id": int(job.id), "dedupe_key": job.dedupe_key}

    return _with_db_retry(_enqueue)


def _mcp_tool_integration_google_status(params: dict[str, object]) -> dict[str, object]:
    from thoth_terminal.google_oauth import GoogleIntegrationError, status as google_status

    try:
        return {"ok": True, "status": google_status().as_dict()}
    except GoogleIntegrationError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _mcp_tool_integration_google_test(params: dict[str, object]) -> dict[str, object]:
    from thoth_terminal.google_oauth import (
        GoogleIntegrationError,
        drive_test as google_drive_test,
        gmail_test as google_gmail_test,
    )

    service = str(params.get("service") or "").strip().lower()
    if service not in {"gmail", "drive"}:
        raise ValueError("service must be gmail|drive")
    try:
        result = google_gmail_test() if service == "gmail" else google_drive_test()
        return {"ok": True, "service": service, "result": result}
    except GoogleIntegrationError as exc:
        return {"ok": False, "service": service, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "service": service, "error": f"{type(exc).__name__}: {exc}"}


def _mcp_tool_discord_agent_settings_get(params: dict[str, object]) -> dict[str, object]:
    guild_id = params.get("guild_id")
    if guild_id is None or not str(guild_id).isdigit():
        raise ValueError("guild_id is required.")
    raw, effective = _agent_settings_for_guild(int(guild_id))
    return {
        "guild_id": int(guild_id),
        "raw": raw,
        "effective": effective,
        "defaults": _agent_settings_defaults(),
    }


def _mcp_tool_discord_agent_settings_update(params: dict[str, object]) -> dict[str, object]:
    guild_id = params.get("guild_id")
    patch = params.get("patch")
    if guild_id is None or not str(guild_id).isdigit():
        raise ValueError("guild_id is required.")
    if not isinstance(patch, dict):
        raise ValueError("patch must be an object.")
    updated_by_user_id = params.get("updated_by_user_id")
    user_id = int(updated_by_user_id) if updated_by_user_id is not None and str(updated_by_user_id).isdigit() else 0
    normalized_patch = _normalize_agent_settings_patch(dict(patch))

    from thoth_terminal.discord.desk import update_agent_settings

    def _update() -> dict[str, object]:
        raw = update_agent_settings(
            guild_id=int(guild_id),
            patch=normalized_patch,
            user_id=int(user_id),
        )
        _invalidate_agent_settings_cache(int(guild_id))
        _, effective = _agent_settings_for_guild(int(guild_id))
        return {
            "guild_id": int(guild_id),
            "raw": raw,
            "effective": effective,
            "applied_patch": normalized_patch,
        }

    return _with_db_retry(_update)
