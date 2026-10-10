"""Bounded isolated ArtCraft stdio sessions; this is not an OS sandbox.

Native documents stay in the chosen workspace. GUI bridges, scripting and batch
execution are deliberately unavailable. The native engine still runs with the
desktop user's permissions; confinement here validates explicit file arguments.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from .config import settings
from .security import build_security_posture

APPS = {
    "photocraft": "Layered raster editing and native image documents",
    "vectorcraft": "Vector illustration, editable documents and exports",
    "filmcraft": "Video projects and sequences",
    "lightcraft": "Photo library and non-destructive development",
    "pdfcraft": "PDF inspection, creation and editing",
    "effectcraft": "Motion graphics projects and frame rendering",
    "designcraft": "Page layout, typography and editable publishing documents",
}
MAX_MESSAGE = 16 * 1024 * 1024
MAX_ARGUMENTS = 64 * 1024
MAX_IMAGE = 8 * 1024 * 1024
MAX_STDOUT = 32 * 1024 * 1024
SESSION_SECONDS = 120
CALL_SECONDS = 30
LIMITATION = (
    "Isolated headless session, not an OS sandbox. Explicit file arguments must stay in "
    "the workspace. GUI/live bridges, batches, scripts and expressions are denied. "
    "Generic command execution is limited to explicitly supported editing commands."
)
_COMMANDS = {
    "file.new", "edit.undo", "edit.redo", "undo", "redo", "doc.new", "document.new",
    "comp.new", "layer.newText", "layer.newSolid", "frame.create", "object.delete",
    "document.open", "document.save", "document.export", "document.activate",
    "document.inspect", "document.formats", "project.new", "project.save", "project.open",
    "file.import", "puppet.addPin", "puppet.movePin", "puppet.setPin",
    "puppet.removePin", "puppet.mesh", "puppet.info", "puppet.recordPin", "puppet.follow",
}
_EDIT_NAMESPACES = {
    "shape", "paint", "object", "transform", "frame", "text", "type", "style",
    "layout", "layer", "comp", "time", "timeline", "sequence", "effects", "effect",
    "filter", "graph", "swatch", "path", "pathfinder", "keys", "selection", "select",
    "mask", "marker", "develop", "crop", "adjustment", "artboard", "group",
}
_COMMAND_TOOLS = {"run_command", "command_run", "execute_command", "execute"}
_DENIED = {
    "batch", "command_batch", "control_call", "inspect_ui", "select_tool",
    "pointer_gesture", "press_key", "type_text", "invoke_menu", "open_panel",
    "pointer", "key", "click", "drag", "menu_list", "dialog_set", "dialog_confirm",
    "dialog_cancel", "printers", "doc_print", "form_set_props",
}
_PATH_KEYS = {
    "path", "file", "filename", "filepath", "file_path", "output", "out",
    "output_path", "input_path", "destination", "source", "directory", "folder",
    "input", "output_file", "input_file", "save_path", "export_path",
    "paths", "files", "out_dir", "output_dir", "input_dir", "image",
}


def _lab() -> Path:
    return Path(os.environ.get("THOTH_ARTCRAFT_LAB_DIR") or settings.data_dir / "artcraft").expanduser().resolve()


def _inventory() -> dict:
    path = _lab() / "inventory/current.json"
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("ArtCraft inventory exceeds 1 MiB")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("apps"), dict):
        raise TypeError("ArtCraft inventory has no apps mapping")
    return value


def _selected(app: str, *, verify: bool = True) -> tuple[dict, Path]:
    if app not in APPS:
        raise ValueError("Unknown ArtCraft app; choose " + ", ".join(APPS))
    record = _inventory()["apps"].get(app)
    if record is None:
        raise ValueError(f"{app} is not installed in the selected inventory")
    if not isinstance(record, dict):
        raise TypeError(f"{app} has an invalid inventory record")
    candidates = [e for e in record.get("executables", []) if e.get("role") == "cli"]
    if len(candidates) != 1:
        raise ValueError(f"{app} must have exactly one selected CLI executable")
    entry = candidates[0]
    cli = Path(entry["path"]).resolve()
    if not cli.is_relative_to((_lab() / "apps").resolve()) or not cli.is_file():
        raise ValueError("Selected CLI must be an existing file under ArtCraftLab/apps")
    if verify:
        expected = entry.get("sha256", "")
        if not re.fullmatch(r"[a-fA-F0-9]{64}", expected):
            raise ValueError("Selected CLI has no valid SHA256 provenance")
        with cli.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual.lower() != expected.lower():
            raise ValueError("Selected CLI SHA256 differs from inventory; session refused")
    return record, cli


def artcraft_status() -> dict:
    """Read installed selections without starting any native process."""
    if settings.public_build or build_security_posture(settings).public:
        return {"ok": False, "error": "ArtCraft local capabilities are unavailable in public builds"}
    result: dict[str, Any] = {"ok": True, "lab": str(_lab()), "apps": [],
                              "limitation": LIMITATION}
    try:
        inventory = _inventory()
        for app, summary in APPS.items():
            row = {"app": app, "installed": False, "capabilities": summary}
            try:
                record, cli = _selected(app, verify=False)
                row.update(installed=True, tag=record.get("tag"), cli=str(cli),
                           release_url=record.get("release_url"))
            except (OSError, ValueError, TypeError, KeyError) as exc:
                row["error"] = str(exc)
            result["apps"].append(row)
        result["selected_at"] = inventory.get("selected_at")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        result.update(ok=False, error=f"ArtCraft unavailable: {exc}")
    return result


def _workspace(slug: str) -> Path:
    if not isinstance(slug, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", slug):
        raise ValueError("workspace must be a slug of 1–64 letters, digits, underscores or hyphens")
    root = (settings.data_dir / "artcraft").resolve()
    path = (root / slug).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Workspace resolves outside the ArtCraft data directory")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _arguments(value: Any, root: Path, key: str = "", depth: int = 0,
               property_paths: bool = False) -> Any:
    if depth > 16:
        raise ValueError("Argument nesting exceeds 16 levels")
    if isinstance(value, dict):
        return {k: _arguments(v, root, k, depth + 1, property_paths) for k, v in value.items()}
    if isinstance(value, list):
        return [_arguments(v, root, key, depth + 1, property_paths) for v in value]
    normalized = re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower()
    if normalized in {"script", "code", "expression", "expressions", "shell", "url", "uri"}:
        raise ValueError(f"Unsupported unsafe argument: {key}")
    if property_paths and normalized == "path":
        return value
    if isinstance(value, str) and (normalized in _PATH_KEYS or normalized.endswith(("_path", "_file", "_directory"))):
        if not value or "\x00" in value or "://" in value:
            raise ValueError(f"Invalid workspace file argument: {key}")
        path = Path(value)
        # Windows drive/UNC syntax must also fail on non-Windows hosts.
        if os.name != "nt" and (re.match(r"^[A-Za-z]:", value) or value.startswith("\\\\")):
            raise ValueError(f"File argument escapes workspace: {key}")
        path = (path if path.is_absolute() else root / path).resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"File argument escapes workspace: {key}")
        return str(path)
    return value


def _admit(tool: str, arguments: dict) -> None:
    if tool.startswith("ui_") or tool in _DENIED or re.search(r"script|expression|batch|bridge|control_call", tool):
        raise ValueError(f"Unsupported GUI, batch or scripting entrypoint: {tool}")
    if arguments.get("window") or arguments.get("wait") is False:
        raise ValueError("Live window capture and asynchronous native jobs are unsupported")
    if tool in _COMMAND_TOOLS or tool.startswith("cmd_"):
        command = arguments.get("command", arguments.get("id", arguments.get("command_id")))
        if tool.startswith("cmd_"):
            command = tool[4:].replace("_", ".")
        structured = arguments.get("params", {})
        if structured is not None and not isinstance(structured, dict):
            raise ValueError("Generic command params must be a structured JSON object")
        safe_edit = (isinstance(command, str) and
                     bool(re.fullmatch(r"[a-zA-Z][a-zA-Z0-9]*(?:\.[a-zA-Z][a-zA-Z0-9]*)+", command)) and
                     command.split(".")[0] in _EDIT_NAMESPACES and
                     not re.search(r"script|expression|eval|import|export|open|save|link|render|plugin|launch|execute|batch", command, re.IGNORECASE))
        if command not in _COMMANDS and not safe_edit:
            raise ValueError(f"Generic command is not yet scoped for execution: {command!r}; "
                             f"supported commands: {', '.join(sorted(_COMMANDS))}")


def _windows_job(process: subprocess.Popen) -> tuple[Any, Any]:
    """Ensure native descendants die on close, without restricting OS permissions."""
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                    ("flags", wintypes.DWORD), ("min_working", ctypes.c_size_t),
                    ("max_working", ctypes.c_size_t), ("active", wintypes.DWORD),
                    ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                    ("scheduling", wintypes.DWORD)]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("basic", BasicLimits), ("io", ctypes.c_ulonglong * 6),
                    ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                    ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                               ctypes.c_void_p, wintypes.DWORD]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateJobObjectW(None, None)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = ExtendedLimits()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or not kernel.AssignProcessToJobObject(handle, int(process._handle)):
        error = ctypes.WinError(ctypes.get_last_error())
        kernel.CloseHandle(handle)
        raise error
    return kernel, handle


class _Session:
    def __init__(self, app: str, root: Path):
        record, cli = _selected(app)
        self.provenance = {"app": app, "tag": record.get("tag"), "cli": str(cli),
                           "release_url": record.get("release_url")}
        self.deadline = time.monotonic() + SESSION_SECONDS
        self.messages: queue.Queue = queue.Queue(maxsize=4)
        self.stderr = bytearray()
        self.identifier = 0
        self.shutdown_error = None
        argv = [str(cli), "mcp"] + (["--headless"] if app == "vectorcraft" else [])
        kwargs = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os.name == "nt" else {"start_new_session": True}
        self.process = subprocess.Popen(argv, cwd=root, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
        self.job = None
        if os.name == "nt":
            try:
                self.job = _windows_job(self.process)
            except OSError:
                self.process.kill()
                self.process.wait(timeout=5)
                for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
                    pipe.close()
                raise
        self.threads = [threading.Thread(target=self._stdout, daemon=True),
                        threading.Thread(target=self._stderr, daemon=True)]
        for thread in self.threads:
            thread.start()

    def _enqueue(self, value: Any) -> bool:
        try:
            self.messages.put(value, timeout=0.1)
            return True
        except queue.Full:
            self.process.kill()
            return False

    def _stdout(self) -> None:
        total = 0
        try:
            while True:
                line = self.process.stdout.readline(MAX_MESSAGE + 1)
                if not line:
                    break
                total += len(line)
                if total > MAX_STDOUT:
                    raise ValueError("Native MCP stdout exceeds 32 MiB session limit")
                if len(line) > MAX_MESSAGE or not line.endswith(b"\n"):
                    raise ValueError("Native MCP message exceeds limit or is unterminated")
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise TypeError("Native MCP message is not an object")
                if not self._enqueue(message):
                    return
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            self._enqueue(exc)
        finally:
            self._enqueue(EOFError("Native MCP stdout closed"))

    def _stderr(self) -> None:
        total = 0
        while True:
            chunk = self.process.stderr.read(4096)
            if not chunk:
                return
            total += len(chunk)
            if total > 1024 * 1024:
                self._enqueue(ValueError("Native stderr exceeds 1 MiB session limit"))
                self.process.kill()
                return
            self.stderr.extend(chunk[:max(0, 8000 - len(self.stderr))])

    def _send(self, value: dict) -> None:
        data = (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8")
        if len(data) > MAX_ARGUMENTS:
            raise ValueError("Native request exceeds 64 KiB")
        self.process.stdin.write(data)
        self.process.stdin.flush()

    def call(self, method: str, params: dict) -> dict:
        self.identifier += 1
        identifier = self.identifier
        self._send({"jsonrpc": "2.0", "id": identifier, "method": method, "params": params})
        deadline = min(self.deadline, time.monotonic() + CALL_SECONDS)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Native MCP response deadline exceeded")
            try:
                message = self.messages.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError("Native MCP response deadline exceeded") from None
            if isinstance(message, Exception):
                raise message
            if message.get("id") == identifier:
                if "error" in message:
                    encoded = json.dumps(message["error"], ensure_ascii=False)
                    if len(encoded) > 24000:
                        encoded = json.dumps({"text": encoded[:24000], "truncated": True,
                                              "original_chars": len(encoded)}, ensure_ascii=False)
                    raise RuntimeError("Native MCP error: " + encoded)
                result = message.get("result")
                if not isinstance(result, dict):
                    raise ValueError("Native MCP result is not an object")
                return result

    def initialize(self) -> list[dict]:
        info = self.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                       "clientInfo": {"name": "thoth-artcraft", "version": "1"}})
        self.provenance["serverInfo"] = info.get("serverInfo", {})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        tools, cursor = [], None
        for _ in range(10):
            page = self.call("tools/list", {"cursor": cursor} if cursor else {})
            tools.extend(page.get("tools", []))
            if len(tools) > 500:
                raise ValueError("Native tool catalog exceeds 500 tools")
            for tool in page.get("tools", []):
                if not isinstance(tool, dict) or not isinstance(tool.get("name"), str) or not 1 <= len(tool["name"]) <= 200:
                    raise ValueError("Native tool has an invalid or oversized name")
            cursor = page.get("nextCursor")
            if not cursor:
                return tools
        raise ValueError("Native tool catalog pagination exceeds 10 pages")

    def close(self) -> None:
        try:
            self.process.stdin.close()
            self.process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            self.shutdown_error = "Native session required forced termination after stdin EOF"
            if os.name == "nt":
                try:
                    subprocess.run(["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=5, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                except (OSError, subprocess.TimeoutExpired):
                    pass
            else:
                import signal
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if self.process.poll() is None:
                self.process.kill()
            self.process.wait(timeout=5)
        finally:
            if self.job:
                kernel, handle = self.job
                kernel.CloseHandle(handle)
                self.job = None
            elif os.name != "nt":
                import signal
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            for thread in self.threads:
                thread.join(timeout=1)
            self.process.stdout.close()
            self.process.stderr.close()


def _finish(result: dict, session: _Session | None) -> dict:
    if session:
        session.close()
        result.update(provenance=session.provenance, exit_code=session.process.returncode)
        if session.stderr:
            result["stderr"] = session.stderr.decode("utf-8", errors="replace")
        if session.shutdown_error or session.process.returncode:
            result.update(ok=False, shutdown_error=session.shutdown_error or "Native process exited unsuccessfully")
    return result


def artcraft_catalog(app: str, query: str = "", tool: str | None = None, limit: int = 20) -> dict:
    """Discover native tools, returning one exact input schema only on request."""
    if settings.public_build or build_security_posture(settings).public:
        return {"ok": False, "error": "ArtCraft local capabilities are unavailable in public builds"}
    result: dict = {"ok": False, "app": app, "limitation": LIMITATION}
    session = None
    temporary = None
    try:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        if not isinstance(query, str) or len(query) > 200:
            raise ValueError("query must be at most 200 characters")
        temporary = tempfile.TemporaryDirectory(prefix="thoth-artcraft-catalog-")
        session = _Session(app, Path(temporary.name))
        tools = session.initialize()
        matches = [item for item in tools if (item.get("name") == tool if tool else
                   query.lower() in (item.get("name", "") + " " + item.get("description", "")).lower())]
        rows = []
        for item in matches[:limit]:
            row = {"name": item["name"], "title": item.get("title", item["name"])[:200],
                   "description": item.get("description", "")[:2000]}
            if tool:
                schema = item.get("inputSchema", {})
                if len(json.dumps(schema)) > 128 * 1024:
                    raise ValueError("Requested native schema exceeds 128 KiB; not truncated")
                row["inputSchema"] = schema
            rows.append(row)
        result.update(ok=bool(matches) if tool else True, tool_count=len(tools),
                      matched_count=len(matches), tools=rows, truncated=len(matches) > limit)
        if tool and not matches:
            result["error"] = f"Native tool not found: {tool}"
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, TimeoutError) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    try:
        return _finish(result, session)
    finally:
        if temporary:
            temporary.cleanup()


def _summary(value: Any, images: list[dict], depth: int = 0) -> Any:
    if depth > 16:
        return {"truncated": True, "reason": "summary nesting limit"}
    if isinstance(value, dict):
        if value.get("type") == "image":
            mime, encoded = value.get("mimeType"), value.get("data", "")
            if mime in {"image/png", "image/jpeg"} and isinstance(encoded, str) and len(encoded) <= MAX_IMAGE * 4 // 3 + 4:
                try:
                    raw = base64.b64decode(encoded, validate=True)
                    valid = raw.startswith(b"\x89PNG\r\n\x1a\n") if mime == "image/png" else raw.startswith(b"\xff\xd8\xff")
                    if valid and len(raw) <= MAX_IMAGE:
                        images[:] = [{"_image_data_base64": encoded, "content_type": mime}]
                except ValueError:
                    pass
            return {"type": "image", "mimeType": mime, "image_omitted_from_summary": True}
        rows = {k: ({"binary_omitted": True, "encoded_chars": len(v)}
                    if k.lower() in {"database64", "base64", "image_base64"} and isinstance(v, str)
                    else _summary(v, images, depth + 1))
                for k, v in list(value.items())[:100]}
        if len(value) > 100:
            rows["_summary_truncation"] = {"omitted_keys": len(value) - 100}
        return rows
    if isinstance(value, list):
        rows = [_summary(v, images, depth + 1) for v in value[:50]]
        if len(value) > 50:
            rows.append({"truncated": True, "omitted_items": len(value) - 50})
        return rows
    if isinstance(value, str):
        if value.lstrip().startswith(("{", "[")):
            try:
                parsed = json.loads(value)
                compact = json.dumps(_summary(parsed, images, depth + 1), ensure_ascii=False,
                                     separators=(",", ":"))
                if len(compact) <= 24000:
                    return compact
                return {"truncated": True, "reason": "Native JSON summary exceeds 24000 characters",
                        "original_chars": len(value), "summary_chars": len(compact)}
            except (ValueError, RecursionError):
                pass
        if len(value) > 6000:
            return {"text": value[:6000], "truncated": True, "original_chars": len(value)}
    return value


def artcraft_workflow(app: str, workspace: str, steps: list[dict]) -> dict:
    """Run at most twelve sequential native calls in one isolated session."""
    if settings.public_build or build_security_posture(settings).public:
        return {"ok": False, "error": "ArtCraft local capabilities are unavailable in public builds"}
    result: dict = {"ok": False, "app": app, "steps": [], "completed_steps": 0,
                    "partial": False, "limitation": LIMITATION}
    session, images = None, []
    try:
        if not isinstance(steps, list) or not 1 <= len(steps) <= 12:
            raise ValueError("steps must contain 1–12 tool calls")
        if len(json.dumps(steps, ensure_ascii=False).encode()) > MAX_ARGUMENTS:
            raise ValueError("Workflow arguments exceed 64 KiB")
        root = _workspace(workspace)
        result["workspace"] = str(root)
        prepared = []
        for step in steps:
            if not isinstance(step, dict) or not isinstance(step.get("tool"), str) or not isinstance(step.get("arguments", {}), dict):
                raise TypeError("Each step requires a tool name and arguments object")
            arguments = _arguments(step.get("arguments", {}), root,
                                   property_paths=app == "effectcraft" and step["tool"] in
                                   {"get_property", "set_property", "add_keyframe", "remove_keyframe"})
            _admit(step["tool"], arguments)
            prepared.append((step["tool"], arguments))
        session = _Session(app, root)
        known = {item["name"] for item in session.initialize()}
        for tool, arguments in prepared:
            if tool not in known:
                raise ValueError(f"Unknown native tool: {tool}")
            native = session.call("tools/call", {"name": tool, "arguments": arguments})
            summary = _summary(native, images)
            encoded = json.dumps(summary, ensure_ascii=False)
            if len(encoded) > 24000:
                summary = {"truncated": True, "reason": "Native step summary exceeds 24000 characters",
                           "original_summary_chars": len(encoded)}
            row = {"tool": tool, "ok": not bool(native.get("isError")), "result": summary}
            result["steps"].append(row)
            if native.get("isError"):
                result["error"] = {"tool": tool, "native": row["result"]}
                break
            result["completed_steps"] += 1
        else:
            result["ok"] = True
    except (OSError, ValueError, KeyError, RuntimeError, TimeoutError, TypeError) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    result["partial"] = not result["ok"] and bool(result["completed_steps"])
    if images:
        result.update(images[-1])
    _finish(result, session)
    result["partial"] = not result["ok"] and bool(result["completed_steps"])
    return result
