from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import MetaData, Table, create_engine, func, insert, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.sql.sqltypes import Date, DateTime, JSON

from thoth_terminal.config import settings
from thoth_terminal.models import Base


def _sqlite_url(path: Path) -> str:
    return f"sqlite:///{Path(path).resolve(strict=False).as_posix()}"


def _engine(url: str) -> Engine:
    return create_engine(url, pool_pre_ping=True)


def _table_count(engine: Engine, table: Table) -> int:
    with engine.connect() as conn:
        try:
            return int(conn.execute(select(func.count()).select_from(table)).scalar() or 0)
        except Exception:
            return 0


def _coerce_value(value: Any, column_type: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.replace("\x00", "")
    if isinstance(column_type, JSON):
        if isinstance(value, (dict, list)):
            return _sanitize_json(value)
        if isinstance(value, str):
            try:
                return _sanitize_json(json.loads(value))
            except Exception:
                return value
        return value
    if isinstance(column_type, (DateTime, Date)):
        return value
    return value


def _sanitize_json(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, list):
        return [_sanitize_json(item) for item in value]
    if isinstance(value, dict):
        return {
            _sanitize_json(key): _sanitize_json(item)
            for key, item in value.items()
        }
    return value


def _reset_postgres_sequence(conn, table: Table) -> None:
    pk_cols = list(table.primary_key.columns)
    if len(pk_cols) != 1:
        return
    col = pk_cols[0]
    try:
        max_id = conn.execute(select(func.max(table.c[col.name]))).scalar()
        if max_id is None:
            return
        conn.execute(
            text("select setval(pg_get_serial_sequence(:table_name, :column_name), :value, true)"),
            {"table_name": table.name, "column_name": col.name, "value": int(max_id)},
        )
    except Exception:
        return


def migrate_sqlite_to_postgres(
    *,
    sqlite_path: Path | None = None,
    postgres_url: str | None = None,
    replace: bool = False,
    batch_size: int = 1000,
) -> dict[str, Any]:
    source_path = Path(sqlite_path or settings.db_path).resolve(strict=False)
    target_url = str(postgres_url or settings.database_url or "").strip()
    if not target_url:
        raise ValueError("A Postgres URL is required via postgres_url or THOTH_DATABASE_URL.")
    if not source_path.exists():
        raise FileNotFoundError(str(source_path))

    import thoth_terminal.discord.models  # noqa: F401

    source_engine = _engine(_sqlite_url(source_path))
    target_engine = _engine(target_url)
    Base.metadata.create_all(target_engine)

    source_meta = MetaData()
    source_meta.reflect(bind=source_engine)

    target_meta = MetaData()
    target_meta.reflect(bind=target_engine)

    copied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    with target_engine.begin() as target_conn:
        target_conn.execute(text("set session_replication_role = replica"))
        try:
            if replace:
                for table in reversed(Base.metadata.sorted_tables):
                    if table.name in target_meta.tables:
                        target_conn.execute(target_meta.tables[table.name].delete())

            for model_table in Base.metadata.sorted_tables:
                source_table = source_meta.tables.get(model_table.name)
                target_table = target_meta.tables.get(model_table.name)
                if source_table is None:
                    skipped.append({"table": model_table.name, "reason": "missing source table"})
                    continue
                if target_table is None:
                    skipped.append({"table": model_table.name, "reason": "missing target table"})
                    continue
                target_existing = _table_count(target_engine, target_table)
                if target_existing and not replace:
                    skipped.append({"table": model_table.name, "reason": "target not empty", "rows": target_existing})
                    continue

                target_cols = set(target_table.c.keys())
                source_cols = set(source_table.c.keys())
                column_names = [
                    col.name
                    for col in model_table.c
                    if col.name in source_cols and col.name in target_cols
                ]
                if not column_names:
                    skipped.append({"table": model_table.name, "reason": "no common columns"})
                    continue

                rows_copied = 0
                with source_engine.connect() as source_conn:
                    result = source_conn.execution_options(stream_results=True).execute(
                        select(*[source_table.c[name] for name in column_names])
                    )
                    batch: list[dict[str, Any]] = []
                    for row in result.mappings():
                        payload = {
                            name: _coerce_value(row.get(name), target_table.c[name].type)
                            for name in column_names
                        }
                        batch.append(payload)
                        if len(batch) >= max(1, int(batch_size)):
                            target_conn.execute(insert(target_table), batch)
                            rows_copied += len(batch)
                            batch = []
                    if batch:
                        target_conn.execute(insert(target_table), batch)
                        rows_copied += len(batch)
                copied.append({"table": model_table.name, "rows": rows_copied})

            for table in Base.metadata.sorted_tables:
                target_table = target_meta.tables.get(table.name)
                if target_table is not None:
                    _reset_postgres_sequence(target_conn, target_table)
        finally:
            target_conn.execute(text("set session_replication_role = origin"))

    return {
        "ok": True,
        "source": str(source_path),
        "target": target_url,
        "replace": replace,
        "copied": copied,
        "skipped": skipped,
    }
