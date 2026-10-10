from __future__ import annotations

import asyncio
import base64
import difflib
import json
import logging
import secrets
import re
import time
import requests
from types import SimpleNamespace
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse
from zoneinfo import ZoneInfo

from fastapi import Body, Depends, FastAPI, File, Form, Request, Response, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    RedirectResponse,
    StreamingResponse,
)
from thoth_terminal.web.json_response import JSONResponse
from thoth_terminal.web.public_boundary import PublicSurfaceBoundary
from thoth_terminal.web.public_reports import published_report_metadata
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import OperationalError
from sqlalchemy import func, or_, and_, case
from sqlalchemy.orm import Session
from sqlalchemy.orm import Load, defer, joinedload, load_only, selectinload

from thoth_terminal.omori_mood_theme import OMORI_MOOD_BACKGROUNDS, OMORI_MOOD_THEMES
from thoth_terminal import __version__

from thoth_terminal.agent.autonomy import (
    PROPOSAL_STATUS_PENDING,
    PROPOSAL_STATUS_PROMOTED,
    PROPOSAL_STATUS_REJECTED,
    RUN_STATUS_ERROR,
    RUN_STATUS_RUNNING,
    RUN_STATUS_SUCCESS,
    get_change_run,
    list_change_runs,
    list_thesis_proposals,
    record_change_event,
    review_thesis_proposal,
    serialize_change_run,
    serialize_thesis_proposal,
)
from thoth_terminal.agent.context_bundle import build_recent_context_bundle
from thoth_terminal.agent.llm_client import AgentLlmError, ChatMessage, chat_json_auto, llm_metadata
from thoth_terminal.agent.model_catalog import build_model_catalog
from thoth_terminal.agent.thesis_autopilot import (
    AUTOPILOT_ACTION_KIND,
    build_thesis_autopilot_snapshot,
    compute_evidence_need,
)
from thoth_terminal.agent_actions import (
    ACTION_STATUS_COMPLETED,
    ACTION_STATUS_PENDING,
    ACTION_STATUS_RUNNING,
    ACTION_STATUS_SKIPPED,
    action_status_filter_values,
    canonical_action_status,
    list_agent_actions,
    serialize_agent_action,
    update_agent_action_status,
)
from thoth_terminal.config import configured_allowed_origins, ensure_data_dirs, settings
from thoth_terminal.dataset_service import (
    build_dataset_chart_cards,
    build_dataset_view_builder,
    bulk_paste_dataset_draft_rows,
    create_dataset,
    create_dataset_view,
    dataset_family_meta,
    delete_dataset_draft_row,
    ensure_dataset_draft,
    ensure_dataset_tables,
    export_dataset_csv_bytes,
    export_dataset_xlsx_bytes,
    latest_dataset_version,
    link_dataset,
    list_dataset_draft_rows,
    normalize_dataset_view_definition,
    parse_structured_dataset_view_payload,
    unlink_dataset,
    parse_csv_dataset,
    parse_xlsx_dataset,
    preview_dataset_view,
    publish_dataset_snapshot,
    query_dataset,
    replace_dataset_draft,
    serialize_dataset,
    serialize_dataset_summaries,
    serialize_dataset_summary,
    upsert_dataset_draft_row,
    update_dataset_metadata,
)
from thoth_terminal.decision_spine_service import (
    cached_company_decision_payload,
    cached_company_decision_page_payload,
    calendar_today,
    company_decision_payload,
    list_analyst_evidence,
    list_investment_catalysts,
    resolve_company_or_issuer_from_payload,
    serialize_analyst_evidence,
    serialize_catalyst,
    serialize_memo,
    sync_decision_spine,
    upsert_analyst_evidence,
    upsert_investment_catalyst,
    upsert_investment_memo,
)
from thoth_terminal.integration_health import official_api_key_health
from thoth_terminal.integrations.ibkr.client import IbkrAuthError, IbkrClientError
from thoth_terminal.integrations.ibkr.gateway import gateway_browser_login_url, launch_gateway_if_needed
from thoth_terminal.integrations.ibkr.routes import ibkr_service, register_ibkr_routes
from thoth_terminal.home_intelligence import build_home_intelligence, build_market_watchlist, derive_home_portfolio_state
from thoth_terminal.institutional_model_vocab import MODEL_METRICS
from thoth_terminal.knowledge_readiness import (
    list_knowledge_readiness,
    score_knowledge_asset,
)
from thoth_terminal.local_agent_review import (
    build_local_agent_review_state,
    create_eval_case_from_compression,
    record_review_decision,
    run_eval_case,
    seed_default_eval_cases,
)
from thoth_terminal.local_agent_service import build_agent_overview_state, build_agent_workspace_state
from thoth_terminal.object_search import object_aware_search
from thoth_terminal.portfolio_targets import read_portfolio_target_profile, write_portfolio_target_profile
from thoth_terminal.db import DatabaseBusyError, get_session, session_factory, session_scope, utcnow
from thoth_terminal.ttl_cache import TTLCache, json_cache_weight
from thoth_terminal.url_utils import clean_url
from thoth_terminal.document_media import (
    extract_candidate_image_urls,
    extract_external_links,
    extract_image_ocr_notes,
    extract_thread_sections,
)
from thoth_terminal.document_readability import maybe_hydrate_pdf_attachment_text, maybe_hydrate_url_document_text
from thoth_terminal.export import export_bundle, export_note_bundle, export_thesis_dossier
from thoth_terminal.fx import ensure_fx_rates, get_latest_rates
from thoth_terminal.foundation_service import maybe_hydrate_transcript_document
from thoth_terminal.ingest import ingest_note, ingest_source, ingest_tweet
from thoth_terminal.institutional_service import (
    backfill_structured_facts,
    ensure_source_profile,
    serialize_source_profile,
    sync_source_profiles,
)
from thoth_terminal.instrument_service import (
    build_active_universe_request_scope,
    coverage_gap_scope,
    create_instrument_request,
    fulfill_instrument_request,
    get_instrument_request,
    instrument_request_markdown,
    instrument_request_scope_counts,
    instrument_request_scope_map,
    instrument_request_scope,
    list_instrument_requests,
    request_queue_scope_matches,
    review_instrument_request,
    run_instrument_request,
    serialize_instrument_request,
    serialize_instrument_request_summary,
)
from thoth_terminal.source_acquisition_registry import (
    ensure_source_acquisition_templates,
    get_source_acquisition_rail,
    instrument_request_payload_for_acquisition_rail,
    list_source_acquisition_rails,
)
from thoth_terminal.connectors.web_list_browser import probe_web_list_browser
from thoth_terminal.connectors.paid_reader_browser import probe_paid_reader_browser
from thoth_terminal.connectors.x_list import probe_x_list
from thoth_terminal.connectors import fetch_items_result
from thoth_terminal.discord import link_intake as discord_link_intake
from thoth_terminal.discord.models import DiscordEvidenceItem, DiscordEvidenceTarget
from thoth_terminal.jobs import enqueue_ingest_job, enqueue_postprocess_job, enqueue_research_play_job, enqueue_watchlist_transcript_scan_job
from thoth_terminal.migrations import init_db
from thoth_terminal.models import (
    AlignmentFeedback,
    AgentAction,
    AgentChangeRun,
    AnalystEvidence,
    Attachment,
    Chart,
    ChartDatasetView,
    ChartSeries,
    Company,
    CompanyDocument,
    CoverageGap,
    DataPoint,
    DataSeries,
    Dataset,
    DatasetColumn,
    DatasetLink,
    DatasetVersion,
    DatasetView,
    Document,
    DocumentEntity,
    DocumentEvent,
    Domain,
    EarningsEvent,
    Entity,
    EventType,
    ExchangeDisclosure,
    IngestionRun,
    InstrumentRequest,
    InvestmentCatalyst,
    InvestmentMemo,
    InstitutionalObservation,
    IRArtifact,
    IPOEvent,
    Job,
    Issuer,
    IssuerIdentifier,
    KnowledgeAsset,
    KnowledgeAssetVersion,
    DocumentMention,
    Note,
    NoteDocument,
    NoteProposition,
    NoteThesis,
    NotificationRule,
    Observation,
    ProductStageEntry,
    ProductStageHistory,
    Proposition,
    Program,
    ResearchInstrument,
    ResearchPlay,
    ResearchPlayRun,
    ResearchPlaySignal,
    ResearchPlaySkill,
    ResearchReport,
    ResearchReportArtifact,
    ResearchReportQuality,
    ResearchReportVersion,
    ResearchSkill,
    ResearchSourcePack,
    ResearchSubject,
    ResearchSubjectAlias,
    ResearchUniverse,
    ResearchUniverseRow,
    ResearchUniverseSection,
    SecuritiesFiling,
    Source,
    SourceActor,
    SourceActorAlias,
    SourceProfile,
    SourceState,
    Site,
    StructuredDataFact,
    SupplyChain,
    SupplyChainAlertRule,
    SupplyChainEdge,
    SupplyChainLink,
    SupplyChainMetricBinding,
    SupplyChainNode,
    SupplyChainRelation,
    SupplyChainScenario,
    SupplyChainSnapshot,
    SupplyChainVersion,
    Tag,
    Thesis,
    ThesisEvidence,
    ThesisProposal,
    ThesisProposition,
    TranscriptSummary,
    TranscriptWatchState,
    document_domains,
    document_event_types,
    document_tags,
)
from thoth_terminal.universe_service import (
    add_universe_row,
    delete_section,
    delete_universe,
    delete_universe_row,
    ensure_section,
    ensure_universe,
    import_ibkr_watchlist_payload,
    instrument_detail,
    label_suggestions,
    list_universes,
    normalize_sparse_ibkr_contracts,
    reconcile_universe_provider_links,
    reorder_universe_rows,
    reorder_universe_sections,
    reorder_universes,
    search_instruments,
    serialize_instrument,
    serialize_universe,
    update_section,
    update_universe,
    update_universe_row,
)
from thoth_terminal.gap_hunter_service import (
    resolve_gap_hunter_context_request,
    run_gap_hunter_cloud_task,
    universe_coverage_index,
)
from thoth_terminal.gap_hunter_agent_service import (
    continue_gap_hunter_goal_run,
    start_gap_hunter_goal_run,
    step_gap_hunter_goal_run,
)
from thoth_terminal.cloud_goal_run_service import get_cloud_goal_run, list_cloud_goal_events, serialize_cloud_goal_run
from thoth_terminal.cloud_goal_run_service import compact_cloud_goal_context
from thoth_terminal.red_team_agent_service import continue_red_team_goal_run, start_red_team_goal_run, step_red_team_goal_run
from thoth_terminal.report_body_builder_agent_service import (
    continue_report_body_builder_goal_run,
    start_report_body_builder_goal_run,
    step_report_body_builder_goal_run,
)
from thoth_terminal.source_pack_agent_service import (
    continue_source_pack_goal_run,
    start_source_pack_goal_run,
    step_source_pack_goal_run,
)
from thoth_terminal.bottleneck_constraint_agent_service import (
    continue_bottleneck_constraint_goal_run,
    start_bottleneck_constraint_goal_run,
    step_bottleneck_constraint_goal_run,
)
from thoth_terminal.cloud_orchestrator_service import (
    continue_cloud_orchestrator_run,
    continue_cloud_orchestrator_handoffs,
    dispatch_next_cloud_orchestrator_step,
    start_cloud_orchestrator_run,
    step_cloud_orchestrator_run,
)
from thoth_terminal.cloud_goal_command_service import (
    ensure_cloud_overlord_skill,
    execute_cloud_goal_command,
    list_cloud_goals,
    serialize_cloud_overlord_skill,
    update_cloud_overlord_skill,
)
from thoth_terminal.cloud_skill_runtime_service import (
    cloud_skill_runtime_payload,
    compile_static_cloud_skill_protocol,
    resolve_cloud_skills,
)
from thoth_terminal.cloud_prompt_state_service import (
    get_or_create_cloud_prompt_state_snapshot,
    render_cloud_prompt_state_delta,
)
from thoth_terminal.cloud_prompt_volatile_state_service import (
    get_or_create_cloud_prompt_volatile_snapshot,
    render_cloud_prompt_volatile_delta,
)
from thoth_terminal.cloud_research_automation import (
    CLOUD_RESEARCH_AUTOMATION_SYSTEM,
    CLOUD_RESEARCH_PROMPT_CACHE_KEY,
)
from thoth_terminal.cloud_task_catalog import get_task_capability, list_task_capabilities
from thoth_terminal.source_pack_service import build_source_pack
from thoth_terminal.structured_data_service import company_structured_fact_scope_ids
from thoth_terminal.security import (
    assert_no_trading_routes,
    build_security_posture,
    public_safe_path,
    validate_security_posture,
)
from thoth_terminal.report_jobs import enqueue_report_build
from thoth_terminal.research_play_service import (
    bind_skill_to_play,
    create_research_play,
    create_research_skill,
    execute_research_play,
    get_research_play,
    get_research_skill,
    list_recent_play_runs,
    list_recent_play_signals,
    list_research_plays,
    list_research_skills,
    record_alignment_feedback,
    serialize_alignment_feedback,
    serialize_research_play,
    serialize_research_play_run,
    serialize_research_play_signal,
    serialize_research_skill,
)
from thoth_terminal.research_report_service import (
    REPORT_TYPE_LABELS,
    REPORT_TYPES,
    SECTION_ORDER as RESEARCH_REPORT_SECTION_ORDER,
    append_research_report_addendum,
    assemble_report_payload,
    build_bottleneck_report,
    apply_report_memory_decision,
    build_research_report,
    create_ai_clean_derivative,
    create_report_memory_review_request,
    import_research_report_bytes,
    list_research_report_versions,
    list_research_reports,
    normalize_report_type,
    _render_print_html,
    report_payload_has_detail_sections as service_report_payload_has_detail_sections,
    resolve_report_subject,
    serialize_report_summary,
    serialize_report_version,
)
from thoth_terminal.knowledge_service import (
    generate_knowledge_asset_draft,
    get_knowledge_asset,
    get_research_subject,
    list_knowledge_assets,
    list_research_subjects,
    publish_knowledge_asset,
    save_knowledge_asset_draft,
)
from thoth_terminal.research_library_service import (
    add_research_report_addendum,
    get_research_library_report,
    ingest_research_library,
    list_research_conference_index,
    list_research_library,
    review_research_library,
    upsert_research_source_card,
)
from thoth_terminal.freshness_service import (
    get_freshness_review,
    list_freshness_reviews,
    run_freshness_review,
)
from thoth_terminal.bottleneck_service import (
    assess_supply_chain_bottlenecks,
    get_bottleneck_model_payload,
    list_bottleneck_models,
    serialize_bottleneck_model,
    upsert_bottleneck_model,
)
from thoth_terminal.supply_chain_service import (
    apply_supply_chain_ops,
    bind_supply_chain_dataset,
    compare_snapshots,
    create_supply_chain,
    create_supply_chain_snapshot,
    ensure_base_scenario,
    ensure_draft_snapshot,
    ensure_supply_chain_snapshot_tables,
    get_supply_chain_node_details,
    get_supply_chain_subchain_preview,
    graph_from_snapshot,
    import_legacy_chain_if_needed,
    latest_chain_snapshot,
    latest_chain_snapshot_map,
    link_supply_chain_object,
    list_supply_chain_alerts,
    list_supply_chain_lints,
    publish_supply_chain_snapshot,
    replace_draft_snapshot_graph,
    serialize_supply_chain,
    supply_chain_stage_family_legend,
    supply_chain_state_summary,
    version_payload as supply_chain_version_payload,
)
from thoth_terminal.supply_chain_metric_service import (
    list_supply_chain_metric_bindings,
    suggest_metric_binding_request_fulfillments,
    suggest_dataset_supply_chain_bindings,
    suggest_supply_chain_metric_bindings,
)
from thoth_terminal.product_stage_service import (
    create_product_stage_claim,
    get_product_stage_entry,
    get_product_stage_claim,
    link_product_stage_entry_object,
    list_product_stage_claims,
    list_product_stage_entries,
    list_product_stage_entries_linked_to_note,
    list_product_stage_entries_linked_to_thesis,
    list_product_stage_history,
    query_product_stage,
    review_product_stage_claim,
    serialize_product_stage_claim,
    serialize_product_stage_entry,
    serialize_product_stage_history,
    unlink_product_stage_entry_object,
    update_product_stage_entry,
)
from thoth_terminal.notes_query import list_notes, search_note_ids
from thoth_terminal.notes_service import (
    NOTE_LINK_RELATIONS,
    NOTE_PROP_RELATIONS,
    NOTE_TYPES,
    create_note,
    link_note_document,
    link_note_proposition,
    link_note_thesis,
    render_markdown_batch_to_html,
    render_markdown_to_html,
    unlink_note_document,
    unlink_note_proposition,
    unlink_note_thesis,
    update_note,
)
from thoth_terminal.object_store import put_bytes, guess_extension, abs_path as object_abs_path
from thoth_terminal.query import (
    document_text_projection,
    human_feed_visible_filter,
    is_human_feed_hidden_source_name,
    list_documents,
    list_sources,
    search_document_ids,
)
from thoth_terminal.scheduler import run_scheduler_tick_no_lock
from thoth_terminal.sec_filing_text import build_sec_filing_review, filing_review_excerpt, maybe_hydrate_sec_filing_document
from thoth_terminal.sources.cleanup import cleanup_legacy_company_sources, is_cleanup_candidate, retire_source_definition
from thoth_terminal.sources.policy import (
    apply_noninteractive_browser_policy,
    apply_source_deprecation_policy,
    is_source_deprecated_config,
)
from thoth_terminal.transcripts import (
    _parse_period_from_text,
    earnings_fiscal_year_bounds,
    is_plausible_earnings_fiscal_year,
)
from thoth_terminal.transcripts import get_transcript as query_transcript
from thoth_terminal.transcripts import list_transcripts as query_transcripts
from thoth_terminal.trade import (
    TRADE_CATEGORY,
    find_trade_series_by_hs_payload,
    get_trade_series_payload,
    list_trade_series_payload,
    trade_latest_summary_payload,
)
from thoth_terminal.thesis_service import (
    add_observation,
    add_thesis_evidence,
    create_thesis,
    link_thesis_proposition,
    parse_key_inline_observation,
    resolve_domains,
    resolve_entities,
    resolve_event_types,
    resolve_tags,
    upsert_proposition,
)
from thoth_terminal.research_signal_service import (
    THESIS_HIDDEN_STATUSES,
    THESIS_WORKFLOW_STATUSES,
    normalize_research_signal_kind,
    research_signal_kind_profiles,
    thesis_kind_profile,
)
from thoth_terminal.triage_service import (
    TRIAGE_ACTIONED,
    TRIAGE_ARCHIVED,
    TRIAGE_IGNORED,
    TRIAGE_NEW,
    TRIAGE_OPENED,
    bulk_apply,
    mark_actioned,
    mark_archived,
    mark_ignored,
    mark_opened,
    mark_saved,
    mark_seen,
    mark_unsaved,
    reset_to_new,
)
from thoth_terminal.x_capture import fetch_oembed_tweet
from thoth_terminal.translate import translate_document
from thoth_terminal.runtime_env import configure_logging
from thoth_terminal.runtime_info import get_runtime_identity, list_runtime_states, runtime_fingerprint
from thoth_terminal.web.institutional_web import (
    handle_institutional_mcp_tool,
    institutional_mcp_tool_definitions,
    register_institutional_routes,
)
from thoth_terminal.web.dataset_routes import register_dataset_routes
from thoth_terminal.web.local_agent_routes import register_local_agent_routes
from thoth_terminal.web.mcp_routes import register_mcp_routes


BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals["settings"] = settings
templates.env.globals["omori_mood_backgrounds"] = OMORI_MOOD_BACKGROUNDS
templates.env.globals["omori_mood_themes"] = OMORI_MOOD_THEMES
templates.env.filters["clean_url"] = clean_url
configure_logging(settings.log_level)

_latest_ingest_cache: dict[str, object] = {"ts": 0.0, "db_path": None, "value": 0}
_LATEST_INGEST_TTL_S = 10.0


def _get_latest_ingest_id() -> int:
    """Get the latest ingestion run ID for seeding notification polling."""
    global _latest_ingest_cache
    now = time.time()
    db_key = str(settings.db_path)
    cached_db = _latest_ingest_cache.get("db_path")
    cached_value = _latest_ingest_cache.get("value")
    if cached_db == db_key and (now - float(_latest_ingest_cache.get("ts", 0.0))) < _LATEST_INGEST_TTL_S:
        return int(cached_value or 0)
    try:
        with session_scope() as db:
            latest = (
                db.query(IngestionRun.id)
                .filter(IngestionRun.finished_at.isnot(None))
                .order_by(IngestionRun.id.desc())
                .first()
            )
            value = int(latest[0]) if latest else 0
            _latest_ingest_cache = {"ts": now, "db_path": db_key, "value": value}
            return value
    except Exception:
        if cached_db == db_key:
            return int(cached_value or 0)
        return 0


