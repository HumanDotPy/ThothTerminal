"""Execute agent-authored calculators outside Thoth's host and data permissions.

Only the operator selects the already installed image. Agent input never controls
Docker arguments, mounts, environment, entrypoint, or resource limits.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from typing import Any

MAX_SOURCE_CHARS = 40_000
MAX_JSON_BYTES = 65_536
MAX_STREAM_BYTES = 131_072
MAX_REQUEST_BYTES = 2 * MAX_JSON_BYTES + 256
MAX_JSON_DEPTH = 32
DEFAULT_IMAGE = "python:3.12-slim-bookworm"
LIMITS = {"wall_seconds": 10, "memory_bytes": 268_435_456, "cpus": 1, "pids": 32,
          "input_bytes": MAX_JSON_BYTES, "output_bytes": MAX_JSON_BYTES}

# The runner is trusted code in the chosen image; source/input arrive on stdin,
# never on a shell command line or in a mounted file.
_RUNNER = r'''
import contextlib, io, json, math, sys
class LimitedText(io.StringIO):
    def write(self, value):
        if self.tell() + len(value) > 65536:
            raise ValueError("calculator diagnostic output exceeded its limit")
        return super().write(value)
def check(value, depth=0):
    if depth > 32: raise ValueError("output nesting exceeds its limit")
    if isinstance(value, float) and not math.isfinite(value): raise ValueError("output must be finite")
    if isinstance(value, dict):
        if not all(isinstance(k, str) for k in value): raise ValueError("output keys must be strings")
        for v in value.values(): check(v, depth+1)
    elif isinstance(value, list):
        for v in value: check(v, depth+1)
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise ValueError("output must contain JSON values")
try:
    request = json.loads(sys.stdin.buffer.read(131329))
    namespace = {"__name__": "thoth_calculator"}
    with contextlib.redirect_stdout(LimitedText()), contextlib.redirect_stderr(LimitedText()):
        exec(compile(request["source_code"], "<calculator>", "exec"), namespace)
        result = namespace["calculate"](request["inputs"])
        check(result)
        encoded = json.dumps(result, allow_nan=False, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 65536: raise ValueError("output exceeds its byte limit")
    print(json.dumps({"ok": True, "output": result}, allow_nan=False, ensure_ascii=True))
except BaseException as exc:
    print(json.dumps({"ok": False, "error": (type(exc).__name__ + ": " + str(exc))[:2048]}, ensure_ascii=True))
'''


def canonical_json(value: Any, *, max_bytes: int = MAX_JSON_BYTES) -> bytes:
    def check(item: Any, depth: int = 0) -> None:
        if depth > MAX_JSON_DEPTH:
            raise ValueError("JSON nesting exceeds its limit")
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ValueError("JSON keys must be strings")
            for child in item.values():
                check(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                check(child, depth + 1)
        elif item is not None and not isinstance(item, (str, int, float, bool)):
            raise ValueError("only JSON values are supported")
    check(value)
    try:
        encoded = json.dumps(value, allow_nan=False, ensure_ascii=True, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ValueError("finite JSON values are required") from exc
    if len(encoded) > max_bytes:
        raise ValueError(f"JSON exceeds {max_bytes} bytes")
    return encoded


def validate_execution_spec(spec: Any) -> dict[str, str]:
    if not isinstance(spec, dict) or set(spec) != {"runtime", "source_code"}:
        raise ValueError("execution requires only runtime and source_code")
    if spec.get("runtime") != "python":
        raise ValueError("execution runtime must be python")
    source = spec.get("source_code")
    if not isinstance(source, str) or not source.strip() or len(source) > MAX_SOURCE_CHARS:
        raise ValueError(f"source_code must contain 1..{MAX_SOURCE_CHARS} characters")
    canonical_json(source)
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise ValueError("source_code must be valid Python") from exc
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "calculate"]
    if len(functions) != 1:
        raise ValueError("source_code must define calculate(inputs)")
    return {"runtime": "python", "source_code": source}


def _creation_flags() -> int:
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def _resolve_image(docker: str) -> str:
    reference = os.environ.get("THOTH_RESEARCH_EXTENSION_IMAGE", DEFAULT_IMAGE).strip()
    if not reference or len(reference) > 256 or reference.startswith("-"):
        raise ValueError("invalid operator-selected calculator image")
    result = subprocess.run([docker, "image", "inspect", reference], capture_output=True,
                            timeout=5, creationflags=_creation_flags(), check=False)
    if result.returncode:
        raise ValueError(f"calculator image is unavailable; install {reference} before execution")
    images = json.loads(result.stdout)
    if len(images) != 1 or images[0].get("Os") != "linux":
        raise ValueError("calculator execution requires one Linux image")
    image = images[0]
    if (image.get("Config") or {}).get("Volumes"):
        raise ValueError("calculator image must not declare filesystem volumes")
    image_id = str(image.get("Id") or "")
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
        raise ValueError("calculator image did not resolve to an immutable image ID")
    return image_id


def _command(docker: str, name: str, image: str) -> list[str]:
    return [docker, "run", "--rm", "--pull=never", "--name", name,
            "--label", "thoth.research-extension=true", "--network=none", "--read-only",
            "--user=65534:65534", "--cap-drop=ALL", "--security-opt=no-new-privileges=true",
            "--ipc=none", "--memory=256m", "--memory-swap=256m", "--cpus=1",
            "--pids-limit=32", "--ulimit=cpu=8:9", "--log-driver=none",
            "--entrypoint=/usr/local/bin/python", "-i", image, "-I", "-B", "-c", _RUNNER]


def get_execution_image_id() -> str:
    """Resolve the operator's installed runtime without creating a container."""
    docker = shutil.which("docker")
    if not docker:
        raise ValueError("Docker CLI is unavailable; no host execution fallback is permitted")
    return _resolve_image(docker)


