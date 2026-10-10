from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from thoth_terminal.db import utcnow
from thoth_terminal.institutional_service import ensure_source_profile
from thoth_terminal.models import Source, SourceState


ACQUISITION_RAIL_IDS = (7050, 7051, 7052, 7053, 7054, 7055, 7056)
ACQUISITION_REGISTRY_VERSION = "source-acquisition-rails-7050-7056-v1"


@dataclass(frozen=True)
class AcquisitionSurface:
    key: str
    label: str
    connector: str | None
    acquisition_mode: str
    source_family: str
    rights_class: str = "public"
    cadence_label: str = "event_driven"
    output_objects: tuple[str, ...] = ()
    required_config_keys: tuple[str, ...] = ()
    config_template: dict[str, Any] = field(default_factory=dict)
    provenance_fields: tuple[str, ...] = ()
    notes: str | None = None


@dataclass(frozen=True)
class AcquisitionRail:
    request_id: int
    rail_key: str
    title: str
    intent: str
    source_kind: str
    summary: str
    target_themes: tuple[str, ...]
    desired_outputs: tuple[str, ...]
    downstream_routes: tuple[str, ...]
    required_payload_fields: tuple[str, ...]
    surfaces: tuple[AcquisitionSurface, ...]


def _surface(
    *,
    key: str,
    label: str,
    connector: str | None,
    acquisition_mode: str,
    source_family: str,
    rights_class: str = "public",
    cadence_label: str = "event_driven",
    output_objects: tuple[str, ...] = (),
    required_config_keys: tuple[str, ...] = (),
    config_template: dict[str, Any] | None = None,
    provenance_fields: tuple[str, ...] = (),
    notes: str | None = None,
) -> AcquisitionSurface:
    template = dict(config_template or {})
    if connector:
        template.setdefault("connector", connector)
    template.setdefault("max_items_per_run", 25)
    return AcquisitionSurface(
        key=key,
        label=label,
        connector=connector,
        acquisition_mode=acquisition_mode,
        source_family=source_family,
        rights_class=rights_class,
        cadence_label=cadence_label,
        output_objects=output_objects,
        required_config_keys=required_config_keys,
        config_template=template,
        provenance_fields=provenance_fields,
        notes=notes,
    )


