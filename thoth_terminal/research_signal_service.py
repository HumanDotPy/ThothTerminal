from __future__ import annotations

from typing import Any

from thoth_terminal.models import Thesis


THESIS_WORKFLOW_STATUSES: tuple[str, ...] = ("watching", "active", "confirmed", "disproven", "paused")
THESIS_HIDDEN_STATUSES: frozenset[str] = frozenset({"archived", "duplicate", "merged", "superseded"})


RESEARCH_SIGNAL_KINDS: dict[str, dict[str, Any]] = {
    "claim": {
        "label": "Claim",
        "summary": "A compact assertion that may later graduate into a stronger thesis.",
        "artifact_shape": "atomic",
        "use_when": "Use for reusable claims, event-card style assertions, and evidence-backed statements.",
        "evidence_rules": ["Needs at least one provenance link before it should influence reports."],
        "autopilot_bias": {"semantic_fit": 0.45, "freshness": 0.35, "source_quality": 0.20},
    },
    "thesis": {
        "label": "Thesis",
        "summary": "A durable research view that should evolve with supporting and opposing evidence.",
        "artifact_shape": "durable_view",
        "use_when": "Use for investable or strategic views that should be monitored over time.",
        "evidence_rules": ["Track support, contradiction, and supersession separately when possible."],
        "autopilot_bias": {"semantic_fit": 0.50, "freshness": 0.30, "source_quality": 0.20},
    },
    "scenario": {
        "label": "Scenario",
        "summary": "A plausible future path or conditional outcome.",
        "artifact_shape": "conditional",
        "use_when": "Use when the object is a possible future state rather than a current assertion.",
        "evidence_rules": ["Make assumptions and time horizon explicit."],
        "autopilot_bias": {"semantic_fit": 0.45, "freshness": 0.40, "source_quality": 0.15},
    },
    "watchpoint": {
        "label": "Watchpoint",
        "summary": "A monitored condition that should trigger review when evidence changes.",
        "artifact_shape": "monitor",
        "use_when": "Use for things an agent or human should keep checking.",
        "evidence_rules": ["Prefer fresh evidence and explicit trigger conditions."],
        "autopilot_bias": {"semantic_fit": 0.35, "freshness": 0.50, "source_quality": 0.15},
    },
    "dependency": {
        "label": "Dependency",
        "summary": "A relationship where one outcome depends on another bottleneck, supplier, or event.",
        "artifact_shape": "relationship",
        "use_when": "Use for 'X depends on Y' logic in company, supply-chain, or roadmap work.",
        "evidence_rules": ["Link both the dependent object and the enabling or blocking object."],
        "autopilot_bias": {"semantic_fit": 0.50, "freshness": 0.30, "source_quality": 0.20},
    },
    "bottleneck": {
        "label": "Bottleneck",
        "summary": "A gating constraint such as capacity, packaging, qualification, yield, power, or regulation.",
        "artifact_shape": "constraint",
        "use_when": "Use when the main value is identifying what blocks scale or adoption.",
        "evidence_rules": ["Bind metrics or supply-chain nodes when available."],
        "autopilot_bias": {"semantic_fit": 0.45, "freshness": 0.35, "source_quality": 0.20},
    },
    "probe": {
        "label": "Probe",
        "summary": "A deliberately weak or exploratory question that should not be over-weighted.",
        "artifact_shape": "question",
        "use_when": "Use when testing whether a pattern exists.",
        "evidence_rules": ["Do not promote without stronger semantic evidence."],
        "autopilot_bias": {"semantic_fit": 0.60, "freshness": 0.25, "source_quality": 0.15},
    },
    "deep_dive": {
        "label": "Deep Dive",
        "summary": "A long-form research synthesis, such as advanced packaging technology comparisons.",
        "artifact_shape": "long_form",
        "use_when": "Use for detailed industry, technology, or architecture work that spans many claims.",
        "evidence_rules": ["Needs sections, source coverage, linked notes/reports, and ideally datasets."],
        "autopilot_bias": {"semantic_fit": 0.50, "freshness": 0.30, "source_quality": 0.20},
    },
    "company_research": {
        "label": "Company Research",
        "summary": "A durable company-specific research view, including cycle, product, and roadmap work.",
        "artifact_shape": "company_profile",
        "use_when": "Use for objects like a Nanya research view that mixes facts, cycle thesis, and scenarios.",
        "evidence_rules": ["Prefer issuer evidence, datasets, product-stage entries, and explicit scenario framing."],
        "autopilot_bias": {"semantic_fit": 0.45, "freshness": 0.35, "source_quality": 0.20},
    },
    "event": {
        "label": "Event",
        "summary": "A dated occurrence or event-card style signal.",
        "artifact_shape": "event_card",
        "use_when": "Use when the date and event type matter as much as the interpretation.",
        "evidence_rules": ["Event date, provenance, and affected entities should be explicit."],
        "autopilot_bias": {"semantic_fit": 0.35, "freshness": 0.50, "source_quality": 0.15},
    },
    "observation": {
        "label": "Observation",
        "summary": "A low-level empirical observation that can support claims or propositions.",
        "artifact_shape": "observation",
        "use_when": "Use for measured or observed facts that are not yet a broader thesis.",
        "evidence_rules": ["Prefer numeric proposition or dataset linkage when possible."],
        "autopilot_bias": {"semantic_fit": 0.40, "freshness": 0.35, "source_quality": 0.25},
    },
}

_ALIASES = {
    "deepdive": "deep_dive",
    "deep-dive": "deep_dive",
    "deep dive": "deep_dive",
    "company": "company_research",
    "company research": "company_research",
    "company_research_page": "company_research",
    "research": "thesis",
    "claim_card": "claim",
    "event_card": "event",
    "question": "probe",
    "dependency_claim": "dependency",
}


def normalize_research_signal_kind(raw: object) -> str:
    value = str(raw or "claim").strip().lower().replace("-", "_")
    value = " ".join(value.split())
    value = _ALIASES.get(value, value)
    value = value.replace(" ", "_")
    return value if value in RESEARCH_SIGNAL_KINDS else "claim"


def research_signal_kind_profiles() -> dict[str, dict[str, Any]]:
    return {key: dict(value) for key, value in RESEARCH_SIGNAL_KINDS.items()}


def thesis_kind_profile(kind: object) -> dict[str, Any]:
    key = normalize_research_signal_kind(kind)
    payload = dict(RESEARCH_SIGNAL_KINDS[key])
    payload["kind"] = key
    return payload


def thesis_status_is_hidden(status: object) -> bool:
    return str(status or "").strip().lower() in THESIS_HIDDEN_STATUSES


def serialize_thesis_signal(thesis: Thesis, *, include_profile: bool = True) -> dict[str, Any]:
    kind = normalize_research_signal_kind(getattr(thesis, "kind", None))
    payload: dict[str, Any] = {
        "id": int(thesis.id),
        "title": thesis.title,
        "statement": thesis.statement,
        "kind": kind,
        "status": thesis.status,
        "importance": int(thesis.importance or 0),
        "updated_at": thesis.updated_at.isoformat() if thesis.updated_at else None,
    }
    if include_profile:
        payload["kind_profile"] = thesis_kind_profile(kind)
    return payload
