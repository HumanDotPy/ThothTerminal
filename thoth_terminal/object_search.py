from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime
from typing import Any

from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.orm import Session

from thoth_terminal.dataset_discovery import discover_datasets, dataset_search_values
from thoth_terminal.institutional_search import InstitutionalSearchContext, ranked_query, resolve_search_context
from thoth_terminal.models import (
    AnalystEvidence,
    Chart,
    BottleneckModel,
    BottleneckModelLink,
    Company,
    Dataset,
    DatasetLink,
    Document,
    InstrumentRequest,
    InstitutionalEvidenceItem,
    InstitutionalObservation,
    InvestmentCatalyst,
    KnowledgeAsset,
    KnowledgeAssetLink,
    LocalAgentDocumentCompression,
    LocalAgentEntityRoute,
    Note,
    NoteDocument,
    NoteProposition,
    NoteThesis,
    Observation,
    Proposition,
    ResearchPlay,
    ResearchReport,
    ResearchReportVersion,
    ResearchSubject,
    Site,
    SourceActor,
    SourceActorAlias,
    Source,
    Program,
    ProductStageEntry,
    SupplyChain,
    SupplyChainLink,
    SupplyChainMetricBinding,
    SupplyChainSnapshot,
    Thesis,
    ThesisEvidence,
    ThesisProposition,
)
from thoth_terminal.query import document_match_projection, list_documents
from thoth_terminal.research_report_service import list_research_reports
from thoth_terminal.research_signal_service import THESIS_HIDDEN_STATUSES, thesis_kind_profile


_KIND_ALIASES = {
    "all": "all",
    "any": "all",
    "*": "all",
    "doc": "document",
    "docs": "document",
    "documents": "document",
    "theses": "thesis",
    "notes": "note",
    "datasets": "dataset",
    "charts": "chart",
    "companies": "company",
    "chains": "supply_chain",
    "chain": "supply_chain",
    "supply-chain": "supply_chain",
    "supply_chains": "supply_chain",
    "reports": "research_report",
    "report": "research_report",
    "subjects": "research_subject",
    "subject": "research_subject",
    "asset": "knowledge_asset",
    "assets": "knowledge_asset",
    "knowledge": "knowledge_asset",
    "plays": "research_play",
    "play": "research_play",
    "propositions": "proposition",
    "observations": "observation",
    "sources": "source",
    "sites": "site",
    "programs": "program",
    "instrument": "instrument_request",
    "instruments": "instrument_request",
    "instrument_requests": "instrument_request",
    "product_stage": "product_stage",
    "product-stage": "product_stage",
    "stage": "product_stage",
    "bottleneck": "bottleneck_model",
    "bottlenecks": "bottleneck_model",
    "bottleneck_model": "bottleneck_model",
    "bottleneck_models": "bottleneck_model",
    "analyst": "source_actor",
    "analysts": "source_actor",
    "actor": "source_actor",
    "actors": "source_actor",
    "source_actor": "source_actor",
    "source_actors": "source_actor",
    "signal": "signal_extraction",
    "signals": "signal_extraction",
    "signal_extraction": "signal_extraction",
    "signal_extractions": "signal_extraction",
    "compression": "signal_extraction",
    "compressions": "signal_extraction",
    "local_agent_compression": "signal_extraction",
    "local_agent_compressions": "signal_extraction",
    "analyst_evidence": "analyst_evidence",
    "analyst_evidences": "analyst_evidence",
    "consensus_evidence": "analyst_evidence",
    "sell_side_evidence": "analyst_evidence",
    "catalyst": "investment_catalyst",
    "catalysts": "investment_catalyst",
    "investment_catalyst": "investment_catalyst",
    "investment_catalysts": "investment_catalyst",
    "institutional_evidence": "institutional_evidence",
    "institutional_evidence_item": "institutional_evidence",
    "institutional_evidence_items": "institutional_evidence",
    "institutional_observation": "institutional_observation",
    "institutional_observations": "institutional_observation",
}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _tokens(value: object) -> set[str]:
    return {
        token
        for token in re.split(r"[^a-z0-9]+", str(value or "").lower())
        if len(token) >= 2
    }


def _rank(query: str, *values: object) -> int:
    from thoth_terminal.search_native import rank_native

    return rank_native(query, [str(value or "") for value in values])


def _score_from_rank(rank: int) -> float:
    if rank >= 999:
        return 0.0
    return max(1.0, 100.0 - float(rank * 8))


def _kind(raw: object) -> str:
    text = str(raw or "all").strip().lower().replace(" ", "_")
    return _KIND_ALIASES.get(text, text)


def _like_filter(model: Any, query: str, fields: list[str]) -> Any:
    pattern = f"%{query}%"
    return or_(*[getattr(model, field).ilike(pattern) for field in fields])


def _json_text_like(column: Any, query: str) -> Any:
    return cast(column, String).ilike(f"%{query}%")


def _signal_route_match_conditions(query: str) -> list[Any]:
    return [
        _like_filter(
            LocalAgentEntityRoute,
            query,
            [
                "entity_name",
                "entity_type",
                "entity_role",
                "durable_action",
                "materiality",
                "novelty",
                "temporal_posture",
                "evidence_tier",
                "resolution_status",
                "new_information",
                "rationale",
                "destination_type",
            ],
        ),
        _json_text_like(LocalAgentEntityRoute.claim_refs_json, query),
        _json_text_like(LocalAgentEntityRoute.payload, query),
    ]


def _session_dialect_name(session: Session) -> str:
    bind = session.get_bind()
    return str(getattr(getattr(bind, "dialect", None), "name", "") or "").lower()


def _json_key_value(session: Session, column: Any, key: str) -> Any:
    if _session_dialect_name(session) == "postgresql":
        return column.op("->>")(key)
    return func.json_extract(column, f"$.{key}")


def _json_truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value == 1
    return str(value or "").strip().lower() in {"true", "1", "yes"}


def _default_next_tools(kind: str) -> list[str]:
    return {
        "document": ["expand_document_match", "query_document", "get_document", "fetch", "add_thesis_evidence"],
        "source": ["get_source", "list_sources", "run_source"],
        "thesis": ["get_thesis", "update_thesis", "add_thesis_evidence"],
        "note": ["get_note", "update_note"],
        "dataset": ["get_dataset", "query_dataset", "suggest_dataset_supply_chain_bindings"],
        "chart": ["open_chart"],
        "company": ["get_company", "list_company_docs"],
        "supply_chain": ["get_supply_chain", "suggest_supply_chain_metric_bindings", "preview_supply_chain_ops"],
        "bottleneck_model": ["get_bottleneck_model", "assess_supply_chain_bottlenecks", "upsert_bottleneck_model"],
        "analyst_evidence": ["durable_context", "get_document", "agent_upsert_analyst_evidence"],
        "investment_catalyst": ["durable_context", "calendar", "agent_write_analysis_result"],
        "institutional_evidence": ["durable_context", "get_document", "object_search"],
        "institutional_observation": ["durable_context", "object_search", "assess_supply_chain_bottlenecks"],
        "source_actor": ["durable_context", "object_search", "agent_upsert_analyst_evidence"],
        "signal_extraction": ["get_document", "durable_context", "agent_create_signal_extraction_trace"],
        "research_report": ["get_research_report", "build_research_report"],
        "research_subject": ["get_research_subject", "list_knowledge_assets", "list_research_reports"],
        "knowledge_asset": ["get_knowledge_asset", "score_knowledge_asset_readiness"],
        "research_play": ["get_research_play", "run_research_play"],
        "instrument_request": ["get_instrument_request", "run_instrument_request", "review_instrument_request"],
        "product_stage": ["get_product_stage_entry", "list_product_stage_claims", "update_product_stage_entry"],
        "site": ["get_site", "list_programs", "list_gaps"],
        "program": ["get_program", "get_bottleneck", "list_gaps"],
        "proposition": ["get_thesis"],
        "observation": ["get_document"],
    }.get(kind, [])


def _item(
    *,
    kind: str,
    object_id: int,
    label: object,
    href: str,
    query: str,
    values: list[object],
    summary: object = None,
    updated_at: datetime | None = None,
    status: object = None,
    extra: dict[str, Any] | None = None,
    next_tools: list[str] | None = None,
    rank: int | None = None,
) -> dict[str, Any]:
    rank = _rank(query, *values) if rank is None else rank
    payload: dict[str, Any] = {
        "id": f"{kind}:{int(object_id)}",
        "object_id": int(object_id),
        "kind": kind,
        "label": str(label or f"{kind} {object_id}")[:220],
        "href": href,
        "summary": str(summary or "")[:500] or None,
        "status": str(status or "").strip() or None,
        "updated_at": _iso(updated_at),
        "score": _score_from_rank(rank),
        "next_tools": next_tools or _default_next_tools(kind),
        "_rank": rank,
    }
    if extra:
        payload.update(extra)
    return payload


def _add_doc_items(session: Session, query: str, limit: int, items: list[dict[str, Any]]) -> None:
    docs = list_documents(
        session,
        query=query,
        days=None,
        limit=max(limit * 4, 40),
        defer_text=True,
        include_domains=False,
        order_by_relevance=True,
    )
    matches = document_match_projection(session, [int(doc.id) for doc in docs], query=query, limit=500)
    docs.sort(key=lambda doc: (float((matches.get(int(doc.id)) or {}).get("relevance_score") or 0.0), int(doc.id)), reverse=True)
    for doc in docs[: max(limit, 10)]:
        match = matches.get(int(doc.id)) or {}
        title = doc.title or doc.url or f"Document {int(doc.id)}"
        items.append(
            _item(
                kind="document",
                object_id=int(doc.id),
                label=title,
                href=f"/docs/{int(doc.id)}",
                query=query,
                values=[title, doc.url, match.get("match_excerpt")],
                summary=None,
                updated_at=doc.ingested_at,
                status=doc.triage_state,
                extra={
                    "url": doc.url or doc.canonical_url,
                    "source": doc.source.name if doc.source else None,
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
                },
            )
        )


def _query_rows(session: Session, model: Any, query: str, fields: list[str], limit: int) -> list[Any]:
    conditions = [_like_filter(model, query, fields)]
    for token in sorted(_tokens(query)):
        if len(token) < 3:
            continue
        conditions.append(_like_filter(model, token, fields))
    rows = session.query(model).filter(or_(*conditions)).limit(max(limit, 10)).all()
    out: list[Any] = []
    seen: set[Any] = set()
    for row in rows:
        key = getattr(row, "id", id(row))
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _dedupe_rows(rows: list[Any]) -> list[Any]:
    out: list[Any] = []
    seen: set[Any] = set()
    for row in rows:
        key = getattr(row, "id", id(row))
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _visible_thesis_query(query: Any, *, include_hidden: bool = False) -> Any:
    if include_hidden:
        return query
    return query.filter(~func.lower(Thesis.status).in_(sorted(THESIS_HIDDEN_STATUSES)))


