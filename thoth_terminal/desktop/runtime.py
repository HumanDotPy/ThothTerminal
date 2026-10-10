from __future__ import annotations

import os
import sqlite3
import shutil
import subprocess
import sys
from pathlib import Path

from thoth_terminal.config import ensure_data_dirs, settings
from thoth_terminal.db import database_backend
from thoth_terminal.runtime_env import detect_platform, normalize_path, repo_root
from thoth_terminal.runtime_info import write_runtime_state


APP_NAME = "ThothTerminal"
DEFAULT_DB_NAME = "thoth_clean.db"


def _desktop_user_root() -> Path:
    platform_kind = detect_platform()
    home = Path.home()
    if platform_kind == "windows":
        base = Path(os.getenv("APPDATA") or (home / "AppData" / "Roaming"))
        return base / APP_NAME
    if platform_kind == "mac":
        return home / "Library" / "Application Support" / APP_NAME
    return Path(os.getenv("XDG_DATA_HOME") or (home / ".local" / "share")) / APP_NAME


def _should_rehome_runtime() -> bool:
    root = repo_root().resolve()
    return bool(getattr(sys, "frozen", False)) or (not (root / ".git").exists())


def _apply_path_setting(name: str, attr: str, value: Path) -> None:
    if os.getenv(name):
        return
    setattr(settings, attr, normalize_path(value, base_dir=value.parent))


def _env_present(*names: str) -> bool:
    return any(os.getenv(name) is not None for name in names)


def _default_playwright_browsers_path() -> Path:
    platform_kind = detect_platform()
    home = Path.home()
    if platform_kind == "windows":
        base = Path(os.getenv("LOCALAPPDATA") or (home / "AppData" / "Local"))
        return base / "ms-playwright"
    if platform_kind == "mac":
        return home / "Library" / "Caches" / "ms-playwright"
    return Path(os.getenv("XDG_CACHE_HOME") or (home / ".cache")) / "ms-playwright"


def _configure_playwright_runtime() -> None:
    current = os.getenv("PLAYWRIGHT_BROWSERS_PATH")
    if current and current != "0":
        return
    # Playwright's PyInstaller path defaults to package-local ".local-browsers".
    # For the desktop app, use the normal user browser cache so packaged builds
    # can reuse browsers installed by `python -m playwright install chromium`.
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(_default_playwright_browsers_path())


def _apply_bool_setting(names: tuple[str, ...], attr: str, value: bool) -> None:
    if _env_present(*names):
        return
    setattr(settings, attr, bool(value))


def _candidate_seed_databases() -> list[Path]:
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve(strict=False)
        for parent in exe.parents:
            candidates.append(parent / "data" / DEFAULT_DB_NAME)
    candidates.append(repo_root().resolve(strict=False) / "data" / DEFAULT_DB_NAME)
    out: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        resolved = path.resolve(strict=False)
        if resolved in seen:
            continue
        seen.add(resolved)
        out.append(resolved)
    return out


def _table_count(db_path: Path, table: str) -> int:
    if not db_path.exists():
        return 0
    try:
        with sqlite3.connect(str(db_path)) as conn:
            return int(conn.execute(f"select count(*) from {table}").fetchone()[0])
    except Exception:
        return 0


def _desktop_db_needs_seed(db_path: Path) -> bool:
    if not db_path.exists():
        return True
    object_count = sum(_table_count(db_path, table) for table in ("datasets", "notes", "theses"))
    return object_count == 0


def _seed_desktop_database_if_empty() -> None:
    if str(getattr(settings, "database_url", "") or "").strip():
        return
    if _env_present("THOTH_DB_PATH"):
        return
    target = Path(settings.db_path).resolve(strict=False)
    if not _desktop_db_needs_seed(target):
        return
    for seed in _candidate_seed_databases():
        if seed == target or not seed.exists():
            continue
        if sum(_table_count(seed, table) for table in ("datasets", "notes", "theses")) <= 0:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            backup = target.with_name(f"{target.name}.preseed")
            attempt = 0
            while backup.exists():
                attempt += 1
                backup = target.with_name(f"{target.name}.preseed.{attempt}")
            shutil.copy2(target, backup)
        with sqlite3.connect(str(seed)) as source, sqlite3.connect(str(target)) as destination:
            source.backup(destination)
        return


