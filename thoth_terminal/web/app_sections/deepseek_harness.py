"""Thoth's native conversation surface for the installed DeepSeek Harness."""

from __future__ import annotations

import secrets
from urllib.parse import urlparse

from fastapi import File, Form, HTTPException, Request, UploadFile
from starlette.websockets import WebSocket
import aiohttp
from starlette.concurrency import run_in_threadpool

from thoth_terminal.security import is_loopback_host
from thoth_terminal.agent_evidence_service import MAX_AGENT_PDF_BYTES, ingest_agent_pdf
from thoth_terminal.external_agent_intake_service import queue_operator_evidence_request
from thoth_terminal.web.app_sections._shared import bind_app_context
from thoth_terminal.web.deepseek_harness_service import (
    HarnessRemoteError,
    HarnessUnavailable,
    harness_service,
)


bind_app_context(globals())


def _require_native_access(request: Request, *, mutation: bool = False) -> None:
    authenticated = _request_has_app_auth(request)
    local = is_loopback_host(request.client.host if request.client else "") and is_loopback_host(request.url.hostname or "")
    if not (local or authenticated):
        raise HTTPException(status_code=403, detail="The native Harness interface requires local or authenticated Thoth access.")
    if mutation and not authenticated:
        if request.headers.get("sec-fetch-site") == "cross-site":
            raise HTTPException(status_code=403, detail="Cross-origin Harness actions are disabled.")
        origin = request.headers.get("origin")
        if origin:
            parsed = urlparse(origin)
            expected_scheme = {"ws": "http", "wss": "https"}.get(request.url.scheme, request.url.scheme)
            if parsed.scheme != expected_scheme or parsed.netloc.casefold() != request.headers.get("host", "").casefold():
                raise HTTPException(status_code=403, detail="Cross-origin Harness actions are disabled.")


def _harness_error(exc: Exception) -> HTTPException:
    if isinstance(exc, HarnessUnavailable):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, HarnessRemoteError):
        status = 404 if exc.code.endswith("not-found") else 409 if "busy" in exc.code else 400
        return HTTPException(status_code=status, detail={"code": exc.code, "message": str(exc)})
    return HTTPException(status_code=500, detail="Harness request failed.")


@app.get("/agents/harness", response_class=HTMLResponse)
def harness_page(request: Request) -> HTMLResponse:
    _require_native_access(request)
    return _template_response("deepseek_harness.html", {"request": request})


@app.api_route("/agents/harness/native/{path:path}", methods=["GET", "HEAD", "POST"])
async def harness_native_page(request: Request, path: str):
    _require_native_access(request, mutation=True)
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(status_code=403, detail="Cross-origin Harness access is disabled.")
    # Native RPCs (including read RPCs) are POSTs. Harness state changes do not
    # invalidate Thoth's unrelated page caches; Thoth writes use its MCP rails.
    request.state.skip_terminal_cache_invalidation = True
    from thoth_terminal.web.deepseek_harness_proxy import native_http
    try:
        return await native_http(request, path)
    except HarnessUnavailable as exc:
        raise _harness_error(exc) from exc


@app.websocket("/agents/harness/native/api/remote.mux")
async def harness_native_stream(socket: WebSocket):
    from thoth_terminal.web.deepseek_harness_proxy import native_socket
    try:
        _require_native_access(socket, mutation=True)
        if socket.headers.get("sec-fetch-site") == "cross-site":
            await socket.close(code=1008)
            return
        await native_socket(socket)
    except HTTPException:
        await socket.close(code=1008)
    except (HarnessUnavailable, aiohttp.ClientError, TimeoutError):
        await socket.close(code=1013)


@app.get("/api/agents/harness/status")
def harness_status(request: Request) -> JSONResponse:
    _require_native_access(request)
    return JSONResponse(harness_service.status())


@app.get("/api/agents/intake/status")
async def agent_intake_status(request: Request) -> JSONResponse:
    _require_native_access(request)
    from thoth_terminal.web.agent_intake_status import intake_bridge_status
    return JSONResponse(await intake_bridge_status())


@app.post("/api/agents/harness/start")
def harness_start(request: Request) -> JSONResponse:
    _require_native_access(request, mutation=True)
    try:
        return JSONResponse(harness_service.start())
    except (HarnessUnavailable, HarnessRemoteError) as exc:
        raise _harness_error(exc) from exc


@app.post("/api/agents/harness/evidence/pdf")
async def harness_pdf_evidence_upload(
    request: Request,
    file: UploadFile = File(...),
    title: str = Form(""),
    publisher: str = Form(""),
    source_url: str = Form(""),
    published_at: str = Form(""),
    rights_class: str = Form("user_provided"),
    research_request: str = Form(""),
    request_key: str = Form(""),
) -> JSONResponse:
    owner_token = getattr(settings, "muse_owner_agent_token", None)
    auth = str(request.headers.get("authorization") or "")
    muse_owner = bool(owner_token and auth.lower().startswith("bearer ")
                      and secrets.compare_digest(auth[7:].strip(), str(owner_token)))
    if not muse_owner:
        _require_native_access(request, mutation=True)
    data = await file.read(MAX_AGENT_PDF_BYTES + 1)

    def _ingest() -> dict[str, object]:
        with session_scope() as session:
            result = ingest_agent_pdf(
                session, data=data, filename=file.filename or "", title=title,
                publisher=publisher, source_url=source_url,
                published_at=published_at, rights_class=rights_class,
            )
            if muse_owner:
                intake = queue_operator_evidence_request(
                    session, document_id=int(result["document_id"]),
                    request_key=request_key, request_text=research_request,
                )
                return {**result, "research_request_id": int(intake.id),
                        "research_request_status": intake.status}
            return result

    try:
        return JSONResponse(await run_in_threadpool(_ingest))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
