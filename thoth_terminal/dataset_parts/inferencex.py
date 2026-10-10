from __future__ import annotations

import hashlib
import json
import lzma
import math
import re
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from sqlalchemy.orm import Session

from thoth_terminal.config import settings
from thoth_terminal.dataset_parts.core import ensure_dataset_tables
from thoth_terminal.dataset_parts.mutations import (
    create_dataset,
    create_dataset_view,
    latest_dataset_version,
    publish_dataset_snapshot,
    replace_dataset_draft,
)
from thoth_terminal.db import utcnow
from thoth_terminal.models import Dataset, DatasetVersion


INFERENCEX_APP_REPO = "SemiAnalysisAI/InferenceX-app"
INFERENCEX_BENCHMARK_REPO = "SemiAnalysisAI/InferenceX"
INFERENCEX_DATASET_SLUG = "inferencex-upstream-benchmarks"
INFERENCEX_RELEASE_API = f"https://api.github.com/repos/{INFERENCEX_APP_REPO}/releases"
INFERENCEX_RELEASE_TAG_PREFIX = "db-dump/"
INFERENCEX_IMPORT_SCHEMA_VERSION = 3
AGENTX_API_URL = "https://inferencex.semianalysis.com/api/v1/benchmarks"
AGENTX_OPTIONS_URL = "https://inferencex.semianalysis.com/api/v1/views/options"
AGENTX_AVAILABILITY_URL = "https://inferencex.semianalysis.com/api/v1/availability"
AGENTX_SOURCE_SCHEMA_VERSION = "v1"


@dataclass(frozen=True)
class InferenceXReleaseAsset:
    name: str
    url: str
    size: int | None = None
    digest: str | None = None


@dataclass(frozen=True)
class InferenceXRelease:
    tag_name: str
    name: str
    published_at: str | None
    html_url: str | None
    assets: tuple[InferenceXReleaseAsset, ...]


def _inferencex_cache_root(cache_dir: Path | None = None) -> Path:
    return Path(cache_dir or settings.data_dir / "upstream" / "inferencex").resolve()


def _github_headers(token: str | None = None) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "ThothTerminal-InferenceX-Upstream",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def list_inferencex_db_dump_releases(*, limit: int = 20, github_token: str | None = None) -> list[InferenceXRelease]:
    """List public InferenceX dashboard DB dump releases from GitHub."""

    response = requests.get(
        INFERENCEX_RELEASE_API,
        params={"per_page": max(1, min(int(limit), 100))},
        headers=_github_headers(github_token),
        timeout=30,
    )
    response.raise_for_status()
    releases: list[InferenceXRelease] = []
    for raw in response.json():
        tag_name = str(raw.get("tag_name") or "")
        if not tag_name.startswith(INFERENCEX_RELEASE_TAG_PREFIX):
            continue
        assets = []
        for asset in raw.get("assets") or []:
            name = str(asset.get("name") or "")
            browser_url = str(asset.get("browser_download_url") or "")
            if not name or not browser_url:
                continue
            assets.append(
                InferenceXReleaseAsset(
                    name=name,
                    url=browser_url,
                    size=int(asset["size"]) if asset.get("size") is not None else None,
                    digest=str(asset.get("digest") or "") or None,
                )
            )
        if assets:
            releases.append(
                InferenceXRelease(
                    tag_name=tag_name,
                    name=str(raw.get("name") or tag_name),
                    published_at=raw.get("published_at"),
                    html_url=raw.get("html_url"),
                    assets=tuple(sorted(assets, key=lambda item: item.name)),
                )
            )
    return releases


def latest_inferencex_db_dump_release(*, github_token: str | None = None) -> InferenceXRelease:
    releases = list_inferencex_db_dump_releases(limit=30, github_token=github_token)
    if not releases:
        raise RuntimeError("No InferenceX DB dump releases found.")
    return releases[0]


def _release_date_from_tag(tag_name: str) -> str:
    raw = tag_name.rsplit("/", 1)[-1]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return raw or "unknown"
    return raw


