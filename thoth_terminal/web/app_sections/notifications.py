from __future__ import annotations

from thoth_terminal.web.app_sections._shared import bind_app_context


bind_app_context(globals())


@app.get("/notifications", response_class=HTMLResponse)
def notifications(request: Request, db: Session = Depends(get_session)) -> HTMLResponse:
    rules = db.query(NotificationRule).order_by(NotificationRule.id.asc()).all()
    sources = list_sources(db)
    filter_options = _cached_note_filter_options(db)
    entities = db.query(Entity).order_by(Entity.canonical_name.asc()).limit(200).all()
    return _template_response(
        "notifications.html",
        {
            "request": request,
            "rules": rules,
            "sources": sources,
            "domains": filter_options["domains"],
            "tags": filter_options["tags"],
            "entities": entities,
        },
    )


def _alert_rule_filters(
    rule: NotificationRule,
    *,
    source_names: dict[int, str],
    entity_names: dict[int, str],
) -> list[str]:
    filters: list[str] = []
    if rule.source_ids:
        names = [source_names.get(int(source_id), f"source {source_id}") for source_id in rule.source_ids]
        filters.append("sources: " + ", ".join(names))
    if rule.domain_slugs:
        filters.append("domains: " + ", ".join(str(item) for item in rule.domain_slugs))
    if rule.tag_slugs:
        filters.append("tags: " + ", ".join(str(item) for item in rule.tag_slugs))
    if rule.entity_ids:
        names = [entity_names.get(int(entity_id), f"entity {entity_id}") for entity_id in rule.entity_ids]
        filters.append("entities: " + ", ".join(names))
    if rule.keyword_regex:
        filters.append(f"regex: {rule.keyword_regex}")
    return filters


