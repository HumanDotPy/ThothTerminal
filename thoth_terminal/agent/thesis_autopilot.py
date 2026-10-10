from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from thoth_terminal.agent.autonomy import (
    finish_change_run,
    record_change_event,
    start_change_run,
    create_thesis_proposal,
)
from thoth_terminal.agent.execution_policy import TASK_THESIS_AUTOPILOT, select_synthesis_engine
from thoth_terminal.agent.evidence_policy import (
    PRECEDENCE_DEPRECATED_LOW_CONFIDENCE,
    corroboration_strength,
    issuer_proximity_score,
    needs_corroboration,
    precedence_bucket,
    source_quality_score,
    translation_risk_score,
)
from thoth_terminal.agent_actions import upsert_agent_action
from thoth_terminal.db import utcnow
from thoth_terminal.models import Document, DocumentEntity, Domain, Entity, Thesis, ThesisEvidence, ThesisProposal
from thoth_terminal.notes_service import create_note
from thoth_terminal.thesis_service import add_thesis_evidence
from thoth_terminal.trade import relevant_trade_context


_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9\-/]{2,}", re.IGNORECASE)
_STOPWORDS = {
    "about",
    "after",
    "again",
    "against",
    "among",
    "analyst",
    "analysts",
    "around",
    "because",
    "between",
    "build",
    "built",
    "could",
    "every",
    "from",
    "have",
    "into",
    "just",
    "many",
    "move",
    "news",
    "report",
    "reports",
    "rumor",
    "rumors",
    "says",
    "should",
    "that",
    "their",
    "there",
    "these",
    "they",
    "this",
    "update",
    "using",
    "with",
    "will",
}

AUTOPILOT_ACTION_KIND = "thesis_autopilot_review"


@dataclass(frozen=True)
class CandidateScore:
    document_id: int
    score: float
    precedence_bucket: str
    source_quality: float
    issuer_proximity: float
    locality: float
    corroboration_strength: float
    keyword_overlap: float
    domain_overlap: float
    freshness: float
    translation_risk: float
    needs_corroboration: bool
    entity_overlap_count: int
    matched_entities: tuple[str, ...]
    precedence_note: str
    reasons: tuple[str, ...]


def _norm_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _keyword_tokens(*parts: str | None, limit: int = 18) -> list[str]:
    seen: list[str] = []
    for part in parts:
        for raw in _TOKEN_RE.findall(str(part or "").lower()):
            token = raw.strip("-/")
            if len(token) < 3 or token in _STOPWORDS:
                continue
            if token not in seen:
                seen.append(token)
            if len(seen) >= limit:
                return seen
    return seen


def _doc_title(doc: Document) -> str:
    extra = doc.extra if isinstance(doc.extra, dict) else {}
    translated = extra.get("translation_title_en")
    if translated:
        return str(translated)
    return str(doc.title or doc.url or f"doc {doc.id}")


def _doc_text(doc: Document, *, max_chars: int = 4000) -> str:
    parts = [_doc_title(doc), doc.translation_en, doc.normalized_text]
    return "\n".join([str(p).strip() for p in parts if str(p or "").strip()])[:max_chars]


def compute_evidence_need(
    thesis: Thesis,
    *,
    now=None,
    evidence_count: int | None = None,
    last_evidence_at=None,
    contradict_count: int = 0,
) -> dict[str, Any]:
    now_v = now or utcnow()
    evidence_total = int(evidence_count if evidence_count is not None else len(thesis.evidence or []))
    last_ts = last_evidence_at
    if last_ts is None and thesis.evidence:
        last_ts = max((ev.created_at for ev in thesis.evidence if ev.created_at), default=None)
    base = 12 + int(thesis.importance or 0) * 11
    if thesis.status in {"watching", "active"}:
        base += 8
    if thesis.status == "confirmed":
        base -= 6
    if thesis.status in {"paused", "disproven"}:
        base -= 18
    if evidence_total == 0:
        base += 24
    elif evidence_total <= 2:
        base += 10
    else:
        base -= min(14, (evidence_total - 2) * 3)
    overdue_days = None
    if thesis.stale_after_days:
        basis = last_ts or thesis.created_at
        overdue_days = (now_v - basis).days - int(thesis.stale_after_days)
        if overdue_days > 0:
            base += min(28, overdue_days * 2)
    if contradict_count > 0:
        base += min(16, contradict_count * 6)
    score = max(0, min(100, int(base)))
    label = "high" if score >= 70 else "medium" if score >= 45 else "low"
    return {"score": score, "label": label, "overdue_days": overdue_days}


