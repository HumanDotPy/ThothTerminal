from __future__ import annotations

import base64
import hashlib

from thoth_terminal.memory_dataset_resolver import evaluate_memory_system_from_datasets
from thoth_terminal.dataset_discovery import discover_datasets, dataset_search_values
from thoth_terminal.document_match_service import expand_document_match
from thoth_terminal.document_page_service import _safe_attachment_path, render_document_page
from thoth_terminal.document_passage_service import (
    inspect_document_structure,
    query_document_passages,
    read_document_passages,
)
from thoth_terminal.query import document_match_projection
from thoth_terminal.models import Attachment
from thoth_terminal.robotics_deployment_model import robotics_deployment_model
from thoth_terminal.web.mcp_parts._shared import bind_mcp_context


bind_mcp_context(globals())


def _mcp_document_text_excerpt(session: Session, document_id: int, *, limit: int = 1200) -> dict[str, object]:
    """Return bounded text metadata without hydrating a Document text column."""
    limit_v = max(1, min(int(limit), 8000))
    text_expr = func.coalesce(Document.translation_en, Document.normalized_text, "")
    row = (
        session.query(
            func.length(text_expr).label("text_length_chars"),
            func.substr(text_expr, 1, limit_v).label("text_excerpt"),
        )
        .filter(Document.id == int(document_id))
        .first()
    )
    text_length = int((row[0] if row else 0) or 0)
    excerpt = str((row[1] if row else "") or "")
    return {
        "text": excerpt,
        "text_length_chars": text_length,
        "returned_text_length_chars": len(excerpt),
        "truncated": len(excerpt) < text_length,
    }


def _mcp_tool_runtime_info(params: dict[str, object]) -> dict[str, object]:
    include_health = bool(params.get("include_health", True))
    out: dict[str, object] = {"runtime": get_runtime_identity()}
    if include_health:
        out["health"] = _ops_health_payload()
    return out


def _mcp_tool_preview_robotics_deployment_model(params: dict[str, object]) -> dict[str, object]:
    return robotics_deployment_model(dict(params))


def _mcp_tool_evaluate_memory_system(params: dict[str, object]) -> dict[str, object]:
    with session_scope() as session:
        return evaluate_memory_system_from_datasets(session, dict(params))


