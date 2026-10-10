from __future__ import annotations

from thoth_terminal.web.app_sections._shared import bind_app_context


bind_app_context(globals())


_PLACEHOLDER_URL_HOSTS = ("example.com", "example.org", "example.net")


def _clean_external_artifact_url(value: object) -> str | None:
    url = clean_url(value)
    if not url:
        return None
    lower = url.lower()
    if any(f"://{host}" in lower or f".{host}/" in lower for host in _PLACEHOLDER_URL_HOSTS):
        return None
    return url


def _earnings_period_label(year: int | None, quarter: int | None) -> str:
    if year is None:
        return "Results"
    if quarter is None:
        return f"FY {year}"
    return f"Q{quarter} {year}"


def _earnings_period_viewable(year: int | None, *, today: date) -> bool:
    return year is None or is_plausible_earnings_fiscal_year(year, today=today)


def _earnings_display_status(event: EarningsEvent, *, today: date) -> str:
    raw_status = str(getattr(event, "status", "") or "scheduled").strip().lower()
    if raw_status != "completed":
        return raw_status or "scheduled"
    actual_release_at = getattr(event, "actual_release_at", None)
    actual_date = actual_release_at.date() if actual_release_at is not None else None
    scheduled_date = getattr(event, "scheduled_date", None)
    if actual_date is not None and actual_date <= today:
        return "completed"
    if scheduled_date is not None and scheduled_date >= today:
        return "confirmed" if str(getattr(event, "date_provenance", "") or "") != "estimated" else "scheduled"
    return "completed" if actual_date is not None else "scheduled"


def _earnings_sector_clause(model: type[Issuer] | type[Company], sector: str | None):
    key = str(sector or "").strip().lower()
    if not key:
        return None
    terms_by_key = {
        "semis": ("semi", "chip", "semiconductor"),
        "tech": ("tech", "software", "hardware", "cloud", "internet"),
        "materials": ("material", "chemical", "mining", "metals"),
    }
    terms = terms_by_key.get(key)
    if not terms:
        return None
    fields = [model.sector, model.industry]
    clauses = [field.ilike(f"%{term}%") for field in fields for term in terms]
    return or_(*clauses)


def _earnings_release_document_rows(
    db: Session,
    *,
    view: str,
    sector: str | None,
    today: date,
    now: datetime,
    limit: int = 200,
) -> list[tuple[CompanyDocument, Document, Company]]:
    effective_ts = func.coalesce(Document.published_at, Document.ingested_at)
    query = (
        db.query(CompanyDocument, Document, Company)
        .join(Document, Document.id == CompanyDocument.document_id)
        .join(Company, Company.id == CompanyDocument.company_id)
        .filter(CompanyDocument.doc_type == "earnings_release")
    )
    if view == "upcoming":
        query = query.filter(effective_ts >= datetime.combine(today - timedelta(days=1), datetime.min.time()))
    elif view == "recent":
        query = query.filter(effective_ts >= now - timedelta(days=30))
    sector_clause = _earnings_sector_clause(Company, sector)
    if sector_clause is not None:
        query = query.filter(sector_clause)
    sort_expr = effective_ts.asc() if view == "upcoming" else effective_ts.desc()
    return query.order_by(sort_expr, Company.name.asc(), Document.id.desc()).limit(max(1, int(limit))).all()