def _run_bounded(command: list[str], request: bytes, timeout: float) -> tuple[int, bytes, bytes]:
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, creationflags=_creation_flags())
    streams = [bytearray(), bytearray()]
    overflow = threading.Event()

    def read_stream(index: int, pipe: Any) -> None:
        while True:
            block = pipe.read(4096)
            if not block:
                return
            remaining = MAX_STREAM_BYTES - len(streams[index])
            streams[index].extend(block[:max(0, remaining)])
            if len(block) > remaining:
                overflow.set()
                return

    def write_input() -> None:
        try:
            process.stdin.write(request)
            process.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass

    readers = [threading.Thread(target=read_stream, args=(i, pipe), daemon=True)
               for i, pipe in enumerate((process.stdout, process.stderr))]
    writer = threading.Thread(target=write_input, daemon=True)
    for thread in (*readers, writer):
        thread.start()
    deadline = time.monotonic() + timeout
    try:
        while process.poll() is None:
            if overflow.wait(0.02):
                raise ValueError("calculator process output exceeded its limit")
            if time.monotonic() >= deadline:
                raise TimeoutError("calculator exceeded its wall-clock limit")
        for thread in readers:
            thread.join(timeout=1)
        if overflow.is_set():
            raise ValueError("calculator process output exceeded its limit")
        return process.returncode, bytes(streams[0]), bytes(streams[1])
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        writer.join(timeout=1)
        for pipe in (process.stdin, process.stdout, process.stderr):
            try:
                pipe.close()
            except OSError:
                pass


def execute_calculator(spec: Any, inputs: Any) -> dict[str, Any]:
    validated = validate_execution_spec(spec)
    input_bytes = canonical_json(inputs)
    source = validated["source_code"]
    receipt: dict[str, Any] = {
        "execution_id": uuid.uuid4().hex, "ok": False, "output": None, "error": None,
        "error_type": None,
        "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "input_sha256": hashlib.sha256(input_bytes).hexdigest(), "output_sha256": None,
        "image_id": None, "elapsed_ms": 0, "limits": dict(LIMITS),
        "isolation": "docker_no_network_no_mounts_read_only_nonroot",
    }
    docker = shutil.which("docker")
    if not docker:
        receipt["error"] = "Docker CLI is unavailable; no host execution fallback is permitted"
        receipt["error_type"] = "infrastructure_error"
        return receipt
    name = "thoth-calculator-" + receipt["execution_id"]
    started = time.monotonic()
    container_attempted = False
    failure_type = "infrastructure_error"
    try:
        receipt["image_id"] = _resolve_image(docker)
        request = canonical_json({"source_code": source, "inputs": inputs}, max_bytes=MAX_REQUEST_BYTES)
        container_attempted = True
        code, stdout, stderr = _run_bounded(_command(docker, name, receipt["image_id"]),
                                           request, LIMITS["wall_seconds"])
        if code:
            if code in {137, 152}:
                failure_type = "resource_limit"
            raise ValueError(f"calculator exited {code}: " + stderr.decode("utf-8", "replace")[:2048])
        failure_type = "calculator_error"
        result = json.loads(stdout)
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise ValueError(str(result.get("error") if isinstance(result, dict) else "invalid result")[:2048])
        if "output" not in result:
            raise ValueError("calculator result lacks output")
        output_bytes = canonical_json(result["output"])
        receipt.update(ok=True, output=result["output"],
                       output_sha256=hashlib.sha256(output_bytes).hexdigest())
    except (ValueError, OSError, TimeoutError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        receipt["error"] = str(exc)[:2048]
        receipt["error_type"] = "execution_timeout" if isinstance(exc, TimeoutError) else failure_type
    finally:
        if container_attempted:
            try:
                cleaned = subprocess.run([docker, "rm", "--force", name], capture_output=True,
                                         timeout=5, creationflags=_creation_flags(), check=False)
                if cleaned.returncode and b"No such container" not in cleaned.stderr:
                    receipt.update(ok=False, error="Calculator container cleanup failed", error_type="infrastructure_error", output=None,
                                   output_sha256=None)
            except (OSError, subprocess.SubprocessError):
                receipt.update(ok=False, error="Calculator container cleanup failed", error_type="infrastructure_error", output=None,
                               output_sha256=None)
        receipt["elapsed_ms"] = round((time.monotonic() - started) * 1000, 2)
    return receipt