def _candidate_precedence_note(source_quality: float, best_existing_quality: float) -> str:
    if source_quality >= best_existing_quality + 0.08:
        return "higher source-quality prior than current evidence"
    if source_quality + 0.10 < best_existing_quality:
        return "lower source-quality prior than current evidence"
    return "similar source-quality prior to current evidence"


def _score_document_for_thesis(
    thesis: Thesis,
    doc: Document,
    *,
    now,
    best_existing_quality: float,
) -> CandidateScore:
    thesis_entities = [str(e.canonical_name or "").strip().upper() for e in (thesis.entities or []) if str(e.canonical_name or "").strip()]
    doc_entities = {
        str(link.entity.canonical_name or "").strip().upper()
        for link in (doc.entity_links or [])
        if link.entity and str(link.entity.canonical_name or "").strip()
    }
    matched_entities = tuple(sorted(set(thesis_entities) & doc_entities))
    entity_overlap_count = len(matched_entities)
    entity_ratio = (entity_overlap_count / len(thesis_entities)) if thesis_entities else 0.0

    tokens = _keyword_tokens(thesis.title, thesis.statement, limit=14)
    text = _norm_text(_doc_text(doc))
    keyword_hits = 0
    for token in tokens:
        if token in text:
            keyword_hits += 1
    keyword_overlap = (keyword_hits / len(tokens)) if tokens else 0.0

    thesis_domains = {str(d.slug or "").strip().lower() for d in (thesis.domains or []) if str(d.slug or "").strip()}
    doc_domains = {str(d.slug or "").strip().lower() for d in (doc.domains or []) if str(d.slug or "").strip()}
    domain_overlap = (len(thesis_domains & doc_domains) / len(thesis_domains)) if thesis_domains else 0.0

    base_time = doc.published_at or doc.ingested_at or now
    age_days = max(0.0, (now - base_time).total_seconds() / 86400.0)
    freshness = max(0.15, 1.0 - min(age_days, 14.0) / 14.0)

    locality = 0.65 * entity_ratio + 0.25 * keyword_overlap + 0.10 * domain_overlap
    if len(thesis_entities) >= 2 and entity_overlap_count < min(2, len(thesis_entities)):
        locality *= 0.72
    if entity_overlap_count == 0 and keyword_overlap == 0:
        locality *= 0.3

    bucket = precedence_bucket(doc)
    source_quality = source_quality_score(doc)
    translation_risk = translation_risk_score(doc)
    issuer_proximity = issuer_proximity_score(
        bucket=bucket,
        thesis_entities=thesis_entities,
        doc_entities=doc_entities,
    )
    corroboration = corroboration_strength(
        bucket=bucket,
        source_quality=source_quality,
        translation_risk=translation_risk,
        best_existing_quality=best_existing_quality,
        existing_evidence_count=len(thesis.evidence or []),
    )
    needs_corr = needs_corroboration(
        bucket=bucket,
        source_quality=source_quality,
        translation_risk=translation_risk,
        corroboration_strength=corroboration,
    )
    score = (
        0.35 * source_quality
        + 0.25 * issuer_proximity
        + 0.30 * locality
        + 0.10 * freshness
        + 0.08 * corroboration
        - 0.16 * translation_risk
    )
    score = max(0.0, min(1.0, score))

    reasons: list[str] = []
    if matched_entities:
        reasons.append("entity match: " + ", ".join(matched_entities[:4]))
    elif thesis_entities:
        reasons.append("no direct entity match")
    if keyword_overlap >= 0.25:
        reasons.append(f"statement/title overlap {int(round(keyword_overlap * 100))}%")
    if domain_overlap > 0:
        reasons.append("domain overlap")
    if source_quality >= 0.82:
        reasons.append("high-quality source")
    elif source_quality < 0.62:
        reasons.append("lower source-quality prior")
    if translation_risk >= 0.35:
        reasons.append("translation risk")
    if bucket == PRECEDENCE_DEPRECATED_LOW_CONFIDENCE:
        reasons.append("deprecated source")
    precedence_note = _candidate_precedence_note(source_quality, best_existing_quality)
    reasons.append(precedence_note)

    return CandidateScore(
        document_id=int(doc.id),
        score=score,
        precedence_bucket=bucket,
        source_quality=source_quality,
        issuer_proximity=issuer_proximity,
        locality=max(0.0, min(1.0, locality)),
        corroboration_strength=corroboration,
        keyword_overlap=max(0.0, min(1.0, keyword_overlap)),
        domain_overlap=max(0.0, min(1.0, domain_overlap)),
        freshness=freshness,
        translation_risk=max(0.0, min(1.0, translation_risk)),
        needs_corroboration=bool(needs_corr),
        entity_overlap_count=entity_overlap_count,
        matched_entities=matched_entities,
        precedence_note=precedence_note,
        reasons=tuple(reasons),
    )