def _append_earnings_release_document_rows(
    *,
    rows: list[tuple[CompanyDocument, Document, Company]],
    events_data: list[dict[str, object]],
    seen_document_ids: set[int],
    today: date,
) -> None:
    for _company_doc, document, company in rows:
        document_id = int(document.id)
        if document_id in seen_document_ids:
            continue
        effective_at = document.published_at or document.ingested_at
        if effective_at is None:
            continue
        year, quarter = _parse_period_from_text(document.title, document.url or document.canonical_url)
        if not _earnings_period_viewable(year, today=today):
            continue
        release_url = _clean_external_artifact_url(document.url or document.canonical_url)
        events_data.append(
            {
                "issuer_id": int(company.issuer_id) if company.issuer_id is not None else None,
                "issuer_name": company.name,
                "company_slug": company.slug,
                "primary_ticker": company.ticker,
                "fiscal_year": year,
                "fiscal_quarter": quarter,
                "period_label": _earnings_period_label(year, quarter),
                "scheduled_date": effective_at.date(),
                "scheduled_time": None,
                "scheduled_tz": None,
                "date_provenance": "issuer_published",
                "date_source": "company_document",
                "status": "completed",
                "raw_status": "document_derived",
                "actual_release_at": effective_at,
                "is_today": effective_at.date() == today,
                "is_upcoming": False,
                "is_bmo": False,
                "is_amc": effective_at.hour >= 16,
                "release_url": release_url,
                "presentation_url": None,
                "webcast_url": None,
                "transcript_url": None,
                "filing_url": None,
            }
        )
        seen_document_ids.add(document_id)


def _earnings_sort_key(row: dict[str, object]) -> tuple[date, datetime, str]:
    row_date = row.get("scheduled_date")
    if not isinstance(row_date, date):
        row_date = date.min
    actual_at = row.get("actual_release_at")
    if not isinstance(actual_at, datetime):
        actual_at = datetime.combine(row_date, datetime.min.time())
    return row_date, actual_at, str(row.get("issuer_name") or "")


