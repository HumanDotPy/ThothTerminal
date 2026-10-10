from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from thoth_terminal.agent.llm_client import AgentLlmError, ChatMessage, llm_metadata
from thoth_terminal.agent_request_manifest_service import manifested_chat_json
from thoth_terminal.agent.openshell import openshell_runtime_status
from thoth_terminal.config import settings
from thoth_terminal.db import session_scope
from thoth_terminal.discord import public_dto
from thoth_terminal.source_pack_service import build_source_pack, render_source_pack_markdown


@dataclass(frozen=True)
class PublicAnswer:
    answer: str
    key_points: list[str]
    sources: list[dict[str, str]]
    risks: list[str]
    unknowns: list[str]
    provider: str
    model: str
    requested_model: str
    submitted_model: str


class PublicAskError(RuntimeError):
    pass


def _configured_ask_model(model: str | None = None) -> str:
    requested = str(model or "").strip()
    if requested:
        return requested
    configured = str(getattr(settings, "discord_ask_model", None) or "").strip()
    if configured:
        return configured
    return str(getattr(settings, "openrouter_free_model", "openrouter/free") or "openrouter/free").strip()


def _validate_openrouter_free_model(model: str) -> None:
    if not bool(getattr(settings, "openrouter_enforce_free_only", True)):
        return
    if model == "openrouter/free" or model.endswith(":free"):
        return
    raise PublicAskError("OpenRouter free-only mode requires `openrouter/free` or a `:free` model.")


def _require_openshell_ready() -> dict[str, Any]:
    if not bool(getattr(settings, "openshell_enabled", True)):
        raise PublicAskError("OpenShell is disabled for cloud Q&A.")
    status = openshell_runtime_status(probe_gateway=True)
    if not bool(status.get("ready")):
        detail = str(status.get("detail") or status.get("status") or "OpenShell is not ready.").strip()
        raise PublicAskError(f"OpenShell is not ready for public Q&A: {detail}")
    return status


def _build_thoth_context(*, question: str, subject: str | None = None, limit: int) -> str:
    query = (subject or question or "").strip()
    if not query:
        return "- No Thoth Terminal query was provided."
    try:
        with session_scope() as session:
            pack = build_source_pack(
                session,
                query=query,
                subject=subject,
                limit=max(6, min(int(limit or 8), 14)),
                include_object_search=True,
            )
        return render_source_pack_markdown(pack)
    except Exception as exc:
        return f"- Thoth Terminal context search failed: {type(exc).__name__}: {str(exc)[:240]}"


def build_public_context(*, guild_id: int, question: str, subject: str | None = None, limit: int | None = None) -> str:
    query = (subject or question or "").strip()
    limit_v = max(3, min(int(limit or getattr(settings, "discord_ask_context_limit", 8) or 8), 12))
    rows = public_dto.search_public_memory(guild_id=int(guild_id), query=query, limit=limit_v)
    lines: list[str] = [
        "Context boundary:",
        "- Search and answer from the Thoth Terminal context below before falling back to uncertainty.",
        "- Thoth Terminal may contain information newer than the model's training cutoff. Treat dated Thoth evidence as current for this answer.",
        "- Do not reject a question just because the base model believes an event is in the future; first inspect the dated Thoth context.",
        "- Use approved public snippets and Thoth source-pack/object-search snippets below.",
        "- Do not mention private Thoth internals, local runtime state, operator notes, jobs, queues, or file paths.",
        "- If the context is insufficient, say what is missing instead of guessing.",
        "",
        "Approved public context:",
    ]
    if not rows:
        lines.append("- No approved public context matched the question.")
    else:
        for idx, row in enumerate(rows, start=1):
            bits = [f"[{idx}] {row.kind}: {row.title}"]
            if row.symbol:
                bits.append(f"symbol={row.symbol}")
            if row.url:
                bits.append(f"url={row.url}")
            if row.summary:
                bits.append(f"summary={row.summary}")
            lines.append("- " + " | ".join(bits))
    lines.extend(["", "Thoth Terminal source-pack/object-search context:"])
    lines.append(_build_thoth_context(question=question, subject=subject, limit=limit_v))
    return "\n".join(lines)


def _string_list(value: object, *, limit: int = 6) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _source_list(value: object, *, limit: int = 6) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, str]] = []
    for item in value:
        if isinstance(item, dict):
            title = str(item.get("title") or item.get("label") or item.get("source") or "").strip()
            url = str(item.get("url") or "").strip()
        else:
            title = str(item or "").strip()
            url = ""
        if title or url:
            out.append({"title": title or url, "url": url})
        if len(out) >= limit:
            break
    return out


def ask_public_question(
    *,
    guild_id: int,
    question: str,
    subject: str | None = None,
    model: str | None = None,
) -> PublicAnswer:
    prompt = str(question or "").strip()
    if not prompt:
        raise PublicAskError("Question is required.")
    selected_model = _configured_ask_model(model)
    _validate_openrouter_free_model(selected_model)
    _require_openshell_ready()
    context = build_public_context(guild_id=int(guild_id), question=prompt, subject=subject)
    system_prompt = (
        "Return JSON only with keys answer, key_points, sources, risks, unknowns. "
        "You are Thoth Public Research, a guild-facing finance research assistant. "
        "Your job is to help investors understand what matters now from approved public research memory: "
        "evidence, reports, company briefs, watchlists, transcripts, risks, catalysts, and supply-chain exposure. "
        "The Thoth Terminal context in the user message may include information after your training cutoff; "
        "use that dated context as the evidence base instead of refusing or saying an event is future-only. "
        "For current-event questions such as Computex 2026, first search the provided Thoth context and summarize "
        "what it says was announced; only say evidence is missing if the context actually lacks relevant items. "
        "Write like a concise buy-side analyst: distinguish facts from inference, identify the variant issue, "
        "name the missing evidence, and avoid generic market commentary. "
        "Use only the provided public/Thoth context and cite sources from that context when available. "
        "Never expose Thoth internals, local runtime details, private notes, queues, jobs, file paths, "
        "operator-only workflows, or implementation names."
    )
    user_prompt = f"{context}\n\nQuestion:\n{prompt}"
    try:
        with session_scope() as audit_session:
            result = manifested_chat_json(
                audit_session,
                run_kind="discord_public_qa",
                run_id=int(guild_id),
                task_kind="public_research_question",
                messages=[
                    ChatMessage(role="system", content=system_prompt),
                    ChatMessage(role="user", content=user_prompt),
                ],
                provider="openrouter",
                model=selected_model,
                timeout_s=float(getattr(settings, "discord_ask_timeout_s", 30) or 30),
                max_input_chars=None,
                max_output_tokens=None,
                permission_profile="public-context-read-only",
            )
    except AgentLlmError as exc:
        raise PublicAskError(str(exc)) from exc
    meta = llm_metadata(result)
    routed_model = meta.get("model") or selected_model
    submitted_model = meta.get("submitted_model") or selected_model
    answer = str(result.get("answer") or result.get("summary") or "").strip()
    if not answer:
        raise PublicAskError("Cloud Q&A returned no answer.")
    return PublicAnswer(
        answer=answer,
        key_points=_string_list(result.get("key_points")),
        sources=_source_list(result.get("sources")),
        risks=_string_list(result.get("risks")),
        unknowns=_string_list(result.get("unknowns") or result.get("coverage_gaps")),
        provider="openrouter",
        model=routed_model,
        requested_model=selected_model,
        submitted_model=submitted_model,
    )
