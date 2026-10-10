from __future__ import annotations

from thoth_terminal.web.app_sections._shared import bind_app_context


bind_app_context(globals())


@app.get("/theses", response_class=HTMLResponse)
def theses(
    request: Request,
    q: str | None = None,
    status: str | None = None,
    stale: int | None = None,
    review: int | None = None,
    include_hidden: int | None = None,
    db: Session = Depends(get_session),
) -> HTMLResponse:
    include_hidden_theses = bool(status) or _request_includes_hidden_theses(include_hidden)
    row_query = db.query(Thesis).options(
        load_only(
            Thesis.id,
            Thesis.title,
            Thesis.statement,
            Thesis.status,
            Thesis.importance,
            Thesis.stale_after_days,
            Thesis.created_at,
            Thesis.updated_at,
        ),
        selectinload(Thesis.entities).load_only(Entity.id, Entity.canonical_name, Entity.kind),
    )
    if status:
        row_query = row_query.filter(Thesis.status == status)
    else:
        row_query = _visible_thesis_query(row_query, include_hidden=include_hidden_theses)
    if q:
        q_like = f"%{q}%"
        row_query = row_query.outerjoin(Thesis.entities).filter(
            or_(Thesis.title.ilike(q_like), Thesis.statement.ilike(q_like), Entity.canonical_name.ilike(q_like))
        ).distinct()
    rows = (
        row_query.order_by(Thesis.updated_at.desc(), Thesis.id.desc()).all()
    )
    thesis_ids = [int(row.id) for row in rows]
    evidence_stats = _thesis_index_evidence_stats(db, thesis_ids)
    entity_chip_map = _thesis_index_entity_chip_map(db, rows)

    items: list[dict[str, object]] = []
    now = utcnow()
    for th in rows:
        stats = evidence_stats.get(int(th.id), {})
        last_ev = stats.get("last_evidence_at")
        ev_count = int(stats.get("evidence_count") or 0)
        contradict_count = int(stats.get("contradict_count") or 0)

        freshness = _thesis_freshness(th, last_evidence_at=last_ev, now=now)
        is_stale = bool(freshness.get("is_stale"))
        days_since = freshness.get("days_since_basis")
        overdue_days = freshness.get("overdue_days")

        if stale and not is_stale:
            continue

        evidence_need = compute_evidence_need(
            th,
            now=now,
            evidence_count=ev_count,
            last_evidence_at=last_ev,
            contradict_count=contradict_count,
        )
        if review and int(evidence_need.get("score") or 0) < 45:
            continue

        items.append(
            {
                "thesis": th,
                "evidence_count": ev_count,
                "last_evidence_at": last_ev,
                "is_stale": is_stale,
                "days_since_evidence": days_since,
                "overdue_days": overdue_days,
                "days_until_stale": freshness.get("days_until_stale"),
                "freshness": freshness,
                "evidence_need": evidence_need,
                "entity_chips": entity_chip_map.get(int(th.id), []),
            }
        )

    if review:
        items.sort(
            key=lambda it: (
                int((it.get("evidence_need") or {}).get("score") or 0),
                (it.get("overdue_days") or 0),
                int(it["thesis"].importance or 0),
                it["thesis"].updated_at,
            ),
            reverse=True,
        )

    return _template_response(
        "theses.html",
        {
            "request": request,
            "items": items,
            "filters": {
                "q": q or "",
                "status": status,
                "stale": stale,
                "review": review,
                "include_hidden": include_hidden if include_hidden else None,
                "hidden_statuses": THESIS_ARCHIVE_STATUSES,
            },
            "statuses": THESIS_STATUSES,
            "archive_statuses": THESIS_ARCHIVE_STATUSES,
        },
    )


@app.get("/theses/new", response_class=HTMLResponse)
def thesis_new(request: Request, from_doc: int | None = None, db: Session = Depends(get_session)) -> HTMLResponse:
    domains = db.query(Domain).order_by(Domain.name.asc()).all()
    tags = db.query(Tag).order_by(Tag.name.asc()).all()
    event_types = db.query(EventType).order_by(EventType.name.asc()).all()
    entities = db.query(Entity).order_by(Entity.canonical_name.asc()).limit(200).all()

    prefill: dict[str, object] = {}
    if from_doc:
        doc = db.query(Document).filter_by(id=int(from_doc)).first()
        if doc:
            prefill["title"] = (doc.title or f"doc {from_doc}")[:140]
            prefill["statement"] = f"Thesis seeded from [[doc:{from_doc}]]\\n\\n{doc.title or doc.url or ''}"

    return _template_response(
        "thesis_new.html",
        {
            "request": request,
            "error": None,
            "prefill": prefill,
            "from_doc": from_doc,
            "domains": domains,
            "tags": tags,
            "event_types": event_types,
            "entities": entities,
            "dupe_hits": [],
            "dupe_warning": False,
        },
    )


