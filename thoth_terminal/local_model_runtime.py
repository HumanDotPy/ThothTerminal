from __future__ import annotations
import logging
import math
import os
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import mean
from typing import Any

import requests

from thoth_terminal.config import settings
from thoth_terminal.runtime_env import repo_root
from thoth_terminal.runtime_info import read_runtime_state, write_runtime_state
from thoth_terminal.runtime_processes import start_hidden_logged_process


_SUPPORTED_BACKENDS = {"manual", "auto", "vllm", "sglang"}
_SUPPORTED_PROFILES = {"generic", "triattention"}
_LOCAL_MODEL_STATE_SERVICE = "local-model"
logger = logging.getLogger("thoth.local_model")


@dataclass(frozen=True)
class LocalModelRuntimePlan:
    configured_backend: str
    effective_backend: str | None
    profile: str
    endpoint: str | None
    openai_base_url: str | None
    health_url: str | None
    requested_model: str | None
    served_model_name: str | None
    model_path: str | None
    triattention_enabled: bool
    launch_ready: bool
    warnings: list[str]
    env: dict[str, str]
    command: list[str]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _pid_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def _autostart_services() -> set[str]:
    raw = str(getattr(settings, "local_model_autostart_services", "web,worker,desktop") or "").strip()
    if not raw:
        return set()
    return {part.strip().lower() for part in raw.split(",") if part and part.strip()}


def local_model_autostart_enabled_for(service: str | None) -> bool:
    if not bool(getattr(settings, "local_model_autostart", False)):
        return False
    allowed = _autostart_services()
    if not allowed:
        return True
    service_v = str(service or "").strip().lower()
    return bool({"*", "all"} & allowed) or (service_v in allowed)


def normalize_local_model_endpoint(value: str | None) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    out = raw.rstrip("/")
    for suffix in (
        "/v1/chat/completions",
        "/chat/completions",
        "/completions",
        "/v1",
        "/api/chat",
        "/api",
    ):
        if out.lower().endswith(suffix):
            out = out[: -len(suffix)]
            break
    return out.rstrip("/") or None


def local_model_endpoint() -> str | None:
    explicit = normalize_local_model_endpoint(
        getattr(settings, "agent_llm_endpoint", None) or getattr(settings, "local_llm_endpoint", None)
    )
    if explicit:
        return explicit
    backend = str(getattr(settings, "local_model_backend", "manual") or "manual").strip().lower()
    if backend not in _SUPPORTED_BACKENDS:
        backend = "manual"
    if backend in {"auto", "vllm", "sglang"}:
        host = str(getattr(settings, "local_model_host", "127.0.0.1") or "127.0.0.1").strip() or "127.0.0.1"
        port = int(getattr(settings, "local_model_port", 8000) or 8000)
        return f"http://{host}:{port}"
    return None


def local_model_openai_base_url(endpoint: str | None = None) -> str | None:
    base = normalize_local_model_endpoint(endpoint or local_model_endpoint())
    if not base:
        return None
    return f"{base}/v1"


def local_model_health_url(endpoint: str | None = None) -> str | None:
    base = normalize_local_model_endpoint(endpoint or local_model_endpoint())
    if not base:
        return None
    health_path = str(getattr(settings, "local_model_health_path", "/health") or "/health").strip() or "/health"
    if not health_path.startswith("/"):
        health_path = "/" + health_path
    return f"{base}{health_path}"


def local_model_api_key() -> str | None:
    return str(getattr(settings, "local_model_api_key", None) or "").strip() or None


def local_model_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    api_key = local_model_api_key()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def local_model_name() -> str | None:
    requested = str(getattr(settings, "agent_llm_model", None) or "").strip()
    if requested:
        return requested
    served = str(getattr(settings, "local_model_served_model_name", None) or "").strip()
    if served:
        return served
    return None


def _resolve_backend() -> tuple[str, str | None, bool]:
    configured = str(getattr(settings, "local_model_backend", "manual") or "manual").strip().lower()
    if configured not in _SUPPORTED_BACKENDS:
        configured = "manual"
    profile = str(getattr(settings, "local_model_profile", "generic") or "generic").strip().lower()
    if profile not in _SUPPORTED_PROFILES:
        profile = "generic"
    triattention_enabled = profile == "triattention"
    effective = configured
    if configured == "auto":
        effective = "vllm" if triattention_enabled else "vllm"
    elif configured == "manual":
        effective = None
    return configured, effective, triattention_enabled


