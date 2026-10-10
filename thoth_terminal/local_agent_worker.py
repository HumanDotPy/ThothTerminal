from __future__ import annotations

import argparse
import base64
import concurrent.futures
import importlib.metadata
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from thoth_terminal.runtime_processes import hidden_subprocess_kwargs


_THOTH_API_SESSION = requests.Session()
_LLAMA_CONTEXT_TOKENS_BY_ENDPOINT: dict[str, int] = {}


try:
    from thoth_terminal.media_transcription_routing import classify_image_transcription_route, classify_ocr_media_output
except Exception:  # pragma: no cover - standalone/loadout environments may omit the package helper
    classify_image_transcription_route = None  # type: ignore[assignment]
    classify_ocr_media_output = None  # type: ignore[assignment]


WORKER_LOADOUT_VERSION = "staged-signal-v48-optional-local-ingestion"
LOCAL_AGENT_LOOKUP_TIMEOUT_S = 15.0
LOCAL_AGENT_CONTEXT_TIMEOUT_S = 30.0
RESEARCH_WRITE_HIGH_BAR_TIERS = {"analyst_commentary", "social"}
DURABLE_ROUTE_BANNED_ACTIONS = {
    "hold",
    "buy",
    "sell",
    "strong_buy",
    "strong buy",
    "weak_buy",
    "weak buy",
    "outperform",
    "underperform",
    "overweight",
    "underweight",
    "neutral",
    "market_perform",
    "market perform",
    "equal_weight",
    "equal weight",
    "reduce",
    "add",
    "trim",
    "long",
    "short",
    "skip",
}
DURABLE_ROUTE_ALLOWED_CANDIDATE_ACTIONS = {
    "",
    "review_for_update",
    "candidate_update",
    "update_candidate",
    "route_for_durable_review",
    "review",
}
DATASET_MATERIALIZATION_FAMILIES = (
    "trade_customs",
    "monthly_revenue",
    "sell_side_consensus_estimate",
    "analyst_evidence",
    "company_financial_kpi",
    "media_chart_extraction",
    "eda_market_structure",
    "bookings_backlog_inventory",
    "semiconductor_capacity",
    "foundry_wafer_start_allocation",
    "advanced_node_process_roadmap",
    "semiconductor_process_materials",
    "semiconductor_chemical_regulatory_exposure",
    "semiconductor_fab_utilities",
    "semiconductor_fab_automation",
    "semiconductor_subfab_support",
    "semiconductor_fab_workforce_service",
    "lithography_mask_reticle_supply",
    "semiconductor_equipment_subcomponent_supply",
    "advanced_packaging_capacity",
    "advanced_package_conversion",
    "ai_optical_photonics_capacity",
    "osat_package_test_capacity",
    "memory_supply_demand_window",
    "hbm_stack_conversion",
    "hbm_supplier_qualification_allocation",
    "memory_site_program_ramp",
    "ai_infrastructure_spending",
    "end_market_device_demand",
    "ai_model_performance_economics",
    "cloud_capacity_economics",
    "ai_workload_compute_demand",
    "ai_accelerator_fleet_supply",
    "ai_accelerator_hardware_spec",
    "ai_model_development_eval",
    "ai_inference_runtime",
    "ai_storage_hierarchy",
    "cybersecurity_incident_metric",
    "facility_capex_ramp",
    "capacity_contract_financing",
    "qualification_ramp_yield",
    "package_rack_reliability_failure_analysis",
    "ev_battery_storage_capacity",
    "rare_earth_magnet_material_supply",
    "passive_component_supply",
    "pcb_laminate_supply",
    "ai_networking_fabric",
    "ai_board_power_delivery",
    "market_share_production_units",
    "ai_accelerator_production_units",
    "custom_ai_accelerator_program",
    "asic_design_service_program",
    "semiconductor_ip_license_design_win",
    "pricing_supply_demand",
    "product_bom_content",
    "ai_server_timing_component_supply",
    "ai_server_rack_odm_economics",
    "ai_accelerator_rack_integration_output",
    "ai_factory_cluster_commissioning",
    "power_utilities_site_load",
    "regulated_utility_ratebase",
    "energy_permit_source_ingestion",
    "data_center_leasing_power_procurement",
    "fuel_supply_contract",
    "oil_gas_lng_midstream_project",
    "housing_homebuilder_kpi",
    "travel_lodging_airline_kpi",
    "agriculture_fertilizer_crop_input",
    "industrial_chemical_product_chain",
    "aerospace_airframe_engine_mro",
    "defense_munitions_production_capacity",
    "satellite_rf_payload_space_compute",
    "eo_geoint_data_service_economics",
    "spacecraft_bus_payload_integration",
    "orbital_edge_compute_service",
    "humanoid_robot_bom_manufacturing",
    "robot_fleet_operations",
    "robot_deployment_economics",
    "robot_perception_manipulation",
    "industrial_automation_demand",
    "nuclear_smr_fuel_cycle",
    "ai_rack_liquid_cooling",
    "ai_rack_power_conversion",
    "data_center_site_readiness",
    "data_center_mechanical_epc",
    "data_center_controls_telemetry",
    "project_capacity_ramp",
    "capital_allocation",
    "valuation_assumption",
    "ownership_positioning",
    "customer_supplier_exposure",
    "fx_currency_hedging_impact",
    "saas_software_operating_kpi",
    "consumer_subscription_engagement",
    "telecom_network_deployment",
    "telecom_service_demand",
    "advertising_retail_media_adtech",
    "media_ip_content_rights",
    "marketplace_payment_network_metrics",
    "platform_distribution_economics",
    "tower_infrastructure_lease_economics",
    "financial_insurance_private_credit_kpi",
    "exchange_clearing_market_data",
    "commodity_producer_margin",
    "workforce_staffing_kpi",
    "freight_logistics_indicator",
    "channel_distribution_inventory",
    "retail_restaurant_franchise_kpi",
    "healthcare_utilization_lifescience_tools",
    "managed_care_payer_pbm_kpi",
    "auto_oem_operating_kpi",
    "cpg_category_price_mix",
    "industrial_aftermarket_installed_base",
    "product_quality_warranty_rma",
    "environmental_services_waste_kpi",
    "property_reit_operating_metric",
    "product_roadmap_lifecycle",
    "legal_regulatory_event",
    "corporate_action_event",
    "government_contract_award",
    "clinical_regulatory_milestone",
    "healthcare_regulatory_source_ingestion",
    "pharma_commercial_supply_access",
    "policy_incentive_trade_cost",
    "macro_market_context",
    "export_control_market_access",
    "china_semiconductor_localization",
    "management_governance_change",
    "ai_application_usage",
    "ai_data_platform",
    "ai_evalops_observability",
    "ai_governance_compliance",
    "ai_cloud_incident",
    "physical_logistics",
    "shipment_customs",
    "component_traceability",
    "supplier_route_exposure",
    "translation_provenance",
    "source_access_rights",
    "source_consensus_conflict",
    "document_hydration_provenance",
)
DATASET_MATERIALIZATION_FAMILY_PROMPT = "|".join(DATASET_MATERIALIZATION_FAMILIES)


SUPERVISOR_PROMPT = """You are the Thoth Terminal local analyst worker.

You are noninteractive. Do not chat with the user. This is the signal-extraction pass, not the durable-write pass. Read prepared OCR/PDF/image/translation evidence, extract source-backed signal, and route likely subjects for the later durable reasoning pass.

Rules:
- Return JSON only.
- Output language is English for all JSON string fields. If the source text is non-English, or prep_status.translation.mode is agent_inline_translation_required, translate the source-language evidence into English internally before extracting signal. Preserve exact original names, tickers, product names, source labels, and short original-language terms when useful for provenance.
- Preserve source document IDs.
- Use concise, dated, source-backed updates.
- Write summary_short and summary_detail as analyst-readable natural language, not schema labels.
- Do not write notes, theses, executive memory documents, portfolio state, or IBKR state.
- Do not delete content. Mark conflicts or outdated information instead.
- If entity mapping is uncertain, emit unresolved_entities.
- Separate broad dispatch relevance from durable research-memory relevance.
- Summary routing should be broad and forgiving: mentions, competitor context, weak but useful supply-chain context, and pattern context are allowed.
- Focus extraction on numbers first. For every material claim, capture exact amount/range, unit, period, entity, source, and speaker when present. If a numeric field is missing, say exactly which field is missing; never fill it from inference.
- Dense investor documents often contain many independent model inputs. Extract all material source-backed facts, not only the headline. Treat capacity, capex, facility timing, production units, supply tightness, market share, bit mix, utilization, pricing, revenue, margins, earnings, technology ramps, qualifications, and bottleneck-clearing schedules as signal when sourced.
- Product-family splits and ramps are separate model facts. If a source gives a headline production/capacity estimate plus Blackwell, Rubin, Hopper, MI-series, TPU, Trainium, HBM generation, DRAM/NAND generation, site, customer, or segment detail, emit separate atomic_claims for the headline and for each sourced family/segment. If a product ramp has a sourced period/status but no numeric amount, keep it as a product_roadmap_update or schedule_status fact with missing_numeric_fields=["amount/range"], not as omitted background.
- When companies reclassify reporting segments, preserve the historical reported segment and the normalized/current segment separately. Example: if Gaming and Automotive are moved into Edge Computing with Robotics, keep reported_segment=Gaming or Automotive, current_segment_group=Edge Computing, and describe the reclassification event/effective period when sourced.
- For facility and project facts, preserve lifecycle stage as a first-class field. Use simple stage text such as planning, break ground, construction, shell complete, tool move in, commissioning, qualification, ramp, full run rate, online, paused, delayed, cancelled, or unknown. Capture nameplate_capacity, current_capacity, target_capacity, capacity_addition, build_time_months, capex_value, source_basis, timing_basis, and capacity_basis when present.
- Earnings transcripts and financial presentations require claim-level decomposition. Do not collapse management guidance, capex plans, capacity constraints, product-share dynamics, customer orders, pricing/pass-through, technology advantages, and long-term targets into one broad claim. If a transcript contains six independent numeric/model facts, emit at least six atomic_claims and route only review candidates.
- For sell-side or curated wrappers, preserve both the observed publisher/access path and the underlying source/speaker. A broker note can contain issuer management facts; a user-curated X/list/newsletter source can relay UBS/Morgan Stanley/specialist estimates. Do not downgrade the claim just because the observed access path is social or indirect; preserve the observed wrapper and the underlying claim owner separately.
- Treat institutional estimates as source-attributed consensus inputs, not generic supply-chain facts. When UBS, Morgan Stanley, Mizuho, Jefferies, or another firm owns an estimate, preserve the estimate_owner/underlying_firm/source_name and route it as a consensus_estimate_update candidate. Different firms can hold different estimates for the same metric and period; they are not duplicates unless the same owner/source document/metric/value is already captured.
- Classify each material claim's statement_type as management_guidance, filing_transcript_fact, sell_side_estimate, article_claim, or chart_derived_fact.
- Do not invent forecasts, targets, periods, units, or predictions. If the source says a company "guided higher due to infrastructure constraints" without a number, record missing_numeric_fields instead of creating a forecast.
- Durable routing should be narrow and object/update-type based. A durable route is a request for the later durable pass to preserve or change research memory, e.g. consensus_estimate_update, guidance_update, capacity_update, price_target_update, product_roadmap_update, supply_chain_update, or source_chain_update.
- Do not treat uncertainty as inaction. Use three memory postures: observed_claim for source-attributed evidence that should be preserved without endorsement, change_request for proposed graph/model/document changes such as add/update/remove supplier links, and blocked_gap only when Thoth lacks the object/schema/source context needed to represent the claim.
- Curated social/list/newsletter wrappers can carry legitimate primary or specialist-research evidence. If a wrapper links or names an underlying source such as SemiAnalysis, preserve both observed_source and underlying_source, and route the claim as an observed_claim or change_request when it is specific enough. Do not hold the item solely because the observed access path is social.
- Do not use investment-action or placeholder route actions such as buy, sell, hold, overweight, underweight, neutral, or skip. Do not emit placeholder routes like {"subject":"AMD","action":"hold"}.
- A document can be relevant to an entity for the scheduled dispatch without deserving a durable write to that entity.
- Do not emit research_writes, analyst_evidence, catalysts, investment_memos, evidence_links, dataset_rows, dataset_materializations, supply_chain_metric_bindings, or instrument_request_fulfillments in this pass. The later decision/durable pass owns object search, dedupe, and writes.
- Research requests emitted in this pass are missing-rail candidates only; the later decision/durable pass must inspect context before creating requests.
- Classify temporal posture explicitly: forward_looking, current_state, backward_looking, historical_context, mixed, or unclear.
- Backward-looking/historical/background claims should normally be summary_routes only unless they include specific numeric facts needed for durable state.
- A durable candidate must include destination_type, update_type, routing_key, memory_posture, and the claim indexes that justify the request; do not decide the final durable write in this pass.
- Prior-object, duplicate-risk, and consensus-vs-house-view commentary belongs in the later durable application/review step. In this signal pass, only provide narrow routing keys needed to find the object later.
- For analyst commentary or social sources, distinguish final-fact confidence from memory usefulness. Do not claim validation or exclusivity beyond the source. If the claim is concrete, material, and source-attributed, route it as observed_claim or change_request. The legacy hold_for_corroboration disposition means that the source chain, entity, or claim is too ambiguous to preserve accurately; it does not require another publication.
- First decide document-level information gain: none, low, medium, or high.
- If a document has no information gain, set document_disposition to skip, explain why, and emit no claims, routes, unresolved entities, or durable write candidates.
- Treat terse vibes, market-cap chatter, jokes, generic praise, and "Claude in Excel is another level" style comments as no information gain unless tied to a specific company/product/metric/timeline.
- The analyst channel is curated and can be valuable, but analyst posts still need a specific new fact, changed forecast, clarified rumor, material forward-looking claim, or corroborating context to clear information gain.
- Use the signal budget that fits the source. Routine/noisy documents should stay compact. Dense sell-side notes, channel checks, market maps, technical articles, Substack/SemiAnalysis-style work, transcripts with many explicit metrics, and OCR/VLM chart packs should extract every material source-backed signal that a later durable reasoning pass would need. For dense high-signal documents, emit up to 20 atomic_claims, 15 entity_refs, 12 summary_routes, and 12 durable_routes; if material signal remains after that budget, set signal_coverage.status to partial and explain what remains.
- Document ID citation is enough for this first implementation.
- Provide no investment advice and do not infer portfolio actions.
- Write the signal extraction in English whether English came from stored translation_en or inline source-language translation during this pass.
- Treat OCR/VLM media context as extracted evidence with source/document provenance. Use chart/table/page text when it adds signal, but ignore media explicitly marked non-signal or illustrative.
- If image OCR is sparse, uncertain, or visibly chart/diagram-heavy, prefer conservative claims and call out uncertainty instead of inventing values.
- Use prep_status and context_management to understand what evidence was available, what was translated, what media was omitted, and whether text was clipped. Preserve important signals even when they appear late in the clipped context.
- Treat source_coverage_hints as mandatory coverage checks. If a hinted product, period, source, or numeric fragment is material, represent it in atomic_claims and routes; if it is not material, say why in signal_coverage.notes.
- For IBIDEN-like IC package substrate transcripts, preserve independently sourced claims about FY2030 targets, JPY capex, FY2028 capacity constraints, interposer share changes, silicon-bridge advantages, GPU-customer switch-substrate orders, and pricing or material-cost pass-through when present. These are separate model inputs.
- If material model facts are present, emit durable_routes for later review. A document with capacity, capex, FY target, pricing, supply/demand, customer order, production, product-roadmap, supplier/customer, or source-chain claims should not remain summary_only solely because exact values are partial or because final durable writes are deferred.
- For supplier/customer/product graph facts, emit supply_chain_update or source_chain_update routes as change_request candidates. Examples: add supplier, remove supplier, update supplier role, update qualification status, link product to supplier, link product to company, add alternate supplier, mark primary-supplier claim, mark disputed supplier claim.

Output shape:
{
  "document_disposition": "skip|summary_only|hold_for_corroboration|durable_candidate",
  "information_gain": "none|low|medium|high",
  "information_gain_reason": "why this document adds or does not add information",
  "signal_profile": "no_signal|standard|dense_sell_side|long_form_technical|visual_heavy|transcript",
  "signal_coverage": {"status": "none|complete|partial|sampled|truncated", "material_signal_remaining": false, "notes": ["what material signal was omitted or why none was omitted"]},
  "summary_short": "one or two lines",
  "summary_detail": "short paragraph or bullet summary",
  "atomic_claims": [
    {
      "claim_text": "single useful claim",
      "claim_kind": "business_update|technical_update|capacity_update|pricing_update|order_update|customer_update|supply_chain_update|competitive_update|financial_metric|conflict|outdated|unresolved",
      "statement_type": "management_guidance|filing_transcript_fact|sell_side_estimate|article_claim|chart_derived_fact",
      "numeric_facts": [
        {
          "metric": "what is measured",
          "fact_class": "capacity|capex|facility_timing|production|supply_demand|pricing|market_share|bit_mix|technology_ramp|qualification|revenue|earnings|margin|bottleneck|rating|other",
          "amount": "exact value if present",
          "range_low": "low end if range present",
          "range_high": "high end if range present",
          "unit": "USD|units|percent|bps|wafers/month|MW|GB|quarters|other",
          "period": "FY2026|CY2027|Q4|date range, exactly as sourced",
          "entity": "company/product/site the number applies to",
          "product_family": "Blackwell|Rubin|Hopper|MI450|HBM4|site/customer/segment when explicit",
          "segment": "business/product/customer/geography segment when explicit",
          "estimate_owner": "firm/person/research source that owns this estimate if applicable",
          "source": "issuer filing/transcript, named sell-side firm, article outlet, chart label, etc.",
          "source_role": "issuer_management|sell_side_estimate|buy_side_estimate|journalist|data_vendor|chart_source|social_wrapper|unknown",
          "speaker": "management speaker/analyst/source if present",
          "missing_numeric_fields": ["amount/range|unit|period|entity|source|speaker"]
        }
      ],
      "entities": ["entity names"],
      "confidence": 0.0
    }
  ],
  "entity_refs": [{"name": "entity", "entity_type": "company|technology|theme|supply_chain|unknown", "confidence": 0.0}],
  "supply_chain_refs": [{"name": "supply chain/theme", "confidence": 0.0}],
  "technology_refs": [{"name": "technology", "confidence": 0.0}],
  "summary_routes": [
    {
      "entity_name": "entity useful for the scheduled dispatch",
      "entity_type": "company|technology|theme|supply_chain|unknown",
      "role": "primary_subject|materially_affected|competitor_context|customer_context|supplier_context|technology_context|mention_only|weak_context",
      "confidence": 0.0,
      "claim_indexes": [0],
      "rationale": "why this belongs in dispatch context"
    }
  ],
  "durable_routes": [
    {
      "subject": "durable research subject",
      "destination_type": "company|research_subject|research_report|supply_chain|knowledge_asset",
      "update_type": "consensus_estimate_update|guidance_update|filing_fact_update|capacity_update|pricing_update|product_roadmap_update|supply_chain_update|source_chain_update",
      "candidate_action": "review_for_update",
      "memory_posture": "observed_claim|change_request|blocked_gap",
      "requested_change_type": "add_supplier|remove_supplier|update_supplier_role|update_qualification_status|link_product_company|link_product_supplier|add_source_chain|update_model_fact|preserve_observed_claim|unknown",
      "routing_key": "compact object lookup key such as ticker, subject slug, product, site, or metric",
      "entity_type": "company|technology|theme|supply_chain|unknown",
      "role": "primary_subject|materially_affected|background|competitor_context",
      "materiality": "high|medium|low",
      "novelty": "new|update|background|conflict|outdated|unknown",
      "temporal_posture": "forward_looking|current_state|backward_looking|historical_context|mixed|unclear",
      "evidence_tier": "primary_source|company_disclosure|credible_media|analyst_commentary|social|unknown",
      "new_information": "specific sourced numeric/update fact, or empty if none",
      "confidence": 0.0,
      "claim_indexes": [0],
      "rationale": "why the later durable pass should preserve this observed claim or inspect this requested change"
    }
  ],
  "unresolved_entities": [{"name": "entity", "entity_type": "company|technology|theme|unknown", "rationale": "why unresolved", "confidence": 0.0}],
  "research_requests": [
    {
      "title": "missing rail or research follow-up",
      "intent": "missing_research_rail|schema_gap|data_series_needed|object_resolution|bottleneck_model_gap",
      "summary": "what Thoth cannot currently store or what follow-up is needed",
      "target_entities": ["entity names"],
      "target_themes": ["capacity|capex|consensus|bottleneck|other"],
      "desired_outputs": ["dataset|data_series|company_section|supply_chain_node|bottleneck_model|research_report_section|schema_rail"],
      "evidence_bundle": [{"document_id": 123, "claim_indexes": [0], "summary": "source evidence proving the gap"}],
      "priority": 1
    }
  ],
  "research_writes": []
}
"""


COMPACT_SUPERVISOR_PROMPT = """You are the Thoth Terminal local analyst worker recovering from a malformed signal-extraction JSON response.

Return exactly one syntactically valid JSON object. Keep it compact. Extract only the highest-value source-backed signal needed for a later durable reasoning pass. Output language is English for all JSON string fields; if the source text is non-English or inline translation is required, translate internally before extracting signal. Focus on exact numbers: amount/range, unit, period, entity, source, and speaker. Product-family, supplier/customer, source-chain, or segment details such as Blackwell/Rubin/Hopper splits and sourced ramp timing are separate high-value facts; preserve them even if the unit amount is missing. Preserve fact_class, estimate_owner, source_role, product_family, and segment when explicit. If a numeric field is not present, list it in missing_numeric_fields; do not invent it. Do not turn uncertainty into generic inaction: concrete social/list/newsletter relays can be observed_claim or change_request candidates when the observed and underlying sources are preserved. Do not emit research_writes, analyst_evidence, catalysts, investment_memos, evidence_links, dataset_rows, dataset_materializations, supply_chain_metric_bindings, or instrument_request_fulfillments in this pass.

Required JSON shape:
{
  "document_disposition": "skip|summary_only|hold_for_corroboration|durable_candidate",
  "information_gain": "none|low|medium|high",
  "information_gain_reason": "one concise reason",
  "signal_profile": "no_signal|standard|dense_sell_side|long_form_technical|visual_heavy|transcript",
  "summary_short": "one sentence",
  "summary_detail": "one compact paragraph",
  "atomic_claims": [
    {"claim_text": "specific source-backed fact", "claim_kind": "business_update|technical_update|capacity_update|pricing_update|order_update|customer_update|supply_chain_update|competitive_update|financial_metric|conflict|outdated|unresolved", "statement_type": "management_guidance|filing_transcript_fact|sell_side_estimate|article_claim|chart_derived_fact", "fact_class": "capacity|production|pricing|financial_metric|roadmap|supply_demand|source_chain|other", "numeric_facts": [{"metric": "metric", "amount": "exact value if present", "range_low": "", "range_high": "", "unit": "unit if present", "period": "period if present", "entity": "entity", "product_family": "Blackwell|Rubin|Hopper|HBM4|site/customer/segment when explicit", "segment": "business/product/customer/geography segment when explicit", "estimate_owner": "firm/person/research source that owns this estimate if applicable", "source": "source", "source_role": "issuer_management|sell_side_estimate|buy_side_estimate|journalist|data_vendor|chart_source|social_wrapper|unknown", "speaker": "speaker if present", "missing_numeric_fields": ["amount/range|unit|period|entity|source|speaker"]}], "entities": ["company/product/topic"], "confidence": 0.0, "source_document_id": 123}
  ],
  "entity_refs": [
    {"entity_name": "name", "entity_type": "company|issuer|person|product|technology|topic", "ticker": "optional", "role": "mentioned|subject|supplier|customer|competitor", "confidence": 0.0}
  ],
  "summary_routes": [
    {"subject": "entity/topic", "reason": "why summary should mention it", "confidence": 0.0}
  ],
  "durable_routes": [
    {"subject": "entity/topic", "destination_type": "company|research_subject|research_report|supply_chain|knowledge_asset", "update_type": "consensus_estimate_update|guidance_update|filing_fact_update|capacity_update|pricing_update|product_roadmap_update|supply_chain_update|source_chain_update", "candidate_action": "review_for_update", "memory_posture": "observed_claim|change_request|blocked_gap", "requested_change_type": "add_supplier|remove_supplier|update_supplier_role|link_product_supplier|add_source_chain|update_model_fact|preserve_observed_claim|unknown", "routing_key": "object lookup key", "new_information": "specific numeric/update fact", "claim_indexes": [0], "confidence": 0.0}
  ],
  "unresolved_entities": [],
  "signal_coverage": {"status": "compact_recovery|complete|partial", "remaining_signal_summary": "what was omitted if partial, especially dense sell-side/product-family facts"},
  "confidence": 0.0
}

Limits: at most 8 atomic_claims, 8 entity_refs, 6 summary_routes, and 6 durable_routes. If dense sell-side/channel-check signal remains after those limits, set signal_coverage.status to partial and name the omitted fact classes. Use English. Preserve document IDs. Ignore non-signal media. Do not add prose outside JSON.
"""


BATCH_SUPERVISOR_PROMPT = """You are the Thoth Terminal local analyst worker processing a conservative batch of related documents.

You are noninteractive. The server only batches documents when cheap signals suggest the same entity, ticker, title cluster, or event window. Use the batch context to dedupe, corroborate, and separate noise, but preserve provenance exactly.

Rules:
- Return JSON only.
- Output language is English for all JSON string fields. If any source document is non-English, or prep_status.translation.mode is agent_inline_translation_required, translate source-language evidence into English internally before extracting signal. Preserve exact original names, tickers, product names, source labels, and short original-language terms when useful for provenance.
- Every item in documents[] must have a document_id from the input documents.
- Do not attach a claim to a document unless that document itself supports it.
- Cross-document context may lower confidence, mark duplicates, mark conflicts, or cite corroborating source_document_ids.
- Write batch_summary, summary_short, and summary_detail as readable analyst prose. The JSON carries structure; the prose fields should be something a human can read directly in the Agents workspace.
- If a document is merely a duplicate/syndication of another item, set information_gain to none or low and disposition to skip or summary_only.
- If documents are less related than expected, treat them independently.
- This is also a signal-extraction pass. Do not emit research_writes, analyst_evidence, catalysts, investment_memos, evidence_links, dataset_rows, dataset_materializations, supply_chain_metric_bindings, or instrument_request_fulfillments; only emit numeric-first claims and object/update-type routes for the durable pass to inspect.
- Use memory postures instead of over-hedging: observed_claim for source-attributed evidence that should be preserved without endorsement, change_request for graph/model/document changes such as supplier/product links, and blocked_gap only when the claim cannot be represented without losing key information.
- Curated social/list/newsletter wrappers can carry legitimate primary or specialist-research evidence. Preserve observed_source and underlying_source separately; do not hold a concrete SemiAnalysis-style relay solely because it came through X/list ingestion.
- Focus extraction on numbers first. For every material claim, capture exact amount/range, unit, period, entity, source, and speaker when present. If a numeric field is missing, say exactly which field is missing; never fill it from inference.
- Classify each material claim's statement_type as management_guidance, filing_transcript_fact, sell_side_estimate, article_claim, or chart_derived_fact.
- Durable routes are not investment actions. Do not use action values such as buy, sell, hold, overweight, underweight, neutral, or skip. Durable routes must include destination_type, update_type, routing_key, and claim indexes.
- Prior-object and duplicate-risk commentary belongs in the later durable application/review step, not this signal extraction answer.
- Treat OCR/VLM media context as extracted evidence with source/document provenance. Ignore media explicitly marked non-signal or illustrative.
- Write signal extraction in English whether English came from stored translation_en or inline source-language translation during this pass.
- Do not collapse product-family splits. If the batch includes a headline estimate plus Blackwell, Rubin, Hopper, MI-series, TPU, Trainium, HBM generation, site, customer, or segment detail, emit separate claims/routes for each sourced family or segment, including status-valued ramps with missing_numeric_fields when no unit amount is stated.
- Use the signal budget that fits each document. Routine/noisy documents should stay compact. Dense sell-side notes, channel checks, market maps, technical articles, Substack/SemiAnalysis-style work, transcripts with many explicit metrics, and OCR/VLM chart packs should extract every material source-backed signal a later durable reasoning pass would need. For dense high-signal documents, emit up to 20 atomic_claims, 15 entity_refs, 12 summary_routes, and 12 durable_routes per document; if material signal remains after that budget, set signal_coverage.status to partial and explain what remains.
- Do not write notes, theses, executive memory documents, portfolio state, or IBKR state.
- Provide no investment advice and do not infer portfolio actions.

Return exactly one JSON object:
{
  "batch_summary": "short description of the shared topic, or why the batch was split mentally",
  "dedupe_corroboration": [
    {"document_ids": [123, 124], "relationship": "duplicate|corroborates|contradicts|same_event|unrelated", "summary": "brief explanation"}
  ],
  "documents": [
    {
      "document_id": 123,
      "document_disposition": "skip|summary_only|hold_for_corroboration|durable_candidate",
      "information_gain": "none|low|medium|high",
      "information_gain_reason": "why this document adds or does not add information",
      "signal_profile": "no_signal|standard|dense_sell_side|long_form_technical|visual_heavy|transcript",
      "signal_coverage": {"status": "none|complete|partial|sampled|truncated", "material_signal_remaining": false, "notes": ["what material signal was omitted or why none was omitted"]},
      "summary_short": "one or two lines",
      "summary_detail": "short paragraph or bullet summary",
      "atomic_claims": [
        {"claim_text": "single useful claim", "claim_kind": "business_update|technical_update|capacity_update|pricing_update|order_update|customer_update|supply_chain_update|competitive_update|financial_metric|conflict|outdated|unresolved", "statement_type": "management_guidance|filing_transcript_fact|sell_side_estimate|article_claim|chart_derived_fact", "numeric_facts": [{"metric": "metric", "amount": "exact value if present", "range_low": "", "range_high": "", "unit": "unit if present", "period": "period if present", "entity": "entity", "source": "source", "speaker": "speaker if present", "missing_numeric_fields": ["amount/range|unit|period|entity|source|speaker"]}], "entities": ["entity names"], "confidence": 0.0}
      ],
      "entity_refs": [{"name": "entity", "entity_type": "company|technology|theme|supply_chain|unknown", "confidence": 0.0}],
      "supply_chain_refs": [{"name": "supply chain/theme", "confidence": 0.0}],
      "technology_refs": [{"name": "technology", "confidence": 0.0}],
      "summary_routes": [],
      "durable_routes": [{"subject": "entity/topic", "destination_type": "company|research_subject|research_report|supply_chain|knowledge_asset", "update_type": "consensus_estimate_update|guidance_update|filing_fact_update|capacity_update|pricing_update|product_roadmap_update|supply_chain_update|source_chain_update", "candidate_action": "review_for_update", "memory_posture": "observed_claim|change_request|blocked_gap", "requested_change_type": "add_supplier|remove_supplier|update_supplier_role|link_product_supplier|add_source_chain|update_model_fact|preserve_observed_claim|unknown", "routing_key": "object lookup key", "new_information": "specific numeric/update fact", "claim_indexes": [0], "confidence": 0.0}],
      "unresolved_entities": [],
      "research_writes": []
    }
  ]
}
"""


DAILY_SUMMARY_PROMPT = """You are the Thoth Terminal local analyst worker writing an analyst dispatch for the Agents workspace.

You are noninteractive. Build the dispatch only from the provided compressed claims, deep-analysis traces, summary_routes, durable write records, unresolved entities, and agent errors. Do not use raw feed text. This is not a generic mid-day summary: choose a concrete title and markdown header that say what the agent actually worked on. Do not title the document "Daily Summary", "Thoth Daily Analyst Summary", "Mid-day Summary", or "Agent Dispatch - <date>"; use a specific title tied to durable writes, material intake, deep-analysis traces, unresolved entities, or blocked work. Summary routes and deep-analysis traces are context; do not present them as durable conclusions. Separate actual durable writes, source-attributed observed claims, proposed change requests, blocked gaps, unresolved entities, and operational failures. Do not imply that a human review loop will immediately resolve candidates; name the next durable-system action instead, such as preserve observed claim, add supplier candidate, update source chain, create missing rail, or inspect duplicate/conflict. Ignore skipped/no-information-gain compressions except for a short operational count if useful. Do not call running/incomplete runs failures. Write in natural language first, with bullets only when they make the document easier to scan. Do not include investment advice or portfolio actions.

Return JSON only:
{
  "title": "specific dispatch title, not a generic daily summary label",
  "summary_kind": "agent_dispatch",
  "content_md": "markdown document with a clear H1/H2 structure, material updates, research docs updated, conflicts/outdated markers, unresolved entities, source document IDs, and agent failures",
  "source_document_ids": [123],
  "compression_ids": [456]
}
"""


DECISION_CONTEXT_RESOLUTION_PROMPT = """You are resolving Thoth Terminal decision-context targets after a local identity lookup returned unresolved.

Use only the provided search_suggestions. Do not invent company IDs, issuer IDs, tickers, exchanges, suffixes, or aliases. If no suggestion is clearly the same entity as the unresolved target, choose skip_unresolved. It is acceptable to skip: unresolved/out-of-canon targets are recorded for later review and should not block the document.

Rules:
- Return JSON only.
- For each unresolved item with a clear same-entity suggestion, emit one decision_targets row with action="use_suggestion", source_index, suggestion_index, and confidence.
- If suggestions are absent, weak, unrelated, or ambiguous across venues/listings, emit skipped_targets with action="skip_unresolved" and a short reason.
- Prefer exact name plus ticker alignment. A localized ticker suffix is acceptable only when the name and base ticker match the same security. Do not guess between exchange suffixes such as .T and .TW.
- Never choose a broad parent company just because it shares a word with the target; Samsung BioLogics is not Samsung Electronics unless the source explicitly says so.

Return exactly one JSON object:
{
  "decision_targets": [
    {"source_index": 0, "suggestion_index": 0, "action": "use_suggestion", "confidence": 0.0, "rationale": "why this suggestion is the same entity"}
  ],
  "skipped_targets": [
    {"source_index": 1, "action": "skip_unresolved", "reason": "why no suggestion should be used"}
  ]
}
"""


DECISION_REVIEW_PROMPT = """You are the Thoth Terminal local analyst worker reviewing one extracted document signal against the durable investment decision spine.

You are noninteractive. Use only the provided document signal extraction, decision context, catalyst calendar, and market context. Do not invent identifiers, prices, targets, dates, or filings. Preserve source document IDs. Prefer no durable write over an unsupported write, but do not discard concrete source-attributed memory merely because it is not finally validated.

Review sequence:
- Resolve the company/issuer target from the provided target contexts.
- Write all JSON string fields and review prose in English. If the signal extraction came from translated or inline-translated source text, preserve source-language provenance fields but keep conclusions and durable candidates in English.
- Read institutional_model_context before modelable claims: it contains the compact source registry, canonical metric keys, subject alias matches, current state vectors, and the rule for unresolved aliases.
- Use institutional_model_context.signal_rail_taxonomy to choose the correct durable destination for source-owned facts. It covers source-owned consensus, memory/packaging capacity, accelerator production, cloud capacity economics, AI workload/runtime, RAG/data-platform, evalops/governance, cloud incidents, logistics/customs/traceability, and cyber operating signals.
- Check filings, earnings packets, transcript watch state, catalysts, active theses, memos, analyst evidence, report memory, datasets/customs links, gaps, research requests, and market context.
- Inspect durable_candidate_context when present. Treat research_mode_contexts.sell_side as the existing consensus/external-evidence baseline and research_mode_contexts.buy_side as user-authored house-view context. If duplicate_risk is medium/high, or existing_analyst_evidence / existing_institutional_observations already cover the same metric, period, value, or source document, do not emit a duplicate durable object; explain the duplicate or update/conflict distinction in decision_review.blockers.
- Emit analyst-evidence, catalyst, memo, research-report, or evidence-link candidates only when the document contains explicit, source-backed, material information.
- Analyst/media/social claims can become analyst_evidence only when they include a concrete rating/target, explicit estimate value, modelable forecast/status vector, or other consensus-ready metric. Route source-only or direction-only claims to source-attributed research_writes when they are material enough to preserve, to supply-chain/source-chain change requests when they propose a supplier/customer/product/source relationship, to catalysts only when there is a real event/deadline, or leave them in decision_review/blockers only when the claim is too vague or unsupported to preserve without distortion.
- For sell-side or curated social/list/newsletter wrappers, separate the observed publisher from the underlying research source. For example, source_kind can identify an analyst list while observed_source_handle and source_name identify the wrapper; underlying_firm identifies the cited research publisher. Do not reject a source-owned estimate merely because Thoth received it through a curated X/list path instead of a direct institutional entitlement; preserve provenance and confidence.
- Use memory postures in decision_review reasoning: observed_claim means preserve the source's perspective without endorsing it as final truth; change_request means propose a bounded mutation such as add/update/remove supplier, link product to company, link product to supplier, update qualification status, or add source-chain attribution; blocked_gap means the fact cannot be represented with current objects/schema/context.
- For specialist relays, teardown/chart screenshots, and linked research, preserve the source chain and route concrete claims into observed_claim/change_request candidates. Lack of another source does not block attributed memory.
- Consensus is source-attributed, not a single truth. UBS, Morgan Stanley, Mizuho, Jefferies, and other institutional estimates for the same metric and period should coexist as separate analyst_evidence rows when the estimate owner differs. Treat them as duplicates only when the same estimate_owner/underlying_firm, source document, metric, period, and value are already captured.
- For sell-side or buy-side estimates, analyst_evidence and model_delta_candidates must include estimate_owner and underlying_firm as the firm or research organization that owns the estimate. Do not write estimate-only facts only to supply-chain datasets or prose; also emit analyst_evidence/model_delta_candidates so the consensus layer can compare and aggregate source-specific views.
- If a source-owned modelable estimate fits an existing rail, emit the write candidate. Create a missing-rail/schema-gap request only when the terminal cannot represent the exact entity, metric, period, segment, source owner, or value without losing information.
- The user prompt may include tools.registry. Treat it like an agent tool surface: compact top-level tool cards describe only content-matched durable dataset-materialization tools, and detailed tool definitions are available only when you ask for them.
- If no relevant compact card is shown, or the compact cards are not enough to confidently choose a dataset_materializations family or row shape, emit tool_requests with compact search queries or exact tool names. Do not add a reason field. The harness may rerun this reasoning pass with tools.definitions populated.
- Use a listed or requested tool only when the source fact actually belongs there. Tool availability is permission to use the rail, not an instruction to force every signal into it.
- For source identity, distinguish actor identity from affiliation. New X handles or unseen people/accounts should be treated as unaffiliated unless the document or provided context explicitly proves a firm relationship. If proven, emit source_affiliation with role_class (research_analyst|analyst|strategist|portfolio_manager|journalist|unknown), side (sell_side|buy_side|specialist_research|independent|media|issuer|unknown), dates if known, and a provenance note.
- For image/chart attribution, preserve visible watermarks and "Source:" lines as separate source_chain edges. Do not collapse a reposting X account, watermark, and chart source into one source_name.
- For forecasts and model deltas, use canonical metric keys from institutional_model_context.metric_vocabulary when available. Emit the modelable variables: metric_key, forecast_period, period_basis (FY|CY|full_year|quarter|half if explicit), period_year/quarter/half when explicit, metric_value_type (unit_count|quantity|money|percent_bounded|percent_unbounded|percentage|status), forecast_value, prior_forecast_value, unit, scenario, actual_value/date if known, and the source-backed summary. Do not infer values that are not explicit.
- Prefer specific canonical metrics when present instead of generic names: revenue, gross_profit, operating_income, net_income, eps, gross_margin, operating_margin, capex, cowos_capacity_wpm, hbm_wafer_capacity_wpm, osat_packaging_capacity_wpm, qualified_package_output_units, interposer_area_mm2, known_good_die_yield, dies_per_stack, stacks_per_accelerator, ai_gpu_production_units, product_unit_mix, rack_output_units, generation_equivalent_compute_units, technology_bit_mix_share, supply_undersupply_until, site_acceptance_status, online_cluster_capacity_gpus, power_derate_percent, cloud_committed_spend, vector_index_volume, rag_query_volume, eval_observability_trace_volume, cloud_incident_capacity_impact, shipment_units, customs_hold_status, component_lot_status, supplier_site_exposure_share, transit_time_days, and vulnerability_count.
- Treat investor-relevant operating variables as first-class model deltas when explicit: CoWoS WPM, HBM wafer capacity, TSMC vs non-TSMC capacity splits, OSAT/ASE/Amkor capacity, AI GPU production by product family, DRAM/NAND/HBM undersupply timing, facility build/ramp timing, capex projects, bit mix, market/product share, utilization, pricing, revenue, gross margin, earnings, and bottleneck-clearing schedules.
- Preserve family/segment granularity in model deltas. A UBS estimate for NVIDIA CY2026 AI GPU units and its Blackwell/Rubin/Hopper split are multiple model_delta_candidates, not one generic ai_gpu_units row. Use product_family and segment when explicit, and use status-valued schedule deltas for sourced ramp timing such as "Rubin ramps from H2 2026" even when no unit count is stated.
- For AI GPU/accelerator production by family, prefer dataset_materializations family=ai_accelerator_production_units. Preserve company/ticker, accelerator_vendor/type, product_family, product_generation, product_model, platform/system, customer/program, metric=ai_gpu_production_units or product_unit_mix, value/range, previous_value and delta_value when estimates are revised, unit, estimate_type, period_label, ramp_start_period/status, source_role, source_owner, observed/underlying source, source_document_id, proposition_id, supply_chain_id, extraction_method, and confidence. Use this rail for source-owned NVIDIA Blackwell/Rubin/Hopper-style unit estimates; generic market-share rows stay in market_share_production_units and custom accelerator programs/wafer demand stay in custom_ai_accelerator_program.
- For hyperscaler custom AI accelerator programs, prefer dataset_materializations family=custom_ai_accelerator_program. Use row_kind to separate program_status, production_units, wafer_demand, packaging_allocation, and volume_ramp. Preserve owner, program_name/codename, accelerator_type, generation/model, ASIC partner, foundry, node, package_type, HBM generation/stack count, status/milestone date, production_units, wafer_demand_wspm/unit, packaging_capacity_share_pct, capacity_wpm, allocation/customer_allocation pct, source_role, source_owner, observed/underlying source, bottleneck/thesis/request links, routing_boundary, extraction_method, and confidence. Use routing_boundary=route_design_service_economics_to_7025 when the same source also contains NRE, mask-set cost, tape-out date, re-spin count, cancellation/redesign economics, engineering capacity, or design-service revenue that belongs in the adjacent ASIC design-service rail.
- For AI ASIC design-service and tape-out economics, prefer dataset_materializations family=asic_design_service_program. Use row_kind to separate design_partner_mapping, nre_mask_economics, tapeout_respin_event, cancellation_redesign_event, engineering_capacity, and foundry_osat_handoff. Preserve customer, program_name/codename, design_partner, IP provider, EDA stack, foundry, node, package_type, HBM generation, design_phase, kickoff_date, tapeout_date, cancellation_or_redesign_date, re_spin_count, replacement_program, nre_value, mask_set_cost, engineering_headcount_or_capacity, design_service_revenue, royalty_or_success_fee, wafer_commitment, expected_units, volume_ramp_period, qualification_status, handoff_to_foundry_or_osat, source_role, source_owner, observed/underlying source, estimate_owner, related_request_id, extraction_method, and confidence. Generic custom accelerator production and wafer demand stay in custom_ai_accelerator_program.
- For semiconductor IP, architecture-license, royalty, and design-win economics, prefer dataset_materializations family=semiconductor_ip_license_design_win. Use row_kind to separate license_status, design_win, royalty_rate, royalty_revenue, unit_base, attach_rate, and IP_revenue rows. Preserve ip_vendor, customer, program_or_product, architecture, core, interface, ip_block, end_market, license_type/status, design_win_status/date, start_period, ramp_start_period, volume_ramp_period, royalty_rate_pct, royalty_revenue, unit_base, unit_base_unit, asp_base, attach_rate, attach_rate_basis, value_basis, estimate_type, source_role, source_owner, observed/underlying source, estimate_owner, related_request_id, extraction_method, and confidence. Do not collapse Arm-style royalty economics, RISC-V/custom-core license facts, HBM/PHY/interface IP attach, or source-owned unit-base estimates into generic EDA revenue, product-roadmap prose, or ASIC NRE/tape-out rows.
- For company financial KPI, segment metric, guidance, actual, and source-owned estimate rows, prefer dataset_materializations family=company_financial_kpi when rows can be represented. Preserve reported_segment, normalized_segment, current_segment_group, segment_reclassification_event, segment_reclassification_effective_date, and segment_mapping_basis so reporting-strategy changes can be compared without rewriting history.
- For facility capex, construction, build-time, and capacity-ramp rows, prefer dataset_materializations family=facility_capex_ramp. Use stage rather than vague "capacity ramp" prose, and default unknown identifiers to "unknown" when a source gives capex/timing/capacity but omits the facility, project, product, stage, or basis. Separate company-stated facts from industry-average or model-assumption facts with source_basis/timing_basis/capacity_basis.
- For critical-minerals, strategic-material, mine, smelter, refinery, and AI-infrastructure feedstock project rows, prefer dataset_materializations family=project_capacity_ramp. Use row_kind to separate reserve_resource, project_capacity, production, offtake, permitting, construction, processing_route, capex_cost, and ramp rows. Preserve company/role, project, site_or_asset, resource_or_product, strategic_material_family, refined/intermediate product, feedstock, grade/purity, reserve/resource tons and category, country/region/jurisdiction, project_stage, permitting_status, construction_status, planned_start_date, ramp_start_date, target_run_rate_date, ramp_phase/curve, capacity/nameplate/current/target/addition, production_volume/unit, recovery_rate_pct, capex_value/currency, operating_cost/cost_unit, offtake_or_customer/volume/unit/status, processing_route, refinery_or_smelter, mine_or_feedstock_source, source owner/provenance, document/request links, extraction_method, and confidence. Use this for fluorspar/HF feedstock, gallium, germanium, graphite, antimony, tungsten, molybdenum, tantalum, tin, silver, lithium, nickel, cobalt, copper, rare-earth feedstock, and other semiconductor/AI infrastructure strategic materials when the fact is mine/smelter/refinery/offtake specific. Downstream semiconductor material qualification stays in 6937; chemical regulatory exposure stays in 7065; commodity prices/spreads stay in 6916; rare-earth permanent magnet capacity stays in 6959.
- For AI data-center physical power, utility interconnect, electrical-BOP equipment, and onsite-generation facts, prefer dataset_materializations family=power_utilities_site_load. Use row_kind to separate interconnection_queue, site_power_state, electrical_equipment, onsite_generation, lead_time_orderbook, and metric_binding rows. Preserve operator_or_company, site, region, utility_or_grid, load_stage, power_source, power_availability_status, requested_mw, contracted_mw, energized_mw, available_mw, interconnection_queue, interconnection_queue_id, interconnect_status, study_phase, deposit_amount, deposit_currency, deposit_refundability, network_upgrade_cost, network_upgrade_cost_allocation, upgrade_scope, expected_online_date, withdrawal_or_delay_reason, feed_or_substation, equipment_category, equipment_type, transformer_type, transformer_mva, transformer_core_material, electrical_steel_type, amorphous_metal_status, winding_material, bushing_type, tap_changer_type, insulation_material, oil_type, switchgear_voltage_kv, ups_capacity_mw, pdu_capacity_mw, capacitor_bank_mvar, harmonic_filter_status, reactive_power_mvar, power_factor_correction_status, circuit_breaker_type, relay_or_protection_type, circuit_interruption_rating_ka, gas_turbine_model, generator_model, bess_capacity_mw/mwh, fuel_cell_capacity_mw, microgrid_capacity_mw, onsite_generation_mw, hvdc_or_transmission_cable_type, busbar_type, lead_time_weeks, orderbook_value/unit, supply_chain_id, metric_binding_kind/key, source owner/provenance, document links, extraction_method, and confidence. Commercial PPA/offtake terms stay in 6911, utility tariffs/dockets in 6973, official queue/docket ingestion in 7049, site readiness/permitting in 6941, fuel contracts in 6942, rack-local PSU/rectifier shelves/BBU/CBU/ride-through/power-quality topology in 7063, and board-level VRM/POL/PMIC in 6900.
- For regulated utility rate-base, load-growth, tariff, rider, allowed-ROE, and rate-case economics, prefer dataset_materializations family=regulated_utility_ratebase. Use row_kind to separate rate_case, rate_base, allowed_roe, tariff_rider, revenue_requirement, capex_plan, load_growth, data_center_load, interconnection_queue, ppa_offtake, and source_conflict rows. Preserve utility, parent company/ticker, jurisdiction/state/commission, docket id/type/status, filing/order/effective/in-service dates, tariff_or_rider, customer class/customer, project/site/region, rate_base value/currency/addition, allowed/requested/approved ROE, capex, revenue requirement, rate increase and bill impact, load growth MW/pct, data-center load MW, contracted load MW, interconnection queue MW/id, PPA/offtake status, contracted MW, PPA price/unit, source owner/provenance, document/request/supply-chain/model/knowledge links, extraction_method, and confidence. Physical site MW, interconnect equipment, deposits, and electrical BOP stay in 6881; PPA/offtake commercial terms stay in 6911; official queue/docket acquisition stays in 7049; fuel deliverability stays in 6942.
- For natural-gas, pipeline, LNG, diesel, backup-fuel, onsite-power fuel, and fuel-cell feedstock deliverability facts, prefer dataset_materializations family=fuel_supply_contract. Use row_kind to separate pipeline_interconnect, fuel_supply_contract, deliverability_commitment, price_basis, backup_fuel, fuel_cell_feedstock, curtailment_force_majeure, and shortfall_remedy rows. Preserve operator_or_company, customer, site/project/region, market_or_hub, fuel_type, fuel_supplier, pipeline_operator/name/interconnect, interconnect_status, fuel_supply_status, service_class, firm_or_interruptible_service, pipeline_capacity_mcf_d, pipeline_capacity_mmbtu_d, contracted_volume_mcf_d, contracted_volume_mmbtu_d, deliverable_capacity_mcf_d, deliverable_capacity_mmbtu_d, deliverable_volume/unit, take_or_pay_status/volume/unit, minimum_volume/unit, transport_path, receipt_point, delivery_point, balancing_obligation, fuel_price_index, basis_location/spread/unit, pass_through_terms, escalation_terms, backup_fuel_type/volume/unit/days, fuel_cell_feedstock/status, lng_dependency, diesel_dependency, curtailment_terms, force_majeure_terms, contract_start_date/end_date, contract_term_months, deliverability_obligation, liquidated_damages, shortfall_remedy, capacity_reservation_fee, demand_charge, currency, source owner/provenance, document links, extraction_method, and confidence. Onsite generation equipment/MW/orderbook stays in power_utilities_site_load; commercial PPA/offtake/project-finance terms stay in 6911; utility tariff/rate-case economics stay in 6973; commodity benchmark prices/spreads stay in 6916; official source ingestion stays in 7049.
- For oil/gas upstream production, reserves/resources, rigs/DUCs, LNG export, pipeline, and midstream project facts, prefer dataset_materializations family=oil_gas_lng_midstream_project. Use row_kind to separate upstream_production, reserve_resource, drilling_activity, decline_rate, lng_export_capacity, liquefaction_train, pipeline_capacity, midstream_contract, price_differential, capex_project, and source_conflict rows. Preserve company/ticker, operator/partner, basin, asset/field/project, project_stage, region/country, commodity/product mix, oil/gas/liquids mix, production in boe/d, mcf/d, bbl/d, reserves/resources and category, decline rate, rig count, DUC count, pipeline capacity, LNG capacity, liquefaction train, contracted volume/unit/status, customer/offtaker, price differential/unit/benchmark, online/start date, capex/currency, operating cost, utilization, source owner/provenance, document/request/supply-chain/model/knowledge links, extraction_method, and confidence. Data-center fuel deliverability and plant-gate gas contracts stay in 6942; commodity prices/spreads/inventory stay in 6916; macro/geopolitical wrappers stay in 6940/6908 unless they provide explicit source-owned physical variables.
- For housing, homebuilder, mortgage-rate, and inventory operating KPIs, prefer dataset_materializations family=housing_homebuilder_kpi. Use row_kind to separate orders, closings, backlog, cancellations, pricing_margin, starts_permits, inventory, mortgage_rate, incentives, community_count, and source_conflict rows. Preserve company/ticker, segment, region/market, community type, orders, closings, backlog units/value, cancellation rate, ASP, gross margin, starts, permits, inventory units/months, spec and lot inventory, mortgage rate, incentive value/pct, community count/active/net-new communities, metric/value/unit/currency, source owner/provenance, document/request links, extraction_method, and confidence. Use this for housing-cycle/homebuilder operating facts; generic company revenue/EPS stays in company_financial_kpi.
- For travel, lodging, hotel, OTA, and airline operating KPIs, prefer dataset_materializations family=travel_lodging_airline_kpi. Use row_kind to separate ota_bookings, hotel_operating, airline_capacity, airline_unit_economics, cancellations, route_market, and source_conflict rows. Preserve company/ticker, business_model, segment, region/country/market, route/origin/destination, customer segment, channel, gross bookings, booking value, bookings units, room nights and growth, ADR, RevPAR, occupancy, available room nights, cancellations and cancellation rate, ASM, RPM, RASM, CASM, load factor, passengers, capacity units/unit, metric/value/unit/currency, source owner/provenance, document/request links, extraction_method, and confidence. Keep marketplace take-rate/merchant economics in 6930 when explicitly OTA marketplace-specific; generic company revenue/EPS stays in company_financial_kpi.
- For agriculture, fertilizer, crop-balance, and input-cost facts, prefer dataset_materializations family=agriculture_fertilizer_crop_input. Use row_kind to separate fertilizer_capacity, fertilizer_price, feedstock_cost, crop_acreage, crop_yield_production, stocks_inventory, planting_progress, harvest_progress, weather_impact, and source_conflict rows. Preserve company/ticker, supplier/customer, product, fertilizer type, nutrient, crop, crop year/season, region/country/market, ammonia/urea/potash/phosphate capacity, production, utilization, fertilizer price/unit/currency, feedstock type and cost/unit, planted/harvested acreage, yield and unit, crop production/unit, stocks-to-use, ending stocks, fertilizer inventory, inventory days, planting/harvest progress, weather, soil moisture, drought status, metric/value/unit/currency, source owner/provenance, document/request links, extraction_method, and confidence. Commodity benchmark prices/spreads stay in 6916 when not tied to an input/crop row; gas deliverability and contracts stay in 6942.
- For industrial chemicals, resin/polymer, chlor-alkali, caustic, product-spread, and feedstock-spread facts, prefer dataset_materializations family=industrial_chemical_product_chain. Use row_kind to separate chemical_capacity, production_utilization, product_price, feedstock_spread, resin_polymer_inventory, chlor_alkali, turnaround, plant_outage, contract_pricing, and source_conflict rows. Preserve company/ticker, supplier/customer, product chain, chemical family/product, resin/polymer, product grade, region/country/market, facility/plant/status, capacity, production, utilization/operating rate, product price/unit, contract price/unit, feedstock type and cost/unit, product spread/unit/basis, inventory days/tons, resin/polymer inventory, chlorine/caustic capacity, caustic price, chlor-alkali margin, turnaround/outage status/dates/reason/capacity/duration, contract status/volume/unit, metric/value/unit/currency, source owner/provenance, document/request links, extraction_method, and confidence. Semiconductor-grade process materials stay in 6937; chemical regulatory exposure stays in 7065; pure commodity benchmark prices/spreads stay in 6916.
- For aerospace airframe, engine, MRO, and delivery-backlog production facts, prefer dataset_materializations family=aerospace_airframe_engine_mro. Use row_kind to separate aircraft_delivery, build_rate, production_rate, backlog_orderbook, engine_production, mro_shop_visit, spare_parts, supplier_constraint, qualification_certification, delivery_delay, and source_conflict rows. Preserve company/ticker, manufacturer, airline/operator, supplier/customer, program, aircraft model/family, engine family/model, component and constraint component, aircraft deliveries, delivery period, build/production rates and units, backlog units/value, order units, book-to-bill, MRO shop visits/capacity, spare-parts units/revenue, lead time, qualification/certification status, delivery delay, constraint reason, metric/value/unit/currency, source owner/provenance, document/request links, extraction_method, and confidence. Generic backlog rows stay in 6904 when not aerospace-specific; data-center turbine/generator facts stay in 6881 unless the source is about aerospace engine production.
- For defense munitions, missile, drone, rocket-motor, and energetics production-capacity facts, prefer dataset_materializations family=defense_munitions_production_capacity. Use row_kind to separate munitions_production_rate, missile_interceptor_capacity, drone_production, rocket_motor_capacity, energetics_constraint, funded_backlog, orderbook, stockpile_replenishment, capacity_addition, unit_price, qualification, and source_conflict rows. Preserve company/ticker, prime contractor, supplier/customer/agency/country, program, weapon system, munition type, missile family, drone platform, rocket motor, energetics material, component/subsystem, annual production rate/unit, surge capacity/unit, funded backlog value/units, orderbook value/units, stockpile units, replenishment rate/unit, unit price/currency, capacity additions/unit/online date, lead time, qualification/certification status, constraint reason, contract status, metric/value/unit/currency, source owner/provenance, document/request links, extraction_method, and confidence. Government award/obligation details stay in 6913; aerospace engine production stays in 6967; generic commodity explosives/feedstocks stay in 6916 or 6966 when upstream material-specific.
- For satellite RF payload, user-terminal, launch, and rad-hard semiconductor hardware facts, prefer dataset_materializations family=satellite_rf_payload_space_compute. Use row_kind to separate constellation_deployment, launch_cadence, rf_payload_capacity, phased_array_payload, user_terminal_modem, ground_segment_gateway, rad_hard_semiconductor, backlog_asp, qualification, lead_time, and source_conflict rows. Preserve company/ticker, constellation operator, constellation, satellite program/model, orbit, frequency band, payload type, RF payload capacity/unit, phased arrays, beams, bandwidth/throughput, user terminal units/ASP, modem units/ASP, ground segment sites, gateways, launch count/provider/vehicle, satellites deployed/backlog, rad-hard part/node/units/ASP, qualification, lead time, backlog units/value, ASP/currency, capacity units/unit, metric/value/unit/currency, source owner/provenance, document/request/supply-chain links, extraction_method, and confidence. EO/GEOINT data-service economics stay in 7057; spacecraft bus/payload integration throughput stays in 7058; orbital edge-compute service pricing stays in 7059; government contract award values stay in 6913.
- For nuclear restart, SMR, uranium, conversion, enrichment, HALEU, fuel-fabrication, and nuclear power offtake facts, prefer dataset_materializations family=nuclear_smr_fuel_cycle. Use row_kind to separate reactor_project, restart_newbuild, smr_offtake, regulatory_milestone, uranium_supply, conversion_capacity, enrichment_haleu_capacity, fuel_fabrication, price_contract, and metric_binding rows. Preserve company, operator_or_utility, developer, customer_or_offtaker, project/site/region/country, reactor_type, reactor_model, reactor_status, restart_or_newbuild_status, project_stage, regulatory_body, regulatory_milestone, milestone_date, license_or_docket_id, target_online_date, target_cod_date, construction_start_date, capacity_mw, contracted_mw, capex_value/currency, offtake_contract_type, offtake_customer, contract_start_date/end_date, contract_term_months, ppa_price, capacity_payment, fuel_cycle_stage, uranium_capacity_lbs_u3o8, conversion_capacity_kgu, enrichment_capacity_swu, haleu_capacity_kg, fuel_fabrication_capacity, fuel_unit, uranium_price, conversion_price, enrichment_price, haleu_price, price_unit, contract_volume/unit, supplier/customer, source_role, source owner/provenance, document links, supply_chain_id, metric_binding_kind/key, extraction_method, and confidence. Generic facility build-time/capex stays in 6893 when not nuclear-specific; utility tariff/rate-case economics stay in 6973; government funding/awards stay in 6913; non-nuclear commodity prices stay in 6916; gas and backup-fuel deliverability stays in fuel_supply_contract.
- For AI rack liquid cooling, direct-to-chip cold plates, CDUs, manifolds, quick-disconnects, pumps, coolant, and component supply facts, prefer dataset_materializations family=ai_rack_liquid_cooling. Use row_kind to separate cooling_architecture, cold_plate, cdu, manifold, quick_disconnect, pump, coolant, qualification, pricing, adoption_deployment, service_leak_risk, and lead_time_orderbook rows. Preserve supplier, customer, operator_or_company, platform_or_system, rack_platform, accelerator_platform, site/region, cooling_architecture, rack_density_kw, heat_load_kw, component_category, component_type, cold_plate_model, cdu_model, manifold_model, quick_disconnect_model, pump_model, coolant_type, coolant_chemistry, flow_rate_lpm, flow_rate_gpm, component_capacity_kw, component_capacity_units/capacity_unit, deployment_units, adoption_rate_pct, qualification_status/customer, lead_time_weeks, orderbook_value/unit, asp, price, currency, service_risk, leak_risk, service_interval_months, source owner/provenance, document links, supply_chain_id, metric_binding_kind/key, extraction_method, and confidence. Building-level HVAC/chillers/mechanical EPC stays in data_center_mechanical_epc; controls telemetry and leak sensors stay in data_center_controls_telemetry; rack-local power conversion stays in ai_rack_power_conversion; package/rack failure mechanisms and FA capacity stay in 7066.
- For rack-local AI power conversion, PSU/power shelf, rectifier shelf, 800V/HVDC, BBU/CBU buffering, ride-through, and power-quality facts, prefer dataset_materializations family=ai_rack_power_conversion. Use row_kind to separate rack_conversion_architecture, power_shelf, rectifier_shelf, local_buffering, ride_through, power_quality, lead_time_orderbook, and qualification rows. Preserve supplier, customer, operator_or_company, platform_or_system, rack_platform, server_platform, accelerator_platform, rack_power_conversion_architecture, power_shelf_model, psu_shelf_model, rectifier_shelf_model, psu_count, rectifier_count, conversion_stage, ac_dc_stage, dc_dc_stage, input_voltage_v, output_voltage_v, input_topology, output_topology, hvdc_topology, rack_power_kw, power_capacity_kw/kwh, bbu_capacity_kw/kwh, cbu_capacity_kw/kwh, local_buffering_status, ride_through_runtime_ms/s, efficiency_pct, power_quality_status, thd_pct, harmonic_filter_status, transient_response_ms, capacity_units/unit, orderbook_value/unit, lead_time_weeks, qualification_status/customer, source owner/provenance, document links, extraction_method, and confidence. Site/grid MW and electrical-BOP equipment stay in power_utilities_site_load; board-level VRM/POL/PMIC/DrMOS stays in 6900; generic BESS/cell capacity stays in 6931; rack BOM/integration stays in 6891/7033; online schedulable capacity stays in 7034.
- For AI server timing, clocking, crystal oscillator, TCXO, OCXO, resonator, clock generator, timing IC, and synchronization-content facts, prefer dataset_materializations family=ai_server_timing_component_supply. Use row_kind to separate crystal_oscillator, tcxo, ocxo, resonator, clock_generator, timing_ic, synchronization_content, capacity, pricing, allocation, qualification, and source_conflict rows. Preserve supplier/role, customer/role, platform_or_system, rack_platform, server_platform, accelerator_vendor/generation/model, timing_component_category, component_family/type/name/model, timing_function, synchronization_role, timing_specification, frequency_mhz or frequency_min/max_mhz, jitter_fs, phase_noise, accuracy_ppm, temperature_grade, package_or_form_factor, content_scope, content_per_server/rack/cluster, attach_rate, ASP/price/currency, price_change_pct, capacity_units/unit, utilization, lead_time_weeks, allocation_status/share/amount/unit, qualification_status/stage/customer, routing_boundary, source owner/provenance, observed/underlying source, document links, related_request_id, supply_chain_id, bottleneck_model_id, knowledge_asset_id, extraction_method, and confidence. Generic passives stay in 6897; board-level power delivery stays in 6900; rack BOM/integration stays in 6891/7033; runtime performance effects stay in 6938/7038.
- For AI server rack BOM, ASP, ODM/JDM/EMS revenue, backlog/orderbook, rack-equivalent units, customer exposure, pass-through GPU content, and consignment-vs-merchant accounting facts, prefer dataset_materializations family=ai_server_rack_odm_economics. Use row_kind to separate rack_bom, server_unit_volume, rack_equivalent_units, odm_revenue, jdm_ems_revenue, backlog_orderbook, customer_exposure, pass_through_gpu_content, consignment_accounting, merchant_vs_consigned_revenue, shipment_build_rate, allocation, and source_conflict rows. Preserve company, odm_jdm_partner, ems_partner, customer/role, platform_or_system, rack_platform, server_platform, accelerator_vendor/generation/model, gpu_count_per_server, accelerators_per_rack, server_units, rack_units, rack_equivalent_units, bom_scope, rack_bom_value, bom_cost, asp/currency, odm_revenue, jdm_revenue, ems_revenue, merchant_revenue, pass_through_gpu_content/value, consigned_gpu_units, consignment_accounting_treatment, consignment_share_pct, gross_margin_pct, backlog_value/units, orderbook_value, order_units, customer_exposure_pct, allocation_status, build_rate_units/unit, shipment_units/unit, revenue_recognition_basis, routing_boundary, source owner/provenance, document links, related_request_id, supply_chain_id, bottleneck_model_id, knowledge_asset_id, extraction_method, and confidence. Physical module/tray/rack integration, burn-in, validation, and qualified-system-output conversion stay in 7033; rack-local cooling stays in 6882; rack-local power conversion stays in 7063; networking fabric stays in 6899; board-level power delivery stays in 6900; online/schedulable capacity stays in 7034.
- For AI accelerator module, board/tray, rack integration, burn-in, validation, acceptance, first-pass yield, DOA/failure, rework, and qualified-system-output facts, prefer dataset_materializations family=ai_accelerator_rack_integration_output. Use row_kind to separate module_integration, board_baseboard_integration, tray_server_integration, rack_integration, burn_in_capacity, validation_acceptance, first_pass_yield, doa_failure_rework, qualified_system_output, backlog_orderbook, customer_allocation, and source_conflict rows. Preserve platform_or_system, accelerator_vendor, customer/role, accelerator_generation/model, module_form_factor, baseboard_ubb, tray_server_platform, rack_platform, accelerators_per_module/board/tray/rack, cpu_host_ratio, host_cpu, switch_content, nic_content, dpu_content, cooling_architecture, power_architecture, odm_jdm_partner, ems_partner, integration_site, region/country, module/board/tray/rack line capacity units, capacity_unit, burn_in_duration_hours, burn_in_capacity_units, validation_status, acceptance_status, first_pass_yield, doa_failure_rate_pct, rework_rate_pct, qualified_system_output_units/unit, backlog/orderbook value, customer_allocation_pct, lead_time_weeks, constraint_reason, routing_boundary, source owner/provenance, document links, related_request_id, supply_chain_id, bottleneck_model_id, knowledge_asset_id, extraction_method, and confidence. BOM/ASP/ODM revenue and consignment accounting stay in 6891; package output conversion stays in 7032; HBM stack conversion stays in 7031; package-test/ATE capacity stays in 6936; cooling components stay in 6882; networking fabric stays in 6899; board-level power delivery stays in 6900; post-sale quality/warranty stays in 6988; online/schedulable capacity stays in 7034.
- For AI factory or cloud cluster commissioning, delivered/installed racks, shipment batch, site arrival, warehouse/staging, install start, hookup readiness, deployment crew/FSE capacity, FRU/spares availability, delivered/installed/online/schedulable accelerators, effective GPU-equivalent capacity, commissioning dates, site/customer acceptance, deployment delays, burn-in/soak, uptime, utilization ramp, cloud service/SKU, and allocation facts, prefer dataset_materializations family=ai_factory_cluster_commissioning. Use row_kind to separate shipment_arrival, staging_inventory, rack_install, hookup_readiness, deployment_crew_fse, fru_spares, commissioning, acceptance, online_capacity, schedulable_capacity, effective_gpu_capacity, deployment_delay, utilization_ramp, cloud_sku_allocation, and source_conflict rows. Preserve site, region/country, operator_or_cloud_provider, customer_or_tenant, platform_or_system, rack_platform, accelerator_vendor/generation/model, delivered_racks, installed_racks, shipment_batch, arrival_date, warehouse_staging_location, install_start_date, hookup_readiness_status, deployment_crew_owner/capacity/unit, fse_owner/capacity, fru_spares_availability_status, fru_spares_days_on_hand, delivered_accelerators, installed_accelerators, online_accelerators, schedulable_accelerators, effective_gpu_equivalent_capacity, capacity_unit, commissioning_start_date, commissioning_complete_date, site_acceptance_date, customer_acceptance_status, deployment_delay_days/reason, burn_in_soak_hours, uptime_pct, utilization_ramp_pct, cloud_service, service_sku, allocation_status/share, routing_boundary, source owner/provenance, document links, related_request_id, supply_chain_id, bottleneck_model_id, knowledge_asset_id, extraction_method, and confidence. Rack integration and qualified-output conversion stay in 7033; cloud commercial committed-spend/utilization economics stay in 6951; site power/EPC/readiness stays in 6881/6960/6941; fabric stays in 6899; storage stays in 6943; runtime operations stay in 7023; warranty/RMA stays in 6988; physical freight lanes and dwell stay in 7044.
- For data-center land, water, WUE, discharge, air-emissions, zoning, and construction-readiness facts, prefer dataset_materializations family=data_center_site_readiness. Use row_kind to separate land_zoning, water_rights, wue_water_consumption, discharge_permit, air_emissions_permit, community_constraint, shell_delivery, construction_labor, and site_readiness rows. Preserve operator_or_company, site, project, region, jurisdiction, land_control_status, land_area_acres, zoning_status, permit_status, site_readiness_status, shell_delivery_date, construction_labor_count/status, water_rights_status, water_source, water_consumption_mgd, peak_water_withdrawal_mgd, annual_water_withdrawal_mgy, wue, reclaimed_nonpotable_share_pct, discharge_permit_status/id, discharge_limit/unit, water_treatment_contractor, drought_constraint, community_constraint, cooling_method, air_emissions_permit_status, air_permit_id, generator_air_permit_status, nox_limit_tpy, co2_limit_tpy, particulate_limit_tpy, sox_limit_tpy, operating_hour_limit, mitigation_commitment, source owner/provenance, document links, extraction_method, and confidence. Official water/air permit ingestion stays in 7049; semiconductor fab UPW/wastewater stays in 6962; onsite generation equipment/MW stays in power_utilities_site_load; fuel deliverability stays in 6942; HVAC/chiller assets stay in 6960; rack liquid cooling stays in 6882.
- For data-center HVAC/chiller, mechanical plant, EPC labor, commissioning, procurement, and project material-intensity facts, prefer dataset_materializations family=data_center_mechanical_epc. Use row_kind to separate hvac_chiller, facility_equipment, epc_labor, commissioning, material_intensity, procurement_orderbook, and lead_time rows. Preserve operator_or_company, customer, project, site, region, project_phase, supplier, epc_contractor, equipment_category/type, facility_equipment, hvac_equipment, chiller_type, chiller_capacity_tons, cooling_capacity_kw, mechanical_system, building_controls_scope, commissioning_status/start_date/labor_count, epc_labor_count/trade, material_family/spec/quantity/unit, material_intensity_per_mw, material_intensity_per_rack, material_intensity_per_sqft, price, price_index, currency, supplier_or_epc_procurement_status, lead_time_weeks, orderbook_value/unit, capacity_units/unit, source owner/provenance, document links, extraction_method, and confidence. Commodity price/spread/inventory stays in 6916; electrical equipment orderbook and MVA/MW lead time stay in power_utilities_site_load; upstream mine/refinery capacity stays in 6966; DCIM/BMS/EPMS/SCADA telemetry stays in 7064.
- For data-center DCIM/BMS/EPMS/SCADA/PLC, controls telemetry, digital twin, safety/security, OT segmentation, derating, and operational-readiness facts, prefer dataset_materializations family=data_center_controls_telemetry. Use row_kind to separate dcim_bms_epms_scada, sensor_telemetry, control_loop, digital_twin, leak_fire_security, ot_cybersecurity, derating_holdback, supplier_capacity, and acceptance rows. Preserve operator_or_company, site, project, region, supplier/customer, subsystem_scope, dcim_vendor, bms_vendor, epms_vendor, scada_vendor, plc_vendor, sensor_type/count/density, telemetry_rate_hz, alarm_count, event_count, commissioning_state, control_loop_status, digital_twin_coverage_pct, digital_twin_update_cadence, thermal_control_integration, power_monitoring_integration, rack_room_site_scope, leak_detection_status, fire_detection_status, fire_suppression_status, physical_security_status, access_control_status, ot_cybersecurity_segmentation, availability_derate_pct, capacity_holdback_mw, supplier_capacity_units/unit, lead_time_weeks, qualification_status, acceptance_status, source owner/provenance, document links, extraction_method, and confidence. Physical EPC/HVAC assets stay in data_center_mechanical_epc; power equipment stays in power_utilities_site_load; cooling components stay in 6882; online schedulable capacity stays in 7034; actual incidents/outages/security events stay in 7040.
- For OCR/VLM chart, table, diagram, or non-signal media evidence, prefer dataset_materializations family=media_chart_extraction before promoting to other families. Use one row per visible chart/table data point when numbers are recoverable. Use row_kind=qualitative_readout when the visual is useful but values are not recoverable. Use row_kind=non_signal_discard and destination_recommendation=discard when the media is decorative/non-signal. The vision model supplies evidence; this text-only durable pass owns structured routing and JSON correctness.
- For EDA market structure and design-cycle throughput, prefer dataset_materializations family=eda_market_structure. Use row_kind to separate vendor_revenue, market_share, duopoly_share, design_flow_stage_duration, verification_workload, emulation_capacity, eda_cloud_hpc_license_constraint, physical_signoff_mask_dataprep_constraint, and bottleneck_evidence. Preserve vendor/tool/platform, tool_category, customer/program, node, stage, duration_low/high, verification_cycle_share_pct, capacity_units, license_seats, compute_hours, utilization, mask/data-prep dependency, source owner, extraction_method, and chart/media provenance. Do not reduce EDA operational bottlenecks to generic revenue/share rows.
- For semiconductor WFE, process-control, metrology, inspection, yield-management software, advanced-packaging equipment, orderbook, throughput, and service facts, prefer dataset_materializations family=semiconductor_capacity. Use row_kind to separate fab_capacity, wfe_orderbook, process_control_tool, wafer_inspection, metrology_tool, yield_management_software, advanced_packaging_equipment, service_revenue, and source_conflict rows. Preserve company/supplier/customer, facility/region, capacity_type, node_or_technology, process_step, package_type, equipment_category/type, tool_category/tool_or_platform, process_control_type, inspection_type, metrology_type, advanced_packaging_equipment_type, wafer/e-beam/optical inspection status, overlay/CD-SEM/scatterometry/thin-film metrology status, yield_management_software/module, temporary bonding/debonding, hybrid bonding, TCB, die bonding, RDL plating, laser drilling, wafer_size_mm, installed_base, tool_units, shipments_units, capacity_units/unit, throughput_wph, utilization, orderbook/backlog value and unit, lead_time_weeks, service_revenue/currency, defect_density, defect_capture_rate_pct, qualification_status/customer, source owner/provenance, document links, related_request_id, supply_chain_id, bottleneck_model_id, knowledge_asset_id, product_stage_id, extraction_method, and confidence. Observed yield and customer acceptance outcomes stay in 6909; lithography scanners, mask blanks, photomasks, reticles, pellicles, and mask-shop capacity stay in 6944; package-test/ATE/probe/socket capacity stays in 6936; fab automation/AMHS/MES/APC queue/cycle-time stays in 7027; internal WFE subcomponents/spares/rebuild capacity stays in 7030.
- For lithography scanner, EUV/DUV installed-base, scanner throughput, uptime, shipment-slot, field-service, mask blank, photomask, reticle, pellicle, inspection/repair, reticle-handling, and mask-shop capacity facts, prefer dataset_materializations family=lithography_mask_reticle_supply. Use row_kind to separate scanner_installed_base, scanner_throughput, scanner_shipment_slots, scanner_field_service, mask_blank_capacity, photomask_capacity, reticle_capacity, pellicle_capacity, mask_shop_capacity, inspection_repair_capacity, reticle_handling_capacity, qualification, and source_conflict rows. Preserve supplier/customer/company/foundry, fab/facility, region/country, lithography_type, scanner_model, scanner_installed_base, scanner_shipment_slots, scanner_throughput_wph, uptime_pct, field_service_engineer_count/coverage, mask_blank supplier/type/capacity, photomask supplier/capacity, reticle supplier/capacity/count, pellicle supplier/type/capacity, mask_shop/capacity, inspection_tool, inspection_repair_capacity_units, reticle_handling_capacity_units, process_node, product_or_process, capacity_units/unit, orderbook/backlog value, lead_time_weeks, qualification_status, constraint_reason, price/currency, source owner/provenance, document links, related_request_id, supply_chain_id, bottleneck_model_id, knowledge_asset_id, extraction_method, and confidence. Generic WFE/process-control/metrology tool capacity stays in semiconductor_capacity/6889; EDA mask-data-prep/design-flow bottlenecks stay in eda_market_structure/6877; package geometry reticle stitching stays in advanced_package_conversion/7032; process materials/photoresist stay in 6937; lithography workforce/FSE staffing detail can route to 7028 when the source is about labor rather than lithography supply capacity.
- For foundry wafer-start, utilization, node pricing, allocation, and end-market exposure facts, prefer dataset_materializations family=foundry_wafer_start_allocation. Use row_kind to separate wafer_start_capacity, actual_wafer_starts, utilization, node_pricing, customer_allocation, end_market_exposure, and source_conflict rows. Preserve foundry/company, customer, facility/region, process_node/variant, capacity_scope, end_market, application_or_program, wafer_size_mm, wafer_start_capacity_wspm, wafer_starts_wspm, allocated_wspm, utilization, capacity_units/unit, wafer_price/currency/price_basis, allocation_status/share, end_market_exposure_pct, qualification_status, lead_time_weeks, start/ramp period, source owner/provenance, document links, related_request_id, supply_chain_id, bottleneck_model_id, knowledge_asset_id, extraction_method, and confidence. Process-feature roadmap, GAA/backside-power/PPA/yield-readiness, and PDK/IP readiness stay in 7026; WFE/process-control tool capacity stays in semiconductor_capacity/6889; HBM supplier qualification/allocation stays in hbm_supplier_qualification_allocation; generic commodity prices stay in pricing_supply_demand.
- For advanced-node process roadmap, GAA/nanosheet/CFET, backside power/SPR/BSPDN, PPA, yield-readiness, PDK/IP readiness, and process-feature risk facts, prefer dataset_materializations family=advanced_node_process_roadmap. Use row_kind to separate process_feature_status, transistor_architecture, backside_power_status, ppa_delta, yield_readiness, pdk_ip_readiness, volume_production_timing, customer_qualification, dependency, and source_conflict rows. Preserve foundry, customer, anchor_product, target_application, process_node/variant/feature, transistor_architecture, gaa_status, nanosheet_status, cfet_status, backside_power_architecture, spr_bspdn_status, ppa_metric/baseline/value/unit, yield_rate, defect_density, yield_readiness_status, qualification_status, pdk_ip_readiness, risk_level/reason, risk_production_date, volume_production_date, WFE/lithography/material dependencies, source owner/provenance, document links, related_request_id, supply_chain_id, bottleneck_model_id, knowledge_asset_id, extraction_method, and confidence. Foundry wafer-start capacity, utilization, allocation, and node pricing stay in foundry_wafer_start_allocation; WFE/lithography equipment capacity stays in semiconductor_capacity/6889 or 6944; material capacity/pricing stays in 6937; customer program/tape-out economics stay in 7025 or 6939.
- For semiconductor process materials, process gases, fluorochemical/PFAS-class supply, reclaim, and advanced-packaging consumables, prefer dataset_materializations family=semiconductor_process_materials. Use row_kind to separate wafer_supply, soi_epi_supply, quartz_ceramics, photoresist, wet_chemical, precursor, filter_media, process_gas, fluorochemical_supply, fluoropolymer_supply, cmp_slurry_pad, plating_uhp_chemical, reclaim_recycling, packaging_consumable, tim_underfill_emc_die_attach, qualification, contract_pricing, and source_conflict rows. Preserve supplier, customer/fab, facility/site, region/country, material_family/category/name, chemical_name/formula, pfas_or_fluorochemical_class, material_grade, purity/unit, process_step, technology_or_process, process_node, package_type, product_or_platform, application_or_program, capacity_value/unit, production_volume/unit, consumption_rate/unit, inventory_days, utilization, yield_rate, price/unit/currency, contract_status/type, committed_volume/unit, lead_time_weeks, qualification_status/customer/stage, qualified_site, reclaim_status/route/capacity/unit, recycling_rate_pct, substitution_status/material, requalification_time_weeks, constraint_reason, feedstock_dependency, upstream_feedstock, routing_boundary, source owner/provenance, document links, related_request_id, supply_chain_id, bottleneck_model_id, knowledge_asset_id, extraction_method, and confidence. Chemical registration, PFAS/fluorochemical restrictions, abatement, permits, compliance deadlines, and regulatory exposure stay in 7065; upstream mine/smelter/refinery/feedstock project capacity stays in 6966; semiconductor fab UPW/wastewater utilities stay in 6962; material failure mechanisms and FA capacity stay in 7066; substrate/package capacity stays in advanced_packaging_capacity/6883; WFE/process-control tool capacity stays in semiconductor_capacity/6889.
- For semiconductor chemical regulatory exposure, PFAS/fluorochemical restrictions, registrations, permits, abatement, reclaim requirements, compliance deadlines, substitution/requalification exposure, and modeled capacity/price impact, prefer dataset_materializations family=semiconductor_chemical_regulatory_exposure. Use row_kind to separate pfas_restriction, fluorochemical_registration, environmental_permit, abatement_requirement, reclaim_requirement, substitution_requalification, affected_capacity, capex_price_impact, and source_conflict rows. Preserve material_family/category/name, chemical_name/formula, pfas_or_fluorochemical_class, material_grade, supplier/site, customer/fab, facility/region/country, jurisdiction, regulator, rule_name/identifier, registration_status, permit_status, restriction_type, compliance_status, effective_date, compliance_deadline, affected_process_step, process_node, package_type, product_or_platform, affected_capacity_value/unit, affected_material_volume/unit, requalification_path, substitution_path/status, requalification_time_weeks, abatement_requirement/capacity/unit, reclaim_requirement, environmental_permit_status, capex_impact, price_impact_pct, lead_time_impact_weeks, modeled_impact/unit, risk_level/reason, routing_boundary, source owner/provenance, document links, related_request_id, supply_chain_id, bottleneck_model_id, knowledge_asset_id, extraction_method, and confidence. Capacity, price, purity, qualification, lead time, and reclaim-as-supply rows stay in semiconductor_process_materials/6937; fab UPW/wastewater readiness stays in 6962; upstream mine/refinery/feedstock projects stay in 6966; generic export controls/license restrictions stay in 6906; tariffs/subsidies stay in 6923.
- For semiconductor fab utilities, UPW, wastewater, hookups, utility permits, and ramp-readiness facts, prefer dataset_materializations family=semiconductor_fab_utilities. Use row_kind to separate upw_capacity, raw_water_supply, reclaimed_water_supply, wastewater_treatment, discharge_permit, utility_hookup, bulk_gas_utility, power_utility, water_intensity, utility_contractor, ramp_readiness, and source_conflict rows. Preserve company/customer, fab, facility/site, project, region/country/jurisdiction, product_or_process, process_node, wafer_size_mm, ramp_phase, utility_type/scope, utility_capacity_value/unit, upw_capacity_m3_day, raw_water_capacity_m3_day, reclaimed_water_capacity_m3_day, wastewater_treatment_capacity_m3_day, discharge_limit/unit, water_per_wafer_liters, water_reuse_pct, reclaimed_water_share_pct, power_capacity_mw, gas_capacity_nm3_hour, wastewater_permit_status, water_rights_status, utility_permit_status, discharge_permit_id, hookup_status, hookup_ready_date, utility_readiness_status, water_source, utility_provider/contractor, water_treatment_contractor, constraint_reason, capex_value/currency, lead_time_weeks, source owner/provenance, document links, related_request_id, supply_chain_id, bottleneck_model_id, knowledge_asset_id, extraction_method, and confidence. Data-center water/WUE/site-readiness stays in 6941; official water/air/energy/queue permit ingestion stays in 7049; process gases/materials stay in 6937; sub-fab vacuum/abatement/chiller support equipment stays in 7029; fab automation and mini-environment readiness stay in 7027.
- For semiconductor fab automation, AMHS/OHT/stocker, FOUP/FOSB, EFEM/load-port, wafer-handling robot, mini-environment, cleanroom filtration/contamination-control, MES dispatch, APC/FDC, queue-time, cycle-time, tool-utilization, throughput, and automation-readiness facts, prefer dataset_materializations family=semiconductor_fab_automation. Use row_kind to separate amhs_oht_capacity, stocker_capacity, carrier_foup_fosb, efem_load_port, wafer_handling_robot, mini_environment_cleanroom, mes_dispatch, apc_fdc_control, queue_cycle_time, automation_readiness, and source_conflict rows. Preserve company/supplier/customer, fab/facility/project, region/country, product_or_process, process_node, wafer_size_mm, ramp_phase, automation_scope/system, amhs_type, oht_system, stocker_system, carrier_type, foup_capacity_units, fosb_capacity_units, carrier_capacity_units, carrier_turns_per_day, efem_model, load_port_count, wafer_handling_robot_model, robot_count, mini_environment_status, cleanroom_class, cleanroom_filtration_status, contamination_control_status, mes_system, mes_dispatch_status, apc_system/status, fdc_system/status, queue_time_hours, cycle_time_days, cycle_time_reduction_pct, tool_utilization_effect_pct, automation_throughput_wph, move_capacity_wafers_per_hour, installed_base, capacity_units/unit, orderbook/backlog value, lead_time_weeks, install_status, qualification_status, automation_readiness_status, constraint_reason, source owner/provenance, document links, related_request_id, supply_chain_id, bottleneck_model_id, knowledge_asset_id, extraction_method, and confidence. Generic WFE/process-control/metrology capacity stays in semiconductor_capacity/6889; fab utilities/UPW/wastewater stays in 6962; physical sub-fab support equipment stays in 7029; workforce/FSE staffing stays in 7028; observed yield/qualification outcomes stay in 6909.
- For semiconductor sub-fab vacuum, dry pumps, abatement, scrubbers, exhaust, chillers, temperature-control units, gas cabinets, valve/manifold boxes, pump rebuild/service, spares, uptime, response-time, and tool-facility interface readiness, prefer dataset_materializations family=semiconductor_subfab_support. Use row_kind to separate vacuum_pump_capacity, dry_pump_capacity, abatement_scrubber_capacity, exhaust_capacity, chiller_tcu_capacity, gas_cabinet_vmb_capacity, install_hookup_readiness, service_rebuild_capacity, spares_availability, uptime_service, and source_conflict rows. Preserve supplier, customer/company, fab/facility, region/country, support_equipment_category, component_type/model, tool_category/tool_or_platform, process_step, product_or_process, process_node, utility_interface, capacity_units/unit, dry_pump_capacity_units, abatement_capacity_units, scrubber_capacity_units, exhaust_flow_capacity_cfm, chiller_capacity_tons, temperature_control_units, gas_cabinet_capacity_units, valve_manifold_capacity_units, installed_base, orderbook/backlog value, lead_time_weeks, install_status, qualification_status, hookup_status, vacuum/abatement/exhaust/chiller readiness, uptime_pct, service_interval_days, pump_rebuild_capacity_units, spares_availability_status, spares_days_on_hand, response_time_hours, service_provider, constraint_reason, source owner/provenance, document links, related_request_id, supply_chain_id, bottleneck_model_id, knowledge_asset_id, extraction_method, and confidence. Site utilities and UPW/wastewater stay in semiconductor_fab_utilities/6962; process gases/materials stay in 6937; top-level WFE/tool-category capacity stays in semiconductor_capacity/6889; fab automation/AMHS/MES/mini-environment stays in 7027; workforce/FSE staffing stays in 7028; internal WFE modules, subcomponents, and spares stay in 7030.
- For semiconductor fab workforce, technician/operator availability, tool-install crews, vendor field-service engineers, commissioning teams, spares support, operator training, certification, shift staffing, labor cost, labor-driven lead time, and ramp-readiness facts, prefer dataset_materializations family=semiconductor_fab_workforce_service. Use row_kind to separate site_workforce, technician_availability, operator_staffing, tool_install_crew, vendor_fse_coverage, commissioning_team, spares_support, operator_training, certification, shift_staffing, labor_cost, ramp_readiness, and source_conflict rows. Preserve company, supplier/vendor, customer, fab/facility, region/country, role, skill_family, labor_category, headcount, open_roles, hiring_plan_count, shift_count, shift_coverage_pct, labor_cost/currency, tool_vendor, tool_category/tool_or_platform, tool_install_crew_capacity/unit, commissioning_status/team_count, field_service_engineer_count/coverage, spares_support_status, response_time_hours, operator_training_slots, certified_staff, certification_status, training_provider, ramp_readiness_status, product_or_process, process_node, lead_time_weeks, constraint_reason, source owner/provenance, document links, related_request_id, supply_chain_id, bottleneck_model_id, knowledge_asset_id, extraction_method, and confidence. Broad employer workforce stays in 6932; WFE/tool category capacity stays in semiconductor_capacity/6889; fab automation hardware/software stays in 7027; data-center EPC labor stays in 6960; facility schedules stay in facility_capex_ramp/6893; observed qualification/yield outcomes stay in 6909; physical sub-fab equipment capacity stays in 7029; WFE internal spares/subcomponents stay in 7030.
- For semiconductor equipment internal modules, critical subcomponents, spare parts, service-parts supply, rebuild/refurb capacity, failure/replacement rates, allocation, qualification, substitution, and single-source exposure facts, prefer dataset_materializations family=semiconductor_equipment_subcomponent_supply. Use row_kind to separate rf_power_module, mass_flow_controller, valve_manifold_part, esc_chamber_part, heater_showerhead, power_supply_module, motion_stage, sensor_controller, spare_part_availability, rebuild_refurb_capacity, failure_replacement_rate, allocation, qualification, substitution_second_source, and source_conflict rows. Preserve supplier/subcomponent_supplier, tool_vendor, customer/site/fab/facility, region/country, tool_category/tool_or_platform, process_step, process_node, component_family/type/name/model/spec, module_type/model, capacity_units/unit, utilization, orderbook/backlog value, lead_time_weeks, allocation_status/share/amount/unit, qualification_status/stage/customer, spare_part_availability_status, spare_parts_days_on_hand, service_parts_capacity_units, rebuild_capacity_units, refurb_capacity_units, turnaround_time_days, mtbf_hours, failure_rate_pct, replacement_rate_pct, single_source_exposure/supplier, substitution_status, substitute_supplier, requalification_time_weeks, price/currency, constraint_reason, source owner/provenance, document links, related_request_id, supply_chain_id, bottleneck_model_id, knowledge_asset_id, extraction_method, and confidence. Top-level WFE/tool capacity stays in semiconductor_capacity/6889; lithography scanners, masks, reticles, and pellicles stay in 6944; process materials and consumables stay in 6937; sub-fab facility support equipment stays in 7029; workforce/FSE labor stays in 7028; observed qualification/yield outcomes stay in 6909.
- For CoWoS, advanced-packaging capacity/allocation, ABF, glass substrate, interposer, silicon bridge, TGV, and panel-level packaging facts, prefer dataset_materializations family=advanced_packaging_capacity. Use row_kind to separate cowos_capacity, osat_cowos_capacity, substrate_capacity, glass_substrate_capacity, interposer_bridge, tgv_panel_packaging, customer_demand_share, allocation_share, package_output, substitution, and capacity_split. Preserve supplier/OSAT, customer, market_scope, capacity_scope, package_type/variant, technology_or_process, substrate_material, substrate_type, substrate_format, interposer_type, bridge_type, tgv_via_type, panel_or_wafer_format, package_body_size_mm2, layer_count, line_space_um, ai_package_platform, program_or_platform, period_label/kind such as end-2026, capacity_wpm, capacity_units/unit, utilization, yield_rate, lead_time_weeks, allocation_status, qualification_status/customer, ramp_period, substitution_target/status/risk, price/currency, customer_demand_share_pct, allocation_share_pct, source_role, source_owner, observed/underlying source, bottleneck_model_id, and confidence. Reuse existing TSMC CoWoS propositions when known, but do not drop non-TSMC/OSAT, substrate supplier, glass/TGV, or NVIDIA demand-share rows. Package geometry-to-output conversion factors stay in advanced_package_conversion; substrate material chemistry/consumables stay in 6937; PCB/CCL/glass-cloth laminate stays in 6898.
- For CoWoS package geometry and WPM-to-output conversion, prefer dataset_materializations family=advanced_package_conversion. Use row_kind to separate package_geometry, interposer_substrate_conversion, wpm_to_package_output, and yield_cycle_time. Preserve package_platform, accelerator_vendor, customer_or_program, generation, cowos_variant, interposer_type/area, package_body_size_mm2, reticle_count/stitching, logic_die_count/area, hbm_stack_count, substrate and panel/wafer format, packages/substrates per panel or wafer, good_package_yield, assembly_cycle_time, bonding_dependency, capacity_wpm, qualified_package_output_units, conversion_factor/basis, source owner, and bottleneck-model links. This rail converts capacity headlines into sellable package output; do not use it for customer demand share owned by advanced_packaging_capacity.
- For AI optical-link, InP, laser, MOCVD, silicon-photonics, CPO optical-engine, FAU/fiber-array, lens/micro-optic attach, PIC foundry, package/test/yield, capacity, speed-generation, volume-ramp, and qualification facts, prefer dataset_materializations family=ai_optical_photonics_capacity. Use row_kind to separate inp_laser_capacity, mocvd_capacity, silicon_photonics_pic, cpo_optical_engine, fau_fiber_array, micro_optic_attach, package_test_yield, speed_generation, qualification, and volume_ramp rows. Preserve company, supplier, customer, program_or_platform, switch_platform, optical_link_type, material_system, laser_type, mocvd_tool, mocvd_capacity, epi_wafer_capacity, wafer_size_mm, pic_foundry, process_node, optical_engine_architecture, cpo_status, fau_model, fiber_array_count, lens_micro_optic_attach, package_type, test_capacity_units, yield_rate, capacity_units/unit, port_count, engine_count, speed_generation, bandwidth_gbps, volume_ramp_period, qualification_status/customer, lead_time_weeks, source owner/provenance, document links, supply_chain_id, bottleneck_model_id, knowledge_asset_id, extraction_method, and confidence. Fabric topology, collective communication, congestion, and switch/network utilization stay in 6899; product roadmap/design-win timing stays in 6921; generic semiconductor process/materials such as red phosphorus/InP inputs stay in 6937 or request 928 when they are upstream material constraints.
- For OSAT assembly, package-test, wafer-probe, final-test, burn-in, handlers, sockets/contactors, probe cards, load boards, burn-in boards, and ATE capacity facts, prefer dataset_materializations family=osat_package_test_capacity. Use row_kind to separate package_assembly_capacity, package_test_capacity, wafer_probe_capacity, final_test_capacity, burn_in_capacity, handler_capacity, socket_contactor_capacity, probe_card_capacity, load_board_capacity, burn_in_board_capacity, ate_installed_base, utilization_pricing, qualification, yield_escape_rate, and source_conflict rows. Preserve osat_company, supplier, customer, site/region, ai_platform, package_type, process_node, assembly_wpm, package_test_capacity_units, wafer_probe_capacity_units, final_test_capacity_units, burn_in_capacity_units, handler_capacity_units, socket_contactor_capacity_units, probe_card_capacity_units, load_board_capacity_units, burn_in_board_capacity_units, ate_platform, tester_model, tester_count, capacity_units/unit, utilization, asp/price/currency, lead_time_weeks, qualification_status/customer, yield_rate, escape_rate_pct, metric/value/unit, source owner/provenance, document links, related_request_id, supply_chain_id, bottleneck_model_id, knowledge_asset_id, extraction_method, and confidence. Qualification protocol, yield ramp, and customer acceptance outcomes stay in 6909; package geometry-to-output conversion stays in advanced_package_conversion; process-control/metrology tool capacity stays in 6889; package/rack failure mechanisms and FA lab capacity stay in 7066; field-failure/RMA economics stay in 6988; lot/certificate traceability stays in 7046.
- For qualification milestones, sample status, yield, ramp phase, customer acceptance, blocker reason, and qualification-test protocol facts, prefer dataset_materializations family=qualification_ramp_yield. Use row_kind to separate qualification_milestone, sample_status, yield_ramp, customer_acceptance, test_protocol, stress_condition, pass_fail_result, yield_escape, retest_requalification, and source_conflict rows. Preserve supplier/customer, product, platform_or_program, package_or_device, package_type, device_type, facility/site/region, process_node, qualification_owner/status/stage/milestone/date, sample_status, ramp_phase/stage/start/date/period, customer_acceptance_status, blocker_reason, standard_or_protocol, test_protocol, test_condition, temperature_c, humidity_pct, voltage_v, power_watts, duration_hours/cycles, sample_size, fail_count, pass_fail_status, yield_rate, yield_escape_rate, retest_needed, requalification_needed, qualification_customer, source owner/provenance, observed/underlying source, document links, related_request_id, supply_chain_id, bottleneck_model_id, knowledge_asset_id, product_stage_id, extraction_method, and confidence. In-factory test capacity stays in 6936; field-failure/RMA economics stay in 6988; lot/certificate traceability stays in 7046; mechanism-level package/rack reliability and FA capacity stays in 7066.
- For package and rack thermal-mechanical reliability, field-failure mechanism, and failure-analysis capacity facts, prefer dataset_materializations family=package_rack_reliability_failure_analysis. Use row_kind to separate package_failure_mechanism, rack_failure_mechanism, liquid_cooling_interface_failure, thermal_cycle_failure, field_failure_rate, containment_rework, rma_warranty_linkage, fa_queue_capacity, fa_turnaround, bottleneck_equipment_skill, and source_conflict rows. Preserve company/supplier/customer, platform_or_system, product/model, package_or_device/type, rack_platform, rack_or_cooling_interface, failure_mechanism, failure_location, failure_mode, thermal_cycle_condition, load_condition, qualification_or_field_condition, sample_size, installed_base, fail_count, observed_failure_rate, severity, containment_status/action, rework_status/count, RMA/warranty linkage, FA lab/site, analysis_type, fa_queue_backlog, fa_capacity_samples_per_period, capacity_period, turnaround_days, bottleneck_equipment, bottleneck_skill, source owner/provenance, observed/underlying source, document links, related_request_id, supply_chain_id, bottleneck_model_id, knowledge_asset_id, product_stage_id, extraction_method, and confidence. Material capacity/pricing stays in 6937; qualification protocol/yield/ramp stays in 6909; package-test capacity stays in 6936; generic warranty/RMA economics stays in 6988; lot/certificate traceability stays in 7046; rack liquid-cooling component supply stays in 6882.
- For EV battery, cell-material, BESS, storage deployment, PCS/inverter, duration, orders/backlog, transformer/interconnect dependency, and balance-of-plant facts, prefer dataset_materializations family=ev_battery_storage_capacity. Use row_kind to separate cell_capacity, cell_material_input, ev_pack_capacity, bess_project_capacity, storage_deployment, pcs_inverter, transformer_interconnect_dependency, bop_scope, orders_backlog, supplier_customer_split, qualification, and source_conflict rows. Preserve company/supplier/customer, project_or_site, region/country, end_market, application, battery_product, battery_form_factor, cell_chemistry, cathode/anode/electrolyte/material input and volume/unit, cell_capacity_gwh, pack_capacity_gwh, storage_deployments_mw/mwh, bess_capacity_mw/mwh, duration_hours, pcs_inverter_type/supplier/capacity/units, transformer_dependency, interconnect_dependency/status, balance_of_plant_scope, capacity_units/unit, production_units, utilization, orders/backlog value and units, supplier/customer share, qualification_status, ramp_period, source owner/provenance, observed/underlying source, document links, related_request_id, supply_chain_id, bottleneck_model_id, knowledge_asset_id, extraction_method, and confidence. Data-center site-level BESS and onsite-generation equipment stay in 6881; upstream mine/smelter/refinery feedstock projects stay in 6966; commodity prices/spreads stay in 6916; rare-earth magnets stay in 6959.
- For rare-earth permanent magnet, NdPr, dysprosium, terbium, magnet alloy, and magnet-material supply facts, prefer dataset_materializations family=rare_earth_magnet_material_supply. Use row_kind to separate rare_earth_material_capacity, magnet_capacity, magnet_production, quota_license, price, supplier_customer_split, qualification, robotics_ev_defense_demand, and source_conflict rows. Preserve company/supplier/customer, customer_or_program, end_market/application, country/region, material_family, rare_earth_element, material_or_product, product_form, magnet_type, magnet_grade, magnet_capacity_tons, magnet_production_tons, material_capacity_tons, material_production_tons, utilization, price/currency/price_unit, quota_status, license_status, export_license_status, country_quota_volume/unit, supplier/customer share, qualification_status/customer, ramp_period, constraint_reason, source owner/provenance, observed/underlying source, document links, related_request_id, supply_chain_id, bottleneck_model_id, knowledge_asset_id, extraction_method, and confidence. Upstream mine/smelter/refinery project capacity stays in 6966; commodity prices/spreads stay in 6916 unless tied to a magnet/material supply fact; EV battery cells stay in 6931; robot actuator/component content stays in 6954/7048/7060 as applicable.
- For HBM front-end wafer capacity and DRAM/NAND/HBM undersupply windows, prefer dataset_materializations family=memory_supply_demand_window. Use row_kind to separate hbm_front_end_wafer_capacity, memory_undersupply_window, memory_profit_cycle_timing, and source_conflict rows. Preserve company/supplier, memory_product, memory_generation, region_or_scope, application_or_end_market, capacity_scope, period_label/kind, capacity_wpm or capacity_low/high_wpm, start_period, end_period, undersupply_until, supply_demand_state, tightness_direction, profit_peak_not_before_period, cycle_phase, cycle_timing_basis, demand_driver, source_role, source_owner, observed/underlying source, estimate_owner, knowledge_asset_id, bottleneck_model_id, supply_chain_id, proposition_id, related_request_id, extraction_method, and confidence. Keep DRAM/NAND/HBM pricing in pricing_supply_demand, HBM qualification/allocation in qualification/allocation rails, and HBM stack conversion in hbm_stack_conversion.
- For HBM stack architecture, known-good-die economics, die-to-wafer conversion, bonding/TSV stack yield, and accelerator HBM attach, prefer dataset_materializations family=hbm_stack_conversion. Use row_kind to separate stack_architecture, die_wafer_kgd_conversion, stack_yield_conversion, accelerator_hbm_attach, and source_conflict rows. Preserve supplier, hbm_generation, stack_height, core_die_count, base_die_process_node/source, bits_per_core_die, die_size_mm2, wafer_size_mm, gross_dies_per_wafer, known_good_die_yield, kgd_required_per_stack, bonding_or_tsv_yield, final_stack_yield, qualified_stack_output, capacity_per_wpm, stack_capacity_gb, bandwidth_per_stack, power_or_thermal_spec, accelerator_vendor, platform_or_gpu_asic, accelerator_generation, package_type/platform, cowos_or_packaging_dependency, customer, stacks_per_accelerator, total_hbm_capacity_per_accelerator_gb, supplier_split_pct, ramp_period, constraint_reason, source owner, bottleneck/model/supply-chain links, extraction_method, and confidence. Do not use this rail for plain HBM WPM/undersupply windows owned by memory_supply_demand_window or generic package geometry owned by advanced_package_conversion.
- For HBM supplier qualification and customer allocation facts, prefer dataset_materializations family=hbm_supplier_qualification_allocation. Use row_kind to separate supplier_qualification, customer_allocation, ramp_schedule, qualified_capacity, committed_volume, and source_conflict rows. Preserve supplier, customer, hbm_generation, product, accelerator_vendor/platform, qualification_status/stage/milestone/date, ramp_start_date, ramp_date, ramp_period, ramp_status, allocation_status, allocation_amount/unit, allocation_share_pct, customer_allocation_pct, committed_volume/unit, qualified_capacity_units/unit, constraint_reason, metric/value/unit, source owner/provenance, document links, related_request_id, supply_chain_id, bottleneck_model_id, knowledge_asset_id, extraction_method, and confidence. HBM front-end WPM and memory undersupply windows stay in memory_supply_demand_window; stack architecture and attach conversion stay in hbm_stack_conversion; generic qualification protocol/test-condition detail stays in 6909; HBM price/ASP stays in pricing_supply_demand.
- For memory site/program capacity expansion, technology ramps, capacity states, and bit-mix or product-share facts, prefer dataset_materializations family=memory_site_program_ramp. Use row_kind to separate canonical_capacity_state, facility_program_expansion, technology_ramp_status, bit_mix_share, product_share, schedule_change, and source_conflict rows. Preserve company/ticker, site/facility/fab/phase/building_or_cleanroom, program/country_or_region, product purpose/family, technology/process_node/eligible_node_set/generation, capacity_type/state, gross_nameplate_wpm, equipped_wpm, eligible_wpm, qualified_wpm, utilized_wpm, allocated_wpm, capacity low/high, utilization/ramp factor, wafer/die/yield/good-bit conversion inputs, stage/milestone and construction/equipment/risk-production/qualification/ramp timing, schedule/conflict status, share type/basis, bit/product shares, capex/currency, assumption lineage/scenario/vintage, source ownership, document and model links, extraction_method, and confidence. Never collapse shell, equipped, eligible, qualified, utilized, allocated, and good-bit capacity into one capacity_value. Use this for Micron-style Tongluo, Idaho, New York, Singapore, 1-gamma DRAM, G9 NAND, HBM4/HBM4E, and data-SSD-share facts; generic facility capex/build-time without memory-program specificity stays in facility_capex_ramp.
- For AI infrastructure spending, hyperscaler capex/facility spend, Gartner AI spending splits, NVIDIA data-center/networking revenue, and product revenue outlooks such as Vera CPU sales, prefer dataset_materializations family=ai_infrastructure_spending. Use row_kind to separate ai_infrastructure_spending_estimate, hyperscaler_capex, ai_spending_split, nvidia_segment_revenue, and product_revenue_outlook rows. Preserve company_or_group, company/ticker, scope, spend_type, segment, product, metric, value/range, unit/currency, growth_yoy_pct, growth_qoq_pct, infrastructure_split_pct, facility_spend, ai_spending, ai_infrastructure_spending, revenue, revenue_outlook, source_role, source_owner, observed/underlying source, estimate_owner, speaker, translation_source_language/status, source_document_id, research_report_id, related_request_id, extraction_method, and confidence. Keep translated-source provenance when extracting document 45368-style Korean articles; do not bury $3T-$4T AI infrastructure ranges or Vera CPU revenue outlooks in prose.
- For end-market device shipments, installed base, inventory/replacement cycles, automotive/ADAS demand bridges, AI PC/smartphone NPU/TOPS attach, and edge-AI semiconductor content, prefer dataset_materializations family=end_market_device_demand. Use row_kind to separate device_shipments, installed_base, inventory_cycle, automotive_content_bridge, edge_ai_npu_attach, ai_pc_smartphone_content_bridge, and edge_workload_bridge rows. Preserve device_category/subcategory, oem_or_market_scope, vendor_or_supplier, region, sell_in_or_sell_through, shipments_units, installed_base, inventory_units/days, replacement_cycle_months, vehicle_category, powertrain, saar_or_production_units, sales_units, ADAS level/feature, semiconductor_content_per_device/value, EV inverter or power-module content, SiC/IGBT adoption, processor_or_soc, npu_or_accelerator_architecture, claimed_tops, sustained_tops_or_real_workload_metric, memory_capacity_gb, memory_bandwidth/unit, on_device_model_class, local_inference_workload, ai_feature_attach_rate, processor_asp_or_content_value, memory_content_value, power_thermal_constraint, software_runtime_or_api, source owner/provenance, document/request links, extraction_method, and confidence. Separate raw TOPS marketing claims from sustained workload performance and from actual unit shipments or attach rates.
- For AI model performance, architecture efficiency, context windows, benchmark/capability results, token/API pricing, estimated inference cost, and cloud GPU rental facts, prefer dataset_materializations family=ai_model_performance_economics. Use row_kind to separate model_benchmark, architecture_efficiency, token_api_pricing, inference_cost, cloud_gpu_rental, and availability rows. Preserve model_provider, model_name, model_family, architecture_family, modality, parameter_count, active_parameters, context_window_tokens, attention_or_sequence_algorithm, time_complexity, memory_complexity, benchmark, benchmark_score/unit, capability_result, task_horizon, token_api_price_input/output, pricing_unit, estimated_inference_cost, cost_unit, cloud_provider, gpu_or_instance, rental_price/unit, region, availability_status, utilization, contract_terms, source owner/provenance, document/request links, extraction_method, and confidence. Keep source-owned cost/performance facts separate from application-usage token volume, inference-runtime internals, and accelerator hardware-spec facts.
- For cloud consumption, committed-spend, capacity-utilization, available GPU capacity, service/SKU, region, backlog, and AI workload revenue/mix facts, prefer dataset_materializations family=cloud_capacity_economics. Use row_kind to separate consumption_revenue, committed_spend, capacity_utilization, available_gpu_capacity, backlog, and workload_mix rows. Preserve cloud_provider, customer_or_tenant, service, service_sku, region, availability_zone, workload_type, ai_workload_mix_pct, consumption_revenue, committed_spend, remaining_commitment, backlog, capacity_units/unit, contracted/reserved/allocated/available capacity units, available_gpu_capacity, utilization/basis, accelerator_vendor/model/generation, gpu_or_instance, contract dates, source owner/provenance, document/request links, extraction_method, and confidence. Route physical cluster commissioning, site/customer acceptance, GPU-online counts, and schedulable-capacity conversion to 7034 rather than this commercial rail.
- For AI workload-to-compute conversion facts, prefer dataset_materializations family=ai_workload_compute_demand. Use row_kind to separate usage_to_tokens, context_distribution, runtime_conversion, gpu_hour_demand, memory_power_demand, latency_capacity_requirement, and scenario rows. Preserve application_or_workload, provider_or_operator, customer_or_tenant, model provider/name/family, architecture_family, active_parameters, modality, request/user/agent count, input/output/total tokens, tokens_per_request, context_length_distribution and p50/p95/max, reasoning_steps, agent_steps, tool_call_count, retrieval_read_count, batch_size, concurrency, prefill/decode share, kv_cache_bytes, memory_footprint_gb, hbm_bandwidth/unit, accelerator_type/model, accelerators_per_replica, tokens_per_second, requests_per_second, gpu_hours or accelerator_hours, utilization, power_per_token, power_mw, online_capacity_required/unit, latency_ms, SLA target, region/cloud, source owner/provenance, document/request links, extraction_method, and confidence. Raw app usage stays in 6993, runtime implementation details stay in 7023, token/API price and cost stay in 6938, cloud commercial capacity stays in 6951, and physical online capacity stays in 7034.
- For AI accelerator installed-base, fleet vintage, useful-life, retirement, redeployment, resale, depreciation, and effective-supply facts, prefer dataset_materializations family=ai_accelerator_fleet_supply. Use row_kind to separate installed_base, useful_life_change, redeployment_retirement, resale_secondary_market, depreciation, restriction_derate, and effective_supply_conversion rows. Preserve operator_or_owner, cloud_provider, customer_or_tenant, accelerator vendor/model/generation, platform_or_system, region/site, deployment_vintage, shipped/installed/online/schedulable/available units, redeployed/retired/refurbished/resold units, fleet_age_months, old/new useful_life_months, depreciation_schedule, residual_value, secondary_market_price, cloud_rental_availability, lease_term_months, utilization, failure_mtbf_hours, software_support_eol_date, export_geofence_restriction, power_cooling_derate_pct, effective_accelerator_supply/unit, generation_equivalent_factor, conversion_method, source owner/provenance, document/request links, extraction_method, and confidence. New production stays in ai_accelerator_production_units or custom_ai_accelerator_program; site acceptance and online cluster conversion stay in 7034.
- For AI accelerator hardware specifications, generation-equivalence, and workload-normalized performance facts, prefer dataset_materializations family=ai_accelerator_hardware_spec. Use row_kind to separate hardware_spec, memory_io_spec, precision_performance, workload_throughput, generation_equivalence, power_cooling, and software_dependency rows. Preserve accelerator vendor/model/generation/codename, platform_or_system, customer_or_operator, process_node, chiplet_count, package_form_factor, host_cpu_dependency, HBM generation/stack count/capacity/bandwidth, interconnect type/bandwidth, PCIe/CXL generation, precision_mode, dense/sparse performance and unit, benchmark_or_workload, tokens_per_second, training/inference throughput, latency and batching context, baseline_accelerator, equivalence_factor/method, TDP/board power/rack power density, cooling_requirement, perf_per_watt, perf_per_dollar, software_stack_dependency, launch/availability period, source owner/provenance, document/request links, extraction_method, and confidence. Production units stay in ai_accelerator_production_units; fleet stock stays in ai_accelerator_fleet_supply; workload demand conversion stays in ai_workload_compute_demand.
- For AI model development, evaluation, training-data rights, labeling labor, synthetic data, post-training compute, and physical-AI data-flywheel facts, prefer dataset_materializations family=ai_model_development_eval. Use row_kind to separate data_rights_deal, annotation_labor, synthetic_data, post_training_compute, evaluation_result, simulation_teleoperation, and robot_feedback_flywheel rows. Preserve model_developer, model_name/family, development_stage, training_data_source, data_modality, data_rights_status, content_owner, license_deal_value, license_term_months, annotation_workload, rlhf_task_count, expert_labeling_hours, expert_labor_capacity/unit, synthetic_data_vendor, synthetic_data_volume/unit, synthetic_scenario_count, post_training_compute_hours/unit, eval_protocol, eval_benchmark, eval_score/unit, simulation_hours, teleoperation_hours, robot_fleet_feedback_units, data_flywheel_metric, compute_demand_bridge, source owner/provenance, document/request links, extraction_method, and confidence. Legal disputes stay in legal_regulatory_event, source-access rights in source_access_rights, app usage in ai_application_usage, and workload compute conversion in ai_workload_compute_demand.
- For AI inference runtime, model-serving, KV-cache, context memory, batching, prefill/decode, latency, QPS, and runtime-fee facts, prefer dataset_materializations family=ai_inference_runtime. Use row_kind to separate serving_stack, context_distribution, kv_cache, context_memory_tier, batching_throughput, rag_lookup, utilization_power, and runtime_fee rows. Preserve provider_or_operator, application_or_workload, model provider/name/family, serving_framework, runtime_stack, context_length_distribution and p50/p95/max, prefill/decode share, batch_size, concurrency, kv_cache_bytes, kv_cache_location, kv_cache_hit_rate, context_memory_storage_tier, rag_vector_lookups, latency_ms, tokens_per_second, qps, utilization, power_per_token, software_runtime_fee/unit, deployment_mode, region/cloud, accelerator_model, source owner/provenance, document/request links, extraction_method, and confidence. Conversion from runtime facts into GPU-hours, memory, power, and online capacity belongs in ai_workload_compute_demand.
- For AI storage hierarchy, enterprise SSD, NAND data-center bit mix, nearline HDD/HAMR, QLC/TLC, controller attach, GPU-direct storage, CXL, and HBF facts, prefer dataset_materializations family=ai_storage_hierarchy. Use row_kind to separate enterprise_ssd, nand_bit_mix, nearline_hdd, storage_attach, gpu_direct_storage, cxl_memory_expansion, hbf_memory_expansion, pricing_lead_time, and qualification rows. Preserve company, supplier, customer, product/storage_product, storage_tier, form_factor, interface, memory_or_storage_type, NAND generation and bit mix, QLC/TLC mix, enterprise_ssd_units, enterprise_ssd_capacity_tb, enterprise_ssd_revenue, nearline_hdd_exabytes, HAMR/ePMR status, controller vendor/attach, storage_per_ai_server_tb, gpu_direct_storage_status, CXL program status/capacity/latency, HBF program status/capacity/bandwidth, ASP/price, lead_time_weeks, qualification_status/customer, capacity/utilization, source owner/provenance, document/request links, extraction_method, and confidence. Runtime KV-cache mechanics stay in ai_inference_runtime; workload-to-storage demand conversion stays in ai_workload_compute_demand.
- For cybersecurity vulnerability, exploit, breach, ransomware, and security-spend impact facts, prefer dataset_materializations family=cybersecurity_incident_metric. Use row_kind to separate vulnerability_count, cve_exploit, breach_event, ransomware_incident, remediation_cost, and security_spend_impact rows. Preserve company, affected_vendor, affected_product, affected_target, incident_type, attack_vector, cve_id, vulnerability_severity, vulnerability_count, severe_vulnerability_count, high_vulnerability_count, exploit_status, breach_count, records_exposed, ransomware_incident_count, incident_start_date, disclosure_date, patch_status, remediation_cost, security_spend_impact, source owner/provenance, document/request links, extraction_method, and confidence. Cloud/data-center operational capacity incidents stay in ai_cloud_incident.
- If the correct destination rail does not exist or the metric vocabulary cannot represent the fact, emit research_requests with intent missing_research_rail/schema_gap/data_series_needed instead of dropping the signal.
- Use percent_bounded only for nonnegative 0-100% measures such as market share, mix, penetration, attach rate, yield, or utilization. Use percent_unbounded for growth, price changes, YoY/QoQ, CAGR, or cumulative percentage moves that can exceed 100% or be negative.
- Do not create a new canonical subject for abbreviations or chemical/material names. If the context does not resolve a term such as InP/indium phosphide, preserve raw_subject_text and set requires_resolution=true in model_delta_candidates.
- If a model delta clears the durable evidence bar, also emit analyst_evidence with the same metric_key/value/period fields so the server can materialize it into institutional observations and state vectors.
- Use dataset_rows only when updating rows for a known dataset_id/id. Use dataset_materializations when a supported dataset family can store the sourced rows through the family materializer. Use supply_chain_metric_bindings only when binding a known supply chain id, dataset_id, and metric_kind/metric_key. Use instrument_request_fulfillments only when the requested work has actually been implemented and verified. Include confidence on these durable outputs.
- If a dataset, supply-chain, or instrument-request durable target is missing required IDs or schema support, emit a research_request instead of an incomplete write candidate.
- Catalyst writes require a real event or deadline. Investment memo writes require actual memo content, not an empty shell.
- Evidence links may omit target ids for newly written objects; the worker will link successful writes back to the source document.
- Inspect run_durable_progress when present. It lists durable candidates already materialized or skipped in this worker run. Do not repeat those objects; continue with the next highest-value new signal from the current document. If the current document has more durable-worthy signal than fits in one response, prefer analyst_evidence/model_delta_candidates over prose writes and set decision_review.next_pass_needed=true with a concise remaining_signal_summary.
- Keep output compact by default for routine documents. For dense sell-side/model-delta documents with multiple explicit metrics, periods, values, source-chain items, dataset rows, or supply-chain bindings, emit up to 12 analyst_evidence items, 12 model_delta_candidates, 8 dataset_rows, 4 dataset_materializations, 4 supply_chain_metric_bindings, 4 instrument_request_fulfillments, and 6 research_requests if each clears identity, confidence, novelty, and value/status or missing-rail requirements. If material facts remain, set decision_review.next_pass_needed=true and summarize the remaining fact classes.
- Do not write notes, theses, executive memory documents, portfolio state, or IBKR state.
- Do not provide investment advice or portfolio actions.

Return JSON only:
{
  "decision_review": {
    "summary": "short decision-spine review",
    "blockers": ["why no stronger durable action was possible"],
    "confidence": 0.0
  },
  "tool_requests": [
    {"query": "compact search terms for needed durable dataset tools", "names": ["optional_exact_tool_name"]}
  ],
  "decision_targets": [
    {"ticker": "ACHIP", "company_id": 1, "issuer_id": 2, "subject": "Analyst Chip", "confidence": 0.0, "rationale": "why this is the target"}
  ],
  "analyst_evidence": [
    {
      "ticker": "ACHIP",
      "company_id": 1,
      "evidence_kind": "price_target|rating_change|estimate|media_claim|analyst_note",
      "source_name": "analyst or outlet",
      "author": "author if known",
      "outlet": "outlet if known",
      "observed_source_name": "wrapper/publisher name if different",
      "observed_source_handle": "handle if from X or another social rail",
      "observed_actor_type": "person|firm|publication|account|unknown",
      "underlying_source_name": "original note, broker, desk, or cited research source",
      "underlying_actor_type": "firm|research_org|publication|data_vendor|unknown",
      "underlying_firm": "sell-side firm or research organization if known",
      "estimate_owner": "firm/research organization/person that owns this estimate, e.g. UBS or Morgan Stanley",
      "estimate_owner_type": "sell_side|buy_side|issuer|media|specialist_research|unknown",
      "source_chain": [{"role": "observed_publisher|visible_watermark|source_line|underlying_research|ingested_document", "name": "source", "handle": "optional", "firm": "optional", "extraction_method": "text|ocr|image_watermark|manual"}],
      "source_affiliation": {"person_handle": "optional", "person_name": "optional", "firm": "optional", "role_class": "research_analyst|analyst|unknown", "side": "sell_side|buy_side|specialist_research|independent|unknown", "start_date": "YYYY-MM-DD if explicit", "end_date": "YYYY-MM-DD if explicit", "evidence_note": "why this affiliation is proven"},
      "action": "raised|lowered|initiated|reiterated|claim",
      "rating": "rating if explicit",
      "prior_rating": "prior rating if explicit",
      "price_target": 0.0,
      "prior_price_target": 0.0,
      "currency": "USD",
      "estimate_metric": "metric if explicit",
      "estimate_period": "period if explicit",
      "estimate_value": 0.0,
      "estimate_unit": "unit if explicit",
      "forecast_value": 0.0,
      "prior_forecast_value": 0.0,
      "period_basis": "FY|CY|full_year|quarter if explicit",
      "metric_value_type": "unit_count|quantity|money|percent_bounded|percent_unbounded|percentage",
      "actual_value": 0.0,
      "actual_date": "YYYY-MM-DD if known",
      "scenario": "scenario/mix case if explicit",
      "summary": "source-backed analyst/media claim",
      "confidence": 0.0,
      "evidence_tier": "analyst_commentary|credible_media|company_disclosure|primary_source|social|unknown",
      "source_kind": "source kind",
      "rationale": "why this belongs in analyst evidence"
    }
  ],
  "model_delta_candidates": [
    {
      "raw_subject_text": "raw product/material/capacity subject",
      "canonical_subject_id": 123,
      "canonical_subject_key": "research subject key if matched",
      "requires_resolution": false,
      "estimate_owner": "firm/research organization/person that owns this estimate if applicable",
      "underlying_firm": "sell-side firm or research organization if known",
      "canonical_metric_key": "installed_capacity|effective_capacity|capex|tam|sam|som|utilization|yield_rate|market_share|lead_time_days|lead_time_weeks|inventory_days|allocation_status|qualification_status|demand_signal|schedule_status|token_usage",
      "fact_class": "capacity|capex|facility_timing|production|supply_demand|pricing|market_share|bit_mix|technology_ramp|qualification|revenue|earnings|margin|bottleneck|other",
      "metric_value_type": "quantity|money|percent_bounded|percent_unbounded|days|weeks|status",
      "value_number": 0.0,
      "value_text": "status value if nonnumeric",
      "unit": "unit if explicit",
      "currency": "USD if explicit",
      "product_family": "product family/generation if explicit",
      "segment": "customer/geography/business segment if explicit",
      "period": "raw period text",
      "period_basis": "FY|CY|quarter|half|point_in_time if explicit",
      "period_year": 2026,
      "period_quarter": 1,
      "period_half": 1,
      "as_of_date": "YYYY-MM-DD if explicit",
      "summary": "source-backed model delta",
      "confidence": 0.0,
      "source_document_id": 123,
      "rationale": "why this is a durable model input or why it needs resolution"
    }
  ],
  "catalysts": [
    {
      "ticker": "ACHIP",
      "company_id": 1,
      "title": "specific catalyst title",
      "catalyst_kind": "earnings|filing|product|customer|capacity|regulatory|ipo|manual",
      "event_date": "YYYY-MM-DD",
      "event_at": "ISO timestamp if known",
      "status": "watch|confirmed|done|cancelled",
      "priority": 5,
      "summary": "why this is a catalyst",
      "confidence": 0.0,
      "evidence_tier": "analyst_commentary|credible_media|company_disclosure|primary_source|social|unknown",
      "source_kind": "source kind",
      "rationale": "why this belongs in the catalyst calendar"
    }
  ],
  "investment_memos": [
    {
      "ticker": "ACHIP",
      "company_id": 1,
      "title": "memo title",
      "summary_md": "concise markdown memo update",
      "thesis_md": "only if the source updates thesis context",
      "valuation_md": "only if the source updates valuation context",
      "catalysts_md": "only if the source updates catalyst context",
      "risks_md": "only if the source updates risk context",
      "conviction": "watch|low|medium|high if explicit",
      "priority": 5,
      "inclusion_reason": "why this object belongs in a universe if explicit",
      "active_thesis": "active thesis note if explicit",
      "next_catalyst": "next catalyst if explicit",
      "missing_data_warnings": ["specific missing-data warning"],
      "confidence": 0.0,
      "rationale": "why this memo update is warranted"
    }
  ],
  "research_requests": [
    {
      "title": "missing rail or research follow-up",
      "intent": "missing_research_rail|schema_gap|data_series_needed|object_resolution|bottleneck_model_gap",
      "summary": "what cannot be stored yet or what follow-up is needed",
      "target_entities": ["entity names"],
      "target_themes": ["capacity|capex|consensus|bottleneck|other"],
      "desired_outputs": ["dataset|data_series|company_section|supply_chain_node|bottleneck_model|research_report_section|schema_rail"],
      "evidence_bundle": [{"document_id": 123, "summary": "source evidence proving the gap"}],
      "priority": 6,
      "rationale": "why the rail/request is needed"
    }
  ],
  "dataset_rows": [
    {
      "id": 123,
      "dataset_id": 123,
      "rows": [{"row_key": "stable row key", "values": {"period_date": "YYYY-MM-DD", "metric": 0.0}}],
      "columns": [],
      "label": "optional draft label",
      "confidence": 0.0,
      "rationale": "why these rows are source-backed"
    }
  ],
  "dataset_materializations": [
    {
      "family": "__DATASET_MATERIALIZATION_FAMILIES__",
      "slug": "optional stable dataset slug",
      "title": "optional dataset title",
      "rows": [{"row_key": "stable row key", "period_date": "YYYY-MM-DD"}],
      "provenance": {"provenance_summary": "source-backed materialization rationale"},
      "links": [{"object_type": "instrument_request|company|supply_chain|note|thesis", "object_id": 123, "relation": "context"}],
      "confidence": 0.0,
      "rationale": "why this family materialization is warranted"
    }
  ],
  "supply_chain_metric_bindings": [
    {
      "id": 123,
      "supply_chain_id": 123,
      "dataset_id": 456,
      "dataset_view_id": 789,
      "scope_type": "chain|node|edge",
      "scope_key": "chain or node/edge key",
      "metric_kind": "canonical metric kind",
      "column_key": "dataset column key",
      "aggregation": "latest|sum|avg|max|min",
      "confidence": 0.0,
      "rationale": "why this dataset column should bind to the supply-chain metric"
    }
  ],
  "instrument_request_fulfillments": [
    {
      "id": 123,
      "instrument_request_id": 123,
      "fulfillment_summary": "what was implemented or verified",
      "fulfilled_by": "local_agent_worker",
      "live_payload_patch": {},
      "linked_objects_patch": {},
      "evidence_bundle_patch": [{"document_id": 123, "summary": "verification evidence"}],
      "verification": {"tests_passed": true},
      "confidence": 0.0,
      "rationale": "why this request can be closed"
    }
  ],
  "research_writes": [
    {
      "subject": "known entity or report subject",
      "write_summary": "short description",
      "append_md": "- dated bullet(s) to append",
      "source_document_ids": [123],
      "materiality": "high|medium",
      "novelty": "new|update|conflict|outdated",
      "temporal_posture": "forward_looking|current_state|mixed",
      "evidence_tier": "primary_source|company_disclosure|credible_media|analyst_commentary|social|unknown",
      "new_information": "specific new fact that clears the durable write bar",
      "rationale": "why this durable write clears the bar",
      "confidence": 0.0
    }
  ],
  "evidence_links": [
    {
      "target_type": "investment_catalyst|investment_memo|analyst_evidence|research_report",
      "target_id": 123,
      "evidence_object_type": "document",
      "evidence_object_id": 456,
      "relation": "source|supports|contradicts|updates",
      "summary": "why the link matters",
      "confidence": 0.0
    }
  ]
}
"""


DEEP_ANALYSIS_PROMPT = """You are the Thoth Terminal idle deep-analysis lane.

You are noninteractive. Analyze one source document that already has a signal extraction to decide whether the earlier pass missed signal that deserves human/Codex review later. This lane does not write durable memory, theses, notes, portfolio state, or research reports. It records a trace for evaluation.

Use the signal extraction as the main input. Use the source excerpt only to clarify ambiguity or recover missed details. Be especially useful for long articles, Substack posts, earnings/transcript-like material, sell-side notes, and documents that may connect to existing semiconductor supply-chain knowledge.

Return JSON only:
{
  "analysis_mode": "idle_deep",
  "signal_level": "none|low|medium|high",
  "signal_score": 0.0,
  "summary_md": "markdown summary for the Agents workspace; write readable analyst prose, not schema narration",
  "key_findings": [
    {"finding": "specific source-backed signal", "why_it_matters": "investment/research relevance", "confidence": 0.0}
  ],
  "knowledge_connections": [
    {"topic": "existing primer/thesis/supply-chain area", "connection": "how this source connects", "confidence": 0.0}
  ],
  "contradictions": [
    {"claim": "possible contradiction or tension", "needs_review": "what human/Codex should check", "confidence": 0.0}
  ],
  "open_questions": [
    {"question": "research question raised by the source", "why": "why this should be revisited"}
  ],
  "followups": [
    {"action": "candidate follow-up for later steward review", "priority": "low|medium|high", "rationale": "why"}
  ]
}
"""


def _strip_static_dataset_tool_lines(prompt: str) -> str:
    lines: list[str] = []
    inserted_marker = False
    for line in str(prompt or "").splitlines():
        if line.startswith("- For ") and "prefer dataset_materializations family=" in line:
            if not inserted_marker:
                lines.append(
                    "- Detailed dataset materialization tool definitions are omitted from this runtime prompt. "
                    "Use tools.registry and tool_requests to discover the relevant tool definitions when needed."
                )
                inserted_marker = True
            continue
        lines.append(line)
    return "\n".join(lines).strip() + "\n"


DECISION_REVIEW_PROMPT = DECISION_REVIEW_PROMPT.replace(
    "__DATASET_MATERIALIZATION_FAMILIES__",
    DATASET_MATERIALIZATION_FAMILY_PROMPT,
)
DATASET_TOOL_SOURCE_PROMPT = DECISION_REVIEW_PROMPT
DECISION_REVIEW_RUNTIME_PROMPT = _strip_static_dataset_tool_lines(DECISION_REVIEW_PROMPT)
_DATASET_TOOL_LINE_RE = re.compile(
    r"^- For (?P<use_when>.+?), prefer dataset_materializations family=(?P<name>[a-z0-9_]+)\. (?P<body>.*)$"
)
_DATASET_TOOL_CARD_CACHE: list[dict[str, Any]] | None = None


MEDIA_OCR_PROMPT = (
    "OCR markdown. Preserve headings, paragraphs, bullets, tables, chart titles, "
    "axis labels, legend labels, source notes, and visible numeric values. Do not infer missing values."
)


MEDIA_OCR_BACKEND_CHOICES = (
    "auto",
    "gguf_cli",
    "glm_sdk_sidecar",
    "vllm_ocr_sidecar",
    "cli",
    "sdk",
    "glm_ocr_cli",
    "glm_ocr_sdk",
    "vllm_ocr",
    "vllm-sidecar",
    "gguf-cli",
    "glm-sdk-sidecar",
)


MEDIA_VISION_PROMPT_VERSION = "visual-signal-v6-non-signal-discard"
MEDIA_VISION_PROMPT = """You are doing visual signal extraction for an investment research pipeline.

Use the image as the source of truth. The OCR text, if provided, is only a noisy first pass.

Your job is not to write a general caption. Convert the visual into raw evidence that a later text-only analyst model can use without seeing the image.
Write all explanatory output in English. If the visual text is non-English, translate labels, titles, units, notes, and useful signal into English while preserving important original labels or names in parentheses when helpful.

Return compact Markdown with these sections:

## Visual Kind
Classify as one of: chart, table, process_flow, technical_diagram, architecture_diagram, text_page, photo_non_signal, mixed. Say whether it is useful signal or non-signal. If the image is a decorative photo, logo, generic product render, portrait, or stock image with no source-backed data, mark photo_non_signal and do not spend tokens trying to infer business meaning.
If the image is noise, corrupted, blank, unreadable, decorative, a profile/avatar/logo, a generic article thumbnail, or otherwise has no useful source-backed investment signal, write exactly: "signal: non_signal" in this section and explain the discard reason briefly. This is a successful visual decision, not a model failure.

## Title Purpose Source
Transcribe the title, subtitle, visible source line, watermark, footnotes, date range, geography, company names, tickers, and the purpose of the visual.

## Axes Units Legend
For charts: identify x-axis, y-axis, units, scale, categories, series, legend mapping, colors, and whether the chart is stacked, grouped, line, bar, scatter, or mixed.
For diagrams/flows: identify components, arrows/relationships, layers, dependencies, timing, comparisons, and direction of dependency or flow.
For tables: preserve rows/columns as Markdown.
For photos: say whether the photo contains source-backed investment signal or is only illustrative.

## Data Readout
List every visible numeric value you can read. For approximate visual values, prefix with "approx.". Preserve units. For stacked bars or shares, break down series by period/category when visually recoverable.

## Structured Readout
When the visual contains chart/table values, first write a compact Markdown table of visible data points with columns: series, metric, category, period, value/value_text, unit, source, approximate. Also attempt a fenced JSON object with key "structured_series" using these keys when known: series, metric, category, period, value, value_text, unit, x_axis, y_axis, source, confidence, approximate. Treat JSON as auxiliary best-effort evidence; the Markdown readout is the source of truth for the downstream text-only durable pass. Do not omit recoverable values just because JSON may be imperfect, and do not invent rows to satisfy JSON.

## Relationships And Trends
Explain the visible relationships: ranking, market-share changes, inflections, bottlenecks, cycle time, growth/decline, dependencies, and what changed over time.

## Useful Signal
Write the durable raw signal a downstream text model should receive. Include quantified claims, relationships, bottlenecks, timelines, market share, revenue, capacity, architecture, process-cycle implications, visible source attribution, and explicit units. Prefer terse evidence bullets over narrative prose.

## Uncertainty
List unreadable areas, approximate values, OCR conflicts, axis/legend ambiguity, missing units, and anything that must not be treated as exact.

Do not invent values or relationships. If a value is approximate from visual reading, mark it as approximate. If the image is decorative/non-signal, say so explicitly."""


@dataclass(frozen=True)
class WorkerConfig:
    thoth_base_url: str
    thoth_token: str
    llama_base_url: str
    model: str
    worker_id: str
    prompt_version: str
    poll_interval_s: float
    batch_size: int
    lookback_hours: int
    queue_strategy: str
    include_opened: bool
    backlog_mode: bool
    include_filings: bool
    target_document_ids: tuple[int, ...]
    auto_write: bool
    once: bool
    timeout_s: float
    llama_disable_thinking: bool
    max_input_chars: int
    max_output_tokens: int
    daily_summary_enabled: bool
    daily_summary_hhmm: str
    daily_summary_lookback_hours: int
    decision_spine_enabled: bool
    decision_max_targets: int
    decision_min_confidence: float
    market_base_currency: str | None
    market_live_quotes: bool
    market_max_items: int
    calendar_days_back: int
    calendar_days_forward: int
    calendar_limit: int
    loadout_preflight: bool
    preflight_ticker: str
    true_batching: bool
    batch_max_input_tokens: int
    batch_max_documents: int
    stage_planning_enabled: bool
    media_transcription_enabled: bool
    media_transcription_only: bool
    media_transcription_limit: int
    media_transcription_mode: str
    media_render_readable_pdf_pages: bool
    media_ocr_concurrency: int
    media_vlm_concurrency: int
    media_sidecar_concurrency_auto: bool
    media_ocr_backend: str
    media_ocr_config: str | None
    media_ocr_api_url: str | None
    media_ocr_mode: str | None
    media_ocr_layout_device: str | None
    media_ocr_vllm_base_url: str | None
    media_ocr_vllm_model: str | None
    media_ocr_cli: str | None
    media_ocr_model: str
    media_ocr_min_chars: int
    media_ocr_timeout_s: float
    media_vision_base_url: str | None
    media_vision_model: str | None
    media_vision_timeout_s: float
    media_vision_disable_thinking: bool
    media_vision_allow_text_endpoint: bool
    media_max_output_tokens: int
    idle_deep_analysis_enabled: bool
    idle_deep_analysis_limit: int
    idle_deep_analysis_lookback_hours: int
    idle_deep_analysis_min_interval_s: float
    idle_deep_analysis_max_text_chars: int
    durable_review_only: bool
    durable_review_limit: int
    durable_review_lookback_hours: int
    durable_review_max_text_chars: int
    harness_run_id: int | None = None


class AgentWorkerError(RuntimeError):
    def __init__(self, message: str, *, failure_type: str | None = None, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.failure_type = _normalize_failure_type(failure_type, default="agent_worker_error") if failure_type else None
        self.detail = dict(detail or {})


def _normalize_failure_type(value: object, *, default: str = "agent_worker_error") -> str:
    raw = str(value or default).strip().lower()
    clean = re.sub(r"[^a-z0-9_]+", "_", raw).strip("_")
    return (clean or default)[:96]


def _error_text_looks_oom(text: object) -> bool:
    blob = str(text or "").lower()
    return any(
        marker in blob
        for marker in (
            "out of memory",
            "cuda malloc",
            "cuda allocation",
            "allocation failed",
            "failed to allocate",
            "not enough memory",
            "ggml_cuda_pool_malloc",
            "cublas status alloc failed",
            "vk_error_out_of_device_memory",
            "outofdevicememory",
        )
    )


def _endpoint_failure_type(
    error_prefix: str,
    exc: Exception | None = None,
    *,
    status_code: int | None = None,
    response_text: str | None = None,
) -> str:
    prefix = str(error_prefix or "").strip().lower()
    text_parts = [prefix, str(status_code or ""), str(response_text or "")]
    if exc is not None:
        text_parts.append(f"{type(exc).__name__}: {exc}")
    text = " ".join(text_parts).lower()
    oom = _error_text_looks_oom(text)
    loading = any(
        marker in text
        for marker in (
            "503",
            "loading",
            "load model",
            "model is being loaded",
            "not ready",
            "no slots available",
            "server busy",
            "temporarily unavailable",
        )
    )
    rotation_not_ready = any(
        marker in text
        for marker in (
            "rotation in progress",
            "connection",
            "max retries",
            "failed to establish",
            "connection refused",
            "actively refused",
            "winerror 10061",
            "not listening",
        )
    )
    corrupt_media = any(
        marker in text
        for marker in (
            "invalid image",
            "image decode",
            "decode image",
            "failed to load image",
            "could not load image",
            "unsupported image",
            "corrupt image",
        )
    )
    if "media vision" in prefix:
        if oom:
            return "media_vlm_oom"
        if "not configured" in text:
            return "media_vlm_endpoint_not_configured"
        if "refusing to send images to the text endpoint" in text:
            return "media_vlm_text_endpoint_refused"
        if corrupt_media:
            return "media_object_corrupt"
        if loading:
            return "media_vlm_loading_in_progress"
        if rotation_not_ready:
            return "media_vlm_rotation_endpoint_not_ready"
        return "media_vlm_endpoint_unavailable"
    if "vllm ocr" in prefix or "ocr sidecar" in prefix:
        if oom:
            return "media_ocr_oom"
        if "not configured" in text:
            return "media_ocr_vllm_not_configured"
        if corrupt_media:
            return "media_object_corrupt"
        if loading:
            return "media_ocr_loading_in_progress"
        if rotation_not_ready:
            return "media_ocr_rotation_endpoint_not_ready"
        return "media_ocr_vllm_endpoint_unavailable"
    if "llama" in prefix:
        if oom:
            return "llama_text_oom"
        if loading:
            return "llama_loading_in_progress"
        if rotation_not_ready:
            return "llama_rotation_endpoint_not_ready"
        return "llama_endpoint_unavailable"
    if oom:
        return "model_oom"
    if loading:
        return "model_loading_in_progress"
    if rotation_not_ready:
        return "model_rotation_endpoint_not_ready"
    return "model_endpoint_unavailable"


def _agent_worker_failure_type(exc: Exception, *, default: str = "agent_worker_error") -> str:
    explicit = getattr(exc, "failure_type", None)
    if explicit:
        return _normalize_failure_type(explicit, default=default)
    text = f"{type(exc).__name__}: {exc}".lower()
    if _error_text_looks_oom(text):
        if "media vision" in text or "vlm" in text or "vision" in text:
            return "media_vlm_oom"
        if "ocr" in text or "mtmd" in text or "image" in text:
            return "media_ocr_oom"
        if "llama" in text or "text" in text or "chat/completions" in text:
            return "llama_text_oom"
        return "model_oom"
    if "gguf ocr cli" in text or "media ocr exited" in text or "llama-mtmd-cli" in text:
        return "media_ocr_gguf_cli_error"
    if "thoth_media_ocr_vllm_base_url" in text:
        return "media_ocr_vllm_not_configured"
    if "glmocr package not importable" in text or "glmocr sdk unavailable" in text:
        return "media_ocr_sdk_unavailable"
    if "media vision requires --media-vision-base-url" in text or "refusing to send images to the text endpoint" in text:
        return "media_vlm_text_endpoint_refused"
    if "media vision endpoint is not configured" in text:
        return "media_vlm_endpoint_not_configured"
    if "media vision" in text and any(
        marker in text
        for marker in (
            "invalid image",
            "image decode",
            "decode image",
            "failed to load image",
            "could not load image",
            "unsupported image",
            "corrupt image",
        )
    ):
        return "media_object_corrupt"
    if "media vision" in text and any(
        marker in text
        for marker in (
            "503",
            "loading",
            "load model",
            "model is being loaded",
            "not ready",
            "no slots available",
            "server busy",
            "temporarily unavailable",
        )
    ):
        return "media_vlm_loading_in_progress"
    if "media vision" in text and ("connection" in text or "max retries" in text or "failed to establish" in text):
        return "media_vlm_rotation_endpoint_not_ready"
    if "connection" in text or "max retries" in text or "failed to establish" in text:
        return "model_rotation_endpoint_not_ready"
    return _normalize_failure_type(default, default=default)


def _is_text_oom_error(exc: Exception) -> bool:
    failure_type = _normalize_failure_type(getattr(exc, "failure_type", "") or "")
    return failure_type in {"llama_text_oom", "model_oom"} or _error_text_looks_oom(f"{type(exc).__name__}: {exc}")


def _deterministic_json_repair(text: str) -> list[str]:
    raw = str(text or "").strip()
    if not raw:
        return []
    candidates: list[str] = []
    repaired = re.sub(r",\s*([}\]])", r"\1", raw)
    repaired = re.sub(
        r'("|\d|true|false|null|[}\]])\s*\n\s*("[^"\n\r]+\"\s*:)',
        r"\1,\n\2",
        repaired,
        flags=re.I,
    )
    repaired = re.sub(r"([}\]])\s*\n\s*([{[])", r"\1,\n\2", repaired)
    if repaired != raw:
        candidates.append(repaired)
    return candidates


def _json_from_text(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        raise AgentWorkerError("model returned empty content")
    candidates = [raw]
    match = re.search(r"\{.*\}", raw, flags=re.S)
    if match and match.group(0) != raw:
        candidates.append(match.group(0))
    for candidate in list(candidates):
        candidates.extend(_deterministic_json_repair(candidate))
    last_exc: Exception | None = None
    non_object_seen = False
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except Exception as exc:
            last_exc = exc
            continue
        if not isinstance(parsed, dict):
            non_object_seen = True
            continue
        return parsed
    if non_object_seen and last_exc is None:
        raise AgentWorkerError("model returned non-object JSON")
    if not match:
        raise AgentWorkerError("model returned non-JSON content") from last_exc
    raise AgentWorkerError(f"model returned malformed JSON: {last_exc}") from last_exc


def _harness_completion_content(cfg: WorkerConfig, payload: dict[str, Any], *, role: str = "text", timeout_s: float | None = None) -> str:
    deadline = min(900.0, max(30.0, float(timeout_s or cfg.timeout_s)))
    body = {
        "request_id": str(uuid.uuid4()), "run_id": cfg.harness_run_id,
        "worker_id": cfg.worker_id, "role": role, "model": payload["model"],
        "messages": payload["messages"], "max_tokens": int(payload.get("max_tokens") or cfg.max_output_tokens),
        "temperature": float(payload.get("temperature", 0.1)), "timeout_s": deadline,
    }
    try:
        response = _THOTH_API_SESSION.post(
            f"{cfg.thoth_base_url.rstrip('/')}/api/local-agent/harness/completions",
            headers=_headers(cfg, run_id=cfg.harness_run_id), json=body, timeout=deadline + 90,
        )
    except requests.RequestException as exc:
        raise AgentWorkerError("Thoth Harness inference is unavailable", failure_type="harness_transport_error") from exc
    try:
        result = response.json()
    except ValueError as exc:
        raise AgentWorkerError("Thoth Harness returned an invalid response", failure_type="harness_transport_error") from exc
    if not isinstance(result, dict):
        raise AgentWorkerError("Thoth Harness returned an invalid response", failure_type="harness_transport_error")
    if response.status_code >= 400:
        detail = result.get("detail") or {}
        message = detail.get("message") if isinstance(detail, dict) else str(detail)
        code = detail.get("code") if isinstance(detail, dict) else None
        raise AgentWorkerError(str(message or "Harness inference failed"), failure_type=code or "harness_inference_error")
    text = result.get("text")
    if not result.get("ok") or not isinstance(text, str) or not text.strip():
        raise AgentWorkerError("Harness inference returned no text", failure_type="harness_empty_response")
    return text


def _headers(cfg: WorkerConfig, *, run_id: int | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {cfg.thoth_token}", "Content-Type": "application/json",
               "X-Thoth-Agent-Inference-Provider": "local"}
    if run_id is not None:
        headers["X-Thoth-Agent-Run-Id"] = str(int(run_id))
    return headers


def _thoth_api_timeout(cfg: WorkerConfig, override_s: float | None = None) -> float:
    if override_s is None:
        return float(cfg.timeout_s)
    return max(1.0, min(float(cfg.timeout_s), float(override_s)))


def _thoth_api_failure_type(exc: Exception | None = None, *, status_code: int | None = None) -> str:
    if isinstance(exc, requests.Timeout):
        return "thoth_api_timeout"
    if isinstance(exc, requests.ConnectionError):
        return "thoth_api_connectivity"
    if status_code in {401, 403}:
        return "thoth_api_auth_error"
    if status_code is not None and status_code >= 500:
        return "thoth_api_server_error"
    if status_code is not None:
        return "thoth_api_http_error"
    return "thoth_api_error"


def _thoth_response_failure_type(response: requests.Response) -> str:
    if response.status_code == 409:
        try:
            detail = response.json().get("detail")
        except (ValueError, AttributeError):
            detail = None
        if isinstance(detail, dict) and detail.get("code") == "local_ingestion_disabled":
            return "local_ingestion_disabled"
    return _thoth_api_failure_type(status_code=response.status_code)


def _api_get(
    cfg: WorkerConfig,
    path: str,
    *,
    run_id: int | None = None,
    params: dict[str, Any] | None = None,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    try:
        resp = _THOTH_API_SESSION.get(
            f"{cfg.thoth_base_url.rstrip('/')}{path}",
            headers=_headers(cfg, run_id=run_id),
            params=params or {},
            timeout=_thoth_api_timeout(cfg, timeout_s),
        )
    except requests.RequestException as exc:
        raise AgentWorkerError(
            f"Thoth GET {path} unavailable: {exc}",
            failure_type=_thoth_api_failure_type(exc),
        ) from exc
    if resp.status_code >= 400:
        raise AgentWorkerError(
            f"Thoth GET {path} HTTP {resp.status_code}: {resp.text[:300]}",
            failure_type=_thoth_response_failure_type(resp),
        )
    data = resp.json()
    if not isinstance(data, dict) or not data.get("ok", False):
        raise AgentWorkerError(f"Thoth GET {path} failed: {data}")
    return data


def _api_post(
    cfg: WorkerConfig,
    path: str,
    payload: dict[str, Any],
    *,
    run_id: int | None = None,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    try:
        resp = _THOTH_API_SESSION.post(
            f"{cfg.thoth_base_url.rstrip('/')}{path}",
            headers=_headers(cfg, run_id=run_id),
            json=payload,
            timeout=_thoth_api_timeout(cfg, timeout_s),
        )
    except requests.RequestException as exc:
        raise AgentWorkerError(
            f"Thoth POST {path} unavailable: {exc}",
            failure_type=_thoth_api_failure_type(exc),
        ) from exc
    if resp.status_code >= 400:
        raise AgentWorkerError(
            f"Thoth POST {path} HTTP {resp.status_code}: {resp.text[:300]}",
            failure_type=_thoth_response_failure_type(resp),
        )
    data = resp.json()
    if not isinstance(data, dict) or not data.get("ok", False):
        raise AgentWorkerError(f"Thoth POST {path} failed: {data}")
    return data


def _api_get_bytes(cfg: WorkerConfig, path: str, *, run_id: int | None = None, params: dict[str, Any] | None = None) -> bytes:
    resp = _THOTH_API_SESSION.get(
        f"{cfg.thoth_base_url.rstrip('/')}{path}",
        headers=_headers(cfg, run_id=run_id),
        params=params or {},
        timeout=cfg.timeout_s,
    )
    if resp.status_code >= 400:
        failure_type = "media_object_missing" if "/media/attachments/" in str(path) and resp.status_code in {404, 410} else "thoth_api_bytes_error"
        if "/media/attachments/" in str(path) and resp.status_code == 415:
            failure_type = "media_object_not_image"
        raise AgentWorkerError(
            f"Thoth GET {path} HTTP {resp.status_code}: {resp.text[:300]}",
            failure_type=failure_type,
        )
    return bytes(resp.content)


def _endpoint_identity(base_url: str) -> str:
    raw = str(base_url or "").strip().rstrip("/")
    if not raw:
        return ""
    parsed = urlparse(raw if "://" in raw else f"http://{raw}")
    scheme = (parsed.scheme or "http").lower()
    host = (parsed.hostname or parsed.netloc or parsed.path or "").lower()
    port = f":{parsed.port}" if parsed.port else ""
    path = str(parsed.path or "").rstrip("/")
    return f"{scheme}://{host}{port}{path}"


def _model_ids_from_status(status: dict[str, Any]) -> list[str]:
    return [
        str(item.get("id") or item.get("object") or "").strip()
        for item in _as_list(status.get("data"))
        if isinstance(item, dict) and str(item.get("id") or item.get("object") or "").strip()
    ][:10]


def _context_tokens_from_status(status: dict[str, Any]) -> int | None:
    candidates: list[Any] = []
    settings = status.get("default_generation_settings") if isinstance(status, dict) else {}
    if isinstance(settings, dict):
        params = settings.get("params") if isinstance(settings.get("params"), dict) else {}
        candidates.append(params.get("n_ctx"))
    for item in _as_list(status.get("data") if isinstance(status, dict) else None):
        if isinstance(item, dict):
            meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
            candidates.append(meta.get("n_ctx"))
    for value in candidates:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return None


def _remember_llama_context(cfg: WorkerConfig, status: dict[str, Any], stats: dict[str, Any] | None = None) -> None:
    context_tokens = _context_tokens_from_status(status)
    if not context_tokens:
        return
    _LLAMA_CONTEXT_TOKENS_BY_ENDPOINT[_endpoint_identity(cfg.llama_base_url)] = int(context_tokens)
    if stats is not None:
        stats["llama_context_tokens"] = int(context_tokens)
        stats["llama_prompt_safe_tokens"] = max(1_200, int(context_tokens * 0.82))


def _check_openai_models_ready(base_url: str, *, timeout_s: float, error_prefix: str) -> dict[str, Any]:
    url = f"{str(base_url or '').rstrip('/')}/v1/models"
    try:
        resp = requests.get(url, timeout=min(max(timeout_s, 5.0), 30.0))
    except requests.RequestException as exc:
        raise AgentWorkerError(
            f"{error_prefix} models endpoint unavailable: {exc}",
            failure_type=_endpoint_failure_type(error_prefix, exc),
        ) from exc
    if resp.status_code >= 400:
        raise AgentWorkerError(
            f"{error_prefix} models HTTP {resp.status_code}: {resp.text[:300]}",
            failure_type=_endpoint_failure_type(error_prefix, status_code=resp.status_code, response_text=resp.text),
        )
    data = resp.json()
    if not isinstance(data, dict):
        raise AgentWorkerError(f"{error_prefix} models endpoint returned non-object JSON")
    models = data.get("data")
    if not isinstance(models, list):
        raise AgentWorkerError(f"{error_prefix} models endpoint returned no model list")
    return data


def _check_llama_ready(cfg: WorkerConfig) -> dict[str, Any]:
    return _check_openai_models_ready(cfg.llama_base_url, timeout_s=cfg.timeout_s, error_prefix="llama.cpp")


def _llama_json(
    cfg: WorkerConfig,
    *,
    system_prompt: str,
    user_prompt: dict[str, Any],
    max_output_tokens_override: int | None = None,
) -> dict[str, Any]:
    raw_user_content = json.dumps(user_prompt, ensure_ascii=False)
    initial_limit = min(len(raw_user_content), _llama_safe_user_content_chars(cfg, system_prompt))
    caps = [initial_limit]
    for ratio in (0.72, 0.55, 0.4, 0.3):
        cap = max(800, int(initial_limit * ratio))
        if cap and cap not in caps and cap < initial_limit:
            caps.append(cap)
    max_output_tokens = _llama_safe_output_tokens(cfg)
    if max_output_tokens_override is not None:
        max_output_tokens = max(512, min(max_output_tokens, int(max_output_tokens_override)))
    last_prompt_size_error: AgentWorkerError | None = None
    for idx, cap in enumerate(caps or [0]):
        user_content = raw_user_content[:cap] if cap else ""
        payload = {
            "model": cfg.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.1,
            "max_tokens": max_output_tokens,
        }
        try:
            content = _harness_completion_content(cfg, payload)
        except AgentWorkerError as exc:
            if (_is_context_limit_error(exc) or _is_text_oom_error(exc)) and idx + 1 < len(caps):
                last_prompt_size_error = exc
                continue
            raise
        try:
            return _json_from_text(content)
        except AgentWorkerError as first_error:
            repair_payload = {
                "model": cfg.model,
                "messages": [
                    {"role": "system", "content": "Repair the previous assistant output into one valid JSON object. Return JSON only. Do not add prose."},
                    {"role": "user", "content": content[: max(1000, max_output_tokens * 8)]},
                ],
                "temperature": 0.0,
                    "max_tokens": max_output_tokens,
                    }
            try:
                repair_content = _harness_completion_content(cfg, repair_payload)
                return _json_from_text(repair_content)
            except AgentWorkerError as repair_error:
                retry_payload = {
                    "model": cfg.model,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                f"{system_prompt}\n\nYour last JSON response and the repair attempt failed parsing. "
                                "Regenerate the answer as exactly one syntactically valid JSON object. "
                                "Do not include markdown, comments, or trailing commas."
                            ),
                        },
                        {"role": "user", "content": user_content},
                    ],
                    "temperature": 0.0,
                            "max_tokens": max_output_tokens,
                                }
                try:
                    retry_content = _harness_completion_content(cfg, retry_payload)
                    return _json_from_text(retry_content)
                except AgentWorkerError as retry_error:
                    raise AgentWorkerError(f"{first_error}; repair failed: {repair_error}; retry failed: {retry_error}") from retry_error
    if last_prompt_size_error is not None:
        raise AgentWorkerError(
            f"llama JSON prompt still exceeded context after adaptive shrinking: {last_prompt_size_error}",
            failure_type="llama_context_limit_after_prompt_retries",
        ) from last_prompt_size_error
    raise AgentWorkerError("model JSON call failed before request")


def _is_context_limit_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "exceeds the available context size" in text or "exceed_context_size" in text or "context size" in text


def _is_model_parse_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "malformed json" in text or "non-json content" in text or "non-object json" in text


def _signal_result_blob(result: dict[str, Any]) -> str:
    items: list[str] = []
    for key in ("summary_short", "summary_detail", "information_gain_reason", "skip_reason"):
        if result.get(key):
            items.append(str(result.get(key)))
    for key in (
        "atomic_claims",
        "summary_routes",
        "durable_routes",
        "research_requests",
        "analyst_evidence",
        "model_delta_candidates",
    ):
        for row in _as_list(result.get(key)):
            if row:
                items.append(json.dumps(row, ensure_ascii=False, default=str) if isinstance(row, dict) else str(row))
    return "\n".join(items)


def _coverage_term_present(term: str, blob: str) -> bool:
    if not term:
        return True
    if term == "JPY capex":
        return bool(re.search(r"\bJPY\b", blob, flags=re.IGNORECASE) and re.search(r"\bcap(?:ex|ital investment)\b", blob, flags=re.IGNORECASE))
    return re.search(r"\b" + re.escape(term).replace(r"\ ", r"\s+") + r"\b", blob, flags=re.IGNORECASE) is not None


def _missing_source_coverage_terms(result: dict[str, Any], hints: dict[str, Any]) -> list[str]:
    required = set(SOURCE_COVERAGE_REQUIRED_OUTPUT_TERMS)
    hinted = _dedupe_text_items([*_as_list(hints.get("terms")), *_as_list(hints.get("source_owners"))])
    blob = _signal_result_blob(result)
    missing = [term for term in hinted if term in required and not _coverage_term_present(term, blob)]
    return missing[:6]


def _merge_signal_results(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base or {})
    for key in ("summary_short", "summary_detail", "information_gain", "information_gain_reason", "document_disposition", "signal_profile"):
        if extra.get(key) and not merged.get(key):
            merged[key] = extra.get(key)
    for key in (
        "atomic_claims",
        "entity_refs",
        "supply_chain_refs",
        "technology_refs",
        "summary_routes",
        "durable_routes",
        "unresolved_entities",
        "research_requests",
    ):
        rows = [*_as_list(merged.get(key)), *_as_list(extra.get(key))]
        if rows:
            merged[key] = rows
    coverage = merged.get("signal_coverage") if isinstance(merged.get("signal_coverage"), dict) else {}
    extra_coverage = extra.get("signal_coverage") if isinstance(extra.get("signal_coverage"), dict) else {}
    if extra_coverage:
        coverage = {**coverage, **extra_coverage}
    if coverage:
        merged["signal_coverage"] = coverage
    return merged


CHUNK_EXTRACTION_PROMPT = r"""
Extract source-backed signal from one ordered chunk of a larger document. Return exactly one valid JSON object.

Return these fields:
- summary_short, summary_detail, information_gain, information_gain_reason, document_disposition, confidence
- atomic_claims: at most 8 exact claims, each with claim_text, claim_kind, statement_type, fact_class, numeric_facts, entities, confidence, and source_document_id
- entity_refs: at most 6
- supply_chain_refs and technology_refs: at most 4 each
- summary_routes and durable_routes: at most 6 each
- unresolved_entities and research_requests only when necessary
- signal_coverage with status and material_signal_remaining

Rules:
- This is one chunk, not a separate source. Use the supplied source_document_id.
- Extract only facts present in this chunk or its attached media. Preserve supplier, site, product, segment, period, estimate owner, and source role.
- Use source_anchor to attribute attached media. Do not inherit an entity from a previous section when the heading changes.
- Do not emit durable writes, analyst evidence, datasets, catalysts, memos, or portfolio actions.
- Keep JSON compact. Do not repeat the same fact in summaries and multiple claims unnecessarily.
""".strip()


CHUNK_RECONCILIATION_PROMPT = r"""
You reconcile indexed claims extracted from ordered parts of one source document.
Return exactly one compact valid JSON object. Do not regenerate the complete claim list.

Return:
- summary_short: complete-document summary, at most 350 characters
- summary_detail: complete-document summary, at most 1200 characters
- duplicate_claim_groups: [{"keep_claim_id":"u0c0","drop_claim_ids":["u1c0"],"reason":"..."}]
- attribution_corrections: [{"claim_id":"u0c0","claim_text":"corrected text","entities":["..."],"reason":"..."}]
- conflict_claim_ids: [["u0c0","u1c2"]]
- information_gain, information_gain_reason, document_disposition, confidence

Rules:
- Preserve source intent and chronology. A later chunk is not a newer source; all chunks belong to the same document.
- Deduplicate semantically equivalent claims that appear at chunk boundaries. Do not treat chunks as separate documents or corroborating sources.
- Keep distinct supplier, site, product, segment, period, and source-owner facts separate.
- Preserve genuine contradictions as explicit conflict claims; never average conflicting values.
- Correct attribution using chunk headings, source anchors, and adjacent text. Do not carry an entity heading into a later section when the source anchor changes.
- Reference only supplied claim IDs. Omit duplicate groups and corrections when none are needed.
""".strip()


def _ordered_text_ranges(text: str, *, max_chars: int) -> list[tuple[int, int]]:
    body = str(text or "")
    if not body:
        return [(0, 0)]
    cap = max(1_200, int(max_chars or 0))
    if len(body) <= cap:
        return [(0, len(body))]
    ranges: list[tuple[int, int]] = []
    start = 0
    while start < len(body):
        hard_end = min(len(body), start + cap)
        end = hard_end
        if hard_end < len(body):
            window = body[start:hard_end]
            floor = max(0, int(len(window) * 0.6))
            candidates = [window.rfind("\n\n", floor), window.rfind("\n", floor)]
            sentence_ends = [match.end() for match in re.finditer(r"[.!?](?:[\"')\]]+)?\s+", window[floor:])]
            if sentence_ends:
                candidates.append(floor + sentence_ends[-1])
            best = max(candidates)
            if best > 0:
                end = start + best
        if end <= start:
            end = hard_end
        ranges.append((start, end))
        start = end
        while start < len(body) and body[start].isspace():
            start += 1
    return ranges


def _media_anchor_offset(text: str, media: dict[str, Any]) -> int | None:
    anchor = media.get("source_anchor") if isinstance(media.get("source_anchor"), dict) else {}
    for key in ("preceding_text", "heading", "caption", "following_text"):
        value = " ".join(str(anchor.get(key) or "").split())
        if not value:
            continue
        probes = [value, value[-240:], value[:240]]
        for probe in probes:
            if len(probe) < 12:
                continue
            offset = text.lower().find(probe.lower())
            if offset >= 0:
                return offset
    return None


def _document_source_text(document: dict[str, Any]) -> str:
    source_text = document.get("source_text")
    if isinstance(source_text, str):
        return source_text
    return str(document.get("text") or "")


def _document_requires_ordered_extraction(cfg: WorkerConfig, document: dict[str, Any]) -> bool:
    source_text = _document_source_text(document)
    media = [row for row in _as_list(document.get("media")) if isinstance(row, dict)]
    text_cap = _llama_safe_document_text_chars(cfg, {**document, "text": source_text, "media": []}, CHUNK_EXTRACTION_PROMPT)
    return len(source_text) > text_cap or len(media) > 4


def _ordered_document_units(cfg: WorkerConfig, document: dict[str, Any]) -> list[dict[str, Any]]:
    text = _document_source_text(document)
    media = [row for row in _as_list(document.get("media")) if isinstance(row, dict)]
    text_cap = _llama_safe_document_text_chars(cfg, {**document, "media": []}, CHUNK_EXTRACTION_PROMPT)
    ranges = _ordered_text_ranges(text, max_chars=text_cap)
    media_by_chunk: list[list[dict[str, Any]]] = [[] for _ in ranges]
    for index, row in enumerate(media):
        anchor_offset = _media_anchor_offset(text, row)
        if anchor_offset is None:
            target = min(len(ranges) - 1, int(index * len(ranges) / max(1, len(media))))
        else:
            target = next(
                (idx for idx, (start, end) in enumerate(ranges) if start <= anchor_offset < end),
                len(ranges) - 1,
            )
        media_by_chunk[target].append(row)

    units: list[dict[str, Any]] = []
    total_text_chunks = len(ranges)
    for chunk_index, ((start, end), chunk_media) in enumerate(zip(ranges, media_by_chunk, strict=True)):
        media_groups = [chunk_media[idx : idx + 4] for idx in range(0, len(chunk_media), 4)] or [[]]
        for media_group_index, media_group in enumerate(media_groups):
            unit = dict(document)
            unit["text"] = text[start:end] if media_group_index == 0 else ""
            unit["media"] = media_group
            unit["chunk_context"] = {
                "chunk_index": chunk_index,
                "chunk_number": chunk_index + 1,
                "chunk_count": total_text_chunks,
                "source_char_start": start,
                "source_char_end": end,
                "media_group_index": media_group_index,
                "media_only_followup": media_group_index > 0,
            }
            units.append(unit)
    return units


def _deterministic_chunk_merge(results: list[dict[str, Any]]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for result in results:
        merged = _merge_signal_results(merged, result)
    for key in ("atomic_claims", "entity_refs", "supply_chain_refs", "technology_refs", "summary_routes", "durable_routes", "unresolved_entities", "research_requests"):
        seen: set[str] = set()
        rows: list[Any] = []
        for row in _as_list(merged.get(key)):
            identity = json.dumps(row, sort_keys=True, ensure_ascii=False, default=str) if isinstance(row, dict) else " ".join(str(row).lower().split())
            if identity in seen:
                continue
            seen.add(identity)
            rows.append(row)
        if rows:
            merged[key] = rows
    return merged


def _compact_chunk_result_for_reconciliation(result: dict[str, Any], *, max_chars: int) -> dict[str, Any]:
    budget = max(700, int(max_chars or 0))
    compact: dict[str, Any] = {}
    for key, limit in (("summary_short", 500), ("summary_detail", 900), ("information_gain", 80), ("document_disposition", 80), ("information_gain_reason", 500)):
        value = result.get(key)
        if value not in (None, "", [], {}):
            compact[key] = _media_summary(str(value), max_chars=limit)
    for key in ("atomic_claims", "entity_refs", "supply_chain_refs", "technology_refs", "summary_routes", "durable_routes", "unresolved_entities", "research_requests"):
        rows: list[Any] = []
        for row in _as_list(result.get(key)):
            candidate = [*rows, row]
            probe = {**compact, key: candidate}
            if len(json.dumps(probe, ensure_ascii=False, default=str)) > budget:
                break
            rows.append(row)
        if rows:
            compact[key] = rows
    return compact


def _source_coverage_excerpt_for_term(hints: dict[str, Any], term: str) -> str:
    term_excerpts = hints.get("term_excerpts") if isinstance(hints.get("term_excerpts"), dict) else {}
    direct = term_excerpts.get(term) if isinstance(term_excerpts, dict) else None
    if direct:
        return _media_summary(str(direct), max_chars=640)
    for hit in _as_list(hints.get("media_hits")):
        if not isinstance(hit, dict):
            continue
        terms = _as_list(hit.get("terms"))
        if term in terms and hit.get("excerpt"):
            return _media_summary(str(hit.get("excerpt")), max_chars=640)
    return ""


def _coverage_fallback_entities(term: str, hints: dict[str, Any], excerpt: str) -> list[str]:
    entities = [term]
    for owner in _as_list(hints.get("source_owners")):
        if owner and re.search(r"\b" + re.escape(str(owner)) + r"\b", excerpt, flags=re.IGNORECASE):
            entities.append(str(owner))
    for label, pattern in SOURCE_COVERAGE_TERM_PATTERNS:
        if label != term and re.search(pattern, excerpt, flags=re.IGNORECASE):
            entities.append(label)
    return _dedupe_text_items(entities)[:8]


def _coverage_fallback_claim_kind(term: str, excerpt: str) -> str:
    blob = f"{term} {excerpt}".lower()
    if any(token in blob for token in ("capex", "capital investment", "capacity", "wpm", "wafer", "cowos", "hbm", "dram", "nand")):
        return "capacity_update"
    if any(token in blob for token in ("rubin", "blackwell", "hopper", "mi450", "ramp", "roadmap")):
        return "product_roadmap_update"
    if any(token in blob for token in ("forecast", "estimate", "fy2030", "fy2028", "revenue", "eps", "sales")):
        return "financial_metric"
    return "source_chain_update"


def _coverage_fallback_update_type(term: str, excerpt: str) -> str:
    return _synthesized_route_update_type({"claim_kind": _coverage_fallback_claim_kind(term, excerpt)}, f"{term} {excerpt}".lower())


def _apply_source_coverage_fallback(
    result: dict[str, Any],
    *,
    hints: dict[str, Any],
    missing: list[str],
    document_id: Any = None,
) -> dict[str, Any]:
    fallback_terms = [term for term in missing if term in SOURCE_COVERAGE_FALLBACK_TERMS]
    if not fallback_terms:
        return result
    out = dict(result or {})
    claims = _as_list(out.get("atomic_claims"))
    routes = _as_list(out.get("durable_routes"))
    added: list[str] = []
    result_blob = _signal_result_blob(out)
    for term in fallback_terms:
        if len(added) >= 4:
            break
        if _coverage_term_present(term, result_blob):
            continue
        excerpt = _source_coverage_excerpt_for_term(hints, term)
        if not excerpt:
            continue
        claim_text = (
            f"Prepared OCR/VLM/source context contains an unextracted {term} signal requiring durable review: "
            f"{excerpt}"
        )
        entities = _coverage_fallback_entities(term, hints, excerpt)
        claim_index = len(claims)
        claim = {
            "claim_text": claim_text,
            "claim_kind": _coverage_fallback_claim_kind(term, excerpt),
            "statement_type": "extraction_review_fallback",
            "entities": entities,
            "confidence": 0.6,
            "extraction_method": "source_coverage_fallback",
            "payload": {
                "coverage_term": term,
                "source_excerpt": excerpt,
                "fallback_reason": "term_present_in_prepared_context_but_missing_after_signal_repair",
            },
        }
        if document_id not in (None, ""):
            claim["source_document_id"] = document_id
        claims.append(claim)
        subject = entities[0] if entities else term
        routes.append(
            {
                "subject": subject,
                "destination_type": "research_subject",
                "update_type": _coverage_fallback_update_type(term, excerpt),
                "candidate_action": "review_for_update",
                "routing_key": f"coverage:{subject}:{term}",
                "entity_type": "technology",
                "role": "material_source_coverage_gap",
                "materiality": "medium",
                "novelty": "unknown",
                "temporal_posture": "mixed",
                "evidence_tier": "prepared_ocr_vlm_context",
                "new_information": claim_text,
                "confidence": 0.6,
                "claim_indexes": [claim_index],
                "rationale": "Coverage fallback: prepared context term remained missing after model repair, so it needs durable extraction review.",
            }
        )
        added.append(term)
        result_blob = result_blob + "\n" + claim_text
    if not added:
        return result
    out["atomic_claims"] = claims
    out["durable_routes"] = routes
    coverage = out.get("signal_coverage") if isinstance(out.get("signal_coverage"), dict) else {}
    notes = _as_list(coverage.get("notes"))
    notes.append("Source coverage fallback added extraction-review claims/routes for: " + ", ".join(added))
    remaining = [term for term in missing if term not in added]
    if remaining:
        notes.append("Source coverage terms still require later extraction review: " + ", ".join(remaining))
    coverage["status"] = coverage.get("status") or "partial"
    coverage["material_signal_remaining"] = True
    coverage["notes"] = notes
    out["signal_coverage"] = coverage
    if str(out.get("document_disposition") or "").strip().lower() in {"", "summary_only", "summary_candidate"}:
        out["document_disposition"] = "durable_candidate"
    out.setdefault("information_gain", "medium")
    return out


def _repair_source_coverage_if_needed(
    cfg: WorkerConfig,
    *,
    user_prompt: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    hints = user_prompt.get("source_coverage_hints") if isinstance(user_prompt.get("source_coverage_hints"), dict) else {}
    if not hints:
        return result
    missing = _missing_source_coverage_terms(result, hints)
    if not missing:
        return result
    repair_prompt = {
        "task": "repair_missing_source_coverage_terms",
        "missing_terms": missing,
        "source_coverage_hints": hints,
        "document_id": user_prompt.get("document_id"),
        "title": user_prompt.get("title"),
        "source_name": user_prompt.get("source_name"),
        "author": user_prompt.get("author"),
        "previous_result": result,
        "text_excerpt": str(user_prompt.get("text") or "")[:8_000],
        "media": _as_list(user_prompt.get("media"))[:8],
        "instructions": (
            "Return a complete signal-extraction JSON object. Preserve all previous valid claims/routes, "
            "and add source-backed atomic_claims plus durable_routes for any missing terms that are material. "
            "If a missing term is not material, preserve it in signal_coverage.notes with a reason."
        ),
    }
    try:
        repaired = _llama_json(cfg, system_prompt=SUPERVISOR_PROMPT, user_prompt=repair_prompt)
    except AgentWorkerError:
        coverage = result.get("signal_coverage") if isinstance(result.get("signal_coverage"), dict) else {}
        notes = _as_list(coverage.get("notes"))
        notes.append("Missing source coverage terms require later extraction review: " + ", ".join(missing))
        coverage["status"] = coverage.get("status") or "partial"
        coverage["material_signal_remaining"] = True
        coverage["notes"] = notes
        result["signal_coverage"] = coverage
        return _apply_source_coverage_fallback(result, hints=hints, missing=missing, document_id=user_prompt.get("document_id"))
    merged = _merge_signal_results(result, repaired)
    remaining = _missing_source_coverage_terms(merged, hints)
    if remaining:
        merged = _apply_source_coverage_fallback(merged, hints=hints, missing=remaining, document_id=user_prompt.get("document_id"))
    return merged


def _llama_chat_json_unit(cfg: WorkerConfig, *, document: dict[str, Any]) -> dict[str, Any]:
    doc_text = _document_source_text(document)
    is_chunk = isinstance(document.get("chunk_context"), dict) and bool(document.get("chunk_context"))
    system_prompt = CHUNK_EXTRACTION_PROMPT if is_chunk else SUPERVISOR_PROMPT
    initial_cap = min(len(doc_text), _llama_safe_document_text_chars(cfg, document, system_prompt)) if doc_text else 0
    caps = [initial_cap]
    min_retry_cap = 800 if (_effective_llama_context_tokens(cfg) or 0) <= 4096 else 2_000
    for ratio in (0.75, 0.55, 0.4, 0.3, 0.2):
        cap = max(min_retry_cap, int(initial_cap * ratio))
        if cap and cap not in caps and cap < initial_cap:
            caps.append(cap)
    last_retriable_error: AgentWorkerError | None = None
    retry_caps = caps or [0]
    for idx, cap in enumerate(retry_caps):
        clipped = doc_text[:cap] if cap else ""
        user_prompt = {
            "document_id": document.get("id"),
            "title": document.get("title"),
            "source_name": document.get("source_name"),
            "source_category": document.get("source_category"),
            "author": document.get("author"),
            "kind": document.get("kind"),
            "published_at": document.get("published_at"),
            "language": document.get("language"),
            "extra": _compact_document_extra_for_prompt(document.get("extra") if isinstance(document.get("extra"), dict) else {}),
            "prep_status": document.get("prep_status") if isinstance(document.get("prep_status"), dict) else {},
            "context_management": document.get("context_management") if isinstance(document.get("context_management"), dict) else {},
            "media": _compact_document_media_for_prompt(cfg, document.get("media") if isinstance(document.get("media"), list) else []),
            "source_coverage_hints": _source_coverage_hints(document),
            "image_ocr_status": document.get("image_ocr_status") if isinstance(document.get("image_ocr_status"), dict) else {},
            "chunk_context": document.get("chunk_context") if isinstance(document.get("chunk_context"), dict) else {},
            "text": clipped,
        }
        if len(clipped) < len(doc_text):
            user_prompt["truncation_note"] = {
                "original_chars": len(doc_text),
                "included_chars": len(clipped),
                "reason": "adaptive_context_limit_retry",
            }
        try:
            result = _llama_json(
                cfg,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_output_tokens_override=1_600,
            )
            if not is_chunk:
                result = _repair_source_coverage_if_needed(cfg, user_prompt=user_prompt, result=result)
            if len(clipped) < len(doc_text):
                result.setdefault("information_gain_reason", "Compressed from a context/VRAM-limited excerpt; long-form deep analysis may be needed.")
                result.setdefault("long_document_trace_needed", True)
            return result
        except AgentWorkerError as exc:
            parse_error = _is_model_parse_error(exc)
            oom_error = _is_text_oom_error(exc)
            if _is_context_limit_error(exc) or oom_error or (parse_error and idx + 1 < len(retry_caps)):
                last_retriable_error = exc
                continue
            if parse_error:
                compact_prompt = dict(user_prompt)
                compact_prompt["recovery_mode"] = "compact_valid_json_after_parse_failure"
                try:
                    result = _llama_json(
                        cfg,
                        system_prompt=CHUNK_EXTRACTION_PROMPT if is_chunk else COMPACT_SUPERVISOR_PROMPT,
                        user_prompt=compact_prompt,
                        max_output_tokens_override=1_600,
                    )
                except AgentWorkerError:
                    raise exc
                result.setdefault(
                    "signal_coverage",
                    {"status": "compact_recovery", "remaining_signal_summary": "Full signal extraction response was malformed."},
                )
                if len(clipped) < len(doc_text):
                    result.setdefault(
                        "information_gain_reason",
                        "Compressed from a context-limited excerpt after malformed JSON recovery; long-form deep analysis may be needed.",
                    )
                    result.setdefault("long_document_trace_needed", True)
                return result
            raise
    if last_retriable_error is not None:
        if _is_text_oom_error(last_retriable_error):
            raise AgentWorkerError(
                f"text model OOM after finer chunk retries: {last_retriable_error}",
                failure_type="llama_text_oom_after_chunk_retries",
            ) from last_retriable_error
        raise last_retriable_error
    raise AgentWorkerError("compression failed before model call")


def _llama_chat_json(cfg: WorkerConfig, *, document: dict[str, Any]) -> dict[str, Any]:
    units = _ordered_document_units(cfg, document)
    if len(units) <= 1:
        return _llama_chat_json_unit(cfg, document=units[0] if units else document)

    chunk_results: list[dict[str, Any]] = []
    reconciliation_rows: list[dict[str, Any]] = []
    claim_identity_by_id: dict[str, str] = {}
    chunk_failures: list[dict[str, Any]] = []
    for unit_index, unit in enumerate(units):
        try:
            result = _llama_chat_json_unit(cfg, document=unit)
        except AgentWorkerError as exc:
            chunk_failures.append(
                {
                    "unit_index": unit_index,
                    "chunk_context": unit.get("chunk_context"),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        chunk_results.append(result)
        indexed_claims: list[dict[str, Any]] = []
        for claim_index, claim in enumerate(_as_list(result.get("atomic_claims"))):
            if not isinstance(claim, dict):
                continue
            claim_id = f"u{unit_index}c{claim_index}"
            claim_identity_by_id[claim_id] = json.dumps(claim, sort_keys=True, ensure_ascii=False, default=str)
            indexed_claims.append(
                {
                    "claim_id": claim_id,
                    "claim_text": _media_summary(str(claim.get("claim_text") or ""), max_chars=420),
                    "entities": _as_list(claim.get("entities"))[:6],
                    "numeric_facts": _as_list(claim.get("numeric_facts"))[:4],
                }
            )
        reconciliation_rows.append(
            {
                "unit_index": unit_index,
                "chunk_context": unit.get("chunk_context"),
                "source_anchors": [
                    {
                        key: _media_summary(str(value), max_chars=240)
                        for key, value in row.get("source_anchor", {}).items()
                        if value not in (None, "", [], {})
                    }
                    for row in _as_list(unit.get("media"))
                    if isinstance(row, dict) and isinstance(row.get("source_anchor"), dict)
                ][:4],
                "summary": _media_summary(str(result.get("summary_detail") or result.get("summary_short") or ""), max_chars=500),
                "claims": indexed_claims,
            }
        )

    if not chunk_results:
        last_error = chunk_failures[-1].get("error") if chunk_failures else "no chunk results"
        raise AgentWorkerError(f"all ordered document chunks failed extraction: {last_error}")
    fallback = _deterministic_chunk_merge(chunk_results)
    reconcile_prompt = {
        "task": "reconcile_ordered_document_chunks",
        "document_id": document.get("id"),
        "title": document.get("title"),
        "source_name": document.get("source_name"),
        "author": document.get("author"),
        "published_at": document.get("published_at"),
        "ordered_chunks": reconciliation_rows,
        "chunk_failures": chunk_failures,
    }
    try:
        reconciled = _llama_json(
            cfg,
            system_prompt=CHUNK_RECONCILIATION_PROMPT,
            user_prompt=reconcile_prompt,
        )
    except AgentWorkerError as exc:
        fallback.setdefault("information_gain_reason", f"Ordered chunk extraction completed; reconciliation used deterministic fallback after {type(exc).__name__}.")
        fallback["_chunk_reconciliation_fallback"] = True
        fallback["_chunk_count"] = len(units)
        fallback["_chunk_extraction_failures"] = len(chunk_failures)
        return fallback
    drop_ids = {
        str(claim_id)
        for group in _as_list(reconciled.get("duplicate_claim_groups"))
        if isinstance(group, dict)
        for claim_id in _as_list(group.get("drop_claim_ids"))
        if str(claim_id) in claim_identity_by_id
    }
    corrections = {
        str(row.get("claim_id")): row
        for row in _as_list(reconciled.get("attribution_corrections"))
        if isinstance(row, dict) and str(row.get("claim_id") or "") in claim_identity_by_id
    }
    drop_identities = {claim_identity_by_id[claim_id] for claim_id in drop_ids}
    correction_by_identity = {
        claim_identity_by_id[claim_id]: row
        for claim_id, row in corrections.items()
    }
    merged_claims: list[Any] = []
    for claim in _as_list(fallback.get("atomic_claims")):
        identity = json.dumps(claim, sort_keys=True, ensure_ascii=False, default=str) if isinstance(claim, dict) else ""
        if identity in drop_identities:
            continue
        correction = correction_by_identity.get(identity)
        if correction and isinstance(claim, dict):
            claim = dict(claim)
            if correction.get("claim_text"):
                claim["claim_text"] = str(correction.get("claim_text"))
            if _as_list(correction.get("entities")):
                claim["entities"] = _as_list(correction.get("entities"))
            claim["reconciliation_correction_reason"] = str(correction.get("reason") or "").strip() or None
        merged_claims.append(claim)
    fallback["atomic_claims"] = merged_claims
    for key in ("summary_short", "summary_detail", "information_gain", "information_gain_reason", "document_disposition", "confidence"):
        if reconciled.get(key) not in (None, "", [], {}):
            fallback[key] = reconciled.get(key)
    fallback["_chunk_count"] = len(units)
    fallback["_chunk_extraction_failures"] = len(chunk_failures)
    fallback["_chunk_reconciliation_fallback"] = False
    if chunk_failures:
        coverage = fallback.get("signal_coverage") if isinstance(fallback.get("signal_coverage"), dict) else {}
        coverage["status"] = "partial"
        coverage["material_signal_remaining"] = True
        coverage["notes"] = [*_as_list(coverage.get("notes")), f"{len(chunk_failures)} ordered chunk unit(s) failed extraction."]
        fallback["signal_coverage"] = coverage
        fallback["long_document_trace_needed"] = True
    return fallback


def _text_token_estimate(text: object) -> int:
    body = str(text or "")
    words = len(re.findall(r"\S+", body))
    return max(1, int(max(len(body) / 3.5, words * 1.3)))


def _effective_llama_context_tokens(cfg: WorkerConfig) -> int | None:
    raw_env = str(os.environ.get("THOTH_LLAMA_CONTEXT_TOKENS") or "").strip()
    if raw_env:
        try:
            parsed = int(raw_env)
        except ValueError:
            parsed = 0
        if parsed > 0:
            return parsed
    return _LLAMA_CONTEXT_TOKENS_BY_ENDPOINT.get(_endpoint_identity(cfg.llama_base_url))


def _llama_safe_output_tokens(cfg: WorkerConfig) -> int:
    context_tokens = _effective_llama_context_tokens(cfg)
    if not context_tokens:
        return int(cfg.max_output_tokens)
    return max(512, min(int(cfg.max_output_tokens), int(context_tokens * 0.32)))


def _llama_safe_user_content_chars(cfg: WorkerConfig, system_prompt: str) -> int:
    context_tokens = _effective_llama_context_tokens(cfg)
    if not context_tokens:
        return int(cfg.max_input_chars)
    # Pinned pi-ai reserves 4096 tokens before allocating generation output.
    safe_prompt_tokens = max(0, context_tokens - 4096 - _llama_safe_output_tokens(cfg) - 128)
    system_tokens = _text_token_estimate(system_prompt) + 128
    user_tokens = max(500, safe_prompt_tokens - system_tokens)
    return max(1_200, min(int(cfg.max_input_chars), int(user_tokens * 3.1)))


def _llama_safe_document_text_chars(cfg: WorkerConfig, document: dict[str, Any], system_prompt: str) -> int:
    limit = _llama_safe_user_content_chars(cfg, system_prompt)
    if _effective_llama_context_tokens(cfg):
        media_count = len(_as_list(document.get("media") if isinstance(document, dict) else None))
        metadata_reserve = 900 + min(media_count, 8) * 280
        return max(800, min(int(cfg.max_input_chars), limit - metadata_reserve))
    return int(cfg.max_input_chars)


def _compact_document_extra_for_prompt(extra: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key in ("connector", "handle", "tweet_id", "author", "canonical_url"):
        value = extra.get(key)
        if value not in (None, "", [], {}):
            compact[key] = value
    links = _as_list(extra.get("links"))
    if links:
        compact["links"] = links[:5]
        compact["link_count"] = len(links)
    thread = extra.get("thread_context") if isinstance(extra.get("thread_context"), dict) else {}
    if thread:
        compact["thread_context"] = {
            "ancestor_count": len(_as_list(thread.get("ancestors"))),
            "reply_count": len(_as_list(thread.get("replies"))),
            "reply_excerpt": [
                {
                    "relation": row.get("relation"),
                    "tweet_id": row.get("tweet_id"),
                    "text": _media_summary(str(row.get("text") or ""), max_chars=180),
                    "media_count": len(_as_list(row.get("media"))),
                }
                for row in _as_list(thread.get("replies"))[:3]
                if isinstance(row, dict)
            ],
        }
    return compact


def _compact_document_media_for_prompt(cfg: WorkerConfig, media: list[Any]) -> list[dict[str, Any]]:
    rows = [row for row in media if isinstance(row, dict)]
    if not rows:
        return []
    context_tokens = _effective_llama_context_tokens(cfg)
    total_chars = 1_400 if context_tokens and context_tokens <= 4096 else 2_800
    per_row = max(220, min(900, total_chars // max(1, len(rows))))
    compact_rows: list[dict[str, Any]] = []
    for row in rows[:12]:
        text = str(row.get("ocr_text") or row.get("vision_text") or row.get("vlm_text") or row.get("ocr_summary") or "")
        compact = {
            "label": row.get("label"),
            "section": row.get("section"),
            "url": row.get("url"),
            "media_signal": row.get("media_signal"),
            "is_signal": row.get("is_signal"),
            "media_kind": row.get("media_kind"),
            "needs_vlm": row.get("needs_vlm"),
            "vlm_required": row.get("vlm_required"),
            "vlm_available": row.get("vlm_available"),
            "prep_blocking": row.get("prep_blocking"),
            "signal_reason": row.get("signal_reason"),
            "visual_signal_terms": _as_list(row.get("visual_signal_terms"))[:8],
            "source_anchor": row.get("source_anchor") if isinstance(row.get("source_anchor"), dict) else None,
            "text_excerpt": _media_summary(text, max_chars=per_row) if text else None,
            "text_chars": len(text),
        }
        compact_rows.append({key: value for key, value in compact.items() if value not in (None, "", [], {})})
    return compact_rows


SOURCE_COVERAGE_TERM_PATTERNS: tuple[tuple[str, str], ...] = (
    ("Rubin", r"\bRubin\b"),
    ("Blackwell", r"\bBlackwell\b"),
    ("Hopper", r"\bHopper\b"),
    ("MI450", r"\bMI\s*-?\s*450\b|\bMI450\b"),
    ("TPU", r"\bTPU(?:s)?\b"),
    ("Trainium", r"\bTrainium\b"),
    ("HBM", r"\bHBM\d*(?:E)?\b"),
    ("DRAM", r"\bDRAM\b"),
    ("NAND", r"\bNAND\b"),
    ("CoWoS", r"\bCoWoS(?:-[A-Z])?\b"),
    ("WPM", r"\bWPM\b|\bwafers?\s+per\s+month\b"),
    ("FY2030", r"\bFY\s*2030\b|\bFY2030\b"),
    ("FY2028", r"\bFY\s*2028\b|\bFY2028\b"),
    ("JPY capex", r"\bJPY\s*\d+(?:\.\d+)?\s*(?:bn|billion|trillion)?\b"),
    ("AI demand", r"\bAI\s+demand\b|\bAI-related\s+demand\b"),
    ("capacity constraint", r"\bcapacity\s+constraint|\bconstraints?\s+(?:in|such as|on)\b|\bexpand\s+capacity\b"),
    ("pricing", r"\bpricing\b|\bselling\s+prices?\b|\bprice\s+negotiations?\b"),
)


SOURCE_COVERAGE_OWNER_PATTERNS: tuple[tuple[str, str], ...] = (
    ("UBS", r"\bUBS\b"),
    ("Morgan Stanley", r"\bMorgan\s+Stanley\b"),
    ("Mizuho", r"\bMizuho\b"),
    ("Jefferies", r"\bJefferies\b"),
    ("JPMorgan", r"\bJ\.?P\.?\s*Morgan\b|\bJPMorgan\b"),
    ("Goldman Sachs", r"\bGoldman\s+Sachs\b"),
    ("BofA", r"\bBofA\b|\bBank\s+of\s+America\b"),
    ("SemiAnalysis", r"\bSemiAnalysis\b"),
    ("Gartner", r"\bGartner\b"),
)


SOURCE_COVERAGE_REQUIRED_OUTPUT_TERMS = {
    "Rubin",
    "Blackwell",
    "Hopper",
    "MI450",
    "FY2030",
    "FY2028",
    "JPY capex",
    "CoWoS",
    "HBM",
    "DRAM",
    "NAND",
    "UBS",
    "Morgan Stanley",
    "Mizuho",
    "Jefferies",
}

SOURCE_COVERAGE_FALLBACK_TERMS = {
    "Rubin",
    "Blackwell",
    "Hopper",
    "MI450",
    "FY2030",
    "FY2028",
    "JPY capex",
    "CoWoS",
    "HBM",
    "DRAM",
    "NAND",
    "UBS",
    "Morgan Stanley",
    "Mizuho",
    "Jefferies",
}


def _coverage_excerpt(text: str, matched_terms: list[str], *, max_chars: int = 520) -> str:
    if not text:
        return ""
    best_index: int | None = None
    for term in matched_terms:
        if not term:
            continue
        pattern = next(
            (candidate for label, candidate in SOURCE_COVERAGE_TERM_PATTERNS + SOURCE_COVERAGE_OWNER_PATTERNS if label == term),
            "",
        )
        match = re.search(pattern, text, flags=re.IGNORECASE) if pattern else None
        idx = match.start() if match else text.lower().find(term.lower())
        if idx >= 0 and (best_index is None or idx < best_index):
            best_index = idx
    if best_index is None:
        return _media_summary(text, max_chars=max_chars)
    start = max(0, best_index - max_chars // 3)
    end = min(len(text), best_index + (max_chars * 2 // 3))
    excerpt = text[start:end].strip()
    if start > 0:
        excerpt = "..." + excerpt
    if end < len(text):
        excerpt = excerpt + "..."
    return _media_summary(excerpt, max_chars=max_chars)


def _source_coverage_hints(document: dict[str, Any]) -> dict[str, Any]:
    text_parts: list[str] = [
        str(document.get("title") or ""),
        str(document.get("source_name") or ""),
        str(document.get("author") or ""),
        _document_source_text(document)[:24_000],
    ]
    media_hits: list[dict[str, Any]] = []
    for idx, row in enumerate(_as_list(document.get("media"))[:12]):
        if not isinstance(row, dict):
            continue
        media_text = str(row.get("ocr_text") or row.get("vision_text") or row.get("vlm_text") or row.get("ocr_summary") or "")
        if not media_text:
            continue
        text_parts.append(media_text[:4_000])
        matched_terms = [
            label
            for label, pattern in SOURCE_COVERAGE_TERM_PATTERNS + SOURCE_COVERAGE_OWNER_PATTERNS
            if re.search(pattern, media_text, flags=re.IGNORECASE)
        ]
        if matched_terms:
            media_hits.append(
                {
                    "media_index": idx,
                    "terms": _dedupe_text_items(matched_terms)[:12],
                    "is_signal": row.get("is_signal"),
                    "needs_vlm": row.get("needs_vlm") or row.get("vlm_required"),
                    "excerpt": _coverage_excerpt(media_text, matched_terms),
                }
            )
    blob = "\n".join(part for part in text_parts if part)
    if not blob.strip():
        return {}
    terms = [
        label
        for label, pattern in SOURCE_COVERAGE_TERM_PATTERNS
        if re.search(pattern, blob, flags=re.IGNORECASE)
    ]
    owners = [
        label
        for label, pattern in SOURCE_COVERAGE_OWNER_PATTERNS
        if re.search(pattern, blob, flags=re.IGNORECASE)
    ]
    term_excerpts = {
        label: _coverage_excerpt(blob, [label], max_chars=640)
        for label in _dedupe_text_items([*terms, *owners])
        if label in SOURCE_COVERAGE_REQUIRED_OUTPUT_TERMS
    }
    numeric_fragments = _dedupe_text_items(
        re.findall(
            r"\b(?:FY|CY)\s*20\d{2}\b|\b[QH][1-4]\s*20\d{2}\b|\b\d+(?:\.\d+)?\s*(?:%|bps|WPM|wafers/month|units|mn|m|bn|billion|trillion)\b|\bJPY\s*\d+(?:\.\d+)?\s*(?:bn|billion|trillion)?\b",
            blob,
            flags=re.IGNORECASE,
        )
    )[:18]
    hints: dict[str, Any] = {
        "terms": _dedupe_text_items(terms)[:18],
        "source_owners": _dedupe_text_items(owners)[:12],
        "numeric_fragments": numeric_fragments,
        "media_hits": media_hits[:8],
        "term_excerpts": term_excerpts,
    }
    return {key: value for key, value in hints.items() if value not in (None, "", [], {})}


def _batch_document_payloads(cfg: WorkerConfig, documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    docs = [doc for doc in documents if isinstance(doc, dict) and doc.get("id") is not None]
    if not docs:
        return []
    budget_chars = max(4_000, min(int(cfg.batch_max_input_tokens * 3.5), max(cfg.max_input_chars, 4_000)))
    metadata_reserve = 1_500 + (700 * len(docs))
    text_budget = max(1_000 * len(docs), budget_chars - metadata_reserve)
    per_doc_cap = max(1_200, text_budget // len(docs))
    out: list[dict[str, Any]] = []
    for doc in docs:
        doc_text = _document_source_text(doc)
        out.append(
            {
                "document_id": doc.get("id"),
                "title": doc.get("title"),
                "source_name": doc.get("source_name"),
                "source_category": doc.get("source_category"),
                "author": doc.get("author"),
                "kind": doc.get("kind"),
                "url": doc.get("url"),
                "published_at": doc.get("published_at"),
                "language": doc.get("language"),
                "extra": doc.get("extra") if isinstance(doc.get("extra"), dict) else {},
                "prep_status": doc.get("prep_status") if isinstance(doc.get("prep_status"), dict) else {},
                "context_management": doc.get("context_management") if isinstance(doc.get("context_management"), dict) else {},
                "media": _compact_document_media_for_prompt(cfg, doc.get("media") if isinstance(doc.get("media"), list) else []),
                "source_coverage_hints": _source_coverage_hints(doc),
                "image_ocr_status": doc.get("image_ocr_status") if isinstance(doc.get("image_ocr_status"), dict) else {},
                "estimated_tokens": _text_token_estimate(doc_text),
                "text": doc_text[:per_doc_cap],
            }
        )
    return out


def _llama_batch_json(cfg: WorkerConfig, *, documents: list[dict[str, Any]], batch: dict[str, Any]) -> dict[str, Any]:
    user_prompt = {
        "batch_id": batch.get("batch_id"),
        "batch_reason": batch.get("reason"),
        "document_ids": [doc.get("id") for doc in documents],
        "max_input_tokens": cfg.batch_max_input_tokens,
        "documents": _batch_document_payloads(cfg, documents),
    }
    return _llama_json(cfg, system_prompt=BATCH_SUPERVISOR_PROMPT, user_prompt=user_prompt)


def _batch_results_by_document(batch_result: dict[str, Any], *, allowed_document_ids: set[int]) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    batch_summary = str(batch_result.get("batch_summary") or "").strip()
    dedupe = _as_list(batch_result.get("dedupe_corroboration"))
    for item in _as_list(batch_result.get("documents")):
        if not isinstance(item, dict):
            continue
        try:
            document_id = int(item.get("document_id") or item.get("id"))
        except Exception:
            continue
        if document_id not in allowed_document_ids:
            continue
        result = dict(item.get("compression") if isinstance(item.get("compression"), dict) else item)
        result.pop("document_id", None)
        result.pop("id", None)
        if batch_summary:
            result.setdefault("batch_summary", batch_summary)
        if dedupe:
            result.setdefault("dedupe_corroboration", dedupe)
        out[document_id] = result
    return out


def _sanitize_result_source_ids(result: dict[str, Any], *, document_id: int, allowed_document_ids: set[int]) -> dict[str, Any]:
    sanitized = dict(result or {})
    for field in (
        "research_writes",
        "analyst_evidence",
        "model_delta_candidates",
        "catalysts",
        "investment_memos",
        "evidence_links",
        "dataset_rows",
        "dataset_materializations",
        "supply_chain_metric_bindings",
        "instrument_request_fulfillments",
    ):
        rows: list[Any] = []
        for item in _as_list(sanitized.get(field)):
            if not isinstance(item, dict):
                continue
            row = dict(item)
            ids = []
            for value in _as_list(row.get("source_document_ids")):
                try:
                    source_id = int(value)
                except Exception:
                    continue
                if source_id in allowed_document_ids:
                    ids.append(source_id)
            scalar_source_id = _int_or_none(row.get("source_document_id"))
            if scalar_source_id is not None and scalar_source_id not in allowed_document_ids:
                row["source_document_id"] = int(document_id)
            if field == "research_writes":
                row["source_document_ids"] = ids or [int(document_id)]
            elif row.get("source_document_id") is None:
                row["source_document_id"] = int(document_id)
            rows.append(row)
        if rows:
            sanitized[field] = rows
    return sanitized


def _fallback_compression_result(document: dict[str, Any], *, error: str) -> dict[str, Any]:
    title = str(document.get("title") or f"Document {document.get('id') or ''}").strip()
    source_name = str(document.get("source_name") or document.get("source") or "").strip()
    published_at = str(document.get("published_at") or "").strip()
    context_bits = [bit for bit in (title, source_name, published_at) if bit]
    context = " | ".join(context_bits)
    return {
        "document_disposition": "skip",
        "information_gain": "none",
        "information_gain_reason": "Local model output could not be parsed as valid JSON; no claims or durable writes were emitted.",
        "skip_reason": "model_output_parse_failure",
        "summary_short": f"Model parse failure; queued for backlog retry: {title}"[:500],
        "summary_detail": (
            f"The local model returned malformed or non-JSON output while compressing {context}. "
            "This error compression preserves the failure without emitting analyst claims."
        ),
        "atomic_claims": [],
        "entity_refs": [],
        "supply_chain_refs": [],
        "technology_refs": [],
        "summary_routes": [],
        "durable_routes": [],
        "unresolved_entities": [],
        "research_writes": [],
        "confidence": 0.0,
        "error": error,
        "_compression_status": "error",
    }


def _compact_rows(value: object, *, limit: int) -> list[Any]:
    return _as_list(value)[: max(0, int(limit))]


def _compact_decision_context(context: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(context, dict):
        return {}
    return {
        "subject": context.get("subject"),
        "coverage_score": context.get("coverage_score"),
        "coverage_inputs": context.get("coverage_inputs"),
        "decision_snapshot": context.get("decision_snapshot"),
        "memos": _compact_rows(context.get("memos"), limit=3),
        "active_theses": _compact_rows(context.get("active_theses"), limit=5),
        "analyst_evidence": _compact_rows(context.get("analyst_evidence"), limit=8),
        "latest_report": context.get("latest_report"),
        "report_memory": context.get("report_memory"),
        "catalysts": _compact_rows(context.get("catalysts"), limit=10),
        "earnings_packets": _compact_rows(context.get("earnings_packets"), limit=5),
        "gaps": _compact_rows(context.get("gaps"), limit=10),
        "research_requests": _compact_rows(context.get("research_requests"), limit=8),
        "dataset_context": context.get("dataset_context"),
        "dataset_count": context.get("dataset_count"),
        "structured_fact_count": context.get("structured_fact_count"),
        "source_rail_health": context.get("source_rail_health"),
        "alert_inventory": context.get("alert_inventory"),
        "transcript_watch_states": _compact_rows(context.get("transcript_watch_states"), limit=5),
    }


def _compact_market_context(context: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(context, dict):
        return {}
    compact = dict(context)
    for key in ("market_watchlist",):
        if isinstance(compact.get(key), dict) and isinstance(compact[key].get("items"), list):
            nested = dict(compact[key])
            nested["items"] = nested["items"][:20]
            compact[key] = nested
    for key in ("indexes", "futures", "commodities", "rates", "fx_rates"):
        if isinstance(compact.get(key), list):
            compact[key] = compact[key][:20]
    return compact


def _compact_institutional_model_context(context: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(context, dict):
        return {}
    return {
        "version": context.get("version"),
        "source_registry_version": context.get("source_registry_version"),
        "skill_card": context.get("skill_card"),
        "metric_vocabulary": _compact_rows(context.get("metric_vocabulary"), limit=80),
        "signal_rail_taxonomy": _compact_rows(context.get("signal_rail_taxonomy"), limit=20),
        "durable_rail_catalog": {
            "supported_destinations": _compact_rows((context.get("durable_rail_catalog") or {}).get("supported_destinations"), limit=8)
            if isinstance(context.get("durable_rail_catalog"), dict)
            else [],
            "missing_rail_request_matches": _compact_rows((context.get("durable_rail_catalog") or {}).get("missing_rail_request_matches"), limit=8)
            if isinstance(context.get("durable_rail_catalog"), dict)
            else [],
        },
        "period_grammar": context.get("period_grammar"),
        "source_actor_matches": _compact_rows(context.get("source_actor_matches"), limit=12),
        "source_registry_sample": _compact_rows(context.get("source_registry_sample"), limit=12),
        "subject_matches": _compact_rows(context.get("subject_matches"), limit=8),
        "state_vectors": _compact_rows(context.get("state_vectors"), limit=8),
        "report_input_contract": context.get("report_input_contract"),
    }


_DATASET_TOOL_STOPWORDS = {
    "action",
    "and",
    "author",
    "basis",
    "center",
    "claim",
    "claims",
    "company",
    "confidence",
    "context",
    "data",
    "discard",
    "disposition",
    "document",
    "documents",
    "durable",
    "entity",
    "entities",
    "fact",
    "facts",
    "for",
    "from",
    "id",
    "investment",
    "investor",
    "into",
    "kind",
    "language",
    "materiality",
    "market",
    "method",
    "metric",
    "metrics",
    "modelable",
    "note",
    "notes",
    "observed",
    "only",
    "owner",
    "period",
    "prefer",
    "preserve",
    "provenance",
    "published",
    "request",
    "requests",
    "review",
    "rows",
    "signal",
    "source",
    "source-backed",
    "source_owned",
    "backed",
    "status",
    "stays",
    "summary",
    "short",
    "the",
    "this",
    "title",
    "unit",
    "unknown",
    "url",
    "use",
    "useful",
    "value",
    "when",
    "with",
}
_DATASET_TOOL_REGISTRY_LIMIT = 8
_DATASET_TOOL_REGISTRY_MIN_SCORE = 3.5
_DATASET_TOOL_REGISTRY_RELATIVE_MIN_SCORE = 0.5
_DATASET_TOOL_DEFAULT_NAMES = (
    "sell_side_consensus_estimate",
    "company_financial_kpi",
    "media_chart_extraction",
    "facility_capex_ramp",
    "market_share_production_units",
    "pricing_supply_demand",
)
_DATASET_TOOL_HINTS: dict[str, tuple[str, ...]] = {
    "company_financial_kpi": (
        "revenue",
        "sales",
        "gross margin",
        "operating margin",
        "eps",
        "earnings",
        "guidance",
        "segment",
        "constant currency",
        "data center revenue",
        "networking sales",
    ),
    "sell_side_consensus_estimate": (
        "consensus",
        "estimate",
        "forecast",
        "price target",
        "rating",
        "revision",
        "upgrade",
        "downgrade",
    ),
    "fx_currency_hedging_impact": (
        "fx",
        "foreign exchange",
        "currency",
        "constant currency",
        "hedge",
        "hedging",
        "transaction exposure",
        "translation exposure",
    ),
    "media_chart_extraction": (
        "chart",
        "graph",
        "axis",
        "watermark",
        "ocr",
        "vlm",
        "visual readout",
    ),
}


def _tool_card_text(value: object, *, limit: int = 420) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _dataset_tool_tokens(text: object) -> list[str]:
    tokens: list[str] = []
    for raw in re.findall(r"[A-Za-z0-9][A-Za-z0-9_+./%-]*", str(text or "").lower()):
        raw = raw.strip("._-/+%")
        if not raw:
            continue
        candidates = [raw]
        candidates.extend(part for part in re.split(r"[_/.\-+%]+", raw) if part)
        for token in candidates:
            if len(token) < 2:
                continue
            if token not in tokens:
                tokens.append(token)
    return tokens


def _dataset_tool_service_metadata() -> tuple[dict[str, dict[str, str]], dict[str, tuple[dict[str, str], ...]], set[str]]:
    try:
        from thoth_terminal import dataset_service

        family_meta = getattr(dataset_service, "DATASET_FAMILY_META", {})
        column_templates = getattr(dataset_service, "DATASET_FAMILY_COLUMN_TEMPLATES", {})
        materializer_families = getattr(dataset_service, "DATASET_FAMILY_MATERIALIZER_FAMILIES", ())
        return (
            family_meta if isinstance(family_meta, dict) else {},
            column_templates if isinstance(column_templates, dict) else {},
            set(materializer_families) if isinstance(materializer_families, (set, frozenset, tuple, list)) else set(),
        )
    except Exception:
        return {}, {}, set()


def _dataset_tool_title(name: str) -> str:
    return " ".join(part.upper() if part in {"ai", "eda", "hbm", "ip", "rf"} else part.title() for part in name.split("_"))


def _dataset_tool_row_kinds(text: str) -> list[str]:
    match = re.search(r"Use row_kind to separate (?P<rows>.+?) rows", text)
    if not match:
        return []
    row_text = match.group("rows")
    row_text = row_text.replace(" and ", ", ")
    rows: list[str] = []
    for item in row_text.split(","):
        clean = re.sub(r"\s+", "_", item.strip().strip(".")).strip("_").lower()
        if clean and clean not in rows:
            rows.append(clean)
        if len(rows) >= 14:
            break
    return rows


def _dataset_tool_cards() -> list[dict[str, Any]]:
    global _DATASET_TOOL_CARD_CACHE
    if _DATASET_TOOL_CARD_CACHE is not None:
        return _DATASET_TOOL_CARD_CACHE
    family_meta, column_templates, service_families = _dataset_tool_service_metadata()
    valid_names = set(DATASET_MATERIALIZATION_FAMILIES) | set(service_families)
    cards: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in DATASET_TOOL_SOURCE_PROMPT.splitlines():
        match = _DATASET_TOOL_LINE_RE.match(line)
        if not match:
            continue
        name = match.group("name").strip()
        if name not in valid_names or name in seen:
            continue
        seen.add(name)
        meta = family_meta.get(name) if isinstance(family_meta.get(name), dict) else {}
        label = str(meta.get("label") or _dataset_tool_title(name)).strip()
        summary = str(meta.get("summary") or match.group("body")).strip()
        use_when = match.group("use_when").strip()
        body = match.group("body").strip()
        columns = [
            str(col.get("key") or "").strip()
            for col in column_templates.get(name, ())
            if isinstance(col, dict) and str(col.get("key") or "").strip()
        ]
        search_text = " ".join([name.replace("_", " "), label, use_when, summary, body, " ".join(columns)])
        search_tokens = _dataset_tool_tokens(search_text)
        tags: list[str] = []
        for token in _dataset_tool_tokens(" ".join([name.replace("_", " "), use_when, body, " ".join(columns[:24])])):
            if token in _DATASET_TOOL_STOPWORDS:
                continue
            if token not in tags:
                tags.append(token)
            if len(tags) >= 16:
                break
        cards.append(
            {
                "name": name,
                "type": "dataset_materialization",
                "label": label,
                "summary": _tool_card_text(summary, limit=360),
                "use_when": _tool_card_text(use_when, limit=240),
                "definition": _tool_card_text(body, limit=820),
                "row_kinds": _dataset_tool_row_kinds(body),
                "key_fields": columns[:42],
                "tags": tags,
                "_search_text": search_text.lower(),
                "_search_tokens": search_tokens,
            }
        )
    for name in DATASET_MATERIALIZATION_FAMILIES:
        if name in seen or name not in valid_names:
            continue
        seen.add(name)
        meta = family_meta.get(name) if isinstance(family_meta.get(name), dict) else {}
        label = str(meta.get("label") or _dataset_tool_title(name)).strip()
        summary = str(meta.get("summary") or f"Materialize {label} rows.").strip()
        columns = [
            str(col.get("key") or "").strip()
            for col in column_templates.get(name, ())
            if isinstance(col, dict) and str(col.get("key") or "").strip()
        ]
        body = f"{summary} Key fields: {', '.join(columns[:42])}."
        search_text = " ".join([name.replace("_", " "), label, summary, body, " ".join(columns)])
        tags: list[str] = []
        for token in _dataset_tool_tokens(" ".join([name.replace("_", " "), summary, " ".join(columns[:24])])):
            if token in _DATASET_TOOL_STOPWORDS:
                continue
            if token not in tags:
                tags.append(token)
            if len(tags) >= 16:
                break
        cards.append(
            {
                "name": name,
                "type": "dataset_materialization",
                "label": label,
                "summary": _tool_card_text(summary, limit=360),
                "use_when": _tool_card_text(summary, limit=240),
                "definition": _tool_card_text(body, limit=820),
                "row_kinds": [],
                "key_fields": columns[:42],
                "tags": tags,
                "_search_text": search_text.lower(),
                "_search_tokens": _dataset_tool_tokens(search_text),
            }
        )
    for name in sorted(valid_names - seen):
        meta = family_meta.get(name) if isinstance(family_meta.get(name), dict) else {}
        if not meta and name not in column_templates:
            continue
        label = str(meta.get("label") or _dataset_tool_title(name)).strip()
        summary = str(meta.get("summary") or f"Materialize {label} rows.").strip()
        columns = [
            str(col.get("key") or "").strip()
            for col in column_templates.get(name, ())
            if isinstance(col, dict) and str(col.get("key") or "").strip()
        ]
        body = f"{summary} Key fields: {', '.join(columns[:42])}."
        search_text = " ".join([name.replace("_", " "), label, summary, body, " ".join(columns)])
        cards.append(
            {
                "name": name,
                "type": "dataset_materialization",
                "label": label,
                "summary": _tool_card_text(summary, limit=360),
                "use_when": _tool_card_text(summary, limit=240),
                "definition": _tool_card_text(body, limit=820),
                "row_kinds": [],
                "key_fields": columns[:42],
                "tags": [
                    token
                    for token in _dataset_tool_tokens(" ".join([name.replace("_", " "), summary, " ".join(columns[:24])]))
                    if token not in _DATASET_TOOL_STOPWORDS
                ][:16],
                "_search_text": search_text.lower(),
                "_search_tokens": _dataset_tool_tokens(search_text),
            }
        )
    _DATASET_TOOL_CARD_CACHE = cards
    return cards


def _dataset_tool_public_card(card: dict[str, Any], *, detailed: bool) -> dict[str, Any]:
    if detailed:
        return {
            "name": card.get("name"),
            "type": "dataset_materialization",
            "label": card.get("label"),
            "use_when": card.get("use_when"),
            "definition": card.get("definition"),
            "row_kinds": _as_list(card.get("row_kinds"))[:14],
            "key_fields": _as_list(card.get("key_fields"))[:42],
            "output_contract": f"Emit dataset_materializations[].family={card.get('name')} with source-backed rows only.",
        }
    return {
        "name": card.get("name"),
        "type": "dataset_materialization",
        "label": card.get("label"),
        "tags": _as_list(card.get("tags"))[:12],
    }


def _dataset_tool_safe_json(value: object, *, limit: int) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = str(value or "")
    return text[: max(0, int(limit))]


def _decision_tool_query_text(
    *,
    document: dict[str, Any],
    compression: dict[str, Any],
    decision_contexts: list[dict[str, Any]],
    institutional_model_context: dict[str, Any] | None,
) -> str:
    pieces: list[str] = []
    for key in ("title", "source_name", "source_category", "author", "kind", "language", "url"):
        value = document.get(key)
        if value:
            pieces.append(str(value))
    extra = document.get("extra")
    if isinstance(extra, dict):
        pieces.append(_dataset_tool_safe_json(extra, limit=1600))
    pieces.append(str(document.get("text") or "")[:10000])
    pieces.append(_dataset_tool_safe_json(compression, limit=10000))
    pieces.append(_dataset_tool_safe_json(decision_contexts[:6], limit=6000))
    if isinstance(institutional_model_context, dict):
        pieces.append(_dataset_tool_safe_json(_compact_institutional_model_context(institutional_model_context), limit=5000))
    return "\n".join(part for part in pieces if str(part or "").strip())


def _score_dataset_tool(card: dict[str, Any], query_text: str, query_tokens: list[str]) -> float:
    search_text = str(card.get("_search_text") or "")
    search_tokens = set(_as_list(card.get("_search_tokens")))
    name = str(card.get("name") or "")
    score = 0.0
    query_lower = query_text.lower()
    if name and name in query_lower:
        score += 90.0
    for part in name.split("_"):
        if part and part not in _DATASET_TOOL_STOPWORDS and part in query_tokens:
            score += 2.5
    for hint in _DATASET_TOOL_HINTS.get(name, ()):
        hint_lower = hint.lower()
        if " " in hint_lower:
            if hint_lower in query_lower:
                score += 4.5
            continue
        if hint_lower in query_tokens:
            score += 3.0
    for token in set(query_tokens):
        if token in _DATASET_TOOL_STOPWORDS:
            continue
        if token in search_tokens:
            score += 1.75
        elif len(token) >= 4 and token in search_text:
            score += 0.45
    label = str(card.get("label") or "").lower()
    if label and label in query_lower:
        score += 18.0
    return score


def _rank_dataset_tools(
    query_text: str,
    *,
    limit: int,
    defaults: bool,
    min_score: float = 0.0,
    relative_min_score: float = 0.0,
) -> tuple[list[dict[str, Any]], int]:
    query_tokens = _dataset_tool_tokens(query_text)
    scored: list[tuple[float, int, dict[str, Any]]] = []
    for index, card in enumerate(_dataset_tool_cards()):
        score = _score_dataset_tool(card, query_text, query_tokens)
        if score > 0:
            scored.append((score, -index, card))
    scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
    top_score = scored[0][0] if scored else 0.0
    effective_min_score = max(float(min_score or 0.0), float(relative_min_score or 0.0) * top_score)
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for score, _index, card in scored:
        if score < effective_min_score:
            continue
        name = str(card.get("name") or "")
        if name in seen:
            continue
        selected.append(card)
        seen.add(name)
        if len(selected) >= limit:
            break
    if defaults and len(selected) < limit:
        by_name = {str(card.get("name") or ""): card for card in _dataset_tool_cards()}
        for name in _DATASET_TOOL_DEFAULT_NAMES:
            card = by_name.get(name)
            if card is None or name in seen:
                continue
            selected.append(card)
            seen.add(name)
            if len(selected) >= limit:
                break
    return selected, max(0, len(_dataset_tool_cards()) - len(selected))


def _decision_tools_registry_context(
    *,
    document: dict[str, Any],
    compression: dict[str, Any],
    decision_contexts: list[dict[str, Any]],
    institutional_model_context: dict[str, Any] | None,
) -> dict[str, Any]:
    query_text = _decision_tool_query_text(
        document=document,
        compression=compression,
        decision_contexts=decision_contexts,
        institutional_model_context=institutional_model_context,
    )
    cards, omitted_count = _rank_dataset_tools(
        query_text,
        limit=_DATASET_TOOL_REGISTRY_LIMIT,
        defaults=False,
        min_score=_DATASET_TOOL_REGISTRY_MIN_SCORE,
        relative_min_score=_DATASET_TOOL_REGISTRY_RELATIVE_MIN_SCORE,
    )
    return {
        "registry_name": "dataset_materialization_tools",
        "description": (
            "Durable dataset-materialization tools for modelable investment research facts. "
            "Top-level cards are intentionally compact and content-matched; request detailed definitions before using an unfamiliar row shape."
        ),
        "available": [_dataset_tool_public_card(card, detailed=False) for card in cards],
        "lookup": {
            "request_field": "tool_requests",
            "request_shape": {"query": "free-text tool search terms", "names": ["optional_exact_tool_name"]},
            "max_requests": 4,
        },
        "omitted_tool_count": omitted_count,
    }


def _normalize_tool_name(value: object) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", str(value or "").strip().lower()).strip("_")


def _extract_decision_tool_requests(decision: dict[str, Any]) -> list[dict[str, Any]]:
    raw_items: list[Any] = []
    raw_items.extend(_as_list(decision.get("tool_requests")))
    review = decision.get("decision_review")
    if isinstance(review, dict):
        raw_items.extend(_as_list(review.get("tool_requests")))
    requests_out: list[dict[str, Any]] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for item in raw_items:
        if isinstance(item, str):
            query = item.strip()
            names: list[str] = []
        elif isinstance(item, dict):
            query = str(item.get("query") or item.get("search") or item.get("terms") or "").strip()
            names = []
            for key in ("names", "tools", "tool_names", "families", "family"):
                value = item.get(key)
                values = value if isinstance(value, list) else [value] if value else []
                for candidate in values:
                    name = _normalize_tool_name(candidate)
                    if name and name not in names:
                        names.append(name)
            if not query and names:
                query = " ".join(names)
        else:
            continue
        if not query and not names:
            continue
        names = [name for name in names if name in DATASET_MATERIALIZATION_FAMILIES][:8]
        key = (query.lower(), tuple(names))
        if key in seen:
            continue
        seen.add(key)
        requests_out.append({"query": query, "names": names})
        if len(requests_out) >= 4:
            break
    return requests_out


def _decision_tool_definition_context(
    requests_in: list[dict[str, Any]],
    *,
    document: dict[str, Any],
    compression: dict[str, Any],
    decision_contexts: list[dict[str, Any]],
    institutional_model_context: dict[str, Any] | None,
) -> dict[str, Any]:
    by_name = {str(card.get("name") or ""): card for card in _dataset_tool_cards()}
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    unresolved: list[str] = []
    for request in requests_in:
        for name in _as_list(request.get("names")):
            card = by_name.get(str(name))
            if card is None:
                unresolved.append(str(name))
                continue
            if str(card.get("name") or "") not in seen:
                selected.append(card)
                seen.add(str(card.get("name") or ""))
    request_query = " ".join(
        part
        for request in requests_in
        for part in [str(request.get("query") or ""), " ".join(str(name) for name in _as_list(request.get("names")))]
        if part.strip()
    )
    if request_query:
        ranked, _omitted = _rank_dataset_tools(request_query, limit=12, defaults=False)
        for card in ranked:
            name = str(card.get("name") or "")
            if name in seen:
                continue
            selected.append(card)
            seen.add(name)
            if len(selected) >= 12:
                break
    if len(selected) < 6:
        fallback_query = _decision_tool_query_text(
            document=document,
            compression=compression,
            decision_contexts=decision_contexts,
            institutional_model_context=institutional_model_context,
        )
        ranked, _omitted = _rank_dataset_tools(fallback_query, limit=8, defaults=False)
        for card in ranked:
            name = str(card.get("name") or "")
            if name in seen:
                continue
            selected.append(card)
            seen.add(name)
            if len(selected) >= 8:
                break
    return {
        "requests": requests_in,
        "definitions": [_dataset_tool_public_card(card, detailed=True) for card in selected[:12]],
        "unresolved_names": unresolved[:12],
    }


def _attach_tool_lookup_trace(
    decision: dict[str, Any],
    *,
    requests_in: list[dict[str, Any]],
    definitions: dict[str, Any],
    status: str,
) -> dict[str, Any]:
    result = dict(decision or {})
    review = result.get("decision_review") if isinstance(result.get("decision_review"), dict) else {}
    review = dict(review)
    definition_names = [
        str(item.get("name") or "")
        for item in _as_list(definitions.get("definitions"))
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    ]
    review["tool_lookup"] = {
        "status": status,
        "requested": requests_in,
        "definition_names": definition_names,
        "definition_count": len(definition_names),
    }
    result["decision_review"] = review
    return result


def _llama_decision_json(
    cfg: WorkerConfig,
    *,
    document: dict[str, Any],
    compression: dict[str, Any],
    decision_contexts: list[dict[str, Any]],
    market_context: dict[str, Any] | None,
    institutional_model_context: dict[str, Any] | None,
    run_durable_progress: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    doc_text = str(document.get("text") or "")
    max_doc_chars = max(2000, min(12000, cfg.max_input_chars // 3))
    user_prompt = {
        "document": {
            "id": document.get("id"),
            "title": document.get("title"),
            "source_name": document.get("source_name"),
            "source_category": document.get("source_category"),
            "author": document.get("author"),
            "kind": document.get("kind"),
            "url": document.get("url"),
            "published_at": document.get("published_at"),
            "language": document.get("language"),
            "extra": document.get("extra") if isinstance(document.get("extra"), dict) else {},
            "text": doc_text[:max_doc_chars],
        },
        "compression": compression,
        "decision_contexts": decision_contexts,
        "market_context": _compact_market_context(market_context),
        "institutional_model_context": _compact_institutional_model_context(institutional_model_context),
    }
    user_prompt["tools"] = {
        "registry": _decision_tools_registry_context(
            document=document,
            compression=compression,
            decision_contexts=decision_contexts,
            institutional_model_context=institutional_model_context,
        )
    }
    compact_progress = _compact_run_durable_progress(run_durable_progress)
    if compact_progress:
        user_prompt["run_durable_progress"] = compact_progress
    first_decision = _llama_json(cfg, system_prompt=DECISION_REVIEW_RUNTIME_PROMPT, user_prompt=user_prompt)
    tool_requests = _extract_decision_tool_requests(first_decision)
    if not tool_requests:
        return first_decision
    definitions = _decision_tool_definition_context(
        tool_requests,
        document=document,
        compression=compression,
        decision_contexts=decision_contexts,
        institutional_model_context=institutional_model_context,
    )
    if not _as_list(definitions.get("definitions")):
        return _attach_tool_lookup_trace(first_decision, requests_in=tool_requests, definitions=definitions, status="no_definitions")
    second_prompt = dict(user_prompt)
    second_prompt["tools"] = {
        "registry": user_prompt["tools"]["registry"],
        "definitions": definitions,
    }
    second_prompt["prior_tool_request_pass"] = {
        "tool_requests": tool_requests,
        "decision_review": first_decision.get("decision_review") if isinstance(first_decision.get("decision_review"), dict) else {},
        "instruction": "Use the definitions below to complete durable reasoning. Do not repeat tool_requests unless definitions are still missing.",
    }
    try:
        second_decision = _llama_json(cfg, system_prompt=DECISION_REVIEW_RUNTIME_PROMPT, user_prompt=second_prompt)
    except AgentWorkerError as exc:
        fallback = _attach_tool_lookup_trace(first_decision, requests_in=tool_requests, definitions=definitions, status="definition_pass_failed")
        review = fallback.get("decision_review") if isinstance(fallback.get("decision_review"), dict) else {}
        blockers = _as_list(review.get("blockers"))
        blockers.append(f"tool definition pass failed: {type(exc).__name__}: {exc}")
        review["blockers"] = blockers[:8]
        fallback["decision_review"] = review
        return fallback
    return _attach_tool_lookup_trace(second_decision, requests_in=tool_requests, definitions=definitions, status="definitions_applied")


def _llama_deep_analysis_json(cfg: WorkerConfig, *, candidate: dict[str, Any]) -> dict[str, Any]:
    compression = candidate.get("compression") if isinstance(candidate.get("compression"), dict) else {}
    document = candidate.get("document") if isinstance(candidate.get("document"), dict) else {}
    source_text = str(document.get("text") or "")
    max_source_chars = max(1_000, min(cfg.idle_deep_analysis_max_text_chars, max(1_000, cfg.max_input_chars // 2)))
    user_prompt = {
        "compression": compression,
        "document": {
            "id": document.get("id"),
            "title": document.get("title"),
            "source_name": document.get("source_name"),
            "kind": document.get("kind"),
            "url": document.get("url"),
            "published_at": document.get("published_at"),
            "source_excerpt": source_text[:max_source_chars],
        },
        "candidate_score": candidate.get("candidate_score"),
    }
    result = _llama_json(cfg, system_prompt=DEEP_ANALYSIS_PROMPT, user_prompt=user_prompt)
    result.setdefault("analysis_mode", "idle_deep")
    result.setdefault("status", "success")
    result.setdefault("model", cfg.model)
    result.setdefault("prompt_version", cfg.prompt_version)
    return result


def _run_model_label(cfg: WorkerConfig) -> str:
    if not cfg.media_transcription_only:
        return str(cfg.model or "").strip()
    mode = str(cfg.media_transcription_mode or "route").strip().lower().replace("-", "_")
    ocr_model = str(cfg.media_ocr_model or "GLM-OCR").strip()
    vision_model = str(cfg.media_vision_model or "Qwen-VLM").strip()
    if mode == "ocr_only":
        return f"GLM-OCR ({ocr_model})"
    if mode == "vision_only":
        return f"Qwen-VLM ({vision_model})"
    return f"media prep: GLM-OCR ({ocr_model}) -> Qwen-VLM ({vision_model})"


def _start_run(cfg: WorkerConfig, run_type: str, *, initial_stats: dict[str, Any] | None = None) -> int:
    model_label = _run_model_label(cfg)
    payload: dict[str, Any] = {
        "run_type": run_type,
        "model_profile": model_label,
        "model_name": model_label,
        "prompt_version": cfg.prompt_version,
        "worker_id": cfg.worker_id,
        "inference_provider": "local",
    }
    if initial_stats is not None:
        payload["stats"] = dict(initial_stats)
    data = _api_post(
        cfg,
        "/api/local-agent/runs/start",
        payload,
    )
    return int(data["run"]["id"])


def _log_worker_event(event: str, **fields: Any) -> None:
    payload = {"ok": False, "event": event, **fields}
    try:
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr, flush=True)
    except Exception:
        print(f"{event}: {fields}", file=sys.stderr, flush=True)


def _mark_run_start_error(stats: dict[str, Any], exc: Exception, *, run_type: str) -> dict[str, Any]:
    if isinstance(exc, AgentWorkerError) and exc.failure_type == "local_ingestion_disabled":
        stats["local_ingestion_enabled"] = False
        stats["idle_reason"] = "local_ingestion_disabled"
        return stats
    error = f"{type(exc).__name__}: {exc}"
    stats["errors"] = int(stats.get("errors") or 0) + 1
    stats["run_start_error"] = error
    _log_worker_event("run_start_failed", run_type=run_type, error=error)
    return stats


def _finish_run(cfg: WorkerConfig, run_id: int, *, status: str, summary: str, stats: dict[str, Any] | None = None, error: str | None = None) -> None:
    try:
        _api_post(
            cfg,
            f"/api/local-agent/runs/{int(run_id)}/finish",
            {"status": status, "summary": summary, "stats": stats or {}, "error": error},
            run_id=run_id,
        )
    except Exception as exc:
        _log_worker_event(
            "finish_run_failed",
            run_id=int(run_id),
            status=status,
            summary=summary,
            error=f"{type(exc).__name__}: {exc}",
        )


def _record_error(cfg: WorkerConfig, run_id: int | None, *, error: str, document_id: int | None = None, action_kind: str = "worker_error") -> None:
    try:
        _api_post(
            cfg,
            "/api/local-agent/errors",
            {
                "run_id": run_id,
                "action_kind": action_kind,
                "document_id": document_id,
                "error": error,
                "summary": error[:240],
                "model": cfg.model,
                "prompt_version": cfg.prompt_version,
            },
            run_id=run_id,
        )
    except Exception:
        pass


def _entity_resolution_error_code(exc: Exception) -> str:
    failure_type = getattr(exc, "failure_type", None)
    if failure_type == "thoth_api_timeout":
        return "entity_resolution_endpoint_timeout"
    if failure_type == "thoth_api_connectivity":
        return "entity_resolution_endpoint_connectivity"
    if failure_type == "thoth_api_auth_error":
        return "entity_resolution_auth_error"
    if failure_type == "thoth_api_server_error":
        return "entity_resolution_endpoint_server_error"
    if failure_type == "thoth_api_http_error":
        return "entity_resolution_endpoint_http_error"
    return "entity_resolution_error"


def _local_agent_read_error_code(exc: Exception, *, prefix: str) -> str:
    failure_type = getattr(exc, "failure_type", None)
    if failure_type == "thoth_api_timeout":
        return f"{prefix}_endpoint_timeout"
    if failure_type == "thoth_api_connectivity":
        return f"{prefix}_endpoint_connectivity"
    if failure_type == "thoth_api_auth_error":
        return f"{prefix}_auth_error"
    if failure_type == "thoth_api_server_error":
        return f"{prefix}_endpoint_server_error"
    if failure_type == "thoth_api_http_error":
        return f"{prefix}_endpoint_http_error"
    return f"{prefix}_error"


def _record_worker_action(
    cfg: WorkerConfig,
    run_id: int | None,
    *,
    action_kind: str,
    summary: str,
    status: str = "success",
    document_id: int | None = None,
    response: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    try:
        _api_post(
            cfg,
            "/api/local-agent/actions",
            {
                "run_id": run_id,
                "action_kind": action_kind,
                "status": status,
                "tool_name": "agent_worker_progress",
                "endpoint": "/api/local-agent/actions",
                "document_id": document_id,
                "summary": summary[:500],
                "response": response or {},
                "error": error,
                "model": cfg.model,
                "prompt_version": cfg.prompt_version,
            },
            run_id=run_id,
        )
    except Exception:
        pass


def _candidate_count(result: dict[str, Any], field: str) -> int:
    total = len(_as_list(result.get(field)))
    nested = result.get("decision_actions")
    if isinstance(nested, dict):
        total += len(_as_list(nested.get(field)))
    return total


def _increment_stats_counter(stats: dict[str, Any], key: str, name: str, *, amount: int = 1) -> None:
    clean_name = str(name or "unknown").strip() or "unknown"
    existing = stats.get(key)
    counter = dict(existing) if isinstance(existing, dict) else {}
    counter[clean_name] = int(counter.get(clean_name) or 0) + int(amount or 0)
    stats[key] = counter


def _document_prep_failure_type(reason: str | None) -> str:
    reason_key = str(reason or "prep_incomplete").strip().split(":", 1)[0].strip().lower()
    reason_key = re.sub(r"[^a-z0-9_]+", "_", reason_key).strip("_") or "prep_incomplete"
    return f"document_prep_{reason_key}"


def _record_document_processing_issue(stats: dict[str, Any], *, reason: str | None) -> str:
    failure_type = _document_prep_failure_type(reason)
    reason_text = str(reason or failure_type).strip() or failure_type
    stats["documents_deferred_pending_prep"] = int(stats.get("documents_deferred_pending_prep") or 0) + 1
    _increment_stats_counter(stats, "document_processing_issue_types", failure_type)
    _increment_stats_counter(stats, "documents_deferred_pending_prep_reasons", reason_text)
    return failure_type


def _format_stats_counter(value: object, *, limit: int = 3) -> str:
    if not isinstance(value, dict):
        return ""
    rows = sorted(
        ((str(key), int(count or 0)) for key, count in value.items() if int(count or 0) > 0),
        key=lambda item: (-item[1], item[0]),
    )
    if not rows:
        return ""
    render_limit = max(1, int(limit or 1))
    rendered = [f"{key}={count}" for key, count in rows[:render_limit]]
    remaining = sum(count for _key, count in rows[render_limit:])
    if remaining:
        rendered.append(f"other={remaining}")
    return ", ".join(rendered)


def _feed_triage_finish_summary(stats: dict[str, Any], *, no_documents_ready: bool = False) -> str:
    parts = [f"processed {int(stats.get('documents') or 0)} document(s)"]
    media_completed = int(stats.get("media_jobs_completed") or 0)
    media_failed = int(stats.get("media_jobs_failed") or 0)
    if media_completed:
        parts.append(f"processed {media_completed} media job(s)")
    if no_documents_ready:
        parts.append("no documents ready")
    deferred = int(stats.get("documents_deferred_pending_prep") or 0)
    if deferred:
        detail = _format_stats_counter(stats.get("document_processing_issue_types"))
        suffix = f" ({detail})" if detail else ""
        parts.append(f"deferred {deferred} document(s){suffix}")
    elif no_documents_ready and int(stats.get("stage_plan_deferred_pending_media") or 0) > 0:
        parts.append(f"{int(stats.get('stage_plan_deferred_pending_media') or 0)} document(s) blocked before signal extraction (pending_media)")
    if media_failed:
        detail = _format_stats_counter(stats.get("media_job_failure_types"))
        suffix = f" ({detail})" if detail else ""
        parts.append(f"failed {media_failed} media job(s){suffix}")
    errors = int(stats.get("errors") or 0)
    if errors:
        parts.append(f"{errors} error(s)")
    return "; ".join(parts)


def _compact_run_durable_progress(value: object, *, limit: int = 12) -> list[dict[str, Any]]:
    rows = [dict(row) for row in _as_list(value) if isinstance(row, dict)]
    compact: list[dict[str, Any]] = []

    def positive_counts(payload: dict[str, Any]) -> dict[str, int]:
        out: dict[str, int] = {}
        for key, raw in payload.items():
            try:
                count = int(raw or 0)
            except Exception:
                continue
            if count > 0:
                out[str(key)] = count
        return out

    for row in rows[-max(1, int(limit)) :]:
        materialized = row.get("materialized") if isinstance(row.get("materialized"), dict) else {}
        skipped = row.get("skipped") if isinstance(row.get("skipped"), dict) else {}
        compact.append(
            {
                "document_id": row.get("document_id"),
                "summary": _summary_text(row.get("summary"), limit=220),
                "materialized": positive_counts(materialized),
                "skipped": positive_counts(skipped),
                "candidate_summaries": _as_list(row.get("candidate_summaries"))[:6],
            }
        )
    return compact


def _durable_candidate_summaries(result: dict[str, Any], *, limit: int = 8) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []

    def add(field: str, item: dict[str, Any], text: object) -> None:
        summary = _summary_text(text, limit=180)
        if not summary:
            return
        summaries.append({"kind": field, "summary": summary})

    for field, keys in (
        ("analyst_evidence", ("summary", "rationale")),
        ("model_delta_candidates", ("summary", "value_text", "rationale")),
        ("catalysts", ("title", "summary")),
        ("investment_memos", ("title", "summary_md")),
        ("research_writes", ("write_summary", "new_information", "append_md")),
        ("durable_routes", ("new_information", "rationale", "subject")),
        ("research_requests", ("title", "summary", "rationale")),
        ("dataset_rows", ("label", "rationale", "row_key")),
        ("dataset_materializations", ("title", "slug", "family", "rationale")),
        ("supply_chain_metric_bindings", ("metric_kind", "metric_key", "rationale")),
        ("instrument_request_fulfillments", ("fulfillment_summary", "summary", "rationale")),
    ):
        for item in _as_list(result.get(field)):
            if not isinstance(item, dict):
                continue
            text = next((item.get(key) for key in keys if _clean_text(item.get(key))), "")
            add(field, item, text)
            if len(summaries) >= limit:
                return summaries
    return summaries


def _record_run_durable_progress(
    holder: dict[str, Any],
    *,
    document_id: int,
    result: dict[str, Any],
    counts: dict[str, Any] | None,
    stats: dict[str, Any],
) -> None:
    counts = dict(counts or {})
    materialized = {
        "research_writes": int(counts.get("research_writes") or 0),
        "research_requests": int(counts.get("research_requests") or 0),
        "analyst_evidence": int(counts.get("analyst_evidence") or 0),
        "catalysts": int(counts.get("catalysts") or 0),
        "investment_memos": int(counts.get("investment_memos") or 0),
        "evidence_links": int(counts.get("evidence_links") or 0),
        "dataset_rows": int(counts.get("dataset_rows") or 0),
        "dataset_materializations": int(counts.get("dataset_materializations") or 0),
        "supply_chain_metric_bindings": int(counts.get("supply_chain_metric_bindings") or 0),
        "instrument_request_fulfillments": int(counts.get("instrument_request_fulfillments") or 0),
    }
    skipped = {
        "analyst_evidence_duplicate_skipped": int(counts.get("analyst_evidence_duplicate_skipped") or 0),
        "research_write_errors": int(counts.get("research_write_errors") or 0),
        "errors": int(counts.get("errors") or 0),
    }
    candidate_summaries = _durable_candidate_summaries(result)
    if not any(materialized.values()) and not any(skipped.values()) and not candidate_summaries:
        return
    progress = _compact_run_durable_progress(holder.get("durable_progress"), limit=11)
    progress.append(
        {
            "document_id": int(document_id),
            "summary": result.get("summary_short") or result.get("summary_detail"),
            "materialized": {key: value for key, value in materialized.items() if value > 0},
            "skipped": {key: value for key, value in skipped.items() if value > 0},
            "candidate_summaries": candidate_summaries[:6],
        }
    )
    holder["durable_progress"] = progress[-12:]
    stats["run_durable_progress_items"] = int(stats.get("run_durable_progress_items") or 0) + 1


def _record_decision_review_action(
    cfg: WorkerConfig,
    run_id: int,
    document_id: int,
    decision: dict[str, Any],
    *,
    decision_context_count: int,
    stats: dict[str, Any],
) -> None:
    review = decision.get("decision_review") if isinstance(decision.get("decision_review"), dict) else {}
    candidate_counts = {
        "analyst_evidence": _candidate_count(decision, "analyst_evidence"),
        "model_delta_candidates": _candidate_count(decision, "model_delta_candidates"),
        "catalysts": _candidate_count(decision, "catalysts"),
        "investment_memos": _candidate_count(decision, "investment_memos"),
        "research_writes": _candidate_count(decision, "research_writes"),
        "research_requests": _candidate_count(decision, "research_requests"),
        "evidence_links": _candidate_count(decision, "evidence_links"),
        "dataset_rows": _candidate_count(decision, "dataset_rows"),
        "dataset_materializations": _candidate_count(decision, "dataset_materializations"),
        "supply_chain_metric_bindings": _candidate_count(decision, "supply_chain_metric_bindings"),
        "instrument_request_fulfillments": _candidate_count(decision, "instrument_request_fulfillments"),
    }
    write_candidate_count = sum(candidate_counts.values())
    stats["decision_reviews"] = int(stats.get("decision_reviews", 0) or 0) + 1
    stats["decision_review_memo_candidates"] = int(stats.get("decision_review_memo_candidates", 0) or 0) + candidate_counts["investment_memos"]
    if write_candidate_count == 0:
        stats["decision_review_noops"] = int(stats.get("decision_review_noops", 0) or 0) + 1
    summary = _clean_text(review.get("summary")) or "Decision review completed without a durable write candidate."
    try:
        _api_post(
            cfg,
            "/api/local-agent/actions",
            {
                "run_id": run_id,
                "action_kind": "decision_review",
                "status": "success",
                "tool_name": "agent_record_decision_review",
                "document_id": document_id,
                "summary": summary,
                "confidence": _confidence(review.get("confidence")),
                "request": {
                    "document_id": document_id,
                    "decision_context_count": decision_context_count,
                },
                "response": {
                    "candidate_counts": candidate_counts,
                    "write_candidate_count": write_candidate_count,
                    "blockers": _as_list(review.get("blockers")),
                    "decision_targets": _as_list(decision.get("decision_targets")),
                },
            },
            run_id=run_id,
        )
    except Exception as exc:
        _log_worker_event("decision_review_action_log_failed", document_id=document_id, error=f"{type(exc).__name__}: {exc}")


def _as_list(value: object) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _parse_document_ids(value: object) -> tuple[int, ...]:
    ids: list[int] = []
    if value is None:
        return ()
    raw_items = value if isinstance(value, list) else re.split(r"[\s,;]+", str(value))
    for item in raw_items:
        text = str(item or "").strip()
        if not text:
            continue
        try:
            document_id = int(text)
        except Exception:
            continue
        if document_id > 0 and document_id not in ids:
            ids.append(document_id)
        if len(ids) >= 50:
            break
    return tuple(ids)


def _summary_text(value: object, *, limit: int = 260) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _read_token_from_path(path: object) -> str:
    token_path = str(path or "").strip()
    if not token_path:
        return ""
    with open(token_path, "r", encoding="utf-8-sig") as handle:
        token = handle.read().lstrip("\ufeff").strip()
    if not token:
        raise ValueError(f"Thoth token file is empty: {token_path}")
    return token


def _dispatch_material_compressions(context: dict[str, Any]) -> list[dict[str, Any]]:
    compressions = [row for row in _as_list(context.get("compressions")) if isinstance(row, dict)]
    return [
        row
        for row in compressions
        if str(row.get("information_gain") or "").lower() in {"medium", "high"}
        or str(row.get("document_disposition") or "").lower() in {"durable_candidate", "summary_candidate"}
    ]


def _agent_dispatch_default_title(context: dict[str, Any]) -> str:
    if not isinstance(context, dict):
        context = {}
    writes = [row for row in _as_list(context.get("writes")) if isinstance(row, dict)]
    deep_traces = [row for row in _as_list(context.get("deep_analysis_traces")) if isinstance(row, dict)]
    unresolved = [row for row in _as_list(context.get("unresolved_entities")) if isinstance(row, dict)]
    errors = [row for row in _as_list(context.get("errors")) if isinstance(row, dict)]
    material = _dispatch_material_compressions(context)
    parts: list[str] = []
    if writes:
        parts.append(f"{len(writes)} durable write{'s' if len(writes) != 1 else ''}")
    if material:
        parts.append(f"{len(material)} material intake item{'s' if len(material) != 1 else ''}")
    if deep_traces:
        parts.append(f"{len(deep_traces)} deep trace{'s' if len(deep_traces) != 1 else ''}")
    if unresolved:
        parts.append(f"{len(unresolved)} unresolved entit{'ies' if len(unresolved) != 1 else 'y'}")
    if errors:
        parts.append(f"{len(errors)} agent error{'s' if len(errors) != 1 else ''}")
    if not parts:
        parts.append("no material work product")
    return f"Agent Dispatch: {', '.join(parts[:3]).title()} - {date.today().isoformat()}"


def _is_generic_dispatch_title(title: object) -> bool:
    text = _summary_text(title, limit=180).casefold()
    if not text:
        return True
    if "daily summary" in text or "daily analyst summary" in text or "mid-day summary" in text or "midday summary" in text:
        return True
    return bool(
        re.fullmatch(
            r"(?:scheduled\s+daily\s+dispatch|agent\s+dispatch|local\s+agent\s+dispatch)\s*[-:]?\s*\d{4}-\d{2}-\d{2}",
            text,
        )
    )


def _normalize_dispatch_title(title: object, context: dict[str, Any]) -> str:
    candidate = _summary_text(title, limit=180)
    if _is_generic_dispatch_title(candidate):
        return _agent_dispatch_default_title(context)
    return candidate[:255]


def _normalize_dispatch_content_heading(content_md: str, title: str) -> str:
    lines = str(content_md or "").strip().splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("# "):
            heading = stripped[2:].strip()
            if _is_generic_dispatch_title(heading):
                lines[index] = f"# {title}"
        break
    return "\n".join(lines).strip() + "\n"


def _daily_summary_fallback_markdown(context: dict[str, Any], *, reason: str | None = None) -> str:
    if not isinstance(context, dict):
        context = {}
    compressions = [row for row in _as_list(context.get("compressions")) if isinstance(row, dict)]
    writes = [row for row in _as_list(context.get("writes")) if isinstance(row, dict)]
    deep_traces = [row for row in _as_list(context.get("deep_analysis_traces")) if isinstance(row, dict)]
    unresolved = [row for row in _as_list(context.get("unresolved_entities")) if isinstance(row, dict)]
    errors = [row for row in _as_list(context.get("errors")) if isinstance(row, dict)]
    material = _dispatch_material_compressions(context)
    title = _agent_dispatch_default_title(context)

    lines: list[str] = [
        f"# {title}",
        "",
        "## What the Agent Worked On",
        (
            f"- Context window: {context.get('lookback_hours') or 'unknown'}h; "
            f"compressions={len(compressions)}, deep_traces={len(deep_traces)}, writes={len(writes)}, unresolved={len(unresolved)}, errors={len(errors)}."
        ),
    ]
    if reason:
        lines.append(f"- Model summary fallback used: {_summary_text(reason, limit=180)}")

    if writes:
        lines.extend(["", "## Durable Writes"])
        for row in writes[:12]:
            dest = str(row.get("destination_type") or "object")
            dest_id = row.get("destination_id")
            label = f"{dest}:{dest_id}" if dest_id is not None else dest
            summary = _summary_text(row.get("write_summary") or row.get("diff_text"), limit=280)
            confidence = row.get("confidence")
            suffix = f" confidence={confidence}" if confidence is not None else ""
            lines.append(f"- {label}: {summary or 'write recorded'}{suffix}")

    if material:
        lines.extend(["", "## Material Intake"])
        for row in material[:18]:
            doc_id = row.get("document_id")
            title = _summary_text(row.get("title"), limit=120) or f"doc:{doc_id}"
            summary = _summary_text(row.get("summary_short") or row.get("summary_detail"), limit=320)
            gain = row.get("information_gain")
            disposition = row.get("document_disposition")
            ref = f" [[doc:{doc_id}]]" if doc_id is not None else ""
            meta = ", ".join(str(v) for v in (gain, disposition) if v)
            meta_s = f" ({meta})" if meta else ""
            lines.append(f"- {title}{ref}{meta_s}: {summary or 'compressed by local agent'}")

    if deep_traces:
        lines.extend(["", "## Deep Analysis Traces"])
        for row in deep_traces[:10]:
            doc_id = row.get("document_id")
            signal = row.get("signal_level") or "signal n/a"
            summary = _summary_text(row.get("summary_md") or row.get("error"), limit=320)
            ref = f" [[doc:{doc_id}]]" if doc_id is not None else ""
            lines.append(f"- Trace {row.get('id')}{ref} ({signal}): {summary or 'trace recorded'}")

    if unresolved:
        lines.extend(["", "## Unresolved Entities"])
        for row in unresolved[:10]:
            name = _summary_text(row.get("name"), limit=120) or "unknown"
            rationale = _summary_text(row.get("rationale"), limit=220)
            entity_type = row.get("entity_type") or "entity"
            lines.append(f"- {name} ({entity_type}): {rationale or 'needs resolution'}")

    if errors:
        lines.extend(["", "## Agent Errors"])
        for row in errors[:10]:
            kind = row.get("action_kind") or row.get("tool_name") or "agent_error"
            detail = _summary_text(row.get("summary") or row.get("error"), limit=260)
            lines.append(f"- {kind}: {detail or 'error recorded'}")

    if len(lines) <= 5:
        lines.extend(["", "No material local-agent summary context was available."])
    return "\n".join(lines).strip() + "\n"


def _confidence(value: object) -> float | None:
    try:
        return float(value)
    except Exception:
        return None


def _dedupe_key_value(row: dict[str, Any], field: str) -> object:
    value: object = row
    for part in str(field or "").split("."):
        if not isinstance(value, dict):
            return ""
        value = value.get(part)
    return value


def _dedupe_dicts(rows: list[Any], key_fields: tuple[str, ...]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = tuple(str(_dedupe_key_value(row, field) or "").strip().lower() for field in key_fields)
        if not any(key):
            key = (json.dumps(row, sort_keys=True, ensure_ascii=False)[:500].lower(),)
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _dedupe_text_items(values: list[Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = " ".join(str(value or "").strip().split())
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _route_text(value: object, *, limit: int = 260) -> str:
    text = " ".join(str(value or "").strip().split())
    return text[:limit].strip()


def _route_action_text(route: dict[str, Any]) -> str:
    return _route_text(route.get("action") or route.get("durable_action") or route.get("candidate_action")).strip().lower()


def _durable_route_update_type(route: dict[str, Any]) -> str:
    return _route_text(route.get("update_type") or route.get("object_update_type") or route.get("durable_update_type")).strip().lower()


def _durable_route_destination_type(route: dict[str, Any]) -> str:
    return _route_text(route.get("destination_type") or route.get("object_type") or route.get("target_type")).strip().lower()


def _durable_route_routing_key(route: dict[str, Any]) -> str:
    return _route_text(route.get("routing_key") or route.get("object_key") or route.get("ticker") or route.get("subject") or route.get("entity_name"))


def _normalize_durable_routes(routes: list[Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in routes:
        if not isinstance(item, dict):
            continue
        route = dict(item)
        action = _route_action_text(route)
        if action in DURABLE_ROUTE_BANNED_ACTIONS:
            continue
        if action and action not in DURABLE_ROUTE_ALLOWED_CANDIDATE_ACTIONS:
            continue
        update_type = _durable_route_update_type(route)
        destination_type = _durable_route_destination_type(route)
        routing_key = _durable_route_routing_key(route)
        new_information = _route_text(route.get("new_information") or route.get("new_info") or route.get("summary"), limit=1000)
        claim_indexes = _as_list(route.get("claim_indexes") or route.get("source_claim_indexes"))
        if not update_type or not destination_type or not routing_key:
            continue
        if not new_information and not claim_indexes:
            continue
        route.pop("action", None)
        route.pop("durable_action", None)
        route["candidate_action"] = action or _route_text(route.get("candidate_action")) or "review_for_update"
        route["update_type"] = update_type
        route["destination_type"] = destination_type
        route["routing_key"] = routing_key
        if not _route_text(route.get("memory_posture")):
            route["memory_posture"] = "change_request" if update_type in {"supply_chain_update", "source_chain_update"} else "observed_claim"
        if not _route_text(route.get("requested_change_type")):
            route["requested_change_type"] = (
                "add_source_chain"
                if update_type == "source_chain_update"
                else "update_supplier_role"
                if update_type == "supply_chain_update"
                else "update_model_fact"
            )
        normalized.append(route)
    return normalized


MODEL_ROUTE_CLAIM_KINDS = {
    "capacity_update",
    "pricing_update",
    "order_update",
    "customer_update",
    "supply_chain_update",
    "financial_metric",
    "technical_update",
}


def _claim_text_blob(claim: dict[str, Any]) -> str:
    parts = [
        str(claim.get("claim_text") or ""),
        str(claim.get("claim_kind") or ""),
        json.dumps(claim.get("numeric_facts") or [], ensure_ascii=False, default=str),
    ]
    payload = claim.get("payload") if isinstance(claim.get("payload"), dict) else {}
    if payload:
        parts.append(json.dumps(payload, ensure_ascii=False, default=str)[:1600])
    return " ".join(part for part in parts if part)


def _claim_first_entity(claim: dict[str, Any]) -> str:
    entities = _dedupe_text_items(_as_list(claim.get("entities")))
    if entities:
        return entities[0]
    for fact in _as_list(claim.get("numeric_facts")):
        if isinstance(fact, dict):
            entity = _route_text(fact.get("entity"), limit=120)
            if entity:
                return entity
    return _route_text(claim.get("entity") or claim.get("subject"), limit=120)


def _synthesized_route_update_type(claim: dict[str, Any], blob_lower: str) -> str:
    kind = str(claim.get("claim_kind") or "").strip().lower()
    if "price" in kind or any(token in blob_lower for token in ("pricing", "selling price", "price negotiation", "material cost")):
        return "pricing_update"
    if any(token in blob_lower for token in ("capex", "capital investment", "capacity", "wpm", "wafer", "substrate", "fy2028")):
        return "capacity_update"
    if any(token in blob_lower for token in ("ramp", "roadmap", "rubin", "blackwell", "hopper", "mi450", "hbm4", "hbm4e")):
        return "product_roadmap_update"
    if any(token in blob_lower for token in ("forecast", "estimate", "target", "fy2030", "operating profit", "sales plan", "eps", "revenue")):
        return "consensus_estimate_update"
    if any(token in blob_lower for token in ("order", "customer", "microsoft", "switch", "supply", "demand", "tightness")):
        return "supply_chain_update"
    return "guidance_update" if "guidance" in blob_lower or "management" in blob_lower else "supply_chain_update"


def _claim_is_material_model_fact(claim: dict[str, Any]) -> bool:
    kind = str(claim.get("claim_kind") or "").strip().lower()
    blob = _claim_text_blob(claim).lower()
    if kind in MODEL_ROUTE_CLAIM_KINDS:
        return True
    return bool(
        re.search(
            r"\b(?:fy|cy)\s*20\d{2}\b|\b(?:capex|capital investment|capacity|pricing|wpm|wafer|substrate|rubin|blackwell|hopper|mi450|hbm|dram|nand|cowos|operating profit|sales plan|demand|supply|customer|order)\b",
            blob,
            flags=re.IGNORECASE,
        )
    )


def _synthesize_durable_routes_from_claims(result: dict[str, Any], *, limit: int = 6) -> list[dict[str, Any]]:
    routes: list[dict[str, Any]] = []
    for idx, claim in enumerate(_as_list(result.get("atomic_claims"))):
        if not isinstance(claim, dict) or not _claim_is_material_model_fact(claim):
            continue
        subject = _claim_first_entity(claim)
        if not subject:
            continue
        blob = _claim_text_blob(claim)
        blob_lower = blob.lower()
        destination_type = "research_subject" if subject.upper() in {"DRAM", "NAND", "HBM", "COWOS"} or "substrate" in blob_lower else "company"
        update_type = _synthesized_route_update_type(claim, blob_lower)
        routes.append(
            {
                "subject": subject,
                "destination_type": destination_type,
                "update_type": update_type,
                "candidate_action": "review_for_update",
                "memory_posture": "change_request" if update_type in {"supply_chain_update", "source_chain_update"} else "observed_claim",
                "requested_change_type": "add_source_chain" if update_type == "source_chain_update" else "update_supplier_role" if update_type == "supply_chain_update" else "update_model_fact",
                "routing_key": f"{subject}:{update_type}",
                "entity_type": "technology" if destination_type == "research_subject" else "company",
                "role": "primary_subject" if idx == 0 else "materially_affected",
                "materiality": "high" if any(token in blob_lower for token in ("fy2030", "jpy", "capacity", "capex", "mi450", "rubin")) else "medium",
                "novelty": "unknown",
                "temporal_posture": "forward_looking" if re.search(r"\b(?:fy|cy)\s*20\d{2}\b|\bq[1-4]\s*20\d{2}\b|\bfy2028\b|\bfy2030\b", blob_lower) else "mixed",
                "evidence_tier": "unknown",
                "new_information": _route_text(claim.get("claim_text") or blob, limit=1000),
                "confidence": _confidence(claim.get("confidence")) or 0.72,
                "claim_indexes": [idx],
                "rationale": "Material model fact requires later durable review; synthesized because the signal response omitted an explicit durable route.",
            }
        )
        if len(routes) >= limit:
            break
    return routes


DECISION_RESULT_LIST_FIELDS = (
    "decision_targets",
    "analyst_evidence",
    "model_delta_candidates",
    "catalysts",
    "investment_memos",
    "research_writes",
    "research_requests",
    "evidence_links",
    "dataset_rows",
    "dataset_materializations",
    "supply_chain_metric_bindings",
    "instrument_request_fulfillments",
)


DECISION_BULK_PAYLOAD_FIELDS = (
    "analyst_evidence",
    "catalysts",
    "investment_memos",
    "evidence_links",
    "research_requests",
    "dataset_rows",
    "dataset_materializations",
    "supply_chain_metric_bindings",
    "instrument_request_fulfillments",
)


def _dedupe_result(result: dict[str, Any]) -> dict[str, Any]:
    result = dict(result or {})
    result["atomic_claims"] = _dedupe_dicts(_as_list(result.get("atomic_claims")), ("claim_text", "claim_kind"))
    result["entity_refs"] = _dedupe_dicts(_as_list(result.get("entity_refs")), ("name", "entity_type"))
    result["supply_chain_refs"] = _dedupe_dicts(_as_list(result.get("supply_chain_refs")), ("name",))
    result["technology_refs"] = _dedupe_dicts(_as_list(result.get("technology_refs")), ("name",))
    result["summary_routes"] = _dedupe_dicts(_as_list(result.get("summary_routes")), ("entity_name", "name", "subject", "role"))
    durable_routes = _normalize_durable_routes(_as_list(result.get("durable_routes")))
    if not durable_routes:
        durable_routes = _normalize_durable_routes(_synthesize_durable_routes_from_claims(result))
    if durable_routes and str(result.get("document_disposition") or "").strip().lower() in {"summary_only", "summary_candidate", ""}:
        result["document_disposition"] = "durable_candidate"
        result.setdefault("information_gain", "medium")
    result["durable_routes"] = _dedupe_dicts(
        durable_routes,
        ("subject", "entity_name", "destination_type", "update_type", "routing_key", "role"),
    )
    result["entity_routing"] = _dedupe_dicts(_as_list(result.get("entity_routing")), ("entity_name", "name", "lane", "role"))
    result["unresolved_entities"] = _dedupe_dicts(_as_list(result.get("unresolved_entities")), ("name", "entity_type"))
    result["research_writes"] = _dedupe_dicts(_as_list(result.get("research_writes")), ("subject", "write_summary", "append_md"))
    result["research_requests"] = _dedupe_dicts(_as_list(result.get("research_requests")), ("title", "intent", "summary"))
    result["decision_targets"] = _dedupe_dicts(_as_list(result.get("decision_targets")), ("ticker", "company_id", "issuer_id", "subject", "name"))
    result["analyst_evidence"] = _dedupe_dicts(
        _as_list(result.get("analyst_evidence")),
        (
            "ticker",
            "company_id",
            "evidence_kind",
            "metric_key",
            "estimate_metric",
            "forecast.product_family",
            "product_family",
            "forecast.segment",
            "segment",
            "forecast.value",
            "forecast_value",
            "estimate_value",
            "forecast.value_text",
            "forecast_status",
            "unit",
            "estimate_unit",
            "summary",
            "source_document_id",
            "estimate_owner",
            "underlying_firm",
            "source_name",
        ),
    )
    result["model_delta_candidates"] = _dedupe_dicts(
        _as_list(result.get("model_delta_candidates")),
        (
            "canonical_metric_key",
            "raw_subject_text",
            "product_family",
            "segment",
            "value_number",
            "value_text",
            "unit",
            "period",
            "summary",
            "source_document_id",
            "estimate_owner",
            "underlying_firm",
        ),
    )
    result["catalysts"] = _dedupe_dicts(_as_list(result.get("catalysts")), ("ticker", "company_id", "title", "event_date"))
    result["investment_memos"] = _dedupe_dicts(_as_list(result.get("investment_memos")), ("ticker", "company_id", "title", "summary_md", "thesis_md"))
    result["evidence_links"] = _dedupe_dicts(_as_list(result.get("evidence_links")), ("target_type", "target_id", "evidence_object_type", "evidence_object_id", "relation"))
    result["dataset_rows"] = _dedupe_dicts(_as_list(result.get("dataset_rows")), ("id", "dataset_id", "row_key", "rows", "label"))
    result["dataset_materializations"] = _dedupe_dicts(_as_list(result.get("dataset_materializations")), ("family", "dataset_family", "slug", "title", "rows"))
    result["supply_chain_metric_bindings"] = _dedupe_dicts(
        _as_list(result.get("supply_chain_metric_bindings")),
        ("id", "supply_chain_id", "chain_id", "dataset_id", "dataset_view_id", "scope_type", "scope_key", "metric_kind", "metric_key", "column_key"),
    )
    result["instrument_request_fulfillments"] = _dedupe_dicts(
        _as_list(result.get("instrument_request_fulfillments")),
        ("id", "instrument_request_id", "request_id", "fulfillment_summary", "summary"),
    )
    return result


SIGNAL_EXTRACTION_DEFER_FIELDS = (*DECISION_RESULT_LIST_FIELDS, "decision_actions")


def _defer_signal_extraction_side_effects(result: dict[str, Any], stats: dict[str, Any] | None = None) -> dict[str, Any]:
    """Keep the first pass to signal extraction; durable mutations are decided later."""
    cleaned = dict(result or {})
    deferred: dict[str, Any] = {}
    for field in SIGNAL_EXTRACTION_DEFER_FIELDS:
        value = cleaned.get(field)
        if value in (None, "", [], {}):
            continue
        deferred[field] = value
        cleaned[field] = [] if isinstance(value, list) else {}
    if deferred:
        cleaned["durable_reasoning_deferred"] = {
            "fields": sorted(deferred),
            "candidate_summaries": _durable_candidate_summaries(deferred, limit=8),
            "reason": "Signal-extraction pass cannot emit direct durable writes; decision pass must use search/dedupe context.",
        }
        if stats is not None:
            stats["signal_extraction_deferred_fields"] = int(stats.get("signal_extraction_deferred_fields") or 0) + len(deferred)
    return cleaned


def _nested_decision_list(result: dict[str, Any], field: str) -> list[Any]:
    rows = _as_list(result.get(field))
    nested = result.get("decision_actions")
    if isinstance(nested, dict):
        rows.extend(_as_list(nested.get(field)))
    return rows


def _merge_decision_result(result: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    merged = dict(result or {})
    for field in DECISION_RESULT_LIST_FIELDS:
        merged[field] = _as_list(merged.get(field)) + _nested_decision_list(decision, field)
    if isinstance(decision.get("decision_review"), dict):
        merged["decision_review"] = decision.get("decision_review")
    elif decision.get("decision_review"):
        merged["decision_review"] = {"summary": str(decision.get("decision_review"))}
    return _dedupe_result(merged)


def _should_skip_side_effects(result: dict[str, Any]) -> bool:
    disposition = str(result.get("document_disposition") or result.get("disposition") or "").strip().lower()
    information_gain = str(result.get("information_gain") or "").strip().lower()
    return disposition in {"skip", "noop"} or information_gain == "none"


def _submit_claims(cfg: WorkerConfig, run_id: int, document_id: int, compression_id: int, result: dict[str, Any]) -> int:
    created = 0
    for claim in _as_list(result.get("atomic_claims")):
        if not isinstance(claim, dict):
            continue
        claim_text = str(claim.get("claim_text") or "").strip()
        if not claim_text:
            continue
        entities = _dedupe_text_items(_as_list(claim.get("entities")))
        if not entities:
            entities = _dedupe_text_items([v.get("name") for v in _as_list(result.get("entity_refs")) if isinstance(v, dict)])
        for entity in entities or ["unresolved"]:
            payload = {
                "run_id": run_id,
                "compression_id": compression_id,
                "document_id": document_id,
                "entity_type": "unknown",
                "entity_name": entity,
                "claim_kind": str(claim.get("claim_kind") or "business_update"),
                "claim_text": claim_text,
                "confidence": _confidence(claim.get("confidence")),
                "routing_reason": "local llama.cpp signal extraction",
                "payload": dict(claim),
            }
            try:
                response = _api_post(cfg, "/api/local-agent/claims", payload, run_id=run_id)
                if response.get("created", True):
                    created += 1
            except Exception as exc:
                _record_error(cfg, run_id, error=f"claim write failed: {exc}", document_id=document_id, action_kind="claim_write_error")
    return created


def _submit_unresolved(cfg: WorkerConfig, run_id: int, document_id: int, result: dict[str, Any]) -> int:
    created = 0
    for item in _as_list(result.get("unresolved_entities")):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        payload = {
            "run_id": run_id,
            "document_id": document_id,
            "name": name,
            "entity_type": item.get("entity_type"),
            "rationale": item.get("rationale"),
            "confidence": _confidence(item.get("confidence")),
            "candidate_payload": item,
        }
        try:
            _api_post(cfg, "/api/local-agent/unresolved-entities", payload, run_id=run_id)
            created += 1
        except Exception as exc:
            _record_error(cfg, run_id, error=f"unresolved entity write failed: {exc}", document_id=document_id, action_kind="unresolved_write_error")
    return created


def _effective_research_write_evidence_tier(write: dict[str, Any], document: dict[str, Any] | None) -> str:
    tier = str(write.get("evidence_tier") or "").strip().lower()
    doc = document if isinstance(document, dict) else {}
    doc_text = " ".join(
        str(value or "").strip().lower()
        for value in [
            doc.get("source_name"),
            doc.get("source_category"),
            doc.get("source_kind"),
            doc.get("kind"),
            ((doc.get("extra") or {}).get("connector") if isinstance(doc.get("extra"), dict) else None),
        ]
        if str(value or "").strip()
    )
    if any(token in doc_text for token in ("x_list", "twitter", "tweet")) or "social" in doc_text:
        return "analyst_commentary"
    return tier


def _research_write_policy_skip_reason(
    *,
    confidence: float | None,
    materiality: str,
    novelty: str,
    temporal_posture: str,
    evidence_tier: str,
    information_gain: str,
    new_information: str,
) -> str:
    if confidence is None or confidence < 0.7:
        return "confidence_below_durable_threshold"
    if information_gain in {"", "none", "low"}:
        return "insufficient_information_gain"
    if not materiality or materiality not in {"high", "medium"}:
        return "insufficient_materiality"
    if not novelty or novelty in {"duplicate", "background"}:
        return "insufficient_novelty"
    if not temporal_posture:
        return "missing_temporal_posture"
    if temporal_posture in {"backward_looking", "historical_context", "retrospective"} and novelty not in {"conflict", "outdated"}:
        return "backward_looking_context"
    if len(str(new_information or "").strip()) < 20:
        return "missing_new_information"
    if evidence_tier in RESEARCH_WRITE_HIGH_BAR_TIERS and (confidence < 0.85 or materiality != "high"):
        return "analyst_social_write_below_high_bar"
    return ""


def _auto_write_research(
    cfg: WorkerConfig,
    run_id: int,
    document_id: int,
    result: dict[str, Any],
    *,
    document: dict[str, Any] | None = None,
    stats: dict[str, Any] | None = None,
) -> int:
    if not cfg.auto_write:
        return 0
    writes = _as_list(result.get("research_writes"))
    created = 0
    for write in writes:
        if not isinstance(write, dict):
            continue
        subject = str(write.get("subject") or "").strip()
        append_md = str(write.get("append_md") or "").strip()
        confidence = _confidence(write.get("confidence"))
        materiality = str(write.get("materiality") or "").strip().lower()
        novelty = str(write.get("novelty") or "").strip().lower()
        temporal_posture = str(write.get("temporal_posture") or "").strip().lower()
        evidence_tier = _effective_research_write_evidence_tier(write, document)
        new_information = str(write.get("new_information") or "").strip()
        if not subject or not append_md:
            continue
        information_gain = str(write.get("information_gain") or result.get("information_gain") or "").strip().lower()
        if stats is not None:
            stats["research_write_candidates"] = int(stats.get("research_write_candidates", 0) or 0) + 1
        skip_reason = _research_write_policy_skip_reason(
            confidence=confidence,
            materiality=materiality,
            novelty=novelty,
            temporal_posture=temporal_posture,
            evidence_tier=evidence_tier,
            information_gain=information_gain,
            new_information=new_information,
        )
        if skip_reason:
            if stats is not None:
                stats["research_write_skipped_policy"] = int(stats.get("research_write_skipped_policy", 0) or 0) + 1
            continue
        payload = {
            "run_id": run_id,
            "destination": {"type": "research_report", "subject": subject},
            "append_md": append_md,
            "write_summary": str(write.get("write_summary") or f"Document {document_id} update"),
            "source_document_ids": write.get("source_document_ids") or [document_id],
            "confidence": confidence,
            "model": cfg.model,
            "materiality": materiality,
            "novelty": novelty,
            "temporal_posture": temporal_posture,
            "evidence_tier": evidence_tier,
            "information_gain": information_gain,
            "new_information": new_information,
            "routing": {
                "materiality": materiality,
                "novelty": novelty,
                "temporal_posture": temporal_posture,
                "evidence_tier": evidence_tier,
                "information_gain": information_gain,
                "new_information": new_information,
                "rationale": write.get("rationale"),
            },
        }
        try:
            _api_post(cfg, "/api/local-agent/research-docs/write", payload, run_id=run_id)
            created += 1
        except Exception as exc:
            if stats is not None:
                stats["research_write_errors"] = int(stats.get("research_write_errors", 0) or 0) + 1
            _record_error(cfg, run_id, error=f"research write failed for {subject}: {exc}", document_id=document_id, action_kind="research_write_error")
    return created


def _int_or_none(value: object) -> int | None:
    try:
        if str(value or "").strip().isdigit():
            return int(str(value).strip())
    except Exception:
        return None
    return None


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _ticker_text(value: object) -> str:
    ticker = _clean_text(value).upper().lstrip("$")
    return ticker if re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,15}", ticker) else ""


def _target_query(payload: dict[str, Any]) -> dict[str, Any]:
    target = payload.get("target") if isinstance(payload.get("target"), dict) else {}
    subject_id = _int_or_none(payload.get("subject_id") or payload.get("canonical_subject_id") or target.get("subject_id"))
    company_id = _int_or_none(payload.get("company_id") or target.get("company_id"))
    issuer_id = _int_or_none(payload.get("issuer_id") or target.get("issuer_id"))
    ticker = _ticker_text(payload.get("ticker") or payload.get("symbol") or target.get("ticker") or target.get("symbol"))
    if company_id is not None:
        return {"company_id": company_id}
    if issuer_id is not None:
        return {"issuer_id": issuer_id}
    if subject_id is not None:
        return {"subject_id": subject_id}
    if ticker:
        return {"ticker": ticker}
    return {}


def _target_key(query: dict[str, Any]) -> str:
    if query.get("company_id") is not None:
        return f"company:{query['company_id']}"
    if query.get("issuer_id") is not None:
        return f"issuer:{query['issuer_id']}"
    if query.get("subject_id") is not None:
        return f"subject:{query['subject_id']}"
    if query.get("ticker"):
        return f"ticker:{str(query['ticker']).upper()}"
    return ""


def _target_name(payload: dict[str, Any]) -> str:
    target = payload.get("target") if isinstance(payload.get("target"), dict) else {}
    return _clean_text(
        payload.get("subject")
        or payload.get("entity_name")
        or payload.get("name")
        or payload.get("label")
        or payload.get("company")
        or payload.get("raw_subject_text")
        or target.get("subject")
        or target.get("name")
        or target.get("label")
    )


def _candidate_target_payloads(document: dict[str, Any], result: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []

    def add(value: object, *, source: str) -> None:
        if isinstance(value, dict):
            row = dict(value)
        else:
            row = {"name": _clean_text(value)}
        row["_source"] = source
        if _target_query(row) or _target_name(row):
            candidates.append(row)

    for field in ("decision_targets", "analyst_evidence", "model_delta_candidates", "catalysts", "investment_memos"):
        for item in _as_list(result.get(field)):
            add(item, source=field)
    for field in ("entity_refs", "summary_routes", "durable_routes", "entity_routing"):
        for item in _as_list(result.get(field)):
            if not isinstance(item, dict):
                continue
            entity_type = str(item.get("entity_type") or "").strip().lower()
            if entity_type and entity_type not in {"company", "issuer", "unknown"}:
                continue
            add(item, source=field)
    for item in _as_list(result.get("research_writes")):
        if isinstance(item, dict):
            add({"name": item.get("subject"), "confidence": item.get("confidence")}, source="research_writes")
    hay = " ".join(str(document.get(key) or "") for key in ("title", "text"))[:4000]
    for ticker in re.findall(r"(?<![A-Za-z0-9])\$([A-Z][A-Z0-9.\-]{0,7})(?![A-Za-z0-9])", hay):
        add({"ticker": ticker, "confidence": 0.7}, source="cashtag")
    return candidates


def _target_from_search_hit(hit: dict[str, Any], *, fallback_name: str, confidence: float | None) -> dict[str, Any] | None:
    kind = str(hit.get("kind") or "").strip().lower()
    query: dict[str, Any] = {}
    company_id = _int_or_none(hit.get("company_id"))
    issuer_id = _int_or_none(hit.get("issuer_id"))
    subject_id = _int_or_none(hit.get("subject_id"))
    ticker = _ticker_text(hit.get("ticker"))
    security_type = str(hit.get("security_type") or "").strip().upper()
    asset_class = str(hit.get("asset_class") or "").strip().upper()
    if company_id is not None:
        query["company_id"] = company_id
    elif issuer_id is not None:
        query["issuer_id"] = issuer_id
    elif subject_id is not None:
        query["subject_id"] = subject_id
    elif kind == "company":
        company_id = _int_or_none(hit.get("id"))
        if company_id is not None:
            query["company_id"] = company_id
    elif kind == "issuer":
        issuer_id = _int_or_none(hit.get("id"))
        if issuer_id is not None:
            query["issuer_id"] = issuer_id
    elif kind == "research_subject":
        subject_key = _clean_text(hit.get("subject_key"))
        if subject_key.startswith("company:"):
            company_id = _int_or_none(subject_key.split(":", 1)[1])
            if company_id is not None:
                query["company_id"] = company_id
        elif subject_key.startswith("issuer:"):
            issuer_id = _int_or_none(subject_key.split(":", 1)[1])
            if issuer_id is not None:
                query["issuer_id"] = issuer_id
        elif subject_id is None:
            subject_id = _int_or_none(hit.get("id"))
            if subject_id is not None:
                query["subject_id"] = subject_id
    if (
        not query
        and kind == "research_instrument"
        and (security_type or asset_class)
        and security_type not in {"", "STK"}
        and asset_class not in {"", "STK", "EQUITY", "STOCK"}
    ):
        return None
    if not query and ticker:
        query["ticker"] = ticker
    if not query:
        return None
    return {
        "query": query,
        "label": _clean_text(hit.get("label") or fallback_name),
        "ticker": ticker or None,
        "company_id": query.get("company_id"),
        "issuer_id": query.get("issuer_id"),
        "subject_id": query.get("subject_id"),
        "confidence": confidence,
        "resolution": hit,
    }


def _resolve_decision_targets(cfg: WorkerConfig, run_id: int, document_id: int, document: dict[str, Any], result: dict[str, Any]) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    seen: set[str] = set()
    searched: set[str] = set()

    def add_target(target: dict[str, Any]) -> None:
        query = target.get("query") if isinstance(target.get("query"), dict) else _target_query(target)
        key = _target_key(query)
        if not key or key in seen:
            return
        seen.add(key)
        row = dict(target)
        row["query"] = query
        row.setdefault("company_id", query.get("company_id"))
        row.setdefault("issuer_id", query.get("issuer_id"))
        row.setdefault("subject_id", query.get("subject_id"))
        row.setdefault("ticker", query.get("ticker"))
        ticker = _ticker_text(row.get("ticker") or query.get("ticker"))
        if ticker:
            seen.add(f"ticker:{ticker}")
        targets.append(row)

    for candidate in _candidate_target_payloads(document, result):
        confidence = _confidence(candidate.get("confidence"))
        query = _target_query(candidate)
        name = _target_name(candidate)
        if query:
            add_target(
                {
                    "query": query,
                    "label": name or _clean_text(candidate.get("ticker") or candidate.get("company_id") or candidate.get("issuer_id")),
                    "ticker": _ticker_text(candidate.get("ticker") or candidate.get("symbol")) or None,
                    "company_id": query.get("company_id"),
                    "issuer_id": query.get("issuer_id"),
                    "subject_id": query.get("subject_id"),
                    "confidence": confidence,
                    "source": candidate.get("_source"),
                    "rationale": candidate.get("rationale"),
                }
            )
        elif name and name.lower() not in searched:
            searched.add(name.lower())
            try:
                rows = _api_get(
                    cfg,
                    "/api/local-agent/entities/search",
                    run_id=run_id,
                    params={"q": name, "limit": 3, "run_id": run_id},
                    timeout_s=LOCAL_AGENT_LOOKUP_TIMEOUT_S,
                ).get("results") or []
            except Exception as exc:
                error_code = _entity_resolution_error_code(exc)
                _record_error(
                    cfg,
                    run_id,
                    error=f"{error_code}: entity resolution failed for {name}: {exc}",
                    document_id=document_id,
                    action_kind=error_code,
                )
                rows = []
            for hit in rows:
                if not isinstance(hit, dict):
                    continue
                target = _target_from_search_hit(hit, fallback_name=name, confidence=confidence)
                if target is not None:
                    target["source"] = candidate.get("_source")
                    add_target(target)
                    break
        if len(targets) >= cfg.decision_max_targets:
            break
    return targets[: cfg.decision_max_targets]


def _read_market_context(cfg: WorkerConfig, run_id: int, stats: dict[str, Any]) -> dict[str, Any]:
    if not cfg.decision_spine_enabled:
        return {}
    try:
        data = _api_get(
            cfg,
            "/api/local-agent/market-context",
            run_id=run_id,
            params={
                "base_currency": cfg.market_base_currency or "",
                "live_quotes": str(bool(cfg.market_live_quotes)).lower(),
                "max_items": cfg.market_max_items,
                "run_id": run_id,
            },
            timeout_s=LOCAL_AGENT_CONTEXT_TIMEOUT_S,
        )
        stats["market_context_reads"] += 1
        return data.get("context") or {}
    except Exception as exc:
        stats["errors"] += 1
        error_code = _local_agent_read_error_code(exc, prefix="market_context")
        _record_error(cfg, run_id, error=f"{error_code}: market context read failed: {exc}", action_kind=error_code)
        return {}


def _read_institutional_model_context(
    cfg: WorkerConfig,
    run_id: int,
    document_id: int,
    document: dict[str, Any],
    decision_contexts: list[dict[str, Any]],
    stats: dict[str, Any],
) -> dict[str, Any]:
    if not cfg.decision_spine_enabled:
        return {}
    subject_id = None
    if len(decision_contexts) == 1:
        context = decision_contexts[0].get("context") if isinstance(decision_contexts[0].get("context"), dict) else {}
        subject = context.get("subject") if isinstance(context.get("subject"), dict) else {}
        subject_id = _int_or_none(subject.get("id"))
    query = (
        str(document.get("author") or "").strip()
        or str(document.get("source_name") or "").strip()
        or str(document.get("title") or "").strip()[:120]
    )
    try:
        params: dict[str, Any] = {
            "document_id": document_id,
            "q": query,
            "run_id": run_id,
            "max_source_actors": 12,
            "max_subjects": 8,
            "max_state_vectors": 8,
        }
        if subject_id is not None:
            params["subject_id"] = subject_id
        data = _api_get(cfg, "/api/local-agent/institutional/model-context", run_id=run_id, params=params)
        stats["institutional_model_context_reads"] += 1
        return data.get("context") or {}
    except Exception as exc:
        stats["errors"] += 1
        _record_error(cfg, run_id, error=f"institutional model context read failed: {exc}", document_id=document_id, action_kind="institutional_model_context_error")
        return {}


def _decision_target_label(target: dict[str, Any]) -> str:
    query = target.get("query") if isinstance(target.get("query"), dict) else {}
    label = _clean_text(target.get("label") or target.get("name") or target.get("subject") or "")
    ticker = _ticker_text(target.get("ticker") or query.get("ticker") or query.get("symbol")) or _clean_text(query.get("ticker") or "")
    if label and ticker:
        return f"{label} ({ticker})"
    return label or ticker or _clean_text(target)


def _decision_context_error_is_unresolved(value: object) -> bool:
    text = str(value or "").lower()
    return "company or issuer not found" in text or "not_tracked_or_unresolved_identity" in text


def _record_decision_context_unresolved(
    cfg: WorkerConfig,
    run_id: int,
    document_id: int,
    target: dict[str, Any],
    stats: dict[str, Any],
    *,
    reason: object = None,
    suggestions: object = None,
) -> None:
    stats["decision_context_unresolved"] = int(stats.get("decision_context_unresolved") or 0) + 1
    label = _decision_target_label(target)
    message = _clean_text(reason) or "No tracked company or issuer matched this decision-context target."
    _record_worker_action(
        cfg,
        run_id,
        action_kind="decision_context_unresolved",
        status="success",
        document_id=document_id,
        summary=f"decision context target unresolved/out-of-canon: {label}; {message}",
        response={
            "target": target,
            "reason": message,
            "search_suggestions": _as_list(suggestions)[:5],
        },
    )


def _compact_decision_search_suggestion(hit: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "kind",
        "id",
        "label",
        "name",
        "ticker",
        "company_id",
        "issuer_id",
        "subject_key",
        "security_type",
        "asset_class",
        "exchange",
        "matched_query",
        "score",
        "confidence",
    )
    return {key: hit.get(key) for key in fields if hit.get(key) not in (None, "", [])}


def _decision_context_unresolved_item(value: dict[str, Any], *, fallback_target: dict[str, Any] | None = None) -> dict[str, Any]:
    item = dict(value or {})
    target = item.get("target") if isinstance(item.get("target"), dict) else fallback_target or {}
    item["target"] = dict(target or {})
    suggestions = [dict(row) for row in _as_list(item.get("search_suggestions")) if isinstance(row, dict)]
    item["search_suggestions"] = suggestions[:5]
    item["message"] = _clean_text(item.get("message") or item.get("reason") or item.get("error")) or "No tracked company or issuer matched this target."
    return item


def _decision_context_retry_prompt_items(unresolved_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prompt_items: list[dict[str, Any]] = []
    for index, item in enumerate(unresolved_items):
        suggestions = [row for row in _as_list(item.get("search_suggestions")) if isinstance(row, dict)]
        if not suggestions:
            continue
        target = item.get("target") if isinstance(item.get("target"), dict) else {}
        prompt_items.append(
            {
                "source_index": index,
                "target": target,
                "target_label": _decision_target_label(target),
                "message": item.get("message") or item.get("reason"),
                "search_suggestions": [_compact_decision_search_suggestion(row) for row in suggestions[:5]],
            }
        )
    return prompt_items


def _decision_resolution_choice_key(choice: dict[str, Any], *names: str) -> int | None:
    for name in names:
        value = choice.get(name)
        if value is None:
            continue
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed >= 0:
            return parsed
    return None


def _retry_unresolved_decision_contexts(
    cfg: WorkerConfig,
    run_id: int,
    document_id: int,
    document: dict[str, Any],
    result: dict[str, Any],
    unresolved_items: list[dict[str, Any]],
    stats: dict[str, Any],
) -> list[dict[str, Any]]:
    contexts: list[dict[str, Any]] = []
    if not unresolved_items:
        return contexts
    prompt_items = _decision_context_retry_prompt_items(unresolved_items)
    no_suggestion_indexes = {idx for idx, item in enumerate(unresolved_items) if not _as_list(item.get("search_suggestions"))}
    for idx in sorted(no_suggestion_indexes):
        item = unresolved_items[idx]
        target = item.get("target") if isinstance(item.get("target"), dict) else {}
        _record_decision_context_unresolved(cfg, run_id, document_id, target, stats, reason=item.get("message") or "No search suggestions were available.", suggestions=[])
        stats["decision_context_unresolved_retry_skipped"] = int(stats.get("decision_context_unresolved_retry_skipped") or 0) + 1
    if not prompt_items:
        return contexts

    stats["decision_context_unresolved_retries"] = int(stats.get("decision_context_unresolved_retries") or 0) + 1
    retry_targets: list[dict[str, Any]] = []
    skipped_indexes: set[int] = set(no_suggestion_indexes)
    try:
        decision = _llama_json(
            cfg,
            system_prompt=DECISION_CONTEXT_RESOLUTION_PROMPT,
            user_prompt={
                "task": "resolve_unresolved_decision_context_targets_once",
                "document": {
                    "id": document.get("id"),
                    "title": document.get("title"),
                    "source_name": document.get("source_name"),
                    "author": document.get("author"),
                    "published_at": document.get("published_at"),
                },
                "signal": {
                    "summary_short": result.get("summary_short"),
                    "entity_refs": _as_list(result.get("entity_refs"))[:12],
                    "durable_routes": _as_list(result.get("durable_routes"))[:8],
                    "unresolved_entities": _as_list(result.get("unresolved_entities"))[:8],
                },
                "unresolved_targets": prompt_items,
                "instruction": "Select at most one provided suggestion per unresolved target, or skip_unresolved. This is the only retry.",
            },
        )
    except Exception as exc:
        stats["decision_context_unresolved_retry_failed"] = int(stats.get("decision_context_unresolved_retry_failed") or 0) + 1
        for index, item in enumerate(unresolved_items):
            if index in skipped_indexes:
                continue
            target = item.get("target") if isinstance(item.get("target"), dict) else {}
            _record_decision_context_unresolved(
                cfg,
                run_id,
                document_id,
                target,
                stats,
                reason=f"Identity retry failed; leaving target unresolved: {type(exc).__name__}: {exc}",
                suggestions=item.get("search_suggestions"),
            )
        return contexts

    retryable_by_index = {int(row["source_index"]): row for row in prompt_items if row.get("source_index") is not None}
    selected_indexes: set[int] = set()
    for choice in _as_list(decision.get("skipped_targets")):
        if not isinstance(choice, dict):
            continue
        source_index = _decision_resolution_choice_key(choice, "source_index", "index", "unresolved_index")
        if source_index is None or source_index not in retryable_by_index:
            continue
        selected_indexes.add(source_index)
        skipped_indexes.add(source_index)
        item = unresolved_items[source_index]
        target = item.get("target") if isinstance(item.get("target"), dict) else {}
        _record_decision_context_unresolved(
            cfg,
            run_id,
            document_id,
            target,
            stats,
            reason=choice.get("reason") or "Model chose skip_unresolved during identity retry.",
            suggestions=item.get("search_suggestions"),
        )
        stats["decision_context_unresolved_retry_skipped"] = int(stats.get("decision_context_unresolved_retry_skipped") or 0) + 1

    for choice in _as_list(decision.get("decision_targets")):
        if not isinstance(choice, dict):
            continue
        source_index = _decision_resolution_choice_key(choice, "source_index", "index", "unresolved_index")
        if source_index is None or source_index not in retryable_by_index or source_index in selected_indexes:
            continue
        action = _clean_text(choice.get("action")).lower()
        if action and action not in {"use_suggestion", "resolve", "resolved"}:
            continue
        item = unresolved_items[source_index]
        suggestions = [row for row in _as_list(item.get("search_suggestions")) if isinstance(row, dict)]
        suggestion_index = _decision_resolution_choice_key(choice, "suggestion_index", "suggestion", "suggestion_id")
        if suggestion_index is None and len(suggestions) == 1:
            suggestion_index = 0
        if suggestion_index is None or suggestion_index >= len(suggestions):
            target = item.get("target") if isinstance(item.get("target"), dict) else {}
            _record_decision_context_unresolved(
                cfg,
                run_id,
                document_id,
                target,
                stats,
                reason="Identity retry did not choose a valid suggestion index.",
                suggestions=suggestions,
            )
            stats["decision_context_unresolved_retry_skipped"] = int(stats.get("decision_context_unresolved_retry_skipped") or 0) + 1
            skipped_indexes.add(source_index)
            selected_indexes.add(source_index)
            continue
        confidence = _confidence(choice.get("confidence")) or 0.0
        if confidence and confidence < 0.55:
            target = item.get("target") if isinstance(item.get("target"), dict) else {}
            _record_decision_context_unresolved(
                cfg,
                run_id,
                document_id,
                target,
                stats,
                reason=choice.get("rationale") or "Identity retry confidence was too low.",
                suggestions=suggestions,
            )
            stats["decision_context_unresolved_retry_skipped"] = int(stats.get("decision_context_unresolved_retry_skipped") or 0) + 1
            skipped_indexes.add(source_index)
            selected_indexes.add(source_index)
            continue
        retry_target = _target_from_search_hit(
            suggestions[suggestion_index],
            fallback_name=_decision_target_label(item.get("target") if isinstance(item.get("target"), dict) else {}),
            confidence=confidence or 0.75,
        )
        if retry_target is None:
            target = item.get("target") if isinstance(item.get("target"), dict) else {}
            _record_decision_context_unresolved(
                cfg,
                run_id,
                document_id,
                target,
                stats,
                reason="Identity retry selected a suggestion that cannot be used as a decision-context target.",
                suggestions=suggestions,
            )
            stats["decision_context_unresolved_retry_skipped"] = int(stats.get("decision_context_unresolved_retry_skipped") or 0) + 1
            skipped_indexes.add(source_index)
            selected_indexes.add(source_index)
            continue
        retry_target["source"] = "decision_context_unresolved_retry"
        retry_target["rationale"] = _clean_text(choice.get("rationale")) or "Model selected a server-provided identity suggestion."
        retry_targets.append(retry_target)
        selected_indexes.add(source_index)

    for source_index, item in enumerate(unresolved_items):
        if source_index in selected_indexes or source_index in skipped_indexes:
            continue
        target = item.get("target") if isinstance(item.get("target"), dict) else {}
        _record_decision_context_unresolved(
            cfg,
            run_id,
            document_id,
            target,
            stats,
            reason="Identity retry made no selection; leaving target unresolved.",
            suggestions=item.get("search_suggestions"),
        )
        stats["decision_context_unresolved_retry_skipped"] = int(stats.get("decision_context_unresolved_retry_skipped") or 0) + 1

    if not retry_targets:
        return contexts
    stats["decision_context_unresolved_retry_targets"] = int(stats.get("decision_context_unresolved_retry_targets") or 0) + len(retry_targets)
    _record_worker_action(
        cfg,
        run_id,
        action_kind="decision_context_unresolved_retry",
        status="success",
        document_id=document_id,
        summary=f"retrying {len(retry_targets)} unresolved decision-context target(s) with model-selected search suggestions",
        response={"retry_targets": retry_targets[:5]},
    )
    try:
        data = _api_post(
            cfg,
            "/api/local-agent/decision/context-batch",
            {
                "run_id": run_id,
                "targets": retry_targets,
                "days_back": cfg.calendar_days_back,
                "days_forward": cfg.calendar_days_forward,
                "limit": cfg.calendar_limit,
                "retry": {"kind": "unresolved_suggestion", "max_attempts": 1},
            },
            run_id=run_id,
        )
    except Exception as exc:
        stats["decision_context_unresolved_retry_failed"] = int(stats.get("decision_context_unresolved_retry_failed") or 0) + 1
        for target in retry_targets:
            _record_decision_context_unresolved(
                cfg,
                run_id,
                document_id,
                target,
                stats,
                reason=f"Decision-context retry read failed: {type(exc).__name__}: {exc}",
                suggestions=target.get("resolution"),
            )
        return contexts

    resolved_count = 0
    for row in _as_list(data.get("results")):
        if not isinstance(row, dict):
            continue
        target = row.get("target") if isinstance(row.get("target"), dict) else {}
        context = _compact_decision_context(row.get("context") or {})
        calendar = _compact_rows(row.get("calendar"), limit=cfg.calendar_limit)
        if context:
            stats["decision_context_reads"] += 1
        if calendar:
            stats["calendar_reads"] += 1
        if context or calendar:
            resolved_count += 1
            contexts.append({"target": target, "context": context, "calendar": calendar, "sync": row.get("sync")})
    if resolved_count:
        stats["decision_context_unresolved_retry_resolved"] = int(stats.get("decision_context_unresolved_retry_resolved") or 0) + resolved_count
    for error in _as_list(data.get("errors")):
        if not isinstance(error, dict):
            continue
        target = error.get("target") if isinstance(error.get("target"), dict) else {}
        if _decision_context_error_is_unresolved(error.get("error")):
            _record_decision_context_unresolved(cfg, run_id, document_id, target, stats, reason=error.get("error"), suggestions=error.get("search_suggestions"))
            continue
        stats["errors"] += 1
        _record_error(
            cfg,
            run_id,
            error=f"decision context retry failed for {error.get('target')}: {error.get('error')}",
            document_id=document_id,
            action_kind="decision_context_retry_error",
        )
    for unresolved in _as_list(data.get("unresolved")):
        if isinstance(unresolved, dict):
            target = unresolved.get("target") if isinstance(unresolved.get("target"), dict) else {}
            _record_decision_context_unresolved(
                cfg,
                run_id,
                document_id,
                target,
                stats,
                reason=unresolved.get("message") or unresolved.get("reason"),
                suggestions=unresolved.get("search_suggestions"),
            )
    return contexts


def _read_decision_contexts(
    cfg: WorkerConfig,
    run_id: int,
    document_id: int,
    document: dict[str, Any],
    result: dict[str, Any],
    stats: dict[str, Any],
) -> list[dict[str, Any]]:
    if not cfg.decision_spine_enabled:
        return []
    contexts: list[dict[str, Any]] = []
    targets = _resolve_decision_targets(cfg, run_id, document_id, document, result)
    if not targets:
        return []
    try:
        data = _api_post(
            cfg,
            "/api/local-agent/decision/context-batch",
            {
                "run_id": run_id,
                "targets": targets,
                "days_back": cfg.calendar_days_back,
                "days_forward": cfg.calendar_days_forward,
                "limit": cfg.calendar_limit,
            },
            run_id=run_id,
        )
        handled_batch_response = False
        unresolved_items: list[dict[str, Any]] = []
        for row in _as_list(data.get("results")):
            if not isinstance(row, dict):
                continue
            handled_batch_response = True
            target = row.get("target") if isinstance(row.get("target"), dict) else {}
            context = _compact_decision_context(row.get("context") or {})
            calendar = _compact_rows(row.get("calendar"), limit=cfg.calendar_limit)
            if context:
                stats["decision_context_reads"] += 1
            if calendar:
                stats["calendar_reads"] += 1
            if context or calendar:
                contexts.append({"target": target, "context": context, "calendar": calendar, "sync": row.get("sync")})
        for error in _as_list(data.get("errors")):
            if isinstance(error, dict):
                handled_batch_response = True
                if _decision_context_error_is_unresolved(error.get("error")):
                    target = error.get("target") if isinstance(error.get("target"), dict) else {}
                    unresolved_items.append(
                        _decision_context_unresolved_item(
                            {
                                "target": target,
                                "message": error.get("error"),
                                "reason": error.get("error"),
                                "search_suggestions": error.get("search_suggestions"),
                            },
                            fallback_target=target,
                        )
                    )
                    continue
                stats["errors"] += 1
                _record_error(
                    cfg,
                    run_id,
                    error=f"decision context batch read failed for {error.get('target')}: {error.get('error')}",
                    document_id=document_id,
                    action_kind="decision_context_batch_error",
                )
        for unresolved in _as_list(data.get("unresolved")):
            if isinstance(unresolved, dict):
                handled_batch_response = True
                unresolved_items.append(_decision_context_unresolved_item(unresolved))
        if unresolved_items:
            contexts.extend(_retry_unresolved_decision_contexts(cfg, run_id, document_id, document, result, unresolved_items, stats))
        if contexts or handled_batch_response:
            return contexts
    except Exception as exc:
        _log_worker_event("decision_context_batch_fallback", document_id=document_id, error=f"{type(exc).__name__}: {exc}", targets=len(targets))
    for target in targets:
        query = dict(target.get("query") or {})
        if not query:
            continue
        row: dict[str, Any] = {"target": target}
        try:
            data = _api_get(cfg, "/api/local-agent/decision/context", run_id=run_id, params={**query, "run_id": run_id})
            stats["decision_context_reads"] += 1
            row["context"] = _compact_decision_context(data.get("context") or {})
            row["sync"] = data.get("sync")
        except Exception as exc:
            if _decision_context_error_is_unresolved(exc):
                _record_decision_context_unresolved(cfg, run_id, document_id, target, stats, reason=exc)
                continue
            stats["errors"] += 1
            _record_error(
                cfg,
                run_id,
                error=f"decision context read failed for {target.get('label') or query}: {exc}",
                document_id=document_id,
                action_kind="decision_context_error",
            )
        try:
            calendar = _api_get(
                cfg,
                "/api/local-agent/calendar",
                run_id=run_id,
                params={
                    **query,
                    "days_back": cfg.calendar_days_back,
                    "days_forward": cfg.calendar_days_forward,
                    "limit": cfg.calendar_limit,
                    "run_id": run_id,
                },
            )
            stats["calendar_reads"] += 1
            row["calendar"] = _compact_rows(calendar.get("catalysts"), limit=cfg.calendar_limit)
        except Exception as exc:
            stats["errors"] += 1
            _record_error(
                cfg,
                run_id,
                error=f"calendar read failed for {target.get('label') or query}: {exc}",
                document_id=document_id,
                action_kind="calendar_context_error",
            )
        if row.get("context") or row.get("calendar"):
            contexts.append(row)
    return contexts


def _read_durable_candidate_context(
    cfg: WorkerConfig,
    run_id: int,
    document_id: int,
    result: dict[str, Any],
    stats: dict[str, Any],
) -> dict[str, Any]:
    if not cfg.decision_spine_enabled:
        return {}
    durable_routes = _as_list(result.get("durable_routes"))
    candidate_writes = _as_list(result.get("research_writes"))
    decision_candidates = [
        *_as_list(result.get("decision_targets")),
        *_as_list(result.get("analyst_evidence")),
        *_as_list(result.get("model_delta_candidates")),
        *_as_list(result.get("catalysts")),
        *_as_list(result.get("investment_memos")),
        *_as_list(result.get("evidence_links")),
    ]
    if not durable_routes and not candidate_writes and not decision_candidates:
        return {}
    try:
        data = _api_post(
            cfg,
            "/api/local-agent/durable/context",
            {
                "run_id": run_id,
                "document_id": document_id,
                "durable_routes": durable_routes,
                "candidate_writes": candidate_writes,
                "result": {
                    "summary_short": result.get("summary_short"),
                    "information_gain": result.get("information_gain"),
                    "document_disposition": result.get("document_disposition"),
                    "atomic_claims": _as_list(result.get("atomic_claims"))[:8],
                    "entity_refs": _as_list(result.get("entity_refs"))[:8],
                    "decision_targets": _as_list(result.get("decision_targets"))[:6],
                    "analyst_evidence": _as_list(result.get("analyst_evidence"))[:6],
                    "model_delta_candidates": _as_list(result.get("model_delta_candidates"))[:6],
                    "catalysts": _as_list(result.get("catalysts"))[:4],
                    "investment_memos": _as_list(result.get("investment_memos"))[:4],
                    "evidence_links": _as_list(result.get("evidence_links"))[:4],
                    "dataset_rows": _as_list(result.get("dataset_rows"))[:4],
                    "dataset_materializations": _as_list(result.get("dataset_materializations"))[:4],
                    "supply_chain_metric_bindings": _as_list(result.get("supply_chain_metric_bindings"))[:4],
                    "instrument_request_fulfillments": _as_list(result.get("instrument_request_fulfillments"))[:4],
                },
                "limit": 8,
            },
            run_id=run_id,
        )
        context = data.get("context") if isinstance(data.get("context"), dict) else {}
        stats["durable_context_reads"] += 1
        return context
    except Exception as exc:
        stats["errors"] += 1
        stats["durable_context_errors"] += 1
        _record_error(cfg, run_id, error=f"durable candidate context read failed: {exc}", document_id=document_id, action_kind="durable_candidate_context_error")
        return {}


def _confidence_allows_write(item: dict[str, Any], cfg: WorkerConfig) -> bool:
    confidence = _confidence(item.get("confidence"))
    return confidence is not None and confidence >= cfg.decision_min_confidence


def _single_context_target(decision_contexts: list[dict[str, Any]]) -> dict[str, Any] | None:
    if len(decision_contexts) != 1:
        return None
    target = decision_contexts[0].get("target")
    return target if isinstance(target, dict) else None


def _context_target_matches_item(target: dict[str, Any], item: dict[str, Any]) -> bool:
    """A document-wide context is not an identity for every claim in that document."""
    text = " ".join(
        _clean_text(item.get(field))
        for field in ("raw_subject_text", "subject", "entity_name", "company_name", "summary", "claim")
    )
    if not text.strip():
        return False
    query = target.get("query") if isinstance(target.get("query"), dict) else {}
    names = (
        _clean_text(target.get("label")),
        _clean_text(target.get("ticker")),
        _clean_text(query.get("ticker")),
    )
    return any(
        len(name) >= 3 and re.search(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", text, flags=re.IGNORECASE)
        for name in names
        if name
    )


def _enrich_decision_payload(
    cfg: WorkerConfig,
    *,
    run_id: int,
    document_id: int,
    document: dict[str, Any],
    item: dict[str, Any],
    decision_contexts: list[dict[str, Any]],
) -> dict[str, Any]:
    payload = dict(item)
    if not _target_query(payload):
        target = _single_context_target(decision_contexts)
        if target is not None and _context_target_matches_item(target, payload):
            query = dict(target.get("query") or {})
            payload.update({k: v for k, v in query.items() if v is not None})
    payload.setdefault("run_id", run_id)
    payload.setdefault("source_document_id", document_id)
    if document.get("url") and not payload.get("source_url"):
        payload["source_url"] = document.get("url")
    doc_extra = document.get("extra") if isinstance(document.get("extra"), dict) else {}
    if not payload.get("source_name") and (document.get("author") or doc_extra.get("handle")):
        payload["source_name"] = document.get("author") or f"@{str(doc_extra.get('handle')).lstrip('@')}"
    if doc_extra.get("handle") and not payload.get("observed_source_handle"):
        payload["observed_source_handle"] = str(doc_extra.get("handle")).lstrip("@")
    if not payload.get("observed_source_name") and (document.get("author") or doc_extra.get("handle")):
        payload["observed_source_name"] = document.get("author") or f"@{str(doc_extra.get('handle')).lstrip('@')}"
    if document.get("source_name") and not payload.get("source_kind"):
        payload["source_kind"] = document.get("source_name")
    existing_payload = dict(payload.get("payload") or {}) if isinstance(payload.get("payload"), dict) else {}
    existing_payload.update(
        {
            "local_agent": {
                "run_id": run_id,
                "document_id": document_id,
                "model": cfg.model,
                "prompt_version": cfg.prompt_version,
            }
        }
    )
    existing_payload["source_document"] = {
        "source_name": document.get("source_name"),
        "source_category": document.get("source_category"),
        "author": document.get("author"),
        "language": document.get("language"),
        "handle": doc_extra.get("handle"),
        "connector": doc_extra.get("connector"),
    }
    for key in ("rationale", "source_claim_indexes", "claim_indexes"):
        if item.get(key) is not None:
            existing_payload[key] = item.get(key)
    payload["payload"] = existing_payload
    _preserve_estimate_owner(payload, _estimate_owner(payload))
    return payload


def _has_decision_identity(payload: dict[str, Any]) -> bool:
    return bool(_target_query(payload))


def _durable_candidate_context_from_decisions(decision_contexts: list[dict[str, Any]]) -> dict[str, Any]:
    for context in decision_contexts:
        durable_context = context.get("durable_candidate_context") if isinstance(context, dict) else None
        if isinstance(durable_context, dict):
            return durable_context
    return {}


def _durable_context_duplicate_block(context: dict[str, Any]) -> str | None:
    duplicate_risk = context.get("duplicate_risk") if isinstance(context.get("duplicate_risk"), dict) else {}
    level = str(duplicate_risk.get("level") or "").strip().lower()
    recommended_action = str(context.get("recommended_action") or "").strip().lower()
    if level == "high" or recommended_action == "hold_for_duplicate_review":
        reasons = [str(value) for value in _as_list(duplicate_risk.get("reasons")) if str(value).strip()]
        return "duplicate_context:" + ",".join(reasons or [level or recommended_action])
    return None


def _durable_item_allows_duplicate_context(item: dict[str, Any]) -> bool:
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    routing = item.get("routing") if isinstance(item.get("routing"), dict) else {}
    values = {
        str(item.get("novelty") or "").strip().lower(),
        str(item.get("action") or "").strip().lower(),
        str(routing.get("novelty") or "").strip().lower(),
        str(routing.get("action") or "").strip().lower(),
        str(payload.get("novelty") or "").strip().lower(),
        str(payload.get("action") or "").strip().lower(),
    }
    if any(value in {"update", "material_update", "conflict", "contradiction", "correction", "revision"} for value in values):
        return True
    return bool(item.get("duplicate_context_override") or payload.get("duplicate_context_override"))


def _durable_context_blocks_item(item: dict[str, Any], decision_contexts: list[dict[str, Any]]) -> str | None:
    reason = _durable_context_duplicate_block(_durable_candidate_context_from_decisions(decision_contexts))
    if not reason or _durable_item_allows_duplicate_context(item):
        return None
    return reason


def _post_document_evidence_link(
    cfg: WorkerConfig,
    run_id: int,
    document_id: int,
    *,
    target_type: str,
    target_id: int | None,
    source_payload: dict[str, Any],
    stats: dict[str, Any],
) -> None:
    if target_id is None:
        return
    payload = {
        "run_id": run_id,
        "target_type": target_type,
        "target_id": target_id,
        "evidence_object_type": "document",
        "evidence_object_id": document_id,
        "relation": "source",
        "confidence": _confidence(source_payload.get("confidence")),
        "summary": source_payload.get("summary") or source_payload.get("title") or source_payload.get("write_summary"),
        "evidence_tier": source_payload.get("evidence_tier"),
        "source_kind": source_payload.get("source_kind"),
        "source_url": source_payload.get("source_url"),
    }
    query = _target_query(source_payload)
    payload.update({k: v for k, v in query.items() if v is not None})
    try:
        _api_post(cfg, "/api/local-agent/evidence-links", payload, run_id=run_id)
        stats["evidence_links"] += 1
    except Exception as exc:
        _record_error(cfg, run_id, error=f"evidence link write failed for {target_type}:{target_id}: {exc}", document_id=document_id, action_kind="evidence_link_write_error")


def _memo_has_content(payload: dict[str, Any]) -> bool:
    for key in ("summary_md", "thesis_md", "valuation_md", "catalysts_md", "risks_md", "conviction", "inclusion_reason", "active_thesis", "next_catalyst"):
        if _clean_text(payload.get(key)):
            return True
    return bool(_as_list(payload.get("missing_data_warnings")))


def _estimate_owner(item: dict[str, Any]) -> str:
    attribution = item.get("source_attribution") if isinstance(item.get("source_attribution"), dict) else {}
    underlying = attribution.get("underlying_source") if isinstance(attribution.get("underlying_source"), dict) else {}
    return _clean_text(
        item.get("estimate_owner")
        or item.get("underlying_firm")
        or item.get("source_firm")
        or item.get("research_firm")
        or item.get("institution")
        or item.get("firm")
        or item.get("source_owner")
        or underlying.get("firm")
        or underlying.get("name")
    )


def _preserve_estimate_owner(payload: dict[str, Any], owner: str) -> None:
    if not owner:
        return
    payload.setdefault("estimate_owner", owner)
    payload.setdefault("underlying_firm", owner)
    if not payload.get("source_name") and not payload.get("observed_source_name"):
        payload["source_name"] = owner
    attribution = payload.get("source_attribution") if isinstance(payload.get("source_attribution"), dict) else {}
    attribution = dict(attribution)
    underlying = attribution.get("underlying_source") if isinstance(attribution.get("underlying_source"), dict) else {}
    underlying = dict(underlying)
    underlying.setdefault("firm", owner)
    underlying.setdefault("name", owner)
    attribution["underlying_source"] = underlying
    payload["source_attribution"] = attribution


def _model_delta_candidate_to_analyst_evidence(item: dict[str, Any]) -> dict[str, Any] | None:
    metric_key = _clean_text(item.get("canonical_metric_key") or item.get("metric_key"))
    if not metric_key:
        return None
    value_number = item.get("value_number")
    if value_number is None:
        value_number = item.get("forecast_value")
    value_text = _clean_text(item.get("value_text") or item.get("forecast_status"))
    if value_number is None and not value_text:
        return None
    if bool(item.get("requires_resolution")):
        return None
    summary = _clean_text(item.get("summary") or item.get("rationale"))
    if not summary:
        subject = _clean_text(item.get("raw_subject_text") or item.get("canonical_subject_key") or item.get("subject"))
        period = _clean_text(item.get("period") or item.get("forecast_period"))
        value_label = str(value_number if value_number is not None else value_text)
        summary = f"{subject} {metric_key} {value_label} {period}".strip()
    forecast = {
        "metric_key": metric_key,
        "metric_value_type": item.get("metric_value_type"),
        "value": value_number,
        "value_text": value_text or None,
        "forecast_status": value_text or None,
        "unit": item.get("unit"),
        "currency": item.get("currency"),
        "product_family": item.get("product_family"),
        "segment": item.get("segment"),
        "period": item.get("period") or item.get("forecast_period"),
        "period_basis": item.get("period_basis"),
        "period_year": item.get("period_year"),
        "period_quarter": item.get("period_quarter"),
        "period_half": item.get("period_half"),
        "as_of_date": item.get("as_of_date"),
        "scenario": item.get("scenario"),
    }
    payload = {
        **item,
        "evidence_kind": "estimate",
        "action": item.get("action") or "claim",
        "summary": summary,
        "metric_key": metric_key,
        "estimate_metric": metric_key,
        "estimate_period": item.get("period") or item.get("forecast_period"),
        "estimate_value": value_number,
        "forecast_value": value_number,
        "forecast_status": value_text or None,
        "estimate_unit": item.get("unit"),
        "currency": item.get("currency"),
        "forecast": {k: v for k, v in forecast.items() if v not in (None, "", [])},
        "payload": {
            **(item.get("payload") if isinstance(item.get("payload"), dict) else {}),
            "model_delta_candidate": dict(item),
        },
    }
    _preserve_estimate_owner(payload, _estimate_owner(item))
    return {k: v for k, v in payload.items() if v not in (None, "", [])}


def _numberish(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = _clean_text(value)
    if not text:
        return None
    text = text.replace(",", "").replace("$", "").replace("%", "")
    try:
        return float(text)
    except ValueError:
        return None


def _analyst_evidence_is_modelable(payload: dict[str, Any]) -> bool:
    forecast = payload.get("forecast") if isinstance(payload.get("forecast"), dict) else {}
    if any(
        _numberish(payload.get(field)) is not None
        for field in (
            "price_target",
            "prior_price_target",
            "forecast_value",
            "prior_forecast_value",
            "estimate_value",
            "prior_estimate_value",
            "actual_value",
        )
    ):
        return True
    if any(_numberish(forecast.get(field)) is not None for field in ("value", "forecast_value", "prior_value", "actual_value")):
        return True
    if _clean_text(payload.get("rating")) or _clean_text(payload.get("prior_rating")):
        return True
    metric_key = _clean_text(
        payload.get("metric_key")
        or payload.get("estimate_metric")
        or forecast.get("metric_key")
        or forecast.get("metric")
        or forecast.get("metric_name")
    )
    if not metric_key:
        return False
    status = _clean_text(payload.get("forecast_status") or forecast.get("forecast_status") or forecast.get("status") or payload.get("value_text"))
    if any(
        _clean_text(payload.get(field) or forecast.get(field))
        for field in (
            "target_date",
            "actual_date",
            "start_date",
            "end_date",
            "forecast_start_date",
            "forecast_end_date",
            "forecast_period",
            "estimate_period",
            "period",
        )
    ) and status:
        return True
    metric_type = _clean_text(payload.get("metric_value_type") or forecast.get("metric_value_type")).lower()
    metric_text = metric_key.lower()
    if not status:
        return False
    if re.search(r"\d", status):
        return True
    if metric_type in {"status", "event", "date"}:
        return True
    return any(token in metric_text for token in ("status", "qualified", "qualification", "delivery", "start", "lead_time", "availability", "allocation"))


def _auto_write_decision_objects(
    cfg: WorkerConfig,
    run_id: int,
    document_id: int,
    document: dict[str, Any],
    result: dict[str, Any],
    decision_contexts: list[dict[str, Any]],
    stats: dict[str, Any],
) -> None:
    if not cfg.auto_write:
        return
    evidence_items = _dedupe_dicts(
        [
            *[item for item in _as_list(result.get("analyst_evidence")) if isinstance(item, dict)],
            *[
                converted
                for item in _as_list(result.get("model_delta_candidates"))
                if isinstance(item, dict)
                for converted in [_model_delta_candidate_to_analyst_evidence(item)]
                if converted is not None
            ],
        ],
        ("ticker", "company_id", "issuer_id", "metric_key", "summary", "source_document_id", "estimate_owner", "underlying_firm", "source_name"),
    )
    for item in evidence_items:
        if not isinstance(item, dict) or not _confidence_allows_write(item, cfg):
            continue
        duplicate_block = _durable_context_blocks_item(item, decision_contexts)
        if duplicate_block:
            stats["durable_duplicate_skipped"] = int(stats.get("durable_duplicate_skipped") or 0) + 1
            continue
        payload = _enrich_decision_payload(cfg, run_id=run_id, document_id=document_id, document=document, item=item, decision_contexts=decision_contexts)
        if not _has_decision_identity(payload) or not _clean_text(payload.get("summary")):
            continue
        if not _analyst_evidence_is_modelable(payload):
            stats["analyst_evidence_skipped_not_modelable"] = int(stats.get("analyst_evidence_skipped_not_modelable") or 0) + 1
            continue
        try:
            data = _api_post(cfg, "/api/local-agent/analyst-evidence", payload, run_id=run_id)
            row = data.get("analyst_evidence") if isinstance(data.get("analyst_evidence"), dict) else {}
            stats["analyst_evidence"] += 1
            _post_document_evidence_link(
                cfg,
                run_id,
                document_id,
                target_type="analyst_evidence",
                target_id=_int_or_none(row.get("id")),
                source_payload=payload,
                stats=stats,
            )
        except Exception as exc:
            _record_error(cfg, run_id, error=f"analyst evidence write failed: {exc}", document_id=document_id, action_kind="analyst_evidence_write_error")
    for item in _as_list(result.get("catalysts")):
        if not isinstance(item, dict) or not _confidence_allows_write(item, cfg):
            continue
        payload = _enrich_decision_payload(cfg, run_id=run_id, document_id=document_id, document=document, item=item, decision_contexts=decision_contexts)
        if not _has_decision_identity(payload) or not _clean_text(payload.get("title")):
            continue
        try:
            data = _api_post(cfg, "/api/local-agent/catalysts", payload, run_id=run_id)
            row = data.get("catalyst") if isinstance(data.get("catalyst"), dict) else {}
            stats["catalysts"] += 1
            _post_document_evidence_link(
                cfg,
                run_id,
                document_id,
                target_type="investment_catalyst",
                target_id=_int_or_none(row.get("id")),
                source_payload=payload,
                stats=stats,
            )
        except Exception as exc:
            _record_error(cfg, run_id, error=f"catalyst write failed: {exc}", document_id=document_id, action_kind="catalyst_write_error")
    for item in _as_list(result.get("investment_memos")):
        if not isinstance(item, dict):
            continue
        stats["memo_candidates"] += 1
        if not _confidence_allows_write(item, cfg):
            stats["memo_skipped_low_confidence"] += 1
            continue
        payload = _enrich_decision_payload(cfg, run_id=run_id, document_id=document_id, document=document, item=item, decision_contexts=decision_contexts)
        if not _has_decision_identity(payload):
            stats["memo_skipped_missing_identity"] += 1
            continue
        if not _memo_has_content(payload):
            stats["memo_skipped_missing_content"] += 1
            continue
        duplicate_block = _durable_context_blocks_item(item, decision_contexts)
        if duplicate_block:
            stats["durable_duplicate_skipped"] = int(stats.get("durable_duplicate_skipped") or 0) + 1
            continue
        try:
            data = _api_post(cfg, "/api/local-agent/investment-memos", payload, run_id=run_id)
            row = data.get("memo") if isinstance(data.get("memo"), dict) else {}
            stats["memos"] += 1
            _post_document_evidence_link(
                cfg,
                run_id,
                document_id,
                target_type="investment_memo",
                target_id=_int_or_none(row.get("id")),
                source_payload=payload,
                stats=stats,
            )
        except Exception as exc:
            stats["memo_write_errors"] += 1
            _record_error(cfg, run_id, error=f"investment memo write failed: {exc}", document_id=document_id, action_kind="investment_memo_write_error")
    for item in _as_list(result.get("evidence_links")):
        if not isinstance(item, dict) or not _confidence_allows_write(item, cfg):
            continue
        payload = _enrich_decision_payload(cfg, run_id=run_id, document_id=document_id, document=document, item=item, decision_contexts=decision_contexts)
        payload.setdefault("evidence_object_type", "document")
        payload.setdefault("evidence_object_id", document_id)
        if not _clean_text(payload.get("target_type")) or _int_or_none(payload.get("target_id")) is None:
            continue
        try:
            _api_post(cfg, "/api/local-agent/evidence-links", payload, run_id=run_id)
            stats["evidence_links"] += 1
        except Exception as exc:
            _record_error(cfg, run_id, error=f"explicit evidence link write failed: {exc}", document_id=document_id, action_kind="evidence_link_write_error")


def _media_suffix(job: dict[str, Any]) -> str:
    content_type = str(job.get("content_type") or "").strip().lower()
    if "jpeg" in content_type or "jpg" in content_type:
        return ".jpg"
    if "webp" in content_type:
        return ".webp"
    if "gif" in content_type:
        return ".gif"
    if "bmp" in content_type:
        return ".bmp"
    if "tif" in content_type:
        return ".tiff"
    return ".png"


def _media_data_url(path: Path, *, content_type: object = None) -> str:
    mime = str(content_type or "").strip().lower()
    if not mime.startswith("image/"):
        mime = "image/jpeg" if path.suffix.lower() in {".jpg", ".jpeg"} else "image/png"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def _media_summary(text: str, *, max_chars: int = 700) -> str:
    cleaned = re.sub(r"\s+", " ", str(text or "").strip())
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max(80, max_chars - 1)].rstrip() + "..."


def _media_route(path: Path) -> dict[str, Any]:
    if classify_image_transcription_route is None:
        return {
            "lane": "ocr_first_then_vlm_if_low_confidence",
            "image_kind": "unknown",
            "confidence": 0.5,
            "reason": "Image route classifier unavailable in this worker environment.",
            "features": {},
        }
    try:
        route = classify_image_transcription_route(path)
        if hasattr(route, "as_dict"):
            return dict(route.as_dict())
        return dict(route) if isinstance(route, dict) else {"lane": "ocr_first_then_vlm_if_low_confidence", "image_kind": "unknown", "confidence": 0.5}
    except Exception as exc:
        return {
            "lane": "ocr_first_then_vlm_if_low_confidence",
            "image_kind": "unknown",
            "confidence": 0.4,
            "reason": f"Image route classifier failed: {type(exc).__name__}: {exc}",
            "features": {},
        }


def _media_source_context(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "document_id": job.get("document_id"),
        "document_title": job.get("document_title"),
        "document_url": job.get("document_url") or job.get("canonical_url"),
        "source_name": job.get("source_name"),
        "source_url": job.get("source_url"),
        "original_url": job.get("original_url"),
    }


def _ocr_media_assessment(text: str, *, route: dict[str, Any], job: dict[str, Any], detail: object = None) -> dict[str, Any]:
    if classify_ocr_media_output is None:
        cleaned = re.sub(r"\s+", " ", str(text or "").strip())
        alnum = len(re.findall(r"[A-Za-z0-9]", cleaned))
        sparse = len(cleaned) < 180 or alnum < 60
        return {
            "media_kind": str(route.get("image_kind") or "unknown"),
            "ocr_quality": "sparse" if sparse else "unknown",
            "needs_vlm": sparse or bool(route.get("needs_vlm")),
            "is_signal": True,
            "confidence": 0.45,
            "reason": "OCR media classifier unavailable.",
            "source_risk": "unknown",
            "visual_signal_terms": [],
            "layout_labels": [],
        }
    layout_details = None
    if isinstance(detail, dict):
        layout_details = detail.get("layout_details") or detail.get("json_result") or detail.get("layout")
    assessment = classify_ocr_media_output(
        text,
        route=route,
        layout_details=layout_details,
        source_context=_media_source_context(job),
    )
    if hasattr(assessment, "as_dict"):
        return dict(assessment.as_dict())
    return dict(assessment) if isinstance(assessment, dict) else {}


def _media_ocr_cli(cfg: WorkerConfig) -> str | None:
    explicit = str(cfg.media_ocr_cli or "").strip()
    candidates = [explicit] if explicit else []
    candidates.extend(
        [
            "llama-mtmd-cli.exe",
            "llama-mtmd-cli",
            str(Path.home() / "Documents" / "ThothTerminalAgent" / "tools" / "llama.cpp-b8966-cuda13.1" / "llama-mtmd-cli.exe"),
            str(Path.home() / "Documents" / "ThothTerminalAgent" / "tools" / "llama.cpp-b8960-cuda12.4" / "llama-mtmd-cli.exe"),
            str(Path.home() / "Documents" / "ThothTerminalAgent" / "tools" / "llama.cpp-mtp-clean-release-20260516" / "llama-mtmd-cli.exe"),
            str(Path.home() / "Documents" / "ThothTerminalAgent" / "tools" / "llama.cpp-mtp-clean-release-20260516" / "llama-cli.exe"),
        ]
    )
    for candidate in candidates:
        if not candidate:
            continue
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
        path = Path(candidate)
        if path.exists():
            return str(path)
    return None


def _hf_hub_cache_dir() -> Path:
    explicit = str(os.environ.get("HUGGINGFACE_HUB_CACHE") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    hf_home = str(os.environ.get("HF_HOME") or "").strip()
    if hf_home:
        return Path(hf_home).expanduser() / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def _split_hf_repo_quant(model_hint: str) -> tuple[str, str | None] | None:
    hint = str(model_hint or "").strip()
    if not hint or "/" not in hint:
        return None
    repo, sep, quant = hint.rpartition(":")
    if sep and "/" in repo and not re.match(r"^[A-Za-z]:[\\/]", hint):
        return repo, quant or None
    return hint, None


def _gguf_name_matches_quant(path: Path, quant: str | None) -> bool:
    if not quant:
        return True
    normalized_name = re.sub(r"[^a-z0-9]+", "", path.name.lower())
    normalized_quant = re.sub(r"[^a-z0-9]+", "", quant.lower())
    return normalized_quant in normalized_name


def _find_sibling_mmproj(model_path: Path, quant: str | None = None) -> Path | None:
    candidates = [
        path
        for path in model_path.parent.glob("*.gguf")
        if "mmproj" in path.name.lower() and _gguf_name_matches_quant(path, quant)
    ]
    if not candidates:
        candidates = [path for path in model_path.parent.glob("*.gguf") if "mmproj" in path.name.lower()]
    return sorted(candidates, key=lambda path: path.name.lower())[0] if candidates else None


def _resolve_cached_hf_gguf_pair(model_hint: str) -> tuple[Path, Path] | None:
    hint = str(model_hint or "").strip()
    direct = Path(hint).expanduser() if hint else None
    if direct and direct.exists() and direct.suffix.lower() == ".gguf":
        mmproj = _find_sibling_mmproj(direct)
        return (direct, mmproj) if mmproj else None

    parsed = _split_hf_repo_quant(hint)
    if not parsed:
        return None
    repo, quant = parsed
    repo_dir = _hf_hub_cache_dir() / f"models--{repo.replace('/', '--')}"
    snapshots_dir = repo_dir / "snapshots"
    if not snapshots_dir.exists():
        return None
    snapshots = sorted(
        [path for path in snapshots_dir.iterdir() if path.is_dir()],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for snapshot in snapshots:
        model_candidates = [
            path
            for path in snapshot.glob("*.gguf")
            if "mmproj" not in path.name.lower() and _gguf_name_matches_quant(path, quant)
        ]
        if not model_candidates:
            model_candidates = [path for path in snapshot.glob("*.gguf") if "mmproj" not in path.name.lower()]
        for model_path in sorted(model_candidates, key=lambda path: path.name.lower()):
            mmproj = _find_sibling_mmproj(model_path, quant)
            if mmproj:
                return model_path, mmproj
    return None


def _media_ocr_cli_model_args(cfg: WorkerConfig) -> tuple[list[str], dict[str, Any]]:
    model_hint = str(cfg.media_ocr_model or "").strip()
    cached_pair = _resolve_cached_hf_gguf_pair(model_hint)
    if cached_pair:
        model_path, mmproj_path = cached_pair
        return (
            ["-m", str(model_path), "--mmproj", str(mmproj_path)],
            {
                "model_arg_mode": "local_hf_cache",
                "model_path": str(model_path),
                "mmproj_path": str(mmproj_path),
            },
        )
    return ["-hf", model_hint], {"model_arg_mode": "hf_repo", "hf_repo": model_hint}


def _maybe_call(value: object) -> object:
    if callable(value):
        try:
            return value()
        except TypeError:
            return value
    return value


def _glmocr_attr(result: object, *names: str) -> object:
    if isinstance(result, dict):
        for name in names:
            value = result.get(name)
            if value is not None:
                return _maybe_call(value)
    for name in names:
        value = getattr(result, name, None)
        if value is not None:
            return _maybe_call(value)
    return None


def _glmocr_to_dict(result: object) -> object:
    if isinstance(result, list):
        return [_glmocr_to_dict(item) for item in result]
    if isinstance(result, dict):
        return result
    to_dict = getattr(result, "to_dict", None)
    if callable(to_dict):
        try:
            return to_dict()
        except Exception:
            pass
    return None


def _glmocr_result_text(result: object) -> str:
    if isinstance(result, list):
        parts = [_glmocr_result_text(item) for item in result]
        return "\n\n".join(part for part in parts if part.strip()).strip()
    value = _glmocr_attr(result, "markdown", "markdown_result", "md_results", "md", "text", "content")
    if isinstance(value, list):
        return "\n\n".join(str(item or "").strip() for item in value if str(item or "").strip()).strip()
    return str(value or "").strip()


def _glmocr_result_json(result: object) -> object:
    if isinstance(result, list):
        return [_glmocr_result_json(item) for item in result]
    value = _glmocr_attr(result, "json_result", "json", "result_json")
    if value is not None:
        return value
    result_dict = _glmocr_to_dict(result)
    if isinstance(result_dict, dict):
        for name in ("json_result", "json", "result_json"):
            if result_dict.get(name) is not None:
                return result_dict.get(name)
    return None


def _glmocr_layout_details(result: object) -> object:
    if isinstance(result, list):
        return [_glmocr_layout_details(item) for item in result]
    value = _glmocr_attr(result, "layout_details", "layout", "layout_result")
    if value is not None:
        return value
    result_dict = _glmocr_to_dict(result)
    if isinstance(result_dict, dict):
        for name in ("layout_details", "layout", "layout_result", "json_result"):
            if result_dict.get(name) is not None:
                return result_dict.get(name)
    return _glmocr_result_json(result)


def _glmocr_usage(result: object) -> object:
    if isinstance(result, list):
        return [_glmocr_usage(item) for item in result]
    value = _glmocr_attr(result, "usage", "metadata", "meta")
    if value is not None:
        return value
    result_dict = _glmocr_to_dict(result)
    if isinstance(result_dict, dict):
        for name in ("usage", "metadata", "meta"):
            if result_dict.get(name) is not None:
                return result_dict.get(name)
    return None


def _json_safe(value: object) -> object:
    try:
        json.dumps(value)
        return value
    except Exception:
        try:
            return json.loads(json.dumps(value, default=str))
        except Exception:
            return str(value)


def _media_ocr_env_value(*keys: str) -> str:
    for key in keys:
        value = str(os.environ.get(key) or "").strip()
        if value:
            return value
    return ""


def _media_ocr_api_key() -> str:
    return _media_ocr_env_value(
        "ZHIPU_API_KEY",
        "ZHIPUAI_API_KEY",
        "BIGMODEL_API_KEY",
        "GLMOCR_API_KEY",
        "GLM_OCR_API_KEY",
    )


def _normalize_media_ocr_backend(value: object) -> str:
    raw = str(value or "auto").strip().lower().replace("-", "_")
    aliases = {
        "auto": "auto",
        "cli": "gguf_cli",
        "gguf": "gguf_cli",
        "gguf_cli": "gguf_cli",
        "glm_ocr_cli": "gguf_cli",
        "sdk": "glm_sdk_sidecar",
        "glm_sdk": "glm_sdk_sidecar",
        "glm_ocr_sdk": "glm_sdk_sidecar",
        "glm_sdk_sidecar": "glm_sdk_sidecar",
        "vllm": "vllm_ocr_sidecar",
        "vllm_ocr": "vllm_ocr_sidecar",
        "vllm_sidecar": "vllm_ocr_sidecar",
        "vllm_ocr_sidecar": "vllm_ocr_sidecar",
    }
    return aliases.get(raw, raw)


def _media_ocr_sdk_layout_status() -> dict[str, Any]:
    try:
        from glmocr.layout import PPDocLayoutDetector  # type: ignore
    except Exception as exc:
        return {
            "available": False,
            "reason": f"glmocr layout detector import failed: {type(exc).__name__}: {str(exc)[:180]}",
        }
    if PPDocLayoutDetector is None:
        missing: list[str] = []
        for module_name in ("paddle", "paddleocr", "cv2", "onnxruntime"):
            try:
                if importlib.util.find_spec(module_name) is None:
                    missing.append(module_name)
            except Exception:
                missing.append(module_name)
        suffix = f"; missing modules: {', '.join(missing)}" if missing else ""
        return {
            "available": False,
            "reason": f"glmocr self-hosted layout detector unavailable{suffix}",
        }
    return {"available": True, "reason": "available"}


def _media_ocr_sdk_status(cfg: WorkerConfig) -> dict[str, Any]:
    config_path = str(cfg.media_ocr_config or os.environ.get("THOTH_MEDIA_OCR_CONFIG") or "").strip()
    api_url = str(
        cfg.media_ocr_api_url
        or os.environ.get("THOTH_MEDIA_OCR_API_URL")
        or os.environ.get("GLMOCR_API_URL")
        or ""
    ).strip()
    mode = str(cfg.media_ocr_mode or os.environ.get("THOTH_MEDIA_OCR_MODE") or os.environ.get("GLMOCR_MODE") or "").strip().lower()
    layout_device = str(cfg.media_ocr_layout_device or os.environ.get("THOTH_MEDIA_OCR_LAYOUT_DEVICE") or "").strip()
    api_key_present = bool(_media_ocr_api_key())
    selfhosted_env_present = any(
        bool(str(os.environ.get(key) or "").strip())
        for key in ("GLMOCR_OCR_API_URL", "GLMOCR_OCR_API_HOST", "GLMOCR_OCR_API_PORT")
    )
    selfhosted_surface_present = bool(api_url or selfhosted_env_present)
    try:
        available = importlib.util.find_spec("glmocr") is not None
    except Exception:
        available = False
    version = ""
    if available:
        try:
            version = importlib.metadata.version("glmocr")
        except Exception:
            version = "unknown"
    config_path_exists = bool(config_path and Path(config_path).exists())
    layout_status = _media_ocr_sdk_layout_status() if available else {"available": False, "reason": "glmocr package not importable"}
    layout_available = bool(layout_status.get("available"))
    requested_maas = mode == "maas" or bool(api_key_present and mode in {"", "maas"})
    requested_selfhosted = mode == "selfhosted" or bool(selfhosted_surface_present and mode != "maas")
    configured = bool(
        config_path_exists
        or (requested_maas and api_key_present)
        or (requested_selfhosted and layout_available)
    )
    reason = "configured"
    if not available:
        reason = "glmocr package not importable"
    elif config_path and not config_path_exists and not (api_url or api_key_present or mode or selfhosted_env_present):
        reason = "configured SDK config path does not exist"
    elif requested_maas and not api_key_present:
        reason = "MaaS SDK mode requires ZHIPU_API_KEY or GLMOCR_API_KEY"
    elif requested_selfhosted and not layout_available:
        reason = str(layout_status.get("reason") or "self-hosted SDK layout detector unavailable")
    elif not configured:
        reason = "missing SDK config/api_url/api_key"
    return {
        "available": available,
        "version": version or None,
        "configured": configured,
        "reason": reason,
        "config_path_present": bool(config_path),
        "config_path_exists": config_path_exists,
        "api_url_present": bool(api_url),
        "api_key_present": api_key_present,
        "selfhosted_env_present": selfhosted_env_present,
        "selfhosted_surface_present": selfhosted_surface_present,
        "layout_available": layout_available,
        "layout_reason": layout_status.get("reason"),
        "mode": mode or None,
        "layout_device": layout_device or None,
    }


def _run_media_ocr_sdk(cfg: WorkerConfig, image_path: Path) -> dict[str, Any]:
    try:
        from glmocr import GlmOcr, parse  # type: ignore
    except Exception as exc:
        raise AgentWorkerError(f"glmocr SDK unavailable: {exc}") from exc

    layout_device = str(cfg.media_ocr_layout_device or os.environ.get("THOTH_MEDIA_OCR_LAYOUT_DEVICE") or "").strip()
    config_path = str(cfg.media_ocr_config or os.environ.get("THOTH_MEDIA_OCR_CONFIG") or "").strip() or None
    api_url = str(
        cfg.media_ocr_api_url
        or os.environ.get("THOTH_MEDIA_OCR_API_URL")
        or os.environ.get("GLMOCR_API_URL")
        or ""
    ).strip() or None
    sdk_mode = str(cfg.media_ocr_mode or os.environ.get("THOTH_MEDIA_OCR_MODE") or os.environ.get("GLMOCR_MODE") or "").strip() or None
    api_key = _media_ocr_api_key() or None
    parser_kwargs: dict[str, Any] = {
        "config_path": config_path,
        "api_url": api_url,
        "api_key": api_key,
        "mode": sdk_mode,
        "timeout": max(30, int(cfg.media_ocr_timeout_s)),
    }
    if layout_device:
        parser_kwargs["layout_device"] = layout_device
    model_hint = str(cfg.media_ocr_model or "").strip()
    if model_hint and "gguf" not in model_hint.lower() and "GLM-OCR-GGUF" not in model_hint:
        parser_kwargs["model"] = model_hint
    parser_kwargs = {key: value for key, value in parser_kwargs.items() if value not in (None, "")}
    if parser_kwargs:
        with GlmOcr(**parser_kwargs) as parser:
            result = parser.parse(str(image_path), save_layout_visualization=False)
    else:
        result = parse(str(image_path))
    text = _glmocr_result_text(result)
    if not text:
        raise AgentWorkerError("glmocr SDK returned empty markdown")
    json_result = _json_safe(_glmocr_result_json(result))
    layout_details = _json_safe(_glmocr_layout_details(result))
    usage = _json_safe(_glmocr_usage(result))
    result_dict = _json_safe(_glmocr_to_dict(result))
    return {
        "text": text,
        "backend": "glm-ocr-sdk",
        "model": model_hint if parser_kwargs.get("model") else "glm-ocr",
        "detail": {
            "backend_mode": "sdk",
            "sdk": "glmocr",
            "config_path": config_path,
            "api_url": api_url,
            "api_key_present": bool(api_key),
            "mode": sdk_mode,
            "layout_device": layout_device or None,
            "markdown_result": text,
            "json_result": json_result,
            "layout_details": layout_details,
            "layout_details_present": bool(layout_details not in (None, "", [], {})),
            "usage": usage,
            "result": result_dict,
        },
    }


def _process_output_excerpt(stderr: object, stdout: object, *, limit: int = 1400) -> str:
    text = str(stderr or stdout or "").strip()
    if len(text) <= limit:
        return text
    head_len = min(420, max(160, limit // 3))
    tail_len = max(420, limit - head_len - 40)
    return f"{text[:head_len]}\n...[truncated]...\n{text[-tail_len:]}"


def _prepare_media_ocr_cli_image(image_path: Path) -> tuple[Path, dict[str, Any]]:
    max_side_raw = str(os.environ.get("THOTH_MEDIA_OCR_CLI_MAX_IMAGE_SIDE") or "1280").strip()
    try:
        max_side = max(512, min(int(max_side_raw), 4096))
    except Exception:
        max_side = 1280
    detail: dict[str, Any] = {
        "ocr_cli_image_max_side": max_side,
        "ocr_cli_image_resized": False,
        "ocr_cli_source_image_path": str(image_path),
    }
    try:
        from PIL import Image, ImageOps
    except Exception as exc:
        detail["ocr_cli_resize_skipped"] = f"PIL unavailable: {type(exc).__name__}"
        return image_path, detail
    try:
        with Image.open(image_path) as img:
            img = ImageOps.exif_transpose(img)
            width, height = img.size
            source_format = str(img.format or "").upper()
            detail["ocr_cli_source_width"] = int(width)
            detail["ocr_cli_source_height"] = int(height)
            detail["ocr_cli_source_format"] = source_format
            needs_jpeg_conversion = source_format not in {"JPEG", "JPG", "PNG"}
            if max(width, height) <= max_side and not needs_jpeg_conversion:
                return image_path, detail
            work = img.convert("RGB")
            work.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            handle = tempfile.NamedTemporaryFile(prefix="thoth-ocr-", suffix=".jpg", delete=False)
            temp_path = Path(handle.name)
            handle.close()
            work.save(temp_path, format="JPEG", quality=92, optimize=True)
            detail["ocr_cli_image_resized"] = max(width, height) > max_side
            detail["ocr_cli_image_converted"] = needs_jpeg_conversion
            detail["ocr_cli_input_image_path"] = str(temp_path)
            detail["ocr_cli_input_width"] = int(work.size[0])
            detail["ocr_cli_input_height"] = int(work.size[1])
            return temp_path, detail
    except Exception as exc:
        detail["ocr_cli_resize_skipped"] = f"{type(exc).__name__}: {exc}"
        return image_path, detail


def _run_media_ocr_cli(cfg: WorkerConfig, image_path: Path) -> dict[str, Any]:
    cli = _media_ocr_cli(cfg)
    if not cli:
        raise AgentWorkerError("media OCR CLI not found; pass --media-ocr-cli or install llama-mtmd-cli on PATH")
    model_args, model_detail = _media_ocr_cli_model_args(cfg)
    cli_image_path, image_detail = _prepare_media_ocr_cli_image(image_path)
    cmd = [
        cli,
        *model_args,
        "--image",
        str(cli_image_path),
        "-p",
        MEDIA_OCR_PROMPT,
        "-n",
        str(cfg.media_max_output_tokens),
        "--temp",
        "0",
        "--top-k",
        "1",
        "-c",
        "8192",
        "-ngl",
        "99",
    ]
    try:
        proc = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(cfg.media_ocr_timeout_s, 30.0),
            check=False,
            **hidden_subprocess_kwargs(),
        )
    finally:
        if cli_image_path != image_path:
            try:
                cli_image_path.unlink(missing_ok=True)
            except Exception:
                pass
    if proc.returncode != 0:
        failure_type = "media_ocr_oom" if _error_text_looks_oom(f"{proc.stderr}\n{proc.stdout}") else "media_ocr_gguf_cli_exit"
        raise AgentWorkerError(
            f"GGUF OCR CLI exited {proc.returncode}: {_process_output_excerpt(proc.stderr, proc.stdout)}",
            failure_type=failure_type,
            detail={"returncode": proc.returncode},
        )
    text = str(proc.stdout or "").strip()
    if not text:
        raise AgentWorkerError("media OCR returned empty text")
    return {
        "text": text,
        "backend": "glm-ocr",
        "model": cfg.media_ocr_model,
        "detail": {
            "backend_mode": "cli",
            "cli": cli,
            "returncode": proc.returncode,
            "stderr_excerpt": str(proc.stderr or "")[-1200:],
            **image_detail,
            **model_detail,
        },
    }


def _media_ocr_vllm_base_url(cfg: WorkerConfig) -> str:
    return str(
        cfg.media_ocr_vllm_base_url
        or os.environ.get("THOTH_MEDIA_OCR_VLLM_BASE_URL")
        or os.environ.get("THOTH_MEDIA_OCR_SIDECAR_BASE_URL")
        or cfg.media_ocr_api_url
        or ""
    ).strip().rstrip("/")


def _media_ocr_vllm_model(cfg: WorkerConfig) -> str:
    explicit = str(cfg.media_ocr_vllm_model or os.environ.get("THOTH_MEDIA_OCR_VLLM_MODEL") or "").strip()
    if explicit:
        return explicit
    model_hint = str(cfg.media_ocr_model or "").strip()
    if model_hint and "gguf" not in model_hint.lower() and "glm-ocr-gguf" not in model_hint.lower():
        return model_hint
    return "glm-ocr"


def _media_ocr_vllm_api_key() -> str:
    return _media_ocr_env_value(
        "THOTH_MEDIA_OCR_VLLM_API_KEY",
        "THOTH_MEDIA_OCR_SIDECAR_API_KEY",
    )


def _media_ocr_vllm_status(cfg: WorkerConfig) -> dict[str, Any]:
    base_url = _media_ocr_vllm_base_url(cfg)
    model = _media_ocr_vllm_model(cfg)
    status: dict[str, Any] = {
        "available": False,
        "configured": bool(base_url),
        "ready": False,
        "base_url_present": bool(base_url),
        "model": model,
    }
    if not base_url:
        status["reason"] = "THOTH_MEDIA_OCR_VLLM_BASE_URL is not configured"
        return status
    try:
        models_payload = _check_openai_models_ready(
            base_url,
            timeout_s=min(max(cfg.media_ocr_timeout_s, 5.0), 30.0),
            error_prefix="vLLM OCR sidecar",
        )
        model_ids = _model_ids_from_status(models_payload)
        model_lower = model.lower()
        matching_model = any(model_lower == candidate.lower() or model_lower in candidate.lower() for candidate in model_ids)
        status.update(
            {
                "available": True,
                "ready": True,
                "reason": "vLLM OCR sidecar models endpoint is ready",
                "models": model_ids,
                "model_match": matching_model,
            }
        )
        return status
    except Exception as exc:
        status["reason"] = f"{type(exc).__name__}: {exc}"
        return status


def _run_media_ocr_vllm(cfg: WorkerConfig, image_path: Path) -> dict[str, Any]:
    base_url = _media_ocr_vllm_base_url(cfg)
    if not base_url:
        raise AgentWorkerError(
            "vLLM OCR sidecar requested but THOTH_MEDIA_OCR_VLLM_BASE_URL is not configured",
            failure_type="media_ocr_vllm_not_configured",
        )
    model = _media_ocr_vllm_model(cfg)
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": MEDIA_OCR_PROMPT},
                    {"type": "image_url", "image_url": {"url": _media_data_url(image_path)}},
                ],
            }
        ],
        "max_tokens": int(cfg.media_max_output_tokens),
        "temperature": 0,
    }
    text = _harness_completion_content(cfg, payload, role="ocr", timeout_s=cfg.media_ocr_timeout_s)
    return {
        "text": text.strip(),
        "backend": "glm-ocr-vllm",
        "model": model,
        "detail": {
            "backend_mode": "vllm_ocr_sidecar",
            "base_url": base_url,
            "execution": "deepseek_harness",
        },
    }


def _run_media_ocr(cfg: WorkerConfig, image_path: Path) -> dict[str, Any]:
    backend = _normalize_media_ocr_backend(getattr(cfg, "media_ocr_backend", "auto"))
    skipped: list[str] = []
    attempt_errors: list[str] = []
    last_failure_type: str | None = None
    if backend in {"vllm_ocr_sidecar", "auto"}:
        vllm_status = _media_ocr_vllm_status(cfg)
        if not vllm_status.get("ready"):
            skipped.append(f"vllm_ocr_sidecar: {vllm_status.get('reason') or 'not ready'}")
            if backend == "vllm_ocr_sidecar":
                raise AgentWorkerError(
                    "vLLM OCR sidecar requested but not ready: "
                    f"{vllm_status.get('reason') or 'not ready'}; "
                    "set THOTH_MEDIA_OCR_VLLM_BASE_URL and start the GLM-OCR vLLM sidecar",
                    failure_type="media_ocr_vllm_not_ready",
                )
        else:
            try:
                result = _run_media_ocr_vllm(cfg, image_path)
                detail = dict(result.get("detail") or {})
                detail["vllm_status"] = vllm_status
                result["detail"] = detail
                return result
            except Exception as exc:
                last_failure_type = _agent_worker_failure_type(exc, default="media_ocr_vllm_error")
                attempt_errors.append(f"vllm_ocr_sidecar failed ({last_failure_type}): {type(exc).__name__}: {exc}")
                if backend == "vllm_ocr_sidecar":
                    raise
    if backend in {"glm_sdk_sidecar", "auto"}:
        sdk_status = _media_ocr_sdk_status(cfg)
        if not sdk_status.get("available") or not sdk_status.get("configured"):
            skipped.append(f"glm_sdk_sidecar: {sdk_status.get('reason') or 'not ready'}")
            if backend == "glm_sdk_sidecar":
                raise AgentWorkerError(
                    "glmocr SDK sidecar requested but not ready: "
                    f"{sdk_status.get('reason') or 'not ready'}; "
                    "set THOTH_MEDIA_OCR_CONFIG, THOTH_MEDIA_OCR_API_URL, or an SDK API key; mode alone is not sufficient",
                    failure_type="media_ocr_sdk_not_ready",
                )
        else:
            try:
                result = _run_media_ocr_sdk(cfg, image_path)
                detail = dict(result.get("detail") or {})
                detail["sdk_status"] = sdk_status
                result["detail"] = detail
                return result
            except Exception as exc:
                last_failure_type = _agent_worker_failure_type(exc, default="media_ocr_sdk_error")
                attempt_errors.append(f"glm_sdk_sidecar failed ({last_failure_type}): {type(exc).__name__}: {exc}")
                if backend == "glm_sdk_sidecar":
                    raise
    if backend in {"gguf_cli", "auto"}:
        try:
            result = _run_media_ocr_cli(cfg, image_path)
            if skipped:
                detail = dict(result.get("detail") or {})
                detail["fallback_errors"] = [
                    (
                        f"vllm skipped: {item.split(':', 1)[1].strip()}"
                        if item.startswith("vllm_ocr_sidecar:")
                        else f"sdk skipped: {item.split(':', 1)[1].strip()}"
                        if item.startswith("glm_sdk_sidecar:")
                        else f"skipped: {item}"
                    )
                    for item in skipped
                ]
                if "sdk_status" not in detail:
                    detail["sdk_status"] = _media_ocr_sdk_status(cfg)
                result["detail"] = detail
            return result
        except Exception as exc:
            last_failure_type = _agent_worker_failure_type(exc, default="media_ocr_gguf_cli_error")
            attempt_errors.append(f"gguf_cli failed ({last_failure_type}): {type(exc).__name__}: {exc}")
            if backend == "gguf_cli":
                raise AgentWorkerError(str(exc), failure_type=last_failure_type, detail=getattr(exc, "detail", {})) from exc
    if attempt_errors:
        diagnostic = f"; skipped OCR candidates: {'; '.join(skipped)}" if skipped else ""
        raise AgentWorkerError("; ".join(attempt_errors) + diagnostic, failure_type=last_failure_type or "media_ocr_error")
    raise AgentWorkerError(
        f"unsupported media OCR backend: {backend}; expected auto, gguf_cli, glm_sdk_sidecar, or vllm_ocr_sidecar",
        failure_type="media_ocr_backend_unsupported",
    )


def _media_vision_base_url(cfg: WorkerConfig) -> str:
    base_url = str(cfg.media_vision_base_url or "").strip()
    if base_url:
        return base_url
    if cfg.media_vision_allow_text_endpoint:
        return str(cfg.llama_base_url or "").strip()
    return ""


def _media_vision_text_endpoint_refused(cfg: WorkerConfig) -> bool:
    if cfg.media_vision_allow_text_endpoint:
        return False
    text_endpoint = _endpoint_identity(cfg.llama_base_url)
    vision_base_url = _media_vision_base_url(cfg)
    vision_endpoint = _endpoint_identity(vision_base_url)
    return not vision_endpoint or bool(text_endpoint and vision_endpoint == text_endpoint)


def _media_vision_endpoint_stats(cfg: WorkerConfig) -> dict[str, Any]:
    text_endpoint = _endpoint_identity(cfg.llama_base_url)
    vision_endpoint = _endpoint_identity(_media_vision_base_url(cfg))
    vision_model = str(cfg.media_vision_model or cfg.model).strip()
    text_model = str(cfg.model or "").strip()
    split = bool(vision_endpoint and text_endpoint and vision_endpoint != text_endpoint)
    uses_text_endpoint = bool(vision_endpoint and text_endpoint and vision_endpoint == text_endpoint)
    refused_text_endpoint = _media_vision_text_endpoint_refused(cfg)
    return {
        "media_vision_base_url_present": bool(str(cfg.media_vision_base_url or "").strip()),
        "media_vision_endpoint_split": split,
        "media_vision_uses_text_endpoint": uses_text_endpoint,
        "media_vision_requires_split_endpoint": not bool(cfg.media_vision_allow_text_endpoint),
        "media_vision_text_endpoint_allowed": bool(cfg.media_vision_allow_text_endpoint),
        "media_vision_refused_text_endpoint": refused_text_endpoint,
        "media_vision_model_differs": bool(vision_model and text_model and vision_model != text_model),
        "media_vision_health_checked": False,
        "media_vision_ready": False,
        "media_vision_models": [],
        "media_vision_health_error": None,
        "media_vision_disable_thinking": bool(cfg.media_vision_disable_thinking),
    }


def _probe_media_vision_endpoint(cfg: WorkerConfig, run_id: int | None, stats: dict[str, Any]) -> None:
    if not cfg.media_transcription_enabled:
        return
    mode = str(cfg.media_transcription_mode or "route").strip().lower().replace("-", "_")
    if mode == "ocr_only":
        stats["media_vision_ready"] = False
        stats["media_vision_health_checked"] = False
        stats["media_vision_health_error"] = None
        stats["media_vision_health_skipped_reason"] = "ocr_only_mode"
        return
    if bool(stats.get("media_vision_refused_text_endpoint")):
        stats["media_vision_ready"] = False
        stats["media_vision_health_error"] = "media vision requires --media-vision-base-url; refusing to send images to the text endpoint"
        return
    if bool(stats.get("media_vision_uses_text_endpoint")):
        stats["media_vision_ready"] = bool(stats.get("llama_ready"))
        stats["media_vision_models"] = list(_as_list(stats.get("llama_models")))
        return
    stats["media_vision_health_checked"] = True
    try:
        status = _check_openai_models_ready(
            _media_vision_base_url(cfg),
            timeout_s=cfg.media_vision_timeout_s,
            error_prefix="media vision endpoint",
        )
        stats["media_vision_ready"] = True
        stats["media_vision_models"] = _model_ids_from_status(status)
        stats["media_vision_health_error"] = None
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        stats["media_vision_ready"] = False
        stats["media_vision_health_error"] = error
        _record_error(cfg, run_id, error=f"media vision endpoint health failed: {error}", action_kind="media_vision_health_warning")


def _media_vision_prompt(job: dict[str, Any], *, route: dict[str, Any], ocr_text: str | None, ocr_assessment: dict[str, Any] | None) -> str:
    context = _media_source_context(job)
    lines = [MEDIA_VISION_PROMPT, "", "Routing context:"]
    if context.get("source_name"):
        lines.append(f"- source_name: {context.get('source_name')}")
    if context.get("document_title"):
        lines.append(f"- document_title: {_media_summary(str(context.get('document_title')), max_chars=220)}")
    if context.get("original_url"):
        lines.append(f"- image_url: {context.get('original_url')}")
    lines.append(f"- pre_ocr_route: {route.get('image_kind') or 'unknown'} / {route.get('lane') or 'unknown'}")
    if ocr_assessment:
        lines.append(
            "- ocr_assessment: "
            f"kind={ocr_assessment.get('media_kind')}; quality={ocr_assessment.get('ocr_quality')}; "
            f"reason={_media_summary(str(ocr_assessment.get('reason') or ''), max_chars=240)}"
        )
    if ocr_text:
        lines.append("")
        lines.append("Noisy OCR first pass:")
        lines.append(_media_summary(ocr_text, max_chars=1800))
    return "\n".join(lines).strip()


def _normalize_media_structured_series(value: object) -> list[dict[str, Any]]:
    payload = value
    if isinstance(payload, dict):
        for key in ("structured_series", "series", "data_points", "points", "rows"):
            if isinstance(payload.get(key), list):
                payload = payload.get(key)
                break
    rows: list[dict[str, Any]] = []
    for raw in payload if isinstance(payload, list) else []:
        if not isinstance(raw, dict):
            continue
        row: dict[str, Any] = {}
        aliases = {
            "series": ("series", "series_name", "legend", "name"),
            "metric": ("metric", "metric_name", "measure", "label"),
            "category": ("category", "x", "x_value", "bucket"),
            "period": ("period", "date", "year", "quarter", "x_axis_value"),
            "value": ("value", "value_number", "y", "y_value"),
            "value_text": ("value_text", "raw_value", "display_value", "approx_value"),
            "unit": ("unit", "units", "value_unit"),
            "x_axis": ("x_axis", "x_axis_label"),
            "y_axis": ("y_axis", "y_axis_label"),
            "source": ("source", "source_line", "visible_source"),
            "confidence": ("confidence", "read_confidence"),
            "approximate": ("approximate", "is_approximate", "estimated"),
        }
        for dest, keys in aliases.items():
            for key in keys:
                if raw.get(key) not in (None, "", []):
                    row[dest] = raw.get(key)
                    break
        numeric = row.get("value")
        if numeric not in (None, ""):
            try:
                row["value"] = float(str(numeric).replace(",", "").replace("%", "").strip())
            except Exception:
                row["value_text"] = str(numeric)
                row.pop("value", None)
        if row.get("approximate") not in (None, ""):
            row["approximate"] = str(row.get("approximate")).strip().lower() in {"1", "true", "yes", "y", "approx", "approximate"}
        if row and any(row.get(key) not in (None, "", []) for key in ("series", "metric", "category", "period", "value", "value_text")):
            rows.append(row)
        if len(rows) >= 80:
            break
    return rows


def _extract_media_structured_series_parse_result(text: str) -> dict[str, Any]:
    raw = str(text or "")
    candidates: list[object] = []
    malformed_json_blocks = 0
    parse_errors: list[str] = []
    for match in re.finditer(r"```(?:json)?\s*(.*?)```", raw, flags=re.I | re.S):
        block = match.group(1).strip()
        if not block:
            continue
        try:
            candidates.append(json.loads(block))
        except Exception as exc:
            malformed_json_blocks += 1
            if len(parse_errors) < 3:
                parse_errors.append(_media_summary(f"{type(exc).__name__}: {exc}", max_chars=160))
            continue
    if not candidates:
        decoder = json.JSONDecoder()
        first_raw_error: Exception | None = None
        for idx, char in enumerate(raw):
            if char not in "[{":
                continue
            try:
                parsed, _end = decoder.raw_decode(raw[idx:])
            except Exception as exc:
                if first_raw_error is None:
                    first_raw_error = exc
                continue
            candidates.append(parsed)
            break
        if not candidates and first_raw_error is not None:
            malformed_json_blocks += 1
            if len(parse_errors) < 3:
                parse_errors.append(_media_summary(f"{type(first_raw_error).__name__}: {first_raw_error}", max_chars=160))
    for candidate in candidates:
        rows = _normalize_media_structured_series(candidate)
        if rows:
            return {
                "rows": rows,
                "json_candidates": len(candidates),
                "malformed_json_blocks": malformed_json_blocks,
                "parse_errors": parse_errors,
            }
    return {
        "rows": [],
        "json_candidates": len(candidates),
        "malformed_json_blocks": malformed_json_blocks,
        "parse_errors": parse_errors,
    }


def _extract_media_structured_series_from_text(text: str) -> list[dict[str, Any]]:
    result = _extract_media_structured_series_parse_result(text)
    return [dict(row) for row in _as_list(result.get("rows")) if isinstance(row, dict)]


def _media_vision_signal_hint(text: str) -> dict[str, Any]:
    raw = str(text or "")
    blob = raw.lower()
    visual_kind = ""
    match = re.search(r"##\s*visual\s+kind\s*(.*?)(?:\n##\s+|\Z)", raw, flags=re.I | re.S)
    if match:
        visual_kind = _media_summary(match.group(1), max_chars=260).lower()
    non_signal_markers = (
        "signal: non_signal",
        "signal: non-signal",
        "non-signal",
        "non signal",
        "photo_non_signal",
        "decorative",
        "profile photo",
        "avatar",
        "company logo",
        "generic article thumbnail",
        "stock image",
        "blank image",
        "unreadable",
        "no useful source-backed",
        "no useful investment signal",
        "no source-backed investment signal",
        "no quantitative data",
    )
    useful_markers = (
        "useful signal",
        "chart",
        "table",
        "axis",
        "legend",
        "data readout",
        "structured readout",
    )
    non_signal = any(marker in visual_kind for marker in non_signal_markers) or (
        any(marker in blob for marker in non_signal_markers)
        and not any(marker in visual_kind for marker in useful_markers if marker not in {"chart", "table"})
    )
    if not non_signal:
        return {"is_signal": True, "signal": "signal", "signal_reason": "VLM produced useful visual evidence."}
    return {
        "is_signal": False,
        "signal": "non_signal",
        "signal_reason": _media_summary(visual_kind or raw, max_chars=360) or "VLM classified the image as non-signal.",
        "media_kind": "photo_non_signal" if "photo_non_signal" in blob else "non_signal_image",
    }


def _run_media_vision(
    cfg: WorkerConfig,
    image_path: Path,
    job: dict[str, Any],
    *,
    route: dict[str, Any] | None = None,
    ocr_text: str | None = None,
    ocr_assessment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if _media_vision_text_endpoint_refused(cfg):
        raise AgentWorkerError(
            "media vision requires --media-vision-base-url; refusing to send images to the text endpoint",
            failure_type="media_vlm_text_endpoint_refused",
        )
    base_url = _media_vision_base_url(cfg)
    if not base_url:
        raise AgentWorkerError("media vision endpoint is not configured", failure_type="media_vlm_endpoint_not_configured")
    model = str(cfg.media_vision_model or cfg.model).strip()
    route = dict(route or {})
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _media_vision_prompt(job, route=route, ocr_text=ocr_text, ocr_assessment=ocr_assessment)},
                    {"type": "image_url", "image_url": {"url": _media_data_url(image_path, content_type=job.get("content_type"))}},
                ],
            }
        ],
        "temperature": 0,
        "max_tokens": cfg.media_max_output_tokens,
    }
    text = _harness_completion_content(
        cfg, payload, role="vision", timeout_s=cfg.media_vision_timeout_s,
    ).strip()
    if not text:
        raise AgentWorkerError("media vision returned empty text", failure_type="media_vlm_empty_response")
    structured_series_parse = _extract_media_structured_series_parse_result(text)
    structured_parse_detail = {
        "json_candidates": int(structured_series_parse.get("json_candidates") or 0),
        "malformed_json_blocks": int(structured_series_parse.get("malformed_json_blocks") or 0),
        "structured_series_rows": len(_as_list(structured_series_parse.get("rows"))),
    }
    if structured_series_parse.get("parse_errors"):
        structured_parse_detail["parse_errors"] = _as_list(structured_series_parse.get("parse_errors"))[:3]
    signal_hint = _media_vision_signal_hint(text)
    return {
        "text": text,
        "backend": "qwen-vlm",
        "model": model,
        "detail": {
            "base_url": base_url,
            "prompt_version": MEDIA_VISION_PROMPT_VERSION,
            "structured_series": _as_list(structured_series_parse.get("rows")),
            "structured_series_parse": structured_parse_detail,
            **signal_hint,
        },
    }


def _media_text_sparse(text: str, cfg: WorkerConfig) -> bool:
    cleaned = re.sub(r"\s+", " ", str(text or "").strip())
    alnum = len(re.findall(r"[A-Za-z0-9]", cleaned))
    return len(cleaned) < cfg.media_ocr_min_chars or alnum < max(20, cfg.media_ocr_min_chars // 3)


def _transcribe_media_job(cfg: WorkerConfig, job: dict[str, Any], image_path: Path, stats: dict[str, Any]) -> dict[str, Any]:
    route = _media_route(image_path)
    lane = str(route.get("lane") or "ocr_first_then_vlm_if_low_confidence").strip().lower()
    mode = str(cfg.media_transcription_mode or "route").strip().lower()
    job_payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    force_vlm_visual = bool(job_payload.get("force_vlm_after_ocr_failure") or job_payload.get("force_vlm_for_prep_blocking_ocr"))
    force_vlm_visual = force_vlm_visual or bool(job.get("force_vlm_after_ocr_failure") or job.get("force_vlm_for_prep_blocking_ocr"))
    if force_vlm_visual and mode in {"route", "ocr-first", "ocr_first"}:
        mode = "vision-only"
        route = {**route, "lane": "vlm_first", "reason": "VLM visual readout required before text signal extraction."}
        lane = "vlm_first"
    if lane == "ocr_first":
        stats["media_route_ocr_first"] += 1
    elif lane == "vlm_first":
        stats["media_route_vlm_first"] += 1
    else:
        stats["media_route_mixed"] += 1

    route_requests_vlm_after_ocr = bool(mode == "route" and lane == "vlm_first")
    use_ocr_only_stage = mode in {"ocr-only", "ocr_only"}
    use_ocr = mode in {"route", "ocr-first", "ocr_first", "ocr-only", "ocr_only"}
    use_vlm_first = mode in {"vlm-first", "vlm_first", "vision-first", "vision_first", "vision-only", "vision_only"}
    allow_vlm_fallback = mode in {"route", "ocr-first", "ocr_first", "vlm-first", "vlm_first", "vision-first", "vision_first"}

    ocr: dict[str, Any] | None = None
    vision: dict[str, Any] | None = None
    ocr_assessment: dict[str, Any] | None = None
    errors: list[str] = []
    error_types: list[str] = []

    def record_media_error(label: str, exc: Exception, *, default: str) -> None:
        failure_type = _agent_worker_failure_type(exc, default=default)
        error_types.append(failure_type)
        errors.append(f"{label}: {failure_type}: {type(exc).__name__}: {exc}")

    def record_ocr_success(result: dict[str, Any]) -> dict[str, Any]:
        stats["media_ocr_attempts"] += 1
        stats["media_ocr_chars"] += len(str(result.get("text") or ""))
        assessment = _ocr_media_assessment(
            str(result.get("text") or ""),
            route=route,
            job=job,
            detail=result.get("detail") or {},
        )
        quality = str(assessment.get("ocr_quality") or "").strip().lower()
        media_kind = str(assessment.get("media_kind") or "").strip().lower()
        if quality in {"empty", "sparse", "garbled"}:
            stats["media_ocr_quality_low"] += 1
        if assessment.get("source_risk") == "high_value_visual" or media_kind in {"chart", "process_diagram", "technical_diagram", "visual_slide"}:
            stats["media_high_value_visuals"] += 1
        if assessment.get("is_signal") is False:
            stats["media_non_signal_images"] += 1
        if _as_list(assessment.get("layout_labels")):
            stats["media_ocr_layout_label_rows"] += 1
        if (result.get("detail") or {}).get("layout_details_present"):
            stats["media_ocr_layout_details_present"] += 1
        return assessment

    if use_vlm_first:
        try:
            vision = _run_media_vision(cfg, image_path, job, route=route)
            stats["media_vlm_attempts"] += 1
            stats["media_vlm_chars"] += len(str(vision.get("text") or ""))
        except Exception as exc:
            record_media_error("vision", exc, default="media_vlm_error")
    if use_ocr:
        try:
            ocr = _run_media_ocr(cfg, image_path)
            ocr_assessment = record_ocr_success(ocr)
        except Exception as exc:
            record_media_error("ocr", exc, default="media_ocr_error")
    if ocr is None and vision is None and allow_vlm_fallback and not use_vlm_first:
        try:
            vision = _run_media_vision(cfg, image_path, job, route=route)
            stats["media_vlm_attempts"] += 1
            stats["media_vlm_chars"] += len(str(vision.get("text") or ""))
            stats["media_escalations"] += 1
        except Exception as exc:
            record_media_error("vision_fallback_after_ocr_error", exc, default="media_vlm_error")
    ocr_text = str((ocr or {}).get("text") or "")
    classifier_needs_vlm = bool(ocr_assessment and ocr_assessment.get("needs_vlm"))
    sparse_needs_vlm = bool(ocr is not None and _media_text_sparse(ocr_text, cfg))
    vision_detail = (vision or {}).get("detail") if isinstance((vision or {}).get("detail"), dict) else {}
    vision_non_signal = bool(vision_detail.get("is_signal") is False or str(vision_detail.get("signal") or "").strip().lower() == "non_signal")
    non_signal_media = bool((ocr_assessment and ocr_assessment.get("is_signal") is False) or vision_non_signal)
    if vision_non_signal:
        stats["media_non_signal_images"] += 1
    if ocr is not None and allow_vlm_fallback and not non_signal_media and (route_requests_vlm_after_ocr or classifier_needs_vlm or sparse_needs_vlm):
        try:
            vision = _run_media_vision(cfg, image_path, job, route=route, ocr_text=ocr_text, ocr_assessment=ocr_assessment)
            stats["media_vlm_attempts"] += 1
            stats["media_vlm_chars"] += len(str(vision.get("text") or ""))
            stats["media_escalations"] += 1
            if route_requests_vlm_after_ocr or classifier_needs_vlm:
                stats["media_vlm_required_by_classifier"] += 1
        except Exception as exc:
            record_media_error("vision_fallback", exc, default="media_vlm_error")
    if use_vlm_first and vision is None and ocr is None and mode not in {"vision-only", "vision_only"}:
        try:
            ocr = _run_media_ocr(cfg, image_path)
            ocr_assessment = record_ocr_success(ocr)
            stats["media_escalations"] += 1
        except Exception as exc:
            record_media_error("ocr_fallback_after_vision_error", exc, default="media_ocr_error")

    parts: list[str] = []
    backends: list[str] = []
    models: list[str] = []
    detail: dict[str, Any] = {"route": route, "mode": mode, "errors": errors, "error_types": list(dict.fromkeys(error_types))}
    vlm_required_reasons: list[str] = []
    if not non_signal_media:
        if route_requests_vlm_after_ocr:
            vlm_required_reasons.append("route_lane_vlm_first")
        if classifier_needs_vlm:
            vlm_required_reasons.append("classifier_needs_vlm")
        if sparse_needs_vlm:
            vlm_required_reasons.append("sparse_ocr")
        if use_vlm_first:
            vlm_required_reasons.append("mode_vlm_first")
    vlm_required = bool((allow_vlm_fallback or use_vlm_first or use_ocr_only_stage) and vlm_required_reasons)
    vlm_available = vision is not None
    detail["vlm_required"] = vlm_required
    detail["vlm_available"] = vlm_available
    detail["vlm_required_reasons"] = vlm_required_reasons
    detail["prep_blocking"] = bool(vlm_required and not vlm_available)
    if detail["prep_blocking"]:
        stats["media_vlm_required_missing"] += 1
    if ocr_assessment:
        detail["ocr_assessment"] = ocr_assessment
        detail["media_kind"] = ocr_assessment.get("media_kind")
        detail["ocr_quality"] = ocr_assessment.get("ocr_quality")
        detail["needs_vlm"] = bool(ocr_assessment.get("needs_vlm"))
        detail["source_risk"] = ocr_assessment.get("source_risk")
        detail["visual_signal_terms"] = ocr_assessment.get("visual_signal_terms") or []
        detail["layout_labels"] = ocr_assessment.get("layout_labels") or []
        detail["is_signal"] = bool(ocr_assessment.get("is_signal", True))
        detail["signal"] = "signal" if detail["is_signal"] else "non_signal"
        detail["signal_reason"] = ocr_assessment.get("reason")
    if ocr is not None:
        parts.append(f"OCR markdown:\n{ocr['text']}")
        backends.append(str(ocr.get("backend") or "ocr"))
        models.append(str(ocr.get("model") or ""))
        detail["ocr"] = ocr.get("detail") or {}
    if vision is not None:
        parts.append(f"Visual description:\n{vision['text']}")
        backends.append(str(vision.get("backend") or "vlm"))
        models.append(str(vision.get("model") or ""))
        detail["vision"] = vision.get("detail") or {}
        if vision_non_signal:
            detail["is_signal"] = False
            detail["signal"] = "non_signal"
            detail["signal_reason"] = vision_detail.get("signal_reason") or "VLM classified the image as non-signal."
            detail["media_kind"] = vision_detail.get("media_kind") or detail.get("media_kind") or "non_signal_image"
        structured_series = _normalize_media_structured_series((vision.get("detail") or {}).get("structured_series"))
        if structured_series:
            detail["structured_series"] = structured_series
            stats["media_structured_series_rows"] += len(structured_series)
    text = "\n\n".join(part for part in parts if part.strip()).strip()
    if not text:
        unique_error_types = list(dict.fromkeys(error_types))
        has_ocr_failure = any(value.startswith("media_ocr") for value in unique_error_types)
        has_vlm_failure = any(value.startswith("media_vlm") for value in unique_error_types)
        failure_type = (
            "media_ocr_failed_vlm_unavailable"
            if has_ocr_failure and has_vlm_failure
            else unique_error_types[0]
            if unique_error_types
            else "media_transcription_no_text"
        )
        raise AgentWorkerError(
            "; ".join(errors) or "media transcription produced no text",
            failure_type=failure_type,
            detail={"error_types": unique_error_types},
        )
    return {
        "status": "success",
        "job_id": int(job["job_id"]),
        "document_id": int(job["document_id"]),
        "attachment_id": int(job["attachment_id"]),
        "backend": "+".join(dict.fromkeys(backends))[:64] or "local-media",
        "model": "+".join(value for value in dict.fromkeys(models) if value)[:255] or None,
        "confidence": "medium" if errors or str(detail.get("ocr_quality") or "").lower() in {"empty", "sparse", "garbled"} else "high",
        "text": text,
        "summary": _media_summary(text),
        "detail": detail,
    }


def _stage_rows(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [row for row in _as_list(plan.get("stages")) if isinstance(row, dict)]


def _stage_quota(plan: dict[str, Any], stage: str, default: int) -> int:
    if not plan:
        return max(0, int(default or 0))
    stage_key = str(stage or "").strip().lower()
    for row in _stage_rows(plan):
        if str(row.get("stage") or "").strip().lower() != stage_key:
            continue
        try:
            return max(0, int(row.get("quota") or 0))
        except Exception:
            return max(0, int(default or 0))
    return max(0, int(default or 0))


def _stage_signal_lookback_hours(plan: dict[str, Any], cfg: WorkerConfig) -> int:
    if not plan:
        return int(cfg.lookback_hours)
    candidates: list[object] = [plan.get("signal_lookback_hours")]
    for row in _stage_rows(plan):
        if str(row.get("stage") or "").strip().lower() == "signal_extraction":
            candidates.append(row.get("lookback_hours"))
    candidates.append(plan.get("lookback_hours"))
    for value in candidates:
        try:
            hours = int(value or 0)
        except Exception:
            continue
        if hours > 0:
            return hours
    return int(cfg.lookback_hours)


def _merge_media_worker_stats(stats: dict[str, Any], job_stats: dict[str, Any]) -> None:
    for key, value in dict(job_stats or {}).items():
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            stats[key] = stats.get(key, 0) + value


def _media_executor_concurrency(cfg: WorkerConfig, stats: dict[str, Any], *, job_count: int) -> tuple[int, int, int, bool]:
    ocr_ready = bool(stats.get("media_ocr_vllm_ready")) or (
        bool(stats.get("media_ocr_sdk_available")) and bool(stats.get("media_ocr_sdk_configured"))
    )
    vlm_ready = bool(stats.get("media_vision_ready"))
    sidecar_ready = bool(ocr_ready or vlm_ready)
    ocr_concurrency = max(1, min(int(cfg.media_ocr_concurrency or 1), 8))
    vlm_concurrency = max(1, min(int(cfg.media_vlm_concurrency or 1), 8))
    if not ocr_ready:
        # Current media jobs are OCR-first. Until the OCR phase itself is served
        # by a sidecar/SDK surface, keep job-level execution serialized so route
        # mode does not fan out multiple GGUF CLI OCR subprocesses just because
        # a VLM sidecar is available for the later fallback step.
        ocr_concurrency = 1
        vlm_concurrency = 1
    if cfg.media_sidecar_concurrency_auto and sidecar_ready:
        if ocr_ready and ocr_concurrency <= 1:
            ocr_concurrency = min(4, max(1, job_count))
        if ocr_ready and vlm_ready and vlm_concurrency <= 1:
            vlm_concurrency = min(2, max(1, job_count))
    worker_count = max(1, min(max(ocr_concurrency, vlm_concurrency), max(1, job_count), 8))
    if not sidecar_ready and cfg.media_sidecar_concurrency_auto and cfg.media_ocr_concurrency <= 1 and cfg.media_vlm_concurrency <= 1:
        worker_count = 1
        ocr_concurrency = 1
        vlm_concurrency = 1
    return worker_count, ocr_concurrency, vlm_concurrency, sidecar_ready


def _read_stage_plan(cfg: WorkerConfig, run_id: int, stats: dict[str, Any]) -> dict[str, Any]:
    if not cfg.stage_planning_enabled:
        return {}
    try:
        data = _api_get(
            cfg,
            "/api/local-agent/stages/plan",
            run_id=run_id,
            params={
                "media_limit": cfg.media_transcription_limit if cfg.media_transcription_enabled else 0,
                "signal_limit": cfg.batch_size,
                "durable_limit": cfg.idle_deep_analysis_limit if cfg.idle_deep_analysis_enabled else 0,
                "lookback_hours": cfg.lookback_hours,
                "queue_strategy": cfg.queue_strategy,
                "prompt_version": cfg.prompt_version,
                "run_id": run_id,
            },
        )
        plan = data.get("plan") if isinstance(data, dict) else {}
        if not isinstance(plan, dict):
            return {}
        stats["stage_plan_reads"] = int(stats.get("stage_plan_reads") or 0) + 1
        stats["stage_plan_queue_strategy"] = str(plan.get("queue_strategy") or cfg.queue_strategy)
        stats["stage_plan_media_quota"] = _stage_quota(plan, "media_prep", cfg.media_transcription_limit if cfg.media_transcription_enabled else 0)
        stats["stage_plan_signal_quota"] = _stage_quota(plan, "signal_extraction", cfg.batch_size)
        stats["stage_plan_durable_quota"] = _stage_quota(plan, "durable_reasoning", cfg.idle_deep_analysis_limit if cfg.idle_deep_analysis_enabled else 0)
        stats["stage_plan_signal_lookback_hours"] = _stage_signal_lookback_hours(plan, cfg)
        lookback_expansion = plan.get("lookback_expansion") if isinstance(plan.get("lookback_expansion"), dict) else {}
        stats["stage_plan_lookback_expanded"] = bool(lookback_expansion.get("expanded"))
        stats["stage_plan_lookback_expansion_reason"] = lookback_expansion.get("reason")
        for row in _stage_rows(plan):
            stage = str(row.get("stage") or "").strip().lower()
            if stage == "media_prep":
                stats["stage_plan_pending_media"] = int(row.get("pending_work") or row.get("pending_jobs") or 0)
                stats["stage_plan_pending_media_work"] = int(row.get("pending_work") or row.get("pending_jobs") or 0)
                stats["stage_plan_unresolved_media_documents"] = int(row.get("unresolved_documents") or 0)
                stats["stage_plan_ocr_pending_media"] = int(row.get("ocr_pending_jobs") or 0)
                stats["stage_plan_vlm_pending_media"] = int(row.get("vlm_pending_jobs") or 0)
            elif stage == "signal_extraction":
                stats["stage_plan_documents_planned"] = int(row.get("planned_documents") or 0)
                stats["stage_plan_retry_pending_signal"] = int(row.get("retry_pending_compressions") or 0)
            elif stage == "durable_reasoning":
                stats["stage_plan_durable_candidates"] = int(row.get("candidate_routes") or 0)
        batch_plan = plan.get("batch_plan") if isinstance(plan.get("batch_plan"), dict) else {}
        stats["stage_plan_deferred_pending_media"] = int(batch_plan.get("documents_deferred_pending_media") or 0)
        return plan
    except Exception as exc:
        stats["stage_plan_errors"] = int(stats.get("stage_plan_errors") or 0) + 1
        _log_worker_event("stage_plan_read_failed", error=f"{type(exc).__name__}: {exc}")
        return {}


def _signal_extraction_defers_pending_media(_cfg: WorkerConfig) -> bool:
    # Signal extraction is text-only. Documents with unresolved media must wait
    # for the OCR/VLM prep stage even when this worker loadout is not running it.
    return True


def _run_media_transcriptions(
    cfg: WorkerConfig,
    run_id: int,
    stats: dict[str, Any],
    *,
    limit_override: int | None = None,
) -> None:
    total_limit = cfg.media_transcription_limit if limit_override is None else max(0, int(limit_override))
    total_limit = min(total_limit, cfg.media_transcription_limit)
    if not cfg.media_transcription_enabled or total_limit <= 0:
        return
    mode = str(cfg.media_transcription_mode or "route").strip().lower().replace("-", "_")
    if mode in {"vision_only", "vision_first", "vlm_first"}:
        media_lane = "vlm"
    elif mode in {"ocr_only", "ocr_first"}:
        media_lane = "ocr"
    elif not bool(stats.get("media_vision_ready")):
        media_lane = "ocr"
    else:
        media_lane = "any"
    stats["media_claim_lane"] = media_lane
    chunk_limit = max(1, min(total_limit, _env_int("THOTH_MEDIA_TRANSCRIPTION_CHUNK_LIMIT", 8)))
    remaining = total_limit
    stats["media_adaptive_requested_limit"] = max(int(stats.get("media_adaptive_requested_limit") or 0), total_limit)
    stats["media_adaptive_initial_chunk_limit"] = max(int(stats.get("media_adaptive_initial_chunk_limit") or 0), chunk_limit)

    def post_error(job: dict[str, Any], exc: Exception) -> None:
        stats["media_jobs_failed"] += 1
        stats["errors"] += 1
        failure_type = _agent_worker_failure_type(exc, default="media_transcription_error")
        secondary_error_types = [str(value) for value in _as_list(getattr(exc, "detail", {}).get("error_types") if isinstance(getattr(exc, "detail", None), dict) else None)]
        _increment_stats_counter(stats, "media_job_failure_types", failure_type)
        error = f"{type(exc).__name__}: {exc}"
        try:
            _api_post(
                cfg,
                "/api/local-agent/media/transcriptions",
                {
                    "job_id": job.get("job_id"),
                    "document_id": job.get("document_id"),
                    "attachment_id": job.get("attachment_id"),
                    "status": "error",
                    "backend": "local-media",
                    "model": cfg.media_vision_model or cfg.model,
                    "failure_type": failure_type,
                    "error_types": secondary_error_types,
                    "error": error,
                },
                run_id=run_id,
            )
        except Exception:
            pass
        _record_error(
            cfg,
            run_id,
            error=f"media transcription failed ({failure_type}): {error}",
            document_id=int(job["document_id"]) if str(job.get("document_id") or "").isdigit() else None,
            action_kind="media_transcription_error",
        )

    while remaining > 0:
        claim_limit = min(remaining, chunk_limit)
        params = {
            "limit": claim_limit,
            "enqueue_limit": claim_limit,
            "run_id": run_id,
            "render_readable_pdf_pages": str(bool(cfg.media_render_readable_pdf_pages)).lower(),
            "media_lane": media_lane,
        }
        if cfg.target_document_ids:
            params["document_ids"] = ",".join(str(value) for value in cfg.target_document_ids)
        data = _api_get(
            cfg,
            "/api/local-agent/media/transcriptions/next",
            run_id=run_id,
            params=params,
        )
        jobs = [job for job in _as_list(data.get("jobs")) if isinstance(job, dict)]
        stats["media_queue_reads"] += 1
        stats["media_jobs_claimed"] += len(jobs)
        if not jobs:
            return
        remaining -= len(jobs)
        chunk_failure_types_before = dict(stats.get("media_job_failure_types") or {})
        ocr_attempts_before = int(stats.get("media_ocr_attempts") or 0)
        vlm_attempts_before = int(stats.get("media_vlm_attempts") or 0)

        with tempfile.TemporaryDirectory(prefix="thoth-media-batch-") as tmp:
            prepared: list[tuple[dict[str, Any], Path]] = []
            for job in jobs:
                try:
                    bytes_url = str(job.get("bytes_url") or f"/api/local-agent/media/attachments/{int(job['attachment_id'])}/bytes")
                    image_bytes = _api_get_bytes(cfg, bytes_url, run_id=run_id, params={"run_id": run_id})
                    stats["media_bytes_read"] += len(image_bytes)
                    image_path = Path(tmp) / f"attachment-{int(job['attachment_id'])}{_media_suffix(job)}"
                    image_path.write_bytes(image_bytes)
                    prepared.append((job, image_path))
                except Exception as exc:
                    post_error(job, exc)
            if not prepared:
                continue
            stats["media_batch_size"] = max(int(stats.get("media_batch_size") or 0), len(prepared))
            worker_count, ocr_concurrency, vlm_concurrency, sidecar_ready = _media_executor_concurrency(cfg, stats, job_count=len(prepared))
            stats["media_worker_concurrency"] = max(int(stats.get("media_worker_concurrency") or 1), worker_count)
            stats["media_ocr_concurrency"] = ocr_concurrency
            stats["media_vlm_concurrency"] = vlm_concurrency
            stats["media_sidecar_batch_ready"] = bool(sidecar_ready and worker_count > 1)
            started = time.perf_counter()

            def run_prepared(item: tuple[dict[str, Any], Path]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
                job, image_path = item
                job_stats: defaultdict[str, Any] = defaultdict(int)
                result = _transcribe_media_job(cfg, job, image_path, job_stats)
                return job, result, dict(job_stats)

            futures: dict[concurrent.futures.Future[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]], dict[str, Any]] = {}
            with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="thoth-media") as executor:
                for item in prepared:
                    future = executor.submit(run_prepared, item)
                    futures[future] = item[0]
                for future in concurrent.futures.as_completed(futures):
                    job = futures[future]
                    try:
                        _job, result, job_stats = future.result()
                        _merge_media_worker_stats(stats, job_stats)
                        detail = result.get("detail") if isinstance(result.get("detail"), dict) else {}
                        if detail.get("vlm_required") or detail.get("needs_vlm"):
                            stats["media_vlm_needed_after_ocr"] += 1
                        _api_post(cfg, "/api/local-agent/media/transcriptions", result, run_id=run_id)
                        stats["media_jobs_completed"] += 1
                    except Exception as exc:
                        post_error(job, exc)
            elapsed = round(time.perf_counter() - started, 3)
            stats["media_batch_executor_wall_seconds"] = round(float(stats.get("media_batch_executor_wall_seconds") or 0.0) + elapsed, 3)
            if int(stats.get("media_ocr_attempts") or 0) > ocr_attempts_before:
                stats["media_ocr_phase_wall_seconds"] = round(float(stats.get("media_ocr_phase_wall_seconds") or 0.0) + elapsed, 3)
            if int(stats.get("media_vlm_attempts") or 0) > vlm_attempts_before:
                stats["media_vlm_phase_wall_seconds"] = round(float(stats.get("media_vlm_phase_wall_seconds") or 0.0) + elapsed, 3)

        chunk_failure_types_after = dict(stats.get("media_job_failure_types") or {})
        oom_delta = 0
        for failure_type in ("media_ocr_oom", "media_vlm_oom", "model_oom"):
            oom_delta += int(chunk_failure_types_after.get(failure_type) or 0) - int(chunk_failure_types_before.get(failure_type) or 0)
        if oom_delta > 0 and chunk_limit > 1:
            chunk_limit = max(1, chunk_limit // 2)
            stats["media_adaptive_oom_backoffs"] = int(stats.get("media_adaptive_oom_backoffs") or 0) + 1
            stats["media_adaptive_current_chunk_limit"] = chunk_limit


def _initial_run_stats(cfg: WorkerConfig) -> dict[str, Any]:
    sdk_status = _media_ocr_sdk_status(cfg)
    vllm_status = _media_ocr_vllm_status(cfg)
    media_vision_status = _media_vision_endpoint_stats(cfg)
    return {
        "documents": 0,
        "compressions": 0,
        "claims": 0,
        "unresolved": 0,
        "writes": 0,
        "research_write_candidates": 0,
        "research_write_skipped_policy": 0,
        "research_write_errors": 0,
        "research_requests": 0,
        "errors": 0,
        "model_fallbacks": 0,
        "decision_reviews": 0,
        "decision_review_noops": 0,
        "decision_review_memo_candidates": 0,
        "decision_context_reads": 0,
        "decision_context_unresolved": 0,
        "decision_context_unresolved_retries": 0,
        "decision_context_unresolved_retry_targets": 0,
        "decision_context_unresolved_retry_resolved": 0,
        "decision_context_unresolved_retry_skipped": 0,
        "decision_context_unresolved_retry_failed": 0,
        "calendar_reads": 0,
        "market_context_reads": 0,
        "institutional_model_context_reads": 0,
        "durable_context_reads": 0,
        "durable_context_errors": 0,
        "run_durable_progress_items": 0,
        "signal_extraction_deferred_fields": 0,
        "catalysts": 0,
        "memos": 0,
        "memo_candidates": 0,
        "memo_skipped_low_confidence": 0,
        "memo_skipped_missing_identity": 0,
        "memo_skipped_missing_content": 0,
        "memo_write_errors": 0,
        "analyst_evidence": 0,
        "analyst_evidence_skipped_not_modelable": 0,
        "evidence_links": 0,
        "dataset_rows": 0,
        "dataset_materializations": 0,
        "supply_chain_metric_bindings": 0,
        "instrument_request_fulfillments": 0,
        "model_delta_candidates": 0,
        "model_delta_evidence": 0,
        "model_delta_skipped_low_confidence": 0,
        "model_delta_skipped_missing_identity": 0,
        "model_delta_skipped_missing_value": 0,
        "model_delta_skipped_duplicate_context": 0,
        "durable_duplicate_skipped": 0,
        "backlog_mode": bool(cfg.backlog_mode),
        "queue_strategy": cfg.queue_strategy,
        "target_document_ids": list(cfg.target_document_ids),
        "target_document_count": len(cfg.target_document_ids),
        "llama_ready": False,
        "llama_models": [],
        "llama_context_tokens": None,
        "llama_prompt_safe_tokens": None,
        "worker_loadout_version": WORKER_LOADOUT_VERSION,
        "loadout_preflight": False,
        "backlog_plan_documents": 0,
        "backlog_plan_batches": 0,
        "stage_planning_enabled": bool(cfg.stage_planning_enabled),
        "stage_plan_reads": 0,
        "stage_plan_errors": 0,
        "stage_plan_queue_strategy": cfg.queue_strategy,
        "stage_plan_media_quota": 0,
        "stage_plan_signal_quota": 0,
        "stage_plan_durable_quota": 0,
        "stage_plan_signal_lookback_hours": cfg.lookback_hours,
        "stage_plan_lookback_expanded": False,
        "stage_plan_lookback_expansion_reason": None,
        "stage_plan_pending_media": 0,
        "stage_plan_pending_media_work": 0,
        "stage_plan_unresolved_media_documents": 0,
        "stage_plan_ocr_pending_media": 0,
        "stage_plan_vlm_pending_media": 0,
        "stage_plan_documents_planned": 0,
        "stage_plan_retry_pending_signal": 0,
        "stage_plan_durable_candidates": 0,
        "stage_plan_deferred_pending_media": 0,
        "documents_deferred_pending_prep": 0,
        "documents_deferred_pending_prep_reasons": {},
        "document_processing_issue_types": {},
        "true_batching": bool(cfg.true_batching),
        "batch_plan_reads": 0,
        "planned_batches": 0,
        "planned_documents": 0,
        "packed_batches": 0,
        "packed_documents": 0,
        "single_documents": 0,
        "batch_model_fallbacks": 0,
        "batch_plan_fallbacks": 0,
        "media_transcription_enabled": bool(cfg.media_transcription_enabled),
        "media_transcription_only": bool(cfg.media_transcription_only),
        "media_transcription_mode": cfg.media_transcription_mode,
        "media_claim_lane": None,
        "media_render_readable_pdf_pages": bool(cfg.media_render_readable_pdf_pages),
        "media_batch_size": 0,
        "media_sidecar_batch_ready": False,
        "media_ocr_concurrency": int(cfg.media_ocr_concurrency),
        "media_vlm_concurrency": int(cfg.media_vlm_concurrency),
        "media_worker_concurrency": 1,
        "media_sidecar_concurrency_auto": bool(cfg.media_sidecar_concurrency_auto),
        "media_batch_executor_wall_seconds": 0.0,
        "media_ocr_phase_wall_seconds": 0.0,
        "media_vlm_phase_wall_seconds": 0.0,
        "media_vlm_needed_after_ocr": 0,
        "media_jobs_claimed": 0,
        "media_jobs_completed": 0,
        "media_jobs_failed": 0,
        "media_job_failure_types": {},
        "media_queue_reads": 0,
        "media_bytes_read": 0,
        "media_ocr_attempts": 0,
        "media_vlm_attempts": 0,
        "media_escalations": 0,
        "media_vlm_required_by_classifier": 0,
        "media_vlm_required_missing": 0,
        "media_ocr_quality_low": 0,
        "media_high_value_visuals": 0,
        "media_non_signal_images": 0,
        "media_ocr_chars": 0,
        "media_vlm_chars": 0,
        "media_ocr_layout_details_present": 0,
        "media_ocr_layout_label_rows": 0,
        "media_ocr_backend": cfg.media_ocr_backend,
        "media_ocr_model": cfg.media_ocr_model,
        "media_ocr_sdk_available": bool(sdk_status.get("available")),
        "media_ocr_sdk_configured": bool(sdk_status.get("configured")),
        "media_ocr_sdk_reason": sdk_status.get("reason"),
        "media_ocr_sdk_version": sdk_status.get("version"),
        "media_ocr_sdk_config_path_present": bool(sdk_status.get("config_path_present")),
        "media_ocr_sdk_config_path_exists": bool(sdk_status.get("config_path_exists")),
        "media_ocr_sdk_api_url_present": bool(sdk_status.get("api_url_present")),
        "media_ocr_sdk_api_key_present": bool(sdk_status.get("api_key_present")),
        "media_ocr_sdk_selfhosted_surface_present": bool(sdk_status.get("selfhosted_surface_present")),
        "media_ocr_sdk_layout_available": bool(sdk_status.get("layout_available")),
        "media_ocr_sdk_layout_reason": sdk_status.get("layout_reason"),
        "media_ocr_sdk_mode": sdk_status.get("mode"),
        "media_ocr_sdk_layout_device": sdk_status.get("layout_device"),
        "media_ocr_vllm_configured": bool(vllm_status.get("configured")),
        "media_ocr_vllm_ready": bool(vllm_status.get("ready")),
        "media_ocr_vllm_reason": vllm_status.get("reason"),
        "media_ocr_vllm_model": vllm_status.get("model"),
        "media_ocr_vllm_models": vllm_status.get("models") or [],
        "media_vision_model": cfg.media_vision_model or cfg.model,
        **media_vision_status,
        "media_route_ocr_first": 0,
        "media_route_vlm_first": 0,
        "media_route_mixed": 0,
        "idle_deep_analysis_enabled": bool(cfg.idle_deep_analysis_enabled),
        "idle_deep_analysis_candidates": 0,
        "idle_deep_analysis_traces": 0,
        "idle_deep_analysis_errors": 0,
        "durable_review_only": bool(cfg.durable_review_only),
        "durable_review_candidates": 0,
        "durable_review_processed": 0,
        "durable_review_errors": 0,
    }


def run_loadout_preflight(cfg: WorkerConfig) -> dict[str, Any]:
    stats = _initial_run_stats(cfg)
    stats["loadout_preflight"] = True
    try:
        run_id = _start_run(cfg, "worker_preflight", initial_stats=stats)
        cfg = replace(cfg, harness_run_id=run_id)
    except Exception as exc:
        return _mark_run_start_error(stats, exc, run_type="worker_preflight")
    try:
        ticker = str(cfg.preflight_ticker or "TSM").strip().upper() or "TSM"
        market = _api_get(
            cfg,
            "/api/local-agent/market-context",
            run_id=run_id,
            params={
                "base_currency": cfg.market_base_currency,
                "live_quotes": str(bool(cfg.market_live_quotes)).lower(),
                "max_items": cfg.market_max_items,
                "run_id": run_id,
            },
        )
        stats["market_context_reads"] += 1
        stats["market_context_watchlist_count"] = len(_as_list(((market.get("context") or {}).get("market_watchlist") or {}).get("items")))
        if cfg.decision_spine_enabled:
            _api_get(
                cfg,
                "/api/local-agent/decision/context",
                run_id=run_id,
                params={"ticker": ticker, "run_id": run_id},
            )
            stats["decision_context_reads"] += 1
            _api_get(
                cfg,
                "/api/local-agent/calendar",
                run_id=run_id,
                params={
                    "ticker": ticker,
                    "days_back": cfg.calendar_days_back,
                    "days_forward": cfg.calendar_days_forward,
                    "limit": cfg.calendar_limit,
                    "run_id": run_id,
                },
            )
            stats["calendar_reads"] += 1
        backlog = _api_get(
            cfg,
            "/api/local-agent/documents/backlog/plan",
            run_id=run_id,
            params={
                "lookback_hours": cfg.lookback_hours,
                "include_opened": str(bool(cfg.include_opened or cfg.backlog_mode)).lower(),
                "include_filings": str(bool(cfg.include_filings)).lower(),
                "include_product_snapshots": "false",
                "queue_strategy": cfg.queue_strategy,
                "sample_limit": 5,
                "batch_size": cfg.batch_size,
                "run_id": run_id,
            },
        )
        plan = backlog.get("plan") if isinstance(backlog, dict) else {}
        if isinstance(plan, dict):
            stats["backlog_plan_documents"] = int(plan.get("total_documents") or 0)
            stats["backlog_plan_batches"] = int(plan.get("estimated_batches") or 0)
        if cfg.true_batching:
            batch_plan = _api_get(
                cfg,
                "/api/local-agent/documents/batch-plan",
                run_id=run_id,
                params={
                    "lookback_hours": cfg.lookback_hours,
                    "include_opened": str(bool(cfg.include_opened or cfg.backlog_mode)).lower(),
                    "include_filings": str(bool(cfg.include_filings)).lower(),
                    "include_product_snapshots": "false",
                    "defer_pending_media": str(_signal_extraction_defers_pending_media(cfg)).lower(),
                    "queue_strategy": cfg.queue_strategy,
                    "prompt_version": cfg.prompt_version,
                    "limit": cfg.batch_size,
                    "max_input_tokens": cfg.batch_max_input_tokens,
                    "max_documents_per_batch": cfg.batch_max_documents,
                    "run_id": run_id,
                },
            ).get("plan") or {}
            stats["batch_plan_reads"] += 1
            stats["planned_batches"] = len(_as_list(batch_plan.get("batches")))
            stats["planned_documents"] = int(batch_plan.get("documents_planned") or 0)
        _read_stage_plan(cfg, run_id, stats)
        _finish_run(cfg, run_id, status="success", summary="worker loadout preflight passed", stats=stats)
        return stats
    except Exception as exc:
        stats["errors"] += 1
        error = f"{type(exc).__name__}: {exc}"
        _record_error(cfg, run_id, error=error, action_kind="worker_preflight_error")
        _finish_run(cfg, run_id, status="error", summary="worker loadout preflight failed", stats=stats, error=error)
        return stats


def _fetch_document_batches(
    cfg: WorkerConfig,
    run_id: int,
    stats: dict[str, Any],
    *,
    limit_override: int | None = None,
    stage_plan: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    batch_limit = cfg.batch_size if limit_override is None else max(0, int(limit_override))
    batch_limit = min(batch_limit, cfg.batch_size)
    if batch_limit <= 0:
        return []
    if cfg.target_document_ids:
        ids = list(cfg.target_document_ids[:batch_limit])
        stats["planned_batches"] += 1
        stats["planned_documents"] += len(ids)
        if cfg.true_batching and len(ids) > 1:
            return [
                {
                    "batch_id": "targeted-" + "-".join(str(value) for value in ids),
                    "mode": "packed",
                    "reason": "target_document_ids",
                    "document_ids": ids,
                }
            ]
        return [
            {
                "batch_id": f"targeted-{document_id}",
                "mode": "single",
                "reason": "target_document_ids",
                "document_ids": [document_id],
            }
            for document_id in ids
        ]
    signal_lookback_hours = _stage_signal_lookback_hours(stage_plan or {}, cfg)
    if cfg.true_batching and batch_limit > 1:
        try:
            data = _api_get(
                cfg,
                "/api/local-agent/documents/batch-plan",
                run_id=run_id,
                params={
                    "limit": batch_limit,
                    "lookback_hours": signal_lookback_hours,
                    "include_filings": str(bool(cfg.include_filings)).lower(),
                    "include_opened": str(bool(cfg.include_opened)).lower(),
                    "include_product_snapshots": "false",
                    "defer_pending_media": str(_signal_extraction_defers_pending_media(cfg)).lower(),
                    "queue_strategy": cfg.queue_strategy,
                    "prompt_version": cfg.prompt_version,
                    "max_input_tokens": cfg.batch_max_input_tokens,
                    "max_documents_per_batch": min(cfg.batch_max_documents, batch_limit),
                    "run_id": run_id,
                },
            )
            plan = data.get("plan") if isinstance(data, dict) else {}
            batches = _as_list(plan.get("batches") if isinstance(plan, dict) else None)
            clean_batches = [batch for batch in batches if isinstance(batch, dict) and _as_list(batch.get("document_ids"))]
            if clean_batches:
                stats["batch_plan_reads"] += 1
                stats["planned_batches"] += len(clean_batches)
                stats["planned_documents"] += sum(len(_as_list(batch.get("document_ids"))) for batch in clean_batches)
                return clean_batches
        except Exception as exc:
            stats["batch_plan_fallbacks"] += 1
            _record_error(cfg, run_id, error=f"batch plan read failed; falling back to single queue: {exc}", action_kind="document_batch_plan_error")
    queue = _api_get(
        cfg,
        "/api/local-agent/documents/next",
        run_id=run_id,
        params={
            "limit": batch_limit,
            "lookback_hours": signal_lookback_hours,
            "include_filings": str(bool(cfg.include_filings)).lower(),
            "include_opened": str(bool(cfg.include_opened)).lower(),
            "defer_pending_media": str(_signal_extraction_defers_pending_media(cfg)).lower(),
            "queue_strategy": cfg.queue_strategy,
            "prompt_version": cfg.prompt_version,
            "run_id": run_id,
        },
    ).get("documents") or []
    return [
        {
            "batch_id": f"single-{item.get('id')}",
            "mode": "single",
            "reason": "legacy_queue",
            "document_ids": [item.get("id")],
            "documents": [item],
        }
        for item in queue
        if isinstance(item, dict) and item.get("id") is not None
    ]


def _fetch_documents_by_ids(
    cfg: WorkerConfig,
    run_id: int,
    document_ids: list[int],
    stats: dict[str, Any],
) -> list[dict[str, Any]]:
    if not document_ids:
        return []
    try:
        data = _api_post(
            cfg,
            "/api/local-agent/documents/batch",
            {
                "run_id": run_id,
                "document_ids": document_ids,
                "max_text_chars": min(128_000, max(cfg.max_input_chars, cfg.max_input_chars * 4)),
            },
            run_id=run_id,
        )
        docs = [doc for doc in _as_list(data.get("documents")) if isinstance(doc, dict) and doc.get("id") is not None]
        if docs:
            return docs
        for error in _as_list(data.get("errors")):
            if isinstance(error, dict):
                stats["errors"] += 1
                _record_error(
                    cfg,
                    run_id,
                    error=f"document batch read failed for {error.get('document_id')}: {error.get('error')}",
                    document_id=_int_or_none(error.get("document_id")),
                    action_kind="document_batch_read_error",
                )
    except Exception as exc:
        _log_worker_event("document_batch_fetch_fallback", error=f"{type(exc).__name__}: {exc}", count=len(document_ids))
    docs: list[dict[str, Any]] = []
    for document_id in document_ids:
        try:
            doc = _api_get(cfg, f"/api/local-agent/documents/{document_id}", run_id=run_id, params={"run_id": run_id}).get("document") or {}
            if doc:
                docs.append(doc)
        except Exception as exc:
            stats["errors"] += 1
            _record_error(cfg, run_id, error=f"document read failed: {type(exc).__name__}: {exc}", document_id=document_id)
    return docs


def _document_prep_pending_reason(doc: dict[str, Any]) -> str | None:
    prep = doc.get("prep_status") if isinstance(doc.get("prep_status"), dict) else {}
    if not prep:
        return None
    media = prep.get("media") if isinstance(prep.get("media"), dict) else {}
    translation = prep.get("translation") if isinstance(prep.get("translation"), dict) else {}
    if int(media.get("pending_count") or 0) > 0:
        return f"pending_media:{int(media.get('pending_count') or 0)}"
    if (
        translation.get("needed") is True
        and translation.get("available") is not True
        and translation.get("inline_signal_extraction_fallback") is not True
    ):
        return "pending_translation"
    if prep.get("prep_complete") is not False:
        return None
    return "prep_incomplete"


def _compression_payload(cfg: WorkerConfig, run_id: int, document_id: int, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "document_id": document_id,
        "compression": {
            "model": cfg.model,
            "prompt_version": cfg.prompt_version,
            "summary_short": result.get("summary_short") or "",
            "summary_detail": result.get("summary_detail") or "",
            "atomic_claims": _as_list(result.get("atomic_claims")),
            "entity_refs": _as_list(result.get("entity_refs")),
            "supply_chain_refs": _as_list(result.get("supply_chain_refs")),
            "technology_refs": _as_list(result.get("technology_refs")),
            "summary_routes": _as_list(result.get("summary_routes")),
            "durable_routes": _as_list(result.get("durable_routes")),
            "entity_routing": _as_list(result.get("entity_routing")),
            "research_writes": _as_list(result.get("research_writes")),
            "decision_targets": _as_list(result.get("decision_targets")),
            "decision_review": result.get("decision_review"),
            "analyst_evidence": _as_list(result.get("analyst_evidence")),
            "catalysts": _as_list(result.get("catalysts")),
            "investment_memos": _as_list(result.get("investment_memos")),
            "evidence_links": _as_list(result.get("evidence_links")),
            "dataset_rows": _as_list(result.get("dataset_rows")),
            "dataset_materializations": _as_list(result.get("dataset_materializations")),
            "supply_chain_metric_bindings": _as_list(result.get("supply_chain_metric_bindings")),
            "instrument_request_fulfillments": _as_list(result.get("instrument_request_fulfillments")),
            "unresolved_entities": _as_list(result.get("unresolved_entities")),
            "information_gain": result.get("information_gain"),
            "document_disposition": result.get("document_disposition"),
            "information_gain_reason": result.get("information_gain_reason"),
            "skip_reason": result.get("skip_reason"),
            "status": result.get("_compression_status") or ("skipped" if _should_skip_side_effects(result) else "compressed"),
            "confidence": _confidence(result.get("confidence")),
            "error": result.get("error"),
        },
    }


def _claim_payloads_for_analysis_result(run_id: int, document_id: int, result: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for claim in _as_list(result.get("atomic_claims")):
        if not isinstance(claim, dict):
            continue
        claim_text = str(claim.get("claim_text") or "").strip()
        if not claim_text:
            continue
        entities = _dedupe_text_items(_as_list(claim.get("entities")))
        if not entities:
            entities = _dedupe_text_items([v.get("name") for v in _as_list(result.get("entity_refs")) if isinstance(v, dict)])
        for entity in entities or ["unresolved"]:
            rows.append(
                {
                    "run_id": run_id,
                    "document_id": document_id,
                    "entity_type": "unknown",
                    "entity_name": entity,
                    "claim_kind": str(claim.get("claim_kind") or "business_update"),
                    "claim_text": claim_text,
                    "confidence": _confidence(claim.get("confidence")),
                    "routing_reason": "local llama.cpp signal extraction",
                    "payload": dict(claim),
                }
            )
    return rows


def _unresolved_payloads_for_analysis_result(run_id: int, document_id: int, result: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in _as_list(result.get("unresolved_entities")):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        rows.append(
            {
                "run_id": run_id,
                "document_id": document_id,
                "name": name,
                "entity_type": item.get("entity_type"),
                "rationale": item.get("rationale"),
                "confidence": _confidence(item.get("confidence")),
                "candidate_payload": item,
            }
        )
    return rows


def _research_write_payloads_for_analysis_result(
    cfg: WorkerConfig,
    run_id: int,
    document_id: int,
    result: dict[str, Any],
    *,
    document: dict[str, Any] | None,
    stats: dict[str, Any],
) -> list[dict[str, Any]]:
    if not cfg.auto_write:
        return []
    rows: list[dict[str, Any]] = []
    for write in _as_list(result.get("research_writes")):
        if not isinstance(write, dict):
            continue
        subject = str(write.get("subject") or "").strip()
        append_md = str(write.get("append_md") or "").strip()
        confidence = _confidence(write.get("confidence"))
        materiality = str(write.get("materiality") or "").strip().lower()
        novelty = str(write.get("novelty") or "").strip().lower()
        temporal_posture = str(write.get("temporal_posture") or "").strip().lower()
        evidence_tier = _effective_research_write_evidence_tier(write, document)
        new_information = str(write.get("new_information") or "").strip()
        if not subject or not append_md:
            continue
        information_gain = str(write.get("information_gain") or result.get("information_gain") or "").strip().lower()
        stats["research_write_candidates"] = int(stats.get("research_write_candidates", 0) or 0) + 1
        skip_reason = _research_write_policy_skip_reason(
            confidence=confidence,
            materiality=materiality,
            novelty=novelty,
            temporal_posture=temporal_posture,
            evidence_tier=evidence_tier,
            information_gain=information_gain,
            new_information=new_information,
        )
        if skip_reason:
            stats["research_write_skipped_policy"] = int(stats.get("research_write_skipped_policy", 0) or 0) + 1
            continue
        rows.append(
            {
                "run_id": run_id,
                "destination": {"type": "research_report", "subject": subject},
                "append_md": append_md,
                "write_summary": str(write.get("write_summary") or f"Document {document_id} update"),
                "source_document_ids": write.get("source_document_ids") or [document_id],
                "confidence": confidence,
                "model": cfg.model,
                "materiality": materiality,
                "novelty": novelty,
                "temporal_posture": temporal_posture,
                "evidence_tier": evidence_tier,
                "information_gain": information_gain,
                "new_information": new_information,
                "routing": {
                    "materiality": materiality,
                    "novelty": novelty,
                    "temporal_posture": temporal_posture,
                    "evidence_tier": evidence_tier,
                    "information_gain": information_gain,
                    "new_information": new_information,
                    "rationale": write.get("rationale"),
                },
            }
        )
    return rows


def _empty_decision_payloads() -> dict[str, list[dict[str, Any]]]:
    return {field: [] for field in DECISION_BULK_PAYLOAD_FIELDS}


def _dict_rows(value: object) -> list[dict[str, Any]]:
    return [dict(row) for row in _as_list(value) if isinstance(row, dict)]


def _source_metadata(
    cfg: WorkerConfig,
    *,
    run_id: int,
    document_id: int,
    document: dict[str, Any],
) -> dict[str, Any]:
    doc_extra = document.get("extra") if isinstance(document.get("extra"), dict) else {}
    metadata = {
        "run_id": run_id,
        "source_document_id": document_id,
        "document_id": document_id,
        "model": cfg.model,
        "prompt_version": cfg.prompt_version,
        "source_url": document.get("url"),
        "source_name": document.get("source_name"),
        "source_category": document.get("source_category"),
        "author": document.get("author"),
        "language": document.get("language"),
        "handle": doc_extra.get("handle"),
        "connector": doc_extra.get("connector"),
    }
    return {key: value for key, value in metadata.items() if value not in (None, "", [])}


def _merge_payload_dict_defaults(payload: dict[str, Any], field: str, defaults: dict[str, Any]) -> None:
    existing = dict(payload.get(field) or {}) if isinstance(payload.get(field), dict) else {}
    for key, value in defaults.items():
        if value not in (None, "", []):
            existing.setdefault(key, value)
    if existing:
        payload[field] = existing


def _shape_dataset_rows_payload(
    cfg: WorkerConfig,
    *,
    run_id: int,
    document_id: int,
    document: dict[str, Any],
    item: dict[str, Any],
    decision_contexts: list[dict[str, Any]],
) -> dict[str, Any] | None:
    payload = _enrich_decision_payload(cfg, run_id=run_id, document_id=document_id, document=document, item=item, decision_contexts=decision_contexts)
    dataset_id = _int_or_none(payload.get("dataset_id") or payload.get("id"))
    if dataset_id is None or dataset_id <= 0:
        return None
    rows = _dict_rows(payload.get("rows"))
    if not rows and isinstance(payload.get("values"), dict):
        row: dict[str, Any] = {"values": dict(payload.get("values") or {})}
        row_key = _clean_text(payload.get("row_key"))
        if row_key:
            row["row_key"] = row_key
        sort_order = _int_or_none(payload.get("sort_order"))
        if sort_order is not None:
            row["sort_order"] = sort_order
        rows = [row]
    if not rows:
        return None
    payload["id"] = dataset_id
    payload["dataset_id"] = dataset_id
    payload["rows"] = rows
    _merge_payload_dict_defaults(payload, "provenance", _source_metadata(cfg, run_id=run_id, document_id=document_id, document=document))
    return payload


def _shape_dataset_materialization_payload(
    cfg: WorkerConfig,
    *,
    run_id: int,
    document_id: int,
    document: dict[str, Any],
    item: dict[str, Any],
    decision_contexts: list[dict[str, Any]],
) -> dict[str, Any] | None:
    payload = _enrich_decision_payload(cfg, run_id=run_id, document_id=document_id, document=document, item=item, decision_contexts=decision_contexts)
    family = _clean_text(payload.get("family") or payload.get("dataset_family"))
    if not family:
        return None
    payload["family"] = family
    if "rows" in payload:
        payload["rows"] = _dict_rows(payload.get("rows"))
    provenance = _source_metadata(cfg, run_id=run_id, document_id=document_id, document=document)
    rationale = _clean_text(payload.get("rationale"))
    if rationale:
        provenance.setdefault("provenance_summary", rationale)
    _merge_payload_dict_defaults(payload, "provenance", provenance)
    links = [
        link
        for link in _dict_rows(payload.get("links"))
        if _clean_text(link.get("object_type")).lower() != "document"
    ]
    if links:
        payload["links"] = links
    elif "links" in payload:
        payload.pop("links", None)
    return payload


def _shape_supply_chain_metric_binding_payload(
    cfg: WorkerConfig,
    *,
    run_id: int,
    document_id: int,
    document: dict[str, Any],
    item: dict[str, Any],
    decision_contexts: list[dict[str, Any]],
) -> dict[str, Any] | None:
    payload = _enrich_decision_payload(cfg, run_id=run_id, document_id=document_id, document=document, item=item, decision_contexts=decision_contexts)
    chain_id = _int_or_none(payload.get("supply_chain_id") or payload.get("chain_id") or payload.get("id"))
    dataset_id = _int_or_none(payload.get("dataset_id"))
    metric_kind = _clean_text(payload.get("metric_kind") or payload.get("metric_key") or payload.get("canonical_metric_key"))
    if chain_id is None or chain_id <= 0 or dataset_id is None or dataset_id <= 0 or not metric_kind:
        return None
    dataset_view_id = _int_or_none(payload.get("dataset_view_id"))
    payload["id"] = chain_id
    payload["supply_chain_id"] = chain_id
    payload["dataset_id"] = dataset_id
    if dataset_view_id is not None:
        payload["dataset_view_id"] = dataset_view_id
    payload["scope_type"] = _clean_text(payload.get("scope_type") or "chain")
    payload["scope_key"] = _clean_text(payload.get("scope_key") or "chain")
    payload["metric_kind"] = metric_kind
    _merge_payload_dict_defaults(payload, "extra", _source_metadata(cfg, run_id=run_id, document_id=document_id, document=document))
    return payload


def _shape_instrument_request_fulfillment_payload(
    cfg: WorkerConfig,
    *,
    run_id: int,
    document_id: int,
    document: dict[str, Any],
    item: dict[str, Any],
    decision_contexts: list[dict[str, Any]],
) -> dict[str, Any] | None:
    payload = _enrich_decision_payload(cfg, run_id=run_id, document_id=document_id, document=document, item=item, decision_contexts=decision_contexts)
    request_id = _int_or_none(payload.get("instrument_request_id") or payload.get("request_id") or payload.get("id"))
    fulfillment_summary = _clean_text(payload.get("fulfillment_summary") or payload.get("summary") or payload.get("review_note"))
    if request_id is None or request_id <= 0 or not fulfillment_summary:
        return None
    payload["id"] = request_id
    payload["instrument_request_id"] = request_id
    payload["fulfillment_summary"] = fulfillment_summary
    payload.setdefault("fulfilled_by", "local_agent_worker")
    evidence_bundle = _dict_rows(payload.get("evidence_bundle_patch"))
    if not evidence_bundle:
        evidence_bundle.append(
            {
                "document_id": int(document_id),
                "title": document.get("title") or f"Document {document_id}",
                "url": document.get("url"),
                "source": document.get("source_name"),
                "summary": fulfillment_summary,
            }
        )
    payload["evidence_bundle_patch"] = evidence_bundle
    return payload


def _shape_local_agent_durable_output_payload(
    cfg: WorkerConfig,
    *,
    field: str,
    run_id: int,
    document_id: int,
    document: dict[str, Any],
    item: dict[str, Any],
    decision_contexts: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not _confidence_allows_write(item, cfg):
        return None
    if field == "dataset_rows":
        return _shape_dataset_rows_payload(cfg, run_id=run_id, document_id=document_id, document=document, item=item, decision_contexts=decision_contexts)
    if field == "dataset_materializations":
        return _shape_dataset_materialization_payload(cfg, run_id=run_id, document_id=document_id, document=document, item=item, decision_contexts=decision_contexts)
    if field == "supply_chain_metric_bindings":
        return _shape_supply_chain_metric_binding_payload(cfg, run_id=run_id, document_id=document_id, document=document, item=item, decision_contexts=decision_contexts)
    if field == "instrument_request_fulfillments":
        return _shape_instrument_request_fulfillment_payload(cfg, run_id=run_id, document_id=document_id, document=document, item=item, decision_contexts=decision_contexts)
    return None


def _append_local_agent_durable_output_payloads(
    payloads: dict[str, list[dict[str, Any]]],
    cfg: WorkerConfig,
    *,
    field: str,
    run_id: int,
    document_id: int,
    document: dict[str, Any],
    result: dict[str, Any],
    decision_contexts: list[dict[str, Any]],
) -> None:
    for item in _as_list(result.get(field)):
        if not isinstance(item, dict):
            continue
        payload = _shape_local_agent_durable_output_payload(
            cfg,
            field=field,
            run_id=run_id,
            document_id=document_id,
            document=document,
            item=item,
            decision_contexts=decision_contexts,
        )
        if payload is not None:
            payloads[field].append(payload)


def _decision_payloads_for_analysis_result(
    cfg: WorkerConfig,
    run_id: int,
    document_id: int,
    document: dict[str, Any],
    result: dict[str, Any],
    decision_contexts: list[dict[str, Any]],
    stats: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    payloads = _empty_decision_payloads()
    if not cfg.auto_write:
        return payloads
    for item in _as_list(result.get("analyst_evidence")):
        if not isinstance(item, dict) or not _confidence_allows_write(item, cfg):
            continue
        duplicate_block = _durable_context_blocks_item(item, decision_contexts)
        if duplicate_block:
            stats["durable_duplicate_skipped"] = int(stats.get("durable_duplicate_skipped") or 0) + 1
            continue
        payload = _enrich_decision_payload(cfg, run_id=run_id, document_id=document_id, document=document, item=item, decision_contexts=decision_contexts)
        if _has_decision_identity(payload) and _clean_text(payload.get("summary")):
            if not _analyst_evidence_is_modelable(payload):
                stats["analyst_evidence_skipped_not_modelable"] = int(stats.get("analyst_evidence_skipped_not_modelable") or 0) + 1
                continue
            payloads["analyst_evidence"].append(payload)
    for item in _as_list(result.get("model_delta_candidates")):
        if not isinstance(item, dict):
            continue
        stats["model_delta_candidates"] = int(stats.get("model_delta_candidates") or 0) + 1
        if not _confidence_allows_write(item, cfg):
            stats["model_delta_skipped_low_confidence"] = int(stats.get("model_delta_skipped_low_confidence") or 0) + 1
            continue
        converted = _model_delta_candidate_to_analyst_evidence(item)
        if converted is None:
            stats["model_delta_skipped_missing_value"] = int(stats.get("model_delta_skipped_missing_value") or 0) + 1
            continue
        duplicate_block = _durable_context_blocks_item(item, decision_contexts) or _durable_context_blocks_item(converted, decision_contexts)
        if duplicate_block:
            stats["durable_duplicate_skipped"] = int(stats.get("durable_duplicate_skipped") or 0) + 1
            stats["model_delta_skipped_duplicate_context"] = int(stats.get("model_delta_skipped_duplicate_context") or 0) + 1
            continue
        payload = _enrich_decision_payload(cfg, run_id=run_id, document_id=document_id, document=document, item=converted, decision_contexts=decision_contexts)
        if not _has_decision_identity(payload) or not _clean_text(payload.get("summary")):
            stats["model_delta_skipped_missing_identity"] = int(stats.get("model_delta_skipped_missing_identity") or 0) + 1
            continue
        if not _analyst_evidence_is_modelable(payload):
            stats["model_delta_skipped_missing_value"] = int(stats.get("model_delta_skipped_missing_value") or 0) + 1
            continue
        payloads["analyst_evidence"].append(payload)
        stats["model_delta_evidence"] = int(stats.get("model_delta_evidence") or 0) + 1
    payloads["analyst_evidence"] = _dedupe_dicts(
        payloads["analyst_evidence"],
        (
            "ticker",
            "company_id",
            "issuer_id",
            "metric_key",
            "estimate_metric",
            "forecast.product_family",
            "product_family",
            "forecast.segment",
            "segment",
            "forecast.value",
            "forecast_value",
            "estimate_value",
            "forecast.value_text",
            "forecast_status",
            "unit",
            "estimate_unit",
            "summary",
            "source_document_id",
            "estimate_owner",
            "underlying_firm",
            "source_name",
        ),
    )
    for item in _as_list(result.get("catalysts")):
        if not isinstance(item, dict) or not _confidence_allows_write(item, cfg):
            continue
        payload = _enrich_decision_payload(cfg, run_id=run_id, document_id=document_id, document=document, item=item, decision_contexts=decision_contexts)
        if _has_decision_identity(payload) and _clean_text(payload.get("title")):
            payloads["catalysts"].append(payload)
    for item in _as_list(result.get("investment_memos")):
        if not isinstance(item, dict):
            continue
        stats["memo_candidates"] += 1
        if not _confidence_allows_write(item, cfg):
            stats["memo_skipped_low_confidence"] += 1
            continue
        payload = _enrich_decision_payload(cfg, run_id=run_id, document_id=document_id, document=document, item=item, decision_contexts=decision_contexts)
        if not _has_decision_identity(payload):
            stats["memo_skipped_missing_identity"] += 1
            continue
        if not _memo_has_content(payload):
            stats["memo_skipped_missing_content"] += 1
            continue
        duplicate_block = _durable_context_blocks_item(item, decision_contexts)
        if duplicate_block:
            stats["durable_duplicate_skipped"] = int(stats.get("durable_duplicate_skipped") or 0) + 1
            continue
        payloads["investment_memos"].append(payload)
    for item in _as_list(result.get("evidence_links")):
        if not isinstance(item, dict) or not _confidence_allows_write(item, cfg):
            continue
        payload = _enrich_decision_payload(cfg, run_id=run_id, document_id=document_id, document=document, item=item, decision_contexts=decision_contexts)
        payload.setdefault("evidence_object_type", "document")
        payload.setdefault("evidence_object_id", document_id)
        if _clean_text(payload.get("target_type")) and _int_or_none(payload.get("target_id")) is not None:
            payloads["evidence_links"].append(payload)
    for item in _as_list(result.get("research_requests")):
        if not isinstance(item, dict):
            continue
        payload = _enrich_decision_payload(cfg, run_id=run_id, document_id=document_id, document=document, item=item, decision_contexts=decision_contexts)
        if _clean_text(payload.get("title")) and _clean_text(payload.get("summary")):
            payloads["research_requests"].append(payload)
    for field in ("dataset_rows", "dataset_materializations", "supply_chain_metric_bindings", "instrument_request_fulfillments"):
        _append_local_agent_durable_output_payloads(
            payloads,
            cfg,
            field=field,
            run_id=run_id,
            document_id=document_id,
            document=document,
            result=result,
            decision_contexts=decision_contexts,
        )
    return payloads


def _submit_analysis_result_bulk(
    cfg: WorkerConfig,
    run_id: int,
    document_id: int,
    doc: dict[str, Any],
    result: dict[str, Any],
    decision_contexts: list[dict[str, Any]],
    stats: dict[str, Any],
) -> dict[str, Any] | None:
    skip_side_effects = _should_skip_side_effects(result)
    payload_stats = dict(stats)
    decision_payloads = (
        _empty_decision_payloads()
        if skip_side_effects
        else _decision_payloads_for_analysis_result(cfg, run_id, document_id, doc, result, decision_contexts, payload_stats)
    )
    payload = {
        **_compression_payload(cfg, run_id, document_id, result),
        "result": result,
        "claims": [] if skip_side_effects else _claim_payloads_for_analysis_result(run_id, document_id, result),
        "unresolved_entities": [] if skip_side_effects else _unresolved_payloads_for_analysis_result(run_id, document_id, result),
        "research_writes": []
        if skip_side_effects
        else _research_write_payloads_for_analysis_result(cfg, run_id, document_id, result, document=doc, stats=payload_stats),
        **decision_payloads,
    }
    try:
        data = _api_post(cfg, f"/api/local-agent/documents/{document_id}/analysis-result", payload, run_id=run_id)
        data["_worker_stat_deltas"] = {
            key: int(payload_stats.get(key) or 0) - int(stats.get(key) or 0)
            for key in (
                "research_write_candidates",
                "research_write_skipped_policy",
                "memo_candidates",
                "memo_skipped_low_confidence",
                "memo_skipped_missing_identity",
                "memo_skipped_missing_content",
                "analyst_evidence_skipped_not_modelable",
                "model_delta_candidates",
                "model_delta_evidence",
                "model_delta_skipped_low_confidence",
                "model_delta_skipped_missing_identity",
                "model_delta_skipped_missing_value",
                "model_delta_skipped_duplicate_context",
                "durable_duplicate_skipped",
                "research_requests",
            )
        }
        return data
    except Exception as exc:
        if "document_prep_incomplete" in str(exc):
            return {"_document_prep_deferred": True, "error": f"{type(exc).__name__}: {exc}"}
        _log_worker_event("analysis_result_bulk_fallback", document_id=document_id, error=f"{type(exc).__name__}: {exc}")
        return None


def _process_document_result(
    cfg: WorkerConfig,
    run_id: int,
    document_id: int,
    doc: dict[str, Any],
    result: dict[str, Any],
    stats: dict[str, Any],
    market_context_holder: dict[str, Any],
    *,
    allowed_document_ids: set[int] | None = None,
) -> None:
    allowed_ids = set(allowed_document_ids or {int(document_id)})
    result = _sanitize_result_source_ids(
        _dedupe_result(_defer_signal_extraction_side_effects(result, stats)),
        document_id=document_id,
        allowed_document_ids=allowed_ids,
    )
    decision_contexts: list[dict[str, Any]] = []
    if cfg.decision_spine_enabled and not _should_skip_side_effects(result):
        decision_contexts = _read_decision_contexts(cfg, run_id, document_id, doc, result, stats)
        if decision_contexts:
            durable_context = _read_durable_candidate_context(cfg, run_id, document_id, result, stats)
            if durable_context:
                decision_contexts[0]["durable_candidate_context"] = durable_context
            if market_context_holder.get("market_context") is None:
                market_context_holder["market_context"] = _read_market_context(cfg, run_id, stats)
            institutional_context = _read_institutional_model_context(cfg, run_id, document_id, doc, decision_contexts, stats)
            try:
                decision_kwargs = {
                    "document": doc,
                    "compression": result,
                    "decision_contexts": decision_contexts,
                    "market_context": market_context_holder.get("market_context"),
                    "institutional_model_context": institutional_context,
                }
                run_durable_progress = _compact_run_durable_progress(market_context_holder.get("durable_progress"))
                if run_durable_progress:
                    decision_kwargs["run_durable_progress"] = run_durable_progress
                decision = _llama_decision_json(
                    cfg,
                    **decision_kwargs,
                )
                _record_decision_review_action(
                    cfg,
                    run_id,
                    document_id,
                    decision,
                    decision_context_count=len(decision_contexts),
                    stats=stats,
                )
                result = _sanitize_result_source_ids(
                    _merge_decision_result(result, decision),
                    document_id=document_id,
                    allowed_document_ids=allowed_ids,
                )
            except Exception as exc:
                stats["errors"] += 1
                _record_error(cfg, run_id, error=f"decision review failed: {type(exc).__name__}: {exc}", document_id=document_id, action_kind="decision_review_error")
    bulk_result = _submit_analysis_result_bulk(cfg, run_id, document_id, doc, result, decision_contexts, stats)
    if bulk_result is not None and bulk_result.get("_document_prep_deferred"):
        failure_type = _record_document_processing_issue(stats, reason="analysis_write_barrier_race")
        _record_worker_action(
            cfg,
            run_id,
            summary="document preparation changed before write; analysis deferred without partial writes",
            document_id=document_id,
            action_kind="document_prep_write_deferred",
            status="blocked",
            response={"failure_type": failure_type},
            error=str(bulk_result.get("error") or "document_prep_incomplete"),
        )
        return
    if bulk_result is not None:
        counts = bulk_result.get("counts") if isinstance(bulk_result.get("counts"), dict) else {}
        _record_run_durable_progress(
            market_context_holder,
            document_id=document_id,
            result=result,
            counts=counts,
            stats=stats,
        )
        stat_deltas = bulk_result.get("_worker_stat_deltas") if isinstance(bulk_result.get("_worker_stat_deltas"), dict) else {}
        for key, value in stat_deltas.items():
            stats[key] = int(stats.get(key) or 0) + int(value or 0)
        stats["documents"] += 1
        stats["compressions"] += 1
        stats["claims"] += int(counts.get("claims_created") or 0)
        stats["unresolved"] += int(counts.get("unresolved_created") or 0)
        stats["writes"] += int(counts.get("research_writes") or 0)
        stats["research_requests"] += int(counts.get("research_requests") or 0)
        stats["research_write_errors"] += int(counts.get("research_write_errors") or 0)
        stats["analyst_evidence"] += int(counts.get("analyst_evidence") or 0)
        stats["catalysts"] += int(counts.get("catalysts") or 0)
        stats["memos"] += int(counts.get("investment_memos") or 0)
        stats["evidence_links"] += int(counts.get("evidence_links") or 0)
        stats["dataset_rows"] += int(counts.get("dataset_rows") or 0)
        stats["dataset_materializations"] += int(counts.get("dataset_materializations") or 0)
        stats["supply_chain_metric_bindings"] += int(counts.get("supply_chain_metric_bindings") or 0)
        stats["instrument_request_fulfillments"] += int(counts.get("instrument_request_fulfillments") or 0)
        stats["errors"] += int(counts.get("errors") or 0)
        return
    extraction_response = _api_post(
        cfg,
        "/api/local-agent/signal-extractions",
        _compression_payload(cfg, run_id, document_id, result),
        run_id=run_id,
    )
    compression = extraction_response.get("signal_extraction") or extraction_response.get("compression") or {}
    compression_id = int(compression.get("id"))
    stats["documents"] += 1
    stats["compressions"] += 1
    if _should_skip_side_effects(result):
        return
    stats["claims"] += _submit_claims(cfg, run_id, document_id, compression_id, result)
    stats["unresolved"] += _submit_unresolved(cfg, run_id, document_id, result)
    stats["writes"] += _auto_write_research(cfg, run_id, document_id, result, document=doc, stats=stats)
    _auto_write_decision_objects(cfg, run_id, document_id, doc, result, decision_contexts, stats)


def run_once(cfg: WorkerConfig) -> dict[str, Any]:
    stats = _initial_run_stats(cfg)
    try:
        run_id = _start_run(cfg, "feed_triage", initial_stats=stats)
        cfg = replace(cfg, harness_run_id=run_id)
    except Exception as exc:
        return _mark_run_start_error(stats, exc, run_type="feed_triage")
    try:
        if cfg.media_transcription_only:
            try:
                if str(cfg.media_vision_base_url or "").strip():
                    _probe_media_vision_endpoint(cfg, run_id, stats)
                stage_plan = _read_stage_plan(cfg, run_id, stats)
                media_quota = _stage_quota(stage_plan, "media_prep", cfg.media_transcription_limit if cfg.media_transcription_enabled else 0)
                _run_media_transcriptions(cfg, run_id, stats, limit_override=media_quota)
            except Exception as exc:
                stats["errors"] += 1
                _record_error(cfg, run_id, error=f"media transcription only loop failed: {type(exc).__name__}: {exc}", action_kind="media_transcription_loop_error")
            summary = f"media transcription only: completed {stats.get('media_jobs_completed') or 0} job(s), failed {stats.get('media_jobs_failed') or 0}"
            status = "error" if stats.get("errors") and not stats.get("media_jobs_completed") else "success"
            _finish_run(cfg, run_id, status=status, summary=summary, stats=stats)
            return stats

        stage_plan: dict[str, Any] = {}
        try:
            stage_plan = _read_stage_plan(cfg, run_id, stats)
            media_quota = _stage_quota(stage_plan, "media_prep", cfg.media_transcription_limit if cfg.media_transcription_enabled else 0)
            if cfg.media_transcription_enabled and str(cfg.media_vision_base_url or "").strip():
                _probe_media_vision_endpoint(cfg, run_id, stats)
            _run_media_transcriptions(cfg, run_id, stats, limit_override=media_quota)
        except Exception as exc:
            stats["errors"] += 1
            _record_error(cfg, run_id, error=f"media transcription loop failed: {type(exc).__name__}: {exc}", action_kind="media_transcription_loop_error")

        pending_media_work = int(stats.get("stage_plan_pending_media_work") or stats.get("stage_plan_pending_media") or 0)
        planned_signal_documents = int(stats.get("stage_plan_documents_planned") or 0)
        if pending_media_work > 0 and planned_signal_documents <= 0:
            summary = _feed_triage_finish_summary(stats, no_documents_ready=True)
            if cfg.media_transcription_enabled:
                summary = f"{summary}; text lane deferred until media prep drains"
            else:
                summary = f"{summary}; text lane blocked because media prep is pending"
            status = "error" if stats.get("media_jobs_failed") and not stats.get("media_jobs_completed") else "success"
            _finish_run(cfg, run_id, status=status, summary=summary, stats=stats)
            return stats

        try:
            llama_status = _check_llama_ready(cfg)
            stats["llama_ready"] = True
            stats["llama_models"] = _model_ids_from_status(llama_status)
            _remember_llama_context(cfg, llama_status, stats)
            if not cfg.media_transcription_enabled:
                _probe_media_vision_endpoint(cfg, run_id, stats)
        except Exception as exc:
            stats["errors"] += 1
            error = f"{type(exc).__name__}: {exc}"
            _record_error(cfg, run_id, error=error, action_kind="llama_health_error")
            if cfg.media_transcription_enabled and (
                int(stats.get("media_jobs_claimed") or 0) > 0
                or int(stats.get("media_jobs_completed") or 0) > 0
                or int(stats.get("media_jobs_failed") or 0) > 0
            ):
                summary = (
                    "media prep completed before text lane; "
                    f"completed {stats.get('media_jobs_completed') or 0} job(s), "
                    f"failed {stats.get('media_jobs_failed') or 0}; text endpoint unavailable"
                )
                status = "error" if stats.get("media_jobs_failed") and not stats.get("media_jobs_completed") else "success"
                _finish_run(cfg, run_id, status=status, summary=summary, stats=stats, error=error if status == "error" else None)
                return stats
            _finish_run(cfg, run_id, status="error", summary="llama.cpp unavailable; no documents opened", stats=stats, error=error)
            return stats

        signal_quota = _stage_quota(stage_plan, "signal_extraction", cfg.batch_size)
        batches = _fetch_document_batches(cfg, run_id, stats, limit_override=signal_quota, stage_plan=stage_plan)
        if not batches:
            summary = _feed_triage_finish_summary(stats, no_documents_ready=True)
            _finish_run(cfg, run_id, status="success", summary=summary, stats=stats)
            return stats
        market_context_holder: dict[str, Any] = {"market_context": None}
        for batch in batches:
            raw_ids = _as_list(batch.get("document_ids"))
            document_ids: list[int] = []
            for value in raw_ids:
                try:
                    document_ids.append(int(value))
                except Exception:
                    continue
            if not document_ids:
                continue
            docs = _fetch_documents_by_ids(cfg, run_id, document_ids, stats)
            if not docs:
                continue
            ready_docs: list[dict[str, Any]] = []
            for doc in docs:
                document_id = int(doc.get("id"))
                pending_prep_reason = _document_prep_pending_reason(doc)
                if pending_prep_reason:
                    failure_type = _record_document_processing_issue(stats, reason=pending_prep_reason)
                    _record_worker_action(
                        cfg,
                        run_id,
                        summary=f"document prep incomplete; deferring signal extraction: {failure_type}",
                        document_id=document_id,
                        action_kind="document_prep_deferred",
                        status="blocked",
                        response={"pending_prep_reason": pending_prep_reason, "failure_type": failure_type},
                        error=f"{failure_type}:{pending_prep_reason}",
                    )
                    continue
                ready_docs.append(doc)
            docs = ready_docs
            if not docs:
                continue
            allowed_ids = {int(doc.get("id")) for doc in docs if doc.get("id") is not None}
            use_packed = bool(
                cfg.true_batching
                and len(docs) > 1
                and str(batch.get("mode") or "").lower() == "packed"
                and not any(_document_requires_ordered_extraction(cfg, doc) for doc in docs)
            )
            batch_results: dict[int, dict[str, Any]] = {}
            if use_packed:
                try:
                    packed = _llama_batch_json(cfg, documents=docs, batch=batch)
                    batch_results = _batch_results_by_document(packed, allowed_document_ids=allowed_ids)
                    if len(batch_results) == len(docs):
                        stats["packed_batches"] += 1
                        stats["packed_documents"] += len(docs)
                    else:
                        stats["batch_model_fallbacks"] += 1
                        _record_error(
                            cfg,
                            run_id,
                            error=f"batch output covered {len(batch_results)} of {len(docs)} documents; missing documents fall back to single signal extraction",
                            action_kind="batch_partial_output",
                        )
                except AgentWorkerError as exc:
                    stats["batch_model_fallbacks"] += 1
                    if _is_text_oom_error(exc):
                        _record_worker_action(
                            cfg,
                            run_id,
                            action_kind="batch_signal_extraction_oom_fallback",
                            status="success",
                            summary="packed batch hit text-model VRAM pressure; falling back to per-document finer chunks",
                            response={"failure_type": _agent_worker_failure_type(exc, default="llama_text_oom")},
                        )
                    else:
                        stats["errors"] += 1
                        _record_error(cfg, run_id, error=f"batch signal extraction failed: {type(exc).__name__}: {exc}", action_kind="batch_signal_extraction_model_error")
            for doc in docs:
                document_id = int(doc.get("id"))
                result = batch_results.get(document_id)
                if result is None:
                    stats["single_documents"] += 1
                    try:
                        result = _llama_chat_json(cfg, document=doc)
                    except AgentWorkerError as exc:
                        stats["errors"] += 1
                        stats["model_fallbacks"] += 1
                        error = f"{type(exc).__name__}: {exc}"
                        _record_error(cfg, run_id, error=error, document_id=document_id, action_kind="signal_extraction_model_error")
                        result = _fallback_compression_result(doc, error=error)
                chunk_count = int(result.get("_chunk_count") or 1)
                if chunk_count > 1:
                    stats["chunked_documents"] = int(stats.get("chunked_documents") or 0) + 1
                    stats["document_chunks_processed"] = int(stats.get("document_chunks_processed") or 0) + chunk_count
                    if result.get("_chunk_reconciliation_fallback"):
                        stats["chunk_reconciliation_fallbacks"] = int(stats.get("chunk_reconciliation_fallbacks") or 0) + 1
                    stats["chunk_extraction_failures"] = int(stats.get("chunk_extraction_failures") or 0) + int(result.get("_chunk_extraction_failures") or 0)
                try:
                    _process_document_result(
                        cfg,
                        run_id,
                        document_id,
                        doc,
                        result,
                        stats,
                        market_context_holder,
                        allowed_document_ids=allowed_ids,
                    )
                except Exception as exc:
                    stats["errors"] += 1
                    _record_error(cfg, run_id, error=f"{type(exc).__name__}: {exc}", document_id=document_id)
        _finish_run(cfg, run_id, status="success", summary=_feed_triage_finish_summary(stats), stats=stats)
        return stats
    except Exception as exc:
        stats["errors"] += 1
        _record_error(cfg, run_id, error=f"{type(exc).__name__}: {exc}", action_kind="run_error")
        _finish_run(cfg, run_id, status="error", summary="feed triage failed", stats=stats, error=f"{type(exc).__name__}: {exc}")
        return stats


def _normal_intake_quiet(stats: dict[str, Any]) -> bool:
    if not _stats_ok(stats) or int(stats.get("errors") or 0) > 0:
        return False
    return bool(
        int(stats.get("documents") or 0) == 0
        and int(stats.get("media_jobs_claimed") or 0) == 0
        and int(stats.get("media_jobs_completed") or 0) == 0
        and int(stats.get("media_jobs_failed") or 0) == 0
        and int(stats.get("documents_deferred_pending_prep") or 0) == 0
    )


def _normal_intake_progress(stats: dict[str, Any]) -> bool:
    if not _stats_ok(stats) or int(stats.get("errors") or 0) > 0:
        return False
    return bool(
        int(stats.get("documents") or 0) > 0
        or int(stats.get("media_jobs_completed") or 0) > 0
    )


def _normal_intake_allows_durable_lane(stats: dict[str, Any]) -> bool:
    return bool(
        _stats_ok(stats)
        and int(stats.get("errors") or 0) == 0
        and bool(stats.get("llama_ready"))
        and int(stats.get("documents_deferred_pending_prep") or 0) == 0
    )


def _durable_lane_due_after_intake(
    cfg: WorkerConfig,
    stats: dict[str, Any],
    *,
    last_idle_deep_analysis_at: float,
    now_monotonic: float | None = None,
) -> bool:
    if not cfg.idle_deep_analysis_enabled or cfg.idle_deep_analysis_limit <= 0:
        return False
    if not _normal_intake_allows_durable_lane(stats):
        return False
    if not (_normal_intake_quiet(stats) or _normal_intake_progress(stats)):
        return False
    now_value = time.monotonic() if now_monotonic is None else float(now_monotonic)
    return (now_value - float(last_idle_deep_analysis_at or 0.0)) >= cfg.idle_deep_analysis_min_interval_s


def _stage_limited_deep_analysis_config(cfg: WorkerConfig, stats: dict[str, Any]) -> WorkerConfig:
    if not cfg.stage_planning_enabled:
        return cfg
    if int(stats.get("stage_plan_reads") or 0) <= 0 or int(stats.get("stage_plan_errors") or 0) > 0:
        return cfg
    try:
        quota = max(0, int(stats.get("stage_plan_durable_quota") or 0))
    except Exception:
        return cfg
    quota = min(quota, int(cfg.idle_deep_analysis_limit or 0))
    if quota == int(cfg.idle_deep_analysis_limit or 0):
        return cfg
    return replace(cfg, idle_deep_analysis_limit=quota)


def _initial_deep_analysis_stats(cfg: WorkerConfig) -> dict[str, Any]:
    return {
        "worker_loadout_version": WORKER_LOADOUT_VERSION,
        "loadout_preflight": False,
        "llama_ready": False,
        "llama_models": [],
        "llama_context_tokens": None,
        "llama_prompt_safe_tokens": None,
        "idle_deep_analysis_enabled": bool(cfg.idle_deep_analysis_enabled),
        "idle_deep_analysis_candidates": 0,
        "idle_deep_analysis_traces": 0,
        "idle_deep_analysis_errors": 0,
        "errors": 0,
    }


def run_idle_deep_analysis(cfg: WorkerConfig) -> dict[str, Any]:
    stats = _initial_deep_analysis_stats(cfg)
    if not cfg.idle_deep_analysis_enabled or cfg.idle_deep_analysis_limit <= 0:
        return stats
    try:
        run_id = _start_run(cfg, "idle_deep_analysis", initial_stats=stats)
        cfg = replace(cfg, harness_run_id=run_id)
    except Exception as exc:
        return _mark_run_start_error(stats, exc, run_type="idle_deep_analysis")
    try:
        llama_status = _check_llama_ready(cfg)
        stats["llama_ready"] = True
        stats["llama_models"] = [
            str(item.get("id") or item.get("object") or "").strip()
            for item in _as_list(llama_status.get("data"))
            if isinstance(item, dict) and str(item.get("id") or item.get("object") or "").strip()
        ][:10]
        _remember_llama_context(cfg, llama_status, stats)
        data = _api_get(
            cfg,
            "/api/local-agent/deep-analysis/next",
            run_id=run_id,
            params={
                "limit": cfg.idle_deep_analysis_limit,
                "hours": cfg.idle_deep_analysis_lookback_hours,
                "max_text_chars": cfg.idle_deep_analysis_max_text_chars,
                "run_id": run_id,
            },
        )
        candidates = [row for row in _as_list(data.get("candidates")) if isinstance(row, dict)]
        stats["idle_deep_analysis_candidates"] = len(candidates)
        if not candidates:
            _finish_run(cfg, run_id, status="success", summary="idle deep analysis: no candidates", stats=stats)
            return stats
        for candidate in candidates:
            compression = candidate.get("compression") if isinstance(candidate.get("compression"), dict) else {}
            compression_id = _int_or_none(compression.get("id"))
            document_id = _int_or_none(compression.get("document_id"))
            if compression_id is None:
                continue
            try:
                trace = _llama_deep_analysis_json(cfg, candidate=candidate)
                _api_post(
                    cfg,
                    "/api/local-agent/compression-traces",
                    {
                        "run_id": run_id,
                        "compression_id": compression_id,
                        "trace": trace,
                    },
                    run_id=run_id,
                )
                stats["idle_deep_analysis_traces"] += 1
            except Exception as exc:
                stats["errors"] += 1
                stats["idle_deep_analysis_errors"] += 1
                error = f"{type(exc).__name__}: {exc}"
                try:
                    _api_post(
                        cfg,
                        "/api/local-agent/compression-traces",
                        {
                            "run_id": run_id,
                            "compression_id": compression_id,
                            "trace": {
                                "analysis_mode": "idle_deep",
                                "status": "error",
                                "signal_level": "none",
                                "signal_score": 0.0,
                                "summary_md": "",
                                "error": error,
                                "model": cfg.model,
                                "prompt_version": cfg.prompt_version,
                            },
                        },
                        run_id=run_id,
                    )
                except Exception:
                    pass
                _record_error(cfg, run_id, error=error, document_id=document_id, action_kind="idle_deep_analysis_error")
        summary = f"idle deep analysis: {stats['idle_deep_analysis_traces']} trace(s)"
        _finish_run(cfg, run_id, status="success" if stats["errors"] == 0 else "error", summary=summary, stats=stats)
        return stats
    except Exception as exc:
        stats["errors"] += 1
        stats["idle_deep_analysis_errors"] += 1
        error = f"{type(exc).__name__}: {exc}"
        _record_error(cfg, run_id, error=error, action_kind="idle_deep_analysis_run_error")
        _finish_run(cfg, run_id, status="error", summary="idle deep analysis failed", stats=stats, error=error)
        return stats


def _durable_review_signal_from_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    signal = candidate.get("signal_extraction") if isinstance(candidate.get("signal_extraction"), dict) else {}
    if not signal:
        signal = candidate.get("compression") if isinstance(candidate.get("compression"), dict) else {}
    result = dict(signal or {})
    durable_routes = _as_list(candidate.get("durable_routes")) or _as_list(result.get("durable_routes"))
    if durable_routes:
        result["durable_routes"] = durable_routes
        result.setdefault("document_disposition", "durable_candidate")
    return result


def run_durable_review_only(cfg: WorkerConfig) -> dict[str, Any]:
    stats = _initial_run_stats(cfg)
    stats.update(
        {
            "durable_review_candidates": 0,
            "durable_review_processed": 0,
            "durable_review_errors": 0,
        }
    )
    try:
        run_id = _start_run(cfg, "durable_review", initial_stats=stats)
        cfg = replace(cfg, harness_run_id=run_id)
    except Exception as exc:
        return _mark_run_start_error(stats, exc, run_type="durable_review")
    try:
        llama_status = _check_llama_ready(cfg)
        stats["llama_ready"] = True
        stats["llama_models"] = _model_ids_from_status(llama_status)
        _remember_llama_context(cfg, llama_status, stats)
        params = {
            "limit": cfg.durable_review_limit,
            "hours": cfg.durable_review_lookback_hours,
            "max_text_chars": cfg.durable_review_max_text_chars,
            "run_id": run_id,
        }
        if cfg.target_document_ids:
            params["document_ids"] = ",".join(str(value) for value in cfg.target_document_ids)
        data = _api_get(
            cfg,
            "/api/local-agent/durable-review/next",
            run_id=run_id,
            params=params,
        )
        candidates = [row for row in _as_list(data.get("candidates")) if isinstance(row, dict)]
        stats["durable_review_candidates"] = len(candidates)
        if not candidates:
            _finish_run(cfg, run_id, status="success", summary="durable review: no extracted-signal candidates", stats=stats)
            return stats
        market_context_holder: dict[str, Any] = {"market_context": None}
        for candidate in candidates:
            doc = candidate.get("document") if isinstance(candidate.get("document"), dict) else {}
            result = _durable_review_signal_from_candidate(candidate)
            document_id = _int_or_none(doc.get("id") if isinstance(doc, dict) else None) or _int_or_none(result.get("document_id"))
            if document_id is None:
                stats["durable_review_errors"] += 1
                stats["errors"] += 1
                continue
            try:
                _process_document_result(
                    cfg,
                    run_id,
                    int(document_id),
                    doc,
                    result,
                    stats,
                    market_context_holder,
                    allowed_document_ids={int(document_id)},
                )
                stats["durable_review_processed"] += 1
            except Exception as exc:
                stats["errors"] += 1
                stats["durable_review_errors"] += 1
                _record_error(
                    cfg,
                    run_id,
                    error=f"durable review failed: {type(exc).__name__}: {exc}",
                    document_id=int(document_id),
                    action_kind="durable_review_error",
                )
        summary = f"durable review: processed {stats['durable_review_processed']} candidate(s)"
        _finish_run(cfg, run_id, status="success" if stats["errors"] == 0 else "error", summary=summary, stats=stats)
        return stats
    except Exception as exc:
        stats["errors"] += 1
        stats["durable_review_errors"] += 1
        error = f"{type(exc).__name__}: {exc}"
        _record_error(cfg, run_id, error=error, action_kind="durable_review_run_error")
        _finish_run(cfg, run_id, status="error", summary="durable review failed", stats=stats, error=error)
        return stats


def run_daily_summary(cfg: WorkerConfig, *, content_md: str) -> None:
    stats = {
        "summaries": 0,
        "errors": 0,
        "model_fallbacks": 0,
        "worker_loadout_version": WORKER_LOADOUT_VERSION,
        "loadout_preflight": False,
    }
    try:
        run_id = _start_run(cfg, "daily_summary", initial_stats=stats)
        cfg = replace(cfg, harness_run_id=run_id)
    except Exception as exc:
        _mark_run_start_error(stats, exc, run_type="daily_summary")
        return
    try:
        title = _agent_dispatch_default_title({})
        content_md = _normalize_dispatch_content_heading(content_md, title)
        _api_post(
            cfg,
            "/api/local-agent/daily-summaries",
            {
                "run_id": run_id,
                "summary_date": date.today().isoformat(),
                "summary_kind": "agent_dispatch",
                "title": title,
                "content_md": content_md,
            },
            run_id=run_id,
        )
        stats["summaries"] = 1
        _finish_run(cfg, run_id, status="success", summary="agent dispatch written", stats=stats)
    except Exception as exc:
        stats["errors"] += 1
        error = f"{type(exc).__name__}: {exc}"
        stats["daily_summary_error"] = error
        _record_error(cfg, run_id, error=error, action_kind="daily_summary_error")
        _finish_run(cfg, run_id, status="error", summary="agent dispatch failed", stats=stats, error=error)


def run_daily_summary_from_context(cfg: WorkerConfig) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "summaries": 0,
        "compressions": 0,
        "claims": 0,
        "writes": 0,
        "errors": 0,
        "model_fallbacks": 0,
        "worker_loadout_version": WORKER_LOADOUT_VERSION,
        "loadout_preflight": False,
    }
    try:
        run_id = _start_run(cfg, "daily_summary", initial_stats=stats)
        cfg = replace(cfg, harness_run_id=run_id)
    except Exception as exc:
        return _mark_run_start_error(stats, exc, run_type="daily_summary")
    try:
        context = _api_get(
            cfg,
            "/api/local-agent/daily-summary/context",
            run_id=run_id,
            params={
                "hours": cfg.daily_summary_lookback_hours,
                "limit": 250,
                "run_id": run_id,
            },
        ).get("context") or {}
        stats["compressions"] = len(_as_list(context.get("compressions")))
        stats["claims"] = len(_as_list(context.get("claims")))
        stats["writes"] = len(_as_list(context.get("writes")))
        if stats["compressions"] == 0 and stats["writes"] == 0:
            _finish_run(cfg, run_id, status="success", summary="idle: no agent dispatch context", stats=stats)
            return stats
        fallback_reason: str | None = None
        try:
            result = _llama_json(
                cfg,
                system_prompt=DAILY_SUMMARY_PROMPT,
                user_prompt={
                    "summary_date": date.today().isoformat(),
                    "lookback_hours": cfg.daily_summary_lookback_hours,
                    "context": context,
                },
            )
        except AgentWorkerError as exc:
            result = {}
            fallback_reason = f"{type(exc).__name__}: {exc}"
        content_md = str(result.get("content_md") or result.get("summary_md") or result.get("summary") or "").strip()
        if not content_md:
            fallback_reason = fallback_reason or "agent dispatch model returned empty content_md"
            content_md = _daily_summary_fallback_markdown(context, reason=fallback_reason)
            stats["model_fallbacks"] = 1
        source_document_ids = result.get("source_document_ids") or context.get("source_document_ids") or []
        compression_ids = result.get("compression_ids") or context.get("compression_ids") or []
        payload_meta = {"model": cfg.model, "prompt_version": cfg.prompt_version}
        if fallback_reason:
            payload_meta["fallback_reason"] = fallback_reason
        summary_kind = str(result.get("summary_kind") or "agent_dispatch").strip()[:64] or "agent_dispatch"
        title = _normalize_dispatch_title(result.get("title"), context)
        content_md = _normalize_dispatch_content_heading(content_md, title)
        _api_post(
            cfg,
            "/api/local-agent/daily-summaries",
            {
                "run_id": run_id,
                "summary_date": date.today().isoformat(),
                "summary_kind": summary_kind,
                "title": title,
                "content_md": content_md,
                "source_document_ids": source_document_ids,
                "compression_ids": compression_ids,
                "payload": payload_meta,
            },
            run_id=run_id,
        )
        stats["summaries"] = 1
        _finish_run(cfg, run_id, status="success", summary="agent dispatch written", stats=stats)
        return stats
    except Exception as exc:
        stats["errors"] += 1
        error = f"{type(exc).__name__}: {exc}"
        stats["daily_summary_error"] = error
        _record_error(cfg, run_id, error=error, action_kind="daily_summary_error")
        _finish_run(cfg, run_id, status="error", summary="agent dispatch failed", stats=stats, error=error)
        return stats


def _daily_summary_already_written(cfg: WorkerConfig, summary_key: str) -> bool:
    lookback_hours = max(30, min(max(int(cfg.daily_summary_lookback_hours), 24) + 12, 96))
    try:
        data = _api_get(
            cfg,
            "/api/local-agent/actions/recent",
            params={
                "hours": lookback_hours,
                "limit": 20,
                "action_kind": "daily_summary_write",
                "include_payloads": "true",
            },
        )
    except Exception as exc:
        _log_worker_event("daily_summary_existing_check_failed", error=f"{type(exc).__name__}: {exc}")
        return False
    for action in _as_list(data.get("actions")):
        if not isinstance(action, dict) or action.get("action_kind") != "daily_summary_write":
            continue
        response = action.get("response") if isinstance(action.get("response"), dict) else {}
        if str(response.get("summary_date") or "").strip() == summary_key:
            return True
        if summary_key and summary_key in str(action.get("summary") or ""):
            return True
    return False


def _daily_summary_due(cfg: WorkerConfig, last_summary_key: str | None) -> str | None:
    if not cfg.daily_summary_enabled:
        return None
    try:
        hour_text, minute_text = str(cfg.daily_summary_hhmm or "21:00").split(":", 1)
        target_hour = max(0, min(int(hour_text), 23))
        target_minute = max(0, min(int(minute_text), 59))
    except Exception:
        target_hour = 21
        target_minute = 0
    now = datetime.now()
    key = now.date().isoformat()
    if last_summary_key == key:
        return None
    if (now.hour, now.minute) >= (target_hour, target_minute):
        if _daily_summary_already_written(cfg, key):
            return None
        return key
    return None


def _stats_ok(stats: dict[str, Any]) -> bool:
    return not bool(stats.get("run_start_error") or stats.get("unexpected_error"))


def _daily_summary_stats_ok(stats: dict[str, Any]) -> bool:
    return _stats_ok(stats) and not bool(stats.get("daily_summary_error"))


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except Exception:
        return int(default)


def build_config(argv: list[str]) -> WorkerConfig:
    parser = argparse.ArgumentParser(description="Run the Thoth local analyst agent worker.")
    parser.add_argument("--thoth-base-url", required=True, help="Base URL for the Thoth local-agent API, e.g. http://DESKTOP:8765")
    parser.add_argument("--thoth-token", default=None, help="Bearer token matching THOTH_AGENT_API_TOKEN on the Thoth desktop.")
    parser.add_argument("--thoth-token-path", default=None, help="Path to a runtime file containing the Thoth local-agent bearer token.")
    parser.add_argument("--llama-base-url", default="http://127.0.0.1:8080", help="llama.cpp OpenAI-compatible base URL.")
    parser.add_argument("--model", default="qwen-3.5-9b", help="Served model name.")
    parser.add_argument("--worker-id", default=f"{socket.gethostname()}-local-agent", help="Worker id recorded in Thoth.")
    parser.add_argument("--prompt-version", default="local-analyst-v6-observed-claim-routes")
    parser.add_argument("--poll-interval-s", type=float, default=120.0)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--lookback-hours", type=int, default=None)
    parser.add_argument(
        "--queue-strategy",
        default=os.environ.get("THOTH_LOCAL_AGENT_QUEUE_STRATEGY", "balanced"),
        choices=["newest", "oldest", "balanced"],
        help="Document queue sampling strategy. balanced keeps a fresh head while advancing the oldest tail inside the lookback.",
    )
    parser.add_argument("--backlog-mode", action="store_true", help="Drain old intake intentionally: wider default lookback and include OPENED-but-uncompressed documents.")
    parser.add_argument("--backlog-lookback-hours", type=int, default=720, help="Lookback used by --backlog-mode when --lookback-hours is omitted.")
    parser.add_argument("--include-opened", action="store_true", help="Include OPENED documents that have not already been compressed.")
    parser.add_argument("--include-filings", action="store_true", help="Allow SEC filing documents into the queue.")
    parser.add_argument("--document-ids", default=os.environ.get("THOTH_LOCAL_AGENT_DOCUMENT_IDS"), help="Comma/space separated document IDs for a targeted, API-railed one-shot run.")
    parser.add_argument("--timeout-s", type=float, default=60.0)
    parser.add_argument("--max-input-chars", type=int, default=24000)
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    parser.add_argument("--daily-summary-hhmm", default="21:00", help="Local laptop time for one daily summary run.")
    parser.add_argument("--daily-summary-lookback-hours", type=int, default=24)
    parser.add_argument("--calendar-days-back", type=int, default=30)
    parser.add_argument("--calendar-days-forward", type=int, default=365)
    parser.add_argument("--calendar-limit", type=int, default=50)
    parser.add_argument("--market-base-currency", default=None)
    parser.add_argument("--market-live-quotes", action="store_true")
    parser.add_argument("--market-max-items", type=int, default=30)
    parser.add_argument("--decision-max-targets", type=int, default=3)
    parser.add_argument("--decision-min-confidence", type=float, default=0.7)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--no-auto-write", action="store_true", help="Disable direct research-report append writes.")
    parser.add_argument("--no-daily-summary", action="store_true", help="Disable the scheduled daily summary mode.")
    parser.add_argument("--no-decision-spine", action="store_true", help="Disable decision context/calendar/market reads and durable decision object writes.")
    parser.add_argument("--loadout-preflight", action="store_true", help="Start and finish a read-only worker preflight run without opening documents or calling llama.cpp.")
    parser.add_argument("--preflight-ticker", default="TSM", help="Ticker used for decision/calendar preflight reads.")
    parser.add_argument("--no-true-batching", action="store_true", help="Disable conservative packed model calls and use one model call per document.")
    parser.add_argument("--batch-max-input-tokens", type=int, default=8500, help="Approximate input-token budget for one packed document batch.")
    parser.add_argument("--batch-max-documents", type=int, default=6, help="Maximum documents in one packed model call.")
    parser.add_argument("--no-stage-planning", action="store_true", help="Disable the read-only staged work plan and use static media/document quotas.")
    parser.add_argument("--media-transcription-enabled", dest="media_transcription_enabled", action="store_true", default=True, help="Poll and complete protected local-agent media transcription jobs before document compression (enabled by default).")
    parser.add_argument("--no-media-transcription", dest="media_transcription_enabled", action="store_false", help="Disable the media transcription lane.")
    parser.add_argument("--media-transcription-only", action="store_true", help="Run only the media transcription/prep lane and skip llama health plus document signal extraction.")
    parser.add_argument("--media-transcription-limit", type=int, default=24, help="Maximum image jobs to claim per worker run.")
    parser.add_argument("--media-transcription-mode", default="route", choices=["route", "ocr-first", "ocr-only", "vlm-first", "vision-first", "vision-only"], help="Image transcription strategy.")
    render_readable_default = str(os.environ.get("THOTH_MEDIA_RENDER_READABLE_PDF_PAGES") or "1").strip().lower() not in {"0", "false", "no", "off"}
    parser.add_argument("--media-render-readable-pdf-pages", action="store_true", default=render_readable_default, help="Also render readable stored PDFs into page images for chart/table OCR, bounded by media queue caps.")
    parser.add_argument("--media-ocr-concurrency", type=int, default=_env_int("THOTH_MEDIA_OCR_CONCURRENCY", 1), help="Maximum concurrent OCR media requests. Keep at 1 for GGUF CLI unless explicitly testing.")
    parser.add_argument("--media-vlm-concurrency", type=int, default=_env_int("THOTH_MEDIA_VLM_CONCURRENCY", 1), help="Maximum concurrent VLM media requests.")
    parser.add_argument(
        "--media-sidecar-concurrency-auto",
        action="store_true",
        default=str(os.environ.get("THOTH_MEDIA_SIDECAR_CONCURRENCY_AUTO") or "1").strip().lower() not in {"0", "false", "no", "off"},
        help="Raise media request concurrency automatically only when OCR/VLM sidecar readiness is true.",
    )
    parser.add_argument("--no-media-sidecar-concurrency-auto", dest="media_sidecar_concurrency_auto", action="store_false", help="Disable readiness-gated media sidecar concurrency.")
    parser.add_argument(
        "--media-ocr-backend",
        default=os.environ.get("THOTH_MEDIA_OCR_BACKEND", "gguf_cli"),
        choices=MEDIA_OCR_BACKEND_CHOICES,
        help=(
            "GLM-OCR execution path. Canonical values are auto, gguf_cli, glm_sdk_sidecar, and vllm_ocr_sidecar. "
            "The legacy cli/sdk aliases remain accepted."
        ),
    )
    parser.add_argument("--media-ocr-config", default=os.environ.get("THOTH_MEDIA_OCR_CONFIG"), help="Optional glmocr SDK config.yaml path. API keys stay in env/config outside the repo.")
    parser.add_argument("--media-ocr-api-url", default=os.environ.get("THOTH_MEDIA_OCR_API_URL"), help="Optional glmocr SDK MaaS/self-hosted server URL.")
    parser.add_argument("--media-ocr-mode", default=os.environ.get("THOTH_MEDIA_OCR_MODE"), choices=["maas", "selfhosted"], help="Optional glmocr SDK mode override.")
    parser.add_argument("--media-ocr-layout-device", default=os.environ.get("THOTH_MEDIA_OCR_LAYOUT_DEVICE"), help="Optional glmocr SDK layout device, e.g. cpu or cuda:1.")
    parser.add_argument("--media-ocr-vllm-base-url", default=os.environ.get("THOTH_MEDIA_OCR_VLLM_BASE_URL"), help="Optional vLLM GLM-OCR sidecar base URL, e.g. http://127.0.0.1:8111.")
    parser.add_argument("--media-ocr-vllm-model", default=os.environ.get("THOTH_MEDIA_OCR_VLLM_MODEL"), help="Optional vLLM GLM-OCR served model name. Defaults to glm-ocr.")
    parser.add_argument("--media-ocr-cli", default=None, help="Path to a llama-mtmd-cli-compatible OCR CLI. Defaults to PATH/common worker-host install paths.")
    parser.add_argument("--media-ocr-model", default="ggml-org/GLM-OCR-GGUF:Q8_0", help="HF repo/quant or model id used by the OCR CLI.")
    parser.add_argument("--media-ocr-min-chars", type=int, default=180, help="Escalate OCR output below this text length to the vision model when fallback is allowed.")
    parser.add_argument("--media-ocr-timeout-s", type=float, default=240.0)
    parser.add_argument("--media-vision-base-url", default=os.environ.get("THOTH_MEDIA_VISION_BASE_URL"), help="OpenAI-compatible VLM endpoint. Required for VLM media prep unless --media-vision-allow-text-endpoint is explicitly set.")
    parser.add_argument("--media-vision-model", default=os.environ.get("THOTH_MEDIA_VISION_MODEL"), help="VLM model label. Defaults to --model.")
    parser.add_argument("--media-vision-timeout-s", type=float, default=360.0)
    parser.add_argument(
        "--llama-disable-thinking",
        dest="llama_disable_thinking",
        action="store_true",
        default=str(os.environ.get("THOTH_LLAMA_DISABLE_THINKING") or "1").strip().lower() not in {"0", "false", "no", "off"},
        help="Send thinking-off hints (chat_template_kwargs.enable_thinking=false, reasoning_effort=none) on text completions. Honored by FreeToken; ignored by llama.cpp servers.",
    )
    parser.add_argument(
        "--llama-keep-thinking",
        dest="llama_disable_thinking",
        action="store_false",
        help="Do not send thinking-off hints on text completions.",
    )
    parser.add_argument(
        "--media-vision-disable-thinking",
        action="store_true",
        default=str(os.environ.get("THOTH_MEDIA_VISION_DISABLE_THINKING") or "").strip().lower() in {"1", "true", "yes", "on"},
        help="Ask Qwen-style VLM endpoints to disable thinking text when supported.",
    )
    parser.add_argument(
        "--media-vision-allow-text-endpoint",
        action="store_true",
        default=str(os.environ.get("THOTH_MEDIA_VISION_ALLOW_TEXT_ENDPOINT") or "").strip().lower() in {"1", "true", "yes", "on"},
        help="Explicit diagnostic escape hatch allowing image payloads to use --llama-base-url when no split VLM endpoint is configured.",
    )
    parser.add_argument("--media-max-output-tokens", type=int, default=1800)
    parser.add_argument("--idle-deep-analysis-enabled", dest="idle_deep_analysis_enabled", action="store_true", default=False, help="Run a deep-analysis trace lane over recent compressions when fresh intake is quiet.")
    parser.add_argument("--no-idle-deep-analysis", dest="idle_deep_analysis_enabled", action="store_false", help="Disable the idle deep-analysis trace lane.")
    parser.add_argument("--idle-deep-analysis-limit", type=int, default=1, help="Maximum compression traces to generate per idle cycle.")
    parser.add_argument("--idle-deep-analysis-lookback-hours", type=int, default=72)
    parser.add_argument("--idle-deep-analysis-min-interval-s", type=float, default=900.0, help="Minimum seconds between idle deep-analysis runs.")
    parser.add_argument("--idle-deep-analysis-max-text-chars", type=int, default=12000)
    parser.add_argument("--durable-review-only", action="store_true", help="Run only the text-only durable-writing pass over stored signal extractions.")
    parser.add_argument("--durable-review-limit", type=int, default=2, help="Maximum stored signal extractions to review for durable writes.")
    parser.add_argument("--durable-review-lookback-hours", type=int, default=72)
    parser.add_argument("--durable-review-max-text-chars", type=int, default=12000)
    args = parser.parse_args(argv)
    thoth_token = str(args.thoth_token or os.environ.get("THOTH_TOKEN") or "").strip()
    if not thoth_token:
        try:
            thoth_token = _read_token_from_path(args.thoth_token_path or os.environ.get("THOTH_TOKEN_PATH"))
        except Exception as exc:
            parser.error(str(exc))
    if not thoth_token:
        parser.error("Missing Thoth token. Pass --thoth-token, set THOTH_TOKEN, or pass --thoth-token-path.")
    backlog_mode = bool(args.backlog_mode)
    raw_lookback = args.lookback_hours if args.lookback_hours is not None else (args.backlog_lookback_hours if backlog_mode else 48)
    return WorkerConfig(
        thoth_base_url=args.thoth_base_url,
        thoth_token=thoth_token,
        llama_base_url=args.llama_base_url,
        model=args.model,
        worker_id=args.worker_id,
        prompt_version=args.prompt_version,
        poll_interval_s=max(5.0, float(args.poll_interval_s)),
        batch_size=max(1, min(int(args.batch_size), 25)),
        lookback_hours=max(1, min(int(raw_lookback), 24 * 365)),
        queue_strategy=str(args.queue_strategy or "balanced").strip().lower(),
        include_opened=bool(args.include_opened or backlog_mode),
        backlog_mode=backlog_mode,
        include_filings=bool(args.include_filings),
        target_document_ids=_parse_document_ids(args.document_ids),
        auto_write=not bool(args.no_auto_write),
        once=bool(args.once),
        timeout_s=max(5.0, float(args.timeout_s)),
        max_input_chars=max(1000, int(args.max_input_chars)),
        max_output_tokens=max(256, min(int(args.max_output_tokens), 8192)),
        daily_summary_enabled=not bool(args.no_daily_summary),
        daily_summary_hhmm=str(args.daily_summary_hhmm or "21:00"),
        daily_summary_lookback_hours=max(1, min(int(args.daily_summary_lookback_hours), 168)),
        decision_spine_enabled=not bool(args.no_decision_spine),
        decision_max_targets=max(1, min(int(args.decision_max_targets), 8)),
        decision_min_confidence=max(0.0, min(float(args.decision_min_confidence), 1.0)),
        market_base_currency=str(args.market_base_currency or "").strip().upper() or None,
        market_live_quotes=bool(args.market_live_quotes),
        market_max_items=max(1, min(int(args.market_max_items), 100)),
        calendar_days_back=max(1, min(int(args.calendar_days_back), 3650)),
        calendar_days_forward=max(1, min(int(args.calendar_days_forward), 3650)),
        calendar_limit=max(1, min(int(args.calendar_limit), 500)),
        loadout_preflight=bool(args.loadout_preflight),
        preflight_ticker=str(args.preflight_ticker or "TSM").strip().upper() or "TSM",
        true_batching=not bool(args.no_true_batching),
        batch_max_input_tokens=max(2_000, min(int(args.batch_max_input_tokens), 64_000)),
        batch_max_documents=max(1, min(int(args.batch_max_documents), 12)),
        stage_planning_enabled=not bool(args.no_stage_planning),
        media_transcription_enabled=bool(args.media_transcription_enabled),
        media_transcription_only=bool(args.media_transcription_only),
        media_transcription_limit=max(0, min(int(args.media_transcription_limit), 64)),
        media_transcription_mode=str(args.media_transcription_mode or "route").strip().lower(),
        media_render_readable_pdf_pages=bool(args.media_render_readable_pdf_pages),
        media_ocr_concurrency=max(1, min(int(args.media_ocr_concurrency), 8)),
        media_vlm_concurrency=max(1, min(int(args.media_vlm_concurrency), 8)),
        media_sidecar_concurrency_auto=bool(args.media_sidecar_concurrency_auto),
        media_ocr_backend=_normalize_media_ocr_backend(args.media_ocr_backend),
        media_ocr_config=str(args.media_ocr_config or "").strip() or None,
        media_ocr_api_url=str(args.media_ocr_api_url or "").strip() or None,
        media_ocr_mode=str(args.media_ocr_mode or "").strip().lower() or None,
        media_ocr_layout_device=str(args.media_ocr_layout_device or "").strip() or None,
        media_ocr_vllm_base_url=str(args.media_ocr_vllm_base_url or "").strip() or None,
        media_ocr_vllm_model=str(args.media_ocr_vllm_model or "").strip() or None,
        media_ocr_cli=str(args.media_ocr_cli or "").strip() or None,
        media_ocr_model=str(args.media_ocr_model or "ggml-org/GLM-OCR-GGUF:Q8_0").strip(),
        media_ocr_min_chars=max(40, min(int(args.media_ocr_min_chars), 2000)),
        media_ocr_timeout_s=max(30.0, float(args.media_ocr_timeout_s)),
        media_vision_base_url=str(args.media_vision_base_url or "").strip() or None,
        media_vision_model=str(args.media_vision_model or "").strip() or None,
        media_vision_timeout_s=max(30.0, float(args.media_vision_timeout_s)),
        media_vision_disable_thinking=bool(args.media_vision_disable_thinking),
        llama_disable_thinking=bool(args.llama_disable_thinking),
        media_vision_allow_text_endpoint=bool(args.media_vision_allow_text_endpoint),
        media_max_output_tokens=max(128, min(int(args.media_max_output_tokens), 4096)),
        idle_deep_analysis_enabled=bool(args.idle_deep_analysis_enabled),
        idle_deep_analysis_limit=max(0, min(int(args.idle_deep_analysis_limit), 8)),
        idle_deep_analysis_lookback_hours=max(1, min(int(args.idle_deep_analysis_lookback_hours), 720)),
        idle_deep_analysis_min_interval_s=max(60.0, float(args.idle_deep_analysis_min_interval_s)),
        idle_deep_analysis_max_text_chars=max(1000, min(int(args.idle_deep_analysis_max_text_chars), 64000)),
        durable_review_only=bool(args.durable_review_only),
        durable_review_limit=max(0, min(int(args.durable_review_limit), 12)),
        durable_review_lookback_hours=max(1, min(int(args.durable_review_lookback_hours), 720)),
        durable_review_max_text_chars=max(1000, min(int(args.durable_review_max_text_chars), 64000)),
    )


def main(argv: list[str] | None = None) -> int:
    cfg = build_config(list(argv if argv is not None else sys.argv[1:]))
    if cfg.loadout_preflight:
        try:
            stats = run_loadout_preflight(cfg)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            stats = {"errors": 1, "unexpected_error": error, "worker_loadout_version": WORKER_LOADOUT_VERSION}
            _log_worker_event("preflight_unexpected_error", error=error)
        print(json.dumps({"ok": stats.get("errors", 0) == 0 and _stats_ok(stats), "preflight": stats, "error": stats.get("run_start_error") or stats.get("unexpected_error")}, ensure_ascii=False), flush=True)
        return 0 if stats.get("errors", 0) == 0 else 1
    if cfg.durable_review_only:
        try:
            stats = run_durable_review_only(cfg)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            stats = {"errors": 1, "unexpected_error": error, "worker_loadout_version": WORKER_LOADOUT_VERSION}
            _log_worker_event("durable_review_unexpected_error", error=error)
        print(json.dumps({"ok": _stats_ok(stats), "durable_review": stats, "error": stats.get("run_start_error") or stats.get("unexpected_error")}, ensure_ascii=False), flush=True)
        return 0 if _stats_ok(stats) else 1
    last_daily_summary_key: str | None = None
    last_idle_deep_analysis_at = 0.0
    while True:
        try:
            stats = run_once(cfg)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            stats = {"errors": 1, "unexpected_error": error, "worker_loadout_version": WORKER_LOADOUT_VERSION}
            _log_worker_event("run_once_unexpected_error", error=error)
        print(json.dumps({"ok": _stats_ok(stats), "stats": stats, "error": stats.get("run_start_error") or stats.get("unexpected_error")}, ensure_ascii=False), flush=True)
        if cfg.once:
            return 0 if _stats_ok(stats) else 1
        if stats.get("local_ingestion_enabled") is False:
            time.sleep(cfg.poll_interval_s)
            continue
        if not cfg.once:
            due_key = _daily_summary_due(cfg, last_daily_summary_key)
            if due_key:
                try:
                    daily_stats = run_daily_summary_from_context(cfg)
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    daily_stats = {"errors": 1, "unexpected_error": error, "worker_loadout_version": WORKER_LOADOUT_VERSION}
                    _log_worker_event("daily_summary_unexpected_error", error=error)
                last_daily_summary_key = due_key
                print(
                    json.dumps(
                        {
                            "ok": _daily_summary_stats_ok(daily_stats),
                            "daily_summary": daily_stats,
                            "error": daily_stats.get("run_start_error") or daily_stats.get("unexpected_error") or daily_stats.get("daily_summary_error"),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
        intake_progress = _normal_intake_progress(stats)
        ran_deep_analysis = False
        if _durable_lane_due_after_intake(cfg, stats, last_idle_deep_analysis_at=last_idle_deep_analysis_at):
            deep_cfg = _stage_limited_deep_analysis_config(cfg, stats)
            if deep_cfg.idle_deep_analysis_limit <= 0:
                time.sleep(cfg.poll_interval_s)
                continue
            try:
                deep_stats = run_idle_deep_analysis(deep_cfg)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                deep_stats = {"errors": 1, "unexpected_error": error, "worker_loadout_version": WORKER_LOADOUT_VERSION}
                _log_worker_event("idle_deep_analysis_unexpected_error", error=error)
            last_idle_deep_analysis_at = time.monotonic()
            print(
                json.dumps(
                    {
                        "ok": _stats_ok(deep_stats),
                        "idle_deep_analysis": deep_stats,
                        "error": deep_stats.get("run_start_error") or deep_stats.get("unexpected_error"),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            ran_deep_analysis = True
        if intake_progress and not ran_deep_analysis:
            continue
        time.sleep(cfg.poll_interval_s)


if __name__ == "__main__":
    raise SystemExit(main())
