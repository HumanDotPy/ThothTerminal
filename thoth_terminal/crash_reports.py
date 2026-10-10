from __future__ import annotations

import json
import os
import platform
import re
import sys
import threading
import traceback
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from thoth_terminal.config import settings
from thoth_terminal.runtime_env import repo_root
from thoth_terminal.runtime_processes import run_hidden_process


_CONTEXT_PROVIDER: Callable[[], dict[str, Any]] | None = None
_CURRENT_SESSION_ID: str | None = None
_HOOKS_INSTALLED = False
_PREVIOUS_SYS_EXCEPTHOOK = None
_PREVIOUS_THREADING_EXCEPTHOOK = None
_SESSION_LOCK = threading.RLock()

_SENSITIVE_KEY_RE = re.compile(r"(token|secret|password|passwd|api[_-]?key|authorization|cookie)", re.IGNORECASE)
_MAX_STRING = 12_000
_MAX_LOG_TAIL_BYTES = 64_000


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def crash_reports_dir() -> Path:
    path = Path(settings.runtime_state_dir) / "crashes"
    path.mkdir(parents=True, exist_ok=True)
    return path


def support_bundles_dir() -> Path:
    path = Path(settings.runtime_state_dir) / "support-bundles"
    path.mkdir(parents=True, exist_ok=True)
    return path


def desktop_session_path() -> Path:
    path = Path(settings.runtime_state_dir) / "desktop-session.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _git_line(args: list[str]) -> str | None:
    try:
        root = repo_root().resolve(strict=False)
        result = run_hidden_process(["git", "-C", str(root), *args], capture_output=True, text=True, timeout_s=3)
        if result.returncode != 0:
            return None
        line = (result.stdout or "").strip().splitlines()
        return line[0] if line else None
    except Exception:
        return None


