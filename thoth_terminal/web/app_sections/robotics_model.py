from __future__ import annotations

from thoth_terminal.robotics_deployment_model import robotics_deployment_model
from thoth_terminal.web.app_sections._shared import bind_app_context


bind_app_context(globals())


@app.get("/visualizations/robotics-deployment", response_class=HTMLResponse)
def robotics_deployment_view(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "robotics_deployment.html",
        {
            "request": request,
            "title": "Robotics Deployment Model",
        },
    )


@app.post("/api/robotics/deployment-model")
async def api_robotics_deployment_model(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "error": "JSON object payload required."}, status_code=400)
    try:
        return JSONResponse({"ok": True, "result": robotics_deployment_model(payload)})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
