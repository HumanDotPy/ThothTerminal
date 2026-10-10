from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, Iterable

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload, load_only

from thoth_terminal.agent.autonomy import serialize_change_run, serialize_thesis_proposal
from thoth_terminal.agent.evidence_policy import (
    discord_source_evidence_metadata,
    document_evidence_metadata,
    note_evidence_metadata,
)
from thoth_terminal.db import utcnow
from thoth_terminal.dataset_service import serialize_dataset_summaries, serialize_dataset_summary
from thoth_terminal.dataset_delta_service import dataset_value_delta
from thoth_terminal.document_refinery import get_document_refinery
from thoth_terminal.discord import notes_bridge
from thoth_terminal.discord.models import DiscordEvidenceItem, DiscordEvidenceTarget, DiscordOutboxItem, DiscordQueueItem
from thoth_terminal.models import (
    AgentAction,
    AgentChangeRun,
    Chart,
    Dataset,
    Document,
    DocumentEntity,
    DocumentEvent,
    Domain,
    Entity,
    IngestionRun,
    InstrumentRequest,
    IRArtifact,
    Note,
    ProductStageEntry,
    Source,
    SupplyChain,
    Thesis,
    ThesisProposal,
    note_entities,
)
from thoth_terminal.research_library_service import list_research_library
from thoth_terminal.supply_chain_service import supply_chain_counts_by_chain_id
from thoth_terminal.trade import TRADE_SECTION_NAME, relevant_trade_context

DEFAULT_CONTEXT_SECTIONS = [
    "documents",
    "notes",
    "discord_sources",
    TRADE_SECTION_NAME,
    "datasets",
    "charts",
    "supply_chains",
    "instrument_requests",
    "product_stages",
    "transcripts",
    "theses",
    "research_library",
    "agent_actions",
    "change_runs",
    "thesis_proposals",
    "document_events",
    "ingestion_runs",
    "backlog_stats",
]


def _to_set_str(values: Iterable[object] | None) -> set[str]:
    out: set[str] = set()
    for v in values or []:
        txt = str(v or "").strip()
        if txt:
            out.add(txt)
    return out


def _bundle_ts() -> str:
    return utcnow().isoformat()


def _is_agent_generated_document(doc: Document) -> bool:
    extra = doc.extra if isinstance(doc.extra, dict) else {}
    return bool(extra.get("agent_generated"))


def _is_agent_generated_note(note: Note) -> bool:
    extra = note.extra if isinstance(note.extra, dict) else {}
    discord_extra = extra.get("discord") if isinstance(extra.get("discord"), dict) else {}
    kind = str(discord_extra.get("kind") or "").strip().lower()
    return kind.startswith("agent_")


def _document_context_row_payload(
    *,
    row: Any,
    source: Any,
    domains: list[str],
    text: str,
    metadata: dict[str, Any],
    refinery: dict[str, Any],
) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "title": row.title or row.url or f"doc {row.id}",
        "source": source.name if source else None,
        "source_id": int(row.source_id),
        "triage_state": row.triage_state,
        "saved": bool(row.saved),
        "published_at": row.published_at.isoformat() if row.published_at else None,
        "ingested_at": row.ingested_at.isoformat() if row.ingested_at else None,
        "url": row.url,
        "canonical_url": row.canonical_url,
        "domains": domains,
        "text": text,
        "refinery_signal": str(refinery.get("signal") or "").strip() or None,
        "refinery_summary": str(refinery.get("summary") or "").strip() or None,
        "refinery_why_it_matters": str(refinery.get("why_it_matters") or "").strip() or None,
        "refinery_key_points": [
            str(item).strip()
            for item in (refinery.get("key_points") or [])
            if str(item).strip()
        ][:3],
        "refinery_topics": [
            str(item).strip()
            for item in (refinery.get("topics") or [])
            if str(item).strip()
        ][:6],
        "refinery_confidence": refinery.get("confidence"),
        "refinery_engine": str(refinery.get("engine") or "").strip() or None,
        "refinery_llm_used": bool(refinery.get("llm_used")) if refinery else False,
        **metadata,
    }


def _domain_map_for_documents(session: Session, document_ids: list[int]) -> dict[int, list[str]]:
    if not document_ids:
        return {}
    rows = (
        session.query(Document.id, Domain.slug)
        .join(Document.domains)
        .filter(Document.id.in_(document_ids))
        .order_by(Document.id.asc(), Domain.slug.asc())
        .all()
    )
    out: dict[int, list[str]] = {}
    for doc_id, slug in rows:
        out.setdefault(int(doc_id), []).append(str(slug))
    return out


