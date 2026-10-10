from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime
from typing import Any

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, defer, selectinload

from thoth_terminal.agent.llm_client import AgentLlmError, ChatMessage, active_llm_model, llm_available
from thoth_terminal.agent_request_manifest_service import manifested_chat_json
from thoth_terminal.config import settings
from thoth_terminal.db import utcnow
from thoth_terminal.instrument_service import create_instrument_request
from thoth_terminal.models import (
    KnowledgeAsset,
    KnowledgeAssetLink,
    KnowledgeAssetVersion,
    ResearchSubject,
    ResearchSubjectAlias,
)
from thoth_terminal.research_report_service import assemble_report_payload, resolve_report_subject


KNOWLEDGE_KINDS = {"primer", "teardown"}


def _slugify(value: str | None, *, default: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return cleaned or default


def _clean_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _dt_iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _normalize_kind(value: str) -> str:
    kind = str(value or "").strip().lower()
    if kind not in KNOWLEDGE_KINDS:
        raise ValueError("kind must be primer or teardown")
    return kind


def _unique_subject_slug(session: Session, base_slug: str) -> str:
    base = (base_slug[:118].strip("-") or "research-subject")
    candidate = base
    suffix = 2
    while session.query(ResearchSubject.id).filter_by(slug=candidate).first() is not None:
        tail = f"-{suffix}"
        candidate = f"{base[: 128 - len(tail)].rstrip('-')}{tail}"
        suffix += 1
    return candidate


def serialize_research_subject(row: ResearchSubject) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "slug": row.slug,
        "subject_key": row.subject_key,
        "kind": row.kind,
        "display_name": row.display_name,
        "legal_name": row.legal_name,
        "description": row.description,
        "issuer_id": row.issuer_id,
        "company_id": row.company_id,
        "primary_entity_id": row.primary_entity_id,
        "status": row.status,
        "aliases": sorted({str(alias.alias or "").strip() for alias in (row.aliases or []) if str(alias.alias or "").strip()}),
        "updated_at": _dt_iso(row.updated_at),
        "href": f"/reports/{row.slug}",
    }


