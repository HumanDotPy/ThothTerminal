from __future__ import annotations

import base64
import asyncio
from concurrent.futures import ThreadPoolExecutor
import difflib
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode, urlparse
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse
from thoth_terminal.web.json_response import JSONResponse
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session, defer, joinedload, load_only, selectinload

from thoth_terminal.config import bind_host, configured_allowed_origins, settings
from thoth_terminal.db import database_backend, session_scope, track_database_queries, using_postgres, utcnow
from thoth_terminal.gap_hunter_service import universe_coverage_index
from thoth_terminal.runtime_info import get_runtime_identity, list_runtime_states, runtime_fingerprint
from thoth_terminal.web.mcp_tool_registry import (
    BATCH_SAFE_TOOL_NAMES,
    DESTRUCTIVE_TOOL_NAMES,
    OPEN_WORLD_TOOL_NAMES,
    READ_ONLY_TOOL_NAMES,
    capability_for_tool,
    is_batch_safe_tool,
    is_parallel_safe_tool,
    is_read_only_tool,
)

BASE_DIR = Path(__file__).resolve().parent
logger = logging.getLogger("thoth.web.mcp")
router = APIRouter()
_CONTEXT_PROVIDER: Callable[[], Mapping[str, object]] | None = None


def _refresh_context() -> None:
    if _CONTEXT_PROVIDER is None:
        return
    context = dict(_CONTEXT_PROVIDER())
    for key in list(context):
        if key.startswith("__") and key.endswith("__"):
            context.pop(key, None)
    for key in ("router", "_CONTEXT_PROVIDER", "_refresh_context", "register_mcp_routes"):
        context.pop(key, None)
    globals().update(context)
    finalize = globals().get("finalize_mcp_tool_modules")
    if callable(finalize):
        finalize(globals())


def register_mcp_routes(app_obj: Any, *, context_provider: Callable[[], Mapping[str, object]] | None = None) -> None:
    global _CONTEXT_PROVIDER
    _CONTEXT_PROVIDER = context_provider
    app_obj.include_router(router)


def _mcp_auth_scope(request: Request) -> str | None:
    token = getattr(settings, "mcp_auth_token", None)
    if not token:
        return "operator"
    auth = request.headers.get("authorization") or request.headers.get("Authorization") or ""
    if not auth.lower().startswith("bearer "):
        return None
    provided = auth.split(" ", 1)[1].strip()
    if secrets.compare_digest(provided, str(token)):
        return "operator"
    muse_token = getattr(settings, "muse_owner_agent_token", None)
    if muse_token and secrets.compare_digest(provided, str(muse_token)):
        return "operator_agent_v1"
    scoped = hmac.new(str(token).encode(), b"thoth:dsh:worldview-v1", hashlib.sha256).hexdigest()
    if secrets.compare_digest(provided, scoped):
        return "worldview_v1"
    interactive = hmac.new(str(token).encode(), b"thoth:dsh:operator-agent-v1", hashlib.sha256).hexdigest()
    return "operator_agent_v1" if secrets.compare_digest(provided, interactive) else None


def _mcp_require_auth(request: Request) -> bool:
    return _mcp_auth_scope(request) is not None


MCP_CURRENT_PROTOCOL_VERSION = "2025-06-18"
MCP_LEGACY_PROTOCOL_VERSION = "2024-11-05"
MCP_SUPPORTED_PROTOCOL_VERSIONS = frozenset({MCP_CURRENT_PROTOCOL_VERSION, MCP_LEGACY_PROTOCOL_VERSION})
_MCP_TRUSTED_CHATGPT_ORIGINS = frozenset({
    "https://chat.openai.com",
    "https://chatgpt.com",
    "https://www.chatgpt.com",
})


def _mcp_normalize_origin(value: object) -> str | None:
    raw = str(value or "").strip()
    if not raw or raw.lower() == "null":
        return None
    try:
        parsed = urlparse(raw)
        host = parsed.hostname
        if parsed.scheme.lower() not in {"http", "https"} or not host:
            return None
        if parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
            return None
        host = host.lower().rstrip(".")
        port = parsed.port
    except (TypeError, ValueError):
        return None
    default_port = 80 if parsed.scheme.lower() == "http" else 443
    suffix = "" if port in {None, default_port} else f":{port}"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{parsed.scheme.lower()}://{host}{suffix}"


def _mcp_origin_allowed(request: Request) -> bool:
    """Validate Origin without trusting an arbitrary Host header.

    No-Origin clients (including non-browser MCP clients) remain supported.
    Browser origins are limited to explicit configuration, the configured
    public base URL, local loopback, or the known ChatGPT app origins.
    """

    raw_origin = request.headers.get("origin")
    if raw_origin is None or not str(raw_origin).strip():
        return True
    origin = _mcp_normalize_origin(raw_origin)
    if origin is None:
        return False
    allowed = {
        normalized
        for normalized in (_mcp_normalize_origin(value) for value in configured_allowed_origins())
        if normalized
    }
    public_base = _mcp_public_base_url()
    normalized_public = _mcp_normalize_origin(public_base)
    if normalized_public:
        allowed.add(normalized_public)
    allowed.update(_MCP_TRUSTED_CHATGPT_ORIGINS)

    # Loopback is safe for a local-bound server.  Do not treat 0.0.0.0 or an
    # arbitrary reverse-proxy Host value as an allowed origin.
    configured_host = bind_host().strip().lower().rstrip(".")
    local_hosts = {"localhost", "127.0.0.1", "::1"}
    if configured_host in local_hosts:
        local_hosts.add(configured_host)
    parsed_origin = urlparse(origin)
    if parsed_origin.hostname in local_hosts:
        port = parsed_origin.port
        configured_port = int(getattr(settings, "port", 0) or 0)
        if port in {None, 80, 443, configured_port}:
            allowed.add(origin)
    if configured_host not in {"", "0.0.0.0", "::", "::0"}:
        # An explicitly configured host is a safer same-origin allowance than
        # blindly echoing request.url, which may be attacker-controlled during
        # a DNS-rebinding attempt.
        if parsed_origin.hostname == configured_host:
            allowed.add(origin)
    return origin in allowed


def _mcp_accepts(request: Request, *media_types: str) -> bool:
    raw = request.headers.get("accept", "")
    accepted: set[str] = set()
    for item in raw.split(","):
        token, _, params = item.strip().lower().partition(";")
        if not token:
            continue
        quality_zero = False
        for part in params.split(";"):
            key, separator, value = part.strip().partition("=")
            if separator and key.strip() == "q":
                try:
                    quality_zero = float(value.strip()) <= 0.0
                except ValueError:
                    quality_zero = False
        if quality_zero:
            continue
        accepted.add(token)
    # The Streamable HTTP contract requires the concrete media types, not only
    # a wildcard.  This also prevents a browser's generic default Accept from
    # silently opting into a response format it cannot consume.
    return all(media in accepted for media in media_types)


def _mcp_negotiate_protocol(requested: object) -> str:
    value = str(requested or "").strip()
    if value == MCP_LEGACY_PROTOCOL_VERSION:
        return MCP_LEGACY_PROTOCOL_VERSION
    # 2025-03-26 is a compatible pre-Streamable-HTTP version.  The current
    # endpoint speaks the current transport while retaining its protocol
    # family for clients that request that version.
    return MCP_CURRENT_PROTOCOL_VERSION


def _mcp_response(
    request: Request,
    payload: object,
    *,
    status_code: int = 200,
    headers: Mapping[str, str] | None = None,
):
    response_headers = dict(headers or {})
    accept = request.headers.get("accept", "").lower()
    if "text/event-stream" in accept:
        data = json.dumps(payload)

        async def _gen():
            yield f"data: {data}\n\n"

        return StreamingResponse(
            _gen(),
            media_type="text/event-stream",
            status_code=status_code,
            headers=response_headers,
        )
    return JSONResponse(payload, status_code=status_code, headers=response_headers)


def _mcp_error(rpc_id: object, code: int, message: str) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}


def _mcp_result(rpc_id: object, result: object) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


_MCP_WIDGET_RESOURCE_URI = "ui://widget/thoth-terminal-app-v1.html"
_MCP_WIDGET_MIME_TYPE = "text/html;profile=mcp-app"
_MCP_WIDGET_FILE = BASE_DIR / "chatgpt_widget_v1.html"
_MCP_DESK_RESOURCE_URI = "ui://widget/thoth-research-desk-v1.html"
_MCP_DESTRUCTIVE_TOOLS = DESTRUCTIVE_TOOL_NAMES
_MCP_OPEN_WORLD_TOOLS = set(OPEN_WORLD_TOOL_NAMES) | {"discord_outbox_create"}
_MCP_READ_ONLY_TOOL_NAMES = READ_ONLY_TOOL_NAMES
_MCP_TOOL_GATEWAY_NAMES = frozenset({"tool_catalog", "get_tool_schema", "invoke_read_tool"})
_MCP_TOOL_PROFILE_ALIASES = {
    "analyst": "analyst_v1",
    "heartbeat": "heartbeat_v1",
    "documents": "documents_v1",
    "reports": "reports_v1",
    "full_v1": "full",
}
_MCP_TOOL_PROFILE_HOT_NAMES: dict[str, frozenset[str]] = {
    "heartbeat_v1": frozenset(
        {
            "session_info",
            "batch_read",
            "resolve_object",
            "object_search",
            "search_documents",
            "query_document",
            "expand_document_match",
            "get_document",
            "get_research_report",
            "read_report_content",
            "get_subject_source_pack",
            "get_recent_context_bundle",
            *_MCP_TOOL_GATEWAY_NAMES,
        }
    ),
    "documents_v1": frozenset(
        {
            "session_info",
            "batch_read",
            "object_search",
            "search_documents",
            "get_document",
            "query_document",
            "expand_document_match",
            "read_document",
            "inspect_document",
            "render_document_page",
            "ingest_research_library",
            "list_research_library",
            "get_research_library_report",
            *_MCP_TOOL_GATEWAY_NAMES,
        }
    ),
    "reports_v1": frozenset(
        {
            "session_info",
            "batch_read",
            "resolve_object",
            "object_search",
            "search_documents",
            "query_document",
            "expand_document_match",
            "get_research_report",
            "read_report_content",
            "get_subject_source_pack",
            "append_report_addendum",
            "build_research_report",
            "build_bottleneck_report",
            "create_research_report_derivative",
            *_MCP_TOOL_GATEWAY_NAMES,
        }
    ),
    "analyst_v1": frozenset(
        {
            "thoth_help",
            "session_info",
            "batch_read",
            "resolve_object",
            "object_search",
            "search",
            "fetch",
            "search_documents",
            "get_document",
            "query_document",
            "expand_document_match",
            "read_document",
            "get_company",
            "get_research_report",
            "read_report_content",
            "get_research_subject",
            "get_subject_source_pack",
            "get_agent_canon_card",
            "get_knowledge_asset",
            "get_dataset",
            "query_dataset",
            "get_thesis",
            "get_note",
            "get_instrument_request",
            "get_supply_chain",
            "get_bottleneck_model",
            "get_recent_context_bundle",
            "create_note",
            "update_note",
            "create_thesis",
            "update_thesis",
            "add_thesis_evidence",
            "create_instrument_request",
            "append_report_addendum",
            "upsert_research_source_card",
            "ingest_research_library",
            *_MCP_TOOL_GATEWAY_NAMES,
        }
    ),
    # Versioned schema profile.  Keep the original analyst_v1 definition list
    # and hash untouched; this profile opts into the small, tested output
    # schemas below without exposing the full catalog.
    "analyst_v2": frozenset(
        {
            "thoth_help",
            "session_info",
            "batch_read",
            "resolve_object",
            "object_search",
            "search",
            "fetch",
            "search_documents",
            "get_document",
            "get_research_report",
            "read_report_content",
            "get_research_subject",
            "get_subject_source_pack",
            "get_agent_canon_card",
            "get_knowledge_asset",
            "get_dataset",
            "query_dataset",
            "get_thesis",
            "get_note",
            "get_instrument_request",
            "get_supply_chain",
            "get_bottleneck_model",
            "get_recent_context_bundle",
            *_MCP_TOOL_GATEWAY_NAMES,
        }
    ),
}
# Scheduled research uses a distinct write capability without changing the
# frozen analyst profile shared by interactive clients.
_WORLDVIEW_UNSCOPED_MUTATIONS = frozenset({
    "create_note", "update_note", "create_thesis", "update_thesis",
    "add_thesis_evidence", "create_instrument_request", "append_report_addendum",
    "upsert_research_source_card", "ingest_research_library",
})
_MCP_TOOL_PROFILE_HOT_NAMES["worldview_v1"] = frozenset(
    (_MCP_TOOL_PROFILE_HOT_NAMES["analyst_v1"] - _WORLDVIEW_UNSCOPED_MUTATIONS)
    | {"list_document_arrival_cards", "apply_worldview_write", "apply_worldview_scenario", "apply_worldview_journal", "apply_worldview_consensus_evidence",
       "get_worldview_memory", "apply_worldview_memory",
       "get_research_state_budget_policy", "list_state_compaction_candidates",
       "get_state_compaction_source", "compact_research_state",
       "get_operator_research_inbox", "get_operator_research_message", "send_worldview_operator_message",
       "list_research_extensions", "create_research_extension_candidate",
       "validate_research_extension_candidate", "activate_research_extension_candidate",
       "rollback_research_extension",
       "execute_research_extension", "get_research_extension_executions",
       "apply_worldview_product_model", "apply_worldview_product_configuration",
       "apply_worldview_agentx_configuration",
       "apply_worldview_roadmap_update", "apply_worldview_roadmap_review", "get_roadmap_review_queue", "resolve_worldview_research_request",
       "get_consensus_snapshot", "list_external_agent_intake",
       "list_external_agent_proposals", "get_external_agent_proposal"}
)
_MCP_TOOL_PROFILE_HOT_NAMES["operator_agent_v1"] = frozenset(
    (_MCP_TOOL_PROFILE_HOT_NAMES["worldview_v1"]
     - {"apply_worldview_write", "apply_worldview_scenario", "apply_worldview_journal", "apply_worldview_consensus_evidence",
        "apply_worldview_memory", "compact_research_state",
        "send_worldview_operator_message",
        "list_research_extensions", "create_research_extension_candidate",
        "validate_research_extension_candidate", "activate_research_extension_candidate",
        "rollback_research_extension",
        "execute_research_extension", "get_research_extension_executions",
        "apply_worldview_product_model", "apply_worldview_product_configuration",
        "apply_worldview_agentx_configuration",
        "apply_worldview_roadmap_update", "apply_worldview_roadmap_review", "resolve_worldview_research_request"})
    | {"apply_interactive_research_write", "apply_interactive_consensus_evidence",
       "render_document_page", "get_worldview_journal", "list_worldview_journals",
       "publish_external_model_projection", "list_external_agent_proposals",
       "get_external_agent_proposal", "resolve_external_agent_proposal",
       "list_external_agent_intake", "resolve_external_agent_intake",
       "submit_operator_research_request",
       "submit_operator_feedback",
       "get_research_desk", "get_research_change",
       "get_artcraft_workbench", "artcraft_status", "artcraft_catalog", "artcraft_workflow",
       "withdraw_external_model_projection"}
)
_MCP_TOOL_PROFILE_HOT_NAMES["research_desk_v1"] = frozenset(
    _MCP_TOOL_PROFILE_HOT_NAMES["analyst_v1"] | {"get_research_desk", "get_research_change",
    "get_artcraft_workbench", "artcraft_status", "artcraft_catalog", "artcraft_workflow"}
)
_MCP_TOOL_PROFILE_HOT_NAMES["creative_v1"] = frozenset({
    "session_info", "thoth_help", "object_search", "resolve_object", "search", "fetch",
    "get_artcraft_workbench", "artcraft_status", "artcraft_catalog", "artcraft_workflow",
})

MCP_OUTPUT_SCHEMA_PROFILE_VERSION = "mcp_output_schemas_v1"
_MCP_OUTPUT_SCHEMAS: dict[str, dict[str, object]] = {
    "fetch": {
        "type": "object",
        "properties": {
            "id": {},
            "title": {},
            "url": {},
            "metadata": {"type": ["object", "null"]},
            "text_excerpt": {"type": ["string", "null"]},
            "text_scope": {},
            "text_length_chars": {"type": ["integer", "null"]},
            "returned_text_length_chars": {"type": ["integer", "null"]},
            "truncated": {"type": ["boolean", "null"]},
        },
        "additionalProperties": False,
    },
    "get_document": {
        "type": "object",
        "properties": {
            "id": {},
            "title": {},
            "translation_title": {},
            "url": {},
            "source": {},
            "author": {},
            "triage_state": {},
            "saved": {"type": ["boolean", "null"]},
            "published_at": {},
            "domains": {"type": ["array", "null"]},
            "tags": {"type": ["array", "null"]},
            "text": {"type": ["string", "null"]},
            "text_scope": {},
            "text_length_chars": {"type": ["integer", "null"]},
            "returned_text_length_chars": {"type": ["integer", "null"]},
            "truncated": {"type": ["boolean", "null"]},
            "has_images": {"type": "boolean"},
        },
        "additionalProperties": False,
    },
}
_MCP_TOOL_PROFILE_FROZEN_SHA256 = {
    "analyst_v1:operator": "de6baad57a1e4a75d1f760562d72560061a16300cae85e448ae1300e8d4f0a80",
    "analyst_v1:chatgpt_write": "1efed7c13f928814451e0fdeb67c3ef11ebbd2157b1d9fc4dd7e01fcf130bcd3",
    "analyst_v1:chatgpt_read_only": "15ed77ba1d663be4594041acaef6b50d2f6cdc05b20f4b8d916bff815173ceed",
    "heartbeat_v1:operator": "fd1dda7a29f168fceca578f5280b9fa721777118c2bbd679e7ded1d92bbd2e10",
    "heartbeat_v1:chatgpt_write": "8b91e536b07bd274e3a1b88502a8d9717951bceab64e5f46aad7b1aa4c7af019",
    "heartbeat_v1:chatgpt_read_only": "4b33322be990f40eeb0220948794cd40221dab007d714873b7484aea303656fa",
    "documents_v1:operator": "f488623b195b728b9710da58a0c4a4fe18fa17ad27b6bad9f67c19ca0be4a605",
    "documents_v1:chatgpt_write": "3c302c9c8a86e4f8ecf3f1e432f8e2f890dc2b2c98df12c01737e6c3e2f29023",
    "documents_v1:chatgpt_read_only": "1556f1c95fac5f94a64c33a640ad691861e28571cc45b810ab30862af50a998b",
    "reports_v1:operator": "37de1fc0d008d7dceed90d988238401e047b8b7aa949dd5aa3e9596fdb9d2385",
    "reports_v1:chatgpt_write": "ff9f607fac6dd8e2e2aa1ad9c2ec74549f67816943d60538452231465c068614",
    "reports_v1:chatgpt_read_only": "ae538ca74987bf3e3a94eb5a959dadd988ccb6f913842f212be52beea46725fb",
}
_MCP_BATCH_READ_MAX_OPERATIONS = 12
_MCP_BATCH_READ_TOOL_NAMES = BATCH_SAFE_TOOL_NAMES
_MCP_HTTP_PARALLEL_MAX = 4
_MCP_HTTP_PARALLEL_BATCH_MAX = 32
_MCP_BATCH_READ_PARALLEL_MAX = 4
_MCP_CHATGPT_APP_BLOCKED_TOOL_NAMES = {
    "get_research_change",
    "submit_operator_feedback", "send_worldview_operator_message",
    "get_operator_research_inbox", "get_operator_research_message",
    "apply_worldview_roadmap_review",
    "get_worldview_memory",
    "apply_worldview_memory",
    "get_research_state_budget_policy", "list_state_compaction_candidates",
    "get_state_compaction_source", "compact_research_state",
    "list_research_extensions",
    "create_research_extension_candidate",
    "validate_research_extension_candidate",
    "activate_research_extension_candidate",
    "rollback_research_extension",
    "execute_research_extension",
    "get_research_extension_executions",
    "runtime_info",
    "integration_google_status",
    "integration_google_test",
}
_MCP_TOOL_ACTION_PREFIXES = {
    "add",
    "agent",
    "append",
    "apply",
    "bind",
    "build",
    "bulk",
    "compare",
    "create",
    "delete",
    "discord",
    "export",
    "fetch",
    "find",
    "generate",
    "get",
    "help",
    "import",
    "link",
    "list",
    "mark",
    "preview",
    "publish",
    "query",
    "read",
    "record",
    "register",
    "refresh",
    "resolve",
    "review",
    "run",
    "save",
    "search",
    "session",
    "set",
    "unlink",
    "update",
    "upsert",
}
_MCP_NOUN_IRREGULARS = {
    "analysis": "analyses",
    "bottleneck_model": "bottleneck_models",
    "company": "companies",
    "research_campaign": "research_campaigns",
    "research_canon_gap": "research_canon_gaps",
    "knowledge_asset": "knowledge_assets",
    "note": "notes",
    "research_report": "research_reports",
    "subject_scenario": "subject_scenarios",
    "source": "sources",
    "supply_chain": "supply_chains",
    "thesis": "theses",
}
_MCP_NOUN_SINGULARS = {plural: singular for singular, plural in _MCP_NOUN_IRREGULARS.items()}
_MCP_NOUN_SINGULARS.update(
    {
        "chains": "supply_chain",
        "reports": "research_report",
        "report": "research_report",
        "assets": "knowledge_asset",
        "bottlenecks": "bottleneck_model",
        "bottleneck": "bottleneck_model",
        "campaigns": "research_campaign",
        "campaign": "research_campaign",
        "scenarios": "subject_scenario",
        "scenario": "subject_scenario",
        "gaps": "research_canon_gap",
        "notes": "note",
        "sources": "source",
        "companies": "company",
        "thesis": "thesis",
        "theses": "thesis",
    }
)


