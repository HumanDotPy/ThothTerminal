from __future__ import annotations

import argparse
import base64
import ctypes
from datetime import date, datetime
from decimal import Decimal
import importlib
import json
import os
from pathlib import Path
import traceback
from typing import Any, Callable
from uuid import UUID


ALLOWED_FUNCTIONS = {
    "thoth_terminal.connectors.web_list_browser:fetch_web_list_browser",
    "thoth_terminal.connectors.x_list:fetch_x_list",
    "thoth_terminal.connectors.x_list:fetch_x_thread",
    "thoth_terminal.connectors.paid_reader_browser:fetch_substack_inbox_browser",
    "thoth_terminal.connectors.paid_reader_browser:fetch_note_creator_browser",
    "thoth_terminal.connectors.x_list:probe_x_list",
    "thoth_terminal.connectors.web_list_browser:probe_web_list_browser",
    "thoth_terminal.connectors.paid_reader_browser:probe_paid_reader_browser",
}


def _is_browser_launch_failure(exc: BaseException) -> bool:
    """Classify only recognizable Playwright/Chromium startup failures."""

    text = f"{type(exc).__module__}.{type(exc).__name__}: {exc}".lower()
    strong_markers = (
        "browsertype.launch",
        "browser_type.launch",
        "chromium.launch",
        "launch_persistent_context",
        "executable doesn't exist",
        "executable does not exist",
        "please run the following command to download new browsers",
        "playwright install",
        "chrome-headless",
        "failed to launch browser",
        "failed to launch chromium",
    )
    if any(marker in text for marker in strong_markers):
        return True
    return "playwright" in text and "launch" in text and "browser" in text


def _suppress_windows_child_error_ui() -> int | None:
    """Disable crash/error dialogs for this wrapper process and its children.

    Playwright and Chromium run below this dedicated child.  The Windows
    process error mode is inherited by descendants, unlike a registry/WER
    setting, so normal Edge processes are unaffected.
    """

    if os.name != "nt":
        return None
    try:
        kernel32 = ctypes.windll.kernel32
        get_error_mode = getattr(kernel32, "GetErrorMode", None)
        previous = int(get_error_mode()) if get_error_mode is not None else 0
        flags = previous | 0x0001 | 0x0002  # SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX
        kernel32.SetErrorMode.argtypes = [ctypes.c_uint]
        kernel32.SetErrorMode.restype = ctypes.c_uint
        return int(kernel32.SetErrorMode(flags))
    except (AttributeError, OSError, TypeError):
        # A failed best-effort suppression must not prevent a connector from
        # returning a structured result/error through the IPC file.
        return None


def _json_default(value: object) -> object:
    if isinstance(value, datetime):
        return {"__thoth_ipc_type__": "datetime", "value": value.isoformat()}
    if isinstance(value, date):
        return {"__thoth_ipc_type__": "date", "value": value.isoformat()}
    if isinstance(value, Path):
        return {"__thoth_ipc_type__": "path", "value": str(value)}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {
            "__thoth_ipc_type__": "bytes",
            "value": base64.b64encode(bytes(value)).decode("ascii"),
        }
    if isinstance(value, Decimal):
        return {"__thoth_ipc_type__": "decimal", "value": str(value)}
    if isinstance(value, UUID):
        return {"__thoth_ipc_type__": "uuid", "value": str(value)}
    raise TypeError(f"browser ingestion result is not JSON serializable: {type(value).__name__}")


def _resolve_function(name: str) -> Callable[[dict[str, Any]], Any]:
    if name not in ALLOWED_FUNCTIONS:
        raise ValueError(f"browser connector is not allow-listed: {name}")
    module_name, function_name = name.split(":", 1)
    module = importlib.import_module(module_name)
    function = getattr(module, function_name)
    if not callable(function):
        raise TypeError(f"browser connector is not callable: {name}")
    return function


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, default=_json_default), encoding="utf-8")
    os.replace(temp, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    parser.add_argument("--result", required=True)
    args = parser.parse_args()
    result_path = Path(args.result)
    try:
        _suppress_windows_child_error_ui()
        request = json.loads(Path(args.request).read_text(encoding="utf-8"))
        if not isinstance(request, dict) or not isinstance(request.get("config"), dict):
            raise ValueError("invalid browser ingestion request")
        progress_path = Path(str(request["config"].get("_thoth_progress_path") or ""))
        if str(progress_path):
            _atomic_json(
                progress_path,
                {"stage": "connector_started", "monotonic": __import__("time").monotonic()},
            )
        function = _resolve_function(str(request.get("function") or ""))
        result = function(dict(request["config"]))
        _atomic_json(result_path, {"ok": True, "result": result})
        return 0
    except BaseException as exc:
        error_type = "BrowserLaunchCrash" if _is_browser_launch_failure(exc) else type(exc).__name__
        _atomic_json(
            result_path,
            {
                "ok": False,
                "error_type": error_type,
                "error": str(exc),
                "traceback": traceback.format_exc(limit=20),
            },
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