@app.get("/earnings", response_class=HTMLResponse)
def earnings_view(
    request: Request,
    view: str = "upcoming",
    sector: str | None = None,
    db: Session = Depends(get_session),
) -> HTMLResponse:
    """Earnings calendar with provenance-labeled dates."""
    today = calendar_today()
    now = utcnow()
    min_fiscal_year, max_fiscal_year = earnings_fiscal_year_bounds(today=today)

    events_query = (
        db.query(EarningsEvent, Issuer)
        .join(Issuer, Issuer.id == EarningsEvent.issuer_id)
        .filter(func.lower(func.coalesce(EarningsEvent.status, "")) != "invalid")
        .filter(
            or_(
                EarningsEvent.fiscal_year.is_(None),
                and_(
                    EarningsEvent.fiscal_year >= min_fiscal_year,
                    EarningsEvent.fiscal_year <= max_fiscal_year,
                ),
            )
        )
    )
    sector_clause = _earnings_sector_clause(Issuer, sector)
    if sector_clause is not None:
        events_query = events_query.filter(sector_clause)

    if view == "upcoming":
        events_query = events_query.filter(
            EarningsEvent.scheduled_date >= today - timedelta(days=1)
        ).order_by(EarningsEvent.scheduled_date.asc())
    elif view == "recent":
        events_query = events_query.filter(
            EarningsEvent.status == "completed",
            EarningsEvent.actual_release_at >= now - timedelta(days=30),
        ).order_by(EarningsEvent.actual_release_at.desc())
    else:  # all
        events_query = events_query.order_by(EarningsEvent.scheduled_date.desc())

    events_raw = events_query.limit(200).all()

    events_data: list[dict] = []
    seen_document_ids: set[int] = set()
    for event, issuer in events_raw:
        if not _earnings_period_viewable(event.fiscal_year, today=today):
            continue
        primary_ticker = None
        primary_ident = (
            db.query(IssuerIdentifier)
            .filter(
                IssuerIdentifier.issuer_id == issuer.id,
                IssuerIdentifier.id_type == "TICKER",
            )
            .order_by(IssuerIdentifier.is_primary.desc())
            .first()
        )
        if primary_ident:
            primary_ticker = primary_ident.id_value

        period_label = _earnings_period_label(event.fiscal_year, event.fiscal_quarter)

        release_url = None
        presentation_url = None
        webcast_url = None
        transcript_url = None
        filing_url = None
        company_row = _primary_company_for_issuer(db, int(issuer.id))

        if event.release_artifact_id:
            artifact = db.query(IRArtifact).filter_by(id=event.release_artifact_id).first()
            if artifact:
                release_url = _clean_external_artifact_url(artifact.canonical_url)
                if artifact.document_id is not None:
                    seen_document_ids.add(int(artifact.document_id))
        if event.presentation_artifact_id:
            artifact = db.query(IRArtifact).filter_by(id=event.presentation_artifact_id).first()
            if artifact:
                presentation_url = _clean_external_artifact_url(artifact.canonical_url)
                if artifact.document_id is not None:
                    seen_document_ids.add(int(artifact.document_id))
        if event.webcast_artifact_id:
            artifact = db.query(IRArtifact).filter_by(id=event.webcast_artifact_id).first()
            if artifact:
                webcast_url = _clean_external_artifact_url(artifact.webcast_url or artifact.canonical_url)
                if artifact.document_id is not None:
                    seen_document_ids.add(int(artifact.document_id))
        if event.transcript_artifact_id:
            artifact = db.query(IRArtifact).filter_by(id=event.transcript_artifact_id).first()
            if artifact:
                transcript_url = _clean_external_artifact_url(artifact.canonical_url)
                if artifact.document_id is not None:
                    seen_document_ids.add(int(artifact.document_id))
        if event.release_filing_id:
            filing = db.query(SecuritiesFiling).filter_by(id=event.release_filing_id).first()
            if filing:
                filing_url = _clean_external_artifact_url(filing.primary_document_url or filing.filing_url)
                if filing.document_id is not None:
                    seen_document_ids.add(int(filing.document_id))
        raw_meta = event.raw_meta if isinstance(event.raw_meta, dict) else {}
        for document_id in raw_meta.get("legacy_company_document_ids") or []:
            if str(document_id).isdigit():
                seen_document_ids.add(int(document_id))

        display_status = _earnings_display_status(event, today=today)
        is_today = event.scheduled_date == today if event.scheduled_date else False
        is_upcoming = bool(event.scheduled_date and event.scheduled_date >= today and display_status != "completed")

        events_data.append({
            "issuer_id": issuer.id,
            "issuer_name": issuer.display_name,
            "company_slug": company_row.slug if company_row is not None else None,
            "primary_ticker": primary_ticker,
            "fiscal_year": event.fiscal_year,
            "fiscal_quarter": event.fiscal_quarter,
            "period_label": period_label,
            "scheduled_date": event.scheduled_date,
            "scheduled_time": event.scheduled_time,
            "scheduled_tz": event.scheduled_tz,
            "date_provenance": event.date_provenance,
            "date_source": event.date_source,
            "status": display_status,
            "raw_status": event.status,
            "actual_release_at": event.actual_release_at,
            "is_today": is_today,
            "is_upcoming": is_upcoming,
            "is_bmo": event.scheduled_time and event.scheduled_time < "10:00",
            "is_amc": event.scheduled_time and event.scheduled_time >= "16:00",
            "release_url": release_url,
            "presentation_url": presentation_url,
            "webcast_url": webcast_url,
            "transcript_url": transcript_url,
            "filing_url": filing_url,
        })

    _append_earnings_release_document_rows(
        rows=_earnings_release_document_rows(db, view=view, sector=sector, today=today, now=now),
        events_data=events_data,
        seen_document_ids=seen_document_ids,
        today=today,
    )
    events_data.sort(key=_earnings_sort_key, reverse=(view != "upcoming"))
    events_data = events_data[:200]

    recent_results_raw = (
        db.query(EarningsEvent, Issuer)
        .join(Issuer, Issuer.id == EarningsEvent.issuer_id)
        .filter(
            EarningsEvent.status == "completed",
            EarningsEvent.fiscal_year >= min_fiscal_year,
            EarningsEvent.fiscal_year <= max_fiscal_year,
            EarningsEvent.actual_release_at.isnot(None),
            EarningsEvent.actual_release_at <= now,
            EarningsEvent.actual_release_at >= now - timedelta(days=30),
        )
        .order_by(EarningsEvent.actual_release_at.desc())
        .limit(15)
        .all()
    )

    recent_results: list[dict] = []
    seen_recent_document_ids: set[int] = set()
    for event, issuer in recent_results_raw:
        if _earnings_display_status(event, today=today) != "completed":
            continue
        period_label = _earnings_period_label(event.fiscal_year, event.fiscal_quarter)
        release_url = None
        if event.release_artifact_id:
            artifact = db.query(IRArtifact).filter_by(id=event.release_artifact_id).first()
            if artifact:
                release_url = _clean_external_artifact_url(artifact.canonical_url)
                if artifact.document_id is not None:
                    seen_recent_document_ids.add(int(artifact.document_id))
        raw_meta = event.raw_meta if isinstance(event.raw_meta, dict) else {}
        for document_id in raw_meta.get("legacy_company_document_ids") or []:
            if str(document_id).isdigit():
                seen_recent_document_ids.add(int(document_id))
        company_row = _primary_company_for_issuer(db, int(issuer.id))
        recent_results.append({
            "issuer_id": issuer.id,
            "issuer_name": issuer.display_name,
            "company_slug": company_row.slug if company_row is not None else None,
            "period_label": period_label,
            "actual_release_at": event.actual_release_at,
            "release_url": release_url,
        })
    recent_doc_rows = _earnings_release_document_rows(db, view="recent", sector=sector, today=today, now=now, limit=50)
    for _company_doc, document, company in recent_doc_rows:
        document_id = int(document.id)
        if document_id in seen_recent_document_ids:
            continue
        effective_at = document.published_at or document.ingested_at
        if effective_at is None or effective_at > now or effective_at < now - timedelta(days=30):
            continue
        year, quarter = _parse_period_from_text(document.title, document.url or document.canonical_url)
        if not _earnings_period_viewable(year, today=today):
            continue
        recent_results.append({
            "issuer_id": int(company.issuer_id) if company.issuer_id is not None else None,
            "issuer_name": company.name,
            "company_slug": company.slug,
            "period_label": _earnings_period_label(year, quarter),
            "actual_release_at": effective_at,
            "release_url": _clean_external_artifact_url(document.url or document.canonical_url),
        })
        seen_recent_document_ids.add(document_id)
    recent_results.sort(
        key=lambda row: row["actual_release_at"] if isinstance(row.get("actual_release_at"), datetime) else datetime.min,
        reverse=True,
    )
    recent_results = recent_results[:15]

    return _template_response(
        "earnings.html",
        {
            "request": request,
            "events": events_data,
            "recent_results": recent_results,
            "view": view,
            "sector_filter": sector,
        },
    )


