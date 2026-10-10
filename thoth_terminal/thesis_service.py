from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from thoth_terminal.db import utcnow
from thoth_terminal.models import (
    Document,
    Domain,
    Entity,
    EventType,
    Observation,
    Proposition,
    Tag,
    Thesis,
    ThesisEvidence,
    ThesisProposition,
)


_PERIOD_RE = re.compile(r"^(?P<year>\d{4})(?:Q(?P<quarter>[1-4]))?(?P<flag>[AEF])?$", re.IGNORECASE)
_NUM_TOKEN_RE = re.compile(r"^([+-]?\d[\d,]*\.?\d*)([a-z]{1,2})?$", re.IGNORECASE)
_NUM_SUFFIXES = {
    "k": 1_000.0,
    "m": 1_000_000.0,
    "mn": 1_000_000.0,
    "mm": 1_000_000.0,
    "b": 1_000_000_000.0,
    "bn": 1_000_000_000.0,
    "t": 1_000_000_000_000.0,
}


@dataclass(frozen=True)
class TargetPeriod:
    year: int | None
    quarter: int | None
    granularity: str | None  # year | quarter
    estimate_flag: str | None  # A | E | F


def split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [p.strip() for p in value.split(",") if p.strip()]


def parse_target_period(value: str | None) -> TargetPeriod:
    if not value:
        return TargetPeriod(year=None, quarter=None, granularity=None, estimate_flag=None)
    raw = value.strip().upper()
    m = _PERIOD_RE.match(raw)
    if not m:
        return TargetPeriod(year=None, quarter=None, granularity=None, estimate_flag=None)
    year = int(m.group("year")) if m.group("year") else None
    quarter = int(m.group("quarter")) if m.group("quarter") else None
    flag = m.group("flag").upper() if m.group("flag") else None
    gran = "quarter" if quarter else "year"
    return TargetPeriod(year=year, quarter=quarter, granularity=gran, estimate_flag=flag)


def _parse_numeric_value(raw: str, *, allow_percent: bool = False) -> float | None:
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if not s:
        return None
    if s.endswith("%"):
        if not allow_percent:
            return None
        s = s[:-1].strip()
    m = _NUM_TOKEN_RE.match(s)
    if not m:
        return None
    num_part = (m.group(1) or "").replace(",", "")
    suffix = (m.group(2) or "").lower()
    try:
        base = float(num_part)
    except Exception:
        return None
    if suffix:
        mult = _NUM_SUFFIXES.get(suffix)
        if mult is None:
            return None
        base *= mult
    return base


def _looks_like_float(value: str) -> bool:
    return _parse_numeric_value(value, allow_percent=True) is not None


def _infer_value_type_from_unit(unit: str | None) -> str:
    if not unit:
        return "number"
    u = unit.strip()
    if "%" in u:
        return "percent"
    return "number"


def parse_key_inline_observation(key: str) -> tuple[str, str | None, str | None]:
    """
    Allows a convenience key format for assertions:
      subject:predicate:object:qualifier:target_period
    Example:
      samsung:supplies_share_of:nvidia_rubin_hbm4:0.30:2027E

    Returns (base_key, value, target_period).
    """
    raw = (key or "").strip()
    if not raw:
        return ("", None, None)
    if raw.startswith("manual:"):
        return (raw, None, None)

    parts = raw.split(":")
    if len(parts) >= 5 and _looks_like_float(parts[3]) and _PERIOD_RE.match(parts[4].strip().upper()):
        return (":".join(parts[:3]), parts[3].strip(), parts[4].strip())
    return (raw, None, None)


def resolve_entities(session: Session, tokens: list[str]) -> list[Entity]:
    out: list[Entity] = []
    for tok in [t.strip() for t in tokens if t.strip()]:
        entity: Entity | None = None
        if tok.isdigit():
            entity = session.query(Entity).filter_by(id=int(tok)).first()
        if not entity:
            entity = session.query(Entity).filter(Entity.canonical_name.ilike(tok)).first()
        if not entity:
            entity = Entity(canonical_name=tok, kind="other")
            session.add(entity)
            session.flush()
        if entity not in out:
            out.append(entity)
    return out


def _resolve_by_slug(session: Session, model: type[Domain] | type[Tag] | type[EventType], slugs: list[str]) -> list[Any]:
    if not slugs:
        return []
    rows = session.query(model).filter(model.slug.in_(slugs)).all()
    # Preserve input ordering where possible.
    by_slug = {r.slug: r for r in rows}
    return [by_slug[s] for s in slugs if s in by_slug]