def _load_thesis(session: Session, thesis_id: int) -> Thesis | None:
    return (
        session.query(Thesis)
        .options(
            joinedload(Thesis.entities),
            joinedload(Thesis.domains),
            joinedload(Thesis.evidence)
            .joinedload(ThesisEvidence.document)
            .joinedload(Document.source),
            joinedload(Thesis.evidence)
            .joinedload(ThesisEvidence.document)
            .joinedload(Document.domains),
            joinedload(Thesis.evidence)
            .joinedload(ThesisEvidence.document)
            .joinedload(Document.entity_links)
            .joinedload(DocumentEntity.entity),
        )
        .filter(Thesis.id == int(thesis_id))
        .first()
    )


def _is_agent_generated_document(doc: Document | None) -> bool:
    if not isinstance(doc, Document):
        return False
    extra = doc.extra if isinstance(doc.extra, dict) else {}
    if bool(extra.get("agent_generated")):
        return True
    return str(doc.kind or "").strip() in {"agent_digest", "agent_context_compression"}


def build_thesis_autopilot_snapshot(
    session: Session,
    *,
    thesis_id: int,
    lookback_hours: int = 72,
    candidate_limit: int = 5,
) -> dict[str, Any]:
    thesis = _load_thesis(session, thesis_id)
    if not thesis:
        raise ValueError("Thesis not found.")

    now = utcnow()
    cutoff = now - timedelta(hours=max(1, int(lookback_hours or 72)))
    existing_doc_ids = {int(ev.document_id) for ev in (thesis.evidence or []) if ev.document_id is not None}
    best_existing_quality = max(
        (source_quality_score(ev.document) for ev in (thesis.evidence or []) if ev.document),
        default=0.0,
    )
    contradict_count = sum(1 for ev in (thesis.evidence or []) if str(ev.polarity or "") == "contradict")
    need = compute_evidence_need(
        thesis,
        now=now,
        evidence_count=len(thesis.evidence or []),
        contradict_count=contradict_count,
    )

    thesis_entity_names = [str(e.canonical_name or "").strip().upper() for e in (thesis.entities or []) if str(e.canonical_name or "").strip()]
    q = session.query(Document.id, Document.ingested_at).filter(Document.ingested_at >= cutoff)
    if existing_doc_ids:
        q = q.filter(~Document.id.in_(list(existing_doc_ids)))
    if thesis_entity_names:
        q = (
            q.join(DocumentEntity, DocumentEntity.document_id == Document.id)
            .join(Entity, Entity.id == DocumentEntity.entity_id)
            .filter(func.upper(Entity.canonical_name).in_(thesis_entity_names))
        )
    candidate_id_rows = (
        q.distinct()
        .order_by(Document.ingested_at.desc(), Document.id.desc())
        .limit(max(40, candidate_limit * 10))
        .all()
    )
    candidate_doc_ids = [int(row[0]) for row in candidate_id_rows if row and row[0] is not None]
    order_by_id = {doc_id: idx for idx, doc_id in enumerate(candidate_doc_ids)}
    raw_candidate_docs = []
    if candidate_doc_ids:
        raw_candidate_docs = (
            session.query(Document)
            .options(
                joinedload(Document.source),
                joinedload(Document.domains),
                joinedload(Document.entity_links).joinedload(DocumentEntity.entity),
            )
            .filter(Document.id.in_(candidate_doc_ids))
            .all()
        )
        raw_candidate_docs.sort(key=lambda doc: order_by_id.get(int(doc.id), len(order_by_id)))
    candidate_docs = [doc for doc in raw_candidate_docs if not _is_agent_generated_document(doc)]

    scored: list[tuple[Document, CandidateScore]] = []
    for doc in candidate_docs:
        score = _score_document_for_thesis(thesis, doc, now=now, best_existing_quality=best_existing_quality)
        if score.score < 0.28:
            continue
        scored.append((doc, score))
    scored.sort(key=lambda item: (item[1].score, item[0].ingested_at, item[0].id), reverse=True)

    candidates: list[dict[str, Any]] = []
    for doc, score in scored[: max(1, int(candidate_limit or 5))]:
        candidates.append(
            {
                "document_id": int(doc.id),
                "title": _doc_title(doc),
                "url": doc.url or doc.canonical_url,
                "source": doc.source.name if doc.source else None,
                "precedence_bucket": score.precedence_bucket,
                "source_quality": round(score.source_quality, 3),
                "issuer_proximity": round(score.issuer_proximity, 3),
                "locality": round(score.locality, 3),
                "corroboration_strength": round(score.corroboration_strength, 3),
                "keyword_overlap": round(score.keyword_overlap, 3),
                "domain_overlap": round(score.domain_overlap, 3),
                "freshness": round(score.freshness, 3),
                "translation_risk": round(score.translation_risk, 3),
                "needs_corroboration": bool(score.needs_corroboration),
                "score": round(score.score, 3),
                "score_pct": int(round(score.score * 100)),
                "precedence_note": score.precedence_note,
                "matched_entities": list(score.matched_entities),
                "reasons": list(score.reasons),
                "published_at": (doc.published_at or doc.ingested_at).isoformat() if (doc.published_at or doc.ingested_at) else None,
            }
        )

    strongest = candidates[0] if candidates else None
    trade_context = relevant_trade_context(
        session,
        text_parts=[thesis.title, thesis.statement],
        entity_tokens=thesis_entity_names,
        limit=6,
    )
    summary_bits = [f"evidence need {need['score']}/100 ({need['label']})"]
    if need.get("overdue_days") is not None and int(need["overdue_days"] or 0) > 0:
        summary_bits.append(f"overdue {int(need['overdue_days'])}d")
    if strongest:
        summary_bits.append(
            f"top candidate #{strongest['document_id']} {strongest['score_pct']}% via {strongest.get('source') or 'source'}"
        )
    elif not thesis.evidence:
        summary_bits.append("no evidence linked yet")
    else:
        summary_bits.append("no recent candidate evidence")
    if trade_context:
        summary_bits.append(f"trade context {len(trade_context)} series")

    return {
        "thesis_id": int(thesis.id),
        "title": thesis.title,
        "evidence_need": need,
        "candidate_count": len(candidates),
        "best_existing_source_quality": round(best_existing_quality, 3),
        "top_candidates": candidates,
        "trade_context": trade_context,
        "summary": " • ".join(summary_bits),
    }


