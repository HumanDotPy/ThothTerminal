from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
import hashlib
import io
import json
from pathlib import Path
import re
import tempfile
from typing import Any

from sqlalchemy import create_engine, func, or_
from sqlalchemy.orm import Session, joinedload, load_only, sessionmaker

from thoth_terminal.agent_actions import upsert_agent_action
from thoth_terminal.config import settings
from thoth_terminal.db import utcnow
from thoth_terminal.local_agent_service import (
    _image_ocr_row_signal,
    get_document_for_agent,
    serialize_route,
    serialize_write_diff,
)
try:
    from thoth_terminal.media_transcription_routing import classify_ocr_media_output
except Exception:  # pragma: no cover - eval can still run without optional media helper
    classify_ocr_media_output = None  # type: ignore[assignment]
from thoth_terminal.models import (
    AgentAction,
    AnalystEvidence,
    Attachment,
    Base,
    Company,
    Dataset,
    DatasetColumn,
    DatasetRow,
    DatasetVersion,
    Document,
    InstitutionalEvidenceItem,
    InstitutionalObservation,
    Job,
    LocalAgentDocumentCompression,
    LocalAgentEntityClaim,
    LocalAgentEntityRoute,
    LocalAgentEvalCase,
    LocalAgentEvalRun,
    LocalAgentReviewDecision,
    LocalAgentRun,
    LocalAgentUnresolvedEntity,
    LocalAgentWriteDiff,
    ResearchReport,
    ResearchReportVersion,
    Source,
    SourceActor,
)


REVIEW_DECISIONS = {"approve", "hold", "demote", "quarantine", "research_request", "noop"}
EVAL_PASS_THRESHOLD = 0.8


DEFAULT_EVAL_CASES: list[dict[str, Any]] = [
    {
        "slug": "generic-claude-excel-no-info",
        "title": "Generic Claude in Excel praise",
        "source_label": "x_list_analysts",
        "fixture_text": "Claude in Excel is another level.",
        "description": "Generic social praise with no entity, metric, or timing delta.",
        "tags": ["social-noise", "no-information-gain"],
        "expected": {
            "document_disposition": ["skip", "noop"],
            "information_gain": "none",
            "allow_research_writes": False,
            "requires_no_claims": True,
        },
    },
    {
        "slug": "kioxia-market-cap-chatter",
        "title": "Kioxia market-cap ranking chatter",
        "source_label": "x_list_analysts",
        "fixture_text": (
            "It'll naturally reach 2nd place, right? It might even take 1st place in a year. "
            "There are probably going to be a lot of strong counterarguments, though."
        ),
        "description": "Market-cap ranking chatter should not become durable research memory.",
        "tags": ["social-noise", "market-cap-chatter"],
        "expected": {
            "document_disposition": ["skip", "summary_only"],
            "information_gain": ["none", "low"],
            "allow_research_writes": False,
        },
    },
    {
        "slug": "tsmc-high-na-euv-rumor-correction",
        "title": "TSMC High-NA EUV rumor correction",
        "source_label": "analyst_channel",
        "fixture_text": (
            "It is important to clarify that TSMC has not completely avoided purchasing or "
            "abandoned High-NA EUV. TSMC has already obtained a small number of High-NA EUV "
            "tools mainly for R&D, process learning, and early ecosystem preparation. The real "
            "issue is when TSMC will scale High-NA EUV into high-volume manufacturing."
        ),
        "description": "Contradiction candidate: useful, but should search prior memory before writing.",
        "tags": ["contradiction", "lithography", "analyst-commentary"],
        "expected": {
            "document_disposition": ["hold_for_corroboration", "summary_only"],
            "information_gain": ["medium", "high"],
            "required_summary_entities": ["TSMC", "High-NA EUV"],
            "required_durable_actions": [{"subject": "TSMC", "action": "hold"}],
            "allow_research_writes": False,
        },
    },
    {
        "slug": "jeff-pu-copos-3d-packaging",
        "title": "Jeff Pu CoPoS versus 3D packaging",
        "source_label": "analyst_channel",
        "fixture_text": (
            "CoPoS won't come by end-2028, consistent with TSMC comments. This is likely due "
            "to equipment supply chain readiness. Major AI GPU, TPU, and ASICs are expected "
            "to move to 3D in 2028. Equipment beneficiaries: inspection, CMP, and hybrid bonding."
        ),
        "description": "Forward-looking packaging timing and beneficiary map.",
        "tags": ["advanced-packaging", "analyst-commentary"],
        "expected": {
            "document_disposition": ["hold_for_corroboration", "durable_candidate"],
            "information_gain": ["medium", "high"],
            "required_summary_entities": ["CoPoS", "3D packaging", "TSMC"],
            "allow_research_writes": False,
        },
    },
    {
        "slug": "brokerage-winway-metrics",
        "title": "WinWay brokerage metrics",
        "source_label": "analyst_channel",
        "fixture_text": (
            "US brokerage believes WinWay 1Q gross margin expansion was driven by MEMS probe "
            "card cost structure and new procurement contracts. 2Q revenue is expected to grow "
            "22% QoQ with gross margin rising to 43.7%. The company is at the core of the AI/HPC "
            "cycle, with EPS estimates raised to 103.7, 224, and 426 for 2026-2028."
        ),
        "description": "Concrete analyst metrics should be promoted to summary and held for object work.",
        "tags": ["brokerage-metrics", "high-information-gain"],
        "expected": {
            "document_disposition": ["hold_for_corroboration", "durable_candidate"],
            "information_gain": "high",
            "required_summary_entities": ["WinWay"],
            "allow_research_writes": False,
        },
    },
    {
        "slug": "nvidia-hyperscaler-capex-model-delta-durable-output",
        "title": "NVIDIA hyperscaler capex durable model-delta",
        "source_label": "analyst_channel",
        "fixture_text": (
            "NVIDIA CFO Colette Kress says hyperscaler AI infrastructure investment may "
            "reach $3 trillion to $4 trillion per year by 2030, with Microsoft, Google, "
            "Amazon, and Meta investing about $700 billion this year."
        ),
        "description": "Positive durable-output fixture: model-delta signal should validate as analyst evidence and institutional observation.",
        "tags": ["durable-output", "model-delta", "analyst-evidence", "benchmark"],
        "expected": {
            "durable_output_fixture": {
                "company": {
                    "name": "NVIDIA Eval Durable Output",
                    "ticker": "NVDX",
                    "exchange": "NASDAQ",
                    "sector": "semis",
                    "industry": "accelerators",
                },
                "document": {
                    "title": "NVIDIA CFO hyperscaler capex signal",
                    "source_name": "analyst_channel",
                    "source_category": "analyst",
                    "url": "https://example.com/eval/nvidia-hyperscaler-capex",
                    "author": "NVIDIA CFO Colette Kress",
                    "language": "en",
                },
                "analysis_result": {
                    "model_delta_candidates": [
                        {
                            "evidence_key": "eval:model-delta:nvidia-hyperscaler-capex",
                            "raw_subject_text": "NVIDIA data center demand",
                            "canonical_metric_key": "demand_signal",
                            "metric_value_type": "status",
                            "value_text": "Hyperscaler AI infrastructure investment may reach $3T-$4T per year by 2030.",
                            "period": "2030",
                            "summary": "NVIDIA says hyperscaler AI infrastructure investment may reach $3T-$4T per year by 2030.",
                            "confidence": 0.86,
                            "evidence_tier": "analyst_commentary",
                            "source_kind": "analyst",
                        }
                    ]
                },
            },
            "required_model_delta_candidates_count": 1,
            "required_model_delta_evidence_count": 1,
            "required_analyst_evidence_count": 1,
            "required_institutional_observation_count": 1,
            "required_durable_context_reads": 1,
            "required_matched_identity_count": 1,
            "required_candidate_metric_count": 1,
            "required_destination_document_count": 1,
            "required_research_mode_context_count": 1,
            "required_metric_keys": ["demand_signal"],
            "required_durable_output_terms": ["$3T-$4T", "2030", "hyperscaler AI infrastructure"],
            "allow_research_writes": False,
        },
    },
    {
        "slug": "nvidia-hyperscaler-capex-duplicate-durable-context",
        "title": "NVIDIA hyperscaler capex duplicate durable context",
        "source_label": "analyst_channel",
        "fixture_text": "The same NVIDIA hyperscaler AI infrastructure investment signal is already present in durable evidence.",
        "description": "Duplicate durable-output fixture: context lookup should detect existing analyst evidence and hold materialization.",
        "tags": ["durable-output", "duplicate", "analyst-evidence", "benchmark"],
        "expected": {
            "durable_output_fixture": {
                "materialize_outputs": False,
                "company": {
                    "name": "NVIDIA Eval Duplicate Context",
                    "ticker": "NVDY",
                    "exchange": "NASDAQ",
                    "sector": "semis",
                    "industry": "accelerators",
                },
                "document": {
                    "title": "NVIDIA duplicate capex signal",
                    "source_name": "analyst_channel",
                    "source_category": "analyst",
                    "url": "https://example.com/eval/nvidia-duplicate-capex",
                    "author": "NVIDIA CFO Colette Kress",
                    "language": "en",
                },
                "existing_analyst_evidence": [
                    {
                        "evidence_key": "eval:existing:nvidia-hyperscaler-capex",
                        "evidence_kind": "estimate",
                        "source_name": "NVIDIA CFO Colette Kress",
                        "summary": "Existing durable evidence already says hyperscaler AI infrastructure investment may reach $3T-$4T per year by 2030.",
                        "metric_key": "demand_signal",
                        "estimate_metric": "demand_signal",
                        "estimate_period": "2030",
                        "forecast_status": "Hyperscaler AI infrastructure investment may reach $3T-$4T per year by 2030.",
                        "confidence": 0.9,
                        "evidence_tier": "analyst_commentary",
                        "same_source_document": True,
                    }
                ],
                "existing_institutional_evidence": [
                    {
                        "evidence_key": "eval:institutional:nvidia-hyperscaler-capex",
                        "title": "Existing institutional NVIDIA hyperscaler capex signal",
                        "evidence_kind": "analyst_context",
                        "provenance_summary": "Existing institutional evidence already captured the NVIDIA hyperscaler AI infrastructure demand range for 2030.",
                        "metric_key": "demand_signal",
                        "period": "2030",
                        "value_text": "Hyperscaler AI infrastructure investment may reach $3T-$4T per year by 2030.",
                        "summary": "Existing institutional evidence already covers the same $3T-$4T hyperscaler AI infrastructure signal.",
                        "confidence": 0.9,
                        "same_source_document": True,
                    }
                ],
                "analysis_result": {
                    "model_delta_candidates": [
                        {
                            "evidence_key": "eval:model-delta:nvidia-duplicate-capex",
                            "raw_subject_text": "NVIDIA data center demand",
                            "canonical_metric_key": "demand_signal",
                            "metric_value_type": "status",
                            "value_text": "Hyperscaler AI infrastructure investment may reach $3T-$4T per year by 2030.",
                            "period": "2030",
                            "summary": "NVIDIA says hyperscaler AI infrastructure investment may reach $3T-$4T per year by 2030.",
                            "confidence": 0.86,
                            "evidence_tier": "analyst_commentary",
                            "source_kind": "analyst",
                        }
                    ]
                },
            },
            "required_model_delta_candidates_count": 1,
            "required_durable_context_reads": 1,
            "required_existing_analyst_evidence_count": 1,
            "required_existing_institutional_evidence_count": 1,
            "expected_duplicate_risk_level": "high",
            "expected_duplicate_risk_reasons": [
                "existing_analyst_evidence_same_source_or_metric_value",
                "existing_institutional_evidence_same_source_or_metric_value",
            ],
            "expected_durable_recommended_action": "hold_for_duplicate_review",
            "forbid_durable_outputs": True,
            "allow_research_writes": False,
        },
    },
    {
        "slug": "nvidia-hyperscaler-capex-durable-reasoning-e2e",
        "title": "NVIDIA hyperscaler capex durable reasoning path",
        "source_label": "analyst_channel",
        "fixture_text": (
            "NVIDIA CFO Colette Kress says hyperscaler AI infrastructure investment may "
            "reach $3 trillion to $4 trillion per year by 2030, with Microsoft, Google, "
            "Amazon, and Meta investing about $700 billion this year."
        ),
        "description": "End-to-end durable-reasoning fixture: signal extraction resolves context, decision pass emits model-delta evidence, and service layer materializes it.",
        "tags": ["durable-reasoning", "model-delta", "analyst-evidence", "benchmark"],
        "expected": {
            "durable_reasoning_fixture": {
                "company": {
                    "name": "NVIDIA Eval Durable Reasoning",
                    "ticker": "NVDR",
                    "exchange": "NASDAQ",
                    "sector": "semis",
                    "industry": "accelerators",
                },
                "document": {
                    "title": "NVIDIA CFO hyperscaler capex durable reasoning signal",
                    "source_name": "analyst_channel",
                    "source_category": "analyst",
                    "url": "https://example.com/eval/nvidia-hyperscaler-capex-durable-reasoning",
                    "author": "NVIDIA CFO Colette Kress",
                    "language": "en",
                },
                "signal_result": {
                    "summary_short": "NVIDIA frames hyperscaler AI infrastructure as a multi-trillion-dollar annual demand pool by 2030.",
                    "summary_detail": "The source says hyperscaler AI infrastructure investment may reach $3T-$4T per year by 2030.",
                    "document_disposition": "durable_candidate",
                    "information_gain": "high",
                    "signal_profile": "dense_sell_side",
                    "signal_coverage": {
                        "status": "complete",
                        "material_signal_remaining": False,
                        "notes": ["Single-source fixture has one quantified durable model-delta signal."],
                    },
                    "atomic_claims": [
                        {
                            "claim_text": "NVIDIA CFO Colette Kress said hyperscaler AI infrastructure investment may reach $3T-$4T per year by 2030.",
                            "claim_kind": "financial_metric",
                            "entities": ["NVIDIA Eval Durable Reasoning"],
                            "confidence": 0.88,
                        }
                    ],
                    "entity_refs": [
                        {
                            "entity_type": "company",
                            "entity_name": "NVIDIA Eval Durable Reasoning",
                            "name": "NVIDIA Eval Durable Reasoning",
                            "ticker": "NVDR",
                            "confidence": 0.9,
                        }
                    ],
                    "durable_routes": [
                        {
                            "subject": "NVIDIA Eval Durable Reasoning data center demand",
                            "candidate_action": "review_for_update",
                            "destination_type": "research_subject",
                            "update_type": "consensus_estimate_update",
                            "routing_key": "NVIDIA Eval Durable Reasoning data center demand",
                            "new_information": "NVIDIA CFO Colette Kress said hyperscaler AI infrastructure investment may reach $3T-$4T per year by 2030.",
                            "rationale": "quantified hyperscaler AI infrastructure demand range",
                            "canonical_metric_key": "demand_signal",
                            "value_text": "Hyperscaler AI infrastructure investment may reach $3T-$4T per year by 2030.",
                            "period": "2030",
                            "claim_indexes": [0],
                            "confidence": 0.86,
                        }
                    ],
                },
                "decision_result": {
                    "decision_review": {
                        "decision": "approve",
                        "summary": "Context and duplicate checks support one model-delta analyst evidence row.",
                    },
                    "model_delta_candidates": [
                        {
                            "evidence_key": "eval:model-delta:nvidia-hyperscaler-capex-durable-reasoning",
                            "raw_subject_text": "NVIDIA Eval Durable Reasoning data center demand",
                            "canonical_metric_key": "demand_signal",
                            "metric_value_type": "status",
                            "value_text": "Hyperscaler AI infrastructure investment may reach $3T-$4T per year by 2030.",
                            "period": "2030",
                            "summary": "NVIDIA says hyperscaler AI infrastructure investment may reach $3T-$4T per year by 2030.",
                            "confidence": 0.86,
                            "evidence_tier": "analyst_commentary",
                            "source_kind": "analyst",
                        }
                    ],
                },
            },
            "required_model_delta_candidates_count": 1,
            "required_model_delta_evidence_count": 1,
            "required_analyst_evidence_count": 1,
            "required_institutional_observation_count": 1,
            "required_durable_context_reads": 1,
            "required_decision_context_reads": 1,
            "required_institutional_model_context_reads": 1,
            "required_market_context_reads": 1,
            "required_matched_identity_count": 1,
            "required_candidate_metric_count": 1,
            "required_destination_document_count": 1,
            "required_research_mode_context_count": 1,
            "required_signal_profile": "dense_sell_side",
            "required_signal_coverage_status": "complete",
            "required_no_material_signal_remaining": True,
            "min_atomic_claims": 1,
            "min_durable_routes": 1,
            "required_metric_keys": ["demand_signal"],
            "required_worker_api_paths": [
                "/api/local-agent/decision/context-batch",
                "/api/local-agent/durable/context",
                "/api/local-agent/market-context",
                "/api/local-agent/institutional/model-context",
                "/api/local-agent/documents/{document_id}/analysis-result",
            ],
            "required_durable_output_terms": ["$3T-$4T", "2030", "hyperscaler AI infrastructure"],
            "allow_research_writes": False,
        },
    },
    {
        "slug": "nonmodelable-material-requires-resolution-durable-output",
        "title": "Nonmodelable material requires resolution durable output",
        "source_label": "analyst_channel",
        "fixture_text": "A note mentions InP capacity status but does not resolve the canonical subject.",
        "description": "Non-modelable fixture: unresolved raw subject should not become durable evidence until identity resolution exists.",
        "tags": ["durable-output", "nonmodelable", "identity-resolution", "benchmark"],
        "expected": {
            "durable_output_fixture": {
                "company": {
                    "name": "Durable Resolution Eval",
                    "ticker": "DREV",
                    "exchange": "NASDAQ",
                    "sector": "semis",
                    "industry": "materials",
                },
                "document": {
                    "title": "InP capacity unresolved signal",
                    "source_name": "analyst_channel",
                    "source_category": "analyst",
                    "url": "https://example.com/eval/inp-resolution",
                    "author": "Unit Analyst",
                    "language": "en",
                },
                "analysis_result": {
                    "model_delta_candidates": [
                        {
                            "raw_subject_text": "InP capacity",
                            "requires_resolution": True,
                            "canonical_metric_key": "capacity_status",
                            "metric_value_type": "status",
                            "value_text": "capacity constrained",
                            "period": "2026",
                            "summary": "InP capacity appears constrained, but the canonical subject is unresolved.",
                            "confidence": 0.84,
                            "evidence_tier": "analyst_commentary",
                        }
                    ]
                },
            },
            "required_model_delta_candidates_count": 1,
            "required_model_delta_evidence_count": 0,
            "required_analyst_evidence_count": 0,
            "required_institutional_observation_count": 0,
            "required_durable_context_reads": 1,
            "forbid_durable_outputs": True,
            "allow_research_writes": False,
        },
    },
]

DOCUMENT_QUALITY_EVAL_CASES: list[dict[str, Any]] = [
    {
        "slug": "doc-45146-jefferies-taiwan-media-ocr-vlm",
        "title": "45146 Jefferies Taiwan semi media OCR/VLM",
        "source_label": "x_list_analysts",
        "document_id": 45146,
        "description": "Media-rich sell-side/channel-check tweet; OCR/VLM output should be available before compression.",
        "tags": ["document-quality", "ocr", "vlm", "compression", "benchmark"],
        "expected": {
            "latest_signal_extraction_status": "compressed",
            "required_media_ocr_count": 5,
            "required_signal_media_count": 5,
            "required_needs_vlm_media_count": 1,
            "required_media_signal_terms": ["MI450", "Rubin"],
            "required_text_terms": ["Jefferies", "MI450", "Rubin"],
            "required_signal_output_terms": ["Jefferies", "MI450", "Rubin"],
            "required_source_owner_terms": ["Jefferies"],
            "min_atomic_claims": 5,
            "min_source_owned_signal_items": 2,
            "forbidden_latest_compression_status": "error",
            "required_prep_complete": True,
            "hard_required_checks": [
                "latest_signal_extraction_status",
                "required_prep_complete",
                "required_signal_output:Jefferies",
                "required_signal_output:MI450",
                "required_signal_output:Rubin",
                "required_source_owner:Jefferies",
                "min_atomic_claims",
                "min_source_owned_signal_items",
            ],
            "allow_research_writes": False,
        },
    },
    {
        "slug": "doc-45368-korean-nvidia-graph-translation",
        "title": "45368 Korean NVIDIA article with graph",
        "source_label": "hankyung_tech",
        "document_id": 45368,
        "description": "Korean article plus graph; local-agent reads should translate to English and retain OCR graph context.",
        "tags": ["document-quality", "translation", "ocr", "graph", "benchmark"],
        "expected": {
            "latest_signal_extraction_status": "compressed",
            "document_language": "ko",
            "requires_translation_en": True,
            "min_translation_chars": 500,
            "required_media_ocr_count": 1,
            "required_media_kinds": ["chart"],
            "required_needs_vlm_media_count": 1,
            "required_signal_media_text_en_count": 1,
            "required_media_translation_available_count": 1,
            "required_text_terms": ["NVIDIA", "hyperscalers", "$700 billion", "Gartner"],
            "required_signal_output_terms": ["NVIDIA", "Gartner", "hyperscaler", "networking"],
            "required_source_owner_terms": ["NVIDIA", "Gartner"],
            "min_atomic_claims": 8,
            "min_summary_routes": 4,
            "min_source_owned_signal_items": 2,
            "hard_required_checks": [
                "required_signal_media_text_en_count",
                "required_media_translation_available_count",
            ],
            "allow_research_writes": False,
        },
    },
    {
        "slug": "doc-45375-korean-useless-media-discard",
        "title": "45375 Korean article with non-signal stock media",
        "source_label": "hankyung_tech",
        "document_id": 45375,
        "description": "Article image is illustrative/non-signal and should be marked or omitted from compression signal.",
        "tags": ["document-quality", "translation", "media-discard", "benchmark"],
        "expected": {
            "latest_signal_extraction_status": "compressed",
            "document_language": "ko",
            "requires_translation_en": True,
            "min_translation_chars": 300,
            "required_media_ocr_count": 1,
            "required_non_signal_media_count": 1,
            "required_media_kinds": ["non_signal_image"],
            "required_signal_output_terms": ["Blind", "Layoff Tracker", "New York Times"],
            "forbidden_text_terms": ["professional distress", "red high-heeled shoes"],
            "forbidden_signal_output_terms": ["professional distress", "red high-heeled shoes", "stock photograph"],
            "max_durable_routes": 0,
            "allow_research_writes": False,
            "forbid_research_writes_hard": True,
        },
    },
    {
        "slug": "doc-45155-asml-ocr-analyst-note",
        "title": "45155 ASML analyst note OCR",
        "source_label": "x_list_analysts",
        "document_id": 45155,
        "description": "ASML analyst note with image OCR; compression should preserve broker metrics and entities.",
        "tags": ["document-quality", "ocr", "analyst-commentary", "benchmark"],
        "expected": {
            "latest_signal_extraction_status": "compressed",
            "document_language": "en",
            "required_media_ocr_count": 2,
            "required_text_terms": ["ASML", "High NA", "EPS"],
            "required_signal_output_terms": ["UBS", "ASML", "High NA", "EPS"],
            "required_source_owner_terms": ["UBS"],
            "information_gain": ["medium", "high"],
            "min_atomic_claims": 4,
            "min_source_owned_signal_items": 1,
            "required_summary_entities": ["ASML"],
            "hard_required_checks": ["summary_entity:ASML"],
            "allow_research_writes": False,
        },
    },
    {
        "slug": "doc-45154-mizuho-memory-analog-ocr",
        "title": "45154 Mizuho memory/analog OCR",
        "source_label": "x_list_analysts",
        "document_id": 45154,
        "description": "Short tweet where attached images carry Mizuho memory/analog pricing signal.",
        "tags": ["document-quality", "ocr", "vlm", "analyst-commentary", "benchmark"],
        "expected": {
            "latest_signal_extraction_status": "compressed",
            "document_language": "en",
            "required_media_ocr_count": 4,
            "required_needs_vlm_media_count": 1,
            "required_media_signal_terms": ["DRAM", "NAND"],
            "required_text_terms": ["Mizuho", "DRAM", "NAND"],
            "required_signal_output_terms": ["Mizuho", "DRAM", "NAND", "40%", "50%"],
            "required_source_owner_terms": ["Mizuho"],
            "information_gain": ["medium", "high"],
            "min_atomic_claims": 6,
            "min_source_owned_signal_items": 2,
            "allow_research_writes": False,
        },
    },
    {
        "slug": "doc-44964-thelec-samsung-gan-translation",
        "title": "44964 TheElec Samsung GaN translation",
        "source_label": "thelec_semicon",
        "document_id": 44964,
        "description": "Korean article with existing English translation and concrete Samsung GaN strategy signal.",
        "tags": ["document-quality", "translation", "compression", "benchmark"],
        "expected": {
            "latest_signal_extraction_status": "compressed",
            "document_language": "ko",
            "requires_translation_en": True,
            "min_translation_chars": 1000,
            "required_text_terms": ["Samsung", "GaN", "foundry"],
            "required_signal_output_terms": ["Samsung", "GaN", "foundry", "Rds_on"],
            "information_gain": ["medium", "high"],
            "min_atomic_claims": 4,
            "required_summary_entities": ["Samsung"],
            "allow_research_writes": False,
            "allow_durable_write_routes": False,
            "forbid_durable_write_routes_hard": True,
        },
    },
    {
        "slug": "doc-40734-ibiden-transcript-hydration",
        "title": "40734 IBIDEN transcript hydration",
        "source_label": "earnings_transcript_links",
        "document_id": 40734,
        "description": "Transcript URL/PDF case; local-agent read should hydrate transcript text and reopen metadata-only skip for retry.",
        "tags": ["document-quality", "transcript", "hydration", "compression", "benchmark"],
        "expected": {
            "latest_signal_extraction_status": "compressed",
            "document_language": "en",
            "min_text_chars": 3000,
            "required_text_terms": ["IBIDEN", "Financial Presentation", "AI"],
            "required_signal_output_terms": ["IBIDEN", "JPY 500", "FY2030", "AI"],
            "latest_compression_status": ["retry_pending", "compressed"],
            "min_atomic_claims": 6,
            "min_durable_routes": 1,
            "hard_required_checks": [
                "latest_signal_extraction_status",
                "min_text_chars",
                "required_signal_output:IBIDEN",
                "required_signal_output:JPY 500",
                "required_signal_output:FY2030",
                "required_signal_output:AI",
                "min_atomic_claims",
                "min_durable_routes",
            ],
            "allow_research_writes": False,
            "allow_durable_write_routes": False,
            "forbid_durable_write_routes_hard": True,
        },
    },
    {
        "slug": "doc-36400-substack-teaser-no-info",
        "title": "36400 Substack teaser no-info",
        "source_label": "x_list_analysts",
        "document_id": 36400,
        "description": "Teaser/link-only Substack social post should not become durable research memory.",
        "tags": ["document-quality", "no-information-gain", "social-noise"],
        "expected": {
            "document_disposition": ["skip", "noop"],
            "information_gain": "none",
            "requires_no_claims": True,
            "allow_research_writes": False,
            "forbid_research_writes_hard": True,
        },
    },
    {
        "slug": "doc-27470-useless-chart-social-no-info",
        "title": "27470 Useless chart social no-info",
        "source_label": "x_list_analysts",
        "document_id": 27470,
        "description": "Vague chart post with no actionable entity/metric should remain skip/no-claims.",
        "tags": ["document-quality", "media-discard", "no-information-gain"],
        "expected": {
            "document_disposition": ["skip", "noop"],
            "information_gain": "none",
            "requires_no_claims": True,
            "allow_research_writes": False,
            "forbid_research_writes_hard": True,
        },
    },
    {
        "slug": "doc-45149-ksb-blykalla-social-summary-only",
        "title": "45149 KSB/Blykalla text-only social",
        "source_label": "x_list_analysts",
        "document_id": 45149,
        "description": "Ordinary text-only social source with concrete but low-confidence SMR pump optionality.",
        "tags": ["document-quality", "text-only", "social"],
        "expected": {
            "document_language": "en",
            "required_text_terms": ["KSB", "Blykalla"],
            "information_gain": ["low", "medium"],
            "allow_research_writes": False,
        },
    },
    {
        "slug": "doc-45151-cloud-infra-pricing-ocr",
        "title": "45151 Cloud infrastructure pricing OCR",
        "source_label": "x_list_analysts",
        "document_id": 45151,
        "description": "SemiAnalysis-style CPU/DRAM/storage cloud price increase case with OCR rows.",
        "tags": ["document-quality", "ocr", "bottleneck"],
        "expected": {
            "document_language": "en",
            "required_media_ocr_count": 2,
            "required_text_terms": ["CPU", "DRAM", "storage"],
            "allow_research_writes": False,
        },
    },
    {
        "slug": "doc-27936-udn-innolux-existing-translation",
        "title": "27936 Innolux FOPLP existing translation",
        "source_label": "udn_tw",
        "document_id": 27936,
        "description": "Chinese article with existing translation and concrete FOPLP/packaging entities.",
        "tags": ["document-quality", "translation", "packaging"],
        "expected": {
            "requires_translation_en": True,
            "min_translation_chars": 500,
            "required_text_terms": ["Innolux", "FOPLP"],
            "allow_research_writes": False,
        },
    },
]


