from __future__ import annotations

import ctypes
from ctypes import wintypes
import base64
import binascii
from datetime import date, datetime
from decimal import Decimal
import json
import logging
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable
from contextlib import contextmanager
from uuid import UUID

from thoth_terminal.config import settings
from thoth_terminal.runtime_env import repo_root
from thoth_terminal.runtime_info import runtime_state_dir
from thoth_terminal.runtime_processes import hidden_subprocess_kwargs
from thoth_terminal.runtime_supervisor import (
    _assign_windows_job,
    _close_windows_job,
    process_private_memory_mb,
)


logger = logging.getLogger("thoth.browser_ingestion_runtime")


class BrowserIngestionError(RuntimeError):
    pass


class BrowserIngestionChildCrash(BrowserIngestionError):
    """The owned wrapper exited before writing a structured result."""


class BrowserIngestionLaunchCrash(BrowserIngestionChildCrash):
    """The child reported a recognizable Playwright/Chromium launch failure."""


class BrowserIngestionTimeout(BrowserIngestionError):
    pass


class BrowserIngestionDeferred(BrowserIngestionError):
    pass


_local_lane_lock = threading.Lock()
_DIAGNOSTIC_TAIL_BYTES = 64 * 1024


class _DiagnosticTail:
    """Drain child stderr without allowing an unbounded per-job log/pipe."""

    def __init__(self, path: Path, stream: Any) -> None:
        self.path = path
        self.stream = stream
        self._tail = bytearray()
        self._thread: threading.Thread | None = None
        self._closed = False
        try:
            self.path.touch(exist_ok=True)
        except OSError:
            logger.debug("browser diagnostic log could not be created path=%s", self.path, exc_info=True)

    def start(self) -> None:
        if self.stream is None:
            return
        self._thread = threading.Thread(target=self._drain, name="thoth-browser-stderr", daemon=True)
        self._thread.start()

    def _drain(self) -> None:
        try:
            while True:
                chunk = self.stream.read(8192)
                if not chunk:
                    break
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8", errors="replace")
                self._tail.extend(bytes(chunk))
                if len(self._tail) > _DIAGNOSTIC_TAIL_BYTES:
                    del self._tail[: len(self._tail) - _DIAGNOSTIC_TAIL_BYTES]
        except (OSError, TypeError, ValueError):
            logger.debug("browser diagnostic stderr drain ended", exc_info=True)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        try:
            self.path.write_bytes(bytes(self._tail[-_DIAGNOSTIC_TAIL_BYTES:]))
        except OSError:
            logger.debug("browser diagnostic log could not be written path=%s", self.path, exc_info=True)

    def text(self, *, limit: int = 4000) -> str:
        return bytes(self._tail[-max(1, int(limit)):]).decode("utf-8", errors="replace")


@contextmanager
def browser_ingestion_lane(*, timeout_s: float):
    """Serialize browser ingestion across worker, web probes, and CLI processes."""
    acquired_local = _local_lane_lock.acquire(timeout=max(0.0, float(timeout_s)))
    if not acquired_local:
        raise BrowserIngestionDeferred("browser ingestion lane remained busy")
    mutex = None
    lock_file = None
    try:
        if os.name == "nt":
            kernel32 = ctypes.windll.kernel32
            kernel32.CreateMutexW.restype = wintypes.HANDLE
            kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
            kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel32.WaitForSingleObject.restype = wintypes.DWORD
            mutex = kernel32.CreateMutexW(None, False, "Local\\ThothTerminalBrowserIngestionV1")
            wait_ms = min(0xFFFFFFFE, max(0, int(float(timeout_s) * 1000)))
            if not mutex or kernel32.WaitForSingleObject(mutex, wait_ms) not in {0, 0x80}:
                raise BrowserIngestionDeferred("browser ingestion lane remained busy")
        else:
            import fcntl

            lock_path = runtime_state_dir() / "browser-ingestion.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_file = lock_path.open("a+")
            deadline = time.monotonic() + max(0.0, float(timeout_s))
            while True:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise BrowserIngestionDeferred("browser ingestion lane remained busy")
                    time.sleep(0.25)
        yield
    finally:
        if os.name == "nt" and mutex:
            ctypes.windll.kernel32.ReleaseMutex(wintypes.HANDLE(mutex))
            ctypes.windll.kernel32.CloseHandle(wintypes.HANDLE(mutex))
        elif lock_file is not None:
            try:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                lock_file.close()
            except OSError:
                pass
        _local_lane_lock.release()


