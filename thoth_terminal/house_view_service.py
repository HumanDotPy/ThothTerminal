from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from thoth_terminal.db import utcnow
from thoth_terminal.instrument_service import create_instrument_request, serialize_instrument_request
from thoth_terminal.models import (
    AnalystEvidence,
    Company,
    InstrumentRequest,
    Issuer,
    LocalAgentEntityRoute,
    ResearchInstrument,
    ResearchSubject,
    ResearchUniverse,
    ResearchUniverseRow,
    Thesis,
)
from thoth_terminal.thesis_service import create_thesis, resolve_entities


HOUSE_VIEW_EXTRA_KEY = "house_view"
HOUSE_VIEW_REQUEST_INTENT = "house_view_interview"
HOUSE_VIEW_REQUEST_SOURCE_KIND = "house_view_question"
HOUSE_VIEW_ACTIVE_STATUSES = {"watching", "active", "confirmed"}
USER_IDEA_ORIGINS = {"user", "owner", "operator", "self"}


def _slugify(value: object, *, default: str = "house-view") -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return cleaned[:160].strip("-") or default


def _clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _norm(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _as_list(value: object) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if value in (None, ""):
        return []
    return [value]


def _int_set(value: object) -> set[int]:
    out: set[int] = set()
    for item in _as_list(value):
        try:
            if isinstance(item, bool):
                continue
            out.add(int(str(item).strip()))
        except Exception:
            continue
    return out


def _str_list(value: object) -> list[str]:
    out: list[str] = []
    for item in _as_list(value):
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def _parse_iso_dt(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def normalize_note_provenance_extra(extra: dict[str, Any] | None) -> dict[str, Any]:
    """Mark notes as context/evidence rails, not user-origin house-view theses."""
    payload = dict(extra or {})
    provenance = dict(payload.get("provenance") or {})
    provenance.setdefault("rail", "note")
    provenance.setdefault("self_authored_idea", False)
    provenance.setdefault("idea_origin", "external_or_agent_context")
    provenance.setdefault("authority", "context_not_house_view")
    payload["provenance"] = provenance
    return payload


def _house_view_extra(
    *,
    subject: ResearchSubject | None,
    consensus_view: str | None,
    variant_view: str | None,
    model_drivers: list[str] | None,
    technology_assumptions: list[str] | None,
    invalidation_triggers: list[str] | None,
    open_questions: list[str] | None,
    confidence: float | None,
    time_horizon: str | None,
    review_cadence_days: int,
    question_cooldown_days: int,
    idea_origin: str,
    authoring_agent: str | None,
    audit: dict[str, Any] | None,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = utcnow()
    prior = dict(existing or {})
    house = dict(prior.get(HOUSE_VIEW_EXTRA_KEY) or {})
    provenance = dict(prior.get("provenance") or {})

    last_user_reviewed_at = now
    next_review_due_at = last_user_reviewed_at + timedelta(days=max(1, int(review_cadence_days or 90)))
    subject_ids = _int_set(house.get("subject_ids"))
    issuer_ids = _int_set(house.get("issuer_ids"))
    company_ids = _int_set(house.get("company_ids"))
    if subject is not None:
        subject_ids.add(int(subject.id))
        if subject.issuer_id is not None:
            issuer_ids.add(int(subject.issuer_id))
        if subject.company_id is not None:
            company_ids.add(int(subject.company_id))

    house.update(
        {
            "mode_scope": "buy_side",
            "consensus_view": _clean(consensus_view),
            "variant_view": _clean(variant_view),
            "model_drivers": _str_list(model_drivers),
            "technology_assumptions": _str_list(technology_assumptions),
            "invalidation_triggers": _str_list(invalidation_triggers),
            "open_questions": _str_list(open_questions),
            "confidence": float(confidence) if confidence is not None else house.get("confidence"),
            "time_horizon": _clean(time_horizon),
            "review_cadence_days": max(1, int(review_cadence_days or 90)),
            "question_cooldown_days": max(1, int(question_cooldown_days or review_cadence_days or 90)),
            "last_user_reviewed_at": last_user_reviewed_at.isoformat(),
            "next_review_due_at": next_review_due_at.isoformat(),
            "subject_ids": sorted(subject_ids),
            "issuer_ids": sorted(issuer_ids),
            "company_ids": sorted(company_ids),
            "updated_at": now.isoformat(),
        }
    )
    if subject is not None:
        house["primary_subject"] = {
            "id": int(subject.id),
            "subject_key": subject.subject_key,
            "display_name": subject.display_name,
            "kind": subject.kind,
        }
    provenance.update(
        {
            "rail": "thesis",
            "self_authored_idea": True,
            "idea_origin": _clean(idea_origin).lower() or "user",
            "authority": "user_house_view",
            "agent_may_draft_wording": True,
        }
    )
    if authoring_agent:
        provenance["authoring_agent"] = _clean(authoring_agent)
    if audit:
        house["audit"] = dict(audit)
    prior[HOUSE_VIEW_EXTRA_KEY] = house
    prior["provenance"] = provenance
    return prior


def _validate_user_origin(*, user_confirmed: bool, idea_origin: str) -> None:
    origin = _clean(idea_origin).lower() or "user"
    if not bool(user_confirmed) or origin not in USER_IDEA_ORIGINS:
        raise ValueError(
            "House-view theses require a user-confirmed, self-authored idea. "
            "Use a house-view question/request for agent- or sell-side-derived candidates."
        )


def _subject_entity_tokens(session: Session, subject: ResearchSubject | None, explicit: list[str] | None) -> list[str]:
    tokens = [value for value in _str_list(explicit) if value]
    if subject is None:
        return tokens
    for value in (subject.display_name, subject.legal_name, subject.slug, subject.subject_key):
        if value and str(value).strip() not in tokens:
            tokens.append(str(value).strip())
    if subject.company_id is not None:
        company = session.get(Company, int(subject.company_id))
        if company is not None:
            for value in (company.name, company.ticker):
                if value and str(value).strip() not in tokens:
                    tokens.append(str(value).strip())
    if subject.issuer_id is not None:
        issuer = session.get(Issuer, int(subject.issuer_id))
        if issuer is not None:
            for value in (issuer.display_name, issuer.legal_name):
                if value and str(value).strip() not in tokens:
                    tokens.append(str(value).strip())
    return tokens or [subject.display_name if subject is not None else "House View"]


def upsert_house_view_thesis(
    session: Session,
    *,
    title: str,
    statement: str,
    subject_id: int | None = None,
    entity_tokens: list[str] | None = None,
    consensus_view: str | None = None,
    variant_view: str | None = None,
    model_drivers: list[str] | None = None,
    technology_assumptions: list[str] | None = None,
    invalidation_triggers: list[str] | None = None,
    open_questions: list[str] | None = None,
    confidence: float | None = None,
    time_horizon: str | None = None,
    importance: int = 4,
    status: str = "active",
    stale_after_days: int | None = None,
    review_cadence_days: int = 90,
    question_cooldown_days: int | None = None,
    idea_origin: str = "user",
    user_confirmed: bool = False,
    authoring_agent: str | None = "codex",
    client_request_id: str | None = None,
    audit: dict[str, Any] | None = None,
) -> Thesis:
    _validate_user_origin(user_confirmed=user_confirmed, idea_origin=idea_origin)
    title_clean = _clean(title)
    statement_clean = str(statement or "").strip()
    if not title_clean or not statement_clean:
        raise ValueError("title and statement are required.")
    subject = session.get(ResearchSubject, int(subject_id)) if subject_id is not None else None
    if subject_id is not None and subject is None:
        raise ValueError("Research subject not found.")
    cid = _clean(client_request_id)
    if not cid:
        digest = hashlib.sha1(statement_clean.encode("utf-8")).hexdigest()[:10]
        cid = f"house-view:{_slugify(title_clean)}:{digest}"

    now = utcnow()
    extra = _house_view_extra(
        subject=subject,
        consensus_view=consensus_view,
        variant_view=variant_view,
        model_drivers=model_drivers,
        technology_assumptions=technology_assumptions,
        invalidation_triggers=invalidation_triggers,
        open_questions=open_questions,
        confidence=confidence,
        time_horizon=time_horizon,
        review_cadence_days=review_cadence_days,
        question_cooldown_days=int(question_cooldown_days or review_cadence_days or 90),
        idea_origin=idea_origin,
        authoring_agent=authoring_agent,
        audit=audit,
    )
    existing = session.query(Thesis).filter_by(client_request_id=cid).first()
    tokens = _subject_entity_tokens(session, subject, entity_tokens)
    if existing is None:
        existing = create_thesis(
            session,
            title=title_clean,
            statement=statement_clean,
            kind="thesis",
            status=status,
            importance=max(1, min(int(importance or 4), 10)),
            stale_after_days=stale_after_days,
            entity_tokens=tokens,
            tag_slugs=["house-view"],
            client_request_id=cid,
            extra=extra,
        )
    else:
        merged = _house_view_extra(
            subject=subject,
            consensus_view=consensus_view,
            variant_view=variant_view,
            model_drivers=model_drivers,
            technology_assumptions=technology_assumptions,
            invalidation_triggers=invalidation_triggers,
            open_questions=open_questions,
            confidence=confidence,
            time_horizon=time_horizon,
            review_cadence_days=review_cadence_days,
            question_cooldown_days=int(question_cooldown_days or review_cadence_days or 90),
            idea_origin=idea_origin,
            authoring_agent=authoring_agent,
            audit=audit,
            existing=dict(existing.extra or {}),
        )
        existing.title = title_clean
        existing.statement = statement_clean
        existing.kind = "thesis"
        existing.status = status or existing.status
        existing.importance = max(1, min(int(importance or existing.importance or 4), 10))
        existing.stale_after_days = stale_after_days
        existing.extra = merged
        existing.entities = resolve_entities(session, tokens)
        existing.updated_at = now
    existing.last_reviewed_at = now
    session.flush()
    return existing


def _is_house_view(thesis: Thesis) -> bool:
    extra = dict(thesis.extra or {})
    house = extra.get(HOUSE_VIEW_EXTRA_KEY)
    provenance = extra.get("provenance")
    return isinstance(house, dict) and isinstance(provenance, dict) and bool(provenance.get("self_authored_idea"))


def serialize_house_view_thesis(thesis: Thesis) -> dict[str, Any]:
    extra = dict(thesis.extra or {})
    house = dict(extra.get(HOUSE_VIEW_EXTRA_KEY) or {})
    provenance = dict(extra.get("provenance") or {})
    return {
        "id": int(thesis.id),
        "client_request_id": thesis.client_request_id,
        "title": thesis.title,
        "statement": thesis.statement,
        "kind": thesis.kind,
        "status": thesis.status,
        "importance": int(thesis.importance or 0),
        "stale_after_days": int(thesis.stale_after_days) if thesis.stale_after_days is not None else None,
        "consensus_view": house.get("consensus_view") or None,
        "variant_view": house.get("variant_view") or None,
        "model_drivers": list(house.get("model_drivers") or []),
        "technology_assumptions": list(house.get("technology_assumptions") or []),
        "invalidation_triggers": list(house.get("invalidation_triggers") or []),
        "open_questions": list(house.get("open_questions") or []),
        "confidence": house.get("confidence"),
        "time_horizon": house.get("time_horizon") or None,
        "mode_scope": house.get("mode_scope") or "buy_side",
        "subject_ids": list(house.get("subject_ids") or []),
        "issuer_ids": list(house.get("issuer_ids") or []),
        "company_ids": list(house.get("company_ids") or []),
        "self_authored_idea": bool(provenance.get("self_authored_idea")),
        "idea_origin": provenance.get("idea_origin"),
        "last_user_reviewed_at": house.get("last_user_reviewed_at") or _dt(thesis.last_reviewed_at),
        "next_review_due_at": house.get("next_review_due_at"),
        "review_cadence_days": house.get("review_cadence_days"),
        "question_cooldown_days": house.get("question_cooldown_days"),
        "updated_at": _dt(thesis.updated_at),
    }


def _subject_terms(session: Session, subject: ResearchSubject) -> set[str]:
    terms: set[str] = set()
    for value in (subject.display_name, subject.legal_name, subject.slug, subject.subject_key):
        if value:
            terms.add(_norm(value))
    for alias in list(subject.aliases or []):
        if alias.alias:
            terms.add(_norm(alias.alias))
    if subject.company_id is not None:
        company = session.get(Company, int(subject.company_id))
        if company is not None:
            for value in (company.name, company.slug, company.ticker, company.exchange):
                if value:
                    terms.add(_norm(value))
    if subject.issuer_id is not None:
        issuer = session.get(Issuer, int(subject.issuer_id))
        if issuer is not None:
            for value in (issuer.display_name, issuer.legal_name, issuer.slug):
                if value:
                    terms.add(_norm(value))
    return {term for term in terms if term}


def _term_match(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True
    if len(left) >= 5 and len(right) >= 5 and (left in right or right in left):
        return True
    return False


def _house_view_matches_subject(session: Session, thesis: Thesis, subject: ResearchSubject) -> bool:
    extra = dict(thesis.extra or {})
    house = dict(extra.get(HOUSE_VIEW_EXTRA_KEY) or {})
    if int(subject.id) in _int_set(house.get("subject_ids")):
        return True
    if subject.issuer_id is not None and int(subject.issuer_id) in _int_set(house.get("issuer_ids")):
        return True
    if subject.company_id is not None and int(subject.company_id) in _int_set(house.get("company_ids")):
        return True
    terms = _subject_terms(session, subject)
    entity_terms = {_norm(entity.canonical_name) for entity in list(thesis.entities or []) if entity.canonical_name}
    if any(_term_match(a, b) for a in terms for b in entity_terms):
        return True
    haystack = _norm(f"{thesis.title} {thesis.statement}")
    return any(len(term) >= 5 and term in haystack for term in terms)


def list_house_view_theses(
    session: Session,
    *,
    subject_id: int | None = None,
    include_inactive: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    subject = session.get(ResearchSubject, int(subject_id)) if subject_id is not None else None
    q = session.query(Thesis).order_by(Thesis.importance.desc(), Thesis.updated_at.desc(), Thesis.id.desc())
    if not include_inactive:
        q = q.filter(Thesis.status.in_(sorted(HOUSE_VIEW_ACTIVE_STATUSES)))
    out: list[dict[str, Any]] = []
    for row in q.limit(max(1, min(int(limit or 50) * 5, 1000))).all():
        if not _is_house_view(row):
            continue
        if subject is not None and not _house_view_matches_subject(session, row, subject):
            continue
        out.append(serialize_house_view_thesis(row))
        if len(out) >= max(1, min(int(limit or 50), 200)):
            break
    return out


def _compact_analyst_evidence(row: AnalystEvidence) -> dict[str, Any]:
    payload = dict(row.payload or {})
    return {
        "id": int(row.id),
        "evidence_kind": row.evidence_kind,
        "source_name": row.source_name,
        "outlet": row.outlet,
        "published_at": _dt(row.published_at),
        "action": row.action,
        "rating": row.rating,
        "price_target": row.price_target,
        "prior_price_target": row.prior_price_target,
        "currency": row.currency,
        "estimate_metric": row.estimate_metric,
        "estimate_period": row.estimate_period,
        "estimate_value": row.estimate_value,
        "estimate_unit": row.estimate_unit,
        "evidence_tier": row.evidence_tier,
        "source_kind": row.source_kind,
        "trust_label": row.trust_label,
        "confidence": row.confidence,
        "source_attribution": payload.get("source_attribution") or {},
        "forecast": payload.get("forecast") or {},
        "summary": _clean(row.summary)[:420] if row.summary else None,
    }


def _recent_analyst_evidence(session: Session, subject: ResearchSubject, *, limit: int) -> list[dict[str, Any]]:
    filters = [AnalystEvidence.subject_id == int(subject.id)]
    if subject.company_id is not None:
        filters.append(AnalystEvidence.company_id == int(subject.company_id))
    if subject.issuer_id is not None:
        filters.append(AnalystEvidence.issuer_id == int(subject.issuer_id))
    rows = (
        session.query(AnalystEvidence)
        .filter(AnalystEvidence.status != "archived")
        .filter(or_(*filters))
        .order_by(AnalystEvidence.published_at.desc().nullslast(), AnalystEvidence.updated_at.desc(), AnalystEvidence.id.desc())
        .limit(max(1, min(int(limit or 8), 50)))
        .all()
    )
    return [_compact_analyst_evidence(row) for row in rows]


def _recent_local_agent_routes(session: Session, subject: ResearchSubject, *, limit: int) -> list[dict[str, Any]]:
    terms = [term for term in _subject_terms(session, subject) if len(term) >= 3]
    q = session.query(LocalAgentEntityRoute)
    route_filters = []
    if subject.company_id is not None:
        route_filters.append(
            (LocalAgentEntityRoute.destination_type == "company") & (LocalAgentEntityRoute.destination_id == int(subject.company_id))
        )
    route_filters.append(
        (LocalAgentEntityRoute.destination_type == "research_subject") & (LocalAgentEntityRoute.destination_id == int(subject.id))
    )
    for term in sorted(terms)[:10]:
        route_filters.append(LocalAgentEntityRoute.entity_name.ilike(f"%{term}%"))
    if route_filters:
        q = q.filter(or_(*route_filters))
    rows = (
        q.order_by(LocalAgentEntityRoute.created_at.desc(), LocalAgentEntityRoute.id.desc())
        .limit(max(1, min(int(limit or 8), 50)))
        .all()
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "id": int(row.id),
                "lane": row.lane,
                "entity_name": row.entity_name,
                "entity_type": row.entity_type,
                "summary_relevance": bool(row.summary_relevance),
                "durable_relevance": bool(row.durable_relevance),
                "durable_action": row.durable_action,
                "materiality": row.materiality,
                "novelty": row.novelty,
                "evidence_tier": row.evidence_tier,
                "new_information": _clean(row.new_information)[:420] if row.new_information else None,
                "rationale": _clean(row.rationale)[:320] if row.rationale else None,
                "created_at": _dt(row.created_at),
            }
        )
    return out


def _consensus_snapshot(analyst_evidence: list[dict[str, Any]], local_agent_routes: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for item in analyst_evidence[:4]:
        source = item.get("source_name") or item.get("outlet") or "analyst evidence"
        action = item.get("action") or item.get("rating")
        summary = item.get("summary")
        if summary:
            lines.append(f"{source}: {summary}")
        elif action:
            lines.append(f"{source}: {action}")
    for item in local_agent_routes[:2]:
        info = item.get("new_information") or item.get("rationale")
        if info:
            lines.append(f"local-agent route: {info}")
    if not lines:
        return "No explicit sell-side/agent consensus evidence is currently attached in Thoth."
    return " ".join(lines)[:900]


def house_view_context_for_subject(session: Session, *, subject_id: int, limit: int = 6) -> dict[str, Any]:
    subject = session.get(ResearchSubject, int(subject_id))
    if subject is None:
        return {}
    rows = list_house_view_theses(session, subject_id=int(subject.id), limit=limit)
    return {
        "subject_id": int(subject.id),
        "subject_key": subject.subject_key,
        "display_name": subject.display_name,
        "authority": "user_self_authored_house_view",
        "count": len(rows),
        "house_views": rows,
        "usage": {
            "buy_side": "Use these as house priors only when the requested mode is buy-side or house-view-aware.",
            "sell_side": "Do not use these to represent consensus; use analyst evidence and source rails instead.",
        },
    }


def build_research_mode_context(
    session: Session,
    *,
    subject_id: int,
    mode: str = "both",
    limit: int = 6,
) -> dict[str, Any]:
    subject = session.get(ResearchSubject, int(subject_id))
    if subject is None:
        return {}
    mode_norm = _clean(mode).lower() or "both"
    analyst_evidence = _recent_analyst_evidence(session, subject, limit=limit)
    local_routes = _recent_local_agent_routes(session, subject, limit=limit)
    sell_side = {
        "authority": "consensus_or_external_evidence",
        "consensus_snapshot": _consensus_snapshot(analyst_evidence, local_routes),
        "analyst_evidence": analyst_evidence,
        "local_agent_signals": local_routes,
        "usage": "Use for sell-side/consensus mode and as the contrast set for buy-side variant views.",
    }
    buy_side = house_view_context_for_subject(session, subject_id=int(subject.id), limit=limit)
    payload = {
        "subject": {
            "id": int(subject.id),
            "subject_key": subject.subject_key,
            "display_name": subject.display_name,
            "kind": subject.kind,
        },
        "mode": mode_norm,
    }
    if mode_norm in {"buy_side", "buy-side", "house_view", "house-view", "both"}:
        payload["buy_side"] = buy_side
    if mode_norm in {"sell_side", "sell-side", "consensus", "both"}:
        payload["sell_side"] = sell_side
    return payload


def _subject_for_instrument(session: Session, instrument: ResearchInstrument) -> ResearchSubject | None:
    filters = []
    if instrument.company_id is not None:
        filters.append(ResearchSubject.company_id == int(instrument.company_id))
    if instrument.issuer_id is not None:
        filters.append(ResearchSubject.issuer_id == int(instrument.issuer_id))
    if filters:
        subject = session.query(ResearchSubject).filter(or_(*filters)).order_by(ResearchSubject.id.asc()).first()
        if subject is not None:
            return subject
    return None


def _subjects_from_universe(session: Session, universe_slug: str, *, limit: int) -> list[ResearchSubject]:
    universe = session.query(ResearchUniverse).filter_by(slug=universe_slug).first()
    if universe is None:
        return []
    rows = (
        session.query(ResearchUniverseRow)
        .filter(ResearchUniverseRow.universe_id == int(universe.id), ResearchUniverseRow.archived_at.is_(None))
        .order_by(ResearchUniverseRow.sort_order.asc(), ResearchUniverseRow.id.asc())
        .limit(max(1, min(int(limit or 50) * 4, 1000)))
        .all()
    )
    out: list[ResearchSubject] = []
    seen: set[int] = set()
    for row in rows:
        if row.instrument is None:
            continue
        subject = _subject_for_instrument(session, row.instrument)
        if subject is None or int(subject.id) in seen:
            continue
        out.append(subject)
        seen.add(int(subject.id))
        if len(out) >= max(1, min(int(limit or 50), 200)):
            break
    return out


def _question_request_key(*, subject: ResearchSubject | None, theme: str | None, question: str) -> str:
    scope = f"subject:{int(subject.id)}" if subject is not None else f"theme:{_slugify(theme or 'general')}"
    digest = hashlib.sha1(question.encode("utf-8")).hexdigest()[:12]
    return f"house-view-question:{scope}:{digest}"


def _request_recent(row: InstrumentRequest | None, *, cadence_days: int) -> bool:
    if row is None:
        return False
    basis = row.reviewed_at or row.updated_at or row.created_at
    if basis is None:
        return False
    return (utcnow() - basis).days < max(1, int(cadence_days or 90))


def _question_for_subject(
    session: Session,
    subject: ResearchSubject,
    *,
    cadence_days: int,
    limit: int,
) -> dict[str, Any]:
    mode_context = build_research_mode_context(session, subject_id=int(subject.id), mode="both", limit=limit)
    buy = dict(mode_context.get("buy_side") or {})
    sell = dict(mode_context.get("sell_side") or {})
    views = list(buy.get("house_views") or [])
    consensus = str(sell.get("consensus_snapshot") or "").strip()
    if views:
        stale_views = []
        now = utcnow()
        for view in views:
            due_at = _parse_iso_dt(view.get("next_review_due_at"))
            if due_at is not None and due_at <= now:
                stale_views.append(view)
        anchor = stale_views[0] if stale_views else views[0]
        question = (
            f"Has your house view changed on {subject.display_name}: {anchor.get('title')}? "
            f"Consensus/agent context: {consensus}"
        )
        reason = "review_existing_house_view" if stale_views else "refresh_if_discussed"
    else:
        question = (
            f"What is your house view on {subject.display_name} versus current consensus? "
            f"Consensus/agent context: {consensus}"
        )
        reason = "missing_house_view"
    return {
        "subject_id": int(subject.id),
        "subject_key": subject.subject_key,
        "display_name": subject.display_name,
        "question": question[:1600],
        "reason": reason,
        "consensus_view": consensus,
        "existing_house_view_count": len(views),
        "cadence_days": max(1, int(cadence_days or 90)),
    }


def generate_house_view_questions(
    session: Session,
    *,
    subject_id: int | None = None,
    universe_slug: str | None = None,
    theme: str | None = None,
    limit: int = 10,
    cadence_days: int = 90,
    create_requests: bool = False,
) -> dict[str, Any]:
    subjects: list[ResearchSubject] = []
    if subject_id is not None:
        subject = session.get(ResearchSubject, int(subject_id))
        if subject is None:
            raise ValueError("Research subject not found.")
        subjects = [subject]
    elif universe_slug:
        subjects = _subjects_from_universe(session, universe_slug, limit=limit)

    questions: list[dict[str, Any]] = []
    created_requests: list[dict[str, Any]] = []
    existing_requests: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    if subjects:
        for subject in subjects[: max(1, min(int(limit or 10), 100))]:
            question = _question_for_subject(session, subject, cadence_days=cadence_days, limit=8)
            key = _question_request_key(subject=subject, theme=None, question=question["question"])
            question["request_key"] = key
            existing = session.query(InstrumentRequest).filter_by(request_key=key).first()
            if _request_recent(existing, cadence_days=cadence_days):
                skipped.append({**question, "skip_reason": "cooldown_active", "request_id": int(existing.id) if existing else None})
                continue
            if create_requests:
                row = create_instrument_request(
                    session,
                    request_key=key,
                    title=f"House-view question: {subject.display_name}",
                    intent=HOUSE_VIEW_REQUEST_INTENT,
                    summary=question["question"],
                    target_entities=[subject.display_name],
                    target_themes=[theme] if theme else ["house_view"],
                    desired_outputs=[
                        "user-confirmed house view or explicit no-view",
                        "consensus view acknowledged",
                        "variant view, model drivers, invalidation triggers, and review cadence",
                    ],
                    evidence_bundle=[
                        {
                            "kind": "consensus_snapshot",
                            "summary": question.get("consensus_view"),
                            "source": "analyst_evidence_and_local_agent_routes",
                        }
                    ],
                    provenance_summary="Generated from Thoth sell-side/agent context to ask for a user-authored buy-side house view.",
                    evidence_tier="internal_context",
                    status="draft",
                    priority=7 if question["reason"] == "missing_house_view" else 5,
                    automation_source="house_view",
                    source_kind=HOUSE_VIEW_REQUEST_SOURCE_KIND,
                    draft_payload=question,
                    linked_objects={"subject_id": int(subject.id), "subject_key": subject.subject_key},
                )
                if existing is None:
                    created_requests.append(serialize_instrument_request(row))
                else:
                    existing_requests.append(serialize_instrument_request(row))
                question["request_id"] = int(row.id)
            questions.append(question)
    else:
        topic = _clean(theme) or "general market model"
        consensus = f"No scoped subject was supplied. Ask for the user's house view on {topic} and capture consensus before variant view."
        question_text = f"What is your house view on {topic} versus current consensus? Consensus/agent context: {consensus}"
        key = _question_request_key(subject=None, theme=topic, question=question_text)
        question = {
            "theme": topic,
            "question": question_text,
            "reason": "theme_house_view_seed",
            "consensus_view": consensus,
            "cadence_days": max(1, int(cadence_days or 90)),
            "request_key": key,
        }
        existing = session.query(InstrumentRequest).filter_by(request_key=key).first()
        if _request_recent(existing, cadence_days=cadence_days):
            skipped.append({**question, "skip_reason": "cooldown_active", "request_id": int(existing.id) if existing else None})
        else:
            if create_requests:
                row = create_instrument_request(
                    session,
                    request_key=key,
                    title=f"House-view question: {topic}",
                    intent=HOUSE_VIEW_REQUEST_INTENT,
                    summary=question_text,
                    target_entities=[],
                    target_themes=[topic, "house_view"],
                    desired_outputs=[
                        "user-confirmed house view or explicit no-view",
                        "consensus view acknowledged",
                        "variant view, model drivers, invalidation triggers, and review cadence",
                    ],
                    evidence_bundle=[{"kind": "operator_theme", "summary": consensus}],
                    provenance_summary="Generated from a requested house-view theme.",
                    evidence_tier="internal_context",
                    status="draft",
                    priority=6,
                    automation_source="house_view",
                    source_kind=HOUSE_VIEW_REQUEST_SOURCE_KIND,
                    draft_payload=question,
                    linked_objects={"theme": topic},
                )
                if existing is None:
                    created_requests.append(serialize_instrument_request(row))
                else:
                    existing_requests.append(serialize_instrument_request(row))
                question["request_id"] = int(row.id)
            questions.append(question)

    return {
        "questions": questions,
        "created_requests": created_requests,
        "existing_requests": existing_requests,
        "skipped": skipped,
        "summary": {
            "question_count": len(questions),
            "created_request_count": len(created_requests),
            "updated_request_count": len(existing_requests),
            "skipped_count": len(skipped),
            "cadence_days": max(1, int(cadence_days or 90)),
            "created": bool(create_requests),
        },
    }