templates.env.globals["get_latest_ingest_id"] = _get_latest_ingest_id


def _desktop_bridge(app_obj: FastAPI) -> object | None:
    state = getattr(app_obj, "state", None) if app_obj is not None else None
    attached = getattr(state, "desktop_bridge", None) if state is not None else None
    if attached is not None:
        return attached
    base_url = str(getattr(settings, "desktop_control_url", "") or "").strip()
    token = str(getattr(settings, "desktop_control_token", "") or "").strip()
    if not base_url or not token:
        return None
    cached = getattr(state, "remote_desktop_bridge", None) if state is not None else None
    if cached is None:
        from thoth_terminal.desktop.control_api import RemoteDesktopBridge

        cached = RemoteDesktopBridge(base_url, token)
        if state is not None:
            state.remote_desktop_bridge = cached
    return cached


def _desktop_status_payload(app_obj: FastAPI) -> dict[str, object] | None:
    bridge = _desktop_bridge(app_obj)
    if bridge is None:
        return None
    status_fn = getattr(bridge, "status_payload", None)
    if not callable(status_fn):
        return None
    payload = status_fn()
    if isinstance(payload, dict):
        return payload
    return {"enabled": True}


def _template_response(name: str, context: dict[str, object], **kwargs: object) -> HTMLResponse:
    request = context.get("request")
    if not isinstance(request, Request):
        raise ValueError(f"Template context for {name!r} is missing a FastAPI request object")
    context.setdefault("desktop_enabled", _desktop_bridge(request.scope.get("app")) is not None)
    return templates.TemplateResponse(request, name, context, **kwargs)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    ensure_data_dirs()
    validate_security_posture(
        settings,
        local_asset_refs=[
            settings.omori_local_asset_root,
            settings.omori_generated_asset_root,
            settings.omori_public_asset_root,
        ]
        if getattr(settings, "enable_omori_local_assets", False)
        else [],
    )
    assert_no_trading_routes(app.router.routes)
    logger.info("web startup: %s", runtime_fingerprint())
    if not bool(getattr(app.state, "database_prepared_by_desktop", False)):
        try:
            init_db(allow_locked=True)
        except Exception as exc:
            logger.exception("init_db failed on startup: %s", exc)
            raise
    # Policy normalization, institutional synchronization, and backfills are
    # explicit maintenance operations. Web startup establishes only the serving
    # plane and must not scan or rewrite durable research state.

    static_dir = BASE_DIR / "static"
    if static_dir.exists() and not any(route.path == "/static" for route in app.router.routes):
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    posture = build_security_posture(settings)
    if (
        (not posture.public)
        and settings.objects_dir.exists()
        and not any(route.path == "/objects" for route in app.router.routes)
    ):
        app.mount("/objects", StaticFiles(directory=str(settings.objects_dir)), name="objects")
    if (
        (not posture.public)
        and settings.omori_generated_asset_root.exists()
        and not any(route.path == "/omori-mood-runtime" for route in app.router.routes)
    ):
        app.mount(
            "/omori-mood-runtime",
            StaticFiles(directory=str(settings.omori_generated_asset_root)),
            name="omori-mood-runtime",
        )
    if (
        (not posture.public)
        and getattr(settings, "enable_omori_local_assets", False)
        and settings.omori_public_asset_root.exists()
        and not any(route.path == "/omori-local" for route in app.router.routes)
    ):
        app.mount("/omori-local", StaticFiles(directory=str(settings.omori_public_asset_root)), name="omori-local")

    if getattr(settings, "web_scheduler_enabled", False):
        app.state.scheduler_task = asyncio.create_task(_scheduler_loop())
    try:
        yield
    finally:
        task = getattr(app.state, "scheduler_task", None)
        if task:
            task.cancel()