def _mcp_tool_thoth_help(
    params: dict[str, object],
    *,
    chatgpt_app: bool = False,
    chatgpt_access_mode: str = "write",
    tool_profile: str = "full",
) -> dict[str, object]:
    topic = _mcp_normalize_token(params.get("topic"))
    surface = "chatgpt_app" if chatgpt_app else "operator_mcp"
    access_mode = chatgpt_access_mode if chatgpt_app else "operator"
    focus: dict[str, object] | None = None
    if topic in {"supply_chain", "supply_chains", "chain", "chains"}:
        focus = {
            "topic": "supply_chain",
            "steps": [
                {"tool": "resolve_object", "arguments": {"query": "Optical Interconnect and CPO Ecosystem", "kind": "supply_chain"}},
                {"tool": "get_supply_chain", "arguments": {"id": 18}},
                {"tool": "preview_supply_chain_ops", "arguments": {"id": 18, "ops": [{"op": "upsert_node", "name": "Hybrid bonding", "layer": "capability"}]}},
                {"tool": "apply_supply_chain_ops", "arguments": {"id": 18, "ops": [{"op": "upsert_node", "name": "Hybrid bonding", "layer": "capability"}]}},
                {"tool": "publish_supply_chain_snapshot", "arguments": {"id": 18, "label": "ofc-2026-update"}},
            ],
            "notes": [
                "Use `preview_supply_chain_ops` before `apply_supply_chain_ops` when you want a dry run.",
                "Supply chains are versioned. Draft changes live on the active draft snapshot until published.",
            ],
        }
    elif topic in {"research_library", "compiled_memory", "deep_research", "reports_dropbox", "report_library"}:
        focus = {
            "topic": "research_library",
            "steps": [
                {"tool": "ingest_research_library", "arguments": {}},
                {"tool": "list_research_conference_index", "arguments": {"query": "OFC HBM Hot Chips", "limit": 10}},
                {"tool": "list_research_library", "arguments": {"query": "memory supercycle", "limit": 10}},
                {"tool": "get_research_library_report", "arguments": {"document_id": 1}},
                {"tool": "review_research_library", "arguments": {"review_window_days": 14}},
                {"tool": "add_research_report_addendum", "arguments": {"document_id": 1, "title": "Q2 update", "body_md": "Audit stamp: ...\n\nNew evidence: ..."}},
                {"tool": "object_search", "arguments": {"query": "memory hierarchy statefulness", "kind": "all"}},
            ],
            "notes": [
                "The research library is compiled memory for long deep dives, Deep Research PDFs, and high-quality paid/analyst pieces you manually drop into the local folder.",
                "Full report refresh defaults to quarterly. Addenda extend the useful lifetime when new evidence updates part of the report without requiring a full rewrite.",
            ],
        }
    elif topic in {"research", "ingest", "research_ingest", "update"}:
        focus = {
            "topic": "research_ingest",
            "steps": [
                {"tool": "object_search", "arguments": {"query": "OFC 2026 CPO packaging", "kind": "all"}},
                {"tool": "list_research_library", "arguments": {"query": "OFC 2026 CPO packaging", "limit": 5}},
                {"tool": "list_research_signal_kinds", "arguments": {}},
                {"tool": "create_note", "arguments": {"title": "OFC 2026 synthesis", "body_md": "..."}},
                {"tool": "create_thesis", "arguments": {"title": "Packaging is the bottleneck", "kind": "bottleneck", "statement": "...", "entities": ["Broadcom", "Corning"]}},
                {"tool": "resolve_object", "arguments": {"query": "Optical Interconnect and CPO Ecosystem", "kind": "supply_chain"}},
                {"tool": "preview_supply_chain_ops", "arguments": {"id": 18, "ops": [{"op": "upsert_node", "name": "Glass substrate", "layer": "input"}]}},
            ],
        }
    elif topic in {"search", "object_search", "retrieval"}:
        focus = {
            "topic": "object_search",
            "steps": [
                {"tool": "object_search", "arguments": {"query": "Nanya DDR4 DDR5 CXL", "kind": "all", "limit": 12}},
                {"tool": "resolve_object", "arguments": {"query": "Nanya", "kind": "company"}},
                {"tool": "get_company", "arguments": {"id": 1}},
            ],
            "notes": [
                "`search` keeps a document-only `results` field for ChatGPT compatibility, but now also returns object_results, groups, and neighborhoods.",
                "Use `object_search` when the agent should find documents, theses, notes, datasets, reports, knowledge assets, research plays, and supply chains together.",
            ],
        }
    elif topic in {"metrics", "metric", "binding", "bindings"}:
        focus = {
            "topic": "metric_bindings",
            "steps": [
                {"tool": "suggest_supply_chain_metric_bindings", "arguments": {"id": 16, "query": "CPO optics", "limit": 10}},
                {"tool": "preview_supply_chain_ops", "arguments": {"id": 16, "ops": [{"op": "bind_metric", "scope_type": "node", "scope_key": "example", "metric_kind": "value_usd", "dataset_id": 1, "column_key": "value", "aggregation": "latest"}]}},
                {"tool": "apply_supply_chain_ops", "arguments": {"id": 16, "ops": [{"op": "bind_metric", "scope_type": "node", "scope_key": "example", "metric_kind": "value_usd", "dataset_id": 1, "column_key": "value", "aggregation": "latest"}]}},
                {"tool": "get_supply_chain_metric_bindings", "arguments": {"id": 16}},
            ],
        }
    elif topic in {"bottleneck", "bottlenecks", "constraint", "constraints"}:
        focus = {
            "topic": "bottleneck_modeling",
            "steps": [
                {"tool": "object_search", "arguments": {"query": "HBM CoWoS optics marginal system leverage", "kind": "all"}},
                {"tool": "list_bottleneck_models", "arguments": {"query": "HBM", "limit": 10}},
                {"tool": "start_bottleneck_report", "arguments": {"model_key": "hbm4-supply-gate", "title": "HBM4 Supply Gate", "scope_type": "supply_chain_node", "constrained_input": "HBM4 qualified supply", "output_unit": "accelerator systems", "execute": True, "force": True}},
                {"tool": "read_report_content", "arguments": {"report_id": 1}},
                {"tool": "run_research_campaign_items", "arguments": {"campaign_key": "campaign:bottleneck:hbm4-supply-gate:startup", "limit": 1, "dry_run": False, "force": True}},
                {"tool": "get_bottleneck_model", "arguments": {"id": 1}},
            ],
            "notes": [
                "Bottleneck models are durable objects; bottleneck assessments are dated scorecards over bindingness, shadow price, ramp resistance, substitutability, persistence, latent-demand absorption, supplier qualification, and marginal system leverage.",
                "`start_bottleneck_report` is the preferred entry point for user-defined bottleneck work. With `execute=true`, it returns a workspace startup packet with report/version ids, candidate sell-side evidence, role prompts, and next MCP calls.",
                "Mention frequency should create candidates, not conclusions; record evidence and confidence before treating a bottleneck as active.",
            ],
        }
    elif topic in {"knowledge", "readiness", "primer", "teardown"}:
        focus = {
            "topic": "knowledge_readiness",
            "steps": [
                {"tool": "list_knowledge_assets", "arguments": {"query": "advanced packaging", "published_only": False}},
                {"tool": "score_knowledge_asset_readiness", "arguments": {"id": 1}},
                {"tool": "list_knowledge_readiness", "arguments": {"query": "advanced packaging", "limit": 20}},
            ],
            "notes": ["Readiness scoring is advisory for automations; it does not prevent intentional human publication."],
        }
    elif topic in {"institutional", "institution", "program", "programs", "site", "sites", "gap", "gaps"}:
        focus = {
            "topic": "institutional",
            "steps": [
                {"tool": "list_sites", "arguments": {"query": "Loudoun", "limit": 10}},
                {"tool": "get_site", "arguments": {"site_id": 1}},
                {"tool": "list_programs", "arguments": {"site_id": 1, "limit": 20}},
                {"tool": "get_program", "arguments": {"program_id": 1}},
                {"tool": "list_gaps", "arguments": {"program_id": 1, "open_only": True, "limit": 20}},
                {"tool": "get_bottleneck", "arguments": {"program_id": 1}},
                {"tool": "enqueue_closure_pass", "arguments": {"program_id": 1, "force": True}},
            ],
            "notes": [
                "Institutional tools live on the main MCP operator surface and use the same flat tool naming as the rest of Thoth.",
                "Use `list_gaps` and `get_bottleneck` after refresh or closure jobs to inspect what still blocks the institutional spine.",
            ],
        }
    available = sorted(
        _mcp_profile_direct_names(
            tool_profile=tool_profile,
            chatgpt_app=chatgpt_app,
            access_mode=chatgpt_access_mode,
        )
    )
    return {
        "surface": surface,
        "access_mode": access_mode,
        "mental_model": {
            "tool_style": "Use flat tool names like `get_supply_chain` or `create_note`. Do not invent slash or namespace paths.",
            "resources_vs_tools": "Resources are read-only reference URIs. Reads and writes should normally go through tools.",
            "object_first_workflow": "Search across object neighborhoods, resolve the target, inspect it, preview changes when available, then mutate.",
            "research_signals": "Theses intentionally store overlapping claims, theses, deep dives, company research, watchpoints, scenarios, events, and bottlenecks via the `kind` field.",
            "compiled_memory": "Long-form local reports belong in the research library first; agents should search them before rewriting known deep-dive context from scratch.",
        },
        "main_nouns": [
            {"kind": "source", "purpose": "External feeds and connectors.", "core_tools": ["list_sources", "get_source", "run_source", "update_source_config"]},
            {"kind": "note", "purpose": "Working notes and synthesis.", "core_tools": ["list_notes", "get_note", "create_note", "update_note"]},
            {"kind": "thesis", "purpose": "Tracked research signals: claims, theses, deep dives, company research, watchpoints, scenarios, events, dependencies, and bottlenecks.", "core_tools": ["list_research_signal_kinds", "list_theses", "get_thesis", "create_thesis", "add_thesis_evidence", "update_thesis"]},
            {"kind": "research_library_report", "purpose": "Local compiled-memory reports, conference source cards, papers, decks, and addenda from Deep Research/PDF/Markdown drops.", "core_tools": ["ingest_research_library", "list_research_conference_index", "list_research_library", "get_research_library_report", "review_research_library", "add_research_report_addendum"]},
            {"kind": "dataset", "purpose": "Versioned structured tables.", "core_tools": ["list_datasets", "get_dataset", "query_dataset", "upsert_dataset_draft_row"]},
            {"kind": "supply_chain", "purpose": "Versioned graph models, bottleneck maps, and metric-bound operating dashboards.", "core_tools": ["list_supply_chains", "get_supply_chain", "suggest_supply_chain_metric_bindings", "preview_supply_chain_ops", "apply_supply_chain_ops", "publish_supply_chain_snapshot"]},
            {"kind": "bottleneck_model", "purpose": "Durable bottleneck artifacts with marginal system leverage, supplier qualification, and evidence-backed scorecards.", "core_tools": ["start_bottleneck_report", "list_bottleneck_models", "get_bottleneck_model", "assess_supply_chain_bottlenecks", "upsert_bottleneck_model"]},
            {"kind": "research_report", "purpose": "Canonical and imported long-form reports.", "core_tools": ["list_research_reports", "get_research_report", "register_imported_report", "append_report_addendum", "read_report_content", "build_research_report"]},
            {"kind": "knowledge_asset", "purpose": "Primers and teardowns with advisory readiness scoring.", "core_tools": ["list_knowledge_assets", "get_knowledge_asset", "score_knowledge_asset_readiness", "list_knowledge_readiness"]},
            {"kind": "research_play", "purpose": "Reusable research/deep-dive workflows that agents can run and improve.", "core_tools": ["list_research_plays", "get_research_play", "run_research_play"]},
            {"kind": "site", "purpose": "Institutional facilities, campuses, and physical footprints.", "core_tools": ["list_sites", "get_site", "list_programs", "list_gaps"]},
            {"kind": "program", "purpose": "Institutional buildouts with node, edge, and bottleneck views.", "core_tools": ["list_programs", "get_program", "get_bottleneck", "enqueue_closure_pass"]},
        ],
        "common_workflows": [
            {
                "name": "search_object_neighborhood_before_work",
                "steps": ["object_search", "resolve_object", "get_*"],
            },
            {
                "name": "update_supply_chain_from_new_research",
                "steps": ["object_search", "resolve_object", "get_supply_chain", "suggest_supply_chain_metric_bindings", "preview_supply_chain_ops", "apply_supply_chain_ops", "publish_supply_chain_snapshot"],
            },
            {
                "name": "model_active_and_shadow_bottlenecks",
                "steps": ["object_search", "start_bottleneck_report", "run_research_campaign_items", "read_report_content", "get_bottleneck_model"],
            },
            {
                "name": "create_note_and_research_signal_from_documents",
                "steps": ["search", "fetch", "list_research_signal_kinds", "create_note", "create_thesis", "add_thesis_evidence"],
            },
            {
                "name": "use_compiled_report_memory_before_deep_work",
                "steps": ["ingest_research_library", "list_research_library", "get_research_library_report", "review_research_library", "object_search", "add_research_report_addendum"],
            },
            {
                "name": "evaluate_asset_before_agent_publication",
                "steps": ["list_knowledge_assets", "score_knowledge_asset_readiness", "save_knowledge_asset_draft"],
            },
            {
                "name": "look_up_existing_object_before_editing",
                "steps": ["resolve_object", "get_note/get_thesis/get_supply_chain/get_dataset"],
            },
            {
                "name": "refresh_institutional_spine_and_review_gaps",
                "steps": ["list_sites", "list_programs", "enqueue_institutional_refresh", "enqueue_closure_pass", "list_gaps", "get_bottleneck"],
            },
        ],
        "canonical_examples": [
            {"tool": "resolve_object", "arguments": {"query": "Silicon Photonics and CPO Engine Chain", "kind": "supply_chain"}},
            {"tool": "object_search", "arguments": {"query": "advanced packaging TSMC Intel Amkor ASE Samsung SK Hynix", "kind": "all"}},
            {"tool": "list_research_library", "arguments": {"query": "memory supercycle", "limit": 5}},
            {"tool": "get_supply_chain", "arguments": {"id": 16}},
            {"tool": "assess_supply_chain_bottlenecks", "arguments": {"id": 16, "output_unit": "accelerator systems"}},
            {"tool": "list_bottleneck_models", "arguments": {"query": "HBM", "limit": 5}},
            {"tool": "suggest_supply_chain_metric_bindings", "arguments": {"id": 16, "query": "photonic packaging", "limit": 8}},
            {"tool": "preview_supply_chain_ops", "arguments": {"id": 16, "ops": [{"op": "upsert_node", "name": "External laser source", "layer": "component"}]}},
            {"tool": "create_note", "arguments": {"title": "OFC 2026 optics synthesis", "body_md": "Packaging and reliability emerged as the gating bottlenecks."}},
            {"tool": "create_thesis", "arguments": {"title": "Packaging beats hero-device metrics in CPO", "kind": "bottleneck", "statement": "Packaging, thermal partitioning, and reliability proof now gate CPO commercialization.", "entities": ["Broadcom", "Corning", "TSMC"]}},
            {"tool": "create_thesis", "arguments": {"title": "Nanya cycle and CXL research", "kind": "company_research", "statement": "Nanya is better framed around specialty DRAM, DDR4/DDR5 cycle leverage, CXL, CPU clusters, mobile, and robotics than HBM competition.", "entities": ["Nanya"]}},
            {"tool": "list_gaps", "arguments": {"subject_id": 1, "open_only": True, "limit": 20}},
            {"tool": "get_bottleneck", "arguments": {"program_id": 1}},
        ],
        "error_recovery": [
            "If a tool name fails, call `thoth_help` or `session_info` and use the exact tool names from `tools/list`.",
            "If you only know the object title, call `resolve_object` first and then use the returned id.",
            "If you are editing a supply chain, call `preview_supply_chain_ops` before `apply_supply_chain_ops`.",
        ],
        "available_tool_count": len(available),
        "tool_profile": _mcp_normalize_tool_profile(tool_profile),
        "focus": focus,
    }


