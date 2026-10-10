from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import uuid
from types import SimpleNamespace
from typing import Mapping, Sequence

from thoth_terminal.runtime_processes import hidden_subprocess_kwargs


logger = logging.getLogger("thoth.runtime_supervisor")


def _assign_windows_job(process: subprocess.Popen[bytes], *, memory_limit_mb: int) -> int | None:
    if os.name != "nt":
        return None

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
    JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    info.BasicLimitInformation.LimitFlags = (
        JOB_OBJECT_LIMIT_PROCESS_MEMORY
        | JOB_OBJECT_LIMIT_JOB_MEMORY
        | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    )
    info.ProcessMemoryLimit = int(memory_limit_mb) * 1024 * 1024
    info.JobMemoryLimit = int(memory_limit_mb) * 1024 * 1024
    JobObjectExtendedLimitInformation = 9
    configured = kernel32.SetInformationJobObject(
        job,
        JobObjectExtendedLimitInformation,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    assigned = configured and kernel32.AssignProcessToJobObject(job, wintypes.HANDLE(process._handle))
    if not assigned:
        kernel32.CloseHandle(job)
        return None
    return int(job)


def _close_windows_job(handle: int | None) -> None:
    if os.name == "nt" and handle:
        ctypes.windll.kernel32.CloseHandle(wintypes.HANDLE(int(handle)))


def process_private_memory_mb(pid: int) -> float | None:
    process_id = int(pid or 0)
    if process_id <= 0:
        return None
    if os.name == "nt":
        class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t),
            ]

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        psapi = ctypes.windll.psapi
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, process_id)
        if not handle:
            return None
        try:
            counters = PROCESS_MEMORY_COUNTERS_EX()
            counters.cb = ctypes.sizeof(counters)
            ok = psapi.GetProcessMemoryInfo(
                handle,
                ctypes.byref(counters),
                counters.cb,
            )
            return float(counters.PrivateUsage) / (1024 * 1024) if ok else None
        finally:
            kernel32.CloseHandle(handle)
    status_path = Path(f"/proc/{process_id}/status")
    try:
        for line in status_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("VmRSS:"):
                return float(line.split()[1]) / 1024
    except Exception:
        return None
    return None


def runtime_child_command(service: str, *, host: str | None = None, port: int | None = None) -> list[str]:
    if bool(getattr(sys, "frozen", False)):
        command = [sys.executable, "--runtime-child", str(service)]
    else:
        command = [sys.executable, "-m", "thoth_terminal.runtime_child", str(service)]
    if host is not None:
        command.extend(["--host", str(host)])
    if port is not None:
        command.extend(["--port", str(int(port))])
    return command


def runtime_child_environment(*, overrides: Mapping[str, str] | None = None) -> dict[str, str]:
    from thoth_terminal.config import settings

    env = dict(os.environ)
    env.update(
        {
            "THOTH_DATA_DIR": str(settings.data_dir),
            "THOTH_DB_PATH": str(settings.db_path),
            "THOTH_DUCKDB_PATH": str(settings.duckdb_path),
            "THOTH_OBJECTS_DIR": str(settings.objects_dir),
            "THOTH_RUNTIME_STATE_DIR": str(settings.runtime_state_dir),
        }
    )
    if str(getattr(settings, "database_url", "") or "").strip():
        env["THOTH_DATABASE_URL"] = str(settings.database_url)
    if str(getattr(settings, "agent_api_token", "") or "").strip():
        env["THOTH_AGENT_API_TOKEN"] = str(settings.agent_api_token)
    if str(getattr(settings, "agent_api_write_token", "") or "").strip():
        env["THOTH_AGENT_API_WRITE_TOKEN"] = str(settings.agent_api_write_token)
    desktop_control_url = str(getattr(settings, "desktop_control_url", "") or "").strip()
    desktop_control_token = str(getattr(settings, "desktop_control_token", "") or "").strip()
    if desktop_control_url and desktop_control_token:
        env["THOTH_DESKTOP_CONTROL_URL"] = desktop_control_url
        env["THOTH_DESKTOP_CONTROL_TOKEN"] = desktop_control_token
    if overrides:
        env.update({str(key): str(value) for key, value in overrides.items()})
    return env