def download_inferencex_release_dump(
    release: InferenceXRelease | None = None,
    *,
    cache_dir: Path | None = None,
    github_token: str | None = None,
    force: bool = False,
) -> Path:
    """Download and unpack an InferenceX DB dump release, returning the dump directory."""

    resolved_release = release or latest_inferencex_db_dump_release(github_token=github_token)
    cache_root = _inferencex_cache_root(cache_dir)
    release_date = _release_date_from_tag(resolved_release.tag_name)
    target_dir = cache_root / f"inferencex-dump-{release_date}"
    marker = target_dir / "benchmark_results.json"
    if marker.exists() and not force:
        return target_dir

    download_dir = cache_root / "downloads" / release_date
    download_dir.mkdir(parents=True, exist_ok=True)
    part_paths: list[Path] = []
    headers = _github_headers(github_token)
    for asset in resolved_release.assets:
        if ".tar.xz" not in asset.name:
            continue
        part_path = download_dir / asset.name
        part_paths.append(part_path)
        if part_path.exists() and part_path.stat().st_size > 0 and not force:
            continue
        with requests.get(asset.url, headers=headers, stream=True, timeout=120) as response:
            response.raise_for_status()
            with part_path.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        handle.write(chunk)
    if not part_paths:
        raise RuntimeError(f"InferenceX release {resolved_release.tag_name} has no tar.xz dump assets.")

    combined_path = download_dir / f"inferencex-dump-{release_date}.tar.xz"
    if force or not combined_path.exists():
        with combined_path.open("wb") as out:
            for part_path in sorted(part_paths, key=lambda path: path.name):
                with part_path.open("rb") as src:
                    while True:
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)

    target_dir.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(combined_path, mode="r:xz") as archive:
            archive.extractall(target_dir)
    except lzma.LZMAError as exc:
        raise RuntimeError(f"Failed to decompress InferenceX dump {combined_path}: {exc}") from exc

    found = _find_inferencex_dump_dir(target_dir)
    if found != target_dir:
        return found
    if not marker.exists():
        raise RuntimeError(f"InferenceX dump extraction did not produce benchmark_results.json under {target_dir}.")
    return target_dir


def _load_json_table(dump_dir: Path, table: str) -> list[dict[str, Any]]:
    path = dump_dir / f"{table}.json"
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, list):
        raise ValueError(f"InferenceX dump table {path} is not a JSON array.")
    return [row for row in payload if isinstance(row, dict)]


def _find_inferencex_dump_dir(path: Path) -> Path:
    candidate = Path(path)
    if (candidate / "benchmark_results.json").exists():
        return candidate
    for child in candidate.rglob("benchmark_results.json"):
        return child.parent
    return candidate


