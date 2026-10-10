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
from thoth_terminal.dataset_storage import canonical_dataset, copy_version_memberships, replace_version_rows, version_fingerprint
from thoth_terminal.dataset_mirror_cache import mark_dataset_mirror_dirty


def latest_dataset_version(session: Session, dataset_id: int, *, status: str | None = None) -> DatasetVersion | None:
    if status in {"draft", "published"}:
        dataset = session.get(Dataset, int(dataset_id))
        if dataset is not None:
            dataset_id = int(canonical_dataset(session, dataset).id)
    q = session.query(DatasetVersion).filter_by(dataset_id=int(dataset_id))
    if status:
        q = q.filter_by(status=status)
    return q.order_by(DatasetVersion.version.desc(), DatasetVersion.updated_at.desc()).first()


def _next_dataset_version_num(session: Session, dataset_id: int) -> int:
    latest = latest_dataset_version(session, dataset_id, status="published")
    return int(latest.version if latest else 0) + 1


def _normalize_column_key(key: str, *, fallback: str) -> str:
    return _slugify(key, default=fallback).replace("-", "_")


def _coerce_scalar(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    raw = str(value).strip()
    if raw == "":
        return ""
    lower = raw.lower()
    if lower in {"true", "false"}:
        return lower == "true"
    try:
        if "." in raw:
            return float(raw)
        return int(raw)
    except Exception:
        pass
    try:
        return date.fromisoformat(raw).isoformat()
    except Exception:
        return raw


def _infer_data_type(values: list[Any]) -> str:
    non_empty = [v for v in values if v not in (None, "")]
    if not non_empty:
        return "text"
    if all(isinstance(v, bool) for v in non_empty):
        return "bool"
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in non_empty):
        return "number"
    if all(isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) for v in non_empty):
        return "date"
    return "text"