@app.post("/theses/new")
def thesis_create(
    request: Request,
    title: str = Form(...),
    statement: str = Form(...),
    status: str = Form("watching"),
    importance: int = Form(3),
    stale_after_days: str | None = Form("90"),
    entities: str = Form(...),
    domains: str | None = Form(None),
    tags: str | None = Form(None),
    event_types: str | None = Form(None),
    metric_subject: str | None = Form(None),
    metric_topic: str | None = Form(None),
    metric_name: str | None = Form(None),
    metric_unit: str | None = Form(None),
    metric_value_type: str | None = Form(None),
    metric_key_raw: str | None = Form(None),
    metric_use_raw: str | None = Form(None),
    from_doc: int | None = Form(None),
    force_create: str | None = Form(None),
    db: Session = Depends(get_session),
):
    try:
        stale_val = None
        if stale_after_days and stale_after_days.strip():
            stale_val = int(stale_after_days.strip())

        entity_tokens = _split_csv(entities)
        dupe_hits = _find_similar_theses(
            db,
            title=title,
            statement=statement,
            entity_tokens=entity_tokens,
            limit=6,
        )
        if dupe_hits and not force_create:
            domains_rows = db.query(Domain).order_by(Domain.name.asc()).all()
            tags_rows = db.query(Tag).order_by(Tag.name.asc()).all()
            event_rows = db.query(EventType).order_by(EventType.name.asc()).all()
            entities_rows = db.query(Entity).order_by(Entity.canonical_name.asc()).limit(200).all()

            prefill = {
                "title": title,
                "statement": statement,
                "status": status,
                "importance": importance,
                "stale_after_days": stale_after_days,
                "entities": entities,
                "domains": domains or "",
                "tags": tags or "",
                "event_types": event_types or "",
                "metric_subject": metric_subject or "",
                "metric_topic": metric_topic or "",
                "metric_name": metric_name or "",
                "metric_unit": metric_unit or "",
                "metric_key_raw": metric_key_raw or "",
            }
            return _template_response(
                "thesis_new.html",
                {
                    "request": request,
                    "error": None,
                    "prefill": prefill,
                    "from_doc": from_doc,
                    "domains": domains_rows,
                    "tags": tags_rows,
                    "event_types": event_rows,
                    "entities": entities_rows,
                    "dupe_hits": dupe_hits,
                    "dupe_warning": True,
                },
            )

        with session_scope() as session:
            th = create_thesis(
                session,
                title=title,
                statement=statement,
                status=status,
                importance=int(importance),
                stale_after_days=stale_val,
                entity_tokens=entity_tokens,
                domain_slugs=_split_csv(domains)[:2],
                tag_slugs=_split_csv(tags),
                event_type_slugs=_split_csv(event_types),
            )
            session.flush()

            subj = (metric_subject or "").strip()
            topic = (metric_topic or "").strip()
            mname = (metric_name or "").strip()
            unit = (metric_unit or "").strip()
            use_raw = bool(metric_use_raw) and (metric_key_raw or "").strip()
            raw_key = (metric_key_raw or "").strip()
            if (subj or topic or mname or use_raw) and (mname or use_raw):
                if not subj:
                    if th.entities:
                        subj = th.entities[0].canonical_name
                key = raw_key if use_raw else _build_metric_key(subj, topic, mname)
                vtype = _metric_value_type(unit or None, (metric_value_type or "").strip() or None)
                prop = upsert_proposition(
                    session,
                    key=key,
                    kind="series",
                    unit=unit or None,
                    value_type=vtype,
                )
                _set_primary_prop(
                    session,
                    th,
                    prop,
                    {
                        "metric_subject": subj,
                        "metric_topic": topic,
                        "metric_name": mname,
                        "metric_unit": unit,
                        "metric_value_type": vtype,
                        "metric_key": key,
                    },
                )

            if from_doc:
                try:
                    add_thesis_evidence(session, thesis=th, document_id=int(from_doc), polarity="support")
                except Exception:
                    pass

        return RedirectResponse(url=f"/theses/{th.id}", status_code=303)
    except Exception as e:
        domains_rows = db.query(Domain).order_by(Domain.name.asc()).all()
        tags_rows = db.query(Tag).order_by(Tag.name.asc()).all()
        event_rows = db.query(EventType).order_by(EventType.name.asc()).all()
        entities_rows = db.query(Entity).order_by(Entity.canonical_name.asc()).limit(200).all()

        prefill = {
            "title": title,
            "statement": statement,
            "status": status,
            "importance": importance,
            "stale_after_days": stale_after_days,
            "entities": entities,
            "domains": domains or "",
            "tags": tags or "",
            "event_types": event_types or "",
            "metric_subject": metric_subject or "",
            "metric_topic": metric_topic or "",
            "metric_name": metric_name or "",
            "metric_unit": metric_unit or "",
            "metric_key_raw": metric_key_raw or "",
        }
        return _template_response(
            "thesis_new.html",
            {
                "request": request,
                "error": str(e),
                "prefill": prefill,
                "domains": domains_rows,
                "tags": tags_rows,
                "event_types": event_rows,
                "entities": entities_rows,
                "dupe_hits": [],
                "dupe_warning": False,
            },
        )


def _load_thesis_base(db: Session, thesis_id: int) -> Thesis | None:
    return (
        db.query(Thesis)
        .options(
            joinedload(Thesis.entities),
            joinedload(Thesis.domains),
            joinedload(Thesis.tags),
            joinedload(Thesis.event_types),
        )
        .filter(Thesis.id == int(thesis_id))
        .first()
    )


def _thesis_index_evidence_stats(db: Session, thesis_ids: list[int]) -> dict[int, dict[str, object]]:
    if not thesis_ids:
        return {}
    rows = (
        db.query(
            ThesisEvidence.thesis_id.label("thesis_id"),
            func.count(ThesisEvidence.id).label("evidence_count"),
            func.max(ThesisEvidence.created_at).label("last_evidence_at"),
            func.sum(case((ThesisEvidence.polarity == "contradict", 1), else_=0)).label("contradict_count"),
        )
        .filter(ThesisEvidence.thesis_id.in_(thesis_ids))
        .group_by(ThesisEvidence.thesis_id)
        .all()
    )
    return {
        int(row.thesis_id): {
            "evidence_count": int(row.evidence_count or 0),
            "last_evidence_at": row.last_evidence_at,
            "contradict_count": int(row.contradict_count or 0),
        }
        for row in rows
    }