def enqueue_autopilot_actions(
    session: Session,
    *,
    thesis_id: int | None = None,
    guild_id: int | None = None,
    lookback_hours: int = 72,
    candidate_limit: int = 5,
    limit_theses: int = 50,
) -> list[dict[str, Any]]:
    q = (
        session.query(Thesis.id)
        .filter(Thesis.status.in_(["watching", "active", "confirmed"]))
        .order_by(Thesis.updated_at.desc(), Thesis.id.desc())
    )
    if thesis_id is not None:
        q = q.filter(Thesis.id == int(thesis_id))
    thesis_ids = [int(row[0]) for row in q.limit(max(1, min(int(limit_theses or 50), 300))).all()]

    out: list[dict[str, Any]] = []
    for th_id in thesis_ids:
        snapshot = build_thesis_autopilot_snapshot(
            session,
            thesis_id=int(th_id),
            lookback_hours=lookback_hours,
            candidate_limit=candidate_limit,
        )
        need = snapshot.get("evidence_need") or {}
        score = int(need.get("score") or 0)
        top_candidates = snapshot.get("top_candidates") if isinstance(snapshot.get("top_candidates"), list) else []
        strongest = top_candidates[0] if top_candidates else None
        strongest_score = float(strongest.get("score") or 0.0) if isinstance(strongest, dict) else 0.0
        if score < 45 and strongest_score < 0.55:
            continue
        priority = 2 if score >= 80 or strongest_score >= 0.78 else 4 if score >= 60 or strongest_score >= 0.62 else 6
        basis = f"{th_id}:{score}:{','.join(str(item.get('document_id')) for item in top_candidates[:3])}"
        action = upsert_agent_action(
            session,
            guild_id=guild_id,
            kind=AUTOPILOT_ACTION_KIND,
            title=f"Review thesis: {str(snapshot.get('title') or f'thesis {th_id}')[:220]}",
            priority=priority,
            dedupe_key=f"thesis_autopilot:{int(th_id)}",
            rationale=str(snapshot.get("summary") or ""),
            evidence_score=strongest_score if strongest else None,
            thesis_id=int(th_id),
            document_id=int(strongest.get("document_id")) if isinstance(strongest, dict) and strongest.get("document_id") else None,
            payload={
                "autopilot_snapshot": snapshot,
                "autopilot_signature": hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16],
                "lookback_hours": int(lookback_hours),
            },
        )
        out.append(
            {
                "action_id": int(action.id),
                "thesis_id": int(th_id),
                "title": snapshot.get("title"),
                "summary": snapshot.get("summary"),
                "candidate_count": len(top_candidates),
                "top_score": round(strongest_score, 3) if strongest else None,
            }
        )
    return out


