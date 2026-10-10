from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import platform
import re
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from sqlalchemy import text
except ImportError:  # Standalone worker-host captures do not require database support.
    text = None

from thoth_terminal.runtime_processes import hidden_subprocess_kwargs
from thoth_terminal.process_metrics import sample_process_resources, sample_system_resources


DEFAULT_PROCESS_PATTERNS = (
    "thoth_terminal",
    "thoth-local-agent-worker",
    "local_agent_worker",
    "local_agent_worker_standalone",
    "llama-server",
    "vllm",
    "sglang",
    "uvicorn",
)
EVENT_ACTION_KINDS = {
    "run_start",
    "run_finish",
    "document_queue_read",
    "batch_plan_read",
    "media_transcription_queue_read",
    "deep_analysis_queue_read",
    "durable_review_queue_read",
}
RUN_STATS_KEEP_MARKERS = (
    "batch",
    "document",
    "durable",
    "media",
    "model",
    "ocr",
    "queue",
    "stage",
    "vlm",
)
SENSITIVE_PATTERNS = (
    re.compile(r"(--thoth-token\s+)(\S+)", re.IGNORECASE),
    re.compile(r"(--token\s+)(\S+)", re.IGNORECASE),
    re.compile(r"(--api-key\s+)(\S+)", re.IGNORECASE),
    re.compile(r"(authorization:\s*bearer\s+)(\S+)", re.IGNORECASE),
    re.compile(r"(THOTH_AGENT_API_TOKEN=)(\S+)", re.IGNORECASE),
    re.compile(r"(THOTH_TOKEN=)(\S+)", re.IGNORECASE),
)
EXTERNAL_PROCESS_LIMIT = 12
TELEMETRY_PROCESS_MARKERS = ("compattelemetry", "diagtrack", "telemetry", "werfault", "devicecensus")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime | None = None) -> str:
    return (value or utcnow()).isoformat().replace("+00:00", "Z")


