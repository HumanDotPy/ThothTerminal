"""Authoritative execution capabilities for the MCP tool surface.

The registry is intentionally separate from the tool-definition builder.  Tool
descriptions are presentation/cache inputs; these capabilities are the safety
contract used by dispatch and batching.  An unclassified name gets the
conservative default and is never eligible for parallel or batch execution.
"""

from __future__ import annotations

from dataclasses import dataclass


CAPABILITY_PROFILE_VERSION = "mcp_capabilities_v1"
OPENAI_AGENT_PROFILE_VERSION = "analyst_v2"


@dataclass(frozen=True)
class ToolCapability:
    """Execution and result properties for one MCP tool.

    These fields are internal policy metadata.  Only the MCP-standard hint
    fields are copied into ``tools/list`` annotations.
    """

    known: bool = False
    read_only: bool = False
    destructive: bool = False
    idempotent: bool = False
    open_world: bool = False
    batch_safe: bool = False
    parallel_safe: bool = False
    bounded: bool = False
    citation_mode: str = "none"
    max_output_items: int | None = None


_READ_ONLY_NAMES = frozenset(
    {
        "get_research_desk",
        "get_artcraft_workbench",
        "artcraft_status",
        "artcraft_catalog",
        "get_research_change",
        "batch_read",
        "thoth_help",
        "session_info",
        "resolve_object",
        "runtime_info",
        "research_context_health",
        "codex_usage",
        "trade_latest_summary",
        "list_source_acquisition_rails",
        "get_source_acquisition_rail",
        "instrument_request_acquisition_payload",
        "integration_google_status",
        "integration_google_test",
        "object_search",
        "search",
        "fetch",
        "query_document",
        "expand_document_match",
        "read_document",
        "inspect_document",
        "render_document_page",
        "list_research_signal_kinds",
        "list_research_skills",
        "list_research_plays",
        "get_research_play",
        "read_report_content",
        "get_supply_chain_metric_bindings",
        "suggest_supply_chain_metric_bindings",
        "suggest_dataset_supply_chain_bindings",
        "suggest_metric_binding_request_fulfillments",
        "score_knowledge_asset_readiness",
        "list_knowledge_readiness",
        "tool_catalog",
        "get_tool_schema",
        "invoke_read_tool",
        # Local, read-only tools that were historically covered by the
        # name-prefix heuristic.  They are listed explicitly here so the
        # registry, rather than a naming convention, owns the decision.
        "list_sites",
        "list_programs",
        "list_gaps",
        "get_bottleneck",
        "list_agent_actions",
        "list_agent_change_runs",
        "get_agent_change_run",
        "get_agent_canon_card",
        "list_bottleneck_models",
        "get_bottleneck_model",
        "get_company",
        "get_dataset",
        "get_document",
        "list_document_arrival_cards",
        "get_document_image",
        "get_worldview_journal",
        "get_external_agent_proposal",
        "get_consensus_snapshot",
        "get_roadmap_snapshot",
        "get_roadmap_review_queue",
        "get_roadmap_entry_history",
        "get_operator_research_inbox",
        "get_operator_research_message",
        "get_freshness_review",
        "get_instrument_request",
        "get_knowledge_asset",
        "get_note",
        "get_program",
        "get_product_stage_claim",
        "get_product_stage_entry",
        "get_recent_context_bundle",
        "get_research_campaign",
        "get_research_canon_gap",
        "get_research_library_report",
        "get_research_subject",
        "get_source",
        "get_subject_report_quality",
        "get_subject_source_pack",
        "get_supply_chain",
        "get_worldview_worlds",
        "get_worldview_memory",
        "get_research_state_budget_policy", "list_state_compaction_candidates", "get_state_compaction_source",
        "list_research_extensions",
        "get_research_extension_executions",
        "get_worldview_reconciliation",
        "get_product_models",
        "get_agentx_context",
        "get_agentx_schema",
        "get_agentx_configuration",
        "evaluate_agentx_configuration",
        "get_product_configuration",
        "get_source_product_model_context",
        "preview_product_configuration_change",
        "get_supply_chain_node",
        "get_thesis",
        "get_trade_series",
        "get_site",
        "find_trade_series_by_hs",
        "list_earnings_calendar",
        "list_companies",
        "list_company_docs",
        "list_datasets",
        "list_doc_events",
        "list_freshness_reviews",
        "list_house_view_theses",
        "list_instrument_requests",
        "list_ipo_calendar",
        "list_knowledge_assets",
        "list_worldview_journals",
        "list_external_agent_proposals",
        "list_external_agent_intake",
        "list_next_research_campaign_items",
        "list_notes",
        "list_observations",
        "list_product_stage_claims",
        "list_product_stage_entries",
        "list_product_stage_history",
        "list_research_canon_gaps",
        "list_research_conference_index",
        "list_research_library",
        "list_research_report_versions",
        "list_research_reports",
        "list_research_subjects",
        "list_sources",
        "list_subject_scenarios",
        "list_supply_chain_alerts",
        "list_supply_chain_lints",
        "list_supply_chains",
        "list_theses",
        "list_thesis_proposals",
        "list_trade_series",
        "list_transcripts",
        "preview_dataset_view",
        "preview_gap_hunter_universe_index",
        "preview_research_campaign_from_universe",
        "preview_robotics_deployment_model",
        "preview_supply_chain_ops",
        "query_dataset",
        "query_product_stage",
        "search_object_neighborhood_before_work",
    }
)


