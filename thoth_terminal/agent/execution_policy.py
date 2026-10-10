from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from sqlalchemy.orm import Session

from thoth_terminal.agent.llm_client import AgentLlmError, ChatMessage, chat_json_auto, llm_available
from thoth_terminal.agent_request_manifest_service import manifested_chat_json
from thoth_terminal.config import settings
from thoth_terminal.local_model_runtime import local_model_endpoint, local_model_name


ENGINE_CODEX_NATIVE = "codex_native"
ENGINE_CHATGPT_APP = "chatgpt_app"
ENGINE_API_MODEL = "api_model"
ENGINE_LOCAL_MODEL = "local_model"
ENGINE_LEGACY_INTERNAL = "legacy_internal"
ENGINE_ORDER = [
    ENGINE_CODEX_NATIVE,
    ENGINE_CHATGPT_APP,
    ENGINE_API_MODEL,
    ENGINE_LOCAL_MODEL,
    ENGINE_LEGACY_INTERNAL,
]

TASK_CONTEXT_COMPRESSION = "context_compression"
TASK_DESK_DIGEST = "desk_digest"
TASK_THESIS_AUTOPILOT = "thesis_autopilot"


@dataclass
class SynthesisResult:
    summary: str = ""
    key_points: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    proposed_mutations: list[dict[str, Any]] = field(default_factory=list)
    coverage_gaps: list[str] = field(default_factory=list)
    engine_metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _agent_endpoint() -> str | None:
    return local_model_endpoint()


def _agent_model() -> str | None:
    return local_model_name()


def _available_engines() -> dict[str, bool]:
    endpoint = _agent_endpoint()
    model = _agent_model()
    api_available = llm_available(endpoint=None, model=None, provider=getattr(settings, "ai_provider", "openai"))
    local_available = bool(endpoint and model and llm_available(endpoint=endpoint, model=model, provider="local"))
    return {
        ENGINE_CODEX_NATIVE: bool(getattr(settings, "agent_codex_native_enabled", False)),
        ENGINE_CHATGPT_APP: bool(getattr(settings, "agent_chatgpt_app_enabled", False)),
        ENGINE_API_MODEL: bool(api_available),
        ENGINE_LOCAL_MODEL: bool(local_available),
        ENGINE_LEGACY_INTERNAL: True,
    }


def select_synthesis_engine(*, task_kind: str, requested_engine: str | None = None) -> dict[str, Any]:
    requested = str(requested_engine or getattr(settings, "agent_preferred_synthesis_engine", "") or "").strip().lower() or None
    available = _available_engines()
    policy = str(getattr(settings, "agent_execution_policy", "policy_routed") or "policy_routed").strip().lower()
    if policy not in {"policy_routed", "codex_first", "api_first"}:
        policy = "policy_routed"

    if requested and available.get(requested):
        return {"engine": requested, "requested_engine": requested, "policy": policy, "fallback_chain": [requested]}

    if policy == "codex_first":
        order = [ENGINE_CODEX_NATIVE, ENGINE_CHATGPT_APP, ENGINE_API_MODEL, ENGINE_LOCAL_MODEL, ENGINE_LEGACY_INTERNAL]
    elif policy == "api_first":
        order = [ENGINE_API_MODEL, ENGINE_LOCAL_MODEL, ENGINE_CODEX_NATIVE, ENGINE_CHATGPT_APP, ENGINE_LEGACY_INTERNAL]
    else:
        # Policy-routed: keep Codex/App available for operator surfaces, but server-side tasks still fall back
        # deterministically to in-process engines when external adapters are unavailable.
        if task_kind == TASK_THESIS_AUTOPILOT:
            order = [ENGINE_CODEX_NATIVE, ENGINE_CHATGPT_APP, ENGINE_API_MODEL, ENGINE_LOCAL_MODEL, ENGINE_LEGACY_INTERNAL]
        else:
            order = [ENGINE_CODEX_NATIVE, ENGINE_CHATGPT_APP, ENGINE_API_MODEL, ENGINE_LOCAL_MODEL, ENGINE_LEGACY_INTERNAL]

    for engine in order:
        if available.get(engine):
            return {"engine": engine, "requested_engine": requested, "policy": policy, "fallback_chain": order}
    return {"engine": ENGINE_LEGACY_INTERNAL, "requested_engine": requested, "policy": policy, "fallback_chain": order}


