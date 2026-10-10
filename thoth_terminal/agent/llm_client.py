from __future__ import annotations

import json
import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Iterable

import requests

from thoth_terminal.agent.local_llm import ChatMessage, LocalLlmError, chat_json as local_chat_json
from thoth_terminal.config import settings
from thoth_terminal.local_model_runtime import (
    ensure_local_model_runtime,
    local_model_endpoint,
    local_model_name,
)
from thoth_terminal.web.mcp_tool_registry import (
    OPENAI_AGENT_PROFILE_VERSION,
    capability_for_tool,
    is_parallel_safe_tool,
    is_ptc_eligible_tool,
)


class AgentLlmError(RuntimeError):
    pass


_OPENAI_CHAT_COMPLETIONS_URL = "https://api.openai.com/v1/chat/completions"
_OPENROUTER_CHAT_COMPLETIONS_URL = "https://openrouter.ai/api/v1/chat/completions"
_OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
_DEEPSEEK_CHAT_COMPLETIONS_URL = "https://api.deepseek.com/chat/completions"
_DEEPSEEK_DEFAULT_MODEL = "deepseek-v4-flash"
_NOUS_DEFAULT_BASE_URL = "https://inference-api.nousresearch.com/v1"
_META_DEFAULT_BASE_URL = "https://api.meta.ai/v1"
_META_DEFAULT_MODEL = "muse-spark-1.3-contributor"
_OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
LLM_METADATA_KEY = "_thoth_llm"
LLM_RAW_RESPONSE_KEY = "_thoth_raw_response"
LLM_TOOL_CALLS_KEY = "_thoth_tool_calls"
LLM_CITATIONS_KEY = "_thoth_citations"
LLM_RESPONSES_OUTPUT_KEY = "_thoth_responses_output"
LLM_PROGRAM_OUTPUTS_KEY = "_thoth_program_outputs"


