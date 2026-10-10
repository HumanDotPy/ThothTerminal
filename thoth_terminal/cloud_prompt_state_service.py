from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from thoth_terminal.cloud_goal_run_service import estimate_tokens
from thoth_terminal.config import settings
from thoth_terminal.db import utcnow
from thoth_terminal.institutional_model_vocab import (
    INSTITUTIONAL_SOURCE_REGISTRY,
    compact_metric_vocabulary,
    compact_signal_rail_taxonomy,
)
from thoth_terminal.models import (
    BottleneckModel,
    CloudAgentMemoryEvent,
    CloudAgentMemorySegment,
    CloudPromptStateSnapshot,
    Company,
    Dataset,
    DatasetColumn,
    DatasetRow,
    DatasetVersion,
    Issuer,
    IssuerIdentifier,
    KnowledgeAsset,
    LocalAgentDeepAnalysisTrace,
    ResearchReport,
    ResearchReportVersion,
    ResearchSubject,
    ResearchSubjectAlias,
    RoadmapEntry,
    Source,
    SourceActor,
    SourceActorAlias,
    StateVectorSnapshot,
    SupplyChain,
    SupplyChainEdge,
    SupplyChainMetricBinding,
    SupplyChainNode,
    SupplyChainRelation,
    SupplyChainSnapshot,
)
from thoth_terminal.source_acquisition_registry import list_source_acquisition_rails


logger = logging.getLogger(__name__)


CLOUD_PROMPT_STATE_COMPILER_VERSION = "cloud-prompt-state-v5"
CLOUD_PROMPT_STATE_START_MARKER = '<thoth_stable_state version="cloud-prompt-state-v5">'
CLOUD_PROMPT_STATE_END_MARKER = "</thoth_stable_state>"

_CORE_DATASET_SLUGS = {
    "ai-model-cost-quality-frontier",
    "ai-provider-api-pricing",
    "ai-serving-margin-scenarios",
    "ai-serving-scenario-assumptions",
    "artificial-analysis-model-economics",
    "inferencex-upstream-benchmarks",
}
_MODEL_MEMORY_TYPES = {
    "company_model",
    "contradiction",
    "mechanism",
    "sector_model",
    "working_model",
    "world_model",
}


@dataclass(frozen=True)
class CloudPromptStateOptions:
    supply_mode: str = "analytical"  # topology | analytical | none
    dataset_mode: str = "state_cards"  # schemas | state_cards | none
    report_mode: str = "titles"  # titles | summaries | none
    identity_mode: str = "routing"  # routing | compact | full | none
    include_source_registry: bool = True
    include_search_atlas: bool = True
    include_roadmap: bool = True
    include_models: bool = True
    include_memory: bool = True


def configured_cloud_prompt_state_options() -> CloudPromptStateOptions:
    return CloudPromptStateOptions(
        supply_mode=str(getattr(settings, "cloud_research_stable_state_supply_mode", "analytical") or "analytical"),
        dataset_mode=str(getattr(settings, "cloud_research_stable_state_dataset_mode", "state_cards") or "state_cards"),
        report_mode=str(getattr(settings, "cloud_research_stable_state_report_mode", "titles") or "titles"),
        identity_mode=str(getattr(settings, "cloud_research_stable_state_identity_mode", "routing") or "routing"),
        include_search_atlas=bool(getattr(settings, "cloud_research_stable_state_include_search_atlas", False)),
        include_roadmap=bool(getattr(settings, "cloud_research_stable_state_include_roadmap", True)),
        include_memory=bool(getattr(settings, "cloud_research_stable_state_include_memory", True)),
    )


def _text(value: object) -> str:
    return str(value or "").strip()


def _short(value: object, limit: int = 800) -> str | None:
    text = _text(value)
    if not text:
        return None
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _clean(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value not in (None, "", [], {})}


def _compact_value(value: object, *, depth: int = 0) -> object:
    if depth >= 4:
        return _short(value, 300)
    if isinstance(value, dict):
        return {
            str(key): _compact_value(item, depth=depth + 1)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]).casefold())[:40]
        }
    if isinstance(value, list):
        return [_compact_value(item, depth=depth + 1) for item in value[:20]]
    if isinstance(value, str):
        return _short(value, 600) or ""
    return value