def _mcp_tool_session_info(
    params: dict[str, object],
    *,
    chatgpt_app: bool = False,
    chatgpt_access_mode: str = "write",
    tool_profile: str = "full",
) -> dict[str, object]:
    del params
    surface = "chatgpt_app" if chatgpt_app else "operator_mcp"
    access_mode = chatgpt_access_mode if chatgpt_app else "operator"
    available = sorted(
        _mcp_profile_direct_names(
            tool_profile=tool_profile,
            chatgpt_app=chatgpt_app,
            access_mode=chatgpt_access_mode,
        )
    )
    read_only = [name for name in available if _mcp_is_read_only_tool(name)]
    write_capable = [name for name in available if name not in read_only]
    states = []
    for row in list_runtime_states()[:8]:
        states.append(
            {
                "service": str(row.get("service") or ""),
                "pid": row.get("pid"),
                "started_at_utc": row.get("started_at_utc"),
                "boot_id": row.get("boot_id"),
            }
        )
    return {
        "surface": surface,
        "access_mode": access_mode,
        "tool_profile": _mcp_normalize_tool_profile(tool_profile),
        "tool_profile_cache_stable": True,
        "canonical_tool_style": "flat_names_only",
        "mutations_available": access_mode != "read_only",
        "resource_mode": "widget_only" if chatgpt_app else "full",
        "public_base_url": _mcp_public_base_url(),
        "widget_resource_uri": _MCP_WIDGET_RESOURCE_URI,
        "tool_discovery": {
            "gateway_tools": [name for name in ("tool_catalog", "get_tool_schema", "invoke_read_tool")
                              if name in available],
            "cold_reads": "Use tool_catalog, get_tool_schema, then invoke_read_tool for reads outside the active profile.",
            "metadata_refresh": "If advertised tools are missing from your connector, refresh its metadata and start a new conversation.",
        },
        "tool_counts": {
            "total": len(available),
            "read_only": len(read_only),
            "write_capable": len(write_capable),
        },
        "runtime": {
            "app_version": app.version,
            "boot_id": get_runtime_identity().get("boot_id"),
            "started_at_utc": get_runtime_identity().get("started_at_utc"),
        },
        "service_registry": states,
        "chatgpt_app_configured": bool(_mcp_chatgpt_app_token()),
        "chatgpt_read_only_configured": bool(_mcp_chatgpt_app_read_only_token()),
        "recovery": [
            "Use exact tool names from `tools/list`.",
            "Call `resolve_object` before edits when you only know a title or slug.",
            "Use the write app endpoint if you need mutations; read-only surfaces expose only non-mutating tools.",
        ],
    }


