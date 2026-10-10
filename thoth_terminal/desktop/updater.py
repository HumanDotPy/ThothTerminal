from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from thoth_terminal.runtime_processes import run_hidden_process
from thoth_terminal.runtime_env import STARTED_AT_UTC, repo_root, resolve_git_executable


class DesktopUpdateError(RuntimeError):
    pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _short_sha(value: str | None) -> str | None:
    text = str(value or "").strip()
    return text[:12] if text else None


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout_s: float = 30.0,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.setdefault("PIP_DISABLE_PIP_VERSION_CHECK", "1")
    result = run_hidden_process(
        [str(part) for part in command],
        cwd=str(cwd),
        env=env,
        check=False,
        timeout_s=timeout_s,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip() or f"exit={result.returncode}"
        raise DesktopUpdateError(f"{command[0]} failed: {detail}")
    return result


def _git(
    root: Path,
    args: Sequence[str],
    *,
    timeout_s: float = 30.0,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return _run([resolve_git_executable(), "-C", str(root), *args], cwd=root, timeout_s=timeout_s, check=check)


def _line(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stdout or "").strip().splitlines()[0].strip() if (result.stdout or "").strip() else ""


def _tail(value: str | None, *, lines: int = 20) -> str:
    return "\n".join((value or "").splitlines()[-lines:])


def _started_at_timestamp() -> float | None:
    try:
        return datetime.fromisoformat(STARTED_AT_UTC.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _porcelain_path(line: str) -> str:
    text = str(line or "").rstrip()
    if not text.strip():
        return ""
    path = text[3:].strip() if len(text) > 3 else text
    if " -> " in path:
        path = path.rsplit(" -> ", 1)[-1].strip()
    return path.strip('"')


def _runtime_relevant_path(path: str) -> bool:
    rel = str(path or "").replace("\\", "/").lstrip("/")
    if not rel:
        return False
    runtime_files = {
        "pyproject.toml",
        "setup.cfg",
        "setup.py",
        "requirements.txt",
        "requirements-dev.txt",
        "uv.lock",
        "poetry.lock",
    }
    if rel in runtime_files:
        return True
    return rel.startswith("thoth_terminal/") or rel.startswith("scripts/")


def _runtime_source_candidates(path: Path) -> list[Path]:
    if not path.exists():
        return []
    if path.is_file():
        if path.suffix.lower() in {".pyc", ".pyo"}:
            return []
        return [path]
    if not path.is_dir():
        return []
    candidates: list[Path] = []
    for child in path.rglob("*"):
        if "__pycache__" in child.parts:
            continue
        if not child.is_file():
            continue
        if child.suffix.lower() in {".pyc", ".pyo"}:
            continue
        candidates.append(child)
    return candidates


def _local_source_change_status(root: Path, dirty_lines: Sequence[str]) -> dict[str, object]:
    started_ts = _started_at_timestamp()
    changed: list[str] = []
    relevant_count = 0
    for line in dirty_lines:
        rel = _porcelain_path(line)
        if not _runtime_relevant_path(rel):
            continue
        relevant_count += 1
        candidate = root / rel
        if not candidate.exists():
            # Deleted files stay in `git status` until committed/restored, but after
            # a restart there is no filesystem mtime to prove a new post-start
            # source change. Do not keep the restart banner alive forever for them.
            continue
        changed_since_start = True
        if started_ts is not None:
            source_candidates = _runtime_source_candidates(candidate)
            if not source_candidates:
                changed_since_start = False
            else:
                changed_since_start = False
                for source_candidate in source_candidates:
                    try:
                        if source_candidate.stat().st_mtime > started_ts:
                            changed_since_start = True
                            break
                    except OSError:
                        changed_since_start = True
                        break
        if changed_since_start:
            changed.append(line)

    preview = changed[:8]
    return {
        "source_dirty_count": relevant_count,
        "source_change_count": len(changed),
        "source_change_preview": preview,
        "restart_available": bool(changed),
        "can_restart": bool(changed),
        "restart_reason": (
            "Runtime source files changed after this desktop process started; restart Thoth to load them."
            if changed
            else None
        ),
        "restart_token": "|".join(preview),
    }


def _powershell_executable() -> str:
    configured = str(os.environ.get("THOTH_POWERSHELL") or "").strip()
    if configured:
        return configured
    return "powershell.exe" if os.name == "nt" else "pwsh"


def _resolve_upstream(root: Path, branch: str) -> tuple[str | None, str | None]:
    result = _git(
        root,
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        check=False,
    )
    if result.returncode == 0:
        upstream = _line(result)
        if upstream:
            return upstream, None
    if branch and branch != "HEAD":
        candidate = f"origin/{branch}"
        verify = _git(root, ["rev-parse", "--verify", candidate], check=False)
        if verify.returncode == 0:
            return candidate, None
    remote = _git(root, ["remote"], check=False)
    if remote.returncode != 0 or not _line(remote):
        return None, "No git remote is configured."
    return None, "No upstream branch is configured for this checkout."


def check_local_update(*, root: Path | None = None, fetch: bool = False) -> dict[str, object]:
    workspace = (root or repo_root()).resolve(strict=False)
    if getattr(sys, "frozen", False):
        return {
            "state": "unsupported",
            "supported": False,
            "checked_at": _now_iso(),
            "reason": "Packaged builds do not have source git metadata. Use the source checkout updater.",
        }
    if not (workspace / ".git").exists():
        return {
            "state": "unsupported",
            "supported": False,
            "checked_at": _now_iso(),
            "root": str(workspace),
            "reason": f"Missing git metadata at {workspace / '.git'}.",
        }

    try:
        top_level = Path(_line(_git(workspace, ["rev-parse", "--show-toplevel"]))).resolve(strict=False)
        branch = _line(_git(top_level, ["rev-parse", "--abbrev-ref", "HEAD"]))
        head = _line(_git(top_level, ["rev-parse", "HEAD"]))
        upstream, upstream_error = _resolve_upstream(top_level, branch)
        if fetch and upstream:
            _git(top_level, ["fetch", "--quiet", "--prune"], timeout_s=120)

        dirty_raw = _git(top_level, ["status", "--porcelain"], timeout_s=15).stdout or ""
        dirty_lines = [line for line in dirty_raw.splitlines() if line.strip()]
        local_source = _local_source_change_status(top_level, dirty_lines)
        restart_available = bool(local_source.get("restart_available"))

        if not upstream:
            return {
                "state": "restart_available" if restart_available else "blocked",
                "supported": True,
                "checked_at": _now_iso(),
                "root": str(top_level),
                "branch": branch,
                "head": head,
                "head_short": _short_sha(head),
                "dirty": bool(dirty_lines),
                "dirty_count": len(dirty_lines),
                "dirty_preview": dirty_lines[:8],
                **local_source,
                "update_available": False,
                "can_update": False,
                "blocked_reason": upstream_error or "No upstream branch is configured.",
            }

        upstream_sha = _line(_git(top_level, ["rev-parse", upstream]))
        counts = _line(_git(top_level, ["rev-list", "--left-right", "--count", f"HEAD...{upstream}"]))
        ahead, behind = (0, 0)
        if counts:
            parts = counts.split()
            if len(parts) >= 2:
                ahead, behind = int(parts[0]), int(parts[1])

        blocked_reason = None
        if ahead and behind:
            blocked_reason = "Local branch has diverged from upstream."
        elif ahead:
            blocked_reason = "Local branch has commits that are not on upstream."
        state = "available" if behind else ("restart_available" if restart_available else "current")
        if blocked_reason:
            state = "blocked" if behind else ("restart_available" if restart_available else "current")

        return {
            "state": state,
            "supported": True,
            "checked_at": _now_iso(),
            "root": str(top_level),
            "branch": branch,
            "upstream": upstream,
            "head": head,
            "head_short": _short_sha(head),
            "upstream_head": upstream_sha,
            "upstream_head_short": _short_sha(upstream_sha),
            "ahead": ahead,
            "behind": behind,
            "dirty": bool(dirty_lines),
            "dirty_count": len(dirty_lines),
            "dirty_preview": dirty_lines[:8],
            **local_source,
            "dirty_warning": (
                "Uncommitted files are present; update will proceed only if Git can fast-forward "
                "without overwriting them."
                if dirty_lines
                else None
            ),
            "update_available": bool(behind > 0),
            "can_update": bool(behind > 0 and not blocked_reason),
            "blocked_reason": blocked_reason,
        }
    except DesktopUpdateError as exc:
        return {
            "state": "error",
            "supported": False,
            "checked_at": _now_iso(),
            "root": str(workspace),
            "error": str(exc),
        }
    except Exception as exc:
        return {
            "state": "error",
            "supported": False,
            "checked_at": _now_iso(),
            "root": str(workspace),
            "error": f"{type(exc).__name__}: {exc}",
        }


def repair_desktop_integration(
    *,
    root: Path | None = None,
    launch_at_login: bool = False,
    launch: bool = False,
    use_bundled_exe: bool = False,
    skip_dependency_install: bool = False,
) -> dict[str, object]:
    workspace = (root or repo_root()).resolve(strict=False)
    script = workspace / "scripts" / "install-thoth-desktop.ps1"
    if not script.exists():
        raise DesktopUpdateError(f"Desktop repair script was not found at {script}.")

    command = [
        _powershell_executable(),
        "-NoProfile",
    ]
    if os.name == "nt":
        command.extend(["-WindowStyle", "Hidden"])
    command.extend(["-ExecutionPolicy", "Bypass", "-File", str(script)])
    if skip_dependency_install:
        command.append("-SkipDependencyInstall")
    if launch_at_login:
        command.append("-LaunchAtLogin")
    if not launch:
        command.append("-NoLaunch")
    if use_bundled_exe:
        command.append("-UseBundledExe")

    result = _run(command, cwd=workspace, timeout_s=900)
    return {
        "ok": True,
        "repaired": True,
        "checked_at": _now_iso(),
        "root": str(workspace),
        "launch_at_login": bool(launch_at_login),
        "launched": bool(launch),
        "use_bundled_exe": bool(use_bundled_exe),
        "skip_dependency_install": bool(skip_dependency_install),
        "returncode": result.returncode,
        "stdout_tail": _tail(result.stdout),
        "stderr_tail": _tail(result.stderr),
    }


def apply_local_update(
    *,
    root: Path | None = None,
    python_exe: str | None = None,
    reinstall: bool = True,
) -> dict[str, object]:
    workspace = (root or repo_root()).resolve(strict=False)
    status = check_local_update(root=workspace, fetch=True)
    if not status.get("supported"):
        raise DesktopUpdateError(str(status.get("reason") or status.get("error") or "Updater is unavailable."))
    if status.get("blocked_reason"):
        raise DesktopUpdateError(str(status["blocked_reason"]))
    if not status.get("update_available"):
        return {
            "ok": True,
            "updated": False,
            "checked_at": _now_iso(),
            "status": status,
        }

    upstream = str(status.get("upstream") or "").strip()
    if not upstream:
        raise DesktopUpdateError("No upstream branch is configured.")

    before = str(status.get("head") or "")
    try:
        merge = _git(workspace, ["merge", "--ff-only", upstream], timeout_s=180)
    except DesktopUpdateError as exc:
        raise DesktopUpdateError(
            "Git could not fast-forward without risking local work. Commit, stash, or move the "
            f"conflicting files shown by git status, then retry. {exc}"
        ) from exc
    after = _line(_git(workspace, ["rev-parse", "HEAD"]))
    install_payload: dict[str, object] | None = None
    if reinstall:
        script = workspace / "scripts" / "install-thoth-desktop.ps1"
        if script.exists():
            install_payload = repair_desktop_integration(root=workspace, launch=False)
        else:
            exe = str(python_exe or sys.executable)
            install = _run(
                [exe, "-m", "pip", "install", "-e", ".[desktop]"],
                cwd=workspace,
                timeout_s=600,
            )
            install_payload = {
                "returncode": install.returncode,
                "stdout_tail": _tail(install.stdout),
                "stderr_tail": _tail(install.stderr),
            }

    return {
        "ok": True,
        "updated": bool(before != after),
        "checked_at": _now_iso(),
        "root": str(workspace),
        "before": before,
        "before_short": _short_sha(before),
        "after": after,
        "after_short": _short_sha(after),
        "upstream": upstream,
        "merge_stdout_tail": _tail(merge.stdout),
        "merge_stderr_tail": _tail(merge.stderr),
        "install": install_payload,
    }