def _document_context_projection_rows(
    session: Session,
    *,
    cutoff: datetime,
    source_id_set: set[int],
    domain_set: set[str],
    ticker_set: set[str],
    limit: int,
    max_items_per_source: int | None = None,
) -> list[dict[str, Any]]:
    text_expr = func.coalesce(Document.translation_en, Document.normalized_text, "")
    q = (
        session.query(
            Document.id.label("id"),
            Document.title.label("title"),
            Document.url.label("url"),
            Document.canonical_url.label("canonical_url"),
            Document.source_id.label("source_id"),
            Document.triage_state.label("triage_state"),
            Document.saved.label("saved"),
            Document.published_at.label("published_at"),
            Document.ingested_at.label("ingested_at"),
            Document.language.label("language"),
            Document.language_confidence.label("language_confidence"),
            Document.extra.label("extra"),
            func.length(func.coalesce(Document.translation_en, "")).label("translation_chars"),
            func.substr(text_expr, 1, 500).label("text_excerpt"),
            Source.name.label("source_name"),
            Source.category.label("source_category"),
            Source.config.label("source_config"),
        )
        .select_from(Document)
        .join(Source, Source.id == Document.source_id)
        .filter(Document.ingested_at >= cutoff)
    )
    if source_id_set:
        q = q.filter(Document.source_id.in_(list(source_id_set)))
    if domain_set:
        q = q.join(Document.domains).filter(Domain.slug.in_(list(domain_set)))
    if ticker_set:
        q = q.join(DocumentEntity, DocumentEntity.document_id == Document.id).join(Entity, Entity.id == DocumentEntity.entity_id)
        q = q.filter(func.upper(Entity.canonical_name).in_(list(ticker_set)))
    candidate_limit = limit if not max_items_per_source else min(2000, max(limit, limit * 8))
    projected = q.order_by(Document.ingested_at.desc()).limit(candidate_limit).all()
    document_ids = [int(row.id) for row in projected]
    domains_by_doc = _domain_map_for_documents(session, document_ids)

    rows: list[dict[str, Any]] = []
    seen_document_ids: set[int] = set()
    source_counts: dict[int, int] = {}
    for row in projected:
        doc_id = int(row.id)
        if doc_id in seen_document_ids:
            continue
        seen_document_ids.add(doc_id)
        source_id = int(row.source_id)
        if max_items_per_source and source_counts.get(source_id, 0) >= int(max_items_per_source):
            continue
        source = SimpleNamespace(name=row.source_name, category=row.source_category, config=row.source_config)
        doc = SimpleNamespace(
            id=row.id,
            title=row.title,
            url=row.url,
            canonical_url=row.canonical_url,
            source_id=row.source_id,
            source=source,
            triage_state=row.triage_state,
            saved=row.saved,
            published_at=row.published_at,
            ingested_at=row.ingested_at,
            language=row.language,
            language_confidence=row.language_confidence,
            translation_en=("1" if int(row.translation_chars or 0) > 0 else ""),
            extra=row.extra if isinstance(row.extra, dict) else {},
        )
        if _is_agent_generated_document(doc):  # type: ignore[arg-type]
            continue
        metadata = document_evidence_metadata(doc)  # type: ignore[arg-type]
        if metadata.get("deprecated_source"):
            continue
        refinery = get_document_refinery(doc) or {}  # type: ignore[arg-type]
        if metadata.get("extraction_usable") is False:
            refinery = {}
        excerpt = str(row.text_excerpt or "").strip()
        if metadata.get("extraction_usable") is False:
            excerpt = ""
        if refinery:
            excerpt = excerpt[:220]
        rows.append(
            _document_context_row_payload(
                row=row,
                source=source,
                domains=domains_by_doc.get(int(row.id), []),
                text=excerpt,
                metadata=metadata,
                refinery=refinery,
            )
        )
        source_counts[source_id] = source_counts.get(source_id, 0) + 1
        if len(rows) >= limit:
            break
    return rows