def mark_browser_progress(config: dict[str, Any], stage: str) -> None:
    raw = str((config or {}).get("_thoth_progress_path") or "").strip()
    if not raw:
        return
    path = Path(raw)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp.write_text(
            json.dumps({"stage": str(stage), "monotonic": time.monotonic()}),
            encoding="utf-8",
        )
        os.replace(temp, path)
    except OSError:
        pass


def _positive_float(value: object, fallback: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return float(fallback)
    return parsed if parsed > 0 else float(fallback)


def is_shared_browser_profile(raw: object) -> bool:
    if not raw:
        return False
    normalized = str(Path(str(raw)).expanduser()).replace("/", "\\").rstrip("\\").lower()
    return any(marker in normalized for marker in ("\\microsoft\\edge\\user data", "\\google\\chrome\\user data"))


def _reject_shared_browser_profile(cfg: dict[str, Any]) -> None:
    raw = cfg.get("user_data_dir") or cfg.get("browser_user_data_dir")
    if is_shared_browser_profile(raw):
        raise BrowserIngestionError(
            "Refusing to attach automated ingestion to the normal Edge/Chrome profile. "
            "Use storage_state_path or a dedicated Thoth browser profile outside the browser User Data directory."
        )


def _prepare_browser_config(fn: Callable[[dict[str, Any]], Any], cfg: dict[str, Any]) -> dict[str, Any]:
    """Remove ignored profile hints only where the connector is provably non-persistent."""
    prepared = dict(cfg or {})
    fn_name = getattr(fn, "__name__", "")
    if fn_name in {"fetch_web_list_browser", "probe_web_list_browser"} and bool(prepared.get("headless", True)):
        raw = prepared.get("user_data_dir") or prepared.get("browser_user_data_dir")
        if raw:
            if is_shared_browser_profile(raw):
                logger.info(
                    "ignoring shared browser profile for headless non-persistent connector fn=%s",
                    fn_name,
                )
                for key in (
                    "user_data_dir",
                    "browser_user_data_dir",
                    "profile_directory",
                    "browser_profile_directory",
                ):
                    prepared.pop(key, None)
    _reject_shared_browser_profile(prepared)
    return prepared


def system_memory_snapshot_mb() -> dict[str, float] | None:
    """Return total/available physical memory without adding a resident dependency."""
    if os.name == "nt":
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", wintypes.DWORD),
                ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        unit = 1024.0 * 1024.0
        return {
            "total_mb": float(status.ullTotalPhys) / unit,
            "available_mb": float(status.ullAvailPhys) / unit,
            "commit_available_mb": float(status.ullAvailPageFile) / unit,
        }
    try:
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        total = int(os.sysconf("SC_PHYS_PAGES")) * page_size
        available = int(os.sysconf("SC_AVPHYS_PAGES")) * page_size
    except (AttributeError, OSError, ValueError):
        return None
    unit = 1024.0 * 1024.0
    return {"total_mb": total / unit, "available_mb": available / unit}


def required_browser_headroom_mb(*, total_mb: float, memory_limit_mb: int) -> float:
    floor_mb = _positive_float(
        getattr(settings, "browser_ingestion_min_available_memory_mb", 4096),
        4096,
    )
    percent = _positive_float(
        getattr(settings, "browser_ingestion_min_available_memory_percent", 15.0),
        15.0,
    )
    # Keep enough room for the desktop and normal Edge even if the child uses
    # its whole allowance.  This is admission control, not a dynamic cap raise.
    return max(floor_mb, float(total_mb) * percent / 100.0, float(memory_limit_mb) * 2.0)


def wait_for_browser_memory_headroom(
    *,
    memory_limit_mb: int,
    wait_s: float,
    poll_s: float,
    snapshot_fn: Callable[[], dict[str, float] | None] = system_memory_snapshot_mb,
) -> dict[str, float] | None:
    deadline = time.monotonic() + max(0.0, float(wait_s))
    last: dict[str, float] | None = None
    while True:
        last = snapshot_fn()
        if last is None:
            # Unknown telemetry must not make ingestion permanently unavailable;
            # the child Job Object remains the hard safety boundary.
            return None
        required_mb = required_browser_headroom_mb(
            total_mb=float(last.get("total_mb") or 0.0),
            memory_limit_mb=memory_limit_mb,
        )
        commit_available = last.get("commit_available_mb")
        commit_ok = commit_available is None or float(commit_available) >= float(memory_limit_mb) * 2.0
        if float(last.get("available_mb") or 0.0) >= required_mb and commit_ok:
            return {**last, "required_mb": required_mb}
        if time.monotonic() >= deadline:
            raise BrowserIngestionDeferred(
                "browser ingestion deferred for memory pressure: "
                f"available={float(last.get('available_mb') or 0.0):.0f}MB "
                f"required={required_mb:.0f}MB "
                f"commit_available={float(commit_available or 0.0):.0f}MB"
            )
        time.sleep(max(0.25, min(float(poll_s), max(0.0, deadline - time.monotonic()))))


def _read_json(path: Path) -> dict[str, Any] | list[Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_hook=_ipc_object_hook)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, (dict, list)) else None


