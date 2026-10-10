"""Validate identity aliases and repair unambiguous cross-issuer associations.

PostgreSQL/SQLite performs ownership matching; Python orchestrates bounded writes
and audit receipts. A related company name is not an equivalent identity.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from sqlalchemy import func, select, union_all
from sqlalchemy.orm import Session
from sqlalchemy.orm import aliased

from thoth_terminal.db import utcnow
from thoth_terminal.models import (
    Company, EntityIdentity, EntityIdentityAlias, Issuer, IssuerIdentifier,
    ResearchSubject, ResearchSubjectAlias,
)


def normalized_alias(column):
    return func.lower(func.trim(func.replace(func.replace(column, "-", " "), "_", " ")))


def canonical_alias_owners():
    branches = []
    for model, issuer_column, fields in (
        (Company, Company.issuer_id, (Company.name, Company.slug, Company.ticker)),
        (Issuer, Issuer.id, (Issuer.display_name, Issuer.legal_name, Issuer.slug)),
        (IssuerIdentifier, IssuerIdentifier.issuer_id, (IssuerIdentifier.id_value,)),
    ):
        for field in fields:
            statement = select(normalized_alias(field).label("alias"), issuer_column.label("issuer_id")).where(
                issuer_column.is_not(None), field.is_not(None), func.trim(field) != "")
            if model is IssuerIdentifier:
                statement = statement.where(IssuerIdentifier.id_type == "TICKER")
            branches.append(statement)
    return union_all(*branches).cte("canonical_alias_owners")


def _possibly_same_name(left, right):
    from thoth_terminal.decision_spine_service import canonical_entity_key
    a, b = canonical_entity_key(left), canonical_entity_key(right)
    # Shared brand words also cover abbreviations and ADR/local issuer records.
    # This is a reason to defer, never evidence authorizing a merge or relink.
    return bool(a and b and (a == b or a.startswith(b + " ") or b.startswith(a + " ")
                            or {word for word in a.split() if len(word) >= 3} & set(b.split())))


def valid_identity_aliases(session: Session, *, issuer_id: int | None, aliases):
    """Keep aliases that do not claim a different canonical issuer's identity."""
    values = list(dict.fromkeys(str(value or "").strip()[:255] for value in aliases if str(value or "").strip()))
    if not values:
        return []
    keys = {value: value.lower().replace("-", " ").replace("_", " ") for value in values}
    canonical = canonical_alias_owners()
    owners = defaultdict(set)
    for alias, owner in session.execute(select(canonical.c.alias, canonical.c.issuer_id)
                                        .where(canonical.c.alias.in_(list(set(keys.values())))).distinct()):
        owners[alias].add(owner)
    # Similar names cannot authorize new cross-issuer equivalence. Existing
    # possible duplicates are deferred by repair, pending identity evidence.
    return [value for value in values if not owners[keys[value]] or issuer_id in owners[keys[value]]]


def require_consistent_identity(*, company: Company | None, issuer: Issuer | None):
    if company is not None and issuer is not None and company.issuer_id is not None and company.issuer_id != issuer.id:
        raise ValueError(f"Company {company.id} belongs to issuer {company.issuer_id}, not issuer {issuer.id}.")