def _usage_int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _usage_float(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _normalize_usage(usage: dict[str, Any]) -> dict[str, Any]:
    def first(*keys: str) -> Any:
        for key in keys:
            if key in usage and usage.get(key) is not None:
                return usage.get(key)
        return None

    prompt_details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    completion_details = usage.get("completion_tokens_details") or usage.get("output_tokens_details") or {}
    if not isinstance(prompt_details, dict):
        prompt_details = {}
    if not isinstance(completion_details, dict):
        completion_details = {}

    input_tokens = _usage_int(first("prompt_tokens", "input_tokens"))
    output_tokens = _usage_int(first("completion_tokens", "output_tokens"))
    cache_hit_tokens = _usage_int(
        first(
            "prompt_cache_hit_tokens",
            "input_cache_hit_tokens",
            "cache_hit_tokens",
            "cache_read_input_tokens",
        )
    )
    if cache_hit_tokens is None:
        cache_hit_tokens = _usage_int(prompt_details.get("cached_tokens"))
    cache_write_tokens = _usage_int(
        first(
            "prompt_cache_write_tokens",
            "input_cache_write_tokens",
            "cache_write_tokens",
            "cache_creation_input_tokens",
        )
    )
    if cache_write_tokens is None:
        cache_write_tokens = _usage_int(prompt_details.get("cache_write_tokens"))
    anthropic_cache_fields = any(
        usage.get(key) is not None for key in ("cache_read_input_tokens", "cache_creation_input_tokens")
    )
    cache_miss_tokens = _usage_int(
        first("prompt_cache_miss_tokens", "input_cache_miss_tokens", "cache_miss_tokens")
    )
    if cache_miss_tokens is None and input_tokens is not None:
        cache_miss_tokens = input_tokens if anthropic_cache_fields else max(0, input_tokens - int(cache_hit_tokens or 0))

    total_tokens = _usage_int(usage.get("total_tokens"))
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
        if anthropic_cache_fields:
            total_tokens += int(cache_hit_tokens or 0) + int(cache_write_tokens or 0)

    effective_input_tokens = input_tokens
    if anthropic_cache_fields and input_tokens is not None:
        effective_input_tokens = input_tokens + int(cache_hit_tokens or 0) + int(cache_write_tokens or 0)
    cache_hit_rate = None
    if effective_input_tokens and cache_hit_tokens is not None:
        cache_hit_rate = round(int(cache_hit_tokens) / int(effective_input_tokens), 6)

    cost = _usage_float(first("cost", "total_cost", "reported_cost_usd"))
    return {
        "input_tokens": input_tokens,
        "effective_input_tokens": effective_input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "reasoning_tokens": _usage_int(completion_details.get("reasoning_tokens")),
        "prompt_cache_hit_tokens": cache_hit_tokens,
        "prompt_cache_miss_tokens": cache_miss_tokens,
        "prompt_cache_write_tokens": cache_write_tokens,
        "cache_hit_rate": cache_hit_rate,
        "reported_cost_usd": cost,
        "usage_reported": True,
    }


def _trim_messages(messages: list[ChatMessage], *, max_chars: int | None) -> list[ChatMessage]:
    if max_chars is None or int(max_chars) <= 0:
        return [ChatMessage(role=str(msg.role), content=msg.content) for msg in messages]
    max_chars = max(1000, int(max_chars or 1000))
    leading_system: ChatMessage | None = None
    rest = list(messages)
    if rest and str(rest[0].role or "").lower() == "system":
        system_content = rest[0].content
        system_budget = max(500, max_chars // 4)
        if isinstance(system_content, list):
            system_content, used = _trim_content_blocks(system_content, system_budget)
        else:
            system_content = str(system_content or "")[:system_budget]
            used = len(system_content)
        leading_system = ChatMessage(role=str(rest[0].role), content=system_content)
        max_chars -= used
        rest = rest[1:]
    out: list[ChatMessage] = []
    remaining = max_chars
    for msg in reversed(rest):
        if remaining <= 0:
            break
        if isinstance(msg.content, list):
            content, used = _trim_content_blocks(msg.content, remaining)
        else:
            content = str(msg.content or "")
            if len(content) > remaining:
                content = content[-remaining:]
            used = len(content)
        out.append(ChatMessage(role=str(msg.role), content=content))
        remaining -= used
    trimmed = list(reversed(out))
    if leading_system is not None:
        return [leading_system, *trimmed]
    return trimmed


def _trim_content_blocks(blocks: list[Any], budget: int) -> tuple[list[dict[str, Any]], int]:
    """Count only text toward the character cap; retain structured image blocks."""

    kept: list[dict[str, Any]] = []
    remaining = max(0, budget)
    for block in reversed(blocks):
        if not isinstance(block, dict):
            continue
        copied = dict(block)
        if copied.get("type") in {"text", "input_text", "output_text"}:
            value = str(copied.get("text") or "")
            if not remaining:
                continue
            copied["text"] = value[-remaining:]
            remaining -= len(copied["text"])
        kept.append(copied)
    return list(reversed(kept)), budget - remaining


def prepare_chat_messages(
    messages: list[ChatMessage],
    *,
    max_input_chars: int | None,
) -> list[ChatMessage]:
    """Return the exact normalized/truncated messages submitted to a provider.

    Manifested dispatchers use this public boundary before persisting an audit
    record, then pass the result with ``max_input_chars=None`` so truncation is
    applied exactly once.
    """

    return _trim_messages(messages, max_chars=max_input_chars)


def _extract_json_str(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if not raw:
        raise AgentLlmError("LLM returned empty content")
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
    try:
        parsed = json.loads(raw)
    except Exception:
        decoder = json.JSONDecoder()
        parsed = None
        for idx, char in enumerate(raw):
            if char != "{":
                continue
            try:
                parsed, _end = decoder.raw_decode(raw[idx:])
                break
            except Exception:
                continue
        if parsed is None:
            raise AgentLlmError("LLM returned non-JSON content")
    if not isinstance(parsed, dict):
        raise AgentLlmError("LLM returned unexpected JSON payload")
    return parsed


def _extract_chat_completion_content(data: dict[str, Any], *, provider_label: str) -> str:
    choices = data.get("choices") if isinstance(data, dict) else None
    if not isinstance(choices, list) or not choices:
        raise AgentLlmError(f"{provider_label} returned unexpected response")
    msg = (choices[0] or {}).get("message") if isinstance(choices[0], dict) else {}
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, list):
        chunks: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                txt = item.get("text")
                if isinstance(txt, str):
                    chunks.append(txt)
        content = "\n".join(chunks)
    if not isinstance(content, str):
        raise AgentLlmError(f"{provider_label} returned missing text content")
    return content


def _attach_llm_metadata(
    payload: dict[str, Any],
    *,
    provider: str,
    requested_model: str,
    submitted_model: str | None = None,
    response_model: str | None = None,
    usage: dict[str, Any] | None = None,
    request_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resolved_model = str(response_model or submitted_model or requested_model or "").strip()
    metadata = {
        "provider": str(provider or "").strip().lower(),
        "requested_model": str(requested_model or "").strip(),
        "submitted_model": str(submitted_model or requested_model or "").strip(),
        "response_model": str(response_model or "").strip(),
        "model": resolved_model,
    }
    if usage:
        metadata["usage"] = _normalize_usage(usage)
    if request_options:
        metadata["request"] = {
            str(key): value for key, value in request_options.items() if value is not None
        }
    payload[LLM_METADATA_KEY] = metadata
    return payload


def llm_metadata(payload: dict[str, Any] | None) -> dict[str, str]:
    if not isinstance(payload, dict):
        return {}
    value = payload.get(LLM_METADATA_KEY)
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for key in ("provider", "requested_model", "submitted_model", "response_model", "model"):
        text = str(value.get(key) or "").strip()
        if text:
            out[key] = text
    request = value.get("request")
    if isinstance(request, dict):
        for key in (
            "endpoint_kind",
            "parallel_tool_calls",
            "programmatic_tool_calling",
            "tool_route",
            "capability_profile",
            "response_count",
            "tool_call_count",
        ):
            raw = request.get(key)
            if raw is not None:
                out[key] = str(raw)
    usage = value.get("usage")
    if isinstance(usage, dict):
        for key in (
            "input_tokens",
            "effective_input_tokens",
            "output_tokens",
            "total_tokens",
            "reasoning_tokens",
            "prompt_cache_hit_tokens",
            "prompt_cache_miss_tokens",
            "prompt_cache_write_tokens",
            "cache_hit_rate",
            "reported_cost_usd",
            "usage_reported",
        ):
            raw = usage.get(key)
            if raw is not None:
                out[key] = str(raw)
    return out


def _text_payload_with_metadata(
    text: str,
    *,
    provider: str,
    requested_model: str,
    submitted_model: str | None = None,
    response_model: str | None = None,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _attach_llm_metadata(
        {"text": str(text or "")},
        provider=provider,
        requested_model=requested_model,
        submitted_model=submitted_model,
        response_model=response_model,
        usage=usage,
    )


def _chat_completions_json(
    *,
    api_key: str,
    model: str,
    messages: list[ChatMessage],
    timeout_s: float,
    url: str,
    provider_label: str,
    requested_model: str | None = None,
    extra_headers: dict[str, str] | None = None,
    extra_payload: dict[str, Any] | None = None,
    response_format: bool = True,
    max_output_tokens: int | None = None,
    preserve_raw_text: bool = False,
) -> dict[str, Any]:
    payload = {
        "model": model,
        "messages": [{"role": m.role, "content": m.content} for m in messages],
        "temperature": 0.2,
    }
    if response_format:
        payload["response_format"] = {"type": "json_object"}
    if max_output_tokens is not None:
        payload["max_tokens"] = max(1, int(max_output_tokens))
    if extra_payload:
        payload.update(extra_payload)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)
    try:
        resp = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=float(timeout_s),
        )
    except requests.RequestException as exc:
        raise AgentLlmError(f"{provider_label} request failed: {exc}") from exc
    if resp.status_code >= 400:
        raise AgentLlmError(f"{provider_label} HTTP {resp.status_code}: {resp.text[:240]}")
    try:
        data = resp.json()
    except Exception as exc:
        raise AgentLlmError(f"{provider_label} returned invalid JSON") from exc
    raw_content = _extract_chat_completion_content(data, provider_label=provider_label)
    payload = _extract_json_str(raw_content)
    if preserve_raw_text:
        payload[LLM_RAW_RESPONSE_KEY] = raw_content
    response_model = str(data.get("model") or "").strip() if isinstance(data, dict) else ""
    return _attach_llm_metadata(
        payload,
        provider=provider_label,
        requested_model=str(requested_model or model),
        submitted_model=model,
        response_model=response_model or None,
        usage=data.get("usage") if isinstance(data.get("usage"), dict) else None,
    )


def _chat_completions_text(
    *,
    api_key: str,
    model: str,
    messages: list[ChatMessage],
    timeout_s: float,
    url: str,
    provider_label: str,
    requested_model: str | None = None,
    extra_headers: dict[str, str] | None = None,
    extra_payload: dict[str, Any] | None = None,
    max_output_tokens: int | None = None,
) -> dict[str, Any]:
    payload = {
        "model": model,
        "messages": [{"role": m.role, "content": m.content} for m in messages],
        "temperature": 0.2,
    }
    if max_output_tokens is not None:
        payload["max_tokens"] = max(1, int(max_output_tokens))
    if extra_payload:
        payload.update(extra_payload)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=float(timeout_s))
    except requests.RequestException as exc:
        raise AgentLlmError(f"{provider_label} request failed: {exc}") from exc
    if resp.status_code >= 400:
        raise AgentLlmError(f"{provider_label} HTTP {resp.status_code}: {resp.text[:240]}")
    try:
        data = resp.json()
    except Exception as exc:
        raise AgentLlmError(f"{provider_label} returned invalid JSON") from exc
    response_model = str(data.get("model") or "").strip() if isinstance(data, dict) else ""
    return _text_payload_with_metadata(
        _extract_chat_completion_content(data, provider_label=provider_label),
        provider=provider_label,
        requested_model=str(requested_model or model),
        submitted_model=model,
        response_model=response_model or None,
        usage=data.get("usage") if isinstance(data.get("usage"), dict) else None,
    )


def _openai_chat_completions(*, api_key: str, model: str, messages: list[ChatMessage], timeout_s: float, max_output_tokens: int | None = None, prompt_cache_key: str | None = None, preserve_raw_text: bool = False) -> dict[str, Any]:
    extra_payload = {"prompt_cache_key": str(prompt_cache_key)} if prompt_cache_key else None
    return _chat_completions_json(
        api_key=api_key,
        model=model,
        messages=messages,
        timeout_s=timeout_s,
        url=_OPENAI_CHAT_COMPLETIONS_URL,
        provider_label="OpenAI",
        max_output_tokens=max_output_tokens,
        preserve_raw_text=preserve_raw_text,
        extra_payload=extra_payload,
    )


def _openrouter_headers(api_key: str) -> dict[str, str]:
    referer = str(getattr(settings, "public_base_url", "") or "").strip() or "http://localhost"
    return {
        "Authorization": f"Bearer {api_key}",
        "HTTP-Referer": referer,
        "X-Title": "Thoth Terminal",
        "X-OpenRouter-Experimental-Metadata": "enabled",
    }


def _openrouter_free_model_configured_candidates() -> list[str]:
    raw = str(getattr(settings, "openrouter_free_fallback_models", "") or "")
    values = [
        str(getattr(settings, "openrouter_free_model", "") or "").strip(),
        str(getattr(settings, "openrouter_model", "") or "").strip(),
    ]
    values.extend(part.strip() for part in raw.replace("\n", ",").split(","))
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value or value in seen:
            continue
        if value == "openrouter/free" or value.endswith(":free"):
            out.append(value)
            seen.add(value)
    return out


def _price_is_zero(value: object) -> bool:
    try:
        return float(str(value or "0")) == 0.0
    except Exception:
        return False


def _openrouter_discover_free_models(*, api_key: str, timeout_s: float) -> list[str]:
    try:
        resp = requests.get(
            _OPENROUTER_MODELS_URL,
            headers=_openrouter_headers(api_key),
            params={"output_modalities": "text", "supported_parameters": "response_format"},
            timeout=min(max(2.0, float(timeout_s or 5.0)), 10.0),
        )
    except requests.RequestException:
        return []
    if resp.status_code >= 400:
        return []
    try:
        data = resp.json()
    except Exception:
        return []
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    out: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        model_id = str(row.get("id") or "").strip()
        if not model_id:
            continue
        pricing = row.get("pricing") if isinstance(row.get("pricing"), dict) else {}
        is_free = model_id.endswith(":free") or (
            _price_is_zero(pricing.get("prompt"))
            and _price_is_zero(pricing.get("completion"))
            and _price_is_zero(pricing.get("request"))
        )
        if not is_free:
            continue
        supported = row.get("supported_parameters")
        if isinstance(supported, list) and supported and "response_format" not in supported:
            continue
        architecture = row.get("architecture") if isinstance(row.get("architecture"), dict) else {}
        modalities = row.get("modalities") if isinstance(row.get("modalities"), dict) else {}
        input_modalities = architecture.get("input_modalities") or modalities.get("input_modalities") or ["text"]
        output_modalities = architecture.get("output_modalities") or modalities.get("output_modalities") or ["text"]
        output_values = {str(item).strip().lower() for item in output_modalities if str(item).strip()} if isinstance(output_modalities, list) else {str(output_modalities).strip().lower()}
        if not output_values or "text" not in output_values or output_values - {"text"}:
            continue
        out.append(model_id)
    return out


def _openrouter_free_model_candidates(*, api_key: str, timeout_s: float) -> list[str]:
    configured = _openrouter_free_model_configured_candidates()
    discovered = _openrouter_discover_free_models(api_key=api_key, timeout_s=timeout_s)
    discovered_set = set(discovered)
    out: list[str] = []
    seen: set[str] = set()
    if discovered:
        values = [*(value for value in configured if value == "openrouter/free" or value in discovered_set), *discovered]
    else:
        values = [*configured]
    for value in values:
        if value and value not in seen:
            out.append(value)
            seen.add(value)
    return out


def _openrouter_should_try_free_fallback(model: str, exc: AgentLlmError) -> bool:
    if model != "openrouter/free" and not model.endswith(":free"):
        return False
    text = str(exc).lower()
    return "http 429" in text or "rate-limited" in text or "http 404" in text or "no endpoints" in text


def _openrouter_response_matches_requested(*, requested_model: str, response_model: str | None) -> bool:
    requested = str(requested_model or "").strip()
    response = str(response_model or "").strip()
    if not requested or requested == "openrouter/free" or not response:
        return True
    if response == requested:
        return True
    requested_base = requested.removesuffix(":free")
    response_base = response.removesuffix(":free")
    return response_base == requested_base or response_base.startswith(f"{requested_base}-")


def _enforce_openrouter_concrete_model(payload: dict[str, Any], *, candidate_model: str) -> dict[str, Any]:
    meta = llm_metadata(payload)
    response_model = meta.get("response_model") or meta.get("model")
    if not _openrouter_response_matches_requested(requested_model=candidate_model, response_model=response_model):
        raise AgentLlmError(
            f"OpenRouter routed explicit model {candidate_model} to {response_model or 'unknown response model'}"
        )
    return payload


def _openrouter_chat_completions(*, api_key: str, model: str, messages: list[ChatMessage], timeout_s: float, max_output_tokens: int | None = None, preserve_raw_text: bool = False) -> dict[str, Any]:
    def call(candidate_model: str) -> dict[str, Any]:
        extra_payload: dict[str, Any] = {"metadata": {"app": "thoth_terminal", "provider": "openrouter"}}
        if bool(getattr(settings, "openrouter_enforce_free_only", True)):
            if candidate_model != "openrouter/free" and not candidate_model.endswith(":free"):
                raise AgentLlmError(f"OpenRouter free-only mode rejected paid model: {candidate_model}")
            extra_payload["provider"] = {"max_price": {"prompt": 0, "completion": 0, "request": 0}}
        payload = _chat_completions_json(
            api_key=api_key,
            model=candidate_model,
            messages=messages,
            timeout_s=timeout_s,
            url=_OPENROUTER_CHAT_COMPLETIONS_URL,
            provider_label="OpenRouter",
            requested_model=model,
            extra_headers=_openrouter_headers(api_key),
            extra_payload=extra_payload,
            max_output_tokens=max_output_tokens,
            preserve_raw_text=preserve_raw_text,
        )
        return _enforce_openrouter_concrete_model(payload, candidate_model=candidate_model)

    try:
        return call(model)
    except AgentLlmError as exc:
        if not _openrouter_should_try_free_fallback(model, exc):
            raise
        errors = [f"{model}: {exc}"]
        retry_limit = max(1, int(getattr(settings, "openrouter_free_retry_limit", 4) or 4))
        for candidate in _openrouter_free_model_candidates(api_key=api_key, timeout_s=timeout_s):
            if candidate == model:
                continue
            try:
                return call(candidate)
            except AgentLlmError as candidate_exc:
                errors.append(f"{candidate}: {candidate_exc}")
                if len(errors) >= retry_limit:
                    break
        raise AgentLlmError(f"OpenRouter free models failed after {len(errors)} attempt(s): {errors[-1]}") from exc


def _openrouter_chat_text(*, api_key: str, model: str, messages: list[ChatMessage], timeout_s: float, max_output_tokens: int | None = None) -> dict[str, Any]:
    def call(candidate_model: str) -> dict[str, Any]:
        extra_payload: dict[str, Any] = {"metadata": {"app": "thoth_terminal", "provider": "openrouter"}}
        if bool(getattr(settings, "openrouter_enforce_free_only", True)):
            if candidate_model != "openrouter/free" and not candidate_model.endswith(":free"):
                raise AgentLlmError(f"OpenRouter free-only mode rejected paid model: {candidate_model}")
            extra_payload["provider"] = {"max_price": {"prompt": 0, "completion": 0, "request": 0}}
        payload = _chat_completions_text(
            api_key=api_key,
            model=candidate_model,
            messages=messages,
            timeout_s=timeout_s,
            url=_OPENROUTER_CHAT_COMPLETIONS_URL,
            provider_label="OpenRouter",
            requested_model=model,
            extra_headers=_openrouter_headers(api_key),
            extra_payload=extra_payload,
            max_output_tokens=max_output_tokens,
        )
        return _enforce_openrouter_concrete_model(payload, candidate_model=candidate_model)

    try:
        return call(model)
    except AgentLlmError as exc:
        if not _openrouter_should_try_free_fallback(model, exc):
            raise
        errors = [f"{model}: {exc}"]
        retry_limit = max(1, int(getattr(settings, "openrouter_free_retry_limit", 4) or 4))
        for candidate in _openrouter_free_model_candidates(api_key=api_key, timeout_s=timeout_s):
            if candidate == model:
                continue
            try:
                return call(candidate)
            except AgentLlmError as candidate_exc:
                errors.append(f"{candidate}: {candidate_exc}")
                if len(errors) >= retry_limit:
                    break
        raise AgentLlmError(f"OpenRouter free models failed after {len(errors)} attempt(s): {errors[-1]}") from exc


def _deepseek_chat_completions(
    *,
    api_key: str,
    model: str,
    messages: list[ChatMessage],
    timeout_s: float,
    max_output_tokens: int | None = None,
    thinking: bool = True,
    reasoning_effort: str = "max",
    preserve_raw_text: bool = False,
) -> dict[str, Any]:
    thinking_payload = {"thinking": {"type": "disabled"}} if not thinking else {
        "thinking": {"type": "enabled"},
        "reasoning_effort": str(reasoning_effort or "max").strip().lower() or "max",
    }
    return _chat_completions_json(
        api_key=api_key,
        model=model,
        messages=messages,
        timeout_s=timeout_s,
        url=_DEEPSEEK_CHAT_COMPLETIONS_URL,
        provider_label="DeepSeek",
        max_output_tokens=max_output_tokens,
        response_format=False,
        extra_payload=thinking_payload,
        preserve_raw_text=preserve_raw_text,
    )


def _deepseek_chat_text(
    *,
    api_key: str,
    model: str,
    messages: list[ChatMessage],
    timeout_s: float,
    max_output_tokens: int | None = None,
    thinking: bool = True,
    reasoning_effort: str = "max",
) -> dict[str, Any]:
    thinking_payload = {"thinking": {"type": "disabled"}} if not thinking else {
        "thinking": {"type": "enabled"},
        "reasoning_effort": str(reasoning_effort or "max").strip().lower() or "max",
    }
    return _chat_completions_text(
        api_key=api_key,
        model=model,
        messages=messages,
        timeout_s=timeout_s,
        url=_DEEPSEEK_CHAT_COMPLETIONS_URL,
        provider_label="DeepSeek",
        max_output_tokens=max_output_tokens,
        extra_payload=thinking_payload,
    )


def _nous_base_url() -> str:
    return str(getattr(settings, "nous_base_url", _NOUS_DEFAULT_BASE_URL) or _NOUS_DEFAULT_BASE_URL).strip().rstrip("/")


def _nous_chat_completions(*, api_key: str, model: str, messages: list[ChatMessage], timeout_s: float, max_output_tokens: int | None = None, preserve_raw_text: bool = False) -> dict[str, Any]:
    return _chat_completions_json(
        api_key=api_key,
        model=model,
        messages=messages,
        timeout_s=timeout_s,
        url=f"{_nous_base_url()}/chat/completions",
        provider_label="Nous",
        max_output_tokens=max_output_tokens,
        preserve_raw_text=preserve_raw_text,
    )


def _nous_chat_text(*, api_key: str, model: str, messages: list[ChatMessage], timeout_s: float, max_output_tokens: int | None = None) -> dict[str, Any]:
    return _chat_completions_text(
        api_key=api_key,
        model=model,
        messages=messages,
        timeout_s=timeout_s,
        url=f"{_nous_base_url()}/chat/completions",
        provider_label="Nous",
        max_output_tokens=max_output_tokens,
    )


def _meta_base_url() -> str:
    return str(getattr(settings, "meta_base_url", _META_DEFAULT_BASE_URL) or _META_DEFAULT_BASE_URL).strip().rstrip("/")


def _meta_api_mode() -> str:
    value = str(getattr(settings, "meta_api_mode", "responses") or "responses").strip().lower()
    if value not in {"responses", "chat_completions"}:
        raise AgentLlmError("Meta API mode must be responses or chat_completions")
    return value


def _meta_prompt_cache_retention(value: str | None = None) -> str | None:
    raw = str(
        value
        if value is not None
        else getattr(settings, "meta_prompt_cache_retention", "24h")
        or ""
    ).strip().lower()
    if not raw or raw == "none":
        return None
    if raw not in {"in_memory", "24h"}:
        raise AgentLlmError("Meta prompt cache retention must be in_memory, 24h, or empty")
    return raw


def provider_request_profile(
    provider: str,
    *,
    prompt_cache_key: str | None = None,
    prompt_cache_retention: str | None = None,
) -> dict[str, Any]:
    """Return the non-secret transport/cache configuration for audit manifests."""

    provider_v = str(provider or "").strip().lower()
    if provider_v == "meta":
        mode = _meta_api_mode()
        retention = _meta_prompt_cache_retention(prompt_cache_retention) if mode == "responses" else None
        return {
            "endpoint_kind": mode,
            "prompt_cache_key": str(prompt_cache_key or "").strip() or None,
            "prompt_cache_retention": retention,
            "store": bool(getattr(settings, "meta_store_responses", False)) if mode == "responses" else None,
        }
    if provider_v == "openai":
        return {
            "endpoint_kind": "chat_completions",
            "prompt_cache_key": str(prompt_cache_key or "").strip() or None,
            "prompt_cache_retention": None,
            "store": None,
        }
    return {
        "endpoint_kind": "chat_completions",
        "prompt_cache_key": None,
        "prompt_cache_retention": None,
        "store": None,
    }


def _meta_chat_completions(*, api_key: str, model: str, messages: list[ChatMessage], timeout_s: float, max_output_tokens: int | None = None, prompt_cache_key: str | None = None, preserve_raw_text: bool = False) -> dict[str, Any]:
    extra_payload = {"prompt_cache_key": str(prompt_cache_key)} if prompt_cache_key else None
    return _chat_completions_json(
        api_key=api_key,
        model=model,
        messages=messages,
        timeout_s=timeout_s,
        url=f"{_meta_base_url()}/chat/completions",
        provider_label="Meta",
        max_output_tokens=max_output_tokens,
        preserve_raw_text=preserve_raw_text,
        extra_payload=extra_payload,
    )


def _meta_chat_text(*, api_key: str, model: str, messages: list[ChatMessage], timeout_s: float, max_output_tokens: int | None = None, prompt_cache_key: str | None = None) -> dict[str, Any]:
    extra_payload = {"prompt_cache_key": str(prompt_cache_key)} if prompt_cache_key else None
    return _chat_completions_text(
        api_key=api_key,
        model=model,
        messages=messages,
        timeout_s=timeout_s,
        url=f"{_meta_base_url()}/chat/completions",
        provider_label="Meta",
        max_output_tokens=max_output_tokens,
        extra_payload=extra_payload,
    )


def _meta_responses_input(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.role or "user").strip().lower() or "user"
        content_type = "output_text" if role == "assistant" else "input_text"
        if isinstance(message.content, list):
            blocks: list[dict[str, Any]] = []
            for block in message.content:
                if not isinstance(block, dict):
                    continue
                kind = block.get("type")
                if kind in {"text", "input_text", "output_text"}:
                    blocks.append({"type": content_type, "text": str(block.get("text") or "")})
                elif kind in {"image_url", "input_image"} and role == "user":
                    image = block.get("image_url")
                    url = image.get("url") if isinstance(image, dict) else image
                    if isinstance(url, str) and url:
                        blocks.append({"type": "input_image", "image_url": url})
            content = blocks
        else:
            content = [{"type": content_type, "text": str(message.content or "")}]
        out.append({"role": role, "content": content})
    return out


def _extract_responses_output_text(data: dict[str, Any], *, provider_label: str) -> str:
    chunks: list[str] = []
    for item in data.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for block in item.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "output_text" and isinstance(block.get("text"), str):
                chunks.append(str(block["text"]))
    if not chunks:
        output_types = [
            str(item.get("type") or "unknown")
            for item in data.get("output") or []
            if isinstance(item, dict)
        ]
        incomplete = data.get("incomplete_details") if isinstance(data.get("incomplete_details"), dict) else {}
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        raise AgentLlmError(
            f"{provider_label} returned missing response output text "
            f"(status={data.get('status')!r}, output_types={output_types}, "
            f"incomplete_reason={incomplete.get('reason')!r}, output_tokens={usage.get('output_tokens')!r})"
        )
    return "\n".join(chunks)


def _meta_responses(
    *,
    api_key: str,
    model: str,
    messages: list[ChatMessage],
    timeout_s: float,
    max_output_tokens: int | None,
    prompt_cache_key: str | None,
    prompt_cache_retention: str | None,
    json_output: bool,
    preserve_raw_text: bool = False,
) -> dict[str, Any]:
    retention = _meta_prompt_cache_retention(prompt_cache_retention)
    store = bool(getattr(settings, "meta_store_responses", False))
    payload: dict[str, Any] = {
        "model": model,
        "input": _meta_responses_input(messages),
        "store": store,
    }
    if json_output:
        payload["text"] = {"format": {"type": "json_object"}}
    if max_output_tokens is not None:
        payload["max_output_tokens"] = max(16, int(max_output_tokens))
    if prompt_cache_key:
        payload["prompt_cache_key"] = str(prompt_cache_key)
    if retention:
        payload["prompt_cache_retention"] = retention
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        resp = requests.post(
            f"{_meta_base_url()}/responses",
            headers=headers,
            json=payload,
            timeout=float(timeout_s),
        )
    except requests.RequestException as exc:
        raise AgentLlmError(f"Meta request failed: {exc}") from exc
    if resp.status_code >= 400:
        raise AgentLlmError(f"Meta HTTP {resp.status_code}: {resp.text[:240]}")
    try:
        data = resp.json()
    except Exception as exc:
        raise AgentLlmError("Meta returned invalid JSON") from exc
    raw_content = _extract_responses_output_text(data, provider_label="Meta")
    result = _extract_json_str(raw_content) if json_output else {"text": raw_content}
    if preserve_raw_text:
        result[LLM_RAW_RESPONSE_KEY] = raw_content
    return _attach_llm_metadata(
        result,
        provider="Meta",
        requested_model=model,
        submitted_model=model,
        response_model=str(data.get("model") or "").strip() or None,
        usage=data.get("usage") if isinstance(data.get("usage"), dict) else None,
        request_options={
            "endpoint_kind": "responses",
            "prompt_cache_key": str(prompt_cache_key or "").strip() or None,
            "prompt_cache_retention": retention,
            "store": store,
        },
    )


def openai_responses_tools_enabled() -> bool:
    """Return the explicit opt-in gate for client-owned Responses tool loops."""

    value = getattr(settings, "openai_responses_tools_enabled", os.getenv("THOTH_OPENAI_RESPONSES_TOOLS_ENABLED", "false"))
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def openai_programmatic_tool_calling_enabled() -> bool:
    """Return the separate opt-in gate for the GPT-5.6 PTC adapter."""

    value = getattr(
        settings,
        "openai_programmatic_tool_calling_enabled",
        os.getenv("THOTH_OPENAI_PROGRAMMATIC_TOOL_CALLING_ENABLED", "false"),
    )
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def provider_capabilities(
    provider: str | None = None,
    *,
    model: str | None = None,
) -> dict[str, Any]:
    """Report only capabilities implemented and explicitly enabled here."""

    provider_v = str(provider or getattr(settings, "ai_provider", "openai") or "openai").strip().lower()
    model_v = str(model or active_llm_model(provider_v) or "").strip()
    responses_enabled = provider_v == "openai" and openai_responses_tools_enabled()
    ptc_model = model_v.lower().startswith("gpt-5.6")
    ptc_enabled = responses_enabled and ptc_model and openai_programmatic_tool_calling_enabled()
    return {
        "provider": provider_v,
        "model": model_v,
        "supports_responses_tool_workflows": responses_enabled,
        "supports_parallel_tool_calls": responses_enabled,
        "supports_programmatic_tool_calling": ptc_enabled,
        "capability_profile": OPENAI_AGENT_PROFILE_VERSION if responses_enabled else None,
    }


def _responses_input_items(messages: Iterable[ChatMessage]) -> list[dict[str, Any]]:
    return _meta_responses_input(list(messages))


def _bounded_response_value(value: object, *, max_chars: int = 12000) -> object:
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


def _response_output_items(data: dict[str, Any]) -> list[dict[str, Any]]:
    raw = data.get("output")
    if not isinstance(raw, list):
        return []
    return [dict(item) for item in raw if isinstance(item, dict)]


def parse_responses_tool_events(data: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Parse standard Responses output items without executing any event."""

    items = _response_output_items(data)
    return {
        "items": items,
        "function_calls": [item for item in items if str(item.get("type") or "") == "function_call"],
        "programs": [item for item in items if str(item.get("type") or "") == "program"],
        "program_outputs": [item for item in items if str(item.get("type") or "") == "program_output"],
        "messages": [item for item in items if str(item.get("type") or "") == "message"],
    }


def _response_citations(items: Iterable[dict[str, Any]], *, limit: int = 32) -> list[object]:
    citations: list[object] = []
    seen: set[str] = set()

    def add(value: object) -> None:
        if len(citations) >= limit:
            return
        try:
            key = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        except Exception:
            key = str(value)
        if key in seen:
            return
        seen.add(key)
        citations.append(_bounded_response_value(value, max_chars=2000))

    def walk(value: object) -> None:
        if len(citations) >= limit:
            return
        if isinstance(value, dict):
            for key in ("citations", "sources", "source_refs", "annotations"):
                raw = value.get(key)
                if isinstance(raw, list):
                    for item in raw:
                        add(item)
                elif isinstance(raw, dict):
                    add(raw)
            for key in ("content", "output", "result", "structuredContent"):
                if key in value:
                    walk(value[key])
        elif isinstance(value, list):
            for item in value[:limit]:
                walk(item)

    for item in items:
        walk(item)
    return citations


def _response_text_if_present(items: Iterable[dict[str, Any]]) -> str:
    chunks: list[str] = []
    for item in items:
        if str(item.get("type") or "") != "message":
            continue
        for block in item.get("content") or []:
            if isinstance(block, dict) and block.get("type") in {"output_text", "text"}:
                text = block.get("text")
                if isinstance(text, str):
                    chunks.append(text)
    return "\n".join(chunks).strip()


def _response_usage_totals(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    totals: dict[str, int] = {}
    for usage in rows:
        if not isinstance(usage, dict):
            continue
        normalized = _normalize_usage(usage)
        for key in (
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "reasoning_tokens",
            "prompt_cache_hit_tokens",
            "prompt_cache_miss_tokens",
            "prompt_cache_write_tokens",
        ):
            value = normalized.get(key)
            if isinstance(value, int):
                totals[key] = totals.get(key, 0) + value
    if totals:
        totals["usage_reported"] = True
    return totals


def _response_call_output(
    *,
    value: object,
    citations: list[object] | None = None,
    ptc: bool,
) -> dict[str, Any]:
    if isinstance(value, dict) and "result" in value:
        result = value.get("result")
        own_citations = value.get("citations")
        if isinstance(own_citations, list):
            citations = list(own_citations)
    else:
        result = value
    payload: dict[str, Any] = {
        "result": _bounded_response_value(result),
        "citations": list(citations or []),
    }
    if not ptc:
        return payload
    return payload


def _response_tool_error_output(message: str) -> dict[str, Any]:
    return {"result": None, "citations": [], "error": str(message)[:1000]}


def _response_call_record(
    *,
    call: dict[str, Any],
    name: str,
    caller: object,
    status: str,
    output: object,
    arguments: object,
) -> dict[str, Any]:
    encoded_args = json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)
    encoded_output = json.dumps(output, ensure_ascii=False, sort_keys=True, default=str)
    return {
        "call_id": str(call.get("call_id") or call.get("id") or "").strip(),
        "name": name,
        "caller": _bounded_response_value(caller, max_chars=2000) if caller is not None else None,
        "status": status,
        "arguments_sha256": hashlib.sha256(encoded_args.encode("utf-8")).hexdigest(),
        "output_sha256": hashlib.sha256(encoded_output.encode("utf-8")).hexdigest(),
        "citations": list(output.get("citations") or []) if isinstance(output, dict) else [],
    }


def openai_responses_tool_loop(
    *,
    messages: list[ChatMessage],
    tools: list[dict[str, Any]],
    tool_executor: Callable[[str, dict[str, Any]], object],
    model: str | None = None,
    timeout_s: float = 60.0,
    max_turns: int = 6,
    max_tool_calls: int = 32,
    parallel_tool_calls: bool = False,
    programmatic_tool_calling: bool = False,
    json_output: bool = False,
    max_output_tokens: int | None = None,
    prompt_cache_key: str | None = None,
    store: bool = False,
    capability_profile: str = OPENAI_AGENT_PROFILE_VERSION,
    enabled: bool | None = None,
    preserve_raw_text: bool = False,
) -> dict[str, Any]:
    """Run a bounded OpenAI Responses function/MCP tool loop.

    The adapter is opt-in because the repository's existing provider paths are
    chat-completions based.  It preserves response output items internally for
    stateless continuation and returns compact call/citation records for the
    request ledger.  Generated PTC JavaScript is never executed locally.
    """

    if enabled is None:
        enabled = openai_responses_tools_enabled()
    if not enabled:
        raise AgentLlmError("OpenAI Responses tool workflows are disabled by configuration")
    if programmatic_tool_calling:
        if not openai_programmatic_tool_calling_enabled():
            raise AgentLlmError("OpenAI Programmatic Tool Calling is disabled by configuration")
        mdl_check = str(model or active_llm_model("openai") or "").lower()
        if not mdl_check.startswith("gpt-5.6"):
            raise AgentLlmError("Programmatic Tool Calling is gated to GPT-5.6 models")

    tool_defs = [dict(row) for row in tools if isinstance(row, dict)]
    if len(tool_defs) > 64:
        raise AgentLlmError("Responses tool workflow accepts at most 64 tool definitions")
    tool_by_name = {
        str(row.get("name") or "").strip(): row
        for row in tool_defs
        if str(row.get("name") or "").strip()
    }
    if not tool_by_name:
        raise AgentLlmError("Responses tool workflow requires at least one tool definition")
    parallel_enabled = (
        bool(parallel_tool_calls)
        and not programmatic_tool_calling
        and len(tool_by_name) > 1
        and all(
        is_parallel_safe_tool(name) and not capability_for_tool(name).open_world
        for name in tool_by_name
        )
    )
    if programmatic_tool_calling and not any(
        str(row.get("type") or "") == "programmatic_tool_calling" for row in tool_defs
    ):
        tool_defs.append({"type": "programmatic_tool_calling"})

    key = str(getattr(settings, "openai_api_key", None) or "").strip()
    if not key:
        raise AgentLlmError("OpenAI API key is not configured")
    submitted_model = active_llm_model("openai", model) or "gpt-4o"
    input_items: list[dict[str, Any]] = _responses_input_items(messages)
    all_call_records: list[dict[str, Any]] = []
    all_citations: list[object] = []
    usage_rows: list[dict[str, Any]] = []
    response_items_for_audit: list[dict[str, Any]] = []
    program_outputs_seen: list[dict[str, Any]] = []
    response_count = 0
    total_tool_calls = 0
    final_text = ""

    for turn in range(max(1, min(int(max_turns or 6), 12))):
        payload: dict[str, Any] = {
            "model": submitted_model,
            "input": input_items,
            "tools": tool_defs,
            "parallel_tool_calls": parallel_enabled,
            "store": bool(store),
        }
        if json_output:
            payload["text"] = {"format": {"type": "json_object"}}
        if max_output_tokens is not None:
            payload["max_output_tokens"] = max(16, int(max_output_tokens))
        if prompt_cache_key:
            payload["prompt_cache_key"] = str(prompt_cache_key)
        if programmatic_tool_calling:
            # The hosted tool is a real Responses tool; it is not sent through
            # the MCP tools/list surface.
            payload["tools"] = tool_defs
            payload["tool_choice"] = "programmatic_tool_calling"

        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        try:
            response = requests.post(
                _OPENAI_RESPONSES_URL,
                headers=headers,
                json=payload,
                timeout=float(timeout_s),
            )
        except requests.RequestException as exc:
            raise AgentLlmError(f"OpenAI Responses request failed: {exc}") from exc
        if response.status_code >= 400:
            raise AgentLlmError(f"OpenAI Responses HTTP {response.status_code}: {response.text[:500]}")
        try:
            data = response.json()
        except Exception as exc:
            raise AgentLlmError("OpenAI Responses returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise AgentLlmError("OpenAI Responses returned an unexpected payload")

        response_count += 1
        usage = data.get("usage")
        if isinstance(usage, dict):
            usage_rows.append(usage)
        events = parse_responses_tool_events(data)
        output_items = events["items"]
        program_outputs_seen.extend(events["program_outputs"][:32])
        response_items_for_audit.extend(output_items[:64])
        all_citations.extend(_response_citations(output_items))
        final_text = _response_text_if_present(output_items) or final_text

        status = str(data.get("status") or "completed")
        if status not in {"completed", "incomplete"}:
            raise AgentLlmError(f"OpenAI Responses ended with status {status}")
        if status == "incomplete" and not any(str(item.get("type") or "") == "function_call" for item in output_items):
            raise AgentLlmError(
                f"OpenAI Responses incomplete: {((data.get('incomplete_details') or {}).get('reason') if isinstance(data.get('incomplete_details'), dict) else '')}"
            )

        function_calls = events["function_calls"]
        if not function_calls:
            if final_text:
                break
            # A program can produce program_output before the final message;
            # replay the complete output and let the service continue.
            input_items.extend(output_items)
            continue
        total_tool_calls += len(function_calls)
        if total_tool_calls > max(1, min(int(max_tool_calls or 32), 128)):
            raise AgentLlmError("Responses tool workflow exceeded its tool-call limit")

        pending: list[tuple[dict[str, Any], str, dict[str, Any], object, bool]] = []
        output_rows: list[dict[str, Any] | None] = [None] * len(function_calls)
        record_rows: list[dict[str, Any] | None] = [None] * len(function_calls)
        for index, call in enumerate(function_calls):
            name = str(call.get("name") or "").strip()
            raw_args = call.get("arguments") or "{}"
            try:
                arguments = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except Exception:
                arguments = None
            caller = call.get("caller")
            caller_type = str(caller.get("type") or "") if isinstance(caller, dict) else ""
            from_program = caller_type == "program"
            definition = tool_by_name.get(name)
            allowed_callers = definition.get("allowed_callers") if isinstance(definition, dict) else None
            allowed = list(allowed_callers) if isinstance(allowed_callers, list) else ["direct"]
            valid = (
                bool(name)
                and definition is not None
                and isinstance(arguments, dict)
                and (not from_program or programmatic_tool_calling)
                and (not from_program or is_ptc_eligible_tool(name))
                and (not from_program or "programmatic" in allowed)
            )
            if not valid:
                error_value = _response_tool_error_output(
                    "Tool call rejected by the configured direct/PTC capability policy"
                )
                output_rows[index] = {
                    "type": "function_call_output",
                    "call_id": str(call.get("call_id") or call.get("id") or ""),
                    "output": json.dumps(error_value, ensure_ascii=False),
                    **({"caller": caller} if from_program and caller is not None else {}),
                }
                record_rows[index] = _response_call_record(
                    call=call,
                    name=name,
                    caller=caller,
                    status="rejected",
                    output=error_value,
                    arguments=arguments,
                )
                continue
            pending.append((call, name, arguments, caller, from_program))

        can_parallelize = parallel_enabled and len(pending) > 1 and all(
            is_parallel_safe_tool(name) and not capability_for_tool(name).open_world
            for _call, name, _args, _caller, _from_program in pending
        )

        def execute(item: tuple[dict[str, Any], str, dict[str, Any], object, bool]) -> tuple[dict[str, Any], dict[str, Any], str]:
            call, name, arguments, caller, from_program = item
            try:
                raw_value = tool_executor(name, arguments)
                output_value = _response_call_output(
                    value=raw_value,
                    citations=None,
                    ptc=from_program,
                )
                status_value = "success"
            except Exception as exc:
                output_value = _response_tool_error_output(f"{type(exc).__name__}: {exc}")
                status_value = "error"
            output_item: dict[str, Any] = {
                "type": "function_call_output",
                "call_id": str(call.get("call_id") or call.get("id") or ""),
                "output": json.dumps(output_value, ensure_ascii=False, default=str),
            }
            if from_program and caller is not None:
                output_item["caller"] = caller
            return output_item, _response_call_record(
                call=call,
                name=name,
                caller=caller,
                status=status_value,
                output=output_value,
                arguments=arguments,
            ), name

        if can_parallelize:
            with ThreadPoolExecutor(max_workers=min(4, len(pending))) as executor:
                executed = list(executor.map(execute, pending))
        else:
            executed = [execute(item) for item in pending]
        for _output_item, record, _name in executed:
            if isinstance(record.get("citations"), list):
                all_citations.extend(record["citations"])
        pending_by_id = {record[0].get("call_id") or record[0].get("id"): record for record in executed}
        for index, call in enumerate(function_calls):
            if output_rows[index] is not None:
                continue
            row = pending_by_id.get(call.get("call_id") or call.get("id"))
            if row is None:
                continue
            output_rows[index] = row[0]
            record_rows[index] = row[1]

        ordered_outputs = [row for row in output_rows if row is not None]
        all_call_records.extend(row for row in record_rows if row is not None)
        input_items.extend(output_items)
        input_items.extend(ordered_outputs)
    else:
        raise AgentLlmError("OpenAI Responses tool workflow exceeded its turn limit")

    if not final_text:
        raise AgentLlmError("OpenAI Responses returned no final assistant message")
    result: dict[str, Any]
    if json_output:
        result = _extract_json_str(final_text)
    else:
        result = {"text": final_text}
    result[LLM_TOOL_CALLS_KEY] = all_call_records[:128]
    result[LLM_CITATIONS_KEY] = all_citations[:64]
    result[LLM_RESPONSES_OUTPUT_KEY] = [
        _bounded_response_value(item, max_chars=4000) for item in response_items_for_audit[:128]
    ]
    result[LLM_PROGRAM_OUTPUTS_KEY] = [
        _bounded_response_value(item, max_chars=4000) for item in program_outputs_seen[:32]
    ]
    if preserve_raw_text:
        result[LLM_RAW_RESPONSE_KEY] = final_text
    return _attach_llm_metadata(
        result,
        provider="openai",
        requested_model=str(model or submitted_model),
        submitted_model=submitted_model,
        response_model=str(data.get("model") or submitted_model),
        usage=_response_usage_totals(usage_rows),
        request_options={
            "endpoint_kind": "responses",
            "parallel_tool_calls": parallel_enabled,
            "programmatic_tool_calling": bool(programmatic_tool_calling),
            "tool_route": "ptc" if programmatic_tool_calling else "direct",
            "capability_profile": str(capability_profile or OPENAI_AGENT_PROFILE_VERSION),
            "response_count": response_count,
            "tool_call_count": total_tool_calls,
            "prompt_cache_key": str(prompt_cache_key or "").strip() or None,
            "store": bool(store),
        },
    )


def _openrouter_model_for_request(model_override: str | None = None) -> str:
    if model_override:
        return model_override
    if bool(getattr(settings, "openrouter_prefer_free_models", True)):
        free_model = str(getattr(settings, "openrouter_free_model", "") or "").strip()
        if free_model:
            return free_model
    return str(getattr(settings, "openrouter_model", "openrouter/free") or "openrouter/free").strip()


def active_llm_model(provider: str | None = None, model_override: str | None = None) -> str | None:
    prov = (provider or getattr(settings, "ai_provider", "openai") or "openai").strip().lower()
    override = (model_override or "").strip() or None
    if prov == "openai":
        return override or str(getattr(settings, "openai_model", "gpt-4o") or "gpt-4o")
    if prov == "anthropic":
        return override or str(getattr(settings, "anthropic_model", "claude-3-5-sonnet-20241022") or "")
    if prov == "openrouter":
        return _openrouter_model_for_request(override)
    if prov == "deepseek":
        # Thoth intentionally collapses all DeepSeek work onto the current
        # V4 Flash checkpoint. Retired aliases and V4 Pro Preview requests are
        # normalized here so background jobs cannot silently revive them.
        return _DEEPSEEK_DEFAULT_MODEL
    if prov == "nous":
        return override or str(getattr(settings, "nous_model", "Hermes-4.3-36B") or "Hermes-4.3-36B")
    if prov == "meta":
        return override or str(getattr(settings, "meta_model", _META_DEFAULT_MODEL) or _META_DEFAULT_MODEL)
    if prov == "local":
        return override or local_model_name()
    return override


def _anthropic_messages(
    *,
    api_key: str,
    model: str,
    messages: list[ChatMessage],
    timeout_s: float,
    max_output_tokens: int | None = None,
) -> dict[str, Any]:
    system_parts: list[str] = []
    msg_rows: list[dict[str, str]] = []
    for m in messages:
        role = str(m.role or "user").strip().lower()
        content = str(m.content or "")
        if not content:
            continue
        if role == "system":
            system_parts.append(content)
            continue
        if role not in {"user", "assistant"}:
            role = "user"
        msg_rows.append({"role": role, "content": content})
    if not msg_rows:
        raise AgentLlmError("No non-system messages to send to Anthropic")
    payload: dict[str, Any] = {
        "model": model,
        # Anthropic requires this field. Use a high ceiling when Thoth is uncapped.
        "max_tokens": max(1, int(max_output_tokens or 64000)),
        "temperature": 0.2,
        "messages": msg_rows,
    }
    if system_parts:
        payload["system"] = "\n\n".join(system_parts)
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=payload,
            timeout=float(timeout_s),
        )
    except requests.RequestException as exc:
        raise AgentLlmError(f"Anthropic request failed: {exc}") from exc
    if resp.status_code >= 400:
        raise AgentLlmError(f"Anthropic HTTP {resp.status_code}: {resp.text[:240]}")
    try:
        data = resp.json()
    except Exception as exc:
        raise AgentLlmError("Anthropic returned invalid JSON") from exc
    blocks = data.get("content") if isinstance(data, dict) else None
    if not isinstance(blocks, list):
        raise AgentLlmError("Anthropic returned unexpected response")
    text_chunks: list[str] = []
    for blk in blocks:
        if not isinstance(blk, dict):
            continue
        if blk.get("type") != "text":
            continue
        txt = blk.get("text")
        if isinstance(txt, str):
            text_chunks.append(txt)
    return _extract_json_str("\n".join(text_chunks))


def _anthropic_text(*, api_key: str, model: str, messages: list[ChatMessage], timeout_s: float, max_output_tokens: int | None = None) -> dict[str, Any]:
    system_parts: list[str] = []
    msg_rows: list[dict[str, str]] = []
    for m in messages:
        role = str(m.role or "user").strip().lower()
        content = str(m.content or "")
        if not content:
            continue
        if role == "system":
            system_parts.append(content)
            continue
        if role not in {"user", "assistant"}:
            role = "user"
        msg_rows.append({"role": role, "content": content})
    if not msg_rows:
        raise AgentLlmError("No non-system messages to send to Anthropic")
    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": max(1, int(max_output_tokens or 1200)),
        "temperature": 0.2,
        "messages": msg_rows,
    }
    if system_parts:
        payload["system"] = "\n\n".join(system_parts)
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=payload,
            timeout=float(timeout_s),
        )
    except requests.RequestException as exc:
        raise AgentLlmError(f"Anthropic request failed: {exc}") from exc
    if resp.status_code >= 400:
        raise AgentLlmError(f"Anthropic HTTP {resp.status_code}: {resp.text[:240]}")
    try:
        data = resp.json()
    except Exception as exc:
        raise AgentLlmError("Anthropic returned invalid JSON") from exc
    blocks = data.get("content") if isinstance(data, dict) else None
    if not isinstance(blocks, list):
        raise AgentLlmError("Anthropic returned unexpected response")
    text_chunks: list[str] = []
    for blk in blocks:
        if isinstance(blk, dict) and blk.get("type") == "text" and isinstance(blk.get("text"), str):
            text_chunks.append(str(blk.get("text") or ""))
    return _text_payload_with_metadata(
        "\n".join(text_chunks),
        provider="Anthropic",
        requested_model=model,
        submitted_model=model,
        response_model=str(data.get("model") or model),
        usage=data.get("usage") if isinstance(data.get("usage"), dict) else None,
    )


def chat_json_auto(
    *,
    messages: list[ChatMessage],
    endpoint: str | None = None,
    model: str | None = None,
    timeout_s: float = 60.0,
    max_input_chars: int | None = 24000,
    max_output_tokens: int | None = None,
    provider: str | None = None,
    prompt_cache_key: str | None = None,
    prompt_cache_retention: str | None = None,
    deepseek_thinking: bool | None = None,
    deepseek_reasoning_effort: str = "max",
    preserve_raw_text: bool = False,
) -> dict[str, Any]:
    trimmed = _trim_messages(messages, max_chars=max_input_chars)
    ep = (endpoint or "").strip() or None
    mdl_override = (model or "").strip() or None
    prov = (provider or getattr(settings, "ai_provider", "openai") or "openai").strip().lower()

    if ep:
        try:
            return local_chat_json(
                endpoint=ep,
                model=(mdl_override or local_model_name() or ""),
                messages=trimmed,
                timeout_s=float(timeout_s),
                max_input_chars=int(max_input_chars or 24000),
            )
        except LocalLlmError as exc:
            raise AgentLlmError(str(exc)) from exc

    if prov == "local":
        # A local runtime is a demand-loaded dependency. Starting the desktop or
        # an idle worker must not reserve model memory in anticipation of a call.
        ensure_payload = ensure_local_model_runtime(
            service="worker",
            require_opt_in=True,
            wait_s=float(getattr(settings, "local_model_autostart_wait_timeout_s", 0.0) or 0.0),
        )
        resolved_endpoint = local_model_endpoint()
        resolved_model = mdl_override or local_model_name()
        if not resolved_endpoint:
            raise AgentLlmError("Local model endpoint is not configured")
        if not resolved_model:
            raise AgentLlmError("Local model name is not configured")
        if bool(getattr(settings, "local_model_autostart", False)) and ensure_payload.get("status") in {
            "launch_failed",
            "not_launch_ready",
        }:
            detail = ensure_payload.get("error") or ensure_payload.get("status") or "not ready"
            raise AgentLlmError(f"Local model runtime did not become ready: {detail}")
        try:
            return local_chat_json(
                endpoint=resolved_endpoint,
                model=resolved_model,
                messages=trimmed,
                timeout_s=float(timeout_s),
                max_input_chars=int(max_input_chars or 24000),
            )
        except LocalLlmError as exc:
            raise AgentLlmError(str(exc)) from exc

    if prov == "openai":
        key = str(getattr(settings, "openai_api_key", None) or "").strip()
        mdl = active_llm_model("openai", mdl_override) or "gpt-4o"
        if not key:
            raise AgentLlmError("OpenAI API key is not configured")
        return _openai_chat_completions(
            api_key=key,
            model=mdl,
            messages=trimmed,
            timeout_s=float(timeout_s),
            max_output_tokens=max_output_tokens,
            prompt_cache_key=prompt_cache_key,
            preserve_raw_text=preserve_raw_text,
        )

    if prov == "anthropic":
        key = str(getattr(settings, "anthropic_api_key", None) or "").strip()
        mdl = mdl_override or str(getattr(settings, "anthropic_model", "claude-3-5-sonnet-20241022") or "")
        if not key:
            raise AgentLlmError("Anthropic API key is not configured")
        if not mdl:
            raise AgentLlmError("Anthropic model is not configured")
        return _anthropic_messages(
            api_key=key,
            model=mdl,
            messages=trimmed,
            timeout_s=float(timeout_s),
            max_output_tokens=max_output_tokens,
        )

    if prov == "openrouter":
        key = str(getattr(settings, "openrouter_api_key", None) or "").strip()
        mdl = active_llm_model("openrouter", mdl_override) or "openrouter/free"
        if not key:
            raise AgentLlmError("OpenRouter API key is not configured")
        if not mdl:
            raise AgentLlmError("OpenRouter model is not configured")
        return _openrouter_chat_completions(api_key=key, model=mdl, messages=trimmed, timeout_s=float(timeout_s), max_output_tokens=max_output_tokens, preserve_raw_text=preserve_raw_text)

    if prov == "deepseek":
        key = str(getattr(settings, "deepseek_api_key", None) or "").strip()
        mdl = active_llm_model("deepseek", mdl_override) or _DEEPSEEK_DEFAULT_MODEL
        if not key:
            raise AgentLlmError("DeepSeek API key is not configured")
        if not mdl:
            raise AgentLlmError("DeepSeek model is not configured")
        return _deepseek_chat_completions(
            api_key=key,
            model=mdl,
            messages=trimmed,
            timeout_s=float(timeout_s),
            max_output_tokens=max_output_tokens,
            thinking=True if deepseek_thinking is None else bool(deepseek_thinking),
            reasoning_effort=deepseek_reasoning_effort,
            preserve_raw_text=preserve_raw_text,
        )

    if prov == "nous":
        key = str(getattr(settings, "nous_api_key", None) or "").strip()
        mdl = active_llm_model("nous", mdl_override) or "Hermes-4.3-36B"
        if not key:
            raise AgentLlmError("Nous API key is not configured")
        if not mdl:
            raise AgentLlmError("Nous model is not configured")
        return _nous_chat_completions(api_key=key, model=mdl, messages=trimmed, timeout_s=float(timeout_s), max_output_tokens=max_output_tokens, preserve_raw_text=preserve_raw_text)

    if prov == "meta":
        key = str(getattr(settings, "meta_api_key", None) or "").strip()
        mdl = active_llm_model("meta", mdl_override) or _META_DEFAULT_MODEL
        if not key:
            raise AgentLlmError("Meta Model API key is not configured")
        if not mdl:
            raise AgentLlmError("Meta model is not configured")
        if _meta_api_mode() == "responses":
            return _meta_responses(
                api_key=key,
                model=mdl,
                messages=trimmed,
                timeout_s=float(timeout_s),
                max_output_tokens=max_output_tokens,
                prompt_cache_key=prompt_cache_key,
                prompt_cache_retention=prompt_cache_retention,
                json_output=True,
                preserve_raw_text=preserve_raw_text,
            )
        return _meta_chat_completions(
            api_key=key,
            model=mdl,
            messages=trimmed,
            timeout_s=float(timeout_s),
            max_output_tokens=max_output_tokens,
            prompt_cache_key=prompt_cache_key,
            preserve_raw_text=preserve_raw_text,
        )

    raise AgentLlmError(f"Unsupported AI provider: {prov}")


def chat_text_auto(
    *,
    messages: list[ChatMessage],
    model: str | None = None,
    timeout_s: float = 60.0,
    max_input_chars: int | None = 24000,
    max_output_tokens: int | None = None,
    provider: str | None = None,
    prompt_cache_key: str | None = None,
    prompt_cache_retention: str | None = None,
    deepseek_thinking: bool | None = None,
    deepseek_reasoning_effort: str = "max",
) -> dict[str, Any]:
    trimmed = _trim_messages(messages, max_chars=max_input_chars)
    mdl_override = (model or "").strip() or None
    prov = (provider or getattr(settings, "ai_provider", "openai") or "openai").strip().lower()

    if prov == "openai":
        key = str(getattr(settings, "openai_api_key", None) or "").strip()
        mdl = active_llm_model("openai", mdl_override) or "gpt-4o"
        if not key:
            raise AgentLlmError("OpenAI API key is not configured")
        return _chat_completions_text(
            api_key=key,
            model=mdl,
            messages=trimmed,
            timeout_s=float(timeout_s),
            url=_OPENAI_CHAT_COMPLETIONS_URL,
            provider_label="OpenAI",
            max_output_tokens=max_output_tokens,
        )

    if prov == "anthropic":
        key = str(getattr(settings, "anthropic_api_key", None) or "").strip()
        mdl = mdl_override or str(getattr(settings, "anthropic_model", "claude-3-5-sonnet-20241022") or "")
        if not key:
            raise AgentLlmError("Anthropic API key is not configured")
        if not mdl:
            raise AgentLlmError("Anthropic model is not configured")
        return _anthropic_text(api_key=key, model=mdl, messages=trimmed, timeout_s=float(timeout_s), max_output_tokens=max_output_tokens)

    if prov == "openrouter":
        key = str(getattr(settings, "openrouter_api_key", None) or "").strip()
        mdl = active_llm_model("openrouter", mdl_override) or "openrouter/free"
        if not key:
            raise AgentLlmError("OpenRouter API key is not configured")
        if not mdl:
            raise AgentLlmError("OpenRouter model is not configured")
        return _openrouter_chat_text(api_key=key, model=mdl, messages=trimmed, timeout_s=float(timeout_s), max_output_tokens=max_output_tokens)

    if prov == "deepseek":
        key = str(getattr(settings, "deepseek_api_key", None) or "").strip()
        mdl = active_llm_model("deepseek", mdl_override) or _DEEPSEEK_DEFAULT_MODEL
        if not key:
            raise AgentLlmError("DeepSeek API key is not configured")
        if not mdl:
            raise AgentLlmError("DeepSeek model is not configured")
        return _deepseek_chat_text(
            api_key=key,
            model=mdl,
            messages=trimmed,
            timeout_s=float(timeout_s),
            max_output_tokens=max_output_tokens,
            thinking=True if deepseek_thinking is None else bool(deepseek_thinking),
            reasoning_effort=deepseek_reasoning_effort,
        )

    if prov == "nous":
        key = str(getattr(settings, "nous_api_key", None) or "").strip()
        mdl = active_llm_model("nous", mdl_override) or "Hermes-4.3-36B"
        if not key:
            raise AgentLlmError("Nous API key is not configured")
        if not mdl:
            raise AgentLlmError("Nous model is not configured")
        return _nous_chat_text(api_key=key, model=mdl, messages=trimmed, timeout_s=float(timeout_s), max_output_tokens=max_output_tokens)

    if prov == "meta":
        key = str(getattr(settings, "meta_api_key", None) or "").strip()
        mdl = active_llm_model("meta", mdl_override) or _META_DEFAULT_MODEL
        if not key:
            raise AgentLlmError("Meta Model API key is not configured")
        if not mdl:
            raise AgentLlmError("Meta model is not configured")
        if _meta_api_mode() == "responses":
            return _meta_responses(
                api_key=key,
                model=mdl,
                messages=trimmed,
                timeout_s=float(timeout_s),
                max_output_tokens=max_output_tokens,
                prompt_cache_key=prompt_cache_key,
                prompt_cache_retention=prompt_cache_retention,
                json_output=False,
            )
        return _meta_chat_text(
            api_key=key,
            model=mdl,
            messages=trimmed,
            timeout_s=float(timeout_s),
            max_output_tokens=max_output_tokens,
            prompt_cache_key=prompt_cache_key,
        )

    raise AgentLlmError(f"Unsupported text AI provider: {prov}")


def llm_available(*, endpoint: str | None = None, model: str | None = None, provider: str | None = None) -> bool:
    ep = (endpoint or "").strip()
    mdl = (model or "").strip()
    if ep and mdl:
        return True
    prov = (provider or getattr(settings, "ai_provider", "openai") or "openai").strip().lower()
    if prov == "openai":
        return bool(getattr(settings, "openai_api_key", None)) and bool(getattr(settings, "openai_model", None))
    if prov == "anthropic":
        return bool(getattr(settings, "anthropic_api_key", None)) and bool(getattr(settings, "anthropic_model", None))
    if prov == "openrouter":
        return bool(getattr(settings, "openrouter_api_key", None)) and bool(active_llm_model("openrouter", mdl or None))
    if prov == "deepseek":
        return bool(getattr(settings, "deepseek_api_key", None)) and bool(active_llm_model("deepseek", mdl or None))
    if prov == "nous":
        return bool(getattr(settings, "nous_api_key", None)) and bool(active_llm_model("nous", mdl or None))
    if prov == "meta":
        return bool(getattr(settings, "meta_api_key", None)) and bool(active_llm_model("meta", mdl or None))
    if prov == "local":
        return bool(local_model_endpoint()) and bool(local_model_name())
    return False