def _mcp_match_score(query: str, *values: object) -> int:
    target = _mcp_normalize_token(query)
    if not target:
        return 999
    best = 999
    for value in values:
        candidate = _mcp_normalize_token(value)
        if not candidate:
            continue
        if candidate == target:
            return 0
        if candidate.startswith(target):
            best = min(best, 1)
            continue
        if target in candidate:
            best = min(best, 2)
            continue
        if difflib.SequenceMatcher(a=target, b=candidate).ratio() >= 0.72:
            best = min(best, 3)
    return best


def _mcp_resolve_kind(raw: object) -> str | None:
    clean = _mcp_singular_noun(str(raw or ""))
    if not clean:
        return None
    allowed = {
        "company",
        "chart",
        "bottleneck_model",
        "dataset",
        "knowledge_asset",
        "note",
        "observation",
        "program",
        "proposition",
        "research_play",
        "research_report",
        "research_subject",
        "site",
        "source",
        "supply_chain",
        "thesis",
    }
    return clean if clean in allowed else None


def _mcp_next_operations(kind: str, *, object_id: int, slug: str | None = None) -> list[dict[str, object]]:
    if kind == "source":
        return [
            {"tool": "get_source", "arguments": {"id": object_id}},
            {"tool": "run_source", "arguments": {"id": object_id}},
            {"tool": "update_source_config", "arguments": {"id": object_id, "enabled": True}},
        ]
    if kind == "note":
        return [
            {"tool": "get_note", "arguments": {"id": object_id}},
            {"tool": "update_note", "arguments": {"id": object_id, "body_md": "..."}},
        ]
    if kind == "thesis":
        return [
            {"tool": "get_thesis", "arguments": {"id": object_id}},
            {"tool": "update_thesis", "arguments": {"id": object_id, "statement": "..."}},
            {"tool": "add_thesis_evidence", "arguments": {"thesis_id": object_id, "document_id": 1, "note": "..."}},
        ]
    if kind == "company":
        return [
            {"tool": "get_company", "arguments": {"id": object_id}},
            {"tool": "list_company_docs", "arguments": {"company_id": object_id}},
        ]
    if kind == "dataset":
        return [
            {"tool": "get_dataset", "arguments": {"id": object_id}},
            {"tool": "query_dataset", "arguments": {"id": object_id}},
            {"tool": "upsert_dataset_draft_row", "arguments": {"dataset_id": object_id, "row_key": "example", "values": {}}},
        ]
    if kind == "chart":
        return [
            {"tool": "object_search", "arguments": {"query": slug or str(object_id), "kind": "chart"}},
        ]
    if kind == "site":
        return [
            {"tool": "get_site", "arguments": {"site_id": object_id}},
            {"tool": "list_programs", "arguments": {"site_id": object_id, "limit": 20}},
            {"tool": "list_gaps", "arguments": {"site_id": object_id, "open_only": True, "limit": 20}},
        ]
    if kind == "program":
        return [
            {"tool": "get_program", "arguments": {"program_id": object_id}},
            {"tool": "get_bottleneck", "arguments": {"program_id": object_id}},
            {"tool": "list_gaps", "arguments": {"program_id": object_id, "open_only": True, "limit": 20}},
            {"tool": "enqueue_closure_pass", "arguments": {"program_id": object_id, "force": True}},
        ]
    if kind == "research_report":
        return [
            {"tool": "get_research_report", "arguments": {"report_id": object_id}},
            {"tool": "build_research_report", "arguments": {"subject": slug or str(object_id)}},
        ]
    if kind == "research_subject":
        return [
            {"tool": "get_research_subject", "arguments": {"id": object_id}},
            {"tool": "list_knowledge_assets", "arguments": {"subject": slug or str(object_id), "published_only": False}},
            {"tool": "list_research_reports", "arguments": {"subject": slug or str(object_id)}},
        ]
    if kind == "knowledge_asset":
        return [
            {"tool": "get_knowledge_asset", "arguments": {"id": object_id}},
            {"tool": "score_knowledge_asset_readiness", "arguments": {"id": object_id}},
            {"tool": "save_knowledge_asset_draft", "arguments": {"id": object_id, "content_md": "..."}},
        ]
    if kind == "supply_chain":
        return [
            {"tool": "get_supply_chain", "arguments": {"id": object_id}},
            {"tool": "suggest_supply_chain_metric_bindings", "arguments": {"id": object_id}},
            {"tool": "assess_supply_chain_bottlenecks", "arguments": {"id": object_id}},
            {"tool": "preview_supply_chain_ops", "arguments": {"id": object_id, "ops": [{"op": "upsert_node", "name": "Example node", "layer": "component"}]}},
            {"tool": "apply_supply_chain_ops", "arguments": {"id": object_id, "ops": [{"op": "upsert_node", "name": "Example node", "layer": "component"}]}},
            {"tool": "publish_supply_chain_snapshot", "arguments": {"id": object_id, "label": "update"}},
        ]
    if kind == "bottleneck_model":
        return [
            {"tool": "get_bottleneck_model", "arguments": {"id": object_id}},
            {"tool": "list_bottleneck_models", "arguments": {"query": slug or str(object_id), "limit": 10}},
        ]
    if kind == "research_play":
        return [
            {"tool": "get_research_play", "arguments": {"id": object_id}},
            {"tool": "run_research_play", "arguments": {"id": object_id}},
        ]
    if kind == "proposition":
        return [{"tool": "object_search", "arguments": {"query": slug or str(object_id), "kind": "proposition"}}]
    if kind == "observation":
        return [{"tool": "object_search", "arguments": {"query": slug or str(object_id), "kind": "observation"}}]
    return []


