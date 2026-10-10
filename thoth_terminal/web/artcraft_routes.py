"""Read-only local preview pages; editing stays on the authenticated MCP surface."""
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from thoth_terminal.config import settings
from thoth_terminal.security import build_security_posture

router = APIRouter()


def _local_preview(request: Request):
    if settings.public_build or build_security_posture(settings).public or request.url.hostname not in {"127.0.0.1", "localhost", "::1"} or not request.client or request.client.host not in {"127.0.0.1", "::1"}:
        raise HTTPException(404)


@router.get("/artcraft", dependencies=[Depends(_local_preview)])
def artcraft_page():
    return HTMLResponse((Path(__file__).parent / "artcraft_workbench.html").read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-store"})


@router.get("/api/artcraft/status", dependencies=[Depends(_local_preview)])
def artcraft_local_status():
    from thoth_terminal.artcraft_service import artcraft_status
    result = artcraft_status()
    # Preview UI needs capabilities and versions, not private binary locations.
    result.pop("lab", None)
    for app in result.get("apps", []):
        app.pop("cli", None)
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.get("/artcraft/preview/{workspace}/{filename}", dependencies=[Depends(_local_preview)])
def artcraft_local_preview(workspace: str, filename: str):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", workspace) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}\.(png|jpg|jpeg)", filename):
        raise HTTPException(404)
    base = (settings.data_dir / "artcraft").resolve()
    root = (base / workspace).resolve()
    image = (root / filename).resolve()
    if not root.is_relative_to(base) or not image.is_relative_to(root) or not image.is_file() or image.stat().st_size > 8 * 1024 * 1024:
        raise HTTPException(404)
    return FileResponse(image, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
