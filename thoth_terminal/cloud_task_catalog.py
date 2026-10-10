from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class CloudTaskCapability:
    task_id: str
    label: str
    summary: str
    model_tier: str
    task_kind: str
    input_contract: list[str]
    output_contract: list[str]
    allowed_writes: list[str]
    validation: list[str]
    escalation: list[str]
    api_surface: str | None = None


TASK_CAPABILITIES: tuple[CloudTaskCapability, ...] = (
    CloudTaskCapability(
        task_id="source_pack_builder",
        label="Source Pack Builder",
        summary="Compile a compact, sourced context pack for a company, product, material, bottleneck, or question.",
        model_tier="flash",
        task_kind="source_pack_builder",
        input_contract=["query or subject", "optional report_id", "bounded retrieval limit"],
        output_contract=["source pack markdown", "source relevance notes", "coverage gaps"],
        allowed_writes=["cloud_goal_run", "cloud_goal_event"],
        validation=["sources are cited by local ids or titles", "pack stays compact", "claims are not promoted to durable conclusions"],
        escalation=["query requires final thesis", "source pack exposes a bottleneck model gap", "source quality is too weak"],
        api_surface="/api/agents/source-pack/runs",
    ),
    CloudTaskCapability(
        task_id="gap_hunter",
        label="Gap Hunter",
        summary="Find missing definitions, missing company coverage, missing product mechanics, and decisive unknowns.",
        model_tier="flash",
        task_kind="gap_hunter",
        input_contract=["objective or question", "optional keyword/universe/section", "coverage index"],
        output_contract=["ranked gaps", "search/context requests", "candidate work items"],
        allowed_writes=["cloud_goal_run", "cloud_goal_event", "optional candidate persistence"],
        validation=["gaps are specific", "gaps identify why they matter", "does not invent missing evidence"],
        escalation=["gap requires final report writing", "gap requires adversarial review", "gap requires durable write approval"],
        api_surface="/api/agents/gap-hunter/runs",
    ),
    CloudTaskCapability(
        task_id="red_team",
        label="Red Team",
        summary="Challenge a report, thesis, bottleneck classification, source quality, and hidden assumptions.",
        model_tier="strong",
        task_kind="red_team",
        input_contract=["report_id or subject", "report markdown", "source/evidence context"],
        output_contract=["findings", "severity", "falsification checks", "revision recommendations"],
        allowed_writes=["cloud_goal_run", "cloud_goal_event"],
        validation=["findings cite the challenged claim", "severity is calibrated", "missing evidence is separated from contradiction"],
        escalation=["technical synthesis needed", "report should be rewritten", "source pack needed first"],
        api_surface="/api/agents/red-team/runs",
    ),
    CloudTaskCapability(
        task_id="report_body_builder",
        label="Company Report Body Builder",
        summary="Write a technically serious company report body from existing report memory and evidence context.",
        model_tier="strong",
        task_kind="report_body_builder",
        input_contract=["report_id or company subject", "existing report memory", "evidence/source context"],
        output_contract=["body markdown", "assumptions", "gaps", "recommended checks"],
        allowed_writes=["cloud_goal_run", "cloud_goal_event", "optional report addendum"],
        validation=["technical mechanics are present", "supported/assumption/missing labels are used", "public vs structural roadmap is separated"],
        escalation=["red team needed", "source pack missing", "bottleneck report needed"],
        api_surface="/api/agents/report-body/runs",
    ),
    CloudTaskCapability(
        task_id="bottleneck_constraint",
        label="Bottleneck / Constraint Report",
        summary="Build or scope a bottleneck/constraint report program with model plan, evidence needs, and report state.",
        model_tier="strong",
        task_kind="bottleneck_constraint",
        input_contract=["query, report_id, or subject", "existing report context", "optional supply-chain context"],
        output_contract=["report state", "constraint model plan", "gap hunter requests", "challenger requests"],
        allowed_writes=["cloud_goal_run", "cloud_goal_event"],
        validation=["bottleneck vs supply crunch vs extreme demand is separated", "model variables and missing evidence are explicit"],
        escalation=["needs model builder", "needs evidence harvesting", "needs challenger pass"],
        api_surface="/api/agents/bottleneck/runs",
    ),
    CloudTaskCapability(
        task_id="cloud_research_heartbeat",
        label="Unified Cloud Research Steward",
        summary="Cache-friendly long-context steward that reconciles deltas, updates roadmap/models/memory, runs an inquisitive gap pass, and emits one deduplicated dispatch.",
        model_tier="flash",
        task_kind="cloud_research_heartbeat",
        input_contract=["lookback_hours", "recent context bundle", "local-agent daily summary context"],
        output_contract=["dispatch markdown", "priority tasks", "watch items", "coverage gaps"],
        allowed_writes=["cloud_goal_run", "cloud_goal_event", "daily summary dispatch"],
        validation=["does not expose internals", "does not claim durable writes it did not perform", "tasks are bounded"],
        escalation=["final synthesis needed", "write approval needed", "source confidence is weak"],
        api_surface=None,
    ),
    CloudTaskCapability(
        task_id="cloud_research_inquisitive",
        label="Cloud Research Inquisitive Pass (On Demand)",
        summary="Operator-invoked sideways research pass for overlooked objects, adjacent reports, definitions, and source-pack opportunities; not scheduled by default.",
        model_tier="flash",
        task_kind="cloud_research_inquisitive",
        input_contract=["lookback_hours", "recent context bundle", "local-agent work context"],
        output_contract=["dispatch markdown", "sideways tasks", "coverage gaps", "watch items"],
        allowed_writes=["cloud_goal_run", "cloud_goal_event", "daily summary dispatch"],
        validation=["sideways moves are concrete", "small tasks are not mistaken for final reports"],
        escalation=["needs bottleneck report", "needs red team", "needs strong report writer"],
        api_surface=None,
    ),
)


def list_task_capabilities() -> list[dict[str, Any]]:
    return [asdict(item) for item in TASK_CAPABILITIES]


def get_task_capability(task_id: str) -> dict[str, Any] | None:
    wanted = str(task_id or "").strip()
    for item in TASK_CAPABILITIES:
        if item.task_id == wanted or item.task_kind == wanted:
            return asdict(item)
    return None


def task_catalog_markdown() -> str:
    lines = ["# Cloud Task Capability Catalog", ""]
    for item in TASK_CAPABILITIES:
        lines.append(f"## {item.task_id} - {item.label}")
        lines.append(f"- model_tier: {item.model_tier}")
        lines.append(f"- task_kind: {item.task_kind}")
        if item.api_surface:
            lines.append(f"- api_surface: {item.api_surface}")
        lines.append(f"- summary: {item.summary}")
        lines.append("- input_contract: " + "; ".join(item.input_contract))
        lines.append("- output_contract: " + "; ".join(item.output_contract))
        lines.append("- allowed_writes: " + "; ".join(item.allowed_writes))
        lines.append("- validation: " + "; ".join(item.validation))
        lines.append("- escalation: " + "; ".join(item.escalation))
        lines.append("")
    return "\n".join(lines).strip()
