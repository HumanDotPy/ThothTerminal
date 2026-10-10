from __future__ import annotations

from thoth_terminal.memory_model_materializer import materialize_memory_system_result
from thoth_terminal.memory_dataset_resolver import evaluate_memory_system_from_datasets, list_memory_system_inputs
from thoth_terminal.memory_system_model import memory_system_model
from thoth_terminal.web.app_sections._shared import bind_app_context


bind_app_context(globals())


@app.get("/visualizations/memory-system", response_class=HTMLResponse)
def memory_system_view(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "memory_system.html",
        {
            "request": request,
            "title": "Memory System",
        },
    )


@app.post("/api/memory/system/preview")
async def api_memory_system_preview(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "error": "JSON object payload required."}, status_code=400)
    try:
        return JSONResponse({"ok": True, "result": memory_system_model(payload)})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.post("/api/memory/system/evaluate")
async def api_memory_system_evaluate(request: Request, db: Session = Depends(get_session)) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "error": "JSON object payload required."}, status_code=400)
    try:
        return JSONResponse({"ok": True, "result": evaluate_memory_system_from_datasets(db, payload)})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.get("/api/memory/system/inputs")
def api_memory_system_inputs(db: Session = Depends(get_session)) -> JSONResponse:
    return JSONResponse({"ok": True, **list_memory_system_inputs(db)})


@app.post("/api/memory/system/materialize")
async def api_memory_system_materialize(request: Request, db: Session = Depends(get_session)) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "error": "JSON object payload required."}, status_code=400)
    model_payload = payload.get("model") if isinstance(payload.get("model"), dict) else payload
    source_dataset_ids = payload.get("source_dataset_ids") if isinstance(payload.get("source_dataset_ids"), list) else []
    source_document_ids = payload.get("source_document_ids") if isinstance(payload.get("source_document_ids"), list) else []
    try:
        result = memory_system_model(dict(model_payload))
        materialized = materialize_memory_system_result(
            db,
            result,
            slug=str(payload.get("slug") or "memory-system-model-output"),
            publish=bool(payload.get("publish", False)),
            source_dataset_ids=[int(value) for value in source_dataset_ids if str(value).isdigit()],
            source_document_ids=[int(value) for value in source_document_ids if str(value).isdigit()],
            bottleneck_model_id=int(payload["bottleneck_model_id"]) if str(payload.get("bottleneck_model_id") or "").isdigit() else None,
            research_subject_scenario_id=(
                int(payload["research_subject_scenario_id"])
                if str(payload.get("research_subject_scenario_id") or "").isdigit()
                else None
            ),
        )
        db.commit()
        return JSONResponse({"ok": True, "result": result, "materialization": materialized})
    except Exception as exc:
        db.rollback()
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