_REGISTRY: tuple[AcquisitionRail, ...] = (
    AcquisitionRail(
        request_id=7050,
        rail_key="source_pack_ir_artifact_rail",
        title="Issuer IR website, presentation, product-page, and official press/news acquisition",
        intent="source_acquisition_registry",
        source_kind="issuer_ir_artifact_acquisition",
        summary=(
            "Reusable acquisition registry for issuer-owned IR websites, presentations, product pages, "
            "and official press/news pages feeding source-pack gaps."
        ),
        target_themes=("issuer_ir", "official_press", "product_pages", "source_pack"),
        desired_outputs=(
            "IRArtifact",
            "Document",
            "ResearchSourcePack item",
            "CoverageGap provenance",
        ),
        downstream_routes=("research_source_pack", "decision_spine", "document_hydration_7051"),
        required_payload_fields=(
            "issuer_id",
            "company_id",
            "artifact_type",
            "canonical_url",
            "fetch_method",
            "status",
            "rights_class",
            "confidence",
        ),
        surfaces=(
            _surface(
                key="issuer_ir_web_list",
                label="Issuer IR/library pages",
                connector="web_list",
                acquisition_mode="connector",
                source_family="disclosure",
                output_objects=("Document", "IRArtifact"),
                required_config_keys=("list_url",),
                config_template={
                    "category": "company_ir",
                    "include_keywords": ["investor", "presentation", "results", "press", "product"],
                    "bootstrap_days": 365,
                    "max_items_per_run": 40,
                },
                provenance_fields=("list_url", "canonical_url", "published_at", "content_hash"),
            ),
            _surface(
                key="issuer_ir_browser",
                label="Challenge-prone issuer IR pages",
                connector="web_list_browser",
                acquisition_mode="browser_connector",
                source_family="disclosure",
                output_objects=("Document", "IRArtifact"),
                required_config_keys=("list_url",),
                config_template={
                    "category": "company_ir",
                    "include_keywords": [
                        "investor",
                        "presentation",
                        "webcast",
                        "transcript",
                        "press",
                    ],
                    "max_items_per_run": 20,
                },
                provenance_fields=("list_url", "resolved_url", "fetch_method", "robots_state"),
            ),
        ),
    ),
    AcquisitionRail(
        request_id=7051,
        rail_key="document_fetch_hydration_source_provenance",
        title="Rights-aware web, PDF, and artifact hydration provenance",
        intent="source_acquisition_registry",
        source_kind="rights_aware_hydration_provenance",
        summary=(
            "Bounded provenance contract for URL/PDF hydration before OCR, VLM, translation, "
            "or signal extraction."
        ),
        target_themes=("document_fetch", "pdf_hydration", "rights", "provenance"),
        desired_outputs=(
            "Document.extra hydration state",
            "Attachment hydration state",
            "Local-agent media job provenance",
        ),
        downstream_routes=(
            "document_readability",
            "media_transcription",
            "local_agent_signal_extraction",
        ),
        required_payload_fields=(
            "canonical_url",
            "content_hash",
            "fetch_method",
            "fetch_status",
            "content_type",
            "rights_class",
            "robots_state",
            "hydration_status",
            "failure_reason",
        ),
        surfaces=(
            _surface(
                key="document_fetch_service",
                label="Service-owned URL/PDF fetch and text extraction",
                connector=None,
                acquisition_mode="service",
                source_family="hydration",
                output_objects=("Document", "Attachment"),
                config_template={
                    "service": "connectors.document_fetch.build_fetched_document_payload"
                },
                provenance_fields=(
                    "request_url",
                    "final_url",
                    "resolved_content_type",
                    "pdf_extracted",
                    "content_hash",
                    "fetch_status",
                ),
            ),
            _surface(
                key="browser_hydration",
                label="Rights-aware browser hydration fallback",
                connector="web_list_browser",
                acquisition_mode="browser_connector",
                source_family="hydration",
                output_objects=("Document", "Attachment"),
                required_config_keys=("list_url",),
                config_template={"max_items_per_run": 10, "capture_hydration_provenance": True},
                provenance_fields=(
                    "paywall_state",
                    "robots_state",
                    "fetch_method",
                    "failure_reason",
                ),
            ),
        ),
    ),
    AcquisitionRail(
        request_id=7052,
        rail_key="clinical_regulatory_source_ingestion_feeder_to_6914",
        title="FDA, ClinicalTrials.gov, EMA, and healthcare regulatory official ingestion",
        intent="source_acquisition_registry",
        source_kind="healthcare_regulatory_official_ingestion",
        summary=(
            "Official healthcare-regulatory source templates for trials, approvals, labels, "
            "safety notices, advisory records, and EMA procedure/EPAR records."
        ),
        target_themes=("clinical_trials", "fda", "ema", "official_healthcare"),
        desired_outputs=("Document", "StructuredDataFact", "SourceProfile"),
        downstream_routes=("healthcare_metric_6914", "source_conflict_6908"),
        required_payload_fields=(
            "source_system",
            "regulator",
            "record_kind",
            "sponsor_or_applicant",
            "official_id",
            "canonical_url",
            "status",
            "as_of_date",
        ),
        surfaces=(
            _surface(
                key="clinicaltrials_gov_records",
                label="ClinicalTrials.gov records and history",
                connector="web_list",
                acquisition_mode="manual_template",
                source_family="regulatory",
                output_objects=("Document", "StructuredDataFact"),
                required_config_keys=("list_url",),
                config_template={
                    "source_system": "ClinicalTrials.gov",
                    "record_kind": "clinical_trial",
                    "include_keywords": ["trial", "study", "sponsor", "phase", "status"],
                    "max_items_per_run": 30,
                },
                provenance_fields=("nct_id", "sponsor", "status", "as_of_date", "canonical_url"),
            ),
            _surface(
                key="fda_official_records",
                label="FDA approval, label, safety, and advisory records",
                connector="web_list",
                acquisition_mode="manual_template",
                source_family="regulatory",
                output_objects=("Document", "StructuredDataFact"),
                required_config_keys=("list_url",),
                config_template={
                    "source_system": "FDA",
                    "record_kind": "fda_regulatory_record",
                    "include_keywords": ["approval", "label", "safety", "advisory", "device"],
                    "max_items_per_run": 30,
                },
                provenance_fields=(
                    "application_number",
                    "product",
                    "sponsor",
                    "as_of_date",
                    "canonical_url",
                ),
            ),
            _surface(
                key="ema_official_records",
                label="EMA EPAR, CHMP, and procedure records",
                connector="web_list",
                acquisition_mode="manual_template",
                source_family="regulatory",
                output_objects=("Document", "StructuredDataFact"),
                required_config_keys=("list_url",),
                config_template={
                    "source_system": "EMA",
                    "record_kind": "ema_regulatory_record",
                    "include_keywords": ["EPAR", "CHMP", "procedure", "medicine", "opinion"],
                    "max_items_per_run": 30,
                },
                provenance_fields=(
                    "procedure_number",
                    "medicine",
                    "applicant",
                    "as_of_date",
                    "canonical_url",
                ),
            ),
        ),
    ),
    AcquisitionRail(
        request_id=7053,
        rail_key="technical_source_ingestion_feeder_to_BOM_and_6925",
        title="Patent technical-decomposition, assignee, and source ingestion",
        intent="source_acquisition_registry",
        source_kind="patent_technical_decomposition_ingestion",
        summary=(
            "Patent ingestion registry for assignee-resolved patent families, abstracts/claims, "
            "component/process tags, product/BOM links, and legal-event relevance."
        ),
        target_themes=("patents", "technical_decomposition", "bom", "legal_events"),
        desired_outputs=("Document", "StructuredDataFact", "technical decomposition payload"),
        downstream_routes=("bom_component_mapping", "patent_dispute_6925", "source_conflict_6908"),
        required_payload_fields=(
            "patent_id",
            "assignee",
            "publication_date",
            "abstract_or_claim_excerpt",
            "component_tags",
            "process_tags",
            "bottleneck_relevance",
            "adoption_caveat",
        ),
        surfaces=(
            _surface(
                key="patentsview_search",
                label="PatentsView search records",
                connector="patentsview_search",
                acquisition_mode="connector",
                source_family="technical",
                output_objects=("Document", "StructuredDataFact"),
                required_config_keys=("query",),
                config_template={
                    "fields": [
                        "patent_id",
                        "patent_title",
                        "patent_date",
                        "assignee_organization",
                        "patent_abstract",
                    ],
                    "max_items_per_run": 50,
                },
                provenance_fields=(
                    "patent_id",
                    "assignee_organization",
                    "patent_date",
                    "request_body",
                ),
                notes="Patent existence is technical evidence only; commercial adoption needs separate sourcing.",
            ),
        ),
    ),
    AcquisitionRail(
        request_id=7054,
        rail_key="ir_artifact_transcript_source_pack",
        title="Official transcript and earnings call-material acquisition",
        intent="source_acquisition_registry",
        source_kind="official_transcript_call_material_acquisition",
        summary=(
            "Official earnings release, presentation, webcast/audio, transcript, fallback-provider, "
            "watch-state, rights, language, and exhaustion registry for earnings events."
        ),
        target_themes=("transcripts", "earnings_call_material", "ir_artifacts"),
        desired_outputs=("IRArtifact", "EarningsEvent link", "TranscriptWatchState", "Document"),
        downstream_routes=(
            "transcripts",
            "transcript_watchlist",
            "document_hydration_7051",
            "company_metrics_6892",
        ),
        required_payload_fields=(
            "earnings_event_id",
            "issuer_id",
            "fiscal_year",
            "fiscal_quarter",
            "official_material_present",
            "transcript_source_kind",
            "artifact_ids",
            "rights_class",
            "language",
            "exhaustion_reason",
        ),
        surfaces=(
            _surface(
                key="issuer_call_material_web_list",
                label="Issuer call-material pages after official material gate",
                connector="web_list",
                acquisition_mode="connector",
                source_family="disclosure",
                output_objects=("IRArtifact", "Document"),
                required_config_keys=("list_url",),
                config_template={
                    "category": "company_ir",
                    "include_keywords": [
                        "results",
                        "earnings",
                        "presentation",
                        "webcast",
                        "transcript",
                    ],
                    "max_items_per_run": 25,
                },
                provenance_fields=(
                    "earnings_event_id",
                    "list_url",
                    "canonical_url",
                    "artifact_type",
                ),
            ),
            _surface(
                key="taiwan_mops_investor_conferences",
                label="Taiwan MOPS investor conference materials",
                connector="taiwan_mops_investor_conferences",
                acquisition_mode="connector",
                source_family="disclosure",
                cadence_label="event_driven",
                output_objects=("IRArtifact", "Document"),
                config_template={"max_items_per_run": 40},
                provenance_fields=("company_code", "event_date", "material_url", "connector"),
            ),
            _surface(
                key="transcript_lookup_service",
                label="Transcript lookup and watchlist services",
                connector=None,
                acquisition_mode="service",
                source_family="transcript",
                output_objects=("TranscriptWatchState", "IRArtifact", "Document"),
                config_template={"service": "transcripts.get_transcript + transcript_watchlist"},
                provenance_fields=(
                    "watch_state",
                    "fallback_provider",
                    "attempt_count",
                    "exhaustion_reason",
                ),
            ),
        ),
    ),
    AcquisitionRail(
        request_id=7055,
        rail_key="trade_customs_source_ingestion_feeder_to_6907_7045",
        title="Non-USITC official customs and trade-source registration/backfill",
        intent="source_acquisition_registry",
        source_kind="non_usitc_trade_source_registration_backfill",
        summary=(
            "Bounded official customs source registration/backfill templates for UN Comtrade, "
            "US Census, Taiwan, Korea, Japan, and Eurostat Comext."
        ),
        target_themes=("trade_customs", "hs_codes", "mirror_checks", "backfills"),
        desired_outputs=("DataSeries", "DataPoint", "trade source health", "mirror-check payload"),
        downstream_routes=(
            "trade_flow_6907",
            "shipment_events_7045",
            "route_exposure_7047",
            "source_conflict_6908",
        ),
        required_payload_fields=(
            "connector",
            "source_system",
            "reporter_iso",
            "partner_scope",
            "flow",
            "hs_codes",
            "backfill_state",
            "coverage_start",
            "coverage_end",
            "health_status",
            "error_class",
        ),
        surfaces=(
            _surface(
                key="trade_un_comtrade",
                label="UN Comtrade monthly HS flows",
                connector="trade_un_comtrade",
                acquisition_mode="connector",
                source_family="trade",
                cadence_label="monthly",
                output_objects=("DataSeries", "DataPoint"),
                required_config_keys=("hs_codes", "reporter_iso"),
                config_template={
                    "lookback_periods": 24,
                    "allow_public_preview": True,
                    "max_requests_per_run": 80,
                },
                provenance_fields=(
                    "reporter_iso",
                    "partner_iso",
                    "flow",
                    "period",
                    "hs_codes",
                    "api_url",
                ),
            ),
            _surface(
                key="trade_us_census",
                label="US Census official trade series",
                connector="trade_us_census",
                acquisition_mode="connector",
                source_family="trade",
                cadence_label="monthly",
                output_objects=("DataSeries", "DataPoint"),
                required_config_keys=("hs_codes",),
                config_template={"lookback_periods": 24, "max_requests_per_run": 80},
                provenance_fields=("reporter_iso", "partner_iso", "flow", "period", "hs_codes"),
            ),
            _surface(
                key="trade_taiwan_customs",
                label="Taiwan official customs backfill",
                connector="trade_taiwan_customs",
                acquisition_mode="connector",
                source_family="trade",
                cadence_label="monthly",
                output_objects=("DataSeries", "DataPoint"),
                required_config_keys=("hs_codes",),
                config_template={"lookback_periods": 24, "max_requests_per_run": 80},
                provenance_fields=("reporter_iso", "partner_scope", "flow", "period", "hs_codes"),
            ),
            _surface(
                key="trade_korea_customs",
                label="Korea official customs backfill",
                connector="trade_korea_customs",
                acquisition_mode="connector",
                source_family="trade",
                cadence_label="monthly",
                output_objects=("DataSeries", "DataPoint"),
                required_config_keys=("hs_codes",),
                config_template={"lookback_periods": 24, "max_requests_per_run": 80},
                provenance_fields=("reporter_iso", "partner_scope", "flow", "period", "hs_codes"),
            ),
            _surface(
                key="trade_japan_trade_stats",
                label="Japan trade statistics backfill",
                connector="trade_japan_trade_stats",
                acquisition_mode="connector",
                source_family="trade",
                cadence_label="monthly",
                output_objects=("DataSeries", "DataPoint"),
                required_config_keys=("hs_codes",),
                config_template={"lookback_periods": 24, "max_requests_per_run": 80},
                provenance_fields=("reporter_iso", "partner_scope", "flow", "period", "hs_codes"),
            ),
            _surface(
                key="trade_eurostat_comext",
                label="Eurostat Comext official trade backfill",
                connector="trade_eurostat_comext",
                acquisition_mode="connector",
                source_family="trade",
                cadence_label="monthly",
                output_objects=("DataSeries", "DataPoint"),
                required_config_keys=("hs_codes",),
                config_template={"lookback_periods": 24, "max_requests_per_run": 80},
                provenance_fields=("reporter_iso", "partner_scope", "flow", "period", "hs_codes"),
            ),
        ),
    ),
    AcquisitionRail(
        request_id=7056,
        rail_key="commodity_source_ingestion_feeder_to_6916",
        title="Commodity exchange warehouse stock, visible inventory, curve, and source-ingestion rails",
        intent="source_acquisition_registry",
        source_kind="missing_exchange_inventory_source_ingestion_rail",
        summary=(
            "Template-only source registry for LME, SHFE, COMEX/CME, ICE, USGS, and EIA-style "
            "commodity warehouse, visible inventory, stock-flow, and curve source surfaces."
        ),
        target_themes=(
            "commodity_inventory",
            "warehouse_stocks",
            "futures_curves",
            "official_commodity_statistics",
        ),
        desired_outputs=("DataSeries", "DataPoint", "Document", "commodity source health"),
        downstream_routes=(
            "commodity_metric_6916",
            "trade_flow_6907",
            "source_acquisition_7055",
            "route_exposure_7047",
            "source_conflict_6908",
        ),
        required_payload_fields=(
            "source_system",
            "commodity",
            "product_grade",
            "warehouse_location",
            "inventory_state",
            "stock_quantity",
            "flow_quantity",
            "curve_tenor",
            "price",
            "unit",
            "period",
            "as_of_date",
            "revision",
            "canonical_url",
            "rights_class",
            "health_status",
        ),
        surfaces=(
            _surface(
                key="commodity_lme_warehouse_stocks",
                label="LME warehouse stock, warrant, and cancelled-warrant reports",
                connector="commodity_lme_warehouse_stocks",
                acquisition_mode="manual_template",
                source_family="commodity_inventory",
                rights_class="exchange_terms",
                cadence_label="daily",
                output_objects=("DataSeries", "DataPoint", "Document"),
                required_config_keys=("commodity_codes", "warehouse_regions"),
                config_template={
                    "source_system": "LME",
                    "record_kinds": ["warehouse_stock", "on_warrant", "cancelled_warrant"],
                    "inventory_states": [
                        "on_warrant",
                        "cancelled_warrant",
                        "off_warrant_if_available",
                    ],
                    "max_items_per_run": 40,
                },
                provenance_fields=(
                    "report_date",
                    "commodity",
                    "warehouse_location",
                    "inventory_state",
                    "unit",
                    "canonical_url",
                    "revision",
                ),
                notes="Exchange data rights and redistribution limits must be checked before enabling live collection.",
            ),
            _surface(
                key="commodity_shfe_warehouse_stocks",
                label="SHFE warehouse stock and warrant inventory reports",
                connector="commodity_shfe_warehouse_stocks",
                acquisition_mode="manual_template",
                source_family="commodity_inventory",
                rights_class="exchange_terms",
                cadence_label="weekly",
                output_objects=("DataSeries", "DataPoint", "Document"),
                required_config_keys=("commodity_codes", "warehouse_regions"),
                config_template={
                    "source_system": "SHFE",
                    "record_kinds": ["warehouse_stock", "warrant_inventory"],
                    "inventory_states": [
                        "warehouse_stock",
                        "registered_warrant",
                        "cancelled_or_deliverable_if_reported",
                    ],
                    "language": "zh",
                    "max_items_per_run": 40,
                },
                provenance_fields=(
                    "report_date",
                    "commodity",
                    "warehouse_location",
                    "inventory_state",
                    "unit",
                    "canonical_url",
                    "language",
                ),
                notes="Template preserves original-language provenance; translation should route through the document hydration/provenance rail.",
            ),
            _surface(
                key="commodity_cme_comex_inventory_curve",
                label="CME/COMEX metals inventory, delivery, and futures curve surfaces",
                connector="commodity_cme_comex_inventory_curve",
                acquisition_mode="manual_template",
                source_family="commodity_curve",
                rights_class="exchange_terms",
                cadence_label="daily",
                output_objects=("DataSeries", "DataPoint", "Document"),
                required_config_keys=("contract_symbols", "commodity_codes"),
                config_template={
                    "source_system": "CME/COMEX",
                    "record_kinds": [
                        "eligible_registered_stock",
                        "delivery_notice",
                        "settlement_curve",
                    ],
                    "curve_fields": [
                        "contract_month",
                        "settlement_price",
                        "open_interest_if_available",
                    ],
                    "max_items_per_run": 60,
                },
                provenance_fields=(
                    "contract_symbol",
                    "contract_month",
                    "warehouse_location",
                    "report_date",
                    "settlement_date",
                    "canonical_url",
                ),
                notes="Curve and warehouse facts should remain source-attributed; do not infer tightness without 6916 model context.",
            ),
            _surface(
                key="commodity_ice_curve_settlements",
                label="ICE energy and agricultural futures settlement curves",
                connector="commodity_ice_curve_settlements",
                acquisition_mode="manual_template",
                source_family="commodity_curve",
                rights_class="exchange_terms",
                cadence_label="daily",
                output_objects=("DataSeries", "DataPoint", "Document"),
                required_config_keys=("contract_symbols",),
                config_template={
                    "source_system": "ICE",
                    "record_kinds": [
                        "settlement_curve",
                        "open_interest_if_available",
                        "expiry_calendar",
                    ],
                    "curve_fields": ["contract_month", "settlement_price", "spread_to_front_month"],
                    "max_items_per_run": 60,
                },
                provenance_fields=(
                    "contract_symbol",
                    "contract_month",
                    "settlement_date",
                    "price_unit",
                    "canonical_url",
                    "rights_class",
                ),
                notes="Template only; any paid or restricted settlement feed needs explicit operator approval before enabling.",
            ),
            _surface(
                key="commodity_usgs_mineral_statistics",
                label="USGS mineral commodity production, reserves, and apparent-consumption tables",
                connector="commodity_usgs_mineral_statistics",
                acquisition_mode="manual_template",
                source_family="official_statistics",
                cadence_label="monthly",
                output_objects=("DataSeries", "DataPoint", "Document"),
                required_config_keys=("commodity_codes",),
                config_template={
                    "source_system": "USGS",
                    "record_kinds": [
                        "mineral_commodity_summary",
                        "monthly_mineral_industry_survey",
                        "country_table",
                    ],
                    "fields": [
                        "production",
                        "shipments",
                        "stocks",
                        "reserves",
                        "apparent_consumption",
                    ],
                    "max_items_per_run": 50,
                },
                provenance_fields=(
                    "publication",
                    "table_id",
                    "commodity",
                    "country_iso",
                    "period",
                    "as_of_date",
                    "canonical_url",
                    "revision",
                ),
            ),
            _surface(
                key="commodity_eia_inventory_series",
                label="EIA petroleum, gas, power-fuel stock, and energy inventory series",
                connector="commodity_eia_inventory_series",
                acquisition_mode="manual_template",
                source_family="energy_inventory",
                cadence_label="weekly",
                output_objects=("DataSeries", "DataPoint", "Document"),
                required_config_keys=("series_ids",),
                config_template={
                    "source_system": "EIA",
                    "record_kinds": [
                        "weekly_petroleum_status",
                        "natural_gas_storage",
                        "steo_inventory",
                        "electric_power_fuel_stocks",
                    ],
                    "fields": ["stock_quantity", "flow_quantity", "price_if_reported", "revision"],
                    "max_items_per_run": 80,
                },
                provenance_fields=(
                    "series_id",
                    "release_id",
                    "period",
                    "as_of_date",
                    "release_date",
                    "api_url",
                    "revision",
                ),
            ),
        ),
    ),
)

