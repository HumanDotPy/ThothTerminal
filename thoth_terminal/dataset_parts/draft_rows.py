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
from thoth_terminal.dataset_parts.core import _parse_iso_date
from thoth_terminal.product_stage_service import serialize_product_stage_entry


def _non_empty(value: Any) -> bool:
    return value not in (None, "")


def _coerce_value_for_column(column: dict[str, Any], value: Any) -> tuple[Any, str | None]:
    data_type = str(column.get("data_type") or "text").strip().lower()
    if value in (None, ""):
        return ("" if data_type == "text" else None), None
    if data_type == "number":
        try:
            raw = str(value).strip()
            if raw == "":
                return None, None
            if "." in raw:
                return float(raw), None
            return int(raw), None
        except Exception:
            return value, f"{column.get('label') or column.get('key')}: expected number"
    if data_type == "bool":
        raw = str(value).strip().lower()
        if raw in {"true", "1", "yes", "y"}:
            return True, None
        if raw in {"false", "0", "no", "n"}:
            return False, None
        return value, f"{column.get('label') or column.get('key')}: expected boolean"
    if data_type == "date":
        parsed = _parse_iso_date(value)
        if parsed is None:
            return value, f"{column.get('label') or column.get('key')}: expected ISO date"
        return parsed.isoformat(), None
    if data_type == "json":
        if isinstance(value, (dict, list)):
            return value, None
        try:
            return json.loads(str(value)), None
        except Exception:
            return value, f"{column.get('label') or column.get('key')}: expected JSON"
    return str(value), None


