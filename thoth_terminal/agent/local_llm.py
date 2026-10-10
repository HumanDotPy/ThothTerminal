from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import requests

from thoth_terminal.local_model_runtime import local_model_headers, normalize_local_model_endpoint


class LocalLlmError(RuntimeError):
    pass


@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: Any


def _content_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(
            str(item.get("text") or "")
            for item in value
            if isinstance(item, dict) and item.get("type") in {"text", "input_text"}
        )
    return str(value or "")


def _trim_messages(messages: list[ChatMessage], *, max_chars: int) -> list[ChatMessage]:
    max_chars = max(1000, int(max_chars))
    out: list[ChatMessage] = []
    remaining = max_chars
    # Keep the last messages (usually most relevant).
    for msg in reversed(messages):
        if remaining <= 0:
            break
        content = _content_text(msg.content)
        if len(content) > remaining:
            content = content[-remaining:]
        if isinstance(msg.content, list) and len(content) < len(_content_text(msg.content)):
            # Bounded local requests are text-only; preserve the text slice as a
            # normal message instead of silently sending malformed image blocks.
            out.append(ChatMessage(role=msg.role, content=content))
        else:
            out.append(ChatMessage(role=msg.role, content=msg.content))
        remaining -= len(content)
    return list(reversed(out))


def _post_json(url: str, payload: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
    try:
        resp = requests.post(url, json=payload, headers=local_model_headers(), timeout=timeout_s)
    except requests.RequestException as exc:
        raise LocalLlmError(f"Local LLM request failed: {exc}") from exc
    if resp.status_code >= 400:
        raise LocalLlmError(f"Local LLM HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        data = resp.json()
    except Exception as exc:
        raise LocalLlmError("Local LLM returned invalid JSON") from exc
    if not isinstance(data, dict):
        raise LocalLlmError("Local LLM returned unexpected payload")
    return data


def chat_json(
    *,
    endpoint: str,
    model: str,
    messages: list[ChatMessage],
    timeout_s: float = 60.0,
    max_input_chars: int = 24000,
) -> dict[str, Any]:
    """
    Best-effort local LLM call returning a parsed JSON object.

    Supports:
    - OpenAI-compatible: POST {endpoint}/v1/chat/completions
    - Ollama native:     POST {endpoint}/api/chat
    """
    ep = normalize_local_model_endpoint(endpoint)
    mdl = (model or "").strip()
    if not ep:
        raise LocalLlmError("Missing endpoint.")
    if not mdl:
        raise LocalLlmError("Missing model.")

    msgs = _trim_messages(messages, max_chars=int(max_input_chars))
    msgs_dict = [{"role": m.role, "content": m.content} for m in msgs]

    # 1) Try OpenAI-compatible.
    openai_url = f"{ep}/v1/chat/completions"
    try:
        data = _post_json(
            openai_url,
            {
                "model": mdl,
                "messages": msgs_dict,
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
            },
            timeout_s=float(timeout_s),
        )
        choices = data.get("choices") or []
        if isinstance(choices, list) and choices:
            msg = (choices[0] or {}).get("message") or {}
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                try:
                    parsed = json.loads(content)
                except Exception as exc:
                    raise LocalLlmError("OpenAI-compatible endpoint returned non-JSON content") from exc
                if isinstance(parsed, dict):
                    return parsed
        raise LocalLlmError("OpenAI-compatible endpoint returned unexpected response")
    except LocalLlmError as exc:
        # Only fall back on "route not found"-style failures.
        msg = str(exc).lower()
        if "http 404" not in msg and "http 405" not in msg:
            raise

    # 2) Fallback: Ollama native /api/chat.
    ollama_url = f"{ep}/api/chat"
    data = _post_json(
        ollama_url,
        {
            "model": mdl,
            "messages": msgs_dict,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.2},
        },
        timeout_s=float(timeout_s),
    )
    message = data.get("message") or {}
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise LocalLlmError("Ollama endpoint returned empty content")
    try:
        parsed = json.loads(content)
    except Exception as exc:
        raise LocalLlmError("Ollama endpoint returned non-JSON content") from exc
    if not isinstance(parsed, dict):
        raise LocalLlmError("Ollama endpoint returned unexpected JSON")
    return parsed