def _normalize_columns_and_rows(
    columns: list[dict[str, Any]] | None,
    rows: list[dict[str, Any]] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    row_list = [dict(row) for row in (rows or []) if isinstance(row, dict)]
    if columns:
        out_cols: list[dict[str, Any]] = []
        for idx, col in enumerate(columns):
            if not isinstance(col, dict):
                continue
            label = str(col.get("label") or col.get("name") or col.get("key") or f"column_{idx + 1}").strip()
            key = _normalize_column_key(str(col.get("key") or label), fallback=f"column_{idx + 1}")
            out_cols.append(
                {
                    "key": key,
                    "label": label,
                    "data_type": str(col.get("data_type") or "text"),
                    "role": str(col.get("role") or "") or None,
                    "config": dict(col.get("config") or {}),
                }
            )
        normalized_rows = []
        for idx, row in enumerate(row_list):
            source_values = row.get("values") if isinstance(row.get("values"), dict) else row
            values = {}
            for col in out_cols:
                raw = source_values.get(col["key"], source_values.get(col["label"], row.get(col["key"], row.get(col["label"], ""))))
                coerced, _error = _coerce_value_for_column(col, raw)
                values[col["key"]] = coerced
            normalized_rows.append({"row_key": str(row.get("row_key") or idx + 1), "values": values})
        return out_cols, normalized_rows

    key_order: list[str] = []
    for row in row_list:
        for key in row.keys():
            normalized = _normalize_column_key(str(key), fallback="column")
            if normalized not in key_order:
                key_order.append(normalized)
    column_defs = []
    for idx, key in enumerate(key_order):
        values = [_coerce_scalar(row.get(key, row.get(key.replace("_", " "), ""))) for row in row_list]
        column_defs.append(
            {
                "key": key,
                "label": key.replace("_", " ").title(),
                "data_type": _infer_data_type(values),
                "role": "timestamp" if key in {"date", "period_date", "period"} else ("metric" if _infer_data_type(values) == "number" else "dimension"),
                "config": {},
            }
        )
    normalized_rows = []
    for idx, row in enumerate(row_list):
        values = {}
        for col in column_defs:
            raw = row.get(col["key"])
            if raw is None:
                raw = row.get(col["label"])
            values[col["key"]] = _coerce_scalar(raw)
        normalized_rows.append({"row_key": str(row.get("row_key") or idx + 1), "values": values})
    return column_defs, normalized_rows


def create_dataset(
    session: Session,
    *,
    title: str,
    description: str | None = None,
    category: str = "general",
    family: str = "general",
    status: str = "active",
    source_kind: str | None = None,
    slug: str | None = None,
    columns: list[dict[str, Any]] | None = None,
    rows: list[dict[str, Any]] | None = None,
    label: str | None = None,
    extra: dict[str, Any] | None = None,
    source_event: str | None = None,
) -> Dataset:
    ensure_dataset_tables(session)
    now = utcnow()
    dataset_family = (family or category or "general").strip() or "general"
    default_columns = columns
    if default_columns is None and not rows:
        default_columns = dataset_family_column_template(dataset_family)
    dataset = Dataset(
        slug=_dataset_unique_slug(session, title, slug=slug),
        title=title.strip() or "Untitled Dataset",
        description=(description or "").strip() or None,
        category=(category or "general").strip() or "general",
        family=dataset_family,
        status=(status or "active").strip() or "active",
        source_kind=(source_kind or "").strip() or None,
        created_at=now,
        updated_at=now,
        extra=dict(extra or {}),
    )
    session.add(dataset)
    session.flush()
    replace_dataset_draft(
        session,
        dataset,
        columns=default_columns or [],
        rows=rows or [],
        label=label,
        extra=extra,
        source_event=source_event,
    )
    return dataset


def ensure_dataset_draft(session: Session, dataset: Dataset) -> DatasetVersion:
    dataset = canonical_dataset(session, dataset)
    draft = latest_dataset_version(session, int(dataset.id), status="draft")
    if draft:
        return draft
    published = latest_dataset_version(session, int(dataset.id), status="published")
    now = utcnow()
    draft = DatasetVersion(
        dataset_id=int(dataset.id),
        version=0,
        status="draft",
        label=published.label if published is not None else None,
        source_event=published.source_event if published is not None else None,
        created_at=now,
        updated_at=now,
        extra=dict(published.extra or {}) if published is not None else {},
    )
    session.add(draft)
    session.flush()
    if published is not None:
        copy_version_memberships(session, int(published.id), int(draft.id), now)
        mark_dataset_mirror_dirty(session, int(draft.id), replace=True, columns_changed=True)
        session.flush()
    return draft


def replace_dataset_draft(
    session: Session,
    dataset: Dataset,
    *,
    columns: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    label: str | None = None,
    extra: dict[str, Any] | None = None,
    source_event: str | None = None,
) -> DatasetVersion:
    dataset = canonical_dataset(session, dataset)
    draft = ensure_dataset_draft(session, dataset)
    now = utcnow()
    normalized_columns, normalized_rows = _normalize_columns_and_rows(columns, rows)
    old_columns = list(session.query(DatasetColumn).filter_by(dataset_version_id=int(draft.id)).order_by(DatasetColumn.sort_order, DatasetColumn.id))
    old_schema = [{"key": c.key,"label": c.label,"data_type": c.data_type,"role": c.role,"config": c.config} for c in old_columns]
    schema_changed = old_schema != normalized_columns
    if schema_changed:
        for column in old_columns:
            session.delete(column)
        session.flush()
    draft.label = label
    draft.source_event = source_event
    draft.updated_at = now
    if extra is not None:
        draft.extra = dict(extra)
    for idx, col in enumerate(normalized_columns if schema_changed else []):
        session.add(
            DatasetColumn(
                dataset_version_id=int(draft.id),
                key=col["key"],
                label=col["label"],
                data_type=col["data_type"],
                role=col.get("role"),
                sort_order=idx,
                config=dict(col.get("config") or {}),
            )
        )
    changed_keys = replace_version_rows(session, int(draft.id), normalized_rows, now)
    dataset.updated_at = now
    session.add(dataset)
    session.add(draft)
    session.flush()
    if changed_keys or schema_changed:
        mark_dataset_mirror_dirty(session, int(draft.id), row_keys=changed_keys, columns_changed=schema_changed)

    return draft


def publish_dataset_snapshot(session: Session, dataset: Dataset, *, label: str | None = None) -> DatasetVersion:
    dataset = canonical_dataset(session, dataset)
    if session.get_bind().dialect.name == "postgresql":
        session.query(Dataset.id).filter_by(id=int(dataset.id)).with_for_update().one()
    draft = ensure_dataset_draft(session, dataset)
    now = utcnow()
    session.flush()
    fingerprint = version_fingerprint(session, int(draft.id))
    previous = latest_dataset_version(session, int(dataset.id), status="published")
    if previous is not None and version_fingerprint(session, int(previous.id)) == fingerprint:
        dataset.extra = {**dict(dataset.extra or {}), "last_publish_check": {
            "version_id": int(previous.id), "checked_at": now.isoformat(), "label": label,
        }}
        session.flush()
        return previous
    version = DatasetVersion(
        dataset_id=int(dataset.id),
        version=_next_dataset_version_num(session, int(dataset.id)),
        status="published",
        label=label,
        source_event=draft.source_event,
        created_at=now,
        updated_at=now,
        extra={**dict(draft.extra or {}), "content_fingerprint": fingerprint},
    )
    session.add(version)
    session.flush()
    copy_version_memberships(session, int(draft.id), int(version.id), now)
    dataset.updated_at = now
    session.add(dataset)
    session.flush()
    mark_dataset_mirror_dirty(session, int(version.id), replace=True, columns_changed=True)
    return version


def serialize_dataset(
    session: Session,
    dataset: Dataset,
    *,
    version: DatasetVersion | None = None,
    include_rows: bool = True,
    view: DatasetView | None = None,
    limit: int | None = None,
    analysis_limit: int | None = None,
) -> dict[str, Any]:
    if version is None:
        dataset = canonical_dataset(session, dataset)
    resolved = version or latest_dataset_version(session, int(dataset.id), status="draft") or latest_dataset_version(session, int(dataset.id), status="published")
    base = serialize_dataset_summary(session, dataset)
    base["version"] = _dataset_version_payload(resolved)
    if not resolved:
        base["columns"] = []
        base["rows"] = []
        base["views"] = []
        return base
    columns = session.query(DatasetColumn).filter_by(dataset_version_id=int(resolved.id)).order_by(DatasetColumn.sort_order.asc()).all()
    base["columns"] = [
        {
            "key": col.key,
            "label": col.label,
            "data_type": col.data_type,
            "role": col.role,
            "sort_order": int(col.sort_order or 0),
            "config": dict(col.config or {}),
        }
        for col in columns
    ]
    base["counts"]["rows"] = int(session.query(func.count(DatasetRow.id)).filter_by(dataset_version_id=int(resolved.id)).scalar() or 0)
    base["counts"]["columns"] = len(columns)
    views = session.query(DatasetView).filter_by(dataset_id=int(dataset.id)).order_by(DatasetView.name.asc()).all()
    base["views"] = [
        {
            "id": int(v.id),
            "slug": v.slug,
            "name": v.name,
            "description": v.description,
            "filters": dict(v.filters or {}),
            "columns": list(v.columns or []),
            "sort_spec": list(v.sort_spec or []),
            "extra": dict(v.extra or {}),
        }
        for v in views
    ]
    bounded_analysis_limit = max(1, min(int(analysis_limit), 5000)) if analysis_limit is not None else None
    analysis_rows = query_dataset(
        session,
        dataset_id=int(dataset.id),
        version_id=int(resolved.id),
        view_id=int(view.id) if view else None,
        limit=bounded_analysis_limit,
    )["rows"]
    base["profile"] = build_dataset_profile(base["columns"], analysis_rows)
    base["dashboard"] = build_dataset_dashboard(session, dataset, base["columns"], analysis_rows)
    source_total_rows = int(((base.get("counts") or {}).get("rows") or len(analysis_rows)))
    analysis_sampled = bool(
        bounded_analysis_limit is not None
        and (source_total_rows > len(analysis_rows) if view is None else len(analysis_rows) >= bounded_analysis_limit)
    )
    if bounded_analysis_limit is not None:
        if view is None:
            base["profile"]["row_count"] = source_total_rows
        base["profile"]["source_row_count"] = source_total_rows
        base["profile"]["analysis_row_count"] = len(analysis_rows)
        base["profile"]["analysis_limit"] = bounded_analysis_limit
        base["profile"]["analysis_sampled"] = analysis_sampled
        base["dashboard"]["analysis_sampled"] = analysis_sampled
        base["dashboard"]["analysis_row_count"] = len(analysis_rows)
        base["dashboard"]["source_row_count"] = source_total_rows
    base["capabilities"] = dataset_capabilities(dataset)
    base["view_builder"] = build_dataset_view_builder(base["columns"], base["views"], active_view=view, family=dataset.family)
    if include_rows:
        if limit is not None:
            row_limit = max(1, min(int(limit), 5000))
            if bounded_analysis_limit is not None and row_limit <= bounded_analysis_limit:
                base["rows"] = analysis_rows[:row_limit]
            else:
                base["rows"] = query_dataset(
                    session,
                    dataset_id=int(dataset.id),
                    version_id=int(resolved.id),
                    view_id=int(view.id) if view else None,
                    limit=row_limit,
                )["rows"]
        else:
            base["rows"] = analysis_rows
    else:
        base["rows"] = []
    base["links"] = [
        {
            "id": int(link.id),
            "object_type": link.object_type,
            "object_id": int(link.object_id),
            "relation": link.relation,
        }
        for link in session.query(DatasetLink).filter_by(dataset_id=int(dataset.id)).all()
    ]
    return base


def update_dataset_metadata(
    session: Session,
    dataset: Dataset,
    *,
    title: str | None = None,
    description: str | None = None,
    category: str | None = None,
    family: str | None = None,
    status: str | None = None,
    source_kind: str | None = None,
    provenance_summary: str | None = None,
    evidence_tier_summary: str | None = None,
    extra_patch: dict[str, Any] | None = None,
) -> Dataset:
    dataset = canonical_dataset(session, dataset)
    if title is not None:
        dataset.title = str(title).strip() or dataset.title
    if description is not None:
        dataset.description = str(description).strip() or None
    if category is not None:
        dataset.category = str(category).strip() or dataset.category
    if family is not None:
        dataset.family = str(family).strip() or dataset.family
    if status is not None:
        dataset.status = str(status).strip() or dataset.status
    if source_kind is not None:
        dataset.source_kind = str(source_kind).strip() or None
    extra = dict(dataset.extra or {})
    if provenance_summary is not None:
        value = str(provenance_summary).strip()
        if value:
            extra["provenance_summary"] = value
        else:
            extra.pop("provenance_summary", None)
    if evidence_tier_summary is not None:
        value = str(evidence_tier_summary).strip()
        if value:
            extra["evidence_tier_summary"] = value
        else:
            extra.pop("evidence_tier_summary", None)
    if isinstance(extra_patch, dict):
        for key, value in extra_patch.items():
            if value is None:
                extra.pop(str(key), None)
            else:
                extra[str(key)] = value
    dataset.extra = extra
    dataset.updated_at = utcnow()
    session.add(dataset)
    session.flush()
    return dataset


def create_dataset_view(
    session: Session,
    dataset: Dataset,
    *,
    name: str,
    slug: str | None = None,
    description: str | None = None,
    filters: dict[str, Any] | None = None,
    columns: list[str] | None = None,
    sort_spec: list[dict[str, Any]] | None = None,
    extra: dict[str, Any] | None = None,
) -> DatasetView:
    dataset = canonical_dataset(session, dataset)
    ensure_dataset_tables(session)
    now = utcnow()
    slug_value = _slugify(slug or name, default="view")
    existing = session.query(DatasetView).filter_by(dataset_id=int(dataset.id), slug=slug_value).first()
    if existing:
        existing.name = name
        existing.description = description
        existing.filters = dict(filters or {})
        existing.columns = list(columns or [])
        existing.sort_spec = list(sort_spec or [])
        existing.updated_at = now
        existing.extra = dict(extra or {})
        session.add(existing)
        session.flush()
        return existing
    view = DatasetView(
        dataset_id=int(dataset.id),
        slug=slug_value,
        name=name,
        description=description,
        filters=dict(filters or {}),
        columns=list(columns or []),
        sort_spec=list(sort_spec or []),
        created_at=now,
        updated_at=now,
        extra=dict(extra or {}),
    )
    session.add(view)
    session.flush()
    return view