def _normalize_llm_payload(payload: dict[str, Any], *, engine: str, task_kind: str) -> SynthesisResult:
    evidence_rows: list[dict[str, Any]] = []
    for item in payload.get("evidence") or []:
        if not isinstance(item, dict):
            continue
        evidence_rows.append(
            {
                "title": str(item.get("title") or "").strip(),
                "source": str(item.get("source") or "").strip(),
                "url": str(item.get("url") or "").strip(),
                "document_id": int(item.get("document_id")) if str(item.get("document_id")).isdigit() else None,
                "precedence_bucket": str(item.get("precedence_bucket") or "").strip() or None,
            }
        )
    mutations: list[dict[str, Any]] = []
    for item in payload.get("proposed_mutations") or []:
        if isinstance(item, dict):
            mutations.append(dict(item))
    return SynthesisResult(
        summary=str(payload.get("summary") or "").strip(),
        key_points=[str(v).strip() for v in (payload.get("key_points") or []) if str(v).strip()][:8],
        actions=[str(v).strip() for v in (payload.get("actions") or []) if str(v).strip()][:8],
        risks=[str(v).strip() for v in (payload.get("risks") or []) if str(v).strip()][:8],
        evidence=evidence_rows[:12],
        proposed_mutations=mutations[:24],
        coverage_gaps=[str(v).strip() for v in (payload.get("coverage_gaps") or []) if str(v).strip()][:8],
        engine_metadata={
            "engine": engine,
            "task_kind": task_kind,
            "llm_used": engine in {ENGINE_API_MODEL, ENGINE_LOCAL_MODEL},
        },
    )


def run_synthesis_task(
    *,
    task_kind: str,
    prompt: str,
    fallback_factory: Callable[[], SynthesisResult],
    requested_engine: str | None = None,
    audit_session: Session | None = None,
) -> SynthesisResult:
    selection = select_synthesis_engine(task_kind=task_kind, requested_engine=requested_engine)
    engine = str(selection.get("engine") or ENGINE_LEGACY_INTERNAL)
    if engine in {ENGINE_API_MODEL, ENGINE_LOCAL_MODEL}:
        endpoint = _agent_endpoint() if engine == ENGINE_LOCAL_MODEL else None
        model = _agent_model() if engine == ENGINE_LOCAL_MODEL else None
        provider = "local" if engine == ENGINE_LOCAL_MODEL else getattr(settings, "ai_provider", "openai")
        try:
            dispatch = manifested_chat_json if audit_session is not None else chat_json_auto
            dispatch_kwargs: dict[str, Any] = {}
            if audit_session is not None:
                dispatch_kwargs.update(
                    {
                        "session": audit_session,
                        "run_kind": "synthesis_task",
                        "run_id": None,
                        "task_kind": task_kind,
                        "permission_profile": "propose-only",
                        "prompt_cache_key": f"thoth-synthesis-{task_kind}-v1",
                    }
                )
            parsed = dispatch(
                messages=[
                    ChatMessage(role="system", content="Return JSON only."),
                    ChatMessage(role="user", content=prompt),
                ],
                endpoint=endpoint,
                model=model,
                timeout_s=float(getattr(settings, "agent_llm_timeout_s", 60) or 60),
                max_input_chars=int(getattr(settings, "agent_llm_max_input_chars", 24000) or 24000),
                provider=provider,
                **dispatch_kwargs,
            )
            result = _normalize_llm_payload(parsed, engine=engine, task_kind=task_kind)
            result.engine_metadata.update(
                {
                    "policy": selection.get("policy"),
                    "requested_engine": selection.get("requested_engine"),
                    "fallback_chain": list(selection.get("fallback_chain") or []),
                }
            )
            return result
        except AgentLlmError as exc:
            result = fallback_factory()
            result.engine_metadata.update(
                {
                    "engine": ENGINE_LEGACY_INTERNAL,
                    "task_kind": task_kind,
                    "policy": selection.get("policy"),
                    "requested_engine": selection.get("requested_engine"),
                    "fallback_chain": list(selection.get("fallback_chain") or []),
                    "fallback_reason": str(exc),
                    "selected_engine": engine,
                    "llm_used": False,
                }
            )
            return result

    result = fallback_factory()
    result.engine_metadata.update(
        {
            "engine": engine,
            "task_kind": task_kind,
            "policy": selection.get("policy"),
            "requested_engine": selection.get("requested_engine"),
            "fallback_chain": list(selection.get("fallback_chain") or []),
            "llm_used": False,
        }
    )
    if engine in {ENGINE_CODEX_NATIVE, ENGINE_CHATGPT_APP}:
        result.engine_metadata["fallback_reason"] = "external_engine_unavailable_in_process"
    return result