def _thesis_index_key(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _thesis_index_entity_chip_map(db: Session, theses: list[Thesis]) -> dict[int, list[dict[str, object]]]:
    entity_ids: set[int] = set()
    entity_name_keys: set[str] = set()
    thesis_entities: dict[int, list[Entity]] = {}
    for thesis in theses:
        entities = [entity for entity in (thesis.entities or []) if str(entity.canonical_name or "").strip()]
        thesis_entities[int(thesis.id)] = entities
        for entity in entities:
            if entity.id is not None:
                entity_ids.add(int(entity.id))
            key = _thesis_index_key(entity.canonical_name)
            if key:
                entity_name_keys.add(key)
    href_by_entity_id: dict[int, str] = {}
    href_by_name_key: dict[str, str] = {}
    if entity_ids or entity_name_keys:
        subject_filters = []
        if entity_ids:
            subject_filters.append(ResearchSubject.primary_entity_id.in_(entity_ids))
        if entity_name_keys:
            subject_filters.append(func.lower(ResearchSubject.slug).in_(entity_name_keys))
            subject_filters.append(func.lower(ResearchSubject.subject_key).in_(entity_name_keys))
            subject_filters.append(func.lower(ResearchSubject.display_name).in_(entity_name_keys))
            subject_filters.append(func.lower(func.coalesce(ResearchSubject.legal_name, "")).in_(entity_name_keys))
        for row in (
            db.query(
                ResearchSubject.slug.label("slug"),
                ResearchSubject.primary_entity_id.label("primary_entity_id"),
                ResearchSubject.display_name.label("display_name"),
                ResearchSubject.legal_name.label("legal_name"),
                ResearchSubject.subject_key.label("subject_key"),
            )
            .filter(or_(*subject_filters))
            .order_by(ResearchSubject.updated_at.desc(), ResearchSubject.id.desc())
            .all()
        ):
            href = f"/reports/{row.slug}"
            if row.primary_entity_id is not None:
                href_by_entity_id.setdefault(int(row.primary_entity_id), href)
            for value in (row.slug, row.display_name, row.legal_name, row.subject_key):
                key = _thesis_index_key(value)
                if key:
                    href_by_name_key.setdefault(key, href)
        if entity_name_keys:
            for row in (
                db.query(ResearchSubjectAlias.alias.label("alias"), ResearchSubject.slug.label("slug"))
                .join(ResearchSubject, ResearchSubject.id == ResearchSubjectAlias.subject_id)
                .filter(func.lower(ResearchSubjectAlias.alias).in_(entity_name_keys))
                .order_by(ResearchSubjectAlias.id.asc())
                .all()
            ):
                key = _thesis_index_key(row.alias)
                if key:
                    href_by_name_key.setdefault(key, f"/reports/{row.slug}")
            for row in (
                db.query(
                    Company.slug.label("slug"),
                    Company.name.label("name"),
                    Company.ticker.label("ticker"),
                )
                .filter(
                    or_(
                        func.lower(Company.slug).in_(entity_name_keys),
                        func.lower(Company.name).in_(entity_name_keys),
                        func.lower(func.coalesce(Company.ticker, "")).in_(entity_name_keys),
                    )
                )
                .order_by(Company.id.asc())
                .all()
            ):
                href = f"/reports/{row.slug}"
                for value in (row.slug, row.name, row.ticker):
                    key = _thesis_index_key(value)
                    if key:
                        href_by_name_key.setdefault(key, href)

    out: dict[int, list[dict[str, object]]] = {}
    for thesis_id, entities in thesis_entities.items():
        chips: list[dict[str, object]] = []
        for entity in entities:
            name = str(entity.canonical_name or "").strip()
            if not name:
                continue
            key = _thesis_index_key(name)
            href = href_by_entity_id.get(int(entity.id)) if entity.id is not None else None
            href = href or href_by_name_key.get(key)
            chips.append({"name": name, "kind": str(entity.kind or "entity").strip() or "entity", "href": href})
        out[thesis_id] = chips
    return out


def _thesis_freshness(thesis: Thesis, *, last_evidence_at: datetime | None, now: datetime) -> dict[str, object]:
    basis_at = last_evidence_at or thesis.created_at
    days_since_basis = (now - basis_at).days if basis_at is not None else None
    stale_after = int(thesis.stale_after_days) if thesis.stale_after_days is not None else None
    overdue_days = None
    days_until_stale = None
    is_stale = False
    if stale_after is not None and days_since_basis is not None:
        overdue_days = int(days_since_basis) - stale_after
        is_stale = overdue_days >= 0
        days_until_stale = 0 if is_stale else abs(overdue_days)
    return {
        "basis_at": basis_at,
        "days_since_basis": days_since_basis,
        "overdue_days": overdue_days,
        "days_until_stale": days_until_stale,
        "is_stale": is_stale,
    }


def _thesis_entity_chips(db: Session, thesis: Thesis) -> list[dict[str, object]]:
    chips: list[dict[str, object]] = []
    for entity in thesis.entities or []:
        name = str(entity.canonical_name or "").strip()
        if not name:
            continue
        href = None
        try:
            subject = resolve_report_subject(db, name)
            if subject.slug:
                href = f"/reports/{subject.slug}"
        except Exception:
            href = None
        chips.append(
            {
                "name": name,
                "kind": str(entity.kind or "entity").strip() or "entity",
                "href": href,
            }
        )
    return chips


@app.get("/theses/{thesis_id}", response_class=HTMLResponse)
def thesis_detail(
    request: Request,
    thesis_id: int,
    tab: str | None = None,
    edit: int | None = None,
    ev_page: int = 1,
    ev_q: str | None = None,
    ev_polarity: str | None = None,
    ev_source: str | None = None,
    db: Session = Depends(get_session),
) -> HTMLResponse:
    ensure_dataset_tables(db)
    _ensure_supply_chain_tables(db)
    thesis = _load_thesis_base(db, thesis_id)
    if not thesis:
        return HTMLResponse("Not found", status_code=404)

    active_tab = (tab or "overview").lower()
    if active_tab not in {"overview", "data", "evidence", "notes", "advanced"}:
        active_tab = "overview"
    edit_mode = bool(edit)

    extra = thesis.extra or {}
    metric_subject = str(extra.get("metric_subject") or "").strip()
    metric_topic = str(extra.get("metric_topic") or "").strip()
    metric_name = str(extra.get("metric_name") or "").strip()
    metric_unit = str(extra.get("metric_unit") or "").strip()
    metric_value_type = str(extra.get("metric_value_type") or "").strip() or None
    if not metric_subject and thesis.entities:
        metric_subject = thesis.entities[0].canonical_name

    primary_prop = _resolve_primary_prop(db, thesis)
    metric_key = str(extra.get("metric_key") or (primary_prop.key if primary_prop else "") or "")

    scenarios_enabled = bool(extra.get("scenarios_enabled"))
    scenario_labels = extra.get("scenario_labels") or []
    if isinstance(scenario_labels, str):
        scenario_labels = [p.strip() for p in scenario_labels.split(",") if p.strip()]

    metric_meta = {
        "subject": metric_subject,
        "topic": metric_topic,
        "name": metric_name,
        "unit": metric_unit,
        "value_type": metric_value_type or (primary_prop.value_type if primary_prop else "number"),
        "key": metric_key,
    }

    # Confidence + evidence summary
    now = utcnow()
    evidence_counts = {"support": 0, "neutral": 0, "contradict": 0}
    for pol, cnt in (
        db.query(ThesisEvidence.polarity, func.count(ThesisEvidence.id))
        .filter(ThesisEvidence.thesis_id == thesis.id)
        .group_by(ThesisEvidence.polarity)
        .all()
    ):
        evidence_counts[str(pol)] = int(cnt or 0)
    support = evidence_counts.get("support", 0)
    neutral = evidence_counts.get("neutral", 0)
    contradict = evidence_counts.get("contradict", 0)

    last_ev_at = (
        db.query(func.max(ThesisEvidence.created_at))
        .filter(ThesisEvidence.thesis_id == thesis.id)
        .scalar()
    )
    freshness = _thesis_freshness(thesis, last_evidence_at=last_ev_at, now=now)
    overdue_days = freshness.get("overdue_days")

    trend_counts: dict[str, int] = {}
    for (created_at,) in (
        db.query(ThesisEvidence.created_at)
        .filter(ThesisEvidence.thesis_id == thesis.id)
        .all()
    ):
        if created_at is None:
            continue
        period = created_at.strftime("%Y-%m")
        trend_counts[period] = trend_counts.get(period, 0) + 1
    trend_rows = sorted(trend_counts.items())[-12:]
    trend_labels = [row[0] for row in trend_rows]
    trend_values = [int(row[1] or 0) for row in trend_rows]

    score = 45 + support * 8 + neutral * 2 - contradict * 12
    if thesis.status == "confirmed":
        score += 10
    if thesis.status == "disproven":
        score -= 20
    if overdue_days and overdue_days > 0:
        score -= min(25, overdue_days)
    if support + neutral + contradict == 0:
        score = 20
    score = max(0, min(100, score))
    evidence_need = compute_evidence_need(
        thesis,
        now=now,
        evidence_count=support + neutral + contradict,
        last_evidence_at=last_ev_at,
        contradict_count=contradict,
    )
    confidence = {
        "score": int(score),
        "support": support,
        "neutral": neutral,
        "contradict": contradict,
        "overdue_days": overdue_days if overdue_days is not None else None,
        "days_until_stale": freshness.get("days_until_stale"),
        "days_since_basis": freshness.get("days_since_basis"),
        "basis_at": freshness.get("basis_at"),
        "is_stale": freshness.get("is_stale"),
        "trend_labels": trend_labels,
        "trend_values": trend_values,
    }
    autopilot_snapshot = build_thesis_autopilot_snapshot(db, thesis_id=int(thesis.id), lookback_hours=72, candidate_limit=5)
    thesis_action_rows = list_agent_actions(
        db,
        thesis_id=int(thesis.id),
        status=ACTION_STATUS_PENDING,
        limit=6,
    )
    thesis_actions = [serialize_agent_action(row) for row in thesis_action_rows]

    # Overview stats
    latest_obs = None
    if primary_prop and active_tab in ("overview", "data"):
        obs_q = db.query(Observation).filter(Observation.proposition_id == primary_prop.id)
        obs_all = obs_q.all()
        if obs_all:
            def _obs_key(o: Observation) -> tuple[int, int, datetime]:
                year = o.target_year or 0
                quarter = o.target_quarter or 0
                return (year, quarter, o.created_at)
            obs_all.sort(key=_obs_key, reverse=True)
            latest_obs = obs_all[0]

    # Data tab
    data_series: list[dict[str, object]] = []
    data_labels: list[str] = []
    data_rows: list[dict[str, object]] = []
    if active_tab == "data":
        if primary_prop:
            if scenarios_enabled:
                scenario_ids = extra.get("scenario_prop_ids") or []
                props: list[Proposition] = []
                if scenario_ids:
                    props = db.query(Proposition).filter(Proposition.id.in_(scenario_ids)).all()
                if not props and metric_key:
                    props = (
                        db.query(Proposition)
                        .join(ThesisProposition, ThesisProposition.proposition_id == Proposition.id)
                        .filter(ThesisProposition.thesis_id == thesis.id)
                        .filter(Proposition.key.like(f"{metric_key}:%"))
                        .all()
                    )
                if not props:
                    props = [primary_prop]
                props_by_id = {p.id: p for p in props}
                ordered_props = [props_by_id.get(pid) for pid in scenario_ids if pid in props_by_id] if scenario_ids else props
                for idx, prop in enumerate([p for p in ordered_props if p]):
                    label = scenario_labels[idx] if idx < len(scenario_labels) else (prop.key.split(":")[-1] if prop.key else f"scenario_{idx+1}")
                    obs = db.query(Observation).filter(Observation.proposition_id == prop.id).all()
                    data_series.append({"label": label, "prop": prop, "obs": obs})
            else:
                obs = db.query(Observation).filter(Observation.proposition_id == primary_prop.id).all()
                data_series.append({"label": "base", "prop": primary_prop, "obs": obs})
        if data_series:
            data_labels = [str(s.get("label") or "") for s in data_series]
            by_period: dict[str, list[str]] = {}

            def _period_label(o: Observation) -> str:
                if o.target_year and o.target_quarter:
                    return f"{o.target_year}Q{o.target_quarter}{o.estimate_flag or ''}"
                if o.target_year:
                    return f"{o.target_year}{o.estimate_flag or ''}"
                if o.as_of_date:
                    return o.as_of_date.strftime("%Y-%m-%d")
                return o.created_at.strftime("%Y-%m-%d")

            for idx, series in enumerate(data_series):
                obs_list: list[Observation] = series.get("obs") or []
                for obs in obs_list:
                    period = _period_label(obs)
                    val = ""
                    if obs.value_number is not None:
                        val = f"{obs.value_number:g}"
                    elif obs.value_text is not None:
                        val = obs.value_text
                    elif obs.value_bool is not None:
                        val = "true" if obs.value_bool else "false"
                    row = by_period.setdefault(period, [""] * len(data_series))
                    row[idx] = val

            for period, values in sorted(by_period.items()):
                data_rows.append({"period": period, "values": values})

    # Evidence tab (paged)
    evidence_items: list[ThesisEvidence] = []
    evidence_total = 0
    evidence_page = max(1, int(ev_page or 1))
    evidence_page_size = 25
    evidence_pages = 1
    evidence_sources: list[str] = []
    if active_tab == "evidence":
        q = db.query(ThesisEvidence).filter(ThesisEvidence.thesis_id == thesis.id)
        if ev_polarity:
            q = q.filter(ThesisEvidence.polarity == ev_polarity)
        if ev_source:
            q = q.join(Document).join(Source).filter(Source.name == ev_source)
        if ev_q:
            q = q.join(Document).filter(
                (Document.title.ilike(f"%{ev_q}%")) | (Document.url.ilike(f"%{ev_q}%"))
            )
        evidence_total = q.count()
        evidence_pages = max(1, (evidence_total + evidence_page_size - 1) // evidence_page_size)
        evidence_page = min(evidence_page, evidence_pages)
        evidence_items = (
            q.join(Document)
            .options(joinedload(ThesisEvidence.document).joinedload(Document.source))
            .order_by(ThesisEvidence.created_at.desc())
            .offset((evidence_page - 1) * evidence_page_size)
            .limit(evidence_page_size)
            .all()
        )
        evidence_sources = [
            row[0]
            for row in db.query(Source.name)
            .join(Document, Document.source_id == Source.id)
            .join(ThesisEvidence, ThesisEvidence.document_id == Document.id)
            .filter(ThesisEvidence.thesis_id == thesis.id)
            .distinct()
            .order_by(Source.name.asc())
            .all()
        ]

    # Overview evidence highlights
    evidence_highlights = []
    if active_tab == "overview":
        evidence_highlights = (
            db.query(ThesisEvidence)
            .join(Document)
            .options(joinedload(ThesisEvidence.document).joinedload(Document.source))
            .filter(ThesisEvidence.thesis_id == thesis.id)
            .order_by(ThesisEvidence.created_at.desc())
            .limit(3)
            .all()
        )

    # Notes tab
    note_links = []
    if active_tab == "notes":
        note_links = (
            db.query(NoteThesis)
            .options(joinedload(NoteThesis.note))
            .filter(NoteThesis.thesis_id == thesis.id)
            .all()
        )

    # Advanced tab
    prop_links = []
    prop_registry = []
    recent_docs = []
    if edit_mode or active_tab == "advanced":
        prop_links = (
            db.query(ThesisProposition)
            .options(joinedload(ThesisProposition.proposition).joinedload(Proposition.observations))
            .filter(ThesisProposition.thesis_id == thesis.id)
            .all()
        )
        prop_registry = db.query(Proposition).order_by(Proposition.key.asc()).limit(500).all()
    if edit_mode or active_tab == "evidence":
        recent_docs = list_documents(db, days=30, limit=80)
    linked_dataset_rows = (
        db.query(DatasetLink, Dataset)
        .join(Dataset, DatasetLink.dataset_id == Dataset.id)
        .filter(DatasetLink.object_type == "thesis", DatasetLink.object_id == int(thesis.id))
        .order_by(Dataset.updated_at.desc(), DatasetLink.id.desc())
        .all()
    )
    linked_supply_chain_rows = (
        db.query(SupplyChainLink, SupplyChain)
        .join(SupplyChain, SupplyChainLink.chain_id == SupplyChain.id)
        .filter(SupplyChainLink.object_type == "thesis", SupplyChainLink.object_id == int(thesis.id))
        .order_by(SupplyChain.updated_at.desc(), SupplyChainLink.id.desc())
        .all()
    )
    linked_stage_rows = [serialize_product_stage_entry(row) for row in list_product_stage_entries_linked_to_thesis(db, int(thesis.id))]
    linked_charts = db.query(Chart).filter_by(thesis_id=int(thesis.id)).order_by(Chart.updated_at.desc()).all()

    return _template_response(
        "thesis_detail.html",
        {
            "request": request,
            "thesis": thesis,
            "active_tab": active_tab,
            "edit_mode": edit_mode,
            "metric": metric_meta,
            "primary_prop": primary_prop,
            "latest_obs": latest_obs,
            "confidence": confidence,
            "evidence_need": evidence_need,
            "autopilot": autopilot_snapshot,
            "thesis_actions": thesis_actions,
            "entity_chips": _thesis_entity_chips(db, thesis),
            "scenarios_enabled": scenarios_enabled,
            "scenario_labels": scenario_labels,
            "data_series": data_series,
            "data_labels": data_labels,
            "data_rows": data_rows,
            "evidence_items": evidence_items,
            "evidence_total": evidence_total,
            "evidence_page": evidence_page,
            "evidence_pages": evidence_pages,
            "evidence_sources": evidence_sources,
            "evidence_q": ev_q or "",
            "evidence_polarity": ev_polarity or "",
            "evidence_source": ev_source or "",
            "evidence_highlights": evidence_highlights,
            "note_links": note_links,
            "prop_links": prop_links,
            "prop_registry": prop_registry,
            "recent_docs": recent_docs,
            "note_link_relations": NOTE_LINK_RELATIONS,
            "linked_dataset_rows": linked_dataset_rows,
            "linked_supply_chain_rows": linked_supply_chain_rows,
            "linked_stage_rows": linked_stage_rows,
            "linked_charts": linked_charts,
        },
    )


@app.post("/theses/{thesis_id}/delete")
def thesis_delete(thesis_id: int) -> RedirectResponse:
    def _delete() -> None:
        with session_scope() as session:
            session.query(ThesisEvidence).filter_by(thesis_id=int(thesis_id)).delete()
            session.query(ThesisProposition).filter_by(thesis_id=int(thesis_id)).delete()
            session.query(NoteThesis).filter_by(thesis_id=int(thesis_id)).delete()
            th = session.query(Thesis).filter_by(id=int(thesis_id)).first()
            if th:
                session.delete(th)

    _with_db_retry(_delete)
    return RedirectResponse(url="/theses", status_code=303)


@app.post("/theses/{thesis_id}/merge")
def thesis_merge(thesis_id: int, merge_id: int = Form(...)) -> RedirectResponse:
    if int(merge_id) == int(thesis_id):
        return RedirectResponse(url=f"/theses/{thesis_id}?tab=advanced", status_code=303)

    def _merge() -> None:
        with session_scope() as session:
            target = session.query(Thesis).filter_by(id=int(thesis_id)).first()
            other = session.query(Thesis).filter_by(id=int(merge_id)).first()
            if not target or not other:
                return

            target_doc_ids = {
                row[0]
                for row in session.query(ThesisEvidence.document_id).filter_by(thesis_id=target.id).all()
            }
            for ev in session.query(ThesisEvidence).filter_by(thesis_id=other.id).all():
                if ev.document_id in target_doc_ids:
                    session.delete(ev)
                else:
                    ev.thesis_id = target.id

            target_props = {
                row[0]
                for row in session.query(ThesisProposition.proposition_id).filter_by(thesis_id=target.id).all()
            }
            for link in session.query(ThesisProposition).filter_by(thesis_id=other.id).all():
                if link.proposition_id in target_props:
                    session.delete(link)
                else:
                    link.thesis_id = target.id

            target_notes = {
                row[0]
                for row in session.query(NoteThesis.note_id).filter_by(thesis_id=target.id).all()
            }
            for link in session.query(NoteThesis).filter_by(thesis_id=other.id).all():
                if link.note_id in target_notes:
                    session.delete(link)
                else:
                    link.thesis_id = target.id

            for entity in other.entities or []:
                if entity not in target.entities:
                    target.entities.append(entity)
            for dom in other.domains or []:
                if dom not in target.domains:
                    target.domains.append(dom)
            for tag in other.tags or []:
                if tag not in target.tags:
                    target.tags.append(tag)
            for evtype in other.event_types or []:
                if evtype not in target.event_types:
                    target.event_types.append(evtype)

            target.updated_at = utcnow()
            session.delete(other)

    _with_db_retry(_merge)
    return RedirectResponse(url=f"/theses/{thesis_id}?tab=advanced", status_code=303)


@app.post("/theses/{thesis_id}/edit")
def thesis_edit(
    thesis_id: int,
    title: str = Form(...),
    statement: str = Form(...),
    status: str = Form("watching"),
    importance: int = Form(3),
    stale_after_days: str | None = Form(None),
    entities: str | None = Form(None),
    domains: str | None = Form(None),
    tags: str | None = Form(None),
    event_types: str | None = Form(None),
) -> RedirectResponse:
    with session_scope() as session:
        th = session.query(Thesis).filter_by(id=int(thesis_id)).first()
        if not th:
            return RedirectResponse(url="/theses", status_code=303)

        th.title = title.strip()
        th.statement = statement.strip()
        th.status = status.strip()
        th.importance = int(importance)
        if stale_after_days is not None and str(stale_after_days).strip():
            th.stale_after_days = int(str(stale_after_days).strip())
        else:
            th.stale_after_days = None

        if entities is not None:
            th.entities = resolve_entities(session, _split_csv(entities))
        if domains is not None:
            th.domains = resolve_domains(session, _split_csv(domains)[:2])
        if tags is not None:
            th.tags = resolve_tags(session, _split_csv(tags))
        if event_types is not None:
            th.event_types = resolve_event_types(session, _split_csv(event_types))

        th.updated_at = utcnow()
    return RedirectResponse(url=f"/theses/{thesis_id}", status_code=303)


@app.post("/api/theses/{thesis_id}/autosave")
def thesis_autosave(
    thesis_id: int,
    title: str = Form(""),
    statement: str = Form(""),
    status: str = Form(""),
    importance: str | None = Form(None),
    stale_after_days: str | None = Form(None),
    entities: str | None = Form(None),
    domains: str | None = Form(None),
    tags: str | None = Form(None),
    event_types: str | None = Form(None),
) -> JSONResponse:
    with session_scope() as session:
        th = session.query(Thesis).filter_by(id=int(thesis_id)).first()
        if not th:
            return JSONResponse({"ok": False, "error": "Thesis not found"}, status_code=404)

        th.title = title.strip()
        th.statement = statement.strip()
        status_val = status.strip()
        if status_val:
            th.status = status_val
        if importance is not None and str(importance).isdigit():
            th.importance = int(importance)
        if stale_after_days is not None and str(stale_after_days).strip():
            th.stale_after_days = int(str(stale_after_days).strip())
        else:
            th.stale_after_days = None
        if entities is not None:
            th.entities = resolve_entities(session, _split_csv(entities))
        if domains is not None:
            th.domains = resolve_domains(session, _split_csv(domains)[:2])
        if tags is not None:
            th.tags = resolve_tags(session, _split_csv(tags))
        if event_types is not None:
            th.event_types = resolve_event_types(session, _split_csv(event_types))

        th.updated_at = utcnow()
        updated_at = th.updated_at.isoformat()

    return JSONResponse({"ok": True, "updated_at": updated_at})


@app.post("/theses/{thesis_id}/metric")
def thesis_metric_update(
    thesis_id: int,
    metric_subject: str = Form(""),
    metric_topic: str = Form(""),
    metric_name: str = Form(""),
    metric_unit: str | None = Form(None),
    metric_value_type: str | None = Form(None),
    metric_key_raw: str | None = Form(None),
    metric_use_raw: str | None = Form(None),
) -> RedirectResponse:
    subj = metric_subject.strip()
    topic = metric_topic.strip()
    mname = metric_name.strip()
    unit = (metric_unit or "").strip()
    raw_key = (metric_key_raw or "").strip()
    use_raw = bool(metric_use_raw) and raw_key
    if not mname and not use_raw:
        return RedirectResponse(url=f"/theses/{thesis_id}?edit=1", status_code=303)

    with session_scope() as session:
        th = session.query(Thesis).filter_by(id=int(thesis_id)).first()
        if not th:
            return RedirectResponse(url="/theses", status_code=303)
        if not subj and th.entities:
            subj = th.entities[0].canonical_name
        key = raw_key if use_raw else _build_metric_key(subj, topic, mname)
        vtype = _metric_value_type(unit or None, (metric_value_type or "").strip() or None)
        prop = upsert_proposition(
            session,
            key=key,
            kind="series",
            unit=unit or None,
            value_type=vtype,
        )
        _set_primary_prop(
            session,
            th,
            prop,
            {
                "metric_subject": subj,
                "metric_topic": topic,
                "metric_name": mname,
                "metric_unit": unit,
                "metric_value_type": vtype,
                "metric_key": key,
            },
        )
    return RedirectResponse(url=f"/theses/{thesis_id}?edit=1", status_code=303)


@app.post("/theses/{thesis_id}/scenarios")
def thesis_scenarios_update(
    thesis_id: int,
    scenarios_enabled: str | None = Form(None),
    scenario_labels: str | None = Form(None),
) -> RedirectResponse:
    enabled = bool(scenarios_enabled)
    labels = [p.strip() for p in (scenario_labels or "").split(",") if p.strip()]
    with session_scope() as session:
        th = session.query(Thesis).filter_by(id=int(thesis_id)).first()
        if not th:
            return RedirectResponse(url="/theses", status_code=303)
        extra = dict(th.extra or {})
        extra["scenarios_enabled"] = enabled
        if labels:
            extra["scenario_labels"] = labels
        elif "scenario_labels" in extra and not enabled:
            extra.pop("scenario_labels", None)
        th.extra = extra
        th.updated_at = utcnow()
    return RedirectResponse(url=f"/theses/{thesis_id}?edit=1", status_code=303)


@app.post("/theses/{thesis_id}/observations/add_primary")
def thesis_add_primary_obs(
    thesis_id: int,
    value: str = Form(...),
    target_period: str | None = Form(None),
    document_id: str | None = Form(None),
    note: str | None = Form(None),
) -> RedirectResponse:
    with session_scope() as session:
        th = session.query(Thesis).filter_by(id=int(thesis_id)).first()
        if not th:
            return RedirectResponse(url="/theses", status_code=303)
        prop = _resolve_primary_prop(session, th)
        if not prop:
            return RedirectResponse(url=f"/theses/{thesis_id}?edit=1", status_code=303)
        doc_id_val = int(document_id) if (document_id and document_id.strip().isdigit()) else None
        add_observation(
            session,
            proposition=prop,
            document_id=doc_id_val,
            value=value,
            target_period=target_period,
            note=note,
        )
    return RedirectResponse(url=f"/theses/{thesis_id}?edit=1", status_code=303)


@app.post("/theses/{thesis_id}/evidence/add")
def thesis_add_evidence(
    thesis_id: int,
    document_id: str | None = Form(None),
    document_id_recent: str | None = Form(None),
    link_url: str | None = Form(None),
    link_title: str | None = Form(None),
    polarity: str = Form("support"),
    snippet: str | None = Form(None),
    note: str | None = Form(None),
) -> RedirectResponse:
    doc_id_val = None
    for raw in (document_id, document_id_recent):
        if raw and str(raw).strip().isdigit():
            doc_id_val = int(str(raw).strip())
            break

    link_url_val = str(link_url or "").strip() or None
    link_title_val = str(link_title or "").strip() or None

    if not doc_id_val and link_url_val:
        def _create_doc() -> int | None:
            with session_scope() as session:
                existing = session.query(Document).filter_by(canonical_url=link_url_val).first()
                if existing:
                    return int(existing.id)
                doc = None
                if _is_x_url(link_url_val):
                    payload: dict[str, object] = {"url": link_url_val}
                    if link_title_val:
                        payload["title"] = link_title_val
                    doc = ingest_tweet(
                        session,
                        payload=payload,
                        defer_heavy=True,
                        enqueue_postprocess=enqueue_postprocess_job,
                    )
                if doc is None:
                    doc = ingest_note(session, title=link_title_val or link_url_val, text=link_url_val)
                    doc.url = link_url_val
                    doc.canonical_url = link_url_val
                return int(doc.id)

        try:
            doc_id_val = _with_db_retry(_create_doc)
        except Exception:
            doc_id_val = None

    if not doc_id_val:
        return RedirectResponse(url=f"/theses/{thesis_id}", status_code=303)

    def _attach() -> bool:
        with session_scope() as session:
            th = session.query(Thesis).filter_by(id=int(thesis_id)).first()
            if not th:
                return False
            add_thesis_evidence(
                session,
                thesis=th,
                document_id=int(doc_id_val),
                polarity=polarity,
                snippet=snippet,
                note=note,
            )
            return True

    _with_db_retry(_attach)
    return RedirectResponse(url=f"/theses/{thesis_id}", status_code=303)


@app.post("/theses/{thesis_id}/evidence/{evidence_id}/delete")
def thesis_delete_evidence(thesis_id: int, evidence_id: int) -> RedirectResponse:
    with session_scope() as session:
        ev = session.query(ThesisEvidence).filter_by(id=int(evidence_id), thesis_id=int(thesis_id)).first()
        if ev:
            session.delete(ev)
    return RedirectResponse(url=f"/theses/{thesis_id}", status_code=303)


@app.post("/theses/{thesis_id}/notes/link")
def thesis_link_note(thesis_id: int, note_id: int = Form(...), relation: str = Form("context")) -> RedirectResponse:
    with session_scope() as session:
        link_note_thesis(session, note_id=int(note_id), thesis_id=int(thesis_id), relation=relation)
    return RedirectResponse(url=f"/theses/{thesis_id}", status_code=303)


@app.post("/theses/{thesis_id}/notes/unlink")
def thesis_unlink_note(thesis_id: int, note_id: int = Form(...)) -> RedirectResponse:
    with session_scope() as session:
        unlink_note_thesis(session, note_id=int(note_id), thesis_id=int(thesis_id))
    return RedirectResponse(url=f"/theses/{thesis_id}", status_code=303)


@app.post("/theses/{thesis_id}/link/dataset")
def thesis_link_dataset(
    thesis_id: int,
    dataset_id: int = Form(...),
    relation: str = Form("context"),
) -> RedirectResponse:
    with session_scope() as session:
        ensure_dataset_tables(session)
        link_dataset(
            session,
            dataset_id=int(dataset_id),
            object_type="thesis",
            object_id=int(thesis_id),
            relation=str(relation or "context"),
        )
    return RedirectResponse(url=f"/theses/{thesis_id}", status_code=303)


@app.post("/theses/{thesis_id}/unlink/dataset")
def thesis_unlink_dataset(thesis_id: int, dataset_id: int = Form(...)) -> RedirectResponse:
    with session_scope() as session:
        ensure_dataset_tables(session)
        db_row = (
            session.query(DatasetLink)
            .filter_by(dataset_id=int(dataset_id), object_type="thesis", object_id=int(thesis_id))
            .first()
        )
        if db_row:
            session.delete(db_row)
    return RedirectResponse(url=f"/theses/{thesis_id}", status_code=303)


@app.post("/theses/{thesis_id}/link/supply-chain")
def thesis_link_supply_chain(
    thesis_id: int,
    chain_id: int = Form(...),
    relation: str = Form("context"),
) -> RedirectResponse:
    with session_scope() as session:
        _ensure_supply_chain_tables(session)
        link_supply_chain_object(
            session,
            chain_id=int(chain_id),
            object_type="thesis",
            object_id=int(thesis_id),
            relation=str(relation or "context"),
        )
    return RedirectResponse(url=f"/theses/{thesis_id}", status_code=303)


@app.post("/theses/{thesis_id}/unlink/supply-chain")
def thesis_unlink_supply_chain(thesis_id: int, chain_id: int = Form(...)) -> RedirectResponse:
    with session_scope() as session:
        _ensure_supply_chain_tables(session)
        db_row = (
            session.query(SupplyChainLink)
            .filter_by(chain_id=int(chain_id), object_type="thesis", object_id=int(thesis_id))
            .first()
        )
        if db_row:
            session.delete(db_row)
    return RedirectResponse(url=f"/theses/{thesis_id}", status_code=303)


@app.post("/theses/{thesis_id}/link/stage-tracker")
def thesis_link_stage_tracker(thesis_id: int, entry_id: int = Form(...)) -> RedirectResponse:
    with session_scope() as session:
        link_product_stage_entry_object(
            session,
            entry_id=int(entry_id),
            object_type="thesis",
            object_id=int(thesis_id),
        )
    return RedirectResponse(url=f"/theses/{thesis_id}", status_code=303)


@app.post("/theses/{thesis_id}/unlink/stage-tracker")
def thesis_unlink_stage_tracker(thesis_id: int, entry_id: int = Form(...)) -> RedirectResponse:
    with session_scope() as session:
        unlink_product_stage_entry_object(
            session,
            entry_id=int(entry_id),
            object_type="thesis",
            object_id=int(thesis_id),
        )
    return RedirectResponse(url=f"/theses/{thesis_id}", status_code=303)


@app.post("/theses/{thesis_id}/propositions/add")
def thesis_add_prop(
    thesis_id: int,
    proposition_key: str = Form(...),
    role: str = Form("key"),
    document_id: str | None = Form(None),
    observation_value: str | None = Form(None),
    observation_target_period: str | None = Form(None),
    observation_note: str | None = Form(None),
) -> RedirectResponse:
    with session_scope() as session:
        th = session.query(Thesis).filter_by(id=int(thesis_id)).first()
        if not th:
            return RedirectResponse(url="/theses", status_code=303)

        base_key, inline_val, inline_period = parse_key_inline_observation(proposition_key)
        prop = upsert_proposition(session, key=base_key or proposition_key)
        link_thesis_proposition(session, thesis=th, proposition=prop, role=role)

        val = (observation_value or "").strip() or (inline_val or None)
        period = (observation_target_period or "").strip() or (inline_period or None)
        if val:
            doc_id_val = int(document_id) if (document_id and document_id.strip().isdigit()) else None
            add_observation(
                session,
                proposition=prop,
                document_id=doc_id_val,
                value=val,
                target_period=period,
                note=observation_note,
            )

    return RedirectResponse(url=f"/theses/{thesis_id}", status_code=303)


@app.post("/theses/{thesis_id}/propositions/table")
def thesis_add_prop_table(
    thesis_id: int,
    base_key: str = Form(...),
    unit: str | None = Form(None),
    role: str = Form("key"),
    document_id: str | None = Form(None),
    observation_note: str | None = Form(None),
    table_data: str = Form(""),
) -> RedirectResponse:
    base = (base_key or "").strip()
    if not base or not table_data.strip():
        return RedirectResponse(url=f"/theses/{thesis_id}", status_code=303)

    series_names, rows = _parse_table_rows(table_data)
    if not series_names or not rows:
        return RedirectResponse(url=f"/theses/{thesis_id}", status_code=303)

    doc_id_val = int(document_id) if (document_id and document_id.strip().isdigit()) else None
    unit_val = unit.strip() if unit else None
    value_type = "percent" if unit_val and "%" in unit_val else "number"

    with session_scope() as session:
        th = session.query(Thesis).filter_by(id=int(thesis_id)).first()
        if not th:
            return RedirectResponse(url="/theses", status_code=303)

        extra = th.extra or {}
        scenarios_enabled = bool(extra.get("scenarios_enabled"))
        scenario_labels = extra.get("scenario_labels") or []
        if isinstance(scenario_labels, str):
            scenario_labels = [p.strip() for p in scenario_labels.split(",") if p.strip()]

        props: list[Proposition] = []
        if scenarios_enabled:
            if scenario_labels:
                series_names = scenario_labels
            scenario_ids: list[int] = []
            primary_id: int | None = None
            for idx_name, name in enumerate(series_names):
                slug = _slugify_token(name)
                key = base if base.endswith(":") else f"{base}:{slug}"
                prop = upsert_proposition(
                    session,
                    key=key,
                    kind="series",
                    unit=unit_val,
                    value_type=value_type,
                )
                prop_role = "key" if idx_name == 0 else "supporting"
                link_thesis_proposition(session, thesis=th, proposition=prop, role=prop_role)
                props.append(prop)
                scenario_ids.append(prop.id)
                if idx_name == 0:
                    primary_id = prop.id
            extra = dict(extra)
            extra["scenario_prop_ids"] = scenario_ids
            extra["metric_key"] = base
            if unit_val is not None:
                extra["metric_unit"] = unit_val
            if value_type:
                extra["metric_value_type"] = value_type
            if primary_id:
                extra["primary_proposition_id"] = primary_id
            th.extra = extra
        else:
            # Single-series mode: map all columns to the primary metric.
            if len(series_names) > 1:
                series_names = [series_names[0]]
            prop = upsert_proposition(
                session,
                key=base,
                kind="series",
                unit=unit_val,
                value_type=value_type,
            )
            _set_primary_prop(
                session,
                th,
                prop,
                {
                    "metric_key": base,
                    "metric_unit": unit_val or "",
                    "metric_value_type": value_type,
                },
            )
            props = [prop]

        for period, values in rows:
            for idx, prop in enumerate(props):
                val = values[idx] if idx < len(values) else ""
                if not val:
                    continue
                add_observation(
                    session,
                    proposition=prop,
                    document_id=doc_id_val,
                    value=val,
                    target_period=period,
                    note=observation_note,
                )

    return RedirectResponse(url=f"/theses/{thesis_id}", status_code=303)


@app.post("/theses/{thesis_id}/propositions/{prop_id}/unlink")
def thesis_unlink_prop(thesis_id: int, prop_id: int) -> RedirectResponse:
    with session_scope() as session:
        link = session.query(ThesisProposition).filter_by(thesis_id=int(thesis_id), proposition_id=int(prop_id)).first()
        if link:
            session.delete(link)
    return RedirectResponse(url=f"/theses/{thesis_id}", status_code=303)


@app.post("/theses/{thesis_id}/propositions/{prop_id}/observations/add")
def thesis_add_obs(
    thesis_id: int,
    prop_id: int,
    value: str = Form(...),
    unit: str | None = Form(None),
    target_period: str | None = Form(None),
    estimate_flag: str | None = Form(None),
    document_id: str | None = Form(None),
    note: str | None = Form(None),
) -> RedirectResponse:
    with session_scope() as session:
        prop = session.query(Proposition).filter_by(id=int(prop_id)).first()
        if not prop:
            return RedirectResponse(url=f"/theses/{thesis_id}", status_code=303)
        doc_id_val = int(document_id) if (document_id and document_id.strip().isdigit()) else None
        add_observation(
            session,
            proposition=prop,
            document_id=doc_id_val,
            value=value,
            unit=unit,
            target_period=target_period,
            estimate_flag=estimate_flag,
            note=note,
        )
    return RedirectResponse(url=f"/theses/{thesis_id}", status_code=303)


@app.post("/api/theses/{thesis_id}/evidence")
def api_thesis_evidence(
    thesis_id: int,
    payload: dict = Body(default_factory=dict),
) -> JSONResponse:
    doc_id = payload.get("document_id")
    if not doc_id:
        return JSONResponse({"ok": False, "detail": "document_id required"}, status_code=400)
    try:
        doc_id_val = int(doc_id)
    except Exception:
        return JSONResponse({"ok": False, "detail": "invalid document_id"}, status_code=400)

    polarity = payload.get("polarity") or "support"
    snippet = payload.get("snippet")
    note = payload.get("note")

    with session_scope() as session:
        th = session.query(Thesis).filter_by(id=int(thesis_id)).first()
        if not th:
            return JSONResponse({"ok": False, "detail": "thesis not found"}, status_code=404)
        ev = add_thesis_evidence(
            session,
            thesis=th,
            document_id=doc_id_val,
            polarity=str(polarity),
            snippet=str(snippet) if snippet else None,
            note=str(note) if note else None,
        )
    return JSONResponse({"ok": True, "created": ev is not None})
