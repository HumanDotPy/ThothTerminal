from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


@dataclass(frozen=True)
class HiddenProcessHandle:
    process: subprocess.Popen
    command: list[str]
    stdout_log: Path
    stderr_log: Path

    @property
    def pid(self) -> int:
        return int(self.process.pid)


def hidden_creationflags(
    *,
    no_window: bool = True,
    new_process_group: bool = True,
    detached: bool = False,
    breakaway: bool = False,
) -> int:
    if os.name != "nt":
        return 0
    flags = 0
    if no_window:
        flags |= int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if new_process_group:
        flags |= int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    if detached:
        flags |= int(getattr(subprocess, "DETACHED_PROCESS", 0))
    if breakaway:
        flags |= int(getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0))
    return flags


def hidden_startupinfo() -> object | None:
    if os.name != "nt":
        return None
    startupinfo_cls = getattr(subprocess, "STARTUPINFO", None)
    if startupinfo_cls is None:
        return None
    startupinfo = startupinfo_cls()
    startupinfo.dwFlags |= int(getattr(subprocess, "STARTF_USESHOWWINDOW", 1))
    startupinfo.wShowWindow = int(getattr(subprocess, "SW_HIDE", 0))
    return startupinfo


def hidden_subprocess_kwargs(
    *,
    no_window: bool = True,
    new_process_group: bool = True,
    detached: bool = False,
    breakaway: bool = False,
) -> dict[str, object]:
    if os.name != "nt":
        return {}
    kwargs: dict[str, object] = {
        "creationflags": hidden_creationflags(
            no_window=no_window,
            new_process_group=new_process_group,
            detached=detached,
            breakaway=breakaway,
        )
    }
    startupinfo = hidden_startupinfo()
    if startupinfo is not None:
        kwargs["startupinfo"] = startupinfo
    return kwargs


def _is_windows_powershell(executable: str) -> bool:
    name = Path(str(executable or "")).name.lower()
    return name in {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}


def _normalize_hidden_windows_command(command: list[str]) -> list[str]:
    if os.name != "nt" or not command or not _is_windows_powershell(command[0]):
        return command
    normalized = list(command)
    lower_args = [part.lower() for part in normalized[1:]]
    prelude: list[str] = []
    if "-nologo" not in set(lower_args):
        prelude.append("-NoLogo")
    if "-noprofile" not in set(lower_args):
        prelude.append("-NoProfile")
    if "-noninteractive" not in set(lower_args):
        prelude.append("-NonInteractive")
    if "-windowstyle" in lower_args:
        index = lower_args.index("-windowstyle") + 1
        if index + 1 < len(normalized):
            normalized[index + 1] = "Hidden"
        else:
            normalized.append("Hidden")
    else:
        prelude.extend(["-WindowStyle", "Hidden"])
    return [normalized[0], *prelude, *normalized[1:]]


def _string_command(command: Sequence[object]) -> list[str]:
    return _normalize_hidden_windows_command([str(part) for part in command])


def run_hidden_process(
    command: Sequence[object],
    *,
    cwd: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    timeout_s: float | None = None,
    timeout: float | None = None,
    check: bool = False,
    capture_output: bool = True,
    text: bool = True,
    encoding: str | None = None,
    errors: str | None = None,
    no_window: bool = True,
    new_process_group: bool = True,
    detached: bool = False,
    breakaway: bool = False,
) -> subprocess.CompletedProcess[str]:
    if timeout_s is None:
        timeout_s = timeout
    command_v = _string_command(command)
    kwargs = hidden_subprocess_kwargs(
        no_window=no_window,
        new_process_group=new_process_group,
        detached=detached,
        breakaway=breakaway,
    )
    result = subprocess.run(
        command_v,
        cwd=str(cwd) if cwd is not None else None,
        env=dict(env) if env is not None else None,
        stdin=subprocess.DEVNULL,
        capture_output=capture_output,
        text=text,
        encoding=encoding,
        errors=errors,
        timeout=timeout_s,
        check=False,
        **kwargs,
    )
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode,
            command_v,
            output=result.stdout,
            stderr=result.stderr,
        )
    return result


def _safe_log_label(label: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(label or "process")).strip("-")
    return safe or "process"


def _default_log_path(logs_dir: Path, label: str, stream: str) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return logs_dir / f"{_safe_log_label(label)}-{stamp}.{stream}.log"


def start_hidden_logged_process(
    command: Sequence[object],
    *,
    label: str,
    cwd: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    logs_dir: Path | str | None = None,
    stdout_path: Path | str | None = None,
    stderr_path: Path | str | None = None,
    no_window: bool = True,
    new_process_group: bool = True,
    detached: bool = False,
    breakaway: bool = False,
) -> HiddenProcessHandle:
    command_v = _string_command(command)
    log_root = Path(logs_dir) if logs_dir is not None else None
    if stdout_path is None or stderr_path is None:
        if log_root is None:
            raise ValueError("logs_dir is required when stdout_path or stderr_path is omitted.")
        log_root.mkdir(parents=True, exist_ok=True)
    if stdout_path is not None:
        stdout_log = Path(stdout_path)
    else:
        assert log_root is not None
        stdout_log = _default_log_path(log_root, label, "out")
    if stderr_path is not None:
        stderr_log = Path(stderr_path)
    else:
        assert log_root is not None
        stderr_log = _default_log_path(log_root, label, "err")
    stdout_log.parent.mkdir(parents=True, exist_ok=True)
    stderr_log.parent.mkdir(parents=True, exist_ok=True)

    kwargs = hidden_subprocess_kwargs(
        no_window=no_window,
        new_process_group=new_process_group,
        detached=detached,
        breakaway=breakaway,
    )
    if os.name != "nt" and (detached or new_process_group):
        kwargs["start_new_session"] = True

    stdout_handle = stdout_log.open("ab")
    stderr_handle = stderr_log.open("ab")
    try:
        process = subprocess.Popen(
            command_v,
            cwd=str(cwd) if cwd is not None else None,
            env=dict(env) if env is not None else None,
            stdin=subprocess.DEVNULL,
            stdout=stdout_handle,
            stderr=stderr_handle,
            **kwargs,
        )
    finally:
        stdout_handle.close()
        stderr_handle.close()
    return HiddenProcessHandle(
        process=process,
        command=command_v,
        stdout_log=stdout_log,
        stderr_log=stderr_log,
    )
