from __future__ import annotations

from thoth_terminal.web.app_sections._shared import bind_app_context


bind_app_context(globals())


@app.get("/visualizations/{chart_id:int}", response_class=HTMLResponse)
def visualizations_edit(
    request: Request,
    chart_id: int,
    db: Session = Depends(get_session),
) -> HTMLResponse:
    """Edit an existing visualization chart."""
    ensure_dataset_tables(db)
    chart = db.query(Chart).filter_by(id=chart_id).first()
    if not chart:
        raise HTTPException(status_code=404, detail="Chart not found")
    series = db.query(DataSeries).order_by(DataSeries.category, DataSeries.name).all()
    dataset_views = db.query(DatasetView).order_by(DatasetView.name.asc()).all()
    chart_dataset_context = []
    for link in sorted(chart.dataset_view_links, key=lambda x: x.sort_order):
        view = link.dataset_view
        dataset = view.dataset if view else None
        if not view or not dataset:
            continue
        preview_rows = query_dataset(db, dataset_id=int(dataset.id), view_id=int(view.id), limit=8)["rows"]
        chart_dataset_context.append(
            {
                "link": link,
                "view": view,
                "dataset": dataset,
                "summary": serialize_dataset_summary(db, dataset),
                "rows": preview_rows,
            }
        )
    return _template_response(
        "visualization_edit.html",
        {
            "request": request,
            "chart": chart,
            "all_series": series,
            "all_dataset_views": dataset_views,
            "chart_series": sorted(chart.series_links, key=lambda x: x.sort_order),
            "chart_dataset_views": sorted(chart.dataset_view_links, key=lambda x: x.sort_order),
            "chart_dataset_context": chart_dataset_context,
        },
    )


@app.post("/api/visualizations")
def api_visualizations_create(
    title: str = Form(...),
    subtitle: str = Form(default=""),
    category: str = Form(default="general"),
    chart_style: str = Form(default="combo"),
    description_md: str = Form(default=""),
    related_equities: str = Form(default=""),
    db: Session = Depends(get_session),
) -> JSONResponse:
    """Create a new chart."""
    now = utcnow()
    slug = title.lower().replace(" ", "-")[:100]
    # Make slug unique
    existing = db.query(Chart).filter(Chart.slug.like(f"{slug}%")).count()
    if existing > 0:
        slug = f"{slug}-{existing + 1}"

    chart = Chart(
        slug=slug,
        title=title,
        subtitle=subtitle or None,
        category=category,
        chart_style=chart_style,
        description_md=description_md or None,
        related_equities=related_equities or None,
        created_at=now,
        updated_at=now,
    )
    db.add(chart)
    db.commit()
    return JSONResponse({"ok": True, "id": chart.id, "redirect": f"/visualizations/{chart.id}"})


@app.post("/api/visualizations/{chart_id}")
def api_visualizations_update(
    chart_id: int,
    title: str = Form(...),
    subtitle: str = Form(default=""),
    category: str = Form(default="general"),
    chart_style: str = Form(default="combo"),
    description_md: str = Form(default=""),
    related_equities: str = Form(default=""),
    db: Session = Depends(get_session),
) -> JSONResponse:
    """Update an existing chart."""
    chart = db.query(Chart).filter_by(id=chart_id).first()
    if not chart:
        return JSONResponse({"ok": False, "error": "Chart not found"}, status_code=404)

    chart.title = title
    chart.subtitle = subtitle or None
    chart.category = category
    chart.chart_style = chart_style
    chart.description_md = description_md or None
    chart.related_equities = related_equities or None
    chart.updated_at = utcnow()
    db.commit()
    return JSONResponse({"ok": True, "id": chart.id})