# These are the only tools accepted by batch_read.  Keep this set deliberately
# smaller than the read-only set: a read can still be expensive, external, or
# return an unbounded artifact.
_BATCH_SAFE_NAMES = frozenset(
    {
        "fetch",
        "get_company",
        "get_dataset",
        "get_document",
        "get_knowledge_asset",
        "get_note",
        "get_research_report",
        "get_source",
        "get_supply_chain",
        "get_thesis",
        "list_companies",
        "list_document_arrival_cards",
        "list_datasets",
        "list_notes",
        "list_research_reports",
        "list_sources",
        "list_supply_chains",
        "list_theses",
        "object_search",
        "resolve_object",
        "search",
        "search_documents",
        "query_document",
        "expand_document_match",
        "read_document",
        "inspect_document",
    }
)


# Reads that leave the local database/Thoth-owned filesystem or can touch a
# provider API are still read-only, but are not local parallel work.
_OPEN_WORLD_NAMES = frozenset(
    {
        "discord_outbox_create",
        "find_trade_series_by_hs",
        "integration_google_status",
        "integration_google_test",
        "list_earnings_calendar",
        "list_ipo_calendar",
        "list_transcripts",
        "refresh_trade_source",
        "trade_latest_summary",
    }
)

_DESTRUCTIVE_NAMES = frozenset({"delete_dataset_draft_row", "artcraft_workflow"})
_SCOPED_MUTATION_NAMES = frozenset({
    "create_research_extension_candidate", "validate_research_extension_candidate",
    "activate_research_extension_candidate", "rollback_research_extension",
    "execute_research_extension",
    "apply_worldview_memory",
    "compact_research_state",
    "apply_worldview_write", "apply_worldview_scenario", "apply_worldview_journal", "apply_worldview_consensus_evidence",
    "apply_worldview_product_model", "apply_worldview_product_configuration",
    "apply_worldview_agentx_configuration",
    "apply_worldview_roadmap_update",
    "apply_worldview_roadmap_review",
    "resolve_worldview_research_request",
    "apply_interactive_research_write", "apply_interactive_consensus_evidence",
    "publish_external_model_projection",
    "resolve_external_agent_proposal",
    "resolve_external_agent_intake",
    "submit_operator_research_request",
    "submit_operator_feedback",
    "send_worldview_operator_message",
    "withdraw_external_model_projection",
})

# These reads may lazily create tables/passages or materialize legacy graph
# state.  They are excluded from batch/parallel lanes until a pure retrieval
# implementation is separated from the materializing path.
_PARALLEL_UNSAFE_NAMES = frozenset(
    {
        "get_dataset",
        "get_supply_chain",
        "inspect_document",
        "list_datasets",
        "list_supply_chains",
        "object_search",
        "query_document",
        "read_document",
        "resolve_object",
        "search",
    }
)

# PTC needs a predictable, compact reduction input rather than merely a read
# operation.  Keep this allowlist narrower than batch_read; the caller still
# needs an explicit PTC route/configuration gate before these names are
# advertised with ``allowed_callers``.
_PTC_ELIGIBLE_NAMES = frozenset(
    {
        "fetch",
        "get_company",
        "get_note",
        "get_source",
        "list_companies",
        "list_notes",
        "list_sources",
        "search_documents",
        "expand_document_match",
    }
)


# These surfaces are named "read" tools but can create indexes/tables,
# materialize legacy state, or persist derived artifacts.  Keep them out of
# every read-only/batch/parallel contract until a pure retrieval path exists.
_MATERIALIZING_READ_NAMES = frozenset(
    {
        "list_datasets",
        "get_dataset",
        "list_supply_chains",
        "get_supply_chain",
        "get_supply_chain_node",
        "get_supply_chain_metric_bindings",
        "suggest_supply_chain_metric_bindings",
        "suggest_dataset_supply_chain_bindings",
        "suggest_metric_binding_request_fulfillments",
        "list_supply_chain_alerts",
        "list_supply_chain_lints",
        "compare_supply_chain_snapshots",
        "preview_supply_chain_ops",
        "query_document",
        "read_document",
        "inspect_document",
        "render_document_page",
    }
)

