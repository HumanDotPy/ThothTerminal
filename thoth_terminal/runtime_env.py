from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable, Literal, Mapping

from thoth_terminal.runtime_processes import run_hidden_process


RuntimePlatform = Literal["windows", "wsl", "linux", "mac", "unknown"]

BOOT_ID = uuid.uuid4().hex[:12]
STARTED_AT_UTC = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_MNT_DRIVE_RE = re.compile(r"^/mnt/([A-Za-z])/(.*)$")
_LOGGING_CONFIGURED = False
_COMMON_WINDOWS_GIT_PATHS = (
    Path("C:/Program Files/Git/cmd/git.exe"),
    Path("C:/Program Files/Git/bin/git.exe"),
    Path("C:/Program Files (x86)/Git/cmd/git.exe"),
    Path("C:/Program Files (x86)/Git/bin/git.exe"),
)


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def detect_platform() -> RuntimePlatform:
    system = platform.system().lower()
    if system == "windows":
        return "windows"
    if system == "darwin":
        return "mac"
    if system == "linux":
        rel = (platform.release() or "").lower()
        if "microsoft" in rel:
            return "wsl"
        try:
            proc_ver = Path("/proc/version").read_text(encoding="utf-8", errors="ignore").lower()
            if "microsoft" in proc_ver:
                return "wsl"
        except Exception:
            pass
        return "linux"
    return "unknown"


def _windows_to_wsl_path(raw: str) -> str:
    text = raw.replace("\\", "/")
    if not _WINDOWS_DRIVE_RE.match(text):
        return raw
    drive = text[0].lower()
    tail = text[2:].lstrip("/")
    return f"/mnt/{drive}/{tail}"


def _wsl_to_windows_path(raw: str) -> str:
    match = _MNT_DRIVE_RE.match(raw)
    if not match:
        return raw
    drive = match.group(1).upper()
    tail = match.group(2).replace("/", "\\")
    return f"{drive}:\\{tail}"


def normalize_path(path_value: str | Path, *, base_dir: Path | None = None) -> Path:
    if isinstance(path_value, Path):
        raw = str(path_value)
    else:
        raw = str(path_value or "")
    raw = raw.strip().strip('"').strip("'")
    base = base_dir or repo_root()

    platform_kind = detect_platform()
    if platform_kind == "wsl" and _WINDOWS_DRIVE_RE.match(raw):
        raw = _windows_to_wsl_path(raw)
        if os.name == "nt":
            return PurePosixPath(raw)  # type: ignore[return-value]
    elif platform_kind == "windows" and raw.startswith("/mnt/"):
        raw = _wsl_to_windows_path(raw)
    if _WINDOWS_DRIVE_RE.match(raw):
        # On non-Windows runtimes, pathlib treats drive paths as relative.
        # Keep them literal/absolute for downstream tools and diagnostics.
        return Path(raw)

    path = Path(raw).expanduser() if raw else base
    if not path.is_absolute():
        path = base / path
    return path.resolve(strict=False)


def resolve_migrated_repo_path(path_value: str | Path, *, root: Path | None = None) -> Path:
    """Relocate a missing path from an older checkout of this repository."""
    current_root = (root or repo_root()).resolve(strict=False)
    candidate = normalize_path(path_value, base_dir=current_root)
    if candidate.exists():
        return candidate

    raw = str(path_value or "").strip().strip('"').strip("'").replace("\\", "/")
    parts = [part for part in raw.split("/") if part]
    root_name = current_root.name.casefold()
    root_indexes = [index for index, part in enumerate(parts) if part.casefold() == root_name]
    if not root_indexes:
        return candidate

    relative_parts = parts[root_indexes[-1] + 1 :]
    if not relative_parts or any(part in {".", ".."} for part in relative_parts):
        return candidate
    relocated = current_root.joinpath(*relative_parts).resolve(strict=False)
    return relocated if relocated.exists() else candidate


def path_drive(path_value: str | Path) -> str:
    raw = str(path_value or "").strip().strip('"').strip("'")
    if _WINDOWS_DRIVE_RE.match(raw):
        return f"{raw[0].upper()}:"
    match = _MNT_DRIVE_RE.match(raw)
    if match:
        return f"{match.group(1).upper()}:"
    return Path(raw).drive.upper()