@app.post("/api/visualizations/{chart_id}/series")
def api_chart_add_series(
    chart_id: int,
    series_id: int = Form(...),
    y_axis_index: int = Form(default=0),
    display_type: str = Form(default="line"),
    stack_group: str = Form(default=""),
    color: str = Form(default=""),
    db: Session = Depends(get_session),
) -> JSONResponse:
    """Add a data series to a chart."""
    chart = db.query(Chart).filter_by(id=chart_id).first()
    if not chart:
        return JSONResponse({"ok": False, "error": "Chart not found"}, status_code=404)
    series = db.query(DataSeries).filter_by(id=series_id).first()
    if not series:
        return JSONResponse({"ok": False, "error": "Data series not found"}, status_code=404)

    # Check if already linked
    existing = db.query(ChartSeries).filter_by(chart_id=chart_id, series_id=series_id).first()
    if existing:
        return JSONResponse({"ok": False, "error": "Series already added to chart"}, status_code=400)

    max_order = db.query(func.max(ChartSeries.sort_order)).filter_by(chart_id=chart_id).scalar() or 0
    cs = ChartSeries(
        chart_id=chart_id,
        series_id=series_id,
        y_axis_index=y_axis_index,
        display_type=display_type,
        stack_group=stack_group or None,
        color=color or None,
        sort_order=max_order + 1,
    )
    db.add(cs)
    chart.updated_at = utcnow()
    db.commit()
    return JSONResponse({"ok": True, "id": cs.id})


@app.post("/api/visualizations/{chart_id}/dataset-view")
def api_chart_add_dataset_view(
    chart_id: int,
    dataset_view_id: int = Form(...),
    x_key: str = Form(...),
    y_key: str = Form(...),
    series_name: str = Form(default=""),
    y_axis_index: int = Form(default=0),
    display_type: str = Form(default="line"),
    color: str = Form(default=""),
    db: Session = Depends(get_session),
) -> JSONResponse:
    chart = db.query(Chart).filter_by(id=chart_id).first()
    if not chart:
        return JSONResponse({"ok": False, "error": "Chart not found"}, status_code=404)
    view = db.query(DatasetView).filter_by(id=dataset_view_id).first()
    if not view:
        return JSONResponse({"ok": False, "error": "Dataset view not found"}, status_code=404)
    existing = db.query(ChartDatasetView).filter_by(chart_id=chart_id, dataset_view_id=dataset_view_id, y_key=y_key).first()
    if existing:
        return JSONResponse({"ok": False, "error": "Dataset view binding already exists"}, status_code=400)
    max_order = db.query(func.max(ChartDatasetView.sort_order)).filter_by(chart_id=chart_id).scalar() or 0
    row = ChartDatasetView(
        chart_id=chart_id,
        dataset_view_id=dataset_view_id,
        x_key=x_key,
        y_key=y_key,
        series_name=series_name or None,
        y_axis_index=y_axis_index,
        display_type=display_type or "line",
        color=color or None,
        sort_order=max_order + 1,
    )
    db.add(row)
    chart.updated_at = utcnow()
    db.commit()
    return JSONResponse({"ok": True, "id": int(row.id)})


@app.delete("/api/visualizations/{chart_id}/series/{link_id}")
def api_chart_remove_series(
    chart_id: int,
    link_id: int,
    db: Session = Depends(get_session),
) -> JSONResponse:
    """Remove a data series from a chart."""
    cs = db.query(ChartSeries).filter_by(id=link_id, chart_id=chart_id).first()
    if not cs:
        return JSONResponse({"ok": False, "error": "Link not found"}, status_code=404)
    db.delete(cs)
    db.commit()
    return JSONResponse({"ok": True})


@app.delete("/api/visualizations/{chart_id}/dataset-view/{link_id}")
def api_chart_remove_dataset_view(
    chart_id: int,
    link_id: int,
    db: Session = Depends(get_session),
) -> JSONResponse:
    row = db.query(ChartDatasetView).filter_by(id=link_id, chart_id=chart_id).first()
    if not row:
        return JSONResponse({"ok": False, "error": "Link not found"}, status_code=404)
    db.delete(row)
    db.commit()
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Data Series Management
# ---------------------------------------------------------------------------

@app.get("/visualizations/series", response_class=HTMLResponse)
def data_series_list(request: Request, db: Session = Depends(get_session)) -> HTMLResponse:
    """List all data series."""
    _ensure_visualization_tables(db)
    series = db.query(DataSeries).order_by(DataSeries.category, DataSeries.name).all()
    return _template_response(
        "data_series.html",
        {"request": request, "series": series},
    )