def repair_identity_aliases(session: Session, *, apply=False, expected_fingerprint=None,
                            actor="operator", note=None, canonical_issuer_ids=None):
    """Preview/apply only aliases owned by a different, unambiguous issuer.

    Does not relink subjects, listings, companies, or research evidence. Removed
    aliases and the canonical owner are recorded on the existing owning objects.
    """
    canonical = canonical_alias_owners()
    unique = select(canonical.c.alias, func.min(canonical.c.issuer_id).label("issuer_id")).group_by(
        canonical.c.alias).having(func.count(func.distinct(canonical.c.issuer_id)) == 1).cte("unique_alias_owners")
    changes = []
    deferred = []
    target = aliased(Issuer)
    scope = sorted(set(canonical_issuer_ids or []))
    for surface, model, owner_model, foreign_key in (
        ("research_subject_aliases", ResearchSubjectAlias, ResearchSubject, ResearchSubjectAlias.subject_id),
        ("entity_identity_aliases", EntityIdentityAlias, EntityIdentity, EntityIdentityAlias.identity_id),
    ):
        owner_issuer = func.coalesce(owner_model.issuer_id, Company.issuer_id)
        statement = select(model.id, foreign_key.label("owner_id"), model.alias, model.alias_type,
            owner_model.display_name.label("owner_name"), owner_issuer.label("owner_issuer_id"),
            unique.c.issuer_id.label("canonical_issuer_id"), target.display_name.label("canonical_name"),
            target.legal_name.label("canonical_legal_name")).join(owner_model, owner_model.id == foreign_key).outerjoin(
                Company, Company.id == owner_model.company_id).join(unique, unique.c.alias == normalized_alias(model.alias)).where(
                owner_issuer.is_not(None), owner_issuer != unique.c.issuer_id).join(
                target, target.id == unique.c.issuer_id).order_by(model.id)
        if scope:
            statement = statement.where(unique.c.issuer_id.in_(scope))
        for row in session.execute(statement):
            item = {"surface": surface, **dict(row._mapping)}
            if any(_possibly_same_name(row.owner_name, name) for name in (row.canonical_name, row.canonical_legal_name)):
                deferred.append({**item, "reason": "possible_duplicate_or_related_issuer"})
            else:
                changes.append(item)
    fingerprint = hashlib.sha256(json.dumps(changes, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    result = {"applied": False, "canonical_issuer_ids": scope, "fingerprint": fingerprint, "count": len(changes),
              "by_surface": {surface: sum(row["surface"] == surface for row in changes)
                             for surface in ("research_subject_aliases", "entity_identity_aliases")},
              "changes": changes, "deferred": deferred}
    if not apply:
        return result
    if not scope:
        raise ValueError("Applying alias repair requires explicit canonical issuer IDs.")
    if not note or not str(note).strip():
        raise ValueError("An audit note is required for alias repair.")
    if expected_fingerprint != fingerprint:
        raise ValueError("Alias repair scope changed since preview; preview again before applying.")
    now = utcnow()
    grouped = defaultdict(list)
    for row in changes:
        grouped[(row["surface"], row["owner_id"])].append(row)
    for (surface, owner_id), removed in sorted(grouped.items()):
        alias_model, owner_model = ((ResearchSubjectAlias, ResearchSubject) if surface == "research_subject_aliases"
                                    else (EntityIdentityAlias, EntityIdentity))
        owner = session.get(owner_model, owner_id, with_for_update=True, populate_existing=True)
        if owner is None:
            raise ValueError("Alias owner changed since preview; retry the repair.")
        actual_issuer_id = owner.issuer_id
        if actual_issuer_id is None and owner.company_id is not None:
            company = session.get(Company, owner.company_id)
            actual_issuer_id = company.issuer_id if company is not None else None
        if actual_issuer_id != removed[0]["owner_issuer_id"] or owner.display_name != removed[0]["owner_name"]:
            raise ValueError("Alias owner changed since preview; retry the repair.")
        aliases = session.scalars(select(alias_model).where(alias_model.id.in_([row["id"] for row in removed]))
                                  .order_by(alias_model.id).with_for_update()).all()
        if len(aliases) != len(removed) or any(alias.alias != row["alias"] or alias.alias_type != row["alias_type"]
            or getattr(alias, "subject_id" if surface == "research_subject_aliases" else "identity_id") != owner_id
            for alias, row in zip(aliases, removed, strict=True)):
            raise ValueError("Alias rows changed since preview; retry the repair.")
        extra = dict(owner.extra or {})
        history = list(extra.get("alias_integrity_repairs") or [])
        history.append({"at": now.isoformat(), "actor": actor, "note": str(note).strip(),
                        "fingerprint": fingerprint, "removed": removed})
        extra["alias_integrity_repairs"] = history
        owner.extra, owner.updated_at = extra, now
        for alias in aliases:
            session.delete(alias)
    session.flush()
    result["applied"] = True
    return result