def _mcp_is_read_only_tool(tool_name: str) -> bool:
    return is_read_only_tool(tool_name)


def _mcp_tool_title(tool_name: str, explicit: object | None = None) -> str:
    if explicit is not None and str(explicit).strip():
        return str(explicit).strip()
    parts = [part for part in str(tool_name or "").strip().split("_") if part]
    if not parts:
        return "Thoth Tool"
    return " ".join(part.upper() if len(part) <= 3 else part.capitalize() for part in parts)


def _mcp_tool_description(tool_name: str, description: object | None = None) -> str:
    desc = str(description or "").strip()
    return desc or f"{_mcp_tool_title(tool_name)}."


def _mcp_tool_annotations(tool_name: str) -> dict[str, object]:
    capability = capability_for_tool(tool_name)
    return {
        "readOnlyHint": capability.read_only,
        "openWorldHint": capability.open_world or str(tool_name or "") in _MCP_OPEN_WORLD_TOOLS,
        "destructiveHint": capability.destructive,
        "idempotentHint": capability.idempotent,
    }


def _mcp_tool_visibility(tool_name: str) -> list[str]:
    return ["model", "app"] if _mcp_is_read_only_tool(tool_name) else ["model"]


def _mcp_tool_status_text(tool_name: str) -> tuple[str, str]:
    title = _mcp_tool_title(tool_name)
    return (f"Running {title}"[:64], f"{title} ready"[:64])


def _mcp_slug_parts(value: object) -> list[str]:
    raw = str(value or "").strip().lower()
    if not raw:
        return []
    return [part for part in re.split(r"[^a-z0-9]+", raw) if part]


def _mcp_normalize_token(value: object) -> str:
    return "_".join(_mcp_slug_parts(value))


def _mcp_singular_noun(noun: str) -> str:
    clean = _mcp_normalize_token(noun)
    if not clean:
        return ""
    if clean in _MCP_NOUN_SINGULARS:
        return _MCP_NOUN_SINGULARS[clean]
    if clean.endswith("ies") and len(clean) > 3:
        return clean[:-3] + "y"
    if clean.endswith("s") and not clean.endswith("ss"):
        return clean[:-1]
    return clean


def _mcp_plural_noun(noun: str) -> str:
    clean = _mcp_normalize_token(noun)
    if not clean:
        return ""
    if clean in _MCP_NOUN_IRREGULARS:
        return _MCP_NOUN_IRREGULARS[clean]
    if clean.endswith("y") and len(clean) > 1 and clean[-2] not in "aeiou":
        return clean[:-1] + "ies"
    if clean.endswith("s"):
        return clean
    return clean + "s"


def _mcp_available_tool_names(*, chatgpt_app: bool = False, access_mode: str = "write") -> list[str]:
    defs = (
        _mcp_chatgpt_app_tool_definitions(access_mode=access_mode)
        if chatgpt_app
        else _mcp_apps_tool_definitions()
    )
    return [str(tool.get("name") or "").strip() for tool in defs if str(tool.get("name") or "").strip()]


def _mcp_tool_alias_candidates(tool_name: str) -> set[str]:
    clean = _mcp_normalize_token(tool_name)
    if not clean:
        return set()
    out = {clean}
    parts = clean.split("_")
    if parts and parts[0] in _MCP_TOOL_ACTION_PREFIXES and len(parts) > 1:
        action = parts[0]
        noun = "_".join(parts[1:])
        noun_variants = {noun, _mcp_plural_noun(noun), _mcp_singular_noun(noun)}
        if noun.endswith("_ops"):
            noun_base = noun[: -len("_ops")]
            noun_variants.update({noun_base, _mcp_plural_noun(noun_base), _mcp_singular_noun(noun_base)})
        noun_variants = {item for item in noun_variants if item}
        for noun_variant in noun_variants:
            out.add(f"{action}_{noun_variant}")
            out.add(f"{noun_variant}_{action}")
            out.add(f"{noun_variant}/{action}")
            out.add(f"{action}/{noun_variant}")
            out.add(f"{noun_variant}.{action}")
            out.add(f"{action}.{noun_variant}")
    return out


def _mcp_alias_map(*, chatgpt_app: bool = False, access_mode: str = "write") -> dict[str, str]:
    alias_map: dict[str, str] = {
        "help": "thoth_help",
        "info": "thoth_help",
        "thoth_info": "thoth_help",
        "session": "session_info",
        "status": "session_info",
        "lookup": "resolve_object",
        "resolve": "resolve_object",
    }
    for name in _mcp_available_tool_names(chatgpt_app=chatgpt_app, access_mode=access_mode):
        for alias in _mcp_tool_alias_candidates(name):
            alias_map.setdefault(alias, name)
    return alias_map


def _mcp_resolve_tool_name(raw_name: object, *, chatgpt_app: bool = False, access_mode: str = "write") -> str | None:
    raw = str(raw_name or "").strip()
    if not raw:
        return None
    available = set(_mcp_available_tool_names(chatgpt_app=chatgpt_app, access_mode=access_mode))
    if raw in available:
        return raw
    normalized = _mcp_normalize_token(raw)
    if normalized in available:
        return normalized
    alias_target = _mcp_alias_map(chatgpt_app=chatgpt_app, access_mode=access_mode).get(normalized)
    if alias_target:
        return alias_target
    for candidate in available:
        if normalized.endswith(candidate):
            return candidate
    return None


def _mcp_tool_suggestions(
    raw_name: object,
    *,
    chatgpt_app: bool = False,
    access_mode: str = "write",
    limit: int = 5,
) -> list[str]:
    raw = str(raw_name or "").strip()
    if not raw:
        return []
    names = _mcp_available_tool_names(chatgpt_app=chatgpt_app, access_mode=access_mode)
    alias_map = _mcp_alias_map(chatgpt_app=chatgpt_app, access_mode=access_mode)
    alias_hits = difflib.get_close_matches(_mcp_normalize_token(raw), list(alias_map.keys()), n=limit, cutoff=0.55)
    suggestions: list[str] = []
    for alias in alias_hits:
        target = alias_map.get(alias)
        if target and target not in suggestions:
            suggestions.append(target)
    for item in difflib.get_close_matches(raw, names, n=limit, cutoff=0.45):
        if item not in suggestions:
            suggestions.append(item)
    return suggestions[:limit]


def _mcp_unknown_tool_message(raw_name: object, *, chatgpt_app: bool = False, access_mode: str = "write") -> str:
    raw = str(raw_name or "").strip() or "<empty>"
    suggestions = _mcp_tool_suggestions(raw, chatgpt_app=chatgpt_app, access_mode=access_mode)
    parts = [
        f"Unknown tool: {raw}.",
        "Use flat tool names like `get_supply_chain` or `create_note`, not namespace or slash paths.",
        "Use `thoth_help`, `session_info`, or `resolve_object` if you need guidance.",
    ]
    if suggestions:
        parts.append("Nearest tools: " + ", ".join(suggestions) + ".")
    return " ".join(parts)


def _mcp_chatgpt_app_token() -> str | None:
    raw = str(getattr(settings, "chatgpt_app_token", None) or "").strip()
    return raw or None


def _mcp_chatgpt_app_read_only_token() -> str | None:
    raw = str(getattr(settings, "chatgpt_app_read_only_token", None) or "").strip()
    return raw or None


def _mcp_public_base_url() -> str | None:
    raw = str(getattr(settings, "public_base_url", "") or "").strip().rstrip("/")
    return raw or None


def _mcp_absolute_url(url: object | None) -> str | None:
    raw = str(url or "").strip()
    if not raw:
        return None
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    base = _mcp_public_base_url()
    if base and raw.startswith("/"):
        return f"{base}{raw}"
    return raw


def _mcp_trim_payload(value: object, *, depth: int = 0) -> object:
    if isinstance(value, str):
        return _truncate_text(value, limit=900 if depth == 0 else 320)
    if isinstance(value, list):
        items = [_mcp_trim_payload(item, depth=depth + 1) for item in value[:12]]
        if len(value) > 12:
            items.append({"_truncated_items": len(value) - 12})
        return items
    if isinstance(value, dict):
        out: dict[str, object] = {}
        for idx, (key, item) in enumerate(value.items()):
            if idx >= 18:
                out["_truncated_fields"] = len(value) - 18
                break
            key_text = str(key)
            if key_text in {"text", "content_md", "body_md", "normalized_text", "translation_en"}:
                out[key_text] = _truncate_text(str(item or ""), limit=1200 if depth == 0 else 320)
            else:
                out[key_text] = _mcp_trim_payload(item, depth=depth + 1)
        return out
    return value


def _mcp_memory_row_payload(row: object, *, kind: str) -> dict[str, object]:
    from thoth_terminal.research_state_tokens import POLICIES, count_tokens, token_page

    data = dict(row) if isinstance(row, dict) else {}
    content = str(data.get("content_md") or "")
    payload = {key: value for key, value in data.items() if key != "content_md"}
    page = token_page(content, limit_tokens=POLICIES[kind].ceiling_tokens)
    payload["content_md"] = page["text"]
    payload["content_tokens"] = count_tokens(content)
    payload["content_truncated"] = bool(data.get("content_truncated")) or page["truncated"]
    if page["truncated"]:
        payload["transport_page"] = {key: value for key, value in page.items() if key != "text"}
    return payload


def _mcp_memory_result(result: dict[str, object]) -> dict[str, object]:
    """Preserve ranked rows and source cursors within reference-token ceilings."""
    from thoth_terminal.research_state_tokens import POLICIES, budget_status, count_tokens

    raw_segments = result.get("segments") if isinstance(result.get("segments"), list) else []
    raw_history = result.get("history") if isinstance(result.get("history"), list) else []
    shaped = {**result, "segments": [], "history": [],
              "omitted_segments_due_to_transport_budget": 0,
              "omitted_history_due_to_transport_budget": 0}
    ceiling = POLICIES["memory_catalog"].ceiling_tokens
    for field, rows, kind, omitted_key in (
        ("segments", raw_segments, "belief", "omitted_segments_due_to_transport_budget"),
        ("history", raw_history, "belief_history", "omitted_history_due_to_transport_budget"),
    ):
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            payload = _mcp_memory_row_payload(row, kind=kind)
            candidate = {**shaped, field: shaped[field] + [payload]}
            if count_tokens(candidate) > ceiling - 150:
                shaped[omitted_key] = len(rows) - index
                shaped["truncated"] = True
                break
            shaped = candidate
    shaped["count"] = len(shaped["segments"])
    shaped["transport_budget"] = budget_status("memory_catalog", shaped)
    return shaped


def _mcp_widget_resource_meta() -> dict[str, object]:
    ui_meta: dict[str, object] = {
        "prefersBorder": True,
        "csp": {"connectDomains": [], "resourceDomains": []},
    }
    meta: dict[str, object] = {
        "ui": ui_meta,
        "openai/widgetDescription": (
            "Interactive Thoth Terminal workspace for documents, companies, research reports, "
            "knowledge assets, datasets, and supply chains."
        ),
        "openai/widgetPrefersBorder": True,
        "openai/widgetCSP": {"connect_domains": [], "resource_domains": []},
    }
    base = _mcp_public_base_url()
    if base:
        ui_meta["domain"] = base
        meta["openai/widgetDomain"] = base
    return meta


def _mcp_widget_resource() -> dict[str, object]:
    return {
        "uri": _MCP_WIDGET_RESOURCE_URI,
        "name": "Thoth ChatGPT App",
        "description": "Interactive Thoth workspace widget rendered inside ChatGPT.",
        "mimeType": _MCP_WIDGET_MIME_TYPE,
    }


def _mcp_widget_contents() -> dict[str, object]:
    return {
        "contents": [
            {
                "uri": _MCP_WIDGET_RESOURCE_URI,
                "mimeType": _MCP_WIDGET_MIME_TYPE,
                "text": _MCP_WIDGET_FILE.read_text(encoding="utf-8"),
                "_meta": _mcp_widget_resource_meta(),
            }
        ]
    }


def _mcp_desk_resource() -> dict[str, object]:
    return {"uri": _MCP_DESK_RESOURCE_URI, "name": "Thoth research desk",
            "description": "Research dispatches and subject-grouped durable changes.",
            "mimeType": _MCP_WIDGET_MIME_TYPE}


def _mcp_desk_contents() -> dict[str, object]:
    meta = _mcp_widget_resource_meta()
    meta["openai/widgetDescription"] = "Research desk with dispatches and expandable durable changes."
    return {"contents": [{"uri": _MCP_DESK_RESOURCE_URI, "mimeType": _MCP_WIDGET_MIME_TYPE,
                           "text": (BASE_DIR / "research_desk.html").read_text(encoding="utf-8"),
                           "_meta": meta}]}


def _mcp_standard_tool_definitions() -> list[dict[str, object]]:
    return [
        {
            "name": "tool_catalog",
            "title": "Search cold tool catalog",
            "description": "Search the compact catalog of read-only Thoth tools omitted from this cache-stable profile. This does not change the active tool schemas.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 40},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "get_tool_schema",
            "title": "Get one cold tool schema",
            "description": "Return the exact schema for one read-only tool named by tool_catalog without adding it to the active provider tool array.",
            "inputSchema": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
                "additionalProperties": False,
            },
        },
        {
            "name": "invoke_read_tool",
            "title": "Invoke one cold read tool",
            "description": "Invoke a read-only tool after inspecting its schema. Mutation tools are always rejected; writes require an explicitly exposed hot tool schema.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "arguments": {"type": "object"},
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        },
        {
            "name": "batch_read",
            "title": "Batch research reads",
            "description": "Run a bounded group of independent, allowlisted read-only Thoth lookups in one MCP call.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "operations": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": _MCP_BATCH_READ_MAX_OPERATIONS,
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string", "enum": sorted(_MCP_BATCH_READ_TOOL_NAMES)},
                                "arguments": {"type": "object"},
                            },
                            "required": ["name"],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["operations"],
                "additionalProperties": False,
            },
        },
        {
            "name": "search",
            "title": "Search knowledge",
            "description": "Search Thoth documents and object-aware neighborhoods with citation-friendly results.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "kind": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
        {
            "name": "fetch",
            "title": "Fetch document",
            "description": "Get metadata and a bounded leading excerpt for a document returned by search. It does not promise full model-visible text.",
            "inputSchema": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
                "additionalProperties": False,
            },
        },
    ]


def _mcp_apps_tool_definitions() -> list[dict[str, object]]:
    defs = [dict(item) for item in _mcp_tool_definitions()]
    defs.extend(_mcp_standard_tool_definitions())
    defs.extend([
        {"name": "get_research_desk", "title": "Thoth research desk",
         "description": "Open the research desk: published dispatches in minimal view, or recorded changes grouped by subject when operator access permits. Older results use the returned cursor.",
         "inputSchema": {"type": "object", "properties": {
             "mode": {"type": "string", "enum": ["minimal", "operator"], "default": "minimal"},
             "limit": {"type": "integer", "minimum": 1, "maximum": 50},
             "before_id": {"type": "string"}}, "additionalProperties": False}},
        {"name": "get_research_change", "title": "Research change details",
         "description": "Expand one recorded research change by its exact desk ID. Operator access is required; returns rationale, evidence references and recorded version information.",
         "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}},
                         "required": ["id"], "additionalProperties": False}},
    ])
    out: list[dict[str, object]] = []
    for raw in defs:
        tool = dict(raw)
        name = str(tool.get("name") or "").strip()
        tool["title"] = _mcp_tool_title(name, tool.get("title"))
        tool["description"] = _mcp_tool_description(name, tool.get("description"))
        tool["annotations"] = _mcp_tool_annotations(name)
        meta = dict(tool.get("_meta") or {})
        ui_meta = dict(meta.get("ui") or {})
        ui_meta["visibility"] = _mcp_tool_visibility(name)
        if _mcp_is_read_only_tool(name):
            ui_meta["resourceUri"] = _MCP_WIDGET_RESOURCE_URI
            meta["openai/outputTemplate"] = _MCP_WIDGET_RESOURCE_URI
            meta["openai/widgetAccessible"] = True
        if name == "get_research_desk":
            ui_meta["resourceUri"] = _MCP_DESK_RESOURCE_URI
            meta["openai/outputTemplate"] = _MCP_DESK_RESOURCE_URI
            meta["openai/ui"] = {"entrypoints": [{"type": "global"}, {"type": "thread"}]}
        elif name == "get_research_change":
            ui_meta.pop("resourceUri", None)
            meta.pop("openai/outputTemplate", None)
        if name in {"get_artcraft_workbench", "artcraft_workflow"}:
            from thoth_terminal.web.artcraft_mcp import ARTCRAFT_UI_URI
            ui_meta["resourceUri"] = ARTCRAFT_UI_URI
            meta["openai/outputTemplate"] = ARTCRAFT_UI_URI
            if name == "get_artcraft_workbench":
                meta["openai/ui"] = {"entrypoints": [{"type": "global"}, {"type": "thread"}]}
        invoking, invoked = _mcp_tool_status_text(name)
        meta["ui"] = ui_meta
        meta["openai/toolInvocation/invoking"] = invoking
        meta["openai/toolInvocation/invoked"] = invoked
        tool["_meta"] = meta
        out.append(tool)
    return out


def _mcp_normalize_tool_profile(value: object | None, *, default: str = "full") -> str:
    raw = str(value or "").strip().lower()
    fallback_raw = str(default or "full").strip().lower()
    fallback = _MCP_TOOL_PROFILE_ALIASES.get(fallback_raw, fallback_raw)
    if fallback != "full" and fallback not in _MCP_TOOL_PROFILE_HOT_NAMES:
        fallback = "full"
    normalized = _MCP_TOOL_PROFILE_ALIASES.get(raw, raw) if raw else fallback
    if normalized == "full" or normalized in _MCP_TOOL_PROFILE_HOT_NAMES:
        return normalized
    return fallback


def _mcp_profile_tool_definitions(
    *,
    tool_profile: str,
    chatgpt_app: bool = False,
    access_mode: str = "write",
) -> list[dict[str, object]]:
    profile = _mcp_normalize_tool_profile(tool_profile)
    definitions = (
        _mcp_chatgpt_app_tool_definitions(access_mode=access_mode)
        if chatgpt_app
        else _mcp_apps_tool_definitions()
    )
    if profile == "full":
        return [row for row in definitions if str(row.get("name") or "") not in
                (_MCP_TOOL_GATEWAY_NAMES | {"publish_external_model_projection",
                    "list_external_agent_proposals", "get_external_agent_proposal",
                    "resolve_external_agent_proposal", "list_external_agent_intake",
                    "resolve_external_agent_intake", "withdraw_external_model_projection"})]
    hot = _MCP_TOOL_PROFILE_HOT_NAMES[profile]
    selected = [row for row in definitions if str(row.get("name") or "") in hot]
    if profile != "analyst_v2":
        return selected
    # Only this new, explicitly versioned profile carries outputSchema.  The
    # legacy profiles remain byte-for-byte stable for prompt-cache contracts.
    return [
        {**row, "outputSchema": dict(_MCP_OUTPUT_SCHEMAS[str(row.get("name"))])}
        if str(row.get("name") or "") in _MCP_OUTPUT_SCHEMAS
        else row
        for row in selected
    ]


