"""Private, version-pinned DSH session transport for the native Thoth agent page.

The installed Harness owns sessions and teams. Thoth retains no duplicate log and
never gives the Harness launch token or signed browser cookie to a page.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests


class HarnessUnavailable(RuntimeError):
    pass


class HarnessTurnFailed(HarnessUnavailable):
    """A native terminal event ended the turn; retry may create a new session."""


class HarnessRemoteError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


_BRIDGE = Path(__file__).with_name("deepseek_harness_bridge.mjs")
_PIN = "0.1.7-rc.1"


class DeepSeekHarnessService:
    def __init__(self, install_root: Path | None = None):
        self.root = (install_root or Path(os.environ.get("DEEPSEEK_HARNESS_INSTALL_ROOT") or Path.home() / "Apps" / "DeepSeekHarness")).resolve()
        self._lock = threading.RLock()
        self._identity: tuple[int, str] | None = None
        self._cookie = ""
        self._origin = ""
        self._connection_checked_at = 0.0
        self._runtime_stamp: tuple[int, int] | None = None

    @property
    def _entry(self) -> Path:
        return self.root / "node_modules" / "@deepseek-ai" / "dsh" / "lib" / "bin.js"

    def _owned_state(self) -> dict[str, object]:
        try:
            state = json.loads((self.root / "run.json").read_text(encoding="utf-8-sig"))
            pid = int(state["pid"])
            port = int(state["port"])
            started = datetime.fromisoformat(str(state["startedUtc"]).replace("Z", "+00:00"))
            entry = Path(str(state["entry"])).resolve()
            stdout = Path(str(state["stdout"])).resolve()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise HarnessUnavailable("DeepSeek Harness is stopped. Start it from this page or the Start Menu.") from exc
        if pid <= 0 or port != 3080 or entry != self._entry.resolve() or not stdout.is_relative_to((self.root / "logs").resolve()):
            raise HarnessUnavailable("DeepSeek Harness runtime ownership could not be validated.")
        if not self._entry.is_file():
            raise HarnessUnavailable("DeepSeek Harness is not installed at the expected path.")
        try:
            version = json.loads((self._entry.parent.parent / "package.json").read_text(encoding="utf-8"))["version"]
        except (OSError, ValueError, KeyError) as exc:
            raise HarnessUnavailable("DeepSeek Harness installation metadata is unavailable.") from exc
        if version != _PIN:
            raise HarnessUnavailable(f"This native interface requires DeepSeek Harness {_PIN}; revalidate the bridge before upgrading.")
        script = (
            f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}' -ErrorAction SilentlyContinue; "
            "if($p){$s=(Get-Process -Id $p.ProcessId).StartTime.ToUniversalTime().ToString('o'); "
            "@{command=[string]$p.CommandLine;startedUtc=$s}|ConvertTo-Json -Compress}"
        )
        try:
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, text=True, timeout=8, check=True, creationflags=subprocess.CREATE_NO_WINDOW,
            )
            process = json.loads(result.stdout)
            actual = datetime.fromisoformat(process["startedUtc"].replace("Z", "+00:00"))
            command = str(process["command"])
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
            raise HarnessUnavailable("DeepSeek Harness is not running as the installed application.") from exc
        if abs((actual - started.astimezone(timezone.utc)).total_seconds()) >= 3:
            raise HarnessUnavailable("DeepSeek Harness runtime identity changed; reopen it from the Start Menu.")
        if str(entry).casefold() not in command.casefold() or not re.search(r"--profile\s+web(?:\s|$)", command, re.I) or not re.search(r"--no-open(?:\s|$)", command, re.I) or not re.search(r"--port\s+3080(?:\s|$)", command, re.I):
            raise HarnessUnavailable("DeepSeek Harness runtime command does not match the installed application.")
        return {"pid": pid, "startedUtc": str(state["startedUtc"]), "stdout": stdout, "port": port}

    def _connect(self) -> None:
        state = self._owned_state()
        identity = (int(state["pid"]), str(state["startedUtc"]))
        if identity == self._identity and self._cookie:
            return
        try:
            output = Path(state["stdout"]).read_text(encoding="utf-8", errors="replace")
            matches = re.findall(r"dsh web:\s*(https?://[^\s]+)", output)
            parsed = urlparse(matches[-1])
            token = parse_qs(parsed.query).get("token", [])
            if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.port != 3080 or parsed.path != "/" or len(token) != 1:
                raise ValueError("invalid launch URL")
            origin = "http://127.0.0.1:3080/"
            session = requests.Session()
            session.trust_env = False
            response = session.get(matches[-1], allow_redirects=False, timeout=5)
            if response.status_code != 303 or not session.cookies:
                raise ValueError("launch authentication failed")
            cookie = "; ".join(f"{item.name}={item.value}" for item in session.cookies)
        except (OSError, IndexError, ValueError, requests.RequestException) as exc:
            raise HarnessUnavailable("DeepSeek Harness is starting or its authenticated connection is unavailable.") from exc
        self._identity, self._origin, self._cookie = identity, origin, cookie

    def status(self) -> dict[str, object]:
        try:
            with self._lock:
                self._connect()
            return {"available": True, "version": _PIN}
        except HarnessUnavailable as exc:
            return {"available": False, "message": str(exc), "version": _PIN}

    def browser_connection(self) -> tuple[str, str]:
        """Private upstream lease for the mounted native UI; never serialize it.

        A page loads many independent assets/RPCs. Reuse process validation for
        five seconds, but invalidate immediately when its owned runtime changes.
        The existing connection owns authentication; no browser gets its cookie.
        """
        with self._lock:
            try:
                info = (self.root / "run.json").stat()
                stamp = (info.st_mtime_ns, info.st_size)
            except OSError as exc:
                raise HarnessUnavailable("DeepSeek Harness is stopped.") from exc
            if stamp != self._runtime_stamp or time.monotonic() - self._connection_checked_at >= 5:
                self._connect()
                self._runtime_stamp = stamp
                self._connection_checked_at = time.monotonic()
            return self._origin, self._cookie

    def start(self) -> dict[str, object]:
        # Desktop owns the independent installed application. Launching it from
        # a supervised web/worker child would put it in that child's Windows job
        # and kill personal sessions whenever Thoth restarts that service.
        from thoth_terminal.config import settings
        control_url = str(getattr(settings, "desktop_control_url", "") or "")
        control_token = str(getattr(settings, "desktop_control_token", "") or "")
        if control_url and control_token:
            from thoth_terminal.desktop.control_api import RemoteDesktopBridge
            try:
                result = RemoteDesktopBridge(control_url, control_token, timeout_s=115).perform_action("start_harness")
                status = result.get("harness")
                if not isinstance(status, dict):
                    raise ValueError("Missing Harness startup result")
                return status
            except (requests.RequestException, RuntimeError, ValueError):
                raise HarnessUnavailable("Desktop could not start Harness. Check runtime health and retry.") from None
        if os.environ.get("THOTH_RUNTIME_SUPERVISION_ID"):
            raise HarnessUnavailable("The supervised worker has no desktop Harness owner connection. Restart Thoth to restore it.")
        return self.start_owned_application()

    def start_owned_application(self) -> dict[str, object]:
        """Launch/reuse DSH from the desktop owner or a standalone operator."""
        script = self.root / "Start-DeepSeekHarness.ps1"
        if not script.is_file():
            raise HarnessUnavailable("Install DeepSeek Harness before starting its native Thoth interface.")
        try:
            subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script), "-NoOpen"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=110, check=True, creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise HarnessUnavailable("DeepSeek Harness did not start. Check its private logs and the installed launcher.") from exc
        return self.status()

    def ensure_thoth_connection(self) -> dict[str, object]:
        """Recycle an idle Harness whose MCP client predates the Thoth web boot."""
        status = self.status()
        if not status.get("available"):
            return self.start()
        from thoth_terminal.config import settings

        runtime_dir = Path(settings.runtime_state_dir)
        if not runtime_dir.is_absolute():
            runtime_dir = Path(__file__).resolve().parents[2] / runtime_dir
        try:
            web = json.loads((runtime_dir / "web.json").read_text(encoding="utf-8-sig"))
            if Path(str(web.get("repo_root") or "")).resolve() != Path(__file__).resolve().parents[2]:
                return status
            web_started = datetime.fromisoformat(str(web["started_at_utc"]).replace("Z", "+00:00"))
            owned = self._owned_state()
            harness_started = datetime.fromisoformat(str(owned["startedUtc"]).replace("Z", "+00:00"))
        except (OSError, ValueError, KeyError, TypeError):
            return status
        if harness_started + timedelta(seconds=2) >= web_started:
            return status
        deadline = time.monotonic() + 120
        while True:
            listing = self.call("list", {})
            if not any(
                item.get("running")
                or any(((item.get("projections") or {}).get("values") or {}).get("inbox", {}).get(slot)
                       for slot in ("next-turn", "next-step"))
                for item in listing.get("items", [])
            ):
                break
            if time.monotonic() >= deadline:
                raise HarnessUnavailable("Thoth restarted while a Harness session is active; retry the scheduled run after it settles.")
            time.sleep(5)
        stop_script = self.root / "Stop-DeepSeekHarness.ps1"
        if not stop_script.is_file():
            raise HarnessUnavailable("DeepSeek Harness must be restarted to reconnect Thoth MCP, but its stop script is missing.")
        try:
            subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(stop_script)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=30, check=True, creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise HarnessUnavailable("DeepSeek Harness could not be recycled after the Thoth web restart.") from exc
        return self.start()

    def call(self, method: str, request: dict[str, object], *, timeout_s: float = 25) -> object:
        if method not in {"list", "create", "prompt", "page", "projections", "rename", "selectModel", "cancel", "modelCatalog", "intake", "workspace/create", "workspace/rename"}:
            raise ValueError("Unsupported Harness operation")
        with self._lock:
            self._connect()
            payload = {"installRoot": str(self.root), "origin": self._origin, "cookie": self._cookie, "method": method, "request": request}
        try:
            result = subprocess.run(
                ["node.exe", str(_BRIDGE)], input=json.dumps(payload), capture_output=True,
                text=True, encoding="utf-8", timeout=timeout_s, check=True, cwd=self.root, creationflags=subprocess.CREATE_NO_WINDOW,
            )
            answer = json.loads(result.stdout)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            # Node stderr can include private transport details; never expose it.
            raise HarnessUnavailable("DeepSeek Harness session connection failed. Refresh after checking its status.") from exc
        if answer.get("ok") is not True:
            error = answer.get("error") or {}
            raise HarnessRemoteError(str(error.get("code") or "harness/error"), str(error.get("message") or "Harness request failed"))
        return answer.get("value")

    def intake_turn(self, request: dict[str, object]) -> dict[str, object]:
        directory = self.root / "workspace" / "ingestion"
        directory.mkdir(parents=True, exist_ok=True)
        workspace = self.call("workspace/create", {"path": str(directory)})["workspace"]
        if workspace["title"] == directory.name:
            self.call("workspace/rename", {"workspaceId": workspace["workspaceId"], "title": "Thoth Ingestion"})
        request = {**request, "workspaceId": workspace["workspaceId"]}
        try:
            return self.call("intake", request, timeout_s=float(request["timeoutSeconds"]) + 60)
        except HarnessUnavailable:
            try:
                self.call("cancel", {"sessionId": request["sessionId"]})
            except (HarnessUnavailable, HarnessRemoteError):
                pass
            raise

    def ensure_research_workspace(self) -> dict[str, object]:
        # The installed launcher already uses this cwd. Register that same path
        # instead of changing filesystem/skill context and the model prefix.
        directory = self.root / "workspace"
        result = self.call("workspace/create", {"path": str(directory)})
        workspace = dict(result["workspace"])
        if result["created"] or workspace["title"] == directory.name:
            workspace = dict(self.call("workspace/rename", {
                "workspaceId": workspace["workspaceId"], "title": "Thoth Research",
            })["workspace"])
        return workspace

    def check_thoth_connection(self) -> None:
        """Require the scoped Thoth session read before submitting paid research."""
        from thoth_terminal.config import settings
        from thoth_terminal.mcp_client import McpRpcError, McpTransportError, ThothMcpClient

        if not settings.mcp_auth_token:
            raise HarnessUnavailable("Thoth MCP authentication is not configured; scheduled research was not started.")
        auth_token = hmac.new(
            settings.mcp_auth_token.encode(), b"thoth:dsh:worldview-v1", hashlib.sha256,
        ).hexdigest()
        with requests.Session() as transport:
            transport.trust_env = False
            client = ThothMcpClient(
                base_url=f"http://127.0.0.1:{int(settings.port)}",
                auth_token=auth_token, timeout_s=15, session=transport,
            )
            try:
                result = client.call_tool_envelope("session_info", {}, tool_profile="worldview_v1")
            except (McpRpcError, McpTransportError, requests.RequestException):
                raise HarnessUnavailable("Thoth session_info preflight failed; scheduled research was not started.") from None
        if result.get("isError"):
            raise HarnessUnavailable("Thoth session_info returned an error; scheduled research was not started.")

    def research_turn(self, *, session_id: str, request_id: str, prompt: str,
                      timeout_s: float, content_parts: list[dict[str, str]] | None = None,
                      submitted: bool = False, workspace_id: str | None = None) -> dict[str, object]:
        """Submit or resume one durable Harness turn and return its final text.

        The caller persists session_id and request_id before entering this method.
        A resumed turn is inspected before it is submitted again.
        """
        deadline = time.monotonic() + max(30.0, timeout_s)
        adopted = False
        if not submitted:
            projection = self.call("projections", {"sessionId": session_id})
            if projection is None:
                raise HarnessUnavailable("The recorded Harness research session is missing.")
            values = projection.get("values") or {}
            inbox = values.get("inbox") or {}
            queued = list(inbox.get("next-turn") or []) + list(inbox.get("next-step") or [])
            if not values.get("sessionStats", {}).get("turns") and not values.get("turnOutline") and not queued:
                self.call("prompt", {
                    "sessionId": session_id, "requestId": request_id,
                    "mode": "queue", "content": [{"type": "text", "text": prompt}, *(content_parts or [])],
                })
        while time.monotonic() < deadline:
            listing = self.call("list", {})
            item = next((row for row in listing.get("items", []) if row.get("sessionId") == session_id), None)
            if item is None:
                raise HarnessUnavailable("The recorded Harness research session disappeared.")
            projection = item.get("projections") or {}
            if not item.get("running") or item.get("agentAvailable") is False:
                # Cold list projections are cached, possibly partial hints.
                # Recovery and completion require a fresh persisted baseline.
                projection = self.call("projections", {"sessionId": session_id})
                if projection is None:
                    raise HarnessUnavailable("The recorded Harness research session is missing.")
            values = projection.get("values") or {}
            turns = values.get("sessionStats") or {}
            if item.get("agentAvailable") is False and not adopted:
                # list/page are cold reads. Native adoption restores a stopped
                # Agent and closes interrupted log tails; it does not re-prompt
                # or cancel a still-running turn. Never create a second session.
                self.call("create", {"sessionId": session_id, **(
                    {"workspaceId": workspace_id} if workspace_id else {"cwd": str(self.root / "workspace")}
                )})
                adopted = True
                continue
            if not item.get("running") and int(turns.get("turns") or 0) > 0:
                cursor = int(projection.get("asOfSeq") or 0)
                page = self.call("page", {
                    "address": {"kind": "session", "sessionId": session_id},
                    "throughSeq": cursor, "maxMessages": 500,
                    "turnWindow": {"minMessages": 20, "minTurns": 1},
                })
                records = page.get("records") or []
                terminal = [record.get("event") or {} for record in records if isinstance(record, dict)]
                outline = values.get("turnOutline") or []
                target_turn = outline[-1].get("turn") if outline else None
                if target_turn is not None:
                    terminal = [event for event in terminal if (event.get("data") or {}).get("turn") == target_turn]
                end = next((event for event in reversed(terminal) if event.get("type") == "turn/end"), None)
                # A stopped runner can retain an open turn after interruption.
                # Neither a started-turn count nor assistant commentary is a
                # completion receipt. Keep observing the same durable session.
                if end is None:
                    time.sleep(5)
                    continue
                reason = (end.get("data") or {}).get("reason") or {}
                if reason.get("kind") not in {"completed", "max-tokens"}:
                    error = reason.get("error") or {}
                    code = error.get("code") or reason.get("kind") or "unknown termination"
                    message = error.get("message") or "Inspect its session history."
                    raise HarnessTurnFailed(f"The Harness research turn failed ({code}): {message}"[:2000])
                messages = [event for event in terminal if event.get("type") == "assistant/message"]
                if messages:
                    content = ((messages[-1].get("data") or {}).get("message") or {}).get("content") or []
                    answer = "\n".join(part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text").strip()
                    if answer:
                        return {"text": answer, "cursor": cursor, "values": values}
                raise HarnessTurnFailed("The Harness research turn ended without a final assistant response.")
            time.sleep(5)
        raise HarnessUnavailable("The Harness research turn exceeded its configured deadline.")


harness_service = DeepSeekHarnessService()
