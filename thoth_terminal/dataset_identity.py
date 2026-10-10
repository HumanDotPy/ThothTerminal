"""Stable identities for company datasets owned by ingestion producers."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import case, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from thoth_terminal.dataset_parts.core import _slugify, ensure_dataset_tables
from thoth_terminal.dataset_service import create_dataset
from thoth_terminal.models import Dataset


_FACT_PROVENANCE = "Materialized from issuer-linked earnings documents and structured fact rows."
_FILING_PROVENANCE = "Materialized from official/regulatory investor-document ingestion."
_IDENTITY_PART = re.compile(r"[a-z0-9_]{1,64}\Z")


def canonical_managed_dataset_slug(slug: str) -> str:
    return _slugify(slug)


def managed_company_dataset_key(company_id: int, family: str, source_kind: str) -> str:
    if int(company_id) <= 0:
        raise ValueError("Managed company datasets require a stored company ID")
    if not _IDENTITY_PART.fullmatch(family) or not _IDENTITY_PART.fullmatch(source_kind):
        raise ValueError("Invalid managed dataset family or producer")
    if source_kind != family and (family, source_kind) != ("earnings_filings", "official_filings"):
        raise ValueError("Dataset producer does not manage this company family")
    return f"company:{int(company_id)}:{family}:{source_kind}"


def legacy_managed_company_dataset_key(dataset: Dataset) -> str | None:
    """Recognize ingestion-owned legacy rows without claiming analyst datasets."""
    if dataset.canonical_dataset_id is not None:
        return None
    extra = dataset.extra if isinstance(dataset.extra, dict) else {}
    company_slug = str(extra.get("company_slug") or "").strip()
    if not company_slug:
        return None
    family = str(dataset.family or "")
    source_kind = str(dataset.source_kind or "")
    provenance = str(extra.get("provenance_summary") or "")
    if source_kind == "official_filings" and family == "earnings_filings":
        if not provenance.startswith(_FILING_PROVENANCE):
            return None
    elif source_kind == family:
        if provenance != _FACT_PROVENANCE:
            return None
    else:
        return None
    base = canonical_managed_dataset_slug(f"{company_slug}-{family.replace('_', '-')}")
    suffix = str(dataset.slug)[len(base):] if str(dataset.slug).startswith(base) else None
    if suffix != "" and (suffix is None or not re.fullmatch(r"-(?:[2-9]|[1-9]\d+)", suffix)):
        return None
    try:
        return managed_company_dataset_key(int(extra.get("company_id")), family, source_kind)
    except (TypeError, ValueError):
        return None


def lock_managed_company_dataset(
    session: Session,
    *,
    company_id: int,
    family: str,
    source_kind: str,
) -> str:
    """Serialize a producer before reading its source rows, without creating it.

    Under PostgreSQL READ COMMITTED, source queries issued after a waiting
    transaction acquires this lock see the previous producer's committed rows.
    The transaction retains the lock through materialization and commit.
    """
    key = managed_company_dataset_key(company_id, family, source_kind)
    if session.get_bind().dialect.name == "postgresql":
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"dataset-managed:{key}"},
        )
    return key


def ensure_managed_company_dataset(
    session: Session,
    *,
    company_id: int,
    family: str,
    source_kind: str,
    slug: str,
    **creation: Any,
) -> tuple[Dataset, bool]:
    """Find or create one managed identity in the caller's transaction.

    PostgreSQL locks the identity before looking for a row, including the first
    creation. The unique managed key remains the database backstop. This lock
    lasts through the caller's draft replacement, so competing ingestions also
    serialize their updates. SQLite is used only by isolated tests.
    """
    canonical_slug = canonical_managed_dataset_slug(slug)
    ensure_dataset_tables(session)
    key = lock_managed_company_dataset(
        session, company_id=company_id, family=family, source_kind=source_kind,
    )
    query = session.query(Dataset).filter(Dataset.canonical_dataset_id.is_(None))
    dataset = query.filter_by(managed_key=key).with_for_update().first()
    if dataset is not None:
        return dataset, False

    # Old rows predate managed keys. Only the producer's slug and provenance
    # signature qualify; sharing a company association or family is insufficient.
    candidates = query.filter(
        Dataset.managed_key.is_(None),
        Dataset.family == family,
        Dataset.source_kind == source_kind,
        Dataset.extra["company_id"].as_integer() == int(company_id),
    ).order_by(case((Dataset.slug == canonical_slug, 0), else_=1), Dataset.id.asc())
    for candidate in candidates:
        if legacy_managed_company_dataset_key(candidate) == key:
            candidate.managed_key = key
            session.flush()
            return candidate, False

    try:
        with session.begin_nested():
            dataset = create_dataset(
                session, slug=canonical_slug, family=family, source_kind=source_kind, **creation,
            )
            dataset.managed_key = key
            session.flush()
    except IntegrityError:
        # A writer bypassing the advisory lock must still converge on the
        # database identity; keep the caller's transaction usable after conflict.
        dataset = query.filter_by(managed_key=key).with_for_update().first()
        if dataset is None:
            raise
        return dataset, False
    return dataset, True
