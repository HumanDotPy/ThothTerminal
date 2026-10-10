from __future__ import annotations

from thoth_terminal.agentx_service import (
    agentx_context, evaluate_agentx_configuration, get_agentx_configuration, get_agentx_schema, save_agentx_configuration,
)
from thoth_terminal.web.app_sections._shared import bind_app_context

bind_app_context(globals())


@app.get("/agentx", response_class=HTMLResponse)
def agentx_workbench(request: Request, configuration_key: str | None = None,
                     db: Session = Depends(get_session)) -> HTMLResponse:
    initial = None
    if configuration_key:
        try:
            initial = get_agentx_configuration(db, configuration_key)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _template_response("agentx.html", {"request": request, "title": "AgentX models",
                                             "context": agentx_context(db), "initial": initial})


@app.get("/api/agentx/context")
def api_agentx_context(db: Session = Depends(get_session)) -> JSONResponse:
    return JSONResponse({"ok": True, **agentx_context(db)})


@app.get("/api/agentx/schema")
def api_agentx_schema(mode: str = "canonical") -> JSONResponse:
    try:
        return JSONResponse({"ok": True, **get_agentx_schema(mode)})
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.get("/api/agentx/configurations/{configuration_key}")
def api_agentx_configuration(configuration_key: str, version_id: int | None = None,
                            db: Session = Depends(get_session)) -> JSONResponse:
    try:
        return JSONResponse({"ok": True, **get_agentx_configuration(db, configuration_key, version_id)})
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)


async def _agentx_request_payload(request: Request) -> dict:
    # Bound before parsing and evaluating a user-supplied component graph.
    body = await request.body()
    if len(body) > 256_000:
        raise ValueError("AgentX request exceeds 256 KB")
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise ValueError("JSON object required")
    return payload


@app.post("/api/agentx/evaluate")
async def api_agentx_evaluate(request: Request, db: Session = Depends(get_session)) -> JSONResponse:
    try:
        return JSONResponse({"ok": True, "result": evaluate_agentx_configuration(db, await _agentx_request_payload(request))})
    except (ValueError, TypeError, KeyError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.post("/api/agentx/configurations")
async def api_agentx_save(request: Request, db: Session = Depends(get_session)) -> JSONResponse:
    try:
        saved = save_agentx_configuration(db, await _agentx_request_payload(request))
        db.commit()
        return JSONResponse({"ok": True, **saved})
    except (ValueError, TypeError, KeyError) as exc:
        db.rollback()
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.post("/api/agentx/refresh")
def api_agentx_refresh(db: Session = Depends(get_session)) -> JSONResponse:
    """Explicit source refresh through the desktop-owned API, without a second worker."""
    from thoth_terminal.dataset_parts.inferencex import sync_agentx_api_dataset
    from thoth_terminal.dataset_parts.ai_serving_economics import sync_ai_serving_economics_datasets
    try:
        upstream = sync_agentx_api_dataset(db)
        derived = sync_ai_serving_economics_datasets(db)
        db.commit()
        return JSONResponse({"ok": True, "upstream": upstream, "derived": derived})
    except Exception as exc:
        db.rollback()
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