app = FastAPI(title="Thoth Terminal", version=__version__, lifespan=_lifespan, default_response_class=JSONResponse)


@app.get("/public", response_class=HTMLResponse)
def public_surface_index(request: Request, db: Session = Depends(get_session)) -> HTMLResponse:
    rows = (
        db.query(ResearchReport, ResearchReportVersion)
        .join(ResearchReportVersion, ResearchReportVersion.id == ResearchReport.latest_published_version_id)
        .filter(ResearchReport.status == "active")
        .filter(ResearchReportVersion.status == "published")
        .order_by(
            ResearchReportVersion.published_at.desc().nullslast(),
            ResearchReportVersion.as_of_at.desc(),
            ResearchReport.title.asc(),
        )
        .limit(200)
        .all()
    )
    publications: list[dict[str, object]] = []
    for report, version in rows:
        metadata = published_report_metadata(version)
        publications.append(
            {
                **metadata,
                "slug": report.slug,
                "version": int(version.version),
                "published_at": version.published_at,
                "as_of_at": version.as_of_at,
            }
        )
    report_types = sorted({str(row["report_type"]) for row in publications})
    topics = sorted({str(row["topic"]) for row in publications})
    return _template_response(
        "public_index.html",
        {
            "request": request,
            "publications": publications,
            "report_types": report_types,
            "topics": topics,
        },
    )