@app.get("/transcripts", response_class=HTMLResponse)
def transcripts_view(
    request: Request,
    ticker: str | None = None,
    most_recent: bool = True,
    year: int | None = None,
    quarter: int | None = None,
    limit: int = 20,
    db: Session = Depends(get_session),
) -> HTMLResponse:
    sym = (ticker or "").strip().upper()
    transcripts: list[dict[str, object]] = []
    error: str | None = None
    if quarter is not None and quarter not in {1, 2, 3, 4}:
        error = "Quarter must be 1..4."
    elif sym:
        try:
            rows = query_transcripts(
                db,
                ticker=sym,
                most_recent=bool(most_recent),
                year=year,
                quarter=quarter,
                limit=max(1, min(int(limit or 20), 50)),
            )
            if not rows:
                row = query_transcript(
                    db,
                    ticker=sym,
                    most_recent=bool(most_recent),
                    year=year,
                    quarter=quarter,
                    allow_live_fallback=True,
                )
                rows = [row] if row else []
            transcripts = [row.to_dict() for row in rows]
        except Exception as exc:
            error = str(exc)
    return _template_response(
        "transcripts.html",
        {
            "request": request,
            "ticker": sym,
            "most_recent": bool(most_recent),
            "year": year,
            "quarter": quarter,
            "limit": max(1, min(int(limit or 20), 50)),
            "error": error,
            "transcripts": transcripts,
        },
    )