PDF_OCR_PREP_EVAL_CASES: list[dict[str, Any]] = [
    {
        "slug": "synthetic-scanned-pdf-safe-ocr-prep",
        "title": "Synthetic scanned PDF safe OCR prep",
        "source_label": "local_agent_eval",
        "fixture_text": "Synthetic metadata-only PDF fixture for safe OCR preparation.",
        "description": (
            "Service-layer fixture that stores a PDF attachment, renders page images when text extraction is sparse, "
            "and queues the page images for media OCR without persisting durable research rows."
        ),
        "tags": ["document-quality", "pdf", "ocr-prep", "service-layer", "benchmark"],
        "expected": {
            "pdf_ocr_prep_fixture": {
                "pdf_text": "Thoth PDF OCR prep fixture. Sparse extractable text should still render pages for OCR.",
                "url": "https://example.com/eval/synthetic-scanned-pdf-safe-ocr-prep.pdf",
            },
            "required_pdf_page_render_status": "ok",
            "required_pdf_page_rendered_attachments": 1,
            "required_pdf_image_attachment_count": 1,
            "required_pdf_media_jobs": 1,
            "require_pdf_paths_relative": True,
            "require_pdf_page_original_url_marker": True,
            "allow_research_writes": False,
        },
    },
]


MEDIA_ROUTE_EVAL_CASES: list[dict[str, Any]] = [
    {
        "slug": "synthetic-media-route-ocr-first-vlm-followup",
        "title": "Synthetic media route OCR-first VLM follow-up",
        "source_label": "local_agent_eval",
        "fixture_text": "Synthetic media route fixture for OCR-first image preparation.",
        "description": (
            "Deterministic worker media-route eval proving route mode captures OCR for every media item, "
            "uses VLM after OCR for visual-heavy charts, and does not run VLM for non-signal media."
        ),
        "tags": ["document-quality", "media-route", "ocr", "vlm", "benchmark"],
        "expected": {
            "media_route_fixture": {
                "jobs": [
                    {
                        "name": "semianalysis_eda_chart",
                        "route": {
                            "lane": "vlm_first",
                            "image_kind": "diagram_chart_or_visual_slide",
                            "needs_vlm": True,
                        },
                        "job": {
                            "job_id": 1001,
                            "document_id": 45146,
                            "attachment_id": 9001,
                            "content_type": "image/png",
                            "source_name": "SemiAnalysis",
                            "document_title": "EDA market-share chart",
                            "document_url": "https://semianalysis.substack.com/p/eda",
                            "original_url": "https://example.com/eval/eda-market-share.png",
                        },
                        "ocr_text": (
                            "EDA Duopoly Market Share: Synopsys vs Cadence (%) 1Q18 3Q25. "
                            "Legend: SNPS, CDNS. Source: SemiAnalysis, Company Reports."
                        ),
                        "vision_text": (
                            "Title: EDA Duopoly Market Share: Synopsys vs Cadence (%). "
                            "X-axis is quarter from 1Q18 through 3Q25; y-axis is share of duopoly revenue from 35% to 65%. "
                            "Legend: SNPS in orange and CDNS in blue. Synopsys is near 60% in 1Q18, falls to roughly 52% around 4Q24, "
                            "then rebounds to the low 60s by 3Q25; Cadence moves inversely from about 40% to high 30s. "
                            "Source line: SemiAnalysis, Company Reports."
                        ),
                    },
                    {
                        "name": "illustrative_stock_photo",
                        "route": {
                            "lane": "ocr_first_then_vlm_if_low_confidence",
                            "image_kind": "photo_or_low_text_image",
                            "needs_vlm": False,
                        },
                        "job": {
                            "job_id": 1002,
                            "document_id": 45375,
                            "attachment_id": 9002,
                            "content_type": "image/png",
                            "source_name": "hankyung_tech",
                            "document_title": "Illustrative stock media",
                            "original_url": "https://example.com/eval/stock-photo.png",
                        },
                        "ocr_text": "Representative stock photo and profile picture. Shutterstock stock photo used as an illustration only.",
                    },
                    {
                        "name": "plain_text_page",
                        "route": {
                            "lane": "ocr_first",
                            "image_kind": "text_page_or_screenshot",
                            "needs_vlm": False,
                        },
                        "job": {
                            "job_id": 1003,
                            "document_id": 45155,
                            "attachment_id": 9003,
                            "content_type": "image/png",
                            "source_name": "x_list_analysts",
                            "document_title": "ASML analyst note page",
                            "original_url": "https://example.com/eval/asml-note-page.png",
                        },
                        "ocr_text": (
                            "ASML analyst note page. High NA EUV adoption remains tied to customer process timing, "
                            "tool availability, gross margin, EPS bridges, order timing, and installed base service revenue. "
                            "This is a text-heavy page with enough OCR content that visual follow-up is not required."
                        ),
                    },
                ]
            },
            "required_media_route_ocr_attempts": 3,
            "required_media_route_vlm_attempts": 1,
            "required_media_route_vlm_required_by_classifier": 1,
            "required_media_route_non_signal_count": 1,
            "required_media_route_ocr_before_vlm_count": 1,
            "required_media_route_no_vlm_for_non_signal": True,
            "required_media_route_call_sequences": {
                "semianalysis_eda_chart": ["ocr", "vision"],
                "illustrative_stock_photo": ["ocr"],
                "plain_text_page": ["ocr"],
            },
            "required_media_route_job_terms": {
                "semianalysis_eda_chart": [
                    "Title: EDA Duopoly Market Share",
                    "x-axis",
                    "y-axis",
                    "1Q18",
                    "3Q25",
                    "low 60s",
                    "Source line: SemiAnalysis",
                ],
                "plain_text_page": ["ASML", "High NA", "EPS"],
            },
            "allow_research_writes": False,
        },
    },
]


STAGE_PLAN_EVAL_CASES: list[dict[str, Any]] = [
    {
        "slug": "synthetic-balanced-stage-plan-quotas",
        "title": "Synthetic balanced stage-plan quotas",
        "source_label": "local_agent_eval",
        "fixture_text": "Synthetic stage-plan fixture for balanced OCR, signal, and durable scheduling.",
        "description": (
            "Deterministic service-layer eval proving the staged planner keeps bounded media prep, "
            "signal extraction, and durable reasoning quotas while expanding the balanced signal lookback."
        ),
        "tags": ["document-quality", "stage-plan", "balanced-scheduling", "benchmark"],
        "expected": {
            "stage_plan_fixture": {
                "document_age_hours": [1, 2, 3, 23, 25, 47],
                "media_jobs": 3,
                "retry_pending_compressions": 2,
                "durable_routes": 2,
                "media_limit": 2,
                "signal_limit": 4,
                "durable_limit": 1,
                "lookback_hours": 24,
                "queue_strategy": "balanced",
            },
            "required_stage_plan_queue_strategy": "balanced",
            "required_stage_plan_stage_quotas": {
                "media_prep": 2,
                "signal_extraction": 4,
                "durable_reasoning": 1,
            },
            "required_stage_plan_pending_media": 3,
            "required_stage_plan_retry_pending_signal": 2,
            "required_stage_plan_durable_candidates": 2,
            "required_stage_plan_lookback_expanded": True,
            "required_stage_plan_signal_lookback_min_hours": 25,
            "required_stage_plan_document_count": 4,
            "required_stage_plan_includes_oldest_tail": True,
            "allow_research_writes": False,
        },
    },
]


BOTTLENECK_REPORT_START_EVAL_CASES: list[dict[str, Any]] = [
    {
        "slug": "synthetic-bottleneck-report-start-user-defined",
        "title": "Synthetic user-defined bottleneck report startup",
        "source_label": "local_agent_eval",
        "fixture_text": "Synthetic bottleneck report startup fixture.",
        "description": (
            "Deterministic service-layer eval proving a user-defined bottleneck can be upserted, "
            "queued as a bottleneck-report campaign item, executed, and converted into a report workspace."
        ),
        "tags": ["bottleneck-report", "startup", "research-canon", "benchmark"],
        "expected": {
            "bottleneck_report_start_fixture": {
                "model_key": "eval-glass-core-qualified-capacity",
                "title": "Eval Glass Core Qualified Capacity",
                "scope_type": "theme",
                "scope_key": "advanced-packaging",
                "constrained_input": "qualified glass core substrate capacity",
                "output_unit": "advanced package starts",
                "summary": "Glass core qualification could constrain advanced packaging starts.",
                "thesis": "If qualified glass core substrate supply lags demand, advanced package starts are gated until shadow constraints bind.",
                "payload": {
                    "visible_future_horizon": "2026-2030",
                    "evidence_links": [
                        {
                            "title": "Synthetic sell-side chart excerpt",
                            "observed_source": "local_agent_eval",
                            "underlying_source": "unit fixture",
                            "metric_key": "qualified_input_supply",
                            "period": "2027",
                            "value_text": "qualification capacity remains the gating variable",
                        }
                    ],
                    "shadow_constraints": ["substrate attach yield", "advanced package test capacity"],
                    "falsification_tests": ["customer qualification schedules slip less than one quarter"],
                    "dataset_families": ["qualified_input_supply", "conversion_assumptions"],
                },
                "linked_evidence_fixture": {
                    "source_name": "Eval Glass Core Channel Check",
                    "document_title": "Eval glass core sell-side channel check",
                    "document_url": "https://example.com/eval/glass-core-channel-check",
                    "analyst_summary": "Sell-side channel check says qualified glass core substrate supply remains the gating variable for advanced package starts.",
                    "metric_key": "qualified_glass_core_capacity",
                    "period": "2027",
                    "value_text": "qualification capacity remains the gating variable",
                    "dataset_title": "Eval glass core qualified capacity bridge",
                },
                "campaign_key": "campaign:eval:bottleneck-report-start",
                "campaign_name": "Eval bottleneck report startup",
                "priority": 8,
            },
            "required_bottleneck_model_key": "eval-glass-core-qualified-capacity",
            "required_bottleneck_campaign_item_status": "workspace_ready",
            "required_bottleneck_assigned_rail": "bottleneck_reports",
            "required_bottleneck_report_type": "bottleneck_report",
            "required_bottleneck_execute_workspace_ready_count": 1,
            "required_bottleneck_report_artifact_types": ["json", "markdown"],
            "required_bottleneck_workflow_roles": ["gap_hunter_quick", "evidence_harvester", "model_builder", "challenger", "report_writer"],
            "required_bottleneck_open_gap_min": 1,
            "required_bottleneck_linked_evidence_min": 4,
            "required_bottleneck_linked_evidence_types": ["analyst_evidence", "institutional_evidence", "institutional_observation", "dataset"],
            "required_bottleneck_startup_packet": True,
            "allow_research_writes": False,
        },
    },
]


def _dt(value: object) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else None


def _clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _slugify(value: object, *, default: str = "agent-eval") -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return cleaned[:120].strip("-") or default


def _limit(value: int | None, *, default: int = 80, high: int = 250) -> int:
    try:
        parsed = int(value or default)
    except Exception:
        parsed = default
    return max(1, min(parsed, high))


def _as_list(value: object) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _lower_set(values: object) -> set[str]:
    return {_clean(v).lower() for v in _as_list(values) if _clean(v)}


def _expected_match(actual: object, expected: object) -> bool:
    actual_v = _clean(actual).lower()
    if isinstance(expected, list):
        return actual_v in {_clean(item).lower() for item in expected}
    return actual_v == _clean(expected).lower()


def _result_text_blob(result: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in (
        "title",
        "document_title",
        "text",
        "document_text",
        "document_text_excerpt",
        "translation_en",
        "translation_en_excerpt",
        "summary_short",
        "summary_detail",
    ):
        value = result.get(key)
        if value:
            parts.append(str(value))
    for row in _as_list(result.get("media_ocr")):
        if isinstance(row, dict):
            if row.get("is_signal") is False:
                continue
            parts.extend([str(row.get("summary") or ""), str(row.get("text_en") or ""), str(row.get("text") or "")])
    return "\n".join(part for part in parts if part)


def _signal_output_items(result: dict[str, Any]) -> list[str]:
    items: list[str] = []
    for key in ("summary_short", "summary_detail", "information_gain_reason", "skip_reason"):
        value = result.get(key)
        if value:
            items.append(str(value))
    for row in _as_list(result.get("atomic_claims")):
        if isinstance(row, dict):
            items.append(json.dumps(row, ensure_ascii=False, default=str))
    for key in (
        "summary_routes",
        "durable_routes",
        "research_writes",
        "research_requests",
        "model_delta_candidate_payloads",
        "analyst_evidence",
        "institutional_observations",
    ):
        for row in _as_list(result.get(key)):
            if isinstance(row, dict):
                items.append(json.dumps(row, ensure_ascii=False, default=str))
            elif row:
                items.append(str(row))
    return [item for item in items if item.strip()]


def _signal_output_blob(result: dict[str, Any]) -> str:
    return "\n".join(_signal_output_items(result))


def _signal_item_is_model_fact_or_missing_rail(text: str) -> bool:
    value = text.lower()
    if re.search(r"(\d|[%$€¥]|jpy|krw|won|usd|eps|wpm|qoq|yoy|fy\d{2,4}|cy\d{2,4}|q[1-4])", value):
        return True
    return any(
        marker in value
        for marker in (
            "missing rail",
            "missing_rail",
            "requires_resolution",
            "research_request",
            "instrument request",
            "schema request",
        )
    )


def _source_name(row: Document | None) -> str | None:
    return row.source.name if row is not None and row.source is not None else None


def _doc_title(row: Document | None) -> str:
    if row is None:
        return "Unknown document"
    return str(row.title or f"Document {row.id}")


def _serialize_review_route(row: LocalAgentEntityRoute) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "compression_id": int(row.compression_id) if row.compression_id is not None else None,
        "lane": row.lane,
        "entity_name": row.entity_name,
        "entity_type": row.entity_type,
        "entity_role": row.entity_role,
        "durable_action": row.durable_action,
        "materiality": row.materiality,
    }


def _serialize_review_write(row: LocalAgentWriteDiff) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "run_id": int(row.run_id) if row.run_id is not None else None,
        "action_id": int(row.action_id) if row.action_id is not None else None,
        "destination_type": row.destination_type,
        "destination_id": int(row.destination_id) if row.destination_id is not None else None,
        "source_document_ids": list(row.source_document_ids_json or []),
        "before_version_id": int(row.before_version_id) if row.before_version_id is not None else None,
        "after_version_id": int(row.after_version_id) if row.after_version_id is not None else None,
        "write_summary": row.write_summary,
        "confidence": float(row.confidence) if row.confidence is not None else None,
        "created_at": _dt(row.created_at),
    }


def _serialize_decision(row: LocalAgentReviewDecision | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": int(row.id),
        "target_kind": row.target_kind,
        "target_id": int(row.target_id),
        "decision": row.decision,
        "note": row.note,
        "payload": dict(row.payload or {}),
        "created_by": row.created_by,
        "created_at": _dt(row.created_at),
        "updated_at": _dt(row.updated_at),
    }


def _serialize_decision_summary(row: LocalAgentReviewDecision | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": int(row.id),
        "target_kind": row.target_kind,
        "target_id": int(row.target_id),
        "decision": row.decision,
        "note": row.note,
        "created_by": row.created_by,
        "created_at": _dt(row.created_at),
        "updated_at": _dt(row.updated_at),
    }


def serialize_eval_case(row: LocalAgentEvalCase, latest_run: LocalAgentEvalRun | None = None) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "slug": row.slug,
        "title": row.title,
        "description": row.description,
        "source_label": row.source_label,
        "document_id": int(row.document_id) if row.document_id is not None else None,
        "document_title": _doc_title(row.document) if row.document is not None else None,
        "fixture_title": row.fixture_title,
        "fixture_text": row.fixture_text,
        "expected": dict(row.expected_json or {}),
        "tags": list(row.tags_json or []),
        "active": bool(row.active),
        "created_at": _dt(row.created_at),
        "updated_at": _dt(row.updated_at),
        "latest_run": serialize_eval_run(latest_run) if latest_run else None,
    }


def serialize_eval_run(row: LocalAgentEvalRun | None) -> dict[str, Any] | None:
    if row is None:
        return None
    score = dict(row.score_json or {})
    return {
        "id": int(row.id),
        "case_id": int(row.case_id),
        "source_kind": row.source_kind,
        "model": row.model,
        "prompt_version": row.prompt_version,
        "status": row.status,
        "result": dict(row.result_json or {}),
        "score": score,
        "passed": bool(score.get("passed", False)),
        "score_value": float(score.get("score", 0.0) or 0.0),
        "error": row.error,
        "created_at": _dt(row.created_at),
    }


def _serialize_eval_run_summary(row: LocalAgentEvalRun | None) -> dict[str, Any] | None:
    if row is None:
        return None
    score = dict(row.score_json or {})
    return {
        "id": int(row.id),
        "case_id": int(row.case_id),
        "source_kind": row.source_kind,
        "status": row.status,
        "passed": bool(score.get("passed", False)),
        "score_value": float(score.get("score", 0.0) or 0.0),
        "created_at": _dt(row.created_at),
    }


def _serialize_eval_case_summary(
    row: LocalAgentEvalCase,
    latest_run: LocalAgentEvalRun | None = None,
) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "slug": row.slug,
        "title": row.title,
        "source_label": row.source_label,
        "document_id": int(row.document_id) if row.document_id is not None else None,
        "document_title": _doc_title(row.document) if row.document is not None else None,
        "tags": list(row.tags_json or []),
        "latest_run": _serialize_eval_run_summary(latest_run),
    }


def latest_review_decision(
    session: Session,
    *,
    target_kind: str,
    target_id: int,
) -> LocalAgentReviewDecision | None:
    return (
        session.query(LocalAgentReviewDecision)
        .filter(
            LocalAgentReviewDecision.target_kind == str(target_kind),
            LocalAgentReviewDecision.target_id == int(target_id),
        )
        .order_by(LocalAgentReviewDecision.created_at.desc(), LocalAgentReviewDecision.id.desc())
        .first()
    )


def _latest_review_decisions(
    session: Session,
    *,
    target_kind: str,
    target_ids: list[int],
) -> dict[int, LocalAgentReviewDecision]:
    resolved_ids = sorted({int(target_id) for target_id in target_ids if target_id is not None})
    if not resolved_ids:
        return {}
    kind = str(target_kind)
    latest_rank = (
        session.query(
            LocalAgentReviewDecision.id.label("id"),
            func.row_number()
            .over(
                partition_by=LocalAgentReviewDecision.target_id,
                order_by=(LocalAgentReviewDecision.created_at.desc(), LocalAgentReviewDecision.id.desc()),
            )
            .label("rank"),
        )
        .filter(
            LocalAgentReviewDecision.target_kind == kind,
            LocalAgentReviewDecision.target_id.in_(resolved_ids),
        )
        .subquery()
    )
    rows = (
        session.query(LocalAgentReviewDecision)
        .join(latest_rank, LocalAgentReviewDecision.id == latest_rank.c.id)
        .options(
            load_only(
                LocalAgentReviewDecision.id,
                LocalAgentReviewDecision.target_kind,
                LocalAgentReviewDecision.target_id,
                LocalAgentReviewDecision.decision,
                LocalAgentReviewDecision.note,
                LocalAgentReviewDecision.created_by,
                LocalAgentReviewDecision.created_at,
                LocalAgentReviewDecision.updated_at,
            )
        )
        .filter(latest_rank.c.rank == 1)
        .all()
    )
    return {int(row.target_id): row for row in rows}