@app.post("/api/data-series")
def api_data_series_create(
    name: str = Form(...),
    slug: str = Form(default=""),
    category: str = Form(default="general"),
    unit: str = Form(default="units"),
    frequency: str = Form(default="monthly"),
    source_label: str = Form(default=""),
    chart_type: str = Form(default="line"),
    color: str = Form(default=""),
    db: Session = Depends(get_session),
) -> JSONResponse:
    """Create a new data series."""
    now = utcnow()
    if not slug:
        slug = name.lower().replace(" ", "-")[:100]
    # Make slug unique
    existing = db.query(DataSeries).filter_by(slug=slug).first()
    if existing:
        return JSONResponse({"ok": False, "error": "Slug already exists"}, status_code=400)

    series = DataSeries(
        slug=slug,
        name=name,
        category=category,
        unit=unit,
        frequency=frequency,
        source_label=source_label or None,
        chart_type=chart_type,
        color=color or None,
        created_at=now,
        updated_at=now,
    )
    db.add(series)
    db.commit()
    return JSONResponse({"ok": True, "id": series.id})


@app.post("/api/data-series/{series_id}/points")
def api_data_series_add_point(
    series_id: int,
    period_date: str = Form(...),
    value: float = Form(...),
    is_estimate: bool = Form(default=False),
    source_ref: str = Form(default=""),
    note: str = Form(default=""),
    db: Session = Depends(get_session),
) -> JSONResponse:
    """Add a data point to a series."""
    series = db.query(DataSeries).filter_by(id=series_id).first()
    if not series:
        return JSONResponse({"ok": False, "error": "Series not found"}, status_code=404)

    try:
        pd_date = date.fromisoformat(period_date)
    except ValueError:
        return JSONResponse({"ok": False, "error": "Invalid date format"}, status_code=400)

    now = utcnow()
    # Check for existing point
    existing = db.query(DataPoint).filter_by(
        series_id=series_id, period_date=pd_date, is_estimate=is_estimate
    ).first()
    if existing:
        existing.value = value
        existing.source_ref = source_ref or None
        existing.note = note or None
        existing.revision += 1
        existing.updated_at = now
    else:
        point = DataPoint(
            series_id=series_id,
            period_date=pd_date,
            value=value,
            is_estimate=is_estimate,
            source_ref=source_ref or None,
            note=note or None,
            created_at=now,
            updated_at=now,
        )
        db.add(point)

    series.updated_at = now
    db.commit()
    return JSONResponse({"ok": True})


@app.post("/api/data-series/{series_id}/bulk")
def api_data_series_bulk_import(
    series_id: int,
    data: str = Form(...),
    is_estimate: bool = Form(default=False),
    db: Session = Depends(get_session),
) -> JSONResponse:
    """Bulk import data points (CSV format: date,value)."""
    series = db.query(DataSeries).filter_by(id=series_id).first()
    if not series:
        return JSONResponse({"ok": False, "error": "Series not found"}, status_code=404)

    now = utcnow()
    imported = 0
    errors = []

    for line in data.strip().split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(",")
        if len(parts) < 2:
            errors.append(f"Invalid line: {line}")
            continue
        try:
            pd_date = date.fromisoformat(parts[0].strip())
            value = float(parts[1].strip())
        except (ValueError, IndexError) as e:
            errors.append(f"Parse error: {line} - {e}")
            continue

        existing = db.query(DataPoint).filter_by(
            series_id=series_id, period_date=pd_date, is_estimate=is_estimate
        ).first()
        if existing:
            existing.value = value
            existing.revision += 1
            existing.updated_at = now
        else:
            db.add(DataPoint(
                series_id=series_id,
                period_date=pd_date,
                value=value,
                is_estimate=is_estimate,
                created_at=now,
                updated_at=now,
            ))
        imported += 1

    series.updated_at = now
    db.commit()
    return JSONResponse({"ok": True, "imported": imported, "errors": errors})
