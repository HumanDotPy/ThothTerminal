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

from thoth_terminal.dataset_parts.core import _DATASET_SQL_FIRST_ROW_THRESHOLD


def normalize_dataset_view_definition(
    columns: list[dict[str, Any]] | None,
    *,
    filters: dict[str, Any] | list[dict[str, Any]] | None = None,
    filter_rules: list[dict[str, Any]] | None = None,
    selected_columns: list[str] | None = None,
    sort_spec: list[dict[str, Any]] | None = None,
    advanced_filters: dict[str, Any] | list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    valid_columns = {str(col.get("key") or "") for col in (columns or []) if str(col.get("key") or "").strip()}
    rules: list[dict[str, Any]] = []
    source_rules = filter_rules if isinstance(filter_rules, list) else None
    if source_rules is None:
        if isinstance(filters, dict) and isinstance(filters.get("rules"), list):
            source_rules = [rule for rule in filters.get("rules", []) if isinstance(rule, dict)]
        elif isinstance(filters, list):
            source_rules = [rule for rule in filters if isinstance(rule, dict)]
    for rule in source_rules or []:
        key = str(rule.get("column") or "").strip()
        if not key or (valid_columns and key not in valid_columns):
            continue
        op = str(rule.get("op") or "eq").strip().lower() or "eq"
        if op not in DATASET_VIEW_OPERATOR_VALUES:
            op = "eq"
        value = _coerce_scalar(rule.get("value"))
        if value in (None, ""):
            continue
        rules.append({"column": key, "op": op, "value": value})
    if not rules and advanced_filters is not None:
        if isinstance(advanced_filters, dict) and isinstance(advanced_filters.get("rules"), list):
            rules = [rule for rule in advanced_filters.get("rules", []) if isinstance(rule, dict)]
        elif isinstance(advanced_filters, list):
            rules = [rule for rule in advanced_filters if isinstance(rule, dict)]
    normalized_columns = [key for key in (selected_columns or []) if key in valid_columns]
    if not normalized_columns:
        normalized_columns = [str(col.get("key") or "") for col in (columns or []) if str(col.get("key") or "").strip()]
    normalized_sort: list[dict[str, Any]] = []
    for spec in sort_spec or []:
        if not isinstance(spec, dict):
            continue
        key = str(spec.get("column") or "").strip()
        if not key or (valid_columns and key not in valid_columns):
            continue
        direction = str(spec.get("direction") or "asc").strip().lower()
        normalized_sort.append({"column": key, "direction": "desc" if direction == "desc" else "asc"})
    return {
        "filters": {"rules": rules} if rules else {},
        "columns": normalized_columns,
        "sort_spec": normalized_sort,
        "builder": {"filter_rules": rules, "selected_columns": normalized_columns, "sort_spec": normalized_sort},
    }


def parse_structured_dataset_view_payload(
    payload: dict[str, Any],
    dataset_payload: dict[str, Any],
) -> dict[str, Any]:
    columns = dataset_payload.get("columns") if isinstance(dataset_payload.get("columns"), list) else []
    raw_rules = payload.get("filter_rules")
    filter_rules = raw_rules if isinstance(raw_rules, list) else None
    if filter_rules is None and isinstance(payload.get("filters"), dict):
        possible_rules = payload.get("filters", {}).get("rules")
        if isinstance(possible_rules, list):
            filter_rules = possible_rules
    selected_columns = payload.get("columns") if isinstance(payload.get("columns"), list) else None
    sort_spec = payload.get("sort_spec") if isinstance(payload.get("sort_spec"), list) else None
    advanced_filters = None
    raw_advanced = payload.get("advanced_filters")
    if isinstance(raw_advanced, str) and raw_advanced.strip():
        try:
            advanced_filters = json.loads(raw_advanced)
        except Exception as exc:
            raise ValueError("Invalid advanced filter JSON.") from exc
    elif isinstance(raw_advanced, (dict, list)):
        advanced_filters = raw_advanced
    return normalize_dataset_view_definition(
        columns,
        filters=payload.get("filters") if isinstance(payload.get("filters"), (dict, list)) else None,
        filter_rules=filter_rules,
        selected_columns=selected_columns,
        sort_spec=sort_spec,
        advanced_filters=advanced_filters,
    )


def _rule_match(value: Any, op: str, target: Any) -> bool:
    if op == "contains":
        return str(target).lower() in str(value or "").lower()
    if op == "neq":
        return value != target
    if op == "gt":
        try:
            return float(value) > float(target)
        except Exception:
            return False
    if op == "gte":
        try:
            return float(value) >= float(target)
        except Exception:
            return False
    if op == "lt":
        try:
            return float(value) < float(target)
        except Exception:
            return False
    if op == "lte":
        try:
            return float(value) <= float(target)
        except Exception:
            return False
    return value == target


def _apply_filters(rows: list[dict[str, Any]], filters: Any) -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = []
    if isinstance(filters, dict):
        if isinstance(filters.get("rules"), list):
            rules = [r for r in filters.get("rules", []) if isinstance(r, dict)]
        else:
            rules = [{"column": key, "op": "eq", "value": value} for key, value in filters.items()]
    elif isinstance(filters, list):
        rules = [r for r in filters if isinstance(r, dict)]
    if not rules:
        return rows
    out = []
    for row in rows:
        values = row.get("values") or {}
        if all(_rule_match(values.get(str(rule.get("column") or "")), str(rule.get("op") or "eq"), rule.get("value")) for rule in rules):
            out.append(row)
    return out


def _dataset_filter_rules_for_materialization(filters: Any) -> list[dict[str, Any]]:
    if isinstance(filters, dict):
        if isinstance(filters.get("rules"), list):
            return [r for r in filters.get("rules", []) if isinstance(r, dict)]
        return [{"column": key, "op": "eq", "value": value} for key, value in filters.items()]
    if isinstance(filters, list):
        return [r for r in filters if isinstance(r, dict)]
    return []


def _merge_dataset_filters(*filters: Any) -> dict[str, Any]:
    rules: list[dict[str, Any]] = []
    for item in filters:
        rules.extend(dict(rule) for rule in _dataset_filter_rules_for_materialization(item))
    return {"rules": rules} if rules else {}


def _apply_sort(rows: list[dict[str, Any]], sort_spec: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    specs = [s for s in (sort_spec or []) if isinstance(s, dict) and s.get("column")]
    if not specs:
        return rows
    ordered = list(rows)
    for spec in reversed(specs):
        key = str(spec.get("column"))
        reverse = str(spec.get("direction") or "asc").lower() == "desc"
        ordered.sort(key=lambda row: row.get("values", {}).get(key), reverse=reverse)
    return ordered


def _apply_selected_columns(rows: list[dict[str, Any]], selected_columns: list[str] | None) -> list[dict[str, Any]]:
    allowed = [str(value) for value in (selected_columns or []) if str(value).strip()]
    if not allowed:
        return rows
    return [{"row_key": row["row_key"], "values": {key: (row.get("values") or {}).get(key) for key in allowed}} for row in rows]


def _materialize_view_rows(
    rows: list[dict[str, Any]],
    *,
    filters: dict[str, Any] | list[dict[str, Any]] | None = None,
    sort_spec: list[dict[str, Any]] | None = None,
    selected_columns: list[str] | None = None,
) -> list[dict[str, Any]]:
    rows = _apply_filters(rows, filters or {})
    rows = _apply_sort(rows, sort_spec)
    rows = _apply_selected_columns(rows, selected_columns)
    return rows


def _dataset_version_fits_sql_first(session: Session, version_id: int, *, threshold: int = _DATASET_SQL_FIRST_ROW_THRESHOLD) -> bool:
    probe = (
        session.query(DatasetRow.id)
        .filter_by(dataset_version_id=int(version_id))
        .order_by(DatasetRow.id.asc())
        .limit(max(1, int(threshold)) + 1)
        .all()
    )
    return len(probe) <= max(1, int(threshold))


def _query_dataset_rows_sql(session: Session, version_id: int, *, limit: int | None = None) -> list[dict[str, Any]]:
    row_limit = max(1, min(int(limit), 5000)) if limit is not None else None
    q = (
        session.query(DatasetRow)
        .filter_by(dataset_version_id=int(version_id))
        .order_by(DatasetRow.sort_order.asc(), DatasetRow.id.asc())
    )
    if row_limit is not None:
        q = q.limit(row_limit)
    return [{"row_key": row.row_key, "values": dict(row.values or {})} for row in q.all()]


def _dataset_column_types(session: Session, version_id: int) -> dict[str, str]:
    return {
        str(col.key or ""): str(col.data_type or "text")
        for col in session.query(DatasetColumn)
        .filter_by(dataset_version_id=int(version_id))
        .order_by(DatasetColumn.sort_order.asc(), DatasetColumn.id.asc())
        .all()
        if str(col.key or "").strip()
    }


def query_dataset(
    session: Session,
    *,
    dataset_id: int,
    version_id: int | None = None,
    view_id: int | None = None,
    filters: dict[str, Any] | list[dict[str, Any]] | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    version = None
    if version_id is None:
        from thoth_terminal.dataset_storage import canonical_dataset
        dataset = session.get(Dataset, int(dataset_id))
        if dataset is not None:
            dataset_id = int(canonical_dataset(session, dataset).id)
    if version_id is not None:
        version = session.query(DatasetVersion).filter_by(id=int(version_id), dataset_id=int(dataset_id)).first()
    else:
        version = latest_dataset_version(session, dataset_id, status="draft") or latest_dataset_version(session, dataset_id, status="published")
    if not version:
        return {"version": None, "rows": []}
    chosen_view = None
    if view_id is not None:
        chosen_view = session.query(DatasetView).filter_by(id=int(view_id), dataset_id=int(dataset_id)).first()
    rows = []
    sql_attempted = False
    duckdb_returned = False
    pushdown_filters_applied = False
    pushdown_sort_applied = False
    view_filters = (chosen_view.filters or {}) if chosen_view else {}
    request_filters = filters or {}
    combined_filters = _merge_dataset_filters(view_filters, request_filters)
    view_sort = list(chosen_view.sort_spec or []) if chosen_view else []
    try:
        if _dataset_version_fits_sql_first(session, int(version.id)):
            sql_limit = None if (view_id is not None or filters) else limit
            rows = _query_dataset_rows_sql(session, int(version.id), limit=sql_limit)
            sql_attempted = True
    except Exception:
        rows = []
        sql_attempted = False
    if not sql_attempted:
        try:
            from thoth_terminal.duckdb_store import query_dataset_rows, ensure_dataset_mirror

            if not ensure_dataset_mirror(session, int(version.id), expected_revision=int(version.content_revision)):
                raise RuntimeError("Analytic cache unavailable for this revision")

            push_filters = combined_filters
            push_sort = view_sort
            if push_filters or push_sort:
                duckdb_rows = query_dataset_rows(
                    int(version.id),
                    expected_revision=int(version.content_revision),
                    limit=limit,
                    filters=push_filters,
                    sort_spec=push_sort,
                    column_types=_dataset_column_types(session, int(version.id)),
                )
                if duckdb_rows is not None:
                    rows = duckdb_rows
                    duckdb_returned = True
                    pushdown_filters_applied = bool(push_filters)
                    pushdown_sort_applied = bool(push_sort)
                else:
                    rows = query_dataset_rows(int(version.id), expected_revision=int(version.content_revision), limit=None if (view_id is not None or filters) else limit) or []
            else:
                duckdb_rows = query_dataset_rows(int(version.id), expected_revision=int(version.content_revision), limit=limit)
                if duckdb_rows is not None:
                    rows = duckdb_rows
                    duckdb_returned = True
        except Exception:
            rows = []
    if not rows and not sql_attempted and not duckdb_returned:
        fallback_limit = None if (view_id is not None or filters) else limit
        rows = _query_dataset_rows_sql(session, int(version.id), limit=fallback_limit)
    if chosen_view:
        if pushdown_filters_applied and pushdown_sort_applied:
            rows = _apply_selected_columns(rows, list(chosen_view.columns or []))
        else:
            rows = _materialize_view_rows(
                rows,
                filters={} if pushdown_filters_applied else view_filters,
                sort_spec=[] if pushdown_sort_applied else view_sort,
                selected_columns=list(chosen_view.columns or []),
            )
    if filters:
        if not pushdown_filters_applied:
            rows = _apply_filters(rows, filters)
    if limit is not None:
        rows = rows[: max(1, min(int(limit), 5000))]
    return {"version": _dataset_version_payload(version), "rows": rows}


def preview_dataset_view(
    session: Session,
    dataset: Dataset,
    *,
    filters: dict[str, Any] | list[dict[str, Any]] | None = None,
    selected_columns: list[str] | None = None,
    sort_spec: list[dict[str, Any]] | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    payload = serialize_dataset(session, dataset, include_rows=False)
    base_rows = query_dataset(session, dataset_id=int(dataset.id), limit=None).get("rows") or []
    rows = _materialize_view_rows(base_rows, filters=filters, sort_spec=sort_spec, selected_columns=selected_columns)
    limited = rows[: max(1, min(int(limit), 500))]
    return {
        "columns": payload.get("columns") or [],
        "rows": limited,
        "total": len(rows),
        "preview_count": len(limited),
    }
