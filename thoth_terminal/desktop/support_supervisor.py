from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from thoth_terminal.desktop.instance import pid_alive
from thoth_terminal.runtime_env import repo_root
from thoth_terminal.runtime_health import write_service_heartbeat
from thoth_terminal.runtime_info import runtime_state_dir
from thoth_terminal.runtime_processes import run_hidden_process
from thoth_terminal.runtime_desired_state import DesiredStateRegistry, desired_state_registry


logger = logging.getLogger("thoth.desktop.support_supervisor")


@dataclass
class RepairTask:
    name: str
    callback: Callable[[], object]
    interval_seconds: float = 300.0
    next_run_monotonic: float = field(default=0.0)
    component_name: str | None = None

    def __post_init__(self) -> None:
        self.interval_seconds = max(30.0, float(self.interval_seconds))


def _parse_utc(raw: object) -> datetime | None:
    value = str(raw or "").strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def read_watchdog_state(*, state_dir: Path | None = None) -> dict[str, object] | None:
    path = (state_dir or runtime_state_dir()) / "watchdog.json"
    for attempt in range(3):
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            if attempt < 2 and path.exists():
                time.sleep(0.05)
                continue
            return None
        return payload if isinstance(payload, dict) else None
    return None


def watchdog_is_healthy(
    *,
    state_dir: Path | None = None,
    max_heartbeat_age_seconds: float = 90.0,
) -> bool:
    payload = read_watchdog_state(state_dir=state_dir)
    if not payload:
        return False
    try:
        process_id = int(payload.get("pid") or 0)
    except (TypeError, ValueError):
        return False
    if process_id <= 0 or not pid_alive(process_id):
        return False
    heartbeat = _parse_utc(payload.get("heartbeat_at_utc")) or _parse_utc(payload.get("started_at_utc"))
    if heartbeat is None:
        return False
    age = (datetime.now(timezone.utc) - heartbeat.astimezone(timezone.utc)).total_seconds()
    return age <= max(5.0, float(max_heartbeat_age_seconds))


def start_watchdog(*, state_dir: Path | None = None) -> int:
    root = repo_root()
    script = root / "scripts" / "thoth-watchdog.ps1"
    if not script.exists():
        raise FileNotFoundError(f"Missing watchdog script: {script}")
    resolved_state_dir = state_dir or runtime_state_dir()
    stale_state = read_watchdog_state(state_dir=resolved_state_dir)
    try:
        stale_pid = int((stale_state or {}).get("pid") or 0)
    except (TypeError, ValueError):
        stale_pid = 0
    if stale_pid > 0 and pid_alive(stale_pid):
        env = dict(os.environ)
        env["THOTH_STALE_WATCHDOG_PID"] = str(stale_pid)
        env["THOTH_WATCHDOG_SCRIPT_PATH"] = str(script)
        stop_script = (
            "$targetPid=[int]$env:THOTH_STALE_WATCHDOG_PID; "
            "$expected=[regex]::Escape($env:THOTH_WATCHDOG_SCRIPT_PATH); "
            "$proc=Get-CimInstance Win32_Process -Filter \"ProcessId=$targetPid\" -ErrorAction SilentlyContinue; "
            "if ($proc -and ([string]$proc.CommandLine -match $expected)) { "
            "Stop-Process -Id $targetPid -Force -ErrorAction SilentlyContinue }"
        )
        run_hidden_process(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", stop_script],
            cwd=root,
            env=env,
            timeout_s=10,
            check=False,
        )
    logs_dir = resolved_state_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["THOTH_WATCHDOG_SCRIPT_PATH"] = str(script)
    launch_script = (
        "$proc=Start-Process -FilePath 'powershell.exe' "
        "-ArgumentList @('-NoProfile','-NonInteractive','-WindowStyle','Hidden','-ExecutionPolicy','Bypass',"
        "'-File',$env:THOTH_WATCHDOG_SCRIPT_PATH) "
        "-WorkingDirectory $env:THOTH_WATCHDOG_REPO_ROOT -WindowStyle Hidden "
        "-PassThru; "
        "Write-Output $proc.Id"
    )
    env["THOTH_WATCHDOG_REPO_ROOT"] = str(root)
    result = run_hidden_process(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", launch_script],
        cwd=root,
        env=env,
        timeout_s=15,
        check=True,
    )
    process_id = int((result.stdout or "").strip().splitlines()[-1])
    logger.warning("desktop repaired missing/stale watchdog pid=%s", process_id)
    return process_id


class SupportSupervisor:
    """Secondary supervisor for watchdog and idempotent integration repair."""

    def __init__(
        self,
        *,
        poll_seconds: float = 30.0,
        max_heartbeat_age_seconds: float = 90.0,
        repair_tasks: list[RepairTask] | None = None,
        registry: DesiredStateRegistry | None = None,
    ) -> None:
        self.registry = registry or desired_state_registry()
        watchdog_spec = self.registry.component("watchdog")
        self.poll_seconds = max(5.0, float(poll_seconds))
        registry_max_age = float(watchdog_spec.probe.get("max_age_seconds") or max_heartbeat_age_seconds)
        self.max_heartbeat_age_seconds = max(15.0, float(registry_max_age))
        self.repair_tasks = list(repair_tasks or [])
        self.task_status: dict[str, dict[str, object]] = {}
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self, *, force_repairs: bool = False) -> bool:
        repaired_watchdog = False
        if not watchdog_is_healthy(max_heartbeat_age_seconds=self.max_heartbeat_age_seconds):
            start_watchdog()
            repaired_watchdog = True
        now = time.monotonic()
        registry_by_name = self.registry.by_name()
        for task in self.repair_tasks:
            component_name = str(task.component_name or task.name)
            component = registry_by_name.get(component_name)
            if not force_repairs and now < task.next_run_monotonic:
                continue
            interval = component.interval_seconds if component is not None else task.interval_seconds
            task.next_run_monotonic = now + interval
            try:
                result = task.callback()
                if isinstance(result, dict) and result.get("ok") is False:
                    raise RuntimeError(str(result.get("detail") or result.get("error") or result.get("status") or "repair failed"))
                self.task_status[task.name] = {
                    "ok": True,
                    "observed": "healthy",
                    "desired": component.desired if component is not None else "active",
                    "requires": list(component.requires) if component is not None else [],
                    "repair_action": str(component.repair.get("action")) if component is not None else "callback",
                    "checked_at": time.time(),
                }
            except Exception as exc:
                self.task_status[task.name] = {
                    "ok": False,
                    "observed": "degraded",
                    "desired": component.desired if component is not None else "active",
                    "requires": list(component.requires) if component is not None else [],
                    "repair_action": str(component.repair.get("action")) if component is not None else "callback",
                    "checked_at": time.time(),
                    "error": f"{type(exc).__name__}: {exc}",
                }
                logger.exception("desktop integration repair failed: %s", task.name)
        write_service_heartbeat(
            "desktop-supervisor",
            status="healthy" if all(bool(row.get("ok")) for row in self.task_status.values()) else "degraded",
            details={
                "desired_state_registry": self.registry.registry_version,
                "watchdog_repaired": repaired_watchdog,
                "repair_tasks": self.task_status,
            },
        )
        return repaired_watchdog

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                logger.exception("desktop support supervisor repair pass failed")
            self._stop_event.wait(self.poll_seconds)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="thoth-support-supervisor", daemon=True)
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, float(timeout)))
