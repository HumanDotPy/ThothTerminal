from __future__ import annotations

from typing import Any, Callable

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from thoth_terminal.web.json_response import JSONResponse
from sqlalchemy.orm import Session

from thoth_terminal.db import get_session, session_factory, session_scope
from thoth_terminal.institutional_service import (
    enqueue_closure_pass,
    enqueue_institutional_refresh,
    get_bottleneck,
    get_program_payload,
    get_site_payload,
    institutional_ops_summary,
    list_gaps,
    list_observations,
    list_programs,
    list_sites,
)


def register_institutional_routes(
    app: FastAPI,
    *,
    template_response: Callable[[str, dict[str, Any]], HTMLResponse],
) -> None:
    @app.get("/institutional/ops", response_class=HTMLResponse)
    def institutional_ops_view(request: Request, db: Session = Depends(get_session)) -> HTMLResponse:
        return template_response(
            "institutional_ops.html",
            {
                "request": request,
                "summary": institutional_ops_summary(db),
            },
        )

    @app.get("/institutional/sites/{site_id}", response_class=HTMLResponse)
    def institutional_site_view(site_id: int, request: Request, db: Session = Depends(get_session)) -> HTMLResponse:
        payload = get_site_payload(db, site_id=int(site_id))
        if payload is None:
            return HTMLResponse("Not found", status_code=404)
        return template_response(
            "institutional_site.html",
            {
                "request": request,
                "payload": payload,
                "site": payload.get("site"),
            },
        )

    @app.get("/institutional/programs/{program_id}", response_class=HTMLResponse)
    def institutional_program_view(program_id: int, request: Request, db: Session = Depends(get_session)) -> HTMLResponse:
        payload = get_program_payload(db, program_id=int(program_id))
        if payload is None:
            return HTMLResponse("Not found", status_code=404)
        return template_response(
            "institutional_program.html",
            {
                "request": request,
                "payload": payload,
                "program": payload.get("program"),
            },
        )

    @app.get("/api/institutional/sites")
    def institutional_sites_api(
        query: str | None = None,
        subject_id: int | None = None,
        limit: int = 100,
        db: Session = Depends(get_session),
    ) -> JSONResponse:
        rows = list_sites(db, query=query, subject_id=subject_id, limit=limit)
        return JSONResponse({"count": len(rows), "sites": rows})

    @app.get("/api/institutional/programs")
    def institutional_programs_api(
        query: str | None = None,
        subject_id: int | None = None,
        site_id: int | None = None,
        limit: int = 100,
        db: Session = Depends(get_session),
    ) -> JSONResponse:
        rows = list_programs(db, query=query, subject_id=subject_id, site_id=site_id, limit=limit)
        return JSONResponse({"count": len(rows), "programs": rows})

    @app.get("/api/institutional/observations")
    def institutional_observations_api(
        subject_id: int | None = None,
        site_id: int | None = None,
        program_id: int | None = None,
        kind: str | None = None,
        limit: int = 100,
        db: Session = Depends(get_session),
    ) -> JSONResponse:
        rows = list_observations(db, subject_id=subject_id, site_id=site_id, program_id=program_id, kind=kind, limit=limit)
        return JSONResponse({"count": len(rows), "observations": rows})

    @app.get("/api/institutional/gaps")
    def institutional_gaps_api(
        subject_id: int | None = None,
        site_id: int | None = None,
        program_id: int | None = None,
        open_only: bool = True,
        limit: int = 100,
        db: Session = Depends(get_session),
    ) -> JSONResponse:
        rows = list_gaps(db, subject_id=subject_id, site_id=site_id, program_id=program_id, open_only=open_only, limit=limit)
        return JSONResponse({"count": len(rows), "gaps": rows})

    @app.get("/api/institutional/bottlenecks")
    def institutional_bottlenecks_api(
        program_id: int,
        db: Session = Depends(get_session),
    ) -> JSONResponse:
        row = get_bottleneck(db, program_id=int(program_id))
        if row is None:
            return JSONResponse({"bottleneck": None}, status_code=404)
        return JSONResponse({"bottleneck": row})

    @app.post("/api/institutional/refresh")
    def institutional_refresh_api(
        source_id: int | None = None,
        document_id: int | None = None,
        subject_id: int | None = None,
        force: bool = False,
    ) -> JSONResponse:
        with session_scope() as session:
            job = enqueue_institutional_refresh(
                session,
                source_id=source_id,
                document_id=document_id,
                subject_id=subject_id,
                force=force,
            )
        return JSONResponse(job)


