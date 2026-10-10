from __future__ import annotations

import json
import os
import socket
import sys
from collections import OrderedDict
from functools import lru_cache
from pathlib import Path
from threading import Lock
from typing import Any

from thoth_terminal import __version__
from thoth_terminal.runtime_env import (
    BOOT_ID,
    STARTED_AT_UTC,
    assert_paths_not_on_forbidden_drives,
    detect_platform,
    normalize_path,
    repo_root,
)
from thoth_terminal.runtime_processes import run_hidden_process


def _settings_or_default(settings_obj: object | None):
    if settings_obj is not None:
        return settings_obj
    from thoth_terminal.config import settings

    return settings


@lru_cache(maxsize=8)
def _git_sha_cached(root_text: str) -> str | None:
    root_path = Path(root_text).resolve()
    return _git_sha_uncached(root_path)


def _git_sha_uncached(root_path: Path) -> str | None:
    try:
        proc = run_hidden_process(
            ["git", "-C", str(root_path), "rev-parse", "--short", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout_s=5,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    sha = (proc.stdout or "").strip()
    return sha or None


def get_git_sha(*, root: Path | None = None) -> str | None:
    root_path = (root or repo_root()).resolve()
    return _git_sha_cached(str(root_path))


_current_git_cache: OrderedDict[str, tuple[tuple[Any, ...], str]] = OrderedDict()
_current_git_cache_lock = Lock()


def _git_metadata_signature(root_path: Path) -> tuple[Any, ...] | None:
    """Watch file-backed refs; let Git resolve unsupported layouts on every call."""
    git_overrides = {
        "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_NAMESPACE",
        "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_SHALLOW_FILE",
        "GIT_REPLACE_REF_BASE", "GIT_NO_REPLACE_OBJECTS",
    }
    if any(name in git_overrides or name.startswith("GIT_CONFIG") for name in os.environ):
        return None
    try:
        marker = root_path / ".git"
        git_dir = marker
        marker_text = None
        if marker.is_file():
            marker_text = marker.read_text(encoding="utf-8").strip()
            if not marker_text.startswith("gitdir: "):
                return None
            git_dir = (root_path / marker_text[8:]).resolve()
        if not git_dir.is_dir():
            return None
        common_dir = git_dir
        common_text = None
        if (git_dir / "commondir").is_file():
            common_text = (git_dir / "commondir").read_text(encoding="utf-8").strip()
            common_dir = (git_dir / common_text).resolve()
        if (common_dir / "reftable").exists():
            return None

        def stamp(path: Path) -> tuple[Any, ...]:
            try:
                info = path.stat()
                return (str(path), info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            except FileNotFoundError:
                return (str(path), None)

        # Git's abbreviation can be configured independently of the checkout.
        config_paths = (
            common_dir / "config", git_dir / "config.worktree",
            Path.home() / ".gitconfig",
            Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "git" / "config",
        )
        config_stamps = []
        for path in config_paths:
            config_stamps.append(stamp(path))
            if path.is_file():
                text = path.read_text(encoding="utf-8").lower()
                # Includes and alternate ref backends need Git's own resolution.
                if "[include" in text or "refstorage" in text:
                    return None

        refs: list[tuple[str, str | None]] = []
        ref_path = git_dir / "HEAD"
        for _ in range(8):
            try:
                ref_text = ref_path.read_text(encoding="ascii").strip()
            except FileNotFoundError:
                refs.append((str(ref_path), None))
                break  # Git may resolve this ref from packed-refs.
            refs.append((str(ref_path), ref_text))
            if not ref_text.startswith("ref: "):
                break
            ref_name = ref_text[5:]
            if not ref_name.startswith("refs/") or ".." in ref_name.split("/"):
                return None
            private_ref = ref_name.startswith(("refs/bisect/", "refs/worktree/", "refs/rewritten/"))
            ref_path = (git_dir if private_ref else common_dir) / ref_name
        else:
            return None
        return (
            str(git_dir), marker_text, str(common_dir), common_text,
            tuple(refs), stamp(common_dir / "packed-refs"), tuple(config_stamps),
        )
    except (OSError, UnicodeError, ValueError):
        return None


def get_current_git_sha(*, root: Path | None = None) -> str | None:
    root_path = (root or repo_root()).resolve()
    signature = _git_metadata_signature(root_path)
    if signature is None:
        return _git_sha_uncached(root_path)
    root_text = str(root_path)
    with _current_git_cache_lock:
        cached = _current_git_cache.get(root_text)
        if cached is not None and cached[0] == signature:
            _current_git_cache.move_to_end(root_text)
            return cached[1]
    sha = _git_sha_uncached(root_path)
    # Never retain an error or associate a SHA with metadata changed during Git's read.
    if sha is not None and _git_metadata_signature(root_path) == signature:
        with _current_git_cache_lock:
            _current_git_cache[root_text] = (signature, sha)
            _current_git_cache.move_to_end(root_text)
            while len(_current_git_cache) > 8:
                _current_git_cache.popitem(last=False)
    return sha


@lru_cache(maxsize=8)
def _runtime_identity_cached(
    *,
    app_version: str,
    boot_id: str,
    started_at_utc: str,
    platform_name: str,
    python_executable: str,
    module_path: str,
    repo_root_path: str,
    git_sha: str | None,
    current_git_sha: str | None,
    hostname: str,
    pid: int,
    db_path: str,
    database_backend_name: str,
    database_url_configured: bool,
    data_dir: str,
    objects_dir: str,
    runtime_state_dir_path: str,
    runtime_primary_os: str,
    runtime_writer_policy: str,
    runtime_allow_unsafe_wsl_writes: bool,
    runtime_enforce_git_workspace: bool,
) -> dict[str, Any]:
    return {
        "app_version": app_version,
        "boot_id": boot_id,
        "started_at_utc": started_at_utc,
        "platform": platform_name,
        "python_executable": python_executable,
        "module_path": module_path,
        "repo_root": repo_root_path,
        "git_sha": git_sha,
        "current_git_sha": current_git_sha,
        "deployment_drift": {
            "stale_checkout": bool(git_sha and current_git_sha and git_sha != current_git_sha),
            "startup_git_sha": git_sha,
            "current_git_sha": current_git_sha,
        },
        "hostname": hostname,
        "pid": pid,
        "db_path": db_path,
        "database_backend": database_backend_name,
        "database_url_configured": database_url_configured,
        "data_dir": data_dir,
        "objects_dir": objects_dir,
        "runtime_state_dir": runtime_state_dir_path,
        "policy": {
            "runtime_primary_os": runtime_primary_os,
            "runtime_writer_policy": runtime_writer_policy,
            "runtime_allow_unsafe_wsl_writes": runtime_allow_unsafe_wsl_writes,
            "runtime_enforce_git_workspace": runtime_enforce_git_workspace,
        },
    }


def clear_runtime_info_cache() -> None:
    _git_sha_cached.cache_clear()
    with _current_git_cache_lock:
        _current_git_cache.clear()
    _runtime_identity_cached.cache_clear()


def get_runtime_identity(*, settings_obj: object | None = None) -> dict[str, Any]:
    settings = _settings_or_default(settings_obj)
    root = repo_root().resolve()
    module_path = Path(__file__).resolve().parent
    db_url = str(getattr(settings, "database_url", "") or "").strip()
    try:
        from thoth_terminal.db import database_backend

        database_backend_name = database_backend(db_url or None)
    except Exception:
        database_backend_name = "unknown"
    payload = _runtime_identity_cached(
        app_version=__version__,
        boot_id=BOOT_ID,
        started_at_utc=STARTED_AT_UTC,
        platform_name=detect_platform(),
        python_executable=sys.executable,
        module_path=str(module_path),
        repo_root_path=str(root),
        git_sha=get_git_sha(root=root),
        current_git_sha=get_current_git_sha(root=root),
        hostname=socket.gethostname(),
        pid=os.getpid(),
        db_path=str(getattr(settings, "db_path", "")),
        database_backend_name=database_backend_name,
        database_url_configured=bool(db_url),
        data_dir=str(getattr(settings, "data_dir", "")),
        objects_dir=str(getattr(settings, "objects_dir", "")),
        runtime_state_dir_path=str(getattr(settings, "runtime_state_dir", "")),
        runtime_primary_os=getattr(settings, "runtime_primary_os", "windows"),
        runtime_writer_policy=getattr(settings, "runtime_writer_policy", "windows_single_writer"),
        runtime_allow_unsafe_wsl_writes=bool(
            getattr(settings, "runtime_allow_unsafe_wsl_writes", False)
        ),
        runtime_enforce_git_workspace=bool(
            getattr(settings, "runtime_enforce_git_workspace", True)
        ),
    )
    return {**payload, "policy": dict(payload["policy"])}


def runtime_fingerprint(*, settings_obj: object | None = None) -> str:
    payload = get_runtime_identity(settings_obj=settings_obj)
    return (
        f"{payload.get('app_version')} "
        f"sha={payload.get('git_sha') or 'none'} "
        f"boot={payload.get('boot_id')} "
        f"py={payload.get('python_executable')}"
    )


def runtime_state_dir(*, settings_obj: object | None = None) -> Path:
    settings = _settings_or_default(settings_obj)
    path = normalize_path(getattr(settings, "runtime_state_dir", "data/runtime"))
    assert_paths_not_on_forbidden_drives(
        {"runtime_state_dir": path},
        forbidden_drives=getattr(settings, "forbidden_runtime_drives", ""),
        action="Thoth runtime-state access",
    )
    return path


def runtime_state_path(service: str, *, settings_obj: object | None = None) -> Path:
    name = "".join(ch for ch in str(service or "").strip().lower() if ch.isalnum() or ch in {"_", "-"})
    if not name:
        raise ValueError("Missing service name.")
    out_dir = runtime_state_dir(settings_obj=settings_obj)
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"{name}.json"


def gaming_mode_state_path(*, settings_obj: object | None = None) -> Path:
    out_dir = runtime_state_dir(settings_obj=settings_obj)
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / "gaming-mode.json"


def read_gaming_mode_state(*, settings_obj: object | None = None) -> dict[str, Any] | None:
    path = gaming_mode_state_path(settings_obj=settings_obj)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def gaming_mode_enabled(*, settings_obj: object | None = None) -> bool:
    payload = read_gaming_mode_state(settings_obj=settings_obj)
    return bool(payload and payload.get("enabled", True))


def write_runtime_state(
    *,
    service: str,
    data: dict[str, Any] | None = None,
    settings_obj: object | None = None,
) -> Path:
    payload = get_runtime_identity(settings_obj=settings_obj)
    payload["service"] = str(service)
    if data:
        payload.update(data)
    path = runtime_state_path(service, settings_obj=settings_obj)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    return path


def read_runtime_state(
    *,
    service: str,
    settings_obj: object | None = None,
) -> dict[str, Any] | None:
    path = runtime_state_path(service, settings_obj=settings_obj)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def list_runtime_states(*, settings_obj: object | None = None) -> list[dict[str, Any]]:
    state_dir = runtime_state_dir(settings_obj=settings_obj)
    if not state_dir.exists():
        return []
    out: list[dict[str, Any]] = []
    for item in sorted(state_dir.glob("*.json")):
        try:
            payload = json.loads(item.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(payload, dict):
            payload["_path"] = str(item)
            out.append(payload)
    return out
