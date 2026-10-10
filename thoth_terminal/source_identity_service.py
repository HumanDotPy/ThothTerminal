from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from thoth_terminal.db import utcnow
from thoth_terminal.models import (
    AnalystEvidence,
    Document,
    SourceActor,
    SourceActorAlias,
    SourceAffiliation,
    SourceAttributionEdge,
)


_ACTOR_TYPES = {
    "account",
    "data_vendor",
    "firm",
    "issuer",
    "person",
    "publication",
    "research_org",
    "unknown",
}
_SIDES = {
    "buy_side",
    "independent",
    "issuer",
    "media",
    "sell_side",
    "specialist_research",
    "unknown",
}
_ROLE_CLASSES = {
    "analyst",
    "journalist",
    "portfolio_manager",
    "research_analyst",
    "research_org",
    "strategist",
    "unknown",
}


def _json_dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _json_list(value: object) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _text(value: object, *, limit: int | None = None) -> str | None:
    cleaned = " ".join(str(value or "").strip().split())
    if not cleaned:
        return None
    return cleaned[:limit] if limit is not None else cleaned


def _float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _slug(value: object, *, default: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return cleaned or default


def normalize_source_alias(value: object, *, alias_kind: str | None = None) -> str:
    text = " ".join(str(value or "").strip().split())
    if alias_kind == "x_handle":
        text = text.lstrip("@")
    return text.lower()


def normalize_actor_type(value: object) -> str:
    normalized = _slug(value, default="unknown").replace("-", "_")
    return normalized if normalized in _ACTOR_TYPES else "unknown"


def normalize_side(value: object) -> str:
    normalized = _slug(value, default="unknown").replace("-", "_")
    return normalized if normalized in _SIDES else "unknown"


def normalize_role_class(value: object, *, side: str | None = None) -> str:
    normalized = _slug(value, default="unknown").replace("-", "_")
    if normalized in {"researcher", "research"}:
        normalized = "research_analyst"
    if normalized == "unknown" and side in {"sell_side", "specialist_research"}:
        normalized = "analyst"
    return normalized if normalized in _ROLE_CLASSES else "unknown"


def _parse_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _actor_key(*, display_name: str, actor_type: str, handle: str | None = None, firm: str | None = None) -> str:
    if handle:
        return f"x-handle:{normalize_source_alias(handle, alias_kind='x_handle')}"
    if firm:
        return f"firm:{_slug(firm, default='unknown')}"
    return f"{actor_type}:{_slug(display_name, default='unknown')}"


def serialize_source_actor(actor: SourceActor | None) -> dict[str, Any] | None:
    if actor is None:
        return None
    return {
        "id": int(actor.id),
        "actor_key": actor.actor_key,
        "display_name": actor.display_name,
        "actor_type": actor.actor_type,
        "side": actor.side,
        "status": actor.status,
        "trust_label": actor.trust_label,
    }


def serialize_source_affiliation(row: SourceAffiliation | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": int(row.id),
        "person_actor_id": int(row.person_actor_id),
        "organization_actor_id": int(row.organization_actor_id),
        "organization_name": row.organization_actor.display_name if row.organization_actor else None,
        "role_label": row.role_label,
        "role_class": row.role_class,
        "side": row.side,
        "status": row.status,
        "start_date": row.start_date.isoformat() if row.start_date else None,
        "end_date": row.end_date.isoformat() if row.end_date else None,
        "confidence": row.confidence,
        "source_document_id": int(row.source_document_id) if row.source_document_id is not None else None,
        "source_url": row.source_url,
        "evidence_note": row.evidence_note,
    }


def ensure_source_actor(
    session: Session,
    *,
    display_name: str,
    actor_type: str = "unknown",
    actor_key: str | None = None,
    side: str | None = None,
    trust_label: str | None = None,
    notes: str | None = None,
    payload: dict[str, Any] | None = None,
    actor: str = "system",
) -> SourceActor:
    now = utcnow()
    actor_type_norm = normalize_actor_type(actor_type)
    side_norm = normalize_side(side) if side else None
    key = _text(actor_key, limit=255) or _actor_key(display_name=display_name, actor_type=actor_type_norm)
    row = session.query(SourceActor).filter_by(actor_key=key).first()
    if row is None:
        row = SourceActor(
            actor_key=key,
            display_name=_text(display_name, limit=255) or key,
            actor_type=actor_type_norm,
            side=side_norm,
            status="active",
            trust_label=_text(trust_label, limit=64),
            notes=notes,
            payload=dict(payload or {}),
            created_by=actor[:64],
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.flush()
        return row
    if row.actor_type == "unknown" and actor_type_norm != "unknown":
        row.actor_type = actor_type_norm
    if side_norm and not row.side:
        row.side = side_norm
    if trust_label and not row.trust_label:
        row.trust_label = _text(trust_label, limit=64)
    if notes and not row.notes:
        row.notes = notes
    if payload:
        merged = dict(row.payload or {})
        merged.update(payload)
        row.payload = merged
    row.updated_at = now
    session.flush()
    return row


def ensure_source_actor_alias(
    session: Session,
    actor: SourceActor,
    *,
    alias: str,
    alias_kind: str = "name",
    platform: str | None = None,
    source_document_id: int | None = None,
    confidence: float | None = None,
    payload: dict[str, Any] | None = None,
) -> SourceActorAlias:
    alias_text = _text(alias, limit=255)
    if not alias_text:
        raise ValueError("source actor alias is required")
    alias_kind_norm = _slug(alias_kind, default="name").replace("-", "_")[:64]
    platform_norm = _slug(platform, default="")[:64] or None
    normalized_alias = normalize_source_alias(alias_text, alias_kind=alias_kind_norm)[:255]
    q = session.query(SourceActorAlias).filter(
        SourceActorAlias.alias_kind == alias_kind_norm,
        SourceActorAlias.normalized_alias == normalized_alias,
    )
    if platform_norm is None:
        q = q.filter(SourceActorAlias.platform.is_(None))
    else:
        q = q.filter(SourceActorAlias.platform == platform_norm)
    row = q.first()
    now = utcnow()
    if row is None:
        row = SourceActorAlias(
            actor_id=int(actor.id),
            alias=alias_text,
            normalized_alias=normalized_alias,
            alias_kind=alias_kind_norm,
            platform=platform_norm,
            source_document_id=source_document_id,
            confidence=confidence,
            payload=dict(payload or {}),
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    else:
        if row.actor_id != int(actor.id):
            row.payload = {**dict(row.payload or {}), "conflicting_actor_id": int(actor.id)}
        if source_document_id is not None and row.source_document_id is None:
            row.source_document_id = source_document_id
        if confidence is not None:
            row.confidence = confidence
        if payload:
            row.payload = {**dict(row.payload or {}), **payload}
        row.updated_at = now
    session.flush()
    return row


def find_source_actor_by_alias(
    session: Session,
    *,
    alias: str,
    alias_kind: str = "name",
    platform: str | None = None,
) -> SourceActor | None:
    normalized = normalize_source_alias(alias, alias_kind=alias_kind)[:255]
    alias_kind_norm = _slug(alias_kind, default="name").replace("-", "_")[:64]
    platform_norm = _slug(platform, default="")[:64] or None
    q = session.query(SourceActorAlias).filter(
        SourceActorAlias.alias_kind == alias_kind_norm,
        SourceActorAlias.normalized_alias == normalized,
    )
    if platform_norm is None:
        q = q.filter(SourceActorAlias.platform.is_(None))
    else:
        q = q.filter(SourceActorAlias.platform == platform_norm)
    alias_row = q.first()
    return session.get(SourceActor, int(alias_row.actor_id)) if alias_row is not None else None


def resolve_source_actor(
    session: Session,
    *,
    name: str | None = None,
    handle: str | None = None,
    firm: str | None = None,
    actor_type: str = "unknown",
    source_document_id: int | None = None,
    confidence: float | None = None,
    actor: str = "system",
) -> SourceActor | None:
    handle_text = _text(handle, limit=64)
    name_text = _text(name, limit=255)
    firm_text = _text(firm, limit=255)
    if handle_text:
        found = find_source_actor_by_alias(session, alias=handle_text, alias_kind="x_handle", platform="x")
        if found is not None:
            return found
    for alias_value, alias_kind in ((firm_text, "firm_name"), (name_text, "name")):
        if alias_value:
            found = find_source_actor_by_alias(session, alias=alias_value, alias_kind=alias_kind)
            if found is not None:
                if handle_text:
                    ensure_source_actor_alias(
                        session,
                        found,
                        alias=handle_text,
                        alias_kind="x_handle",
                        platform="x",
                        source_document_id=source_document_id,
                        confidence=confidence,
                    )
                return found
    actor_type_norm = normalize_actor_type(actor_type)
    if firm_text and actor_type_norm == "unknown":
        actor_type_norm = "firm"
    display_name = firm_text if firm_text and actor_type_norm in {"firm", "research_org"} else name_text or firm_text or (f"@{handle_text.lstrip('@')}" if handle_text else None)
    if not display_name:
        return None
    row = ensure_source_actor(
        session,
        display_name=display_name,
        actor_type=actor_type_norm,
        actor_key=_actor_key(display_name=display_name, actor_type=actor_type_norm, handle=handle_text, firm=firm_text),
        actor=actor,
    )
    if handle_text:
        ensure_source_actor_alias(
            session,
            row,
            alias=handle_text,
            alias_kind="x_handle",
            platform="x",
            source_document_id=source_document_id,
            confidence=confidence,
        )
    if name_text:
        ensure_source_actor_alias(
            session,
            row,
            alias=name_text,
            alias_kind="name",
            source_document_id=source_document_id,
            confidence=confidence,
        )
    if firm_text:
        ensure_source_actor_alias(
            session,
            row,
            alias=firm_text,
            alias_kind="firm_name",
            source_document_id=source_document_id,
            confidence=confidence,
        )
    return row


def upsert_source_affiliation(
    session: Session,
    *,
    person_actor: SourceActor,
    organization_actor: SourceActor,
    role_label: str | None = None,
    role_class: str | None = None,
    side: str | None = None,
    status: str = "active",
    start_date: date | str | None = None,
    end_date: date | str | None = None,
    confidence: float | None = None,
    source_document_id: int | None = None,
    source_url: str | None = None,
    evidence_note: str | None = None,
    payload: dict[str, Any] | None = None,
    actor: str = "system",
) -> SourceAffiliation:
    side_norm = normalize_side(side)
    role_class_norm = normalize_role_class(role_class, side=side_norm)
    start = _parse_date(start_date)
    end = _parse_date(end_date)
    key_src = "|".join(
        [
            str(person_actor.id),
            str(organization_actor.id),
            role_class_norm,
            side_norm,
            start.isoformat() if start else "",
            end.isoformat() if end else "",
        ]
    )
    key = f"affiliation:{hashlib.sha1(key_src.encode('utf-8')).hexdigest()[:24]}"
    now = utcnow()
    row = session.query(SourceAffiliation).filter_by(affiliation_key=key).first()
    values = {
        "person_actor_id": int(person_actor.id),
        "organization_actor_id": int(organization_actor.id),
        "role_label": _text(role_label, limit=255),
        "role_class": role_class_norm,
        "side": side_norm,
        "status": _slug(status, default="active")[:32],
        "start_date": start,
        "end_date": end,
        "confidence": confidence,
        "source_document_id": source_document_id,
        "source_url": _text(source_url, limit=2048),
        "evidence_note": evidence_note,
        "payload": dict(payload or {}),
    }
    if row is None:
        row = SourceAffiliation(affiliation_key=key, created_by=actor[:64], created_at=now, updated_at=now, **values)
        session.add(row)
    else:
        for field, value in values.items():
            if value is not None or field in {"payload", "status", "role_class", "side"}:
                if field == "payload":
                    row.payload = {**dict(row.payload or {}), **dict(value or {})}
                else:
                    setattr(row, field, value)
        row.updated_at = now
    if side_norm != "unknown" and not person_actor.side:
        person_actor.side = side_norm
        person_actor.updated_at = now
    session.flush()
    return row


def active_source_affiliation(
    session: Session,
    *,
    actor_id: int | None,
    as_of: date | datetime | None = None,
) -> SourceAffiliation | None:
    if actor_id is None:
        return None
    as_of_date = as_of.date() if isinstance(as_of, datetime) else as_of
    q = session.query(SourceAffiliation).filter(
        SourceAffiliation.person_actor_id == int(actor_id),
        SourceAffiliation.status == "active",
    )
    if as_of_date is not None:
        q = q.filter(
            or_(SourceAffiliation.start_date.is_(None), SourceAffiliation.start_date <= as_of_date),
            or_(SourceAffiliation.end_date.is_(None), SourceAffiliation.end_date >= as_of_date),
        )
    return (
        q.order_by(
            SourceAffiliation.confidence.desc().nullslast(),
            SourceAffiliation.start_date.desc().nullslast(),
            SourceAffiliation.id.desc(),
        )
        .first()
    )


def _payload_affiliations(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    direct = payload.get("source_affiliation") or payload.get("affiliation")
    if isinstance(direct, dict):
        rows.append(dict(direct))
    rows.extend(dict(row) for row in _json_list(payload.get("source_affiliations") or payload.get("affiliations")) if isinstance(row, dict))
    return rows


def enrich_analyst_source_identity(
    session: Session,
    *,
    payload: dict[str, Any],
    source_attribution: dict[str, Any],
    source_document: Document | None = None,
    published_at: datetime | None = None,
    actor: str = "system",
) -> dict[str, Any]:
    attribution = dict(source_attribution or {})
    observed = _json_dict(attribution.get("observed_source"))
    underlying = _json_dict(attribution.get("underlying_source"))
    source_document_id = int(source_document.id) if source_document is not None and source_document.id is not None else None
    confidence = _float(payload.get("confidence"))

    observed_actor = resolve_source_actor(
        session,
        name=observed.get("name") or payload.get("observed_source_name") or payload.get("source_name") or payload.get("author"),
        handle=observed.get("handle") or payload.get("observed_source_handle"),
        actor_type=payload.get("observed_actor_type") or payload.get("observed_source_actor_type") or "unknown",
        source_document_id=source_document_id,
        confidence=confidence,
        actor=actor,
    )
    underlying_actor = resolve_source_actor(
        session,
        name=underlying.get("name") or payload.get("underlying_source_name"),
        firm=underlying.get("firm") or payload.get("underlying_firm") or payload.get("firm"),
        actor_type=payload.get("underlying_actor_type") or payload.get("underlying_source_actor_type") or "firm",
        source_document_id=source_document_id,
        confidence=confidence,
        actor=actor,
    )

    explicit_affiliations: list[SourceAffiliation] = []
    for affiliation_payload in _payload_affiliations(payload):
        person = observed_actor
        if affiliation_payload.get("person_actor_id"):
            person = session.get(SourceActor, int(affiliation_payload["person_actor_id"]))
        elif affiliation_payload.get("person_handle") or affiliation_payload.get("person_name"):
            person = resolve_source_actor(
                session,
                name=affiliation_payload.get("person_name"),
                handle=affiliation_payload.get("person_handle"),
                actor_type=affiliation_payload.get("person_actor_type") or "person",
                source_document_id=source_document_id,
                confidence=confidence,
                actor=actor,
            )
        organization = underlying_actor
        if affiliation_payload.get("organization_actor_id"):
            organization = session.get(SourceActor, int(affiliation_payload["organization_actor_id"]))
        elif affiliation_payload.get("organization_name") or affiliation_payload.get("firm"):
            organization = resolve_source_actor(
                session,
                name=affiliation_payload.get("organization_name") or affiliation_payload.get("firm"),
                firm=affiliation_payload.get("firm") or affiliation_payload.get("organization_name"),
                actor_type=affiliation_payload.get("organization_actor_type") or "firm",
                source_document_id=source_document_id,
                confidence=confidence,
                actor=actor,
            )
        if person is None or organization is None:
            continue
        explicit_affiliations.append(
            upsert_source_affiliation(
                session,
                person_actor=person,
                organization_actor=organization,
                role_label=affiliation_payload.get("role_label") or affiliation_payload.get("role"),
                role_class=affiliation_payload.get("role_class") or affiliation_payload.get("role_kind"),
                side=affiliation_payload.get("side") or affiliation_payload.get("source_side"),
                status=affiliation_payload.get("status") or "active",
                start_date=affiliation_payload.get("start_date"),
                end_date=affiliation_payload.get("end_date"),
                confidence=confidence if affiliation_payload.get("confidence") is None else _float(affiliation_payload.get("confidence")),
                source_document_id=source_document_id,
                source_url=affiliation_payload.get("source_url") or payload.get("source_url"),
                evidence_note=affiliation_payload.get("evidence_note") or affiliation_payload.get("provenance_note"),
                payload={"source": "analyst_evidence_payload", **{k: v for k, v in affiliation_payload.items() if k not in {"person_actor_id", "organization_actor_id"}}},
                actor=actor,
            )
        )

    active_affiliation = active_source_affiliation(
        session,
        actor_id=int(observed_actor.id) if observed_actor is not None else None,
        as_of=published_at,
    )
    if explicit_affiliations and active_affiliation is None:
        active_affiliation = explicit_affiliations[0]
    observed_actor_payload = serialize_source_actor(observed_actor)
    underlying_actor_payload = serialize_source_actor(underlying_actor)
    if observed_actor is not None:
        observed["actor_id"] = int(observed_actor.id)
        observed["actor_type"] = observed_actor.actor_type
    if underlying_actor is not None:
        underlying["actor_id"] = int(underlying_actor.id)
        underlying["actor_type"] = underlying_actor.actor_type
    chain = [dict(row) for row in _json_list(attribution.get("source_chain")) if isinstance(row, dict)]
    for edge in chain:
        role = str(edge.get("role") or "").strip().lower()
        if observed_actor is not None and role in {"observed_publisher", "observed_source", "wrapper", "reposter"}:
            edge.setdefault("actor_id", int(observed_actor.id))
            edge.setdefault("actor_type", observed_actor.actor_type)
        if underlying_actor is not None and role in {"underlying_research", "underlying_source", "visible_watermark", "source_line"}:
            edge.setdefault("actor_id", int(underlying_actor.id))
            edge.setdefault("actor_type", underlying_actor.actor_type)
    if active_affiliation is not None:
        identity_status = "actors_resolved_affiliated"
        source_side = active_affiliation.side
        source_role_class = active_affiliation.role_class
    elif observed_actor is not None or underlying_actor is not None:
        identity_status = "actors_resolved_unaffiliated"
        source_side = None
        source_role_class = None
    else:
        identity_status = "unresolved"
        source_side = None
        source_role_class = None
    attribution["observed_source"] = {k: v for k, v in observed.items() if v is not None}
    attribution["underlying_source"] = {k: v for k, v in underlying.items() if v is not None}
    if chain:
        attribution["source_chain"] = chain[:12]
    attribution["identity"] = {
        "observed_actor": observed_actor_payload,
        "underlying_actor": underlying_actor_payload,
        "active_affiliation": serialize_source_affiliation(active_affiliation),
        "source_identity_status": identity_status,
        "source_side": source_side,
        "source_role_class": source_role_class,
    }
    return {
        "source_attribution": attribution,
        "observed_source_actor_id": int(observed_actor.id) if observed_actor is not None else None,
        "underlying_source_actor_id": int(underlying_actor.id) if underlying_actor is not None else None,
        "source_identity_status": identity_status,
        "source_side": source_side,
        "source_role_class": source_role_class,
    }


def sync_source_attribution_edges(
    session: Session,
    *,
    evidence: AnalystEvidence,
    actor: str = "system",
) -> list[SourceAttributionEdge]:
    attribution = _json_dict((evidence.payload or {}).get("source_attribution"))
    chain = [dict(row) for row in _json_list(attribution.get("source_chain")) if isinstance(row, dict)]
    source_document_id = int(evidence.source_document_id) if evidence.source_document_id is not None else None
    session.query(SourceAttributionEdge).filter_by(analyst_evidence_id=int(evidence.id)).delete()
    rows: list[SourceAttributionEdge] = []
    now = utcnow()
    for idx, edge in enumerate(chain[:12]):
        role = _text(edge.get("role"), limit=64) or "source"
        raw_name = _text(edge.get("name") or edge.get("source_name"), limit=255)
        raw_handle = _text(edge.get("handle"), limit=64)
        raw_firm = _text(edge.get("firm"), limit=255)
        actor_row = None
        if role != "ingested_document" and (raw_name or raw_handle or raw_firm):
            actor_row = resolve_source_actor(
                session,
                name=raw_name,
                handle=raw_handle,
                firm=raw_firm,
                actor_type=edge.get("actor_type") or ("firm" if raw_firm and not raw_handle else "unknown"),
                source_document_id=source_document_id,
                confidence=_float(edge.get("confidence")),
                actor=actor,
            )
            if actor_row is not None:
                edge["actor_id"] = int(actor_row.id)
                edge["actor_type"] = actor_row.actor_type
        row = SourceAttributionEdge(
            analyst_evidence_id=int(evidence.id),
            source_document_id=source_document_id,
            edge_order=idx,
            role=role,
            actor_id=int(actor_row.id) if actor_row is not None else None,
            raw_name=raw_name,
            raw_handle=raw_handle,
            raw_firm=raw_firm,
            extraction_method=_text(edge.get("extraction_method") or edge.get("method"), limit=64),
            confidence=_float(edge.get("confidence")),
            payload={k: v for k, v in edge.items() if k not in {"role", "name", "source_name", "handle", "firm"}},
            created_at=now,
        )
        session.add(row)
        rows.append(row)
    session.flush()
    return rows


def list_source_actors(
    session: Session,
    *,
    query: str | None = None,
    actor_type: str | None = None,
    side: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    q = session.query(SourceActor).filter(SourceActor.status != "archived")
    if actor_type:
        q = q.filter(SourceActor.actor_type == normalize_actor_type(actor_type))
    if side:
        q = q.filter(SourceActor.side == normalize_side(side))
    if query:
        needle = f"%{str(query).strip()}%"
        q = q.filter(SourceActor.display_name.ilike(needle))
    rows = q.order_by(SourceActor.updated_at.desc(), SourceActor.id.desc()).limit(max(1, min(int(limit), 250))).all()
    return [serialize_source_actor(row) or {} for row in rows]
