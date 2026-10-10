from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any, Callable, Iterable

from sqlalchemy.orm import Session

from thoth_terminal import __version__
from thoth_terminal.agent.llm_client import (
    LLM_RAW_RESPONSE_KEY,
    LLM_CITATIONS_KEY,
    LLM_TOOL_CALLS_KEY,
    active_llm_model,
    chat_json_auto,
    chat_text_auto,
    llm_metadata,
    openai_programmatic_tool_calling_enabled,
    openai_responses_tool_loop,
    prepare_chat_messages,
    provider_request_profile,
)
from thoth_terminal.agent_inbox_service import (
    AgentInboxClaim,
    acknowledge_agent_inbox_claim,
    claim_agent_inbox_items,
    release_agent_inbox_claim,
    render_agent_inbox_claim,
)
from thoth_terminal.agent.local_llm import ChatMessage
from thoth_terminal.db import utcnow
from thoth_terminal.models import AgentRequestManifest, AgentRequestOutcome
from thoth_terminal.runtime_env import BOOT_ID
from thoth_terminal.runtime_info import get_git_sha
from thoth_terminal.web.mcp_tool_registry import (
    capability_metadata,
    is_parallel_safe_tool,
    is_ptc_eligible_tool,
)


_MANIFEST_TEXT_MAX_CHARS = 16_000
_MANIFEST_JSON_MAX_CHARS = 64_000
_MANIFEST_SECRET_RE = re.compile(
    r"(?i)(bearer\s+|(?:api[_-]?key|token|secret|password)\s*[:=]\s*|(?:sk|rk|pk)-)[^\s,;\"']+"
)
_MANIFEST_SECRET_KEY_PARTS = ("secret", "password", "api_key", "apikey", "authorization", "cookie")
_MANIFEST_EXACT_SECRET_KEYS = {
    "token",
    "access_token",
    "refresh_token",
    "id_token",
    "auth_token",
    "bearer_token",
    "token_value",
    "token_secret",
}


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: object) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _clean_dict(value: dict[str, Any] | None) -> dict[str, Any]:
    return {str(key): item for key, item in dict(value or {}).items() if item is not None}


def _bounded_manifest_value(value: object, *, max_chars: int = 12000) -> object:
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


def _bounded_manifest_text(value: object, *, max_chars: int = _MANIFEST_TEXT_MAX_CHARS) -> str:
    text = _redact_manifest_text(str(value or ""))
    limit = max(128, int(max_chars))
    if len(text) <= limit:
        return text
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    preview = text[: max(32, limit - 120)]
    return f"[truncated sha256={digest} chars={len(text)} preview={preview}]"


def _redact_manifest_text(value: str) -> str:
    return _MANIFEST_SECRET_RE.sub(lambda match: f"{match.group(1)}[redacted]", str(value or ""))


def _redact_manifest_value(value: object, *, depth: int = 0) -> object:
    if depth > 8:
        return "[redacted depth]"
    if isinstance(value, str):
        return _redact_manifest_text(value)
    if isinstance(value, dict):
        out: dict[str, object] = {}
        for key, item in value.items():
            key_text = str(key)
            normalized_key = key_text.lower().replace("-", "_")
            if normalized_key in _MANIFEST_EXACT_SECRET_KEYS or any(
                part in normalized_key for part in _MANIFEST_SECRET_KEY_PARTS
            ):
                out[key_text] = "[redacted]"
            else:
                out[key_text] = _redact_manifest_value(item, depth=depth + 1)
        return out
    if isinstance(value, list):
        return [_redact_manifest_value(item, depth=depth + 1) for item in value]
    if isinstance(value, tuple):
        return [_redact_manifest_value(item, depth=depth + 1) for item in value]
    return value