def _ipc_object_hook(value: dict[str, Any]) -> object:
    kind = value.get("__thoth_ipc_type__")
    raw = value.get("value")
    if kind == "datetime" and isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return value
    if kind == "date" and isinstance(raw, str):
        try:
            return date.fromisoformat(raw)
        except ValueError:
            return value
    if kind == "path" and isinstance(raw, str):
        return Path(raw)
    if kind == "bytes" and isinstance(raw, str):
        try:
            return base64.b64decode(raw.encode("ascii"), validate=True)
        except (binascii.Error, ValueError, UnicodeError):
            return value
    if kind == "decimal" and isinstance(raw, str):
        try:
            return Decimal(raw)
        except ValueError:
            return value
    if kind == "uuid" and isinstance(raw, str):
        try:
            return UUID(raw)
        except ValueError:
            return value
    return value


def _owned_windows_process_snapshot(job_dir: Path) -> list[dict[str, Any]]:
    if os.name != "nt":
        return []
    script = (
        "$needle=$env:THOTH_BROWSER_JOB_DIR; "
        "@(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and "
        "$_.CommandLine.IndexOf($needle,[System.StringComparison]::OrdinalIgnoreCase) -ge 0 } | "
        "Select-Object ProcessId,ParentProcessId,Name,CommandLine) | ConvertTo-Json -Compress"
    )
    env = dict(os.environ)
    env["THOTH_BROWSER_JOB_DIR"] = str(job_dir)
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script],
            cwd=str(repo_root()),
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
            **hidden_subprocess_kwargs(no_window=True, new_process_group=True),
        )
        if result.returncode != 0 or not str(result.stdout or "").strip():
            return []
        payload = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        logger.warning("owned browser process discovery failed path=%s", job_dir, exc_info=True)
        return []
    rows = payload if isinstance(payload, list) else [payload] if isinstance(payload, dict) else []
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            pid = int(row.get("ProcessId") or 0)
        except (AttributeError, TypeError, ValueError):
            continue
        if pid <= 0 or pid == os.getpid():
            continue
        out.append(
            {
                "pid": pid,
                "parent_pid": int(row.get("ParentProcessId") or 0),
                "name": str(row.get("Name") or "")[:120],
                "private_mb": process_private_memory_mb(pid),
            }
        )
    return out


