from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import secrets
import threading
import requests


class DesktopControlServer:
    """Tiny loopback-only bridge between the isolated web process and Qt."""

    def __init__(self, controller: object | None, *, host: str = "127.0.0.1", port: int = 0, token: str | None = None) -> None:
        self.controller = controller
        self.host = str(host)
        self.port = int(port)
        self.token = str(token or secrets.token_urlsafe(32))
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        server = self._server
        port = int(server.server_address[1]) if server is not None else self.port
        return f"http://{self.host}:{port}"

    def start(self) -> None:
        owner = self
        token = self.token

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args: object) -> None:
                return

            def _authorized(self) -> bool:
                auth = str(self.headers.get("Authorization") or "")
                return auth.startswith("Bearer ") and secrets.compare_digest(auth[7:].strip(), token)

            def _reply(self, status: int, payload: dict[str, object]) -> None:
                data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
                self.send_response(int(status))
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _body(self) -> dict[str, object]:
                try:
                    length = min(65_536, max(0, int(self.headers.get("Content-Length") or 0)))
                    payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                    return dict(payload) if isinstance(payload, dict) else {}
                except Exception:
                    return {}

            def do_GET(self) -> None:  # noqa: N802
                if not self._authorized():
                    self._reply(401, {"ok": False, "error": "Unauthorized"})
                    return
                if self.path != "/status":
                    self._reply(404, {"ok": False, "error": "Not found"})
                    return
                controller = owner.controller
                if controller is None:
                    self._reply(503, {"ok": False, "error": "Desktop controller is not ready."})
                    return
                self._reply(200, {"ok": True, "desktop": controller.status_payload()})

            def do_POST(self) -> None:  # noqa: N802
                if not self._authorized():
                    self._reply(401, {"ok": False, "error": "Unauthorized"})
                    return
                body = self._body()
                try:
                    controller = owner.controller
                    if controller is None:
                        self._reply(503, {"ok": False, "error": "Desktop controller is not ready."})
                        return
                    if self.path == "/preferences":
                        result = controller.update_preferences(body)
                    elif self.path == "/action":
                        result = controller.perform_action(str(body.get("action") or ""))
                    else:
                        self._reply(404, {"ok": False, "error": "Not found"})
                        return
                    self._reply(200, {"ok": True, "desktop": result})
                except Exception as exc:
                    self._reply(400, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, name="thoth-desktop-control", daemon=True)
        self._thread.start()

    def stop(self, *, timeout: float = 3.0) -> None:
        server = self._server
        if server is not None:
            server.shutdown()
            server.server_close()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.0, float(timeout)))


class RemoteDesktopBridge:
    def __init__(self, base_url: str, token: str, *, timeout_s: float = 3.0) -> None:
        self.base_url = str(base_url).rstrip("/")
        self.token = str(token)
        self.timeout_s = max(0.25, float(timeout_s))

    def _request(self, method: str, path: str, payload: dict[str, object] | None = None) -> dict[str, object]:
        response = requests.request(
            method,
            f"{self.base_url}{path}",
            headers={"Authorization": f"Bearer {self.token}"},
            json=payload,
            timeout=self.timeout_s,
        )
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict) or body.get("ok") is not True:
            raise RuntimeError(str((body or {}).get("error") or "Desktop control request failed."))
        result = body.get("desktop")
        return dict(result) if isinstance(result, dict) else {}

    def status_payload(self) -> dict[str, object]:
        return self._request("GET", "/status")

    def update_preferences(self, payload: dict[str, object]) -> dict[str, object]:
        return self._request("POST", "/preferences", dict(payload))

    def perform_action(self, action: str) -> dict[str, object]:
        return self._request("POST", "/action", {"action": str(action)})
