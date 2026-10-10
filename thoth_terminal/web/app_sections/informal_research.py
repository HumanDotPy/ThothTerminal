from __future__ import annotations

from thoth_terminal.informal_research_service import get_informal_research_doc, list_informal_research_docs
from thoth_terminal.product_model_service import list_world_product_configurations
from thoth_terminal.worldview_write_service import get_worldview_reconciliation, list_worldview_worlds
from thoth_terminal.web.app_sections._shared import bind_app_context


bind_app_context(globals())


@app.get("/worlds", response_class=HTMLResponse)
def worldview_worlds_index(request: Request, q: str | None = None, db: Session = Depends(get_session)) -> HTMLResponse:
    result = list_worldview_worlds(db, query=q or "", limit=100)
    return _template_response("worldview_worlds.html", {
        "request": request, "worlds": result["worlds"], "query": q or "", "detail": None,
    })


@app.get("/worlds/{subject_id}/{world_key}", response_class=HTMLResponse)
def worldview_world_detail(
    request: Request, subject_id: int, world_key: str, db: Session = Depends(get_session),
) -> HTMLResponse:
    result = list_worldview_worlds(db, subject_id=subject_id, query=world_key, limit=100, include_history=True)
    detail = next((item for item in result["worlds"] if item["world_key"] == world_key), None)
    if detail is None:
        raise HTTPException(status_code=404, detail="World not found")
    reconciliation = get_worldview_reconciliation(db, subject_id=subject_id, world_key=world_key, limit=80)
    return _template_response("worldview_worlds.html", {
        "request": request, "worlds": [], "query": "", "detail": detail,
        "reconciliation": reconciliation,
        "product_configurations": list_world_product_configurations(db, subject_id=subject_id, world_key=world_key),
    })


@app.get("/informal-research", response_class=HTMLResponse)
def informal_research_index(
    request: Request,
    q: str | None = None,
    status: str | None = None,
    category: str | None = None,
    db: Session = Depends(get_session),
) -> HTMLResponse:
    docs = list_informal_research_docs(db, q=q, status=status, category=category, limit=300)
    categories = sorted({row.category for row in docs if row.category})
    return _template_response(
        "informal_research.html",
        {
            "request": request,
            "docs": docs,
            "categories": categories,
            "filters": {
                "q": q or "",
                "status": status or "",
                "category": category or "",
            },
        },
    )


@app.get("/worldview-journal", response_class=HTMLResponse)
def worldview_journal_index(
    request: Request,
    q: str | None = None,
    db: Session = Depends(get_session),
) -> HTMLResponse:
    docs = list_informal_research_docs(db, q=q, category="worldview_journal", limit=100)
    return _template_response(
        "worldview_journal.html", {"request": request, "docs": docs, "query": q or ""},
    )


@app.get("/worldview-journal/{doc_key:path}", response_class=HTMLResponse)
def worldview_journal_detail(
    request: Request,
    doc_key: str,
    db: Session = Depends(get_session),
) -> HTMLResponse:
    doc = get_informal_research_doc(db, doc_key)
    if doc is None or doc.category != "worldview_journal":
        raise HTTPException(status_code=404, detail="Worldview journal entry not found")
    return _template_response(
        "informal_research_detail.html",
        {
            "request": request, "doc": doc, "journal": True,
            "events": sorted(doc.events, key=lambda row: row.created_at, reverse=True),
            "content_html": render_markdown_to_html(db, doc.content_md or ""),
        },
    )


@app.get("/informal-research/{doc_key:path}", response_class=HTMLResponse)
def informal_research_detail(
    request: Request,
    doc_key: str,
    db: Session = Depends(get_session),
) -> HTMLResponse:
    doc = get_informal_research_doc(db, doc_key)
    if doc is None:
        raise HTTPException(status_code=404, detail="Informal research document not found")
    events = sorted(doc.events, key=lambda row: row.created_at, reverse=True)
    return _template_response(
        "informal_research_detail.html",
        {
            "request": request,
            "doc": doc,
            "events": events,
            "content_html": render_markdown_to_html(db, doc.content_md or ""),
        },
    )
