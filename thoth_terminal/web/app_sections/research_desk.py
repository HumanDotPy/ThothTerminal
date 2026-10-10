from pathlib import Path

from fastapi import Depends, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session

from thoth_terminal.research_desk_service import get_research_change, get_research_desk
from thoth_terminal.web.app_sections._shared import bind_app_context

bind_app_context(globals())


def _desk_operator():
    return not build_security_posture(settings).public


@app.get("/research", response_class=HTMLResponse)
def research_desk_page():
    return HTMLResponse((Path(__file__).resolve().parents[1] / "research_desk.html").read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-store"})


@app.get("/api/research-desk")
def research_desk_api(mode: str = "minimal", limit: int = 20, before_id: str | None = None,
                      db: Session = Depends(get_session)):
    try:
        result = get_research_desk(db, mode=mode, limit=limit, before_id=before_id, operator=_desk_operator())
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@app.get("/api/research-desk/changes/{change_id}")
def research_desk_change_api(change_id: str, db: Session = Depends(get_session)):
    try:
        result = get_research_change(db, change_id, operator=_desk_operator())
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return JSONResponse(result, headers={"Cache-Control": "no-store"})
