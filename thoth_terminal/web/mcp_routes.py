from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response


router = APIRouter()
_CONTEXT_PROVIDER = None


def register_mcp_routes(app_obj: Any, *, context_provider=None) -> None:
    """Register the lightweight MCP gateway without importing the MCP engine."""

    global _CONTEXT_PROVIDER
    _CONTEXT_PROVIDER = context_provider
    app_obj.include_router(router)
    from thoth_terminal.web.artcraft_routes import router as artcraft_router
    app_obj.include_router(artcraft_router)


def _load_mcp_engine():
    from thoth_terminal.web import mcp

    if _CONTEXT_PROVIDER is not None:
        mcp._CONTEXT_PROVIDER = _CONTEXT_PROVIDER
    return mcp


@router.post("/mcp")
@router.get("/mcp")
async def mcp_endpoint(request: Request) -> Response:
    mcp = _load_mcp_engine()
    return await mcp._mcp_http_endpoint(request)


@router.post("/mcp/app")
@router.post("/mcp/app/{app_token}")
@router.get("/mcp/app")
@router.get("/mcp/app/{app_token}")
async def mcp_chatgpt_app_endpoint(request: Request, app_token: str | None = None) -> Response:
    mcp = _load_mcp_engine()
    expected = mcp._mcp_chatgpt_app_token()
    if not expected:
        return mcp.JSONResponse({"error": "Not found"}, status_code=404)
    provided = str(app_token or "").strip()
    if not provided or not mcp.secrets.compare_digest(provided, expected):
        return mcp.JSONResponse({"error": "Not found"}, status_code=404)
    return await mcp._mcp_http_endpoint(request, chatgpt_app=True, chatgpt_access_mode="write")


@router.post("/mcp/app/read-only")
@router.post("/mcp/app/read-only/{app_token}")
@router.get("/mcp/app/read-only")
@router.get("/mcp/app/read-only/{app_token}")
async def mcp_chatgpt_app_read_only_endpoint(
    request: Request,
    app_token: str | None = None,
) -> Response:
    mcp = _load_mcp_engine()
    expected = mcp._mcp_chatgpt_app_read_only_token()
    if not expected:
        return mcp.JSONResponse({"error": "Not found"}, status_code=404)
    provided = str(app_token or "").strip()
    if not provided or not mcp.secrets.compare_digest(provided, expected):
        return mcp.JSONResponse({"error": "Not found"}, status_code=404)
    return await mcp._mcp_http_endpoint(request, chatgpt_app=True, chatgpt_access_mode="read_only")


__all__ = ["register_mcp_routes", "router"]
