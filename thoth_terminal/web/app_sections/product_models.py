from __future__ import annotations

from thoth_terminal.product_model_service import get_product_configuration, list_product_models
from thoth_terminal.web.app_sections._shared import bind_app_context


bind_app_context(globals())


@app.get("/product-models", response_class=HTMLResponse)
def product_models_index(request: Request, q: str | None = None, db: Session = Depends(get_session)) -> HTMLResponse:
    result = list_product_models(db, query=q or "", include_history=True, limit=100)
    return _template_response("product_models.html", {
        "request": request, "models": result["models"], "query": q or "", "detail": None,
    })


@app.get("/product-configurations/{configuration_key}", response_class=HTMLResponse)
def product_configuration_detail(
    request: Request, configuration_key: str, db: Session = Depends(get_session),
) -> HTMLResponse:
    try:
        detail = get_product_configuration(db, configuration_key=configuration_key, include_history=True)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _template_response("product_models.html", {
        "request": request, "models": [], "query": "", "detail": detail,
    })