def _triattention_env() -> dict[str, str]:
    env = {
        "ENABLE_TRIATTENTION": "true",
        "TRIATTN_RUNTIME_KV_BUDGET": str(int(getattr(settings, "local_model_triattention_kv_budget", 12000) or 12000)),
        "TRIATTN_RUNTIME_DIVIDE_LENGTH": str(int(getattr(settings, "local_model_triattention_divide_length", 128) or 128)),
        "TRIATTN_RUNTIME_WINDOW_SIZE": str(int(getattr(settings, "local_model_triattention_window_size", 128) or 128)),
        "TRIATTN_RUNTIME_PRUNING_MODE": str(getattr(settings, "local_model_triattention_pruning_mode", "per_head") or "per_head"),
        "TRIATTN_RUNTIME_PROTECT_PREFILL": "true" if bool(getattr(settings, "local_model_triattention_protect_prefill", False)) else "false",
        "TRIATTN_RUNTIME_ENABLE_EXPERIMENTAL_KV_COMPACTION": "true" if bool(getattr(settings, "local_model_triattention_enable_kv_compaction", True)) else "false",
        "TRIATTN_RUNTIME_ENABLE_EXPERIMENTAL_BLOCK_RECLAIM": "true" if bool(getattr(settings, "local_model_triattention_enable_block_reclaim", True)) else "false",
    }
    sparse_stats_path = getattr(settings, "local_model_triattention_sparse_stats_path", None)
    if sparse_stats_path:
        env["TRIATTN_RUNTIME_SPARSE_STATS_PATH"] = str(Path(sparse_stats_path))
    return env


def _vllm_command(*, model_path: str, served_model_name: str | None, triattention_enabled: bool) -> tuple[dict[str, str], list[str]]:
    env: dict[str, str] = {}
    args = [
        "vllm",
        "serve",
        model_path,
        "--host",
        str(getattr(settings, "local_model_host", "127.0.0.1") or "127.0.0.1"),
        "--port",
        str(int(getattr(settings, "local_model_port", 8000) or 8000)),
        "--dtype",
        str(getattr(settings, "local_model_vllm_dtype", "bfloat16") or "bfloat16"),
        "--tensor-parallel-size",
        str(int(getattr(settings, "local_model_vllm_tensor_parallel_size", 1) or 1)),
        "--gpu-memory-utilization",
        str(float(getattr(settings, "local_model_vllm_gpu_memory_utilization", 0.90) or 0.90)),
        "--max-model-len",
        str(int(getattr(settings, "local_model_vllm_max_model_len", 32768) or 32768)),
        "--max-num-batched-tokens",
        str(int(getattr(settings, "local_model_vllm_max_num_batched_tokens", 4096) or 4096)),
        "--max-num-seqs",
        str(int(getattr(settings, "local_model_vllm_max_num_seqs", 16) or 16)),
    ]
    if bool(getattr(settings, "local_model_vllm_enable_chunked_prefill", True)):
        args.append("--enable-chunked-prefill")
    if bool(getattr(settings, "local_model_vllm_enforce_eager", True)):
        args.append("--enforce-eager")
    if bool(getattr(settings, "local_model_vllm_trust_remote_code", True)):
        args.append("--trust-remote-code")
    if not bool(getattr(settings, "local_model_vllm_enable_prefix_caching", False)):
        args.extend(["--enable-prefix-caching", "false"])
    if served_model_name:
        args.extend(["--served-model-name", served_model_name])
    if triattention_enabled:
        env.update(_triattention_env())
    return env, args


def _sglang_command(*, model_path: str, served_model_name: str | None) -> tuple[dict[str, str], list[str]]:
    args = [
        "python",
        "-m",
        "sglang.launch_server",
        "--model-path",
        model_path,
        "--host",
        str(getattr(settings, "local_model_host", "127.0.0.1") or "127.0.0.1"),
        "--port",
        str(int(getattr(settings, "local_model_port", 8000) or 8000)),
        "--tp-size",
        str(int(getattr(settings, "local_model_vllm_tensor_parallel_size", 1) or 1)),
    ]
    if served_model_name:
        args.extend(["--served-model-name", served_model_name])
    return {}, args