def _add_model_items(
    session: Session,
    query: str,
    kind: str,
    limit: int,
    items: list[dict[str, Any]],
    *,
    include_hidden_theses: bool = False,
    institutional_context: InstitutionalSearchContext | None = None,
) -> None:
    if kind == "source":
        deleted_flag = _json_key_value(session, Source.config, "deleted").label("deleted_flag")
        conditions = [_like_filter(Source, query, ["name", "category"])]
        for token in sorted(_tokens(query)):
            if len(token) >= 3:
                conditions.append(_like_filter(Source, token, ["name", "category"]))
        rows = (
            session.query(Source.id, Source.name, Source.category, Source.priority, Source.created_at, deleted_flag)
            .filter(or_(*conditions))
            .limit(max(limit, 10))
            .all()
        )
        for row in rows:
            if _json_truthy(row.deleted_flag):
                continue
            items.append(
                _item(
                    kind="source",
                    object_id=int(row.id),
                    label=row.name,
                    href=f"/sources/{int(row.id)}",
                    query=query,
                    values=[row.name, row.category],
                    summary=row.category,
                    updated_at=row.created_at,
                    extra={"category": row.category, "priority": row.priority},
                )
            )
    elif kind == "thesis":
        conditions = [_like_filter(Thesis, query, ["title", "statement", "kind"])]
        for token in sorted(_tokens(query)):
            if len(token) >= 3:
                conditions.append(_like_filter(Thesis, token, ["title", "statement", "kind"]))
        statement_preview = func.substr(Thesis.statement, 1, 2000).label("statement_preview")
        thesis_rows = session.query(
            Thesis.id,
            Thesis.title,
            statement_preview,
            Thesis.kind,
            Thesis.status,
            Thesis.updated_at,
        ).filter(or_(*conditions))
        thesis_rows = _visible_thesis_query(thesis_rows, include_hidden=include_hidden_theses)
        rows = thesis_rows.order_by(Thesis.updated_at.desc(), Thesis.id.desc()).limit(max(limit, 10)).all()
        for row in rows:
            profile = thesis_kind_profile(row.kind)
            statement = str(row.statement_preview or "")
            items.append(
                _item(
                    kind="thesis",
                    object_id=int(row.id),
                    label=row.title,
                    href=f"/theses/{int(row.id)}",
                    query=query,
                    values=[row.title, statement, row.kind],
                    summary=statement,
                    updated_at=row.updated_at,
                    status=row.status,
                    extra={"signal_kind": profile["kind"], "kind_profile": profile},
                )
            )
    elif kind == "note":
        conditions = [_like_filter(Note, query, ["title", "body_md", "note_type"])]
        for token in sorted(_tokens(query)):
            if len(token) >= 3:
                conditions.append(_like_filter(Note, token, ["title", "body_md", "note_type"]))
        body_preview = func.substr(Note.body_md, 1, 2000).label("body_preview")
        rows = (
            session.query(
                Note.id,
                Note.title,
                body_preview,
                Note.note_type,
                Note.status,
                Note.updated_at,
            )
            .filter(or_(*conditions))
            .order_by(Note.updated_at.desc(), Note.id.desc())
            .limit(max(limit, 10))
            .all()
        )
        for row in rows:
            body = str(row.body_preview or "")
            items.append(
                _item(
                    kind="note",
                    object_id=int(row.id),
                    label=row.title,
                    href=f"/notes/{int(row.id)}",
                    query=query,
                    values=[row.title, body, row.note_type],
                    summary=body,
                    updated_at=row.updated_at,
                    status=row.status,
                    extra={"note_type": row.note_type},
                )
            )
    elif kind == "dataset":
        rows = discover_datasets(session, query=query, limit=limit).rows
        for row in rows:
            description = str(row.description or "")[:2000]
            items.append(
                _item(
                    kind="dataset",
                    object_id=int(row.id),
                    label=row.title,
                    href=f"/visualizations/datasets/{int(row.id)}",
                    query=query,
                    values=[row.title, description, row.slug, row.category, row.family, row.source_kind],
                    summary=description,
                    updated_at=row.updated_at,
                    status=row.status,
                    extra={"slug": row.slug, "category": row.category, "family": row.family, "source_kind": row.source_kind},
                )
            )
    elif kind == "chart":
        conditions = [_like_filter(Chart, query, ["title", "subtitle", "category", "description_md"])]
        for token in sorted(_tokens(query)):
            if len(token) >= 3:
                conditions.append(_like_filter(Chart, token, ["title", "subtitle", "category", "description_md"]))
        description_preview = func.substr(Chart.description_md, 1, 2000).label("description_preview")
        rows = (
            session.query(
                Chart.id,
                Chart.title,
                Chart.subtitle,
                Chart.category,
                description_preview,
                Chart.updated_at,
            )
            .filter(or_(*conditions))
            .limit(max(limit, 10))
            .all()
        )
        for row in rows:
            description = str(row.description_preview or "")
            items.append(
                _item(
                    kind="chart",
                    object_id=int(row.id),
                    label=row.title,
                    href=f"/visualizations/{int(row.id)}",
                    query=query,
                    values=[row.title, row.subtitle, row.category, description],
                    summary=description,
                    updated_at=row.updated_at,
                    extra={"category": row.category},
                )
            )
    elif kind == "company":
        for row in _query_rows(session, Company, query, ["name", "slug", "ticker", "sector", "industry"], limit):
            items.append(
                _item(
                    kind="company",
                    object_id=int(row.id),
                    label=row.name,
                    href=f"/companies/{row.slug}",
                    query=query,
                    values=[row.name, row.slug, row.ticker, row.sector, row.industry],
                    summary=" ".join(v for v in [row.ticker, row.sector, row.industry] if v),
                    extra={"slug": row.slug, "ticker": row.ticker, "sector": row.sector, "industry": row.industry},
                )
            )
    elif kind == "supply_chain":
        conditions = [_like_filter(SupplyChain, query, ["title", "description", "slug", "category", "product"])]
        for token in sorted(_tokens(query)):
            if len(token) >= 3:
                conditions.append(_like_filter(SupplyChain, token, ["title", "description", "slug", "category", "product"]))
        description_preview = func.substr(SupplyChain.description, 1, 2000).label("description_preview")
        rows = (
            session.query(
                SupplyChain.id,
                SupplyChain.slug,
                SupplyChain.title,
                description_preview,
                SupplyChain.category,
                SupplyChain.product,
                SupplyChain.status,
                SupplyChain.updated_at,
            )
            .filter(or_(*conditions))
            .limit(max(limit, 10))
            .all()
        )
        for row in rows:
            description = str(row.description_preview or "")
            items.append(
                _item(
                    kind="supply_chain",
                    object_id=int(row.id),
                    label=row.title,
                    href=f"/visualizations/supply-chains/{int(row.id)}/command-center",
                    query=query,
                    values=[row.title, description, row.slug, row.category, row.product],
                    summary=description,
                    updated_at=row.updated_at,
                    status=row.status,
                    extra={"slug": row.slug, "category": row.category, "product": row.product},
                )
            )
    elif kind == "bottleneck_model":
        search_fields = ["title", "summary", "thesis", "constrained_input", "output_unit", "regime", "scope_key", "model_key"]
        conditions = [
            _like_filter(BottleneckModel, query, search_fields),
            _json_text_like(BottleneckModel.evidence_json, query),
            _json_text_like(BottleneckModel.supplier_qualification_json, query),
            _json_text_like(BottleneckModel.payload, query),
        ]
        for token in sorted(_tokens(query)):
            if len(token) >= 3:
                conditions.append(_like_filter(BottleneckModel, token, search_fields))
                conditions.append(_json_text_like(BottleneckModel.evidence_json, token))
                conditions.append(_json_text_like(BottleneckModel.supplier_qualification_json, token))
                conditions.append(_json_text_like(BottleneckModel.payload, token))
        conditions.extend(
            [
                BottleneckModel.subject_id.in_(
                    session.query(ResearchSubject.id)
                    .filter(_like_filter(ResearchSubject, query, ["slug", "subject_key", "display_name", "legal_name", "description"]))
                    .limit(50)
                ),
                BottleneckModel.site_id.in_(
                    session.query(Site.id)
                    .filter(_like_filter(Site, query, ["slug", "name", "operator_name", "owner_name", "utility_name", "locality", "region", "country_code"]))
                    .limit(50)
                ),
                BottleneckModel.program_id.in_(
                    session.query(Program.id)
                    .filter(_like_filter(Program, query, ["slug", "title", "category", "summary"]))
                    .limit(50)
                ),
                BottleneckModel.id.in_(
                    session.query(BottleneckModelLink.model_id)
                    .filter(or_(_like_filter(BottleneckModelLink, query, ["object_type", "relation"]), _json_text_like(BottleneckModelLink.payload, query)))
                    .limit(100)
                ),
            ]
        )
        summary_preview = func.substr(BottleneckModel.summary, 1, 2000).label("summary_preview")
        thesis_preview = func.substr(BottleneckModel.thesis, 1, 2000).label("thesis_preview")
        rows = (
            session.query(
                BottleneckModel.id,
                BottleneckModel.model_key,
                BottleneckModel.title,
                BottleneckModel.scope_type,
                BottleneckModel.scope_key,
                BottleneckModel.subject_id,
                BottleneckModel.site_id,
                BottleneckModel.program_id,
                BottleneckModel.supply_chain_id,
                BottleneckModel.constrained_input,
                BottleneckModel.output_unit,
                BottleneckModel.regime,
                BottleneckModel.status,
                summary_preview,
                thesis_preview,
                BottleneckModel.updated_at,
            )
            .filter(or_(*conditions))
            .limit(max(limit, 10))
            .all()
        )
        for row in rows:
            summary = str(row.summary_preview or "")
            thesis = str(row.thesis_preview or "")
            items.append(
                _item(
                    kind="bottleneck_model",
                    object_id=int(row.id),
                    label=row.title,
                    href=f"/visualizations/supply-chains/{int(row.supply_chain_id)}/command-center" if row.supply_chain_id else "/theses",
                    query=query,
                    values=[row.title, summary, thesis, row.constrained_input, row.regime, row.scope_key],
                    summary=summary or thesis,
                    updated_at=row.updated_at,
                    status=row.status,
                    extra={
                        "model_key": row.model_key,
                        "scope_type": row.scope_type,
                        "scope_key": row.scope_key,
                        "subject_id": int(row.subject_id) if row.subject_id is not None else None,
                        "site_id": int(row.site_id) if row.site_id is not None else None,
                        "program_id": int(row.program_id) if row.program_id is not None else None,
                        "supply_chain_id": int(row.supply_chain_id) if row.supply_chain_id is not None else None,
                        "regime": row.regime,
                        "constrained_input": row.constrained_input,
                        "output_unit": row.output_unit,
                    },
                )
            )
    elif kind == "analyst_evidence":
        conditions = [
            _like_filter(
                AnalystEvidence,
                query,
                [
                    "evidence_key",
                    "evidence_kind",
                    "source_name",
                    "author",
                    "outlet",
                    "action",
                    "rating",
                    "estimate_metric",
                    "estimate_period",
                    "source_kind",
                    "summary",
                ],
            )
        ]
        for token in sorted(_tokens(query)):
            if len(token) >= 3:
                conditions.append(
                    _like_filter(
                        AnalystEvidence,
                        token,
                        [
                            "evidence_key",
                            "evidence_kind",
                            "source_name",
                            "author",
                            "outlet",
                            "action",
                            "rating",
                            "estimate_metric",
                            "estimate_period",
                            "source_kind",
                            "summary",
                        ],
                    )
                )
        summary_preview = func.substr(AnalystEvidence.summary, 1, 2000).label("summary_preview")
        rows = (
            session.query(
                AnalystEvidence.id,
                AnalystEvidence.evidence_key,
                AnalystEvidence.subject_id,
                AnalystEvidence.issuer_id,
                AnalystEvidence.company_id,
                AnalystEvidence.source_document_id,
                AnalystEvidence.evidence_kind,
                AnalystEvidence.status,
                AnalystEvidence.source_name,
                AnalystEvidence.author,
                AnalystEvidence.outlet,
                AnalystEvidence.action,
                AnalystEvidence.rating,
                AnalystEvidence.price_target,
                AnalystEvidence.currency,
                AnalystEvidence.estimate_metric,
                AnalystEvidence.estimate_period,
                AnalystEvidence.estimate_value,
                AnalystEvidence.estimate_unit,
                AnalystEvidence.evidence_tier,
                AnalystEvidence.source_kind,
                summary_preview,
                AnalystEvidence.published_at,
                AnalystEvidence.updated_at,
            )
            .filter(or_(*conditions), AnalystEvidence.status != "archived")
            .order_by(AnalystEvidence.published_at.desc().nullslast(), AnalystEvidence.updated_at.desc(), AnalystEvidence.id.desc())
            .limit(max(limit, 10))
            .all()
        )
        for row in rows:
            summary = str(row.summary_preview or "")
            metric_bits = " ".join(str(value) for value in [row.estimate_metric, row.estimate_period] if value)
            value_bits = " ".join(str(value) for value in [row.estimate_value, row.estimate_unit] if value is not None and value != "")
            source_bits = " / ".join(str(value) for value in [row.source_name, row.author, row.outlet] if value)
            label = " - ".join(part for part in [source_bits, metric_bits or row.rating or row.action, value_bits] if part) or summary[:120] or f"Analyst evidence {int(row.id)}"
            items.append(
                _item(
                    kind="analyst_evidence",
                    object_id=int(row.id),
                    label=label,
                    href="/consensus",
                    query=query,
                    values=[
                        row.evidence_key,
                        row.evidence_kind,
                        row.source_name,
                        row.author,
                        row.outlet,
                        row.action,
                        row.rating,
                        row.estimate_metric,
                        row.estimate_period,
                        summary,
                    ],
                    summary=summary,
                    updated_at=row.updated_at or row.published_at,
                    status=row.status,
                    extra={
                        "subject_id": int(row.subject_id) if row.subject_id is not None else None,
                        "issuer_id": int(row.issuer_id) if row.issuer_id is not None else None,
                        "company_id": int(row.company_id) if row.company_id is not None else None,
                        "source_document_id": int(row.source_document_id) if row.source_document_id is not None else None,
                        "evidence_kind": row.evidence_kind,
                        "source_name": row.source_name,
                        "author": row.author,
                        "outlet": row.outlet,
                        "estimate_metric": row.estimate_metric,
                        "estimate_period": row.estimate_period,
                        "estimate_value": row.estimate_value,
                        "estimate_unit": row.estimate_unit,
                        "evidence_tier": row.evidence_tier,
                        "source_kind": row.source_kind,
                    },
                )
            )
    elif kind == "investment_catalyst":
        conditions = [_like_filter(InvestmentCatalyst, query, ["catalyst_key", "title", "catalyst_kind", "source_kind", "summary"])]
        for token in sorted(_tokens(query)):
            if len(token) >= 3:
                conditions.append(_like_filter(InvestmentCatalyst, token, ["catalyst_key", "title", "catalyst_kind", "source_kind", "summary"]))
        summary_preview = func.substr(InvestmentCatalyst.summary, 1, 2000).label("summary_preview")
        rows = (
            session.query(
                InvestmentCatalyst.id,
                InvestmentCatalyst.catalyst_key,
                InvestmentCatalyst.subject_id,
                InvestmentCatalyst.issuer_id,
                InvestmentCatalyst.company_id,
                InvestmentCatalyst.source_document_id,
                InvestmentCatalyst.title,
                InvestmentCatalyst.catalyst_kind,
                InvestmentCatalyst.event_date,
                InvestmentCatalyst.event_at,
                InvestmentCatalyst.status,
                InvestmentCatalyst.priority,
                InvestmentCatalyst.evidence_tier,
                InvestmentCatalyst.source_kind,
                summary_preview,
                InvestmentCatalyst.source_url,
                InvestmentCatalyst.updated_at,
            )
            .filter(or_(*conditions), InvestmentCatalyst.status != "archived")
            .order_by(InvestmentCatalyst.event_date.desc().nullslast(), InvestmentCatalyst.updated_at.desc(), InvestmentCatalyst.id.desc())
            .limit(max(limit, 10))
            .all()
        )
        for row in rows:
            summary = str(row.summary_preview or "")
            items.append(
                _item(
                    kind="investment_catalyst",
                    object_id=int(row.id),
                    label=row.title,
                    href="/calendar",
                    query=query,
                    values=[row.catalyst_key, row.title, row.catalyst_kind, row.source_kind, summary],
                    summary=summary,
                    updated_at=row.updated_at,
                    status=row.status,
                    extra={
                        "subject_id": int(row.subject_id) if row.subject_id is not None else None,
                        "issuer_id": int(row.issuer_id) if row.issuer_id is not None else None,
                        "company_id": int(row.company_id) if row.company_id is not None else None,
                        "source_document_id": int(row.source_document_id) if row.source_document_id is not None else None,
                        "catalyst_kind": row.catalyst_kind,
                        "event_date": row.event_date.isoformat() if row.event_date else None,
                        "event_at": _iso(row.event_at),
                        "priority": row.priority,
                        "evidence_tier": row.evidence_tier,
                        "source_kind": row.source_kind,
                        "source_url": row.source_url,
                    },
                )
            )
    elif kind == "institutional_evidence":
        context = institutional_context or resolve_search_context(session, query)
        provenance_preview = func.substr(InstitutionalEvidenceItem.provenance_summary, 1, 2000).label("provenance_preview")
        rows = (
            ranked_query(session, InstitutionalEvidenceItem, [
                InstitutionalEvidenceItem.id,
                InstitutionalEvidenceItem.evidence_key,
                InstitutionalEvidenceItem.document_id,
                InstitutionalEvidenceItem.subject_id,
                InstitutionalEvidenceItem.issuer_id,
                InstitutionalEvidenceItem.company_id,
                InstitutionalEvidenceItem.site_id,
                InstitutionalEvidenceItem.program_id,
                InstitutionalEvidenceItem.title,
                InstitutionalEvidenceItem.evidence_kind,
                provenance_preview,
                InstitutionalEvidenceItem.url,
                InstitutionalEvidenceItem.canonical_url,
                InstitutionalEvidenceItem.published_at,
                InstitutionalEvidenceItem.observed_at,
                InstitutionalEvidenceItem.rights_class,
                InstitutionalEvidenceItem.review_required,
                InstitutionalEvidenceItem.confidence,
                InstitutionalEvidenceItem.updated_at,
            ], context, limit)
            .all()
        )
        for row in rows:
            provenance = str(row.provenance_preview or "")
            items.append(
                _item(
                    kind="institutional_evidence",
                    rank=int(row.search_rank),
                    object_id=int(row.id),
                    label=row.title,
                    href=f"/docs/{int(row.document_id)}" if row.document_id is not None else "/institutional",
                    query=query,
                    values=[row.evidence_key, row.title, row.evidence_kind, provenance, row.rights_class],
                    summary=provenance,
                    updated_at=row.updated_at or row.observed_at or row.published_at,
                    status="review_required" if row.review_required else None,
                    extra={
                        "document_id": int(row.document_id) if row.document_id is not None else None,
                        "subject_id": int(row.subject_id) if row.subject_id is not None else None,
                        "issuer_id": int(row.issuer_id) if row.issuer_id is not None else None,
                        "company_id": int(row.company_id) if row.company_id is not None else None,
                        "site_id": int(row.site_id) if row.site_id is not None else None,
                        "program_id": int(row.program_id) if row.program_id is not None else None,
                        "evidence_kind": row.evidence_kind,
                        "rights_class": row.rights_class,
                        "confidence": row.confidence,
                        "url": row.url or row.canonical_url,
                    },
                )
            )
    elif kind == "institutional_observation":
        context = institutional_context or resolve_search_context(session, query)
        value_preview = func.substr(InstitutionalObservation.value_text, 1, 2000).label("value_preview")
        rows = (
            ranked_query(session, InstitutionalObservation, [
                InstitutionalObservation.id,
                InstitutionalObservation.observation_key,
                InstitutionalObservation.evidence_id,
                InstitutionalObservation.source_id,
                InstitutionalObservation.subject_id,
                InstitutionalObservation.issuer_id,
                InstitutionalObservation.company_id,
                InstitutionalObservation.site_id,
                InstitutionalObservation.program_id,
                InstitutionalObservation.dataset_family,
                InstitutionalObservation.kind,
                InstitutionalObservation.metric_key,
                InstitutionalObservation.value_number,
                value_preview,
                InstitutionalObservation.unit,
                InstitutionalObservation.currency,
                InstitutionalObservation.confidence,
                InstitutionalObservation.period_date,
                InstitutionalObservation.as_of_date,
                InstitutionalObservation.observed_at,
                InstitutionalObservation.review_status,
                InstitutionalObservation.updated_at,
            ], context, limit)
            .all()
        )
        for row in rows:
            value_text = str(row.value_preview or "")
            value_bits = " ".join(str(value) for value in [row.value_number, row.unit or row.currency] if value is not None and value != "")
            label = " - ".join(part for part in [row.metric_key or row.kind, value_bits or value_text[:80]] if part) or f"Institutional observation {int(row.id)}"
            items.append(
                _item(
                    kind="institutional_observation",
                    rank=int(row.search_rank),
                    object_id=int(row.id),
                    label=label,
                    href="/institutional",
                    query=query,
                    values=[row.observation_key, row.dataset_family, row.kind, row.metric_key, value_text, row.unit, row.currency, row.review_status],
                    summary=value_text,
                    updated_at=row.updated_at or row.observed_at,
                    status=row.review_status,
                    extra={
                        "evidence_id": int(row.evidence_id) if row.evidence_id is not None else None,
                        "source_id": int(row.source_id) if row.source_id is not None else None,
                        "subject_id": int(row.subject_id) if row.subject_id is not None else None,
                        "issuer_id": int(row.issuer_id) if row.issuer_id is not None else None,
                        "company_id": int(row.company_id) if row.company_id is not None else None,
                        "site_id": int(row.site_id) if row.site_id is not None else None,
                        "program_id": int(row.program_id) if row.program_id is not None else None,
                        "dataset_family": row.dataset_family,
                        "observation_kind": row.kind,
                        "metric_key": row.metric_key,
                        "value_number": row.value_number,
                        "unit": row.unit,
                        "currency": row.currency,
                        "period_date": row.period_date.isoformat() if row.period_date else None,
                        "as_of_date": row.as_of_date.isoformat() if row.as_of_date else None,
                        "confidence": row.confidence,
                    },
                )
            )
    elif kind == "source_actor":
        conditions = [
            or_(
                _like_filter(SourceActor, query, ["actor_key", "display_name", "actor_type", "side", "trust_label", "notes"]),
                _like_filter(SourceActorAlias, query, ["alias", "normalized_alias", "alias_kind", "platform"]),
            )
        ]
        for token in sorted(_tokens(query)):
            if len(token) >= 3:
                conditions.append(
                    or_(
                        _like_filter(SourceActor, token, ["actor_key", "display_name", "actor_type", "side", "trust_label", "notes"]),
                        _like_filter(SourceActorAlias, token, ["alias", "normalized_alias", "alias_kind", "platform"]),
                    )
                )
        notes_preview = func.substr(SourceActor.notes, 1, 2000).label("notes_preview")
        rows = (
            session.query(
                SourceActor.id,
                SourceActor.actor_key,
                SourceActor.display_name,
                SourceActor.actor_type,
                SourceActor.side,
                SourceActor.status,
                SourceActor.trust_label,
                notes_preview,
                SourceActor.updated_at,
            )
            .outerjoin(SourceActorAlias, SourceActorAlias.actor_id == SourceActor.id)
            .filter(or_(*conditions), SourceActor.status != "archived")
            .order_by(SourceActor.updated_at.desc(), SourceActor.id.desc())
            .limit(max(limit * 2, 20))
            .all()
        )
        seen_actor_ids: set[int] = set()
        for row in rows:
            if int(row.id) in seen_actor_ids:
                continue
            seen_actor_ids.add(int(row.id))
            notes = str(row.notes_preview or "")
            items.append(
                _item(
                    kind="source_actor",
                    object_id=int(row.id),
                    label=row.display_name,
                    href="/consensus",
                    query=query,
                    values=[row.actor_key, row.display_name, row.actor_type, row.side, row.trust_label, notes],
                    summary=notes,
                    updated_at=row.updated_at,
                    status=row.status,
                    extra={
                        "actor_key": row.actor_key,
                        "actor_type": row.actor_type,
                        "side": row.side,
                        "trust_label": row.trust_label,
                    },
                )
            )
            if len(seen_actor_ids) >= max(limit, 10):
                break
    elif kind == "signal_extraction":
        numeric_query = query.isdigit()
        conditions = [
            cast(LocalAgentDocumentCompression.document_id, String).ilike(f"%{query}%"),
            _like_filter(Document, query, ["title", "url", "canonical_url", "external_id"]),
        ]
        if numeric_query:
            conditions.append(LocalAgentDocumentCompression.document_id == int(query))
        else:
            route_match_ids = select(LocalAgentEntityRoute.compression_id).where(
                LocalAgentEntityRoute.compression_id.isnot(None),
                or_(*_signal_route_match_conditions(query)),
            )
            conditions.extend(
                [
                    _like_filter(
                        LocalAgentDocumentCompression,
                        query,
                        [
                            "summary_short",
                            "summary_detail",
                            "information_gain",
                            "document_disposition",
                            "prompt_version",
                            "model",
                            "status",
                        ],
                    ),
                    _json_text_like(LocalAgentDocumentCompression.atomic_claims_json, query),
                    _json_text_like(LocalAgentDocumentCompression.entity_refs_json, query),
                    _json_text_like(LocalAgentDocumentCompression.supply_chain_refs_json, query),
                    _json_text_like(LocalAgentDocumentCompression.technology_refs_json, query),
                    _json_text_like(LocalAgentDocumentCompression.unresolved_entities_json, query),
                    _json_text_like(Document.extra, query),
                    LocalAgentDocumentCompression.id.in_(route_match_ids),
                ]
            )
        for token in sorted(_tokens(query)):
            if len(token) >= 3:
                if token.isdigit():
                    conditions.append(
                        or_(
                            cast(LocalAgentDocumentCompression.document_id, String).ilike(f"%{token}%"),
                            LocalAgentDocumentCompression.document_id == int(token),
                            _like_filter(Document, token, ["title", "url", "canonical_url", "external_id"]),
                        )
                    )
                else:
                    token_route_match_ids = select(LocalAgentEntityRoute.compression_id).where(
                        LocalAgentEntityRoute.compression_id.isnot(None),
                        or_(*_signal_route_match_conditions(token)),
                    )
                    conditions.append(
                        or_(
                            _like_filter(
                                LocalAgentDocumentCompression,
                                token,
                                ["summary_short", "summary_detail", "information_gain", "document_disposition", "prompt_version", "model", "status"],
                            ),
                            _json_text_like(LocalAgentDocumentCompression.atomic_claims_json, token),
                            _json_text_like(LocalAgentDocumentCompression.entity_refs_json, token),
                            _json_text_like(LocalAgentDocumentCompression.supply_chain_refs_json, token),
                            _json_text_like(LocalAgentDocumentCompression.technology_refs_json, token),
                            _json_text_like(LocalAgentDocumentCompression.unresolved_entities_json, token),
                            _like_filter(Document, token, ["title", "url", "canonical_url", "external_id"]),
                            _json_text_like(Document.extra, token),
                            LocalAgentDocumentCompression.id.in_(token_route_match_ids),
                        )
                    )
        summary_preview = func.substr(LocalAgentDocumentCompression.summary_detail, 1, 2000).label("summary_preview")
        atomic_claims_preview = func.substr(cast(LocalAgentDocumentCompression.atomic_claims_json, String), 1, 2000).label("atomic_claims_preview")
        entity_refs_preview = func.substr(cast(LocalAgentDocumentCompression.entity_refs_json, String), 1, 2000).label("entity_refs_preview")
        supply_chain_refs_preview = func.substr(cast(LocalAgentDocumentCompression.supply_chain_refs_json, String), 1, 2000).label("supply_chain_refs_preview")
        technology_refs_preview = func.substr(cast(LocalAgentDocumentCompression.technology_refs_json, String), 1, 2000).label("technology_refs_preview")
        unresolved_entities_preview = func.substr(cast(LocalAgentDocumentCompression.unresolved_entities_json, String), 1, 2000).label("unresolved_entities_preview")
        doc_extra_preview = func.substr(cast(Document.extra, String), 1, 2000).label("doc_extra_preview")
        rows = (
            session.query(
                LocalAgentDocumentCompression.id,
                LocalAgentDocumentCompression.document_id,
                LocalAgentDocumentCompression.model,
                LocalAgentDocumentCompression.prompt_version,
                LocalAgentDocumentCompression.summary_short,
                summary_preview,
                atomic_claims_preview,
                entity_refs_preview,
                supply_chain_refs_preview,
                technology_refs_preview,
                unresolved_entities_preview,
                LocalAgentDocumentCompression.information_gain,
                LocalAgentDocumentCompression.document_disposition,
                LocalAgentDocumentCompression.confidence,
                LocalAgentDocumentCompression.status,
                LocalAgentDocumentCompression.updated_at,
                Document.title.label("document_title"),
                Document.external_id.label("document_external_id"),
                Document.url.label("document_url"),
                Document.canonical_url.label("document_canonical_url"),
                doc_extra_preview,
            )
            .join(Document, Document.id == LocalAgentDocumentCompression.document_id)
            .filter(or_(*conditions))
            .order_by(LocalAgentDocumentCompression.updated_at.desc(), LocalAgentDocumentCompression.id.desc())
            .limit(max(limit * 2, 20))
            .all()
        )
        route_text_by_compression: dict[int, list[str]] = defaultdict(list)
        compression_ids = [int(row.id) for row in rows]
        if compression_ids:
            route_rows = (
                session.query(
                    LocalAgentEntityRoute.compression_id,
                    LocalAgentEntityRoute.entity_name,
                    LocalAgentEntityRoute.entity_type,
                    LocalAgentEntityRoute.durable_action,
                    LocalAgentEntityRoute.materiality,
                    LocalAgentEntityRoute.novelty,
                    LocalAgentEntityRoute.new_information,
                    LocalAgentEntityRoute.rationale,
                    LocalAgentEntityRoute.destination_type,
                    LocalAgentEntityRoute.claim_refs_json,
                    LocalAgentEntityRoute.payload,
                )
                .filter(LocalAgentEntityRoute.compression_id.in_(compression_ids))
                .order_by(LocalAgentEntityRoute.compression_id.asc(), LocalAgentEntityRoute.id.asc())
                .limit(200)
                .all()
            )
            for route in route_rows:
                parts = [
                    route.entity_name,
                    route.entity_type,
                    route.durable_action,
                    route.materiality,
                    route.novelty,
                    route.new_information,
                    route.rationale,
                    route.destination_type,
                    route.claim_refs_json,
                    route.payload,
                ]
                route_text_by_compression[int(route.compression_id)].append(" ".join(str(part or "") for part in parts if part is not None)[:1200])
        seen_compressions: set[int] = set()
        for row in rows:
            compression_id = int(row.id)
            if compression_id in seen_compressions:
                continue
            seen_compressions.add(compression_id)
            summary = row.summary_short or row.summary_preview or ""
            title = row.document_title or row.document_url or f"document {int(row.document_id)}"
            route_text = " ".join(route_text_by_compression.get(compression_id, [])[:6])
            items.append(
                _item(
                    kind="signal_extraction",
                    object_id=compression_id,
                    label=f"{title} - signal extraction #{compression_id}",
                    href=f"/docs/{int(row.document_id)}",
                    query=query,
                    values=[
                        str(row.document_id),
                        row.summary_short,
                        row.summary_preview,
                        row.atomic_claims_preview,
                        row.entity_refs_preview,
                        row.supply_chain_refs_preview,
                        row.technology_refs_preview,
                        row.unresolved_entities_preview,
                        row.information_gain,
                        row.document_disposition,
                        row.prompt_version,
                        row.model,
                        row.status,
                        row.document_title,
                        row.document_external_id,
                        row.document_url,
                        row.document_canonical_url,
                        row.doc_extra_preview,
                        route_text,
                    ],
                    summary=summary,
                    updated_at=row.updated_at,
                    status=row.status,
                    extra={
                        "document_id": int(row.document_id),
                        "document_title": row.document_title,
                        "information_gain": row.information_gain,
                        "document_disposition": row.document_disposition,
                        "confidence": row.confidence,
                        "prompt_version": row.prompt_version,
                        "model": row.model,
                        "matched_route_text": route_text[:500] or None,
                    },
                )
            )
            if len(seen_compressions) >= max(limit, 10):
                break
    elif kind == "research_report":
        for summary in list_research_reports(session, query=query, limit=limit):
            items.append(
                _item(
                    kind="research_report",
                    object_id=int(summary.get("id") or 0),
                    label=summary.get("title") or summary.get("display_name"),
                    href=str(summary.get("href") or f"/reports/{summary.get('slug')}"),
                    query=query,
                    values=[
                        summary.get("title"),
                        summary.get("display_name"),
                        summary.get("slug"),
                        summary.get("subject_key"),
                        summary.get("report_type"),
                        summary.get("summary"),
                        " ".join(summary.get("tags") or []),
                        " ".join(summary.get("topics") or []),
                        " ".join(
                            " ".join(
                                str(value or "")
                                for value in (
                                    addendum.get("title"),
                                    addendum.get("summary"),
                                    addendum.get("body_preview"),
                                )
                            )
                            for addendum in (summary.get("manual_addenda") or [])
                            if isinstance(addendum, dict)
                        ),
                        " ".join(summary.get("materials") or []),
                        " ".join(summary.get("technologies") or []),
                    ],
                    summary=summary.get("summary") or summary.get("subject_key"),
                    updated_at=None,
                    status=summary.get("status"),
                    extra={
                        "slug": summary.get("slug"),
                        "subject_key": summary.get("subject_key"),
                        "report_type": summary.get("report_type"),
                        "topics": summary.get("topics") or [],
                        "tags": summary.get("tags") or [],
                        "manual_addenda": summary.get("manual_addenda") or [],
                    },
                )
            )
    elif kind == "research_subject":
        conditions = [_like_filter(ResearchSubject, query, ["display_name", "legal_name", "description", "slug", "subject_key", "kind"])]
        for token in sorted(_tokens(query)):
            if len(token) >= 3:
                conditions.append(_like_filter(ResearchSubject, token, ["display_name", "legal_name", "description", "slug", "subject_key", "kind"]))
        description_preview = func.substr(ResearchSubject.description, 1, 2000).label("description_preview")
        rows = (
            session.query(
                ResearchSubject.id,
                ResearchSubject.display_name,
                ResearchSubject.legal_name,
                description_preview,
                ResearchSubject.slug,
                ResearchSubject.subject_key,
                ResearchSubject.kind,
                ResearchSubject.status,
                ResearchSubject.updated_at,
            )
            .filter(or_(*conditions))
            .limit(max(limit, 10))
            .all()
        )
        for row in rows:
            description = str(row.description_preview or "")
            items.append(
                _item(
                    kind="research_subject",
                    object_id=int(row.id),
                    label=row.display_name,
                    href=f"/reports/{row.slug}",
                    query=query,
                    values=[row.display_name, row.legal_name, description, row.slug, row.subject_key, row.kind],
                    summary=description,
                    updated_at=row.updated_at,
                    status=row.status,
                    extra={"slug": row.slug, "subject_key": row.subject_key, "subject_kind": row.kind},
                )
            )
    elif kind == "knowledge_asset":
        conditions = [_like_filter(KnowledgeAsset, query, ["title", "summary", "slug", "kind"])]
        for token in sorted(_tokens(query)):
            if len(token) >= 3:
                conditions.append(_like_filter(KnowledgeAsset, token, ["title", "summary", "slug", "kind"]))
        summary_preview = func.substr(KnowledgeAsset.summary, 1, 2000).label("summary_preview")
        rows = (
            session.query(
                KnowledgeAsset.id,
                KnowledgeAsset.subject_id,
                KnowledgeAsset.slug,
                KnowledgeAsset.kind,
                KnowledgeAsset.title,
                summary_preview,
                KnowledgeAsset.status,
                KnowledgeAsset.updated_at,
            )
            .filter(or_(*conditions))
            .limit(max(limit, 10))
            .all()
        )
        for row in rows:
            summary = str(row.summary_preview or "")
            items.append(
                _item(
                    kind="knowledge_asset",
                    object_id=int(row.id),
                    label=row.title,
                    href=f"/knowledge/{row.slug}",
                    query=query,
                    values=[row.title, summary, row.slug, row.kind],
                    summary=summary,
                    updated_at=row.updated_at,
                    status=row.status,
                    extra={"slug": row.slug, "asset_kind": row.kind, "subject_id": int(row.subject_id)},
                )
            )
    elif kind == "research_play":
        conditions = [_like_filter(ResearchPlay, query, ["name", "slug", "description", "owner", "status"])]
        for token in sorted(_tokens(query)):
            if len(token) >= 3:
                conditions.append(_like_filter(ResearchPlay, token, ["name", "slug", "description", "owner", "status"]))
        description_preview = func.substr(ResearchPlay.description, 1, 2000).label("description_preview")
        rows = (
            session.query(
                ResearchPlay.id,
                ResearchPlay.slug,
                ResearchPlay.name,
                description_preview,
                ResearchPlay.owner,
                ResearchPlay.status,
                ResearchPlay.updated_at,
            )
            .filter(or_(*conditions))
            .limit(max(limit, 10))
            .all()
        )
        for row in rows:
            description = str(row.description_preview or "")
            items.append(
                _item(
                    kind="research_play",
                    object_id=int(row.id),
                    label=row.name,
                    href=f"/research/plays/{int(row.id)}",
                    query=query,
                    values=[row.name, row.slug, description, row.owner],
                    summary=description,
                    updated_at=row.updated_at,
                    status=row.status,
                    extra={"slug": row.slug, "owner": row.owner},
                )
            )
    elif kind == "instrument_request":
        conditions = [_like_filter(InstrumentRequest, query, ["title", "intent", "summary", "status"])]
        for token in sorted(_tokens(query)):
            if len(token) >= 3:
                conditions.append(_like_filter(InstrumentRequest, token, ["title", "intent", "summary", "status"]))
        summary_preview = func.substr(InstrumentRequest.summary, 1, 2000).label("summary_preview")
        rows = (
            session.query(
                InstrumentRequest.id,
                InstrumentRequest.title,
                InstrumentRequest.intent,
                summary_preview,
                InstrumentRequest.status,
                InstrumentRequest.updated_at,
            )
            .filter(or_(*conditions))
            .limit(max(limit, 10))
            .all()
        )
        for row in rows:
            summary = str(row.summary_preview or "")
            items.append(
                _item(
                    kind="instrument_request",
                    object_id=int(row.id),
                    label=row.title,
                    href=f"/instruments/requests/{int(row.id)}",
                    query=query,
                    values=[row.title, row.intent, summary, row.status],
                    summary=summary,
                    updated_at=row.updated_at,
                    status=row.status,
                    extra={"intent": row.intent},
                )
            )
    elif kind == "product_stage":
        conditions = [_like_filter(ProductStageEntry, query, ["company_name", "product_name", "stage", "notes", "status"])]
        for token in sorted(_tokens(query)):
            if len(token) >= 3:
                conditions.append(_like_filter(ProductStageEntry, token, ["company_name", "product_name", "stage", "notes", "status"]))
        notes_preview = func.substr(ProductStageEntry.notes, 1, 2000).label("notes_preview")
        rows = (
            session.query(
                ProductStageEntry.id,
                ProductStageEntry.company_name,
                ProductStageEntry.product_name,
                ProductStageEntry.stage,
                notes_preview,
                ProductStageEntry.status,
                ProductStageEntry.updated_at,
            )
            .filter(or_(*conditions))
            .limit(max(limit, 10))
            .all()
        )
        for row in rows:
            label = f"{row.company_name} - {row.product_name} - {row.stage}"
            notes = str(row.notes_preview or "")
            items.append(
                _item(
                    kind="product_stage",
                    object_id=int(row.id),
                    label=label,
                    href=f"/stage-tracker/{int(row.id)}",
                    query=query,
                    values=[row.company_name, row.product_name, row.stage, notes, row.status],
                    summary=notes,
                    updated_at=row.updated_at,
                    status=row.status,
                    extra={"company_name": row.company_name, "product_name": row.product_name, "stage": row.stage},
                )
            )
    elif kind == "site":
        conditions = [_like_filter(Site, query, ["name", "slug", "site_kind", "operator_name", "owner_name"])]
        for token in sorted(_tokens(query)):
            if len(token) >= 3:
                conditions.append(_like_filter(Site, token, ["name", "slug", "site_kind", "operator_name", "owner_name"]))
        rows = (
            session.query(
                Site.id,
                Site.slug,
                Site.name,
                Site.site_kind,
                Site.operator_name,
                Site.owner_name,
                Site.utility_name,
                Site.status,
                Site.updated_at,
            )
            .filter(or_(*conditions))
            .limit(max(limit, 10))
            .all()
        )
        for row in rows:
            items.append(
                _item(
                    kind="site",
                    object_id=int(row.id),
                    label=row.name,
                    href=f"/institutional/sites/{int(row.id)}",
                    query=query,
                    values=[row.name, row.slug, row.site_kind, row.operator_name, row.owner_name, row.utility_name],
                    summary=row.site_kind,
                    updated_at=row.updated_at,
                    status=row.status,
                    extra={"slug": row.slug, "site_kind": row.site_kind},
                )
            )
    elif kind == "program":
        conditions = [_like_filter(Program, query, ["title", "slug", "summary", "category", "status"])]
        for token in sorted(_tokens(query)):
            if len(token) >= 3:
                conditions.append(_like_filter(Program, token, ["title", "slug", "summary", "category", "status"]))
        summary_preview = func.substr(Program.summary, 1, 2000).label("summary_preview")
        rows = (
            session.query(
                Program.id,
                Program.slug,
                Program.title,
                summary_preview,
                Program.category,
                Program.status,
                Program.updated_at,
            )
            .filter(or_(*conditions))
            .limit(max(limit, 10))
            .all()
        )
        for row in rows:
            summary = str(row.summary_preview or "")
            items.append(
                _item(
                    kind="program",
                    object_id=int(row.id),
                    label=row.title,
                    href=f"/institutional/programs/{int(row.id)}",
                    query=query,
                    values=[row.title, row.slug, summary, row.category, row.status],
                    summary=summary,
                    updated_at=row.updated_at,
                    status=row.status,
                    extra={"slug": row.slug, "category": row.category},
                )
            )
    elif kind == "proposition":
        conditions = [_like_filter(Proposition, query, ["key", "kind", "description", "unit"])]
        for token in sorted(_tokens(query)):
            if len(token) >= 3:
                conditions.append(_like_filter(Proposition, token, ["key", "kind", "description", "unit"]))
        description_preview = func.substr(Proposition.description, 1, 2000).label("description_preview")
        rows = (
            session.query(
                Proposition.id,
                Proposition.key,
                Proposition.kind,
                description_preview,
                Proposition.unit,
                Proposition.value_type,
            )
            .filter(or_(*conditions))
            .limit(max(limit, 10))
            .all()
        )
        for row in rows:
            description = str(row.description_preview or "")
            items.append(
                _item(
                    kind="proposition",
                    object_id=int(row.id),
                    label=row.key,
                    href=f"/theses?proposition_id={int(row.id)}",
                    query=query,
                    values=[row.key, row.kind, description, row.unit],
                    summary=description,
                    extra={"proposition_kind": row.kind, "unit": row.unit, "value_type": row.value_type},
                )
            )
    elif kind == "observation":
        conditions = [_like_filter(Observation, query, ["value_text", "note", "unit", "estimate_flag"])]
        for token in sorted(_tokens(query)):
            if len(token) >= 3:
                conditions.append(_like_filter(Observation, token, ["value_text", "note", "unit", "estimate_flag"]))
        value_preview = func.substr(Observation.value_text, 1, 2000).label("value_preview")
        note_preview = func.substr(Observation.note, 1, 2000).label("note_preview")
        rows = (
            session.query(
                Observation.id,
                Observation.proposition_id,
                Observation.document_id,
                value_preview,
                note_preview,
                Observation.unit,
                Observation.estimate_flag,
                Observation.created_at,
            )
            .filter(or_(*conditions))
            .limit(max(limit, 10))
            .all()
        )
        for row in rows:
            value_text = str(row.value_preview or "")
            note = str(row.note_preview or "")
            label = note or value_text or f"Observation {int(row.id)}"
            items.append(
                _item(
                    kind="observation",
                    object_id=int(row.id),
                    label=label,
                    href=f"/docs/{int(row.document_id)}" if row.document_id else "/theses",
                    query=query,
                    values=[value_text, note, row.unit, row.estimate_flag],
                    summary=note or value_text,
                    updated_at=row.created_at,
                    extra={"document_id": int(row.document_id) if row.document_id else None, "proposition_id": int(row.proposition_id)},
                )
            )


