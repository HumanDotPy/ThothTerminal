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
from thoth_terminal.dataset_parts.core import dataset_family_column_template, dataset_family_meta, ensure_dataset_tables
from thoth_terminal.dataset_parts.draft_rows import upsert_dataset_draft_row
from thoth_terminal.dataset_parts.links import link_dataset
from thoth_terminal.dataset_parts.materializer_alias_catalog import _family_materializer_alias_value
from thoth_terminal.dataset_parts.materializer_defaults import _family_materializer_apply_defaults
from thoth_terminal.dataset_parts.materializer_values import _family_materializer_source_values
from thoth_terminal.dataset_parts.mutations import create_dataset, ensure_dataset_draft, replace_dataset_draft
from thoth_terminal.product_stage_service import serialize_product_stage_entry


def _family_materializer_rows_for_template(
    *,
    family: str,
    columns: list[dict[str, str]],
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    migrated: list[dict[str, Any]] = []
    for row in rows:
        source_values = dict(row.get("values") or {})
        values: dict[str, Any] = {}
        for column in columns:
            key = str(column.get("key") or "")
            label = str(column.get("label") or "")
            values[key] = source_values.get(key, source_values.get(label, ""))
            if values.get(key) in (None, ""):
                alias_value = _family_materializer_alias_value(source_values, key)
                if alias_value not in (None, ""):
                    values[key] = alias_value
        migrated.append(
            {
                "row_key": str(row.get("row_key") or ""),
                "values": _family_materializer_apply_defaults(family, values),
            }
        )
    return migrated


def _family_materializer_row_key(family: str, source_values: dict[str, Any], normalized_values: dict[str, Any]) -> str:
    for key in ("row_key", "fact_key", "event_key", "materialization_key", "id"):
        raw = str(source_values.get(key) or "").strip()
        if raw:
            if len(raw) <= 128:
                return raw
            digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
            return f"{raw[:111]}:{digest}"
    stable = json.dumps(normalized_values, sort_keys=True, default=str, separators=(",", ":"))
    digest = hashlib.sha256(stable.encode("utf-8")).hexdigest()[:16]
    return f"{family}:{digest}"[:128]


def _family_materializer_values(
    *,
    family: str,
    columns: list[dict[str, str]],
    row: Any,
    provenance: dict[str, Any] | None,
) -> tuple[str, dict[str, Any]]:
    source_values = _family_materializer_source_values(row)
    provenance_payload = dict(provenance or {})
    values: dict[str, Any] = {}
    for column in columns:
        key = str(column.get("key") or "")
        label = str(column.get("label") or "")
        values[key] = source_values.get(key, source_values.get(label, ""))
    for key in values:
        if values.get(key) in (None, ""):
            alias_value = _family_materializer_alias_value(source_values, key)
            if alias_value not in (None, ""):
                values[key] = alias_value
    if (
        "source_owner" in values
        and source_values.get("source_owner") in (None, "")
        and provenance_payload.get("source_owner") not in (None, "")
    ):
        values["source_owner"] = provenance_payload.get("source_owner")
    for key in ("source_owner", "source_document_id", "observed_source", "underlying_source"):
        if key in values and values.get(key) in (None, "") and provenance_payload.get(key) not in (None, ""):
            values[key] = provenance_payload.get(key)
    if "value" in values and values.get("value") in (None, "") and source_values.get("value_number") not in (None, ""):
        values["value"] = source_values.get("value_number")
    metric_columns = [column for column in columns if str(column.get("role") or "") == "metric"]
    if len(metric_columns) == 1:
        metric_key = str(metric_columns[0].get("key") or "")
        if metric_key and values.get(metric_key) in (None, "") and source_values.get("value_number") not in (None, ""):
            values[metric_key] = source_values.get("value_number")
    timestamp_columns = [column for column in columns if str(column.get("role") or "") == "timestamp"]
    if len(timestamp_columns) == 1:
        timestamp_key = str(timestamp_columns[0].get("key") or "")
        if timestamp_key and values.get(timestamp_key) in (None, ""):
            for fallback_key in ("period_date", "event_date", "observed_at", "as_of_date"):
                if source_values.get(fallback_key) not in (None, ""):
                    values[timestamp_key] = source_values.get(fallback_key)
                    break
    values = _family_materializer_apply_defaults(family, values)
    row_key = _family_materializer_row_key(family, source_values, values)
    return row_key, values


def _family_materializer_existing_schema(session: Session, dataset: Dataset) -> tuple[DatasetVersion, list[str], list[dict[str, Any]]]:
    draft = ensure_dataset_draft(session, dataset)
    columns = (
        session.query(DatasetColumn)
        .filter_by(dataset_version_id=int(draft.id))
        .order_by(DatasetColumn.sort_order.asc(), DatasetColumn.id.asc())
        .all()
    )
    rows = (
        session.query(DatasetRow)
        .filter_by(dataset_version_id=int(draft.id))
        .order_by(DatasetRow.sort_order.asc(), DatasetRow.id.asc())
        .all()
    )
    return draft, [str(column.key or "") for column in columns], [{"row_key": row.row_key, "values": dict(row.values or {})} for row in rows]


def upsert_family_dataset_materialization(
    session: Session,
    *,
    family: str,
    rows: list[Any] | tuple[Any, ...] | None = None,
    slug: str | None = None,
    title: str | None = None,
    description: str | None = None,
    category: str = "structured_data",
    source_kind: str | None = None,
    source_event: str | None = None,
    label: str | None = None,
    extra: dict[str, Any] | None = None,
    provenance: dict[str, Any] | None = None,
    links: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
) -> dict[str, Any]:
    """Create a template-backed family dataset shell and append/upsert rows.

    This intentionally writes through dataset service helpers so materializers
    keep using DatasetVersion, DatasetColumn, DatasetRow, and DatasetLink rails.
    """

    ensure_dataset_tables(session)
    family_key = str(family or "").strip().lower()
    if family_key not in DATASET_FAMILY_MATERIALIZER_FAMILIES:
        raise ValueError(f"Unsupported family dataset materializer: {family_key or 'unknown'}")
    columns = dataset_family_column_template(family_key)
    if not columns:
        raise ValueError(f"No family column template is registered for {family_key}.")
    meta = dataset_family_meta(family_key)
    dataset_title = str(title or meta["label"]).strip() or meta["label"]
    dataset_slug = _family_materializer_slug(family_key, slug, dataset_title)
    dataset_extra = _family_materializer_extra(family=family_key, extra=extra, provenance=provenance)
    dataset = session.query(Dataset).filter_by(slug=dataset_slug).first()
    created_dataset = dataset is None
    if dataset is None:
        dataset = create_dataset(
            session,
            title=dataset_title,
            description=(description or meta["summary"]).strip() or None,
            category=str(category or "structured_data").strip() or "structured_data",
            family=family_key,
            status="active",
            source_kind=(source_kind or family_key),
            slug=dataset_slug,
            columns=columns,
            rows=[],
            label=label or "Family materializer shell",
            extra=dataset_extra,
            source_event=source_event or family_key,
        )
    else:
        dataset.title = dataset_title
        dataset.description = (description or dataset.description or meta["summary"]).strip() or None
        dataset.category = str(category or dataset.category or "structured_data").strip() or "structured_data"
        dataset.family = family_key
        dataset.source_kind = (source_kind or dataset.source_kind or family_key)
        dataset.status = dataset.status or "active"
        merged_extra = dict(dataset.extra or {})
        merged_extra.update(dataset_extra)
        dataset.extra = merged_extra
        dataset.updated_at = utcnow()
        session.add(dataset)
        session.flush()
        _draft, existing_keys, existing_rows = _family_materializer_existing_schema(session, dataset)
        template_keys = [column["key"] for column in columns]
        if existing_keys and existing_keys != template_keys:
            if not _family_materializer_can_upgrade_schema(existing_keys, columns):
                raise ValueError(f"Existing dataset schema for {dataset_slug} does not match the {family_key} family template.")
            replace_dataset_draft(
                session,
                dataset,
                columns=columns,
                rows=_family_materializer_rows_for_template(family=family_key, columns=columns, rows=existing_rows),
                label=label or "Family materializer shell",
                extra=dict(dataset.extra or {}),
                source_event=source_event or family_key,
            )
            _draft, existing_keys, existing_rows = _family_materializer_existing_schema(session, dataset)
        if not existing_keys:
            replace_dataset_draft(
                session,
                dataset,
                columns=columns,
                rows=existing_rows,
                label=label or "Family materializer shell",
                extra=dict(dataset.extra or {}),
                source_event=source_event or family_key,
            )

    written_rows = []
    row_items = [row for row in (rows or [])]
    for row in row_items:
        row_key, values = _family_materializer_values(family=family_key, columns=columns, row=row, provenance=provenance)
        written_rows.append(
            upsert_dataset_draft_row(
                session,
                dataset,
                row_key=row_key,
                values=values,
                sort_order=None,
            )
        )
        if isinstance(row, StructuredDataFact):
            row.dataset_id = int(dataset.id)
            session.add(row)
    for link in links or []:
        if not isinstance(link, dict):
            continue
        link_dataset(
            session,
            dataset_id=int(dataset.id),
            object_type=str(link.get("object_type") or ""),
            object_id=int(link.get("object_id")),
            relation=str(link.get("relation") or "context"),
            extra=dict(link.get("extra") or {}),
        )
    session.flush()
    return {
        "dataset": dataset,
        "dataset_id": int(dataset.id),
        "slug": dataset.slug,
        "family": dataset.family,
        "created": created_dataset,
        "rows_received": len(row_items),
        "rows_written": len(written_rows),
        "row_keys": [row["row_key"] for row in written_rows],
    }