def resolve_local_model_runtime() -> LocalModelRuntimePlan:
    configured_backend, effective_backend, triattention_enabled = _resolve_backend()
    warnings: list[str] = []
    endpoint = local_model_endpoint()
    requested_model = local_model_name()
    served_model_name = str(getattr(settings, "local_model_served_model_name", None) or "").strip() or requested_model
    model_path = str(getattr(settings, "local_model_model_path", None) or "").strip() or served_model_name

    if triattention_enabled:
        warnings.append(
            "TriAttention upstream currently documents vLLM integration as available and SGLang integration as roadmap-only as of 2026-04-09."
        )
        if not getattr(settings, "local_model_triattention_sparse_stats_path", None):
            warnings.append("TriAttention profile is enabled but THOTH_LOCAL_MODEL_TRIATTENTION_SPARSE_STATS_PATH is not set.")
    if configured_backend == "manual" and not endpoint:
        warnings.append("No explicit local model endpoint is configured.")
    if effective_backend == "sglang" and triattention_enabled:
        warnings.append("TriAttention launch support is not built for SGLang in this runtime; use backend=vllm for the TriAttention profile.")
    if not requested_model:
        warnings.append("No request model is configured. Set THOTH_AGENT_LLM_MODEL or THOTH_LOCAL_MODEL_SERVED_MODEL_NAME.")

    env: dict[str, str] = {}
    command: list[str] = []
    launch_ready = False
    if effective_backend == "vllm" and model_path and (not (triattention_enabled and effective_backend != "vllm")):
        env, command = _vllm_command(
            model_path=model_path,
            served_model_name=served_model_name,
            triattention_enabled=triattention_enabled,
        )
        launch_ready = True
    elif effective_backend == "sglang" and model_path and not triattention_enabled:
        env, command = _sglang_command(model_path=model_path, served_model_name=served_model_name)
        launch_ready = True

    return LocalModelRuntimePlan(
        configured_backend=configured_backend,
        effective_backend=effective_backend,
        profile=("triattention" if triattention_enabled else "generic"),
        endpoint=endpoint,
        openai_base_url=local_model_openai_base_url(endpoint),
        health_url=local_model_health_url(endpoint),
        requested_model=requested_model,
        served_model_name=served_model_name,
        model_path=model_path,
        triattention_enabled=triattention_enabled,
        launch_ready=launch_ready,
        warnings=warnings,
        env=env,
        command=command,
    )


def probe_local_model_runtime(*, timeout_s: float = 10.0) -> dict[str, Any]:
    plan = resolve_local_model_runtime()
    payload: dict[str, Any] = {"runtime": plan.as_dict(), "ok": False}
    base_url = plan.openai_base_url
    if not base_url:
        payload["error"] = "Local model endpoint is not configured."
        return payload
    headers = local_model_headers()

    health_payload: Any = None
    health_error: str | None = None
    if plan.health_url:
        try:
            health_resp = requests.get(plan.health_url, headers=headers, timeout=float(timeout_s))
            health_payload = {
                "status_code": int(health_resp.status_code),
                "body": (health_resp.text[:400] if health_resp.text else ""),
            }
        except requests.RequestException as exc:
            health_error = f"{type(exc).__name__}: {exc}"

    models_payload: dict[str, Any] | None = None
    model_names: list[str] = []
    try:
        resp = requests.get(f"{base_url}/models", headers=headers, timeout=float(timeout_s))
        resp.raise_for_status()
        models_payload = resp.json() if resp.content else {}
        for row in (models_payload.get("data") or []) if isinstance(models_payload, dict) else []:
            if not isinstance(row, dict):
                continue
            name = str(row.get("id") or "").strip()
            if name:
                model_names.append(name)
    except requests.RequestException as exc:
        payload["health"] = health_payload
        payload["health_error"] = health_error
        payload["error"] = f"{type(exc).__name__}: {exc}"
        return payload

    chosen_model = plan.requested_model or (model_names[0] if model_names else None)
    payload.update(
        {
            "ok": True,
            "health": health_payload,
            "health_error": health_error,
            "models": model_names,
            "chosen_model": chosen_model,
            "raw_models": models_payload,
        }
    )
    return payload


