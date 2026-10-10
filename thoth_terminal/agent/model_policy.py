from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ModelTaskPolicy:
    task_kind: str
    label: str
    min_intelligence: float | None = None
    min_agentic: float | None = None
    min_context_length: int | None = None
    allow_auto_router: bool = True
    require_text_output: bool = True
    allow_unknown_scores: bool = True


TASK_POLICIES: dict[str, ModelTaskPolicy] = {
    "cloud_chat": ModelTaskPolicy(
        task_kind="cloud_chat",
        label="Cloud chat",
        allow_auto_router=True,
        allow_unknown_scores=True,
    ),
    "gap_hunter": ModelTaskPolicy(
        task_kind="gap_hunter",
        label="Gap Hunter",
        min_intelligence=45,
        min_agentic=50,
        min_context_length=64_000,
        allow_auto_router=False,
        allow_unknown_scores=True,
    ),
    "red_team": ModelTaskPolicy(
        task_kind="red_team",
        label="Red Team",
        min_intelligence=45,
        min_agentic=50,
        min_context_length=64_000,
        allow_auto_router=False,
        allow_unknown_scores=True,
    ),
    "report_body_builder": ModelTaskPolicy(
        task_kind="report_body_builder",
        label="Report Body Builder",
        min_intelligence=45,
        min_agentic=50,
        min_context_length=128_000,
        allow_auto_router=False,
        allow_unknown_scores=True,
    ),
    "source_pack_builder": ModelTaskPolicy(
        task_kind="source_pack_builder",
        label="Source Pack Builder",
        min_intelligence=45,
        min_agentic=50,
        min_context_length=64_000,
        allow_auto_router=False,
        allow_unknown_scores=True,
    ),
    "bottleneck_constraint": ModelTaskPolicy(
        task_kind="bottleneck_constraint",
        label="Bottleneck / Constraint",
        min_intelligence=45,
        min_agentic=50,
        min_context_length=128_000,
        allow_auto_router=False,
        allow_unknown_scores=True,
    ),
    "cloud_research_nightly": ModelTaskPolicy(
        task_kind="cloud_research_nightly",
        label="Cloud Research Nightly",
        min_intelligence=40,
        min_agentic=45,
        min_context_length=128_000,
        allow_auto_router=True,
        allow_unknown_scores=True,
    ),
    "cloud_research_heartbeat": ModelTaskPolicy(
        task_kind="cloud_research_heartbeat",
        label="Cloud Research Heartbeat",
        min_intelligence=40,
        min_agentic=45,
        min_context_length=128_000,
        allow_auto_router=True,
        allow_unknown_scores=True,
    ),
    "cloud_research_inquisitive": ModelTaskPolicy(
        task_kind="cloud_research_inquisitive",
        label="Cloud Research Inquisitive",
        min_intelligence=40,
        min_agentic=45,
        min_context_length=128_000,
        allow_auto_router=True,
        allow_unknown_scores=True,
    ),
    "cloud_orchestrator": ModelTaskPolicy(
        task_kind="cloud_orchestrator",
        label="Cloud Orchestrator",
        min_intelligence=40,
        min_agentic=45,
        min_context_length=128_000,
        allow_auto_router=True,
        allow_unknown_scores=True,
    ),
}


def task_policy(task_kind: str | None) -> ModelTaskPolicy:
    return TASK_POLICIES.get(str(task_kind or "").strip(), TASK_POLICIES["cloud_chat"])


def _float(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except Exception:
        return None


def _int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except Exception:
        return None


def _agentic_score(row: dict[str, Any]) -> float | None:
    direct = _float(row.get("agentic_score"))
    if direct is not None:
        return direct
    aa = row.get("artificial_analysis") if isinstance(row.get("artificial_analysis"), dict) else {}
    return _float(aa.get("agentic_index"))


def evaluate_model_for_task(row: dict[str, Any] | None, policy: ModelTaskPolicy) -> dict[str, Any]:
    if row is None:
        return {
            "ok": True,
            "policy": policy.task_kind,
            "warnings": ["Model is not in the current catalog; task policy could not verify AA scores or context."],
            "failures": [],
        }
    failures: list[str] = []
    warnings: list[str] = []
    model = str(row.get("model") or "")
    if policy.require_text_output:
        output_modalities = {
            str(item).strip().lower()
            for item in row.get("output_modalities", [])
            if str(item).strip()
        }
        if not bool(row.get("text_output", True)) or (output_modalities and ("text" not in output_modalities or output_modalities - {"text"})):
            failures.append("model must produce text output")
    if not policy.allow_auto_router and bool(row.get("auto")):
        failures.append(f"{policy.label} requires an explicit model, not an auto router")
    if policy.min_context_length is not None:
        context_length = _int(row.get("context_length"))
        if context_length is None:
            warnings.append(f"context length unknown; target floor is {policy.min_context_length}")
        elif context_length < policy.min_context_length:
            failures.append(f"context length {context_length} is below floor {policy.min_context_length}")
    intelligence = _float(row.get("intelligence_score"))
    if policy.min_intelligence is not None:
        if intelligence is None:
            if policy.allow_unknown_scores:
                warnings.append(f"AA intelligence score unknown; target floor is {policy.min_intelligence}")
            else:
                failures.append(f"AA intelligence score is required and must be >= {policy.min_intelligence}")
        elif intelligence < policy.min_intelligence:
            failures.append(f"AA intelligence score {intelligence:g} is below floor {policy.min_intelligence:g}")
    agentic = _agentic_score(row)
    if policy.min_agentic is not None:
        if agentic is None:
            warnings.append(f"AA agentic score unknown; preferred floor is {policy.min_agentic}")
        elif agentic < policy.min_agentic:
            warnings.append(f"AA agentic score {agentic:g} is below preferred floor {policy.min_agentic:g}")
    return {
        "ok": not failures,
        "policy": policy.task_kind,
        "model": model,
        "warnings": warnings,
        "failures": failures,
        "min_intelligence": policy.min_intelligence,
        "min_agentic": policy.min_agentic,
        "min_context_length": policy.min_context_length,
    }


def annotate_model_catalog_for_tasks(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        row["task_policy"] = {
            key: evaluate_model_for_task(row, policy)
            for key, policy in TASK_POLICIES.items()
        }
