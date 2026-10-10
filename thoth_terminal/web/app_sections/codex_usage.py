from __future__ import annotations

from thoth_terminal.codex_usage_service import build_codex_usage_snapshot
from thoth_terminal.web.app_sections._shared import bind_app_context


bind_app_context(globals())


@app.get("/codex-usage", response_class=HTMLResponse)
def codex_usage_view(request: Request, days: int = 30) -> HTMLResponse:
    snapshot = build_codex_usage_snapshot(days=days)
    return _template_response(
        "codex_usage.html",
        {
            "request": request,
            "title": "Codex Usage",
            "snapshot": snapshot,
        },
    )


@app.get("/api/codex-usage")
def api_codex_usage(days: int = 30) -> JSONResponse:
    return JSONResponse(build_codex_usage_snapshot(days=days))
