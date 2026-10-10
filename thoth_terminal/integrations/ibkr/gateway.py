from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import requests
import urllib3

from thoth_terminal.config import settings
from thoth_terminal.runtime_processes import run_hidden_process, start_hidden_logged_process

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


DEFAULT_CLIENT_PORTAL_DIR = Path.home() / "ibkrclientportal"
MANAGED_GATEWAY_STATE = "ibkr_gateway.json"


def _gateway_root() -> Path:
    return Path(getattr(settings, "ibkr_clientportal_dir", DEFAULT_CLIENT_PORTAL_DIR) or DEFAULT_CLIENT_PORTAL_DIR)


def gateway_login_url(base_url: str | None = None) -> str:
    value = str(base_url or getattr(settings, "ibkr_gateway_base_url", "https://localhost:5000/v1/api") or "").rstrip("/")
    if value.lower().endswith("/v1/api"):
        value = value[: -len("/v1/api")]
    return value or "https://localhost:5000"


def gateway_browser_login_url(base_url: str | None = None) -> str:
    return gateway_login_url(base_url)


def gateway_sso_login_url(base_url: str | None = None) -> str:
    return f"{gateway_login_url(base_url).rstrip('/')}/sso/Login?forwardTo=22&RL=1&ip2loc=US"


def gateway_available(*, timeout_s: float = 1.0) -> bool:
    try:
        response = requests.get(gateway_login_url(), timeout=float(timeout_s), verify=False)
        return response.status_code < 500
    except Exception:
        return False


def wait_for_gateway_available(*, timeout_s: float = 30.0, stop_event: threading.Event | None = None) -> bool:
    deadline = time.time() + max(1.0, float(timeout_s))
    while time.time() < deadline:
        if stop_event is not None and stop_event.is_set():
            return False
        if gateway_available(timeout_s=1.0):
            return True
        if stop_event is None:
            time.sleep(0.5)
        elif stop_event.wait(0.5):
            return False
    return gateway_available(timeout_s=1.0)


def gateway_process_running() -> bool:
    return bool(gateway_process_ids())


def gateway_process_ids() -> list[int]:
    if os.name != "nt":
        return []
    root = _gateway_root()
    root_literal = str(root)
    try:
        ps = f"""
$rootPattern = [regex]::Escape({root_literal!r})
Get-CimInstance Win32_Process |
  Where-Object {{
    $_.Name -match 'java|cmd' -and
    (
      ([string]$_.CommandLine -match $rootPattern) -or
      ([string]$_.CommandLine -match 'ibkrclientportal|clientportal\\.gw')
    )
  }} |
  Select-Object -ExpandProperty ProcessId
"""
        proc = run_hidden_process(
            ["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass", "-Command", ps],
            check=False,
            timeout_s=5,
        )
    except Exception:
        return []
    pids: list[int] = []
    for line in (proc.stdout or "").splitlines():
        try:
            pids.append(int(str(line).strip()))
        except Exception:
            pass
    return pids


def managed_gateway_state_path() -> Path:
    return Path(settings.runtime_state_dir) / MANAGED_GATEWAY_STATE


def _write_managed_gateway_state(payload: dict[str, Any]) -> None:
    state_path = managed_gateway_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _clear_managed_gateway_state() -> None:
    try:
        managed_gateway_state_path().unlink(missing_ok=True)
    except Exception:
        pass


def _keep_gateway_running() -> bool:
    return bool(getattr(settings, "ibkr_keep_gateway_running", False))


def stop_managed_gateway() -> dict[str, Any]:
    if _keep_gateway_running():
        pids = gateway_process_ids()
        return {
            "ok": True,
            "status": "kept_running",
            "root": str(_gateway_root()),
            "pids": pids,
            "login_url": gateway_login_url(),
        }
    return stop_gateway_processes()


def stop_gateway_processes() -> dict[str, Any]:
    root = _gateway_root()
    if os.name != "nt":
        _clear_managed_gateway_state()
        return {"ok": False, "status": "unsupported_platform", "root": str(root)}

    before = gateway_process_ids()
    if not before:
        _clear_managed_gateway_state()
        return {"ok": True, "status": "no_gateway_processes", "root": str(root), "pids": []}

    ps = f"""
$rootPattern = [regex]::Escape({str(root)!r})
$markers = 'ibkrclientportal|clientportal\\.gw|conf\\.yaml|run\\.bat'
function Stop-Tree([int]$ProcessId) {{
  Get-CimInstance Win32_Process | Where-Object {{ $_.ParentProcessId -eq $ProcessId }} | ForEach-Object {{ Stop-Tree ([int]$_.ProcessId) }}
  Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
}}
$targets = Get-CimInstance Win32_Process |
  Where-Object {{
    $_.Name -match 'java|cmd' -and
    (
      ([string]$_.CommandLine -match $rootPattern) -or
      ([string]$_.CommandLine -match $markers)
    )
  }} |
  Select-Object -ExpandProperty ProcessId -Unique
foreach ($targetPid in $targets) {{ Stop-Tree ([int]$targetPid) }}
"""
    proc = run_hidden_process(
        ["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass", "-Command", ps],
        check=False,
        timeout_s=20,
    )
    after = gateway_process_ids()
    if proc.returncode in {0, 1} and not after:
        _clear_managed_gateway_state()
        return {"ok": True, "status": "stopped", "root": str(root), "pids": before}
    return {
        "ok": False,
        "status": "stop_failed",
        "root": str(root),
        "pids": before,
        "remaining_pids": after,
        "returncode": proc.returncode,
        "stderr": (proc.stderr or "").strip()[:500],
    }