_RAILS_BY_ID = {rail.request_id: rail for rail in _REGISTRY}
_RAILS_BY_KEY = {rail.rail_key: rail for rail in _REGISTRY}


def _serialize_surface(surface: AcquisitionSurface) -> dict[str, Any]:
    return {
        "key": surface.key,
        "label": surface.label,
        "connector": surface.connector,
        "acquisition_mode": surface.acquisition_mode,
        "source_family": surface.source_family,
        "rights_class": surface.rights_class,
        "cadence_label": surface.cadence_label,
        "output_objects": list(surface.output_objects),
        "required_config_keys": list(surface.required_config_keys),
        "config_template": dict(surface.config_template),
        "provenance_fields": list(surface.provenance_fields),
        "notes": surface.notes,
    }


def serialize_acquisition_rail(
    rail: AcquisitionRail, *, include_surfaces: bool = True
) -> dict[str, Any]:
    payload = {
        "request_id": rail.request_id,
        "rail_key": rail.rail_key,
        "title": rail.title,
        "intent": rail.intent,
        "source_kind": rail.source_kind,
        "summary": rail.summary,
        "target_themes": list(rail.target_themes),
        "desired_outputs": list(rail.desired_outputs),
        "downstream_routes": list(rail.downstream_routes),
        "required_payload_fields": list(rail.required_payload_fields),
        "surface_count": len(rail.surfaces),
    }
    if include_surfaces:
        payload["surfaces"] = [_serialize_surface(surface) for surface in rail.surfaces]
    return payload


