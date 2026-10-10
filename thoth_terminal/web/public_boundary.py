"""Serving-plane privacy policy, applied before HTTP and WebSocket routing."""
from __future__ import annotations

import secrets
from urllib.parse import urlparse

from starlette.datastructures import MutableHeaders
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from thoth_terminal.security import build_security_posture, is_loopback_host, public_safe_path


def _matches(provided: str, expected: object) -> bool:
    token = str(expected or "").strip()
    return bool(token and provided and secrets.compare_digest(provided.encode("utf-8"), token.encode("utf-8")))


def owner_authenticated(request: HTTPConnection, settings) -> bool:
    auth = str(request.headers.get("authorization") or "").strip()
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    return _matches(token or str(request.headers.get("x-thoth-auth") or "").strip(), settings.app_auth_token)


def private_transport_authenticated(request: HTTPConnection, settings) -> bool:
    if owner_authenticated(request, settings):
        return True
    path = request.url.path
    if path == "/mcp" and str(getattr(settings, "mcp_auth_token", "") or "").strip():
        # Use the MCP owner's credential scopes rather than a second divergent
        # list of owner, Harness, and invited-agent tokens.
        from thoth_terminal.web.mcp_routes import _load_mcp_engine

        return bool(_load_mcp_engine()._mcp_require_auth(request))
    for prefix, setting in (
        ("/mcp/app/read-only/", "chatgpt_app_read_only_token"),
        ("/mcp/app/", "chatgpt_app_token"),
    ):
        if path.startswith(prefix) and "/" not in path[len(prefix):]:
            return _matches(path[len(prefix):], getattr(settings, setting, None))
    if path == "/api/agents/harness/evidence/pdf":
        auth = str(request.headers.get("authorization") or "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        return _matches(token, getattr(settings, "muse_owner_agent_token", None))
    return False


def _trusted_local(request: HTTPConnection) -> bool:
    # A tunnel's loopback peer is not the operator. Both the peer and authority
    # must be loopback, with no indication that a proxy forwarded the request.
    forwarded_headers = ("forwarded", "x-forwarded-for", "x-forwarded-host", "cf-connecting-ip")
    return bool(
        request.client
        and is_loopback_host(request.client.host)
        and is_loopback_host(request.url.hostname or "")
        and not any(name in request.headers for name in forwarded_headers)
    )


class PublicSurfaceBoundary:
    def __init__(self, app: ASGIApp, *, settings):
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        request = HTTPConnection(scope)
        posture = build_security_posture(self.settings)
        public_host = (urlparse(str(getattr(self.settings, "public_base_url", "") or "")).hostname or "").lower().rstrip(".")
        restricted_host = bool(
            getattr(self.settings, "public_base_url_restrict_to_public_routes", True)
            and public_host
            and (request.url.hostname or "").lower().rstrip(".") == public_host
        )
        local = _trusted_local(request) and not restricted_host
        public_surface = posture.public or not local
        scope.setdefault("state", {})["public_surface"] = public_surface
        method = str(scope.get("method") or "GET")
        safe = scope["type"] == "http" and public_safe_path(scope.get("path", "/"), method)
        authorized = False
        if not posture.public and not local:
            authorized = private_transport_authenticated(request, self.settings)
        public_open = bool(posture.public_build and getattr(self.settings, "public_allow_unauthenticated", False))
        denied = bool(
            (posture.public and (not safe or (self.settings.require_auth and not public_open and not owner_authenticated(request, self.settings)
                                            and not str(scope.get("path") or "").startswith("/public/api/agents/"))))
            or (not posture.public and not local and not safe and not authorized)
        )
        if denied:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                status = 401 if posture.public and safe else 404
                await JSONResponse({"error": "Unauthorized" if status == 401 else "Not found"}, status_code=status,
                                   headers={"Cache-Control": "no-store"})(scope, receive, send)
            return

        async def privacy_send(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                path = str(scope.get("path") or "")
                # Authenticated/private pages and credentialed public APIs must
                # never be retained by browser or shared proxy caches.
                if not safe or path.startswith("/public/api/agents/") or path.startswith("/public/newsletter/") or headers.get("location"):
                    headers["Cache-Control"] = "no-store"
                elif path == "/static/public-site.css" or "text/html" in headers.get("content-type", ""):
                    headers["Cache-Control"] = "no-cache"
                headers.setdefault("X-Content-Type-Options", "nosniff")
                if public_surface:
                    for name in ("X-Thoth-Boot-Id", "X-Thoth-Started-At", "X-Thoth-Git-Sha"):
                        if name in headers:
                            del headers[name]
                    headers["Referrer-Policy"] = "no-referrer"
            await send(message)

        await self.app(scope, receive, privacy_send)
