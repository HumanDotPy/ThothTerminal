from __future__ import annotations

from threading import RLock
from typing import Any
import weakref

from thoth_terminal.web.app_sections._shared import bind_app_context


bind_app_context(globals())


# ---------------------------------------------------------------------------
# Visualizations (ChipBook-style charts)
# ---------------------------------------------------------------------------

CHART_LIST_SERIES_POINT_LIMIT = 720


def _chart_points_by_series_for_list(
    db: Session,
    series_ids: list[int],
    *,
    point_limit: int = CHART_LIST_SERIES_POINT_LIMIT,
) -> dict[int, list[dict[str, object]]]:
    resolved_ids = sorted({int(series_id) for series_id in series_ids if series_id is not None})
    if not resolved_ids:
        return {}
    resolved_limit = max(1, int(point_limit or CHART_LIST_SERIES_POINT_LIMIT))
    ranked_points = (
        db.query(
            DataPoint.series_id.label("series_id"),
            DataPoint.period_date.label("period_date"),
            DataPoint.value.label("value"),
            DataPoint.is_estimate.label("is_estimate"),
            func.row_number()
            .over(
                partition_by=DataPoint.series_id,
                order_by=DataPoint.period_date.desc(),
            )
            .label("rank"),
        )
        .filter(DataPoint.series_id.in_(resolved_ids))
        .subquery()
    )
    rows = (
        db.query(
            ranked_points.c.series_id,
            ranked_points.c.period_date,
            ranked_points.c.value,
            ranked_points.c.is_estimate,
        )
        .filter(ranked_points.c.rank <= resolved_limit)
        .order_by(ranked_points.c.series_id.asc(), ranked_points.c.period_date.asc())
        .all()
    )
    by_series: dict[int, list[dict[str, object]]] = {series_id: [] for series_id in resolved_ids}
    for row in rows:
        period_date = row.period_date
        by_series.setdefault(int(row.series_id), []).append(
            {
                "date": period_date.isoformat() if hasattr(period_date, "isoformat") else str(period_date),
                "value": row.value,
                "is_estimate": bool(row.is_estimate),
            }
        )
    return by_series


@app.get("/visualizations")
def visualizations_default() -> RedirectResponse:
    return RedirectResponse(url="/visualizations/datasets", status_code=303)


_VISUALIZATION_TABLE_ENSURE_LOCK = RLock()
_VISUALIZATION_TABLE_ENSURED_ENGINES: "weakref.WeakSet[Any]" = weakref.WeakSet()


def _ensure_visualization_tables(db: Session) -> None:
    bind = db.get_bind()
    engine = getattr(bind, "engine", bind)
    if engine in _VISUALIZATION_TABLE_ENSURED_ENGINES:
        return
    with _VISUALIZATION_TABLE_ENSURE_LOCK:
        if engine in _VISUALIZATION_TABLE_ENSURED_ENGINES:
            return
        bind = db.get_bind()
        failed = False
        try:
            ensure_dataset_tables(db)
        except Exception:
            failed = True
        for table in (
            Chart.__table__,
            ChartSeries.__table__,
            ChartDatasetView.__table__,
            DataSeries.__table__,
            DataPoint.__table__,
        ):
            try:
                table.create(bind=bind, checkfirst=True)
            except Exception:
                failed = True
        if not failed:
            _VISUALIZATION_TABLE_ENSURED_ENGINES.add(engine)


@app.get("/visualizations/charts", response_class=HTMLResponse)
def visualizations_view(
    request: Request,
    category: str | None = None,
    db: Session = Depends(get_session),
) -> HTMLResponse:
    """List all visualization charts, optionally filtered by category."""
    _ensure_visualization_tables(db)

    query = (
        db.query(Chart)
        .options(
            selectinload(Chart.series_links).joinedload(ChartSeries.data_series),
            selectinload(Chart.dataset_view_links).joinedload(ChartDatasetView.dataset_view).joinedload(DatasetView.dataset),
        )
        .order_by(Chart.sort_order, Chart.updated_at.desc())
    )
    if category:
        query = query.filter(Chart.category == category)
    charts = query.all()

    # Get distinct categories
    categories = [
        row[0] for row in db.query(Chart.category).distinct().all() if row[0]
    ]

    # Build JSON data for charts
    import json
    series_ids = [
        int(cs.series_id)
        for chart in charts
        for cs in chart.series_links
        if cs.series_id is not None
    ]
    points_by_series = _chart_points_by_series_for_list(db, series_ids)
    charts_json = []
    for chart in charts:
        series_data = []
        for cs in sorted(chart.series_links, key=lambda x: x.sort_order):
            ds = cs.data_series
            if ds is None:
                continue
            series_data.append({
                "name": ds.name,
                "unit": ds.unit,
                "chart_type": ds.chart_type,
                "display_type": cs.display_type,
                "y_axis_index": cs.y_axis_index,
                "stack_group": cs.stack_group,
                "color": cs.color or ds.color,
                "data": points_by_series.get(int(ds.id), []),
            })
        for dv_link in sorted(chart.dataset_view_links, key=lambda x: x.sort_order):
            view = dv_link.dataset_view
            if not view:
                continue
            dataset = view.dataset
            if not dataset:
                continue
            dataset_columns = _dataset_chart_columns(db, dataset)
            column_payload = {"columns": dataset_columns}
            y_label = _dataset_column_label(column_payload, dv_link.y_key)
            x_label = _dataset_column_label(column_payload, dv_link.x_key)
            dataset_rows = query_dataset(db, dataset_id=int(dataset.id), view_id=int(view.id), limit=120)["rows"]
            series_data.append({
                "name": dv_link.series_name or f"{dataset.title} · {y_label}",
                "unit": _dataset_column_unit(dataset_columns, dv_link.y_key),
                "x_label": x_label,
                "y_label": y_label,
                "chart_type": dv_link.display_type,
                "display_type": dv_link.display_type,
                "y_axis_index": dv_link.y_axis_index,
                "stack_group": None,
                "color": dv_link.color,
                "data": [
                    {"date": (row.get("values") or {}).get(dv_link.x_key), "value": (row.get("values") or {}).get(dv_link.y_key), "is_estimate": False}
                    for row in dataset_rows
                ],
            })
        charts_json.append({
            "id": chart.id,
            "title": chart.title,
            "subtitle": chart.subtitle,
            "category": chart.category,
            "chart_style": chart.chart_style,
            "series": series_data,
            "config": chart.config or {},
        })

    return _template_response(
        "visualizations.html",
        {
            "request": request,
            "charts": charts,
            "charts_json": json.dumps(charts_json),
            "categories": sorted(set(categories)),
            "category": category,
        },
    )


@app.get("/visualizations/new", response_class=HTMLResponse)
def visualizations_new(request: Request, db: Session = Depends(get_session)) -> HTMLResponse:
    """Create a new visualization chart."""
    # Get all available data series
    _ensure_visualization_tables(db)
    series = db.query(DataSeries).order_by(DataSeries.category, DataSeries.name).all()
    return _template_response(
        "visualization_edit.html",
        {
            "request": request,
            "chart": None,
            "all_series": series,
            "chart_series": [],
        },
    )


# ---------------------------------------------------------------------------
# Supply Chain Modeling
# ---------------------------------------------------------------------------
