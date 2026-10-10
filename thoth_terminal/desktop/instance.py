from __future__ import annotations

import ctypes
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from thoth_terminal.runtime_processes import run_hidden_process
from thoth_terminal.runtime_info import runtime_state_path


@dataclass(frozen=True)
class DesktopInstance:
    pid: int
    base_url: str | None
    started_at_utc: str | None
    lock_path: Path
    runtime_path: Path


_MUTEX_HANDLE = None
_ERROR_ALREADY_EXISTS = 183


def acquire_single_instance_mutex() -> bool:
    global _MUTEX_HANDLE
    if os.name != "nt":
        return True
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.CreateMutexW(None, False, "Global\\ThothTerminalDesktop")
    if not handle:
        return True
    if int(kernel32.GetLastError()) == _ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return False
    _MUTEX_HANDLE = handle
    return True


def desktop_lock_path() -> Path:
    return runtime_state_path("desktop-instance-lock")


def _pid_alive_windows(pid: int) -> bool:
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(0x00100000, False, int(pid))
    if not handle:
        return False
    try:
        wait_timeout = 0x00000102
        return int(kernel32.WaitForSingleObject(handle, 0)) == wait_timeout
    finally:
        kernel32.CloseHandle(handle)


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if int(pid) == os.getpid():
        return True
    try:
        if os.name == "nt":
            return _pid_alive_windows(int(pid))
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def read_existing_instance() -> DesktopInstance | None:
    lock_path = desktop_lock_path()
    runtime_path = runtime_state_path("desktop")
    payload: dict[str, Any] = {}
    for path in (lock_path, runtime_path):
        if not path.exists():
            continue
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(loaded, dict):
            payload = loaded
            break
    pid = int(payload.get("pid") or 0)
    if not pid_alive(pid):
        clear_instance_lock()
        return None
    return DesktopInstance(
        pid=pid,
        base_url=str(payload.get("base_url") or "").strip() or None,
        started_at_utc=str(payload.get("started_at_utc") or "").strip() or None,
        lock_path=lock_path,
        runtime_path=runtime_path,
    )


def write_instance_lock(*, base_url: str | None = None) -> Path:
    lock_path = desktop_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": os.getpid(),
        "base_url": base_url,
        "started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    lock_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return lock_path


def clear_instance_lock() -> None:
    for path in (desktop_lock_path(), runtime_state_path("desktop")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except Exception:
            payload = {}
        if (not payload) or int(payload.get("pid") or 0) == os.getpid() or not pid_alive(int(payload.get("pid") or 0)):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except Exception:
                pass


def terminate_instance(instance: DesktopInstance, *, timeout_s: float = 12.0) -> bool:
    pid = int(instance.pid)
    if not pid_alive(pid):
        clear_instance_lock()
        return True
    if os.name == "nt":
        run_hidden_process(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            check=False,
            timeout_s=max(3.0, float(timeout_s)),
        )
    else:
        try:
            os.kill(pid, 15)
        except Exception:
            pass
    deadline = time.time() + max(1.0, float(timeout_s))
    while time.time() < deadline:
        if not pid_alive(pid):
            clear_instance_lock()
            return True
        time.sleep(0.25)
    return not pid_alive(pid)


def terminate_desktop_processes(*, timeout_s: float = 12.0) -> None:
    if os.name != "nt":
        return
    script = (
        "$self=$PID; "
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.ProcessId -ne $self -and $_.CommandLine -match 'thoth_terminal\\\\.desktop' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    run_hidden_process(
        ["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass", "-Command", script],
        check=False,
        timeout_s=max(3.0, float(timeout_s)),
    )


def terminate_thoth_support_processes(*, timeout_s: float = 12.0) -> None:
    """Stop auxiliary Thoth processes that are outside the Qt app's own threads."""
    if os.name != "nt":
        return
    app_pid = int(os.getpid())
    script = (
        f"$appPid={app_pid}; $self=$PID; "
        "Get-CimInstance Win32_Process | "
        "Where-Object { "
        "$_.ProcessId -ne $self -and $_.ProcessId -ne $appPid -and ("
        "$_.Name -eq 'ThothTerminal.exe' -or "
        "($_.ParentProcessId -eq $appPid -and $_.Name -eq 'QtWebEngineProcess.exe') -or "
        "($_.ParentProcessId -eq $appPid -and $_.Name -eq 'node.exe' -and $_.CommandLine -match 'playwright.*run-driver') -or "
        "($_.Name -match 'cloudflared' -and $_.CommandLine -match 'tunnel run thoth') -or "
        "$_.CommandLine -match 'thoth-watchdog\\.ps1' -or "
        "$_.CommandLine -match 'thoth_terminal\\.cli\\s+local-agent-api' -or "
        "$_.CommandLine -match 'thoth_terminal\\.web\\.local_agent_app:app' -or "
        "$_.CommandLine -match 'tmp-start-local-agent-api\\.ps1' -or "
        "$_.CommandLine -match 'http\\.server\\s+8766' -or "
        "$_.CommandLine -match 'tmp-script-transfer'"
        ") } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    run_hidden_process(
        ["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass", "-Command", script],
        check=False,
        timeout_s=max(3.0, float(timeout_s)),
    )
