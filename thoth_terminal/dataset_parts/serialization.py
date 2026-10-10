from __future__ import annotations

import csv
import hashlib
import json
import re
from datetime import date, datetime
from io import BytesIO, StringIO
from threading import RLock
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from thoth_terminal.db import utcnow
from thoth_terminal.dataset_catalog import (
    DATASET_FAMILY_COLUMN_TEMPLATES,
    DATASET_FAMILY_MATERIALIZER_FAMILIES,
    DATASET_FAMILY_META,
    DATASET_FAMILY_REQUIRED_ROLES,
    DATASET_OBJECT_TYPES,
    DATASET_VIEW_OPERATOR_VALUES,
    DATASET_VIEW_OPERATORS,
)
from thoth_terminal.models import (
    ChartDatasetView,
    Dataset,
    DatasetColumn,
    Document,
    DatasetLink,
    DatasetRow,
    DatasetVersion,
    DatasetView,
    InstrumentRequest,
    ProductStageEntry,
    StructuredDataFact,
)
from thoth_terminal.product_stage_service import serialize_product_stage_entry


def serialize_dataset_summaries(session: Session, datasets: list[Dataset]) -> dict[int, dict[str, Any]]:
    dataset_ids = [int(dataset.id) for dataset in datasets if dataset.id is not None]
    if not dataset_ids:
        return {}

    versions_by_dataset_status: dict[tuple[int, str], DatasetVersion] = {}
    latest_versions = (
        session.query(
            DatasetVersion.id.label("id"),
            func.row_number().over(
                partition_by=(DatasetVersion.dataset_id, DatasetVersion.status),
                order_by=(DatasetVersion.version.desc(), DatasetVersion.updated_at.desc(), DatasetVersion.id.desc()),
            ).label("position"),
        )
        .filter(DatasetVersion.dataset_id.in_(dataset_ids), DatasetVersion.status.in_(["draft", "published"]))
        .subquery()
    )
    for version in (
        session.query(DatasetVersion)
        .join(latest_versions, latest_versions.c.id == DatasetVersion.id)
        .filter(latest_versions.c.position == 1)
        .all()
    ):
        key = (int(version.dataset_id), str(version.status or ""))
        versions_by_dataset_status.setdefault(key, version)

    active_version_ids: list[int] = []
    active_by_dataset: dict[int, DatasetVersion] = {}
    for dataset_id in dataset_ids:
        draft = versions_by_dataset_status.get((dataset_id, "draft"))
        published = versions_by_dataset_status.get((dataset_id, "published"))
        active = draft or published
        if active is not None:
            active_by_dataset[dataset_id] = active
            active_version_ids.append(int(active.id))

    row_counts = (
        {
            int(version_id): int(count)
            for version_id, count in session.query(DatasetRow.dataset_version_id, func.count())
            .filter(DatasetRow.dataset_version_id.in_(active_version_ids))
            .group_by(DatasetRow.dataset_version_id)
            .all()
        }
        if active_version_ids
        else {}
    )
    col_counts = (
        {
            int(version_id): int(count)
            for version_id, count in session.query(DatasetColumn.dataset_version_id, func.count())
            .filter(DatasetColumn.dataset_version_id.in_(active_version_ids))
            .group_by(DatasetColumn.dataset_version_id)
            .all()
        }
        if active_version_ids
        else {}
    )
    view_counts = {
        int(dataset_id): int(count)
        for dataset_id, count in session.query(DatasetView.dataset_id, func.count())
        .filter(DatasetView.dataset_id.in_(dataset_ids))
        .group_by(DatasetView.dataset_id)
        .all()
    }

    links_by_dataset: dict[int, list[DatasetLink]] = {dataset_id: [] for dataset_id in dataset_ids}
    request_ids: set[int] = set()
    stage_ids: set[int] = set()
    for link in session.query(DatasetLink).filter(DatasetLink.dataset_id.in_(dataset_ids)).all():
        dataset_id = int(link.dataset_id)
        links_by_dataset.setdefault(dataset_id, []).append(link)
        object_type = str(link.object_type or "").strip().lower()
        if object_type == "instrument_request":
            request_ids.add(int(link.object_id))
        elif object_type == "product_stage":
            stage_ids.add(int(link.object_id))

    requests_by_id = {
        int(row.id): row
        for row in (
            session.query(InstrumentRequest)
            .filter(InstrumentRequest.id.in_(sorted(request_ids)))
            .order_by(InstrumentRequest.updated_at.desc())
            .all()
            if request_ids
            else []
        )
    }
    stages_by_id = {
        int(row.id): row
        for row in (
            session.query(ProductStageEntry)
            .filter(ProductStageEntry.id.in_(sorted(stage_ids)))
            .order_by(ProductStageEntry.updated_at.desc())
            .all()
            if stage_ids
            else []
        )
    }

    out: dict[int, dict[str, Any]] = {}
    for dataset in datasets:
        if dataset.id is None:
            continue
        dataset_id = int(dataset.id)
        draft = versions_by_dataset_status.get((dataset_id, "draft"))
        published = versions_by_dataset_status.get((dataset_id, "published"))
        active = active_by_dataset.get(dataset_id)
        active_version_id = int(active.id) if active is not None else None
        links = links_by_dataset.get(dataset_id, [])
        linked_requests = []
        linked_stage_entries = []
        for link in links:
            object_type = str(link.object_type or "").strip().lower()
            if object_type == "instrument_request":
                row = requests_by_id.get(int(link.object_id))
                if row is not None:
                    linked_requests.append(
                        {
                            "id": int(row.id),
                            "title": row.title,
                            "status": row.status,
                            "priority": int(row.priority or 0),
                        }
                    )
            elif object_type == "product_stage":
                row = stages_by_id.get(int(link.object_id))
                if row is not None:
                    linked_stage_entries.append(
                        {
                            "id": int(row.id),
                            "company_name": row.company_name,
                            "product_name": row.product_name,
                            "stage": row.stage,
                            "status": row.status,
                            "evidence_tier": row.evidence_tier,
                        }
                    )

        extra = dict(dataset.extra or {})
        family_meta = dataset_family_meta(dataset.family)
        out[dataset_id] = {
            "id": dataset_id,
            "slug": dataset.slug,
            "title": dataset.title,
            "display_title": dataset_display_title(dataset.title),
            "description": dataset.description,
            "category": dataset.category,
            "family": dataset.family,
            "family_label": family_meta["label"],
            "family_summary": family_meta["summary"],
            "family_column_template": dataset_family_column_template(dataset.family),
            "status": dataset.status,
            "source_kind": dataset.source_kind,
            "created_at": dataset.created_at.isoformat() if dataset.created_at else None,
            "updated_at": dataset.updated_at.isoformat() if dataset.updated_at else None,
            "counts": {
                "columns": int(col_counts.get(active_version_id or -1, 0)),
                "rows": int(row_counts.get(active_version_id or -1, 0)),
                "views": int(view_counts.get(dataset_id, 0)),
                "links": len(links),
            },
            "draft_version": _dataset_version_payload(draft),
            "published_version": _dataset_version_payload(published),
            "provenance_summary": str(extra.get("provenance_summary") or "").strip() or None,
            "evidence_tier_summary": str(extra.get("evidence_tier_summary") or "").strip() or None,
            "linked_requests": linked_requests,
            "linked_stage_entries": linked_stage_entries,
            "extra": extra,
        }
    return out


def serialize_dataset_summary(session: Session, dataset: Dataset) -> dict[str, Any]:
    return serialize_dataset_summaries(session, [dataset]).get(int(dataset.id), {})
