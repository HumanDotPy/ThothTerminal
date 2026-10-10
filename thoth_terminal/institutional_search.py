"""Bounded institutional retrieval with relevance applied before the result limit."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import Integer, column, or_, select, text
from sqlalchemy.orm import Session

from thoth_terminal.models import (
    Company, InstitutionalEvidenceItem, InstitutionalObservation, ResearchSubject,
)


def search_index_statements(*, concurrently: bool = False) -> list[str]:
    from _thoth_html import institutional_search_index_statements
    return institutional_search_index_statements(concurrently)


@dataclass
class InstitutionalSearchContext:
    query: str
    tokens: tuple[str, ...]
    # Each token maps only to identities that actually matched that token in the query.
    identities: dict[str, dict[str, set[int]]] = field(default_factory=dict)


def resolve_search_context(session: Session, query: str) -> InstitutionalSearchContext:
    normalized = re.sub(r"\s+", " ", query.strip().lower().replace("-", " ").replace("_", " "))
    words = normalized.split()
    phrases = {normalized}
    # Entity names/aliases are bounded columns. Eight-word spans cover their names
    # without constructing a quadratic predicate for a long research question.
    phrases.update(" ".join(words[start:start + length]) for start in range(len(words)) for length in range(1, min(8, len(words) - start) + 1))
    tokens = tuple(sorted(set(re.findall(r"[^\W_]+", normalized, flags=re.UNICODE))))
    context = InstitutionalSearchContext(query.strip().lower(), tokens)
    if re.fullmatch(r"[a-z_]+:\d+(?::[^\s]+)?", context.query):
        return context
    from _thoth_html import plan_institutional_identities
    sql, parameters = plan_institutional_identities(sorted(phrases), session.get_bind().dialect.name)
    for company_id, issuer_id, subject_id, matched_value in session.execute(text(sql), parameters):
        covered = set(re.findall(r"[^\W_]+", str(matched_value), flags=re.UNICODE)) & set(tokens)
        for token in covered:
            ids = context.identities.setdefault(token, {"company_id": set(), "issuer_id": set(), "subject_id": set()})
            for key, value in (("company_id", company_id), ("issuer_id", issuer_id), ("subject_id", subject_id)):
                if value is not None:
                    ids[key].add(int(value))
    # Close the existing issuer/company/subject links, without materializing catalogs.
    all_company_ids = {i for ids in context.identities.values() for i in ids["company_id"]}
    all_issuer_ids = {i for ids in context.identities.values() for i in ids["issuer_id"]}
    if all_company_ids or all_issuer_ids:
        companies = session.execute(select(Company.id, Company.issuer_id).where(or_(Company.id.in_(all_company_ids), Company.issuer_id.in_(all_issuer_ids)))).all()
        subjects = session.execute(
            select(ResearchSubject.id, ResearchSubject.company_id, ResearchSubject.issuer_id)
            .outerjoin(Company, Company.id == ResearchSubject.company_id)
            .where(
                ResearchSubject.status.notin_(("archived", "merged")),
                or_(ResearchSubject.issuer_id.is_(None), Company.issuer_id.is_(None), ResearchSubject.issuer_id == Company.issuer_id),
                or_(ResearchSubject.company_id.in_(all_company_ids), ResearchSubject.issuer_id.in_(all_issuer_ids)),
            )
        ).all()
        for ids in context.identities.values():
            for company_id, issuer_id in companies:
                if issuer_id is not None and ids["issuer_id"] and issuer_id not in ids["issuer_id"]:
                    continue
                if company_id in ids["company_id"] or issuer_id in ids["issuer_id"]:
                    ids["company_id"].add(company_id)
                    if issuer_id is not None:
                        ids["issuer_id"].add(issuer_id)
            for subject_id, company_id, issuer_id in subjects:
                if company_id in ids["company_id"] or issuer_id in ids["issuer_id"]:
                    ids["subject_id"].add(subject_id)
    return context


def ranked_query(session: Session, model, columns: list, context: InstitutionalSearchContext, limit: int):
    from _thoth_html import plan_institutional_search
    identities = {
        token: tuple(sorted(ids[key]) for key in ("company_id", "issuer_id", "subject_id"))
        for token, ids in context.identities.items()
    }
    kind = "institutional_evidence" if model is InstitutionalEvidenceItem else "institutional_observation"
    sql, parameters = plan_institutional_search(kind, context.query, list(context.tokens), identities, limit, session.get_bind().dialect.name)
    # Typed result columns retain SQLite datetime conversion and PostgreSQL's
    # normal driver types without materializing ORM records or large payloads.
    statement = text(sql).columns(*columns, column("search_rank", Integer))
    return session.execute(statement, parameters)
