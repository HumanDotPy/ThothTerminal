from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo
from thoth_terminal.roadmap_service import list_roadmap_entries, list_roadmap_views
from thoth_terminal.roadmap_outcome_service import get_roadmap_current_forecast_metadata, get_roadmap_entry_history
from thoth_terminal.models import RoadmapEntry
from thoth_terminal.web.app_sections._shared import bind_app_context


bind_app_context(globals())


@app.get("/roadmaps", response_class=HTMLResponse)
def roadmaps_index(
    request: Request,
    q: str | None = None,
    view: str | None = None,
    category: str | None = None,
    period: str | None = None,
    db: Session = Depends(get_session),
) -> HTMLResponse:
    active_period = period.strip().lower() if isinstance(period, str) else "upcoming"
    if active_period not in {"upcoming", "past", "all"}:
        active_period = "upcoming"
    today = datetime.now(ZoneInfo("America/New_York")).date()
    views = list_roadmap_views(db)
    entries = list_roadmap_entries(db, q=q, view_key=view, category=category, period=active_period, limit=300, today=today)
    categories = sorted({row.category for row in entries if row.category})
    return _template_response(
        "roadmaps.html",
        {
            "request": request,
            "entries": entries,
            "forecast_metadata": get_roadmap_current_forecast_metadata(db, [row.id for row in entries]),
            "views": views,
            "categories": categories,
            "today_anchor": today.isoformat(),
            "filters": {
                "q": q or "",
                "view": view or "",
                "category": category or "",
                "period": active_period,
            },
        },
    )


@app.get("/roadmaps/{entry_id}", response_class=HTMLResponse)
def roadmap_detail(request: Request, entry_id: int, db: Session = Depends(get_session)) -> HTMLResponse:
    entry = db.get(RoadmapEntry, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Roadmap entry not found")
    history = get_roadmap_entry_history(db, entry_key=entry.entry_key, limit=30)
    original = history.get("original_forecast")
    versions = history.get("forecast_versions", [])
    if original and all(version["forecast_mention_id"] != original["forecast_mention_id"] for version in versions):
        history = {**history, "forecast_versions": [original, *versions]}
    return _template_response("roadmap_detail.html", {"request": request, "history": history})