def _bounded_manifest_messages(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    bounded = [
        {
            "role": _bounded_manifest_text(row.get("role"), max_chars=128),
            "content": _bounded_manifest_text(row.get("content"), max_chars=_MANIFEST_TEXT_MAX_CHARS),
        }
        for row in messages
    ]
    encoded = _canonical_json(bounded)
    if len(encoded) <= _MANIFEST_JSON_MAX_CHARS:
        return bounded
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    marker = {
        "role": "system",
        "content": f"[messages truncated sha256={digest} chars={len(encoded)}]",
    }
    kept: list[dict[str, str]] = []
    for row in bounded:
        candidate = _canonical_json([*kept, row, marker])
        if len(candidate) > _MANIFEST_JSON_MAX_CHARS:
            break
        kept.append(row)
    return [*kept, marker]


def _bounded_manifest_list(value: list[object], *, max_chars: int = _MANIFEST_JSON_MAX_CHARS) -> list[object]:
    encoded = _canonical_json(value)
    if len(encoded) <= max_chars:
        return value
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    marker = {"_truncated": True, "sha256": digest, "chars": len(encoded)}
    kept: list[object] = []
    for item in value:
        if len(_canonical_json([*kept, item, marker])) > max_chars:
            break
        kept.append(item)
    return [*kept, marker]


def _manifest_tool_definition(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    allowed = {
        "type",
        "name",
        "title",
        "description",
        "parameters",
        "inputSchema",
        "output_schema",
        "allowed_callers",
        "strict",
        "defer_loading",
    }
    item = _redact_manifest_value({str(key): value[key] for key in allowed if key in value})
    if not isinstance(item, dict):
        return None
    name = str(item.get("name") or "").strip()
    if not name:
        return None
    item["capability"] = capability_metadata(name)
    bounded = _bounded_manifest_value(item, max_chars=12000)
    return dict(bounded) if isinstance(bounded, dict) else item


def _manifest_tooling(
    *,
    tool_names: list[str] | None,
    tool_schemas: list[dict[str, Any]] | None,
    capability_profile: str | None,
    tool_call_records: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    definitions: list[dict[str, Any]] = []
    for value in list(tool_schemas or [])[:64]:
        definition = _manifest_tool_definition(value)
        if definition is not None:
            definitions.append(definition)
    calls = [
        _redact_manifest_value(dict(row))
        for row in list(tool_call_records or [])[:128]
        if isinstance(row, dict)
    ]
    profile = str(capability_profile or "").strip() or None
    if not definitions and not calls and profile is None and not tool_names:
        return None
    return {
        "profile_version": profile,
        "tool_names": sorted({str(name).strip() for name in tool_names or [] if str(name).strip()}),
        "tools": definitions,
        "calls": [_bounded_manifest_value(row, max_chars=4000) for row in calls],
    }


def create_agent_request_manifest(
    session: Session,
    *,
    run_kind: str,
    run_id: int | None,
    task_kind: str | None,
    provider: str,
    requested_model: str,
    endpoint_kind: str,
    messages: Iterable[ChatMessage],
    context_sections: list[dict[str, Any]] | None = None,
    tool_names: list[str] | None = None,
    tool_schemas: list[dict[str, Any]] | None = None,
    permission_profile: str | None = None,
    source_refs: dict[str, Any] | None = None,
    request_parameters: dict[str, Any] | None = None,
    prompt_cache_key: str | None = None,
    prompt_cache_retention: str | None = None,
    capability_profile: str | None = None,
    tool_call_records: list[dict[str, Any]] | None = None,
) -> AgentRequestManifest:
    """Persist the exact model-visible input before dispatch.

    The manifest intentionally excludes credentials and transport headers. Callers
    must pass only public request parameters; bearer tokens never belong here.
    """

    normalized_messages = _bounded_manifest_messages(
        [
            {
                "role": _redact_manifest_text(str(message.role or "")),
                "content": _redact_manifest_text(str(message.content or "")),
            }
            for message in messages
        ]
    )
    system_text = next(
        (
            str(message["content"])
            for message in normalized_messages
            if str(message.get("role") or "").lower() == "system"
        ),
        None,
    )
    schema_rows = [
        bounded
        for item in list(tool_schemas or [])[:64]
        if isinstance(item, dict)
        for bounded in [_bounded_manifest_value(_redact_manifest_value(dict(item)), max_chars=12_000)]
        if isinstance(bounded, dict)
    ]
    schemas = _bounded_manifest_list(schema_rows)
    # Preserve explicit nulls: for request limits, null means deliberately
    # unbounded/provider-default and is materially different from an omitted
    # field whose value cannot be reconstructed.
    redacted_parameters = _redact_manifest_value(dict(request_parameters or {}))
    parameters = {
        str(key): _bounded_manifest_value(item, max_chars=12_000)
        for key, item in (redacted_parameters.items() if isinstance(redacted_parameters, dict) else ())
    }
    tooling = _manifest_tooling(
        tool_names=tool_names,
        tool_schemas=tool_schemas,
        capability_profile=capability_profile,
        tool_call_records=tool_call_records,
    )
    if tooling is not None:
        parameters["tooling"] = tooling
    bounded_context_sections = _bounded_manifest_list(
        _redact_manifest_value(list(context_sections or [])),
        max_chars=_MANIFEST_JSON_MAX_CHARS,
    )
    bounded_source_refs = _bounded_manifest_value(
        _redact_manifest_value(_clean_dict(source_refs)),
        max_chars=32_000,
    )
    bounded_parameters = _bounded_manifest_value(
        parameters,
        max_chars=_MANIFEST_JSON_MAX_CHARS,
    )
    if not isinstance(bounded_parameters, dict):
        bounded_parameters = {"_truncated": bounded_parameters}
    envelope = {
        "run_kind": str(run_kind or "agent").strip() or "agent",
        "run_id": int(run_id) if run_id is not None else None,
        "task_kind": str(task_kind or "").strip() or None,
        "provider": str(provider or "").strip().lower(),
        "requested_model": str(requested_model or "").strip(),
        "endpoint_kind": str(endpoint_kind or "").strip().lower(),
        "messages": normalized_messages,
        "context_sections": bounded_context_sections,
        "tool_names": sorted({str(value).strip() for value in tool_names or [] if str(value).strip()}),
        "tool_schemas": schemas,
        "permission_profile": str(permission_profile or "").strip() or None,
        "source_refs": bounded_source_refs,
        "request_parameters": bounded_parameters,
        "prompt_cache_key": str(prompt_cache_key or "").strip() or None,
        "prompt_cache_retention": str(prompt_cache_retention or "").strip() or None,
        "application_version": __version__,
        "git_sha": get_git_sha(),
        "runtime_boot_id": BOOT_ID,
    }
    row = AgentRequestManifest(
        request_key=uuid.uuid4().hex,
        run_kind=envelope["run_kind"],
        run_id=envelope["run_id"],
        task_kind=envelope["task_kind"],
        provider=envelope["provider"],
        requested_model=envelope["requested_model"],
        endpoint_kind=envelope["endpoint_kind"],
        system_prompt_sha256=_sha256(system_text) if system_text is not None else None,
        system_prompt_text=system_text,
        messages_json=normalized_messages,
        context_sections_json=bounded_context_sections,
        tool_names_json=envelope["tool_names"],
        tool_schema_sha256=_sha256(_canonical_json(schemas)) if schemas else None,
        permission_profile=envelope["permission_profile"],
        source_refs_json=envelope["source_refs"],
        request_parameters_json=bounded_parameters,
        prompt_cache_key=envelope["prompt_cache_key"],
        prompt_cache_retention=envelope["prompt_cache_retention"],
        application_version=__version__,
        git_sha=envelope["git_sha"],
        runtime_boot_id=BOOT_ID,
        request_sha256=_sha256(_canonical_json(envelope)),
        created_at=utcnow(),
    )
    session.add(row)
    session.flush()
    return row


def record_agent_request_outcome(
    session: Session,
    manifest: AgentRequestManifest | int,
    *,
    status: str,
    response_provider: str | None = None,
    response_model: str | None = None,
    usage: dict[str, Any] | None = None,
    response_text: str | None = None,
    error: BaseException | None = None,
) -> AgentRequestOutcome:
    manifest_id = int(manifest.id if isinstance(manifest, AgentRequestManifest) else manifest)
    if session.query(AgentRequestOutcome.id).filter_by(manifest_id=manifest_id).first() is not None:
        raise ValueError(f"Agent request manifest #{manifest_id} already has an outcome.")
    row = AgentRequestOutcome(
        manifest_id=manifest_id,
        status=str(status or "unknown").strip().lower() or "unknown",
        response_provider=str(response_provider or "").strip().lower() or None,
        response_model=str(response_model or "").strip() or None,
        usage_json=_clean_dict(usage),
        response_sha256=_sha256(response_text) if response_text is not None else None,
        error_type=type(error).__name__ if error is not None else None,
        error_message=str(error)[:4000] if error is not None else None,
        created_at=utcnow(),
    )
    session.add(row)
    session.flush()
    return row


def serialize_agent_request_manifest(row: AgentRequestManifest) -> dict[str, Any]:
    payload = {
        "id": int(row.id),
        "request_key": row.request_key,
        "run_kind": row.run_kind,
        "run_id": row.run_id,
        "task_kind": row.task_kind,
        "provider": row.provider,
        "requested_model": row.requested_model,
        "endpoint_kind": row.endpoint_kind,
        "system_prompt_sha256": row.system_prompt_sha256,
        "message_count": len(row.messages_json or []),
        "tool_names": list(row.tool_names_json or []),
        "permission_profile": row.permission_profile,
        "prompt_cache_key": row.prompt_cache_key,
        "prompt_cache_retention": row.prompt_cache_retention,
        "application_version": row.application_version,
        "git_sha": row.git_sha,
        "runtime_boot_id": row.runtime_boot_id,
        "request_sha256": row.request_sha256,
        "created_at": row.created_at.isoformat(),
    }
    if row.outcome is not None:
        payload["outcome"] = serialize_agent_request_outcome(row.outcome)
    return payload


def serialize_agent_request_outcome(row: AgentRequestOutcome) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "manifest_id": int(row.manifest_id),
        "status": row.status,
        "response_provider": row.response_provider,
        "response_model": row.response_model,
        "usage": dict(row.usage_json or {}),
        "response_sha256": row.response_sha256,
        "error_type": row.error_type,
        "error_message": row.error_message,
        "created_at": row.created_at.isoformat(),
    }


def get_agent_request_manifest(
    session: Session,
    manifest_id: int,
    *,
    include_payload: bool = True,
) -> dict[str, Any] | None:
    row = session.get(AgentRequestManifest, int(manifest_id))
    if row is None:
        return None
    payload = serialize_agent_request_manifest(row)
    if include_payload:
        payload.update(
            {
                "system_prompt_text": row.system_prompt_text,
                "messages": list(row.messages_json or []),
                "context_sections": list(row.context_sections_json or []),
                "source_refs": dict(row.source_refs_json or {}),
                "request_parameters": dict(row.request_parameters_json or {}),
            }
        )
    return payload


def list_agent_request_manifests(
    session: Session,
    *,
    run_kind: str | None = None,
    run_id: int | None = None,
    task_kind: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    query = session.query(AgentRequestManifest)
    if str(run_kind or "").strip():
        query = query.filter(AgentRequestManifest.run_kind == str(run_kind).strip())
    if run_id is not None:
        query = query.filter(AgentRequestManifest.run_id == int(run_id))
    if str(task_kind or "").strip():
        query = query.filter(AgentRequestManifest.task_kind == str(task_kind).strip())
    if str(status or "").strip():
        query = query.join(AgentRequestOutcome).filter(
            AgentRequestOutcome.status == str(status).strip().lower()
        )
    rows = query.order_by(AgentRequestManifest.id.desc()).limit(max(1, min(int(limit or 100), 500))).all()
    return [serialize_agent_request_manifest(row) for row in rows]


def _resolved_model(provider: str, model: str | None) -> str:
    provider_v = str(provider or "").strip().lower()
    return str(active_llm_model(provider_v, model) or model or "provider-default").strip()


def _response_text(result: dict[str, Any]) -> str:
    raw = result.get(LLM_RAW_RESPONSE_KEY)
    if isinstance(raw, str) and raw:
        return raw
    return _canonical_json(
        {
            str(key): value
            for key, value in result.items()
            if str(key) not in {
                LLM_RAW_RESPONSE_KEY,
                LLM_TOOL_CALLS_KEY,
                LLM_CITATIONS_KEY,
                "_thoth_responses_output",
                "_thoth_program_outputs",
            }
        }
    )


def _record_dispatch_outcome(
    session: Session,
    manifest_id: int,
    *,
    result: dict[str, Any] | None = None,
    error: BaseException | None = None,
) -> None:
    metadata = llm_metadata(result or {})
    usage_payload: dict[str, Any] = dict(metadata)
    if isinstance(result, dict):
        calls = result.get(LLM_TOOL_CALLS_KEY)
        if isinstance(calls, list):
            usage_payload["tool_outcomes"] = [dict(row) for row in calls[:128] if isinstance(row, dict)]
        citations = result.get(LLM_CITATIONS_KEY)
        if isinstance(citations, list):
            usage_payload["citations"] = list(citations[:64])
    record_agent_request_outcome(
        session,
        manifest_id,
        status="error" if error is not None else "success",
        response_provider=str(metadata.get("provider") or "").strip() or None,
        response_model=str(metadata.get("model") or metadata.get("response_model") or "").strip() or None,
        usage=usage_payload,
        response_text=_response_text(result or {}) if result is not None else None,
        error=error,
    )
    session.commit()


def manifested_chat_json(
    session: Session,
    *,
    run_kind: str,
    run_id: int | None,
    task_kind: str,
    messages: list[ChatMessage],
    provider: str,
    model: str | None = None,
    endpoint: str | None = None,
    timeout_s: float = 60.0,
    max_input_chars: int | None = 24000,
    max_output_tokens: int | None = None,
    prompt_cache_key: str | None = None,
    prompt_cache_retention: str | None = None,
    permission_profile: str | None = None,
    context_sections: list[dict[str, Any]] | None = None,
    source_refs: dict[str, Any] | None = None,
    preserve_raw_text: bool = False,
    deepseek_thinking: bool | None = None,
    deepseek_reasoning_effort: str = "max",
    inbox_boundary: str | None = None,
) -> dict[str, Any]:
    """Persist an immutable request and terminal outcome around one JSON call.

    This intentionally commits the caller session before network dispatch. Agent
    workflows must therefore finish their pre-call state changes first; the
    provider wait never holds an open business transaction.
    """

    provider_v = str(provider or "").strip().lower()
    inbox_claim: AgentInboxClaim | None = None
    request_messages = list(messages)
    source_refs_v = dict(source_refs or {})
    if inbox_boundary is not None:
        if run_id is None:
            raise ValueError("inbox_boundary requires run_id")
        inbox_claim = claim_agent_inbox_items(
            session,
            target_kind=run_kind,
            target_id=int(run_id),
            boundary=inbox_boundary,
        )
        if inbox_claim is not None:
            request_messages.append(ChatMessage(role="user", content=render_agent_inbox_claim(inbox_claim)))
            source_refs_v["agent_inbox"] = {
                "boundary": str(inbox_boundary),
                "item_ids": inbox_claim.item_ids,
                "claim_token": inbox_claim.token,
            }
    prepared = prepare_chat_messages(request_messages, max_input_chars=max_input_chars)
    profile = provider_request_profile(
        provider_v,
        prompt_cache_key=prompt_cache_key,
        prompt_cache_retention=prompt_cache_retention,
    )
    endpoint_kind = "local_chat_completions" if endpoint else str(profile.get("endpoint_kind") or "chat_completions")
    manifest = create_agent_request_manifest(
        session,
        run_kind=run_kind,
        run_id=run_id,
        task_kind=task_kind,
        provider=provider_v,
        requested_model=_resolved_model(provider_v, model),
        endpoint_kind=endpoint_kind,
        messages=prepared,
        context_sections=context_sections,
        tool_names=[],
        tool_schemas=[],
        permission_profile=permission_profile,
        source_refs=source_refs_v,
        request_parameters={
            "timeout_s": float(timeout_s),
            "max_input_chars": max_input_chars,
            "max_output_tokens": max_output_tokens,
            "response_format": "json",
            "store": profile.get("store"),
            "deepseek_thinking": deepseek_thinking,
            "deepseek_reasoning_effort": deepseek_reasoning_effort,
        },
        prompt_cache_key=str(profile.get("prompt_cache_key") or "").strip() or None,
        prompt_cache_retention=str(profile.get("prompt_cache_retention") or "").strip() or None,
    )
    manifest_id = int(manifest.id)
    session.commit()
    try:
        result = chat_json_auto(
            messages=prepared,
            endpoint=endpoint,
            model=model,
            timeout_s=timeout_s,
            max_input_chars=None,
            max_output_tokens=max_output_tokens,
            provider=provider_v,
            prompt_cache_key=prompt_cache_key,
            prompt_cache_retention=prompt_cache_retention,
            deepseek_thinking=deepseek_thinking,
            deepseek_reasoning_effort=deepseek_reasoning_effort,
            preserve_raw_text=preserve_raw_text,
        )
    except Exception as exc:
        _record_dispatch_outcome(session, manifest_id, error=exc)
        if inbox_claim is not None:
            release_agent_inbox_claim(session, inbox_claim, error=exc)
            session.commit()
        raise
    _record_dispatch_outcome(session, manifest_id, result=result)
    if inbox_claim is not None:
        acknowledge_agent_inbox_claim(session, inbox_claim)
        session.commit()
    return result


def manifested_responses_tools(
    session: Session,
    *,
    run_kind: str,
    run_id: int | None,
    task_kind: str,
    messages: list[ChatMessage],
    tool_definitions: list[dict[str, Any]],
    tool_executor: Callable[[str, dict[str, Any]], object],
    provider: str = "openai",
    model: str | None = None,
    timeout_s: float = 60.0,
    max_input_chars: int | None = 24000,
    max_output_tokens: int | None = None,
    permission_profile: str | None = None,
    capability_profile: str = "analyst_v2",
    tool_route: str = "direct",
    json_output: bool = False,
    prompt_cache_key: str | None = None,
    store: bool = False,
    max_turns: int = 6,
    max_tool_calls: int = 32,
    enabled: bool | None = None,
    inbox_boundary: str | None = None,
) -> dict[str, Any]:
    """Manifest and execute one explicit Responses direct/parallel/PTC route."""

    provider_v = str(provider or "openai").strip().lower()
    route = str(tool_route or "direct").strip().lower()
    if route not in {"direct", "parallel_direct", "ptc"}:
        raise ValueError("tool_route must be direct, parallel_direct, or ptc")
    requested_route = route
    definitions = [dict(row) for row in tool_definitions if isinstance(row, dict)]
    names = [str(row.get("name") or "").strip() for row in definitions if str(row.get("name") or "").strip()]
    if not names:
        raise ValueError("At least one named tool definition is required")
    if route == "ptc" and not openai_programmatic_tool_calling_enabled():
        # Direct Responses is a safe, explicit fallback when the PTC gate is
        # unavailable.  The manifest records both routes so operators can see
        # that the hosted reduction stage was not silently claimed.
        route = "parallel_direct" if all(is_parallel_safe_tool(name) for name in names) else "direct"
        definitions = [
            row for row in definitions if str(row.get("type") or "") != "programmatic_tool_calling"
        ]
    if route == "ptc":
        if provider_v != "openai":
            raise ValueError("Programmatic Tool Calling is only implemented for OpenAI")
        if not all(is_ptc_eligible_tool(name) for name in names):
            raise ValueError("PTC route contains a tool outside the curated read-only allowlist")
    parallel = route == "parallel_direct" and all(is_parallel_safe_tool(name) for name in names)
    if route == "parallel_direct" and not parallel:
        raise ValueError("parallel_direct requires explicit bounded parallel-safe read tools")
    inbox_claim: AgentInboxClaim | None = None
    request_messages = list(messages)
    source_refs: dict[str, Any] = {}
    if inbox_boundary is not None:
        if run_id is None:
            raise ValueError("inbox_boundary requires run_id")
        inbox_claim = claim_agent_inbox_items(
            session,
            target_kind=run_kind,
            target_id=int(run_id),
            boundary=inbox_boundary,
        )
        if inbox_claim is not None:
            request_messages.append(ChatMessage(role="user", content=render_agent_inbox_claim(inbox_claim)))
            source_refs["agent_inbox"] = {
                "boundary": str(inbox_boundary),
                "item_ids": inbox_claim.item_ids,
                "claim_token": inbox_claim.token,
            }
    prepared = prepare_chat_messages(request_messages, max_input_chars=max_input_chars)
    profile = provider_request_profile(provider_v, prompt_cache_key=prompt_cache_key)
    manifest = create_agent_request_manifest(
        session,
        run_kind=run_kind,
        run_id=run_id,
        task_kind=task_kind,
        provider=provider_v,
        requested_model=_resolved_model(provider_v, model),
        endpoint_kind="responses" if provider_v == "openai" else str(profile.get("endpoint_kind") or "chat_completions"),
        messages=prepared,
        tool_names=names,
        tool_schemas=definitions,
        permission_profile=permission_profile,
        source_refs=source_refs,
        request_parameters={
            "timeout_s": float(timeout_s),
            "max_input_chars": max_input_chars,
            "max_output_tokens": max_output_tokens,
            "response_format": "json" if json_output else "text",
            "store": bool(store),
            "tool_route": route,
            "requested_tool_route": requested_route,
            "parallel_tool_calls": bool(parallel),
            "programmatic_tool_calling": route == "ptc",
        },
        prompt_cache_key=str(prompt_cache_key or "").strip() or None,
        prompt_cache_retention=str(profile.get("prompt_cache_retention") or "").strip() or None,
        capability_profile=capability_profile,
    )
    manifest_id = int(manifest.id)
    session.commit()
    try:
        result = openai_responses_tool_loop(
            messages=prepared,
            tools=definitions,
            tool_executor=tool_executor,
            model=model,
            timeout_s=timeout_s,
            max_turns=max_turns,
            max_tool_calls=max_tool_calls,
            parallel_tool_calls=parallel,
            programmatic_tool_calling=route == "ptc",
            json_output=json_output,
            max_output_tokens=max_output_tokens,
            prompt_cache_key=prompt_cache_key,
            store=store,
            capability_profile=capability_profile,
            enabled=enabled,
        )
    except Exception as exc:
        _record_dispatch_outcome(session, manifest_id, error=exc)
        if inbox_claim is not None:
            release_agent_inbox_claim(session, inbox_claim, error=exc)
            session.commit()
        raise
    tool_calls = result.get(LLM_TOOL_CALLS_KEY) if isinstance(result, dict) else None
    if isinstance(tool_calls, list):
        # The manifest is immutable, so keep call linkage/outcomes on the
        # append-only outcome while the request retains the exact definitions.
        result.setdefault("_thoth_manifest_id", manifest_id)
    _record_dispatch_outcome(session, manifest_id, result=result)
    if inbox_claim is not None:
        acknowledge_agent_inbox_claim(session, inbox_claim)
        session.commit()
    return result


def manifested_chat_text(
    session: Session,
    *,
    run_kind: str,
    run_id: int | None,
    task_kind: str,
    messages: list[ChatMessage],
    provider: str,
    model: str | None = None,
    timeout_s: float = 60.0,
    max_input_chars: int | None = 24000,
    max_output_tokens: int | None = None,
    prompt_cache_key: str | None = None,
    prompt_cache_retention: str | None = None,
    permission_profile: str | None = None,
    context_sections: list[dict[str, Any]] | None = None,
    source_refs: dict[str, Any] | None = None,
    deepseek_thinking: bool | None = None,
    deepseek_reasoning_effort: str = "max",
    inbox_boundary: str | None = None,
) -> dict[str, Any]:
    """Persist an immutable request and terminal outcome around one text call."""

    provider_v = str(provider or "").strip().lower()
    inbox_claim: AgentInboxClaim | None = None
    request_messages = list(messages)
    source_refs_v = dict(source_refs or {})
    if inbox_boundary is not None:
        if run_id is None:
            raise ValueError("inbox_boundary requires run_id")
        inbox_claim = claim_agent_inbox_items(
            session,
            target_kind=run_kind,
            target_id=int(run_id),
            boundary=inbox_boundary,
        )
        if inbox_claim is not None:
            request_messages.append(ChatMessage(role="user", content=render_agent_inbox_claim(inbox_claim)))
            source_refs_v["agent_inbox"] = {
                "boundary": str(inbox_boundary),
                "item_ids": inbox_claim.item_ids,
                "claim_token": inbox_claim.token,
            }
    prepared = prepare_chat_messages(request_messages, max_input_chars=max_input_chars)
    profile = provider_request_profile(
        provider_v,
        prompt_cache_key=prompt_cache_key,
        prompt_cache_retention=prompt_cache_retention,
    )
    manifest = create_agent_request_manifest(
        session,
        run_kind=run_kind,
        run_id=run_id,
        task_kind=task_kind,
        provider=provider_v,
        requested_model=_resolved_model(provider_v, model),
        endpoint_kind=str(profile.get("endpoint_kind") or "chat_completions"),
        messages=prepared,
        context_sections=context_sections,
        tool_names=[],
        tool_schemas=[],
        permission_profile=permission_profile,
        source_refs=source_refs_v,
        request_parameters={
            "timeout_s": float(timeout_s),
            "max_input_chars": max_input_chars,
            "max_output_tokens": max_output_tokens,
            "response_format": "text",
            "store": profile.get("store"),
            "deepseek_thinking": deepseek_thinking,
            "deepseek_reasoning_effort": deepseek_reasoning_effort,
        },
        prompt_cache_key=str(profile.get("prompt_cache_key") or "").strip() or None,
        prompt_cache_retention=str(profile.get("prompt_cache_retention") or "").strip() or None,
    )
    manifest_id = int(manifest.id)
    session.commit()
    try:
        result = chat_text_auto(
            messages=prepared,
            model=model,
            timeout_s=timeout_s,
            max_input_chars=None,
            max_output_tokens=max_output_tokens,
            provider=provider_v,
            prompt_cache_key=prompt_cache_key,
            prompt_cache_retention=prompt_cache_retention,
            deepseek_thinking=deepseek_thinking,
            deepseek_reasoning_effort=deepseek_reasoning_effort,
        )
    except Exception as exc:
        _record_dispatch_outcome(session, manifest_id, error=exc)
        if inbox_claim is not None:
            release_agent_inbox_claim(session, inbox_claim, error=exc)
            session.commit()
        raise
    _record_dispatch_outcome(session, manifest_id, result=result)
    if inbox_claim is not None:
        acknowledge_agent_inbox_claim(session, inbox_claim)
        session.commit()
    return result