def _mcp_tool_resolve_object(params: dict[str, object]) -> dict[str, object]:
    query = str(params.get("query") or "").strip()
    if not query:
        raise ValueError("query is required.")
    kind = _mcp_resolve_kind(params.get("kind"))
    if params.get("kind") is not None and kind is None:
        raise ValueError("Unsupported kind. Use source, note, thesis, company, chart, dataset, site, program, supply_chain, research_report, research_subject, research_play, knowledge_asset, proposition, or observation.")
    limit = int(params.get("limit")) if str(params.get("limit") or "").isdigit() else 8
    limit = max(1, min(20, limit))
    include_hidden_theses = _request_includes_hidden_theses(params.get("include_hidden"))
    pattern = f"%{query}%"
    matches: list[dict[str, object]] = []
    session = _session_read()
    try:
        def add_match(
            *,
            match_kind: str,
            object_id: int,
            label: object,
            slug: object | None = None,
            status: object | None = None,
            description: object | None = None,
            match_values: list[object] | tuple[object, ...],
        ) -> None:
            score = _mcp_match_score(query, *match_values)
            if score >= 999:
                return
            item = {
                "kind": match_kind,
                "id": object_id,
                "label": str(label or "").strip() or f"{match_kind} {object_id}",
                "slug": str(slug or "").strip() or None,
                "status": str(status or "").strip() or None,
                "description": str(description or "").strip() or None,
                "next_operations": _mcp_next_operations(match_kind, object_id=object_id, slug=str(slug or "").strip() or None),
                "_score": score,
            }
            matches.append(item)

        if kind in (None, "source"):
            for row in session.query(Source).filter(Source.name.ilike(pattern)).order_by(Source.name.asc()).limit(limit).all():
                add_match(match_kind="source", object_id=int(row.id), label=row.name, status="active", match_values=[row.name])
        if kind in (None, "note"):
            for row in session.query(Note).filter(Note.title.ilike(pattern)).order_by(Note.updated_at.desc(), Note.id.desc()).limit(limit).all():
                add_match(match_kind="note", object_id=int(row.id), label=row.title, status=row.status, description=row.note_type, match_values=[row.title])
        if kind in (None, "thesis"):
            thesis_query = session.query(Thesis).filter(or_(Thesis.title.ilike(pattern), Thesis.statement.ilike(pattern)))
            thesis_query = _visible_thesis_query(thesis_query, include_hidden=include_hidden_theses)
            for row in thesis_query.order_by(Thesis.updated_at.desc(), Thesis.id.desc()).limit(limit).all():
                add_match(match_kind="thesis", object_id=int(row.id), label=row.title, status=row.status, description=row.statement, match_values=[row.title, row.statement])
        if kind in (None, "company"):
            for row in session.query(Company).filter(or_(Company.name.ilike(pattern), Company.slug.ilike(pattern), Company.ticker.ilike(pattern))).order_by(Company.name.asc()).limit(limit).all():
                add_match(match_kind="company", object_id=int(row.id), label=row.name, slug=row.slug, description=row.ticker, match_values=[row.name, row.slug, row.ticker])
        if kind in (None, "chart"):
            for row in session.query(Chart).filter(or_(Chart.title.ilike(pattern), Chart.slug.ilike(pattern), Chart.category.ilike(pattern))).order_by(Chart.updated_at.desc(), Chart.id.desc()).limit(limit).all():
                add_match(match_kind="chart", object_id=int(row.id), label=row.title, slug=row.slug, description=row.category, match_values=[row.title, row.slug, row.category])
        if kind in (None, "dataset"):
            for row in discover_datasets(session, query=query, limit=limit).rows:
                add_match(match_kind="dataset", object_id=int(row.id), label=row.title, slug=row.slug, status=row.status, description=row.category, match_values=dataset_search_values(row))
        if kind in (None, "site"):
            for row in session.query(Site).filter(or_(Site.name.ilike(pattern), Site.slug.ilike(pattern), Site.operator_name.ilike(pattern), Site.owner_name.ilike(pattern))).order_by(Site.updated_at.desc(), Site.id.desc()).limit(limit).all():
                add_match(match_kind="site", object_id=int(row.id), label=row.name, slug=row.slug, status=row.status, description=row.site_kind, match_values=[row.name, row.slug, row.operator_name, row.owner_name, row.utility_name])
        if kind in (None, "program"):
            for row in session.query(Program).filter(or_(Program.title.ilike(pattern), Program.slug.ilike(pattern), Program.summary.ilike(pattern))).order_by(Program.updated_at.desc(), Program.id.desc()).limit(limit).all():
                add_match(match_kind="program", object_id=int(row.id), label=row.title, slug=row.slug, status=row.status, description=row.category, match_values=[row.title, row.slug, row.summary, row.category])
        if kind in (None, "supply_chain"):
            _ensure_supply_chain_tables(session)
            for row in session.query(SupplyChain).filter(or_(SupplyChain.title.ilike(pattern), SupplyChain.slug.ilike(pattern), SupplyChain.product.ilike(pattern))).order_by(SupplyChain.updated_at.desc(), SupplyChain.id.desc()).limit(limit).all():
                add_match(match_kind="supply_chain", object_id=int(row.id), label=row.title, slug=row.slug, status=row.status, description=row.product, match_values=[row.title, row.slug, row.product])
        if kind in (None, "research_report"):
            for row in session.query(ResearchReport).filter(or_(ResearchReport.title.ilike(pattern), ResearchReport.slug.ilike(pattern), ResearchReport.subject_key.ilike(pattern), ResearchReport.latest_subject_slug.ilike(pattern))).order_by(ResearchReport.updated_at.desc(), ResearchReport.id.desc()).limit(limit).all():
                add_match(match_kind="research_report", object_id=int(row.id), label=row.title, slug=row.slug, status=row.status, description=row.subject_key, match_values=[row.title, row.slug, row.subject_key, row.latest_subject_slug])
        if kind in (None, "research_subject"):
            for row in session.query(ResearchSubject).filter(or_(ResearchSubject.display_name.ilike(pattern), ResearchSubject.slug.ilike(pattern), ResearchSubject.subject_key.ilike(pattern), ResearchSubject.description.ilike(pattern))).order_by(ResearchSubject.updated_at.desc(), ResearchSubject.id.desc()).limit(limit).all():
                add_match(match_kind="research_subject", object_id=int(row.id), label=row.display_name, slug=row.slug, status=row.status, description=row.kind, match_values=[row.display_name, row.slug, row.subject_key, row.description])
        if kind in (None, "knowledge_asset"):
            for row in session.query(KnowledgeAsset).filter(or_(KnowledgeAsset.title.ilike(pattern), KnowledgeAsset.slug.ilike(pattern), KnowledgeAsset.summary.ilike(pattern))).order_by(KnowledgeAsset.updated_at.desc(), KnowledgeAsset.id.desc()).limit(limit).all():
                add_match(match_kind="knowledge_asset", object_id=int(row.id), label=row.title, slug=row.slug, status=row.status, description=row.kind, match_values=[row.title, row.slug, row.summary])
        if kind in (None, "research_play"):
            for row in session.query(ResearchPlay).filter(or_(ResearchPlay.name.ilike(pattern), ResearchPlay.slug.ilike(pattern), ResearchPlay.description.ilike(pattern), ResearchPlay.owner.ilike(pattern))).order_by(ResearchPlay.updated_at.desc(), ResearchPlay.id.desc()).limit(limit).all():
                add_match(match_kind="research_play", object_id=int(row.id), label=row.name, slug=row.slug, status=row.status, description=row.owner, match_values=[row.name, row.slug, row.description, row.owner])
        if kind in (None, "proposition"):
            for row in session.query(Proposition).filter(or_(Proposition.key.ilike(pattern), Proposition.description.ilike(pattern), Proposition.kind.ilike(pattern))).order_by(Proposition.id.desc()).limit(limit).all():
                add_match(match_kind="proposition", object_id=int(row.id), label=row.key, status=row.kind, description=row.description, match_values=[row.key, row.description, row.kind])
        if kind in (None, "observation"):
            for row in session.query(Observation).filter(or_(Observation.value_text.ilike(pattern), Observation.note.ilike(pattern), Observation.unit.ilike(pattern))).order_by(Observation.created_at.desc(), Observation.id.desc()).limit(limit).all():
                add_match(match_kind="observation", object_id=int(row.id), label=row.note or row.value_text or f"Observation {int(row.id)}", description=row.unit, match_values=[row.value_text, row.note, row.unit])
    finally:
        session.close()

    matches.sort(key=lambda item: (int(item.get("_score") or 999), str(item.get("kind") or ""), str(item.get("label") or "").lower()))
    trimmed = []
    for item in matches[:limit]:
        row = dict(item)
        row.pop("_score", None)
        trimmed.append(row)
    return {
        "query": query,
        "kind": kind or "all",
        "count": len(trimmed),
        "matches": trimmed,
        "usage": "Call `resolve_object` first when you know the title but not the numeric id required by `get_*` or `update_*` tools.",
    }