def _actionable_candidate(candidate: dict[str, Any]) -> bool:
    if not isinstance(candidate, dict):
        return False
    if str(candidate.get("precedence_bucket") or "") == PRECEDENCE_DEPRECATED_LOW_CONFIDENCE:
        return False
    return float(candidate.get("score") or 0.0) >= 0.67


def _proposal_similarity(thesis: Thesis, doc: Document) -> float:
    thesis_entities = {str(e.canonical_name or "").strip().upper() for e in (thesis.entities or []) if str(e.canonical_name or "").strip()}
    doc_entities = {
        str(link.entity.canonical_name or "").strip().upper()
        for link in (doc.entity_links or [])
        if link.entity and str(link.entity.canonical_name or "").strip()
    }
    entity_overlap = (len(thesis_entities & doc_entities) / max(1, len(thesis_entities))) if thesis_entities else 0.0
    thesis_tokens = set(_keyword_tokens(thesis.title, thesis.statement, limit=18))
    doc_tokens = set(_keyword_tokens(_doc_title(doc), _doc_text(doc, max_chars=800), limit=18))
    token_overlap = (len(thesis_tokens & doc_tokens) / max(1, len(thesis_tokens))) if thesis_tokens else 0.0
    return max(0.0, min(1.0, 0.62 * entity_overlap + 0.38 * token_overlap))


def _proposal_note_body(
    *,
    doc: Document,
    novelty_score: float,
    confidence: float,
    bucket: str,
) -> str:
    lines = [
        f"# Thesis Proposal — {_doc_title(doc)}",
        "",
        f"- Novelty score: {novelty_score:.2f}",
        f"- Confidence: {confidence:.2f}",
        f"- Evidence channel: {bucket}",
        f"- Source: {doc.source.name if doc.source else 'unknown'}",
        f"- URL: {doc.url or doc.canonical_url or ''}",
        "",
        "## Why this is proposed",
        "This document appears materially different from existing tracked theses and is strong enough to warrant explicit review.",
        "",
        "## Source Excerpt",
        (_doc_text(doc, max_chars=900) or "").strip(),
        "",
    ]
    return "\n".join(line for line in lines if line is not None).strip() + "\n"


def _run_note_body(
    *,
    lookback_hours: int,
    applied_lines: list[str],
    proposal_lines: list[str],
) -> str:
    lines = [
        f"# Thesis Autopilot Run",
        "",
        f"_Lookback: {int(lookback_hours)}h_",
        "",
        "## Applied Changes",
    ]
    if applied_lines:
        lines.extend(f"- {line}" for line in applied_lines[:24])
    else:
        lines.append("- No autonomous thesis changes applied.")
    lines.append("")
    lines.append("## New Thesis Proposals")
    if proposal_lines:
        lines.extend(f"- {line}" for line in proposal_lines[:12])
    else:
        lines.append("- No new thesis proposals created.")
    lines.append("")
    return "\n".join(lines)