register_institutional_routes(app, template_response=_template_response)
register_ibkr_routes(app)
register_local_agent_routes(app)
register_mcp_routes(app, context_provider=lambda: globals())
register_dataset_routes(app, context_provider=lambda: globals())
if getattr(settings, "capture_allow_x_origins", True):
    cors_origins = configured_allowed_origins() or [
        "https://x.com",
        "https://twitter.com",
        "https://mobile.twitter.com",
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=False,
        allow_methods=["POST", "OPTIONS"],
        allow_headers=["*"],
    )


def __getattr__(name: str) -> object:
    if name.startswith(("_mcp", "_MCP")) or name == "_research_canon_mcp_tool_definitions":
        from thoth_terminal.web import mcp as mcp_module

        # Compatibility access through ``web.app`` bypasses the HTTP gateway,
        # so hand the lazily imported engine the same application context.
        mcp_module._CONTEXT_PROVIDER = lambda: globals()
        attr = getattr(mcp_module, name)
        if not callable(attr):
            mcp_module._refresh_context()
            return attr

        def _mcp_proxy(*args: object, **kwargs: object) -> object:
            mcp_module._refresh_context()
            return attr(*args, **kwargs)

        _mcp_proxy.__name__ = name
        _mcp_proxy.__doc__ = getattr(attr, "__doc__", None)
        return _mcp_proxy
    if name.startswith(("_dataset", "_DATASET", "api_dataset")) or name in {"datasets_view", "dataset_workbench"}:
        from thoth_terminal.web import dataset_routes

        attr = getattr(dataset_routes, name)
        if not callable(attr):
            dataset_routes._refresh_context()
            return attr

        def _dataset_proxy(*args: object, **kwargs: object) -> object:
            dataset_routes._refresh_context()
            return attr(*args, **kwargs)

        _dataset_proxy.__name__ = name
        _dataset_proxy.__doc__ = getattr(attr, "__doc__", None)
        return _dataset_proxy
    raise AttributeError(name)



from thoth_terminal.web.app_sections import register_app_sections


register_app_sections(globals())
# Last registration makes this the outermost application middleware. It must
# protect native WebSockets as well as HTTP, before routing or DB dependencies.
app.add_middleware(PublicSurfaceBoundary, settings=settings)