def _mcp_tool_search_documents(params: dict[str, object]) -> dict[str, object]:
    query = str(params.get("query") or "").strip() or None
    triage_state = str(params.get("triage_state") or "").strip() or None
    saved = params.get("saved")
    saved_val = None
    if saved is not None:
        saved_val = bool(saved)
    source_id = params.get("source_id")
    domain = str(params.get("domain") or "").strip() or None
    language = str(params.get("language") or "").strip() or None
    days = params.get("days")
    days_val = int(days) if isinstance(days, int) or str(days).isdigit() else 30
    limit = params.get("limit")
    limit_val = int(limit) if isinstance(limit, int) or str(limit).isdigit() else 20

    session = _session_read()
    try:
        docs = list_documents(
            session,
            query=query,
            days=days_val,
            triage_state=triage_state or None,
            saved=saved_val,
            source_id=int(source_id) if str(source_id).isdigit() else None,
            domain=domain,
            language=language,
            defer_text=True,
            include_domains=False,
            order_by_relevance=bool(query),
            limit=max(limit_val * 4, 40),
            offset=0,
        )
        matches = document_match_projection(session, [int(doc.id) for doc in docs], query=query or "", limit=500)
        if query:
            docs.sort(
                key=lambda doc: (
                    float((matches.get(int(doc.id)) or {}).get("relevance_score") or 0.0),
                    int(doc.id),
                ),
                reverse=True,
            )
        out = []
        for doc in docs[: max(1, min(limit_val, 100))]:
            match = matches.get(int(doc.id)) or {}
            out.append(
                {
                    "id": doc.id,
                    "title": doc.title,
                    "url": doc.url or doc.canonical_url,
                    "source": doc.source.name if doc.source else None,
                    "triage_state": doc.triage_state,
                    "saved": bool(doc.saved),
                    "published_at": doc.published_at.isoformat() if doc.published_at else None,
                    "document_summary": None,
                    "match_excerpt": match.get("match_excerpt"),
                    "match_found": bool(match.get("match_found")),
                    "match_id": match.get("match_id"),
                    "match_start": match.get("match_start"),
                    "match_end": match.get("match_end"),
                    "text_version": match.get("text_version"),
                    "text_length_chars": match.get("text_length_chars"),
                    "page_start": match.get("page_start"),
                    "page_end": match.get("page_end"),
                    "relevance_score": match.get("relevance_score"),
                }
            )
        return {"count": len(out), "documents": out}
    finally:
        session.close()