def build_recent_context_bundle(
    session: Session,
    *,
    guild_id: int | None = None,
    lookback_hours: int = 24,
    tickers: list[str] | None = None,
    domains: list[str] | None = None,
    source_ids: list[int] | None = None,
    include_sections: list[str] | None = None,
    max_items_per_section: int = 25,
    max_items_per_source: int | None = None,
    include_full_text: bool = False,
) -> dict[str, Any]:
    lookback_h = max(1, int(lookback_hours or 24))
    max_items = max(1, min(int(max_items_per_section or 25), 200))
    sections = [s for s in (include_sections or DEFAULT_CONTEXT_SECTIONS) if str(s).strip()]
    wanted = set(sections)
    cutoff = utcnow() - timedelta(hours=lookback_h)
    ticker_set = {str(t).strip().upper() for t in (tickers or []) if str(t).strip()}
    domain_set = _to_set_str(domains)
    source_id_set = {int(s) for s in (source_ids or []) if str(s).isdigit()}

    bundle: dict[str, Any] = {
        "generated_at": _bundle_ts(),
        "lookback_hours": lookback_h,
        "filters": {
            "guild_id": int(guild_id) if guild_id is not None else None,
            "tickers": sorted(ticker_set),
            "domains": sorted(domain_set),
            "source_ids": sorted(source_id_set),
            "include_sections": sections,
            "include_full_text": bool(include_full_text),
            "max_items_per_section": max_items,
            "max_items_per_source": int(max_items_per_source) if max_items_per_source else None,
        },
        "counts": {},
        "sections": {},
    }

    def _put(name: str, rows: list[dict[str, Any]]) -> None:
        bundle["sections"][name] = rows
        bundle["counts"][name] = len(rows)

    if "documents" in wanted:
        rows = []
        if include_full_text:
            q = (
                session.query(Document)
                .options(
                    load_only(
                        Document.id,
                        Document.title,
                        Document.url,
                        Document.canonical_url,
                        Document.source_id,
                        Document.triage_state,
                        Document.saved,
                        Document.published_at,
                        Document.ingested_at,
                        Document.normalized_text,
                        Document.translation_en,
                        Document.language,
                        Document.language_confidence,
                        Document.extra,
                    ),
                    joinedload(Document.source).load_only(Source.id, Source.name, Source.category, Source.config),
                    joinedload(Document.domains).load_only(Domain.id, Domain.slug),
                )
                .filter(Document.ingested_at >= cutoff)
            )
            if source_id_set:
                q = q.filter(Document.source_id.in_(list(source_id_set)))
            if domain_set:
                q = q.join(Document.domains).filter(Domain.slug.in_(list(domain_set)))
            if ticker_set:
                q = q.join(DocumentEntity, DocumentEntity.document_id == Document.id).join(Entity, Entity.id == DocumentEntity.entity_id)
                q = q.filter(func.upper(Entity.canonical_name).in_(list(ticker_set)))
            docs = q.order_by(Document.ingested_at.desc()).limit(max_items * 4).all()
            for d in docs:
                if _is_agent_generated_document(d):
                    continue
                metadata = document_evidence_metadata(d)
                if metadata.get("deprecated_source"):
                    continue
                refinery = get_document_refinery(d) or {}
                if metadata.get("extraction_usable") is False:
                    refinery = {}
                text = (d.translation_en or d.normalized_text or "").strip()
                if metadata.get("extraction_usable") is False:
                    text = ""
                rows.append(
                    _document_context_row_payload(
                        row=d,
                        source=d.source,
                        domains=[dom.slug for dom in (d.domains or [])],
                        text=text,
                        metadata=metadata,
                        refinery=refinery,
                    )
                )
                if len(rows) >= max_items:
                    break
        else:
            rows = _document_context_projection_rows(
                session,
                cutoff=cutoff,
                source_id_set=source_id_set,
                domain_set=domain_set,
                ticker_set=ticker_set,
                limit=max_items * 4,
                max_items_per_source=max_items_per_source,
            )[:max_items]
        _put("documents", rows)

    if "notes" in wanted:
        rows = []
        if include_full_text:
            q = (
                session.query(Note)
                .options(
                    load_only(
                        Note.id,
                        Note.title,
                        Note.note_type,
                        Note.status,
                        Note.updated_at,
                        Note.created_at,
                        Note.body_md,
                        Note.extra,
                    ),
                    joinedload(Note.entities).load_only(Entity.id, Entity.canonical_name),
                )
                .filter(Note.updated_at >= cutoff)
                .filter(Note.status != "archived")
            )
            if ticker_set:
                q = q.join(Note.entities).filter(func.upper(Entity.canonical_name).in_(list(ticker_set)))
            notes = q.order_by(Note.updated_at.desc()).limit(max_items * 4).all()
            for n in notes:
                if _is_agent_generated_note(n):
                    continue
                extra = n.extra if isinstance(n.extra, dict) else {}
                discord_extra = extra.get("discord") if isinstance(extra.get("discord"), dict) else {}
                if str(discord_extra.get("kind") or "").strip().lower() == "source":
                    continue
                metadata = note_evidence_metadata(n)
                rows.append(
                    {
                        "id": int(n.id),
                        "title": n.title or f"note {n.id}",
                        "note_type": n.note_type,
                        "status": n.status,
                        "updated_at": n.updated_at.isoformat() if n.updated_at else None,
                        "created_at": n.created_at.isoformat() if n.created_at else None,
                        "entities": [e.canonical_name for e in (n.entities or [])][:12],
                        "body": n.body_md or "",
                        "kind": extra.get("discord", {}).get("kind") if isinstance(extra.get("discord"), dict) else None,
                        **metadata,
                    }
                )
                if len(rows) >= max_items:
                    break
        else:
            q = (
                session.query(
                    Note.id,
                    Note.title,
                    Note.note_type,
                    Note.status,
                    Note.updated_at,
                    Note.created_at,
                    Note.extra,
                    func.substr(Note.body_md, 1, 400).label("body_excerpt"),
                )
                .filter(Note.updated_at >= cutoff)
                .filter(Note.status != "archived")
            )
            if ticker_set:
                q = q.join(note_entities, note_entities.c.note_id == Note.id).join(Entity, Entity.id == note_entities.c.entity_id)
                q = q.filter(func.upper(Entity.canonical_name).in_(list(ticker_set))).distinct()
            notes = q.order_by(Note.updated_at.desc()).limit(max_items * 4).all()
            note_ids = [int(n.id) for n in notes]
            entities_by_note_id: dict[int, list[str]] = {note_id: [] for note_id in note_ids}
            if note_ids:
                entity_rows = (
                    session.query(note_entities.c.note_id, Entity.canonical_name)
                    .join(Entity, Entity.id == note_entities.c.entity_id)
                    .filter(note_entities.c.note_id.in_(note_ids))
                    .order_by(note_entities.c.note_id.asc(), Entity.canonical_name.asc())
                    .all()
                )
                for note_id, canonical_name in entity_rows:
                    names = entities_by_note_id.setdefault(int(note_id), [])
                    if len(names) < 12:
                        names.append(str(canonical_name or ""))
            for n in notes:
                if _is_agent_generated_note(n):
                    continue
                extra = n.extra if isinstance(n.extra, dict) else {}
                discord_extra = extra.get("discord") if isinstance(extra.get("discord"), dict) else {}
                if str(discord_extra.get("kind") or "").strip().lower() == "source":
                    continue
                metadata = note_evidence_metadata(n)
                rows.append(
                    {
                        "id": int(n.id),
                        "title": n.title or f"note {n.id}",
                        "note_type": n.note_type,
                        "status": n.status,
                        "updated_at": n.updated_at.isoformat() if n.updated_at else None,
                        "created_at": n.created_at.isoformat() if n.created_at else None,
                        "entities": entities_by_note_id.get(int(n.id), []),
                        "body": str(n.body_excerpt or ""),
                        "kind": extra.get("discord", {}).get("kind") if isinstance(extra.get("discord"), dict) else None,
                        **metadata,
                    }
                )
                if len(rows) >= max_items:
                    break
        _put("notes", rows)

    if "discord_sources" in wanted:
        rows: list[dict[str, Any]] = []
        if guild_id is not None:
            q = (
                session.query(DiscordEvidenceItem, DiscordEvidenceTarget)
                .outerjoin(DiscordEvidenceTarget, DiscordEvidenceTarget.evidence_id == DiscordEvidenceItem.id)
                .filter(DiscordEvidenceItem.guild_id == int(guild_id))
                .filter(DiscordEvidenceItem.created_at >= cutoff)
            )
            if ticker_set:
                q = q.filter(func.upper(DiscordEvidenceTarget.symbol).in_(list(ticker_set)))
            source_rows = (
                q.order_by(
                    DiscordEvidenceItem.created_at.desc(),
                    DiscordEvidenceTarget.is_primary.desc(),
                    DiscordEvidenceTarget.id.asc(),
                )
                .limit(max_items * 4)
                .all()
            )
            seen_source_ids: set[int] = set()
            for evidence, target in source_rows:
                evidence_id = int(evidence.id)
                if evidence_id in seen_source_ids:
                    continue
                seen_source_ids.add(evidence_id)
                metadata = discord_source_evidence_metadata(evidence)
                rows.append(
                    {
                        "id": evidence_id,
                        "evidence_id": evidence_id,
                        "symbol": str(getattr(target, "symbol", None) or ""),
                        "title": evidence.title,
                        "url": evidence.url_canonical or evidence.url_raw,
                        "sentiment": evidence.sentiment,
                        "source_type": evidence.source_type,
                        "created_at": evidence.created_at.isoformat() if evidence.created_at else None,
                        "note": (evidence.note or "")[:300],
                        "target_kind": (str(target.target_kind) if target else None),
                        "target_label": (str(target.target_label) if target and target.target_label else None),
                        "resolution_state": evidence.resolution_state,
                        **metadata,
                    }
                )
                if len(rows) >= max_items:
                    break
        _put("discord_sources", rows)

    if TRADE_SECTION_NAME in wanted:
        doc_rows = bundle["sections"].get("documents") if isinstance(bundle["sections"].get("documents"), list) else []
        note_rows = bundle["sections"].get("notes") if isinstance(bundle["sections"].get("notes"), list) else []
        text_parts: list[str] = []
        entity_tokens: list[str] = []
        for row in doc_rows:
            if not isinstance(row, dict):
                continue
            text_parts.extend([str(row.get("title") or ""), str(row.get("text") or "")])
        for row in note_rows:
            if not isinstance(row, dict):
                continue
            text_parts.extend([str(row.get("title") or ""), str(row.get("body") or "")])
            for entity in row.get("entities") or []:
                entity_tokens.append(str(entity))
        rows = relevant_trade_context(
            session,
            text_parts=text_parts,
            entity_tokens=entity_tokens,
            limit=max_items,
        )
        _put(TRADE_SECTION_NAME, rows)

    if "datasets" in wanted:
        rows = []
        datasets = session.query(Dataset).filter(Dataset.updated_at >= cutoff).order_by(Dataset.updated_at.desc()).limit(max_items).all()
        summary_map = serialize_dataset_summaries(session, datasets)
        for dataset in datasets:
            summary = summary_map.get(int(dataset.id)) or serialize_dataset_summary(session, dataset)
            rows.append(
                {
                    "id": int(dataset.id),
                    "title": dataset.title,
                    "category": dataset.category,
                    "family": dataset.family,
                    "status": dataset.status,
                    "updated_at": dataset.updated_at.isoformat() if dataset.updated_at else None,
                    "source_kind": dataset.source_kind,
                    "rows": int(((summary.get("counts") or {}).get("rows") or 0)),
                    "columns": int(((summary.get("counts") or {}).get("columns") or 0)),
                    "provenance_summary": summary.get("provenance_summary"),
                    "value_delta": dataset_value_delta(session, int(dataset.id)),
                    "evidence_tier_summary": summary.get("evidence_tier_summary"),
                    "url": notes_bridge.dataset_url(int(dataset.id)),
                }
            )
        _put("datasets", rows)

    if "charts" in wanted:
        rows = []
        for chart in session.query(Chart).filter(Chart.updated_at >= cutoff).order_by(Chart.updated_at.desc()).limit(max_items).all():
            rows.append(
                {
                    "id": int(chart.id),
                    "title": chart.title,
                    "category": chart.category,
                    "updated_at": chart.updated_at.isoformat() if chart.updated_at else None,
                    "note_id": int(chart.note_id) if chart.note_id is not None else None,
                    "thesis_id": int(chart.thesis_id) if chart.thesis_id is not None else None,
                    "url": notes_bridge.chart_url(int(chart.id)),
                }
            )
        _put("charts", rows)

    if "supply_chains" in wanted:
        rows = []
        chains = session.query(SupplyChain).filter(SupplyChain.updated_at >= cutoff).order_by(SupplyChain.updated_at.desc()).limit(max_items).all()
        counts_by_chain_id = supply_chain_counts_by_chain_id(session, chains)
        for chain in chains:
            counts = counts_by_chain_id.get(int(chain.id), {})
            rows.append(
                {
                    "id": int(chain.id),
                    "title": chain.title,
                    "category": chain.category,
                    "product": chain.product,
                    "status": chain.status,
                    "updated_at": chain.updated_at.isoformat() if chain.updated_at else None,
                    "nodes": int(counts.get("nodes") or 0),
                    "edges": int(counts.get("edges") or 0),
                    "bindings": int(counts.get("bindings") or 0),
                    "alerts": int(counts.get("alerts") or 0),
                    "url": notes_bridge.supply_chain_url(int(chain.id)),
                }
            )
        _put("supply_chains", rows)

    if "instrument_requests" in wanted:
        rows = []
        for item in session.query(InstrumentRequest).filter(InstrumentRequest.updated_at >= cutoff).order_by(InstrumentRequest.updated_at.desc()).limit(max_items).all():
            rows.append(
                {
                    "id": int(item.id),
                    "title": item.title,
                    "intent": item.intent,
                    "status": item.status,
                    "priority": int(item.priority or 0),
                    "evidence_tier": item.evidence_tier,
                    "updated_at": item.updated_at.isoformat() if item.updated_at else None,
                    "url": notes_bridge.instrument_request_url(int(item.id)),
                }
            )
        _put("instrument_requests", rows)

    if "product_stages" in wanted:
        rows = []
        for item in session.query(ProductStageEntry).filter(ProductStageEntry.updated_at >= cutoff).order_by(ProductStageEntry.updated_at.desc()).limit(max_items).all():
            rows.append(
                {
                    "id": int(item.id),
                    "company_name": item.company_name,
                    "product_name": item.product_name,
                    "stage": item.stage,
                    "status": item.status,
                    "evidence_tier": item.evidence_tier,
                    "updated_at": item.updated_at.isoformat() if item.updated_at else None,
                    "url": notes_bridge.product_stage_url(int(item.id)),
                }
            )
        _put("product_stages", rows)

    if "transcripts" in wanted:
        q = session.query(IRArtifact).filter(IRArtifact.artifact_type == "transcript")
        q = q.filter(func.coalesce(IRArtifact.published_at, IRArtifact.created_at) >= cutoff)
        artifacts = q.order_by(IRArtifact.published_at.desc().nullslast(), IRArtifact.id.desc()).limit(max_items).all()
        rows = []
        for a in artifacts:
            rows.append(
                {
                    "id": int(a.id),
                    "issuer_id": int(a.issuer_id),
                    "document_id": int(a.document_id) if a.document_id else None,
                    "title": a.title,
                    "url": a.canonical_url,
                    "published_at": a.published_at.isoformat() if a.published_at else None,
                    "fiscal_year": a.fiscal_year,
                    "fiscal_quarter": a.fiscal_quarter,
                    "subtype": a.artifact_subtype,
                    "derived": bool(a.is_derived_transcript),
                }
            )
        _put("transcripts", rows)

    if "theses" in wanted:
        theses = session.query(Thesis).filter(Thesis.updated_at >= cutoff).order_by(Thesis.updated_at.desc()).limit(max_items).all()
        rows = []
        for t in theses:
            rows.append(
                {
                    "id": int(t.id),
                    "title": t.title,
                    "status": t.status,
                    "kind": t.kind,
                    "importance": t.importance,
                    "updated_at": t.updated_at.isoformat() if t.updated_at else None,
                    "entities": [e.canonical_name for e in (t.entities or [])][:12],
                }
            )
        _put("theses", rows)

    if "research_library" in wanted:
        payload = list_research_library(session, limit=max_items)
        reports = payload.get("reports") if isinstance(payload.get("reports"), list) else []
        rows = []
        for row in reports[:max_items]:
            if not isinstance(row, dict):
                continue
            rows.append(
                {
                    "id": row.get("id"),
                    "kind": row.get("kind"),
                    "title": row.get("title"),
                    "author": row.get("author"),
                    "published_at": row.get("published_at"),
                    "ingested_at": row.get("ingested_at"),
                    "subject_hints": row.get("subject_hints") or [],
                    "addendum_count": row.get("addendum_count"),
                    "next_full_review_at": row.get("next_full_review_at"),
                    "stale": bool(row.get("stale")),
                    "summary": str(row.get("summary") or "")[:420],
                    "url": f"{notes_bridge.public_base_url()}/docs/{int(row['id'])}" if str(row.get("id")).isdigit() else row.get("href"),
                }
            )
        _put("research_library", rows)

    if "agent_actions" in wanted:
        q = session.query(AgentAction).filter(AgentAction.updated_at >= cutoff)
        if guild_id is not None:
            q = q.filter(
                (AgentAction.guild_id == int(guild_id))
                | (AgentAction.guild_id.is_(None))
            )
        rows = []
        actions = q.order_by(AgentAction.priority.asc(), AgentAction.updated_at.desc()).limit(max_items).all()
        for action in actions:
            payload = action.payload if isinstance(action.payload, dict) else {}
            rows.append(
                {
                    "id": int(action.id),
                    "kind": action.kind,
                    "status": action.status,
                    "title": action.title,
                    "priority": int(action.priority or 0),
                    "updated_at": action.updated_at.isoformat() if action.updated_at else None,
                    "thesis_id": int(action.thesis_id) if action.thesis_id is not None else None,
                    "document_id": int(action.document_id) if action.document_id is not None else None,
                    "note_id": int(action.note_id) if action.note_id is not None else None,
                    "summary": str(action.rationale or payload.get("summary") or "")[:240],
                }
            )
        _put("agent_actions", rows)

    if "change_runs" in wanted:
        q = session.query(AgentChangeRun).options(joinedload(AgentChangeRun.changes)).filter(AgentChangeRun.updated_at >= cutoff)
        if guild_id is not None:
            q = q.filter((AgentChangeRun.guild_id == int(guild_id)) | (AgentChangeRun.guild_id.is_(None)))
        rows = [serialize_change_run(row, include_changes=False) for row in q.order_by(AgentChangeRun.updated_at.desc()).limit(max_items).all()]
        _put("change_runs", rows)

    if "thesis_proposals" in wanted:
        q = session.query(ThesisProposal).filter(ThesisProposal.updated_at >= cutoff)
        if guild_id is not None:
            q = q.filter((ThesisProposal.guild_id == int(guild_id)) | (ThesisProposal.guild_id.is_(None)))
        rows = [serialize_thesis_proposal(row) for row in q.order_by(ThesisProposal.updated_at.desc()).limit(max_items).all()]
        _put("thesis_proposals", rows)

    if "document_events" in wanted:
        events = (
            session.query(DocumentEvent)
            .filter(DocumentEvent.event_at >= cutoff)
            .order_by(DocumentEvent.event_at.desc())
            .limit(max_items)
            .all()
        )
        rows = []
        for ev in events:
            rows.append(
                {
                    "id": int(ev.id),
                    "document_id": int(ev.document_id),
                    "event_type": ev.event_type,
                    "event_at": ev.event_at.isoformat() if ev.event_at else None,
                    "meta": ev.meta if isinstance(ev.meta, dict) else {},
                }
            )
        _put("document_events", rows)

    if "ingestion_runs" in wanted:
        runs = (
            session.query(IngestionRun)
            .filter(IngestionRun.finished_at.isnot(None))
            .filter(IngestionRun.finished_at >= cutoff)
            .order_by(IngestionRun.id.desc())
            .limit(max_items)
            .all()
        )
        source_map: dict[int, str] = {}
        for sid, name in session.query(Source.id, Source.name).all():
            try:
                source_map[int(sid)] = str(name)
            except Exception:
                continue
        rows = []
        for r in runs:
            rows.append(
                {
                    "id": int(r.id),
                    "source_id": int(r.source_id),
                    "source": source_map.get(int(r.source_id)),
                    "status": r.status,
                    "items_seen": int(r.items_seen or 0),
                    "items_new": int(r.items_new or 0),
                    "items_updated": int(r.items_updated or 0),
                    "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                    "error": r.error,
                    "connector_stats": (r.extra or {}).get("connector_stats") if isinstance(r.extra, dict) else None,
                }
            )
        _put("ingestion_runs", rows)

    if "backlog_stats" in wanted:
        rows: list[dict[str, Any]] = []
        if guild_id is not None:
            pending_outbox = (
                session.query(func.count(DiscordOutboxItem.id))
                .filter(DiscordOutboxItem.guild_id == int(guild_id))
                .filter(DiscordOutboxItem.status == "pending")
                .scalar()
            )
            pending_queue = (
                session.query(func.count(DiscordQueueItem.id))
                .filter(DiscordQueueItem.guild_id == int(guild_id))
                .filter(DiscordQueueItem.status == "pending")
                .scalar()
            )
            rows.append(
                {
                    "guild_id": int(guild_id),
                    "pending_outbox": int(pending_outbox or 0),
                    "pending_queue": int(pending_queue or 0),
                }
            )
        _put("backlog_stats", rows)

    return bundle