def _valid_ownership_manifest(job_dir: Path) -> bool:
    payload = _read_json(job_dir / "ownership.json")
    if not isinstance(payload, dict):
        return False
    if payload.get("owner") != "thoth_terminal.browser_ingestion_runtime":
        return False
    try:
        declared = Path(str(payload.get("job_dir") or "")).resolve()
        actual = job_dir.resolve()
    except OSError:
        return False
    return declared == actual


def _process_alive(pid: int) -> bool:
    process_id = int(pid or 0)
    if process_id <= 0:
        return False
    if os.name == "nt":
        kernel32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, process_id)
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(process_id, 0)
        return True
    except OSError:
        return False


def _terminate_owned_windows_processes(job_dir: Path, *, reason: str) -> list[dict[str, Any]]:
    if not _valid_ownership_manifest(job_dir):
        logger.warning("browser owned cleanup skipped invalid manifest path=%s reason=%s", job_dir, reason)
        return []
    rows = _owned_windows_process_snapshot(job_dir)
    if not rows:
        return []
    total_private_mb = sum(float(row.get("private_mb") or 0.0) for row in rows)
    logger.warning(
        "browser_owned_process_cleanup reason=%s path=%s process_count=%s aggregate_private_mb=%.1f pids=%s",
        reason,
        job_dir,
        len(rows),
        total_private_mb,
        [row["pid"] for row in rows[:50]],
    )
    if os.name != "nt":
        return rows
    kernel32 = ctypes.windll.kernel32
    PROCESS_TERMINATE = 0x0001
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    for row in rows:
        handle = kernel32.OpenProcess(
            PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            int(row["pid"]),
        )
        if not handle:
            continue
        try:
            kernel32.TerminateProcess(handle, 1)
        finally:
            kernel32.CloseHandle(handle)
    return rows


def _cleanup_stale_job_dirs(root: Path, *, older_than_s: float = 24 * 60 * 60) -> None:
    now = time.time()
    for path in root.glob("thoth-browser-*"):
        try:
            if not path.is_dir():
                continue
            if not _valid_ownership_manifest(path):
                logger.warning("stale browser workdir ignored because ownership is invalid path=%s", path)
                continue
            manifest = dict(_read_json(path / "ownership.json") or {})
            wrapper_pid = int(manifest.get("wrapper_pid") or 0)
            if _process_alive(wrapper_pid) and now - path.stat().st_mtime < older_than_s:
                continue
            _terminate_owned_windows_processes(path, reason="stale_owned_workdir")
            shutil.rmtree(path)
        except OSError:
            logger.warning("stale browser workdir cleanup failed path=%s", path, exc_info=True)


def _progress_stage(path: Path) -> tuple[str | None, float | None]:
    value = _read_json(path)
    if not isinstance(value, dict):
        return None, None
    stage = str(value.get("stage") or "").strip() or None
    try:
        changed = float(value.get("monotonic"))
    except (TypeError, ValueError):
        changed = None
    return stage, changed


def _terminate_tree(process: subprocess.Popen[bytes], job_handle: int | None) -> None:
    if process.poll() is not None:
        _close_windows_job(job_handle)
        return
    if os.name == "nt" and job_handle:
        # KILL_ON_JOB_CLOSE terminates the isolated Python child and every Edge
        # descendant that inherited this nested job.
        _close_windows_job(job_handle)
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            process.kill()
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
                **hidden_subprocess_kwargs(),
            )
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        process.wait(timeout=5.0)
    except Exception:
        try:
            process.kill()
            process.wait(timeout=2.0)
        except Exception:
            logger.warning("unable to terminate browser ingestion pid=%s", process.pid, exc_info=True)