def institutional_mcp_tool_definitions() -> list[dict[str, object]]:
    return [
        {
            "name": "list_sites",
            "description": "List institutional sites and facility records.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "subject_id": {"type": "integer"},
                    "limit": {"type": "integer"},
                },
            },
        },
        {
            "name": "get_site",
            "description": "Fetch one institutional site with observations and gaps.",
            "inputSchema": {
                "type": "object",
                "properties": {"site_id": {"type": "integer"}},
                "required": ["site_id"],
            },
        },
        {
            "name": "list_programs",
            "description": "List institutional programs and dependency graphs.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "subject_id": {"type": "integer"},
                    "site_id": {"type": "integer"},
                    "limit": {"type": "integer"},
                },
            },
        },
        {
            "name": "get_program",
            "description": "Fetch one institutional program with nodes, edges, and bottlenecks.",
            "inputSchema": {
                "type": "object",
                "properties": {"program_id": {"type": "integer"}},
                "required": ["program_id"],
            },
        },
        {
            "name": "list_observations",
            "description": "List institutional observations across subject, site, or program scopes.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "subject_id": {"type": "integer"},
                    "site_id": {"type": "integer"},
                    "program_id": {"type": "integer"},
                    "kind": {"type": "string"},
                    "limit": {"type": "integer"},
                },
            },
        },
        {
            "name": "list_gaps",
            "description": "List institutional coverage gaps and stale-data flags.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "subject_id": {"type": "integer"},
                    "site_id": {"type": "integer"},
                    "program_id": {"type": "integer"},
                    "open_only": {"type": "boolean"},
                    "limit": {"type": "integer"},
                },
            },
        },
        {
            "name": "get_bottleneck",
            "description": "Fetch the latest bottleneck assessment for a program.",
            "inputSchema": {
                "type": "object",
                "properties": {"program_id": {"type": "integer"}},
                "required": ["program_id"],
            },
        },
        {
            "name": "enqueue_institutional_refresh",
            "description": "Enqueue institutional evidence normalization and source/profile refresh.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "source_id": {"type": "integer"},
                    "document_id": {"type": "integer"},
                    "subject_id": {"type": "integer"},
                    "force": {"type": "boolean"},
                },
            },
        },
        {
            "name": "enqueue_closure_pass",
            "description": "Enqueue a program closure and bottleneck recompute pass.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "program_id": {"type": "integer"},
                    "subject_id": {"type": "integer"},
                    "site_id": {"type": "integer"},
                    "force": {"type": "boolean"},
                },
            },
        },
    ]


def handle_institutional_mcp_tool(tool_name: str, params: dict[str, object]) -> dict[str, object] | None:
    SessionLocal = session_factory()
    if tool_name == "enqueue_institutional_refresh":
        with session_scope() as session:
            return enqueue_institutional_refresh(
                session,
                source_id=_int_or_none(params.get("source_id")),
                document_id=_int_or_none(params.get("document_id")),
                subject_id=_int_or_none(params.get("subject_id")),
                force=bool(params.get("force")),
            )
    if tool_name == "enqueue_closure_pass":
        with session_scope() as session:
            return enqueue_closure_pass(
                session,
                program_id=_int_or_none(params.get("program_id")),
                subject_id=_int_or_none(params.get("subject_id")),
                site_id=_int_or_none(params.get("site_id")),
                force=bool(params.get("force")),
            )
    session = SessionLocal()
    try:
        if tool_name == "list_sites":
            rows = list_sites(
                session,
                query=str(params.get("query") or "").strip() or None,
                subject_id=_int_or_none(params.get("subject_id")),
                limit=_int_or_default(params.get("limit"), 100),
            )
            return {"count": len(rows), "sites": rows}
        if tool_name == "get_site":
            payload = get_site_payload(session, site_id=_require_int(params.get("site_id"), "site_id"))
            if payload is None:
                raise ValueError("Site not found.")
            return payload
        if tool_name == "list_programs":
            rows = list_programs(
                session,
                query=str(params.get("query") or "").strip() or None,
                subject_id=_int_or_none(params.get("subject_id")),
                site_id=_int_or_none(params.get("site_id")),
                limit=_int_or_default(params.get("limit"), 100),
            )
            return {"count": len(rows), "programs": rows}
        if tool_name == "get_program":
            payload = get_program_payload(session, program_id=_require_int(params.get("program_id"), "program_id"))
            if payload is None:
                raise ValueError("Program not found.")
            return payload
        if tool_name == "list_observations":
            rows = list_observations(
                session,
                subject_id=_int_or_none(params.get("subject_id")),
                site_id=_int_or_none(params.get("site_id")),
                program_id=_int_or_none(params.get("program_id")),
                kind=str(params.get("kind") or "").strip() or None,
                limit=_int_or_default(params.get("limit"), 100),
            )
            return {"count": len(rows), "observations": rows}
        if tool_name == "list_gaps":
            rows = list_gaps(
                session,
                subject_id=_int_or_none(params.get("subject_id")),
                site_id=_int_or_none(params.get("site_id")),
                program_id=_int_or_none(params.get("program_id")),
                open_only=bool(params.get("open_only", True)),
                limit=_int_or_default(params.get("limit"), 100),
            )
            return {"count": len(rows), "gaps": rows}
        if tool_name == "get_bottleneck":
            row = get_bottleneck(session, program_id=_require_int(params.get("program_id"), "program_id"))
            if row is None:
                raise ValueError("Bottleneck not found.")
            return {"bottleneck": row}
        return None
    finally:
        session.close()


def _int_or_none(value: object) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, int):
        return int(value)
    if str(value).isdigit():
        return int(value)
    return None


def _require_int(value: object, field: str) -> int:
    out = _int_or_none(value)
    if out is None:
        raise ValueError(f"Missing required integer field: {field}")
    return out


def _int_or_default(value: object, default: int) -> int:
    parsed = _int_or_none(value)
    return parsed if parsed is not None else int(default)
