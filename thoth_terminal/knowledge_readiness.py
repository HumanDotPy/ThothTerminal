from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from thoth_terminal.models import (
    DatasetLink,
    KnowledgeAsset,
    KnowledgeAssetLink,
    KnowledgeAssetVersion,
    ResearchSubject,
    SupplyChainMetricBinding,
    SupplyChainSnapshot,
)


def _latest_version(session: Session, asset_id: int, *, status: str | None = None) -> KnowledgeAssetVersion | None:
    q = session.query(KnowledgeAssetVersion).filter(KnowledgeAssetVersion.asset_id == int(asset_id))
    if status:
        q = q.filter(KnowledgeAssetVersion.status == status)
    return q.order_by(KnowledgeAssetVersion.updated_at.desc(), KnowledgeAssetVersion.id.desc()).first()


def _age_days(value: datetime | None) -> int | None:
    if value is None:
        return None
    now = datetime.now(timezone.utc)
    dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return max(0, int((now - dt).total_seconds() // 86400))


def _clamp(value: float) -> int:
    return max(0, min(100, int(round(value))))


def _latest_versions_by_asset(
    session: Session,
    asset_ids: list[int],
    *,
    status: str,
) -> dict[int, KnowledgeAssetVersion]:
    if not asset_ids:
        return {}
    versions: dict[int, KnowledgeAssetVersion] = {}
    for row in (
        session.query(KnowledgeAssetVersion)
        .filter(KnowledgeAssetVersion.asset_id.in_(asset_ids), KnowledgeAssetVersion.status == status)
        .order_by(
            KnowledgeAssetVersion.asset_id.asc(),
            KnowledgeAssetVersion.updated_at.desc(),
            KnowledgeAssetVersion.id.desc(),
        )
        .all()
    ):
        asset_id = int(row.asset_id)
        if asset_id not in versions:
            versions[asset_id] = row
    return versions


def _links_by_asset(session: Session, asset_ids: list[int]) -> dict[int, list[KnowledgeAssetLink]]:
    if not asset_ids:
        return {}
    grouped: dict[int, list[KnowledgeAssetLink]] = defaultdict(list)
    for row in (
        session.query(KnowledgeAssetLink)
        .filter(KnowledgeAssetLink.asset_id.in_(asset_ids))
        .order_by(KnowledgeAssetLink.asset_id.asc(), KnowledgeAssetLink.id.asc())
        .all()
    ):
        grouped[int(row.asset_id)].append(row)
    return dict(grouped)


def _dataset_link_counts(session: Session, dataset_ids: set[int]) -> dict[int, int]:
    if not dataset_ids:
        return {}
    return {
        int(dataset_id): int(count)
        for dataset_id, count in (
            session.query(DatasetLink.dataset_id, func.count(DatasetLink.id))
            .filter(DatasetLink.dataset_id.in_(sorted(dataset_ids)))
            .group_by(DatasetLink.dataset_id)
            .all()
        )
    }


def _chain_metric_counts(session: Session, chain_ids: set[int]) -> dict[int, int]:
    if not chain_ids:
        return {}
    return {
        int(chain_id): int(count)
        for chain_id, count in (
            session.query(SupplyChainSnapshot.chain_id, func.count(SupplyChainMetricBinding.id))
            .join(SupplyChainMetricBinding, SupplyChainMetricBinding.snapshot_id == SupplyChainSnapshot.id)
            .filter(SupplyChainSnapshot.chain_id.in_(sorted(chain_ids)))
            .group_by(SupplyChainSnapshot.chain_id)
            .all()
        )
    }


def _score_knowledge_asset(
    asset: KnowledgeAsset,
    *,
    draft: KnowledgeAssetVersion | None,
    published: KnowledgeAssetVersion | None,
    links: list[KnowledgeAssetLink],
    subject: ResearchSubject | None,
    dataset_link_counts: dict[int, int],
    chain_metric_counts: dict[int, int],
) -> dict[str, Any]:
    version = published or draft
    evidence_bundle = list(version.evidence_bundle or []) if version else []
    content_md = str(version.content_md or "") if version else ""
    content_json = dict(version.content_json or {}) if version else {}
    coverage_json = dict(version.coverage_json or {}) if version else {}

    linked_docs = [link for link in links if str(link.object_type or "").lower() in {"document", "doc"}]
    linked_theses = [link for link in links if str(link.object_type or "").lower() == "thesis"]
    linked_datasets = [link for link in links if str(link.object_type or "").lower() == "dataset"]
    linked_chains = [link for link in links if str(link.object_type or "").lower() == "supply_chain"]
    linked_dataset_ids = {int(link.object_id) for link in linked_datasets}
    linked_chain_ids = {int(link.object_id) for link in linked_chains}

    dataset_link_count = sum(int(dataset_link_counts.get(dataset_id, 0)) for dataset_id in linked_dataset_ids)
    chain_metric_count = sum(int(chain_metric_counts.get(chain_id, 0)) for chain_id in linked_chain_ids)

    evidence_units = len(evidence_bundle) + len(linked_docs) + len(linked_theses)
    evidence_score = _clamp(min(100, evidence_units * 16 + (10 if linked_theses else 0)))
    content_sections = content_md.count("\n## ") + content_md.count("\n# ")
    content_score = _clamp(min(100, len(content_md) / 60 + content_sections * 8 + len(content_json) * 4))
    metric_score = _clamp(min(100, len(linked_datasets) * 18 + len(linked_chains) * 12 + dataset_link_count * 4 + chain_metric_count * 14))
    coverage_terms = len(coverage_json) + len([value for value in coverage_json.values() if value]) if coverage_json else 0
    linkage_score = _clamp(min(100, len(links) * 10 + coverage_terms * 6))
    age = _age_days(version.updated_at if version else asset.updated_at)
    stale_after = int(asset.stale_after_days or 180)
    freshness_score = 100 if age is None else _clamp(100 - max(0, age - stale_after // 2) * (100 / max(stale_after, 1)))

    overall = _clamp(
        evidence_score * 0.28
        + content_score * 0.24
        + metric_score * 0.20
        + linkage_score * 0.16
        + freshness_score * 0.12
    )
    gaps: list[str] = []
    if evidence_score < 45:
        gaps.append("Needs stronger evidence links or evidence bundle.")
    if content_score < 45:
        gaps.append("Looks like a scaffold or short draft.")
    if metric_score < 35:
        gaps.append("Needs linked datasets or supply-chain metric bindings.")
    if linkage_score < 35:
        gaps.append("Needs more object links into theses, notes, datasets, or chains.")
    if freshness_score < 45:
        gaps.append("Needs freshness review.")

    if overall >= 78 and evidence_score >= 55 and content_score >= 60:
        status = "publishable"
    elif overall >= 60:
        status = "ready_for_review"
    elif evidence_score < 35:
        status = "needs_evidence"
    else:
        status = "draft_shell"

    return {
        "asset": {
            "id": int(asset.id),
            "slug": asset.slug,
            "kind": asset.kind,
            "title": asset.title,
            "status": asset.status,
            "subject_id": int(asset.subject_id),
            "subject": subject.display_name if subject else None,
        },
        "status": status,
        "overall_score": overall,
        "scores": {
            "evidence": evidence_score,
            "content": content_score,
            "metrics": metric_score,
            "linkage": linkage_score,
            "freshness": freshness_score,
        },
        "counts": {
            "evidence_bundle": len(evidence_bundle),
            "links": len(links),
            "linked_documents": len(linked_docs),
            "linked_theses": len(linked_theses),
            "linked_datasets": len(linked_datasets),
            "linked_supply_chains": len(linked_chains),
            "supply_chain_metric_bindings": chain_metric_count,
        },
        "blocking_gaps": gaps,
        "version": {
            "id": int(version.id) if version else None,
            "status": version.status if version else None,
            "updated_at": version.updated_at.isoformat() if version and version.updated_at else None,
            "published_at": version.published_at.isoformat() if version and version.published_at else None,
        },
        "guidance": "Readiness is scoring/visibility for automation; it should not block a human-authored asset from being published intentionally.",
    }


def score_knowledge_asset(session: Session, asset: KnowledgeAsset) -> dict[str, Any]:
    links = session.query(KnowledgeAssetLink).filter(KnowledgeAssetLink.asset_id == int(asset.id)).all()
    linked_dataset_ids = {
        int(link.object_id)
        for link in links
        if str(link.object_type or "").lower() == "dataset"
    }
    linked_chain_ids = {
        int(link.object_id)
        for link in links
        if str(link.object_type or "").lower() == "supply_chain"
    }
    subject = session.query(ResearchSubject).filter_by(id=int(asset.subject_id)).first()
    return _score_knowledge_asset(
        asset,
        draft=_latest_version(session, int(asset.id), status="draft"),
        published=_latest_version(session, int(asset.id), status="published"),
        links=links,
        subject=subject,
        dataset_link_counts=_dataset_link_counts(session, linked_dataset_ids),
        chain_metric_counts=_chain_metric_counts(session, linked_chain_ids),
    )


def score_knowledge_assets(session: Session, assets: list[KnowledgeAsset]) -> list[dict[str, Any]]:
    asset_ids = [int(asset.id) for asset in assets]
    links_by_asset = _links_by_asset(session, asset_ids)
    linked_dataset_ids: set[int] = set()
    linked_chain_ids: set[int] = set()
    for links in links_by_asset.values():
        for link in links:
            link_type = str(link.object_type or "").lower()
            if link_type == "dataset":
                linked_dataset_ids.add(int(link.object_id))
            elif link_type == "supply_chain":
                linked_chain_ids.add(int(link.object_id))
    subject_ids = {int(asset.subject_id) for asset in assets}
    subjects = (
        {
            int(row.id): row
            for row in session.query(ResearchSubject)
            .filter(ResearchSubject.id.in_(subject_ids))
            .all()
        }
        if subject_ids
        else {}
    )
    drafts = _latest_versions_by_asset(session, asset_ids, status="draft")
    published = _latest_versions_by_asset(session, asset_ids, status="published")
    dataset_counts = _dataset_link_counts(session, linked_dataset_ids)
    chain_counts = _chain_metric_counts(session, linked_chain_ids)
    return [
        _score_knowledge_asset(
            asset,
            draft=drafts.get(int(asset.id)),
            published=published.get(int(asset.id)),
            links=links_by_asset.get(int(asset.id), []),
            subject=subjects.get(int(asset.subject_id)),
            dataset_link_counts=dataset_counts,
            chain_metric_counts=chain_counts,
        )
        for asset in assets
    ]


def list_knowledge_readiness(
    session: Session,
    *,
    query: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    q = session.query(KnowledgeAsset)
    query_text = str(query or "").strip()
    if query_text:
        pattern = f"%{query_text}%"
        q = q.filter(or_(KnowledgeAsset.title.ilike(pattern), KnowledgeAsset.summary.ilike(pattern), KnowledgeAsset.slug.ilike(pattern)))
    rows = q.order_by(KnowledgeAsset.updated_at.desc(), KnowledgeAsset.id.desc()).limit(max(1, min(int(limit or 50), 200))).all()
    readiness = score_knowledge_assets(session, rows)
    return {"count": len(readiness), "readiness": readiness}