def _mcp_tool_get_document(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("id")
    if raw_id is None or not str(raw_id).isdigit():
        raise ValueError("Missing document id.")
    doc_id = int(raw_id)
    session = _session_read()
    try:
        doc = (
            session.query(Document)
            .options(
                load_only(
                    Document.id,
                    Document.title,
                    Document.extra,
                    Document.url,
                    Document.canonical_url,
                    Document.author,
                    Document.triage_state,
                    Document.saved,
                    Document.published_at,
                    Document.source_id,
                ),
                joinedload(Document.source).load_only(Source.id, Source.name),
                selectinload(Document.domains).load_only(Domain.id, Domain.slug),
                selectinload(Document.tags).load_only(Tag.id, Tag.slug),
            )
            .filter_by(id=doc_id)
            .first()
        )
        if not doc:
            raise ValueError("Document not found.")
        text_projection = _mcp_document_text_excerpt(session, int(doc.id), limit=1200)
        has_images = session.query(Attachment.id).filter(
            Attachment.document_id == int(doc.id),
            Attachment.kind == "image",
        ).limit(1).first() is not None
        text = str(text_projection["text"])
        translation_title = None
        if isinstance(doc.extra, dict):
            translation_title = doc.extra.get("translation_title_en")
        return {
            "id": doc.id,
            "title": doc.title,
            "translation_title": translation_title,
            "url": doc.url or doc.canonical_url,
            "source": doc.source.name if doc.source else None,
            "author": doc.author,
            "triage_state": doc.triage_state,
            "saved": bool(doc.saved),
            "published_at": doc.published_at.isoformat() if doc.published_at else None,
            "text": text,
            "text_scope": "leading_excerpt",
            "text_length_chars": text_projection["text_length_chars"],
            "returned_text_length_chars": text_projection["returned_text_length_chars"],
            "truncated": text_projection["truncated"],
            "has_images": has_images,
            "domains": [d.slug for d in doc.domains],
            "tags": [t.slug for t in doc.tags],
        }
    finally:
        session.close()


def _mcp_tool_get_document_image(params: dict[str, object]) -> dict[str, object]:
    """List stored article images or return one original image as an MCP image block."""
    raw_id = params.get("document_id")
    if raw_id is None or not str(raw_id).isdigit():
        raise ValueError("document_id is required.")
    document_id = int(raw_id)
    raw_attachment_id = params.get("attachment_id")
    if raw_attachment_id is not None and not str(raw_attachment_id).isdigit():
        raise ValueError("attachment_id must be a positive integer.")
    attachment_id = int(raw_attachment_id) if raw_attachment_id is not None else None
    supported_types = {"image/png", "image/jpeg", "image/webp", "image/gif"}
    session = _session_read()
    try:
        document = (
            session.query(Document)
            .options(
                load_only(Document.id, Document.title, Document.extra),
                selectinload(Document.attachments),
            )
            .filter(Document.id == document_id)
            .first()
        )
        if document is None:
            raise ValueError("Document not found.")
        manifest = (document.extra or {}).get("image_manifest") if isinstance(document.extra, dict) else None
        context_by_url = {
            str(row.get("url") or ""): row
            for row in manifest or []
            if isinstance(row, dict) and row.get("url")
        }
        images: list[tuple[Attachment, dict[str, object]]] = []
        seen_hashes: set[str] = set()
        for attachment in sorted(document.attachments or [], key=lambda row: int(row.id)):
            content_type = str(attachment.content_type or "").split(";", 1)[0].strip().lower()
            if content_type not in supported_types:
                continue
            fingerprint = str(attachment.sha256 or attachment.id)
            if fingerprint in seen_hashes:
                continue
            seen_hashes.add(fingerprint)
            context = context_by_url.get(str(attachment.original_url or ""), {})
            images.append((attachment, {
                "attachment_id": int(attachment.id),
                "content_type": content_type,
                "sha256": attachment.sha256,
                "original_url": attachment.original_url,
                "alt": str(context.get("alt") or "")[:300],
                "caption": str(context.get("caption") or "")[:600],
                "heading": str(context.get("heading") or "")[:300],
            }))
        if attachment_id is None:
            return {
                "document_id": document_id,
                "title": document.title,
                "image_count": len(images),
                "images": [metadata for _, metadata in images[:40]],
                "truncated": len(images) > 40,
                "next": "Call get_document_image with document_id and attachment_id to inspect original pixels.",
            }
        selected = next(((row, metadata) for row, metadata in images if int(row.id) == attachment_id), None)
        if selected is None:
            raise ValueError("Image attachment is not available for this document.")
        attachment, metadata = selected
        path = _safe_attachment_path(attachment)
        if path is None or not path.is_file():
            raise ValueError("Stored image attachment is unavailable.")
        if path.stat().st_size > 8_000_000:
            raise ValueError("Stored image exceeds the 8 MB response limit.")
        pixels = path.read_bytes()
        if not pixels or (attachment.sha256 and hashlib.sha256(pixels).hexdigest() != attachment.sha256):
            raise ValueError("Stored image attachment failed integrity verification.")
        return {
            "document_id": document_id,
            "title": document.title,
            **metadata,
            "byte_count": len(pixels),
            "_image_data_base64": base64.b64encode(pixels).decode("ascii"),
        }
    finally:
        session.close()


def _mcp_tool_query_document(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("document_id", params.get("id"))
    if raw_id is None or not str(raw_id).isdigit():
        raise ValueError("document_id is required.")
    query = str(params.get("query") or "").strip()
    if not query:
        raise ValueError("query is required.")
    with session_scope() as session:
        return query_document_passages(
            session,
            document_id=int(raw_id),
            query=query,
            top_k=int(params.get("top_k") or 5),
            max_tokens=int(params.get("max_tokens") or 2500),
            mode=str(params.get("mode") or "hybrid"),
        )


def _mcp_tool_expand_document_match(params: dict[str, object]) -> dict[str, object]:
    match_id = str(params.get("match_id") or "").strip()
    if not match_id:
        raise ValueError("match_id is required.")
    with session_scope() as session:
        return expand_document_match(
            session,
            match_id=match_id,
            before_chars=int(params.get("before_chars") or 4000),
            after_chars=int(params.get("after_chars") or 4000),
        )


def _mcp_tool_read_document(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("document_id", params.get("id"))
    if raw_id is None or not str(raw_id).isdigit():
        raise ValueError("document_id is required.")
    raw_index = params.get("passage_index")
    passage_index = int(raw_index) if raw_index is not None and str(raw_index).isdigit() else None
    with session_scope() as session:
        return read_document_passages(
            session,
            document_id=int(raw_id),
            passage_id=str(params.get("passage_id") or "").strip() or None,
            passage_index=passage_index,
            before=int(params.get("before") or 1),
            after=int(params.get("after") or 1),
            max_tokens=int(params.get("max_tokens") or 4000),
        )


def _mcp_tool_inspect_document(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("document_id", params.get("id"))
    if raw_id is None or not str(raw_id).isdigit():
        raise ValueError("document_id is required.")
    with session_scope() as session:
        return inspect_document_structure(session, document_id=int(raw_id))


def _mcp_tool_render_document_page(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("document_id", params.get("id"))
    raw_page = params.get("page_number", params.get("page"))
    if raw_id is None or not str(raw_id).isdigit():
        raise ValueError("document_id is required.")
    if raw_page is None or not str(raw_page).isdigit():
        raise ValueError("page_number is required.")
    with session_scope() as session:
        return render_document_page(
            session,
            document_id=int(raw_id),
            page_number=int(raw_page),
            dpi=int(params.get("dpi") or 150),
        )


def _mcp_tool_ingest_research_library(params: dict[str, object]) -> dict[str, object]:
    library_dir = str(params.get("library_dir") or "").strip() or None
    with session_scope() as session:
        return ingest_research_library(session, library_dir=library_dir)


def _mcp_tool_list_research_library(params: dict[str, object]) -> dict[str, object]:
    query = str(params.get("query") or "").strip() or None
    conference = str(params.get("conference") or "").strip() or None
    content_kind = str(params.get("content_kind") or "").strip() or None
    year_raw = params.get("conference_year")
    conference_year = int(year_raw) if year_raw is not None and str(year_raw).isdigit() else None
    stale_only = bool(params.get("stale_only", False))
    limit = params.get("limit")
    limit_val = int(limit) if str(limit).isdigit() else 50
    session = _session_read()
    try:
        return list_research_library(
            session,
            query=query,
            conference=conference,
            conference_year=conference_year,
            content_kind=content_kind,
            stale_only=stale_only,
            limit=limit_val,
        )
    finally:
        session.close()


def _mcp_tool_list_research_conference_index(params: dict[str, object]) -> dict[str, object]:
    query = str(params.get("query") or "").strip() or None
    limit = params.get("limit")
    limit_val = int(limit) if str(limit).isdigit() else 50
    session = _session_read()
    try:
        return list_research_conference_index(session, query=query, limit=limit_val)
    finally:
        session.close()


def _mcp_tool_get_research_library_report(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("document_id") or params.get("id")
    if raw_id is None or not str(raw_id).isdigit():
        raise ValueError("document_id is required.")
    session = _session_read()
    try:
        return get_research_library_report(session, document_id=int(raw_id))
    finally:
        session.close()


def _mcp_tool_review_research_library(params: dict[str, object]) -> dict[str, object]:
    guild_raw = params.get("guild_id")
    guild_id = int(guild_raw) if guild_raw is not None and str(guild_raw).isdigit() else None
    stale_only = bool(params.get("stale_only", False))
    window_raw = params.get("review_window_days")
    limit_raw = params.get("limit")
    review_window_days = int(window_raw) if window_raw is not None and str(window_raw).isdigit() else 14
    limit = int(limit_raw) if limit_raw is not None and str(limit_raw).isdigit() else 50
    with session_scope() as session:
        return review_research_library(
            session,
            guild_id=guild_id,
            stale_only=stale_only,
            review_window_days=review_window_days,
            limit=limit,
        )


def _mcp_tool_add_research_report_addendum(params: dict[str, object]) -> dict[str, object]:
    raw_id = params.get("document_id") or params.get("id")
    if raw_id is None or not str(raw_id).isdigit():
        raise ValueError("document_id is required.")
    body_md = str(params.get("body_md") or "").strip()
    if not body_md:
        raise ValueError("body_md is required.")
    title = str(params.get("title") or "").strip()
    actor = str(params.get("actor") or "mcp").strip() or "mcp"
    with session_scope() as session:
        return add_research_report_addendum(
            session,
            document_id=int(raw_id),
            title=title,
            body_md=body_md,
            actor=actor,
        )


def _mcp_tool_upsert_research_source_card(params: dict[str, object]) -> dict[str, object]:
    title = str(params.get("title") or "").strip()
    body_md = str(params.get("body_md") or params.get("text") or "").strip()
    if not title:
        raise ValueError("title is required.")
    if not body_md:
        raise ValueError("body_md is required.")
    topics_raw = params.get("topics")
    if isinstance(topics_raw, list):
        topics = [str(row).strip() for row in topics_raw if str(row).strip()]
    else:
        topics = [part.strip() for part in str(topics_raw or "").split(",") if part.strip()]
    year_raw = params.get("conference_year")
    conference_year = int(year_raw) if year_raw is not None and str(year_raw).isdigit() else None
    with session_scope() as session:
        return upsert_research_source_card(
            session,
            title=title,
            body_md=body_md,
            url=str(params.get("url") or "").strip() or None,
            source_tier=str(params.get("source_tier") or "technical paper/conference material").strip(),
            content_kind=str(params.get("content_kind") or "research_source_card").strip(),
            conference=str(params.get("conference") or "").strip() or None,
            conference_year=conference_year,
            topics=topics,
            actor=str(params.get("actor") or "mcp").strip() or "mcp",
        )


def _mcp_tool_search(params: dict[str, object]) -> dict[str, object]:
    query = str(params.get("query") or "").strip()
    if not query:
        raise ValueError("query is required.")
    kind = str(params.get("kind") or "all").strip() or "all"
    limit = params.get("limit")
    limit_val = int(limit) if isinstance(limit, int) or str(limit).isdigit() else 10
    include_hidden = _request_includes_hidden_theses(params.get("include_hidden"))
    session = _session_read()
    try:
        rich = object_aware_search(
            session,
            query=query,
            kind=kind,
            limit=max(1, min(limit_val, 30)),
            include_neighborhoods=True,
            include_hidden=include_hidden,
        )
        results: list[dict[str, object]] = []
        for item in rich.get("items") or []:
            if not isinstance(item, dict) or item.get("kind") != "document":
                continue
            object_id = int(item.get("object_id") or 0)
            title = str(item.get("label") or f"Document {object_id}").strip()
            url = _mcp_absolute_url(item.get("url") or item.get("href") or f"/docs/{object_id}")
            if not url:
                continue
            results.append(
                {
                    "id": f"doc:{object_id}",
                    "title": title,
                    "url": url,
                    "document_summary": item.get("document_summary"),
                    "match_excerpt": item.get("match_excerpt"),
                    "match_found": item.get("match_found"),
                    "match_id": item.get("match_id"),
                    "match_start": item.get("match_start"),
                    "match_end": item.get("match_end"),
                    "text_version": item.get("text_version"),
                    "text_length_chars": item.get("text_length_chars"),
                    "page_start": item.get("page_start"),
                    "page_end": item.get("page_end"),
                    "relevance_score": item.get("relevance_score"),
                }
            )
        return {
            "results": results,
            "object_results": rich.get("items") or [],
            "groups": rich.get("groups") or {},
            "neighborhoods": rich.get("neighborhoods") or [],
            "guidance": rich.get("guidance") or {},
        }
    finally:
        session.close()


def _mcp_tool_object_search(params: dict[str, object]) -> dict[str, object]:
    query = str(params.get("query") or "").strip()
    if not query:
        raise ValueError("query is required.")
    limit = params.get("limit")
    include = params.get("include_neighborhoods")
    include_hidden = params.get("include_hidden")
    session = _session_read()
    try:
        return object_aware_search(
            session,
            query=query,
            kind=str(params.get("kind") or "all").strip() or "all",
            limit=int(limit) if isinstance(limit, int) or str(limit).isdigit() else 10,
            include_neighborhoods=_boolish_value(include, default=True),
            include_hidden=_boolish_value(include_hidden, default=False),
        )
    finally:
        session.close()


def _mcp_tool_list_research_signal_kinds(params: dict[str, object]) -> dict[str, object]:
    del params
    return {
        "count": len(research_signal_kind_profiles()),
        "kinds": research_signal_kind_profiles(),
        "usage": "Use these as Thesis.kind values. Claims and theses intentionally overlap; kind describes how agents should retrieve, update, and weigh evidence.",
    }


def _mcp_tool_fetch(params: dict[str, object]) -> dict[str, object]:
    raw_id = str(params.get("id") or "").strip()
    if not raw_id:
        raise ValueError("id is required.")
    doc_id: int | None = None
    if raw_id.startswith("doc:") and raw_id[4:].isdigit():
        doc_id = int(raw_id[4:])
    elif raw_id.isdigit():
        doc_id = int(raw_id)
    session = _session_read()
    try:
        q = session.query(Document).options(
            load_only(
                Document.id,
                Document.title,
                Document.url,
                Document.canonical_url,
                Document.published_at,
                Document.triage_state,
                Document.saved,
                Document.source_id,
            ),
            joinedload(Document.source).load_only(Source.id, Source.name),
            selectinload(Document.domains).load_only(Domain.id, Domain.slug),
            selectinload(Document.tags).load_only(Tag.id, Tag.slug),
        )
        if doc_id is not None:
            doc = q.filter(Document.id == doc_id).first()
        else:
            doc = q.filter(or_(Document.url == raw_id, Document.canonical_url == raw_id)).first()
        if not doc:
            raise ValueError("Document not found.")
        text_projection = _mcp_document_text_excerpt(session, int(doc.id), limit=1200)
        text = str(text_projection["text"])
        url = _mcp_absolute_url(doc.url or doc.canonical_url or f"/docs/{int(doc.id)}")
        return {
            "id": f"doc:{int(doc.id)}",
            "title": doc.title or f"Document {int(doc.id)}",
            "text": text,
            "text_scope": "leading_excerpt",
            "text_length_chars": text_projection["text_length_chars"],
            "returned_text_length_chars": text_projection["returned_text_length_chars"],
            "truncated": text_projection["truncated"],
            "url": url,
            "metadata": {
                "source": doc.source.name if doc.source else None,
                "published_at": doc.published_at.isoformat() if doc.published_at else None,
                "triage_state": doc.triage_state,
                "saved": bool(doc.saved),
                "domains": [d.slug for d in doc.domains],
                "tags": [t.slug for t in doc.tags],
                "text_excerpt": _truncate_text(text, limit=420),
            },
        }
    finally:
        session.close()