def run_browser_ingestion(fn: Callable[[dict[str, Any]], Any], cfg: dict[str, Any]) -> Any:
    """Execute one allow-listed browser connector in an owned, bounded process tree."""
    cfg = _prepare_browser_config(fn, cfg)
    configured_limit_mb = int(
        cfg.get("browser_process_memory_limit_mb") or getattr(settings, "browser_ingestion_memory_limit_mb", 2048)
    )
    global_limit_mb = max(512, int(getattr(settings, "browser_ingestion_memory_limit_mb", 2048) or 2048))
    memory_limit_mb = min(global_limit_mb, max(
        512,
        configured_limit_mb,
    ))
    max_runtime_s = _positive_float(
        cfg.get("browser_max_runtime_s") or getattr(settings, "browser_ingestion_max_runtime_s", 7200),
        7200,
    )
    stall_timeout_s = _positive_float(
        cfg.get("browser_stall_timeout_s") or getattr(settings, "browser_ingestion_stall_timeout_s", 900),
        900,
    )
    admission_wait_s = _positive_float(
            cfg.get("browser_admission_wait_s") or getattr(settings, "browser_ingestion_admission_wait_s", 600),
            600,
    )
    lane_wait_s = admission_wait_s + max_runtime_s
    with browser_ingestion_lane(timeout_s=lane_wait_s):
        launch_retry_allowed = cfg.get("browser_retry_on_launch_crash", True) is not False
        for attempt in range(2):
            try:
                return _run_browser_ingestion_owned(
                    fn,
                    cfg,
                    memory_limit_mb=memory_limit_mb,
                    max_runtime_s=max_runtime_s,
                    stall_timeout_s=stall_timeout_s,
                    admission_wait_s=admission_wait_s,
                )
            except BrowserIngestionChildCrash:
                if attempt or not launch_retry_allowed:
                    raise
                # A new invocation gets a new job directory, process tree, and
                # Playwright temporary profile.  No headed fallback is used;
                # the existing ingestion circuit records repeated failures.
                logger.warning(
                    "browser_ingestion_launch_crash_retry fn=%s connector=%s",
                    getattr(fn, "__name__", str(fn)),
                    cfg.get("connector"),
                    exc_info=True,
                )
        raise AssertionError("browser ingestion retry loop did not return or raise")