def stop_managed_gateway_by_state() -> dict[str, Any]:
    state_path = managed_gateway_state_path()
    if not state_path.exists():
        return {"ok": True, "status": "no_managed_gateway"}
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception as exc:
        _clear_managed_gateway_state()
        return {"ok": False, "status": "invalid_state", "message": str(exc)}
    pid = int(state.get("pid") or 0)
    if pid <= 0:
        _clear_managed_gateway_state()
        return {"ok": True, "status": "missing_pid"}
    if os.name != "nt":
        _clear_managed_gateway_state()
        return {"ok": False, "status": "unsupported_platform"}

    ps = f"""
$root = [regex]::Escape({str(state.get("root") or "")!r})
$rootPattern = if ($root) {{ $root }} else {{ "ibkrclientportal|clientportal\\.gw|conf\\.yaml|run\\.bat" }}
function Stop-Tree([int]$ProcessId) {{
  Get-CimInstance Win32_Process | Where-Object {{ $_.ParentProcessId -eq $ProcessId }} | ForEach-Object {{ Stop-Tree ([int]$_.ProcessId) }}
  Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
}}
$proc = Get-CimInstance Win32_Process -Filter "ProcessId={pid}" -ErrorAction SilentlyContinue
if (-not $proc) {{ exit 0 }}
$line = [string]$proc.CommandLine
if ($line -notmatch $rootPattern -and $line -notmatch "ibkrclientportal|clientportal\\.gw|conf\\.yaml|run\\.bat") {{ exit 3 }}
Stop-Tree {pid}
"""
    proc = run_hidden_process(
        ["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass", "-Command", ps],
        check=False,
        timeout_s=15,
    )
    if proc.returncode in {0, 1}:
        _clear_managed_gateway_state()
        return {"ok": True, "status": "stopped", "pid": pid}
    return {
        "ok": False,
        "status": "stop_failed",
        "pid": pid,
        "returncode": proc.returncode,
        "stderr": (proc.stderr or "").strip()[:500],
    }


def launch_gateway_if_needed(*, logs_dir: Path | None = None, stop_event: threading.Event | None = None) -> dict[str, Any]:
    if stop_event is not None and stop_event.is_set():
        return {"ok": False, "status": "cancelled"}
    root = _gateway_root()
    if gateway_available(timeout_s=0.75):
        pids = gateway_process_ids()
        payload = {"ok": True, "status": "already_available", "login_url": gateway_login_url()}
        if pids:
            payload.update({"pid": pids[0], "root": str(root), "adopted": True})
            _write_managed_gateway_state(payload)
        return payload
    if gateway_process_running() and gateway_available(timeout_s=2.5):
        pids = gateway_process_ids()
        payload = {"ok": True, "status": "already_running", "login_url": gateway_login_url()}
        if pids:
            payload.update({"pid": pids[0], "root": str(root), "adopted": True})
            _write_managed_gateway_state(payload)
        return payload
    if gateway_process_running():
        if _keep_gateway_running():
            pids = gateway_process_ids()
            payload = {
                "ok": True,
                "status": "already_running_waiting",
                "ready": False,
                "root": str(root),
                "login_url": gateway_login_url(),
                "message": "IBKR Gateway process is running; keeping it alive for local iteration.",
            }
            if pids:
                payload.update({"pid": pids[0], "adopted": True})
                _write_managed_gateway_state(payload)
            return payload
        stop_payload = stop_gateway_processes()
        if not stop_payload.get("ok"):
            return {
                "ok": False,
                "status": "stale_gateway_stop_failed",
                "root": str(root),
                "login_url": gateway_login_url(),
                "stop": stop_payload,
            }

    run_bat = root / "bin" / "run.bat"
    conf = root / "root" / "conf.yaml"
    if not run_bat.exists() or not conf.exists():
        return {
            "ok": False,
            "status": "missing_gateway",
            "root": str(root),
            "message": "IBKR Client Portal Gateway files were not found.",
        }

    out_dir = Path(logs_dir or Path(settings.runtime_state_dir) / "logs")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    stdout_path = out_dir / f"ibkr-gateway-{stamp}.out.log"
    stderr_path = out_dir / f"ibkr-gateway-{stamp}.err.log"
    if stop_event is not None and stop_event.is_set():
        return {"ok": False, "status": "cancelled"}
    handle = start_hidden_logged_process(
        ["cmd.exe", "/c", str(run_bat), str(conf)],
        label="ibkr-gateway",
        cwd=str(root),
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )
    proc = handle.process
    payload = {
        "ok": True,
        "status": "launched",
        "pid": int(proc.pid),
        "root": str(root),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "login_url": gateway_login_url(),
        "stdout_log": str(stdout_path),
        "stderr_log": str(stderr_path),
    }
    if wait_for_gateway_available(timeout_s=35.0, stop_event=stop_event):
        payload["status"] = "launched_ready"
        payload["ready"] = True
    else:
        payload["ready"] = False
        payload["message"] = "IBKR Gateway process was launched, but localhost did not become ready before timeout."
    _write_managed_gateway_state(payload)
    return payload