def _alert_discovery_context(db: Session, *, q: str = "", status: str = "") -> dict[str, object]:
    query = str(q or "").strip().lower()
    status_filter = str(status or "").strip().lower()
    now = utcnow()

    notification_rules = (
        db.query(NotificationRule)
        .options(
            load_only(
                NotificationRule.id,
                NotificationRule.name,
                NotificationRule.enabled,
                NotificationRule.action,
                NotificationRule.source_ids,
                NotificationRule.domain_slugs,
                NotificationRule.entity_ids,
                NotificationRule.tag_slugs,
                NotificationRule.keyword_regex,
                NotificationRule.quiet_start,
                NotificationRule.quiet_end,
                NotificationRule.quiet_tz,
                NotificationRule.updated_at,
            )
        )
        .order_by(NotificationRule.enabled.desc(), NotificationRule.updated_at.desc(), NotificationRule.id.desc())
        .all()
    )
    supply_alerts = (
        db.query(SupplyChainAlertRule)
        .options(
            load_only(
                SupplyChainAlertRule.id,
                SupplyChainAlertRule.chain_id,
                SupplyChainAlertRule.scope_type,
                SupplyChainAlertRule.scope_key,
                SupplyChainAlertRule.name,
                SupplyChainAlertRule.metric_kind,
                SupplyChainAlertRule.operator,
                SupplyChainAlertRule.threshold_value,
                SupplyChainAlertRule.window_days,
                SupplyChainAlertRule.status,
                SupplyChainAlertRule.last_triggered_at,
                SupplyChainAlertRule.updated_at,
            )
        )
        .order_by(SupplyChainAlertRule.status.asc(), SupplyChainAlertRule.updated_at.desc(), SupplyChainAlertRule.id.desc())
        .all()
    )

    source_ids = sorted({int(source_id) for rule in notification_rules for source_id in (rule.source_ids or [])})
    entity_ids = sorted({int(entity_id) for rule in notification_rules for entity_id in (rule.entity_ids or [])})
    chain_ids = sorted({int(alert.chain_id) for alert in supply_alerts})

    source_names = {int(row.id): row.name for row in db.query(Source.id, Source.name).filter(Source.id.in_(source_ids)).all()} if source_ids else {}
    entity_names = {int(row.id): row.canonical_name for row in db.query(Entity.id, Entity.canonical_name).filter(Entity.id.in_(entity_ids)).all()} if entity_ids else {}
    chains = {int(row.id): row for row in db.query(SupplyChain.id, SupplyChain.title, SupplyChain.slug).filter(SupplyChain.id.in_(chain_ids)).all()} if chain_ids else {}

    recent_runs = (
        db.query(IngestionRun)
        .options(load_only(IngestionRun.id, IngestionRun.source_id, IngestionRun.finished_at, IngestionRun.extra))
        .filter(IngestionRun.finished_at.isnot(None))
        .order_by(IngestionRun.finished_at.desc(), IngestionRun.id.desc())
        .limit(120)
        .all()
    )
    hit_counts: dict[int, int] = {}
    last_hits: dict[int, datetime] = {}
    recent_hits: list[dict[str, object]] = []
    for run in recent_runs:
        notify = run.extra.get("notify") if isinstance(run.extra, dict) else {}
        if not isinstance(notify, dict):
            continue
        for hit in notify.get("rules") or []:
            if not isinstance(hit, dict) or hit.get("id") is None:
                continue
            rule_id = int(hit.get("id"))
            count = int(hit.get("count") or 0)
            hit_counts[rule_id] = hit_counts.get(rule_id, 0) + count
            if run.finished_at and (rule_id not in last_hits or run.finished_at > last_hits[rule_id]):
                last_hits[rule_id] = run.finished_at
            if len(recent_hits) < 20:
                recent_hits.append(
                    {
                        "rule_id": rule_id,
                        "count": count,
                        "quiet": bool(hit.get("quiet")),
                        "run_id": int(run.id),
                        "source_id": int(run.source_id),
                        "finished_at": run.finished_at,
                    }
                )

    notification_rows: list[dict[str, object]] = []
    for rule in notification_rules:
        filters = _alert_rule_filters(rule, source_names=source_names, entity_names=entity_names)
        hay = " ".join([rule.name, rule.action, rule.keyword_regex or "", " ".join(filters)]).lower()
        state = "active" if rule.enabled else "disabled"
        if query and query not in hay:
            continue
        if status_filter and status_filter not in {"all", state}:
            continue
        notification_rows.append(
            {
                "id": int(rule.id),
                "name": rule.name,
                "kind": "notification",
                "state": state,
                "action": rule.action,
                "filters": filters,
                "quiet": " - ".join(part for part in [rule.quiet_start, rule.quiet_end, rule.quiet_tz] if part),
                "recent_hits": hit_counts.get(int(rule.id), 0),
                "last_hit_at": last_hits.get(int(rule.id)),
                "updated_at": rule.updated_at,
                "href": "/notifications",
            }
        )

    supply_rows: list[dict[str, object]] = []
    for alert in supply_alerts:
        chain = chains.get(int(alert.chain_id))
        hay = " ".join(
            [
                alert.name,
                alert.metric_kind,
                alert.operator,
                alert.scope_type,
                alert.scope_key,
                alert.status,
                chain.title if chain else "",
                chain.slug if chain else "",
            ]
        ).lower()
        if query and query not in hay:
            continue
        if status_filter and status_filter not in {"all", alert.status.lower()}:
            continue
        supply_rows.append(
            {
                "id": int(alert.id),
                "name": alert.name,
                "kind": "supply_chain",
                "state": alert.status,
                "chain_id": int(alert.chain_id),
                "chain_title": chain.title if chain else f"chain {alert.chain_id}",
                "chain_href": f"/visualizations/supply-chains/{int(alert.chain_id)}/command-center",
                "scope": f"{alert.scope_type}:{alert.scope_key}",
                "metric": alert.metric_kind,
                "threshold": f"{alert.operator} {alert.threshold_value:g}",
                "window_days": alert.window_days,
                "last_triggered_at": alert.last_triggered_at,
                "updated_at": alert.updated_at,
                "href": f"/visualizations/supply-chains/{int(alert.chain_id)}/command-center",
            }
        )

    return {
        "q": q,
        "status": status,
        "generated_at": now,
        "counts": {
            "notification_total": len(notification_rules),
            "notification_active": sum(1 for rule in notification_rules if rule.enabled),
            "supply_total": len(supply_alerts),
            "supply_active": sum(1 for alert in supply_alerts if alert.status == "active"),
            "recent_hits": sum(hit_counts.values()),
        },
        "notification_rows": notification_rows,
        "supply_rows": supply_rows,
        "recent_hits": recent_hits,
        "source_names": source_names,
    }


@app.get("/alerts", response_class=HTMLResponse)
def alerts_discovery(
    request: Request,
    q: str = "",
    status: str = "",
    db: Session = Depends(get_session),
) -> HTMLResponse:
    return _template_response(
        "alerts.html",
        {
            "request": request,
            "title": "Alert Discovery",
            **_alert_discovery_context(db, q=q, status=status),
        },
    )


