"""Bounded, opt-in resource capture around one explicitly chosen workload.

Raw samples use the existing rotating profiler JSONL rail. No application data,
command lines or credentials are captured. Peaks are sampled lower bounds; CPU
and I/O deltas cover observed process lifetimes, not unobserved exited children.
"""
from __future__ import annotations

import math
import json
import os
from pathlib import Path
import threading
import time
import uuid
from typing import Callable, Any

from thoth_terminal.process_metrics import list_processes, sample_process_resources, sample_system_resources
from thoth_terminal.profiler import CaptureBudgetExceeded, ProfilerConfig, ProfilerSession, iso_utc


COUNTERS = ("cpu_time_s", "cpu_cycles", "io_read_bytes", "io_write_bytes", "io_other_bytes", "io_read_operations", "io_write_operations", "io_other_operations")
GAUGES = ("private_bytes", "working_set_bytes", "handle_count", "thread_count")


def latency_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Nearest-rank empirical quantiles, including failed/timed-out attempts."""
    values = sorted(float(row["wall_ms"]) for row in rows)
    def percentile(p: float) -> float | None:
        return values[max(0, math.ceil(len(values) * p) - 1)] if values else None
    return {
        "attempts": len(rows), "failures": sum(not row.get("ok", False) for row in rows),
        "p50_ms": percentile(.5), "p95_ms": percentile(.95), "p99_ms": percentile(.99),
        "max_ms": max(values) if values else None,
        "quantiles": "empirical nearest rank; small samples do not establish a production tail bound",
    }


def runtime_roots() -> dict[int, str]:
    """Read existing desktop state; never start services or persist new state."""
    from thoth_terminal.runtime_info import list_runtime_states
    roots: dict[int, str] = {}
    for row in list_runtime_states():
        pid = row.get("pid")
        if isinstance(pid, int) and pid > 0:
            roots[pid] = str(row.get("service") or Path(str(row.get("_path", "runtime"))).stem)
    return roots


def compare_workload_captures(baseline: Path, candidate: Path) -> list[dict[str, Any]]:
    """Stream completed workload summaries; changed responses remain explicit."""
    def read(path: Path) -> dict[str, dict[str, Any]]:
        summaries = {}
        candidates = sorted(path.parent.glob(f"{path.stem}.*{path.suffix}"))
        if path.exists():
            candidates.append(path)  # Active file follows its older rotations.
        if not candidates:
            raise FileNotFoundError(path)
        for file in candidates:
            with file.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # Interrupted final line is not a completed workload.
                    if isinstance(row, dict) and row.get("kind") == "workload_latency_summary":
                        if len(summaries) >= 256 and row["name"] not in summaries:
                            raise ValueError("comparison workload limit exceeded")
                        summaries[row["name"]] = row
        return summaries
    before, after = read(baseline), read(candidate)
    result = []
    for name in sorted(before.keys() & after.keys()):
        left, right = before[name], after[name]
        def comparable_inputs(row):
            return {key: value for key, value in row.get("inputs", {}).items() if key not in {"snapshot", "frozen_now"}}
        inputs_match = comparable_inputs(left) == comparable_inputs(right)
        hashes_match = "response_sha256" in left and "response_sha256" in right and left["response_sha256"] == right["response_sha256"]
        def peak(row):
            return row.get("resources", {}).get("phases", {}).get("active", {}).get("peaks", {}).get("private_bytes")
        old_peak, new_peak = peak(left), peak(right)
        result.append({"name": name, "inputs_match": inputs_match, "response_hashes_match": hashes_match,
                       "captures_complete": bool(left.get("resources", {}).get("complete") and right.get("resources", {}).get("complete")),
                       "interpretation": "paired configuration and response" if inputs_match and hashes_match else "descriptive only; configuration or response differs",
                       "baseline_failures": left["failures"], "candidate_failures": right["failures"],
                       "p50_ratio": right["p50_ms"] / left["p50_ms"] if left.get("p50_ms") else None,
                       "max_ratio": right["max_ms"] / left["max_ms"] if left.get("max_ms") else None,
                       "active_private_bytes_peak_delta": new_peak - old_peak if old_peak is not None and new_peak is not None else None})
    return result


def _tree_members(processes: list[dict[str, Any]], roots: dict[int, str]) -> dict[int, str]:
    members = dict(roots)
    # Fixed point includes wrapper -> interpreter -> listener children.
    for _ in range(len(processes)):
        additions = {
            int(row["pid"]): members[int(row["ppid"])]
            for row in processes if row.get("ppid") in members and row.get("pid") not in members
        }
        if not additions:
            break
        members.update(additions)
    return members


class ResourceCapture:
    """Finite capture with a joinable sampler and online, bounded summaries."""
    def __init__(
        self, output: Path, *, roots: Callable[[], dict[int, str]], interval_s: float = .1,
        min_available_bytes: int = 512 * 1024 * 1024, max_commit_fraction: float = .95,
        include_descendants: bool = True, discovery_interval_s: float = 1.0,
        writer: ProfilerSession | None = None,
        min_commit_headroom_bytes: int | None = None, max_private_growth_bytes: int | None = None,
    ):
        if min_commit_headroom_bytes is not None and min_commit_headroom_bytes < 0:
            raise ValueError("Commit headroom must be nonnegative bytes")
        if max_private_growth_bytes is not None and max_private_growth_bytes <= 0:
            raise ValueError("Private growth budget must be positive bytes")
        self._owns_writer = writer is None
        self.writer = writer or ProfilerSession(ProfilerConfig(output_path=output, include_events=False, include_postgres=False))
        self.capture_id = uuid.uuid4().hex
        self.roots = roots
        self.interval_s = max(.02, interval_s)
        self.min_available_bytes = min_available_bytes
        self.max_commit_fraction = max_commit_fraction
        self.min_commit_headroom_bytes = min_commit_headroom_bytes
        self.max_private_growth_bytes = max_private_growth_bytes
        self._baseline_private_bytes: int | None = None
        self.include_descendants = include_descendants
        self.discovery_interval_s = max(self.interval_s, discovery_interval_s)
        self._inventory: list[dict[str, Any]] = []
        self._inventory_at = float("-inf")
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._sampling_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._phase = "baseline"
        self._instances: dict[tuple[int, int], dict[str, Any]] = {}
        self._current: set[tuple[int, int]] = set()
        self._phase_totals: dict[str, dict[str, Any]] = {}
        self.sample_count = 0
        self.sampling_wall_s = 0.0
        self.pressure_reason: str | None = None
        self.failure: str | None = None
        self.started = 0.0
        self._closed = False
        self._final_summary: dict[str, Any] | None = None

    def record(self, record: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self._closed and record.get("kind") != "workload_resource_summary":
                raise RuntimeError("Resource capture is closed")
            payload = {"session_id": self.writer.session_id, "capture_id": self.capture_id, "captured_at": iso_utc(), **record}
            try:
                if record.get("kind") == "workload_resource_summary" and self._owns_writer:
                    return self.writer.write_summary(payload)
                else:
                    self.writer.write_record(payload)
            except CaptureBudgetExceeded:
                self.failure = "capture byte budget exceeded"
                self._stop.set()
                raise
            return payload

    def phase(self, name: str) -> None:
        with self._lock:
            self._phase = name
        self.record({"kind": "workload_phase", "phase": name})

    def snapshot(self) -> dict[str, Any]:
        with self._sampling_lock:
            return self._snapshot()

    def _snapshot(self) -> dict[str, Any]:
        began = time.perf_counter()
        roots = self.roots()
        if self.include_descendants:
            if began - self._inventory_at >= self.discovery_interval_s:
                self._inventory = list_processes()
                self._inventory_at = began
            inventory = self._inventory
            members = _tree_members(inventory, roots)
        else:
            inventory = [{"pid": pid, "name": role} for pid, role in roots.items()]
            members = roots
        inventory_by_pid = {row["pid"]: row for row in inventory}
        rows = []
        for pid, role in members.items():
            # Query only enumerated live instances; a stale state PID is not a live process.
            if pid not in inventory_by_pid:
                continue
            row = {**inventory_by_pid[pid], **sample_process_resources(pid), "role": role}
            rows.append(row)
        confirmed_exited_pids: set[int] = set()
        if self.max_private_growth_bytes is not None and any(row.get("private_bytes") is None for row in rows):
            # Cached inventory can contain short-lived Git/provider helpers.
            # Confirm disappearance; access denial on a live process still
            # leaves the configured private-memory budget unverifiable.
            refreshed = list_processes()
            live_pids = {row["pid"] for row in refreshed}
            confirmed_exited_pids = {row["pid"] for row in rows if row.get("private_bytes") is None and row["pid"] not in live_pids}
            if self.include_descendants:
                self._inventory, self._inventory_at = refreshed, time.perf_counter()
        collector = sample_process_resources(os.getpid())
        system = sample_system_resources()
        elapsed = time.perf_counter() - began
        with self._lock:
            phase = self._phase
            sample = {"kind": "resource_sample", "phase": phase, "elapsed_s": time.perf_counter() - self.started,
                      "inventory_age_s": max(0.0, began - self._inventory_at) if self.include_descendants else None,
                      "sampling_wall_s": elapsed, "processes": rows, "collector": collector, "system": system}
            sample["confirmed_exited_pids"] = sorted(confirmed_exited_pids)
            self.record(sample)
            current = set()
            totals = dict.fromkeys(GAUGES, 0)
            available_counts = dict.fromkeys(GAUGES, 0)
            for row in rows:
                identity = row.get("creation_time")
                if identity is None:
                    continue  # Raw errors remain; do not merge unidentifiable PID reuse.
                key = (row["pid"], identity)
                current.add(key)
                instance = self._instances.get(key)
                if instance is None:
                    if len(self._instances) >= 4096:
                        self.failure = "process instance limit reached; capture incomplete"
                        self._stop.set()
                        break
                    instance = {"pid": key[0], "creation_time": identity, "name": row.get("name"),
                                "role": row["role"], "first": row, "last": row, "peaks": {},
                                "first_phase": phase, "disappeared": False}
                    self._instances[key] = instance
                instance["last"] = row
                for field in GAUGES:
                    value = row.get(field)
                    if value is not None:
                        instance["peaks"][field] = max(instance["peaks"].get(field, value), value)
                        totals[field] += value
                        available_counts[field] += 1
            for key in self._current - current:
                self._instances[key]["disappeared"] = True
            self._current = current
            phase_summary = self._phase_totals.setdefault(phase, {"samples": 0, "first": None, "last": None, "peaks": {}})
            observations = phase_summary.setdefault("counter_observations", {})
            for row in rows:
                if row.get("creation_time") is None:
                    continue
                key = f"{row['pid']}:{row['creation_time']}"
                counters = {field: row.get(field) for field in COUNTERS}
                observed = observations.setdefault(key, {"first": counters, "last": counters})
                observed["last"] = counters
            aggregate = {field: totals[field] if available_counts[field] or not rows else None for field in GAUGES}
            aggregate["process_count"] = len(rows)
            phase_summary["available_counts_last"] = available_counts
            phase_summary["samples"] += 1
            phase_summary["first"] = phase_summary["first"] or aggregate
            phase_summary["last"] = aggregate
            for field, value in aggregate.items():
                if value is not None:
                    phase_summary["peaks"][field] = max(phase_summary["peaks"].get(field, value), value)
            free = system.get("available_physical_bytes")
            committed, limit = system.get("committed_bytes"), system.get("commit_limit_bytes")
            if free is not None and free < self.min_available_bytes:
                self.pressure_reason = "physical available bytes below capture floor"
            if self.min_commit_headroom_bytes is not None:
                if committed is None or limit is None:
                    self.pressure_reason = "system commit headroom unavailable"
                elif limit - committed < self.min_commit_headroom_bytes:
                    self.pressure_reason = "system commit headroom below capture floor"
            elif committed is not None and limit and committed / limit > self.max_commit_fraction:
                self.pressure_reason = "system commit fraction above capture ceiling"
            if self.max_private_growth_bytes is not None:
                private = aggregate["private_bytes"]
                live_rows = [row for row in rows if row["pid"] not in confirmed_exited_pids]
                if private is None or not live_rows or any(row.get("private_bytes") is None for row in live_rows):
                    self.pressure_reason = "selected process private bytes unavailable"
                else:
                    if self._baseline_private_bytes is None:
                        self._baseline_private_bytes = private
                    if private - self._baseline_private_bytes > self.max_private_growth_bytes:
                        self.pressure_reason = "selected process private growth above capture budget"
            if self.pressure_reason:
                self._stop.set()
            self.sample_count += 1
            self.sampling_wall_s += elapsed
        return sample

    def _run(self) -> None:
        try:
            while not self._stop.wait(self.interval_s):
                self.snapshot()
        except Exception as exc:
            self.failure = self.failure or f"sampler failed: {type(exc).__name__}"
            self._stop.set()

    def __enter__(self) -> ResourceCapture:
        self.started = time.perf_counter()
        try:
            self.record({"kind": "workload_capture_start", "interval_s": self.interval_s,
                         "discovery_interval_s": self.discovery_interval_s if self.include_descendants else None,
                         "min_available_bytes": self.min_available_bytes, "max_commit_fraction": self.max_commit_fraction,
                         "min_commit_headroom_bytes": self.min_commit_headroom_bytes,
                         "max_private_growth_bytes": self.max_private_growth_bytes,
                         "scope": "selected process trees; database/remote model CPU excluded unless selected",
                         "peak_semantics": "sampled lower bound; process working sets may share pages"})
            self.snapshot()
        except Exception as error:
            self.failure = self.failure or f"capture startup failed: {type(error).__name__}"
            self._stop.set()
            self._finalize()
            raise
        if not self._stop.is_set():
            self._thread = threading.Thread(target=self._run, name="thoth-resource-capture", daemon=True)
            self._thread.start()
        return self

    def check(self) -> None:
        if self.failure or self.pressure_reason:
            raise RuntimeError(self.failure or self.pressure_reason)

    def summary(self) -> dict[str, Any]:
        with self._lock:
            if self._final_summary is not None:
                return dict(self._final_summary)
            instances = []
            for instance in self._instances.values():
                first, last = instance["first"], instance["last"]
                deltas = {key: last[key] - first[key] if first.get(key) is not None and last.get(key) is not None and last[key] >= first[key] else None for key in COUNTERS}
                instances.append({key: value for key, value in instance.items() if key not in {"first", "last"}} | {
                    "counter_deltas": deltas, "start_private_bytes": first.get("private_bytes"),
                    "end_private_bytes": last.get("private_bytes"), "end_working_set_bytes": last.get("working_set_bytes"),
                    "counter_coverage": "first to last observation; exit tail and between-sample children unobserved",
                })
            phases = {}
            for name, phase in self._phase_totals.items():
                phases[name] = {key: value for key, value in phase.items() if key != "counter_observations"}
                deltas, available = {}, {}
                for field in COUNTERS:
                    values = [row["last"][field] - row["first"][field]
                              for row in phase.get("counter_observations", {}).values()
                              if row["first"][field] is not None and row["last"][field] is not None
                              and row["last"][field] >= row["first"][field]]
                    deltas[field] = sum(values) if values else None
                    available[field] = len(values)
                phases[name].update(counter_deltas=deltas, counter_instances_available=available)
            return {"kind": "workload_resource_summary", "sample_count": self.sample_count,
                    "duration_s": time.perf_counter() - self.started, "sampling_wall_s": self.sampling_wall_s,
                    "complete": not (self.failure or self.pressure_reason), "failure": self.failure,
                    "pressure_reason": self.pressure_reason, "phases": phases, "processes": instances[:256],
                    "process_instances_omitted": max(0, len(instances) - 256),
                    "capture_byte_budget_exceeded": self.writer.budget_exceeded is not None,
                    "truncated": self.writer.budget_exceeded is not None,
                    "stop_reason": "capture_byte_budget" if self.writer.budget_exceeded else "resource_pressure" if self.pressure_reason else "capture_failure" if self.failure else None}

    def _finalize(self) -> None:
        with self._lock:
            if self._final_summary is not None:
                return
            self._closed = True
            try:
                self._final_summary = self.record(self.summary())
            except CaptureBudgetExceeded:
                # Shared captures use the ordinary data allowance. The owner
                # includes these last metrics in its one reserved suite summary.
                self._final_summary = self.summary()

    def __exit__(self, exc_type, exc, traceback) -> None:
        if exc_type is not None and self.failure is None:
            self.failure = f"workload aborted: {exc_type.__name__}"
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                self.failure = "sampler did not stop within 5 seconds"
        if not self.writer.budget_exceeded and (self._thread is None or not self._thread.is_alive()):
            try:
                self.snapshot()
            except CaptureBudgetExceeded:
                pass  # Reserved summary records the truncated capture.
        self._finalize()
