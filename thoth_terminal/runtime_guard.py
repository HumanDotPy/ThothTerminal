from __future__ import annotations

import ctypes
import os
from pathlib import Path
from typing import Any

from thoth_terminal.db import configured_database_url, database_backend
from thoth_terminal.runtime_env import repo_root
from thoth_terminal.runtime_info import list_runtime_states


ALLOW_SQLITE_ENV = "THOTH_ALLOW_SQLITE_LIVE_RUNTIME"
ALLOW_DUPLICATE_ENV = "THOTH_ALLOW_DUPLICATE_THOTH_RUNTIME"
_DESKTOP_OWNED_SERVICES = {"desktop", "web", "worker", "cloud-worker", "local-agent-api"}
_WORKER_ALIASES = {"worker", "run"}


def _env_truthy(name: str) -> bool:
    return str(os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def pid_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        value = int(pid)
    except Exception:
        return False
    if value <= 0:
        return False
    if value == os.getpid():
        return True
    if os.name == "nt":
        try:
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.OpenProcess(0x00100000, False, value)
            if not handle:
                return False
            try:
                wait_timeout = 0x00000102
                return int(kernel32.WaitForSingleObject(handle, 0)) == wait_timeout
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(value, 0)
        return True
    except Exception:
        return False


def _state_service(state: dict[str, Any]) -> str:
    raw = str(state.get("service") or "").strip().lower()
    if raw:
        return raw
    path = str(state.get("_path") or "").strip()
    return Path(path).stem.lower() if path else ""


def _same_repo(state: dict[str, Any], root: Path) -> bool:
    raw = str(state.get("repo_root") or "").strip()
    if not raw:
        return True
    try:
        return Path(raw).resolve(strict=False) == root.resolve(strict=False)
    except Exception:
        return raw.casefold() == str(root).casefold()


def active_runtime_states(
    *,
    service: str | None = None,
    states: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    root = repo_root()
    wanted = str(service or "").strip().lower()
    if wanted == "run":
        wanted = "worker"
    out: list[dict[str, Any]] = []
    for state in states if states is not None else list_runtime_states():
        if not isinstance(state, dict):
            continue
        svc = _state_service(state)
        if wanted and svc != wanted:
            continue
        try:
            pid = int(state.get("pid") or 0)
        except Exception:
            pid = 0
        if pid == os.getpid() or not pid_alive(pid):
            continue
        if not _same_repo(state, root):
            continue
        copied = dict(state)
        copied["service"] = svc
        copied["pid"] = pid
        out.append(copied)
    return out


def assert_live_runtime_database(*, action: str) -> None:
    backend = database_backend(configured_database_url())
    if backend == "postgresql" or _env_truthy(ALLOW_SQLITE_ENV):
        return
    raise RuntimeError(
        f"{action} is a live Thoth runtime and must use the Docker/Postgres database. "
        f"Active backend is `{backend}`. Start local Postgres and set THOTH_DATABASE_URL, "
        f"or set {ALLOW_SQLITE_ENV}=1 only for an isolated smoke/test runtime."
    )


def assert_no_live_runtime_conflict(*, service: str, action: str) -> None:
    if _env_truthy(ALLOW_DUPLICATE_ENV):
        return
    service_key = str(service or "").strip().lower()
    if service_key == "run":
        service_key = "worker"
    states = active_runtime_states()

    desktop_rows = [
        row
        for row in states
        if row.get("desktop_managed") is True or str(row.get("service") or "") == "desktop"
    ]
    if service_key != "desktop" and desktop_rows:
        row = desktop_rows[0]
        raise RuntimeError(
            f"desktop-managed Thoth runtime is already active "
            f"(pid={row.get('pid')}, base_url={row.get('base_url') or 'unknown'}). "
            "Use the desktop/local-agent API surface, stop the desktop runtime, or start an isolated "
            f"test runtime with {ALLOW_DUPLICATE_ENV}=1."
        )

    if service_key == "desktop":
        conflicts = [
            row
            for row in states
            if str(row.get("service") or "") in _DESKTOP_OWNED_SERVICES
        ]
    elif service_key in _WORKER_ALIASES:
        conflicts = [
            row
            for row in states
            if str(row.get("service") or "") in _WORKER_ALIASES
        ]
    else:
        conflicts = [
            row
            for row in states
            if str(row.get("service") or "") == service_key
        ]
    if not conflicts:
        return
    details = ", ".join(
        f"{row.get('service')} pid={row.get('pid')} url={row.get('base_url') or 'unknown'}"
        for row in conflicts[:4]
    )
    raise RuntimeError(
        f"{action} would create a duplicate live Thoth runtime for `{service_key}`. "
        f"Active state: {details}. Stop/reuse the existing process, or use an isolated clone/runtime "
        f"with {ALLOW_DUPLICATE_ENV}=1."
    )