class ManagedRuntimeProcess:
    """Restarting, memory-bounded OS-process supervisor for one runtime lane."""

    def __init__(
        self,
        *,
        name: str,
        command: Sequence[str],
        cwd: Path | str,
        env: Mapping[str, str] | None = None,
        memory_limit_mb: int = 4096,
        poll_interval_s: float = 0.25,
        restart_max_delay_s: float = 30.0,
        shutdown_timeout_s: float = 8.0,
        heartbeat_service: str | None = None,
        heartbeat_max_age_s: float = 90.0,
        heartbeat_startup_grace_s: float = 120.0,
    ) -> None:
        self.name = str(name)
        self.command = [str(part) for part in command]
        self.cwd = Path(cwd)
        self.env = dict(env) if env is not None else None
        self.memory_limit_mb = max(256, int(memory_limit_mb))
        self.poll_interval_s = max(0.1, float(poll_interval_s))
        self.restart_max_delay_s = max(1.0, float(restart_max_delay_s))
        self.shutdown_timeout_s = max(1.0, float(shutdown_timeout_s))
        self.heartbeat_service = str(heartbeat_service or "").strip() or None
        self.heartbeat_max_age_s = max(15.0, float(heartbeat_max_age_s))
        self.heartbeat_startup_grace_s = max(15.0, float(heartbeat_startup_grace_s))
        self.error: Exception | None = None
        self.last_exit_code: int | None = None
        self.last_private_memory_mb: float | None = None
        self.memory_limit_exits = 0
        self.restart_count = 0
        self._process: subprocess.Popen[bytes] | None = None
        self._job_handle: int | None = None
        self._last_spawn_monotonic = 0.0
        self._supervision_id: str | None = None
        self._last_heartbeat_monotonic: float | None = None
        self._last_heartbeat_timestamp: str | None = None
        self._heartbeat_unavailable_since: float | None = None
        self.heartbeat_state: str | None = None
        self._heartbeat_settings: object | None = None
        if self.env and self.env.get("THOTH_RUNTIME_STATE_DIR"):
            from thoth_terminal.config import settings

            self._heartbeat_settings = SimpleNamespace(
                runtime_state_dir=self.env["THOTH_RUNTIME_STATE_DIR"],
                forbidden_runtime_drives=getattr(settings, "forbidden_runtime_drives", ""),
            )
        self._monitor_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()

    @property
    def pid(self) -> int | None:
        with self._lock:
            process = self._process
            return int(process.pid) if process is not None and process.poll() is None else None

    def _spawn(self) -> None:
        kwargs = hidden_subprocess_kwargs(no_window=True, new_process_group=True)
        supervision_id = uuid.uuid4().hex
        child_env = dict(self.env) if self.env is not None else dict(os.environ)
        # The Windows venv launcher and its real child inherit the same identity.
        child_env["THOTH_RUNTIME_SUPERVISION_ID"] = supervision_id
        process = subprocess.Popen(
            self.command,
            cwd=str(self.cwd),
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **kwargs,
        )
        job_handle = _assign_windows_job(process, memory_limit_mb=self.memory_limit_mb)
        with self._lock:
            self._process = process
            self._job_handle = job_handle
            self._last_spawn_monotonic = time.monotonic()
            self._supervision_id = supervision_id
            self._last_heartbeat_monotonic = None
            self._last_heartbeat_timestamp = None
            self._heartbeat_unavailable_since = None
            self.heartbeat_state = None
            self.error = None
        logger.info("runtime child started name=%s pid=%s memory_limit_mb=%s", self.name, process.pid, self.memory_limit_mb)

    def _heartbeat_failure(self, process: subprocess.Popen[bytes], *, now: float) -> str | None:
        from thoth_terminal.runtime_health import heartbeat_age_seconds, read_service_heartbeat

        heartbeat = read_service_heartbeat(self.heartbeat_service, settings_obj=self._heartbeat_settings)
        age = heartbeat_age_seconds(heartbeat)
        status = str((heartbeat or {}).get("status") or "").strip().lower()
        timestamp = str((heartbeat or {}).get("heartbeat_at_utc") or "")
        try:
            heartbeat_pid = int((heartbeat or {}).get("pid") or 0)
        except (TypeError, ValueError, OverflowError):
            heartbeat_pid = 0
        own_spawn = (
            bool(self._supervision_id)
            and (heartbeat or {}).get("supervision_id") == self._supervision_id
        )
        valid = (
            own_spawn and heartbeat_pid > 0 and age is not None
            and status in {"healthy", "running", "degraded"}
        )
        if valid and age <= self.heartbeat_max_age_s:
            if timestamp != self._last_heartbeat_timestamp:
                # A repeatedly readable old file must not refresh the deadline.
                self._last_heartbeat_monotonic = now - age
                self._last_heartbeat_timestamp = timestamp
            self._heartbeat_unavailable_since = None
            self.heartbeat_state = "fresh"
        else:
            if heartbeat is None:
                self.heartbeat_state = "unavailable"
            elif not own_spawn:
                self.heartbeat_state = "foreign_spawn"
            else:
                self.heartbeat_state = "stale" if valid else "invalid"
            if self._heartbeat_unavailable_since is None:
                self._heartbeat_unavailable_since = now

        if now - self._last_spawn_monotonic < self.heartbeat_startup_grace_s:
            return None
        last_fresh = self._last_heartbeat_monotonic
        missing_since = self._heartbeat_unavailable_since
        expired = (
            last_fresh is not None and now - last_fresh > self.heartbeat_max_age_s
        ) or (
            last_fresh is None and missing_since is not None
            and now - missing_since > self.heartbeat_max_age_s
        )
        if not expired:
            return None
        if expired and self.heartbeat_state == "fresh":
            self.heartbeat_state = "stale"
        return (
            f"{self.name} heartbeat {self.heartbeat_state} "
            f"service_pid={heartbeat_pid} launcher_pid={process.pid} "
            f"age_s={age} status={status or 'missing'}"
        )

    def start(self) -> None:
        if self._monitor_thread is not None and self._monitor_thread.is_alive():
            return
        self._stop_event.clear()
        self._spawn()
        self._monitor_thread = threading.Thread(
            target=self._monitor,
            name=f"thoth-supervisor-{self.name}",
            daemon=True,
        )
        self._monitor_thread.start()

    def _terminate_process(self, process: subprocess.Popen[bytes], *, reason: str) -> None:
        if process.poll() is not None:
            return
        logger.warning("runtime child stopping name=%s pid=%s reason=%s", self.name, process.pid, reason)
        try:
            if os.name == "nt":
                process.terminate()
            else:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            process.wait(timeout=self.shutdown_timeout_s)
        except Exception:
            try:
                process.kill()
                process.wait(timeout=2.0)
            except Exception:
                logger.exception("runtime child force-stop failed name=%s pid=%s", self.name, process.pid)
        finally:
            with self._lock:
                job_handle = self._job_handle
                self._job_handle = None
            _close_windows_job(job_handle)

    def _monitor(self) -> None:
        restart_delay_s = 1.0
        while not self._stop_event.is_set():
            with self._lock:
                process = self._process
            if process is None:
                return
            exit_code = process.poll()
            if exit_code is None:
                private_mb = process_private_memory_mb(process.pid)
                self.last_private_memory_mb = private_mb
                if private_mb is not None and private_mb >= float(self.memory_limit_mb):
                    self.memory_limit_exits += 1
                    self.error = MemoryError(
                        f"{self.name} exceeded {self.memory_limit_mb} MB private memory ({private_mb:.1f} MB)"
                    )
                    self._terminate_process(process, reason="memory_limit")
                elif self.heartbeat_service:
                    failure = self._heartbeat_failure(process, now=time.monotonic())
                    if failure is not None:
                        self.error = RuntimeError(failure)
                        self._terminate_process(process, reason="stale_heartbeat")
                    else:
                        self._stop_event.wait(self.poll_interval_s)
                        continue
                else:
                    self._stop_event.wait(self.poll_interval_s)
                    continue
            self.last_exit_code = process.poll()
            with self._lock:
                job_handle = self._job_handle
                self._job_handle = None
                uptime_s = max(0.0, time.monotonic() - self._last_spawn_monotonic)
            _close_windows_job(job_handle)
            if self._stop_event.is_set():
                return
            self.restart_count += 1
            logger.warning(
                "runtime child exited name=%s code=%s restart_in_s=%.1f",
                self.name,
                self.last_exit_code,
                restart_delay_s,
            )
            if self._stop_event.wait(restart_delay_s):
                return
            try:
                self._spawn()
                restart_delay_s = 1.0 if uptime_s >= 30.0 else min(
                    self.restart_max_delay_s,
                    restart_delay_s * 2.0,
                )
            except Exception as exc:
                self.error = exc
                logger.exception("runtime child restart failed name=%s", self.name)
                restart_delay_s = min(self.restart_max_delay_s, restart_delay_s * 2.0)

    def is_alive(self) -> bool:
        with self._lock:
            return bool(self._process is not None and self._process.poll() is None)

    def status(self) -> dict[str, object]:
        return {
            "name": self.name,
            "running": self.is_alive(),
            "pid": self.pid,
            "memory_limit_mb": self.memory_limit_mb,
            "heartbeat_service": self.heartbeat_service,
            "heartbeat_state": self.heartbeat_state,
            "private_memory_mb": round(self.last_private_memory_mb, 1) if self.last_private_memory_mb is not None else None,
            "restart_count": self.restart_count,
            "memory_limit_exits": self.memory_limit_exits,
            "last_exit_code": self.last_exit_code,
            "error": f"{type(self.error).__name__}: {self.error}" if self.error is not None else None,
        }

    def stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            process = self._process
        if process is not None:
            self._terminate_process(process, reason="supervisor_stop")

    def join(self, timeout: float | None = None) -> None:
        thread = self._monitor_thread
        if thread is not None:
            thread.join(timeout=timeout)


class ManagedRuntimeGroup:
    def __init__(self, *children: ManagedRuntimeProcess, name: str) -> None:
        self.children = list(children)
        self.name = str(name)

    @property
    def error(self) -> Exception | None:
        return next((child.error for child in self.children if child.error is not None), None)

    def start(self) -> None:
        for child in self.children:
            child.start()

    def stop(self) -> None:
        for child in reversed(self.children):
            child.stop()

    def join(self, timeout: float | None = None) -> None:
        deadline = time.monotonic() + float(timeout) if timeout is not None else None
        for child in self.children:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            child.join(timeout=remaining)

    def is_alive(self) -> bool:
        return bool(self.children) and all(child.is_alive() for child in self.children)

    def status(self) -> dict[str, object]:
        return {"name": self.name, "running": self.is_alive(), "children": [child.status() for child in self.children]}