@app.get("/currency", response_class=HTMLResponse)
def currency_view(request: Request, db: Session = Depends(get_session)) -> HTMLResponse:
    base = str(settings.base_currency or "USD").upper()
    rates_snapshot = ensure_fx_rates(db, base=base) or get_latest_rates(db, base=base)
    rates = []
    if rates_snapshot:
        rates = sorted((rates_snapshot.rates or {}).items())
    return _template_response(
        "currency.html",
        {
            "request": request,
            "base": base,
            "as_of": rates_snapshot.as_of.isoformat() if rates_snapshot else None,
            "provider": rates_snapshot.provider if rates_snapshot else None,
            "rates": rates,
        },
    )


_MARKETS_TAPE_COOKIE = "thoth_markets_tape"
_TICKER_DISPLAY_COOKIE = "thoth_ticker_display"


def _get_mock_quotes(symbols: list[str]) -> list[dict]:
    """Return mock quote data for development. In production, this would call a real API."""
    import random
    quotes = []
    base_prices = {
        "SPY": 596.42, "QQQ": 518.75, "NVDA": 138.25, "TSM": 203.50, "AVGO": 242.18,
        "AMD": 119.85, "INTC": 19.42, "MU": 95.67, "AMAT": 178.30, "LRCX": 75.25,
        "ASML": 735.40, "KLAC": 698.50, "MRVL": 118.92, "QCOM": 162.35, "SNPS": 505.20,
        "CDNS": 295.80, "ARM": 165.45, "GFS": 40.25, "ON": 55.18, "NXPI": 215.60,
    }
    for sym in symbols:
        price = base_prices.get(sym, random.uniform(50, 500))
        change_pct = random.uniform(-3.5, 3.5)
        change = price * change_pct / 100
        quotes.append({
            "symbol": sym,
            "name": None,
            "price": price,
            "change": change,
            "change_pct": change_pct,
            "change_1d": change_pct,
            "change_1w": random.uniform(-5, 5),
            "change_1m": random.uniform(-10, 10),
            "currency": "USD",
            "low_52w": price * 0.65,
            "high_52w": price * 1.35,
            "range_pct": random.uniform(20, 80),
            "sparkline": ",".join(str(price + random.uniform(-5, 5)) for _ in range(20)),
        })
    return quotes


def _get_mock_macro() -> tuple[list[dict], list[dict], list[dict]]:
    """Return mock macro data for development."""
    import random
    rates = [
        {"label": "US 10Y", "value": 4.68 + random.uniform(-0.05, 0.05), "change": random.uniform(-0.05, 0.05), "unit": "%"},
        {"label": "US 2Y", "value": 4.28 + random.uniform(-0.05, 0.05), "change": random.uniform(-0.05, 0.05), "unit": "%"},
        {"label": "Fed Funds", "value": 5.33, "change": 0, "unit": "%"},
        {"label": "VIX", "value": 17.85 + random.uniform(-1, 1), "change": random.uniform(-5, 5), "unit": None},
    ]
    fx = [
        {"label": "DXY", "value": 109.25 + random.uniform(-0.5, 0.5), "change": random.uniform(-0.5, 0.5)},
        {"label": "EUR/USD", "value": 1.0285 + random.uniform(-0.005, 0.005), "change": random.uniform(-0.3, 0.3)},
        {"label": "USD/JPY", "value": 157.85 + random.uniform(-0.5, 0.5), "change": random.uniform(-0.5, 0.5)},
        {"label": "USD/CNY", "value": 7.33 + random.uniform(-0.02, 0.02), "change": random.uniform(-0.2, 0.2)},
    ]
    commodities = [
        {"label": "WTI Crude", "value": 73.85 + random.uniform(-1, 1), "change": random.uniform(-2, 2)},
        {"label": "Gold", "value": 2688.50 + random.uniform(-10, 10), "change": random.uniform(-1, 1)},
        {"label": "Copper", "value": 4.12 + random.uniform(-0.05, 0.05), "change": random.uniform(-1.5, 1.5)},
    ]
    return rates, fx, commodities