# Apply the deny-list after the readable name sets are declared so a future
# contributor cannot accidentally re-enable one by adding it to a broad list.
_READ_ONLY_NAMES = _READ_ONLY_NAMES - _MATERIALIZING_READ_NAMES
_BATCH_SAFE_NAMES = _BATCH_SAFE_NAMES - _MATERIALIZING_READ_NAMES
_PTC_ELIGIBLE_NAMES = _PTC_ELIGIBLE_NAMES - _MATERIALIZING_READ_NAMES
_PARALLEL_UNSAFE_NAMES = _PARALLEL_UNSAFE_NAMES | _MATERIALIZING_READ_NAMES


def _build_capabilities() -> dict[str, ToolCapability]:
    capabilities: dict[str, ToolCapability] = {}
    citation_names = {"search", "fetch", "search_documents", "query_document", "expand_document_match", "read_document"}
    for name in _READ_ONLY_NAMES | _BATCH_SAFE_NAMES:
        capabilities[name] = ToolCapability(
            known=True,
            read_only=True,
            idempotent=True,
            open_world=name in _OPEN_WORLD_NAMES,
            citation_mode="citations" if name in citation_names else "metadata",
            bounded=name in _BATCH_SAFE_NAMES,
            max_output_items=30 if name in _BATCH_SAFE_NAMES else None,
        )
    for name in _BATCH_SAFE_NAMES:
        current = capabilities.get(name)
        if current is None:
            # This should never happen; retaining a safe default here makes a
            # missing read-only entry fail closed rather than becoming a write.
            continue
        capabilities[name] = ToolCapability(
            **{
                **current.__dict__,
                "batch_safe": True,
                "parallel_safe": name not in _PARALLEL_UNSAFE_NAMES,
                "bounded": True,
            }
        )
    for name in _OPEN_WORLD_NAMES:
        current = capabilities.get(name)
        if current is None:
            capabilities[name] = ToolCapability(known=True, open_world=True)
        else:
            capabilities[name] = ToolCapability(**{**current.__dict__, "open_world": True, "parallel_safe": False})
    for name in _DESTRUCTIVE_NAMES:
        capabilities[name] = ToolCapability(known=True, destructive=True)
    for name in _SCOPED_MUTATION_NAMES:
        capabilities[name] = ToolCapability(known=True)
    return capabilities


_CAPABILITIES = _build_capabilities()
DEFAULT_TOOL_CAPABILITY = ToolCapability()

READ_ONLY_TOOL_NAMES = _READ_ONLY_NAMES | _BATCH_SAFE_NAMES
BATCH_SAFE_TOOL_NAMES = _BATCH_SAFE_NAMES
OPEN_WORLD_TOOL_NAMES = _OPEN_WORLD_NAMES
DESTRUCTIVE_TOOL_NAMES = _DESTRUCTIVE_NAMES


def capability_for_tool(tool_name: object) -> ToolCapability:
    """Return an explicit capability or the conservative unknown default."""

    return _CAPABILITIES.get(str(tool_name or "").strip(), DEFAULT_TOOL_CAPABILITY)


def is_read_only_tool(tool_name: object) -> bool:
    return capability_for_tool(tool_name).read_only


def is_batch_safe_tool(tool_name: object) -> bool:
    capability = capability_for_tool(tool_name)
    return capability.known and capability.batch_safe and capability.read_only


def is_parallel_safe_tool(tool_name: object) -> bool:
    capability = capability_for_tool(tool_name)
    return capability.known and capability.parallel_safe and capability.batch_safe and capability.read_only and capability.bounded and not capability.open_world


def is_ptc_eligible_tool(tool_name: object) -> bool:
    """Return whether a tool is eligible for an explicitly enabled PTC route."""

    name = str(tool_name or "").strip()
    return name in _PTC_ELIGIBLE_NAMES and is_parallel_safe_tool(name)


def capability_metadata(tool_name: object) -> dict[str, object]:
    """Return serializable policy metadata for manifests and adapters."""

    capability = capability_for_tool(tool_name)
    return {
        "known": capability.known,
        "read_only": capability.read_only,
        "destructive": capability.destructive,
        "idempotent": capability.idempotent,
        "open_world": capability.open_world,
        "batch_safe": capability.batch_safe,
        "parallel_safe": capability.parallel_safe,
        "bounded": capability.bounded,
        "citation_mode": capability.citation_mode,
        "max_output_items": capability.max_output_items,
        "ptc_eligible": is_ptc_eligible_tool(tool_name),
        "profile_version": CAPABILITY_PROFILE_VERSION,
    }


def all_capabilities() -> dict[str, ToolCapability]:
    """Return a copy for diagnostics/tests without exposing it over MCP."""

    return dict(_CAPABILITIES)
