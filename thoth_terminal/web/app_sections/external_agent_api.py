"""Public HTTPS API for invited agents; no internal MCP or document serializer."""

from __future__ import annotations

import base64

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, Response
from starlette.datastructures import UploadFile as _AgentUploadFile
from starlette.concurrency import run_in_threadpool

from thoth_terminal.agent_evidence_service import MAX_AGENT_PDF_BYTES, ingest_agent_pdf
from thoth_terminal.document_page_service import render_document_page
from thoth_terminal.external_agent_access_service import authenticate_agent_api_key, reserve_agent_api_use
from thoth_terminal.external_agent_intake_service import queue_evidence_request
from thoth_terminal.external_agent_model_service import (
    get_public_projection, list_public_projections, submit_external_model_proposal,
)
from thoth_terminal.models import Document, DocumentPage, ExternalAgentEvidence, ExternalAgentProposal, ExternalAgentResearchRequest
from thoth_terminal.web.app_sections._shared import bind_app_context


bind_app_context(globals())


def _agent_principal_id(session, request: Request, scope: str) -> int:
    header = str(request.headers.get("authorization") or "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    principal = authenticate_agent_api_key(session, token=token, required_scope=scope)
    if principal is None:
        raise HTTPException(status_code=401, detail="Invalid or insufficient agent API key")
    return int(principal.id)


def _owned_document(session, *, principal_id: int, document_id: int) -> Document:
    grant = session.query(ExternalAgentEvidence).filter_by(
        principal_id=int(principal_id), document_id=int(document_id),
    ).first()
    document = session.query(Document).filter_by(id=int(document_id)).first() if grant is not None else None
    if document is None or str((document.extra or {}).get("access_scope") or "") != "external_agent":
        raise HTTPException(status_code=404, detail="Evidence document not found")
    return document


def _json(data: dict, *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status_code, headers={"Cache-Control": "no-store"})


def _limit_error(exc: ValueError) -> HTTPException:
    return HTTPException(status_code=429, detail=str(exc))


def _authorize_and_charge(request: Request, scope: str, *, upload_bytes: int = 0,
                          render: bool = False, proposal: bool = False) -> int:
    # Commit the charge independently so a later invalid lookup or payload cannot
    # roll back usage accounting. The key is checked again after upload parsing.
    with session_scope() as session:
        principal_id = _agent_principal_id(session, request, scope)
        try:
            reserve_agent_api_use(session, principal_id=principal_id,
                                  upload_bytes=upload_bytes, render=render, proposal=proposal)
        except ValueError as exc:
            raise _limit_error(exc) from exc
        return principal_id


@app.get("/public/api/agents/models")
def public_agent_models(request: Request, q: str = "", limit: int = 20) -> JSONResponse:
    _authorize_and_charge(request, "models:read")
    with session_scope() as session:
        rows = list_public_projections(session, query=q, limit=limit)
    return _json({"models": rows, "count": len(rows)})


@app.get("/public/api/agents/models/{model_key}")
def public_agent_model(request: Request, model_key: str) -> JSONResponse:
    _authorize_and_charge(request, "models:read")
    with session_scope() as session:
        row = get_public_projection(session, model_key=model_key)
    if row is None:
        raise HTTPException(status_code=404, detail="Released model not found")
    return _json(row)


@app.post("/public/api/agents/evidence/pdf")
async def public_agent_pdf_upload(request: Request) -> JSONResponse:
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > MAX_AGENT_PDF_BYTES + 100_000:
        raise HTTPException(status_code=413, detail="PDF upload is too large")
    with session_scope() as session:
        principal_id = _agent_principal_id(session, request, "evidence:upload")
    try:
        form = await request.form(max_files=1, max_fields=8)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid PDF upload form") from exc
    file = form.get("file")
    if not isinstance(file, _AgentUploadFile):
        raise HTTPException(status_code=400, detail="file must be a PDF upload")
    data = await file.read(MAX_AGENT_PDF_BYTES + 1)
    if len(data) > MAX_AGENT_PDF_BYTES:
        raise HTTPException(status_code=413, detail="PDF upload is too large")

    def ingest() -> tuple[dict, int, str]:
        current_principal_id = _authorize_and_charge(request, "evidence:upload", upload_bytes=len(data))
        with session_scope() as session:
            result = ingest_agent_pdf(
                session, data=data, filename=file.filename or "",
                title=str(form.get("title") or ""), publisher=str(form.get("publisher") or ""),
                source_url=str(form.get("source_url") or ""),
                published_at=str(form.get("published_at") or ""),
                rights_class=str(form.get("rights_class") or "user_provided"),
                principal_id=current_principal_id,
            )
            intake = queue_evidence_request(
                session, principal_id=current_principal_id, document_id=int(result["document_id"]),
                request_key=str(form.get("request_key") or ""),
                request_text=str(form.get("research_request") or ""),
            )
            return result, int(intake.id), intake.status

    try:
        result, request_id, intake_status = await run_in_threadpool(ingest)
    except ValueError as exc:
        status = 429 if "limit reached" in str(exc) else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    return _json({**{key: result[key] for key in ("document_id", "title", "page_count", "text_chars", "created", "document_url")},
                  "research_request_id": request_id, "research_request_status": intake_status},
                 status_code=201 if result["created"] else 200)


@app.get("/public/api/agents/requests/{request_id}")
def public_agent_research_request(request: Request, request_id: int) -> JSONResponse:
    principal_id = _authorize_and_charge(request, "evidence:upload")
    with session_scope() as session:
        row = session.query(ExternalAgentResearchRequest).filter_by(id=request_id, principal_id=principal_id).first()
        if row is None:
            raise HTTPException(status_code=404, detail="Research request not found")
        result = {"research_request_id": int(row.id), "document_id": int(row.document_id),
                  "status": row.status, "operator_note": row.operator_note,
                  "resolution_operation_key": row.resolution_operation_key}
    return _json(result)


@app.get("/public/api/agents/evidence/{document_id}")
def public_agent_evidence(request: Request, document_id: int) -> JSONResponse:
    principal_id = _authorize_and_charge(request, "evidence:upload")
    with session_scope() as session:
        document = _owned_document(session, principal_id=principal_id, document_id=document_id)
        extra = dict(document.extra or {})
        result = {"document_id": int(document.id), "title": document.title,
                  "publisher": extra.get("publisher"), "published_at": document.published_at.isoformat() if document.published_at else None,
                  "rights_class": extra.get("rights_class"),
                  "page_count": int((extra.get("pdf_structure") or {}).get("page_count") or 0),
                  "text_excerpt": str(document.normalized_text or "")[:12_000]}
    return _json(result)


@app.get("/public/api/agents/evidence/{document_id}/pages/{page_number}")
def public_agent_evidence_page(request: Request, document_id: int, page_number: int) -> JSONResponse:
    principal_id = _authorize_and_charge(request, "evidence:upload")
    with session_scope() as session:
        _owned_document(session, principal_id=principal_id, document_id=document_id)
        page = session.query(DocumentPage).filter_by(document_id=document_id, page_number=page_number).first()
        if page is None:
            raise HTTPException(status_code=404, detail="Evidence page not found")
        text = str(page.text or "")[:100_000]
    return _json({"document_id": document_id, "page_number": page_number, "text": text})


@app.get("/public/api/agents/evidence/{document_id}/pages/{page_number}/image")
def public_agent_evidence_page_image(request: Request, document_id: int, page_number: int) -> Response:
    if page_number < 1 or page_number > 500:
        raise HTTPException(status_code=404, detail="Evidence page not found")
    principal_id = _authorize_and_charge(request, "evidence:upload", render=True)
    with session_scope() as session:
        _owned_document(session, principal_id=principal_id, document_id=document_id)
        try:
            result = render_document_page(session, document_id=document_id, page_number=page_number, dpi=130)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(base64.b64decode(result["_image_data_base64"]), media_type="image/png",
                    headers={"Cache-Control": "private, max-age=300"})


@app.post("/public/api/agents/proposals")
async def public_agent_proposal(request: Request) -> JSONResponse:
    raw = await request.body()
    if len(raw) > 24_000:
        raise HTTPException(status_code=413, detail="Proposal body is too large")
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Proposal must be JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Proposal must be an object")
    principal_id = _authorize_and_charge(request, "proposals:write", proposal=True)
    with session_scope() as session:
        try:
            row = submit_external_model_proposal(
                session, principal_id=principal_id,
                request_key=body.get("request_key"), target_model_key=body.get("target_model_key"),
                document_ids=body.get("document_ids") or [], payload=body.get("payload"),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        result = {"proposal_id": int(row.id), "status": row.status,
                  "target_model_key": row.target_model_key, "request_key": row.request_key,
                  "operator_note": row.operator_note}
    return _json(result, status_code=202)


@app.get("/public/api/agents/proposals/{proposal_id}")
def public_agent_proposal_status(request: Request, proposal_id: int) -> JSONResponse:
    principal_id = _authorize_and_charge(request, "proposals:write")
    with session_scope() as session:
        row = session.query(ExternalAgentProposal).filter_by(id=proposal_id, principal_id=principal_id).first()
        if row is None:
            raise HTTPException(status_code=404, detail="Proposal not found")
        result = {"proposal_id": int(row.id), "status": row.status,
                  "target_model_key": row.target_model_key, "request_key": row.request_key,
                  "operator_note": row.operator_note}
    return _json(result)