def _notifications_inbox_rows(db: Session) -> list[dict[str, object]]:
    runs = (
        db.query(IngestionRun)
        .options(
            load_only(
                IngestionRun.id,
                IngestionRun.source_id,
                IngestionRun.finished_at,
                IngestionRun.status,
                IngestionRun.items_new,
                IngestionRun.items_updated,
                IngestionRun.extra,
            )
        )
        .filter(IngestionRun.finished_at.isnot(None))
        .order_by(IngestionRun.id.desc())
        .limit(80)
        .all()
    )
    source_ids = sorted({int(run.source_id) for run in runs})
    rule_ids: set[int] = set()
    for run in runs:
        notify = run.extra.get("notify") if isinstance(run.extra, dict) else {}
        if not isinstance(notify, dict):
            continue
        for hit in notify.get("rules") or []:
            if isinstance(hit, dict) and hit.get("id") is not None:
                rule_ids.add(int(hit.get("id")))
    sources = {int(row.id): row.name for row in db.query(Source.id, Source.name).filter(Source.id.in_(source_ids)).all()} if source_ids else {}
    rules = {int(row.id): row.name for row in db.query(NotificationRule.id, NotificationRule.name).filter(NotificationRule.id.in_(sorted(rule_ids))).all()} if rule_ids else {}
    rows: list[dict[str, object]] = []
    for run in runs:
        notify = {}
        if isinstance(run.extra, dict):
            notify = run.extra.get("notify") or {}
        rule_hits = []
        for hit in notify.get("rules") or []:
            rid = hit.get("id")
            if rid is None:
                continue
            rule = rules.get(int(rid))
            rule_hits.append(
                {
                    "id": rid,
                    "name": rule if rule else f"rule {rid}",
                    "count": hit.get("count") or 0,
                    "quiet": bool(hit.get("quiet")),
                }
            )
        rows.append(
            {
                "run": run,
                "source": sources.get(run.source_id, f"source {run.source_id}"),
                "notify": notify,
                "rule_hits": rule_hits,
            }
        )
    return rows


@app.get("/notifications/inbox", response_class=HTMLResponse)
def notifications_inbox(request: Request, db: Session = Depends(get_session)) -> HTMLResponse:
    rows = _notifications_inbox_rows(db)
    return _template_response(
        "notifications_inbox.html",
        {"request": request, "rows": rows},
    )


@app.post("/notifications/new")
def notifications_new(
    name: str = Form(...),
    action: str = Form("toast"),
    enabled: bool = Form(True),
    sources: str | None = Form(None),
    domains: str | None = Form(None),
    tags: str | None = Form(None),
    entities: str | None = Form(None),
    keyword_regex: str | None = Form(None),
    quiet_start: str | None = Form(None),
    quiet_end: str | None = Form(None),
    quiet_tz: str | None = Form(None),
) -> RedirectResponse:
    with session_scope() as session:
        source_ids: list[int] = []
        for tok in _split_csv(sources):
            if tok.isdigit():
                source_ids.append(int(tok))
            else:
                s = session.query(Source).filter(Source.name.ilike(tok)).first()
                if s:
                    source_ids.append(int(s.id))

        entity_ids: list[int] = []
        for tok in _split_csv(entities):
            if tok.isdigit():
                entity_ids.append(int(tok))
            else:
                ent = session.query(Entity).filter(Entity.canonical_name.ilike(tok)).first()
                if ent:
                    entity_ids.append(int(ent.id))

        now = utcnow()
        rule = NotificationRule(
            name=name.strip(),
            enabled=bool(enabled),
            action=(action or "toast").strip(),
            source_ids=source_ids,
            domain_slugs=_split_csv(domains),
            tag_slugs=_split_csv(tags),
            entity_ids=entity_ids,
            keyword_regex=keyword_regex.strip() if keyword_regex else None,
            quiet_start=quiet_start.strip() if quiet_start else None,
            quiet_end=quiet_end.strip() if quiet_end else None,
            quiet_tz=quiet_tz.strip() if quiet_tz else None,
            created_at=now,
            updated_at=now,
        )
        session.add(rule)
    return RedirectResponse(url="/notifications", status_code=303)


@app.post("/notifications/{rule_id}/update")
def notifications_update(
    rule_id: int,
    quiet_start: str | None = Form(None),
    quiet_end: str | None = Form(None),
    quiet_tz: str | None = Form(None),
) -> RedirectResponse:
    with session_scope() as session:
        rule = session.query(NotificationRule).filter_by(id=int(rule_id)).first()
        if rule:
            rule.quiet_start = quiet_start.strip() if quiet_start else None
            rule.quiet_end = quiet_end.strip() if quiet_end else None
            rule.quiet_tz = quiet_tz.strip() if quiet_tz else None
            rule.updated_at = utcnow()
    return RedirectResponse(url="/notifications", status_code=303)


@app.post("/notifications/{rule_id}/toggle")
def notifications_toggle(rule_id: int) -> RedirectResponse:
    with session_scope() as session:
        rule = session.query(NotificationRule).filter_by(id=int(rule_id)).first()
        if rule:
            rule.enabled = not bool(rule.enabled)
            rule.updated_at = utcnow()
    return RedirectResponse(url="/notifications", status_code=303)


@app.post("/notifications/{rule_id}/delete")
def notifications_delete(rule_id: int) -> RedirectResponse:
    with session_scope() as session:
        rule = session.query(NotificationRule).filter_by(id=int(rule_id)).first()
        if rule:
            session.delete(rule)
    return RedirectResponse(url="/notifications", status_code=303)
