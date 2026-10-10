from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


def _jsonrpc_error(rpc_id: object, code: int, message: str) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}


def _read_headers() -> dict[str, str] | None:
    headers: dict[str, str] = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        text = line.decode("utf-8", errors="replace").strip()
        if text == "":
            return headers
        if ":" not in text:
            continue
        key, value = text.split(":", 1)
        headers[key.strip().lower()] = value.strip()


def _read_message() -> object | None:
    headers = _read_headers()
    if headers is None:
        return None
    length_raw = headers.get("content-length")
    if not length_raw:
        return None
    try:
        length = int(length_raw)
    except Exception:
        return None
    if length <= 0:
        return None
    body = sys.stdin.buffer.read(length)
    if not body:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
        return None


def _write_message(payload: object) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(data)}\r\n\r\n".encode("ascii"))
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def _workspace_venv_python(root: Path | None = None) -> Path | None:
    root = root or Path(__file__).resolve().parents[1]
    candidates = [
        root / ".venv" / "Scripts" / "python.exe",
        root / ".venv" / "bin" / "python",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _same_executable(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return os.path.normcase(os.path.abspath(str(left))) == os.path.normcase(os.path.abspath(str(right)))


def _should_reexec_with_venv(
    candidate: Path | None,
    *,
    current_executable: Path | None = None,
) -> bool:
    if os.environ.get("THOTH_MCP_NO_VENV_REEXEC") or os.environ.get("THOTH_MCP_VENV_REEXECED"):
        return False
    if candidate is None or not candidate.exists():
        return False
    current = current_executable or Path(sys.executable)
    return not _same_executable(candidate, current)


def _maybe_reexec_with_workspace_venv() -> None:
    candidate = _workspace_venv_python()
    if not _should_reexec_with_venv(candidate):
        return
    assert candidate is not None
    os.environ["THOTH_MCP_VENV_REEXECED"] = "1"
    os.execv(str(candidate), [str(candidate), *sys.argv])


def serve_forever() -> None:
    # Import lazily so basic module import is cheap and doesn't touch FastAPI unless needed.
    from thoth_terminal.web.app import _mcp_handle_message

    tool_profile = str(os.environ.get("THOTH_MCP_TOOL_PROFILE") or "full").strip() or "full"

    while True:
        msg = _read_message()
        if msg is None:
            return

        if isinstance(msg, list):
            responses: list[dict[str, object]] = []
            for item in msg:
                if not isinstance(item, dict):
                    responses.append(_jsonrpc_error(None, -32600, "Invalid Request"))
                    continue
                try:
                    resp = _mcp_handle_message(item, tool_profile=tool_profile)
                except Exception as exc:
                    resp = _jsonrpc_error(item.get("id"), -32000, f"{type(exc).__name__}: {exc}")
                if resp is not None and resp.get("id") is not None:
                    responses.append(resp)
            if responses:
                _write_message(responses)
            continue

        if not isinstance(msg, dict):
            _write_message(_jsonrpc_error(None, -32600, "Invalid Request"))
            continue

        try:
            resp = _mcp_handle_message(msg, tool_profile=tool_profile)
        except Exception as exc:
            resp = _jsonrpc_error(msg.get("id"), -32000, f"{type(exc).__name__}: {exc}")

        if resp is None or resp.get("id") is None:
            continue
        _write_message(resp)


def main() -> None:
    _maybe_reexec_with_workspace_venv()
    serve_forever()


if __name__ == "__main__":
    main()