def _thoth_benchmark_cases() -> list[dict[str, Any]]:
    context_rows = "\n".join(
        [
            f"- doc#{idx} [trendforce_news] HBM supply-chain update {idx} | precedence=trusted_news_trade_press | triage=NEW | 2026-04-09T12:{idx:02d}:00Z\n"
            f"  signal: Memory and packaging commentary stayed active across SK hynix, Samsung, and TSMC node timing.\n"
            f"  why: Potentially thesis-relevant, but it still needs corroboration and placement inside the broader chain."
            for idx in range(1, 21)
        ]
    )
    return [
        {
            "name": "document_refinery",
            "payload": {
                "messages": [
                    {"role": "system", "content": "Return JSON only."},
                    {
                        "role": "user",
                        "content": (
                            "Return JSON only with keys signal, summary, why_it_matters, key_points, topics, confidence.\n"
                            "Title: Samsung may expand HBM packaging capacity in 2026\n"
                            "Text: Samsung is evaluating additional HBM packaging capacity for 2026 demand. "
                            "The report points to equipment timing, customer qualification, and margin mix as the main variables."
                        ),
                    },
                ],
                "temperature": 0.0,
                "response_format": {"type": "json_object"},
                "max_tokens": 220,
            },
        },
        {
            "name": "link_triage",
            "payload": {
                "messages": [
                    {"role": "system", "content": "Return JSON only."},
                    {
                        "role": "user",
                        "content": (
                            "You classify Discord links into source-ingest decisions.\n"
                            "Return strict JSON with keys: action, ticker, source_type, confidence, reason, title, note.\n"
                            "action must be one of: ingest, queue, ignore.\n"
                            "confidence must be 0.0-1.0.\n"
                            "URL: https://example.com/skhynix-hbm\n"
                            "EmbedTitle: SK hynix HBM capacity comment\n"
                            "Excerpt: chatter about HBM demand, packaging, and customer qualification timing for next year.\n"
                            "DeterministicTicker: 000660.KS\n"
                            "ResolutionState: resolved\n"
                        ),
                    },
                ],
                "temperature": 0.0,
                "response_format": {"type": "json_object"},
                "max_tokens": 180,
            },
        },
        {
            "name": "context_compression",
            "payload": {
                "messages": [
                    {"role": "system", "content": "Return JSON only."},
                    {
                        "role": "user",
                        "content": (
                            "Return JSON ONLY with keys: summary, key_points, actions, risks, evidence, coverage_gaps, proposed_mutations.\n"
                            "Keep summary <= 900 chars and key_points/actions/risks max 8 each.\n\n"
                            "# Recent Thoth Context\n"
                            "Lookback: 24h\n\n"
                            "## documents\n"
                            f"{context_rows}\n"
                        ),
                    },
                ],
                "temperature": 0.0,
                "response_format": {"type": "json_object"},
                "max_tokens": 300,
            },
        },
    ]