def _stable_hash(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _num(value: Any) -> int | float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise ValueError("Non-finite InferenceX numeric value")
        return value
    try:
        text = str(value).strip()
        if not text:
            return None
        if re.fullmatch(r"[+-]?\d+", text):
            return int(text)
        number = float(text)
    except (ValueError, TypeError):
        return None
    if not math.isfinite(number):
        raise ValueError("Non-finite InferenceX numeric value")
    return int(number) if number.is_integer() else number


def _metric(metrics: dict[str, Any], key: str) -> int | float | None:
    return _num(metrics.get(key))


def _latest_workflow_runs(raw_runs: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    by_github_id: dict[int, dict[str, Any]] = {}
    for run in raw_runs:
        github_run_id = _num(run.get("github_run_id"))
        if github_run_id is None:
            continue
        attempt = int(_num(run.get("run_attempt")) or 0)
        existing = by_github_id.get(int(github_run_id))
        existing_attempt = int(_num(existing.get("run_attempt")) or 0) if existing else -1
        if existing is None or attempt > existing_attempt:
            by_github_id[int(github_run_id)] = run
    return {int(_num(run.get("id")) or 0): run for run in by_github_id.values() if _num(run.get("id")) is not None}


def _run_url(run: dict[str, Any] | None) -> str | None:
    if not run:
        return None
    html_url = str(run.get("html_url") or "").strip()
    if not html_url:
        return None
    attempt = _num(run.get("run_attempt"))
    return f"{html_url}/attempts/{int(attempt)}" if attempt is not None else html_url


def _inferencex_row_key(row: dict[str, Any]) -> str:
    if row.get("benchmark_result_id") is not None:
        return f"inferencex:result:{row['benchmark_result_id']}"
    raw = ":".join(
        str(row.get(key) or "")
        for key in (
            "benchmark_result_id",
            "config_id",
            "workflow_run_id",
            "date",
            "isl",
            "osl",
            "concurrency",
        )
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"inferencex:{digest}"


# Throughput denominators and GPU-energy scope follow the public v1 API contract.
_AGENTX_NUMERIC_METRICS = (
    "total_tput_tps", "input_tput_tps", "output_tput_tps", "duration_seconds",
    "total_prompt_tokens", "total_generation_tokens", "total_requests_completed",
    "uncached_input_tput_per_gpu", "cached_input_tput_per_gpu",
    "uncached_input_tput_tps", "cached_input_tput_tps",
    "total_cached_prompt_tokens", "total_uncached_prompt_tokens",
    "server_gpu_cache_hit_rate", "server_cpu_cache_hit_rate", "server_external_cache_hit_rate",
    "theoretical_cache_hit_rate", "p90_ttft", "p90_itl", "p90_tpot", "p90_e2el",
    "p90_full_response_intvty", "power_valid", "power_metric_schema_version",
    "avg_total_gpu_power_w", "total_gpu_energy_j", "allocated_cpu_dram_gb", "p90_ttft_ms",
)
_AGENTX_TEXT_METRICS = ("kv_offloading", "kv_offload_backend", "kv_offload_backend_version", "router_name", "router_version")
_AGENTX_PROVENANCE_FIELDS = (
    "source_model", "display_model", "model_db_keys", "run_started_at", "curve_date", "curve_workflow_run_id",
    "curve_run_started_at", "dataset_variant", "dataset_slug", "dataset_name",
    "hyperscaler_owned_tco_usd_per_gpu_hour", "rental_tco_usd_per_gpu_hour",
    "tco_source_url", "tco_observed_at", "tco_cost_basis", "source_url", "measurement_source", "ttft_unit",
)


def _validate_finite_json(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Non-finite InferenceX payload value")
    if isinstance(value, dict):
        for item in value.values():
            _validate_finite_json(item)
    elif isinstance(value, list):
        for item in value:
            _validate_finite_json(item)


def inferencex_benchmark_columns() -> list[dict[str, Any]]:
    base: list[tuple[str, str, str, str | None]] = [
        ("date", "Date", "date", "timestamp"),
        ("benchmark_result_id", "Benchmark Result ID", "number", "id"),
        ("workflow_run_id", "Workflow Run ID", "number", "dimension"),
        ("github_run_id", "GitHub Run ID", "number", "dimension"),
        ("run_url", "Run URL", "text", "dimension"),
        ("config_id", "Config ID", "number", "dimension"),
        ("benchmark_type", "Workload Type", "text", "dimension"),
        ("offload_mode", "KV Offload Mode", "text", "dimension"),
        ("recipe_fingerprint", "Recipe Fingerprint", "text", "dimension"),
        ("hardware", "Hardware", "text", "dimension"),
        ("framework", "Framework", "text", "dimension"),
        ("model", "Model", "text", "dimension"),
        ("precision", "Precision", "text", "dimension"),
        ("spec_method", "Spec / MTP Method", "text", "dimension"),
        ("disagg", "Disaggregated Serving", "bool", "dimension"),
        ("is_multinode", "Multinode", "bool", "dimension"),
        ("prefill_tp", "Prefill TP", "number", "dimension"),
        ("prefill_ep", "Prefill EP", "number", "dimension"),
        ("prefill_dp_attention", "Prefill DP Attention", "bool", "dimension"),
        ("prefill_num_workers", "Prefill Workers", "number", "dimension"),
        ("decode_tp", "Decode TP", "number", "dimension"),
        ("decode_ep", "Decode EP", "number", "dimension"),
        ("decode_dp_attention", "Decode DP Attention", "bool", "dimension"),
        ("decode_num_workers", "Decode Workers", "number", "dimension"),
        ("num_prefill_gpu", "Prefill GPUs", "number", "metric"),
        ("num_decode_gpu", "Decode GPUs", "number", "metric"),
        ("isl", "Input Sequence Length", "number", "dimension"),
        ("osl", "Output Sequence Length", "number", "dimension"),
        ("concurrency", "Concurrency", "number", "dimension"),
        ("benchmark_ok", "Benchmark OK", "bool", "dimension"),
        ("error", "Error", "text", "dimension"),
        ("image", "Container Image", "text", "dimension"),
        ("tput_per_gpu", "Total Throughput / GPU", "number", "metric"),
        ("input_tput_per_gpu", "Input Throughput / GPU", "number", "metric"),
        ("output_tput_per_gpu", "Output Throughput / GPU", "number", "metric"),
        ("median_intvty", "Median Interactivity", "number", "metric"),
        ("p90_intvty", "P90 Interactivity", "number", "metric"),
        ("mean_intvty", "Mean Interactivity", "number", "metric"),
        ("p99_ttft", "P99 TTFT", "number", "metric"),
        ("median_ttft", "Median TTFT", "number", "metric"),
        ("p99_tpot", "P99 TPOT", "number", "metric"),
        ("median_tpot", "Median TPOT", "number", "metric"),
        ("p99_e2el", "P99 End-to-End Latency", "number", "metric"),
        ("median_e2el", "Median End-to-End Latency", "number", "metric"),
        ("avg_power_w", "Average GPU Power (W)", "number", "metric"),
        ("prefill_avg_power_w", "Prefill Average Power (W)", "number", "metric"),
        ("decode_avg_power_w", "Decode Average Power (W)", "number", "metric"),
        ("joules_per_input_token", "Joules / Input Token", "number", "metric"),
        ("joules_per_output_token", "Joules / Output Token", "number", "metric"),
        ("joules_per_total_token", "Joules / Total Token", "number", "metric"),
        ("avg_util_pct", "Average Utilization (%)", "number", "metric"),
        ("avg_mem_used_mb", "Average Memory Used (MB)", "number", "metric"),
        ("metrics", "Raw Metrics", "json", "dimension"),
        ("workers", "Worker Telemetry", "json", "dimension"),
    ]
    known = {item[0] for item in base}
    base.extend((key, key.replace("_", " ").title(), "number", "metric")
                for key in _AGENTX_NUMERIC_METRICS if key not in known)
    base.extend((key, key.replace("_", " ").title(),
                 "json" if key == "model_db_keys" else "number" if "usd_per_gpu_hour" in key else "text", "dimension")
                for key in _AGENTX_PROVENANCE_FIELDS + _AGENTX_TEXT_METRICS)
    base.append(("measurement_valid", "Measurement structurally validated", "bool", "dimension"))
    base.extend((key, key.replace("_", " ").title(), "json", "dimension")
                for key in ("source_record", "power_audit", "power_invalid_reasons", "metric_semantics"))
    return [
        {"key": key, "label": label, "data_type": data_type, "role": role, "config": {}}
        for key, label, data_type, role in base
    ]


def _inferencex_framework_from_image(image: Any) -> str:
    text = str(image or "").strip().lower()
    if "sglang" in text:
        return "SGLang"
    if "tensorrt" in text or "trt" in text:
        return "TensorRT-LLM"
    if "vllm" in text:
        return "vLLM"
    if "dynamo" in text:
        return "Dynamo"
    return ""


def normalize_inferencex_benchmark_rows(
    dump_dir: Path,
    *,
    yield_every: int = 1000,
    pause_s: float = 0.0,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Normalize the InferenceX benchmark dump into Thoth DatasetRow payloads."""

    resolved_dir = _find_inferencex_dump_dir(Path(dump_dir))
    configs = {int(_num(row.get("id")) or 0): row for row in _load_json_table(resolved_dir, "configs")}
    raw_runs = _load_json_table(resolved_dir, "workflow_runs")
    latest_runs = _latest_workflow_runs(raw_runs)
    runs_by_id = {int(_num(run["id"])): run for run in raw_runs if _num(run.get("id")) is not None}
    raw_benchmarks = _load_json_table(resolved_dir, "benchmark_results")
    rows: list[dict[str, Any]] = []
    ok_count = 0
    for idx, raw in enumerate(raw_benchmarks):
        if idx > 0 and pause_s > 0 and yield_every > 0 and idx % yield_every == 0:
            time.sleep(float(pause_s))
        _validate_finite_json(raw)
        config_id = _num(raw.get("config_id"))
        workflow_run_id = _num(raw.get("workflow_run_id"))
        config = configs.get(config_id, raw)
        image = str(raw.get("image") or "")
        run = runs_by_id.get(workflow_run_id)
        metrics = raw.get("metrics") if isinstance(raw.get("metrics"), dict) else {}
        error = str(raw.get("error") or "").strip() or None
        benchmark_ok = raw.get("benchmark_ok") if isinstance(raw.get("benchmark_ok"), bool) else None
        if error is not None:
            benchmark_ok = False
        elif "error" in raw and benchmark_ok is None:
            benchmark_ok = True
        if benchmark_ok is True:
            ok_count += 1
        values = {
            "date": str(raw.get("date") or run.get("date") if run else raw.get("date") or ""),
            "benchmark_result_id": _num(raw.get("id")),
            "workflow_run_id": workflow_run_id,
            "github_run_id": _num(run.get("github_run_id")) if run else None,
            "run_url": _run_url(run) or raw.get("run_url"),
            "config_id": config_id,
            "benchmark_type": str(raw.get("benchmark_type") or ""),
            "offload_mode": raw.get("offload_mode"),
            "recipe_fingerprint": raw.get("recipe_fingerprint"),
            "hardware": str(config.get("hardware") or raw.get("hardware") or ""),
            "framework": str(config.get("framework") or raw.get("framework") or _inferencex_framework_from_image(image)),
            "model": str(config.get("model") or raw.get("model") or ""),
            "precision": str(config.get("precision") or ""),
            "spec_method": str(config.get("spec_method") or ""),
            "disagg": config.get("disagg") if isinstance(config.get("disagg"), bool) else None,
            "is_multinode": config.get("is_multinode") if isinstance(config.get("is_multinode"), bool) else None,
            "prefill_tp": _num(config.get("prefill_tp")),
            "prefill_ep": _num(config.get("prefill_ep")),
            "prefill_dp_attention": config.get("prefill_dp_attention") if isinstance(config.get("prefill_dp_attention"), bool) else None,
            "prefill_num_workers": _num(config.get("prefill_num_workers")),
            "decode_tp": _num(config.get("decode_tp")),
            "decode_ep": _num(config.get("decode_ep")),
            "decode_dp_attention": config.get("decode_dp_attention") if isinstance(config.get("decode_dp_attention"), bool) else None,
            "decode_num_workers": _num(config.get("decode_num_workers")),
            "num_prefill_gpu": _num(config.get("num_prefill_gpu")),
            "num_decode_gpu": _num(config.get("num_decode_gpu")),
            "isl": _num(raw.get("isl")),
            "osl": _num(raw.get("osl")),
            "concurrency": _num(raw.get("conc")),
            "benchmark_ok": benchmark_ok,
            "error": error,
            "image": image,
            "tput_per_gpu": _metric(metrics, "tput_per_gpu"),
            "input_tput_per_gpu": _metric(metrics, "input_tput_per_gpu"),
            "output_tput_per_gpu": _metric(metrics, "output_tput_per_gpu"),
            "median_intvty": _metric(metrics, "median_intvty"),
            "p90_intvty": _metric(metrics, "p90_intvty"),
            "mean_intvty": _metric(metrics, "mean_intvty"),
            "p99_ttft": _metric(metrics, "p99_ttft"),
            "median_ttft": _metric(metrics, "median_ttft"),
            "p99_tpot": _metric(metrics, "p99_tpot"),
            "median_tpot": _metric(metrics, "median_tpot"),
            "p99_e2el": _metric(metrics, "p99_e2el"),
            "median_e2el": _metric(metrics, "median_e2el"),
            "avg_power_w": _metric(metrics, "avg_power_w"),
            "prefill_avg_power_w": _metric(metrics, "prefill_avg_power_w"),
            "decode_avg_power_w": _metric(metrics, "decode_avg_power_w"),
            "joules_per_input_token": _metric(metrics, "joules_per_input_token"),
            "joules_per_output_token": _metric(metrics, "joules_per_output_token"),
            "joules_per_total_token": _metric(metrics, "joules_per_total_token"),
            "avg_util_pct": _metric(metrics, "avg_util_pct"),
            "avg_mem_used_mb": _metric(metrics, "avg_mem_used_mb"),
            "metrics": metrics,
            "workers": raw.get("workers") if isinstance(raw.get("workers"), list) else None,
            "source_record": raw,
            "power_audit": raw.get("power_audit"),
            "power_invalid_reasons": raw.get("power_invalid_reasons"),
            "metric_semantics": {"throughput_per_gpu": "tokens_per_second_per_physical_chip",
                                 "ttft": "seconds", "interactivity": "output_tokens_per_second_per_user",
                                 "input_tokens": "logical_prompt_tokens_not_computed_prefill",
                                 "avg_power_w": "mean_watts_per_physical_chip",
                                 "chip_count_rule": "sum_prefill_decode_if_disaggregated_else_max",
                                 "energy_scope": "deployment_gpu" if metrics.get("power_metric_schema_version") == 2 else "unknown"},
        }
        for key in _AGENTX_NUMERIC_METRICS:
            values[key] = _metric(metrics, key)
        for key in _AGENTX_PROVENANCE_FIELDS:
            values[key] = raw.get(key)
        for key in _AGENTX_TEXT_METRICS:
            values[key] = metrics.get(key)
        values["run_started_at"] = raw.get("run_started_at") or (run.get("run_started_at") if run else None)
        values["source_model"] = raw.get("source_model") or raw.get("model") or values["model"]
        values["p90_ttft_ms"] = values["p90_ttft"] * 1000 if values["p90_ttft"] is not None else None
        values["ttft_unit"] = "seconds"
        values["measurement_valid"] = raw.get("measurement_valid")
        rows.append({"row_key": _inferencex_row_key(values), "values": values})

    # Put agentic observations first before downstream bounded dataset reads.
    rows.sort(key=lambda row: (row["values"]["benchmark_type"] == "agentic_traces", row["values"]["date"] or ""), reverse=True)
    summary = {
        "dump_dir": str(resolved_dir),
        "table_counts": {
            "configs": len(configs),
            "workflow_runs": len(raw_runs),
            "latest_workflow_runs": len(latest_runs),
            "benchmark_results": len(raw_benchmarks),
            "benchmark_successes": ok_count,
        },
        "snapshot_digest": _stable_hash(
            {
                "configs": len(configs),
                "workflow_runs": len(raw_runs),
                "benchmark_results": len(raw_benchmarks),
                "rows": rows,
            }
        ),
    }
    return rows, summary


def _inferencex_extra(
    *,
    dump_dir: Path,
    release: InferenceXRelease | None,
    summary: dict[str, Any],
) -> dict[str, Any]:
    return {
        "source": "SemiAnalysis InferenceX",
        "source_owner": "SemiAnalysisAI",
        "source_repo": f"https://github.com/{INFERENCEX_APP_REPO}",
        "benchmark_repo": f"https://github.com/{INFERENCEX_BENCHMARK_REPO}",
        "source_url": "https://inferencex.semianalysis.com/",
        "license_note": "InferenceX-app is GPL-3.0; benchmark repository is Apache-2.0. Treat imported public data and copied code separately.",
        "upstream_kind": "github_release_db_dump",
        "release_tag": release.tag_name if release else None,
        "release_name": release.name if release else None,
        "release_published_at": release.published_at if release else None,
        "release_url": release.html_url if release else None,
        "dump_dir": str(dump_dir),
        "snapshot_digest": summary.get("snapshot_digest"),
        "table_counts": dict(summary.get("table_counts") or {}),
        "updated_by": "inferencex_upstream_sync",
        "import_schema_version": INFERENCEX_IMPORT_SCHEMA_VERSION,
        "preferred_benchmark_type": "agentic_traces",
    }


def upsert_inferencex_upstream_dataset(
    session: Session,
    *,
    dump_dir: Path,
    release: InferenceXRelease | None = None,
    publish: bool = True,
    yield_every: int = 1000,
    pause_s: float = 0.0,
) -> dict[str, Any]:
    """Create/update the normalized InferenceX benchmark upstream dataset."""

    ensure_dataset_tables(session)
    resolved_dir = _find_inferencex_dump_dir(Path(dump_dir))
    # Large InferenceX dumps can take minutes to normalize. Release any checked-out
    # DB transaction first so Postgres idle-in-transaction timeouts do not kill it.
    session.rollback()
    rows, summary = normalize_inferencex_benchmark_rows(resolved_dir, yield_every=yield_every, pause_s=pause_s)
    columns = inferencex_benchmark_columns()
    extra = _inferencex_extra(dump_dir=resolved_dir, release=release, summary=summary)
    dataset = session.query(Dataset).filter_by(slug=INFERENCEX_DATASET_SLUG).first()
    created = dataset is None
    latest_published = None if created else latest_dataset_version(session, int(dataset.id), status="published")
    previous_digest = (latest_published.extra or {}).get("snapshot_digest") if isinstance(latest_published, DatasetVersion) else None
    changed = previous_digest != summary.get("snapshot_digest")
    if dataset is not None and not changed:
        return {
            "dataset_id": int(dataset.id),
            "slug": dataset.slug,
            "created": False,
            "changed": False,
            "rows": len(rows),
            "columns": len(columns),
            "published_version_id": None,
            "snapshot_digest": summary.get("snapshot_digest"),
            "table_counts": summary.get("table_counts"),
            "skipped": True,
            "reason": "snapshot digest unchanged; preserved existing draft/published versions",
        }

    if dataset is None:
        dataset = create_dataset(
            session,
            title="AgentX / InferenceX Upstream Benchmarks",
            description=(
                "Normalized SemiAnalysis InferenceX benchmark results joined with config and GitHub Actions "
                "run provenance. Use this as the upstream fact table for AI inference serving economics."
            ),
            category="ai_infrastructure",
            family="ai_inference_benchmarks",
            status="active",
            source_kind="inferencex_github_release",
            slug=INFERENCEX_DATASET_SLUG,
            columns=columns,
            rows=[],
            label="InferenceX upstream shell",
            extra=extra,
            source_event="inferencex_upstream_sync",
        )
    else:
        dataset.title = "AgentX / InferenceX Upstream Benchmarks"
        dataset.description = (
            "Normalized SemiAnalysis InferenceX benchmark results joined with config and GitHub Actions "
            "run provenance. Use this as the upstream fact table for AI inference serving economics."
        )
        dataset.category = "ai_infrastructure"
        dataset.family = "ai_inference_benchmarks"
        dataset.source_kind = "inferencex_github_release"
        merged_extra = dict(dataset.extra or {})
        merged_extra.update(extra)
        dataset.extra = merged_extra
        dataset.updated_at = utcnow()
        session.add(dataset)
        session.flush()

    replace_dataset_draft(
        session,
        dataset,
        columns=columns,
        rows=rows,
        label=str(extra.get("release_tag") or "local dump"),
        extra=extra,
        source_event="inferencex_upstream_sync",
    )

    create_dataset_view(
        session,
        dataset,
        name="AgentX Successful Benchmark Rows",
        slug="successful-benchmarks",
        description="Successful InferenceX benchmark rows for frontier and economics modeling.",
        filters={"benchmark_ok": True, "benchmark_type": "agentic_traces"},
        columns=[
            "date",
            "hardware",
            "framework",
            "model",
            "precision",
            "benchmark_type",
            "offload_mode",
            "recipe_fingerprint",
            "isl",
            "osl",
            "concurrency",
            "median_intvty",
            "tput_per_gpu",
            "input_tput_per_gpu",
            "output_tput_per_gpu",
            "p99_ttft",
            "run_url",
        ],
        sort_spec=[{"key": "date", "direction": "desc"}, {"key": "tput_per_gpu", "direction": "desc"}],
        extra={"source": "inferencex_upstream_sync"},
    )
    create_dataset_view(
        session,
        dataset,
        name="AgentX Throughput Frontier Inputs",
        slug="throughput-frontier-inputs",
        description="Columns needed to reproduce throughput-vs-interactivity frontier charts.",
        filters={"benchmark_ok": True, "benchmark_type": "agentic_traces"},
        columns=[
            "date",
            "hardware",
            "framework",
            "model",
            "precision",
            "benchmark_type",
            "offload_mode",
            "recipe_fingerprint",
            "spec_method",
            "disagg",
            "is_multinode",
            "isl",
            "osl",
            "concurrency",
            "median_intvty",
            "tput_per_gpu",
            "input_tput_per_gpu",
            "output_tput_per_gpu",
            "avg_power_w",
            "joules_per_output_token",
        ],
        sort_spec=[{"key": "model", "direction": "asc"}, {"key": "median_intvty", "direction": "asc"}],
        extra={"source": "inferencex_upstream_sync"},
    )

    published_version_id = None
    if publish and changed:
        published = publish_dataset_snapshot(session, dataset, label=str(extra.get("release_tag") or "InferenceX snapshot"))
        published_version_id = int(published.id)
    session.flush()
    return {
        "dataset_id": int(dataset.id),
        "slug": dataset.slug,
        "created": created,
        "changed": changed,
        "rows": len(rows),
        "columns": len(columns),
        "published_version_id": published_version_id,
        "snapshot_digest": summary.get("snapshot_digest"),
        "table_counts": summary.get("table_counts"),
    }


def sync_inferencex_upstream_dataset(
    session: Session,
    *,
    dump_dir: Path | None = None,
    release_tag: str | None = None,
    cache_dir: Path | None = None,
    github_token: str | None = None,
    force_download: bool = False,
    publish: bool = True,
    yield_every: int = 1000,
    pause_s: float = 0.0,
) -> dict[str, Any]:
    """Download or read an InferenceX dump and upsert the Thoth upstream dataset."""

    release: InferenceXRelease | None = None
    resolved_dump_dir: Path
    if dump_dir is not None:
        resolved_dump_dir = _find_inferencex_dump_dir(Path(dump_dir))
    elif release_tag is None:
        # Current release dumps contain the entire PostgreSQL archive (tens of
        # GB). The public API exposes the latest benchmark facts without traces.
        return sync_agentx_api_dataset(session, cache_dir=cache_dir, publish=publish)
    else:
        releases = list_inferencex_db_dump_releases(limit=50, github_token=github_token)
        if release_tag:
            release = next((item for item in releases if item.tag_name == release_tag or item.tag_name.endswith(f"/{release_tag}")), None)
            if release is None:
                raise RuntimeError(f"InferenceX DB dump release not found: {release_tag}")
        else:
            if not releases:
                raise RuntimeError("No InferenceX DB dump releases found.")
            release = releases[0]
        dataset = session.query(Dataset).filter_by(slug=INFERENCEX_DATASET_SLUG).first()
        latest_published = None if dataset is None else latest_dataset_version(session, int(dataset.id), status="published")
        latest_extra = dict(latest_published.extra or {}) if latest_published is not None else {}
        if not force_download and latest_extra.get("release_tag") == release.tag_name and latest_extra.get("import_schema_version") == INFERENCEX_IMPORT_SCHEMA_VERSION:
            return {
                "dataset_id": int(dataset.id) if dataset is not None else None,
                "slug": INFERENCEX_DATASET_SLUG,
                "created": False,
                "changed": False,
                "rows": None,
                "columns": len(inferencex_benchmark_columns()),
                "published_version_id": None,
                "snapshot_digest": latest_extra.get("snapshot_digest"),
                "table_counts": latest_extra.get("table_counts"),
                "skipped": True,
                "reason": f"latest published InferenceX release already synced: {release.tag_name}",
            }
        session.rollback()
        resolved_dump_dir = download_inferencex_release_dump(
            release,
            cache_dir=cache_dir,
            github_token=github_token,
            force=force_download,
        )
    return upsert_inferencex_upstream_dataset(
        session,
        dump_dir=resolved_dump_dir,
        release=release,
        publish=publish,
        yield_every=yield_every,
        pause_s=pause_s,
    )


def sync_agentx_api_dataset(
    session: Session, *, models: tuple[str, ...] | None = None,
    cache_dir: Path | None = None, publish: bool = True,
) -> dict[str, Any]:
    """Import all advertised AgentX models, or an explicit bounded universe.

    The documented v1 endpoint returns a complete array without pagination.
    Fetch and validate every response before mutation; soft errors, missing
    model coverage and malformed/duplicate IDs abort the whole snapshot.
    """
    options_response = requests.get(AGENTX_OPTIONS_URL, timeout=60)
    options_response.raise_for_status()
    options = options_response.json()
    _validate_finite_json(options)
    if not isinstance(options, dict) or options.get("apiVersion") != AGENTX_SOURCE_SCHEMA_VERSION:
        raise ValueError("Invalid or unsupported AgentX options contract")
    model_options = options.get("models")
    hardware_options = options.get("hardware")
    if not isinstance(model_options, list) or not isinstance(hardware_options, list):
        raise ValueError("AgentX options are missing model/hardware catalogs")
    model_catalog: dict[str, set[str]] = {}
    for model_option in model_options:
        if not isinstance(model_option, dict) or not model_option.get("name") or not isinstance(model_option.get("dbKeys"), list):
            raise ValueError("Invalid AgentX model catalog entry")
        model_catalog[model_option["name"]] = set(model_option["dbKeys"])
    if models is None:
        availability_response = requests.get(AGENTX_AVAILABILITY_URL, timeout=60)
        availability_response.raise_for_status()
        availability = availability_response.json()
        if not isinstance(availability, list) or any(not isinstance(row, dict) for row in availability):
            raise ValueError("Invalid AgentX availability response")
        available_keys = {row.get("model") for row in availability if row.get("benchmark_type") == "agentic_traces"}
        catalog_keys = set().union(*model_catalog.values()) if model_catalog else set()
        if available_keys - catalog_keys:
            raise ValueError("AgentX availability has unmapped model identities")
        models = tuple(name for name, keys in model_catalog.items() if keys & available_keys)
    models = tuple(dict.fromkeys(models))
    if not models or len(models) > 30 or any(model not in model_catalog for model in models):
        raise ValueError("AgentX sync requires 1 to 30 supported display models")
    hardware_catalog = {item["key"]: item for item in hardware_options
                        if isinstance(item, dict) and item.get("key")}
    observed_at = utcnow().isoformat()
    rows: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    responses: list[dict[str, Any]] = []
    for model in models:
        response = requests.get(AGENTX_API_URL, params={"model": model}, timeout=60)
        response.raise_for_status()
        payload = response.json()
        _validate_finite_json(payload)
        if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
            raise ValueError(f"Invalid AgentX response for {model}")
        agentic = [row for row in payload if row.get("benchmark_type") == "agentic_traces"]
        if not agentic:
            raise ValueError(f"No AgentX observations returned for {model}; previous snapshot retained")
        for raw in agentic:
            result_id = _num(raw.get("id"))
            if result_id is None or result_id <= 0 or int(result_id) != result_id or int(result_id) in seen_ids:
                raise ValueError(f"Missing or duplicate AgentX result ID for {model}")
            if raw.get("model") not in model_catalog[model]:
                raise ValueError(f"AgentX model identity mismatch for {model}")
            seen_ids.add(int(result_id))
            metrics = raw.get("metrics")
            if not isinstance(metrics, dict):
                raise ValueError(f"Missing AgentX metrics for result {result_id}")
            # A complete finite observation is distinct from a benchmark-success verdict.
            throughput = _num(metrics.get("tput_per_gpu"))
            input_rate = _num(metrics.get("input_tput_per_gpu"))
            output_rate = _num(metrics.get("output_tput_per_gpu"))
            measurement_valid = (throughput is not None and throughput > 0
                                 and all(rate is None or rate >= 0 for rate in (input_rate, output_rate))
                                 and not raw.get("error") and raw.get("benchmark_ok") is not False)
            item = dict(raw, source_model=raw["model"], display_model=model, model_db_keys=sorted(model_catalog[model]),
                        measurement_source="public_benchmark_api", measurement_valid=measurement_valid,
                        source_url=getattr(response, "url", None) or requests.Request("GET", AGENTX_API_URL, params={"model": model}).prepare().url)
            hardware = hardware_catalog.get(raw.get("hardware"))
            if hardware is not None:
                costs = hardware.get("costPerHour") or {}
                item.update(hyperscaler_owned_tco_usd_per_gpu_hour=_num(costs.get("h")),
                            rental_tco_usd_per_gpu_hour=_num(costs.get("r")),
                            tco_source_url=AGENTX_OPTIONS_URL, tco_observed_at=observed_at[:10],
                            tco_cost_basis="upstream_configured_owning_or_rental_tco")
            rows.append(item)
        responses.append({"model": model, "returned_rows": len(payload), "selected_agentic_rows": len(agentic),
                          "response_digest": _stable_hash(payload)})
    # No publisher call is reached until every advertised model has valid coverage.
    existing = session.query(Dataset).filter_by(slug=INFERENCEX_DATASET_SLUG).one_or_none()
    previous_models = set((existing.extra or {}).get("models") or []) if existing is not None else set()
    if previous_models - set(models):
        raise ValueError("AgentX model coverage would shrink; previous snapshot retained")
    session.rollback()
    root = _inferencex_cache_root(cache_dir)
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="agentx-api-", dir=root) as temporary:
        path = Path(temporary)
        (path / "benchmark_results.json").write_text(json.dumps(rows, allow_nan=False), encoding="utf-8")
        result = upsert_inferencex_upstream_dataset(session, dump_dir=path, publish=publish)
    dataset = session.query(Dataset).filter_by(slug=INFERENCEX_DATASET_SLUG).one()
    metadata = dict(dataset.extra or {})
    metadata.update(source="SemiAnalysis AgentX", upstream_kind="public_benchmark_api",
                    source_url=AGENTX_API_URL, models=list(models), dump_dir=None,
                    source_schema_version=AGENTX_SOURCE_SCHEMA_VERSION, retrieved_at=observed_at,
                    model_catalog_url=AGENTX_OPTIONS_URL, availability_url=AGENTX_AVAILABILITY_URL,
                    response_coverage=responses, pagination="not_supported_complete_array",
                    upstream_options_digest=_stable_hash(options))
    dataset.extra = metadata
    dataset.source_kind = "agentx_public_api"
    dataset.description = "AgentX multi-turn trace benchmarks from the public InferenceX API, with serving configuration and run provenance."
    for status in ("draft", "published") if publish else ("draft",):
        version = latest_dataset_version(session, int(dataset.id), status=status)
        if version is not None and result.get("changed"):
            version.extra = dict(version.extra or {}, **metadata)
    result["source_url"] = AGENTX_API_URL
    result["models"] = list(models)
    result["response_coverage"] = responses
    session.flush()
    return result