def _relation_item(kind: str, object_id: int, label: object, relation: object = None, href: str | None = None) -> dict[str, Any]:
    return {
        "id": f"{kind}:{int(object_id)}",
        "kind": kind,
        "object_id": int(object_id),
        "label": str(label or f"{kind} {object_id}")[:180],
        "relation": str(relation or "").strip() or None,
        "href": href,
    }


def _relation_value(row: object, default: str = "context") -> object:
    return getattr(row, "relation", None) or default


def _neighborhood_payload(item: dict[str, Any], related: list[dict[str, Any]]) -> dict[str, Any]:
    kind = str(item.get("kind") or "")
    return {
        "seed": {key: item.get(key) for key in ("id", "kind", "object_id", "label", "href")},
        "related": related[:16],
        "suggested_tools": _default_next_tools(kind),
    }


def _neighborhood(session: Session, item: dict[str, Any]) -> dict[str, Any]:
    kind = str(item.get("kind") or "")
    object_id = int(item.get("object_id") or 0)
    related: list[dict[str, Any]] = []
    if kind == "document":
        evidence_rows = (
            session.query(ThesisEvidence, Thesis)
            .join(Thesis, Thesis.id == ThesisEvidence.thesis_id)
            .filter(ThesisEvidence.document_id == object_id)
            .filter(~func.lower(Thesis.status).in_(sorted(THESIS_HIDDEN_STATUSES)))
            .limit(8)
            .all()
        )
        related.extend(_relation_item("thesis", int(th.id), th.title, ev.polarity, f"/theses/{int(th.id)}") for ev, th in evidence_rows)
        note_rows = (
            session.query(NoteDocument, Note)
            .join(Note, Note.id == NoteDocument.note_id)
            .filter(NoteDocument.document_id == object_id)
            .limit(8)
            .all()
        )
        related.extend(_relation_item("note", int(note.id), note.title, _relation_value(link), f"/notes/{int(note.id)}") for link, note in note_rows)
    elif kind == "thesis":
        evidence_rows = (
            session.query(ThesisEvidence, Document)
            .join(Document, Document.id == ThesisEvidence.document_id)
            .filter(ThesisEvidence.thesis_id == object_id)
            .limit(8)
            .all()
        )
        related.extend(_relation_item("document", int(doc.id), doc.title or doc.url, ev.polarity, f"/docs/{int(doc.id)}") for ev, doc in evidence_rows)
        note_rows = (
            session.query(NoteThesis, Note)
            .join(Note, Note.id == NoteThesis.note_id)
            .filter(NoteThesis.thesis_id == object_id)
            .limit(6)
            .all()
        )
        related.extend(_relation_item("note", int(note.id), note.title, link.relation, f"/notes/{int(note.id)}") for link, note in note_rows)
        prop_rows = (
            session.query(ThesisProposition, Proposition)
            .join(Proposition, Proposition.id == ThesisProposition.proposition_id)
            .filter(ThesisProposition.thesis_id == object_id)
            .limit(6)
            .all()
        )
        related.extend(_relation_item("proposition", int(prop.id), prop.key, _relation_value(link, "references"), None) for link, prop in prop_rows)
    elif kind == "note":
        thesis_rows = (
            session.query(NoteThesis, Thesis)
            .join(Thesis, Thesis.id == NoteThesis.thesis_id)
            .filter(NoteThesis.note_id == object_id)
            .limit(8)
            .all()
        )
        related.extend(_relation_item("thesis", int(th.id), th.title, link.relation, f"/theses/{int(th.id)}") for link, th in thesis_rows)
        doc_rows = (
            session.query(NoteDocument, Document)
            .join(Document, Document.id == NoteDocument.document_id)
            .filter(NoteDocument.note_id == object_id)
            .limit(8)
            .all()
        )
        related.extend(_relation_item("document", int(doc.id), doc.title or doc.url, _relation_value(link), f"/docs/{int(doc.id)}") for link, doc in doc_rows)
        prop_rows = (
            session.query(NoteProposition, Proposition)
            .join(Proposition, Proposition.id == NoteProposition.proposition_id)
            .filter(NoteProposition.note_id == object_id)
            .limit(6)
            .all()
        )
        related.extend(
            _relation_item("proposition", int(prop.id), prop.key, _relation_value(link, "references"), None)
            for link, prop in prop_rows
        )
    elif kind == "dataset":
        links = session.query(DatasetLink).filter(DatasetLink.dataset_id == object_id).limit(12).all()
        related.extend(
            _relation_item(str(link.object_type), int(link.object_id), f"{link.object_type} {int(link.object_id)}", link.relation, None)
            for link in links
        )
        bindings = session.query(SupplyChainMetricBinding).filter(SupplyChainMetricBinding.dataset_id == object_id).limit(8).all()
        for binding in bindings:
            snapshot = session.query(SupplyChainSnapshot).filter_by(id=int(binding.snapshot_id)).first()
            if snapshot:
                chain = session.query(SupplyChain).filter_by(id=int(snapshot.chain_id)).first()
                if chain:
                    related.append(_relation_item("supply_chain", int(chain.id), chain.title, binding.metric_kind, f"/visualizations/supply-chains/{int(chain.id)}/command-center"))
    elif kind == "supply_chain":
        links = session.query(SupplyChainLink).filter(SupplyChainLink.chain_id == object_id).limit(12).all()
        related.extend(
            _relation_item(str(link.object_type), int(link.object_id), f"{link.object_type} {int(link.object_id)}", link.relation, None)
            for link in links
        )
        snapshot = (
            session.query(SupplyChainSnapshot)
            .filter(SupplyChainSnapshot.chain_id == object_id)
            .order_by(SupplyChainSnapshot.updated_at.desc(), SupplyChainSnapshot.id.desc())
            .first()
        )
        if snapshot:
            bindings = session.query(SupplyChainMetricBinding).filter_by(snapshot_id=int(snapshot.id)).limit(10).all()
            for binding in bindings:
                if binding.dataset:
                    related.append(_relation_item("dataset", int(binding.dataset.id), binding.dataset.title, binding.metric_kind, f"/visualizations/datasets/{int(binding.dataset.id)}"))
        models = session.query(BottleneckModel).filter_by(supply_chain_id=object_id).order_by(BottleneckModel.updated_at.desc()).limit(8).all()
        related.extend(_relation_item("bottleneck_model", int(row.id), row.title, row.regime, f"/visualizations/supply-chains/{object_id}/command-center") for row in models)
    elif kind == "bottleneck_model":
        row = session.query(BottleneckModel).filter_by(id=object_id).first()
        if row:
            if row.subject_id:
                subject = session.query(ResearchSubject).filter_by(id=int(row.subject_id)).first()
                if subject:
                    related.append(_relation_item("research_subject", int(subject.id), subject.display_name, "subject", f"/reports/{subject.slug}"))
            if row.site_id:
                site = session.query(Site).filter_by(id=int(row.site_id)).first()
                if site:
                    related.append(_relation_item("site", int(site.id), site.name, "site", None))
            if row.program_id:
                program = session.query(Program).filter_by(id=int(row.program_id)).first()
                if program:
                    related.append(_relation_item("program", int(program.id), program.title, "program", None))
            if row.supply_chain_id:
                chain = session.query(SupplyChain).filter_by(id=int(row.supply_chain_id)).first()
                if chain:
                    related.append(_relation_item("supply_chain", int(chain.id), chain.title, "supply_chain", f"/visualizations/supply-chains/{int(chain.id)}/command-center"))
            links = session.query(BottleneckModelLink).filter_by(model_id=int(row.id)).order_by(BottleneckModelLink.relation.asc(), BottleneckModelLink.id.asc()).limit(12).all()
            related.extend(
                _relation_item(str(link.object_type), int(link.object_id), f"{link.object_type} {int(link.object_id)}", link.relation, None)
                for link in links
            )
    elif kind == "analyst_evidence":
        row = session.query(AnalystEvidence).filter_by(id=object_id).first()
        if row:
            if row.source_document_id is not None:
                doc = session.query(Document).filter_by(id=int(row.source_document_id)).first()
                if doc:
                    related.append(_relation_item("document", int(doc.id), doc.title or doc.url, "source_document", f"/docs/{int(doc.id)}"))
            if row.subject_id is not None:
                subject = session.query(ResearchSubject).filter_by(id=int(row.subject_id)).first()
                if subject:
                    related.append(_relation_item("research_subject", int(subject.id), subject.display_name, "subject", f"/reports/{subject.slug}"))
            if row.company_id is not None:
                company = session.query(Company).filter_by(id=int(row.company_id)).first()
                if company:
                    related.append(_relation_item("company", int(company.id), company.name, "company", f"/companies/{company.slug}"))
            for actor_id, relation in ((row.observed_source_actor_id, "observed_source"), (row.underlying_source_actor_id, "underlying_source")):
                if actor_id is None:
                    continue
                actor = session.query(SourceActor).filter_by(id=int(actor_id)).first()
                if actor:
                    related.append(_relation_item("source_actor", int(actor.id), actor.display_name, relation, "/consensus"))
    elif kind == "investment_catalyst":
        row = session.query(InvestmentCatalyst).filter_by(id=object_id).first()
        if row:
            if row.source_document_id is not None:
                doc = session.query(Document).filter_by(id=int(row.source_document_id)).first()
                if doc:
                    related.append(_relation_item("document", int(doc.id), doc.title or doc.url, "source_document", f"/docs/{int(doc.id)}"))
            if row.subject_id is not None:
                subject = session.query(ResearchSubject).filter_by(id=int(row.subject_id)).first()
                if subject:
                    related.append(_relation_item("research_subject", int(subject.id), subject.display_name, "subject", f"/reports/{subject.slug}"))
            if row.company_id is not None:
                company = session.query(Company).filter_by(id=int(row.company_id)).first()
                if company:
                    related.append(_relation_item("company", int(company.id), company.name, "company", f"/companies/{company.slug}"))
    elif kind == "source_actor":
        aliases = (
            session.query(SourceActorAlias)
            .filter(SourceActorAlias.actor_id == object_id)
            .order_by(SourceActorAlias.updated_at.desc(), SourceActorAlias.id.desc())
            .limit(6)
            .all()
        )
        related.extend(_relation_item("source_actor_alias", int(alias.id), alias.alias, alias.alias_kind, None) for alias in aliases)
        evidence = (
            session.query(AnalystEvidence)
            .filter(or_(AnalystEvidence.observed_source_actor_id == object_id, AnalystEvidence.underlying_source_actor_id == object_id))
            .filter(AnalystEvidence.status != "archived")
            .order_by(AnalystEvidence.updated_at.desc(), AnalystEvidence.id.desc())
            .limit(8)
            .all()
        )
        related.extend(
            _relation_item("analyst_evidence", int(row.id), row.summary or row.estimate_metric or row.evidence_key, "analyst_evidence", "/consensus")
            for row in evidence
        )
    elif kind == "signal_extraction":
        row = session.query(LocalAgentDocumentCompression).filter_by(id=object_id).first()
        if row:
            doc = session.query(Document).filter_by(id=int(row.document_id)).first()
            if doc:
                related.append(_relation_item("document", int(doc.id), doc.title or doc.url, "source_document", f"/docs/{int(doc.id)}"))
            claims = list(row.atomic_claims_json or [])[:5]
            for idx, claim in enumerate(claims, start=1):
                if not isinstance(claim, dict):
                    continue
                text = claim.get("claim") or claim.get("text") or claim.get("summary") or f"Claim {idx}"
                related.append(_relation_item("signal_claim", idx, text, claim.get("type") or "atomic_claim", None))
    elif kind == "knowledge_asset":
        links = session.query(KnowledgeAssetLink).filter(KnowledgeAssetLink.asset_id == object_id).limit(12).all()
        related.extend(
            _relation_item(str(link.object_type), int(link.object_id), f"{link.object_type} {int(link.object_id)}", link.relation, None)
            for link in links
        )
    elif kind == "research_subject":
        reports = session.query(ResearchReport).filter(ResearchReport.subject_id == object_id).limit(5).all()
        assets = session.query(KnowledgeAsset).filter(KnowledgeAsset.subject_id == object_id).limit(5).all()
        related.extend(_relation_item("research_report", int(row.id), row.title, "report", f"/reports/{row.slug}") for row in reports)
        related.extend(_relation_item("knowledge_asset", int(row.id), row.title, row.kind, f"/knowledge/{row.slug}") for row in assets)
    elif kind == "research_report":
        versions = (
            session.query(ResearchReportVersion)
            .filter(ResearchReportVersion.report_id == object_id)
            .order_by(ResearchReportVersion.updated_at.desc(), ResearchReportVersion.id.desc())
            .limit(4)
            .all()
        )
        related.extend(_relation_item("research_report_version", int(row.id), f"v{row.version} {row.status}", "version", None) for row in versions)
    elif kind == "proposition":
        theses = (
            session.query(ThesisProposition, Thesis)
            .join(Thesis, Thesis.id == ThesisProposition.thesis_id)
            .filter(ThesisProposition.proposition_id == object_id)
            .limit(8)
            .all()
        )
        related.extend(_relation_item("thesis", int(th.id), th.title, link.role, f"/theses/{int(th.id)}") for link, th in theses)
        observations = session.query(Observation).filter(Observation.proposition_id == object_id).order_by(Observation.created_at.desc()).limit(6).all()
        related.extend(_relation_item("observation", int(row.id), row.note or row.value_text or f"Observation {int(row.id)}", "observation", None) for row in observations)
    return _neighborhood_payload(item, related)


