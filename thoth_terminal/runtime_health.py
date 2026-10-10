from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from thoth_terminal.runtime_env import BOOT_ID, STARTED_AT_UTC
from thoth_terminal.runtime_info import runtime_state_dir

_publication_lock = threading.Lock()
_sharing_retry_delays = (0.01, 0.02, 0.04, 0.08, 0.16)
logger = logging.getLogger("thoth.runtime_health")


def _heartbeat_file_opener(path: str, flags: int) -> int:
    """Allow an atomic publisher to replace a file while Windows reads it."""
    if os.name != "nt":
        return os.open(path, flags)
    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    # GENERIC_READ; FILE_SHARE_READ | WRITE | DELETE; OPEN_EXISTING.
    handle = create_file(path, 0x80000000, 0x7, None, 3, 0x80, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return msvcrt.open_osfhandle(int(handle), os.O_RDONLY | os.O_BINARY)
    except BaseException:
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle(handle)
        raise


def _sharing_error(exc: OSError) -> bool:
    return isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in {5, 32, 33}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def service_heartbeat_path(service: str, *, settings_obj: object | None = None) -> Path:
    name = "".join(ch for ch in str(service or "").strip().lower() if ch.isalnum() or ch in {"_", "-"})
    if not name:
        raise ValueError("Missing service name.")
    return runtime_state_dir(settings_obj=settings_obj) / f"{name}-health.json"


def write_service_heartbeat(
    service: str,
    *,
    status: str,
    details: dict[str, Any] | None = None,
    settings_obj: object | None = None,
) -> Path:
    """Atomically publish event-loop/service health for an external supervisor."""
    path = service_heartbeat_path(service, settings_obj=settings_obj)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Serialize same-process writers so an older job heartbeat cannot publish
    # after a newer one. A unique file also isolates other publisher processes.
    with _publication_lock:
        payload: dict[str, Any] = {
            "service": str(service),
            "pid": os.getpid(),
            "boot_id": BOOT_ID,
            "started_at_utc": STARTED_AT_UTC,
            "heartbeat_at_utc": utc_now_iso(),
            "status": str(status or "unknown"),
        }
        supervision_id = os.environ.get("THOTH_RUNTIME_SUPERVISION_ID")
        if supervision_id:
            payload["supervision_id"] = supervision_id
        if details:
            payload["details"] = details
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent,
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2, default=str)
                stream.write("\n")
            for attempt in range(len(_sharing_retry_delays) + 1):
                try:
                    os.replace(temp_path, path)
                    break
                except OSError as exc:
                    if not _sharing_error(exc) or attempt == len(_sharing_retry_delays):
                        raise
                    time.sleep(_sharing_retry_delays[attempt])
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                logger.warning("heartbeat temporary file cleanup failed path=%s", temp_path, exc_info=True)
    return path


def read_service_heartbeat(
    service: str,
    *,
    settings_obj: object | None = None,
) -> dict[str, Any] | None:
    path = service_heartbeat_path(service, settings_obj=settings_obj)
    for attempt in range(len(_sharing_retry_delays) + 1):
        try:
            with open(path, "r", encoding="utf-8", opener=_heartbeat_file_opener) as stream:
                payload = json.load(stream)
            return payload if isinstance(payload, dict) else None
        except OSError as exc:
            if not _sharing_error(exc) or attempt == len(_sharing_retry_delays):
                return None
            time.sleep(_sharing_retry_delays[attempt])
        except ValueError:
            return None
    return None


def heartbeat_age_seconds(payload: dict[str, Any] | None, *, now: datetime | None = None) -> float | None:
    if not payload:
        return None
    raw = str(payload.get("heartbeat_at_utc") or "").strip()
    if not raw:
        return None
    try:
        observed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return max(0.0, (current.astimezone(timezone.utc) - observed.astimezone(timezone.utc)).total_seconds())