def validate_dataset_row(
    family: str | None,
    columns: list[dict[str, Any]],
    values: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    normalized_values: dict[str, Any] = {}
    errors: list[dict[str, Any]] = []
    for column in columns:
        key = str(column.get("key") or "")
        coerced, error = _coerce_value_for_column(column, values.get(key))
        normalized_values[key] = coerced
        if error:
            errors.append({"column": key, "message": error})
    required_roles = DATASET_FAMILY_REQUIRED_ROLES.get(str(family or "").strip().lower() or "general", set())
    for role in sorted(required_roles):
        role_columns = [column for column in columns if str(column.get("role") or "").strip().lower() == role]
        if role_columns and not any(_non_empty(normalized_values.get(str(column.get("key") or ""))) for column in role_columns):
            errors.append({"column": str(role_columns[0].get("key") or ""), "message": f"At least one {role} value is required."})
    if str(family or "").strip().lower() == "memory_site_program_ramp":
        ordered_states = (
            "gross_nameplate_wpm",
            "equipped_wpm",
            "eligible_wpm",
            "qualified_wpm",
            "utilized_wpm",
            "allocated_wpm",
        )
        has_capacity_state = any(_non_empty(normalized_values.get(key)) for key in ordered_states) or any(
            _non_empty(normalized_values.get(key)) for key in ("node_allocation_wpm", "node_allocation_share")
        )
        if has_capacity_state:
            identity_groups = (
                ("supplier", "company"),
                ("site", "fab", "facility"),
                ("period_date", "period_label"),
            )
            for group in identity_groups:
                if not any(_non_empty(normalized_values.get(key)) for key in group):
                    errors.append({"column": group[0], "message": f"At least one of {', '.join(group)} is required."})
            if not _non_empty(normalized_values.get("pool_id")):
                identity_parts = (
                    normalized_values.get("supplier") or normalized_values.get("company"),
                    normalized_values.get("site"),
                    normalized_values.get("fab") or normalized_values.get("facility"),
                    normalized_values.get("phase"),
                    normalized_values.get("process_node") or normalized_values.get("technology"),
                    normalized_values.get("period_date") or normalized_values.get("period_label"),
                )
                normalized_values["pool_id"] = "|".join(
                    str(value).strip().lower().replace(" ", "-") for value in identity_parts if _non_empty(value)
                )
        if any(_non_empty(normalized_values.get(key)) for key in ("node_allocation_wpm", "node_allocation_share")):
            if not _non_empty(normalized_values.get("parent_pool_id") or normalized_values.get("physical_pool_id")):
                errors.append({"column": "parent_pool_id", "message": "A node allocation requires parent_pool_id or physical_pool_id."})
            if not _non_empty(normalized_values.get("node_pool_id")):
                errors.append({"column": "node_pool_id", "message": "A node allocation requires node_pool_id."})
            if not _non_empty(normalized_values.get("process_node")):
                errors.append({"column": "process_node", "message": "A node allocation requires process_node."})
        populated: list[tuple[str, float]] = []
        for key in ordered_states:
            value = normalized_values.get(key)
            if value not in (None, ""):
                try:
                    populated.append((key, float(value)))
                except (TypeError, ValueError):
                    continue
        for (upstream_key, upstream), (downstream_key, downstream) in zip(populated, populated[1:]):
            if downstream > upstream + 1e-9:
                errors.append(
                    {
                        "column": downstream_key,
                        "message": f"{downstream_key} cannot exceed upstream {upstream_key}.",
                    }
                )
    return normalized_values, errors


def _serialize_dataset_row(
    family: str | None,
    columns: list[dict[str, Any]],
    row: DatasetRow | dict[str, Any],
) -> dict[str, Any]:
    row_key = row.row_key if isinstance(row, DatasetRow) else str(row.get("row_key") or "")
    values = dict(row.values or {}) if isinstance(row, DatasetRow) else dict(row.get("values") or {})
    _, errors = validate_dataset_row(family, columns, values)
    sort_order = int(row.sort_order or 0) if isinstance(row, DatasetRow) else int(row.get("sort_order") or 0)
    updated_at = row.updated_at.isoformat() if isinstance(row, DatasetRow) and row.updated_at else None
    return {
        "row_key": row_key,
        "values": values,
        "sort_order": sort_order,
        "validation_errors": errors,
        "updated_at": updated_at,
    }


def list_dataset_draft_rows(
    session: Session,
    dataset: Dataset,
    *,
    limit: int = 50,
    offset: int = 0,
    query: str | None = None,
) -> dict[str, Any]:
    draft = ensure_dataset_draft(session, dataset)
    columns = [
        {
            "key": col.key,
            "label": col.label,
            "data_type": col.data_type,
            "role": col.role,
            "sort_order": int(col.sort_order or 0),
            "config": dict(col.config or {}),
        }
        for col in (
            session.query(DatasetColumn)
            .filter_by(dataset_version_id=int(draft.id))
            .order_by(DatasetColumn.sort_order.asc(), DatasetColumn.id.asc())
            .all()
        )
    ]
    base_rows_query = (
        session.query(DatasetRow)
        .filter_by(dataset_version_id=int(draft.id))
    )
    search = str(query or "").strip().lower()
    if search:
        all_rows = base_rows_query.order_by(DatasetRow.sort_order.asc(), DatasetRow.id.asc()).all()
        all_rows = [
            row
            for row in all_rows
            if search in " ".join(str(value or "") for value in (row.values or {}).values()).lower() or search in str(row.row_key or "").lower()
        ]
        total = len(all_rows)
        sliced = all_rows[max(0, int(offset)) : max(0, int(offset)) + max(1, min(int(limit), 500))]
    else:
        total = int(base_rows_query.count() or 0)
        sliced = (
            base_rows_query.order_by(DatasetRow.sort_order.asc(), DatasetRow.id.asc())
            .offset(max(0, int(offset)))
            .limit(max(1, min(int(limit), 500)))
            .all()
        )
    return {
        "version": _dataset_version_payload(draft),
        "columns": columns,
        "rows": [_serialize_dataset_row(dataset.family, columns, row) for row in sliced],
        "total": total,
        "offset": max(0, int(offset)),
        "limit": max(1, min(int(limit), 500)),
    }


def _next_draft_row_position(session: Session, draft: DatasetVersion) -> tuple[int, str]:
    row = (
        session.query(DatasetRow)
        .filter_by(dataset_version_id=int(draft.id))
        .order_by(DatasetRow.sort_order.desc(), DatasetRow.id.desc())
        .first()
    )
    next_order = int(row.sort_order or 0) + 1 if row is not None else 1
    return next_order, str(next_order)


def upsert_dataset_draft_row(
    session: Session,
    dataset: Dataset,
    *,
    row_key: str | None,
    values: dict[str, Any],
    sort_order: int | None = None,
) -> dict[str, Any]:
    draft = ensure_dataset_draft(session, dataset)
    columns = [
        {
            "key": col.key,
            "label": col.label,
            "data_type": col.data_type,
            "role": col.role,
            "sort_order": int(col.sort_order or 0),
            "config": dict(col.config or {}),
        }
        for col in (
            session.query(DatasetColumn)
            .filter_by(dataset_version_id=int(draft.id))
            .order_by(DatasetColumn.sort_order.asc(), DatasetColumn.id.asc())
            .all()
        )
    ]
    if not columns:
        raise ValueError("Draft editing requires dataset columns. Import data or define columns first.")
    normalized_values, errors = validate_dataset_row(dataset.family, columns, values)
    if errors:
        raise ValueError(json.dumps({"errors": errors}))
    resolved_row_key = str(row_key or "").strip()
    row = (
        session.query(DatasetRow)
        .filter_by(dataset_version_id=int(draft.id), row_key=resolved_row_key)
        .first()
        if resolved_row_key
        else None
    )
    now = utcnow()
    if row is None:
        default_order, default_key = _next_draft_row_position(session, draft)
        resolved_row_key = resolved_row_key or f"row-{default_key}"
        row = DatasetRow(
            dataset_version_id=int(draft.id),
            row_key=resolved_row_key,
            values=normalized_values,
            sort_order=int(sort_order) if sort_order is not None else default_order,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    else:
        row.values = normalized_values
        if sort_order is not None:
            row.sort_order = int(sort_order)
        row.updated_at = now
        session.add(row)
    draft.updated_at = now
    dataset.updated_at = now
    session.add(draft)
    session.add(dataset)
    session.flush()
    from thoth_terminal.dataset_mirror_cache import mark_dataset_mirror_dirty

    mark_dataset_mirror_dirty(session, int(draft.id), row_keys=[resolved_row_key])
    return _serialize_dataset_row(dataset.family, columns, row)


def delete_dataset_draft_row(session: Session, dataset: Dataset, *, row_key: str) -> bool:
    draft = ensure_dataset_draft(session, dataset)
    row = session.query(DatasetRow).filter_by(dataset_version_id=int(draft.id), row_key=str(row_key)).first()
    if row is None:
        return False
    session.delete(row)
    draft.updated_at = utcnow()
    dataset.updated_at = draft.updated_at
    session.add(draft)
    session.add(dataset)
    session.flush()
    from thoth_terminal.dataset_mirror_cache import mark_dataset_mirror_dirty

    mark_dataset_mirror_dirty(session, int(draft.id), row_keys=[str(row_key)])
    return True


def _parse_delimited_text_rows(text: str) -> list[list[str]]:
    sample = text.splitlines()[0] if text.splitlines() else ""
    delimiter = "\t" if "\t" in sample else ","
    reader = csv.reader(StringIO(text), delimiter=delimiter)
    return [list(row) for row in reader if any(str(cell or "").strip() for cell in row)]


def bulk_paste_dataset_draft_rows(session: Session, dataset: Dataset, *, text: str) -> dict[str, Any]:
    raw_text = str(text or "").strip()
    if not raw_text:
        raise ValueError("Paste text is required.")
    draft = ensure_dataset_draft(session, dataset)
    existing_columns = (
        session.query(DatasetColumn)
        .filter_by(dataset_version_id=int(draft.id))
        .order_by(DatasetColumn.sort_order.asc(), DatasetColumn.id.asc())
        .all()
    )
    existing_rows = (
        session.query(DatasetRow)
        .filter_by(dataset_version_id=int(draft.id))
        .order_by(DatasetRow.sort_order.asc(), DatasetRow.id.asc())
        .all()
    )
    if not existing_columns:
        inferred_columns, inferred_rows = parse_csv_dataset(raw_text.replace("\t", ","))
        replace_dataset_draft(session, dataset, columns=inferred_columns, rows=inferred_rows, label="bulk paste")
        listing = list_dataset_draft_rows(session, dataset, limit=50, offset=0)
        return {"added": len(inferred_rows), "rows": listing["rows"], "columns": listing["columns"], "created_columns": True}
    rows_2d = _parse_delimited_text_rows(raw_text)
    column_labels = [col.label for col in existing_columns]
    column_keys = [col.key for col in existing_columns]
    normalized_header = [_normalize_column_key(cell, fallback=f"column_{idx + 1}") for idx, cell in enumerate(rows_2d[0])]
    use_header = normalized_header == [_normalize_column_key(key, fallback=key) for key in column_keys] or normalized_header == [
        _normalize_column_key(label, fallback=label) for label in column_labels
    ]
    data_rows = rows_2d[1:] if use_header else rows_2d
    added = 0
    errors: list[dict[str, Any]] = []
    next_order = int(existing_rows[-1].sort_order if existing_rows else 0)
    for raw_row in data_rows:
        next_order += 1
        values = {col.key: raw_row[idx] if idx < len(raw_row) else "" for idx, col in enumerate(existing_columns)}
        try:
            upsert_dataset_draft_row(
                session,
                dataset,
                row_key=f"row-{next_order}",
                values=values,
                sort_order=next_order,
            )
            added += 1
        except ValueError as exc:
            try:
                error_payload = json.loads(str(exc))
                row_errors = error_payload.get("errors") if isinstance(error_payload, dict) else None
            except Exception:
                row_errors = [{"message": str(exc)}]
            errors.append({"row_index": next_order, "errors": row_errors or [{"message": str(exc)}]})
    if errors:
        raise ValueError(json.dumps({"errors": errors}))
    listing = list_dataset_draft_rows(session, dataset, limit=50, offset=max(0, added - 50))
    return {"added": added, "rows": listing["rows"], "columns": listing["columns"], "created_columns": False}