def list_source_acquisition_rails(
    *,
    rail_ids: list[int] | None = None,
    connector: str | None = None,
    source_family: str | None = None,
    include_surfaces: bool = True,
) -> list[dict[str, Any]]:
    rail_id_filter = {int(value) for value in (rail_ids or []) if str(value).isdigit()}
    connector_norm = str(connector or "").strip()
    family_norm = str(source_family or "").strip()
    out: list[dict[str, Any]] = []
    for rail in _REGISTRY:
        if rail_id_filter and rail.request_id not in rail_id_filter:
            continue
        surfaces = rail.surfaces
        if connector_norm:
            surfaces = tuple(surface for surface in surfaces if surface.connector == connector_norm)
        if family_norm:
            surfaces = tuple(
                surface for surface in surfaces if surface.source_family == family_norm
            )
        if connector_norm or family_norm:
            if not surfaces:
                continue
            rail = AcquisitionRail(
                request_id=rail.request_id,
                rail_key=rail.rail_key,
                title=rail.title,
                intent=rail.intent,
                source_kind=rail.source_kind,
                summary=rail.summary,
                target_themes=rail.target_themes,
                desired_outputs=rail.desired_outputs,
                downstream_routes=rail.downstream_routes,
                required_payload_fields=rail.required_payload_fields,
                surfaces=surfaces,
            )
        out.append(serialize_acquisition_rail(rail, include_surfaces=include_surfaces))
    return out