def benchmark_local_model_runtime(
    *,
    concurrency: int = 4,
    rounds: int = 3,
    timeout_s: float = 60.0,
) -> dict[str, Any]:
    plan = resolve_local_model_runtime()
    base_url = plan.openai_base_url
    model = plan.requested_model
    if not base_url:
        raise ValueError("Local model endpoint is not configured.")
    if not model:
        raise ValueError("Local model request model is not configured.")

    jobs: list[tuple[str, dict[str, Any]]] = []
    for _ in range(max(1, int(rounds))):
        for case in _thoth_benchmark_cases():
            payload = dict(case["payload"])
            payload["model"] = model
            jobs.append((str(case["name"]), payload))

    url = f"{base_url}/chat/completions"
    headers = local_model_headers()

    def _run_one(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        resp = requests.post(url, headers=headers, json=payload, timeout=float(timeout_s))
        elapsed = time.perf_counter() - started
        resp.raise_for_status()
        data = resp.json() if resp.content else {}
        usage = data.get("usage") if isinstance(data, dict) else {}
        choices = data.get("choices") if isinstance(data, dict) else []
        content = ""
        if isinstance(choices, list) and choices:
            msg = (choices[0] or {}).get("message") if isinstance(choices[0], dict) else {}
            content = str((msg or {}).get("content") or "")
        return {
            "name": name,
            "latency_s": elapsed,
            "prompt_tokens": int((usage or {}).get("prompt_tokens") or 0),
            "completion_tokens": int((usage or {}).get("completion_tokens") or 0),
            "total_tokens": int((usage or {}).get("total_tokens") or 0),
            "output_chars": len(content),
        }

    results: list[dict[str, Any]] = []
    errors: list[str] = []
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=max(1, int(concurrency))) as pool:
        futures = [pool.submit(_run_one, name, payload) for name, payload in jobs]
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
    total_s = time.perf_counter() - started

    by_case: dict[str, dict[str, Any]] = {}
    for row in results:
        case = str(row["name"])
        bucket = by_case.setdefault(
            case,
            {
                "count": 0,
                "latencies": [],
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "output_chars": 0,
            },
        )
        bucket["count"] += 1
        bucket["latencies"].append(float(row["latency_s"]))
        bucket["prompt_tokens"] += int(row["prompt_tokens"])
        bucket["completion_tokens"] += int(row["completion_tokens"])
        bucket["total_tokens"] += int(row["total_tokens"])
        bucket["output_chars"] += int(row["output_chars"])

    case_summary: dict[str, Any] = {}
    for name, bucket in by_case.items():
        latencies = sorted(float(v) for v in bucket.pop("latencies"))
        p95_idx = min(len(latencies) - 1, max(0, math.ceil(len(latencies) * 0.95) - 1))
        case_summary[name] = {
            **bucket,
            "mean_latency_s": round(mean(latencies), 4) if latencies else None,
            "p95_latency_s": round(latencies[p95_idx], 4) if latencies else None,
        }

    total_tokens = sum(int(row.get("total_tokens") or 0) for row in results)
    total_completion_tokens = sum(int(row.get("completion_tokens") or 0) for row in results)
    return {
        "runtime": plan.as_dict(),
        "ok": len(errors) == 0,
        "count": len(jobs),
        "ok_count": len(results),
        "error_count": len(errors),
        "errors": errors[:20],
        "concurrency": int(concurrency),
        "rounds": int(rounds),
        "total_seconds": round(total_s, 4),
        "requests_per_second": round((len(results) / total_s), 4) if total_s > 0 else None,
        "total_tokens": int(total_tokens),
        "completion_tokens": int(total_completion_tokens),
        "tokens_per_second": round((total_tokens / total_s), 4) if total_s > 0 and total_tokens > 0 else None,
        "completion_tokens_per_second": round((total_completion_tokens / total_s), 4)
        if total_s > 0 and total_completion_tokens > 0
        else None,
        "cases": case_summary,
    }


def launch_command_as_shell_line(*, shell: str = "powershell") -> dict[str, Any]:
    plan = resolve_local_model_runtime()
    shell_v = str(shell or "powershell").strip().lower()
    if not plan.launch_ready:
        return {"runtime": plan.as_dict(), "shell": shell_v, "line": None}
    if shell_v == "bash":
        env_bits = [f'{key}="{value}"' for key, value in plan.env.items()]
        line = " ".join(env_bits + plan.command)
    else:
        env_bits = [f'$env:{key}="{value}";' for key, value in plan.env.items()]
        line = " ".join(env_bits + plan.command)
    return {"runtime": plan.as_dict(), "shell": shell_v, "line": line}


def _resolve_spawn_command(command: list[str]) -> list[str]:
    if not command:
        raise ValueError("Local model runtime has no launch command.")
    resolved = [str(part) for part in command]
    head = str(resolved[0] or "").strip()
    if not head:
        raise ValueError("Local model runtime has an empty launch command.")
    if head.lower() == "python":
        resolved[0] = sys.executable
        return resolved
    if Path(head).is_absolute():
        return resolved
    found = shutil.which(head)
    if found:
        resolved[0] = found
        return resolved
    py_dir = Path(sys.executable).resolve(strict=False).parent
    candidates = [py_dir / head]
    if os.name == "nt" and "." not in Path(head).name:
        candidates.extend([py_dir / f"{head}.exe", py_dir / f"{head}.cmd", py_dir / f"{head}.bat"])
    for candidate in candidates:
        if candidate.exists():
            resolved[0] = str(candidate)
            return resolved
    raise FileNotFoundError(f"Unable to resolve executable for local model runtime: {head}")