@app.get("/markets", response_class=HTMLResponse)
def markets_view(
    request: Request,
    days: int = 7,
    db: Session = Depends(get_session),
) -> HTMLResponse:
    """Feed intelligence view - aggregate insights from ingested content."""
    now = utcnow()
    window_start = now - timedelta(days=days)

    # Total docs in window
    total_docs = db.query(func.count(Document.id)).filter(
        Document.ingested_at >= window_start
    ).scalar() or 0

    # Docs per day
    docs_per_day = total_docs / max(days, 1)

    # Estimate WPM (words per minute) from total content
    total_words = db.query(func.sum(func.length(Document.normalized_text))).filter(
        Document.ingested_at >= window_start
    ).scalar() or 0
    # Rough estimate: avg 5 chars per word
    estimated_words = total_words / 5
    minutes_in_window = days * 24 * 60
    wpm = estimated_words / max(minutes_in_window, 1)

    # Top sources by volume
    source_counts = (
        db.query(Source.name, func.count(Document.id))
        .join(Document, Document.source_id == Source.id)
        .filter(Document.ingested_at >= window_start)
        .group_by(Source.name)
        .order_by(func.count(Document.id).desc())
        .limit(15)
        .all()
    )
    max_source_count = max((c for _, c in source_counts), default=1)
    source_volumes = [
        {"name": name, "count": count, "pct": (count / max_source_count) * 100}
        for name, count in source_counts
    ]
    top_source = source_counts[0][0] if source_counts else None
    top_source_count = source_counts[0][1] if source_counts else 0

    # Language distribution
    language_counts = (
        db.query(Document.language, func.count(Document.id))
        .filter(Document.ingested_at >= window_start, Document.language.isnot(None))
        .group_by(Document.language)
        .order_by(func.count(Document.id).desc())
        .limit(10)
        .all()
    )
    language_count = len(language_counts)
    top_language = language_counts[0][0] if language_counts else "en"

    # Domain distribution
    domain_counts = (
        db.query(Domain.name, func.count(Document.id))
        .join(document_domains, document_domains.c.domain_id == Domain.id)
        .join(Document, Document.id == document_domains.c.document_id)
        .filter(Document.ingested_at >= window_start)
        .group_by(Domain.name)
        .order_by(func.count(Document.id).desc())
        .limit(10)
        .all()
    )
    domain_distribution = [
        {"name": name, "count": count}
        for name, count in domain_counts
    ]

    # Top entities by mention count
    entity_counts = (
        db.query(Entity.canonical_name, func.count(DocumentEntity.document_id))
        .join(DocumentEntity, DocumentEntity.entity_id == Entity.id)
        .join(Document, Document.id == DocumentEntity.document_id)
        .filter(Document.ingested_at >= window_start)
        .group_by(Entity.canonical_name)
        .order_by(func.count(DocumentEntity.document_id).desc())
        .limit(20)
        .all()
    )
    top_entities = [
        {"name": name, "count": count}
        for name, count in entity_counts
    ]

    # Activity heatmap (last N days)
    heatmap_days = min(days, 14)
    heatmap_start = (now - timedelta(days=heatmap_days - 1)).replace(hour=0, minute=0, second=0, microsecond=0).date()
    day_count_rows = (
        db.query(func.date(Document.ingested_at), func.count(Document.id))
        .filter(Document.ingested_at >= heatmap_start)
        .group_by(func.date(Document.ingested_at))
        .all()
    )
    counts_by_day: dict[date, int] = {}
    for day_value, day_total in day_count_rows:
        if isinstance(day_value, datetime):
            day_value = day_value.date()
        elif not isinstance(day_value, date):
            try:
                day_value = date.fromisoformat(str(day_value)[:10])
            except ValueError:
                continue
        counts_by_day[day_value] = int(day_total or 0)
    activity_heatmap = []
    for i in range(heatmap_days):
        day = heatmap_start + timedelta(days=i)
        day_count = counts_by_day.get(day, 0)
        activity_heatmap.append({
            "date": day.strftime("%Y-%m-%d"),
            "weekday": day.strftime("%a")[0],
            "count": day_count,
            "intensity": min(day_count / max(docs_per_day * 1.5, 1), 1.0),
        })

    # FX rates from existing currency system
    base = str(settings.base_currency or "USD").upper()
    fx_snapshot = get_latest_rates(db, base=base)
    fx_rates = []
    fx_date = None
    if fx_snapshot and fx_snapshot.rates:
        fx_date = fx_snapshot.as_of.isoformat() if fx_snapshot.as_of else None
        # Show common pairs
        common_pairs = ["EUR", "GBP", "JPY", "CNY", "KRW", "TWD", "CHF", "CAD"]
        for pair in common_pairs:
            if pair in fx_snapshot.rates:
                fx_rates.append((f"{base}/{pair}", fx_snapshot.rates[pair]))

    return _template_response(
        "markets.html",
        {
            "request": request,
            "days": days,
            "total_docs": total_docs,
            "docs_per_day": docs_per_day,
            "wpm": wpm,
            "top_source": top_source,
            "top_source_count": top_source_count,
            "language_count": language_count,
            "top_language": top_language,
            "source_volumes": source_volumes,
            "domain_distribution": domain_distribution,
            "top_entities": top_entities,
            "activity_heatmap": activity_heatmap,
            "fx_rates": fx_rates,
            "fx_date": fx_date,
        },
    )