def get_source_acquisition_rail(identifier: int | str) -> dict[str, Any] | None:
    raw = str(identifier or "").strip()
    rail = _RAILS_BY_ID.get(int(raw)) if raw.isdigit() else _RAILS_BY_KEY.get(raw)
    return serialize_acquisition_rail(rail) if rail is not None else None


def instrument_request_payload_for_acquisition_rail(identifier: int | str) -> dict[str, Any]:
    payload = get_source_acquisition_rail(identifier)
    if payload is None:
        raise ValueError("Unknown source acquisition rail.")
    return {
        "title": payload["title"],
        "intent": payload["intent"],
        "summary": payload["summary"],
        "target_themes": payload["target_themes"],
        "desired_outputs": payload["desired_outputs"],
        "evidence_tier": "official_source_registry",
        "source_kind": payload["source_kind"],
        "draft_payload": {
            "acquisition_registry": payload,
            "registry_version": ACQUISITION_REGISTRY_VERSION,
        },
        "linked_objects": {
            "instrument_request_id": payload["request_id"],
            "rail_key": payload["rail_key"],
        },
        "request_key": f"instrument-request-rail:{payload['request_id']}:{payload['rail_key']}",
    }


def _poll_interval_for_cadence(cadence_label: str) -> int:
    cadence = str(cadence_label or "").strip().lower()
    if cadence == "monthly":
        return 30 * 24 * 60 * 60
    if cadence in {"daily", "twice_daily"}:
        return 12 * 60 * 60 if cadence == "twice_daily" else 24 * 60 * 60
    return 7 * 24 * 60 * 60