def _runtime_summary() -> dict[str, Any]:
    head = _git_line(["rev-parse", "HEAD"])
    return {
        "pid": os.getpid(),
        "python": sys.version,
        "executable": sys.executable,
        "platform": platform.platform(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "git_branch": _git_line(["rev-parse", "--abbrev-ref", "HEAD"]),
        "git_head": head,
        "git_head_short": head[:12] if head else None,
        "data_dir": str(settings.data_dir),
        "runtime_state_dir": str(settings.runtime_state_dir),
        "db_path": str(settings.db_path),
        "database_url_configured": bool(str(getattr(settings, "database_url", "") or "").strip()),
    }


def _redact(value: Any, *, key: str | None = None, depth: int = 0) -> Any:
    if key and _SENSITIVE_KEY_RE.search(key):
        return "[redacted]"
    if depth > 8:
        return "[max-depth]"
    if isinstance(value, dict):
        return {str(k): _redact(v, key=str(k), depth=depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_redact(item, depth=depth + 1) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        if len(value) > _MAX_STRING:
            return value[:_MAX_STRING] + "...[truncated]"
        return value
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return str(value)


def _context() -> dict[str, Any]:
    if _CONTEXT_PROVIDER is None:
        return {}
    try:
        payload = _CONTEXT_PROVIDER() or {}
        return dict(payload) if isinstance(payload, dict) else {"context": str(payload)}
    except Exception as exc:
        return {"context_error": f"{type(exc).__name__}: {exc}"}


def _tail(path: Path, max_bytes: int = _MAX_LOG_TAIL_BYTES) -> str:
    try:
        if not path.exists():
            return ""
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            return handle.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


def _recent_log_tails(limit: int = 4) -> list[dict[str, str]]:
    logs_dir = Path(settings.runtime_state_dir) / "logs"
    if not logs_dir.exists():
        return []
    tails: list[dict[str, str]] = []
    try:
        logs = sorted(logs_dir.glob("*.log"), key=lambda item: item.stat().st_mtime, reverse=True)[:limit]
    except Exception:
        return []
    for log in logs:
        tail = _tail(log)
        if tail:
            tails.append({"name": log.name, "path": str(log), "tail": tail})
    return tails


def write_crash_report(
    kind: str,
    *,
    summary: str | None = None,
    exc_info: tuple[type[BaseException], BaseException, Any] | None = None,
    context: dict[str, Any] | None = None,
    severity: str = "error",
    include_logs: bool = True,
) -> dict[str, Any]:
    safe_kind = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(kind or "crash")).strip("-") or "crash"
    report_id = f"{_stamp()}-{os.getpid()}-{safe_kind}-{uuid.uuid4().hex[:8]}"
    path = crash_reports_dir() / f"{report_id}.json"
    payload: dict[str, Any] = {
        "schema_version": 1,
        "id": report_id,
        "kind": safe_kind,
        "severity": severity,
        "summary": summary or safe_kind,
        "created_at": _now_iso(),
        "runtime": _runtime_summary(),
        "session_id": _CURRENT_SESSION_ID,
        "context": {**_context(), **(context or {})},
    }
    if exc_info and exc_info[1] is not None:
        exc_type, exc, tb = exc_info
        payload["exception"] = {
            "type": f"{exc_type.__module__}.{exc_type.__name__}",
            "message": str(exc),
            "traceback": "".join(traceback.format_exception(exc_type, exc, tb)),
        }
    if include_logs:
        log_tails = _recent_log_tails()
        if log_tails:
            payload["log_tails"] = log_tails
    _write_json(path, _redact(payload))
    return {"id": report_id, "path": str(path), "kind": safe_kind, "created_at": payload["created_at"]}


def record_unclean_desktop_session_if_needed() -> dict[str, Any] | None:
    session_path = desktop_session_path()
    payload = _read_json(session_path)
    if not payload or payload.get("status") == "clean":
        return None
    if payload.get("last_crash_report"):
        return dict(payload["last_crash_report"])
    report = write_crash_report(
        "desktop_unclean_exit",
        summary="Previous desktop session did not shut down cleanly.",
        context={"previous_session": payload},
        severity="warning",
        include_logs=False,
    )
    payload["last_crash_report"] = report
    payload["reported_unclean_at"] = _now_iso()
    _write_json(session_path, _redact(payload))
    return report


def begin_desktop_session(context: dict[str, Any] | None = None) -> dict[str, Any]:
    global _CURRENT_SESSION_ID
    previous = record_unclean_desktop_session_if_needed()
    session_id = uuid.uuid4().hex
    now = _now_iso()
    payload: dict[str, Any] = {
        "schema_version": 1,
        "session_id": session_id,
        "status": "active",
        "pid": os.getpid(),
        "started_at": now,
        "updated_at": now,
        "phase": (context or {}).get("phase"),
        "context": context or {},
        "previous_unclean_report": previous,
    }
    with _SESSION_LOCK:
        _CURRENT_SESSION_ID = session_id
        _write_json(desktop_session_path(), _redact(payload))
    return payload


def update_desktop_session(**fields: Any) -> None:
    with _SESSION_LOCK:
        payload = _read_json(desktop_session_path()) or {}
        if not _CURRENT_SESSION_ID or payload.get("session_id") != _CURRENT_SESSION_ID:
            return
        payload.update(fields)
        payload["updated_at"] = _now_iso()
        _write_json(desktop_session_path(), _redact(payload))


def mark_desktop_session_clean(session_id: str | None = None) -> None:
    with _SESSION_LOCK:
        payload = _read_json(desktop_session_path()) or {}
        expected = session_id or _CURRENT_SESSION_ID
        if expected and payload.get("session_id") != expected:
            return
        payload["status"] = "clean"
        payload["finished_at"] = _now_iso()
        payload["updated_at"] = _now_iso()
        _write_json(desktop_session_path(), _redact(payload))


def install_crash_hooks(context_provider: Callable[[], dict[str, Any]] | None = None) -> None:
    global _CONTEXT_PROVIDER, _HOOKS_INSTALLED, _PREVIOUS_SYS_EXCEPTHOOK, _PREVIOUS_THREADING_EXCEPTHOOK
    _CONTEXT_PROVIDER = context_provider
    if _HOOKS_INSTALLED:
        return
    _PREVIOUS_SYS_EXCEPTHOOK = sys.excepthook

    def _sys_hook(exc_type, exc, tb):
        report = write_crash_report(
            "desktop_unhandled_exception",
            summary=str(exc),
            exc_info=(exc_type, exc, tb),
            severity="fatal",
        )
        update_desktop_session(status="crashed", last_crash_report=report)
        if callable(_PREVIOUS_SYS_EXCEPTHOOK):
            _PREVIOUS_SYS_EXCEPTHOOK(exc_type, exc, tb)

    sys.excepthook = _sys_hook

    if hasattr(threading, "excepthook"):
        _PREVIOUS_THREADING_EXCEPTHOOK = threading.excepthook

        def _thread_hook(args):
            report = write_crash_report(
                "desktop_thread_exception",
                summary=str(args.exc_value),
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
                context={"thread_name": getattr(getattr(args, "thread", None), "name", None)},
                severity="fatal",
            )
            update_desktop_session(status="crashed", last_crash_report=report)
            if callable(_PREVIOUS_THREADING_EXCEPTHOOK):
                _PREVIOUS_THREADING_EXCEPTHOOK(args)

        threading.excepthook = _thread_hook

    _HOOKS_INSTALLED = True


def recent_crash_reports(limit: int = 5) -> list[dict[str, Any]]:
    try:
        paths = sorted(crash_reports_dir().glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)[:limit]
    except Exception:
        paths = []
    recent: list[dict[str, Any]] = []
    for path in paths:
        payload = _read_json(path) or {}
        recent.append(
            {
                "id": payload.get("id") or path.stem,
                "kind": payload.get("kind"),
                "severity": payload.get("severity"),
                "summary": payload.get("summary"),
                "created_at": payload.get("created_at"),
                "path": str(path),
            }
        )
    return recent


def crash_status_payload() -> dict[str, Any]:
    recent = recent_crash_reports()
    try:
        count = len(list(crash_reports_dir().glob("*.json")))
    except Exception:
        count = 0
    return {
        "reports_dir": str(crash_reports_dir()),
        "support_bundles_dir": str(support_bundles_dir()),
        "session": _read_json(desktop_session_path()) or {},
        "latest": recent[0] if recent else None,
        "recent": recent,
        "report_count": count,
    }


def create_support_bundle(*, reason: str = "manual") -> dict[str, Any]:
    bundle_id = f"thoth-support-{_stamp()}-{uuid.uuid4().hex[:8]}"
    path = support_bundles_dir() / f"{bundle_id}.zip"
    reports = sorted(crash_reports_dir().glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)[:10]
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "summary.json",
            json.dumps(
                _redact({"created_at": _now_iso(), "reason": reason, "runtime": _runtime_summary()}),
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
        session_path = desktop_session_path()
        if session_path.exists():
            archive.write(session_path, "runtime/desktop-session.json")
        for report in reports:
            archive.write(report, f"crashes/{report.name}")
        for log_tail in _recent_log_tails(limit=8):
            archive.writestr(f"logs/{log_tail['name']}.tail.txt", log_tail["tail"])
    return {"ok": True, "bundle_id": bundle_id, "path": str(path), "report_count": len(reports)}