@app.post("/api/markets/watchlist")
def markets_watchlist_add(
    symbols: list[str] = Body(default=[], embed=True),
) -> JSONResponse:
    # In production, this would persist to user preferences
    return JSONResponse({"ok": True, "added": symbols})


@app.get("/api/trade/series")
def api_trade_series_list(
    reporter_iso: str | None = None,
    hs_code: str | None = None,
    theme: str | None = None,
    flow: str | None = None,
    limit: int = 50,
    db: Session = Depends(get_session),
) -> JSONResponse:
    rows = list_trade_series_payload(
        db,
        reporter_iso=(str(reporter_iso).strip().upper() if reporter_iso else None),
        hs_code=(str(hs_code).strip() if hs_code else None),
        theme=(str(theme).strip().lower() if theme else None),
        flow=(str(flow).strip().lower() if flow else None),
        limit=max(1, min(int(limit or 50), 200)),
    )
    return JSONResponse({"count": len(rows), "series": rows})


@app.get("/api/trade/series/detail")
def api_trade_series_detail(
    slug: str | None = None,
    series_id: int | None = None,
    db: Session = Depends(get_session),
) -> JSONResponse:
    payload = get_trade_series_payload(db, slug=(str(slug).strip() if slug else None), series_id=series_id)
    if not payload:
        return JSONResponse({"ok": False, "detail": "trade series not found"}, status_code=404)
    return JSONResponse(payload)


@app.get("/api/trade/summary")
def api_trade_summary(
    reporter_iso: str | None = None,
    hs_code: str | None = None,
    theme: str | None = None,
    flow: str | None = None,
    limit: int = 6,
    db: Session = Depends(get_session),
) -> JSONResponse:
    payload = trade_latest_summary_payload(
        db,
        reporter_iso=(str(reporter_iso).strip().upper() if reporter_iso else None),
        hs_code=(str(hs_code).strip() if hs_code else None),
        theme=(str(theme).strip().lower() if theme else None),
        flow=(str(flow).strip().lower() if flow else None),
        limit=max(1, min(int(limit or 6), 24)),
    )
    return JSONResponse(payload)