def list_research_subjects(
    session: Session,
    *,
    query: str | None = None,
    kind: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    q = session.query(ResearchSubject)
    kind_norm = str(kind or "").strip().lower()
    if kind_norm:
        q = q.filter(ResearchSubject.kind == kind_norm)
    query_text = str(query or "").strip()
    if query_text:
        pattern = f"%{query_text.lower()}%"
        q = (
            q.outerjoin(ResearchSubjectAlias, ResearchSubjectAlias.subject_id == ResearchSubject.id)
            .filter(
                or_(
                    func.lower(ResearchSubject.display_name).like(pattern),
                    func.lower(func.coalesce(ResearchSubject.legal_name, "")).like(pattern),
                    func.lower(ResearchSubject.slug).like(pattern),
                    func.lower(ResearchSubject.subject_key).like(pattern),
                    func.lower(ResearchSubjectAlias.alias).like(pattern),
                )
            )
            .distinct()
        )
    rows = (
        q.options(selectinload(ResearchSubject.aliases))
        .order_by(ResearchSubject.updated_at.desc(), ResearchSubject.id.desc())
        .limit(max(1, min(int(limit or 50), 200)))
        .all()
    )
    return [serialize_research_subject(row) for row in rows]


def get_research_subject(session: Session, *, subject: str | int) -> dict[str, Any]:
    resolved = resolve_report_subject(session, subject)
    if resolved.subject_row is not None:
        return serialize_research_subject(resolved.subject_row)
    payload = {
        "id": None,
        "slug": resolved.slug,
        "subject_key": resolved.subject_key,
        "kind": resolved.kind,
        "display_name": resolved.display_name,
        "legal_name": resolved.legal_name,
        "description": resolved.description,
        "issuer_id": resolved.issuer_id,
        "company_id": resolved.company_id,
        "primary_entity_id": resolved.primary_entity_id,
        "status": "virtual",
        "aliases": list(resolved.aliases),
        "updated_at": None,
        "href": f"/reports/{resolved.slug}",
    }
    return payload


def _subject_row_or_create(session: Session, *, subject: str | int) -> ResearchSubject:
    try:
        resolved = resolve_report_subject(session, subject)
    except ValueError:
        raw = _clean_text(subject)
        if not raw:
            raise
        base_slug = _slugify(raw, default="research-subject")
        subject_key = f"theme:{base_slug}"
        row = session.query(ResearchSubject).filter_by(subject_key=subject_key).first()
        if row is None:
            slug = _unique_subject_slug(session, base_slug)
            row = ResearchSubject(
                slug=slug,
                subject_key=f"theme:{slug}",
                kind="theme",
                display_name=raw,
                legal_name=None,
                description=f"Knowledge asset subject created from draft import: {raw}",
                status="active",
                issuer_id=None,
                company_id=None,
                primary_entity_id=None,
                extra={"created_from": "knowledge_asset_draft"},
                created_at=utcnow(),
                updated_at=utcnow(),
            )
            session.add(row)
            session.flush()
        return row
    now = utcnow()
    row = resolved.subject_row
    if row is None:
        row = session.query(ResearchSubject).filter_by(subject_key=resolved.subject_key).first()
    if row is None:
        row = ResearchSubject(
            slug=resolved.slug,
            subject_key=resolved.subject_key,
            kind=resolved.kind,
            display_name=resolved.display_name,
            legal_name=resolved.legal_name,
            description=resolved.description,
            status="active",
            issuer_id=resolved.issuer_id,
            company_id=resolved.company_id,
            primary_entity_id=resolved.primary_entity_id,
            extra={"tickers": list(resolved.tickers)},
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.flush()
    else:
        row.updated_at = now
        session.flush()
    alias_values = sorted({value for value in resolved.aliases if value})
    existing = {
        str(alias.alias or "").strip()
        for alias in session.query(ResearchSubjectAlias)
        .filter(ResearchSubjectAlias.subject_id == int(row.id))
        .all()
    }
    from thoth_terminal.identity_alias_service import valid_identity_aliases
    for alias in valid_identity_aliases(session, issuer_id=row.issuer_id, aliases=alias_values):
        if alias in existing:
            continue
        session.add(
            ResearchSubjectAlias(
                subject_id=int(row.id),
                alias=alias,
                alias_type="ticker" if alias.upper() in resolved.tickers or alias.startswith("$") else "name",
                created_at=now,
            )
        )
    session.flush()
    return row


def _asset_title(subject_row: ResearchSubject, *, kind: str) -> str:
    suffix = "Primer" if kind == "primer" else "Teardown"
    return f"{subject_row.display_name} {suffix}"


def _asset_slug(subject_row: ResearchSubject, *, kind: str) -> str:
    return _slugify(f"{subject_row.slug}-{kind}", default=f"knowledge-{kind}")


def _asset_for_subject(session: Session, *, subject_row: ResearchSubject, kind: str, create: bool) -> KnowledgeAsset | None:
    asset = (
        session.query(KnowledgeAsset)
        .filter(KnowledgeAsset.subject_id == int(subject_row.id), KnowledgeAsset.kind == str(kind))
        .first()
    )
    if asset is not None or not create:
        return asset
    now = utcnow()
    asset = KnowledgeAsset(
        subject_id=int(subject_row.id),
        slug=_asset_slug(subject_row, kind=kind),
        kind=str(kind),
        title=_asset_title(subject_row, kind=kind),
        summary=None,
        status="active",
        stale_after_days=180,
        latest_published_version_id=None,
        last_reviewed_at=None,
        last_stale_check_at=None,
        stale_check_state=None,
        extra={},
        created_at=now,
        updated_at=now,
    )
    session.add(asset)
    session.flush()
    return asset


def _draft_version(session: Session, asset_id: int) -> KnowledgeAssetVersion | None:
    return (
        session.query(KnowledgeAssetVersion)
        .filter(KnowledgeAssetVersion.asset_id == int(asset_id), KnowledgeAssetVersion.status == "draft")
        .order_by(KnowledgeAssetVersion.updated_at.desc(), KnowledgeAssetVersion.id.desc())
        .first()
    )


def _latest_published_version(session: Session, asset_id: int) -> KnowledgeAssetVersion | None:
    return (
        session.query(KnowledgeAssetVersion)
        .filter(KnowledgeAssetVersion.asset_id == int(asset_id), KnowledgeAssetVersion.status == "published")
        .order_by(KnowledgeAssetVersion.version.desc(), KnowledgeAssetVersion.id.desc())
        .first()
    )


def serialize_knowledge_asset_version(row: KnowledgeAssetVersion | None, *, include_content: bool = True) -> dict[str, Any] | None:
    if row is None:
        return None
    payload = {
        "id": int(row.id),
        "asset_id": int(row.asset_id),
        "version": int(row.version),
        "status": row.status,
        "title": row.title,
        "summary": row.summary,
        "as_of_at": _dt_iso(row.as_of_at),
        "created_at": _dt_iso(row.created_at),
        "updated_at": _dt_iso(row.updated_at),
        "published_at": _dt_iso(row.published_at),
    }
    if include_content:
        payload.update(
            {
                "content_md": row.content_md,
                "content_json": dict(row.content_json or {}),
                "coverage_json": dict(row.coverage_json or {}),
                "evidence_bundle": list(row.evidence_bundle or []),
                "ai_metadata": dict(row.ai_metadata or {}),
            }
        )
    else:
        payload["content_omitted"] = True
    return payload


def serialize_knowledge_asset(
    asset: KnowledgeAsset,
    *,
    subject_row: ResearchSubject,
    draft: KnowledgeAssetVersion | None,
    published: KnowledgeAssetVersion | None,
    links: list[KnowledgeAssetLink] | None = None,
    include_version_content: bool = True,
) -> dict[str, Any]:
    asset_links = asset.links if links is None else links
    return {
        "id": int(asset.id),
        "subject_id": int(asset.subject_id),
        "subject_slug": subject_row.slug,
        "subject_display_name": subject_row.display_name,
        "subject_kind": subject_row.kind,
        "slug": asset.slug,
        "kind": asset.kind,
        "title": asset.title,
        "summary": asset.summary,
        "status": asset.status,
        "stale_after_days": asset.stale_after_days,
        "latest_published_version_id": asset.latest_published_version_id,
        "last_reviewed_at": _dt_iso(asset.last_reviewed_at),
        "last_stale_check_at": _dt_iso(asset.last_stale_check_at),
        "stale_check_state": asset.stale_check_state,
        "links": [
            {
                "id": int(link.id),
                "object_type": link.object_type,
                "object_id": int(link.object_id),
                "relation": link.relation,
                "created_at": _dt_iso(link.created_at),
            }
            for link in (asset_links or [])
        ],
        "draft": serialize_knowledge_asset_version(draft, include_content=include_version_content),
        "published": serialize_knowledge_asset_version(published, include_content=include_version_content),
        "href": f"/knowledge/{asset.slug}",
        "updated_at": _dt_iso(asset.updated_at),
    }


def _asset_version_maps(
    session: Session,
    asset_ids: list[int],
    *,
    include_content: bool = True,
) -> tuple[dict[int, KnowledgeAssetVersion], dict[int, KnowledgeAssetVersion]]:
    if not asset_ids:
        return {}, {}
    options = []
    if not include_content:
        options = [
            defer(KnowledgeAssetVersion.content_md),
            defer(KnowledgeAssetVersion.content_json),
            defer(KnowledgeAssetVersion.coverage_json),
            defer(KnowledgeAssetVersion.evidence_bundle),
            defer(KnowledgeAssetVersion.ai_metadata),
        ]
    drafts: dict[int, KnowledgeAssetVersion] = {}
    for row in (
        session.query(KnowledgeAssetVersion)
        .options(*options)
        .filter(KnowledgeAssetVersion.asset_id.in_(asset_ids), KnowledgeAssetVersion.status == "draft")
        .order_by(
            KnowledgeAssetVersion.asset_id.asc(),
            KnowledgeAssetVersion.updated_at.desc(),
            KnowledgeAssetVersion.id.desc(),
        )
        .all()
    ):
        asset_id = int(row.asset_id)
        if asset_id not in drafts:
            drafts[asset_id] = row
    published: dict[int, KnowledgeAssetVersion] = {}
    for row in (
        session.query(KnowledgeAssetVersion)
        .options(*options)
        .filter(KnowledgeAssetVersion.asset_id.in_(asset_ids), KnowledgeAssetVersion.status == "published")
        .order_by(
            KnowledgeAssetVersion.asset_id.asc(),
            KnowledgeAssetVersion.version.desc(),
            KnowledgeAssetVersion.id.desc(),
        )
        .all()
    ):
        asset_id = int(row.asset_id)
        if asset_id not in published:
            published[asset_id] = row
    return drafts, published


def _asset_links_by_asset_id(session: Session, asset_ids: list[int]) -> dict[int, list[KnowledgeAssetLink]]:
    if not asset_ids:
        return {}
    links_by_asset: dict[int, list[KnowledgeAssetLink]] = defaultdict(list)
    for row in (
        session.query(KnowledgeAssetLink)
        .filter(KnowledgeAssetLink.asset_id.in_(asset_ids))
        .order_by(KnowledgeAssetLink.asset_id.asc(), KnowledgeAssetLink.id.asc())
        .all()
    ):
        links_by_asset[int(row.asset_id)].append(row)
    return dict(links_by_asset)


def list_knowledge_assets(
    session: Session,
    *,
    query: str | None = None,
    subject: str | int | None = None,
    kind: str | None = None,
    published_only: bool = False,
    include_version_content: bool = True,
    limit: int = 50,
) -> list[dict[str, Any]]:
    q = session.query(KnowledgeAsset, ResearchSubject).join(ResearchSubject, ResearchSubject.id == KnowledgeAsset.subject_id)
    if subject is not None:
        subject_row = _subject_row_or_create(session, subject=subject)
        q = q.filter(KnowledgeAsset.subject_id == int(subject_row.id))
    kind_norm = str(kind or "").strip().lower()
    if kind_norm:
        q = q.filter(KnowledgeAsset.kind == _normalize_kind(kind_norm))
    query_text = str(query or "").strip()
    if query_text:
        pattern = f"%{query_text.lower()}%"
        q = q.filter(
            or_(
                func.lower(KnowledgeAsset.title).like(pattern),
                func.lower(func.coalesce(KnowledgeAsset.summary, "")).like(pattern),
                func.lower(KnowledgeAsset.slug).like(pattern),
                func.lower(ResearchSubject.display_name).like(pattern),
                func.lower(ResearchSubject.slug).like(pattern),
            )
        )
    rows = q.order_by(KnowledgeAsset.updated_at.desc(), KnowledgeAsset.id.desc()).limit(max(1, min(int(limit or 50), 200))).all()
    asset_ids = [int(asset.id) for asset, _subject_row in rows]
    draft_by_asset_id, published_by_asset_id = _asset_version_maps(session, asset_ids, include_content=include_version_content)
    links_by_asset_id = _asset_links_by_asset_id(session, asset_ids)
    out: list[dict[str, Any]] = []
    for asset, subject_row in rows:
        asset_id = int(asset.id)
        published = published_by_asset_id.get(asset_id)
        if published_only and asset.latest_published_version_id is None and published is None:
            continue
        if subject_row is None:
            continue
        out.append(
            serialize_knowledge_asset(
                asset,
                subject_row=subject_row,
                draft=draft_by_asset_id.get(asset_id),
                published=published,
                links=links_by_asset_id.get(asset_id, []),
                include_version_content=include_version_content,
            )
        )
    return out


def get_knowledge_asset(
    session: Session,
    *,
    asset_id: int | None = None,
    slug: str | None = None,
    subject: str | int | None = None,
    kind: str | None = None,
) -> dict[str, Any]:
    asset = None
    if asset_id is not None:
        asset = session.query(KnowledgeAsset).filter_by(id=int(asset_id)).first()
    elif slug:
        asset = session.query(KnowledgeAsset).filter_by(slug=str(slug)).first()
    elif subject is not None and kind:
        subject_row = _subject_row_or_create(session, subject=subject)
        asset = _asset_for_subject(session, subject_row=subject_row, kind=_normalize_kind(kind), create=False)
    if asset is None:
        raise ValueError("Knowledge asset not found.")
    subject_row = session.query(ResearchSubject).filter_by(id=int(asset.subject_id)).first()
    if subject_row is None:
        raise ValueError("Knowledge asset subject not found.")
    return serialize_knowledge_asset(
        asset,
        subject_row=subject_row,
        draft=_draft_version(session, int(asset.id)),
        published=_latest_published_version(session, int(asset.id)),
    )


def _replace_links(session: Session, *, asset: KnowledgeAsset, links: list[dict[str, Any]]) -> None:
    session.query(KnowledgeAssetLink).filter(KnowledgeAssetLink.asset_id == int(asset.id)).delete(synchronize_session=False)
    now = utcnow()
    for row in links:
        if not isinstance(row, dict):
            continue
        object_type = str(row.get("object_type") or "").strip()
        object_id = row.get("object_id")
        relation = str(row.get("relation") or "references").strip() or "references"
        if not object_type or object_id is None or not str(object_id).isdigit():
            continue
        session.add(
            KnowledgeAssetLink(
                asset_id=int(asset.id),
                object_type=object_type,
                object_id=int(object_id),
                relation=relation,
                created_at=now,
            )
        )


def save_knowledge_asset_draft(
    session: Session,
    *,
    subject: str | int,
    kind: str,
    title: str | None = None,
    summary: str | None = None,
    content_md: str | None = None,
    content_json: dict[str, Any] | None = None,
    coverage_json: dict[str, Any] | None = None,
    evidence_bundle: list[dict[str, Any]] | None = None,
    ai_metadata: dict[str, Any] | None = None,
    links: list[dict[str, Any]] | None = None,
    stale_after_days: int | None = None,
    as_of_at: datetime | None = None,
) -> dict[str, Any]:
    kind_norm = _normalize_kind(kind)
    subject_row = _subject_row_or_create(session, subject=subject)
    asset = _asset_for_subject(session, subject_row=subject_row, kind=kind_norm, create=True)
    assert asset is not None
    draft = _draft_version(session, int(asset.id))
    now = utcnow()
    if draft is None:
        draft = KnowledgeAssetVersion(
            asset_id=int(asset.id),
            version=0,
            status="draft",
            title=title or asset.title,
            summary=summary,
            content_md=content_md or "",
            content_json=dict(content_json or {}),
            coverage_json=dict(coverage_json or {}),
            evidence_bundle=[dict(item) for item in (evidence_bundle or []) if isinstance(item, dict)],
            ai_metadata=dict(ai_metadata or {}),
            as_of_at=as_of_at or now,
            created_at=now,
            updated_at=now,
            published_at=None,
        )
        session.add(draft)
        session.flush()
    else:
        if title is not None:
            draft.title = str(title).strip() or asset.title
        if summary is not None:
            draft.summary = str(summary).strip() or None
        if content_md is not None:
            draft.content_md = str(content_md)
        if content_json is not None:
            draft.content_json = dict(content_json)
        if coverage_json is not None:
            draft.coverage_json = dict(coverage_json)
        if evidence_bundle is not None:
            draft.evidence_bundle = [dict(item) for item in evidence_bundle if isinstance(item, dict)]
        if ai_metadata is not None:
            draft.ai_metadata = dict(ai_metadata)
        draft.as_of_at = as_of_at or draft.as_of_at or now
        draft.updated_at = now
    asset.title = str(title).strip() if title is not None and str(title).strip() else asset.title
    asset.summary = str(summary).strip() or None if summary is not None else asset.summary
    if stale_after_days is not None:
        asset.stale_after_days = int(stale_after_days)
    asset.updated_at = now
    if links is not None:
        _replace_links(session, asset=asset, links=links)
    session.flush()
    return serialize_knowledge_asset(asset, subject_row=subject_row, draft=draft, published=_latest_published_version(session, int(asset.id)))


def _maybe_create_gap_request(session: Session, *, subject_row: ResearchSubject, published_asset: KnowledgeAsset) -> None:
    opposite = "teardown" if published_asset.kind == "primer" else "primer"
    existing = (
        session.query(KnowledgeAsset)
        .filter(
            KnowledgeAsset.subject_id == int(subject_row.id),
            KnowledgeAsset.kind == opposite,
            KnowledgeAsset.latest_published_version_id.isnot(None),
        )
        .first()
    )
    if existing is not None:
        return
    create_instrument_request(
        session,
        title=f"{subject_row.display_name} {opposite.title()} Gap",
        intent="knowledge_asset_gap",
        summary=f"Published {published_asset.kind} exists but no published {opposite} exists for {subject_row.display_name}.",
        target_entities=[subject_row.display_name] if subject_row.kind == "company" else None,
        target_themes=[subject_row.display_name] if subject_row.kind != "company" else None,
        desired_outputs=[opposite],
        evidence_bundle=[
            {
                "label": published_asset.title,
                "source": "knowledge_asset",
                "object_type": "knowledge_asset",
                "object_id": int(published_asset.id),
            }
        ],
        provenance_summary="Auto-created after publishing a complementary knowledge asset.",
        evidence_tier="internal",
        status="draft",
        priority=6,
        automation_source="knowledge_publish",
        source_kind="knowledge_asset_gap",
        linked_objects={"subject_id": int(subject_row.id), "knowledge_asset_id": int(published_asset.id)},
    )


def publish_knowledge_asset(
    session: Session,
    *,
    subject: str | int,
    kind: str,
) -> dict[str, Any]:
    kind_norm = _normalize_kind(kind)
    subject_row = _subject_row_or_create(session, subject=subject)
    asset = _asset_for_subject(session, subject_row=subject_row, kind=kind_norm, create=False)
    if asset is None:
        raise ValueError("Knowledge asset not found.")
    draft = _draft_version(session, int(asset.id))
    if draft is None:
        raise ValueError("No draft exists to publish.")
    latest = _latest_published_version(session, int(asset.id))
    next_version = int(latest.version) + 1 if latest is not None else 1
    now = utcnow()
    published = KnowledgeAssetVersion(
        asset_id=int(asset.id),
        version=next_version,
        status="published",
        title=draft.title,
        summary=draft.summary,
        content_md=draft.content_md,
        content_json=dict(draft.content_json or {}),
        coverage_json=dict(draft.coverage_json or {}),
        evidence_bundle=[dict(item) for item in (draft.evidence_bundle or []) if isinstance(item, dict)],
        ai_metadata=dict(draft.ai_metadata or {}),
        as_of_at=draft.as_of_at or now,
        created_at=now,
        updated_at=now,
        published_at=now,
    )
    session.add(published)
    session.flush()
    asset.latest_published_version_id = int(published.id)
    asset.updated_at = now
    _maybe_create_gap_request(session, subject_row=subject_row, published_asset=asset)
    session.flush()
    return serialize_knowledge_asset(asset, subject_row=subject_row, draft=draft, published=published)


def generate_knowledge_asset_draft(
    session: Session,
    *,
    subject: str | int,
    kind: str,
    instructions: str | None = None,
    window_days: int | None = None,
) -> dict[str, Any]:
    kind_norm = _normalize_kind(kind)
    resolved = resolve_report_subject(session, subject)
    subject_row = _subject_row_or_create(session, subject=subject)
    report_payload, _meta = assemble_report_payload(session, subject=resolved, report=None, window_days=window_days)
    sections = report_payload.get("section_order") or []
    heuristic_md = "\n".join(
        [
            f"# {_asset_title(subject_row, kind=kind_norm)}",
            "",
            f"As of {report_payload.get('computed_at') or 'now'}",
            "",
            "## Why This Matters",
            report_payload.get("identity_freshness", {}).get("description") or subject_row.description or f"{subject_row.display_name} matters because it appears repeatedly across linked Thoth research.",
            "",
            "## Existing Coverage",
            f"Compiled from sections: {', '.join(str(item) for item in sections[:8])}",
            "",
            "## Key Inputs",
            "- Theses, notes, primary sources, and linked datasets from the current research graph.",
            "- Supply-chain context and reviewed neighbor subjects where available.",
            "",
            "## Working Draft",
            "Expand this into a published-quality long-form asset before publishing.",
        ]
    ).strip()
    title = _asset_title(subject_row, kind=kind_norm)
    summary = f"AI-assisted {kind_norm} draft for {subject_row.display_name}."
    content_json: dict[str, Any] = {
        "subject": serialize_research_subject(subject_row),
        "kind": kind_norm,
        "sections": [
            {"heading": "Why This Matters", "body": report_payload.get("identity_freshness", {}).get("description") or subject_row.description or ""},
            {"heading": "Coverage Snapshot", "body": report_payload.get("coverage_panel", {}).get("gaps") or []},
            {"heading": "Source Inputs", "body": sections},
        ],
    }
    ai_metadata = {"mode": "heuristic", "instructions": str(instructions or "").strip() or None}

    if llm_available(provider=getattr(settings, "ai_provider", "openai")):
        prompt = (
            "Return JSON only with keys title, summary, markdown, content_json, coverage_json, evidence_bundle. "
            "Write a dense but clean draft for a Thoth long-form research asset. "
            "Do not fabricate facts. Cite evidence in markdown as short inline references where possible.\n\n"
            f"Asset kind: {kind_norm}\n"
            + (f"Instructions: {instructions}\n\n" if instructions and str(instructions).strip() else "")
            + str(report_payload)[:22000]
        )
        try:
            parsed = manifested_chat_json(
                session,
                run_kind="research_subject",
                run_id=int(subject_row.id),
                task_kind=f"knowledge_asset_draft:{kind_norm}",
                messages=[
                    ChatMessage(role="system", content="You draft source-backed primers and teardowns for an investment research terminal."),
                    ChatMessage(role="user", content=prompt),
                ],
                provider=getattr(settings, "ai_provider", "openai"),
                timeout_s=90.0,
                max_input_chars=24000,
                prompt_cache_key="thoth-knowledge-asset-draft-v1",
                permission_profile="draft-only",
            )
            title = str(parsed.get("title") or title).strip() or title
            summary = str(parsed.get("summary") or summary).strip() or summary
            markdown_text = str(parsed.get("markdown") or "").strip()
            if markdown_text:
                heuristic_md = markdown_text
            if isinstance(parsed.get("content_json"), dict):
                content_json = dict(parsed.get("content_json") or {})
            coverage_json = dict(parsed.get("coverage_json") or {}) if isinstance(parsed.get("coverage_json"), dict) else {}
            evidence_bundle = [dict(item) for item in (parsed.get("evidence_bundle") or []) if isinstance(item, dict)]
            ai_metadata = {
                "mode": "llm",
                "model": active_llm_model(getattr(settings, "ai_provider", "openai")),
                "instructions": str(instructions or "").strip() or None,
            }
            return save_knowledge_asset_draft(
                session,
                subject=subject_row.subject_key,
                kind=kind_norm,
                title=title,
                summary=summary,
                content_md=heuristic_md,
                content_json=content_json,
                coverage_json=coverage_json,
                evidence_bundle=evidence_bundle,
                ai_metadata=ai_metadata,
                as_of_at=utcnow(),
            )
        except AgentLlmError:
            pass

    return save_knowledge_asset_draft(
        session,
        subject=subject_row.subject_key,
        kind=kind_norm,
        title=title,
        summary=summary,
        content_md=heuristic_md,
        content_json=content_json,
        coverage_json={"source_sections": sections},
        evidence_bundle=[],
        ai_metadata=ai_metadata,
        as_of_at=utcnow(),
    )