def configure_desktop_runtime() -> Path:
    _configure_playwright_runtime()
    if _should_rehome_runtime():
        user_root = _desktop_user_root()
        data_dir = user_root / "data"
        _apply_path_setting("THOTH_DATA_DIR", "data_dir", data_dir)
        _apply_path_setting("THOTH_DB_PATH", "db_path", data_dir / DEFAULT_DB_NAME)
        _apply_path_setting("THOTH_DUCKDB_PATH", "duckdb_path", data_dir / "analytics.duckdb")
        _apply_path_setting("THOTH_OBJECTS_DIR", "objects_dir", data_dir / "objects")
        _apply_path_setting("THOTH_RUNTIME_STATE_DIR", "runtime_state_dir", data_dir / "runtime")
        _apply_path_setting(
            "THOTH_GOOGLE_OAUTH_CLIENT_SECRETS_PATH",
            "google_oauth_client_secrets_path",
            data_dir / "integrations" / "google" / "client_secrets.json",
        )
        _apply_path_setting(
            "THOTH_GOOGLE_OAUTH_TOKEN_PATH",
            "google_oauth_token_path",
            data_dir / "integrations" / "google" / "oauth_token.json",
        )
        _apply_bool_setting(("THOTH_ENABLE_IBKR", "ENABLE_IBKR"), "enable_ibkr", True)
        _seed_desktop_database_if_empty()
    settings.runtime_enforce_git_workspace = False
    settings.runtime_require_project_venv = False
    settings.runtime_primary_os = detect_platform()
    settings.runtime_writer_policy = "desktop_single_process"
    ensure_data_dirs()
    desktop_logs_dir().mkdir(parents=True, exist_ok=True)
    return Path(settings.data_dir)


def desktop_logs_dir() -> Path:
    return Path(settings.runtime_state_dir) / "logs"


def open_path_in_file_manager(path: Path) -> None:
    target = Path(path).resolve(strict=False)
    platform_kind = detect_platform()
    if platform_kind == "windows":
        os.startfile(str(target))  # type: ignore[attr-defined]
        return
    if platform_kind == "mac":
        subprocess.Popen(["open", str(target)])
        return
    subprocess.Popen(["xdg-open", str(target)])


def write_desktop_runtime_state(
    *,
    base_url: str,
    worker_enabled: bool,
    agent_api_base_url: str | None = None,
    agent_api_enabled: bool = False,
    web_pid: int | None = None,
    worker_pids: list[int] | None = None,
    agent_api_pid: int | None = None,
) -> None:
    shared = {
        "desktop_managed": True,
        "base_url": base_url,
        "agent_api_enabled": bool(agent_api_enabled),
        "agent_api_base_url": agent_api_base_url,
        "database_backend": database_backend(),
        "database_url_configured": bool(str(getattr(settings, "database_url", "") or "").strip()),
        "db_path": str(settings.db_path),
        "playwright_browsers_path": os.getenv("PLAYWRIGHT_BROWSERS_PATH"),
    }
    write_runtime_state(service="desktop", data=shared | {"worker_enabled": worker_enabled})
    write_runtime_state(service="web", data=shared | ({"pid": int(web_pid)} if web_pid else {}))
    if agent_api_enabled:
        write_runtime_state(
            service="local-agent-api",
            data=shared | ({"pid": int(agent_api_pid)} if agent_api_pid else {}),
        )
    if worker_enabled:
        child_pids = [int(pid) for pid in (worker_pids or []) if int(pid or 0) > 0]
        write_runtime_state(
            service="worker",
            data=shared | ({"pid": child_pids[0], "child_pids": child_pids} if child_pids else {}),
        )