def _wait_for_local_model_ready(*, timeout_s: float, probe_timeout_s: float) -> dict[str, Any]:
    last_payload: dict[str, Any] = {"ok": False, "error": "Local model runtime did not become ready."}
    deadline = time.perf_counter() + max(0.0, float(timeout_s))
    while time.perf_counter() < deadline:
        remaining = max(0.25, deadline - time.perf_counter())
        last_payload = probe_local_model_runtime(timeout_s=min(float(probe_timeout_s), remaining))
        if bool(last_payload.get("ok")):
            return last_payload
        time.sleep(min(1.0, remaining))
    return last_payload


def ensure_local_model_runtime(
    *,
    service: str,
    require_opt_in: bool = True,
    wait_s: float | None = None,
) -> dict[str, Any]:
    service_v = str(service or "").strip().lower() or "unknown"
    plan = resolve_local_model_runtime()
    payload: dict[str, Any] = {
        "service": service_v,
        "runtime": plan.as_dict(),
        "status": "skipped",
        "launched": False,
        "ready": False,
        "warnings": list(plan.warnings),
    }
    if require_opt_in and (not local_model_autostart_enabled_for(service_v)):
        payload["status"] = "autostart_disabled"
        return payload

    probe_timeout_s = max(
        0.5,
        float(getattr(settings, "local_model_autostart_probe_timeout_s", 2.5) or 2.5),
    )
    wait_timeout_s = float(wait_s if wait_s is not None else getattr(settings, "local_model_autostart_wait_timeout_s", 0.0) or 0.0)

    probe_payload = probe_local_model_runtime(timeout_s=probe_timeout_s)
    payload["probe"] = probe_payload
    if bool(probe_payload.get("ok")):
        payload["status"] = "already_ready"
        payload["ready"] = True
        return payload

    state = read_runtime_state(service=_LOCAL_MODEL_STATE_SERVICE)
    if isinstance(state, dict) and _pid_alive(state.get("pid")):
        payload["status"] = "already_running"
        payload["existing_state"] = state
        if wait_timeout_s > 0.0:
            waited = _wait_for_local_model_ready(timeout_s=wait_timeout_s, probe_timeout_s=probe_timeout_s)
            payload["probe"] = waited
            payload["ready"] = bool(waited.get("ok"))
            if payload["ready"]:
                payload["status"] = "already_ready"
        return payload

    if not plan.launch_ready:
        payload["status"] = "not_launch_ready"
        return payload

    logs_dir = Path(settings.runtime_state_dir) / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = logs_dir / "local-model.out.log"
    stderr_path = logs_dir / "local-model.err.log"
    launch_env = os.environ.copy()
    launch_env.update({str(key): str(value) for key, value in plan.env.items()})
    launch_env.setdefault("PYTHONUNBUFFERED", "1")
    spawn_command = _resolve_spawn_command(plan.command)

    try:
        handle = start_hidden_logged_process(
            spawn_command,
            label="local-model",
            cwd=str(repo_root()),
            env=launch_env,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            detached=True,
        )
    except Exception as exc:
        payload["status"] = "launch_failed"
        payload["error"] = f"{type(exc).__name__}: {exc}"
        return payload
    proc = handle.process

    state_path = write_runtime_state(
        service=_LOCAL_MODEL_STATE_SERVICE,
        data={
            "pid": int(proc.pid),
            "launcher_service": service_v,
            "endpoint": plan.endpoint,
            "effective_backend": plan.effective_backend,
            "profile": plan.profile,
            "requested_model": plan.requested_model,
            "served_model_name": plan.served_model_name,
            "model_path": plan.model_path,
            "launch_command": spawn_command,
            "stdout_log": str(stdout_path),
            "stderr_log": str(stderr_path),
        },
    )
    payload.update(
        {
            "status": "launched",
            "launched": True,
            "pid": int(proc.pid),
            "state_path": str(state_path),
            "spawn_command": spawn_command,
            "stdout_log": str(stdout_path),
            "stderr_log": str(stderr_path),
        }
    )
    if wait_timeout_s > 0.0:
        waited = _wait_for_local_model_ready(timeout_s=wait_timeout_s, probe_timeout_s=probe_timeout_s)
        payload["probe"] = waited
        payload["ready"] = bool(waited.get("ok"))
        payload["status"] = ("launched_ready" if payload["ready"] else "launched")
    return payload