def normalize_forbidden_drives(values: str | Iterable[str] | None) -> set[str]:
    if values is None:
        return set()
    if isinstance(values, str):
        parts = re.split(r"[,;]\s*|\s+", values.strip())
    else:
        parts = [str(value).strip() for value in values]
    drives: set[str] = set()
    for part in parts:
        value = part.strip().strip('"').strip("'")
        if not value:
            continue
        if len(value) == 1 and value.isalpha():
            drives.add(f"{value.upper()}:")
        elif re.match(r"^[A-Za-z]:?$", value):
            drives.add(f"{value[0].upper()}:")
        else:
            drive = path_drive(value)
            if drive:
                drives.add(drive.upper())
    return drives


def assert_paths_not_on_forbidden_drives(
    paths: Mapping[str, str | Path],
    *,
    forbidden_drives: str | Iterable[str] | None,
    action: str,
) -> None:
    drives = normalize_forbidden_drives(forbidden_drives)
    if not drives:
        return
    violations: list[str] = []
    for label, path in paths.items():
        drive = path_drive(path)
        if drive.upper() in drives:
            violations.append(f"{label}={path}")
    if violations:
        blocked = ", ".join(sorted(drives))
        details = "; ".join(violations)
        raise RuntimeError(f"{action} blocked: configured Thoth path is on forbidden drive(s) {blocked}: {details}")


def configure_logging(level: str | None = None) -> None:
    global _LOGGING_CONFIGURED
    if _LOGGING_CONFIGURED:
        return
    level_name = str(level or os.getenv("THOTH_LOG_LEVEL", "INFO")).strip().upper() or "INFO"
    level_value = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level_value,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    _LOGGING_CONFIGURED = True


def resolve_git_executable() -> str:
    configured = str(os.getenv("THOTH_GIT") or os.getenv("GIT_EXE") or "").strip()
    if configured:
        return configured
    git_exe = shutil.which("git")
    if git_exe:
        return git_exe
    for candidate in _COMMON_WINDOWS_GIT_PATHS:
        if candidate.exists():
            return str(candidate)
    return "git"


def git_workspace_ok(root: Path | None = None) -> tuple[bool, str]:
    root_path = (root or repo_root()).resolve()
    dot_git = root_path / ".git"
    if not dot_git.exists():
        return False, f"Missing git metadata at {dot_git}."
    git_exe = resolve_git_executable()
    try:
        result = run_hidden_process(
            [git_exe, "-C", str(root_path), "rev-parse", "--is-inside-work-tree"],
            check=False,
            capture_output=True,
            text=True,
            timeout_s=5,
        )
    except Exception as exc:
        return False, f"Failed to run git check: {type(exc).__name__}: {exc}"
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip() or "unknown git error"
        return False, f"git rev-parse failed: {err}"
    if (result.stdout or "").strip().lower() != "true":
        return False, "git rev-parse did not report a work tree."
    return True, "ok"


def assert_git_workspace(*, enforce: bool, root: Path | None = None) -> None:
    if not enforce:
        return
    ok, detail = git_workspace_ok(root)
    if ok:
        return
    raise RuntimeError(
        "Git workspace validation failed. "
        f"{detail} "
        "Remediation: re-clone the repository or restore the `.git` directory."
    )


def require_writer_runtime(
    *,
    action: str,
    primary_os: str = "windows",
    writer_policy: str = "windows_single_writer",
    allow_unsafe_writes: bool = False,
) -> None:
    policy = (writer_policy or "").strip().lower()
    primary = (primary_os or "").strip().lower()
    platform_kind = detect_platform()
    if allow_unsafe_writes:
        return
    if policy != "windows_single_writer":
        return
    if primary != "windows":
        return
    if platform_kind == "windows":
        return
    if platform_kind == "wsl":
        raise RuntimeError(
            f"{action} blocked on WSL by runtime policy `windows_single_writer`. "
            "Use Windows runtime for direct writes, or mutate via MCP/web endpoints. "
            "Override only for emergencies with THOTH_RUNTIME_ALLOW_UNSAFE_WRITES=true."
        )
    raise RuntimeError(
        f"{action} blocked on `{platform_kind}` by runtime policy `windows_single_writer`. "
        "Use Windows runtime for direct writes."
    )
