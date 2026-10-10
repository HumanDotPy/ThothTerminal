from __future__ import annotations

import csv
import json
import os
import re
import tempfile
from datetime import timedelta
from pathlib import Path
from threading import RLock
from typing import Any, Iterable

from sqlalchemy import func
from sqlalchemy.orm import Session

from thoth_terminal.config import settings
from thoth_terminal.db import utcnow
from thoth_terminal.models import DatasetColumn, DatasetRow, DatasetVersion


_JSON_KEY_RE = re.compile(r"^[A-Za-z0-9_]+$")
_CACHE_LOCK = RLock()


def duckdb_path() -> Path:
    path = Path(settings.duckdb_path).resolve(strict=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def enabled() -> bool:
    return bool(getattr(settings, "duckdb_enabled", True))


def _duckdb() -> Any:
    import duckdb

    return duckdb


def connect(read_only: bool = False) -> Any:
    duckdb = _duckdb()
    conn = duckdb.connect(str(duckdb_path()), read_only=read_only)
    memory_limit = str(getattr(settings, "duckdb_memory_limit", "") or "").strip()
    if memory_limit:
        escaped = memory_limit.replace("'", "''")
        try:
            conn.execute(f"set memory_limit = '{escaped}'")
        except Exception:
            pass
    return conn


def _ensure_schema(conn: Any) -> None:
    try:
        rows_info = conn.execute("pragma table_info('dataset_rows')").fetchall()
        cols_info = conn.execute("pragma table_info('dataset_columns')").fetchall()
        if any(str(row[2]).upper() == "JSON" for row in rows_info) or any(
            str(row[2]).upper() == "JSON" for row in cols_info
        ):
            conn.execute("drop table if exists dataset_rows")
            conn.execute("drop table if exists dataset_columns")
            conn.execute("drop table if exists dataset_mirror_manifest")
    except Exception:
        pass
    conn.execute(
        """
        create table if not exists dataset_rows (
            dataset_id bigint not null,
            version_id bigint not null,
            row_key varchar not null,
            sort_order bigint not null,
            values_json varchar not null,
            updated_at timestamp,
            primary key (version_id, row_key)
        )
        """
    )
    conn.execute(
        """
        create table if not exists dataset_columns (
            dataset_id bigint not null,
            version_id bigint not null,
            column_key varchar not null,
            label varchar,
            data_type varchar,
            role varchar,
            sort_order bigint not null,
            config_json varchar,
            primary key (version_id, column_key)
        )
        """
    )
    conn.execute(
        "create index if not exists idx_duck_dataset_rows_dataset on dataset_rows(dataset_id)"
    )
    conn.execute(
        "create index if not exists idx_duck_dataset_rows_version_sort on dataset_rows(version_id, sort_order)"
    )
    conn.execute(
        """create table if not exists dataset_mirror_manifest (
            version_id bigint primary key,
            content_revision bigint not null,
            row_count bigint not null,
            last_requested_at timestamp not null
        )"""
    )


def _copy_records_iter(
    conn: Any,
    table_name: str,
    column_names: list[str],
    records: Iterable[dict[str, Any]],
) -> int:
    handle = tempfile.NamedTemporaryFile(
        "w", newline="", encoding="utf-8", suffix=".csv", delete=False
    )
    path = handle.name
    count = 0
    try:
        with handle:
            writer = csv.DictWriter(handle, fieldnames=column_names, extrasaction="ignore")
            writer.writeheader()
            for record in records:
                writer.writerow({name: record.get(name) for name in column_names})
                count += 1
        if count:
            escaped = path.replace("'", "''")
            cols = ", ".join(column_names)
            conn.execute(f"copy {table_name} ({cols}) from '{escaped}' (header true)")
        return count
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _copy_records(
    conn: Any, table_name: str, column_names: list[str], records: list[dict[str, Any]]
) -> int:
    return _copy_records_iter(conn, table_name, column_names, records)


def _iter_dataset_column_records(
    session: Session,
    *,
    version_id: int,
    dataset_id: int,
) -> Iterable[dict[str, Any]]:
    query = (
        session.query(
            DatasetColumn.key,
            DatasetColumn.label,
            DatasetColumn.data_type,
            DatasetColumn.role,
            DatasetColumn.sort_order,
            DatasetColumn.config,
        )
        .filter_by(dataset_version_id=int(version_id))
        .order_by(DatasetColumn.sort_order.asc(), DatasetColumn.id.asc())
        .yield_per(500)
    )
    for key, label, data_type, role, sort_order, config in query:
        yield {
            "dataset_id": int(dataset_id),
            "version_id": int(version_id),
            "column_key": str(key),
            "label": label,
            "data_type": data_type,
            "role": role,
            "sort_order": int(sort_order or 0),
            "config_json": json.dumps(config or {}),
        }


def _iter_dataset_row_records(
    session: Session,
    *,
    version_id: int,
    dataset_id: int,
    row_keys: Iterable[str] | None = None,
) -> Iterable[dict[str, Any]]:
    query = (
        session.query(
            DatasetRow.row_key,
            DatasetRow.sort_order,
            DatasetRow.values,
            DatasetRow.updated_at,
        )
        .filter_by(dataset_version_id=int(version_id))
        .order_by(DatasetRow.sort_order.asc(), DatasetRow.id.asc())
        .yield_per(500)
    )
    if row_keys is not None:
        query = query.filter(DatasetRow.row_key.in_(list(row_keys)))
    for row_key, sort_order, values, updated_at in query:
        yield {
            "dataset_id": int(dataset_id),
            "version_id": int(version_id),
            "row_key": str(row_key or ""),
            "sort_order": int(sort_order or 0),
            "values_json": json.dumps(values or {}),
            "updated_at": updated_at.isoformat(sep=" ") if updated_at else None,
        }


def init_duckdb() -> None:
    if not enabled():
        return
    conn = connect()
    try:
        _ensure_schema(conn)
    finally:
        conn.close()


def _cache_limits() -> tuple[int, int]:
    return (
        max(1, int(getattr(settings, "duckdb_dataset_cache_versions", 8))),
        max(1001, int(getattr(settings, "duckdb_dataset_cache_rows", 250_000))),
    )


def _evict_version(conn: Any, version_id: int) -> None:
    for table in ("dataset_rows", "dataset_columns", "dataset_mirror_manifest"):
        conn.execute(f"delete from {table} where version_id = ?", [int(version_id)])


def _bound_cache(conn: Any, *, keep_version: int | None = None) -> None:
    max_versions, max_rows = _cache_limits()
    entries = conn.execute(
        "select version_id, row_count, last_requested_at from dataset_mirror_manifest order by last_requested_at desc, version_id desc"
    ).fetchall()
    retained = 0
    rows = 0
    cutoff = utcnow() - timedelta(days=7)
    # Protect the currently requested entry, then keep the most recently used.
    entries.sort(key=lambda entry: entry[0] != keep_version)
    for version_id, row_count, requested_at in entries:
        if requested_at < cutoff or retained >= max_versions or rows + row_count > max_rows:
            _evict_version(conn, version_id)
        else:
            retained += 1
            rows += row_count
    # Legacy full mirrors have no freshness manifest and are never readable.
    conn.execute(
        "delete from dataset_rows where version_id not in (select version_id from dataset_mirror_manifest)"
    )
    conn.execute(
        "delete from dataset_columns where version_id not in (select version_id from dataset_mirror_manifest)"
    )


def _rebuild_version(session: Session, conn: Any, version: Any, revision: int) -> int:
    version_id = int(version.id)
    conn.execute("delete from dataset_columns where version_id = ?", [version_id])
    conn.execute("delete from dataset_rows where version_id = ?", [version_id])
    _copy_records_iter(
        conn,
        "dataset_columns",
        [
            "dataset_id",
            "version_id",
            "column_key",
            "label",
            "data_type",
            "role",
            "sort_order",
            "config_json",
        ],
        _iter_dataset_column_records(
            session, version_id=version_id, dataset_id=int(version.dataset_id)
        ),
    )
    row_count = _copy_records_iter(
        conn,
        "dataset_rows",
        ["dataset_id", "version_id", "row_key", "sort_order", "values_json", "updated_at"],
        _iter_dataset_row_records(
            session, version_id=version_id, dataset_id=int(version.dataset_id)
        ),
    )
    conn.execute(
        "insert or replace into dataset_mirror_manifest values (?, ?, ?, ?)",
        [version_id, revision, row_count, utcnow()],
    )
    return row_count


def ensure_dataset_mirror(
    session: Session, version_id: int, *, expected_revision: int | None = None
) -> bool:
    """Admit a large dataset on analytic demand, using only committed content.

    Every generation is built atomically. The manifest revision is checked by
    readers in the same DuckDB transaction as their row query.
    """
    from thoth_terminal.dataset_mirror_cache import dataset_mirror_pending

    if not enabled() or dataset_mirror_pending(session, version_id):
        return False
    # A separate canonical session excludes the request's uncommitted writes.
    bind = session.get_bind()
    if hasattr(bind, "engine"):
        bind = bind.engine
    with _CACHE_LOCK, Session(bind=bind) as committed:
        version = (
            committed.query(
                DatasetVersion.id, DatasetVersion.dataset_id, DatasetVersion.content_revision
            )
            .filter_by(id=int(version_id))
            .with_for_update(read=True)
            .first()
        )
        if version is None:
            return False
        revision = int(version.content_revision)
        if expected_revision is not None and revision != int(expected_revision):
            return False
        conn = connect()
        try:
            _ensure_schema(conn)
            cached = conn.execute(
                "select content_revision from dataset_mirror_manifest where version_id = ?",
                [int(version_id)],
            ).fetchone()
            if cached and int(cached[0]) == revision:
                conn.execute(
                    "update dataset_mirror_manifest set last_requested_at = ? where version_id = ?",
                    [utcnow(), int(version_id)],
                )
                return True
            row_count = (
                committed.query(func.count(DatasetRow.id))
                .filter_by(dataset_version_id=int(version_id))
                .scalar()
                or 0
            )
            if row_count <= 1000 or row_count > _cache_limits()[1]:
                conn.execute("begin transaction")
                _evict_version(conn, version_id)
                conn.execute("commit")
                return False
            conn.execute("begin transaction")
            _rebuild_version(committed, conn, version, revision)
            # A concurrent canonical commit makes this generation unusable;
            # the reader's expected revision prevents a stale result.
            _bound_cache(conn, keep_version=int(version_id))
            conn.execute("commit")
            return True
        except Exception:
            try:
                conn.execute("rollback")
            except Exception:
                pass
            raise
        finally:
            conn.close()


def maintain_committed_dataset_mirrors(
    session: Session, changes: dict[int, dict[str, Any]], revisions: dict[int, int | None]
) -> None:
    """Apply one delta per admitted version after the canonical batch commits."""
    if not enabled() or not duckdb_path().exists():
        return
    with _CACHE_LOCK:
        conn = connect()
        try:
            _ensure_schema(conn)
            conn.execute("begin transaction")
            for version_id in sorted(changes):
                change = changes[version_id]
                cached = conn.execute(
                    "select content_revision, row_count from dataset_mirror_manifest where version_id = ?",
                    [version_id],
                ).fetchone()
                if cached is None:
                    continue
                revision = revisions.get(version_id)
                version = (
                    session.query(
                        DatasetVersion.id,
                        DatasetVersion.dataset_id,
                        DatasetVersion.content_revision,
                    )
                    .filter_by(id=version_id)
                    .with_for_update(read=True)
                    .first()
                )
                if version is None:
                    _evict_version(conn, version_id)
                    continue
                # Do not apply a partial delta to a missed generation or to a
                # newer canonical batch that raced this callback.
                if (
                    revision is None
                    or int(version.content_revision) != revision
                    or int(cached[0]) != revision - 1
                ):
                    continue
                if change["replace"]:
                    count = (
                        session.query(func.count(DatasetRow.id))
                        .filter_by(dataset_version_id=version_id)
                        .scalar()
                        or 0
                    )
                    if count <= 1000 or count > _cache_limits()[1]:
                        _evict_version(conn, version_id)
                        continue
                    _rebuild_version(session, conn, version, revision)
                    continue
                keys = sorted(change["row_keys"])
                records = (
                    list(
                        _iter_dataset_row_records(
                            session,
                            version_id=version_id,
                            dataset_id=int(version.dataset_id),
                            row_keys=keys,
                        )
                    )
                    if keys
                    else []
                )
                removed = 0
                for start in range(0, len(keys), 500):
                    chunk = keys[start : start + 500]
                    placeholders = ",".join("?" for _ in chunk)
                    params = [version_id, *chunk]
                    removed += conn.execute(
                        f"select count(*) from dataset_rows where version_id = ? and row_key in ({placeholders})",
                        params,
                    ).fetchone()[0]
                    conn.execute(
                        f"delete from dataset_rows where version_id = ? and row_key in ({placeholders})",
                        params,
                    )
                _copy_records_iter(
                    conn,
                    "dataset_rows",
                    [
                        "dataset_id",
                        "version_id",
                        "row_key",
                        "sort_order",
                        "values_json",
                        "updated_at",
                    ],
                    records,
                )
                count = int(cached[1]) - removed + len(records)
                if change["columns_changed"]:
                    conn.execute("delete from dataset_columns where version_id = ?", [version_id])
                    _copy_records_iter(
                        conn,
                        "dataset_columns",
                        [
                            "dataset_id",
                            "version_id",
                            "column_key",
                            "label",
                            "data_type",
                            "role",
                            "sort_order",
                            "config_json",
                        ],
                        _iter_dataset_column_records(
                            session, version_id=version_id, dataset_id=int(version.dataset_id)
                        ),
                    )
                if count <= 1000:
                    _evict_version(conn, version_id)
                else:
                    conn.execute(
                        "update dataset_mirror_manifest set content_revision = ?, row_count = ? where version_id = ?",
                        [revision, count, version_id],
                    )
            _bound_cache(conn)
            conn.execute("commit")
        except Exception:
            conn.execute("rollback")
            raise
        finally:
            conn.close()


def sync_all_datasets(session: Session) -> dict[str, Any]:
    """Clear the derived cache; analytic requests repopulate bounded entries."""
    if not enabled():
        return {"ok": False, "skipped": "duckdb disabled"}
    with _CACHE_LOCK:
        conn = connect()
        try:
            _ensure_schema(conn)
            conn.execute("begin transaction")
            for table in ("dataset_rows", "dataset_columns", "dataset_mirror_manifest"):
                conn.execute(f"delete from {table}")
            conn.execute("commit")
            return {
                "ok": True,
                "versions": 0,
                "rows": 0,
                "columns": 0,
                "mode": "bounded demand cache",
            }
        except Exception:
            conn.execute("rollback")
            raise
        finally:
            conn.close()


def _dataset_filter_rules(
    filters: dict[str, Any] | list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    if isinstance(filters, dict):
        if isinstance(filters.get("rules"), list):
            return [dict(rule) for rule in filters.get("rules", []) if isinstance(rule, dict)]
        return [{"column": key, "op": "eq", "value": value} for key, value in filters.items()]
    if isinstance(filters, list):
        return [dict(rule) for rule in filters if isinstance(rule, dict)]
    return []


def _json_path(column_key: str) -> str | None:
    key = str(column_key or "").strip()
    if not key or not _JSON_KEY_RE.fullmatch(key):
        return None
    return f"$.{key}"


def _column_data_type(column_types: dict[str, str] | None, column_key: str) -> str:
    return str((column_types or {}).get(str(column_key or "")) or "").strip().lower()


def _compile_filter_sql(
    filters: dict[str, Any] | list[dict[str, Any]] | None,
    *,
    column_types: dict[str, str] | None = None,
) -> tuple[list[str], list[Any]] | None:
    clauses: list[str] = []
    params: list[Any] = []
    for rule in _dataset_filter_rules(filters):
        key = str(rule.get("column") or "").strip()
        path = _json_path(key)
        if path is None:
            return None
        op = str(rule.get("op") or "eq").strip().lower() or "eq"
        value = rule.get("value")
        data_type = _column_data_type(column_types, key)
        is_numeric = data_type == "number" or (
            isinstance(value, (int, float)) and not isinstance(value, bool)
        )
        text_expr = "json_extract_string(values_json, ?)"
        numeric_expr = "try_cast(json_extract_string(values_json, ?) as double)"
        if op == "contains":
            clauses.append(f"lower(coalesce({text_expr}, '')) like ?")
            params.extend([path, f"%{str(value or '').lower()}%"])
        elif op in {"eq", "neq"}:
            if isinstance(value, bool):
                return None
            if is_numeric:
                try:
                    numeric_value = float(value)
                except Exception:
                    return None
                if op == "eq":
                    clauses.append(f"{numeric_expr} = ?")
                    params.extend([path, numeric_value])
                else:
                    clauses.append(f"({numeric_expr} is null or {numeric_expr} <> ?)")
                    params.extend([path, path, numeric_value])
            else:
                if op == "eq":
                    clauses.append(f"{text_expr} = ?")
                    params.extend([path, str(value)])
                else:
                    clauses.append(f"coalesce({text_expr}, '') <> ?")
                    params.extend([path, str(value)])
        elif op in {"gt", "gte", "lt", "lte"}:
            try:
                numeric_value = float(value)
            except Exception:
                return None
            symbol = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[op]
            clauses.append(f"{numeric_expr} {symbol} ?")
            params.extend([path, numeric_value])
        else:
            return None
    return clauses, params


def _compile_sort_sql(
    sort_spec: list[dict[str, Any]] | None,
    *,
    column_types: dict[str, str] | None = None,
) -> tuple[list[str], list[Any]] | None:
    clauses: list[str] = []
    params: list[Any] = []
    for spec in sort_spec or []:
        if not isinstance(spec, dict):
            continue
        key = str(spec.get("column") or "").strip()
        path = _json_path(key)
        if path is None:
            return None
        direction = (
            "desc" if str(spec.get("direction") or "asc").strip().lower() == "desc" else "asc"
        )
        if _column_data_type(column_types, key) == "number":
            clauses.append(
                f"try_cast(json_extract_string(values_json, ?) as double) {direction} nulls last"
            )
        else:
            clauses.append(f"json_extract_string(values_json, ?) {direction} nulls last")
        params.append(path)
    return clauses, params


def query_dataset_rows(
    version_id: int,
    *,
    expected_revision: int | None = None,
    limit: int | None = None,
    filters: dict[str, Any] | list[dict[str, Any]] | None = None,
    sort_spec: list[dict[str, Any]] | None = None,
    column_types: dict[str, str] | None = None,
) -> list[dict[str, Any]] | None:
    if expected_revision is None or not enabled() or not duckdb_path().exists():
        return None
    filter_sql = _compile_filter_sql(filters, column_types=column_types)
    sort_sql = _compile_sort_sql(sort_spec, column_types=column_types)
    if filter_sql is None or sort_sql is None:
        return None
    filter_clauses, filter_params = filter_sql
    sort_clauses, sort_params = sort_sql
    sql = "select row_key, values_json from dataset_rows where version_id = ?"
    params: list[Any] = [int(version_id)]
    if filter_clauses:
        sql += " and " + " and ".join(filter_clauses)
        params.extend(filter_params)
    if sort_clauses:
        sql += " order by " + ", ".join(sort_clauses) + ", sort_order asc, row_key asc"
        params.extend(sort_params)
    else:
        sql += " order by sort_order, row_key"
    if limit is not None:
        sql += " limit ?"
        params.append(max(1, int(limit)))
    try:
        conn = connect(read_only=True)
        try:
            conn.execute("begin transaction")
            manifest = conn.execute(
                "select content_revision from dataset_mirror_manifest where version_id = ?",
                [int(version_id)],
            ).fetchone()
            if manifest is None or int(manifest[0]) != int(expected_revision):
                return None
            rows = conn.execute(sql, params).fetchall()
            conn.execute("commit")
        finally:
            conn.close()
    except Exception:
        return None
    out: list[dict[str, Any]] = []
    for row_key, values_raw in rows:
        if isinstance(values_raw, str):
            values = json.loads(values_raw)
        else:
            values = values_raw
        out.append({"row_key": row_key, "values": values or {}})
    return out