def _route_subject(route: dict[str, Any]) -> str:
    return _clean(route.get("subject") or route.get("entity_name") or route.get("name"))


def score_agent_eval_output(result: dict[str, Any], expected: dict[str, Any]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(name: str, passed: bool, *, weight: float = 1.0, detail: str = "") -> None:
        checks.append({"name": name, "passed": bool(passed), "weight": weight, "detail": detail})

    if expected.get("eval_kind") in {"knowledge_qa", "knowledge_qa_multimodal"}:
        expected_answers = {_clean(value).upper() for value in _as_list(expected.get("correct_answers")) if _clean(value)}
        raw_answers = result.get("answers")
        if not isinstance(raw_answers, list):
            raw_answers = [result.get("answer")] if result.get("answer") is not None else []
        actual_answers = {_clean(value).upper() for value in raw_answers if _clean(value)}
        add(
            "correct_answer",
            bool(expected_answers) and actual_answers == expected_answers,
            weight=10.0,
            detail=f"actual={sorted(actual_answers)} expected={sorted(expected_answers)}",
        )

    if "document_disposition" in expected:
        add(
            "document_disposition",
            _expected_match(result.get("document_disposition"), expected.get("document_disposition")),
            weight=2.0,
            detail=f"actual={result.get('document_disposition')}",
        )
    if "information_gain" in expected:
        add(
            "information_gain",
            _expected_match(result.get("information_gain"), expected.get("information_gain")),
            weight=2.0,
            detail=f"actual={result.get('information_gain')}",
        )
    if "document_language" in expected:
        add(
            "document_language",
            _expected_match(result.get("document_language") or result.get("language"), expected.get("document_language")),
            detail=f"actual={result.get('document_language') or result.get('language')}",
        )
    if expected.get("requires_translation_en") is True:
        chars = int(result.get("translation_chars") or 0)
        add("translation_en_available", chars > 0, weight=2.0, detail=f"translation_chars={chars}")
    if "min_translation_chars" in expected:
        try:
            min_chars = int(expected.get("min_translation_chars") or 0)
        except Exception:
            min_chars = 0
        chars = int(result.get("translation_chars") or 0)
        add("min_translation_chars", chars >= min_chars, weight=2.0, detail=f"translation_chars={chars} min={min_chars}")
    if "min_text_chars" in expected:
        try:
            min_chars = int(expected.get("min_text_chars") or 0)
        except Exception:
            min_chars = 0
        chars = int(result.get("text_chars") or 0)
        add("min_text_chars", chars >= min_chars, weight=2.0, detail=f"text_chars={chars} min={min_chars}")
    if "required_media_ocr_count" in expected:
        required = int(expected.get("required_media_ocr_count") or 0)
        actual = int(result.get("media_ocr_count") or 0)
        add("required_media_ocr_count", actual >= required, weight=2.0, detail=f"actual={actual} required={required}")
    if "required_signal_media_count" in expected:
        required = int(expected.get("required_signal_media_count") or 0)
        actual = int(result.get("signal_media_count") or 0)
        add("required_signal_media_count", actual >= required, detail=f"actual={actual} required={required}")
    if "required_non_signal_media_count" in expected:
        required = int(expected.get("required_non_signal_media_count") or 0)
        actual = int(result.get("non_signal_media_count") or 0)
        add("required_non_signal_media_count", actual >= required, weight=1.5, detail=f"actual={actual} required={required}")
    media_rows = [row for row in _as_list(result.get("media_ocr")) if isinstance(row, dict)]
    if "required_media_kinds" in expected:
        actual_kinds = {_clean(row.get("media_kind")).lower() for row in media_rows if _clean(row.get("media_kind"))}
        for kind in _as_list(expected.get("required_media_kinds")):
            expected_kind = _clean(kind).lower()
            add(f"required_media_kind:{kind}", expected_kind in actual_kinds, weight=1.5, detail=f"actual={sorted(actual_kinds)}")
    if "required_needs_vlm_media_count" in expected:
        required = int(expected.get("required_needs_vlm_media_count") or 0)
        actual = sum(1 for row in media_rows if bool(row.get("needs_vlm")) is True)
        add("required_needs_vlm_media_count", actual >= required, weight=1.5, detail=f"actual={actual} required={required}")
    if "required_signal_media_text_en_count" in expected:
        required = int(expected.get("required_signal_media_text_en_count") or 0)
        actual = sum(1 for row in media_rows if row.get("is_signal") is True and str(row.get("text_en") or "").strip())
        add("required_signal_media_text_en_count", actual >= required, weight=2.0, detail=f"actual={actual} required={required}")
    if "required_media_translation_available_count" in expected:
        required = int(expected.get("required_media_translation_available_count") or 0)
        actual = sum(1 for row in media_rows if _dict(row.get("translation")).get("available") is True)
        add("required_media_translation_available_count", actual >= required, weight=2.0, detail=f"actual={actual} required={required}")
    if "required_media_ocr_quality" in expected:
        allowed = _lower_set(expected.get("required_media_ocr_quality"))
        actual = {_clean(row.get("ocr_quality")).lower() for row in media_rows if _clean(row.get("ocr_quality"))}
        add("required_media_ocr_quality", bool(actual & allowed), detail=f"actual={sorted(actual)} allowed={sorted(allowed)}")
    if "latest_compression_status" in expected:
        add(
            "latest_compression_status",
            _expected_match(result.get("latest_compression_status") or result.get("status"), expected.get("latest_compression_status")),
            detail=f"actual={result.get('latest_compression_status') or result.get('status')}",
        )
    if "latest_signal_extraction_status" in expected:
        actual_status = result.get("latest_signal_extraction_status") or result.get("latest_compression_status") or result.get("status")
        add(
            "latest_signal_extraction_status",
            _expected_match(actual_status, expected.get("latest_signal_extraction_status")),
            detail=f"actual={actual_status}",
        )
    if "forbidden_latest_compression_status" in expected:
        forbidden = _lower_set(expected.get("forbidden_latest_compression_status"))
        if not forbidden:
            forbidden = {_clean(expected.get("forbidden_latest_compression_status")).lower()}
        actual = _clean(result.get("latest_compression_status") or result.get("status")).lower()
        add("forbidden_latest_compression_status", actual not in forbidden, detail=f"actual={actual} forbidden={sorted(forbidden)}")
    if "required_prep_complete" in expected:
        required = bool(expected.get("required_prep_complete"))
        actual = bool(_dict(result.get("prep_status")).get("prep_complete"))
        add("required_prep_complete", actual is required, weight=2.0, detail=f"actual={actual} required={required}")

    text_blob = _result_text_blob(result).lower()
    for term in _as_list(expected.get("required_text_terms")):
        needle = _clean(term).lower()
        add(f"required_text:{term}", bool(needle and needle in text_blob), weight=1.5, detail=f"term={term}")
    for term in _as_list(expected.get("forbidden_text_terms")):
        needle = _clean(term).lower()
        add(f"forbidden_text:{term}", not bool(needle and needle in text_blob), detail=f"term={term}")

    signal_output_items = _signal_output_items(result)
    signal_output_blob = "\n".join(signal_output_items).lower()
    for term in _as_list(expected.get("required_signal_output_terms")):
        needle = _clean(term).lower()
        add(
            f"required_signal_output:{term}",
            bool(needle and needle in signal_output_blob),
            weight=2.0,
            detail=f"term={term}",
        )
    for term in _as_list(expected.get("forbidden_signal_output_terms")):
        needle = _clean(term).lower()
        add(
            f"forbidden_signal_output:{term}",
            not bool(needle and needle in signal_output_blob),
            weight=1.5,
            detail=f"term={term}",
        )
    for term in _as_list(expected.get("required_source_owner_terms")):
        needle = _clean(term).lower()
        add(
            f"required_source_owner:{term}",
            bool(needle and needle in signal_output_blob),
            weight=1.5,
            detail=f"term={term}",
        )

    summary_names = {_route_subject(row).lower() for row in _as_list(result.get("summary_routes"))}
    for entity in _as_list(expected.get("required_summary_entities")):
        name = _clean(entity).lower()
        add(f"summary_entity:{entity}", any(name in route for route in summary_names), detail=str(sorted(summary_names)))

    durable_routes = [row for row in _as_list(result.get("durable_routes")) if isinstance(row, dict)]
    durable_write_routes = [
        row
        for row in durable_routes
        if _clean(row.get("action") or row.get("durable_action")).lower() == "write"
    ]
    research_writes = [row for row in _as_list(result.get("research_writes")) if isinstance(row, dict)]
    historical_write_diffs = [row for row in _as_list(result.get("historical_write_diffs")) if isinstance(row, dict)]
    if expected.get("allow_research_writes") is False:
        add(
            "no_direct_research_writes",
            not research_writes,
            weight=2.0,
            detail=(
                f"research_writes={len(research_writes)} "
                f"historical_write_diffs={len(historical_write_diffs)} "
                f"durable_write_routes={len(durable_write_routes)}"
            ),
        )
        add(
            "historical_write_diffs_flag",
            not historical_write_diffs,
            weight=0.001,
            detail=f"historical_write_diffs={len(historical_write_diffs)}",
        )
    if expected.get("allow_durable_write_routes") is False:
        add(
            "no_durable_write_routes",
            not durable_write_routes,
            weight=2.0,
            detail=f"durable_write_routes={len(durable_write_routes)}",
        )
    if expected.get("forbid_durable_write_routes_hard") is True:
        add(
            "no_durable_write_routes_hard",
            not durable_write_routes,
            weight=0.0,
            detail=f"durable_write_routes={len(durable_write_routes)}",
        )
    if expected.get("forbid_historical_write_diffs_hard") is True:
        add(
            "no_historical_write_diffs_hard",
            not historical_write_diffs,
            weight=0.0,
            detail=f"historical_write_diffs={len(historical_write_diffs)}",
        )
    if expected.get("forbid_research_writes_hard") is True:
        add(
            "no_direct_research_writes_hard",
            not durable_write_routes and not research_writes and not historical_write_diffs,
            weight=0.0,
            detail=(
                f"research_writes={len(research_writes)} "
                f"historical_write_diffs={len(historical_write_diffs)} "
                f"durable_write_routes={len(durable_write_routes)}"
            ),
        )

    for required in _as_list(expected.get("required_durable_actions")):
        if not isinstance(required, dict):
            continue
        subject = _clean(required.get("subject")).lower()
        action = _clean(required.get("action")).lower()
        matched = False
        for route in durable_routes:
            route_subject = _route_subject(route).lower()
            route_action = _clean(route.get("action") or route.get("durable_action")).lower()
            if subject and subject in route_subject and (not action or action == route_action):
                matched = True
                break
        add(f"durable_action:{required.get('subject')}:{action}", matched, detail=str(durable_routes[:5]))

    forbidden = _lower_set(expected.get("forbidden_durable_entities"))
    if forbidden:
        durable_subjects = {_route_subject(row).lower() for row in durable_write_routes + research_writes}
        add("forbidden_durable_entities", not bool(forbidden & durable_subjects), detail=str(sorted(durable_subjects)))

    if expected.get("requires_no_claims") is True:
        claims = _as_list(result.get("atomic_claims"))
        add("no_atomic_claims", len(claims) == 0, weight=1.5, detail=f"claims={len(claims)}")
    if "min_atomic_claims" in expected:
        required = int(expected.get("min_atomic_claims") or 0)
        actual = len(_as_list(result.get("atomic_claims")))
        add("min_atomic_claims", actual >= required, weight=2.0, detail=f"actual={actual} required={required}")
    if "min_summary_routes" in expected:
        required = int(expected.get("min_summary_routes") or 0)
        actual = len(_as_list(result.get("summary_routes")))
        add("min_summary_routes", actual >= required, weight=1.5, detail=f"actual={actual} required={required}")
    if "min_durable_routes" in expected:
        required = int(expected.get("min_durable_routes") or 0)
        actual = len(_as_list(result.get("durable_routes")))
        add("min_durable_routes", actual >= required, weight=2.0, detail=f"actual={actual} required={required}")
    if "max_durable_routes" in expected:
        allowed = int(expected.get("max_durable_routes") or 0)
        actual = len(_as_list(result.get("durable_routes")))
        add("max_durable_routes", actual <= allowed, weight=1.5, detail=f"actual={actual} allowed={allowed}")
    if "min_source_owned_signal_items" in expected:
        required = int(expected.get("min_source_owned_signal_items") or 0)
        owner_terms = [
            _clean(term).lower()
            for term in _as_list(expected.get("required_source_owner_terms"))
            if _clean(term)
        ]
        if not owner_terms:
            owner_terms = [
                _clean(result.get("source_name") or result.get("source_label")).lower(),
                _clean(result.get("author")).lower(),
            ]
            owner_terms = [term for term in owner_terms if term]
        actual = 0
        for item in signal_output_items:
            item_lower = item.lower()
            if owner_terms and not any(term in item_lower for term in owner_terms):
                continue
            if _signal_item_is_model_fact_or_missing_rail(item_lower):
                actual += 1
        add(
            "min_source_owned_signal_items",
            actual >= required,
            weight=2.0,
            detail=f"actual={actual} required={required} owners={owner_terms}",
        )
    if "required_signal_profile" in expected:
        expected_profile = _clean(expected.get("required_signal_profile")).lower()
        actual_profile = _clean(result.get("signal_profile")).lower()
        add("required_signal_profile", actual_profile == expected_profile, weight=1.5, detail=f"actual={actual_profile} expected={expected_profile}")
    signal_coverage = _dict(result.get("signal_coverage"))
    if "required_signal_coverage_status" in expected:
        expected_status = _clean(expected.get("required_signal_coverage_status")).lower()
        actual_status = _clean(signal_coverage.get("status") or result.get("signal_coverage_status")).lower()
        add("required_signal_coverage_status", actual_status == expected_status, weight=1.5, detail=f"actual={actual_status} expected={expected_status}")
    if expected.get("required_no_material_signal_remaining") is True:
        remaining = signal_coverage.get("material_signal_remaining")
        add("required_no_material_signal_remaining", remaining is False, weight=1.5, detail=f"material_signal_remaining={remaining}")

    for term in _as_list(expected.get("required_claim_terms")):
        needle = _clean(term).lower()
        claims_blob = " ".join(_clean(row.get("claim_text")).lower() for row in _as_list(result.get("atomic_claims")) if isinstance(row, dict))
        add(f"required_claim:{term}", bool(needle and needle in claims_blob), weight=2.0, detail=f"term={term}")

    if "required_model_delta_candidates_count" in expected:
        required = int(expected.get("required_model_delta_candidates_count") or 0)
        actual = int(result.get("model_delta_candidates_count") or result.get("model_delta_candidates") or 0)
        add("required_model_delta_candidates_count", actual >= required, weight=1.5, detail=f"actual={actual} required={required}")
    if "required_model_delta_evidence_count" in expected:
        required = int(expected.get("required_model_delta_evidence_count") or 0)
        actual = int(result.get("model_delta_evidence_count") or result.get("model_delta_evidence") or 0)
        add("required_model_delta_evidence_count", actual >= required, weight=2.0, detail=f"actual={actual} required={required}")
    if "required_analyst_evidence_count" in expected:
        required = int(expected.get("required_analyst_evidence_count") or 0)
        actual = int(result.get("analyst_evidence_count") or len(_as_list(result.get("analyst_evidence"))))
        add("required_analyst_evidence_count", actual >= required, weight=2.0, detail=f"actual={actual} required={required}")
    if "required_institutional_observation_count" in expected:
        required = int(expected.get("required_institutional_observation_count") or 0)
        actual = int(result.get("institutional_observation_count") or len(_as_list(result.get("institutional_observations"))))
        add("required_institutional_observation_count", actual >= required, weight=2.0, detail=f"actual={actual} required={required}")
    if "required_durable_context_reads" in expected:
        required = int(expected.get("required_durable_context_reads") or 0)
        stats = _dict(result.get("stats"))
        actual = int(result.get("durable_context_reads") or stats.get("durable_context_reads") or 0)
        add("required_durable_context_reads", actual >= required, weight=1.5, detail=f"actual={actual} required={required}")
    if "required_decision_context_reads" in expected:
        required = int(expected.get("required_decision_context_reads") or 0)
        stats = _dict(result.get("stats"))
        actual = int(result.get("decision_context_reads") or stats.get("decision_context_reads") or 0)
        add("required_decision_context_reads", actual >= required, weight=1.5, detail=f"actual={actual} required={required}")
    if "required_institutional_model_context_reads" in expected:
        required = int(expected.get("required_institutional_model_context_reads") or 0)
        stats = _dict(result.get("stats"))
        actual = int(result.get("institutional_model_context_reads") or stats.get("institutional_model_context_reads") or 0)
        add("required_institutional_model_context_reads", actual >= required, weight=1.5, detail=f"actual={actual} required={required}")
    if "required_market_context_reads" in expected:
        required = int(expected.get("required_market_context_reads") or 0)
        stats = _dict(result.get("stats"))
        actual = int(result.get("market_context_reads") or stats.get("market_context_reads") or 0)
        add("required_market_context_reads", actual >= required, weight=1.0, detail=f"actual={actual} required={required}")
    required_worker_api_paths = [str(value).strip() for value in _as_list(expected.get("required_worker_api_paths")) if str(value or "").strip()]
    if required_worker_api_paths:
        actual_paths = {
            str(value).strip()
            for value in _as_list(result.get("worker_api_paths"))
            if str(value or "").strip()
        }
        for call in _as_list(result.get("worker_api_calls")):
            if isinstance(call, dict) and str(call.get("path") or "").strip():
                actual_paths.add(str(call.get("path")).strip())
        for required_path in required_worker_api_paths:
            add(f"required_worker_api_path:{required_path}", required_path in actual_paths, weight=1.0, detail=f"actual={sorted(actual_paths)}")
    if "required_matched_identity_count" in expected:
        required = int(expected.get("required_matched_identity_count") or 0)
        actual = int(result.get("matched_identity_count") or len(_as_list(_dict(result.get("durable_candidate_context")).get("matched_identities"))))
        add("required_matched_identity_count", actual >= required, weight=1.5, detail=f"actual={actual} required={required}")
    if "required_candidate_metric_count" in expected:
        required = int(expected.get("required_candidate_metric_count") or 0)
        actual = int(result.get("candidate_metric_count") or len(_as_list(_dict(result.get("durable_candidate_context")).get("candidate_metrics"))))
        add("required_candidate_metric_count", actual >= required, weight=1.5, detail=f"actual={actual} required={required}")
    if "required_destination_document_count" in expected:
        required = int(expected.get("required_destination_document_count") or 0)
        actual = int(result.get("destination_document_count") or len(_as_list(_dict(result.get("durable_candidate_context")).get("destination_documents"))))
        add("required_destination_document_count", actual >= required, weight=1.5, detail=f"actual={actual} required={required}")
    if "required_research_mode_context_count" in expected:
        required = int(expected.get("required_research_mode_context_count") or 0)
        actual = int(result.get("research_mode_context_count") or len(_as_list(_dict(result.get("durable_candidate_context")).get("research_mode_contexts"))))
        add("required_research_mode_context_count", actual >= required, weight=1.5, detail=f"actual={actual} required={required}")
    if "required_existing_analyst_evidence_count" in expected:
        required = int(expected.get("required_existing_analyst_evidence_count") or 0)
        actual = int(result.get("existing_analyst_evidence_count") or len(_as_list(_dict(result.get("durable_candidate_context")).get("existing_analyst_evidence"))))
        add("required_existing_analyst_evidence_count", actual >= required, weight=1.5, detail=f"actual={actual} required={required}")
    if "required_existing_institutional_evidence_count" in expected:
        required = int(expected.get("required_existing_institutional_evidence_count") or 0)
        actual = int(
            result.get("existing_institutional_evidence_count")
            or len(_as_list(_dict(result.get("durable_candidate_context")).get("existing_institutional_evidence")))
        )
        add("required_existing_institutional_evidence_count", actual >= required, weight=1.5, detail=f"actual={actual} required={required}")
    if "expected_duplicate_risk_level" in expected:
        expected_level = _clean(expected.get("expected_duplicate_risk_level")).lower()
        duplicate_risk = _dict(_dict(result.get("durable_candidate_context")).get("duplicate_risk"))
        actual_level = _clean(result.get("duplicate_risk_level") or duplicate_risk.get("level")).lower()
        add("expected_duplicate_risk_level", actual_level == expected_level, weight=2.0, detail=f"actual={actual_level} expected={expected_level}")
    for reason in _as_list(expected.get("expected_duplicate_risk_reasons")):
        needle = _clean(reason)
        duplicate_risk = _dict(_dict(result.get("durable_candidate_context")).get("duplicate_risk"))
        reasons = {_clean(value) for value in _as_list(duplicate_risk.get("reasons"))}
        add(f"expected_duplicate_risk_reason:{needle}", needle in reasons, weight=1.0, detail=f"actual={sorted(reasons)}")
    if "expected_durable_recommended_action" in expected:
        expected_action = _clean(expected.get("expected_durable_recommended_action")).lower()
        actual_action = _clean(result.get("durable_recommended_action") or _dict(result.get("durable_candidate_context")).get("recommended_action")).lower()
        add("expected_durable_recommended_action", actual_action == expected_action, weight=2.0, detail=f"actual={actual_action} expected={expected_action}")
    if expected.get("forbid_durable_outputs") is True:
        actual = {
            "analyst_evidence": int(result.get("analyst_evidence_count") or 0),
            "institutional_observations": int(result.get("institutional_observation_count") or 0),
            "materialized_outputs": int(result.get("materialized_output_count") or 0),
        }
        add(
            "forbid_durable_outputs",
            actual["analyst_evidence"] == 0 and actual["institutional_observations"] == 0 and actual["materialized_outputs"] == 0,
            weight=0.0,
            detail=str(actual),
        )
    if "required_metric_keys" in expected:
        metric_keys: set[str] = set()
        for row in _as_list(result.get("analyst_evidence")):
            if not isinstance(row, dict):
                continue
            metric_keys.add(_clean(row.get("estimate_metric") or row.get("metric_key")).lower())
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            forecast = payload.get("forecast") if isinstance(payload.get("forecast"), dict) else {}
            metric_keys.add(_clean(forecast.get("metric_key")).lower())
        for row in _as_list(result.get("institutional_observations")):
            if isinstance(row, dict):
                metric_keys.add(_clean(row.get("metric_key")).lower())
        metric_keys.discard("")
        for metric_key in _as_list(expected.get("required_metric_keys")):
            needle = _clean(metric_key).lower()
            add(f"required_metric_key:{metric_key}", needle in metric_keys, weight=1.5, detail=f"actual={sorted(metric_keys)}")

    media_blob = " ".join(
        _clean(value).lower()
        for row in _as_list(result.get("media_ocr"))
        if isinstance(row, dict)
        for value in (row.get("summary"), row.get("text_en"), row.get("text"), row.get("ocr_summary"), row.get("ocr_text"))
    )
    for term in _as_list(expected.get("required_media_signal_terms")):
        needle = _clean(term).lower()
        add(f"required_media_signal:{term}", bool(needle and needle in media_blob), weight=1.5, detail=f"term={term}")

    durable_output_blob = json.dumps(
        {
            "analyst_evidence": result.get("analyst_evidence"),
            "institutional_observations": result.get("institutional_observations"),
            "model_delta_candidates": result.get("model_delta_candidate_payloads"),
            "stats": result.get("stats"),
        },
        ensure_ascii=False,
        default=str,
    ).lower()
    for term in _as_list(expected.get("required_durable_output_terms")):
        needle = _clean(term).lower()
        add(f"required_durable_output:{term}", bool(needle and needle in durable_output_blob), weight=1.5, detail=f"term={term}")

    if "required_pdf_page_render_status" in expected:
        add(
            "required_pdf_page_render_status",
            _expected_match(result.get("pdf_page_render_status"), expected.get("required_pdf_page_render_status")),
            weight=2.0,
            detail=f"actual={result.get('pdf_page_render_status')}",
        )
    if "required_pdf_page_rendered_attachments" in expected:
        required = int(expected.get("required_pdf_page_rendered_attachments") or 0)
        actual = int(result.get("pdf_page_rendered_attachments") or 0)
        add("required_pdf_page_rendered_attachments", actual >= required, weight=2.0, detail=f"actual={actual} required={required}")
    if "required_pdf_image_attachment_count" in expected:
        required = int(expected.get("required_pdf_image_attachment_count") or 0)
        actual = int(result.get("pdf_image_attachment_count") or 0)
        add("required_pdf_image_attachment_count", actual >= required, weight=2.0, detail=f"actual={actual} required={required}")
    if "required_pdf_media_jobs" in expected:
        required = int(expected.get("required_pdf_media_jobs") or 0)
        actual = int(result.get("pdf_media_jobs") or 0)
        add("required_pdf_media_jobs", actual >= required, weight=2.0, detail=f"actual={actual} required={required}")
    if expected.get("require_pdf_paths_relative") is True:
        add(
            "pdf_paths_relative_to_object_root",
            bool(result.get("object_paths_relative_to_eval_root")),
            weight=1.5,
            detail=str(result.get("object_paths")),
        )
    if expected.get("require_pdf_page_original_url_marker") is True:
        add(
            "pdf_page_original_url_marker",
            bool(result.get("page_original_url_marker")),
            weight=1.0,
            detail=str(result.get("page_original_urls")),
        )
    route_stats = _dict(result.get("media_route_stats") or result.get("stats"))
    if "required_media_route_ocr_attempts" in expected:
        required = int(expected.get("required_media_route_ocr_attempts") or 0)
        actual = int(result.get("media_route_ocr_attempts") or route_stats.get("media_ocr_attempts") or 0)
        add("required_media_route_ocr_attempts", actual >= required, weight=2.0, detail=f"actual={actual} required={required}")
    if "required_media_route_vlm_attempts" in expected:
        required = int(expected.get("required_media_route_vlm_attempts") or 0)
        actual = int(result.get("media_route_vlm_attempts") or route_stats.get("media_vlm_attempts") or 0)
        add("required_media_route_vlm_attempts", actual >= required, weight=1.5, detail=f"actual={actual} required={required}")
    if "required_media_route_vlm_required_by_classifier" in expected:
        required = int(expected.get("required_media_route_vlm_required_by_classifier") or 0)
        actual = int(result.get("media_route_vlm_required_by_classifier") or route_stats.get("media_vlm_required_by_classifier") or 0)
        add("required_media_route_vlm_required_by_classifier", actual >= required, weight=1.5, detail=f"actual={actual} required={required}")
    if "required_media_route_non_signal_count" in expected:
        required = int(expected.get("required_media_route_non_signal_count") or 0)
        actual = int(result.get("media_route_non_signal_count") or route_stats.get("media_non_signal_images") or 0)
        add("required_media_route_non_signal_count", actual >= required, weight=1.5, detail=f"actual={actual} required={required}")
    if "required_media_route_ocr_before_vlm_count" in expected:
        required = int(expected.get("required_media_route_ocr_before_vlm_count") or 0)
        actual = int(result.get("media_route_ocr_before_vlm_count") or 0)
        add("required_media_route_ocr_before_vlm_count", actual >= required, weight=2.0, detail=f"actual={actual} required={required}")
    if expected.get("required_media_route_no_vlm_for_non_signal") is True:
        offenders = [
            str(row.get("name"))
            for row in _as_list(result.get("media_route_jobs"))
            if isinstance(row, dict) and row.get("is_signal") is False and "vision" in _as_list(row.get("call_sequence"))
        ]
        add("required_media_route_no_vlm_for_non_signal", not offenders, weight=2.0, detail=f"offenders={offenders}")
    required_sequences = expected.get("required_media_route_call_sequences")
    if isinstance(required_sequences, dict):
        actual_sequences = {
            str(row.get("name")): [str(item) for item in _as_list(row.get("call_sequence"))]
            for row in _as_list(result.get("media_route_jobs"))
            if isinstance(row, dict)
        }
        for name, sequence in required_sequences.items():
            expected_sequence = [str(item) for item in _as_list(sequence)]
            actual_sequence = actual_sequences.get(str(name), [])
            add(
                f"required_media_route_call_sequence:{name}",
                actual_sequence == expected_sequence,
                weight=1.5,
                detail=f"actual={actual_sequence} expected={expected_sequence}",
            )
    required_job_terms = expected.get("required_media_route_job_terms")
    if isinstance(required_job_terms, dict):
        actual_jobs = {
            str(row.get("name")): " ".join(
                _clean(value).lower()
                for value in (
                    row.get("summary"),
                    row.get("text_excerpt"),
                    " ".join(str(term) for term in _as_list(row.get("visual_signal_terms"))),
                )
            )
            for row in _as_list(result.get("media_route_jobs"))
            if isinstance(row, dict)
        }
        for name, terms in required_job_terms.items():
            blob = actual_jobs.get(str(name), "")
            for term in _as_list(terms):
                needle = _clean(term).lower()
                add(f"required_media_route_job_term:{name}:{term}", bool(needle and needle in blob), weight=1.5, detail=f"term={term}")

    if "required_stage_plan_queue_strategy" in expected:
        required = str(expected.get("required_stage_plan_queue_strategy") or "").strip().lower()
        actual = str(result.get("stage_plan_queue_strategy") or _dict(result.get("stage_plan")).get("queue_strategy") or "").strip().lower()
        add("required_stage_plan_queue_strategy", actual == required, weight=1.5, detail=f"actual={actual} required={required}")
    required_stage_quotas = expected.get("required_stage_plan_stage_quotas")
    if isinstance(required_stage_quotas, dict):
        actual_quotas = _dict(result.get("stage_plan_quota_by_stage"))
        for stage_name, required_value in required_stage_quotas.items():
            actual = int(actual_quotas.get(str(stage_name)) or 0)
            required = int(required_value or 0)
            add(
                f"required_stage_plan_stage_quota:{stage_name}",
                actual == required,
                weight=1.5,
                detail=f"actual={actual} required={required}",
            )
    if "required_stage_plan_pending_media" in expected:
        required = int(expected.get("required_stage_plan_pending_media") or 0)
        actual = int(result.get("stage_plan_pending_media") or 0)
        add("required_stage_plan_pending_media", actual >= required, weight=1.0, detail=f"actual={actual} required={required}")
    if "required_stage_plan_retry_pending_signal" in expected:
        required = int(expected.get("required_stage_plan_retry_pending_signal") or 0)
        actual = int(result.get("stage_plan_retry_pending_signal") or 0)
        add("required_stage_plan_retry_pending_signal", actual >= required, weight=1.0, detail=f"actual={actual} required={required}")
    if "required_stage_plan_durable_candidates" in expected:
        required = int(expected.get("required_stage_plan_durable_candidates") or 0)
        actual = int(result.get("stage_plan_durable_candidates") or 0)
        add("required_stage_plan_durable_candidates", actual >= required, weight=1.0, detail=f"actual={actual} required={required}")
    if "required_stage_plan_lookback_expanded" in expected:
        required = bool(expected.get("required_stage_plan_lookback_expanded"))
        actual = bool(result.get("stage_plan_lookback_expanded"))
        add("required_stage_plan_lookback_expanded", actual is required, weight=1.5, detail=f"actual={actual} required={required}")
    if "required_stage_plan_signal_lookback_min_hours" in expected:
        required = int(expected.get("required_stage_plan_signal_lookback_min_hours") or 0)
        actual = int(result.get("stage_plan_signal_lookback_hours") or 0)
        add("required_stage_plan_signal_lookback_min_hours", actual >= required, weight=1.5, detail=f"actual={actual} required={required}")
    if "required_stage_plan_document_count" in expected:
        required = int(expected.get("required_stage_plan_document_count") or 0)
        actual = len(_as_list(result.get("stage_plan_document_ids")))
        add("required_stage_plan_document_count", actual >= required, weight=1.0, detail=f"actual={actual} required={required}")
    if expected.get("required_stage_plan_includes_oldest_tail") is True:
        add(
            "required_stage_plan_includes_oldest_tail",
            bool(result.get("stage_plan_includes_oldest_tail")),
            weight=2.0,
            detail=f"oldest={result.get('oldest_fixture_document_id')} planned={result.get('stage_plan_document_ids')}",
        )
    if "required_bottleneck_model_key" in expected:
        required = _clean(expected.get("required_bottleneck_model_key"))
        actual = _clean(result.get("bottleneck_model_key"))
        add("required_bottleneck_model_key", actual == required, weight=1.5, detail=f"actual={actual} required={required}")
    if "required_bottleneck_campaign_item_status" in expected:
        required = _clean(expected.get("required_bottleneck_campaign_item_status")).lower()
        actual = _clean(result.get("campaign_item_status")).lower()
        add("required_bottleneck_campaign_item_status", actual == required, weight=2.0, detail=f"actual={actual} required={required}")
    if "required_bottleneck_assigned_rail" in expected:
        required = _clean(expected.get("required_bottleneck_assigned_rail")).lower()
        actual = _clean(result.get("assigned_rail")).lower()
        add("required_bottleneck_assigned_rail", actual == required, weight=1.5, detail=f"actual={actual} required={required}")
    if "required_bottleneck_report_type" in expected:
        required = _clean(expected.get("required_bottleneck_report_type")).lower()
        actual = _clean(result.get("report_type")).lower()
        add("required_bottleneck_report_type", actual == required, weight=2.0, detail=f"actual={actual} required={required}")
    if "required_bottleneck_execute_completed_count" in expected:
        required = int(expected.get("required_bottleneck_execute_completed_count") or 0)
        actual = int(_dict(result.get("execute_result_counts")).get("completed") or 0)
        add("required_bottleneck_execute_completed_count", actual >= required, weight=2.0, detail=f"actual={actual} required={required}")
    if "required_bottleneck_execute_workspace_ready_count" in expected:
        required = int(expected.get("required_bottleneck_execute_workspace_ready_count") or 0)
        actual = int(_dict(result.get("execute_result_counts")).get("workspace_ready") or 0)
        add("required_bottleneck_execute_workspace_ready_count", actual >= required, weight=2.0, detail=f"actual={actual} required={required}")
    if "required_bottleneck_open_gap_min" in expected:
        required = int(expected.get("required_bottleneck_open_gap_min") or 0)
        actual = int(result.get("open_gap_count") or 0)
        add("required_bottleneck_open_gap_min", actual >= required, weight=1.0, detail=f"actual={actual} required={required}")
    required_artifacts = {_clean(value).lower() for value in _as_list(expected.get("required_bottleneck_report_artifact_types")) if _clean(value)}
    if required_artifacts:
        actual = {_clean(value).lower() for value in _as_list(result.get("report_artifact_types")) if _clean(value)}
        add("required_bottleneck_report_artifact_types", required_artifacts.issubset(actual), weight=1.5, detail=f"actual={sorted(actual)} required={sorted(required_artifacts)}")
    required_roles = {_clean(value).lower() for value in _as_list(expected.get("required_bottleneck_workflow_roles")) if _clean(value)}
    if required_roles:
        actual = {_clean(value).lower() for value in _as_list(result.get("workflow_roles")) if _clean(value)}
        add("required_bottleneck_workflow_roles", required_roles.issubset(actual), weight=1.5, detail=f"actual={sorted(actual)} required={sorted(required_roles)}")
    if expected.get("required_bottleneck_startup_packet"):
        startup = _dict(result.get("startup_packet"))
        role_prompts = [row for row in _as_list(startup.get("role_prompts")) if isinstance(row, dict)]
        search_queries = [row for row in _as_list(startup.get("search_queries")) if _clean(row)]
        next_calls = [row for row in _as_list(startup.get("next_mcp_calls")) if isinstance(row, dict)]
        passed = bool(startup) and len(role_prompts) >= 4 and bool(search_queries) and bool(next_calls)
        add(
            "required_bottleneck_startup_packet",
            passed,
            weight=2.0,
            detail=f"roles={len(role_prompts)} queries={len(search_queries)} next_calls={len(next_calls)}",
        )
    if "required_bottleneck_linked_evidence_min" in expected:
        required = int(expected.get("required_bottleneck_linked_evidence_min") or 0)
        actual = int(result.get("linked_evidence_count") or 0)
        add("required_bottleneck_linked_evidence_min", actual >= required, weight=2.0, detail=f"actual={actual} required={required}")
    required_linked_types = {_clean(value).lower() for value in _as_list(expected.get("required_bottleneck_linked_evidence_types")) if _clean(value)}
    if required_linked_types:
        actual = {_clean(value).lower() for value in _as_list(result.get("linked_evidence_types")) if _clean(value)}
        add("required_bottleneck_linked_evidence_types", required_linked_types.issubset(actual), weight=2.0, detail=f"actual={sorted(actual)} required={sorted(required_linked_types)}")

    for term in _as_list(expected.get("required_context_not_truncated_terms")):
        needle = _clean(term).lower()
        add(f"context_retains:{term}", bool(needle and needle in text_blob), weight=2.0, detail=f"term={term}")

    total = sum(float(row["weight"]) for row in checks) or 1.0
    earned = sum(float(row["weight"]) for row in checks if bool(row["passed"]))
    score = earned / total
    hard_required = {_clean(value) for value in _as_list(expected.get("hard_required_checks")) if _clean(value)}
    hard_failed = [
        row
        for row in checks
        if (float(row.get("weight") or 0.0) == 0.0 or str(row.get("name") or "") in hard_required)
        and not bool(row.get("passed"))
    ]
    return {
        "score": round(score, 4),
        "passed": bool(score >= EVAL_PASS_THRESHOLD and not hard_failed),
        "threshold": EVAL_PASS_THRESHOLD,
        "hard_failed": [row["name"] for row in hard_failed],
        "checks": checks,
    }


def seed_default_eval_cases(session: Session) -> dict[str, int]:
    now = utcnow()
    created = 0
    updated = 0
    skipped_missing_documents = 0
    for case in [
        *DEFAULT_EVAL_CASES,
        *DOCUMENT_QUALITY_EVAL_CASES,
        *PDF_OCR_PREP_EVAL_CASES,
        *MEDIA_ROUTE_EVAL_CASES,
        *STAGE_PLAN_EVAL_CASES,
        *BOTTLENECK_REPORT_START_EVAL_CASES,
    ]:
        slug = str(case["slug"])
        raw_document_id = case.get("document_id")
        document_id = int(raw_document_id) if str(raw_document_id or "").isdigit() else None
        missing_document = False
        if document_id is not None and session.get(Document, document_id) is None:
            skipped_missing_documents += 1
            document_id = None
            missing_document = True
        active = bool(case.get("active", True)) and not missing_document
        row = session.query(LocalAgentEvalCase).filter_by(slug=slug).first()
        if row is None:
            row = LocalAgentEvalCase(
                slug=slug,
                title=str(case["title"]),
                description=case.get("description"),
                source_label=case.get("source_label"),
                document_id=document_id,
                fixture_title=str(case["title"]),
                fixture_text=str(case.get("fixture_text") or ""),
                expected_json=dict(case.get("expected") or {}),
                tags_json=[str(tag) for tag in _as_list(case.get("tags"))],
                active=active,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            created += 1
        else:
            row.title = str(case["title"])
            row.description = case.get("description")
            row.source_label = case.get("source_label")
            row.document_id = document_id
            row.fixture_title = str(case["title"])
            row.fixture_text = str(case.get("fixture_text") or "")
            row.expected_json = dict(case.get("expected") or {})
            row.tags_json = [str(tag) for tag in _as_list(case.get("tags"))]
            row.active = active
            row.updated_at = now
            updated += 1
    session.flush()
    return {"created": created, "updated": updated, "skipped_missing_documents": skipped_missing_documents}


def _compression_to_result(session: Session, row: LocalAgentDocumentCompression) -> dict[str, Any]:
    routes = (
        session.query(LocalAgentEntityRoute)
        .filter(LocalAgentEntityRoute.compression_id == int(row.id))
        .order_by(LocalAgentEntityRoute.id.asc())
        .all()
    )
    claims = (
        session.query(LocalAgentEntityClaim)
        .filter(LocalAgentEntityClaim.compression_id == int(row.id))
        .order_by(LocalAgentEntityClaim.id.asc())
        .all()
    )
    source_doc_ids = [int(row.document_id)]
    doc = row.document
    doc_payload: dict[str, Any] = {}
    if doc is not None:
        doc_payload = get_document_for_agent(session, document_id=int(row.document_id), max_text_chars=64000)
        session.flush()
    media_rows = _case_media_rows(doc)
    signal_media = [media for media in media_rows if media.get("is_signal") is True]
    non_signal_media = [media for media in media_rows if media.get("is_signal") is False]
    doc_text = str((doc.translation_en or doc.normalized_text or doc.raw_text) if doc is not None else "")
    write_rows = (
        session.query(LocalAgentWriteDiff)
        .order_by(LocalAgentWriteDiff.created_at.desc(), LocalAgentWriteDiff.id.desc())
        .limit(250)
        .all()
    )
    writes = [
        serialize_write_diff(write)
        for write in write_rows
        if int(row.document_id) in [int(v) for v in list(write.source_document_ids_json or [])]
    ]
    return {
        "document_id": int(row.document_id),
        "status": row.status,
        "latest_compression_status": row.status,
        "latest_signal_extraction_status": row.status,
        "document_title": doc.title if doc is not None else None,
        "document_language": doc.language if doc is not None else None,
        "translation_chars": len(str(doc.translation_en or "")) if doc is not None else 0,
        "translation_en_excerpt": _safe_excerpt(doc.translation_en, max_chars=4000) if doc is not None else "",
        "text_chars": len(doc_text),
        "document_text_excerpt": _safe_excerpt(doc_text, max_chars=6000),
        "media_ocr_count": len(media_rows),
        "signal_media_count": len(signal_media),
        "non_signal_media_count": len(non_signal_media),
        "media_ocr": media_rows,
        "prep_status": doc_payload.get("prep_status"),
        "context_management": doc_payload.get("context_management"),
        "document_payload_keys": sorted(doc_payload.keys()),
        "image_ocr_status": doc_payload.get("image_ocr_status"),
        "document_disposition": row.document_disposition or ("skip" if row.status == "skipped" else None),
        "information_gain": row.information_gain,
        "information_gain_reason": row.skip_reason,
        "summary_short": row.summary_short,
        "summary_detail": row.summary_detail,
        "atomic_claims": [
            {
                "claim_text": claim.claim_text,
                "claim_kind": claim.claim_kind,
                "entities": [claim.entity_name],
                "confidence": claim.confidence,
                "routing_reason": claim.routing_reason,
                "payload": dict(claim.payload or {}),
            }
            for claim in claims
        ],
        "summary_routes": [serialize_route(route) for route in routes if route.lane == "summary"],
        "durable_routes": [serialize_route(route) for route in routes if route.lane == "durable"],
        "research_writes": [],
        "historical_write_diffs": writes,
        "historical_write_diff_count": len(writes),
        "source_document_ids": source_doc_ids,
    }


def create_eval_case_from_compression(
    session: Session,
    *,
    compression_id: int,
    expected: dict[str, Any] | None = None,
) -> LocalAgentEvalCase:
    comp = (
        session.query(LocalAgentDocumentCompression)
        .options(joinedload(LocalAgentDocumentCompression.document).joinedload(Document.source))
        .filter(LocalAgentDocumentCompression.id == int(compression_id))
        .first()
    )
    if comp is None:
        raise ValueError("Compression not found.")
    doc = comp.document
    slug_base = f"doc-{int(comp.document_id)}-{_slugify(doc.title if doc else comp.summary_short)}"
    slug = slug_base[:128]
    suffix = 2
    while (
        session.query(LocalAgentEvalCase)
        .filter(LocalAgentEvalCase.slug == slug, LocalAgentEvalCase.document_id != int(comp.document_id))
        .first()
    ):
        suffix += 1
        slug = f"{slug_base[:116]}-{suffix}"
    now = utcnow()
    defaults = {
        "document_disposition": comp.document_disposition or ("skip" if comp.status == "skipped" else None),
        "information_gain": comp.information_gain,
        "allow_research_writes": False,
    }
    defaults.update({k: v for k, v in dict(expected or {}).items() if v not in (None, "", [])})
    row = session.query(LocalAgentEvalCase).filter_by(slug=slug).first()
    if row is None:
        row = LocalAgentEvalCase(
            slug=slug,
            title=_doc_title(doc),
            description=f"Regression case created from compression #{int(comp.id)}.",
            source_label=_source_name(doc),
            document_id=int(comp.document_id),
            fixture_title=doc.title if doc else None,
            fixture_text=None,
            expected_json=defaults,
            tags_json=["from-review-surface"],
            active=True,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    else:
        row.expected_json = defaults
        row.updated_at = now
    session.flush()
    return row


def _latest_compression_for_case(session: Session, case: LocalAgentEvalCase) -> LocalAgentDocumentCompression | None:
    if case.document_id is None:
        return None
    return (
        session.query(LocalAgentDocumentCompression)
        .filter(LocalAgentDocumentCompression.document_id == int(case.document_id))
        .order_by(LocalAgentDocumentCompression.updated_at.desc(), LocalAgentDocumentCompression.id.desc())
        .first()
    )


def _safe_excerpt(value: object, *, max_chars: int = 4000) -> str:
    text = str(value or "")
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 18)].rstrip() + "\n...[truncated]"


def _case_media_rows(doc: Document | None) -> list[dict[str, Any]]:
    if doc is None:
        return []
    rows: list[dict[str, Any]] = []
    for row in _as_list((doc.extra or {}).get("image_ocr")):
        if not isinstance(row, dict):
            continue
        signal = _image_ocr_row_signal(row)
        inferred: dict[str, Any] = {}
        if classify_ocr_media_output is not None:
            try:
                detail = row.get("detail") if isinstance(row.get("detail"), dict) else {}
                assessment = classify_ocr_media_output(
                    f"{row.get('summary') or ''}\n{row.get('text') or ''}",
                    route=(detail or {}).get("route") if isinstance(detail, dict) else None,
                    layout_details=(detail or {}).get("layout_details") or (detail or {}).get("json_result") if isinstance(detail, dict) else None,
                    source_context={
                        "source_name": doc.source.name if isinstance(doc.source, Source) else None,
                        "document_url": doc.url or doc.canonical_url,
                        "document_title": doc.title,
                        "original_url": row.get("original_url"),
                    },
                )
                inferred = assessment.as_dict() if hasattr(assessment, "as_dict") else dict(assessment or {})
            except Exception:
                inferred = {}
        if signal.get("is_signal") is False and not row.get("media_kind"):
            media_kind = "non_signal_image"
        else:
            media_kind = row.get("media_kind") or inferred.get("media_kind")
        ocr_quality = row.get("ocr_quality") or inferred.get("ocr_quality")
        needs_vlm = row.get("needs_vlm")
        if needs_vlm is None:
            needs_vlm = False if signal.get("is_signal") is False else inferred.get("needs_vlm")
        rows.append(
            {
                "attachment_id": row.get("attachment_id"),
                "backend": row.get("backend"),
                "model": row.get("model"),
                "confidence": row.get("confidence"),
                "media_kind": media_kind,
                "ocr_quality": ocr_quality,
                "needs_vlm": needs_vlm,
                "source_risk": row.get("source_risk") or inferred.get("source_risk"),
                "visual_signal_terms": _as_list(row.get("visual_signal_terms") or inferred.get("visual_signal_terms")),
                "layout_labels": _as_list(row.get("layout_labels") or inferred.get("layout_labels")),
                "signal": signal.get("signal"),
                "is_signal": bool(signal.get("is_signal")),
                "signal_reason": signal.get("reason"),
                "summary": _safe_excerpt(row.get("summary"), max_chars=1200),
                "text_en": _safe_excerpt(row.get("text_en"), max_chars=2400),
                "translation": row.get("translation") if isinstance(row.get("translation"), dict) else {},
                "text": _safe_excerpt(row.get("text"), max_chars=2400),
            }
        )
    return rows


def _latest_compression_result(session: Session, case: LocalAgentEvalCase) -> tuple[LocalAgentDocumentCompression | None, dict[str, Any]]:
    comp = _latest_compression_for_case(session, case)
    if comp is None:
        return None, {}
    return comp, _compression_to_result(session, comp)


def _trace_dir() -> Path:
    root = Path(getattr(settings, "data_dir", Path("data")))
    path = root / "eval_traces" / "local_agent_document_quality"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_eval_trace(*, case: LocalAgentEvalCase, mode: str, result: dict[str, Any], score: dict[str, Any]) -> str:
    path = _trace_dir() / f"{utcnow().strftime('%Y%m%d')}.jsonl"
    payload = {
        "created_at": _dt(utcnow()),
        "case_id": int(case.id),
        "slug": case.slug,
        "mode": mode,
        "document_id": int(case.document_id) if case.document_id is not None else None,
        "status": result.get("status"),
        "latest_compression_status": result.get("latest_compression_status"),
        "score": score,
        "result": result,
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    return str(path)


def _document_quality_result(session: Session, case: LocalAgentEvalCase) -> dict[str, Any]:
    if case.document_id is None:
        raise ValueError("Document-quality eval requires a document_id.")
    doc_payload = get_document_for_agent(session, document_id=int(case.document_id), max_text_chars=64000)
    session.flush()
    doc = (
        session.query(Document)
        .options(joinedload(Document.source))
        .filter(Document.id == int(case.document_id))
        .first()
    )
    if doc is None:
        raise ValueError("Document not found.")
    attachments = session.query(Attachment).filter(Attachment.document_id == int(doc.id)).order_by(Attachment.id.asc()).all()
    media_rows = _case_media_rows(doc)
    signal_media = [row for row in media_rows if row.get("is_signal") is True]
    non_signal_media = [row for row in media_rows if row.get("is_signal") is False]
    comp, comp_result = _latest_compression_result(session, case)
    text = str(doc_payload.get("text") or "")
    result: dict[str, Any] = {
        "document_id": int(doc.id),
        "document_title": doc.title,
        "document_language": doc.language,
        "language": doc.language,
        "language_confidence": doc.language_confidence,
        "source_name": _source_name(doc),
        "text_chars": len(text),
        "document_text_excerpt": _safe_excerpt(text, max_chars=6000),
        "translation_chars": len(str(doc.translation_en or "")),
        "translation_backend": doc.translation_backend,
        "translation_status": (doc.extra or {}).get("local_agent_translation_status") if isinstance(doc.extra, dict) else None,
        "translation_en_excerpt": _safe_excerpt(doc.translation_en, max_chars=4000),
        "prep_status": doc_payload.get("prep_status"),
        "context_management": doc_payload.get("context_management"),
        "attachment_count": len(attachments),
        "image_attachment_count": sum(1 for attachment in attachments if str(attachment.kind or "").lower() == "image"),
        "media_ocr_count": len(media_rows),
        "signal_media_count": len(signal_media),
        "non_signal_media_count": len(non_signal_media),
        "media_ocr": media_rows,
        "document_payload_keys": sorted(doc_payload.keys()),
        "image_ocr_status": doc_payload.get("image_ocr_status"),
        "latest_compression_id": int(comp.id) if comp is not None else None,
        "latest_signal_extraction_id": int(comp.id) if comp is not None else None,
        "latest_compression_status": comp.status if comp is not None else None,
        "latest_signal_extraction_status": comp.status if comp is not None else None,
        "status": comp.status if comp is not None else "no_compression",
    }
    result.update({k: v for k, v in comp_result.items() if k not in result or v not in (None, "", [], {})})
    return result


def _dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _cleanup_durable_output_eval_leaks(
    session: Session,
    *,
    company_slug: str,
) -> None:
    """Best-effort cleanup for SQLite test engines whose in-memory pool shares one connection."""
    company = session.query(Company).filter_by(slug=company_slug).first()
    if company is not None:
        session.query(InstitutionalObservation).filter(InstitutionalObservation.company_id == int(company.id)).delete(synchronize_session=False)
        session.query(AnalystEvidence).filter(AnalystEvidence.company_id == int(company.id)).delete(synchronize_session=False)
    session.flush()


def _eval_pdf_bytes(text: str) -> bytes:
    """Build a tiny one-page PDF for the service-layer PDF OCR-prep eval."""
    try:
        from reportlab.lib.pagesizes import letter  # type: ignore
        from reportlab.pdfgen import canvas  # type: ignore
    except Exception:
        try:
            import fitz  # type: ignore
        except Exception as exc:  # pragma: no cover - optional runtime packages
            try:
                from pypdf import PdfWriter  # type: ignore
            except Exception as pypdf_exc:  # pragma: no cover - project dependency should be present
                raise RuntimeError("PDF OCR-prep eval requires reportlab, PyMuPDF, or pypdf.") from pypdf_exc
            writer = PdfWriter()
            writer.add_blank_page(width=612, height=792)
            writer.add_metadata({"/Title": "Thoth PDF OCR prep fixture", "/Subject": text[:220]})
            buffer = io.BytesIO()
            writer.write(buffer)
            return buffer.getvalue()
        doc = fitz.open()
        page = doc.new_page(width=612, height=792)
        page.insert_text((72, 96), text[:220], fontsize=12)
        try:
            return bytes(doc.tobytes())
        finally:
            doc.close()

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=letter)
    pdf.setFont("Helvetica", 12)
    y = 720
    for line in re.findall(r".{1,82}(?:\s+|$)", text.strip() or "Thoth PDF OCR prep fixture."):
        pdf.drawString(72, y, line.strip())
        y -= 18
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def _path_inside_root(path: str | None, *, root: Path) -> bool:
    if not path:
        return False
    try:
        resolved = (root / str(path)).resolve(strict=False)
        resolved.relative_to(root.resolve(strict=False))
    except Exception:
        return False
    return True


def _pdf_ocr_prep_eval_result(session: Session, case: LocalAgentEvalCase) -> dict[str, Any]:
    """Validate that PDFs enter OCR prep through the Thoth object-store/service rail."""
    expected = dict(case.expected_json or {})
    fixture = _dict(expected.get("pdf_ocr_prep_fixture"))
    if not fixture:
        raise ValueError("PDF OCR-prep eval requires expected.pdf_ocr_prep_fixture.")

    from thoth_terminal.document_readability import maybe_hydrate_pdf_attachment_text
    from thoth_terminal.local_agent_service import MEDIA_TRANSCRIPTION_JOB_KIND, enqueue_pending_media_transcription_jobs
    from thoth_terminal.object_store import put_bytes

    now = utcnow()
    scratch_factory = sessionmaker(bind=session.get_bind(), expire_on_commit=False)
    scratch = scratch_factory()
    original_objects_dir = settings.objects_dir
    with tempfile.TemporaryDirectory(prefix="thoth-pdf-ocr-prep-eval-") as tmp_dir:
        eval_root = Path(tmp_dir)
        settings.objects_dir = eval_root
        try:
            pdf_text = str(fixture.get("pdf_text") or case.fixture_text or "Thoth PDF OCR prep fixture.")
            pdf_bytes = _eval_pdf_bytes(pdf_text)
            pdf_object_path, pdf_sha = put_bytes(pdf_bytes, ext=".pdf")
            source = Source(
                name=f"local_agent_eval_pdf_ocr_prep_{int(case.id)}_{now.strftime('%Y%m%d%H%M%S%f')}",
                category="eval",
                priority=1,
                config={"connector": "local_agent_eval", "case_id": int(case.id), "rail": "pdf_ocr_prep"},
                created_at=now,
            )
            scratch.add(source)
            scratch.flush()
            document = Document(
                kind="document",
                source_id=int(source.id),
                external_id=f"pdf-ocr-prep-eval-{int(case.id)}-{now.strftime('%Y%m%d%H%M%S%f')}",
                canonical_url=str(fixture.get("url") or "") or None,
                url=str(fixture.get("url") or "") or None,
                title=str(case.title or "PDF OCR Prep Eval")[:1024],
                author="local_agent_eval",
                published_at=now,
                ingested_at=now,
                raw_text=str(case.fixture_text or "Metadata-only PDF fixture."),
                normalized_text=str(case.fixture_text or "Metadata-only PDF fixture."),
                language="en",
                language_confidence=1.0,
                content_hash=hashlib.sha256(f"{case.slug}:{pdf_sha}".encode("utf-8")).hexdigest(),
                extra={"content_kind": "pdf", "is_pdf": True, "pdf_extracted": False, "eval_case_id": int(case.id)},
                importance=1,
                triage_state="NEW",
                saved=False,
            )
            scratch.add(document)
            scratch.flush()
            pdf_attachment = Attachment(
                document_id=int(document.id),
                kind="pdf",
                original_url=document.url,
                content_type="application/pdf",
                object_path=pdf_object_path,
                sha256=pdf_sha,
                created_at=now,
            )
            scratch.add(pdf_attachment)
            scratch.flush()

            hydration = maybe_hydrate_pdf_attachment_text(scratch, document, force=True)
            enqueue = enqueue_pending_media_transcription_jobs(scratch, limit=8, render_readable_pdf_pages=True)
            scratch.flush()
            payload = get_document_for_agent(scratch, document_id=int(document.id), max_text_chars=12000)
            scratch.flush()

            attachments = scratch.query(Attachment).filter(Attachment.document_id == int(document.id)).order_by(Attachment.id.asc()).all()
            image_attachments = [
                row
                for row in attachments
                if str(row.kind or "").lower() == "image" or str(row.content_type or "").lower().startswith("image/")
            ]
            media_jobs = (
                scratch.query(Job)
                .filter(Job.kind == MEDIA_TRANSCRIPTION_JOB_KIND)
                .filter(Job.payload["document_id"].as_integer() == int(document.id))
                .order_by(Job.id.asc())
                .all()
            )
            doc_extra = dict(document.extra or {})
            page_render = _dict(_dict(hydration).get("page_image_render")) or _dict(doc_extra.get("pdf_page_image_render"))
            object_paths = [str(row.object_path or "") for row in attachments if row.object_path]
            page_original_urls = [str(row.original_url or "") for row in image_attachments]
            return {
                "status": "pdf_ocr_prep_validated",
                "document_id": int(document.id),
                "document_title": document.title,
                "source_name": source.name,
                "pdf_text_hydration": hydration,
                "pdf_page_image_render": page_render,
                "pdf_page_render_status": page_render.get("status"),
                "pdf_page_rendered_attachments": int(page_render.get("stored_attachments") or 0),
                "pdf_attachment_count": sum(1 for row in attachments if str(row.kind or "").lower() == "pdf"),
                "pdf_image_attachment_count": len(image_attachments),
                "pdf_media_jobs": len(media_jobs),
                "pdf_media_job_payloads": [dict(row.payload or {}) for row in media_jobs],
                "enqueue_result": enqueue,
                "prep_status": payload.get("prep_status"),
                "document_payload_text_excerpt": _safe_excerpt(payload.get("text"), max_chars=2000),
                "page_original_urls": page_original_urls,
                "object_paths": object_paths,
                "object_paths_relative_to_eval_root": all(_path_inside_root(path, root=eval_root) for path in object_paths),
                "page_original_url_marker": any("page=" in url for url in page_original_urls),
                "rolled_back": True,
                "object_store_scope": "temporary_eval_root",
            }
        finally:
            scratch.rollback()
            scratch.close()
            settings.objects_dir = original_objects_dir


def _media_route_eval_result(session: Session, case: LocalAgentEvalCase) -> dict[str, Any]:
    """Validate OCR-first route-mode behavior without live model variance."""
    expected = dict(case.expected_json or {})
    fixture = _dict(expected.get("media_route_fixture"))
    jobs = [dict(row) for row in _as_list(fixture.get("jobs")) if isinstance(row, dict)]
    if not jobs:
        raise ValueError("Media-route eval requires expected.media_route_fixture.jobs.")

    from thoth_terminal import local_agent_worker as worker

    cfg = worker.build_config(
        [
            "--thoth-base-url",
            "http://local-agent-eval.invalid",
            "--thoth-token",
            "eval-token",
            "--media-transcription-enabled",
            "--media-transcription-mode",
            "route",
            "--media-ocr-backend",
            "cli",
            "--media-ocr-model",
            "ggml-org/GLM-OCR-GGUF:Q8_0",
            "--media-vision-model",
            "qwen35-4b-gguf-vlm",
            "--once",
        ]
    )
    stats = worker._initial_run_stats(cfg)
    by_name = {str(row.get("name") or f"job_{idx + 1}"): row for idx, row in enumerate(jobs)}
    call_sequences: dict[str, list[str]] = {name: [] for name in by_name}
    vision_inputs: dict[str, dict[str, Any]] = {}

    def route_for_path(path: Path) -> dict[str, Any]:
        return dict(_dict(by_name[path.stem].get("route")))

    def run_ocr(_cfg: Any, image_path: Path) -> dict[str, Any]:
        name = image_path.stem
        row = by_name[name]
        call_sequences[name].append("ocr")
        return {
            "text": str(row.get("ocr_text") or ""),
            "backend": str(row.get("ocr_backend") or "glm-ocr"),
            "model": str(row.get("ocr_model") or "ggml-org/GLM-OCR-GGUF:Q8_0"),
            "detail": dict(_dict(row.get("ocr_detail"))),
        }

    def run_vision(
        _cfg: Any,
        image_path: Path,
        _job: dict[str, Any],
        *,
        route: dict[str, Any] | None = None,
        ocr_text: str | None = None,
        ocr_assessment: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        name = image_path.stem
        row = by_name[name]
        call_sequences[name].append("vision")
        vision_inputs[name] = {
            "route": dict(route or {}),
            "ocr_text_chars": len(str(ocr_text or "")),
            "ocr_assessment": dict(ocr_assessment or {}),
        }
        return {
            "text": str(row.get("vision_text") or "Visual description unavailable."),
            "backend": str(row.get("vision_backend") or "qwen-vlm"),
            "model": str(row.get("vision_model") or "qwen35-4b-gguf-vlm"),
            "detail": {"prompt_version": worker.MEDIA_VISION_PROMPT_VERSION},
        }

    original_route = worker._media_route
    original_ocr = worker._run_media_ocr
    original_vision = worker._run_media_vision
    result_rows: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="thoth-media-route-eval-") as tmp_dir:
        tmp_root = Path(tmp_dir)
        try:
            worker._media_route = route_for_path  # type: ignore[assignment]
            worker._run_media_ocr = run_ocr  # type: ignore[assignment]
            worker._run_media_vision = run_vision  # type: ignore[assignment]
            for idx, row in enumerate(jobs):
                name = str(row.get("name") or f"job_{idx + 1}")
                image_path = tmp_root / f"{_slugify(name, default=f'job-{idx + 1}')}.png"
                by_name[image_path.stem] = row
                call_sequences.setdefault(image_path.stem, [])
                image_path.write_bytes(b"synthetic media route eval image")
                job_payload = dict(_dict(row.get("job")))
                job_payload.setdefault("job_id", 10_000 + idx)
                job_payload.setdefault("document_id", int(case.document_id or 0) or 1)
                job_payload.setdefault("attachment_id", 20_000 + idx)
                job_payload.setdefault("content_type", "image/png")
                transcription = worker._transcribe_media_job(cfg, job_payload, image_path, stats)
                detail = _dict(transcription.get("detail"))
                result_rows.append(
                    {
                        "name": name,
                        "source_name": job_payload.get("source_name"),
                        "route": _dict(row.get("route")),
                        "call_sequence": list(call_sequences.get(image_path.stem, [])),
                        "backend": transcription.get("backend"),
                        "model": transcription.get("model"),
                        "confidence": transcription.get("confidence"),
                        "media_kind": detail.get("media_kind"),
                        "ocr_quality": detail.get("ocr_quality"),
                        "needs_vlm": detail.get("needs_vlm"),
                        "is_signal": detail.get("is_signal"),
                        "signal": detail.get("signal"),
                        "signal_reason": detail.get("signal_reason"),
                        "visual_signal_terms": _as_list(detail.get("visual_signal_terms")),
                        "summary": _safe_excerpt(transcription.get("summary"), max_chars=800),
                        "text_excerpt": _safe_excerpt(transcription.get("text"), max_chars=1200),
                        "vision_input": vision_inputs.get(image_path.stem, {}),
                    }
                )
        finally:
            worker._media_route = original_route  # type: ignore[assignment]
            worker._run_media_ocr = original_ocr  # type: ignore[assignment]
            worker._run_media_vision = original_vision  # type: ignore[assignment]

    ocr_before_vlm = 0
    for row in result_rows:
        sequence = [str(item) for item in _as_list(row.get("call_sequence"))]
        if "ocr" in sequence and "vision" in sequence and sequence.index("ocr") < sequence.index("vision"):
            ocr_before_vlm += 1
    return {
        "status": "media_route_validated",
        "mode": "route",
        "prompt_version": cfg.prompt_version,
        "media_vision_prompt_version": worker.MEDIA_VISION_PROMPT_VERSION,
        "worker_loadout_version": worker.WORKER_LOADOUT_VERSION,
        "media_route_jobs": result_rows,
        "media_route_stats": dict(stats),
        "media_route_ocr_attempts": int(stats.get("media_ocr_attempts") or 0),
        "media_route_vlm_attempts": int(stats.get("media_vlm_attempts") or 0),
        "media_route_vlm_required_by_classifier": int(stats.get("media_vlm_required_by_classifier") or 0),
        "media_route_non_signal_count": int(stats.get("media_non_signal_images") or 0),
        "media_route_ocr_before_vlm_count": ocr_before_vlm,
        "rolled_back": True,
    }


def _stage_plan_eval_result(session: Session, case: LocalAgentEvalCase) -> dict[str, Any]:
    """Validate staged scheduling quotas in an isolated synthetic eval database."""
    expected = dict(case.expected_json or {})
    fixture = _dict(expected.get("stage_plan_fixture"))
    if not fixture:
        raise ValueError("Stage-plan eval requires expected.stage_plan_fixture.")

    from thoth_terminal.local_agent_service import (
        MEDIA_TRANSCRIPTION_JOB_KIND,
        TEXT_READY_RETRY_STATUS,
        local_agent_stage_plan,
    )

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    scratch_factory = sessionmaker(bind=engine, expire_on_commit=False)
    scratch = scratch_factory()
    now = utcnow()
    try:
        source = Source(
            name=f"local_agent_eval_stage_plan_{int(case.id)}",
            category="eval",
            priority=1,
            config={"connector": "local_agent_eval", "case_id": int(case.id), "rail": "stage_plan"},
            created_at=now,
        )
        scratch.add(source)
        scratch.flush()

        ages = [float(value) for value in _as_list(fixture.get("document_age_hours")) if str(value).strip()]
        if not ages:
            ages = [1.0, 2.0, 3.0, 23.0, 25.0, 47.0]
        documents: list[Document] = []
        for idx, age_hours in enumerate(ages):
            published = now - timedelta(hours=float(age_hours))
            doc = Document(
                kind="news",
                source_id=int(source.id),
                external_id=f"stage-plan-eval-{int(case.id)}-{idx}",
                canonical_url=None,
                url=f"https://example.com/eval/stage-plan/{int(case.id)}/{idx}",
                title=f"Stage-plan eval fixture {idx}",
                author="local_agent_eval",
                published_at=published,
                ingested_at=published,
                raw_text=f"Stage-plan eval text {idx}. Age hours: {age_hours}.",
                normalized_text=f"Stage-plan eval text {idx}. Age hours: {age_hours}.",
                language="en",
                language_confidence=1.0,
                translation_en=None,
                translation_backend=None,
                translation_model=None,
                translation_glossary_version=None,
                translated_at=None,
                triage_state="NEW",
                saved=True,
                seen_at=None,
                opened_at=None,
                saved_at=published,
                actioned_at=None,
                ignored_at=None,
                triage_updated_at=None,
                content_hash=hashlib.sha256(f"{case.slug}:{idx}:{age_hours}".encode("utf-8")).hexdigest(),
                extra={"eval_case_id": int(case.id), "age_hours": age_hours},
                importance=100,
                storage_tier="hot",
                cold_archive_id=None,
            )
            documents.append(doc)
        scratch.add_all(documents)
        scratch.flush()

        media_jobs = max(0, int(fixture.get("media_jobs") or 0))
        for idx in range(media_jobs):
            doc = documents[idx % len(documents)]
            scratch.add(
                Job(
                    kind=MEDIA_TRANSCRIPTION_JOB_KIND,
                    status="pending",
                    priority=5,
                    payload={"document_id": int(doc.id), "attachment_id": 90_000 + idx, "eval_case_id": int(case.id)},
                    dedupe_key=f"stage-plan-eval-media-{int(case.id)}-{idx}",
                    attempts=0,
                    max_attempts=5,
                    run_after=None,
                    locked_at=None,
                    started_at=None,
                    finished_at=None,
                    last_error=None,
                    created_at=now,
                    updated_at=now,
                )
            )

        retry_pending = max(0, int(fixture.get("retry_pending_compressions") or 0))
        for idx in range(retry_pending):
            doc = documents[(idx + media_jobs) % len(documents)]
            scratch.add(
                LocalAgentDocumentCompression(
                    run_id=None,
                    document_id=int(doc.id),
                    model="stage-plan-eval",
                    prompt_version=f"stage-plan-eval-{int(case.id)}-{idx}",
                    summary_short="Retry pending after media preparation.",
                    summary_detail=None,
                    atomic_claims_json=[],
                    entity_refs_json=[],
                    supply_chain_refs_json=[],
                    technology_refs_json=[],
                    unresolved_entities_json=[],
                    confidence=0.0,
                    information_gain="none",
                    document_disposition="skip",
                    skip_reason="eval_retry_pending",
                    status=TEXT_READY_RETRY_STATUS,
                    error=None,
                    created_at=now,
                    updated_at=now,
                )
            )

        durable_routes = max(0, int(fixture.get("durable_routes") or 0))
        for idx in range(durable_routes):
            doc = documents[(idx + media_jobs + retry_pending) % len(documents)]
            scratch.add(
                LocalAgentEntityRoute(
                    run_id=None,
                    compression_id=None,
                    document_id=int(doc.id),
                    lane="durable",
                    entity_name=f"Stage Plan Durable Entity {idx}",
                    entity_type="company",
                    entity_role="subject",
                    summary_relevance=False,
                    summary_confidence=None,
                    durable_relevance=True,
                    durable_confidence=0.9,
                    durable_action="hold",
                    materiality="high",
                    novelty="new",
                    temporal_posture="forward_looking",
                    evidence_tier="eval",
                    resolution_status="resolved",
                    new_information="Synthetic durable candidate for stage-plan quota eval.",
                    rationale="Stage-plan eval fixture.",
                    claim_refs_json=[],
                    source_claim_indexes_json=[],
                    destination_type=None,
                    destination_id=None,
                    payload={"eval_case_id": int(case.id)},
                    created_at=now,
                )
            )
        scratch.flush()

        params = {
            "media_limit": int(fixture.get("media_limit") or 2),
            "signal_limit": int(fixture.get("signal_limit") or 4),
            "durable_limit": int(fixture.get("durable_limit") or 1),
            "lookback_hours": int(fixture.get("lookback_hours") or 24),
            "queue_strategy": str(fixture.get("queue_strategy") or "balanced"),
        }
        plan = local_agent_stage_plan(scratch, **params)
        stages = {str(row.get("stage")): dict(row) for row in _as_list(plan.get("stages")) if isinstance(row, dict)}
        document_ids = [int(value) for value in _as_list(_dict(plan.get("batch_plan")).get("document_ids")) if str(value).isdigit()]
        oldest_doc = min(documents, key=lambda row: row.published_at or now)
        return {
            "status": "stage_plan_validated",
            "stage_plan": plan,
            "stage_plan_params": params,
            "stage_plan_queue_strategy": plan.get("queue_strategy"),
            "stage_plan_signal_lookback_hours": int(plan.get("signal_lookback_hours") or 0),
            "stage_plan_lookback_expanded": bool(_dict(plan.get("lookback_expansion")).get("expanded")),
            "stage_plan_lookback_expansion": _dict(plan.get("lookback_expansion")),
            "stage_plan_quota_by_stage": {name: int(row.get("quota") or 0) for name, row in stages.items()},
            "stage_plan_pending_media": int(_dict(stages.get("media_prep")).get("pending_jobs") or 0),
            "stage_plan_retry_pending_signal": int(_dict(stages.get("signal_extraction")).get("retry_pending_compressions") or 0),
            "stage_plan_durable_candidates": int(_dict(stages.get("durable_reasoning")).get("candidate_routes") or 0),
            "stage_plan_document_ids": document_ids,
            "fixture_document_ids": [int(row.id) for row in documents],
            "oldest_fixture_document_id": int(oldest_doc.id),
            "stage_plan_includes_oldest_tail": int(oldest_doc.id) in set(document_ids),
            "rolled_back": True,
            "object_store_scope": "isolated_sqlite_eval",
        }
    finally:
        scratch.rollback()
        scratch.close()
        engine.dispose()


def _install_eval_research_report(
    session: Session,
    *,
    company: Company,
    issuer: object | None,
    subject: object | None,
    title: str,
    content_md: str,
    now: Any,
) -> ResearchReport:
    subject_id = getattr(subject, "id", None)
    subject_key = str(getattr(subject, "subject_key", None) or f"company:{int(company.id)}")
    report = ResearchReport(
        subject_id=int(subject_id) if subject_id is not None else None,
        slug=f"{company.slug}-eval-report",
        subject_key=subject_key,
        issuer_id=int(getattr(issuer, "id")) if getattr(issuer, "id", None) is not None else None,
        company_id=int(company.id),
        title=title[:255] or f"{company.name} Eval Research Report",
        status="active",
        latest_published_version_id=None,
        latest_subject_slug=getattr(subject, "slug", None),
        extra={"source": "local_agent_eval"},
        created_at=now,
        updated_at=now,
    )
    session.add(report)
    session.flush()
    version = ResearchReportVersion(
        report_id=int(report.id),
        version=1,
        status="published",
        trigger_kind="local_agent_eval",
        trigger_label="Existing durable context fixture",
        trigger_payload={"source": "local_agent_eval"},
        change_fingerprint=None,
        content_json={"eval_existing_context": True},
        content_md=content_md,
        coverage_json={},
        as_of_at=now,
        error=None,
        created_at=now,
        updated_at=now,
        published_at=now,
    )
    session.add(version)
    session.flush()
    report.latest_published_version_id = int(version.id)
    report.updated_at = now
    session.flush()
    return report


def _install_eval_institutional_evidence(
    session: Session,
    specs: list[Any],
    *,
    source: Source,
    document: Document,
    company: Company,
    issuer: object | None,
    subject: object | None,
    now: Any,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for idx, spec_obj in enumerate(specs, start=1):
        if not isinstance(spec_obj, dict):
            continue
        spec = dict(spec_obj)
        payload = spec.get("payload") if isinstance(spec.get("payload"), dict) else {}
        row = InstitutionalEvidenceItem(
            evidence_key=str(spec.get("evidence_key") or f"eval:institutional-evidence:{int(document.id)}:{idx}")[:255],
            source_id=int(source.id),
            source_profile_id=None,
            document_id=int(document.id) if spec.get("same_source_document", True) else None,
            subject_id=int(getattr(subject, "id")) if getattr(subject, "id", None) is not None else None,
            issuer_id=int(getattr(issuer, "id")) if getattr(issuer, "id", None) is not None else None,
            company_id=int(company.id),
            site_id=None,
            program_id=None,
            title=str(spec.get("title") or "Eval institutional evidence")[:512],
            evidence_kind=str(spec.get("evidence_kind") or "analyst_context")[:64],
            provenance_summary=str(spec.get("provenance_summary") or spec.get("summary") or "")[:4000] or None,
            url=str(spec.get("url") or document.url or "")[:2048] or None,
            canonical_url=str(spec.get("canonical_url") or spec.get("url") or document.canonical_url or document.url or "")[:2048] or None,
            published_at=now,
            observed_at=now,
            rights_class=str(spec.get("rights_class") or "internal")[:64],
            review_required=bool(spec.get("review_required", False)),
            confidence=float(spec.get("confidence") or 0.8),
            payload={
                **payload,
                "metric_key": spec.get("metric_key") or payload.get("metric_key"),
                "period": spec.get("period") or payload.get("period"),
                "value_text": spec.get("value_text") or payload.get("value_text"),
                "summary": spec.get("summary") or payload.get("summary"),
            },
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.flush()
        rows.append(
            {
                "id": int(row.id),
                "evidence_key": row.evidence_key,
                "document_id": int(row.document_id) if row.document_id is not None else None,
                "company_id": int(row.company_id) if row.company_id is not None else None,
                "subject_id": int(row.subject_id) if row.subject_id is not None else None,
                "title": row.title,
                "evidence_kind": row.evidence_kind,
                "provenance_summary": row.provenance_summary,
                "confidence": row.confidence,
            }
        )
    return rows


def _durable_output_eval_result(session: Session, case: LocalAgentEvalCase) -> dict[str, Any]:
    """Validate model-delta output against the real durable service layer without committing fixtures."""
    expected = dict(case.expected_json or {})
    fixture = _dict(expected.get("durable_output_fixture"))
    if not fixture:
        raise ValueError("Durable-output eval requires expected.durable_output_fixture.")

    from thoth_terminal.decision_spine_service import ensure_decision_subject, serialize_analyst_evidence, upsert_analyst_evidence
    from thoth_terminal.foundation_service import ensure_company_issuer
    from thoth_terminal.local_agent_service import durable_candidate_context
    from thoth_terminal.local_agent_worker import _decision_payloads_for_analysis_result, _initial_run_stats, build_config

    now = utcnow()
    slug_suffix = f"{int(case.id)}-{now.strftime('%Y%m%d%H%M%S%f')}"
    source_name = f"local_agent_eval_durable_output_{slug_suffix}"
    company_slug = f"eval-durable-output-{slug_suffix}"
    document_external_id = f"durable-output-eval-{slug_suffix}"
    company_spec = _dict(fixture.get("company"))
    document_spec = _dict(fixture.get("document"))
    analysis_result = _dict(fixture.get("analysis_result"))
    if not analysis_result:
        raise ValueError("Durable-output eval fixture must include expected.durable_output_fixture.analysis_result.")
    materialize_outputs = bool(fixture.get("materialize_outputs", True))

    scratch_factory = sessionmaker(bind=session.get_bind(), expire_on_commit=False)
    scratch = scratch_factory()
    try:
        source = Source(
            name=source_name,
            category=str(document_spec.get("source_category") or "analyst")[:64],
            priority=1,
            polling_interval_seconds=None,
            config={"connector": "local_agent_eval", "case_id": int(case.id)},
            created_at=now,
        )
        company = Company(
            slug=company_slug,
            name=str(company_spec.get("name") or case.title or "Durable Output Eval")[:255],
            ticker=str(company_spec.get("ticker") or "EDUR").strip().upper()[:32] or None,
            exchange=str(company_spec.get("exchange") or "")[:64] or None,
            region=str(company_spec.get("region") or "US")[:64] or None,
            sector=str(company_spec.get("sector") or "")[:128] or None,
            industry=str(company_spec.get("industry") or "")[:128] or None,
            created_at=now,
        )
        scratch.add_all([source, company])
        scratch.flush()
        issuer = ensure_company_issuer(scratch, company)
        subject = ensure_decision_subject(scratch, company=company, issuer=issuer)
        scratch.flush()
        report_spec = _dict(fixture.get("existing_research_report"))
        _install_eval_research_report(
            scratch,
            company=company,
            issuer=issuer,
            subject=subject,
            title=str(report_spec.get("title") or f"{company.name} existing durable context"),
            content_md=str(
                report_spec.get("content_md")
                or (
                    f"# {company.name} Existing Durable Context\n\n"
                    "Existing durable research context fixture. This report body is intentionally present so "
                    "durable reasoning must inspect the current company artifact before deciding whether a "
                    "signal is novel, duplicate, an update, or a conflict."
                )
            ),
            now=now,
        )

        fixture_text = str(case.fixture_text or document_spec.get("text") or case.description or case.title or "")
        document = Document(
            kind=str(document_spec.get("kind") or "eval_fixture")[:64],
            source_id=int(source.id),
            external_id=document_external_id,
            canonical_url=str(document_spec.get("url") or "") or None,
            url=str(document_spec.get("url") or "") or None,
            title=str(document_spec.get("title") or case.title)[:1024],
            author=str(document_spec.get("author") or "")[:255] or None,
            published_at=now,
            ingested_at=now,
            raw_text=fixture_text,
            normalized_text=fixture_text,
            language=str(document_spec.get("language") or "en")[:16],
            language_confidence=1.0,
            content_hash=hashlib.sha256(f"{case.slug}:{slug_suffix}".encode("utf-8")).hexdigest(),
            extra={"eval_case_id": int(case.id), "eval_mode": "durable_output"},
            importance=1,
            triage_state="NEW",
            saved=False,
        )
        scratch.add(document)
        scratch.flush()

        preexisting_analyst_rows: list[dict[str, Any]] = []
        for idx, spec in enumerate(_as_list(fixture.get("existing_analyst_evidence")), start=1):
            if not isinstance(spec, dict):
                continue
            existing_payload = {
                **dict(spec),
                "evidence_key": spec.get("evidence_key") or f"eval:existing:{case.slug}:{idx}",
                "ticker": spec.get("ticker") or company.ticker,
                "company_id": int(company.id),
                "issuer_id": int(issuer.id) if issuer is not None else spec.get("issuer_id"),
                "subject_id": int(subject.id) if subject is not None else spec.get("subject_id"),
                "source_document_id": int(document.id) if spec.get("same_source_document") else spec.get("source_document_id"),
                "payload": {
                    **(spec.get("payload") if isinstance(spec.get("payload"), dict) else {}),
                    "forecast": {
                        "metric_key": spec.get("metric_key") or spec.get("estimate_metric"),
                        "period": spec.get("period") or spec.get("estimate_period"),
                        "value": spec.get("forecast_value") or spec.get("estimate_value"),
                        "value_text": spec.get("value_text") or spec.get("forecast_status"),
                        "forecast_status": spec.get("forecast_status") or spec.get("value_text"),
                    },
                },
            }
            row = upsert_analyst_evidence(scratch, payload=existing_payload, actor="local_agent_eval")
            scratch.flush()
            preexisting_analyst_rows.append(serialize_analyst_evidence(row))
        preexisting_institutional_evidence_rows = _install_eval_institutional_evidence(
            scratch,
            _as_list(fixture.get("existing_institutional_evidence")),
            source=source,
            document=document,
            company=company,
            issuer=issuer,
            subject=subject,
            now=now,
        )
        preexisting_observation_ids = {
            int(row.id)
            for row in scratch.query(InstitutionalObservation.id)
            .filter(InstitutionalObservation.company_id == int(company.id))
            .all()
        }

        doc_payload = {
            "id": int(document.id),
            "title": document.title,
            "url": document.url,
            "author": document.author,
            "source_id": int(source.id),
            "source_name": str(document_spec.get("source_name") or source.name),
            "source_category": source.category,
            "language": document.language,
            "extra": dict(document.extra or {}),
        }
        target_query: dict[str, Any] = {"company_id": int(company.id)}
        if issuer is not None:
            target_query["issuer_id"] = int(issuer.id)
        if company.ticker:
            target_query["ticker"] = company.ticker
        if subject is not None:
            target_query["subject_id"] = int(subject.id)
        decision_contexts = [{"target": {"query": target_query}, "company_id": int(company.id), "subject_id": int(subject.id) if subject is not None else None}]
        cfg = build_config(
            [
                "--thoth-base-url",
                "http://127.0.0.1:8765",
                "--thoth-token",
                "eval",
                "--once",
                "--decision-min-confidence",
                str(expected.get("decision_min_confidence") or "0.7"),
            ]
        )
        stats = _initial_run_stats(cfg)
        payloads = _decision_payloads_for_analysis_result(
            cfg,
            0,
            int(document.id),
            doc_payload,
            analysis_result,
            decision_contexts,
            stats,
        )
        context_result = dict(analysis_result)
        context_result.setdefault(
            "decision_targets",
            [
                {
                    "company_id": int(company.id),
                    "issuer_id": int(issuer.id) if issuer is not None else None,
                    "subject_id": int(subject.id) if subject is not None else None,
                    "subject": company.name,
                    "ticker": company.ticker,
                }
            ],
        )
        context_result["analyst_evidence"] = list(_as_list(payloads.get("analyst_evidence")))
        context_durable_routes = [row for row in _as_list(context_result.get("durable_routes")) if isinstance(row, dict)]
        if not context_durable_routes and _as_list(payloads.get("analyst_evidence")):
            context_durable_routes = [{"subject": company.name, "action": "write"}]
        durable_context = durable_candidate_context(
            scratch,
            document_id=int(document.id),
            payload={
                "durable_routes": context_durable_routes,
                "candidate_writes": [],
                "result": context_result,
            },
            limit=8,
        )
        stats["durable_context_reads"] = int(stats.get("durable_context_reads") or 0) + 1
        analyst_rows: list[dict[str, Any]] = []
        if materialize_outputs:
            for idx, payload in enumerate(_as_list(payloads.get("analyst_evidence")), start=1):
                if not isinstance(payload, dict):
                    continue
                payload.setdefault("evidence_key", f"eval:model-delta:{case.slug}:{idx}")
                row = upsert_analyst_evidence(scratch, payload=payload, actor="local_agent")
                scratch.flush()
                analyst_rows.append(serialize_analyst_evidence(row))

        observations_query = scratch.query(InstitutionalObservation).filter(InstitutionalObservation.company_id == int(company.id))
        if preexisting_observation_ids:
            observations_query = observations_query.filter(~InstitutionalObservation.id.in_(sorted(preexisting_observation_ids)))
        observations = observations_query.order_by(InstitutionalObservation.id.asc()).all()
        observation_rows = [
            {
                "id": int(row.id),
                "kind": row.kind,
                "metric_key": row.metric_key,
                "value_number": row.value_number,
                "value_text": row.value_text,
                "unit": row.unit,
                "currency": row.currency,
                "confidence": row.confidence,
                "period_date": row.period_date.isoformat() if row.period_date is not None else None,
                "review_status": row.review_status,
                "payload": dict(row.payload or {}),
            }
            for row in observations
        ]
        return {
            "status": "durable_output_validated",
            "document_id": int(document.id),
            "document_title": document.title,
            "company": {"id": int(company.id), "name": company.name, "ticker": company.ticker},
            "model": cfg.model,
            "prompt_version": cfg.prompt_version,
            "model_delta_candidate_payloads": _as_list(analysis_result.get("model_delta_candidates")),
            "model_delta_candidates_count": int(stats.get("model_delta_candidates") or 0),
            "model_delta_evidence_count": int(stats.get("model_delta_evidence") or 0),
            "analyst_evidence_count": len(analyst_rows),
            "institutional_observation_count": len(observation_rows),
            "durable_context_reads": int(stats.get("durable_context_reads") or 0),
            "matched_identity_count": len(_as_list(durable_context.get("matched_identities"))),
            "candidate_metric_count": len(_as_list(durable_context.get("candidate_metrics"))),
            "destination_document_count": len(_as_list(durable_context.get("destination_documents"))),
            "research_mode_context_count": len(_as_list(durable_context.get("research_mode_contexts"))),
            "existing_analyst_evidence_count": len(_as_list(durable_context.get("existing_analyst_evidence"))),
            "existing_institutional_evidence_count": len(_as_list(durable_context.get("existing_institutional_evidence"))),
            "existing_institutional_observation_count": len(_as_list(durable_context.get("existing_institutional_observations"))),
            "duplicate_risk_level": _dict(durable_context.get("duplicate_risk")).get("level"),
            "durable_recommended_action": durable_context.get("recommended_action"),
            "durable_candidate_context": durable_context,
            "preexisting_analyst_evidence": preexisting_analyst_rows,
            "preexisting_institutional_evidence": preexisting_institutional_evidence_rows,
            "analyst_evidence": analyst_rows,
            "institutional_observations": observation_rows,
            "materialized_outputs": materialize_outputs,
            "materialized_output_count": len(analyst_rows) + len(observation_rows),
            "payload_counts": {key: len(_as_list(value)) for key, value in payloads.items()},
            "stats": dict(stats),
            "rolled_back": True,
        }
    finally:
        scratch.rollback()
        scratch.close()
        _cleanup_durable_output_eval_leaks(
            session,
            company_slug=company_slug,
        )


def _durable_reasoning_eval_result(session: Session, case: LocalAgentEvalCase) -> dict[str, Any]:
    """Exercise the worker durable-reasoning path with deterministic in-process API rails."""
    expected = dict(case.expected_json or {})
    fixture = _dict(expected.get("durable_reasoning_fixture"))
    if not fixture:
        raise ValueError("Durable-reasoning eval requires expected.durable_reasoning_fixture.")

    from thoth_terminal.decision_spine_service import (
        cached_company_decision_payload,
        company_decision_payload,
        ensure_decision_subject,
        list_investment_catalysts,
        resolve_company_or_issuer_from_payload,
        serialize_analyst_evidence,
        upsert_analyst_evidence,
    )
    from thoth_terminal.foundation_service import ensure_company_issuer
    from thoth_terminal.local_agent_service import (
        create_claim,
        create_or_update_compression,
        create_unresolved_entity,
        durable_candidate_context,
        institutional_model_context,
        serialize_compression,
        start_run,
    )
    from thoth_terminal.local_agent_worker import _initial_run_stats, build_config
    import thoth_terminal.local_agent_worker as worker
    from thoth_terminal.market_context_service import build_agent_market_context

    now = utcnow()
    slug_suffix = f"{int(case.id)}-{now.strftime('%Y%m%d%H%M%S%f')}"
    source_name = f"local_agent_eval_durable_reasoning_{slug_suffix}"
    company_slug = f"eval-durable-reasoning-{slug_suffix}"
    document_external_id = f"durable-reasoning-eval-{slug_suffix}"
    company_spec = _dict(fixture.get("company"))
    document_spec = _dict(fixture.get("document"))
    signal_result = _dict(fixture.get("signal_result"))
    decision_result = _dict(fixture.get("decision_result"))
    if not signal_result:
        raise ValueError("Durable-reasoning eval fixture must include signal_result.")
    if not decision_result:
        raise ValueError("Durable-reasoning eval fixture must include decision_result.")

    scratch_factory = sessionmaker(bind=session.get_bind(), expire_on_commit=False)
    scratch = scratch_factory()
    try:
        source = Source(
            name=source_name,
            category=str(document_spec.get("source_category") or "analyst")[:64],
            priority=1,
            polling_interval_seconds=None,
            config={"connector": "local_agent_eval", "case_id": int(case.id), "mode": "durable_reasoning"},
            created_at=now,
        )
        company = Company(
            slug=company_slug,
            name=str(company_spec.get("name") or case.title or "Durable Reasoning Eval")[:255],
            ticker=str(company_spec.get("ticker") or "EDUR").strip().upper()[:32] or None,
            exchange=str(company_spec.get("exchange") or "")[:64] or None,
            region=str(company_spec.get("region") or "US")[:64] or None,
            sector=str(company_spec.get("sector") or "")[:128] or None,
            industry=str(company_spec.get("industry") or "")[:128] or None,
            created_at=now,
        )
        scratch.add_all([source, company])
        scratch.flush()
        issuer = ensure_company_issuer(scratch, company)
        subject = ensure_decision_subject(scratch, company=company, issuer=issuer)
        scratch.flush()
        report_spec = _dict(fixture.get("existing_research_report"))
        _install_eval_research_report(
            scratch,
            company=company,
            issuer=issuer,
            subject=subject,
            title=str(report_spec.get("title") or f"{company.name} existing durable context"),
            content_md=str(
                report_spec.get("content_md")
                or (
                    f"# {company.name} Existing Durable Context\n\n"
                    "Existing durable research context fixture. The durable-reasoning pass should see this "
                    "current company report before choosing whether to materialize a new durable model delta."
                )
            ),
            now=now,
        )

        fixture_text = str(case.fixture_text or document_spec.get("text") or case.description or case.title or "")
        document = Document(
            kind=str(document_spec.get("kind") or "eval_fixture")[:64],
            source_id=int(source.id),
            external_id=document_external_id,
            canonical_url=str(document_spec.get("url") or "") or None,
            url=str(document_spec.get("url") or "") or None,
            title=str(document_spec.get("title") or case.title)[:1024],
            author=str(document_spec.get("author") or "")[:255] or None,
            published_at=now,
            ingested_at=now,
            raw_text=fixture_text,
            normalized_text=fixture_text,
            language=str(document_spec.get("language") or "en")[:16],
            language_confidence=1.0,
            content_hash=hashlib.sha256(f"{case.slug}:{slug_suffix}:durable-reasoning".encode("utf-8")).hexdigest(),
            extra={"eval_case_id": int(case.id), "eval_mode": "durable_reasoning"},
            importance=1,
            triage_state="NEW",
            saved=False,
        )
        scratch.add(document)
        scratch.flush()
        preexisting_institutional_evidence_rows = _install_eval_institutional_evidence(
            scratch,
            _as_list(fixture.get("existing_institutional_evidence")),
            source=source,
            document=document,
            company=company,
            issuer=issuer,
            subject=subject,
            now=now,
        )

        doc_payload = {
            "id": int(document.id),
            "title": document.title,
            "url": document.url,
            "author": document.author,
            "source_id": int(source.id),
            "source_name": str(document_spec.get("source_name") or source.name),
            "source_category": source.category,
            "language": document.language,
            "text": fixture_text,
            "extra": dict(document.extra or {}),
        }
        for row in _as_list(signal_result.get("entity_refs")):
            if isinstance(row, dict):
                row.setdefault("company_id", int(company.id))
                row.setdefault("issuer_id", int(issuer.id) if issuer is not None else None)
                row.setdefault("ticker", company.ticker)
        cfg = build_config(
            [
                "--thoth-base-url",
                "http://127.0.0.1:8765",
                "--thoth-token",
                "eval",
                "--once",
                "--decision-min-confidence",
                str(expected.get("decision_min_confidence") or "0.7"),
            ]
        )
        stats = _initial_run_stats(cfg)
        eval_run = start_run(
            scratch,
            run_type="durable_reasoning_eval",
            model_profile=cfg.model,
            model_name=cfg.model,
            prompt_version=cfg.prompt_version,
            worker_id="local_agent_eval",
            stats=stats,
        )
        eval_run_id = int(eval_run.id)
        worker_api_calls: list[dict[str, Any]] = []
        last_durable_context: dict[str, Any] = {}

        def record_call(method: str, path: str) -> None:
            normalized = path
            if normalized.startswith(f"/api/local-agent/documents/{int(document.id)}/analysis-result"):
                normalized = "/api/local-agent/documents/{document_id}/analysis-result"
            worker_api_calls.append({"method": method, "path": normalized})

        def fake_api_get(_cfg: Any, path: str, *, run_id: int | None = None, params: dict[str, Any] | None = None) -> dict[str, Any]:
            del _cfg, run_id
            params = dict(params or {})
            record_call("GET", path)
            if path == "/api/local-agent/market-context":
                return {
                    "ok": True,
                    "context": build_agent_market_context(
                        scratch,
                        base_currency=params.get("base_currency"),
                        live_quotes=False,
                        max_items=max(1, min(int(params.get("max_items") or 30), 100)),
                    ),
                }
            if path == "/api/local-agent/institutional/model-context":
                return {
                    "ok": True,
                    "context": institutional_model_context(
                        scratch,
                        document_id=int(params["document_id"]) if str(params.get("document_id") or "").isdigit() else None,
                        query=params.get("q"),
                        subject_id=int(params["subject_id"]) if str(params.get("subject_id") or "").isdigit() else None,
                        max_source_actors=max(1, min(int(params.get("max_source_actors") or 12), 30)),
                        max_subjects=max(1, min(int(params.get("max_subjects") or 8), 20)),
                        max_state_vectors=max(1, min(int(params.get("max_state_vectors") or 8), 20)),
                    ),
                }
            if path == "/api/local-agent/entities/search":
                needle = str(params.get("q") or "").strip().lower()
                results = []
                if needle and (needle in company.name.lower() or (company.ticker and needle == company.ticker.lower())):
                    results.append(
                        {
                            "kind": "company",
                            "id": int(company.id),
                            "company_id": int(company.id),
                            "issuer_id": int(issuer.id) if issuer is not None else None,
                            "ticker": company.ticker,
                            "label": company.name,
                        }
                    )
                return {"ok": True, "results": results}
            raise RuntimeError(f"Unhandled eval GET path: {path}")

        def fake_api_post(_cfg: Any, path: str, payload: dict[str, Any], *, run_id: int | None = None) -> dict[str, Any]:
            nonlocal last_durable_context
            del _cfg
            payload = dict(payload or {})
            record_call("POST", path)
            if path == "/api/local-agent/decision/context-batch":
                results: list[dict[str, Any]] = []
                errors: list[dict[str, Any]] = []
                for index, target in enumerate(_as_list(payload.get("targets"))[:20]):
                    if not isinstance(target, dict):
                        continue
                    query = target.get("query") if isinstance(target.get("query"), dict) else target
                    try:
                        resolved_company, resolved_issuer = resolve_company_or_issuer_from_payload(scratch, dict(query))
                        if resolved_company is None and resolved_issuer is None:
                            errors.append({"index": index, "target": target, "error": "Company or issuer not found."})
                            continue
                        context = cached_company_decision_payload(scratch, company=resolved_company, issuer=resolved_issuer, read_only=True)
                        if not context:
                            context = company_decision_payload(scratch, company=resolved_company, issuer=resolved_issuer, read_only=False)
                        calendar = list_investment_catalysts(
                            scratch,
                            company_id=int(resolved_company.id) if resolved_company is not None else None,
                            issuer_id=int(resolved_issuer.id) if resolved_issuer is not None else None,
                            days_back=max(1, min(int(payload.get("days_back") or 30), 3650)),
                            days_forward=max(1, min(int(payload.get("days_forward") or 365), 3650)),
                            limit=max(1, min(int(payload.get("limit") or 100), 500)),
                        )
                        results.append(
                            {
                                "target": target,
                                "resolved": {
                                    "company_id": int(resolved_company.id) if resolved_company is not None else None,
                                    "issuer_id": int(resolved_issuer.id) if resolved_issuer is not None else None,
                                },
                                "context": context,
                                "calendar": calendar,
                                "sync": {"read_only": True},
                            }
                        )
                    except Exception as exc:
                        errors.append({"index": index, "target": target, "error": f"{type(exc).__name__}: {exc}"})
                return {"ok": True, "results": results, "errors": errors}
            if path == "/api/local-agent/durable/context":
                context = durable_candidate_context(
                    scratch,
                    document_id=int(payload["document_id"]) if str(payload.get("document_id") or "").isdigit() else None,
                    payload=payload,
                    limit=int(payload.get("limit") or 8) if str(payload.get("limit") or "").isdigit() else 8,
                )
                last_durable_context = dict(context)
                return {"ok": True, "context": context}
            if path == f"/api/local-agent/documents/{int(document.id)}/analysis-result":
                compression = create_or_update_compression(scratch, document_id=int(document.id), payload=payload, run_id=run_id)
                counts = {
                    "claims_created": 0,
                    "claims_deduped": 0,
                    "unresolved_created": 0,
                    "research_writes": 0,
                    "research_write_errors": 0,
                    "analyst_evidence": 0,
                    "catalysts": 0,
                    "investment_memos": 0,
                    "evidence_links": 0,
                    "errors": 0,
                }
                for claim_payload in _as_list(payload.get("claims")):
                    if isinstance(claim_payload, dict):
                        create_claim(scratch, payload=claim_payload, run_id=run_id)
                        counts["claims_created"] += 1
                for unresolved_payload in _as_list(payload.get("unresolved_entities")):
                    if isinstance(unresolved_payload, dict):
                        create_unresolved_entity(scratch, payload=unresolved_payload, run_id=run_id)
                        counts["unresolved_created"] += 1
                for evidence_payload in _as_list(payload.get("analyst_evidence")):
                    if isinstance(evidence_payload, dict):
                        upsert_analyst_evidence(scratch, payload=evidence_payload, actor="local_agent")
                        counts["analyst_evidence"] += 1
                scratch.flush()
                serialized = serialize_compression(compression)
                return {"ok": True, "compression": serialized, "signal_extraction": dict(serialized), "counts": counts, "errors": []}
            if path == "/api/local-agent/actions":
                return {"ok": True, "action": {"id": None}}
            if path == "/api/local-agent/errors":
                return {"ok": True}
            raise RuntimeError(f"Unhandled eval POST path: {path}")

        original_api_get = worker._api_get
        original_api_post = worker._api_post
        original_llama_decision_json = worker._llama_decision_json
        try:
            worker._api_get = fake_api_get  # type: ignore[assignment]
            worker._api_post = fake_api_post  # type: ignore[assignment]
            worker._llama_decision_json = lambda *_args, **_kwargs: dict(decision_result)  # type: ignore[assignment]
            worker._process_document_result(
                cfg,
                eval_run_id,
                int(document.id),
                doc_payload,
                dict(signal_result),
                stats,
                {"market_context": None},
                allowed_document_ids={int(document.id)},
            )
        finally:
            worker._api_get = original_api_get  # type: ignore[assignment]
            worker._api_post = original_api_post  # type: ignore[assignment]
            worker._llama_decision_json = original_llama_decision_json  # type: ignore[assignment]

        compression = (
            scratch.query(LocalAgentDocumentCompression)
            .filter(LocalAgentDocumentCompression.document_id == int(document.id))
            .order_by(LocalAgentDocumentCompression.id.desc())
            .first()
        )
        analyst_rows = [
            serialize_analyst_evidence(row)
            for row in scratch.query(AnalystEvidence).filter(AnalystEvidence.company_id == int(company.id)).order_by(AnalystEvidence.id.asc()).all()
        ]
        observation_rows = [
            {
                "id": int(row.id),
                "kind": row.kind,
                "metric_key": row.metric_key,
                "value_number": row.value_number,
                "value_text": row.value_text,
                "unit": row.unit,
                "currency": row.currency,
                "confidence": row.confidence,
                "period_date": row.period_date.isoformat() if row.period_date is not None else None,
                "review_status": row.review_status,
                "payload": dict(row.payload or {}),
            }
            for row in scratch.query(InstitutionalObservation).filter(InstitutionalObservation.company_id == int(company.id)).order_by(InstitutionalObservation.id.asc()).all()
        ]
        final_result = dict(signal_result)
        for field in ("decision_targets", "analyst_evidence", "model_delta_candidates", "catalysts", "investment_memos", "research_writes", "evidence_links"):
            final_result[field] = _as_list(final_result.get(field)) + _as_list(decision_result.get(field))
        if isinstance(decision_result.get("decision_review"), dict):
            final_result["decision_review"] = dict(decision_result.get("decision_review") or {})
        durable_context = dict(last_durable_context)
        if not durable_context:
            durable_context = durable_candidate_context(
                scratch,
                document_id=int(document.id),
                payload={"durable_routes": _as_list(final_result.get("durable_routes")), "result": final_result, "limit": 8},
                limit=8,
            )
        return {
            "status": "durable_reasoning_validated",
            "document_id": int(document.id),
            "document_title": document.title,
            "company": {"id": int(company.id), "name": company.name, "ticker": company.ticker},
            "model": cfg.model,
            "prompt_version": cfg.prompt_version,
            "signal_extraction_id": int(compression.id) if compression is not None else None,
            "latest_signal_extraction_status": compression.status if compression is not None else None,
            "document_disposition": final_result.get("document_disposition"),
            "information_gain": final_result.get("information_gain"),
            "signal_profile": final_result.get("signal_profile"),
            "signal_coverage": final_result.get("signal_coverage") if isinstance(final_result.get("signal_coverage"), dict) else {},
            "atomic_claims": _as_list(final_result.get("atomic_claims")),
            "summary_routes": _as_list(final_result.get("summary_routes")),
            "durable_routes": _as_list(final_result.get("durable_routes")),
            "model_delta_candidate_payloads": _as_list(final_result.get("model_delta_candidates")),
            "model_delta_candidates_count": int(stats.get("model_delta_candidates") or 0),
            "model_delta_evidence_count": int(stats.get("model_delta_evidence") or 0),
            "analyst_evidence_count": len(analyst_rows),
            "institutional_observation_count": len(observation_rows),
            "durable_context_reads": int(stats.get("durable_context_reads") or 0),
            "decision_context_reads": int(stats.get("decision_context_reads") or 0),
            "institutional_model_context_reads": int(stats.get("institutional_model_context_reads") or 0),
            "market_context_reads": int(stats.get("market_context_reads") or 0),
            "matched_identity_count": len(_as_list(durable_context.get("matched_identities"))),
            "candidate_metric_count": len(_as_list(durable_context.get("candidate_metrics"))),
            "destination_document_count": len(_as_list(durable_context.get("destination_documents"))),
            "research_mode_context_count": len(_as_list(durable_context.get("research_mode_contexts"))),
            "existing_institutional_evidence_count": len(_as_list(durable_context.get("existing_institutional_evidence"))),
            "duplicate_risk_level": _dict(durable_context.get("duplicate_risk")).get("level"),
            "durable_recommended_action": durable_context.get("recommended_action"),
            "durable_candidate_context": durable_context,
            "preexisting_institutional_evidence": preexisting_institutional_evidence_rows,
            "analyst_evidence": analyst_rows,
            "institutional_observations": observation_rows,
            "materialized_outputs": True,
            "materialized_output_count": len(analyst_rows) + len(observation_rows),
            "worker_api_calls": worker_api_calls,
            "worker_api_paths": sorted({str(row.get("path")) for row in worker_api_calls if row.get("path")}),
            "stats": dict(stats),
            "rolled_back": True,
        }
    finally:
        scratch.rollback()
        scratch.close()
        _cleanup_durable_output_eval_leaks(
            session,
            company_slug=company_slug,
        )


def _bottleneck_report_start_eval_result(session: Session, case: LocalAgentEvalCase) -> dict[str, Any]:
    """Validate user-defined bottleneck report startup through real service rails without committing fixtures."""
    expected = dict(case.expected_json or {})
    fixture = _dict(expected.get("bottleneck_report_start_fixture"))
    if not fixture:
        raise ValueError("Bottleneck-report-start eval requires expected.bottleneck_report_start_fixture.")

    from thoth_terminal.research_canon_service import execute_campaign_items, start_bottleneck_report

    scratch_factory = sessionmaker(bind=session.get_bind(), expire_on_commit=False)
    scratch = scratch_factory()
    old_data_dir = settings.data_dir
    old_objects_dir = settings.objects_dir
    old_runtime_state_dir = settings.runtime_state_dir
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings.data_dir = root
            settings.objects_dir = root / "objects"
            settings.runtime_state_dir = root / "runtime"
            settings.objects_dir.mkdir(parents=True, exist_ok=True)
            settings.runtime_state_dir.mkdir(parents=True, exist_ok=True)

            links = [dict(item) for item in fixture.get("links") if isinstance(item, dict)] if isinstance(fixture.get("links"), list) else []
            linked_fixture = _dict(fixture.get("linked_evidence_fixture"))
            if linked_fixture:
                now = utcnow()
                suffix = f"{int(case.id)}-{now.strftime('%Y%m%d%H%M%S%f')}"
                source = Source(
                    name=f"local_agent_eval_bottleneck_start_{suffix}",
                    category="analyst",
                    priority=1,
                    polling_interval_seconds=None,
                    config={"connector": "local_agent_eval", "case_id": int(case.id), "mode": "bottleneck_report_start"},
                    created_at=now,
                )
                actor = SourceActor(
                    actor_key=f"eval:bottleneck-start:{suffix}",
                    display_name=str(linked_fixture.get("source_name") or "Eval sell-side source")[:255],
                    actor_type="organization",
                    side="sell_side",
                    status="active",
                    trust_label="trusted_professional",
                    notes="Synthetic eval source actor for bottleneck linked-evidence hydration.",
                    payload={},
                    created_by="local_agent_eval",
                    created_at=now,
                    updated_at=now,
                )
                scratch.add_all([source, actor])
                scratch.flush()
                doc_text = str(linked_fixture.get("analyst_summary") or fixture.get("thesis") or case.fixture_text or case.description or case.title)
                document = Document(
                    kind="eval_fixture",
                    source_id=int(source.id),
                    external_id=f"bottleneck-start-linked-evidence-{suffix}",
                    canonical_url=str(linked_fixture.get("document_url") or "") or None,
                    url=str(linked_fixture.get("document_url") or "") or None,
                    title=str(linked_fixture.get("document_title") or "Eval bottleneck linked evidence")[:1024],
                    author=str(linked_fixture.get("source_name") or "")[:255] or None,
                    published_at=now,
                    ingested_at=now,
                    raw_text=doc_text,
                    normalized_text=doc_text,
                    language="en",
                    language_confidence=1.0,
                    content_hash=hashlib.sha256(f"{case.slug}:{suffix}:bottleneck-linked-evidence".encode("utf-8")).hexdigest(),
                    extra={"eval_case_id": int(case.id), "eval_mode": "bottleneck_report_start"},
                    importance=1,
                    triage_state="NEW",
                    saved=False,
                )
                scratch.add(document)
                scratch.flush()
                metric_key = str(linked_fixture.get("metric_key") or "qualified_capacity")[:128]
                period = str(linked_fixture.get("period") or "current")[:64]
                value_text = str(linked_fixture.get("value_text") or linked_fixture.get("analyst_summary") or "")[:2000]
                analyst = AnalystEvidence(
                    evidence_key=f"eval:bottleneck-start:analyst:{suffix}",
                    subject_id=None,
                    issuer_id=None,
                    company_id=None,
                    source_id=int(source.id),
                    source_document_id=int(document.id),
                    evidence_kind="analyst_note",
                    status="active",
                    source_name=actor.display_name,
                    author=actor.display_name,
                    outlet="local_agent_eval",
                    published_at=now,
                    action=None,
                    rating=None,
                    prior_rating=None,
                    price_target=None,
                    prior_price_target=None,
                    currency=None,
                    target_horizon=None,
                    estimate_metric=metric_key,
                    estimate_period=period,
                    estimate_value=None,
                    estimate_unit=None,
                    evidence_tier="analyst",
                    source_kind="sell_side",
                    trust_label="trusted_professional",
                    rights_class="public",
                    observed_source_actor_id=int(actor.id),
                    underlying_source_actor_id=int(actor.id),
                    source_identity_status="observed_and_underlying_resolved",
                    source_side="sell_side",
                    source_role_class="analyst",
                    confidence=0.82,
                    summary=doc_text,
                    source_url=document.url,
                    payload={
                        "forecast": {"metric_key": metric_key, "period": period, "forecast_status": value_text},
                        "source_attribution": {
                            "observed_source": {"name": actor.display_name, "type": "eval_fixture"},
                            "underlying_source": {"name": actor.display_name, "type": "sell_side_fixture"},
                        },
                    },
                    created_by="local_agent_eval",
                    created_at=now,
                    updated_at=now,
                )
                institutional = InstitutionalEvidenceItem(
                    evidence_key=f"eval:bottleneck-start:institutional:{suffix}",
                    source_id=int(source.id),
                    source_profile_id=None,
                    document_id=int(document.id),
                    subject_id=None,
                    issuer_id=None,
                    company_id=None,
                    site_id=None,
                    program_id=None,
                    title=str(linked_fixture.get("document_title") or "Eval bottleneck institutional evidence")[:512],
                    evidence_kind="analyst_context",
                    provenance_summary=doc_text,
                    url=document.url,
                    canonical_url=document.canonical_url or document.url,
                    published_at=now,
                    observed_at=now,
                    rights_class="public",
                    review_required=False,
                    confidence=0.82,
                    payload={"metric_key": metric_key, "period": period, "value_text": value_text, "summary": doc_text},
                    created_at=now,
                    updated_at=now,
                )
                dataset = Dataset(
                    slug=f"eval-bottleneck-start-{int(case.id)}-{now.strftime('%H%M%S%f')}",
                    title=str(linked_fixture.get("dataset_title") or "Eval bottleneck capacity bridge")[:255],
                    description="Synthetic dataset proving bottleneck report startup hydrates linked dataset inputs.",
                    category="supply_chain",
                    family="qualified_input_supply",
                    status="active",
                    source_kind="local_agent_eval",
                    created_at=now,
                    updated_at=now,
                    extra={"metric_key": metric_key, "period": period},
                )
                scratch.add_all([analyst, institutional, dataset])
                scratch.flush()
                observation = InstitutionalObservation(
                    observation_key=f"eval:bottleneck-start:observation:{suffix}",
                    evidence_id=int(institutional.id),
                    source_id=int(source.id),
                    subject_id=None,
                    issuer_id=None,
                    company_id=None,
                    site_id=None,
                    program_id=None,
                    dataset_family="qualified_input_supply",
                    kind="capacity_metric",
                    metric_key=metric_key,
                    value_number=None,
                    value_text=value_text,
                    unit=None,
                    currency=None,
                    confidence=0.82,
                    period_date=now.date(),
                    as_of_date=now.date(),
                    observed_at=now,
                    review_status="accepted",
                    payload={"period": period, "summary": doc_text},
                    created_at=now,
                    updated_at=now,
                )
                dataset_version = DatasetVersion(
                    dataset_id=int(dataset.id),
                    version=1,
                    status="published",
                    label="eval",
                    source_event="local_agent_eval",
                    created_at=now,
                    updated_at=now,
                    extra={},
                )
                scratch.add_all([observation, dataset_version])
                scratch.flush()
                scratch.add_all(
                    [
                        DatasetColumn(
                            dataset_version_id=int(dataset_version.id),
                            key="period",
                            label="Period",
                            data_type="text",
                            role="timestamp",
                            sort_order=1,
                            config={},
                        ),
                        DatasetColumn(
                            dataset_version_id=int(dataset_version.id),
                            key="signal",
                            label="Signal",
                            data_type="text",
                            role="dimension",
                            sort_order=2,
                            config={},
                        ),
                        DatasetRow(
                            dataset_version_id=int(dataset_version.id),
                            row_key=period,
                            values={"period": period, "signal": value_text},
                            sort_order=1,
                            created_at=now,
                            updated_at=now,
                        ),
                    ]
                )
                scratch.flush()
                links.extend(
                    [
                        {"object_type": "analyst_evidence", "object_id": int(analyst.id), "relation": "supports", "payload": {"eval": True}},
                        {"object_type": "institutional_evidence", "object_id": int(institutional.id), "relation": "supports", "payload": {"eval": True}},
                        {"object_type": "institutional_observation", "object_id": int(observation.id), "relation": "supports", "payload": {"eval": True}},
                        {"object_type": "dataset", "object_id": int(dataset.id), "relation": "model_input", "payload": {"eval": True}},
                    ]
                )

            start_result = start_bottleneck_report(
                scratch,
                model_key=str(fixture.get("model_key") or "").strip(),
                title=str(fixture.get("title") or "").strip(),
                scope_type=str(fixture.get("scope_type") or "theme").strip() or "theme",
                scope_key=str(fixture.get("scope_key") or "").strip() or None,
                constrained_input=str(fixture.get("constrained_input") or "").strip() or None,
                output_unit=str(fixture.get("output_unit") or "").strip() or None,
                supply_chain_id=int(fixture.get("supply_chain_id")) if str(fixture.get("supply_chain_id") or "").isdigit() else None,
                subject_id=int(fixture.get("subject_id")) if str(fixture.get("subject_id") or "").isdigit() else None,
                summary=str(fixture.get("summary") or "").strip() or None,
                thesis=str(fixture.get("thesis") or "").strip() or None,
                payload=_dict(fixture.get("payload")) or None,
                links=links or None,
                campaign_key=str(fixture.get("campaign_key") or "").strip() or None,
                campaign_name=str(fixture.get("campaign_name") or "").strip() or None,
                priority=int(fixture.get("priority")) if str(fixture.get("priority") or "").isdigit() else 8,
            )
            campaign_id = int(_dict(start_result.get("campaign")).get("id"))
            item_id = int(_dict(start_result.get("item")).get("id"))
            execute_result = execute_campaign_items(scratch, campaign_id=campaign_id, limit=1, dry_run=False, force=True)
            scratch.flush()
            from thoth_terminal.models import ResearchCampaignItem

            item = scratch.get(ResearchCampaignItem, item_id)
            result_rows = [dict(row) for row in _as_list(execute_result.get("results")) if isinstance(row, dict)]
            workspace_row = next((row for row in result_rows if row.get("status") == "workspace_ready"), None)
            completed = workspace_row or next((row for row in result_rows if row.get("status") == "completed"), result_rows[0] if result_rows else {})
            report_id = int(completed.get("report_id") or getattr(item, "output_report_id", 0) or 0)
            version_id = int(completed.get("report_version_id") or getattr(item, "output_report_version_id", 0) or 0)
            report = scratch.get(ResearchReport, report_id) if report_id else None
            version = scratch.get(ResearchReportVersion, version_id) if version_id else None
            content = _dict(version.content_json if version is not None else {})
            coverage = _dict(version.coverage_json if version is not None else {})
            workflow = _dict(content.get("workflow_contract"))
            linked_evidence = _dict(content.get("linked_evidence_pack"))
            linked_evidence_rows = [row for row in _as_list(linked_evidence.get("rows")) if isinstance(row, dict)]
            startup_packet = _dict(completed.get("startup_packet") or content.get("agent_startup_packet") or start_result.get("startup_packet"))
            artifacts = list(version.artifacts or []) if version is not None else []

            return {
                "status": "bottleneck_report_start_validated",
                "bottleneck_model_id": int(_dict(start_result.get("bottleneck_model")).get("id") or 0),
                "bottleneck_model_key": _dict(start_result.get("bottleneck_model")).get("model_key"),
                "campaign_id": campaign_id,
                "campaign_item_id": item_id,
                "campaign_item_status": item.status if item is not None else None,
                "assigned_rail": item.assigned_rail if item is not None else None,
                "open_gap_count": len(_as_list(start_result.get("open_gaps"))),
                "open_gaps": _as_list(start_result.get("open_gaps")),
                "execute_result_counts": dict(execute_result.get("result_counts") or {}),
                "report_id": report_id,
                "report_version_id": version_id,
                "report_title": report.title if report is not None else None,
                "report_type": _dict(report.extra if report is not None else {}).get("report_type"),
                "report_readiness_state": _dict(coverage.get("readiness")).get("state"),
                "workflow_roles": list(workflow.get("role_order") or []),
                "closed_allocation_model": workflow.get("closed_allocation_model"),
                "report_artifact_types": sorted({str(artifact.artifact_type) for artifact in artifacts}),
                "report_content_keys": sorted(content.keys()),
                "linked_evidence_count": int(linked_evidence.get("count") or len(linked_evidence_rows)),
                "linked_evidence_types": sorted({_clean(row.get("type")).lower() for row in linked_evidence_rows if _clean(row.get("type"))}),
                "linked_evidence_counts_by_type": dict(linked_evidence.get("counts_by_type") or {}),
                "startup_packet": startup_packet,
                "object_store_scope": "isolated_temp_eval",
                "rolled_back": True,
            }
    finally:
        scratch.rollback()
        scratch.close()
        settings.data_dir = old_data_dir
        settings.objects_dir = old_objects_dir
        settings.runtime_state_dir = old_runtime_state_dir


def run_eval_case(
    session: Session,
    *,
    case_id: int,
    mode: str = "latest_compression",
    model: str | None = None,
    prompt_version: str | None = None,
) -> LocalAgentEvalRun:
    case = (
        session.query(LocalAgentEvalCase)
        .options(joinedload(LocalAgentEvalCase.document).joinedload(Document.source))
        .filter(LocalAgentEvalCase.id == int(case_id))
        .first()
    )
    if case is None:
        raise ValueError("Eval case not found.")
    mode_v = str(mode or "latest_compression").strip().lower()
    result: dict[str, Any] = {}
    status = "success"
    error = None
    try:
        if mode_v in {"latest_compression", "latest_signal_extraction", "signal_extraction"}:
            comp = _latest_compression_for_case(session, case)
            if comp is None:
                raise ValueError("No signal extraction exists for this eval case document.")
            result = _compression_to_result(session, comp)
            result["compression_id"] = int(comp.id)
            result["signal_extraction_id"] = int(comp.id)
            model = model or comp.model
            prompt_version = prompt_version or comp.prompt_version
        elif mode_v == "llama":
            result = _run_llama_eval_case(
                case,
                model=model,
                prompt_version=prompt_version,
            )
            model = result.get("model")
        elif mode_v in {"document_quality", "doc_quality"}:
            result = _document_quality_result(session, case)
            comp_id = result.get("latest_compression_id")
            if comp_id:
                comp = session.query(LocalAgentDocumentCompression).filter_by(id=int(comp_id)).first()
                if comp is not None:
                    model = model or comp.model
                    prompt_version = prompt_version or comp.prompt_version
        elif mode_v in {"durable_output", "durable"}:
            result = _durable_output_eval_result(session, case)
            model = model or result.get("model")
            prompt_version = prompt_version or result.get("prompt_version")
        elif mode_v in {"durable_reasoning", "durable_e2e"}:
            result = _durable_reasoning_eval_result(session, case)
            model = model or result.get("model")
            prompt_version = prompt_version or result.get("prompt_version")
        elif mode_v in {"pdf_ocr_prep", "ocr_prep"}:
            result = _pdf_ocr_prep_eval_result(session, case)
        elif mode_v in {"media_route", "media_routing"}:
            result = _media_route_eval_result(session, case)
        elif mode_v in {"stage_plan", "staged_plan", "stage_planning"}:
            result = _stage_plan_eval_result(session, case)
        elif mode_v in {"bottleneck_report_start", "bottleneck_start"}:
            result = _bottleneck_report_start_eval_result(session, case)
        else:
            raise ValueError("Unsupported eval mode.")
        score = score_agent_eval_output(result, dict(case.expected_json or {}))
        if mode_v in {
            "latest_compression",
            "latest_signal_extraction",
            "signal_extraction",
            "document_quality",
            "doc_quality",
            "durable_output",
            "durable",
            "durable_reasoning",
            "durable_e2e",
            "pdf_ocr_prep",
            "ocr_prep",
            "media_route",
            "media_routing",
            "stage_plan",
            "staged_plan",
            "stage_planning",
            "bottleneck_report_start",
            "bottleneck_start",
        }:
            result["trace_path"] = _write_eval_trace(case=case, mode=mode_v, result=result, score=score)
    except Exception as exc:
        status = "error"
        error = f"{type(exc).__name__}: {exc}"
        score = {"score": 0.0, "passed": False, "threshold": EVAL_PASS_THRESHOLD, "checks": []}
    row = LocalAgentEvalRun(
        case_id=int(case.id),
        source_kind=mode_v,
        model=str(model or getattr(settings, "agent_default_model", "") or "")[:255] or None,
        prompt_version=str(prompt_version or getattr(settings, "agent_prompt_version", "") or "")[:64] or None,
        status=status,
        result_json=result,
        score_json=score,
        error=error,
        created_at=utcnow(),
    )
    session.add(row)
    session.flush()
    return row


def _run_llama_eval_case(
    case: LocalAgentEvalCase,
    *,
    model: str | None,
    prompt_version: str | None,
) -> dict[str, Any]:
    from thoth_terminal.local_agent_worker import WorkerConfig, _llama_chat_json
    from thoth_terminal.web.deepseek_harness_service import harness_service

    token = str(getattr(settings, "agent_api_token", "") or "").strip()
    if not token:
        token_path = Path(settings.runtime_state_dir) / "local-agent-api.token.txt"
        try:
            token = token_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            pass
    if not token:
        raise ValueError("Local eval requires the configured Thoth local-agent API credential.")
    catalog = harness_service.call("modelCatalog", {})
    groups = catalog.get("groups", []) if isinstance(catalog, dict) else []
    local_models = [str(row["id"]) for group in groups if group.get("id") == "thoth-local-text"
                    for row in group.get("models", []) if row.get("id")]
    selected_model = str(model or "").strip()
    if not selected_model:
        if len(local_models) != 1:
            raise ValueError("Configure one native thoth-local-text model or select it explicitly for eval.")
        selected_model = local_models[0]
    if selected_model not in local_models:
        raise ValueError("Eval model must be configured on the native thoth-local-text provider.")
    host = str(getattr(settings, "agent_api_host", "") or "127.0.0.1").strip()
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"

    doc = case.document
    text = (
        str(doc.translation_en or doc.normalized_text or doc.raw_text or "")
        if doc is not None
        else str(case.fixture_text or "")
    )
    if not text.strip():
        raise ValueError("Eval case has no document text or fixture text.")
    document_id = int(case.document_id) if case.document_id is not None else 900000000 + int(case.id)
    document = {
        "id": document_id,
        "kind": doc.kind if doc is not None else "eval_fixture",
        "source_id": int(doc.source_id) if doc is not None else 0,
        "source_name": _source_name(doc) or case.source_label,
        "title": doc.title if doc is not None else case.fixture_title or case.title,
        "url": doc.url if doc is not None else None,
        "published_at": _dt(doc.published_at) if doc is not None else None,
        "ingested_at": _dt(doc.ingested_at) if doc is not None else None,
        "text": text,
    }
    cfg = WorkerConfig(
        thoth_base_url=f"http://{host}:{int(settings.agent_api_port)}",
        thoth_token=token,
        llama_base_url=str(getattr(settings, "agent_llama_base_url", "") or "http://127.0.0.1:8080"),
        model=selected_model,
        worker_id="agent-eval-harness",
        prompt_version=str(prompt_version or getattr(settings, "agent_prompt_version", "") or "local-analyst-v3-staged-signal-context-search"),
        poll_interval_s=120.0,
        batch_size=1,
        lookback_hours=48,
        queue_strategy="balanced",
        include_opened=True,
        backlog_mode=False,
        include_filings=True,
        target_document_ids=(),
        auto_write=False,
        once=True,
        timeout_s=240.0,
        max_input_chars=64000,
        max_output_tokens=2048,
        daily_summary_enabled=False,
        daily_summary_hhmm="21:00",
        daily_summary_lookback_hours=24,
        decision_spine_enabled=False,
        decision_max_targets=1,
        decision_min_confidence=0.7,
        market_base_currency=None,
        market_live_quotes=False,
        market_max_items=1,
        calendar_days_back=30,
        calendar_days_forward=365,
        calendar_limit=20,
        loadout_preflight=False,
        preflight_ticker="TSM",
        true_batching=False,
        batch_max_input_tokens=8500,
        batch_max_documents=1,
        stage_planning_enabled=False,
        media_transcription_enabled=False,
        media_transcription_only=False,
        media_transcription_limit=0,
        media_transcription_mode="route",
        media_render_readable_pdf_pages=False,
        media_ocr_concurrency=1,
        media_vlm_concurrency=1,
        media_sidecar_concurrency_auto=True,
        media_ocr_backend="cli",
        media_ocr_config=None,
        media_ocr_api_url=None,
        media_ocr_mode=None,
        media_ocr_layout_device=None,
        media_ocr_vllm_base_url=None,
        media_ocr_vllm_model=None,
        media_ocr_cli=None,
        media_ocr_model="ggml-org/GLM-OCR-GGUF:Q8_0",
        media_ocr_min_chars=180,
        media_ocr_timeout_s=240.0,
        media_vision_base_url=None,
        media_vision_model=None,
        media_vision_timeout_s=360.0,
        media_vision_disable_thinking=False,
        llama_disable_thinking=True,
        media_vision_allow_text_endpoint=False,
        media_max_output_tokens=1800,
        idle_deep_analysis_enabled=False,
        idle_deep_analysis_limit=0,
        idle_deep_analysis_lookback_hours=72,
        idle_deep_analysis_min_interval_s=900.0,
        idle_deep_analysis_max_text_chars=12000,
        durable_review_only=False,
        durable_review_limit=0,
        durable_review_lookback_hours=72,
        durable_review_max_text_chars=12000,
    )
    result = _llama_chat_json(cfg, document=document)
    result["document_id"] = document_id
    result["model"] = selected_model
    return result


def record_review_decision(
    session: Session,
    *,
    target_kind: str,
    target_id: int,
    decision: str,
    note: str | None = None,
    created_by: str = "operator",
) -> tuple[LocalAgentReviewDecision, AgentAction | None]:
    kind = str(target_kind or "").strip().lower()
    decision_v = str(decision or "").strip().lower()
    if decision_v not in REVIEW_DECISIONS:
        raise ValueError("Unsupported review decision.")
    now = utcnow()
    payload = _target_payload(session, target_kind=kind, target_id=int(target_id))
    row = LocalAgentReviewDecision(
        target_kind=kind,
        target_id=int(target_id),
        decision=decision_v,
        note=str(note or "").strip() or None,
        payload=payload,
        created_by=created_by,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    action: AgentAction | None = None
    if kind == "compression" and decision_v == "quarantine":
        comp = session.query(LocalAgentDocumentCompression).filter_by(id=int(target_id)).first()
        if comp is not None:
            comp.status = "quarantined"
            comp.updated_at = now
    if decision_v == "research_request":
        title = str(payload.get("title") or payload.get("summary") or f"{kind} #{target_id}")[:180]
        document_id = payload.get("document_id") if str(payload.get("document_id") or "").isdigit() else None
        action = upsert_agent_action(
            session,
            kind="local_agent_research_request",
            title=f"Research review: {title}"[:255],
            priority=2,
            document_id=int(document_id) if document_id is not None else None,
            dedupe_key=f"local-agent-review:{kind}:{int(target_id)}:research-request",
            rationale=str(note or payload.get("summary") or "Promoted from local-agent review."),
            payload={"review_decision": decision_v, "target_kind": kind, "target_id": int(target_id), "target": payload},
        )
    session.flush()
    return row, action


def _target_payload(session: Session, *, target_kind: str, target_id: int) -> dict[str, Any]:
    if target_kind == "compression":
        row = (
            session.query(LocalAgentDocumentCompression)
            .options(joinedload(LocalAgentDocumentCompression.document).joinedload(Document.source))
            .filter(LocalAgentDocumentCompression.id == int(target_id))
            .first()
        )
        if row is None:
            raise ValueError("Compression not found.")
        return {
            "compression_id": int(row.id),
            "document_id": int(row.document_id),
            "title": _doc_title(row.document),
            "source": _source_name(row.document),
            "summary": row.summary_short,
            "information_gain": row.information_gain,
            "document_disposition": row.document_disposition,
        }
    if target_kind == "route":
        row = session.query(LocalAgentEntityRoute).filter_by(id=int(target_id)).first()
        if row is None:
            raise ValueError("Route not found.")
        return serialize_route(row)
    if target_kind == "write":
        row = session.query(LocalAgentWriteDiff).filter_by(id=int(target_id)).first()
        if row is None:
            raise ValueError("Write diff not found.")
        return serialize_write_diff(row)
    if target_kind == "unresolved":
        row = session.query(LocalAgentUnresolvedEntity).filter_by(id=int(target_id)).first()
        if row is None:
            raise ValueError("Unresolved entity not found.")
        return {
            "id": int(row.id),
            "document_id": int(row.document_id) if row.document_id is not None else None,
            "name": row.name,
            "entity_type": row.entity_type,
            "rationale": row.rationale,
            "confidence": row.confidence,
        }
    raise ValueError("Unsupported review target kind.")


def build_local_agent_review_state(
    session: Session,
    *,
    hours: int = 72,
    limit: int = 80,
    offset: int = 0,
) -> dict[str, Any]:
    resolved_limit = _limit(limit)
    resolved_offset = max(0, int(offset or 0))
    cutoff = utcnow() - timedelta(hours=max(1, min(int(hours or 72), 336)))
    total_compressions = int(
        session.query(func.count(LocalAgentDocumentCompression.id))
        .filter(LocalAgentDocumentCompression.updated_at >= cutoff)
        .scalar()
        or 0
    )
    if total_compressions:
        last_offset = ((total_compressions - 1) // resolved_limit) * resolved_limit
        resolved_offset = min(resolved_offset, last_offset)
    else:
        resolved_offset = 0
    skipped_compressions = int(
        session.query(func.count(LocalAgentDocumentCompression.id))
        .filter(LocalAgentDocumentCompression.updated_at >= cutoff)
        .filter(
            or_(
                LocalAgentDocumentCompression.status.in_(("skipped", "noop")),
                LocalAgentDocumentCompression.information_gain == "none",
            )
        )
        .scalar()
        or 0
    )
    compression_rows = (
        session.query(LocalAgentDocumentCompression)
        .options(
            load_only(
                LocalAgentDocumentCompression.id,
                LocalAgentDocumentCompression.document_id,
                LocalAgentDocumentCompression.model,
                LocalAgentDocumentCompression.prompt_version,
                LocalAgentDocumentCompression.summary_short,
                LocalAgentDocumentCompression.summary_detail,
                LocalAgentDocumentCompression.information_gain,
                LocalAgentDocumentCompression.document_disposition,
                LocalAgentDocumentCompression.skip_reason,
                LocalAgentDocumentCompression.status,
                LocalAgentDocumentCompression.confidence,
                LocalAgentDocumentCompression.updated_at,
            ),
            joinedload(LocalAgentDocumentCompression.document)
            .load_only(Document.id, Document.title, Document.url, Document.published_at, Document.source_id)
            .joinedload(Document.source)
            .load_only(Source.id, Source.name),
        )
        .filter(LocalAgentDocumentCompression.updated_at >= cutoff)
        .order_by(LocalAgentDocumentCompression.updated_at.desc(), LocalAgentDocumentCompression.id.desc())
        .offset(resolved_offset)
        .limit(resolved_limit)
        .all()
    )
    comp_ids = [int(row.id) for row in compression_rows]
    doc_ids = [int(row.document_id) for row in compression_rows]
    routes_by_comp: dict[int, list[LocalAgentEntityRoute]] = defaultdict(list)
    if comp_ids:
        for route in (
            session.query(LocalAgentEntityRoute)
            .options(
                load_only(
                    LocalAgentEntityRoute.id,
                    LocalAgentEntityRoute.compression_id,
                    LocalAgentEntityRoute.lane,
                    LocalAgentEntityRoute.entity_name,
                    LocalAgentEntityRoute.entity_type,
                    LocalAgentEntityRoute.entity_role,
                    LocalAgentEntityRoute.durable_action,
                    LocalAgentEntityRoute.materiality,
                )
            )
            .filter(LocalAgentEntityRoute.compression_id.in_(comp_ids))
            .order_by(LocalAgentEntityRoute.id.asc())
            .all()
        ):
            if route.compression_id is not None:
                routes_by_comp[int(route.compression_id)].append(route)
    claims_by_comp: dict[int, list[LocalAgentEntityClaim]] = defaultdict(list)
    if comp_ids:
        for claim in (
            session.query(LocalAgentEntityClaim)
            .options(
                load_only(
                    LocalAgentEntityClaim.id,
                    LocalAgentEntityClaim.compression_id,
                    LocalAgentEntityClaim.entity_name,
                    LocalAgentEntityClaim.claim_kind,
                    LocalAgentEntityClaim.claim_text,
                    LocalAgentEntityClaim.confidence,
                )
            )
            .filter(LocalAgentEntityClaim.compression_id.in_(comp_ids))
            .order_by(LocalAgentEntityClaim.id.asc())
            .all()
        ):
            if claim.compression_id is not None:
                claims_by_comp[int(claim.compression_id)].append(claim)
    write_rows = (
        session.query(LocalAgentWriteDiff)
        .options(
            load_only(
                LocalAgentWriteDiff.id,
                LocalAgentWriteDiff.run_id,
                LocalAgentWriteDiff.action_id,
                LocalAgentWriteDiff.destination_type,
                LocalAgentWriteDiff.destination_id,
                LocalAgentWriteDiff.source_document_ids_json,
                LocalAgentWriteDiff.before_version_id,
                LocalAgentWriteDiff.after_version_id,
                LocalAgentWriteDiff.write_summary,
                LocalAgentWriteDiff.confidence,
                LocalAgentWriteDiff.created_at,
            )
        )
        .filter(LocalAgentWriteDiff.created_at >= cutoff)
        .order_by(LocalAgentWriteDiff.created_at.desc(), LocalAgentWriteDiff.id.desc())
        .limit(250)
        .all()
    )
    writes_by_doc: dict[int, list[LocalAgentWriteDiff]] = defaultdict(list)
    for write in write_rows:
        for doc_id in list(write.source_document_ids_json or []):
            if str(doc_id).isdigit():
                writes_by_doc[int(doc_id)].append(write)
    compression_decisions = _latest_review_decisions(
        session,
        target_kind="compression",
        target_ids=comp_ids,
    )

    items: list[dict[str, Any]] = []
    for comp in compression_rows:
        routes = routes_by_comp.get(int(comp.id), [])
        claims = claims_by_comp.get(int(comp.id), [])
        writes = writes_by_doc.get(int(comp.document_id), [])
        route_counts = {
            "summary": sum(1 for route in routes if route.lane == "summary"),
            "durable": sum(1 for route in routes if route.lane == "durable"),
            "write": sum(1 for route in routes if route.durable_action == "write"),
            "hold": sum(1 for route in routes if route.durable_action == "hold"),
            "skip": sum(1 for route in routes if route.durable_action in {"skip", "quarantine"}),
        }
        items.append(
            {
                "id": int(comp.id),
                "document_id": int(comp.document_id),
                "document_title": _doc_title(comp.document),
                "document_href": f"/docs/{int(comp.document_id)}",
                "source_name": _source_name(comp.document),
                "published_at": _dt(comp.document.published_at) if comp.document else None,
                "updated_at": _dt(comp.updated_at),
                "model": comp.model,
                "prompt_version": comp.prompt_version,
                "summary_short": comp.summary_short,
                "summary_detail": comp.summary_detail,
                "information_gain": comp.information_gain,
                "document_disposition": comp.document_disposition,
                "skip_reason": comp.skip_reason,
                "status": comp.status,
                "confidence": comp.confidence,
                "route_counts": route_counts,
                "routes": [_serialize_review_route(route) for route in routes[:12]],
                "claims": [
                    {
                        "id": int(claim.id),
                        "entity_name": claim.entity_name,
                        "claim_kind": claim.claim_kind,
                        "claim_text": claim.claim_text,
                        "confidence": claim.confidence,
                    }
                    for claim in claims[:6]
                ],
                "writes": [_serialize_review_write(write) for write in writes[:4]],
                "latest_decision": _serialize_decision_summary(compression_decisions.get(int(comp.id))),
                "recommendation": _recommend_review(comp, routes=routes, writes=writes),
            }
        )

    unresolved_rows = (
        session.query(LocalAgentUnresolvedEntity)
        .options(
            load_only(
                LocalAgentUnresolvedEntity.id,
                LocalAgentUnresolvedEntity.document_id,
                LocalAgentUnresolvedEntity.name,
                LocalAgentUnresolvedEntity.entity_type,
                LocalAgentUnresolvedEntity.rationale,
                LocalAgentUnresolvedEntity.status,
                LocalAgentUnresolvedEntity.confidence,
                LocalAgentUnresolvedEntity.updated_at,
            )
        )
        .filter(LocalAgentUnresolvedEntity.updated_at >= cutoff)
        .order_by(LocalAgentUnresolvedEntity.updated_at.desc(), LocalAgentUnresolvedEntity.id.desc())
        .limit(30)
        .all()
    )
    unresolved_decisions = _latest_review_decisions(
        session,
        target_kind="unresolved",
        target_ids=[int(row.id) for row in unresolved_rows],
    )
    unresolved = [
        {
            "id": int(row.id),
            "document_id": int(row.document_id) if row.document_id is not None else None,
            "document_href": f"/docs/{int(row.document_id)}" if row.document_id is not None else None,
            "name": row.name,
            "entity_type": row.entity_type,
            "rationale": row.rationale,
            "status": row.status,
            "confidence": row.confidence,
            "updated_at": _dt(row.updated_at),
            "latest_decision": _serialize_decision_summary(unresolved_decisions.get(int(row.id))),
        }
        for row in unresolved_rows
    ]

    run_statuses = ("running", "success", "error", "stopped")
    run_counts = {status: 0 for status in run_statuses}
    for status, count in (
        session.query(LocalAgentRun.status, func.count(LocalAgentRun.id))
        .filter(LocalAgentRun.status.in_(run_statuses))
        .group_by(LocalAgentRun.status)
        .all()
    ):
        run_counts[str(status)] = int(count or 0)
    recent_runs = [
        {
            "id": int(row.id),
            "run_type": row.run_type,
            "status": row.status,
            "model_name": row.model_name,
            "summary": row.summary,
            "error": row.error,
            "started_at": _dt(row.started_at),
            "completed_at": _dt(row.completed_at),
        }
        for row in (
            session.query(LocalAgentRun)
            .options(
                load_only(
                    LocalAgentRun.id,
                    LocalAgentRun.run_type,
                    LocalAgentRun.status,
                    LocalAgentRun.model_name,
                    LocalAgentRun.summary,
                    LocalAgentRun.error,
                    LocalAgentRun.started_at,
                    LocalAgentRun.completed_at,
                )
            )
            .order_by(LocalAgentRun.started_at.desc(), LocalAgentRun.id.desc())
            .limit(12)
            .all()
        )
    ]

    eval_cases, eval_pass = _eval_case_summaries(session)
    metrics = {
        "compressions": total_compressions,
        "skipped": skipped_compressions,
        "durable_writes": len(write_rows),
        "unresolved": len(unresolved),
        "document_count": len(set(doc_ids)),
        "eval_cases": len(eval_cases),
        "eval_pass_rate": eval_pass,
        "errors": int(run_counts.get("error") or 0),
    }
    return {
        "hours": hours,
        "limit": resolved_limit,
        "offset": resolved_offset,
        "total_items": total_compressions,
        "metrics": metrics,
        "items": items,
        "unresolved": unresolved,
        "writes": [_serialize_review_write(row) for row in write_rows[:20]],
        "run_counts": run_counts,
        "recent_runs": recent_runs,
        "eval_cases": eval_cases,
    }


def _eval_case_summaries(session: Session) -> tuple[list[dict[str, Any]], float | None]:
    cases = (
        session.query(LocalAgentEvalCase)
        .options(
            load_only(
                LocalAgentEvalCase.id,
                LocalAgentEvalCase.slug,
                LocalAgentEvalCase.title,
                LocalAgentEvalCase.source_label,
                LocalAgentEvalCase.document_id,
                LocalAgentEvalCase.tags_json,
            ),
            joinedload(LocalAgentEvalCase.document).load_only(Document.id, Document.title),
        )
        .order_by(LocalAgentEvalCase.updated_at.desc(), LocalAgentEvalCase.id.desc())
        .limit(100)
        .all()
    )
    latest_runs: dict[int, LocalAgentEvalRun] = {}
    if cases:
        case_ids = [int(row.id) for row in cases]
        latest_rank = (
            session.query(
                LocalAgentEvalRun.id.label("id"),
                func.row_number()
                .over(
                    partition_by=LocalAgentEvalRun.case_id,
                    order_by=(LocalAgentEvalRun.created_at.desc(), LocalAgentEvalRun.id.desc()),
                )
                .label("rank"),
            )
            .filter(LocalAgentEvalRun.case_id.in_(case_ids))
            .subquery()
        )
        for run in (
            session.query(LocalAgentEvalRun)
            .join(latest_rank, LocalAgentEvalRun.id == latest_rank.c.id)
            .options(
                load_only(
                    LocalAgentEvalRun.id,
                    LocalAgentEvalRun.case_id,
                    LocalAgentEvalRun.source_kind,
                    LocalAgentEvalRun.status,
                    LocalAgentEvalRun.score_json,
                    LocalAgentEvalRun.created_at,
                )
            )
            .filter(latest_rank.c.rank == 1)
            .all()
        ):
            latest_runs[int(run.case_id)] = run
    serialized = [_serialize_eval_case_summary(case, latest_runs.get(int(case.id))) for case in cases]
    completed = [row for row in serialized if row.get("latest_run")]
    if not completed:
        return serialized, None
    passed = sum(1 for row in completed if ((row.get("latest_run") or {}).get("passed")))
    return serialized, round(passed / len(completed), 4)


def _recommend_review(
    comp: LocalAgentDocumentCompression,
    *,
    routes: list[LocalAgentEntityRoute],
    writes: list[LocalAgentWriteDiff],
) -> str:
    if comp.status == "quarantined":
        return "quarantined"
    if comp.information_gain == "none" or comp.document_disposition in {"skip", "noop"}:
        return "demote"
    if writes:
        return "promote"
    if any(route.durable_action == "write" for route in routes):
        return "review_write"
    if any(route.durable_action == "hold" for route in routes):
        return "hold"
    if comp.information_gain in {"medium", "high"}:
        return "summary"
    return "noop"