def run_autonomous_thesis_cycle(
    session: Session,
    *,
    thesis_id: int | None = None,
    guild_id: int | None = None,
    lookback_hours: int = 72,
    candidate_limit: int = 5,
    limit_theses: int = 50,
    source_job_id: int | None = None,
    automation_source: str = "agent_thesis_autopilot",
) -> dict[str, Any]:
    now = utcnow()
    engine_selection = select_synthesis_engine(task_kind=TASK_THESIS_AUTOPILOT)
    engine_kind = str(engine_selection.get("engine") or "legacy_internal")
    run = start_change_run(
        session,
        guild_id=guild_id,
        task_kind="thesis_autopilot",
        engine_kind=engine_kind,
        automation_source=automation_source,
        source_job_id=source_job_id,
        payload={
            "lookback_hours": int(lookback_hours),
            "engine_policy": str(engine_selection.get("policy") or "policy_routed"),
            "requested_engine": engine_selection.get("requested_engine"),
            "fallback_chain": list(engine_selection.get("fallback_chain") or []),
        },
    )
    applied_lines: list[str] = []
    proposal_lines: list[str] = []
    used_doc_ids: set[int] = set()

    q = (
        session.query(Thesis.id)
        .filter(Thesis.status.in_(["watching", "active", "confirmed"]))
        .order_by(Thesis.updated_at.desc(), Thesis.id.desc())
    )
    if thesis_id is not None:
        q = q.filter(Thesis.id == int(thesis_id))
    thesis_ids = [int(row[0]) for row in q.limit(max(1, min(int(limit_theses or 50), 300))).all()]
    loaded_theses = [_load_thesis(session, th_id) for th_id in thesis_ids]
    loaded_theses = [th for th in loaded_theses if th is not None]

    for thesis in loaded_theses:
        snapshot = build_thesis_autopilot_snapshot(
            session,
            thesis_id=int(thesis.id),
            lookback_hours=lookback_hours,
            candidate_limit=candidate_limit,
        )
        top_candidates = snapshot.get("top_candidates") if isinstance(snapshot.get("top_candidates"), list) else []
        strongest = top_candidates[0] if top_candidates else None
        if not _actionable_candidate(strongest or {}):
            continue
        doc_id = int(strongest.get("document_id"))
        ev = add_thesis_evidence(
            session,
            thesis=thesis,
            document_id=doc_id,
            polarity="support",
            note=f"Autonomous thesis update via {strongest.get('precedence_bucket')} evidence.",
            extra={
                "autonomous": True,
                "precedence_bucket": strongest.get("precedence_bucket"),
                "source_quality": strongest.get("source_quality"),
                "issuer_proximity": strongest.get("issuer_proximity"),
                "corroboration_strength": strongest.get("corroboration_strength"),
                "translation_risk": strongest.get("translation_risk"),
            },
        )
        if ev:
            used_doc_ids.add(doc_id)
            applied_lines.append(
                f"Attached doc #{doc_id} to thesis #{int(thesis.id)} {thesis.title} ({strongest.get('precedence_bucket')}, score {int(strongest.get('score_pct') or 0)}%)."
            )
            record_change_event(
                session,
                run=run,
                object_type="thesis",
                object_id=int(thesis.id),
                change_kind="add_evidence",
                before_summary="no linked evidence from this document",
                after_summary=f"linked document #{doc_id}",
                confidence=float(strongest.get("score") or 0.0),
                rationale=str(snapshot.get("summary") or ""),
                dedupe_key=f"thesis-evidence:{int(thesis.id)}:{doc_id}",
                payload={
                    "document_id": doc_id,
                    "precedence_bucket": strongest.get("precedence_bucket"),
                    "source_quality": strongest.get("source_quality"),
                    "issuer_proximity": strongest.get("issuer_proximity"),
                    "corroboration_strength": strongest.get("corroboration_strength"),
                },
            )
        before_status = str(thesis.status or "")
        strongest_score = float(strongest.get("score") or 0.0)
        corroboration = float(strongest.get("corroboration_strength") or 0.0)
        if strongest_score >= 0.86 and corroboration >= 0.60 and before_status in {"watching", "active"}:
            thesis.status = "confirmed"
        elif strongest_score >= 0.72 and before_status == "watching":
            thesis.status = "active"
        if thesis.status != before_status:
            thesis.updated_at = now
            applied_lines.append(f"Moved thesis #{int(thesis.id)} {thesis.title} from {before_status} to {thesis.status}.")
            record_change_event(
                session,
                run=run,
                object_type="thesis",
                object_id=int(thesis.id),
                change_kind="status_change",
                before_summary=before_status,
                after_summary=str(thesis.status),
                confidence=strongest_score,
                rationale=str(snapshot.get("summary") or ""),
                dedupe_key=f"thesis-status:{int(thesis.id)}:{before_status}:{thesis.status}",
                payload={"document_id": doc_id, "score": strongest_score},
            )

    cutoff = now - timedelta(hours=max(1, int(lookback_hours or 72)))
    proposal_docs = (
        session.query(Document)
        .options(
            joinedload(Document.source),
            joinedload(Document.entity_links).joinedload(DocumentEntity.entity),
        )
        .filter(Document.ingested_at >= cutoff)
        .order_by(Document.ingested_at.desc(), Document.id.desc())
        .limit(80)
        .all()
    )
    for doc in proposal_docs:
        if _is_agent_generated_document(doc):
            continue
        if int(doc.id) in used_doc_ids:
            continue
        bucket = precedence_bucket(doc)
        quality = source_quality_score(doc)
        translation_risk = translation_risk_score(doc)
        if bucket == PRECEDENCE_DEPRECATED_LOW_CONFIDENCE or translation_risk >= 0.70:
            continue
        entity_tokens = [
            str(link.entity.canonical_name or "").strip()
            for link in (doc.entity_links or [])
            if link.entity and str(link.entity.canonical_name or "").strip()
        ]
        if not entity_tokens:
            continue
        max_similarity = max((_proposal_similarity(th, doc) for th in loaded_theses), default=0.0)
        novelty_score = max(0.0, min(1.0, 1.0 - max_similarity))
        confidence = max(0.0, min(1.0, 0.55 * novelty_score + 0.45 * quality))
        if novelty_score < 0.62 or confidence < 0.68:
            continue
        proposal_cid = f"thesis-proposal:{int(doc.id)}"
        existing_proposal = session.query(ThesisProposal).filter(ThesisProposal.client_request_id == proposal_cid).first()
        proposal = create_thesis_proposal(
            session,
            title=_doc_title(doc)[:255],
            statement=_doc_title(doc)[:500],
            entity_tokens=entity_tokens[:8],
            guild_id=guild_id,
            kind="claim",
            importance=3,
            stale_after_days=90,
            novelty_score=novelty_score,
            confidence=confidence,
            rationale=f"Novel candidate from {doc.source.name if doc.source else 'source'} using {bucket} evidence.",
            evidence=[
                {
                    "document_id": int(doc.id),
                    "title": _doc_title(doc),
                    "url": doc.url or doc.canonical_url,
                    "precedence_bucket": bucket,
                    "source_quality": round(quality, 3),
                }
            ],
            source_job_id=source_job_id,
            client_request_id=proposal_cid,
            note_body_md=_proposal_note_body(doc=doc, novelty_score=novelty_score, confidence=confidence, bucket=bucket),
            payload={"domains": [str(d.slug or "").strip() for d in (doc.domains or []) if str(d.slug or "").strip()]},
        )
        if existing_proposal is None:
            proposal_lines.append(f"Created proposal #{int(proposal.id)} for {_doc_title(doc)}.")
            record_change_event(
                session,
                run=run,
                object_type="thesis_proposal",
                object_id=int(proposal.id),
                change_kind="proposal_created",
                before_summary=None,
                after_summary=proposal.title,
                confidence=confidence,
                rationale=proposal.rationale,
                dedupe_key=f"proposal:{int(proposal.id)}",
                payload={"document_id": int(doc.id), "novelty_score": novelty_score},
            )
            used_doc_ids.add(int(doc.id))
        if len(proposal_lines) >= 3:
            break

    note = create_note(
        session,
        title=f"Thesis Autopilot Run — {now.strftime('%Y-%m-%d %H:%M')}",
        body_md=_run_note_body(lookback_hours=lookback_hours, applied_lines=applied_lines, proposal_lines=proposal_lines),
        note_type="general",
        importance=3,
        client_request_id=f"thesis-autopilot-run:{int(run.id)}",
        extra={"agent": {"kind": "autonomous_change_run", "run_id": int(run.id)}},
    )
    summary_bits = [f"{len(applied_lines)} applied change(s)", f"{len(proposal_lines)} proposal(s)"]
    finish_change_run(
        session,
        run=run,
        status="success",
        summary=" • ".join(summary_bits),
        note_id=int(note.id),
        payload={"applied_changes": len(applied_lines), "proposal_count": len(proposal_lines)},
    )
    return {
        "run_id": int(run.id),
        "note_id": int(note.id),
        "applied_changes": len(applied_lines),
        "proposal_count": len(proposal_lines),
    }