def resolve_domains(session: Session, slugs: list[str]) -> list[Domain]:
    return _resolve_by_slug(session, Domain, slugs)


def resolve_tags(session: Session, slugs: list[str]) -> list[Tag]:
    return _resolve_by_slug(session, Tag, slugs)


def resolve_event_types(session: Session, slugs: list[str]) -> list[EventType]:
    return _resolve_by_slug(session, EventType, slugs)


def create_thesis(
    session: Session,
    *,
    title: str,
    statement: str,
    kind: str = "claim",
    status: str = "watching",
    importance: int = 3,
    stale_after_days: int | None = 90,
    entity_tokens: list[str],
    domain_slugs: list[str] | None = None,
    tag_slugs: list[str] | None = None,
    event_type_slugs: list[str] | None = None,
    client_request_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> Thesis:
    if client_request_id:
        existing = session.query(Thesis).filter_by(client_request_id=client_request_id).first()
        if existing:
            return existing

    now = utcnow()
    thesis = Thesis(
        client_request_id=client_request_id,
        title=title.strip(),
        statement=statement.strip(),
        kind=kind.strip() or "claim",
        status=status.strip() or "watching",
        importance=int(importance),
        stale_after_days=int(stale_after_days) if stale_after_days is not None else None,
        created_at=now,
        updated_at=now,
        last_reviewed_at=None,
        extra=extra or {},
    )
    session.add(thesis)
    session.flush()

    entities = resolve_entities(session, entity_tokens)
    if not entities:
        raise ValueError("At least one entity is required.")
    thesis.entities = entities

    if domain_slugs:
        thesis.domains = resolve_domains(session, domain_slugs)[:2]
    if tag_slugs:
        thesis.tags = resolve_tags(session, tag_slugs)
    if event_type_slugs:
        thesis.event_types = resolve_event_types(session, event_type_slugs)

    return thesis


def add_thesis_evidence(
    session: Session,
    *,
    thesis: Thesis,
    document_id: int,
    polarity: str = "support",
    snippet: str | None = None,
    note: str | None = None,
    weight_override: float | None = None,
    extra: dict[str, Any] | None = None,
) -> ThesisEvidence | None:
    doc = session.query(Document).filter_by(id=int(document_id)).first()
    if not doc:
        raise ValueError("Document not found.")

    existing = (
        session.query(ThesisEvidence)
        .filter_by(thesis_id=thesis.id, document_id=int(document_id))
        .first()
    )
    if existing:
        return None

    now = utcnow()
    ev = ThesisEvidence(
        thesis_id=thesis.id,
        document_id=int(document_id),
        polarity=(polarity or "support").strip(),
        snippet=snippet.strip() if snippet else None,
        note=note.strip() if note else None,
        weight_override=weight_override,
        created_at=now,
        extra=extra or {},
    )
    session.add(ev)
    thesis.updated_at = now
    from thoth_terminal.triage_service import mark_actioned

    mark_actioned(
        session,
        document_id=doc.id,
        meta={"source": "auto:thesis_evidence", "thesis_id": thesis.id},
    )
    return ev


def infer_proposition_from_key(session: Session, key: str) -> dict[str, Any]:
    raw = (key or "").strip()
    if not raw:
        return {"key": "", "kind": "manual", "unit": None, "value_type": "text", "period_kind": None, "rollup_rule": None}

    # Prefer existing registry entry (exact match).
    existing = session.query(Proposition).filter_by(key=raw).first()
    if existing:
        return {
            "key": existing.key,
            "kind": existing.kind,
            "unit": existing.unit,
            "value_type": existing.value_type,
            "period_kind": existing.period_kind,
            "rollup_rule": existing.rollup_rule,
            "stale_after_days_default": existing.stale_after_days_default,
        }

    if raw.startswith("manual:series:"):
        parts = raw.split(":")
        unit = parts[-1] if len(parts) >= 4 else None
        return {
            "key": raw,
            "kind": "series",
            "unit": unit,
            "value_type": _infer_value_type_from_unit(unit),
            "period_kind": None,
            "rollup_rule": None,
            "stale_after_days_default": None,
        }
    if raw.startswith("manual:"):
        return {
            "key": raw,
            "kind": "manual",
            "unit": None,
            "value_type": "text",
            "period_kind": None,
            "rollup_rule": None,
            "stale_after_days_default": None,
        }

    parts = raw.split(":")
    if len(parts) == 4:
        unit = parts[3]
        return {
            "key": raw,
            "kind": "series",
            "unit": unit,
            "value_type": _infer_value_type_from_unit(unit),
            "period_kind": None,
            "rollup_rule": None,
            "stale_after_days_default": None,
        }

    # A predicate key can exist independently (e.g., "supplies_share_of").
    if len(parts) == 1 and raw.endswith("_of"):
        return {
            "key": raw,
            "kind": "predicate",
            "unit": None,
            "value_type": "text",
            "period_kind": None,
            "rollup_rule": None,
            "stale_after_days_default": None,
        }

    return {
        "key": raw,
        "kind": "assertion",
        "unit": None,
        "value_type": "text",
        "period_kind": None,
        "rollup_rule": None,
        "stale_after_days_default": None,
    }


def upsert_proposition(
    session: Session,
    *,
    key: str,
    kind: str | None = None,
    unit: str | None = None,
    value_type: str | None = None,
    period_kind: str | None = None,
    rollup_rule: str | None = None,
    stale_after_days_default: int | None = None,
    description: str | None = None,
    validation: dict[str, Any] | None = None,
) -> Proposition:
    raw = (key or "").strip()
    if not raw:
        raise ValueError("Proposition key is required.")

    existing = session.query(Proposition).filter_by(key=raw).first()
    if existing:
        return existing

    inferred = infer_proposition_from_key(session, raw)
    now = utcnow()
    prop = Proposition(
        key=raw,
        kind=kind or inferred.get("kind") or "manual",
        unit=unit if unit is not None else inferred.get("unit"),
        value_type=value_type or inferred.get("value_type") or "number",
        period_kind=period_kind if period_kind is not None else inferred.get("period_kind"),
        rollup_rule=rollup_rule if rollup_rule is not None else inferred.get("rollup_rule"),
        stale_after_days_default=stale_after_days_default
        if stale_after_days_default is not None
        else inferred.get("stale_after_days_default"),
        description=description,
        validation=validation or {},
        created_at=now,
    )
    session.add(prop)
    session.flush()
    return prop


def link_thesis_proposition(session: Session, *, thesis: Thesis, proposition: Proposition, role: str = "key") -> ThesisProposition:
    existing = (
        session.query(ThesisProposition)
        .filter_by(thesis_id=thesis.id, proposition_id=proposition.id)
        .first()
    )
    now = utcnow()
    if existing:
        existing.role = role or existing.role
        return existing

    link = ThesisProposition(
        thesis_id=thesis.id,
        proposition_id=proposition.id,
        role=(role or "key").strip(),
        created_at=now,
    )
    session.add(link)
    thesis.updated_at = now
    return link


def add_observation(
    session: Session,
    *,
    proposition: Proposition,
    document_id: int | None = None,
    value: str | None = None,
    unit: str | None = None,
    target_period: str | None = None,
    estimate_flag: str | None = None,
    as_of_date: datetime | None = None,
    confidence: float | None = None,
    note: str | None = None,
    extra: dict[str, Any] | None = None,
) -> Observation:
    now = utcnow()
    val = (value or "").strip()
    val_num: float | None = None
    val_text: str | None = None
    val_bool: int | None = None

    if not val:
        raise ValueError("Observation value is required.")

    if proposition.value_type == "bool":
        if val.lower() in ("true", "t", "yes", "y", "1"):
            val_bool = 1
        elif val.lower() in ("false", "f", "no", "n", "0"):
            val_bool = 0
        else:
            raise ValueError("Boolean observation must be true/false.")
    elif proposition.value_type in ("number", "percent"):
        parsed = _parse_numeric_value(val, allow_percent=(proposition.value_type == "percent"))
        if parsed is not None:
            val_num = parsed
        else:
            val_text = val
    else:
        val_text = val

    tp = parse_target_period(target_period)
    if estimate_flag:
        tp = TargetPeriod(tp.year, tp.quarter, tp.granularity, estimate_flag.strip().upper())

    obs = Observation(
        proposition_id=proposition.id,
        document_id=int(document_id) if document_id is not None else None,
        value_number=val_num,
        value_text=val_text,
        value_bool=val_bool,
        unit=(unit.strip() if unit else proposition.unit),
        confidence=confidence,
        target_year=tp.year,
        target_quarter=tp.quarter,
        period_granularity=tp.granularity,
        estimate_flag=tp.estimate_flag,
        as_of_date=as_of_date,
        note=note.strip() if note else None,
        created_at=now,
        extra=extra or {},
    )
    session.add(obs)
    if document_id is not None:
        try:
            from thoth_terminal.triage_service import mark_actioned

            mark_actioned(
                session,
                document_id=int(document_id),
                meta={"source": "auto:proposition_observation", "proposition_id": proposition.id},
            )
        except Exception:
            pass
    return obs