def _run_browser_ingestion_owned(
    fn: Callable[[dict[str, Any]], Any],
    cfg: dict[str, Any],
    *,
    memory_limit_mb: int,
    max_runtime_s: float,
    stall_timeout_s: float,
    admission_wait_s: float,
) -> Any:
    wait_for_browser_memory_headroom(
        memory_limit_mb=memory_limit_mb,
        wait_s=admission_wait_s,
        poll_s=_positive_float(getattr(settings, "browser_ingestion_admission_poll_s", 5.0), 5.0),
    )
    root = runtime_state_dir() / "browser-ingestion"
    root.mkdir(parents=True, exist_ok=True)
    _cleanup_stale_job_dirs(root)
    job_dir = Path(tempfile.mkdtemp(prefix="thoth-browser-", dir=str(root)))
    request_path = job_dir / "request.json"
    result_path = job_dir / "result.json"
    progress_path = job_dir / "progress.json"
    diagnostic_path = job_dir / "diagnostics.log"
    request = {
        "function": f"{fn.__module__}:{fn.__name__}",
        "config": {**dict(cfg or {}), "_thoth_progress_path": str(progress_path)},
    }
    request_path.write_text(json.dumps(request, ensure_ascii=False, default=str), encoding="utf-8")
    (job_dir / "ownership.json").write_text(
        json.dumps(
            {
                "owner": "thoth_terminal.browser_ingestion_runtime",
                "job_dir": str(job_dir),
                "function": request["function"],
                "created_at_epoch": time.time(),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    env = dict(os.environ)
    # Playwright temporary profiles now live under an unambiguous Thoth-owned
    # directory, making cleanup safe and preventing attachment to normal Edge.
    env.update({"TEMP": str(job_dir), "TMP": str(job_dir), "TMPDIR": str(job_dir)})
    command = [
        sys.executable,
        "-m",
        "thoth_terminal.browser_ingestion_child",
        "--request",
        str(request_path),
        "--result",
        str(result_path),
    ]
    process: subprocess.Popen[bytes] | None = None
    job_handle: int | None = None
    diagnostics: _DiagnosticTail | None = None
    started = time.monotonic()
    last_stage: str | None = None
    last_progress = started
    try:
        process = subprocess.Popen(
            command,
            cwd=str(repo_root()),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            # Drain the wrapper and Chromium stderr into an owned bounded
            # tail.  DEVNULL made early Chromium crashes indistinguishable
            # from a connector returning no rows.
            stderr=subprocess.PIPE,
            **hidden_subprocess_kwargs(no_window=True, new_process_group=True),
        )
        diagnostics = _DiagnosticTail(diagnostic_path, getattr(process, "stderr", None))
        diagnostics.start()
        ownership = dict(_read_json(job_dir / "ownership.json") or {})
        ownership["wrapper_pid"] = int(process.pid)
        (job_dir / "ownership.json").write_text(
            json.dumps(ownership, ensure_ascii=False),
            encoding="utf-8",
        )
        job_handle = _assign_windows_job(process, memory_limit_mb=memory_limit_mb)
        if os.name == "nt" and job_handle is None:
            logger.warning(
                "browser nested Job Object assignment failed pid=%s; process-tree termination fallback remains active",
                process.pid,
            )
        while process.poll() is None:
            now = time.monotonic()
            stage, changed = _progress_stage(progress_path)
            if stage and (stage != last_stage or (changed is not None and changed > last_progress)):
                last_stage = stage
                last_progress = changed if changed is not None else now
            elapsed = now - started
            stalled = now - last_progress
            if elapsed > max_runtime_s:
                logger.warning(
                    "browser_ingestion_timeout reason=wall_clock pid=%s elapsed_s=%.1f stage=%s root_private_mb=%s",
                    process.pid,
                    elapsed,
                    last_stage or "startup",
                    process_private_memory_mb(process.pid),
                )
                raise BrowserIngestionTimeout(
                    f"browser ingestion exceeded {max_runtime_s:.0f}s wall clock at stage={last_stage or 'startup'}"
                )
            if stalled > stall_timeout_s:
                logger.warning(
                    "browser_ingestion_timeout reason=stalled pid=%s elapsed_s=%.1f "
                    "stalled_s=%.1f stage=%s root_private_mb=%s",
                    process.pid,
                    elapsed,
                    stalled,
                    last_stage or "startup",
                    process_private_memory_mb(process.pid),
                )
                raise BrowserIngestionTimeout(
                    f"browser ingestion made no progress for {stall_timeout_s:.0f}s at stage={last_stage or 'startup'}"
                )
            time.sleep(0.5)
        payload = _read_json(result_path)
        if not isinstance(payload, dict):
            diagnostics.close() if diagnostics is not None else None
            tail = diagnostics.text() if diagnostics is not None else ""
            detail = f" stderr_tail={tail[-4000:]}" if tail else ""
            raise BrowserIngestionChildCrash(
                f"browser ingestion exited code={process.returncode} without a result.{detail}"
            )
        if not bool(payload.get("ok")):
            error_type = str(payload.get("error_type") or "BrowserIngestionError")
            message = str(payload.get("error") or "browser connector failed")
            if error_type.strip().lower() == "browserlaunchcrash":
                raise BrowserIngestionLaunchCrash(f"{error_type}: {message}")
            raise BrowserIngestionError(f"{error_type}: {message}")
        return payload.get("result")
    except Exception:
        if process is not None:
            _terminate_tree(process, job_handle)
            process = None
            job_handle = None
        if diagnostics is not None:
            diagnostics.close()
            tail = diagnostics.text()
            if tail:
                logger.warning(
                    "browser_ingestion_child_stderr fn=%s tail=%s",
                    getattr(fn, "__name__", str(fn)),
                    tail,
                )
        raise
    finally:
        if process is not None and process.poll() is None:
            _terminate_tree(process, job_handle)
            job_handle = None
        _close_windows_job(job_handle)
        if diagnostics is not None:
            diagnostics.close()
        _terminate_owned_windows_processes(job_dir, reason="job_finalizer")
        try:
            shutil.rmtree(job_dir)
        except OSError:
            logger.warning("browser ingestion workdir cleanup failed path=%s", job_dir, exc_info=True)
