from __future__ import annotations

import json
import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Iterable, Sequence

import requests

from thoth_terminal.config import settings
from thoth_terminal.web.mcp_tool_registry import (
    capability_metadata,
    is_parallel_safe_tool,
    is_ptc_eligible_tool,
)


MCP_CURRENT_PROTOCOL_VERSION = "2025-06-18"
MCP_LEGACY_PROTOCOL_VERSION = "2024-11-05"
MCP_SUPPORTED_PROTOCOL_VERSIONS = frozenset({MCP_CURRENT_PROTOCOL_VERSION, MCP_LEGACY_PROTOCOL_VERSION})
# Native Responses tool search is intentionally disabled until the provider
# event family is implemented end-to-end.  The stable Thoth gateway tools are
# the supported cold-loading path today.
OPENAI_NATIVE_TOOL_SEARCH_SUPPORTED = False


@dataclass
class McpRpcError(Exception):
    code: int
    message: str
    data: object | None = None

    def __str__(self) -> str:  # pragma: no cover
        if self.data is None:
            return f"MCP RPC error {self.code}: {self.message}"
        return f"MCP RPC error {self.code}: {self.message} ({self.data})"


@dataclass
class McpTransportError(Exception):
    message: str
    status_code: int | None = None

    def __str__(self) -> str:  # pragma: no cover
        return self.message


def _coerce_json(value: object) -> object:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


def _bounded_json(value: object, *, max_chars: int = 12000) -> object:
    """Keep tool output useful to an agent without retaining unbounded payloads."""

    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    except Exception:
        return str(value)[:max_chars]
    if len(encoded) <= max_chars:
        return value
    return {
        "truncated": True,
        "sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        "preview": encoded[: max(256, max_chars - 160)],
    }


def _normalize_citation(value: object, *, key_hint: str | None = None) -> object | None:
    """Normalize common MCP/Thoth source shapes without losing identifiers."""

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.startswith(("http://", "https://")):
            return {"url": text}
        if key_hint in {"source_id", "document_id", "citation_id", "id"}:
            return {str(key_hint): text}
        return text
    if isinstance(value, (int, float)) and key_hint in {"source_id", "document_id", "citation_id", "id"}:
        return {str(key_hint): value}
    if isinstance(value, dict):
        normalized: dict[str, object] = {}
        aliases = {
            "uri": "url",
            "source_url": "url",
            "canonical_url": "url",
            "sourceId": "source_id",
            "documentId": "document_id",
            "citationId": "citation_id",
        }
        for raw_key, raw_value in value.items():
            key = aliases.get(str(raw_key), str(raw_key))
            if key not in {"url", "source_id", "document_id", "citation_id", "id", "title", "source"}:
                continue
            if isinstance(raw_value, (str, int, float)) and str(raw_value).strip():
                normalized[key] = raw_value
        if normalized:
            return normalized
    return None


def _extract_citations(value: object, *, limit: int = 32) -> list[object]:
    found: list[object] = []
    seen: set[str] = set()

    def add(item: object, *, key_hint: str | None = None) -> None:
        if len(found) >= limit:
            return
        normalized = _normalize_citation(item, key_hint=key_hint)
        if normalized is None:
            return
        try:
            key = json.dumps(normalized, ensure_ascii=False, sort_keys=True, default=str)
        except Exception:
            key = str(normalized)
        if key in seen:
            return
        seen.add(key)
        found.append(_bounded_json(normalized, max_chars=2000))

    def walk(node: object, *, depth: int = 0) -> None:
        if len(found) >= limit:
            return
        if depth > 8:
            return
        if isinstance(node, str):
            parsed = _coerce_json(node)
            if parsed is not node:
                walk(parsed, depth=depth + 1)
            return
        if isinstance(node, dict):
            for key in (
                "citations",
                "sources",
                "source_refs",
                "annotations",
                "links",
                "source_urls",
                "source_ids",
                "urls",
                "uris",
            ):
                raw = node.get(key)
                key_hint = "source_id" if key == "source_ids" else None
                if isinstance(raw, list):
                    for item in raw:
                        add(item, key_hint=key_hint)
                        walk(item, depth=depth + 1)
                elif isinstance(raw, dict):
                    add(raw, key_hint=key_hint)
                    walk(raw, depth=depth + 1)
            if any(
                key in node
                for key in (
                    "url",
                    "uri",
                    "canonical_url",
                    "source_url",
                    "source_id",
                    "document_id",
                    "sourceId",
                    "documentId",
                )
            ):
                add(node)
            for key, child in node.items():
                if key in {
                    "citations",
                    "sources",
                    "source_refs",
                    "annotations",
                    "links",
                    "source_urls",
                    "source_ids",
                    "urls",
                    "uris",
                }:
                    continue
                if key in {
                    "url",
                    "uri",
                    "canonical_url",
                    "source_url",
                    "source_id",
                    "document_id",
                    "sourceId",
                    "documentId",
                }:
                    continue
                walk(child, depth=depth + 1)
        elif isinstance(node, list):
            for child in node[:limit]:
                walk(child, depth=depth + 1)

    walk(value)
    return found


class ThothMcpClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        auth_token: str | None = None,
        timeout_s: float = 30.0,
        session: requests.Session | None = None,
    ) -> None:
        root = (base_url or f"http://{settings.host}:{settings.port}").rstrip("/")
        self._endpoint = f"{root}/mcp"
        self._auth_token = auth_token if auth_token is not None else settings.mcp_auth_token
        self._timeout_s = float(timeout_s)
        self._session = session
        self._session_lock = threading.Lock()
        self._id_lock = threading.Lock()
        self._protocol_lock = threading.Lock()
        self._handshake_lock = threading.Lock()
        self._next_id = 1
        self._protocol_version: str | None = None
        self._session_id: str | None = None
        self._initialized = False
        self._initialize_result: dict[str, object] | None = None

    @property
    def endpoint(self) -> str:
        return self._endpoint

    def _headers(
        self,
        *,
        tool_profile: str | None = None,
        initializing: bool = False,
    ) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"
        if tool_profile:
            headers["x-thoth-tool-profile"] = str(tool_profile).strip()
        if not initializing:
            with self._protocol_lock:
                protocol_version = self._protocol_version
                session_id = self._session_id
            if protocol_version:
                headers["MCP-Protocol-Version"] = protocol_version
            if session_id:
                headers["Mcp-Session-Id"] = session_id
        return headers

    def _post(
        self,
        payload: dict[str, object],
        *,
        retry_safe: bool = False,
        tool_profile: str | None = None,
        initializing: bool = False,
    ) -> tuple[dict[str, object], dict[str, str]]:
        own_session = self._session is None
        current_session = self._session or requests.Session()
        try:
            if self._session is not None:
                with self._session_lock:
                    resp = current_session.post(
                        self._endpoint,
                        headers=self._headers(tool_profile=tool_profile, initializing=initializing),
                        json=payload,
                        timeout=self._timeout_s,
                    )
            else:
                resp = current_session.post(
                    self._endpoint,
                    headers=self._headers(tool_profile=tool_profile, initializing=initializing),
                    json=payload,
                    timeout=self._timeout_s,
                )
        except requests.RequestException as exc:
            raise McpTransportError(f"Failed to reach MCP endpoint {self._endpoint}: {exc}") from exc
        finally:
            if own_session:
                try:
                    current_session.close()
                except Exception:
                    pass

        if resp.status_code == 401:
            raise McpTransportError(
                "Unauthorized (401). Set THOTH_MCP_AUTH_TOKEN and pass Authorization: Bearer <token>.",
                status_code=401,
            )
        if resp.status_code >= 400:
            raise McpTransportError(
                f"MCP HTTP error {resp.status_code} from {self._endpoint}: {resp.text[:500]}",
                status_code=int(resp.status_code),
            )

        response_headers = {
            str(key).lower(): str(value)
            for key, value in dict(getattr(resp, "headers", {}) or {}).items()
        }
        if int(resp.status_code) == 202 and not str(getattr(resp, "text", "") or "").strip():
            return {}, response_headers
        try:
            content_type = response_headers.get("content-type", "").lower()
            if "text/event-stream" in content_type:
                event_payloads: list[object] = []
                for line in str(getattr(resp, "text", "") or "").splitlines():
                    if line.startswith("data:"):
                        try:
                            event_payloads.append(json.loads(line[5:].strip()))
                        except Exception:
                            continue
                data = event_payloads[-1] if event_payloads else None
            else:
                data = resp.json()
        except Exception as exc:
            raise McpTransportError(f"Invalid JSON response from MCP endpoint: {exc}") from exc

        if not isinstance(data, dict):
            raise McpTransportError("Invalid MCP response: expected a JSON object.")
        return data, response_headers

    def _send_rpc(
        self,
        method: str,
        params: object | None = None,
        *,
        retry_safe: bool = False,
        tool_profile: str | None = None,
        initializing: bool = False,
    ) -> tuple[dict[str, object], dict[str, str], int]:
        with self._id_lock:
            rpc_id = self._next_id
            self._next_id += 1
        msg: dict[str, object] = {"jsonrpc": "2.0", "id": rpc_id, "method": method}
        if params is not None:
            msg["params"] = params
        attempts = 2 if retry_safe else 1
        response: tuple[dict[str, object], dict[str, str]] | None = None
        for attempt in range(attempts):
            try:
                response = self._post(
                    msg,
                    retry_safe=retry_safe,
                    tool_profile=tool_profile,
                    initializing=initializing,
                )
                break
            except McpTransportError as exc:
                if (
                    attempt + 1 >= attempts
                    or exc.status_code not in {408, 425, 429, 500, 502, 503, 504}
                ):
                    raise
                time.sleep(0.05 * (attempt + 1))
        if response is None:
            raise McpTransportError("MCP request did not return a response.")
        envelope, headers = response
        return envelope, headers, rpc_id

    def _rpc(
        self,
        method: str,
        params: object | None = None,
        *,
        retry_safe: bool = False,
        tool_profile: str | None = None,
    ) -> dict[str, object]:
        if method not in {"initialize", "notifications/initialized"} and not self._initialized:
            self.initialize()
        resp, _headers, rpc_id = self._send_rpc(
            method,
            params,
            retry_safe=retry_safe,
            tool_profile=tool_profile,
        )

        if resp.get("jsonrpc") != "2.0" or resp.get("id") != rpc_id:
            raise McpTransportError("Invalid MCP response envelope.")

        if "error" in resp:
            err = resp.get("error")
            if isinstance(err, dict):
                code = int(err.get("code") or -32000)
                message = str(err.get("message") or "Unknown error")
                data = err.get("data")
                raise McpRpcError(code=code, message=message, data=data)
            raise McpTransportError("Invalid MCP error object.")

        result = resp.get("result")
        if not isinstance(result, dict):
            raise McpTransportError("Invalid MCP result object.")
        return result

    def ping(self) -> dict[str, object]:
        return self._rpc("ping", {})

    def _send_initialized_notification(self) -> None:
        msg: dict[str, object] = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        }
        envelope, _headers = self._post(msg, initializing=False)
        if envelope.get("error"):
            err = envelope.get("error")
            if isinstance(err, dict):
                raise McpRpcError(
                    code=int(err.get("code") or -32000),
                    message=str(err.get("message") or "Initialization notification failed"),
                    data=err.get("data"),
                )

    def initialize(self, protocol_version: str = MCP_CURRENT_PROTOCOL_VERSION) -> dict[str, object]:
        with self._handshake_lock:
            if self._initialized and self._initialize_result is not None:
                return dict(self._initialize_result)
            requested = str(protocol_version or MCP_CURRENT_PROTOCOL_VERSION).strip()
            response, response_headers, rpc_id = self._send_rpc(
                "initialize",
                {
                    "protocolVersion": requested,
                    "capabilities": {},
                    "clientInfo": {"name": "ThothMcpClient", "version": "1"},
                },
                initializing=True,
            )
            if response.get("jsonrpc") != "2.0" or response.get("id") != rpc_id:
                raise McpTransportError("Invalid MCP initialize response envelope.")
            if "error" in response:
                err = response.get("error")
                if isinstance(err, dict):
                    raise McpRpcError(
                        code=int(err.get("code") or -32000),
                        message=str(err.get("message") or "Initialization failed"),
                        data=err.get("data"),
                    )
                raise McpTransportError("Invalid MCP initialize error object.")
            result = response.get("result")
            if not isinstance(result, dict):
                raise McpTransportError("Invalid MCP initialize result object.")
            reported = str(result.get("protocolVersion") or "").strip()
            header_version = response_headers.get("mcp-protocol-version", "").strip()
            negotiated = (
                reported
                if reported in MCP_SUPPORTED_PROTOCOL_VERSIONS
                else header_version
                if header_version in MCP_SUPPORTED_PROTOCOL_VERSIONS
                else MCP_CURRENT_PROTOCOL_VERSION
            )
            with self._protocol_lock:
                self._protocol_version = negotiated
                session_id = response_headers.get("mcp-session-id", "").strip()
                self._session_id = session_id or None
                self._initialize_result = dict(result)
                self._initialized = True
            try:
                self._send_initialized_notification()
            except Exception:
                with self._protocol_lock:
                    self._initialized = False
                raise
            return dict(result)

    def list_tools(self, *, tool_profile: str | None = None) -> list[dict[str, object]]:
        result = self._rpc("tools/list", {}, tool_profile=tool_profile)
        tools = result.get("tools")
        if not isinstance(tools, list):
            raise McpTransportError("Invalid tools/list result.")
        return [t for t in tools if isinstance(t, dict)]

    def call_tool_envelope(
        self,
        name: str,
        arguments: dict[str, object] | None = None,
        *,
        retry_safe: bool = False,
        tool_profile: str | None = None,
    ) -> dict[str, object]:
        result = self._rpc(
            "tools/call",
            {"name": name, "arguments": arguments or {}},
            retry_safe=retry_safe,
            tool_profile=tool_profile,
        )
        return result

    @staticmethod
    def _decode_tool_result(result: dict[str, object]) -> object:
        structured = result.get("structuredContent")
        if structured is not None:
            return _coerce_json(structured)
        meta = result.get("_meta")
        if isinstance(meta, dict):
            thoth = meta.get("thoth")
            if isinstance(thoth, dict) and "result" in thoth:
                return thoth.get("result")
        content = result.get("content")
        if not isinstance(content, list) or not content:
            return result
        first = content[0]
        if not isinstance(first, dict):
            return result
        text = first.get("text")
        return _coerce_json(text)

    def call_tool(
        self,
        name: str,
        arguments: dict[str, object] | None = None,
        *,
        tool_profile: str | None = None,
    ) -> object:
        return self._decode_tool_result(
            self.call_tool_envelope(name, arguments, tool_profile=tool_profile)
        )

    def call_tool_with_metadata(
        self,
        name: str,
        arguments: dict[str, object] | None = None,
        *,
        retry_safe: bool = False,
        tool_profile: str | None = None,
    ) -> dict[str, object]:
        envelope = self.call_tool_envelope(
            name,
            arguments,
            retry_safe=retry_safe,
            tool_profile=tool_profile,
        )
        decoded = self._decode_tool_result(envelope)
        citations = _extract_citations(envelope)
        return {
            "result": _bounded_json(decoded),
            "citations": citations,
            "structuredContent": _bounded_json(envelope.get("structuredContent")),
            "_meta": _bounded_json(envelope.get("_meta")),
        }

    def call_tools(
        self,
        operations: Sequence[dict[str, object]],
        *,
        max_workers: int = 4,
        tool_profile: str | None = None,
    ) -> list[dict[str, object]]:
        """Execute a bounded list, parallelizing only explicit safe reads.

        Results retain input order.  Unsafe, unknown, mutating, or open-world
        operations are still supported as direct sequential calls for callers
        that intentionally mix a workflow, but they never share the parallel
        lane or the safe-read retry policy.
        """

        rows = list(operations or [])
        if not rows:
            return []
        if len(rows) > 32:
            raise ValueError("MCP call_tools accepts at most 32 operations")

        normalized: list[tuple[str, dict[str, object]]] = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("MCP operation must be an object")
            name = str(row.get("name") or "").strip()
            args = row.get("arguments") or {}
            if not name or not isinstance(args, dict):
                raise ValueError("MCP operation requires name and object arguments")
            normalized.append((name, dict(args)))

        can_parallelize = (
            len(normalized) > 1
            and max_workers > 1
            and all(is_parallel_safe_tool(name) for name, _args in normalized)
        )

        def run_one(item: tuple[str, dict[str, object]]) -> dict[str, object]:
            name, arguments = item
            safe = is_parallel_safe_tool(name)
            try:
                value = self.call_tool_with_metadata(
                    name,
                    arguments,
                    retry_safe=safe,
                    tool_profile=tool_profile,
                )
                return {"name": name, "arguments": arguments, "ok": True, **value}
            except Exception as exc:
                return {
                    "name": name,
                    "arguments": arguments,
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "citations": [],
                }

        if can_parallelize:
            with ThreadPoolExecutor(max_workers=max(1, min(int(max_workers), 4))) as executor:
                return list(executor.map(run_one, normalized))
        return [run_one(item) for item in normalized]

    def openai_tool_definitions(
        self,
        *,
        tool_profile: str = "analyst_v1",
        selected_names: Iterable[str] | None = None,
        max_tools: int = 24,
        allow_programmatic: bool = False,
        enable_native_tool_search: bool = False,
    ) -> list[dict[str, object]]:
        """Convert a curated MCP profile to Responses function tools.

        The MCP server's cache-stable ``tools/list`` contract is not modified;
        this is an OpenAI request adapter.  Cold reads remain reachable through
        the existing ``tool_catalog``/``get_tool_schema``/``invoke_read_tool``
        gateways instead of exposing the full catalog. Native Responses tool
        search is an explicit capability gate: this adapter does not yet
        parse the provider's tool-search call/output event family, so it never
        emits ``defer_loading`` or a ``tool_search`` item by default.
        """

        if enable_native_tool_search and not OPENAI_NATIVE_TOOL_SEARCH_SUPPORTED:
            raise McpTransportError(
                "Native OpenAI tool search is unavailable; use the stable Thoth gateway tools."
            )

        selected = {str(name).strip() for name in (selected_names or []) if str(name).strip()}
        definitions = self.list_tools(tool_profile=tool_profile)
        out: list[dict[str, object]] = []
        for raw in definitions:
            name = str(raw.get("name") or "").strip()
            if not name or (selected and name not in selected):
                continue
            capability = capability_metadata(name)
            if not capability.get("known"):
                continue
            parameters = raw.get("inputSchema")
            if not isinstance(parameters, dict):
                parameters = {"type": "object", "properties": {}, "additionalProperties": False}
            tool: dict[str, object] = {
                "type": "function",
                "name": name,
                "description": str(raw.get("description") or raw.get("title") or name)[:1200],
                "parameters": dict(parameters),
            }
            if parameters.get("type") == "object" and parameters.get("additionalProperties") is False:
                tool["strict"] = True
            if allow_programmatic and is_ptc_eligible_tool(name):
                tool["allowed_callers"] = ["direct", "programmatic"]
                tool["output_schema"] = {
                    "type": "object",
                    "properties": {
                        "result": {},
                        "citations": {"type": "array", "items": {}},
                        "error": {"type": "string"},
                    },
                    "required": ["result", "citations"],
                    "additionalProperties": False,
                }
            out.append(tool)
            if len(out) >= max(1, min(int(max_tools or 24), 64)):
                break
        return out

    def list_resources(self) -> list[dict[str, object]]:
        result = self._rpc("resources/list", {})
        resources = result.get("resources")
        if not isinstance(resources, list):
            raise McpTransportError("Invalid resources/list result.")
        return [r for r in resources if isinstance(r, dict)]

    def read_resource(self, uri: str) -> object:
        result = self._rpc("resources/read", {"uri": uri})
        contents = result.get("contents")
        if not isinstance(contents, list) or not contents:
            return result
        first = contents[0]
        if not isinstance(first, dict):
            return result
        text = first.get("text")
        return _coerce_json(text)