def _source_name(rail: AcquisitionRail, surface: AcquisitionSurface) -> str:
    return f"acquisition:{rail.request_id}:{surface.key}"[:255]


def ensure_source_acquisition_templates(
    session: Session,
    identifier: int | str,
    *,
    connectors: list[str] | None = None,
    enabled: bool = False,
) -> list[dict[str, Any]]:
    raw = str(identifier or "").strip()
    rail = _RAILS_BY_ID.get(int(raw)) if raw.isdigit() else _RAILS_BY_KEY.get(raw)
    if rail is None:
        raise ValueError("Unknown source acquisition rail.")
    connector_filter = {str(value).strip() for value in (connectors or []) if str(value).strip()}
    now = utcnow()
    out: list[dict[str, Any]] = []
    for surface in rail.surfaces:
        if not surface.connector:
            continue
        if (
            connector_filter
            and surface.connector not in connector_filter
            and surface.key not in connector_filter
        ):
            continue
        name = _source_name(rail, surface)
        source = session.query(Source).filter_by(name=name).first()
        cfg = dict(surface.config_template)
        cfg.update(
            {
                "connector": surface.connector,
                "acquisition_registry": {
                    "request_id": rail.request_id,
                    "rail_key": rail.rail_key,
                    "surface_key": surface.key,
                    "required_payload_fields": list(rail.required_payload_fields),
                    "provenance_fields": list(surface.provenance_fields),
                    "downstream_routes": list(rail.downstream_routes),
                },
                "enabled": bool(enabled),
            }
        )
        if not enabled:
            cfg.setdefault(
                "disabled_reason", "source acquisition template requires operator-specific config"
            )
        if source is None:
            source = Source(
                name=name,
                category=surface.source_family,
                priority=5,
                polling_interval_seconds=_poll_interval_for_cadence(surface.cadence_label),
                config=cfg,
                created_at=now,
            )
            session.add(source)
            session.flush()
        else:
            merged = dict(source.config or {})
            for key, value in cfg.items():
                if key == "enabled":
                    merged[key] = bool(enabled)
                elif key not in merged or key in {"acquisition_registry", "disabled_reason"}:
                    merged[key] = value
            if enabled:
                merged.pop("disabled_reason", None)
            source.config = merged
            if source.polling_interval_seconds is None:
                source.polling_interval_seconds = _poll_interval_for_cadence(surface.cadence_label)
            session.add(source)
            session.flush()
        if source.state is None:
            session.add(
                SourceState(
                    source_id=int(source.id),
                    last_run_at=None,
                    last_success_at=None,
                    next_run_at=None,
                    consecutive_failures=0,
                    backoff_until=None,
                    cursor={},
                    updated_at=now,
                )
            )
        profile = ensure_source_profile(
            session,
            source,
            source_family=surface.source_family,
            rights_class=surface.rights_class,
            acquisition_mode=surface.acquisition_mode,
            cadence_label=surface.cadence_label,
            review_required=surface.acquisition_mode in {"manual_template", "browser_connector"},
            subject_binding_kind="subject",
            notes=surface.notes,
        )
        out.append(
            {
                "source_id": int(source.id),
                "source_name": source.name,
                "connector": surface.connector,
                "surface_key": surface.key,
                "enabled": bool(source.config.get("enabled")),
                "required_config_keys": list(surface.required_config_keys),
                "profile": {
                    "source_family": profile.source_family,
                    "rights_class": profile.rights_class,
                    "acquisition_mode": profile.acquisition_mode,
                    "cadence_label": profile.cadence_label,
                    "review_required": profile.review_required,
                },
            }
        )
    session.flush()
    return out