def _packed(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _section(name: str, payload: object) -> tuple[str, dict[str, Any]]:
    packed = _packed(payload)
    rendered = f"## {name}\n\n```json\n{packed}\n```"
    return rendered, {
        "name": name,
        "sha256": hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
        "chars": len(rendered),
        "token_estimate": estimate_tokens(rendered),
    }


def _compact_record_table(payload: dict[str, Any], key: str) -> dict[str, Any]:
    """Losslessly encode repeated field names once, keeping every record/value."""
    records = payload.get(key) or []
    schema = sorted({field for row in records for field in row})
    encoded = {**payload, "tuple_schema": schema,
               "tuple_note": f"Each {key} row follows tuple_schema; null is a value unless its column index appears in tuple_missing_fields for that row.",
               key: [[row.get(field) for field in schema] for row in records]}
    missing = {str(i): [j for j, field in enumerate(schema) if field not in row]
               for i, row in enumerate(records) if any(field not in row for field in schema)}
    if missing:
        encoded["tuple_missing_fields"] = missing
    return encoded


def _source_registry_payload(session: Session) -> dict[str, Any]:
    sources = session.query(Source).order_by(Source.name.asc(), Source.id.asc()).all()
    actors = session.query(SourceActor).filter(SourceActor.status == "active").order_by(SourceActor.actor_key.asc()).all()
    aliases = (
        session.query(SourceActorAlias)
        .filter(SourceActorAlias.actor_id.in_([row.id for row in actors]))
        .order_by(SourceActorAlias.actor_id.asc(), SourceActorAlias.normalized_alias.asc(), SourceActorAlias.id.asc())
        .all()
        if actors
        else []
    )
    aliases_by_actor: dict[int, list[str]] = defaultdict(list)
    for alias in aliases:
        value = _text(alias.alias)
        if value and value not in aliases_by_actor[int(alias.actor_id)]:
            aliases_by_actor[int(alias.actor_id)].append(value)
    return {
        "registry_version": "cloud-source-registry-v1",
        "institutional_sources": list(INSTITUTIONAL_SOURCE_REGISTRY),
        "configured_sources": [
            _clean(
                {
                    "name": row.name,
                    "category": row.category,
                    "priority": int(row.priority or 0),
                    "poll_seconds": row.polling_interval_seconds,
                }
            )
            for row in sources
        ],
        "source_actors": [
            _clean(
                {
                    "key": row.actor_key,
                    "name": row.display_name,
                    "type": row.actor_type,
                    "side": row.side,
                    "trust": row.trust_label,
                    "aliases": aliases_by_actor.get(int(row.id), [])[:16],
                }
            )
            for row in actors
        ],
    }


def _search_atlas_payload() -> dict[str, Any]:
    return {
        "atlas_version": "cloud-search-atlas-v1",
        "purpose": "Stable map from questions and metrics to source families, acquisition rails, and durable destinations.",
        "metric_vocabulary": compact_metric_vocabulary(include_aliases=True),
        "signal_rails": compact_signal_rail_taxonomy(),
        "acquisition_rails": list_source_acquisition_rails(include_surfaces=False),
    }


def _identity_payload(session: Session, *, mode: str) -> dict[str, Any]:
    if mode == "routing":
        reports = (
            session.query(ResearchReport)
            .filter(ResearchReport.status == "active")
            .order_by(ResearchReport.id.asc())
            .all()
        )
        asset_subject_ids = {
            int(value)
            for (value,) in session.query(KnowledgeAsset.subject_id)
            .filter(KnowledgeAsset.status == "active")
            .distinct()
            .all()
            if value is not None
        }
        routed_subject_ids = asset_subject_ids | {
            int(row.subject_id) for row in reports if row.subject_id is not None
        }
        routed_issuer_ids = {
            int(row.issuer_id) for row in reports if row.issuer_id is not None
        }
        routed_company_ids = {
            int(row.company_id) for row in reports if row.company_id is not None
        }
        routed_subjects = (
            session.query(ResearchSubject)
            .filter(ResearchSubject.id.in_(sorted(routed_subject_ids)))
            .order_by(ResearchSubject.slug.asc(), ResearchSubject.id.asc())
            .all()
            if routed_subject_ids
            else []
        )
        routed_issuer_ids.update(
            int(row.issuer_id) for row in routed_subjects if row.issuer_id is not None
        )
        routed_company_ids.update(
            int(row.company_id) for row in routed_subjects if row.company_id is not None
        )
        routed_companies = (
            session.query(Company)
            .filter(Company.id.in_(sorted(routed_company_ids)))
            .order_by(Company.slug.asc(), Company.id.asc())
            .all()
            if routed_company_ids
            else []
        )
        routed_issuer_ids.update(
            int(row.issuer_id) for row in routed_companies if row.issuer_id is not None
        )
        routed_issuers = (
            session.query(Issuer)
            .filter(Issuer.id.in_(sorted(routed_issuer_ids)))
            .order_by(Issuer.slug.asc(), Issuer.id.asc())
            .all()
            if routed_issuer_ids
            else []
        )
        identifiers = (
            session.query(IssuerIdentifier)
            .filter(IssuerIdentifier.issuer_id.in_(sorted(routed_issuer_ids)))
            .order_by(
                IssuerIdentifier.issuer_id.asc(),
                IssuerIdentifier.is_primary.desc(),
                IssuerIdentifier.id_type.asc(),
                IssuerIdentifier.id_value.asc(),
            )
            .all()
            if routed_issuer_ids
            else []
        )
        primary_by_issuer: dict[int, str] = {}
        for row in identifiers:
            issuer_id = int(row.issuer_id)
            if issuer_id not in primary_by_issuer or bool(row.is_primary):
                primary_by_issuer[issuer_id] = _text(row.id_value)

        return {
            "identity_version": "cloud-identity-routing-index-v3",
            "mode": "routing",
            "purpose": (
                "Cache-stable routes for entities represented by an active report or knowledge asset. This "
                "snapshot is closed input to a one-shot cloud call and does not provide interactive lookup "
                "tools. Absence means unresolved, not nonexistent: surface the unresolved identity in structured "
                "output so the downstream workflow can persist a durable research or entity-resolution request; "
                "do not invent a match."
            ),
            "catalog_counts": {
                "active_reports": len(reports),
                "active_knowledge_assets": session.query(KnowledgeAsset)
                .filter(KnowledgeAsset.status == "active")
                .count(),
                "active_subjects": session.query(ResearchSubject)
                .filter(ResearchSubject.status == "active")
                .count(),
                "issuers": session.query(Issuer).count(),
                "legacy_companies": session.query(Company).count(),
            },
            "routing_scope": {
                "issuer_count": len(routed_issuers),
                "company_count": len(routed_companies),
                "subject_count": len(routed_subjects),
                "subject_routes_live_in": (
                    "research_subjects tuples below; report routes are separately listed in Stable Report Directory"
                ),
            },
            "tuple_schemas": {
                "listed_issuer": ["id", "slug", "name", "primary_identifier"],
                "legacy_company": ["id", "slug", "name", "ticker", "exchange", "issuer_id"],
                "research_subject": ["id", "slug", "subject_key", "kind", "name", "issuer_id", "company_id"],
            },
            "listed_issuers": [
                [int(row.id), row.slug, row.display_name, primary_by_issuer.get(int(row.id))]
                for row in routed_issuers
            ],
            "legacy_companies": [
                [int(row.id), row.slug, row.name, row.ticker, row.exchange, row.issuer_id]
                for row in routed_companies
            ],
            "research_subjects": [
                [
                    int(row.id),
                    row.slug,
                    row.subject_key,
                    row.kind,
                    row.display_name,
                    row.issuer_id,
                    row.company_id,
                ]
                for row in routed_subjects
            ],
        }

    issuers = session.query(Issuer).order_by(Issuer.slug.asc(), Issuer.id.asc()).all()
    issuer_ids = [int(row.id) for row in issuers]
    identifiers = (
        session.query(IssuerIdentifier)
        .filter(IssuerIdentifier.issuer_id.in_(issuer_ids))
        .order_by(IssuerIdentifier.issuer_id.asc(), IssuerIdentifier.is_primary.desc(), IssuerIdentifier.id_type.asc(), IssuerIdentifier.id_value.asc())
        .all()
        if issuer_ids
        else []
    )
    identifiers_by_issuer: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in identifiers:
        identifiers_by_issuer[int(row.issuer_id)].append(
            _clean(
                {
                    "type": row.id_type,
                    "value": row.id_value,
                    "exchange": row.exchange,
                    "currency": row.currency,
                    "primary": bool(row.is_primary),
                }
            )
        )
    companies = session.query(Company).order_by(Company.slug.asc(), Company.id.asc()).all()
    subjects = session.query(ResearchSubject).filter(ResearchSubject.status == "active").order_by(ResearchSubject.slug.asc()).all()
    aliases = (
        session.query(ResearchSubjectAlias)
        .filter(ResearchSubjectAlias.subject_id.in_([row.id for row in subjects]))
        .order_by(ResearchSubjectAlias.subject_id.asc(), ResearchSubjectAlias.alias.asc())
        .all()
        if subjects
        else []
    )
    aliases_by_subject: dict[int, list[str]] = defaultdict(list)
    for row in aliases:
        value = _text(row.alias)
        if value and value not in aliases_by_subject[int(row.subject_id)]:
            aliases_by_subject[int(row.subject_id)].append(value)
    full_payload = {
        "identity_version": "cloud-identity-map-v1",
        "mode": mode,
        "issuers": [
            _clean(
                {
                    "slug": row.slug,
                    "name": row.display_name,
                    "legal_name": row.legal_name if row.legal_name != row.display_name else None,
                    "jurisdiction": row.jurisdiction,
                    "status": row.entity_status,
                    "identifiers": identifiers_by_issuer.get(int(row.id), []),
                }
            )
            for row in issuers
        ],
        "legacy_companies": [
            _clean(
                {
                    "slug": row.slug,
                    "name": row.name,
                    "ticker": row.ticker,
                    "exchange": row.exchange,
                    "region": row.region,
                    "issuer_id": row.issuer_id,
                }
            )
            for row in companies
        ],
        "research_subjects": [
            _clean(
                {
                    "slug": row.slug,
                    "key": row.subject_key,
                    "kind": row.kind,
                    "name": row.display_name,
                    "legal_name": row.legal_name if row.legal_name != row.display_name else None,
                    "issuer_id": row.issuer_id,
                    "company_id": row.company_id,
                    "aliases": aliases_by_subject.get(int(row.id), [])[:20],
                }
            )
            for row in subjects
        ],
    }
    if mode == "full":
        return full_payload

    # The full canon is useful for explicit identity-maintenance work, but it is
    # much too large for an always-loaded prefix. The compact rail keeps the
    # listing resolver plus non-company research subjects; issuer/company
    # subjects are redundant with the two canonical lists above.
    compact_issuers = []
    for row in issuers:
        row_identifiers = identifiers_by_issuer.get(int(row.id), [])
        if not row_identifiers:
            continue
        compact_issuers.append(
            [
                row.slug,
                row.display_name,
                [
                    [
                        item.get("type"),
                        item.get("value"),
                        item.get("exchange"),
                        item.get("currency"),
                        1 if item.get("primary") else 0,
                    ]
                    for item in row_identifiers
                ],
            ]
        )
    compact_companies = [
        [row.slug, row.name, row.ticker, row.exchange, row.issuer_id]
        for row in companies
        if _text(row.ticker)
    ]
    compact_subjects = [
        _clean(
            {
                "slug": row.slug,
                "kind": row.kind,
                "name": row.display_name,
                "aliases": aliases_by_subject.get(int(row.id), [])[:8],
            }
        )
        for row in subjects
        if _text(row.kind).lower() not in {"issuer", "company", "private_company"}
    ]
    return {
        "identity_version": "cloud-identity-map-v2",
        "mode": "compact",
        "tuple_schemas": {
            "listed_issuer": ["slug", "name", "identifiers[type,value,exchange,currency,primary]"],
            "listed_company": ["slug", "name", "ticker", "exchange", "issuer_id"],
        },
        "listed_issuers": compact_issuers,
        "listed_companies": compact_companies,
        "named_research_subjects": compact_subjects,
    }


def _latest_supply_snapshot_ids(session: Session, chain_ids: list[int]) -> dict[int, int]:
    if not chain_ids:
        return {}
    # Prompt construction needs only the selected IDs, never snapshot JSON.
    # Rank before fetching: historical graph payloads can exceed process memory.
    status = func.lower(func.trim(SupplyChainSnapshot.status))
    ranked = session.query(
        SupplyChainSnapshot.id.label("snapshot_id"),
        SupplyChainSnapshot.chain_id,
        func.row_number().over(
            partition_by=SupplyChainSnapshot.chain_id,
            order_by=(case((status == "draft", 0), else_=1),
                      SupplyChainSnapshot.version.desc(), SupplyChainSnapshot.id.desc()),
        ).label("rank"),
    ).filter(SupplyChainSnapshot.chain_id.in_(chain_ids), status.in_(("draft", "published"))).subquery()
    return {int(chain_id): int(snapshot_id) for chain_id, snapshot_id in
            session.query(ranked.c.chain_id, ranked.c.snapshot_id).filter(ranked.c.rank == 1)}


def _supply_chain_payload(session: Session, *, mode: str) -> tuple[dict[str, Any], set[int]]:
    chains = session.query(SupplyChain).filter(SupplyChain.status == "active").order_by(SupplyChain.slug.asc()).all()
    snapshots = _latest_supply_snapshot_ids(session, [int(row.id) for row in chains])
    snapshot_ids = list(snapshots.values())
    nodes = (
        session.query(SupplyChainNode)
        .filter(SupplyChainNode.snapshot_id.in_(snapshot_ids))
        .order_by(SupplyChainNode.snapshot_id.asc(), SupplyChainNode.node_key.asc())
        .all()
        if snapshot_ids
        else []
    )
    edges = (
        session.query(SupplyChainEdge)
        .filter(SupplyChainEdge.snapshot_id.in_(snapshot_ids))
        .order_by(SupplyChainEdge.snapshot_id.asc(), SupplyChainEdge.id.asc())
        .all()
        if snapshot_ids
        else []
    )
    bindings = (
        session.query(SupplyChainMetricBinding)
        .filter(SupplyChainMetricBinding.snapshot_id.in_(snapshot_ids))
        .order_by(SupplyChainMetricBinding.snapshot_id.asc(), SupplyChainMetricBinding.id.asc())
        .all()
        if snapshot_ids
        else []
    )
    nodes_by_snapshot: dict[int, list[SupplyChainNode]] = defaultdict(list)
    edges_by_snapshot: dict[int, list[SupplyChainEdge]] = defaultdict(list)
    bindings_by_snapshot: dict[int, list[SupplyChainMetricBinding]] = defaultdict(list)
    nodes_by_id = {int(row.id): row for row in nodes}
    for row in nodes:
        nodes_by_snapshot[int(row.snapshot_id)].append(row)
    for row in edges:
        edges_by_snapshot[int(row.snapshot_id)].append(row)
    for row in bindings:
        bindings_by_snapshot[int(row.snapshot_id)].append(row)
    bound_dataset_ids = {int(row.dataset_id) for row in bindings if row.dataset_id is not None}
    rendered_chains: list[dict[str, Any]] = []
    for chain in chains:
        snapshot_id = snapshots.get(int(chain.id))
        if snapshot_id is None:
            continue
        chain_nodes = nodes_by_snapshot.get(snapshot_id, [])
        chain_edges = edges_by_snapshot.get(snapshot_id, [])
        base: dict[str, Any] = {
            "slug": chain.slug,
            "title": chain.title,
            "category": chain.category,
            "product": chain.product,
            "nodes": [
                _clean(
                    {
                        "key": row.node_key,
                        "name": row.name,
                        "layer": row.layer_key,
                        "type": row.node_type,
                        "manufacturer": row.manufacturer,
                        "location": row.location,
                    }
                )
                for row in chain_nodes
            ],
            "edges": [
                _clean(
                    {
                        "from": nodes_by_id[int(row.source_node_id)].node_key,
                        "to": nodes_by_id[int(row.target_node_id)].node_key,
                        "type": row.edge_type,
                    }
                )
                for row in chain_edges
                if int(row.source_node_id) in nodes_by_id and int(row.target_node_id) in nodes_by_id
            ],
        }
        if mode == "analytical":
            base.update(
                {
                    "description": _short(chain.description, 1200),
                    "node_signals": [
                        _clean(
                            {
                                "key": row.node_key,
                                "capacity": row.capacity_value,
                                "unit": row.capacity_unit,
                                "lead_time_months": row.lead_time_months,
                                "pricing_power": row.pricing_power,
                                "bottleneck": row.bottleneck,
                                "confidence": row.confidence,
                                "notes": _short(row.notes, 700),
                            }
                        )
                        for row in chain_nodes
                        if any(
                            value not in (None, "")
                            for value in (
                                row.capacity_value,
                                row.lead_time_months,
                                row.pricing_power,
                                row.bottleneck,
                                row.confidence,
                                row.notes,
                            )
                        )
                    ],
                    "edge_signals": [
                        _clean(
                            {
                                "from": nodes_by_id[int(row.source_node_id)].node_key,
                                "to": nodes_by_id[int(row.target_node_id)].node_key,
                                "criticality": row.criticality,
                                "notes": _short(row.notes, 500),
                            }
                        )
                        for row in chain_edges
                        if (row.criticality is not None or _text(row.notes))
                        and int(row.source_node_id) in nodes_by_id
                        and int(row.target_node_id) in nodes_by_id
                    ],
                    "dataset_bindings": [
                        _clean(
                            {
                                "scope": row.scope_type,
                                "key": row.scope_key,
                                "metric": row.metric_kind,
                                "dataset_id": row.dataset_id,
                                "column": row.column_key,
                                "aggregation": row.aggregation,
                            }
                        )
                        for row in bindings_by_snapshot.get(snapshot_id, [])
                    ],
                }
            )
        rendered_chains.append(_clean(base))
    relations = [
        {"parent_chain_id": int(row.parent_chain_id), "child_chain_id": int(row.child_chain_id), "type": row.relation_type}
        for row in session.query(SupplyChainRelation).order_by(SupplyChainRelation.id.asc()).all()
    ]
    return {
        "graph_version": "cloud-supply-chain-v1",
        "mode": mode,
        "chains": rendered_chains,
        "relations": relations,
    }, bound_dataset_ids


def _canonical_dataset_group(slug: str) -> str:
    return re.sub(r"-\d+$", "", _text(slug).lower())


def _latest_dataset_versions(session: Session, dataset_ids: list[int]) -> dict[int, DatasetVersion]:
    rows = (
        session.query(DatasetVersion)
        .filter(DatasetVersion.dataset_id.in_(dataset_ids), DatasetVersion.status.in_(["draft", "published"]))
        .order_by(DatasetVersion.dataset_id.asc(), DatasetVersion.version.desc(), DatasetVersion.id.desc())
        .all()
        if dataset_ids
        else []
    )
    by_status: dict[tuple[int, str], DatasetVersion] = {}
    for row in rows:
        by_status.setdefault((int(row.dataset_id), _text(row.status).lower()), row)
    return {
        dataset_id: by_status.get((dataset_id, "draft")) or by_status.get((dataset_id, "published"))
        for dataset_id in dataset_ids
        if by_status.get((dataset_id, "draft")) or by_status.get((dataset_id, "published"))
    }


def _dataset_payload(session: Session, *, mode: str, bound_dataset_ids: set[int]) -> dict[str, Any]:
    rows = (
        session.query(Dataset)
        .filter(Dataset.status == "active")
        .filter((Dataset.id.in_(sorted(bound_dataset_ids))) | (Dataset.slug.in_(sorted(_CORE_DATASET_SLUGS))))
        .order_by(Dataset.slug.asc(), Dataset.id.asc())
        .all()
    )
    grouped: dict[str, list[Dataset]] = defaultdict(list)
    for row in rows:
        grouped[_canonical_dataset_group(row.slug)].append(row)
    selected: list[Dataset] = []
    aliases_by_id: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for group_rows in grouped.values():
        canonical = next((row for row in group_rows if row.slug == _canonical_dataset_group(row.slug)), group_rows[0])
        selected.append(canonical)
        aliases_by_id[int(canonical.id)] = [
            {"id": int(row.id), "slug": row.slug, "graph_bound": int(row.id) in bound_dataset_ids}
            for row in group_rows
            if int(row.id) != int(canonical.id)
        ]
    selected.sort(key=lambda row: (row.slug.casefold(), int(row.id)))
    versions = _latest_dataset_versions(session, [int(row.id) for row in selected])
    version_ids = [int(row.id) for row in versions.values()]
    columns = (
        session.query(DatasetColumn)
        .filter(DatasetColumn.dataset_version_id.in_(version_ids))
        .order_by(DatasetColumn.dataset_version_id.asc(), DatasetColumn.sort_order.asc(), DatasetColumn.key.asc())
        .all()
        if version_ids
        else []
    )
    columns_by_version: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in columns:
        columns_by_version[int(row.dataset_version_id)].append(
            _clean({"key": row.key, "label": row.label, "type": row.data_type, "role": row.role})
        )
    cards: list[dict[str, Any]] = []
    for dataset in selected:
        version = versions.get(int(dataset.id))
        row_count = 0
        sample_rows: list[dict[str, Any]] = []
        if version is not None:
            row_count = int(session.query(DatasetRow).filter(DatasetRow.dataset_version_id == int(version.id)).count())
            if mode == "state_cards":
                sample = (
                    session.query(DatasetRow)
                    .filter(DatasetRow.dataset_version_id == int(version.id))
                    .order_by(DatasetRow.updated_at.desc(), DatasetRow.row_key.asc())
                    .limit(12 if row_count <= 200 else 8)
                    .all()
                )
                sample_rows = [
                    {"row_key": row.row_key, "values": _compact_value(dict(row.values or {}))}
                    for row in sorted(sample, key=lambda item: (int(item.sort_order or 0), item.row_key))
                ]
        cards.append(
            _clean(
                {
                    "id": int(dataset.id),
                    "slug": dataset.slug,
                    "title": dataset.title,
                    "description": _short(dataset.description, 900),
                    "category": dataset.category,
                    "family": dataset.family,
                    "source_kind": dataset.source_kind,
                    "version": int(version.version) if version is not None else None,
                    "version_status": version.status if version is not None else None,
                    "row_count": row_count,
                    "columns": columns_by_version.get(int(version.id), []) if version is not None else [],
                    "sample_policy": "all-current-rows" if mode == "state_cards" and row_count <= 12 else "latest-compact-sample" if mode == "state_cards" else None,
                    "sample_rows": sample_rows,
                    "duplicate_aliases": aliases_by_id.get(int(dataset.id), []),
                }
            )
        )
    return {
        "dataset_spine_version": "cloud-dataset-spine-v1",
        "mode": mode,
        "selection": "core model datasets plus supply-chain-bound datasets, canonicalized by slug family",
        "datasets": cards,
    }


def _report_payload(session: Session, *, mode: str) -> dict[str, Any]:
    # A title directory needs routes and freshness, never full report bodies.
    # Project scalar columns so large JSON/markdown is neither transferred nor
    # decoded into ORM objects during every stable-state rebuild.
    report_query = (
        session.query(ResearchReport.id, ResearchReport.slug, ResearchReport.subject_key,
                      ResearchReport.title, ResearchReport.latest_published_version_id)
        if mode == "titles" else session.query(ResearchReport)
    )
    reports = report_query.filter(ResearchReport.status == "active").order_by(ResearchReport.slug.asc()).all()
    latest_ids = [int(row.latest_published_version_id) for row in reports if row.latest_published_version_id is not None]
    version_query = (
        session.query(ResearchReportVersion.id, ResearchReportVersion.version,
                      ResearchReportVersion.status, ResearchReportVersion.as_of_at)
        if mode == "titles" else session.query(ResearchReportVersion)
    )
    versions = {
        int(row.id): row
        for row in (
            version_query.filter(ResearchReportVersion.id.in_(latest_ids)).all()
            if latest_ids
            else []
        )
    }
    if mode == "titles":
        return {
            "report_directory_version": "cloud-report-directory-v2",
            "mode": "titles",
            "purpose": "Exact report routes and freshness metadata; retrieve report bodies only when relevant.",
            "tuple_schema": ["id", "slug", "subject_key", "title", "version", "version_status", "as_of"],
            "reports": [
                [
                    int(report.id),
                    report.slug,
                    report.subject_key,
                    report.title,
                    int(version.version) if version is not None else None,
                    version.status if version is not None else "missing",
                    version.as_of_at.date().isoformat() if version is not None and version.as_of_at else None,
                ]
                for report in reports
                for version in [
                    versions.get(int(report.latest_published_version_id))
                    if report.latest_published_version_id
                    else None
                ]
            ],
        }

    rendered: list[dict[str, Any]] = []
    for report in reports:
        version = versions.get(int(report.latest_published_version_id)) if report.latest_published_version_id else None
        extra = dict(report.extra or {})
        content = dict(version.content_json or {}) if version is not None else {}
        summary = None
        if mode == "summaries":
            summary = (
                _short(extra.get("summary"), 320)
                or _short(extra.get("description"), 320)
                or _short(content.get("summary"), 320)
                or _short(content.get("executive_summary"), 320)
            )
        rendered.append(
            _clean(
                {
                    "slug": report.slug,
                    "title": report.title,
                    "subject_key": report.subject_key,
                    "summary": summary,
                    "version": int(version.version) if version is not None else None,
                    "version_status": version.status if version is not None else "missing",
                    "as_of": version.as_of_at.date().isoformat() if version is not None and version.as_of_at else None,
                    "coverage_gaps": [
                        _short(gap, 240)
                        for gap in list((version.coverage_json or {}).get("gaps") or [])[:3]
                    ] if mode == "summaries" and version is not None else [],
                }
            )
        )
    return {"report_directory_version": "cloud-report-directory-v1", "mode": mode, "reports": rendered}


def _roadmap_payload(session: Session) -> dict[str, Any]:
    rows = session.query(RoadmapEntry).filter(RoadmapEntry.status == "active").order_by(RoadmapEntry.entry_key.asc()).all()
    return {
        "roadmap_version": "cloud-roadmap-state-v1",
        "entries": [
            _clean(
                {
                    "key": row.entry_key,
                    "title": row.title,
                    "claim": _short(row.statement, 1200),
                    "category": row.category,
                    "subject": row.subject,
                    "owner": row.owner,
                    "expected_window": row.expected_window,
                    "target_date": row.target_date.isoformat() if row.target_date else None,
                    "target_year": row.target_year,
                    "target_quarter": row.target_quarter,
                    "stance": row.stance,
                    "confidence": row.confidence,
                    "confidence_label": row.confidence_label,
                    "model_use": row.model_use,
                    "materiality": row.materiality,
                    "applies_to": list(row.applies_to or []),
                    "affected_models": list(row.affected_models or []),
                    "beneficiaries": list(row.beneficiaries or []),
                    "dependencies": list(row.dependencies or []),
                    "mention_count": int(row.mention_count or 0),
                    "reiteration_count": int(row.reiteration_count or 0),
                }
            )
            for row in rows
        ],
    }


def _normalized_claim(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", _text(value).lower()).strip()


def _model_payload(session: Session) -> dict[str, Any]:
    models = session.query(BottleneckModel).filter(BottleneckModel.status == "active").order_by(BottleneckModel.model_key.asc()).all()
    state_vectors = (
        session.query(StateVectorSnapshot)
        .filter(StateVectorSnapshot.contradiction_count > 0)
        .order_by(StateVectorSnapshot.snapshot_key.asc())
        .all()
    )
    traces = (
        session.query(LocalAgentDeepAnalysisTrace)
        .filter(LocalAgentDeepAnalysisTrace.status == "success")
        .order_by(LocalAgentDeepAnalysisTrace.updated_at.desc(), LocalAgentDeepAnalysisTrace.id.desc())
        .all()
    )
    contradictions: dict[str, dict[str, Any]] = {}
    for trace in traces:
        for raw in list(trace.contradictions_json or []):
            if not isinstance(raw, dict):
                continue
            claim = _text(raw.get("claim") or raw.get("title") or raw.get("summary"))
            key = _normalized_claim(claim)
            if not key:
                key = hashlib.sha256(_packed(raw).encode("utf-8")).hexdigest()
            if key in contradictions:
                contradictions[key]["occurrences"] = int(contradictions[key].get("occurrences") or 1) + 1
                continue
            contradictions[key] = _clean(
                {
                    "claim": _short(claim, 1000),
                    "needs_review": _short(raw.get("needs_review"), 700),
                    "confidence": raw.get("confidence"),
                    "status": raw.get("status") or "unresolved",
                    "occurrences": 1,
                }
            )
    return {
        "model_ledger_version": "cloud-active-model-ledger-v1",
        "bottleneck_models": [
            _clean(
                {
                    "key": row.model_key,
                    "title": row.title,
                    "scope_type": row.scope_type,
                    "scope_key": row.scope_key,
                    "supply_chain_id": row.supply_chain_id,
                    "constrained_input": row.constrained_input,
                    "output_unit": row.output_unit,
                    "regime": row.regime,
                    "summary": _short(row.summary, 1200),
                    "thesis": _short(row.thesis, 1400),
                    "qualified_supplier_count": row.qualified_supplier_count,
                    "supplier_qualification": _compact_value(dict(row.supplier_qualification_json or {})),
                    "state": _compact_value(dict(row.payload or {})),
                }
            )
            for row in models
        ],
        "state_vector_contradictions": [
            _clean(
                {
                    "key": row.snapshot_key,
                    "scope_type": row.scope_type,
                    "scope_key": row.scope_key,
                    "count": int(row.contradiction_count or 0),
                    "state": _compact_value(dict(row.payload or {})),
                }
            )
            for row in state_vectors
        ],
        "known_contradictions": [contradictions[key] for key in sorted(contradictions)],
    }


def _memory_payload(session: Session) -> dict[str, Any]:
    rows = (
        session.query(CloudAgentMemorySegment)
        .filter(CloudAgentMemorySegment.status == "active")
        .order_by(CloudAgentMemorySegment.cache_scope.asc(), CloudAgentMemorySegment.segment_type.asc(), CloudAgentMemorySegment.segment_key.asc())
        .all()
    )
    return {
        "memory_snapshot_version": "cloud-memory-snapshot-v1",
        "segments": [
            _clean(
                {
                    "key": row.segment_key,
                    "type": row.segment_type,
                    "scope": row.cache_scope,
                    "title": row.title,
                    "summary": _short(row.summary, 800),
                    "content": _short(row.content_md, 5000),
                    "importance": int(row.importance or 0),
                    "entities": list(row.entity_refs_json or [])[:20],
                    "themes": list(row.theme_refs_json or [])[:20],
                }
            )
            for row in rows
        ],
    }


def compile_cloud_prompt_state(
    session: Session,
    *,
    options: CloudPromptStateOptions | None = None,
) -> tuple[str, dict[str, Any]]:
    opts = options or configured_cloud_prompt_state_options()
    supply_mode = _text(opts.supply_mode).lower()
    dataset_mode = _text(opts.dataset_mode).lower()
    report_mode = _text(opts.report_mode).lower()
    identity_mode = _text(opts.identity_mode).lower()
    if supply_mode not in {"topology", "analytical", "none"}:
        raise ValueError("supply_mode must be topology|analytical|none")
    if dataset_mode not in {"schemas", "state_cards", "none"}:
        raise ValueError("dataset_mode must be schemas|state_cards|none")
    if report_mode not in {"titles", "summaries", "none"}:
        raise ValueError("report_mode must be titles|summaries|none")
    if identity_mode not in {"routing", "compact", "full", "none"}:
        raise ValueError("identity_mode must be routing|compact|full|none")

    rendered_sections: list[str] = []
    section_manifest: list[dict[str, Any]] = []

    def add(name: str, payload: object) -> None:
        rendered, metadata = _section(name, payload)
        rendered_sections.append(rendered)
        section_manifest.append(metadata)

    if opts.include_source_registry:
        add("Stable Source Registry", _source_registry_payload(session))
    if opts.include_search_atlas:
        add("Stable Search Atlas", _search_atlas_payload())
    if identity_mode != "none":
        add("Stable Entity And Listing Identity Map", _identity_payload(session, mode=identity_mode))

    bound_dataset_ids: set[int] = set()
    if supply_mode != "none":
        supply_payload, bound_dataset_ids = _supply_chain_payload(session, mode=supply_mode)
        add("Stable Supply Chain Graph", supply_payload)
    if dataset_mode != "none":
        add("Stable Dataset Spine", _dataset_payload(session, mode=dataset_mode, bound_dataset_ids=bound_dataset_ids))
    if report_mode != "none":
        add("Stable Report Directory", _report_payload(session, mode=report_mode))
    if opts.include_roadmap:
        add("Stable Current Roadmap", _compact_record_table(_roadmap_payload(session), "entries"))
    if opts.include_models:
        add("Stable Active Models And Contradictions", _model_payload(session))
    if opts.include_memory:
        add("Stable Compacted Memory Snapshot", _memory_payload(session))

    content_md = "\n\n".join(
        [
            CLOUD_PROMPT_STATE_START_MARKER,
            "# Thoth Stable State",
            "",
            (
                "This immutable state snapshot is shared across cloud research calls. Treat it as the current "
                "baseline; mutable deltas and the operator objective appear after the stable-state boundary."
            ),
            "",
            *rendered_sections,
            CLOUD_PROMPT_STATE_END_MARKER,
        ]
    )
    manifest = {
        "version": CLOUD_PROMPT_STATE_COMPILER_VERSION,
        "sha256": hashlib.sha256(content_md.encode("utf-8")).hexdigest(),
        "chars": len(content_md),
        "token_estimate": estimate_tokens(content_md),
        "options": asdict(opts),
        "sections": section_manifest,
    }
    return content_md, manifest


def _epoch_bounds(now: datetime, hours: int) -> tuple[datetime, datetime]:
    bounded_hours = max(1, min(int(hours or 24), 168))
    aware = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
    epoch_seconds = bounded_hours * 3600
    start_seconds = int(aware.timestamp()) // epoch_seconds * epoch_seconds
    start = datetime.fromtimestamp(start_seconds, tz=timezone.utc).replace(tzinfo=None)
    return start, start + timedelta(hours=bounded_hours)


def get_or_create_cloud_prompt_state_snapshot(
    session: Session,
    *,
    agent_key: str,
    options: CloudPromptStateOptions | None = None,
    now: datetime | None = None,
    force: bool = False,
) -> tuple[CloudPromptStateSnapshot, bool]:
    opts = options or configured_cloud_prompt_state_options()
    captured_at = now or utcnow()
    epoch_start, epoch_end = _epoch_bounds(
        captured_at,
        int(getattr(settings, "cloud_research_stable_state_epoch_hours", 24) or 24),
    )
    option_hash = hashlib.sha256(_packed(asdict(opts)).encode("utf-8")).hexdigest()[:12]
    snapshot_key = f"{_text(agent_key) or 'cloud-agent'}:{CLOUD_PROMPT_STATE_COMPILER_VERSION}:{epoch_start.isoformat()}:{option_hash}"
    existing = session.query(CloudPromptStateSnapshot).filter_by(snapshot_key=snapshot_key).first()
    if existing is not None and not force:
        return existing, False
    try:
        content_md, manifest = compile_cloud_prompt_state(session, options=opts)
    except MemoryError:
        # A stable prefix is an optimization, not a reason to take the research
        # lane offline. Large durable-state growth can make a daily rebuild fail
        # under transient memory pressure; the previous immutable prefix plus
        # the normal mutable deltas is still a valid, auditable input.
        previous = (
            session.query(CloudPromptStateSnapshot)
            .filter(
                CloudPromptStateSnapshot.agent_key == (_text(agent_key) or "cloud-agent"),
                CloudPromptStateSnapshot.compiler_version == CLOUD_PROMPT_STATE_COMPILER_VERSION,
                CloudPromptStateSnapshot.status == "active",
            )
            .order_by(CloudPromptStateSnapshot.epoch_start.desc(), CloudPromptStateSnapshot.id.desc())
            .first()
        )
        if previous is None:
            raise
        logger.exception(
            "stable prompt snapshot rebuild exhausted memory; reusing snapshot id=%s epoch_start=%s",
            previous.id,
            previous.epoch_start,
        )
        return previous, False
    if existing is None:
        existing = CloudPromptStateSnapshot(
            snapshot_key=snapshot_key,
            agent_key=_text(agent_key) or "cloud-agent",
            compiler_version=CLOUD_PROMPT_STATE_COMPILER_VERSION,
            epoch_start=epoch_start,
            epoch_end=epoch_end,
            status="active",
            content_sha256=str(manifest["sha256"]),
            content_md=content_md,
            manifest_json=manifest,
            created_at=captured_at,
            updated_at=captured_at,
        )
    else:
        existing.content_sha256 = str(manifest["sha256"])
        existing.content_md = content_md
        existing.manifest_json = manifest
        existing.updated_at = captured_at
    session.add(existing)
    session.flush()
    return existing, True


def render_cloud_prompt_state_delta(session: Session, snapshot: CloudPromptStateSnapshot) -> tuple[str, dict[str, Any]]:
    changed_segments = (
        session.query(CloudAgentMemorySegment)
        .filter(CloudAgentMemorySegment.updated_at > snapshot.created_at)
        .order_by(CloudAgentMemorySegment.updated_at.asc(), CloudAgentMemorySegment.segment_key.asc())
        .all()
    )
    memory_events = (
        session.query(CloudAgentMemoryEvent)
        .filter(CloudAgentMemoryEvent.created_at > snapshot.created_at)
        .order_by(CloudAgentMemoryEvent.created_at.asc(), CloudAgentMemoryEvent.id.asc())
        .all()
    )
    payload = {
        "changed_memory_segments": [
            _clean(
                {
                    "key": row.segment_key,
                    "status": row.status,
                    "type": row.segment_type,
                    "scope": row.cache_scope,
                    "title": row.title,
                    "summary": _short(row.summary, 800),
                    "content": _short(row.content_md, 5000),
                }
            )
            for row in changed_segments
        ],
        "memory_events": [
            _clean(
                {
                    "kind": row.event_kind,
                    "segment_id": row.segment_id,
                    "summary": _short(row.summary, 800),
                    "content": _short(row.content_md, 1500),
                }
            )
            for row in memory_events
        ],
    }
    rendered, metadata = _section("Mutable State Deltas Since Frozen Snapshot", payload)
    metadata.update(
        {
            "changed_memory_segments": len(changed_segments),
            "memory_events": len(memory_events),
        }
    )
    return rendered, metadata


def cloud_prompt_state_usage_stats(session: Session, *, lookback_runs: int = 100) -> dict[str, dict[str, int]]:
    from thoth_terminal.models import CloudGoalRun

    rows = (
        session.query(CloudGoalRun)
        .filter(CloudGoalRun.task_kind.in_(["cloud_research_heartbeat", "cloud_research_nightly", "cloud_research_inquisitive"]))
        .order_by(CloudGoalRun.created_at.desc(), CloudGoalRun.id.desc())
        .limit(max(1, min(int(lookback_runs or 100), 1000)))
        .all()
    )
    stats: dict[str, dict[str, int]] = defaultdict(lambda: {"loaded": 0, "referenced": 0, "material": 0})
    for row in rows:
        manifest = dict((row.stats_json or {}).get("stable_state") or {})
        for section in manifest.get("sections") or []:
            name = _text(section.get("name")) if isinstance(section, dict) else ""
            if name:
                stats[name]["loaded"] += 1
        final = dict(row.final_result or {})
        for usage in final.get("state_usage") or []:
            if not isinstance(usage, dict):
                continue
            name = _text(usage.get("section"))
            if not name:
                continue
            stats[name]["referenced"] += 1
            if bool(usage.get("material")):
                stats[name]["material"] += 1
    return dict(stats)