def parse_iso_utc(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _safe_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except Exception:
        return None


def _safe_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(str(value).strip())
    except Exception:
        try:
            return int(float(str(value).strip()))
        except Exception:
            return None


def _process_identity(proc: dict[str, Any]) -> tuple[int | None, str | None]:
    creation = proc.get("creation_time")
    if creation is None:
        creation = proc.get("created_at")
    return _safe_int(proc.get("pid")), str(creation) if creation is not None else None


def _native_resources(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Augment discovery without spawning another process or rounding counters."""
    native_by_pid: dict[int, dict[str, Any]] = {}
    for group in ("processes", "external_processes"):
        for proc in snapshot.get(group) or []:
            pid = _safe_int(proc.get("pid"))
            if pid is None:
                continue
            if pid not in native_by_pid:
                native_by_pid[pid] = sample_process_resources(pid)
            native = native_by_pid[pid]
            proc["resource_source"] = native.get("source")
            proc["resource_errors"] = native.get("errors") or {}
            proc["resource_availability"] = native.get("availability") or {}
            for key, value in native.items():
                if key in {"pid", "source", "errors", "availability"}:
                    continue
                if value is not None:
                    proc["cpu_time_seconds" if key == "cpu_time_s" else key] = value
    native_system = sample_system_resources()
    system = snapshot.setdefault("system", {})
    for key in ("available_physical_bytes", "committed_bytes", "commit_limit_bytes"):
        system[key] = native_system.get(key)
    system["resource_errors"] = native_system.get("errors") or {}
    system["resource_availability"] = native_system.get("availability") or {}
    return snapshot


def redact_command_line(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    for pattern in SENSITIVE_PATTERNS:
        text = pattern.sub(r"\1<redacted>", text)
    return text


def _run_command(cmd: list[str], *, timeout_s: float = 10.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_s,
        check=False,
        **hidden_subprocess_kwargs(),
    )


def _powershell_json(script: str, *, timeout_s: float = 15.0) -> Any:
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    exe = "powershell.exe" if os.name == "nt" else "pwsh"
    args = [exe, "-NoProfile"]
    if os.name == "nt":
        args.extend(["-WindowStyle", "Hidden"])
    args.extend(["-EncodedCommand", encoded])
    proc = _run_command(args, timeout_s=timeout_s)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "").strip())
    text = (proc.stdout or "").strip()
    if not text:
        return None
    start = min([idx for idx in (text.find("{"), text.find("[")) if idx >= 0] or [0])
    return json.loads(text[start:])


def _powershell_string_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _process_regex(patterns: tuple[str, ...] | list[str] | None, explicit: str | None) -> str:
    if explicit:
        return str(explicit)
    parts = [re.escape(part) for part in (patterns or DEFAULT_PROCESS_PATTERNS) if str(part).strip()]
    return "|".join(parts) or "thoth_terminal|local_agent_worker"


def _counter_map(rows: list[dict[str, Any]]) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for row in rows:
        path = str(row.get("path") or "").lower()
        value = _safe_float(row.get("value"))
        if "processor" in path and "% processor time" in path:
            out["cpu_total_pct"] = value
        elif "available mbytes" in path:
            out["memory_available_mb"] = value
        elif "committed bytes" in path:
            out["memory_committed_bytes"] = value
        elif "% disk time" in path:
            out["disk_active_pct"] = value
        elif "disk read bytes/sec" in path:
            out["disk_read_bytes_sec"] = value
        elif "disk write bytes/sec" in path:
            out["disk_write_bytes_sec"] = value
        elif "avg. disk queue length" in path:
            out["disk_queue_length"] = value
    return out


def sample_windows(pattern_regex: str, *, external_limit: int = EXTERNAL_PROCESS_LIMIT) -> dict[str, Any]:
    script = f"""
$ErrorActionPreference = 'Continue'
$regex = {_powershell_string_literal(pattern_regex)}
$counterPaths = @(
  '\\Processor(_Total)\\% Processor Time',
  '\\Memory\\Available MBytes',
  '\\Memory\\Committed Bytes',
  '\\PhysicalDisk(_Total)\\% Disk Time',
  '\\PhysicalDisk(_Total)\\Disk Read Bytes/sec',
  '\\PhysicalDisk(_Total)\\Disk Write Bytes/sec',
  '\\PhysicalDisk(_Total)\\Avg. Disk Queue Length'
)
$counters = @()
try {{
  $counters = (Get-Counter -Counter $counterPaths -SampleInterval 1 -MaxSamples 1).CounterSamples |
    ForEach-Object {{ [ordered]@{{ path = $_.Path; value = $_.CookedValue }} }}
}} catch {{}}
$processes = @()
$processInventoryAvailable = $false
$external = @()
try {{
  $processes = Get-CimInstance Win32_Process -ErrorAction Stop |
    Where-Object {{
      ($_.Name -match $regex -or $_.CommandLine -match $regex) -and
      ($_.CommandLine -notmatch 'Get-CimInstance Win32_Process')
    }} |
    Select-Object -First 120 |
    ForEach-Object {{
      [ordered]@{{
        pid = $_.ProcessId
        ppid = $_.ParentProcessId
        name = $_.Name
        executable_path = $_.ExecutablePath
        command_line = $_.CommandLine
        working_set_bytes = $_.WorkingSetSize
        private_bytes = $_.PrivatePageCount
        kernel_time_100ns = $_.KernelModeTime
        user_time_100ns = $_.UserModeTime
        created_at = if ($_.CreationDate) {{ $_.CreationDate.ToUniversalTime().ToString('o') }} else {{ $null }}
      }}
    }}
  $processInventoryAvailable = $true
}} catch {{}}
try {{
  $allExternal = @(Get-Process -ErrorAction SilentlyContinue)
  $external = @(
    $allExternal | Where-Object {{ $_.ProcessName -match 'compattelemetry|diagtrack|telemetry|werfault|devicecensus' }}
    $allExternal | Sort-Object -Property CPU -Descending | Select-Object -First {max(1, min(EXTERNAL_PROCESS_LIMIT, int(external_limit)))}
    $allExternal | Sort-Object -Property WorkingSet64 -Descending | Select-Object -First {max(1, min(EXTERNAL_PROCESS_LIMIT, int(external_limit)))}
  ) | Group-Object -Property Id | ForEach-Object {{ $_.Group[0] }} |
    Select-Object -First {max(1, min(EXTERNAL_PROCESS_LIMIT, int(external_limit)))} |
    ForEach-Object {{
      [ordered]@{{
        pid = $_.Id; name = $_.ProcessName; working_set_bytes = $_.WorkingSet64
        private_bytes = $_.PrivateMemorySize64; cpu_time_seconds = $_.CPU
        executable_path = try {{ $_.Path }} catch {{ $null }}
      }}
    }}
}} catch {{}}
[ordered]@{{
  processor_count = [Environment]::ProcessorCount
  counters = @($counters)
  processes = @($processes)
  process_inventory_available = $processInventoryAvailable
  external_processes = @($external)
}} | ConvertTo-Json -Depth 6
"""
    payload = _powershell_json(script, timeout_s=30.0)
    data = dict(payload or {}) if isinstance(payload, dict) else {}
    processes: list[dict[str, Any]] = []
    for row in data.get("processes") or []:
        if not isinstance(row, dict):
            continue
        kernel = _safe_float(row.get("kernel_time_100ns")) or 0.0
        user = _safe_float(row.get("user_time_100ns")) or 0.0
        processes.append(
            {
                "pid": _safe_int(row.get("pid")),
                "ppid": _safe_int(row.get("ppid")),
                "name": row.get("name"),
                "executable_path": row.get("executable_path"),
                "command_line": redact_command_line(row.get("command_line")),
                "working_set_bytes": _safe_int(row.get("working_set_bytes")),
                "private_bytes": _safe_int(row.get("private_bytes")),
                "cpu_time_seconds": (kernel + user) / 10_000_000.0,
                "created_at": row.get("created_at"),
            }
        )
    counter_rows = [dict(row) for row in data.get("counters") or [] if isinstance(row, dict)]
    external: list[dict[str, Any]] = []
    for row in data.get("external_processes") or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "")
        external.append({
            "pid": _safe_int(row.get("pid")), "name": name,
            "working_set_bytes": _safe_int(row.get("working_set_bytes")),
            "private_bytes": _safe_int(row.get("private_bytes")),
            "cpu_time_seconds": _safe_float(row.get("cpu_time_seconds")),
            "executable_path": redact_command_line(row.get("executable_path")),
            "category": "windows_telemetry" if any(x in name.lower() for x in TELEMETRY_PROCESS_MARKERS) else "external",
        })
    return _native_resources({
        "system": _counter_map(counter_rows),
        "processes": processes,
        "process_inventory_available": data.get("process_inventory_available", False),
        "external_processes": external,
        "processor_count": _safe_int(data.get("processor_count")) or (os.cpu_count() or 1),
    })


def sample_posix(pattern_regex: str) -> dict[str, Any]:
    proc = _run_command(["ps", "-eo", "pid=,ppid=,pcpu=,rss=,comm=,args="], timeout_s=10.0)
    processes: list[dict[str, Any]] = []
    if proc.returncode == 0:
        regex = re.compile(pattern_regex, re.IGNORECASE)
        for line in proc.stdout.splitlines():
            parts = line.strip().split(None, 5)
            if len(parts) < 6:
                continue
            pid, ppid, pcpu, rss_kb, name, command_line = parts
            if not regex.search(name) and not regex.search(command_line):
                continue
            processes.append(
                {
                    "pid": _safe_int(pid),
                    "ppid": _safe_int(ppid),
                    "name": name,
                    "command_line": redact_command_line(command_line),
                    "working_set_bytes": (_safe_int(rss_kb) or 0) * 1024,
                    "private_bytes": None,
                    "cpu_percent": _safe_float(pcpu),
                }
            )
    system: dict[str, float | None] = {}
    try:
        load1, load5, load15 = os.getloadavg()
        system.update({"load1": load1, "load5": load5, "load15": load15})
    except Exception:
        pass
    return _native_resources({"system": system, "processes": processes, "process_inventory_available": proc.returncode == 0, "processor_count": os.cpu_count() or 1})


def sample_gpu() -> list[dict[str, Any]]:
    proc = _run_command(
        [
            "nvidia-smi",
            "--query-gpu=index,name,memory.used,memory.total,utilization.gpu,utilization.memory,power.draw",
            "--format=csv,noheader,nounits",
        ],
        timeout_s=5.0,
    )
    if proc.returncode != 0:
        return []
    rows: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        parts = [part.strip() for part in line.split(",")]
        rows.append(
            {
                "index": _safe_int(parts[0]) if len(parts) > 0 else None,
                "name": parts[1] if len(parts) > 1 else None,
                "memory_used_mb": _safe_float(parts[2]) if len(parts) > 2 else None,
                "memory_total_mb": _safe_float(parts[3]) if len(parts) > 3 else None,
                "utilization_pct": _safe_float(parts[4]) if len(parts) > 4 else None,
                "memory_utilization_pct": _safe_float(parts[5]) if len(parts) > 5 else None,
                "power_w": _safe_float(parts[6]) if len(parts) > 6 else None,
            }
        )
    return rows


def default_model_state_paths() -> list[Path]:
    candidates: list[Path] = []
    env_path = str(os.environ.get("THOTH_MODEL_RUNTIME_STATE_PATH") or "").strip()
    if env_path:
        candidates.append(Path(env_path))
    try:
        from thoth_terminal.config import settings

        runtime_dir = Path(getattr(settings, "runtime_state_dir", "data/runtime"))
        candidates.extend(
            [
                runtime_dir / "local-model.json",
                runtime_dir / "model-runtime-state.json",
                runtime_dir / "raider-adaptive-stage-supervisor.json",
            ]
        )
    except Exception:
        candidates.extend(
            [
                Path("data/runtime/local-model.json"),
                Path("data/runtime/model-runtime-state.json"),
                Path("data/runtime/raider-adaptive-stage-supervisor.json"),
            ]
        )
    if os.name == "nt":
        candidates.append(
            Path.home() / "Documents/ThothTerminalAgent/runtime/model-runtime-state.json"
        )
    seen: set[str] = set()
    out: list[Path] = []
    for path in candidates:
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def read_json_file(path: Path) -> dict[str, Any] | None:
    try:
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    payload = dict(payload)
    payload["_path"] = str(path)
    return payload


def collect_runtime_states(model_state_path: Path | None = None) -> dict[str, Any]:
    runtime_states: list[dict[str, Any]] = []
    try:
        from thoth_terminal.runtime_info import list_runtime_states

        runtime_states = [dict(row) for row in list_runtime_states()[:40]]
    except Exception:
        runtime_states = []

    model_states: list[dict[str, Any]] = []
    paths = [model_state_path] if model_state_path else default_model_state_paths()
    for path in paths:
        if path is None:
            continue
        payload = read_json_file(Path(path))
        if payload:
            model_states.append(payload)

    return {"runtime_states": runtime_states, "model_states": model_states}


def _compact_stats(stats: object) -> dict[str, Any]:
    if not isinstance(stats, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in stats.items():
        key_text = str(key)
        low = key_text.lower()
        if not any(marker in low for marker in RUN_STATS_KEEP_MARKERS):
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[key_text] = value
        elif isinstance(value, list):
            out[key_text] = value[:12]
        elif isinstance(value, dict):
            out[key_text] = {str(k): v for k, v in list(value.items())[:20]}
        if len(out) >= 80:
            break
    return out


def collect_db_events(
    *,
    since: datetime,
    seen_keys: set[str],
    limit: int = 200,
) -> list[dict[str, Any]]:
    try:
        from thoth_terminal.db import session_factory
        from thoth_terminal.models import IngestionRun, LocalAgentAction, LocalAgentRun, Source, SourceState
    except Exception:
        return []

    events: list[dict[str, Any]] = []
    window_keys: set[str] = set()
    max_rows = max(1, min(int(limit), 500))
    SessionLocal = session_factory()
    try:
        with SessionLocal() as session:
            run_rows = (
                session.query(LocalAgentRun)
                .filter(LocalAgentRun.updated_at >= since.replace(tzinfo=None))
                .order_by(LocalAgentRun.updated_at.asc(), LocalAgentRun.id.asc())
                .limit(max_rows)
                .all()
            )
            for row in run_rows:
                key = f"local_agent_run:{row.id}:{row.updated_at}"
                window_keys.add(key)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                events.append(
                    {
                        "kind": "event",
                        "event_type": "local_agent_run",
                        "event_at": iso_utc(row.updated_at.replace(tzinfo=timezone.utc)),
                        "payload": {
                            "id": int(row.id),
                            "run_type": row.run_type,
                            "status": row.status,
                            "model_profile": row.model_profile,
                            "model_name": row.model_name,
                            "prompt_version": row.prompt_version,
                            "worker_id": row.worker_id,
                            "started_at": row.started_at,
                            "completed_at": row.completed_at,
                            "summary": row.summary,
                            "error": row.error,
                            "stats": _compact_stats(row.stats_json),
                        },
                    }
                )

            action_rows = (
                session.query(LocalAgentAction)
                .filter(LocalAgentAction.created_at >= since.replace(tzinfo=None))
                .filter(LocalAgentAction.action_kind.in_(sorted(EVENT_ACTION_KINDS)))
                .order_by(LocalAgentAction.created_at.asc(), LocalAgentAction.id.asc())
                .limit(max_rows)
                .all()
            )
            for row in action_rows:
                key = f"local_agent_action:{row.id}"
                window_keys.add(key)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                events.append(
                    {
                        "kind": "event",
                        "event_type": f"local_agent_{row.action_kind}",
                        "event_at": iso_utc(row.created_at.replace(tzinfo=timezone.utc)),
                        "payload": {
                            "id": int(row.id),
                            "run_id": row.run_id,
                            "status": row.status,
                            "tool_name": row.tool_name,
                            "endpoint": row.endpoint,
                            "document_id": row.document_id,
                            "summary": row.summary,
                            "request": row.request_json if isinstance(row.request_json, dict) else {},
                            "response": row.response_json if isinstance(row.response_json, dict) else {},
                            "error": row.error,
                        },
                    }
                )

            ingest_rows = (
                session.query(IngestionRun, Source)
                .join(Source, IngestionRun.source_id == Source.id)
                .filter(IngestionRun.started_at >= since.replace(tzinfo=None))
                .order_by(IngestionRun.started_at.asc(), IngestionRun.id.asc())
                .limit(max_rows)
                .all()
            )
            for row, source in ingest_rows:
                key = f"ingestion_run:{row.id}:{row.status}:{row.finished_at}"
                window_keys.add(key)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                events.append(
                    {
                        "kind": "event",
                        "event_type": "ingestion_source_poll",
                        "event_at": iso_utc(row.started_at.replace(tzinfo=timezone.utc)),
                        "payload": {
                            "id": int(row.id),
                            "source_id": int(row.source_id),
                            "source_name": source.name,
                            "connector": (source.config or {}).get("connector")
                            if isinstance(source.config, dict)
                            else None,
                            "status": row.status,
                            "started_at": row.started_at,
                            "finished_at": row.finished_at,
                            "items_seen": row.items_seen,
                            "items_new": row.items_new,
                            "items_updated": row.items_updated,
                            "error": row.error,
                            "extra": row.extra if isinstance(row.extra, dict) else {},
                        },
                    }
                )

            state_rows = (
                session.query(SourceState, Source)
                .join(Source, SourceState.source_id == Source.id)
                .filter(SourceState.updated_at >= since.replace(tzinfo=None))
                .order_by(SourceState.updated_at.asc(), SourceState.source_id.asc())
                .limit(max_rows)
                .all()
            )
            for row, source in state_rows:
                key = f"source_state:{row.source_id}:{row.updated_at}"
                window_keys.add(key)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                events.append(
                    {
                        "kind": "event",
                        "event_type": "source_state_update",
                        "event_at": iso_utc(row.updated_at.replace(tzinfo=timezone.utc)),
                        "payload": {
                            "source_id": int(row.source_id),
                            "source_name": source.name,
                            "last_run_at": row.last_run_at,
                            "last_success_at": row.last_success_at,
                            "next_run_at": row.next_run_at,
                            "consecutive_failures": row.consecutive_failures,
                            "backoff_until": row.backoff_until,
                        },
                    }
                )
    except Exception as exc:
        key = f"db_event_error:{type(exc).__name__}:{exc}"
        if key not in seen_keys:
            seen_keys.add(key)
            return [
                {
                    "kind": "event",
                    "event_type": "profiler_db_event_error",
                    "event_at": iso_utc(),
                    "payload": {"error": f"{type(exc).__name__}: {exc}"},
                }
            ]
        return []
    # Queries use a sliding lookback. Keys outside that window cannot recur;
    # retaining them for the full capture grows with historical activity.
    seen_keys.intersection_update(window_keys)
    return sorted(events, key=lambda item: str(item.get("event_at") or ""))


PG_OPTIONAL_COUNTERS = ("temp_blks_read", "temp_blks_written", "shared_blks_dirtied", "shared_blks_written",
                        "blk_read_time", "blk_write_time", "shared_blk_read_time", "shared_blk_write_time",
                        "temp_blk_read_time", "temp_blk_write_time")


def collect_pg_stat_statements(limit: int = 40) -> dict[str, dict[str, Any]] | None:
    """Return a small cumulative snapshot; absence/permissions are non-fatal."""
    if text is None:
        return None
    try:
        from thoth_terminal.db import session_factory
        SessionLocal = session_factory()
        with SessionLocal() as session:
            columns = set(session.execute(text("""
                SELECT attname FROM pg_attribute
                WHERE attrelid = to_regclass('pg_stat_statements') AND attnum > 0 AND NOT attisdropped
            """)).scalars())
            optional = "".join(f", {field}" for field in PG_OPTIONAL_COUNTERS if field in columns)
            rows = session.execute(text(f"""
                SELECT queryid::text, calls, total_exec_time, rows,
                       shared_blks_hit, shared_blks_read{optional},
                       current_setting('block_size')::integer AS block_size_bytes,
                       current_setting('track_io_timing')::boolean AS io_timing_enabled,
                       left(regexp_replace(query, '\\s+', ' ', 'g'), 240) AS query
                FROM pg_stat_statements
                WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
                ORDER BY total_exec_time DESC LIMIT :limit
            """), {"limit": max(1, min(int(limit), 100))}).mappings()
            return {str(row["queryid"]): dict(row) for row in rows}
    except Exception:
        return None


def pg_stat_delta(start: dict[str, dict[str, Any]] | None, end: dict[str, dict[str, Any]] | None) -> dict[str, Any]:
    if not end:
        return {"available": False, "queries": []}
    start = start or {}
    queries: list[dict[str, Any]] = []
    for queryid, row in end.items():
        before = start.get(queryid) or {}
        delta = {"queryid": queryid, "query": row.get("query"), "baseline_present": queryid in start,
                 "block_size_bytes": row.get("block_size_bytes"), "io_timing_enabled": row.get("io_timing_enabled"),
                 "counter_reset_or_eviction": False}
        for field in ("calls", "rows", "shared_blks_hit", "shared_blks_read"):
            difference = (_safe_int(row.get(field)) or 0) - (_safe_int(before.get(field)) or 0)
            delta[field] = difference if difference >= 0 else None
            delta["counter_reset_or_eviction"] |= difference < 0
        difference = (_safe_float(row.get("total_exec_time")) or 0.0) - (_safe_float(before.get("total_exec_time")) or 0.0)
        delta["total_exec_time"] = round(difference, 3) if difference >= 0 else None
        delta["counter_reset_or_eviction"] |= difference < 0
        for field in PG_OPTIONAL_COUNTERS:
            value = row.get(field)
            previous = before.get(field)
            if value is None or (before and previous is None) or (field.endswith("time") and row.get("io_timing_enabled") is not True):
                delta[field] = None
            else:
                difference = value - (previous or 0)
                delta[field] = (round(difference, 3) if isinstance(difference, float) else difference) if difference >= 0 else None
                delta["counter_reset_or_eviction"] |= difference < 0
        block_size = _safe_int(row.get("block_size_bytes"))
        for field in ("temp_blks_read", "temp_blks_written", "shared_blks_read", "shared_blks_written"):
            count = delta.get(field)
            delta[f"{field}_bytes"] = count * block_size if count is not None and block_size else None
        if (delta["calls"] or 0) > 0 or (delta["total_exec_time"] or 0) > 0 or delta["counter_reset_or_eviction"]:
            queries.append(delta)
    queries.sort(key=lambda row: float(row["total_exec_time"] or 0), reverse=True)
    return {"available": True, "baseline_available": bool(start), "queries": queries[:20],
            "io_semantics": "Block counts multiplied by configured block size; reads may hit OS cache. Timing unavailable when track_io_timing is disabled."}


def report_profile(path: Path) -> dict[str, Any]:
    """Read a completed or interrupted capture, including its rotated siblings."""
    candidates = sorted(path.parent.glob(f"{path.stem}*{path.suffix}")) if path.exists() else []
    if not candidates:
        candidates = [path]
    latest_summary: dict[str, Any] | None = None
    sample_count = 0
    started_at = completed_at = None
    for candidate in candidates:
        try:
            with candidate.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(row, dict):
                        continue
                    kind = row.get("kind")
                    if kind == "summary" and (latest_summary is None or str(row.get("completed_at") or "") >= str(latest_summary.get("completed_at") or "")):
                        latest_summary = row
                    elif kind == "sample":
                        sample_count += 1
                        stamp = row.get("captured_at")
                        if stamp and (completed_at is None or stamp > completed_at):
                            completed_at = stamp
                    elif kind == "session_start":
                        stamp = row.get("captured_at")
                        if stamp and (started_at is None or stamp < started_at):
                            started_at = stamp
        except OSError:
            continue
    if latest_summary is not None:
        result = dict(latest_summary)
        result["capture_files"] = [str(item) for item in candidates]
        return result
    return {
        "kind": "report", "complete": False, "sample_count": sample_count,
        "started_at": started_at,
        "completed_at": completed_at,
        "capture_files": [str(item) for item in candidates],
        "note": "Capture ended without a summary; raw samples remain readable.",
    }


PROCESS_COUNTERS = ("cpu_time_seconds", "user_time_s", "kernel_time_s", "cpu_cycles", "io_read_bytes", "io_write_bytes", "io_other_bytes", "io_read_operations", "io_write_operations", "io_other_operations")
PROCESS_GAUGES = ("private_bytes", "working_set_bytes", "peak_working_set_bytes", "handle_count", "cpu_percent")
SYSTEM_GAUGES = ("cpu_total_pct", "disk_active_pct", "disk_read_bytes_sec", "disk_write_bytes_sec", "disk_queue_length", "memory_available_mb", "available_physical_bytes", "committed_bytes", "commit_limit_bytes")


class _ProfileTotals:
    """Bounded online summary; full-fidelity samples live only in JSONL."""

    def __init__(self, external_limit: int):
        self.sample_count = 0
        self.event_count = 0
        self.event_counts: dict[str, int] = {}
        self.system: dict[str, dict[str, Any]] = {}
        self.processes: dict[tuple[int | None, str | None], dict[str, Any]] = {}
        self.process_observations_omitted = 0
        self.external: dict[tuple[int | None, str | None], dict[str, Any]] = {}
        self.external_observations_omitted = 0
        self.external_limit = max(1, min(EXTERNAL_PROCESS_LIMIT, external_limit))
        self.gpu: dict[str, float | None] = {"memory_used_mb_max": None, "utilization_pct_max": None}

    def add_sample(self, sample: dict[str, Any]) -> None:
        self.sample_count += 1
        for field in SYSTEM_GAUGES:
            value = (sample.get("system") or {}).get(field)
            if not isinstance(value, (int, float)):
                continue
            row = self.system.setdefault(field, {"min": value, "max": value, "sum": 0, "count": 0})
            row["min"], row["max"] = min(row["min"], value), max(row["max"], value)
            row["sum"] += value
            row["count"] += 1
        for proc in sample.get("processes") or []:
            if not isinstance(proc, dict):
                continue
            key = _process_identity(proc)
            if key not in self.processes:
                if len(self.processes) >= 80:
                    self.process_observations_omitted += 1
                    continue
                self.processes[key] = {"pid": proc.get("pid"), "name": proc.get("name"), "creation_time": proc.get("creation_time"), "created_at": proc.get("created_at"), **{f"{field}_max": None for field in PROCESS_GAUGES}, **{f"{field}_delta": None for field in PROCESS_COUNTERS}}
            row = self.processes[key]
            for field in PROCESS_GAUGES:
                value = proc.get(field)
                if isinstance(value, (int, float)):
                    max_key = f"{field}_max"
                    row[max_key] = value if row[max_key] is None else max(row[max_key], value)
            for field in PROCESS_COUNTERS:
                delta_key = f"{field}_delta"
                value = proc.get(delta_key)
                if isinstance(value, (int, float)):
                    row[delta_key] = (row[delta_key] or 0) + value
        for proc in sample.get("external_processes") or []:
            key = _process_identity(proc)
            if key not in self.external and len(self.external) >= 120:
                self.external_observations_omitted += 1
                continue
            row = self.external.get(key) or {"pid": proc.get("pid"), "creation_time": proc.get("creation_time"), "created_at": proc.get("created_at"), "name": proc.get("name"), "category": proc.get("category"), "cpu_percent_max": 0.0, "working_set_bytes_max": 0}
            row["cpu_percent_max"] = max(row["cpu_percent_max"], _safe_float(proc.get("cpu_percent")) or 0.0)
            row["working_set_bytes_max"] = max(row["working_set_bytes_max"], _safe_int(proc.get("working_set_bytes")) or 0)
            self.external[key] = row
        for gpu in sample.get("gpu") or []:
            for field in ("memory_used_mb", "utilization_pct"):
                value = _safe_float(gpu.get(field))
                key = f"{field}_max"
                if value is not None:
                    self.gpu[key] = value if self.gpu[key] is None else max(self.gpu[key], value)

    def add_event(self, event: dict[str, Any]) -> None:
        self.event_count += 1
        kind = str(event.get("event_type") or "event")
        self.event_counts[kind] = self.event_counts.get(kind, 0) + 1

    def system_value(self, field: str, statistic: str) -> float | int | None:
        row = self.system.get(field)
        if not row:
            return None
        value = row["sum"] / row["count"] if statistic == "avg" else row[statistic]
        return round(value, 3) if isinstance(value, float) else value


@dataclass
class ProfilerConfig:
    duration_s: float = 30.0
    interval_s: float = 1.0
    output_path: Path | None = None
    device_label: str | None = None
    process_regex: str | None = None
    include_events: bool = True
    event_lookback_s: float = 120.0
    event_limit: int = 200
    model_state_path: Path | None = None
    max_file_bytes: int = 64 * 1024 * 1024
    max_capture_bytes: int = 512 * 1024 * 1024
    max_summary_bytes: int = 1024 * 1024
    external_process_limit: int = EXTERNAL_PROCESS_LIMIT
    include_postgres: bool = True


class CaptureBudgetExceeded(RuntimeError):
    """A record was refused before writing; finalize with write_summary()."""

    def __init__(self, record_kind: str, record_bytes: int):
        self.record_kind = record_kind
        self.record_bytes = record_bytes
        super().__init__(f"Capture byte budget reached before {record_kind} ({record_bytes} bytes)")


class ProfilerSession:
    def __init__(self, config: ProfilerConfig):
        if config.max_capture_bytes < 16 * 1024 or config.max_summary_bytes < 1024:
            raise ValueError("Capture budget must be at least 16 KiB; summary limit must be at least 1 KiB")
        self.config = config
        self.session_id = uuid.uuid4().hex
        self.hostname = socket.gethostname()
        self.device_label = config.device_label or self.hostname
        self.process_regex = _process_regex(DEFAULT_PROCESS_PATTERNS, config.process_regex)
        self.output_path = config.output_path or self.default_output_path()
        self._rotation_index = 0
        self.bytes_written = 0
        self.summary_reserve_bytes = min(config.max_summary_bytes, config.max_capture_bytes // 2)
        self.budget_exceeded: CaptureBudgetExceeded | None = None
        self._summary_written = False
        self._seen_db_event_keys: set[str] = set()
        self._seen_processes: set[tuple[int | None, str | None]] = set()
        self._last_process_cpu: dict[tuple[int | None, str | None], tuple[float, dict[str, Any]]] = {}
        self._last_external_cpu: dict[tuple[int | None, str | None], tuple[float, dict[str, Any]]] = {}
        self._last_runtime_fingerprint: str | None = None
        self._totals = _ProfileTotals(config.external_process_limit)

    def default_output_path(self) -> Path:
        stamp = utcnow().strftime("%Y%m%d-%H%M%S")
        try:
            from thoth_terminal.config import settings

            root = Path(getattr(settings, "data_dir", "data")) / "profiler"
        except Exception:
            root = Path("data") / "profiler"
        return root / f"thoth-profile-{self.device_label}-{stamp}.jsonl"

    def write_record(self, record: dict[str, Any]) -> None:
        if self.budget_exceeded is not None:
            raise self.budget_exceeded
        encoded = self._encode_record(record)
        if self.bytes_written + len(encoded) > self.config.max_capture_bytes - self.summary_reserve_bytes:
            self.budget_exceeded = CaptureBudgetExceeded(str(record.get("kind") or "record"), len(encoded))
            raise self.budget_exceeded
        self._write_encoded(encoded)

    @staticmethod
    def _encode_record(record: dict[str, Any]) -> bytes:
        return (json.dumps(record, ensure_ascii=False, default=str, sort_keys=True) + "\n").encode("utf-8")

    def _write_encoded(self, encoded: bytes) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        if self.output_path.exists() and self.output_path.stat().st_size >= max(1024, self.config.max_file_bytes):
            stamp = utcnow().strftime("%Y%m%d-%H%M%S-%f")
            while True:
                self._rotation_index += 1
                rotated = self.output_path.with_name(f"{self.output_path.stem}.{stamp}.{self._rotation_index:06d}{self.output_path.suffix}")
                if not rotated.exists():
                    break
            self.output_path.replace(rotated)
        with self.output_path.open("ab") as handle:
            handle.write(encoded)
        self.bytes_written += len(encoded)

    def write_summary(self, record: dict[str, Any]) -> dict[str, Any]:
        """Finalize within reserved space, even after a data record was refused."""
        if self._summary_written:
            raise RuntimeError("Capture summary already written")
        summary = dict(record)
        summary.setdefault("complete", True)
        summary.setdefault("truncated", False)
        if self.budget_exceeded is not None:
            summary.update(complete=False, truncated=True, stop_reason="capture_byte_budget",
                           budget_rejected_record_kind=self.budget_exceeded.record_kind,
                           budget_rejected_record_bytes=self.budget_exceeded.record_bytes)
        summary.update(max_capture_bytes=self.config.max_capture_bytes, summary_reserve_bytes=self.summary_reserve_bytes)
        omitted: list[str] = []
        protected = {"kind", "session_id", "complete", "truncated", "stop_reason", "sample_count", "event_count",
                     "max_capture_bytes", "summary_reserve_bytes", "capture_bytes_written", "summary_fields_omitted"}
        while True:
            summary["capture_bytes_written"] = self.bytes_written
            encoded = self._encode_record(summary)
            # Including the final record size needs a tiny fixed-point update.
            for _ in range(4):
                summary["capture_bytes_written"] = self.bytes_written + len(encoded)
                updated = self._encode_record(summary)
                if len(updated) == len(encoded):
                    encoded = updated
                    break
                encoded = updated
            if len(encoded) <= self.summary_reserve_bytes:
                break
            candidates = [key for key in summary if key not in protected]
            if not candidates:
                raise ValueError("Summary metadata exceeds reserved byte budget")
            largest = max(candidates, key=lambda key: len(self._encode_record({key: summary[key]})))
            del summary[largest]
            omitted.append(largest)
            summary.update(complete=False, truncated=True, summary_fields_omitted=omitted)
            summary.setdefault("stop_reason", "summary_byte_budget")
        self._write_encoded(encoded)
        self._summary_written = True
        return summary

    def _sample_os(self) -> dict[str, Any]:
        if os.name == "nt":
            return sample_windows(self.process_regex, external_limit=self.config.external_process_limit)
        return sample_posix(self.process_regex)

    def _annotate_process_cpu(self, snapshot: dict[str, Any], mono_now: float) -> None:
        processor_count = max(1, int(snapshot.get("processor_count") or os.cpu_count() or 1))
        for group, history in (("processes", self._last_process_cpu), ("external_processes", self._last_external_cpu)):
            current = set()
            for proc in snapshot.get(group) or []:
                if not isinstance(proc, dict):
                    continue
                identity = _process_identity(proc)
                if identity[0] is None or identity[1] is None:
                    # A PID alone cannot safely attribute cumulative counters.
                    continue
                current.add(identity)
                previous = history.get(identity)
                if previous is not None:
                    elapsed = max(0.001, mono_now - previous[0])
                    for field in PROCESS_COUNTERS:
                        value, before = proc.get(field), previous[1].get(field)
                        if isinstance(value, (int, float)) and isinstance(before, (int, float)) and value >= before:
                            proc[f"{field}_delta"] = value - before
                    cpu_delta = proc.get("cpu_time_seconds_delta")
                    if cpu_delta is not None:
                        proc["cpu_percent"] = round(cpu_delta / elapsed / processor_count * 100.0, 3)
                    proc["resource_interval_s"] = elapsed
                history[identity] = (mono_now, {field: proc.get(field) for field in PROCESS_COUNTERS})
            for identity in list(history):
                if identity not in current:
                    del history[identity]

    def _process_boundary_events(self, snapshot: dict[str, Any], *, first_sample: bool) -> list[dict[str, Any]]:
        if snapshot.get("process_inventory_available") is False:
            return []
        current = {
            _process_identity(proc): proc
            for proc in snapshot.get("processes") or []
            if isinstance(proc, dict) and proc.get("pid") is not None
        }
        events: list[dict[str, Any]] = []
        for identity, proc in sorted(current.items(), key=lambda item: str(item[0])):
            if identity in self._seen_processes:
                continue
            self._seen_processes.add(identity)
            events.append(
                {
                    "kind": "event",
                    "event_type": "process_present" if first_sample else "process_start",
                    "event_at": iso_utc(),
                    "payload": {
                        "pid": identity[0],
                        "name": proc.get("name"),
                        "command_line": proc.get("command_line"),
                        "created_at": proc.get("created_at"),
                        "creation_time": proc.get("creation_time"),
                    },
                }
            )

        missing = sorted((identity for identity in self._seen_processes if identity not in current), key=str)
        for identity in missing:
            self._seen_processes.remove(identity)
            events.append(
                {
                    "kind": "event",
                    "event_type": "process_stop",
                    "event_at": iso_utc(),
                    "payload": {"pid": identity[0], "process_identity_creation": identity[1]},
                }
            )
        return events

    def _runtime_boundary_events(self, runtime: dict[str, Any]) -> list[dict[str, Any]]:
        fingerprint = hashlib.sha256(json.dumps(runtime, sort_keys=True, default=str).encode("utf-8")).hexdigest()
        if fingerprint == self._last_runtime_fingerprint:
            return []
        self._last_runtime_fingerprint = fingerprint
        return [
            {
                "kind": "event",
                "event_type": "runtime_state_change",
                "event_at": iso_utc(),
                "runtime_fingerprint": fingerprint,
                "payload": runtime,
            }
        ]

    def collect_sample(self, index: int) -> dict[str, Any]:
        os_snapshot = self._sample_os()
        mono_now = time.monotonic()
        self._annotate_process_cpu(os_snapshot, mono_now)
        runtime = collect_runtime_states(self.config.model_state_path)
        sample = {
            "kind": "sample",
            "session_id": self.session_id,
            "device_label": self.device_label,
            "sample_index": index,
            "captured_at": iso_utc(),
            "system": os_snapshot.get("system") or {},
            "gpu": sample_gpu(),
            "processes": os_snapshot.get("processes") or [],
            "process_inventory_available": os_snapshot.get("process_inventory_available", True),
            "external_processes": os_snapshot.get("external_processes") or [],
            "runtime": runtime,
        }
        return sample

    def collect_events(self, since: datetime) -> list[dict[str, Any]]:
        if not self.config.include_events:
            return []
        return collect_db_events(
            since=since,
            seen_keys=self._seen_db_event_keys,
            limit=self.config.event_limit,
        )

    def run(self) -> dict[str, Any]:
        start = utcnow()
        pg_start = collect_pg_stat_statements() if self.config.include_postgres else None
        start_record = {
            "kind": "session_start",
            "session_id": self.session_id,
            "device_label": self.device_label,
            "hostname": self.hostname,
            "platform": platform.platform(),
            "captured_at": iso_utc(start),
            "config": {
                "duration_s": self.config.duration_s,
                "interval_s": self.config.interval_s,
                "process_regex": self.process_regex,
                "include_events": self.config.include_events,
                "event_lookback_s": self.config.event_lookback_s,
                "model_state_path": str(self.config.model_state_path)
                if self.config.model_state_path
                else None,
                "max_file_bytes": self.config.max_file_bytes,
                "max_capture_bytes": self.config.max_capture_bytes,
                "summary_reserve_bytes": self.summary_reserve_bytes,
                "external_process_limit": self.config.external_process_limit,
                "include_postgres": self.config.include_postgres,
            },
        }
        deadline = time.monotonic() + max(0.0, float(self.config.duration_s))
        event_since = start - timedelta(seconds=max(0.0, float(self.config.event_lookback_s)))
        index = 0
        first = True
        def write_events(events: list[dict[str, Any]]) -> None:
            for event in events:
                event["session_id"] = self.session_id
                event["device_label"] = self.device_label
                self.write_record(event)
                self._totals.add_event(event)

        try:
            self.write_record(start_record)
            while True:
                sample = self.collect_sample(index)
                # Full state is emitted once per change, before any referring
                # sample. The digest provides an explicit replay reference.
                write_events(self._runtime_boundary_events(sample.pop("runtime", {}) or {}))
                sample["runtime_fingerprint"] = self._last_runtime_fingerprint
                self.write_record(sample)
                self._totals.add_sample(sample)
                write_events(self._process_boundary_events(sample, first_sample=first))
                write_events(self.collect_events(event_since))
                first = False
                event_since = utcnow() - timedelta(seconds=3)
                index += 1
                if time.monotonic() >= deadline:
                    break
                remaining = max(0.0, deadline - time.monotonic())
                time.sleep(min(max(0.1, float(self.config.interval_s)), remaining))
        except CaptureBudgetExceeded:
            pass

        summary = self.summary(started_at=start, completed_at=utcnow())
        if self.config.include_postgres:
            summary["postgres"] = ({"available": False, "queries": [], "reason": "capture_byte_budget"}
                                   if self.budget_exceeded else pg_stat_delta(pg_start, collect_pg_stat_statements()))
        return self.write_summary(summary)

    def summary(self, *, started_at: datetime, completed_at: datetime) -> dict[str, Any]:
        totals = self._totals
        system = {}
        for field, statistic in (
            ("cpu_total_pct", "max"), ("cpu_total_pct", "avg"),
            ("disk_active_pct", "max"), ("disk_read_bytes_sec", "max"),
            ("disk_write_bytes_sec", "max"), ("disk_queue_length", "max"),
            ("memory_available_mb", "min"), ("available_physical_bytes", "min"),
            ("committed_bytes", "max"), ("commit_limit_bytes", "max"),
        ):
            system[f"{field}_{statistic}"] = totals.system_value(field, statistic)
        return {
            "kind": "summary",
            "session_id": self.session_id,
            "device_label": self.device_label,
            "hostname": self.hostname,
            "started_at": iso_utc(started_at),
            "completed_at": iso_utc(completed_at),
            "duration_s": round((completed_at - started_at).total_seconds(), 3),
            "output_path": str(self.output_path),
            "sample_count": totals.sample_count,
            "event_count": totals.event_count,
            "event_counts": totals.event_counts,
            "system": system,
            "gpu": totals.gpu,
            "process_peaks": list(totals.processes.values()),
            "process_summary_limit": 80,
            "process_observations_omitted": totals.process_observations_omitted,
            "external_offenders": sorted(totals.external.values(), key=lambda row: (row["cpu_percent_max"], row["working_set_bytes_max"]), reverse=True)[:totals.external_limit],
            "external_summary_limit": 120,
            "external_observations_omitted": totals.external_observations_omitted,
            "counter_delta_scope": "Adjacent samples of the same PID and creation time; unavailable counters remain null. Process I/O includes non-disk transfers.",
        }


def run_profiler(config: ProfilerConfig) -> dict[str, Any]:
    return ProfilerSession(config).run()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a lightweight, opt-in Thoth profiler capture.")
    parser.add_argument("--duration-s", type=float, default=30.0)
    parser.add_argument("--interval-s", type=float, default=1.0)
    parser.add_argument("--output", default="")
    parser.add_argument("--device-label", default="")
    parser.add_argument("--process-regex", default="")
    parser.add_argument("--no-events", action="store_true")
    parser.add_argument("--event-lookback-s", type=float, default=120.0)
    parser.add_argument("--event-limit", type=int, default=200)
    parser.add_argument("--model-state-path", default="")
    parser.add_argument("--compact", action="store_true")
    parser.add_argument("--max-file-mb", type=float, default=64.0)
    parser.add_argument("--max-capture-mb", type=float, default=512.0, help="Total bytes written by this invocation, including its final summary; existing history is preserved.")
    parser.add_argument("--max-summary-mb", type=float, default=1.0, help="Maximum reserved summary space, reduced to half of small capture budgets.")
    parser.add_argument("--external-process-limit", type=int, default=EXTERNAL_PROCESS_LIMIT)
    parser.add_argument("--no-postgres", action="store_true")
    parser.add_argument("--report", default="", help="Read and summarize an existing capture instead of sampling.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if str(args.report or "").strip():
        report = report_profile(Path(args.report))
        print(json.dumps(report, ensure_ascii=False, indent=None if args.compact else 2, default=str))
        return 0 if report.get("complete", True) else 2
    output = Path(args.output) if str(args.output or "").strip() else None
    model_state_path = Path(args.model_state_path) if str(args.model_state_path or "").strip() else None
    summary = run_profiler(
        ProfilerConfig(
            duration_s=max(0.0, float(args.duration_s)),
            interval_s=max(0.1, float(args.interval_s)),
            output_path=output,
            device_label=str(args.device_label or "").strip() or None,
            process_regex=str(args.process_regex or "").strip() or None,
            include_events=not bool(args.no_events),
            event_lookback_s=max(0.0, float(args.event_lookback_s)),
            event_limit=max(1, int(args.event_limit)),
            model_state_path=model_state_path,
            max_file_bytes=max(1024, int(float(args.max_file_mb) * 1024 * 1024)),
            max_capture_bytes=int(float(args.max_capture_mb) * 1024 * 1024),
            max_summary_bytes=int(float(args.max_summary_mb) * 1024 * 1024),
            external_process_limit=max(1, min(int(args.external_process_limit), 40)),
            include_postgres=not bool(args.no_postgres),
        )
    )
    print(json.dumps(summary, ensure_ascii=False, indent=None if args.compact else 2, default=str))
    return 0 if summary.get("complete", True) else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