def _institutional_neighborhoods(session: Session, items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    seeds = [item for item in items if item.get("kind") in {"institutional_evidence", "institutional_observation"}]
    if not seeds:
        return {}
    evidence_ids = {int(item["object_id"]) for item in seeds if item["kind"] == "institutional_evidence"}
    referenced_ids = {int(item["evidence_id"]) for item in seeds if item.get("evidence_id")}
    evidence = {
        int(row.id): row for row in session.query(
            InstitutionalEvidenceItem.id, InstitutionalEvidenceItem.title,
            InstitutionalEvidenceItem.document_id, InstitutionalEvidenceItem.subject_id,
            InstitutionalEvidenceItem.company_id,
        ).filter(InstitutionalEvidenceItem.id.in_(evidence_ids | referenced_ids)).all()
    } if evidence_ids or referenced_ids else {}
    document_ids = {row.document_id for row in evidence.values() if row.document_id and row.id in evidence_ids}
    subject_ids = {item["subject_id"] for item in seeds if item.get("subject_id")} | {row.subject_id for row in evidence.values() if row.subject_id}
    company_ids = {item["company_id"] for item in seeds if item.get("company_id")} | {row.company_id for row in evidence.values() if row.company_id}
    documents = {row.id: row for row in session.query(Document.id, Document.title, Document.url).filter(Document.id.in_(document_ids)).all()} if document_ids else {}
    subjects = {row.id: row for row in session.query(ResearchSubject.id, ResearchSubject.display_name, ResearchSubject.slug).filter(ResearchSubject.id.in_(subject_ids)).all()} if subject_ids else {}
    companies = {row.id: row for row in session.query(Company.id, Company.name, Company.slug).filter(Company.id.in_(company_ids)).all()} if company_ids else {}
    observations_by_evidence: dict[int, list[dict[str, Any]]] = defaultdict(list)
    if evidence_ids:
        ranked = select(
            InstitutionalObservation.id, InstitutionalObservation.evidence_id,
            InstitutionalObservation.metric_key,
            func.substr(InstitutionalObservation.value_text, 1, 500).label("value_preview"),
            InstitutionalObservation.kind,
            func.row_number().over(partition_by=InstitutionalObservation.evidence_id, order_by=(InstitutionalObservation.updated_at.desc(), InstitutionalObservation.id.desc())).label("position"),
        ).where(InstitutionalObservation.evidence_id.in_(evidence_ids), InstitutionalObservation.review_status != "rejected").subquery()
        for row in session.execute(select(ranked).where(ranked.c.position <= 8)):
            observations_by_evidence[int(row.evidence_id)].append(_relation_item(
                "institutional_observation", int(row.id), row.metric_key or row.value_preview or f"Observation {row.id}", row.kind, "/institutional",
            ))
    related_by_key = {}
    for item in seeds:
        related = []
        is_evidence = item["kind"] == "institutional_evidence"
        row = evidence.get(int(item["object_id"]) if is_evidence else int(item.get("evidence_id") or 0))
        if row is not None:
            if is_evidence and row.document_id in documents:
                doc = documents[row.document_id]
                related.append(_relation_item("document", int(doc.id), doc.title or doc.url, "source_document", f"/docs/{doc.id}"))
            elif not is_evidence:
                related.append(_relation_item("institutional_evidence", int(row.id), row.title, "evidence", f"/docs/{row.document_id}" if row.document_id else "/institutional"))
        subject_id = item.get("subject_id") or (row.subject_id if row is not None else None)
        company_id = item.get("company_id") or (row.company_id if row is not None else None)
        if subject_id in subjects:
            subject = subjects[subject_id]
            related.append(_relation_item("research_subject", int(subject.id), subject.display_name, "subject", f"/reports/{subject.slug}"))
        if company_id in companies:
            company = companies[company_id]
            related.append(_relation_item("company", int(company.id), company.name, "company", f"/companies/{company.slug}"))
        if is_evidence:
            related.extend(observations_by_evidence.get(int(item["object_id"]), []))
        related_by_key[str(item["id"])] = related
    return related_by_key


def _batched_neighborhoods(session: Session, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    document_ids = [int(item.get("object_id") or 0) for item in items if item.get("kind") == "document"]
    thesis_ids = [int(item.get("object_id") or 0) for item in items if item.get("kind") == "thesis"]
    note_ids = [int(item.get("object_id") or 0) for item in items if item.get("kind") == "note"]
    proposition_ids = [int(item.get("object_id") or 0) for item in items if item.get("kind") == "proposition"]
    dataset_ids = [int(item.get("object_id") or 0) for item in items if item.get("kind") == "dataset"]
    supply_chain_ids = [int(item.get("object_id") or 0) for item in items if item.get("kind") == "supply_chain"]
    bottleneck_model_ids = [int(item.get("object_id") or 0) for item in items if item.get("kind") == "bottleneck_model"]
    knowledge_asset_ids = [int(item.get("object_id") or 0) for item in items if item.get("kind") == "knowledge_asset"]
    subject_ids = [int(item.get("object_id") or 0) for item in items if item.get("kind") == "research_subject"]
    report_ids = [int(item.get("object_id") or 0) for item in items if item.get("kind") == "research_report"]
    related_by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    related_by_key.update(_institutional_neighborhoods(session, items))

    if document_ids:
        for ev, th in (
            session.query(ThesisEvidence, Thesis)
            .join(Thesis, Thesis.id == ThesisEvidence.thesis_id)
            .filter(ThesisEvidence.document_id.in_(document_ids))
            .filter(~func.lower(Thesis.status).in_(sorted(THESIS_HIDDEN_STATUSES)))
            .order_by(ThesisEvidence.document_id.asc(), ThesisEvidence.id.asc())
            .all()
        ):
            key = f"document:{int(ev.document_id)}"
            if sum(1 for item in related_by_key[key] if item.get("kind") == "thesis") >= 8:
                continue
            related_by_key[key].append(_relation_item("thesis", int(th.id), th.title, ev.polarity, f"/theses/{int(th.id)}"))
        for link, note in (
            session.query(NoteDocument, Note)
            .join(Note, Note.id == NoteDocument.note_id)
            .filter(NoteDocument.document_id.in_(document_ids))
            .order_by(NoteDocument.document_id.asc(), NoteDocument.id.asc())
            .all()
        ):
            key = f"document:{int(link.document_id)}"
            if sum(1 for item in related_by_key[key] if item.get("kind") == "note") >= 8:
                continue
            related_by_key[key].append(_relation_item("note", int(note.id), note.title, _relation_value(link), f"/notes/{int(note.id)}"))

    if thesis_ids:
        for ev, doc in (
            session.query(ThesisEvidence, Document)
            .join(Document, Document.id == ThesisEvidence.document_id)
            .filter(ThesisEvidence.thesis_id.in_(thesis_ids))
            .order_by(ThesisEvidence.thesis_id.asc(), ThesisEvidence.id.asc())
            .all()
        ):
            key = f"thesis:{int(ev.thesis_id)}"
            if sum(1 for item in related_by_key[key] if item.get("kind") == "document") >= 8:
                continue
            related_by_key[key].append(_relation_item("document", int(doc.id), doc.title or doc.url, ev.polarity, f"/docs/{int(doc.id)}"))
        for link, note in (
            session.query(NoteThesis, Note)
            .join(Note, Note.id == NoteThesis.note_id)
            .filter(NoteThesis.thesis_id.in_(thesis_ids))
            .order_by(NoteThesis.thesis_id.asc(), NoteThesis.id.asc())
            .all()
        ):
            key = f"thesis:{int(link.thesis_id)}"
            if sum(1 for item in related_by_key[key] if item.get("kind") == "note") >= 6:
                continue
            related_by_key[key].append(_relation_item("note", int(note.id), note.title, link.relation, f"/notes/{int(note.id)}"))
        for link, prop in (
            session.query(ThesisProposition, Proposition)
            .join(Proposition, Proposition.id == ThesisProposition.proposition_id)
            .filter(ThesisProposition.thesis_id.in_(thesis_ids))
            .order_by(ThesisProposition.thesis_id.asc(), ThesisProposition.proposition_id.asc())
            .all()
        ):
            key = f"thesis:{int(link.thesis_id)}"
            if sum(1 for item in related_by_key[key] if item.get("kind") == "proposition") >= 6:
                continue
            related_by_key[key].append(_relation_item("proposition", int(prop.id), prop.key, link.role, None))

    if note_ids:
        for link, th in (
            session.query(NoteThesis, Thesis)
            .join(Thesis, Thesis.id == NoteThesis.thesis_id)
            .filter(NoteThesis.note_id.in_(note_ids))
            .order_by(NoteThesis.note_id.asc(), NoteThesis.id.asc())
            .all()
        ):
            key = f"note:{int(link.note_id)}"
            if sum(1 for item in related_by_key[key] if item.get("kind") == "thesis") >= 8:
                continue
            related_by_key[key].append(_relation_item("thesis", int(th.id), th.title, link.relation, f"/theses/{int(th.id)}"))
        for link, doc in (
            session.query(NoteDocument, Document)
            .join(Document, Document.id == NoteDocument.document_id)
            .filter(NoteDocument.note_id.in_(note_ids))
            .order_by(NoteDocument.note_id.asc(), NoteDocument.id.asc())
            .all()
        ):
            key = f"note:{int(link.note_id)}"
            if sum(1 for item in related_by_key[key] if item.get("kind") == "document") >= 8:
                continue
            related_by_key[key].append(_relation_item("document", int(doc.id), doc.title or doc.url, _relation_value(link), f"/docs/{int(doc.id)}"))
        for link, prop in (
            session.query(NoteProposition, Proposition)
            .join(Proposition, Proposition.id == NoteProposition.proposition_id)
            .filter(NoteProposition.note_id.in_(note_ids))
            .order_by(NoteProposition.note_id.asc(), NoteProposition.id.asc())
            .all()
        ):
            key = f"note:{int(link.note_id)}"
            if sum(1 for item in related_by_key[key] if item.get("kind") == "proposition") >= 6:
                continue
            related_by_key[key].append(_relation_item("proposition", int(prop.id), prop.key, link.relation, None))

    if proposition_ids:
        for link, th in (
            session.query(ThesisProposition, Thesis)
            .join(Thesis, Thesis.id == ThesisProposition.thesis_id)
            .filter(ThesisProposition.proposition_id.in_(proposition_ids))
            .order_by(ThesisProposition.proposition_id.asc(), ThesisProposition.thesis_id.asc())
            .all()
        ):
            key = f"proposition:{int(link.proposition_id)}"
            if sum(1 for item in related_by_key[key] if item.get("kind") == "thesis") >= 8:
                continue
            related_by_key[key].append(_relation_item("thesis", int(th.id), th.title, link.role, f"/theses/{int(th.id)}"))
        for row in (
            session.query(Observation)
            .filter(Observation.proposition_id.in_(proposition_ids))
            .order_by(Observation.proposition_id.asc(), Observation.created_at.desc(), Observation.id.desc())
            .all()
        ):
            key = f"proposition:{int(row.proposition_id)}"
            if sum(1 for item in related_by_key[key] if item.get("kind") == "observation") >= 6:
                continue
            related_by_key[key].append(
                _relation_item("observation", int(row.id), row.note or row.value_text or f"Observation {int(row.id)}", "observation", None)
            )

    if dataset_ids:
        for link in (
            session.query(DatasetLink)
            .filter(DatasetLink.dataset_id.in_(dataset_ids))
            .order_by(DatasetLink.dataset_id.asc(), DatasetLink.id.asc())
            .all()
        ):
            key = f"dataset:{int(link.dataset_id)}"
            if len(related_by_key[key]) >= 12:
                continue
            related_by_key[key].append(
                _relation_item(str(link.object_type), int(link.object_id), f"{link.object_type} {int(link.object_id)}", link.relation, None)
            )
        binding_rows = (
            session.query(SupplyChainMetricBinding, SupplyChainSnapshot, SupplyChain)
            .join(SupplyChainSnapshot, SupplyChainSnapshot.id == SupplyChainMetricBinding.snapshot_id)
            .join(SupplyChain, SupplyChain.id == SupplyChainSnapshot.chain_id)
            .filter(SupplyChainMetricBinding.dataset_id.in_(dataset_ids))
            .order_by(SupplyChainMetricBinding.dataset_id.asc(), SupplyChainMetricBinding.id.asc())
            .all()
        )
        for binding, _snapshot, chain in binding_rows:
            if binding.dataset_id is None:
                continue
            key = f"dataset:{int(binding.dataset_id)}"
            if sum(1 for item in related_by_key[key] if item.get("kind") == "supply_chain") >= 8:
                continue
            related_by_key[key].append(
                _relation_item("supply_chain", int(chain.id), chain.title, binding.metric_kind, f"/visualizations/supply-chains/{int(chain.id)}/command-center")
            )

    if supply_chain_ids:
        for link in (
            session.query(SupplyChainLink)
            .filter(SupplyChainLink.chain_id.in_(supply_chain_ids))
            .order_by(SupplyChainLink.chain_id.asc(), SupplyChainLink.id.asc())
            .all()
        ):
            key = f"supply_chain:{int(link.chain_id)}"
            if len(related_by_key[key]) >= 12:
                continue
            related_by_key[key].append(
                _relation_item(str(link.object_type), int(link.object_id), f"{link.object_type} {int(link.object_id)}", link.relation, None)
            )
        snapshot_by_chain: dict[int, SupplyChainSnapshot] = {}
        for snapshot in (
            session.query(SupplyChainSnapshot)
            .filter(SupplyChainSnapshot.chain_id.in_(supply_chain_ids))
            .order_by(SupplyChainSnapshot.chain_id.asc(), SupplyChainSnapshot.updated_at.desc(), SupplyChainSnapshot.id.desc())
            .all()
        ):
            chain_id = int(snapshot.chain_id)
            if chain_id not in snapshot_by_chain:
                snapshot_by_chain[chain_id] = snapshot
        snapshot_chain_by_id = {int(snapshot.id): int(snapshot.chain_id) for snapshot in snapshot_by_chain.values()}
        if snapshot_chain_by_id:
            for binding, dataset in (
                session.query(SupplyChainMetricBinding, Dataset)
                .join(Dataset, Dataset.id == SupplyChainMetricBinding.dataset_id)
                .filter(SupplyChainMetricBinding.snapshot_id.in_(sorted(snapshot_chain_by_id)))
                .order_by(SupplyChainMetricBinding.snapshot_id.asc(), SupplyChainMetricBinding.id.asc())
                .all()
            ):
                key = f"supply_chain:{snapshot_chain_by_id[int(binding.snapshot_id)]}"
                if sum(1 for item in related_by_key[key] if item.get("kind") == "dataset") >= 10:
                    continue
                related_by_key[key].append(
                    _relation_item("dataset", int(dataset.id), dataset.title, binding.metric_kind, f"/visualizations/datasets/{int(dataset.id)}")
                )
        for row in (
            session.query(BottleneckModel)
            .filter(BottleneckModel.supply_chain_id.in_(supply_chain_ids))
            .order_by(BottleneckModel.supply_chain_id.asc(), BottleneckModel.updated_at.desc(), BottleneckModel.id.desc())
            .all()
        ):
            if row.supply_chain_id is None:
                continue
            key = f"supply_chain:{int(row.supply_chain_id)}"
            if sum(1 for item in related_by_key[key] if item.get("kind") == "bottleneck_model") >= 8:
                continue
            related_by_key[key].append(
                _relation_item("bottleneck_model", int(row.id), row.title, row.regime, f"/visualizations/supply-chains/{int(row.supply_chain_id)}/command-center")
            )

    if bottleneck_model_ids:
        bottleneck_seed_by_id = {
            int(item.get("object_id") or 0): item
            for item in items
            if item.get("kind") == "bottleneck_model" and int(item.get("object_id") or 0) in bottleneck_model_ids
        }
        subject_ids_for_models = sorted({int(item.get("subject_id") or 0) for item in bottleneck_seed_by_id.values() if item.get("subject_id")})
        site_ids_for_models = sorted({int(item.get("site_id") or 0) for item in bottleneck_seed_by_id.values() if item.get("site_id")})
        program_ids_for_models = sorted({int(item.get("program_id") or 0) for item in bottleneck_seed_by_id.values() if item.get("program_id")})
        chain_ids_for_models = sorted({int(item.get("supply_chain_id") or 0) for item in bottleneck_seed_by_id.values() if item.get("supply_chain_id")})
        subjects_by_id = {
            int(row.id): row
            for row in session.query(ResearchSubject).filter(ResearchSubject.id.in_(subject_ids_for_models)).all()
        } if subject_ids_for_models else {}
        sites_by_id = {int(row.id): row for row in session.query(Site).filter(Site.id.in_(site_ids_for_models)).all()} if site_ids_for_models else {}
        programs_by_id = {int(row.id): row for row in session.query(Program).filter(Program.id.in_(program_ids_for_models)).all()} if program_ids_for_models else {}
        chains_by_id = {int(row.id): row for row in session.query(SupplyChain).filter(SupplyChain.id.in_(chain_ids_for_models)).all()} if chain_ids_for_models else {}
        for model_id, item in bottleneck_seed_by_id.items():
            key = f"bottleneck_model:{int(model_id)}"
            subject_id = item.get("subject_id")
            if subject_id and int(subject_id) in subjects_by_id:
                subject = subjects_by_id[int(subject_id)]
                related_by_key[key].append(_relation_item("research_subject", int(subject.id), subject.display_name, "subject", f"/reports/{subject.slug}"))
            site_id = item.get("site_id")
            if site_id and int(site_id) in sites_by_id:
                site = sites_by_id[int(site_id)]
                related_by_key[key].append(_relation_item("site", int(site.id), site.name, "site", None))
            program_id = item.get("program_id")
            if program_id and int(program_id) in programs_by_id:
                program = programs_by_id[int(program_id)]
                related_by_key[key].append(_relation_item("program", int(program.id), program.title, "program", None))
            chain_id = item.get("supply_chain_id")
            if chain_id and int(chain_id) in chains_by_id:
                chain = chains_by_id[int(chain_id)]
                related_by_key[key].append(
                    _relation_item("supply_chain", int(chain.id), chain.title, "supply_chain", f"/visualizations/supply-chains/{int(chain.id)}/command-center")
                )
        for link in (
            session.query(BottleneckModelLink)
            .filter(BottleneckModelLink.model_id.in_(bottleneck_model_ids))
            .order_by(BottleneckModelLink.model_id.asc(), BottleneckModelLink.relation.asc(), BottleneckModelLink.id.asc())
            .all()
        ):
            key = f"bottleneck_model:{int(link.model_id)}"
            if len(related_by_key[key]) >= 16:
                continue
            related_by_key[key].append(
                _relation_item(str(link.object_type), int(link.object_id), f"{link.object_type} {int(link.object_id)}", link.relation, None)
            )

    if knowledge_asset_ids:
        for link in (
            session.query(KnowledgeAssetLink)
            .filter(KnowledgeAssetLink.asset_id.in_(knowledge_asset_ids))
            .order_by(KnowledgeAssetLink.asset_id.asc(), KnowledgeAssetLink.id.asc())
            .all()
        ):
            key = f"knowledge_asset:{int(link.asset_id)}"
            if len(related_by_key[key]) >= 12:
                continue
            related_by_key[key].append(
                _relation_item(str(link.object_type), int(link.object_id), f"{link.object_type} {int(link.object_id)}", link.relation, None)
            )

    if subject_ids:
        for row in (
            session.query(ResearchReport)
            .filter(ResearchReport.subject_id.in_(subject_ids))
            .order_by(ResearchReport.subject_id.asc(), ResearchReport.id.asc())
            .all()
        ):
            key = f"research_subject:{int(row.subject_id)}"
            if sum(1 for item in related_by_key[key] if item.get("kind") == "research_report") >= 5:
                continue
            related_by_key[key].append(_relation_item("research_report", int(row.id), row.title, "report", f"/reports/{row.slug}"))
        for row in (
            session.query(KnowledgeAsset)
            .filter(KnowledgeAsset.subject_id.in_(subject_ids))
            .order_by(KnowledgeAsset.subject_id.asc(), KnowledgeAsset.id.asc())
            .all()
        ):
            key = f"research_subject:{int(row.subject_id)}"
            if sum(1 for item in related_by_key[key] if item.get("kind") == "knowledge_asset") >= 5:
                continue
            related_by_key[key].append(_relation_item("knowledge_asset", int(row.id), row.title, row.kind, f"/knowledge/{row.slug}"))

    if report_ids:
        for row in (
            session.query(ResearchReportVersion)
            .filter(ResearchReportVersion.report_id.in_(report_ids))
            .order_by(ResearchReportVersion.report_id.asc(), ResearchReportVersion.updated_at.desc(), ResearchReportVersion.id.desc())
            .all()
        ):
            key = f"research_report:{int(row.report_id)}"
            if len(related_by_key[key]) >= 4:
                continue
            related_by_key[key].append(_relation_item("research_report_version", int(row.id), f"v{row.version} {row.status}", "version", None))

    batched_kinds = {
        "institutional_evidence",
        "institutional_observation",
        "document",
        "thesis",
        "note",
        "proposition",
        "dataset",
        "supply_chain",
        "bottleneck_model",
        "knowledge_asset",
        "research_subject",
        "research_report",
    }
    neighborhoods: list[dict[str, Any]] = []
    for item in items:
        kind = str(item.get("kind") or "")
        if kind in batched_kinds:
            key = f"{kind}:{int(item.get('object_id') or 0)}"
            neighborhoods.append(_neighborhood_payload(item, related_by_key.get(key, [])))
        else:
            neighborhoods.append(_neighborhood(session, item))
    return neighborhoods


def object_aware_search(
    session: Session,
    query: str,
    *,
    kind: str = "all",
    limit: int = 10,
    include_neighborhoods: bool = True,
    include_hidden: bool = False,
) -> dict[str, Any]:
    query_text = str(query or "").strip()
    if not query_text:
        return {"query": "", "kind": _kind(kind), "count": 0, "items": [], "groups": {}, "neighborhoods": [], "guidance": {}}
    limit_val = max(1, min(int(limit or 10), 50))
    kind_norm = _kind(kind)
    target_kinds = [
        "document",
        "source",
        "thesis",
        "note",
        "dataset",
        "chart",
        "company",
        "supply_chain",
        "research_report",
        "research_subject",
        "knowledge_asset",
        "research_play",
        "instrument_request",
        "product_stage",
        "bottleneck_model",
        "analyst_evidence",
        "investment_catalyst",
        "institutional_evidence",
        "institutional_observation",
        "source_actor",
        "signal_extraction",
        "site",
        "program",
        "proposition",
        "observation",
    ] if kind_norm == "all" else [kind_norm]

    items: list[dict[str, Any]] = []
    if "document" in target_kinds:
        _add_doc_items(session, query_text, min(limit_val, 12), items)
    institutional_context = (
        resolve_search_context(session, query_text)
        if any(target in {"institutional_evidence", "institutional_observation"} for target in target_kinds) else None
    )
    for target in [item for item in target_kinds if item != "document"]:
        _add_model_items(
            session,
            query_text,
            target,
            limit_val if target in {"institutional_evidence", "institutional_observation"} else min(limit_val, 12),
            items,
            include_hidden_theses=include_hidden,
            institutional_context=institutional_context,
        )

    items.sort(key=lambda item: (int(item.get("_rank", 999)), -float(item.get("score") or 0), str(item.get("label") or "").lower()))
    trimmed = []
    seen: set[str] = set()
    for item in items:
        item_id = str(item.get("id") or "")
        if item_id in seen:
            continue
        seen.add(item_id)
        payload = dict(item)
        payload.pop("_rank", None)
        trimmed.append(payload)
        if len(trimmed) >= limit_val:
            break
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in trimmed:
        groups[str(item.get("kind") or "object")].append(item)

    neighborhoods = _batched_neighborhoods(session, trimmed[:6]) if include_neighborhoods else []
    return {
        "query": query_text,
        "kind": kind_norm,
        "count": len(trimmed),
        "items": trimmed,
        "groups": dict(groups),
        "neighborhoods": neighborhoods,
        "guidance": {
            "search_is_object_aware": True,
            "document_compatibility": "MCP search.results remains document-only for ChatGPT compatibility; use object_results/items for full object search.",
            "recommended_flow": ["object_search", "resolve_object", "get_*", "preview_* when available", "mutate only after inspection"],
        },
    }