def render_recent_context_markdown(bundle: dict[str, Any], *, max_chars: int | None = None) -> str:
    sections = bundle.get("sections") if isinstance(bundle.get("sections"), dict) else {}
    counts = bundle.get("counts") if isinstance(bundle.get("counts"), dict) else {}
    lines: list[str] = []
    lines.append(f"# Recent Thoth Context")
    lines.append(f"Generated: {bundle.get('generated_at')}")
    lines.append(f"Lookback: {bundle.get('lookback_hours')}h")
    if counts:
        lines.append("Counts: " + ", ".join([f"{k}={v}" for k, v in counts.items()]))
    lines.append("")

    for sec_name, rows in sections.items():
        if not isinstance(rows, list):
            continue
        lines.append(f"## {sec_name}")
        if not rows:
            lines.append("- (none)")
            lines.append("")
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            if sec_name == "documents":
                extraction_warning = str(row.get("extraction_warning") or "").strip()
                extraction_status = str(row.get("extraction_status") or "").strip()
                lines.append(
                    f"- doc#{row.get('id')} [{row.get('source')}] {row.get('title')} | precedence={row.get('precedence_bucket')} | triage={row.get('triage_state')} | extraction={extraction_status or 'unknown'} | {row.get('published_at') or row.get('ingested_at')}"
                )
                if extraction_warning:
                    lines.append(f"  extraction_warning: {extraction_warning[:320]}")
                signal = str(row.get("refinery_signal") or "").strip()
                summary = str(row.get("refinery_summary") or "").strip()
                why = str(row.get("refinery_why_it_matters") or "").strip()
                key_points = [
                    str(item).strip()
                    for item in (row.get("refinery_key_points") or [])
                    if str(item).strip()
                ]
                if signal:
                    lines.append(f"  signal: {signal[:180]}")
                if summary:
                    lines.append(f"  {summary[:320]}")
                if why:
                    lines.append(f"  why: {why[:240]}")
                if key_points:
                    lines.append("  points: " + " | ".join(key_points[:3])[:320])
                if not signal and not summary:
                    txt = str(row.get("text") or "").strip()
                    if txt:
                        lines.append(f"  {txt[:500]}")
            elif sec_name == "notes":
                lines.append(f"- note#{row.get('id')} {row.get('title')} | precedence={row.get('precedence_bucket')} | {row.get('updated_at')} | type={row.get('note_type')}")
                body = str(row.get("body") or "").strip()
                if body:
                    lines.append(f"  {body[:400]}")
            elif sec_name == "discord_sources":
                lines.append(f"- ${row.get('symbol')} {row.get('title')} | precedence={row.get('precedence_bucket')} | {row.get('created_at')} | {row.get('url')}")
            elif sec_name == TRADE_SECTION_NAME:
                latest = row.get("latest_point") if isinstance(row.get("latest_point"), dict) else {}
                delta_pct = row.get("delta_pct")
                yoy_delta_pct = row.get("yoy_delta_pct")
                deltas: list[str] = []
                if delta_pct is not None:
                    deltas.append(f"MoM={float(delta_pct) * 100:+.1f}%")
                if yoy_delta_pct is not None:
                    deltas.append(f"YoY={float(yoy_delta_pct) * 100:+.1f}%")
                delta_txt = " | " + " ".join(deltas) if deltas else ""
                lines.append(
                    f"- trade[{row.get('reporter_iso')}] {row.get('name')} | hs={row.get('hs_code')} | {latest.get('period_date')}{delta_txt}"
                )
            elif sec_name == "datasets":
                lines.append(
                    f"- dataset#{row.get('id')} {row.get('title')} | family={row.get('family')} rows={row.get('rows')} cols={row.get('columns')} | {row.get('updated_at')} | {row.get('url')}"
                )
                delta = row.get("value_delta") or {}
                if delta:
                    lines.append(f"  Published value delta: {delta.get('previous_version_id')} -> {delta.get('version_id')} ({delta.get('status')})")
                    for change in delta.get("changes", []):
                        lines.append(f"  {change['kind']} row {change['row_key']}")
                        for field in change.get("fields", []):
                            lines.append(f"    {field['field']}: {field['before']} -> {field['after']}")
                    if delta.get("truncated"):
                        lines.append("  More changes exist; inspect the dataset before drawing conclusions.")
            elif sec_name == "instrument_requests":
                lines.append(
                    f"- request#{row.get('id')} {row.get('title')} | status={row.get('status')} prio={row.get('priority')} tier={row.get('evidence_tier')} | {row.get('updated_at')} | {row.get('url')}"
                )
            elif sec_name == "product_stages":
                lines.append(
                    f"- stage#{row.get('id')} {row.get('company_name')} {row.get('product_name')} -> {row.get('stage')} | status={row.get('status')} tier={row.get('evidence_tier')} | {row.get('updated_at')} | {row.get('url')}"
                )
            elif sec_name == "charts":
                lines.append(
                    f"- chart#{row.get('id')} {row.get('title')} | category={row.get('category')} | {row.get('updated_at')} | {row.get('url')}"
                )
            elif sec_name == "supply_chains":
                lines.append(
                    f"- chain#{row.get('id')} {row.get('title')} | nodes={row.get('nodes')} edges={row.get('edges')} bindings={row.get('bindings')} | {row.get('updated_at')} | {row.get('url')}"
                )
            elif sec_name == "transcripts":
                lines.append(f"- transcript#{row.get('id')} {row.get('title') or row.get('url')} | {row.get('published_at')}")
            elif sec_name == "theses":
                lines.append(f"- thesis#{row.get('id')} {row.get('title')} | status={row.get('status')} | {row.get('updated_at')}")
            elif sec_name == "research_library":
                review = "stale" if row.get("stale") else f"next_review={row.get('next_full_review_at')}"
                hints = ", ".join([str(item) for item in (row.get("subject_hints") or [])][:5])
                hint_txt = f" | hints={hints}" if hints else ""
                lines.append(f"- report#{row.get('id')} {row.get('title')} | {review} | addenda={row.get('addendum_count')}{hint_txt} | {row.get('url')}")
                summary = str(row.get("summary") or "").strip()
                if summary:
                    lines.append(f"  {summary[:320]}")
            elif sec_name == "agent_actions":
                lines.append(
                    f"- action#{row.get('id')} {row.get('title')} | status={row.get('status')} | prio={row.get('priority')} | {row.get('updated_at')}"
                )
                summary = str(row.get("summary") or "").strip()
                if summary:
                    lines.append(f"  {summary[:240]}")
            elif sec_name == "change_runs":
                lines.append(
                    f"- run#{row.get('id')} {row.get('task_kind')} | status={row.get('status')} | engine={row.get('engine_kind')} | {row.get('created_at')}"
                )
                if row.get("summary"):
                    lines.append(f"  {str(row.get('summary'))[:240]}")
            elif sec_name == "thesis_proposals":
                lines.append(
                    f"- proposal#{row.get('id')} {row.get('title')} | status={row.get('status')} | novelty={row.get('novelty_score')} | {row.get('created_at')}"
                )
                if row.get("rationale"):
                    lines.append(f"  {str(row.get('rationale'))[:240]}")
            elif sec_name == "document_events":
                lines.append(f"- event#{row.get('id')} doc#{row.get('document_id')} {row.get('event_type')} | {row.get('event_at')}")
            elif sec_name == "ingestion_runs":
                lines.append(
                    f"- run#{row.get('id')} [{row.get('source')}] {row.get('status')} seen={row.get('items_seen')} new={row.get('items_new')} upd={row.get('items_updated')}"
                )
                if row.get("error"):
                    lines.append(f"  error: {str(row.get('error'))[:220]}")
            else:
                lines.append(f"- {jsonable_row(row)}")
        lines.append("")

    text = "\n".join(lines).strip() + "\n"
    if max_chars and max_chars > 0 and len(text) > max_chars:
        return text[: max(0, int(max_chars) - 1)] + "…"
    return text


def jsonable_row(row: dict[str, Any]) -> str:
    parts = []
    for k, v in row.items():
        if isinstance(v, (dict, list)):
            continue
        if v is None:
            continue
        parts.append(f"{k}={v}")
    return ", ".join(parts)