def _mcp_tool_profile_manifest(
    *,
    tool_profile: str,
    chatgpt_app: bool = False,
    access_mode: str = "write",
) -> dict[str, object]:
    profile = _mcp_normalize_tool_profile(tool_profile)
    definitions = _mcp_profile_tool_definitions(
        tool_profile=profile,
        chatgpt_app=chatgpt_app,
        access_mode=access_mode,
    )
    encoded = json.dumps(
        definitions,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    sha256 = hashlib.sha256(encoded).hexdigest()
    surface_key = (
        f"chatgpt_{'read_only' if access_mode == 'read_only' else 'write'}"
        if chatgpt_app
        else "operator"
    )
    contract_key = f"{profile}:{surface_key}"
    frozen = _MCP_TOOL_PROFILE_FROZEN_SHA256.get(contract_key)
    return {
        "name": profile,
        "contract_key": contract_key,
        "tool_count": len(definitions),
        "schema_chars": len(encoded),
        "schema_sha256": sha256,
        "frozen_schema_sha256": frozen,
        "frozen_contract_matches": frozen is None or secrets.compare_digest(sha256, frozen),
        "cold_schema_strategy": "fixed_catalog_schema_invoke_read_gateway" if profile != "full" else "none",
        "output_schema_profile": MCP_OUTPUT_SCHEMA_PROFILE_VERSION if profile == "analyst_v2" else None,
    }


def _mcp_profile_direct_names(
    *,
    tool_profile: str,
    chatgpt_app: bool = False,
    access_mode: str = "write",
) -> set[str]:
    return {
        str(row.get("name") or "").strip()
        for row in _mcp_profile_tool_definitions(
            tool_profile=tool_profile,
            chatgpt_app=chatgpt_app,
            access_mode=access_mode,
        )
        if str(row.get("name") or "").strip()
    }


def _mcp_cold_read_definitions(
    *,
    tool_profile: str,
    chatgpt_app: bool = False,
    access_mode: str = "write",
) -> list[dict[str, object]]:
    profile = _mcp_normalize_tool_profile(tool_profile)
    if profile == "full":
        return []
    direct = _mcp_profile_direct_names(
        tool_profile=profile,
        chatgpt_app=chatgpt_app,
        access_mode=access_mode,
    )
    definitions = (
        _mcp_chatgpt_app_tool_definitions(access_mode=access_mode)
        if chatgpt_app
        else _mcp_apps_tool_definitions()
    )
    return [
        row
        for row in definitions
        if (name := str(row.get("name") or "").strip())
        and name not in direct
        and name not in _MCP_TOOL_GATEWAY_NAMES
        and (name != "list_document_arrival_cards" or profile in {"worldview_v1", "operator_agent_v1"})
        and (name != "get_worldview_memory" or profile in {"worldview_v1", "operator_agent_v1"})
        and (name not in {"get_operator_research_inbox", "get_operator_research_message"}
             or profile in {"worldview_v1", "operator_agent_v1"})
        and (name not in {"list_research_extensions", "get_research_extension_executions"}
             or profile == "worldview_v1")
        and (name not in {"get_source_product_model_context", "preview_product_configuration_change"}
             or profile in {"worldview_v1", "operator_agent_v1"})
        and _mcp_is_read_only_tool(name)
    ]


def _mcp_chatgpt_app_tool_allowed(tool_name: str, *, access_mode: str) -> bool:
    name = str(tool_name or "").strip()
    if not name or name in _MCP_CHATGPT_APP_BLOCKED_TOOL_NAMES:
        return False
    if access_mode == "read_only":
        return _mcp_is_read_only_tool(name)
    return True


def _mcp_chatgpt_app_tool_definitions(*, access_mode: str) -> list[dict[str, object]]:
    security_schemes = [{"type": "noauth"}]
    out: list[dict[str, object]] = []
    for tool in _mcp_apps_tool_definitions():
        name = str(tool.get("name") or "").strip()
        if not _mcp_chatgpt_app_tool_allowed(name, access_mode=access_mode):
            continue
        item = dict(tool)
        item["securitySchemes"] = list(security_schemes)
        meta = dict(item.get("_meta") or {})
        meta["securitySchemes"] = list(security_schemes)
        item["_meta"] = meta
        out.append(item)
    return out


def _mcp_chatgpt_app_audit_target(tool_name: str, arguments: dict[str, object], result: dict[str, object]) -> tuple[str, int | None]:
    target_type = str(tool_name or "mcp_tool")
    target_id: int | None = None
    for raw in (
        result.get("id"),
        result.get("dataset_id"),
        result.get("job_id"),
        result.get("report_id"),
        result.get("version_id"),
        arguments.get("id"),
        arguments.get("document_id"),
        arguments.get("thesis_id"),
        arguments.get("dataset_id"),
        arguments.get("report_id"),
    ):
        if raw is not None and str(raw).isdigit():
            target_id = int(raw)
            break
    return target_type, target_id


def _mcp_record_chatgpt_app_audit(
    tool_name: str,
    arguments: dict[str, object],
    result: dict[str, object],
    *,
    access_mode: str,
) -> dict[str, object] | None:
    if _mcp_is_read_only_tool(tool_name):
        return None
    target_type, target_id = _mcp_chatgpt_app_audit_target(tool_name, arguments, result)
    payload = {
        "surface": "chatgpt_app",
        "access_mode": access_mode,
        "tool_name": tool_name,
        "arguments": _mcp_trim_payload(arguments),
        "result": _mcp_trim_payload(result),
    }
    from thoth_terminal.research_play_service import record_alignment_feedback

    with session_scope() as session:
        row = record_alignment_feedback(
            session,
            target_type=target_type,
            target_id=target_id,
            feedback_kind="chatgpt_app_write",
            actor="chatgpt_app",
            note=f"{tool_name} via ChatGPT app",
            payload=payload,
        )
        return {
            "id": int(row.id),
            "kind": "chatgpt_app_write",
            "actor": "chatgpt_app",
            "tool_name": tool_name,
            "target_type": target_type,
            "target_id": target_id,
            "created_at": row.created_at.isoformat(),
        }


def _research_canon_mcp_tool_definitions() -> list[dict[str, object]]:
    return [
        {"name": "scan_research_canon_gaps", "description": "run the recurring-safe research-canon coverage scan for active universes", "inputSchema": {"type": "object", "properties": {"universe": {"type": "string"}, "limit": {"type": "integer"}, "create_requests": {"type": "boolean"}, "create_cards": {"type": "boolean"}}}},
        {"name": "list_research_canon_gaps", "description": "list durable research-canon coverage gaps by universe, subject, kind, and status", "inputSchema": {"type": "object", "properties": {"universe": {"type": "string"}, "subject_id": {"type": "integer"}, "gap_kind": {"type": "string"}, "status": {"type": "string"}, "limit": {"type": "integer"}}}},
        {"name": "get_research_canon_gap", "description": "fetch one durable research-canon coverage gap by id or key", "inputSchema": {"type": "object", "properties": {"id": {"type": "integer"}, "gap_key": {"type": "string"}}}},
        {"name": "list_subject_scenarios", "description": "list structured scenarios for a research subject", "inputSchema": {"type": "object", "properties": {"subject_id": {"type": "integer"}, "subject": {"type": "string"}, "include_archived": {"type": "boolean"}}}},
        {"name": "upsert_subject_scenario", "description": "create or update a structured scenario for a research subject", "inputSchema": {"type": "object", "properties": {"subject_id": {"type": "integer"}, "subject": {"type": "string"}, "slug": {"type": "string"}, "name": {"type": "string"}, "scenario_kind": {"type": "string"}, "probability": {"type": "number"}, "confidence": {"type": "number"}, "selected_canon": {"type": "boolean"}, "thesis_summary": {"type": "string"}, "assumptions": {"type": "object"}, "key_metrics": {"type": "object"}, "evidence_links": {"type": "array", "items": {"type": "object"}}, "invalidation_triggers": {"type": "array", "items": {"type": "string"}}, "open_questions": {"type": "array", "items": {"type": "string"}}, "status": {"type": "string"}}}},
        {"name": "select_subject_canon_scenario", "description": "select one scenario as the active canon scenario", "inputSchema": {"type": "object", "properties": {"scenario_id": {"type": "integer"}, "supersede_previous": {"type": "boolean"}}, "required": ["scenario_id"]}},
        {"name": "supersede_subject_scenario", "description": "mark a structured subject scenario as superseded", "inputSchema": {"type": "object", "properties": {"scenario_id": {"type": "integer"}, "replacement_scenario_id": {"type": "integer"}}, "required": ["scenario_id"]}},
        {"name": "draft_subject_scenarios", "description": "draft bear, base, bull, and canon scenario placeholders for a subject", "inputSchema": {"type": "object", "properties": {"subject_id": {"type": "integer"}, "subject": {"type": "string"}, "scenarios": {"type": "array", "items": {"type": "object"}}}}},
        {"name": "refresh_subject_source_pack", "description": "refresh official source-pack status and optionally create missing-item requests", "inputSchema": {"type": "object", "properties": {"subject_id": {"type": "integer"}, "subject": {"type": "string"}, "discover": {"type": "boolean"}, "create_requests": {"type": "boolean"}}}},
        {"name": "refresh_active_universe_source_packs", "description": "refresh API-first official source packs for active universe companies and optionally create gated missing-item requests", "inputSchema": {"type": "object", "properties": {"universe": {"type": "string"}, "limit": {"type": "integer"}, "create_requests": {"type": "boolean"}, "create_gap_requests": {"type": "boolean"}, "create_cards": {"type": "boolean"}, "materialize_official_documents": {"type": "boolean"}, "register_official_sources": {"type": "boolean"}, "run_coverage_scan": {"type": "boolean"}, "stale_after_days": {"type": "integer"}}}},
        {"name": "audit_active_universe_source_pack_coverage", "description": "audit active-universe official/API source-pack coverage, source rails, identity blockers, and transcript gate status", "inputSchema": {"type": "object", "properties": {"universe": {"type": "string"}, "limit": {"type": "integer"}, "ensure_subjects": {"type": "boolean"}, "refresh_source_packs": {"type": "boolean"}, "examples_limit": {"type": "integer"}}}},
        {"name": "backfill_active_universe_official_source_packs", "description": "run bounded official/API source backfills, materialize fetched filings/disclosures, and refresh source packs without generic transcript scraping", "inputSchema": {"type": "object", "properties": {"universe": {"type": "string"}, "rails": {"type": "array", "items": {"type": "string"}}, "source_names": {"type": "array", "items": {"type": "string"}}, "max_runs_per_source": {"type": "integer"}, "max_runtime_seconds": {"type": "integer"}, "register_official_sources": {"type": "boolean"}, "materialize_official_documents": {"type": "boolean"}, "refresh_source_packs": {"type": "boolean"}, "include_audit": {"type": "boolean"}, "limit": {"type": "integer"}}}},
        {"name": "get_subject_source_pack", "description": "read official source-pack status for a research subject", "inputSchema": {"type": "object", "properties": {"subject_id": {"type": "integer"}, "subject": {"type": "string"}}}},
        {"name": "refresh_subject_report_quality", "description": "evaluate and store report quality/readiness for a subject", "inputSchema": {"type": "object", "properties": {"subject_id": {"type": "integer"}, "subject": {"type": "string"}, "stale_after_days": {"type": "integer"}}}},
        {"name": "get_subject_report_quality", "description": "read report quality/readiness for a subject", "inputSchema": {"type": "object", "properties": {"subject_id": {"type": "integer"}, "subject": {"type": "string"}}}},
        {"name": "generate_agent_canon_card", "description": "regenerate compact agent canon card for a subject", "inputSchema": {"type": "object", "properties": {"subject_id": {"type": "integer"}, "subject": {"type": "string"}}}},
        {"name": "get_agent_canon_card", "description": "read compact agent canon card for a subject", "inputSchema": {"type": "object", "properties": {"subject_id": {"type": "integer"}, "subject": {"type": "string"}}}},
        {"name": "preview_research_campaign_from_universe", "description": "preview campaign items for an active universe without writes", "inputSchema": {"type": "object", "properties": {"universe": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["universe"]}},
        {"name": "create_research_campaign_from_universe", "description": "create or update a durable research campaign from an active universe", "inputSchema": {"type": "object", "properties": {"universe": {"type": "string"}, "name": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["universe"]}},
        {"name": "get_research_campaign", "description": "fetch a research campaign and optionally its items", "inputSchema": {"type": "object", "properties": {"id": {"type": "integer"}, "campaign_key": {"type": "string"}, "include_items": {"type": "boolean"}, "limit": {"type": "integer"}}}},
        {"name": "list_next_research_campaign_items", "description": "list next queued or retryable research campaign items", "inputSchema": {"type": "object", "properties": {"campaign_id": {"type": "integer"}, "limit": {"type": "integer"}}, "required": ["campaign_id"]}},
        {"name": "run_research_campaign_items", "description": "execute the next bounded set of eligible research campaign items", "inputSchema": {"type": "object", "properties": {"campaign_id": {"type": "integer"}, "campaign_key": {"type": "string"}, "limit": {"type": "integer"}, "dry_run": {"type": "boolean"}, "force": {"type": "boolean"}}}},
        {"name": "mark_research_campaign_item_status", "description": "mark a research campaign item status and store outputs or errors", "inputSchema": {"type": "object", "properties": {"item_id": {"type": "integer"}, "status": {"type": "string"}, "last_error": {"type": "string"}, "output_report_id": {"type": "integer"}, "output_report_version_id": {"type": "integer"}, "quality": {"type": "object"}}, "required": ["item_id", "status"]}},
        {"name": "retry_research_campaign_item", "description": "retry a failed or previously blocked research campaign item", "inputSchema": {"type": "object", "properties": {"item_id": {"type": "integer"}}, "required": ["item_id"]}},
        {"name": "unblock_research_campaign_items", "description": "move blocked campaign items back to queued once blockers are resolved", "inputSchema": {"type": "object", "properties": {"campaign_id": {"type": "integer"}, "limit": {"type": "integer"}}}},
        {"name": "start_bottleneck_report", "description": "upsert a BottleneckModel, create a queued report campaign item, and optionally build the workspace startup packet; open readiness gaps are carried as work context, not blockers", "inputSchema": {"type": "object", "properties": {"model_key": {"type": "string"}, "title": {"type": "string"}, "scope_type": {"type": "string"}, "scope_key": {"type": "string"}, "constrained_input": {"type": "string"}, "output_unit": {"type": "string"}, "supply_chain_id": {"type": "integer"}, "subject_id": {"type": "integer"}, "summary": {"type": "string"}, "thesis": {"type": "string"}, "payload": {"type": "object"}, "links": {"type": "array", "items": {"type": "object"}}, "campaign_key": {"type": "string"}, "campaign_name": {"type": "string"}, "priority": {"type": "integer"}, "execute": {"type": "boolean"}, "force": {"type": "boolean"}}, "required": ["model_key", "title"]}},
        {"name": "upsert_research_bottleneck_object", "description": "create or update a research-canon bottleneck object with structural links", "inputSchema": {"type": "object", "properties": {"model_key": {"type": "string"}, "title": {"type": "string"}, "scope_type": {"type": "string"}, "scope_key": {"type": "string"}, "constrained_input": {"type": "string"}, "supply_chain_id": {"type": "integer"}, "subject_id": {"type": "integer"}, "summary": {"type": "string"}, "thesis": {"type": "string"}, "payload": {"type": "object"}, "links": {"type": "array", "items": {"type": "object"}}}, "required": ["model_key", "title"]}},
        {"name": "refresh_research_bottleneck_gaps", "description": "refresh durable coverage gaps for bottleneck model readiness", "inputSchema": {"type": "object", "properties": {}}},
    ]



from thoth_terminal.web.mcp_parts import finalize_mcp_tool_modules, install_mcp_tool_modules


install_mcp_tool_modules(globals())


def _mcp_validate_schema_value(value: object, schema: object, *, path: str = "arguments") -> None:
    """Validate the bounded subset of JSON Schema used by MCP tool inputs.

    The project intentionally does not add a runtime jsonschema dependency for
    one gateway operation.  This validator covers the standard object/array,
    required, enum, primitive, and numeric bounds used by the tool catalog.
    """

    if not isinstance(schema, dict):
        return
    schema_type = schema.get("type")
    if schema_type == "object":
        if not isinstance(value, dict):
            raise ValueError(f"{path} must be an object")
        properties = schema.get("properties")
        property_map = properties if isinstance(properties, dict) else {}
        required = schema.get("required")
        for key in required if isinstance(required, list) else []:
            if key not in value:
                raise ValueError(f"{path}.{key} is required")
        if schema.get("additionalProperties") is False:
            unknown = sorted(str(key) for key in value if key not in property_map)
            if unknown:
                raise ValueError(f"{path} contains unknown field(s): {', '.join(unknown)}")
        for key, child_schema in property_map.items():
            if key in value:
                _mcp_validate_schema_value(value[key], child_schema, path=f"{path}.{key}")
        return
    if schema_type == "array":
        if not isinstance(value, list):
            raise ValueError(f"{path} must be an array")
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        if minimum is not None and len(value) < int(minimum):
            raise ValueError(f"{path} must contain at least {int(minimum)} item(s)")
        if maximum is not None and len(value) > int(maximum):
            raise ValueError(f"{path} may contain at most {int(maximum)} item(s)")
        item_schema = schema.get("items")
        for index, item in enumerate(value):
            _mcp_validate_schema_value(item, item_schema, path=f"{path}[{index}]")
        return
    if schema_type == "string" and not isinstance(value, str):
        raise ValueError(f"{path} must be a string")
    if schema_type == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
        raise ValueError(f"{path} must be an integer")
    if schema_type == "number" and (isinstance(value, bool) or not isinstance(value, (int, float))):
        raise ValueError(f"{path} must be a number")
    if schema_type == "boolean" and not isinstance(value, bool):
        raise ValueError(f"{path} must be a boolean")
    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        raise ValueError(f"{path} must be one of the permitted values")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if schema.get("minimum") is not None and value < float(schema["minimum"]):
            raise ValueError(f"{path} must be at least {schema['minimum']}")
        if schema.get("maximum") is not None and value > float(schema["maximum"]):
            raise ValueError(f"{path} must be at most {schema['maximum']}")


def _mcp_definition_for_name(
    tool_name: str,
    *,
    chatgpt_app: bool,
    access_mode: str,
) -> dict[str, object] | None:
    definitions = (
        _mcp_chatgpt_app_tool_definitions(access_mode=access_mode)
        if chatgpt_app
        else _mcp_apps_tool_definitions()
    )
    for definition in definitions:
        if str(definition.get("name") or "").strip() == tool_name:
            return definition
    return None


def _mcp_execute_parallel_batch_operation(
    index: int,
    tool_name: str,
    arguments: dict[str, object],
    *,
    chatgpt_app: bool,
    chatgpt_access_mode: str,
    tool_profile: str,
) -> dict[str, object]:
    with track_database_queries() as metrics:
        try:
            result = _mcp_handle_call(
                tool_name,
                arguments,
                chatgpt_app=chatgpt_app,
                chatgpt_access_mode=chatgpt_access_mode,
                tool_profile=tool_profile,
            )
        except Exception as exc:
            return {
                "index": index,
                "name": tool_name,
                "ok": False,
                "error": {"type": type(exc).__name__, "message": str(exc)},
                "diagnostics": {
                    "queryCount": metrics.query_count,
                    "sqlTimeMs": round(metrics.sql_time_s * 1000.0, 3),
                },
            }
    return {
        "index": index,
        "name": tool_name,
        "ok": True,
        "result": result,
        "diagnostics": {
            "queryCount": metrics.query_count,
            "sqlTimeMs": round(metrics.sql_time_s * 1000.0, 3),
        },
    }


def _mcp_tool_batch_read(
    params: dict[str, object],
    *,
    chatgpt_app: bool = False,
    chatgpt_access_mode: str = "write",
    tool_profile: str = "full",
) -> dict[str, object]:
    raw_operations = params.get("operations")
    if not isinstance(raw_operations, list) or not raw_operations:
        raise ValueError("operations must be a non-empty array")
    if len(raw_operations) > _MCP_BATCH_READ_MAX_OPERATIONS:
        raise ValueError(f"operations may contain at most {_MCP_BATCH_READ_MAX_OPERATIONS} items")

    operations: list[dict[str, object]] = []
    valid_operations: list[tuple[int, str, dict[str, object]]] = []
    for index, raw_operation in enumerate(raw_operations):
        if not isinstance(raw_operation, dict):
            operations.append(
                {
                    "index": index,
                    "name": "",
                    "ok": False,
                    "error": {
                        "type": "InvalidOperation",
                        "message": f"operations[{index}] must be an object",
                    },
                }
            )
            continue
        tool_name = str(raw_operation.get("name") or "").strip()
        arguments = raw_operation.get("arguments")
        if arguments is None:
            arguments = {}
        if tool_name not in _MCP_BATCH_READ_TOOL_NAMES:
            operations.append(
                {
                    "index": index,
                    "name": tool_name,
                    "ok": False,
                    "error": {
                        "type": "ToolNotAllowed",
                        "message": f"Tool `{tool_name}` is not available in batch_read.",
                    },
                }
            )
            continue
        if not is_batch_safe_tool(tool_name):
            operations.append(
                {
                    "index": index,
                    "name": tool_name,
                    "ok": False,
                    "error": {
                        "type": "ToolNotAllowed",
                        "message": f"Tool `{tool_name}` is not classified as batch-safe.",
                    },
                }
            )
            continue
        if not isinstance(arguments, dict):
            operations.append(
                {
                    "index": index,
                    "name": tool_name,
                    "ok": False,
                    "error": {"type": "InvalidArguments", "message": "arguments must be an object"},
                }
            )
            continue
        definition = _mcp_definition_for_name(
            tool_name,
            chatgpt_app=chatgpt_app,
            access_mode=chatgpt_access_mode,
        )
        if definition is None:
            operations.append(
                {
                    "index": index,
                    "name": tool_name,
                    "ok": False,
                    "error": {
                        "type": "ToolNotAvailable",
                        "message": f"Tool `{tool_name}` has no published input schema.",
                    },
                }
            )
            continue
        try:
            _mcp_validate_schema_value(
                arguments,
                definition.get("inputSchema") or {"type": "object", "properties": {}},
            )
        except ValueError as exc:
            operations.append(
                {
                    "index": index,
                    "name": tool_name,
                    "ok": False,
                    "error": {"type": "InvalidArguments", "message": str(exc)},
                }
            )
            continue
        if chatgpt_app and not _mcp_chatgpt_app_tool_allowed(tool_name, access_mode=chatgpt_access_mode):
            operations.append(
                {
                    "index": index,
                    "name": tool_name,
                    "ok": False,
                    "error": {"type": "ToolNotAvailable", "message": "Tool is not available on this endpoint."},
                }
            )
            continue
        valid_operations.append((index, tool_name, dict(arguments)))

    can_parallelize = (
        len(valid_operations) > 1
        and len(valid_operations) == len(raw_operations)
        and using_postgres()
        and all(is_parallel_safe_tool(tool_name) for _, tool_name, _ in valid_operations)
    )
    execution_mode = "parallel" if can_parallelize else "sequential"
    if can_parallelize:
        with ThreadPoolExecutor(max_workers=min(_MCP_BATCH_READ_PARALLEL_MAX, len(valid_operations))) as executor:
            completed = list(
                executor.map(
                    lambda item: _mcp_execute_parallel_batch_operation(
                        item[0],
                        item[1],
                        item[2],
                        chatgpt_app=chatgpt_app,
                        chatgpt_access_mode=chatgpt_access_mode,
                        tool_profile=tool_profile,
                    ),
                    valid_operations,
                )
            )
        operations.extend(completed)
    else:
        for index, tool_name, arguments in valid_operations:
            try:
                result = _mcp_handle_call(
                    tool_name,
                    arguments,
                    chatgpt_app=chatgpt_app,
                    chatgpt_access_mode=chatgpt_access_mode,
                    tool_profile=tool_profile,
                )
                operations.append({"index": index, "name": tool_name, "ok": True, "result": result})
            except Exception as exc:
                operations.append(
                    {
                        "index": index,
                        "name": tool_name,
                        "ok": False,
                        "error": {"type": type(exc).__name__, "message": str(exc)},
                    }
                )
    operations.sort(key=lambda operation: int(operation.get("index") or 0))
    succeeded = sum(1 for operation in operations if operation["ok"])
    return {
        "count": len(operations),
        "succeeded": succeeded,
        "failed": len(operations) - succeeded,
        "operations": operations,
        "execution": {
            "mode": execution_mode,
            "backend": database_backend(),
            "max_workers": _MCP_BATCH_READ_PARALLEL_MAX if can_parallelize else 1,
            "parallel_safe": bool(can_parallelize),
        },
    }


def _mcp_profile_read_definition(
    name: object,
    *,
    tool_profile: str,
    chatgpt_app: bool,
    access_mode: str,
) -> dict[str, object] | None:
    requested = str(name or "").strip()
    if not requested:
        return None
    definitions = _mcp_cold_read_definitions(
        tool_profile=tool_profile,
        chatgpt_app=chatgpt_app,
        access_mode=access_mode,
    )
    for row in definitions:
        if str(row.get("name") or "").strip() == requested:
            return row
    return None


def _mcp_tool_catalog(
    params: dict[str, object],
    *,
    tool_profile: str,
    chatgpt_app: bool,
    access_mode: str,
) -> dict[str, object]:
    query = str(params.get("query") or "").strip().lower()
    limit = max(1, min(int(params.get("limit") or 30), 40))
    rows: list[dict[str, object]] = []
    tokens = _mcp_slug_parts(query)
    for definition in _mcp_cold_read_definitions(
        tool_profile=tool_profile,
        chatgpt_app=chatgpt_app,
        access_mode=access_mode,
    ):
        name = str(definition.get("name") or "").strip()
        description = str(definition.get("description") or "").strip()
        surface = f"{name} {description}".lower()
        if query and all(token not in surface for token in tokens):
            continue
        rows.append(
            {
                "name": name,
                "description": description[:240],
                "read_only": True,
                "next": f"get_tool_schema(name={name!r}) then invoke_read_tool",
            }
        )
    if query:
        # Rank before limiting: generic verbs must not hide an exact tool lookup.
        rows.sort(key=lambda row: (
            str(row["name"]) != "_".join(tokens),
            -sum(token in _mcp_slug_parts(row["name"]) for token in tokens),
            -sum(token in f'{row["name"]} {row["description"]}'.lower() for token in tokens),
            str(row["name"]),
        ))
    rows = rows[:limit]
    return {
        "tool_profile": _mcp_normalize_tool_profile(tool_profile),
        "catalog_mode": "cold_read_tools",
        "schema_prefix_changes": False,
        "count": len(rows),
        "tools": rows,
    }


def _mcp_tool_get_tool_schema(
    params: dict[str, object],
    *,
    tool_profile: str,
    chatgpt_app: bool,
    access_mode: str,
) -> dict[str, object]:
    name = str(params.get("name") or "").strip()
    definition = _mcp_profile_read_definition(
        name,
        tool_profile=tool_profile,
        chatgpt_app=chatgpt_app,
        access_mode=access_mode,
    )
    if definition is None:
        raise ValueError(f"Cold read tool `{name}` is not available in profile `{_mcp_normalize_tool_profile(tool_profile)}`.")
    return {
        "tool_profile": _mcp_normalize_tool_profile(tool_profile),
        "name": name,
        "description": definition.get("description"),
        "inputSchema": definition.get("inputSchema") or {"type": "object", "properties": {}},
        "invoke_with": "invoke_read_tool",
        "schema_prefix_changes": False,
    }


def _mcp_tool_invoke_read_tool(
    params: dict[str, object],
    *,
    tool_profile: str,
    chatgpt_app: bool,
    access_mode: str,
) -> dict[str, object]:
    name = str(params.get("name") or "").strip()
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")
    definition = _mcp_profile_read_definition(
        name,
        tool_profile=tool_profile,
        chatgpt_app=chatgpt_app,
        access_mode=access_mode,
    )
    if definition is None or not _mcp_is_read_only_tool(name):
        raise ValueError(f"Tool `{name}` is not an allowed cold read tool.")
    tool_result = _mcp_handle_call(
        name,
        dict(arguments),
        chatgpt_app=chatgpt_app,
        chatgpt_access_mode=access_mode,
        tool_profile="full",
    )
    image_data = tool_result.pop("_image_data_base64", None)
    response = {
        "invoked_tool": name,
        "tool_profile": _mcp_normalize_tool_profile(tool_profile),
        "result": tool_result,
    }
    if image_data:
        response["_image_data_base64"] = image_data
        response["content_type"] = tool_result.get("content_type") or "image/png"
    return response


def _mcp_recommendation_routes(result, *, tool_profile, chatgpt_app, access_mode, help_result=False):
    """Expose recommendations through the same access rules as tool dispatch."""
    direct = _mcp_profile_direct_names(tool_profile=tool_profile, chatgpt_app=chatgpt_app,
                                     access_mode=access_mode)
    cold = {row["name"] for row in _mcp_cold_read_definitions(
        tool_profile=tool_profile, chatgpt_app=chatgpt_app, access_mode=access_mode)}

    def route(value):
        operation = {"tool": value} if isinstance(value, str) else dict(value)
        name = operation.get("tool")
        if name in direct:
            return operation
        if name in cold:
            return {"tool": "get_tool_schema", "arguments": {"name": name},
                    "then": {"tool": "invoke_read_tool", "arguments": {
                        "name": name, "arguments": operation.get("arguments") or {}}}}
        return None

    def visit(value):
        if isinstance(value, list):
            return [visit(item) for item in value]
        if not isinstance(value, dict):
            return value
        out = {}
        extra_operations = []
        for key, item in value.items():
            if key in ({"next_tools", "recommended_next_tools", "suggested_tools"} | ({"core_tools"} if help_result else set())) and isinstance(item, list):
                # Names remain directly callable; cold targets carry explicit two-step routes.
                out[key] = [name for name in item if isinstance(name, str) and name in direct]
                routed = [operation for name in item if isinstance(name, str)
                          and (operation := route(name)) is not None]
                if key == "core_tools":
                    out["core_operations"] = routed
                else:
                    extra_operations.extend(routed)
            elif key in ({"next_operations"} | ({"steps", "canonical_examples"} if help_result else set())) and isinstance(item, list):
                out[key] = [operation for step in item if isinstance(step, (str, dict))
                            and (operation := route(step)) is not None]
            elif key == "groups" and isinstance(item, dict):
                out[key] = {kind: visit(rows) for kind, rows in item.items()}
            elif key in {"matches", "items", "object_results", "neighborhoods", "object", "related",
                         "guidance", "documents", "main_nouns", "common_workflows", "focus"}:
                out[key] = visit(item)
            else:
                # Authored content/data is not a generated tool recommendation.
                out[key] = item
        if extra_operations:
            operations = out.setdefault("next_operations", [])
            for operation in extra_operations:
                if operation not in operations:
                    operations.append(operation)
        return out

    return visit(result)



def _mcp_handle_call(
    tool_name: str,
    arguments: dict[str, object],
    *,
    chatgpt_app: bool = False,
    chatgpt_access_mode: str = "write",
    tool_profile: str = "full",
) -> dict[str, object]:
    from thoth_terminal.web.artcraft_mcp import ARTCRAFT_TOOLS, artcraft_call
    if tool_name in ARTCRAFT_TOOLS:
        if tool_name == "artcraft_workflow" and chatgpt_app and chatgpt_access_mode == "read_only":
            raise ValueError("ArtCraft editing requires write access")
        return artcraft_call(tool_name, arguments)
    if tool_name in {"get_research_desk", "get_research_change"}:
        from thoth_terminal.research_desk_service import get_research_change, get_research_desk

        with session_scope() as session:
            operator = not chatgpt_app
            if tool_name == "get_research_change":
                return get_research_change(session, str(arguments.get("id") or ""), operator=operator)
            return get_research_desk(session, mode=arguments.get("mode", "minimal"),
                                     limit=arguments.get("limit", 20), before_id=arguments.get("before_id"),
                                     operator=operator)
    if tool_name == "tool_catalog":
        return _mcp_tool_catalog(
            arguments,
            tool_profile=tool_profile,
            chatgpt_app=chatgpt_app,
            access_mode=chatgpt_access_mode,
        )
    if tool_name == "get_tool_schema":
        return _mcp_tool_get_tool_schema(
            arguments,
            tool_profile=tool_profile,
            chatgpt_app=chatgpt_app,
            access_mode=chatgpt_access_mode,
        )
    if tool_name == "invoke_read_tool":
        return _mcp_tool_invoke_read_tool(
            arguments,
            tool_profile=tool_profile,
            chatgpt_app=chatgpt_app,
            access_mode=chatgpt_access_mode,
        )
    if tool_name == "batch_read":
        return _mcp_tool_batch_read(
            arguments,
            chatgpt_app=chatgpt_app,
            chatgpt_access_mode=chatgpt_access_mode,
            tool_profile=tool_profile,
        )
    if tool_name == "search":
        return _mcp_tool_search(arguments)
    if tool_name == "fetch":
        return _mcp_tool_fetch(arguments)
    if tool_name == "thoth_help":
        return _mcp_tool_thoth_help(
            arguments,
            chatgpt_app=chatgpt_app,
            chatgpt_access_mode=chatgpt_access_mode,
            tool_profile=tool_profile,
        )
    if tool_name == "session_info":
        return _mcp_tool_session_info(
            arguments,
            chatgpt_app=chatgpt_app,
            chatgpt_access_mode=chatgpt_access_mode,
            tool_profile=tool_profile,
        )
    if tool_name in {"get_research_state_budget_policy", "list_state_compaction_candidates",
                     "get_state_compaction_source", "compact_research_state"}:
        if chatgpt_app or tool_profile not in {"worldview_v1", "operator_agent_v1"}:
            raise ValueError("Research state compaction requires a private research profile")
        if tool_name == "compact_research_state" and tool_profile != "worldview_v1":
            raise ValueError("State compaction writes require an active scheduled worldview run")
        from thoth_terminal.research_state_compaction_service import (
            get_research_state_budget_policy, list_state_compaction_candidates,
            get_state_compaction_source, compact_research_state,
        )
        if tool_name == "get_research_state_budget_policy":
            from thoth_terminal.research_state_tokens import budget_status
            from thoth_terminal.cloud_research_automation import CLOUD_RESEARCH_HARNESS_SYSTEM, CLOUD_RESEARCH_AUTOMATION_SYSTEM, THOTH_CAUSAL_WORLD_MODEL_PREMISE
            native_root = Path(__file__).resolve().parents[2] / ".dsh" / "skills"
            return {**get_research_state_budget_policy(), "protected_state": {
                "scheduled_instructions": budget_status("system_instructions", CLOUD_RESEARCH_HARNESS_SYSTEM),
                "api_research_instructions": budget_status("system_instructions", CLOUD_RESEARCH_AUTOMATION_SYSTEM),
                "causal_premise": budget_status("system_instructions", THOTH_CAUSAL_WORLD_MODEL_PREMISE),
                "native_skills": {path.parent.name: budget_status("harness_native_skill", path.read_text(encoding="utf-8"))
                                  for path in sorted(native_root.glob("*/SKILL.md"))},
                "tool_schemas": {str(row["name"]): budget_status("tool_schema", row)
                                 for row in _mcp_profile_tool_definitions(tool_profile=tool_profile)},
            }}
        with session_scope() as session:
            if tool_name == "list_state_compaction_candidates":
                return list_state_compaction_candidates(session, kinds=arguments.get("kinds"),
                    after_id=int(arguments.get("after_id") or 0), limit=int(arguments.get("limit") or 20))
            if tool_name == "get_state_compaction_source":
                return get_state_compaction_source(session, kind=str(arguments["kind"]),
                    object_id=int(arguments["object_id"]), offset_tokens=int(arguments.get("offset_tokens") or 0),
                    limit_tokens=int(arguments.get("limit_tokens") or 6000),
                    content_field=arguments.get("content_field"))
            receipt = compact_research_state(session, cloud_run_id=int(arguments["cloud_run_id"]),
                capability=str(arguments["capability"]), operation_key=str(arguments["operation_key"]),
                payload=arguments["payload"])
            if receipt.get("kind") == "extension":
                from thoth_terminal.research_extension_service import sync_active_research_extensions
                sync_active_research_extensions(session)
            return receipt
    if tool_name in {
        "list_research_extensions", "create_research_extension_candidate",
        "validate_research_extension_candidate", "activate_research_extension_candidate",
        "rollback_research_extension",
        "execute_research_extension", "get_research_extension_executions",
    }:
        if tool_profile != "worldview_v1" or chatgpt_app:
            raise ValueError("Research extensions require the scheduled worldview profile")
        from thoth_terminal.research_extension_service import (
            list_research_extensions, create_research_extension_candidate,
            validate_research_extension_candidate, activate_research_extension_candidate,
            rollback_research_extension,
            execute_research_extension, get_research_extension_executions,
        )

        with session_scope() as session:
            if tool_name == "list_research_extensions":
                return {"extensions": list_research_extensions(
                    session, extension_key=str(arguments.get("extension_key") or ""),
                    limit=int(arguments.get("limit") or 50),
                )}
            if tool_name == "get_research_extension_executions":
                return get_research_extension_executions(
                    session, extension_key=str(arguments["extension_key"]),
                    limit=int(arguments.get("limit") or 20),
                )
            common = {
                "cloud_run_id": int(arguments["cloud_run_id"]),
                "capability": str(arguments["capability"]),
                "operation_key": str(arguments["operation_key"]),
            }
            if tool_name == "create_research_extension_candidate":
                return create_research_extension_candidate(session, **common, payload=arguments["payload"])
            if tool_name == "execute_research_extension":
                return execute_research_extension(
                    session, **common, extension_key=str(arguments["extension_key"]),
                    inputs=arguments["inputs"],
                    version_id=int(arguments["version_id"]) if arguments.get("version_id") is not None else None,
                )
            if tool_name == "validate_research_extension_candidate":
                return validate_research_extension_candidate(
                    session, **common, candidate_id=int(arguments["candidate_id"]),
                )
            if tool_name == "activate_research_extension_candidate":
                return activate_research_extension_candidate(
                    session, **common, candidate_id=int(arguments["candidate_id"]),
                )
            return rollback_research_extension(
                session, **common, extension_key=str(arguments["extension_key"]),
                target_version_id=int(arguments["target_version_id"]),
            )
    if tool_name == "get_worldview_memory":
        if tool_profile not in {"full", "worldview_v1", "operator_agent_v1"}:
            raise ValueError("Private worldview memory requires a Thoth research-agent profile")
        from thoth_terminal.worldview_memory_service import get_worldview_memory

        with session_scope() as session:
            return get_worldview_memory(
                session, segment_key=str(arguments.get("segment_key") or ""),
                query=str(arguments.get("query") or ""),
                segment_type=str(arguments.get("segment_type") or ""),
                include_history=bool(arguments.get("include_history", False)),
                limit=int(arguments.get("limit") or 20),
            )
    if tool_name == "apply_worldview_memory":
        if tool_profile not in {"full", "worldview_v1"}:
            raise ValueError("Private worldview memory writes require the scheduled research profile")
        from thoth_terminal.worldview_memory_service import apply_worldview_memory

        with session_scope() as session:
            return apply_worldview_memory(
                session, cloud_run_id=int(arguments["cloud_run_id"]),
                capability=str(arguments["capability"]),
                operation_key=str(arguments["operation_key"]),
                payload=arguments["payload"],
            )
    if tool_name == "apply_worldview_write":
        from thoth_terminal.worldview_write_service import apply_worldview_write

        with session_scope() as session:
            return apply_worldview_write(
                session,
                cloud_run_id=int(arguments["cloud_run_id"]),
                capability=str(arguments["capability"]),
                operation_key=str(arguments["operation_key"]),
                destination=arguments["destination"],
                payload=arguments["payload"],
            )
    if tool_name == "apply_worldview_scenario":
        from thoth_terminal.worldview_write_service import apply_worldview_scenario

        with session_scope() as session:
            return apply_worldview_scenario(
                session, cloud_run_id=int(arguments["cloud_run_id"]),
                capability=str(arguments["capability"]),
                operation_key=str(arguments["operation_key"]),
                payload=arguments["payload"],
            )
    if tool_name in {"apply_worldview_product_model", "apply_worldview_product_configuration"}:
        from thoth_terminal.product_model_service import (
            apply_worldview_product_configuration, apply_worldview_product_model,
        )

        writer = apply_worldview_product_model if tool_name == "apply_worldview_product_model" else apply_worldview_product_configuration
        with session_scope() as session:
            return writer(
                session, cloud_run_id=int(arguments["cloud_run_id"]),
                capability=str(arguments["capability"]), operation_key=str(arguments["operation_key"]),
                payload=arguments["payload"],
            )
    if tool_name == "get_agentx_schema":
        from thoth_terminal.agentx_service import get_agentx_schema
        return get_agentx_schema(str(arguments.get("mode") or "canonical"))
    if tool_name in {"get_agentx_context", "get_agentx_configuration", "evaluate_agentx_configuration"}:
        from thoth_terminal.agentx_service import agentx_context, get_agentx_configuration, evaluate_agentx_configuration
        with session_scope() as session:
            if tool_name == "get_agentx_context":
                return agentx_context(session)
            if tool_name == "get_agentx_configuration":
                return get_agentx_configuration(session, str(arguments["configuration_key"]), arguments.get("version_id"))
            return evaluate_agentx_configuration(session, arguments["payload"])
    if tool_name == "apply_worldview_agentx_configuration":
        if chatgpt_app or _mcp_normalize_tool_profile(tool_profile) not in {"full", "worldview_v1"}:
            raise ValueError("AgentX scoped writes require the scheduled worldview profile")
        from thoth_terminal.agentx_service import apply_worldview_agentx_configuration
        with session_scope() as session:
            return apply_worldview_agentx_configuration(session, cloud_run_id=int(arguments["cloud_run_id"]),
                capability=str(arguments["capability"]), operation_key=str(arguments["operation_key"]), payload=arguments["payload"])
    if tool_name == "get_product_models":
        from thoth_terminal.product_model_service import list_product_models

        with session_scope() as session:
            return list_product_models(
                session, query=str(arguments.get("query") or ""),
                subject_id=int(arguments["subject_id"]) if str(arguments.get("subject_id") or "").isdigit() else None,
                include_history=bool(arguments.get("include_history", False)),
                limit=int(arguments.get("limit") or 60),
            )
    if tool_name == "get_product_configuration":
        from thoth_terminal.product_model_service import get_product_configuration

        with session_scope() as session:
            return get_product_configuration(
                session, configuration_key=str(arguments["configuration_key"]),
                include_history=bool(arguments.get("include_history", False)),
                memory_context_payload=arguments.get("memory_context_payload"),
            )
    if tool_name == "get_source_product_model_context":
        if tool_profile not in {"full", "worldview_v1", "operator_agent_v1"}:
            raise ValueError("Source-to-model context requires a Thoth research-agent profile")
        from thoth_terminal.product_model_impact_service import get_source_product_model_context

        with session_scope() as session:
            return get_source_product_model_context(
                session, source_document_id=int(arguments["source_document_id"]),
                subject_id=int(arguments["subject_id"]), limit=int(arguments.get("limit") or 40),
            )
    if tool_name == "preview_product_configuration_change":
        if tool_profile not in {"full", "worldview_v1", "operator_agent_v1"}:
            raise ValueError("Product impact preview requires a Thoth research-agent profile")
        from thoth_terminal.product_model_impact_service import preview_product_configuration_change

        with session_scope() as session:
            return preview_product_configuration_change(
                session, configuration_key=str(arguments["configuration_key"]),
                source_document_id=int(arguments["source_document_id"]),
                input_changes=arguments["input_changes"],
                model_revision_id=int(arguments["model_revision_id"]) if arguments.get("model_revision_id") is not None else None,
            )
    if tool_name == "get_worldview_worlds":
        from thoth_terminal.worldview_write_service import list_worldview_worlds

        with session_scope() as session:
            return list_worldview_worlds(
                session,
                subject_id=int(arguments["subject_id"]) if str(arguments.get("subject_id") or "").isdigit() else None,
                query=str(arguments.get("query") or ""), limit=int(arguments.get("limit") or 40),
                include_history=bool(arguments.get("include_history", False)),
            )
    if tool_name == "get_worldview_reconciliation":
        from thoth_terminal.worldview_write_service import get_worldview_reconciliation

        with session_scope() as session:
            return get_worldview_reconciliation(
                session, subject_id=int(arguments["subject_id"]),
                world_key=str(arguments["world_key"]), limit=int(arguments.get("limit") or 40),
            )
    if tool_name == "apply_worldview_roadmap_update":
        from thoth_terminal.worldview_write_service import apply_worldview_roadmap_update

        with session_scope() as session:
            return apply_worldview_roadmap_update(
                session,
                cloud_run_id=int(arguments["cloud_run_id"]),
                capability=str(arguments["capability"]),
                operation_key=str(arguments["operation_key"]),
                payload=arguments["payload"],
            )
    if tool_name == "apply_worldview_roadmap_review":
        from thoth_terminal.worldview_write_service import apply_worldview_roadmap_review

        with session_scope() as session:
            return apply_worldview_roadmap_review(
                session, cloud_run_id=int(arguments["cloud_run_id"]),
                capability=str(arguments["capability"]),
                operation_key=str(arguments["operation_key"]), payload=arguments["payload"],
            )
    if tool_name == "get_roadmap_review_queue":
        from thoth_terminal.roadmap_outcome_service import get_roadmap_review_queue

        with session_scope() as session:
            return get_roadmap_review_queue(
                session, limit=int(arguments.get("limit") or 20),
                lookahead_days=int(arguments.get("lookahead_days", 30)),
            )
    if tool_name == "get_roadmap_entry_history":
        from thoth_terminal.roadmap_outcome_service import get_roadmap_entry_history

        with session_scope() as session:
            return get_roadmap_entry_history(
                session, entry_key=str(arguments["entry_key"]), limit=int(arguments.get("limit") or 30),
            )
    if tool_name == "resolve_worldview_research_request":
        from thoth_terminal.worldview_write_service import resolve_worldview_research_request

        with session_scope() as session:
            return resolve_worldview_research_request(
                session, cloud_run_id=int(arguments["cloud_run_id"]),
                capability=str(arguments["capability"]),
                request_id=int(arguments["request_id"]),
                write_operation_key=str(arguments["write_operation_key"]),
                note=str(arguments["note"]),
            )
    if tool_name == "get_roadmap_snapshot":
        from thoth_terminal.roadmap_service import get_roadmap_snapshot

        with session_scope() as session:
            return get_roadmap_snapshot(
                session, query=str(arguments.get("query") or ""),
                category=str(arguments.get("category") or ""),
                period=str(arguments.get("period") or "all"),
                limit=int(arguments.get("limit") or 20),
            )
    if tool_name == "apply_interactive_research_write":
        from thoth_terminal.interactive_agent_write_service import apply_interactive_research_write

        with session_scope() as session:
            return apply_interactive_research_write(
                session, operation_key=str(arguments["operation_key"]),
                destination=arguments["destination"], payload=arguments["payload"],
            )
    if tool_name == "apply_interactive_consensus_evidence":
        from thoth_terminal.interactive_agent_write_service import apply_interactive_consensus_evidence

        with session_scope() as session:
            return apply_interactive_consensus_evidence(
                session, operation_key=str(arguments["operation_key"]), payload=arguments["payload"],
            )
    if tool_name in {"get_operator_research_inbox", "get_operator_research_message"}:
        if tool_profile not in {"operator_agent_v1", "worldview_v1"} or chatgpt_app:
            raise ValueError("Private operator relay requires the owner or scheduled research profile")
        from thoth_terminal.operator_research_relay_service import get_operator_research_inbox, get_operator_research_message

        with session_scope() as session:
            if tool_name == "get_operator_research_message":
                return get_operator_research_message(session, request_id=arguments["request_id"])
            return get_operator_research_inbox(
                session, after_request_id=arguments.get("after_request_id", 0),
                limit=arguments.get("limit", 20), pending_only=arguments.get("pending_only", False),
            )
    if tool_name == "submit_operator_feedback":
        if tool_profile != "operator_agent_v1" or chatgpt_app:
            raise ValueError("Feedback submission requires the owner agent profile")
        from thoth_terminal.operator_research_relay_service import submit_operator_feedback

        with session_scope() as session:
            return submit_operator_feedback(
                session, request_key=arguments["request_key"], kind=arguments["kind"],
                text=arguments["text"], speaker=arguments["speaker"],
                rationale=arguments.get("rationale", ""), confidence=arguments.get("confidence"),
                assessment=arguments.get("assessment"),
                source_document_ids=arguments.get("source_document_ids", []),
                reply_to_request_id=arguments.get("reply_to_request_id"),
                supersedes_request_id=arguments.get("supersedes_request_id"),
            )
    if tool_name == "send_worldview_operator_message":
        if tool_profile != "worldview_v1" or chatgpt_app:
            raise ValueError("Research messages require the scheduled worldview profile")
        from thoth_terminal.worldview_write_service import send_worldview_operator_message

        with session_scope() as session:
            return send_worldview_operator_message(
                session, cloud_run_id=int(arguments["cloud_run_id"]), capability=str(arguments["capability"]),
                operation_key=str(arguments["operation_key"]), payload=arguments["payload"],
            )
    if tool_name == "submit_operator_research_request":
        if tool_profile != "operator_agent_v1":
            raise ValueError("Research request submission requires the operator agent profile")
        if not str(arguments.get("research_request") or "").strip():
            raise ValueError("A specific research_request is required")
        from thoth_terminal.external_agent_intake_service import queue_operator_evidence_request, serialize_evidence_request

        with session_scope() as session:
            row = queue_operator_evidence_request(
                session, document_id=int(arguments["document_id"]),
                request_key=str(arguments["request_key"]),
                request_text=str(arguments["research_request"]),
            )
            return serialize_evidence_request(session, row)
    if tool_name == "publish_external_model_projection":
        if tool_profile != "operator_agent_v1":
            raise ValueError("External model publication requires the operator agent profile")
        from thoth_terminal.external_agent_model_service import publish_model_projection, serialize_public_projection

        with session_scope() as session:
            row = publish_model_projection(
                session, model_key=str(arguments["model_key"]),
                model_type=str(arguments["model_type"]), model_id=int(arguments["model_id"]),
                source_document_ids=arguments["source_document_ids"], payload=arguments["payload"],
            )
            return serialize_public_projection(row)
    if tool_name == "withdraw_external_model_projection":
        if tool_profile != "operator_agent_v1":
            raise ValueError("External model withdrawal requires the operator agent profile")
        from thoth_terminal.external_agent_model_service import withdraw_model_projection

        with session_scope() as session:
            return withdraw_model_projection(session, model_key=str(arguments["model_key"]),
                                             reason=str(arguments["reason"]))
    if tool_name in {"list_external_agent_proposals", "get_external_agent_proposal",
                     "resolve_external_agent_proposal", "list_external_agent_intake",
                     "resolve_external_agent_intake"}:
        if tool_profile not in {"operator_agent_v1", "worldview_v1"}:
            raise ValueError("Outside-agent intake requires a Thoth research-agent profile")
        if tool_name.startswith("resolve_") and tool_profile != "operator_agent_v1":
            raise ValueError("Outside-agent request resolution requires the operator agent profile")
        from thoth_terminal.external_agent_model_service import (
            list_operator_proposals, resolve_operator_proposal, serialize_operator_proposal,
        )
        from thoth_terminal.models import ExternalAgentProposal
        from thoth_terminal.external_agent_intake_service import list_evidence_requests, resolve_evidence_request

        with session_scope() as session:
            if tool_name == "list_external_agent_intake":
                return {"requests": list_evidence_requests(
                    session, status=str(arguments.get("status") or "submitted"),
                    limit=int(arguments.get("limit") or 20),
                )}
            if tool_name == "resolve_external_agent_intake":
                return resolve_evidence_request(
                    session, request_id=int(arguments["request_id"]),
                    decision=str(arguments["decision"]), operator_note=str(arguments["operator_note"]),
                    operation_key=str(arguments["operation_key"]) if arguments.get("operation_key") else None,
                )
            if tool_name == "list_external_agent_proposals":
                return {"proposals": list_operator_proposals(
                    session, status=str(arguments.get("status") or "submitted"),
                    limit=int(arguments.get("limit") or 20),
                )}
            if tool_name == "get_external_agent_proposal":
                row = session.query(ExternalAgentProposal).filter_by(id=int(arguments["proposal_id"])).first()
                if row is None:
                    raise ValueError("Proposal not found")
                return serialize_operator_proposal(session, row)
            return resolve_operator_proposal(
                session, proposal_id=int(arguments["proposal_id"]),
                decision=str(arguments["decision"]), operator_note=str(arguments["operator_note"]),
                operation_key=str(arguments["operation_key"]) if arguments.get("operation_key") else None,
            )
    if tool_name == "apply_worldview_journal":
        from thoth_terminal.worldview_write_service import apply_worldview_journal

        with session_scope() as session:
            return apply_worldview_journal(
                session,
                cloud_run_id=int(arguments["cloud_run_id"]),
                capability=str(arguments["capability"]),
                operation_key=str(arguments["operation_key"]),
                payload=arguments["payload"],
            )
    if tool_name == "apply_worldview_consensus_evidence":
        from thoth_terminal.worldview_write_service import apply_worldview_consensus_evidence

        with session_scope() as session:
            return apply_worldview_consensus_evidence(
                session,
                cloud_run_id=int(arguments["cloud_run_id"]),
                capability=str(arguments["capability"]),
                operation_key=str(arguments["operation_key"]),
                payload=arguments["payload"],
            )
    if tool_name == "get_consensus_snapshot":
        from thoth_terminal.web.app_sections.consensus import _consensus_payload

        limit = max(1, min(int(arguments.get("limit") or 10), 20))
        with session_scope() as session:
            view = _consensus_payload(
                session,
                query_text=str(arguments.get("query") or "")[:200],
                subject_filter=str(arguments.get("subject") or "")[:200],
                metric_filter=str(arguments.get("metric") or "")[:128],
                period_filter=str(arguments.get("period") or "")[:64],
                limit=limit,
            )
            return {
                "metrics": view["metrics"],
                "consensus_rows": view["consensus_rows"][:limit],
                "evidence_rows": [{key: row.get(key) for key in (
                    "id", "subject", "source", "person", "metric", "period", "lane", "scenario",
                    "value", "reported_range", "value_basis", "published_at", "summary", "source_document_href", "aggregate_included",
                    "aggregate_exclusions",
                )} for row in view["evidence_rows"][:limit]],
                "truncated": len(view["consensus_rows"]) > limit or len(view["evidence_rows"]) > limit,
            }
    if tool_name == "list_worldview_journals":
        from thoth_terminal.informal_research_service import list_informal_research_docs

        with session_scope() as session:
            rows = list_informal_research_docs(
                session, q=str(arguments.get("query") or "").strip() or None,
                category="worldview_journal", limit=max(1, min(int(arguments.get("limit") or 20), 50)),
            )
            return {"items": [{
                "doc_key": row.doc_key, "title": row.title, "subject": row.subject,
                "summary": row.summary, "word_count": row.word_count,
                "source_document_ids": (row.payload or {}).get("source_document_ids", []),
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            } for row in rows]}
    if tool_name == "get_worldview_journal":
        from thoth_terminal.informal_research_service import get_informal_research_doc

        with session_scope() as session:
            row = get_informal_research_doc(session, str(arguments["doc_key"]))
            if row is None or row.category != "worldview_journal":
                raise ValueError("Worldview journal not found")
            return {
                "doc_key": row.doc_key, "title": row.title, "subject": row.subject,
                "summary": row.summary, "content_md": row.content_md,
                "source_document_ids": (row.payload or {}).get("source_document_ids", []),
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                "events": [{"id": event.id, "summary": event.summary, "created_at": event.created_at.isoformat()}
                           for event in sorted(row.events, key=lambda item: item.id, reverse=True)[:20]],
            }
    if tool_name == "resolve_object":
        return _mcp_tool_resolve_object(arguments)
    if tool_name == "object_search":
        return _mcp_tool_object_search(arguments)
    if tool_name == "list_research_signal_kinds":
        return _mcp_tool_list_research_signal_kinds(arguments)
    if tool_name == "preview_robotics_deployment_model":
        return _mcp_tool_preview_robotics_deployment_model(arguments)
    if tool_name == "evaluate_memory_system":
        return _mcp_tool_evaluate_memory_system(arguments)
    if tool_name == "runtime_info":
        return _mcp_tool_runtime_info(arguments)
    if tool_name == "research_context_health":
        from thoth_terminal.research_health_service import research_context_health
        with session_scope() as session:
            return research_context_health(session)
    if tool_name == "codex_usage":
        from thoth_terminal.codex_usage_service import get_codex_usage
        snapshot = get_codex_usage(days=max(1, min(int(arguments.get("days") or 30), 90)))
        # Model/day rows retain the evidence needed to reconstruct these two
        # rollups. The web dashboard keeps the full snapshot.
        return {key: value for key, value in snapshot.items() if key not in {"days", "models"}}
    if tool_name == "list_sources":
        return _mcp_tool_list_sources(arguments)
    if tool_name == "get_source":
        return _mcp_tool_get_source(arguments)
    if tool_name == "run_source":
        return _mcp_tool_run_source(arguments)
    if tool_name == "update_source_config":
        return _mcp_tool_update_source_config(arguments)
    if tool_name == "list_source_acquisition_rails":
        return _mcp_tool_list_source_acquisition_rails(arguments)
    if tool_name == "get_source_acquisition_rail":
        return _mcp_tool_get_source_acquisition_rail(arguments)
    if tool_name == "ensure_source_acquisition_templates":
        return _mcp_tool_ensure_source_acquisition_templates(arguments)
    if tool_name == "instrument_request_acquisition_payload":
        return _mcp_tool_instrument_request_acquisition_payload(arguments)
    if tool_name == "list_trade_series":
        return _mcp_tool_list_trade_series(arguments)
    if tool_name == "get_trade_series":
        return _mcp_tool_get_trade_series(arguments)
    if tool_name == "find_trade_series_by_hs":
        return _mcp_tool_find_trade_series_by_hs(arguments)
    if tool_name == "refresh_trade_source":
        return _mcp_tool_refresh_trade_source(arguments)
    if tool_name == "trade_latest_summary":
        return _mcp_tool_trade_latest_summary(arguments)
    if tool_name == "search_documents":
        return _mcp_tool_search_documents(arguments)
    if tool_name == "get_document":
        return _mcp_tool_get_document(arguments)
    if tool_name == "get_document_image":
        return _mcp_tool_get_document_image(arguments)
    if tool_name == "list_document_arrival_cards":
        if tool_profile not in {"worldview_v1", "operator_agent_v1"}:
            raise ValueError("Arrival-card paging requires a Thoth research-agent profile")
        from thoth_terminal.cloud_prompt_volatile_state_service import list_document_arrival_cards
        from thoth_terminal.models import CloudGoalRun

        with session_scope() as session:
            run = session.get(CloudGoalRun, int(arguments["cloud_run_id"]))
            if run is None or run.task_kind != "cloud_research_heartbeat" or not bool((run.stats_json or {}).get("scheduled")):
                raise ValueError("Scheduled research run not found")
            bounds = dict((run.stats_json or {}).get("volatile_state", {}).get("delta") or {})
            if not bounds.get("arrival_start_at") or not bounds.get("arrival_end_at"):
                raise ValueError("This run has no arrival-card window")
            return list_document_arrival_cards(
                session,
                since=datetime.fromisoformat(str(bounds["arrival_start_at"])),
                until=datetime.fromisoformat(str(bounds["arrival_end_at"])),
                offset=max(0, int(arguments.get("offset") or 0)),
                limit=max(1, min(int(arguments.get("limit") or 100), 100)),
                max_chars=60_000,
                exclude_document_ids=arguments.get("exclude_document_ids") or [],
                query=str(arguments.get("query") or ""),
                status_only=bool(arguments.get("status_only", False)),
            )
    if tool_name == "query_document":
        return _mcp_tool_query_document(arguments)
    if tool_name == "expand_document_match":
        return _mcp_tool_expand_document_match(arguments)
    if tool_name == "read_document":
        return _mcp_tool_read_document(arguments)
    if tool_name == "inspect_document":
        return _mcp_tool_inspect_document(arguments)
    if tool_name == "render_document_page":
        return _mcp_tool_render_document_page(arguments)
    if tool_name == "ingest_research_library":
        return _mcp_tool_ingest_research_library(arguments)
    if tool_name == "list_research_library":
        return _mcp_tool_list_research_library(arguments)
    if tool_name == "list_research_conference_index":
        return _mcp_tool_list_research_conference_index(arguments)
    if tool_name == "get_research_library_report":
        return _mcp_tool_get_research_library_report(arguments)
    if tool_name == "review_research_library":
        return _mcp_tool_review_research_library(arguments)
    if tool_name == "add_research_report_addendum":
        return _mcp_tool_add_research_report_addendum(arguments)
    if tool_name == "upsert_research_source_card":
        return _mcp_tool_upsert_research_source_card(arguments)
    if tool_name == "set_doc_triage":
        return _mcp_tool_set_doc_triage(arguments)
    if tool_name == "bulk_doc_action":
        return _mcp_tool_bulk_doc_action(arguments)
    if tool_name == "list_doc_events":
        return _mcp_tool_list_doc_events(arguments)
    if tool_name == "list_theses":
        return _mcp_tool_list_theses(arguments)
    if tool_name == "get_thesis":
        return _mcp_tool_get_thesis(arguments)
    if tool_name == "add_thesis_evidence":
        return _mcp_tool_add_thesis_evidence(arguments)
    if tool_name == "create_thesis":
        return _mcp_tool_create_thesis(arguments)
    if tool_name == "list_house_view_theses":
        return _mcp_tool_list_house_view_theses(arguments)
    if tool_name == "house_view_context":
        return _mcp_tool_house_view_context(arguments)
    if tool_name == "upsert_house_view_thesis":
        return _mcp_tool_upsert_house_view_thesis(arguments)
    if tool_name == "generate_house_view_questions":
        return _mcp_tool_generate_house_view_questions(arguments)
    if tool_name == "update_thesis":
        return _mcp_tool_update_thesis(arguments)
    if tool_name == "list_agent_actions":
        return _mcp_tool_list_agent_actions(arguments)
    if tool_name == "update_agent_action":
        return _mcp_tool_update_agent_action(arguments)
    if tool_name == "list_agent_change_runs":
        return _mcp_tool_list_agent_change_runs(arguments)
    if tool_name == "get_agent_change_run":
        return _mcp_tool_get_agent_change_run(arguments)
    if tool_name == "list_thesis_proposals":
        return _mcp_tool_list_thesis_proposals(arguments)
    if tool_name == "review_thesis_proposal":
        return _mcp_tool_review_thesis_proposal(arguments)
    if tool_name == "run_thesis_autopilot":
        return _mcp_tool_run_thesis_autopilot(arguments)
    if tool_name == "create_note":
        return _mcp_tool_create_note(arguments)
    if tool_name == "list_notes":
        return _mcp_tool_list_notes(arguments)
    if tool_name == "get_note":
        return _mcp_tool_get_note(arguments)
    if tool_name == "update_note":
        return _mcp_tool_update_note(arguments)
    if tool_name == "list_datasets":
        return _mcp_tool_list_datasets(arguments)
    if tool_name == "get_dataset":
        return _mcp_tool_get_dataset(arguments)
    if tool_name == "create_dataset":
        return _mcp_tool_create_dataset(arguments)
    if tool_name == "import_dataset":
        return _mcp_tool_import_dataset(arguments)
    if tool_name == "query_dataset":
        return _mcp_tool_query_dataset(arguments)
    if tool_name == "update_dataset_rows":
        return _mcp_tool_update_dataset_rows(arguments)
    if tool_name == "create_dataset_view":
        return _mcp_tool_create_dataset_view(arguments)
    if tool_name == "preview_dataset_view":
        return _mcp_tool_preview_dataset_view(arguments)
    if tool_name == "upsert_dataset_draft_row":
        return _mcp_tool_upsert_dataset_draft_row(arguments)
    if tool_name == "delete_dataset_draft_row":
        return _mcp_tool_delete_dataset_draft_row(arguments)
    if tool_name == "bulk_paste_dataset_rows":
        return _mcp_tool_bulk_paste_dataset_rows(arguments)
    if tool_name == "export_dataset":
        return _mcp_tool_export_dataset(arguments)
    if tool_name == "list_instrument_requests":
        return _mcp_tool_list_instrument_requests(arguments)
    if tool_name == "preview_gap_hunter_universe_index":
        with session_scope() as session:
            return universe_coverage_index(
                session,
                keyword=str(arguments.get("keyword") or "").strip() or None,
                universe_id=int(arguments["universe_id"]) if arguments.get("universe_id") else None,
                section_id=int(arguments["section_id"]) if arguments.get("section_id") else None,
                limit=int(arguments.get("limit") or 200),
            )
    if tool_name == "get_instrument_request":
        return _mcp_tool_get_instrument_request(arguments)
    if tool_name == "create_instrument_request":
        return _mcp_tool_create_instrument_request(arguments)
    if tool_name == "review_instrument_request":
        return _mcp_tool_review_instrument_request(arguments)
    if tool_name == "run_instrument_request":
        return _mcp_tool_run_instrument_request(arguments)
    if tool_name == "fulfill_instrument_request":
        return _mcp_tool_fulfill_instrument_request(arguments)
    if tool_name == "list_product_stage_entries":
        return _mcp_tool_list_product_stage_entries(arguments)
    if tool_name == "query_product_stage":
        return _mcp_tool_query_product_stage(arguments)
    if tool_name == "get_product_stage_entry":
        return _mcp_tool_get_product_stage_entry(arguments)
    if tool_name == "list_product_stage_claims":
        return _mcp_tool_list_product_stage_claims(arguments)
    if tool_name == "get_product_stage_claim":
        return _mcp_tool_get_product_stage_claim(arguments)
    if tool_name == "create_product_stage_claim":
        return _mcp_tool_create_product_stage_claim(arguments)
    if tool_name == "review_product_stage_claim":
        return _mcp_tool_review_product_stage_claim(arguments)
    if tool_name == "update_product_stage_entry":
        return _mcp_tool_update_product_stage_entry(arguments)
    if tool_name == "list_product_stage_history":
        return _mcp_tool_list_product_stage_history(arguments)
    if tool_name == "list_companies":
        return _mcp_tool_list_companies(arguments)
    if tool_name == "get_company":
        return _mcp_tool_get_company(arguments)
    if tool_name == "list_company_docs":
        return _mcp_tool_list_company_docs(arguments)
    if tool_name == "list_earnings_calendar":
        return _mcp_tool_list_earnings_calendar(arguments)
    if tool_name == "list_ipo_calendar":
        return _mcp_tool_list_ipo_calendar(arguments)
    if tool_name == "list_transcripts":
        return _mcp_tool_list_transcripts(arguments)
    if tool_name == "list_research_reports":
        return _mcp_tool_list_research_reports(arguments)
    if tool_name == "get_research_report":
        return _mcp_tool_get_research_report(arguments)
    if tool_name == "register_imported_report":
        return _mcp_tool_register_imported_report(arguments)
    if tool_name == "read_report_content":
        return _mcp_tool_read_report_content(arguments)
    if tool_name == "append_report_addendum":
        return _mcp_tool_append_report_addendum(arguments)
    if tool_name == "build_research_report":
        return _mcp_tool_build_research_report(arguments)
    if tool_name == "build_bottleneck_report":
        return _mcp_tool_build_bottleneck_report(arguments)
    if tool_name == "list_research_report_versions":
        return _mcp_tool_list_research_report_versions(arguments)
    if tool_name == "export_research_report":
        return _mcp_tool_export_research_report(arguments)
    if tool_name == "create_research_report_derivative":
        return _mcp_tool_create_research_report_derivative(arguments)
    if tool_name == "apply_report_memory_decision":
        return _mcp_tool_apply_report_memory_decision(arguments)
    if tool_name == "list_research_subjects":
        return _mcp_tool_list_research_subjects(arguments)
    if tool_name == "get_research_subject":
        return _mcp_tool_get_research_subject(arguments)
    if tool_name == "scan_research_canon_gaps":
        return _mcp_tool_scan_research_canon_gaps(arguments)
    if tool_name == "list_research_canon_gaps":
        return _mcp_tool_list_research_canon_gaps(arguments)
    if tool_name == "get_research_canon_gap":
        return _mcp_tool_get_research_canon_gap(arguments)
    if tool_name == "list_subject_scenarios":
        return _mcp_tool_list_subject_scenarios(arguments)
    if tool_name == "upsert_subject_scenario":
        return _mcp_tool_upsert_subject_scenario(arguments)
    if tool_name == "select_subject_canon_scenario":
        return _mcp_tool_select_subject_canon_scenario(arguments)
    if tool_name == "supersede_subject_scenario":
        return _mcp_tool_supersede_subject_scenario(arguments)
    if tool_name == "draft_subject_scenarios":
        return _mcp_tool_draft_subject_scenarios(arguments)
    if tool_name == "refresh_subject_source_pack":
        return _mcp_tool_refresh_subject_source_pack(arguments)
    if tool_name == "refresh_active_universe_source_packs":
        return _mcp_tool_refresh_active_universe_source_packs(arguments)
    if tool_name == "audit_active_universe_source_pack_coverage":
        return _mcp_tool_audit_active_universe_source_pack_coverage(arguments)
    if tool_name == "backfill_active_universe_official_source_packs":
        return _mcp_tool_backfill_active_universe_official_source_packs(arguments)
    if tool_name == "get_subject_source_pack":
        return _mcp_tool_get_subject_source_pack(arguments)
    if tool_name == "refresh_subject_report_quality":
        return _mcp_tool_refresh_subject_report_quality(arguments)
    if tool_name == "get_subject_report_quality":
        return _mcp_tool_get_subject_report_quality(arguments)
    if tool_name == "generate_agent_canon_card":
        return _mcp_tool_generate_agent_canon_card(arguments)
    if tool_name == "get_agent_canon_card":
        return _mcp_tool_get_agent_canon_card(arguments)
    if tool_name == "preview_research_campaign_from_universe":
        return _mcp_tool_preview_research_campaign_from_universe(arguments)
    if tool_name == "create_research_campaign_from_universe":
        return _mcp_tool_create_research_campaign_from_universe(arguments)
    if tool_name == "get_research_campaign":
        return _mcp_tool_get_research_campaign(arguments)
    if tool_name == "list_next_research_campaign_items":
        return _mcp_tool_list_next_research_campaign_items(arguments)
    if tool_name == "run_research_campaign_items":
        return _mcp_tool_run_research_campaign_items(arguments)
    if tool_name == "mark_research_campaign_item_status":
        return _mcp_tool_mark_research_campaign_item_status(arguments)
    if tool_name == "retry_research_campaign_item":
        return _mcp_tool_retry_research_campaign_item(arguments)
    if tool_name == "unblock_research_campaign_items":
        return _mcp_tool_unblock_research_campaign_items(arguments)
    if tool_name == "start_bottleneck_report":
        return _mcp_tool_start_bottleneck_report(arguments)
    if tool_name == "upsert_research_bottleneck_object":
        return _mcp_tool_upsert_research_bottleneck_object(arguments)
    if tool_name == "refresh_research_bottleneck_gaps":
        return _mcp_tool_refresh_research_bottleneck_gaps(arguments)
    if tool_name == "list_knowledge_assets":
        return _mcp_tool_list_knowledge_assets(arguments)
    if tool_name == "get_knowledge_asset":
        return _mcp_tool_get_knowledge_asset(arguments)
    if tool_name == "score_knowledge_asset_readiness":
        return _mcp_tool_score_knowledge_asset_readiness(arguments)
    if tool_name == "list_knowledge_readiness":
        return _mcp_tool_list_knowledge_readiness(arguments)
    if tool_name == "save_knowledge_asset_draft":
        return _mcp_tool_save_knowledge_asset_draft(arguments)
    if tool_name == "publish_knowledge_asset":
        return _mcp_tool_publish_knowledge_asset(arguments)
    if tool_name == "generate_knowledge_asset_draft":
        return _mcp_tool_generate_knowledge_asset_draft(arguments)
    if tool_name == "run_freshness_review":
        return _mcp_tool_run_freshness_review(arguments)
    if tool_name == "list_freshness_reviews":
        return _mcp_tool_list_freshness_reviews(arguments)
    if tool_name == "get_freshness_review":
        return _mcp_tool_get_freshness_review(arguments)
    if tool_name == "list_supply_chains":
        return _mcp_tool_list_supply_chains(arguments)
    if tool_name == "get_supply_chain":
        return _mcp_tool_get_supply_chain(arguments)
    if tool_name == "get_supply_chain_node":
        return _mcp_tool_get_supply_chain_node(arguments)
    if tool_name == "list_bottleneck_models":
        return _mcp_tool_list_bottleneck_models(arguments)
    if tool_name == "get_bottleneck_model":
        return _mcp_tool_get_bottleneck_model(arguments)
    if tool_name == "upsert_bottleneck_model":
        return _mcp_tool_upsert_bottleneck_model(arguments)
    if tool_name == "assess_supply_chain_bottlenecks":
        return _mcp_tool_assess_supply_chain_bottlenecks(arguments)
    if tool_name == "create_supply_chain":
        return _mcp_tool_create_supply_chain(arguments)
    if tool_name == "create_supply_chain_snapshot":
        return _mcp_tool_create_supply_chain_snapshot(arguments)
    if tool_name == "apply_supply_chain_ops":
        return _mcp_tool_apply_supply_chain_ops(arguments)
    if tool_name == "preview_supply_chain_ops":
        return _mcp_tool_preview_supply_chain_ops(arguments)
    if tool_name == "compare_supply_chain_snapshots":
        return _mcp_tool_compare_supply_chain_snapshots(arguments)
    if tool_name == "publish_supply_chain_snapshot":
        return _mcp_tool_publish_supply_chain(arguments)
    if tool_name == "publish_supply_chain":
        return _mcp_tool_publish_supply_chain(arguments)
    if tool_name == "bind_supply_chain_dataset":
        return _mcp_tool_bind_supply_chain_dataset(arguments)
    if tool_name == "get_supply_chain_metric_bindings":
        return _mcp_tool_get_supply_chain_metric_bindings(arguments)
    if tool_name == "suggest_supply_chain_metric_bindings":
        return _mcp_tool_suggest_supply_chain_metric_bindings(arguments)
    if tool_name == "suggest_dataset_supply_chain_bindings":
        return _mcp_tool_suggest_dataset_supply_chain_bindings(arguments)
    if tool_name == "suggest_metric_binding_request_fulfillments":
        return _mcp_tool_suggest_metric_binding_request_fulfillments(arguments)
    if tool_name == "list_supply_chain_alerts":
        return _mcp_tool_list_supply_chain_alerts(arguments)
    if tool_name == "list_supply_chain_lints":
        return _mcp_tool_list_supply_chain_lints(arguments)
    if tool_name == "link_supply_chain_parent":
        return _mcp_tool_link_supply_chain_parent(arguments)
    if tool_name == "unlink_supply_chain_parent":
        return _mcp_tool_unlink_supply_chain_parent(arguments)
    if tool_name == "discord_outbox_create":
        return _mcp_tool_discord_outbox_create(arguments)
    if tool_name == "discord_outbox_list":
        return _mcp_tool_discord_outbox_list(arguments)
    if tool_name == "discord_outbox_cancel":
        return _mcp_tool_discord_outbox_cancel(arguments)
    if tool_name == "discord_queue_create":
        return _mcp_tool_discord_queue_create(arguments)
    if tool_name == "discord_queue_list":
        return _mcp_tool_discord_queue_list(arguments)
    if tool_name == "discord_watchlist_add":
        return _mcp_tool_discord_watchlist_add(arguments)
    if tool_name == "discord_watchlist_bulk_add":
        return _mcp_tool_discord_watchlist_bulk_add(arguments)
    if tool_name == "discord_watchlist_remove":
        return _mcp_tool_discord_watchlist_remove(arguments)
    if tool_name == "discord_queue_bulk_create":
        return _mcp_tool_discord_queue_bulk_create(arguments)
    if tool_name == "discord_link_collect_now":
        return _mcp_tool_discord_link_collect_now(arguments)
    if tool_name == "discord_link_inbox_list":
        return _mcp_tool_discord_link_inbox_list(arguments)
    if tool_name == "discord_link_inbox_apply_decisions":
        return _mcp_tool_discord_link_inbox_apply_decisions(arguments)
    if tool_name == "discord_link_inbox_stats":
        return _mcp_tool_discord_link_inbox_stats(arguments)
    if tool_name == "agent_enqueue_digest":
        return _mcp_tool_agent_enqueue_digest(arguments)
    if tool_name == "agent_enqueue_ticker_report":
        return _mcp_tool_agent_enqueue_ticker_report(arguments)
    if tool_name == "get_recent_context_bundle":
        return _mcp_tool_get_recent_context_bundle(arguments)
    if tool_name == "agent_enqueue_context_compression":
        return _mcp_tool_agent_enqueue_context_compression(arguments)
    if tool_name == "list_research_skills":
        return _mcp_tool_list_research_skills(arguments)
    if tool_name == "create_research_skill":
        return _mcp_tool_create_research_skill(arguments)
    if tool_name == "list_research_plays":
        return _mcp_tool_list_research_plays(arguments)
    if tool_name == "get_research_play":
        return _mcp_tool_get_research_play(arguments)
    if tool_name == "create_research_play":
        return _mcp_tool_create_research_play(arguments)
    if tool_name == "bind_research_skill_to_play":
        return _mcp_tool_bind_research_skill_to_play(arguments)
    if tool_name == "run_research_play":
        return _mcp_tool_run_research_play(arguments)
    if tool_name == "record_alignment_feedback":
        return _mcp_tool_record_alignment_feedback(arguments)
    if tool_name == "integration_google_status":
        return _mcp_tool_integration_google_status(arguments)
    if tool_name == "integration_google_test":
        return _mcp_tool_integration_google_test(arguments)
    if tool_name == "discord_agent_settings_get":
        return _mcp_tool_discord_agent_settings_get(arguments)
    if tool_name == "discord_agent_settings_update":
        return _mcp_tool_discord_agent_settings_update(arguments)
    institutional_result = handle_institutional_mcp_tool(tool_name, arguments)
    if institutional_result is not None:
        return institutional_result
    raise ValueError(f"Unknown tool: {tool_name}")


def _mcp_trim_nested_list(value: object, *, limit: int, preferred_key: str | None = None) -> object:
    if isinstance(value, dict):
        shaped = dict(value)
        keys = [preferred_key] if preferred_key else []
        keys.extend(["items", "results", "rows", "entries", "assets"])
        for key in keys:
            if not key:
                continue
            nested = shaped.get(key)
            if isinstance(nested, list):
                shaped[key] = nested[:limit]
                return shaped
        return shaped
    if isinstance(value, list):
        return value[:limit]
    if isinstance(value, tuple):
        return list(value[:limit])
    return []


def _mcp_structured_content(tool_name: str, result: dict[str, object]) -> object:
    if tool_name == "artcraft_catalog":
        # Field names in an input schema are data, not private result metadata.
        return result
    if tool_name in {"artcraft_status", "get_artcraft_workbench", "artcraft_catalog", "artcraft_workflow"}:
        # The adapter already bounds results; preserve exact native input schemas.
        return _mcp_public_result(result)
    if tool_name in {"tool_catalog", "get_tool_schema", "get_research_state_budget_policy",
                     "list_state_compaction_candidates", "get_state_compaction_source",
                     "compact_research_state", "get_worldview_worlds"}:
        # A discovered schema must remain exact, including nested enums and required fields.
        return result
    if tool_name == "invoke_read_tool":
        return {**_mcp_public_result(result), "result": _mcp_structured_content(
            str(result.get("invoked_tool") or ""), result.get("result") or {})}
    if tool_name == "get_research_desk":
        # Full reading content belongs to the widget; the conversation gets references.
        return {
            "mode": result.get("mode"), "operator_available": result.get("operator_available"),
            "dispatches": [{"id": row["id"], "title": row["title"], "url": row["url"]}
                           for row in result.get("dispatches", [])],
            "groups": [{"subject": group["subject"], "changes": [
                {key: row.get(key) for key in ("id", "title", "reference")}
                for row in group["changes"]]} for group in result.get("groups", [])],
            "needs_input": [{"id": row["id"], "title": row["title"]}
                            for row in result.get("needs_input", [])],
            "pagination": result.get("pagination"), "coverage": result.get("coverage"),
        }
    if tool_name in {"submit_operator_feedback", "send_worldview_operator_message",
                     "get_operator_research_inbox", "get_operator_research_message"}:
        # The relay service bounds text and records explicitly. Trimming again
        # would hide authored reasoning or corrupt cursor/write lineage.
        return result
    if tool_name == "search":
        return result
    if tool_name == "get_worldview_memory":
        return _mcp_memory_result(result)
    if tool_name == "search_documents":
        documents = result.get("documents") if isinstance(result.get("documents"), list) else []
        return {
            "count": result.get("count"),
            "documents": [
                {
                    **_mcp_trim_payload(row),
                    "match_excerpt": _truncate_text(str(row.get("match_excerpt") or ""), limit=800),
                }
                for row in documents[:30]
                if isinstance(row, dict)
            ],
        }
    if tool_name in {"thoth_help", "session_info", "resolve_object"}:
        return _mcp_trim_payload(result)
    if tool_name == "fetch":
        return {
            "id": result.get("id"),
            "title": result.get("title"),
            "url": result.get("url"),
            "metadata": _mcp_trim_payload(result.get("metadata") or {}),
            "text_excerpt": _truncate_text(str(result.get("text") or ""), limit=1200),
            "text_scope": result.get("text_scope"),
            "text_length_chars": result.get("text_length_chars"),
            "returned_text_length_chars": result.get("returned_text_length_chars"),
            "truncated": result.get("truncated"),
        }
    if tool_name == "get_document":
        return {
            "id": result.get("id"),
            "title": result.get("title"),
            "translation_title": result.get("translation_title"),
            "url": result.get("url"),
            "source": result.get("source"),
            "author": result.get("author"),
            "triage_state": result.get("triage_state"),
            "saved": result.get("saved"),
            "published_at": result.get("published_at"),
            "domains": result.get("domains") or [],
            "tags": result.get("tags") or [],
            "text": _truncate_text(str(result.get("text") or ""), limit=1200),
            "text_scope": result.get("text_scope"),
            "text_length_chars": result.get("text_length_chars"),
            "returned_text_length_chars": result.get("returned_text_length_chars"),
            "truncated": result.get("truncated"),
        }
    if tool_name == "query_document":
        passages = result.get("passages") if isinstance(result.get("passages"), list) else []
        return {
            "document": _mcp_trim_payload(result.get("document") or {}),
            "query": result.get("query"),
            "count": result.get("count"),
            "passages": [
                {
                    **_mcp_trim_payload(row),
                    "match_excerpt": _truncate_text(str(row.get("match_excerpt") or ""), limit=1200),
                }
                for row in passages[:12]
                if isinstance(row, dict)
            ],
            "returned_token_estimate": result.get("returned_token_estimate"),
            "index_created": result.get("index_created"),
            "retrieval_mode": result.get("retrieval_mode"),
            "semantic_embeddings_used": result.get("semantic_embeddings_used"),
            "expanded_terms": result.get("expanded_terms") or [],
        }
    if tool_name == "expand_document_match":
        return {
            **_mcp_trim_payload({key: value for key, value in result.items() if key != "text"}),
            "text": _truncate_text(str(result.get("text") or ""), limit=40000),
        }
    if tool_name == "read_document":
        passages = result.get("passages") if isinstance(result.get("passages"), list) else []
        return {
            "document_id": result.get("document_id"),
            "center_passage_index": result.get("center_passage_index"),
            "count": result.get("count"),
            "passages": [
                {**_mcp_trim_payload(row), "text": _truncate_text(str(row.get("text") or ""), limit=3600)}
                for row in passages[:21]
                if isinstance(row, dict)
            ],
            "returned_token_estimate": result.get("returned_token_estimate"),
            "truncated": result.get("truncated"),
        }
    if tool_name == "inspect_document":
        structure = result.get("structure") if isinstance(result.get("structure"), dict) else {}
        return {
            "document": _mcp_trim_payload(result.get("document") or {}),
            "structure": {
                **_mcp_trim_payload(structure),
                "outline": _mcp_trim_payload((structure.get("outline") or [])[:120]),
            },
            "recommended_next_tools": result.get("recommended_next_tools") or [],
        }
    if tool_name in {"render_document_page", "get_document_image"}:
        return {
            key: value
            for key, value in result.items()
            if not str(key).startswith("_")
        }
    if tool_name == "list_research_library":
        reports = result.get("reports") if isinstance(result.get("reports"), list) else []
        return {
            "count": result.get("count"),
            "stale_count": result.get("stale_count"),
            "reports": _mcp_trim_payload(reports[:12]),
        }
    if tool_name == "list_research_conference_index":
        conferences = result.get("conferences") if isinstance(result.get("conferences"), list) else []
        return {
            "count": result.get("count"),
            "query": result.get("query"),
            "conferences": _mcp_trim_payload(conferences[:24]),
        }
    if tool_name == "get_research_library_report":
        return {
            "id": result.get("id"),
            "kind": result.get("kind"),
            "title": result.get("title"),
            "author": result.get("author"),
            "source_path": result.get("source_path"),
            "content_kind": result.get("content_kind"),
            "source_tier": result.get("source_tier"),
            "source_url": result.get("source_url"),
            "conference": result.get("conference"),
            "conference_year": result.get("conference_year"),
            "topics": result.get("topics") or [],
            "source_pack_seed": result.get("source_pack_seed"),
            "subject_hints": result.get("subject_hints") or [],
            "lifecycle": _mcp_trim_payload(result.get("lifecycle") or {}),
            "next_full_review_at": result.get("next_full_review_at"),
            "stale": result.get("stale"),
            "addendum_count": result.get("addendum_count"),
            "text": _truncate_text(str(result.get("text") or ""), limit=2000),
        }
    if tool_name == "review_research_library":
        actions = result.get("actions") if isinstance(result.get("actions"), list) else []
        candidates = result.get("candidates") if isinstance(result.get("candidates"), list) else []
        return {
            "checked_count": result.get("checked_count"),
            "candidate_count": result.get("candidate_count"),
            "action_count": result.get("action_count"),
            "review_window_days": result.get("review_window_days"),
            "stale_only": result.get("stale_only"),
            "candidates": _mcp_trim_payload(candidates[:8]),
            "actions": _mcp_trim_payload(actions[:8]),
        }
    if tool_name == "add_research_report_addendum":
        return {
            "parent": _mcp_trim_payload(result.get("parent") or {}),
            "addendum": _mcp_trim_payload(result.get("addendum") or {}),
        }
    if tool_name == "get_research_report":
        report = result.get("report") if isinstance(result.get("report"), dict) else {}
        version = result.get("version") if isinstance(result.get("version"), dict) else {}
        content = result.get("content") if isinstance(result.get("content"), dict) else {}
        return {
            "report": {
                "id": report.get("id"),
                "display_name": report.get("display_name"),
                "slug": report.get("slug"),
                "subject_key": report.get("subject_key"),
                "subject_kind": report.get("subject_kind"),
                "primary_ticker": report.get("primary_ticker"),
                "manual_addenda": _mcp_trim_payload(report.get("manual_addenda") or []),
                "updated_at": report.get("updated_at"),
            },
            "version": {
                "id": version.get("id"),
                "version": version.get("version"),
                "status": version.get("status"),
                "published_at": version.get("published_at"),
            },
            "content": {
                "section_order": list(content.get("section_order") or []),
                "identity_freshness": _mcp_trim_payload(content.get("identity_freshness") or {}),
                "coverage_panel": _mcp_trim_payload(content.get("coverage_panel") or {}),
                "primary_source_timeline": _mcp_trim_payload(
                    _mcp_trim_nested_list(content.get("primary_source_timeline"), limit=6, preferred_key="timeline")
                ),
                "published_primers": _mcp_trim_payload(_mcp_trim_nested_list(content.get("published_primers"), limit=3)),
                "published_teardowns": _mcp_trim_payload(
                    _mcp_trim_nested_list(content.get("published_teardowns"), limit=3)
                ),
            },
        }
    if tool_name == "register_imported_report":
        report = result.get("report") if isinstance(result.get("report"), dict) else {}
        version = result.get("version") if isinstance(result.get("version"), dict) else {}
        artifact = result.get("artifact") if isinstance(result.get("artifact"), dict) else {}
        return {
            "report": _mcp_trim_payload(report),
            "version": {
                "id": version.get("id"),
                "version": version.get("version"),
                "status": version.get("status"),
                "published_at": version.get("published_at"),
            },
            "artifact": _mcp_trim_payload(artifact),
        }
    if tool_name == "read_report_content":
        shaped = dict(result)
        if "markdown" in shaped:
            shaped["markdown"] = _truncate_text(str(shaped.get("markdown") or ""), limit=6000)
        if "manual_addenda_markdown" in shaped:
            shaped["manual_addenda_markdown"] = _truncate_text(str(shaped.get("manual_addenda_markdown") or ""), limit=4000)
        if "text_excerpt" in shaped:
            shaped["text_excerpt"] = _truncate_text(str(shaped.get("text_excerpt") or ""), limit=2500)
        return _mcp_trim_payload(shaped)
    if tool_name == "append_report_addendum":
        return {
            "report": _mcp_trim_payload(result.get("report") or {}),
            "version": _mcp_trim_payload(result.get("version") or {}),
            "addendum": _mcp_trim_payload(result.get("addendum") or {}),
        }
    if tool_name == "get_knowledge_asset":
        asset = result.get("asset") if isinstance(result.get("asset"), dict) else result
        version = (
            asset.get("published_version")
            if isinstance(asset.get("published_version"), dict)
            else asset.get("draft_version")
            if isinstance(asset.get("draft_version"), dict)
            else {}
        )
        return {
            "asset": {
                "id": asset.get("id"),
                "slug": asset.get("slug"),
                "kind": asset.get("kind"),
                "title": asset.get("title"),
                "summary": asset.get("summary"),
                "status": asset.get("status"),
                "subject_slug": asset.get("subject_slug"),
                "subject_display_name": asset.get("subject_display_name"),
                "published_version": {
                    "id": version.get("id"),
                    "version": version.get("version"),
                    "summary": version.get("summary"),
                    "content_md": _truncate_text(str(version.get("content_md") or ""), limit=1400),
                },
            }
        }
    if tool_name == "get_research_play":
        play = result.get("play") if isinstance(result.get("play"), dict) else {}
        runs = result.get("runs") if isinstance(result.get("runs"), list) else []
        signals = result.get("signals") if isinstance(result.get("signals"), list) else []
        return {
            "play": {
                "id": play.get("id"),
                "slug": play.get("slug"),
                "name": play.get("name"),
                "owner": play.get("owner"),
                "status": play.get("status"),
                "skill_count": play.get("skill_count"),
                "schedule_json": _mcp_trim_payload(play.get("schedule_json") or {}),
                "latest_run": _mcp_trim_payload(play.get("latest_run") or {}),
            },
            "runs": _mcp_trim_payload(runs[:8]),
            "signals": _mcp_trim_payload(signals[:12]),
        }
    if tool_name in {"get_dataset", "query_dataset"}:
        rows = result.get("rows") if isinstance(result.get("rows"), list) else []
        return {
            "id": result.get("id"),
            "slug": result.get("slug"),
            "title": result.get("title"),
            "category": result.get("category"),
            "family": result.get("family"),
            "status": result.get("status"),
            "columns": _mcp_trim_payload(result.get("columns") or []),
            "rows": _mcp_trim_payload(rows[:12]),
            "views": _mcp_trim_payload((result.get("views") or [])[:6]),
            "row_count": result.get("row_count"),
        }
    if tool_name == "get_supply_chain":
        graph = result.get("data") if isinstance(result.get("data"), dict) else {}
        return {
            "id": result.get("id"),
            "slug": result.get("slug"),
            "title": result.get("title"),
            "product": result.get("product"),
            "category": result.get("category"),
            "status": result.get("status"),
            "counts": _mcp_trim_payload(result.get("counts") or {}),
            "version": _mcp_trim_payload(result.get("version") or {}),
            "nodes": _mcp_trim_payload((graph.get("nodes") or [])[:12]),
            "edges": _mcp_trim_payload((graph.get("edges") or [])[:12]),
            "alerts": _mcp_trim_payload((graph.get("alerts") or result.get("alerts") or [])[:8]),
            "data": _mcp_trim_payload(graph),
        }
    if tool_name == "preview_supply_chain_ops":
        return {
            "id": result.get("id"),
            "title": result.get("title"),
            "slug": result.get("slug"),
            "preview": bool(result.get("preview")),
            "applied": result.get("applied"),
            "warnings": _mcp_trim_payload(result.get("warnings") or []),
            "id_map": _mcp_trim_payload(result.get("id_map") or {}),
            "before_summary": _mcp_trim_payload(result.get("before_summary") or {}),
            "after_summary": _mcp_trim_payload(result.get("after_summary") or {}),
        }
    return _mcp_trim_payload(result)


def _mcp_summary_text(tool_name: str, result: dict[str, object]) -> str:
    if tool_name in {"search_documents", "search"}:
        count = result.get("count")
        if count is None and isinstance(result.get("results"), list):
            count = len(result.get("results") or [])
        return f"Found {int(count or 0)} matching items."
    if tool_name in {"get_document", "fetch"}:
        return f"Loaded document {str(result.get('title') or 'detail')}."
    if tool_name == "ingest_research_library":
        return f"Ingested research library: {int(result.get('new') or 0)} new, {int(result.get('updated') or 0)} updated."
    if tool_name == "list_research_library":
        return f"Found {int(result.get('count') or 0)} research-library items."
    if tool_name == "list_research_conference_index":
        return f"Found {int(result.get('count') or 0)} research conference/year group(s)."
    if tool_name == "get_research_library_report":
        return f"Loaded compiled research report {str(result.get('title') or result.get('id') or '')}.".strip()
    if tool_name == "review_research_library":
        return f"Reviewed research library: {int(result.get('action_count') or 0)} refresh action(s) queued."
    if tool_name == "add_research_report_addendum":
        parent = result.get("parent") if isinstance(result.get("parent"), dict) else {}
        return f"Added research report addendum to {str(parent.get('title') or parent.get('id') or 'report')}."
    if tool_name == "upsert_research_source_card":
        return f"Upserted research source card {str(result.get('title') or result.get('id') or '')}.".strip()
    if tool_name == "get_company":
        return f"Loaded company overview for {str(result.get('display_name') or result.get('name') or 'company')}."
    if tool_name == "get_research_report":
        report = result.get("report") if isinstance(result.get("report"), dict) else {}
        return f"Loaded research report for {str(report.get('display_name') or report.get('title') or 'subject')}."
    if tool_name == "register_imported_report":
        report = result.get("report") if isinstance(result.get("report"), dict) else {}
        return f"Registered imported report {str(report.get('display_name') or report.get('title') or report.get('slug') or '')}.".strip()
    if tool_name == "read_report_content":
        report = result.get("report") if isinstance(result.get("report"), dict) else {}
        return f"Loaded report content for {str(report.get('display_name') or report.get('title') or 'report')}."
    if tool_name == "append_report_addendum":
        report = result.get("report") if isinstance(result.get("report"), dict) else {}
        return f"Added manual addendum to {str(report.get('display_name') or report.get('title') or 'report')}."
    if tool_name == "get_knowledge_asset":
        asset = result.get("asset") if isinstance(result.get("asset"), dict) else result
        return f"Loaded {str(asset.get('kind') or 'knowledge')} asset {str(asset.get('title') or asset.get('slug') or '')}.".strip()
    if tool_name == "get_research_play":
        play = result.get("play") if isinstance(result.get("play"), dict) else {}
        return f"Loaded research play {str(play.get('name') or play.get('slug') or '')}.".strip()
    if tool_name in {"get_dataset", "query_dataset"}:
        return f"Loaded dataset {str(result.get('title') or result.get('slug') or '')}.".strip()
    if tool_name == "get_supply_chain":
        return f"Loaded supply chain {str(result.get('title') or result.get('slug') or '')}.".strip()
    if tool_name == "thoth_help":
        return "Returned Thoth workflow help."
    if tool_name == "session_info":
        surface = str(result.get("surface") or "mcp")
        return f"Returned session info for {surface}."
    if tool_name == "resolve_object":
        return f"Resolved {int(result.get('count') or 0)} matching objects."
    if tool_name == "preview_supply_chain_ops":
        return f"Previewed {int(result.get('applied') or 0)} supply-chain operations."
    if isinstance(result.get("count"), int):
        return f"{_mcp_tool_title(tool_name)} returned {int(result.get('count') or 0)} items."
    return f"{_mcp_tool_title(tool_name)} completed."


def _mcp_public_result(value: object) -> object:
    """Keep the complete tool result model-readable without private or binary data."""
    if isinstance(value, dict):
        return {
            key: _mcp_public_result(item)
            for key, item in value.items()
            if not str(key).startswith("_")
            and not str(key).lower().endswith("base64")
            and str(key).lower() not in {"image_data", "image_bytes", "binary_data"}
            and not isinstance(item, bytes)
        }
    if isinstance(value, (list, tuple)):
        return [_mcp_public_result(item) for item in value if not isinstance(item, bytes)]
    return value


def _mcp_tool_result(
    tool_name: str,
    arguments: dict[str, object],
    result: dict[str, object],
    *,
    chatgpt_app: bool = False,
    audit_stamp: dict[str, object] | None = None,
    database_diagnostics: dict[str, object] | None = None,
) -> dict[str, object]:
    summary = _mcp_summary_text(tool_name, result)
    image_data = str(result.get("_image_data_base64") or "").strip() or None
    public_result = _mcp_public_result(result)
    structured_warning = None
    try:
        structured_content = _mcp_structured_content(tool_name, result)
    except Exception as exc:
        logger.exception("MCP structured content failed for tool=%s", tool_name)
        structured_warning = f"{type(exc).__name__}: {exc}"
        structured_content = _mcp_trim_payload(result)
    model_result = structured_content if tool_name in {"get_worldview_memory", "get_research_desk", "artcraft_catalog"} else public_result
    meta = {
        "thoth": {
            "toolName": tool_name,
            "toolTitle": _mcp_tool_title(tool_name),
            "summary": summary,
            "resultState": "Loaded",
            "arguments": _mcp_trim_payload(arguments),
            "result": public_result if tool_name == "get_research_desk" else model_result,
            "publicBaseUrl": _mcp_public_base_url(),
        }
    }
    if structured_warning:
        meta["thoth"]["structuredContentWarning"] = structured_warning
    if audit_stamp is not None:
        meta["thoth"]["auditStamp"] = audit_stamp
    if database_diagnostics is not None:
        meta["thoth"]["diagnostics"] = {"database": database_diagnostics}
    model_text = (
        json.dumps(model_result, ensure_ascii=False, separators=(",", ":"))
        if not chatgpt_app or tool_name in {"search", "fetch"}
        else summary
    )
    content: list[dict[str, object]] = [{"type": "text", "text": model_text}]
    if image_data:
        content.append({"type": "image", "data": image_data, "mimeType": str(result.get("content_type") or "image/png")})
        if tool_name == "artcraft_workflow":
            meta["artcraft"] = {"preview": {"data": image_data, "mimeType": str(result.get("content_type") or "image/png")}}
    return {
        "structuredContent": structured_content,
        "content": content,
        "_meta": meta,
    }


def _mcp_handle_message(
    msg: dict[str, object],
    *,
    chatgpt_app: bool = False,
    chatgpt_access_mode: str = "write",
    tool_profile: str = "full",
) -> dict[str, object] | None:
    profile = _mcp_normalize_tool_profile(tool_profile)
    if msg.get("jsonrpc") != "2.0":
        return _mcp_error(msg.get("id"), -32600, "Invalid Request")
    method = str(msg.get("method") or "")
    rpc_id = msg.get("id")
    params = msg.get("params") or {}
    if method in {"initialized", "notifications/initialized"}:
        return None
    if method == "initialize":
        proto = None
        if isinstance(params, dict):
            proto = params.get("protocolVersion")
        app_obj = globals().get("app")
        result = {
            "protocolVersion": _mcp_negotiate_protocol(proto),
            "serverInfo": {
                "name": "Thoth Terminal",
                "version": str(getattr(app_obj, "version", "unknown")),
            },
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"subscribe": False, "listChanged": False},
            },
        }
        return _mcp_result(rpc_id, result)
    if method in {"tools/list", "list_tools"}:
        tools = _mcp_profile_tool_definitions(
            tool_profile=profile,
            chatgpt_app=chatgpt_app,
            access_mode=chatgpt_access_mode,
        )
        return _mcp_result(
            rpc_id,
            {
                "tools": tools,
                "_meta": {
                    "thothToolProfile": profile,
                    "thothToolProfileManifest": _mcp_tool_profile_manifest(
                        tool_profile=profile,
                        chatgpt_app=chatgpt_app,
                        access_mode=chatgpt_access_mode,
                    ),
                },
            },
        )
    if method in {"tools/call", "call_tool"}:
        if not isinstance(params, dict):
            return _mcp_error(rpc_id, -32602, "Invalid params")
        requested_tool_name = (
            params.get("name")
            or params.get("tool")
            or params.get("tool_name")
            or params.get("toolName")
        )
        arguments = params.get("arguments") or params.get("args") or {}
        if not requested_tool_name:
            return _mcp_error(rpc_id, -32602, "Missing tool name")
        if not isinstance(arguments, dict):
            return _mcp_error(rpc_id, -32602, "Invalid arguments")
        tool_name = _mcp_resolve_tool_name(
            requested_tool_name,
            chatgpt_app=chatgpt_app,
            access_mode=chatgpt_access_mode,
        )
        if not tool_name:
            return _mcp_error(
                rpc_id,
                -32602,
                _mcp_unknown_tool_message(
                    requested_tool_name,
                    chatgpt_app=chatgpt_app,
                    access_mode=chatgpt_access_mode,
                ),
            )
        if chatgpt_app and not _mcp_chatgpt_app_tool_allowed(str(tool_name), access_mode=chatgpt_access_mode):
            return _mcp_error(
                rpc_id,
                -32602,
                f"Tool `{tool_name}` is not available on this endpoint. Call `session_info` to inspect the current surface.",
            )
        direct_names = _mcp_profile_direct_names(
            tool_profile=profile,
            chatgpt_app=chatgpt_app,
            access_mode=chatgpt_access_mode,
        )
        if str(tool_name) not in direct_names:
            return _mcp_error(
                rpc_id,
                -32602,
                f"Tool `{tool_name}` is cold in profile `{profile}`. Use `tool_catalog`, `get_tool_schema`, then `invoke_read_tool`; mutation tools require an explicit hot schema or the full profile. If these gateway tools are missing from your connector, refresh its metadata and start a new conversation; the cached manifest does not match this profile.",
            )
        started_at = time.perf_counter()
        error: Exception | None = None
        with track_database_queries() as database_metrics:
            try:
                result = _mcp_handle_call(
                    str(tool_name),
                    arguments,
                    chatgpt_app=chatgpt_app,
                    chatgpt_access_mode=chatgpt_access_mode,
                    tool_profile=profile,
                )
                if tool_name in {"object_search", "resolve_object", "search", "thoth_help"}:
                    result = _mcp_recommendation_routes(
                        result, tool_profile=profile, chatgpt_app=chatgpt_app,
                        access_mode=chatgpt_access_mode,
                        help_result=tool_name == "thoth_help",
                    )
                elif tool_name == "invoke_read_tool" and result.get("invoked_tool") in {
                    "object_search", "resolve_object", "search", "thoth_help"
                }:
                    result["result"] = _mcp_recommendation_routes(
                        result["result"], tool_profile=profile, chatgpt_app=chatgpt_app,
                        access_mode=chatgpt_access_mode,
                        help_result=result.get("invoked_tool") == "thoth_help",
                    )
                audit_stamp = None
                if chatgpt_app:
                    audit_stamp = _mcp_record_chatgpt_app_audit(
                        str(tool_name),
                        arguments,
                        result,
                        access_mode=chatgpt_access_mode,
                    )
            except Exception as exc:
                error = exc
        wall_time_ms = round((time.perf_counter() - started_at) * 1000.0, 3)
        database_diagnostics: dict[str, object] = {
            "queryCount": database_metrics.query_count,
            "sqlTimeMs": round(database_metrics.sql_time_s * 1000.0, 3),
            "wallTimeMs": wall_time_ms,
        }
        logger.info(
            "MCP tool diagnostic tool=%s query_count=%d sql_time_ms=%.3f wall_time_ms=%.3f status=%s",
            tool_name,
            database_metrics.query_count,
            database_metrics.sql_time_s * 1000.0,
            wall_time_ms,
            "error" if error is not None else "ok",
        )
        if isinstance(error, ValueError):
            return _mcp_error(rpc_id, -32602, str(error))
        if error is not None:
            return _mcp_error(rpc_id, -32000, f"{type(error).__name__}: {error}")
        return _mcp_result(
            rpc_id,
            _mcp_tool_result(
                str(tool_name),
                arguments,
                result,
                chatgpt_app=chatgpt_app,
                audit_stamp=audit_stamp,
                database_diagnostics=database_diagnostics,
            ),
        )
    if method in {"resources/list", "list_resources"}:
        from thoth_terminal.web.artcraft_mcp import artcraft_resource
        if chatgpt_app:
            return _mcp_result(rpc_id, {"resources": [_mcp_widget_resource(), _mcp_desk_resource(), artcraft_resource()]})
        resources = [
            artcraft_resource(),
            _mcp_widget_resource(),
            _mcp_desk_resource(),
            {
                "uri": "thoth://sources",
                "name": "Sources",
                "description": "All sources with health metadata.",
                "mimeType": "application/json",
            },
            {
                "uri": "thoth://theses",
                "name": "Theses",
                "description": "Default visible theses in Thoth; archived/duplicate statuses are hidden from this resource.",
                "mimeType": "application/json",
            },
            {
                "uri": "thoth://notes",
                "name": "Notes",
                "description": "All notes in Thoth.",
                "mimeType": "application/json",
            },
            {
                "uri": "thoth://agent-actions",
                "name": "Agent Actions",
                "description": "Open and recent agent follow-up actions.",
                "mimeType": "application/json",
            },
            {
                "uri": "thoth://companies",
                "name": "Companies",
                "description": "All companies in Thoth.",
                "mimeType": "application/json",
            },
            {
                "uri": "thoth://research-reports",
                "name": "Research Reports",
                "description": "Canonical research report containers and latest versions.",
                "mimeType": "application/json",
            },
            {
                "uri": "thoth://research-library",
                "name": "Research Library",
                "description": "Local compiled-memory reports, addenda, and lifecycle metadata.",
                "mimeType": "application/json",
            },
            {
                "uri": "thoth://research-conference-index",
                "name": "Research Conference Index",
                "description": "Conference/year coverage for source-card rails such as ISSCC, OFC, Hot Chips, IEDM, VLSI, ECTC, and OCP.",
                "mimeType": "application/json",
            },
            {
                "uri": "thoth://research-reports/{id}",
                "name": "Research Report",
                "description": "Fetch one research report by id.",
                "mimeType": "application/json",
            },
            {
                "uri": "thoth://research-subjects",
                "name": "Research Subjects",
                "description": "Subjects across companies, technologies, components, and themes.",
                "mimeType": "application/json",
            },
            {
                "uri": "thoth://knowledge-assets",
                "name": "Knowledge Assets",
                "description": "Primers and teardowns linked to research subjects.",
                "mimeType": "application/json",
            },
            {
                "uri": "thoth://knowledge-assets/{id}",
                "name": "Knowledge Asset",
                "description": "Fetch one knowledge asset by id.",
                "mimeType": "application/json",
            },
            {
                "uri": "thoth://context/recent",
                "name": "Recent Context Bundle",
                "description": "Broad recent Thoth context bundle for compression/summaries.",
                "mimeType": "application/json",
            },
            {
                "uri": "thoth://discord/agent-settings",
                "name": "Discord Agent Settings",
                "description": "Per-guild agent/compression settings for the configured Discord guild.",
                "mimeType": "application/json",
            },
            {
                "uri": "thoth://documents/{id}",
                "name": "Document",
                "description": "Fetch a document by id.",
                "mimeType": "application/json",
            },
        ]
        return _mcp_result(rpc_id, {"resources": resources})
    if method in {"resources/templates/list", "list_resource_templates"}:
        return _mcp_result(rpc_id, {"resourceTemplates": []})
    if method in {"resources/read", "read_resource"}:
        if not isinstance(params, dict):
            return _mcp_error(rpc_id, -32602, "Invalid params")
        uri = str(
            params.get("uri")
            or params.get("resource")
            or params.get("resourceUri")
            or params.get("path")
            or ""
        )
        from thoth_terminal.web.artcraft_mcp import ARTCRAFT_UI_URI, artcraft_resource_contents
        if uri == ARTCRAFT_UI_URI:
            return _mcp_result(rpc_id, artcraft_resource_contents())
        if uri == _MCP_WIDGET_RESOURCE_URI:
            return _mcp_result(rpc_id, _mcp_widget_contents())
        if uri == _MCP_DESK_RESOURCE_URI:
            return _mcp_result(rpc_id, _mcp_desk_contents())
        if chatgpt_app:
            return _mcp_error(
                rpc_id,
                -32602,
                f"Unknown resource: {uri}. The ChatGPT app exposes its Terminal and research desk widgets; use tools for normal reads and writes.",
            )
        if uri == "thoth://sources":
            result = _mcp_tool_list_sources({})
        elif uri == "thoth://theses":
            result = _mcp_tool_list_theses({})
        elif uri == "thoth://notes":
            result = _mcp_tool_list_notes({})
        elif uri == "thoth://agent-actions":
            result = _mcp_tool_list_agent_actions({})
        elif uri == "thoth://companies":
            result = _mcp_tool_list_companies({})
        elif uri == "thoth://research-reports":
            result = _mcp_tool_list_research_reports({})
        elif uri == "thoth://research-library":
            result = _mcp_tool_list_research_library({})
        elif uri == "thoth://research-conference-index":
            result = _mcp_tool_list_research_conference_index({})
        elif uri == "thoth://research-subjects":
            result = _mcp_tool_list_research_subjects({})
        elif uri == "thoth://knowledge-assets":
            result = _mcp_tool_list_knowledge_assets({"published_only": True})
        elif uri == "thoth://context/recent":
            default_guild = getattr(settings, "discord_guild_id", None)
            _, eff_cfg = _agent_settings_for_guild(int(default_guild) if default_guild else None)
            result = _mcp_tool_get_recent_context_bundle(
                {
                    "guild_id": default_guild,
                    "lookback_hours": eff_cfg.get("context_default_lookback_hours"),
                    "include_sections": eff_cfg.get("default_include_sections"),
                }
            )
        elif uri == "thoth://discord/agent-settings":
            default_guild = getattr(settings, "discord_guild_id", None)
            if default_guild is None:
                result = {"error": "THOTH_DISCORD_GUILD_ID is not configured"}
            else:
                result = _mcp_tool_discord_agent_settings_get({"guild_id": int(default_guild)})
        elif uri.startswith("thoth://research-reports/"):
            report_id = uri.split("thoth://research-reports/", 1)[1]
            result = _mcp_tool_get_research_report({"report_id": report_id})
        elif uri.startswith("thoth://knowledge-assets/"):
            asset_id = uri.split("thoth://knowledge-assets/", 1)[1]
            result = _mcp_tool_get_knowledge_asset({"id": asset_id})
        elif uri.startswith("thoth://documents/"):
            doc_id = uri.split("thoth://documents/", 1)[1]
            result = _mcp_tool_get_document({"id": doc_id})
        else:
            return _mcp_error(
                rpc_id,
                -32602,
                f"Unknown resource: {uri}. Call `resources/list` to inspect valid URIs or use `thoth_help` for the normal tool workflow.",
            )
        payload = json.dumps(result, ensure_ascii=False)
        return _mcp_result(
            rpc_id,
            {"contents": [{"uri": uri, "mimeType": "application/json", "text": payload}]},
        )
    if method == "ping":
        return _mcp_result(rpc_id, {})
    return _mcp_error(rpc_id, -32601, f"Method not found: {method}")


def _mcp_http_message_parallel_safe(
    msg: object,
    *,
    chatgpt_app: bool,
    chatgpt_access_mode: str,
    tool_profile: str,
) -> bool:
    """Return true only for an independently safe local read call."""

    if not using_postgres() or not isinstance(msg, dict):
        return False
    # Notifications have no response slot and can carry initialization or
    # other side effects, so keep them on the serialized path.
    if "id" not in msg or msg.get("jsonrpc") != "2.0":
        return False
    if str(msg.get("method") or "") not in {"tools/call", "call_tool"}:
        return False
    params = msg.get("params")
    if not isinstance(params, dict):
        return False
    requested_name = (
        params.get("name")
        or params.get("tool")
        or params.get("tool_name")
        or params.get("toolName")
    )
    arguments = params.get("arguments") or params.get("args") or {}
    if not requested_name or not isinstance(arguments, dict):
        return False
    tool_name = _mcp_resolve_tool_name(
        requested_name,
        chatgpt_app=chatgpt_app,
        access_mode=chatgpt_access_mode,
    )
    if not tool_name:
        return False
    if chatgpt_app and not _mcp_chatgpt_app_tool_allowed(tool_name, access_mode=chatgpt_access_mode):
        return False
    direct_names = _mcp_profile_direct_names(
        tool_profile=tool_profile,
        chatgpt_app=chatgpt_app,
        access_mode=chatgpt_access_mode,
    )
    if tool_name not in direct_names:
        return False
    capability = capability_for_tool(tool_name)
    return (
        capability.known
        and capability.read_only
        and capability.batch_safe
        and capability.parallel_safe
        and capability.bounded
        and not capability.open_world
        and is_parallel_safe_tool(tool_name)
    )


async def _mcp_http_invoke_message(
    msg: dict[str, object],
    *,
    chatgpt_app: bool,
    chatgpt_access_mode: str,
    tool_profile: str,
) -> dict[str, object] | None:
    """Keep synchronous handlers and their DB sessions off the event loop."""

    try:
        return await asyncio.to_thread(
            _mcp_handle_message,
            msg,
            chatgpt_app=chatgpt_app,
            chatgpt_access_mode=chatgpt_access_mode,
            tool_profile=tool_profile,
        )
    except Exception as exc:
        logger.exception("MCP request handler failed method=%s", msg.get("method"))
        return _mcp_error(msg.get("id"), -32000, f"{type(exc).__name__}: {exc}")


async def _mcp_http_invoke_bounded(
    msg: dict[str, object],
    semaphore: asyncio.Semaphore,
    *,
    chatgpt_app: bool,
    chatgpt_access_mode: str,
    tool_profile: str,
) -> dict[str, object] | None:
    async with semaphore:
        return await _mcp_http_invoke_message(
            msg,
            chatgpt_app=chatgpt_app,
            chatgpt_access_mode=chatgpt_access_mode,
            tool_profile=tool_profile,
        )


def _mcp_http_protocol_version(request: Request, payload: object) -> str | None:
    """Return the validated wire version, or ``None`` for a bad request.

    Initialize negotiates from the JSON-RPC params.  Every later request must
    carry the protocol header; this prevents a client from accidentally using
    legacy JSON-RPC batching against the current endpoint.
    """

    header = str(request.headers.get("mcp-protocol-version") or "").strip()
    method = str(payload.get("method") or "") if isinstance(payload, dict) else ""
    if method == "initialize":
        if header and header not in MCP_SUPPORTED_PROTOCOL_VERSIONS:
            return None
        params = payload.get("params") if isinstance(payload, dict) else None
        requested = params.get("protocolVersion") if isinstance(params, dict) else None
        return _mcp_negotiate_protocol(requested)
    if header == MCP_LEGACY_PROTOCOL_VERSION:
        return MCP_LEGACY_PROTOCOL_VERSION
    if header == MCP_CURRENT_PROTOCOL_VERSION:
        return MCP_CURRENT_PROTOCOL_VERSION
    return None


async def _mcp_http_get_endpoint(
    request: Request,
    *,
    chatgpt_app: bool,
    chatgpt_access_mode: str,
) -> Response:
    """Handle the optional Streamable HTTP GET leg.

    Thoth does not have a durable server-to-client event stream, so it returns
    the protocol-defined 405 instead of pretending that a one-shot stream is a
    subscription.  Clients can continue using POST for requests and SSE
    responses.
    """

    if not _mcp_origin_allowed(request):
        return JSONResponse({"error": "Origin is not allowed"}, status_code=403)
    if not chatgpt_app and not _mcp_require_auth(request):
        return JSONResponse(
            {"error": "Unauthorized"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not _mcp_accepts(request, "text/event-stream"):
        return JSONResponse(
            {"error": "GET requires Accept: text/event-stream"},
            status_code=406,
            headers={"MCP-Protocol-Version": MCP_CURRENT_PROTOCOL_VERSION},
        )
    requested_version = str(request.headers.get("mcp-protocol-version") or "").strip()
    if requested_version and requested_version not in MCP_SUPPORTED_PROTOCOL_VERSIONS:
        return JSONResponse(
            {"error": "Unsupported MCP-Protocol-Version"},
            status_code=400,
            headers={"MCP-Protocol-Version": MCP_CURRENT_PROTOCOL_VERSION},
        )
    return Response(
        status_code=405,
        headers={
            "Allow": "POST",
            "MCP-Protocol-Version": MCP_CURRENT_PROTOCOL_VERSION,
        },
    )


async def _mcp_http_endpoint(
    request: Request,
    *,
    chatgpt_app: bool = False,
    chatgpt_access_mode: str = "write",
) -> Response:
    _refresh_context()
    if request.method.upper() == "GET":
        return await _mcp_http_get_endpoint(
            request,
            chatgpt_app=chatgpt_app,
            chatgpt_access_mode=chatgpt_access_mode,
        )
    if request.method.upper() != "POST":
        return Response(status_code=405, headers={"Allow": "POST"})
    if not _mcp_origin_allowed(request):
        return JSONResponse({"error": "Origin is not allowed"}, status_code=403)
    if not _mcp_accepts(request, "application/json", "text/event-stream"):
        return JSONResponse(
            {"error": "POST requires Accept: application/json, text/event-stream"},
            status_code=406,
        )
    default_profile = str(
        getattr(
            settings,
            "chatgpt_app_tool_profile" if chatgpt_app else "mcp_tool_profile",
            "analyst_v1" if chatgpt_app else "full",
        )
        or ("analyst_v1" if chatgpt_app else "full")
    )
    requested_profile = (
        request.query_params.get("tool_profile")
        or request.headers.get("x-thoth-tool-profile")
        or default_profile
    )
    auth_scope = "operator" if chatgpt_app else _mcp_auth_scope(request)
    if auth_scope is None:
        return JSONResponse(
            {"error": "Unauthorized"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
    tool_profile = (
        "worldview_v1" if auth_scope == "worldview_v1"
        else "operator_agent_v1" if auth_scope == "operator_agent_v1"
        else _mcp_normalize_tool_profile(requested_profile, default=default_profile)
    )

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    protocol_version = _mcp_http_protocol_version(request, payload)
    if protocol_version is None:
        return JSONResponse(
            {"error": "Missing or unsupported MCP-Protocol-Version"},
            status_code=400,
        )
    response_headers = {"MCP-Protocol-Version": protocol_version}

    if isinstance(payload, list):
        if protocol_version != MCP_LEGACY_PROTOCOL_VERSION:
            return JSONResponse(
                {"error": "JSON-RPC batch arrays are only supported for the legacy MCP protocol"},
                status_code=400,
                headers=response_headers,
            )
        can_parallelize = (
            1 < len(payload) <= _MCP_HTTP_PARALLEL_BATCH_MAX
            and all(
                _mcp_http_message_parallel_safe(
                    msg,
                    chatgpt_app=chatgpt_app,
                    chatgpt_access_mode=chatgpt_access_mode,
                    tool_profile=tool_profile,
                )
                for msg in payload
            )
        )
        if can_parallelize:
            semaphore = asyncio.Semaphore(_MCP_HTTP_PARALLEL_MAX)
            results = await asyncio.gather(
                *[
                    _mcp_http_invoke_bounded(
                        msg,
                        semaphore,
                        chatgpt_app=chatgpt_app,
                        chatgpt_access_mode=chatgpt_access_mode,
                        tool_profile=tool_profile,
                    )
                    for msg in payload
                ]
            )
        else:
            # Mixed/unknown/unsafe batches are deliberately serialized.  This
            # includes initialization, notifications, browser/external tools,
            # mutations, and every SQLite request.
            results = []
            for msg in payload:
                if not isinstance(msg, dict):
                    results.append(_mcp_error(None, -32600, "Invalid Request"))
                    continue
                results.append(
                    await _mcp_http_invoke_message(
                        msg,
                        chatgpt_app=chatgpt_app,
                        chatgpt_access_mode=chatgpt_access_mode,
                        tool_profile=tool_profile,
                    )
                )
        responses = [
            response
            for response in results
            if response is not None and response.get("id") is not None
        ]
        if not responses:
            return Response(status_code=202, headers=response_headers)
        return _mcp_response(request, responses, headers=response_headers)

    if not isinstance(payload, dict):
        return JSONResponse({"error": "Invalid request"}, status_code=400)

    resp = await _mcp_http_invoke_message(
        payload,
        chatgpt_app=chatgpt_app,
        chatgpt_access_mode=chatgpt_access_mode,
        tool_profile=tool_profile,
    )
    if resp is None or resp.get("id") is None:
        return Response(status_code=202, headers=response_headers)
    return _mcp_response(request, resp, headers=response_headers)


@router.post("/mcp")
@router.get("/mcp")
async def mcp_endpoint(request: Request) -> Response:
    return await _mcp_http_endpoint(request)


@router.post("/mcp/app")
@router.post("/mcp/app/{app_token}")
@router.get("/mcp/app")
@router.get("/mcp/app/{app_token}")
async def mcp_chatgpt_app_endpoint(request: Request, app_token: str | None = None) -> Response:
    expected = _mcp_chatgpt_app_token()
    if not expected:
        return JSONResponse({"error": "Not found"}, status_code=404)
    provided = str(app_token or "").strip()
    if not provided or not secrets.compare_digest(provided, expected):
        return JSONResponse({"error": "Not found"}, status_code=404)
    return await _mcp_http_endpoint(request, chatgpt_app=True, chatgpt_access_mode="write")


@router.post("/mcp/app/read-only")
@router.post("/mcp/app/read-only/{app_token}")
@router.get("/mcp/app/read-only")
@router.get("/mcp/app/read-only/{app_token}")
async def mcp_chatgpt_app_read_only_endpoint(request: Request, app_token: str | None = None) -> Response:
    expected = _mcp_chatgpt_app_read_only_token()
    if not expected:
        return JSONResponse({"error": "Not found"}, status_code=404)
    provided = str(app_token or "").strip()
    if not provided or not secrets.compare_digest(provided, expected):
        return JSONResponse({"error": "Not found"}, status_code=404)
    return await _mcp_http_endpoint(request, chatgpt_app=True, chatgpt_access_mode="read_only")


finalize_mcp_tool_modules(globals())
