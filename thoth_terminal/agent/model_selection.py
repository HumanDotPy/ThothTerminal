from __future__ import annotations

from typing import Any

from thoth_terminal.config import settings


def _num(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except Exception:
        return None


def _int_value(value: object) -> int:
    try:
        return int(value) if value is not None else 0
    except Exception:
        return 0


def _text_modalities(row: dict[str, Any]) -> bool:
    if not bool(row.get("text_output", True)):
        return False
    output_modalities = {
        str(item).strip().lower()
        for item in row.get("output_modalities", [])
        if str(item).strip()
    }
    return not output_modalities or ("text" in output_modalities and not (output_modalities - {"text"}))


def _task_ok(row: dict[str, Any], task_kind: str) -> bool:
    policy = row.get("task_policy") if isinstance(row.get("task_policy"), dict) else {}
    task_eval = policy.get(task_kind) if isinstance(policy.get(task_kind), dict) else {}
    failures = task_eval.get("failures") if isinstance(task_eval.get("failures"), list) else []
    return not failures


def select_openrouter_free_model(catalog: dict[str, Any], *, task_kind: str) -> dict[str, Any] | None:
    """Pick a concrete OpenRouter free model for a cloud task.

    `openrouter/free` is an auto-router, which hides the real model and can route to
    weak or unsuitable models. Cloud research tasks should instead submit a concrete
    free model selected from the catalog using task policy and AA enrichment.
    """

    candidates: list[dict[str, Any]] = []
    for raw in catalog.get("rows") or []:
        if not isinstance(raw, dict):
            continue
        row = raw
        if str(row.get("provider") or "").lower() != "openrouter":
            continue
        model = str(row.get("model") or "").strip()
        if not model or model == "openrouter/free" or bool(row.get("auto")):
            continue
        if not (bool(row.get("free")) or model.endswith(":free")):
            continue
        if not _text_modalities(row):
            continue
        if not _task_ok(row, task_kind):
            continue
        candidates.append(row)

    if not candidates:
        return None

    preferred = [
        "moonshotai/kimi-k2.6:free",
        "nvidia/nemotron-3-ultra-550b-a55b:free",
        "nvidia/nemotron-3-super-120b-a12b:free",
        "deepseek/deepseek-v3.1-nex-n1:free",
        "qwen/qwen3-next-80b-a3b-instruct:free",
        "qwen/qwen3-coder:free",
        "openai/gpt-oss-120b:free",
    ]
    preferred.extend(
        part.strip()
        for part in str(getattr(settings, "openrouter_free_fallback_models", "") or "").replace("\n", ",").split(",")
        if part.strip()
    )
    preferred_rank: dict[str, int] = {}
    for idx, model in enumerate(preferred):
        preferred_rank.setdefault(model, len(preferred) - idx)

    def score(row: dict[str, Any]) -> tuple[float, float, int, int, int, int, str]:
        intelligence = _num(row.get("intelligence_score"))
        aa = row.get("artificial_analysis") if isinstance(row.get("artificial_analysis"), dict) else {}
        agentic = _num(row.get("agentic_score")) or _num(aa.get("agentic_index"))
        context = _int_value(row.get("context_length"))
        routing_bias = _int_value(row.get("routing_bias"))
        known = 1 if intelligence is not None else 0
        model = str(row.get("model") or "")
        return (
            float(known),
            float(intelligence or 0),
            int(agentic or 0),
            preferred_rank.get(model, 0),
            context,
            routing_bias,
            model,
        )

    return sorted(candidates, key=score, reverse=True)[0]
