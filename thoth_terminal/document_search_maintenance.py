"""Incremental PostgreSQL document vectors and bounded online maintenance.

Nullable vectors never change the text-search contract. Heavy index creation and
backfill are explicit operations; startup only installs metadata and the trigger.
"""
from __future__ import annotations

from contextlib import contextmanager
import re
import threading
import time

from sqlalchemy import text
from sqlalchemy.engine import Connection

VECTOR_INDEX = "idx_documents_search_vector_v1"
MISSING_INDEX = "idx_documents_search_vector_missing"
LEGACY_INDEX = "idx_documents_fts_simple"
TRIGGER = "documents_search_vector_refresh"
FUNCTION = "documents_search_vector_refresh"


def document_vector_sql(prefix: str = "") -> str:
    if prefix and not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*\.", prefix):
        raise ValueError("invalid document column prefix")
    return ("to_tsvector('simple', " + f"coalesce({prefix}title, '') || ' ' || coalesce({prefix}normalized_text, '') || ' ' || coalesce({prefix}translation_en, '')" + ")")


def coalesced_document_vector_sql(prefix: str = "") -> str:
    return f"coalesce({prefix}search_vector, {document_vector_sql(prefix)})"


def _target(connection: Connection) -> tuple[str, str, int]:
    if connection.dialect.name != "postgresql":
        raise ValueError("document vector maintenance requires PostgreSQL")
    row = connection.execute(text("SELECT n.nspname, c.relname, c.oid FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.oid=to_regclass('documents')")).one()
    quote = connection.dialect.identifier_preparer.quote
    return quote(row[0]), f"{quote(row[0])}.{quote(row[1])}", int(row[2])


def ensure_document_search_schema(connection: Connection) -> None:
    """Metadata-only nullable column addition; existing documents stay NULL."""
    schema, table, oid = _target(connection)
    connection.execute(text("SET LOCAL lock_timeout = '2s'"))
    connection.execute(text("SET LOCAL statement_timeout = '10s'"))
    exists = connection.execute(text("SELECT EXISTS(SELECT 1 FROM pg_attribute WHERE attrelid=:oid AND attname='search_vector' AND NOT attisdropped)"), {"oid": oid}).scalar_one()
    if not exists:
        connection.execute(text(f"ALTER TABLE {table} ADD COLUMN search_vector tsvector"))
    connection.execute(text(f"CREATE OR REPLACE FUNCTION {schema}.{FUNCTION}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN NEW.search_vector := {document_vector_sql('NEW.')}; RETURN NEW; END $$"))
    trigger_exists = connection.execute(text("SELECT EXISTS(SELECT 1 FROM pg_trigger WHERE tgrelid=:oid AND tgname=:name AND NOT tgisinternal)"), {"oid": oid, "name": TRIGGER}).scalar_one()
    if not trigger_exists:
        connection.execute(text(f"CREATE TRIGGER {TRIGGER} BEFORE INSERT OR UPDATE OF title, normalized_text, translation_en ON {table} FOR EACH ROW EXECUTE FUNCTION {schema}.{FUNCTION}()"))


def document_search_indexes(connection: Connection) -> dict[str, dict]:
    _, _, oid = _target(connection)
    rows = connection.execute(text("SELECT c.relname, i.indisvalid, i.indisready, c.reloptions, pg_relation_size(c.oid) FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid WHERE i.indrelid=:oid AND c.relname=ANY(:names)"),
                              {"oid": oid, "names": [LEGACY_INDEX, VECTOR_INDEX, MISSING_INDEX]}).all()
    return {row[0]: {"valid": row[1], "ready": row[2], "options": row[3] or [], "bytes": int(row[4])} for row in rows}


def document_search_status(connection: Connection) -> dict:
    _, table, oid = _target(connection)
    vector_exists = connection.execute(text("SELECT EXISTS(SELECT 1 FROM pg_attribute WHERE attrelid=:oid AND attname='search_vector' AND NOT attisdropped)"), {"oid": oid}).scalar_one()
    missing = "count(*) FILTER (WHERE search_vector IS NULL)" if vector_exists else "NULL::bigint"
    row = connection.execute(text(f"SELECT count(*), {missing} FROM {table}")).one()
    size = connection.execute(text("SELECT pg_table_size(:oid), pg_indexes_size(:oid), pg_total_relation_size(:oid)"), {"oid": oid}).one()
    return {"indexes": document_search_indexes(connection), "documents": row[0], "missing_vectors": row[1],
            "vector_column_exists": vector_exists, "relation_bytes": dict(zip(("table", "indexes", "total"), size))}


def document_search_ready(session) -> bool:
    """Cache readiness only for the lifetime of this caller's Session."""
    key = "document_search_vector_index_ready"
    if key not in session.info:
        session.info[key] = bool(session.execute(text("SELECT EXISTS(SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid WHERE i.indrelid=to_regclass('documents') AND c.relname=:name AND i.indisvalid AND i.indisready AND 'fastupdate=off'=ANY(coalesce(c.reloptions, ARRAY[]::text[])))"), {"name": VECTOR_INDEX}).scalar_one())
    return session.info[key]


def ensure_document_search_indexes(connection: Connection, *, concurrently: bool, selection_only: bool = False) -> dict:
    """Caller chooses a finite timeout and cancellation policy before this call.

    Concurrent builds require an AUTOCOMMIT connection. A canceled concurrent
    build is invalid and cannot enable vector queries; rerunning drops that
    invalid index and retries, while leaving the working legacy index intact.
    """
    schema, table, _ = _target(connection)
    state = document_search_indexes(connection)
    definitions = {VECTOR_INDEX: f"USING gin (({coalesced_document_vector_sql()})) WITH (fastupdate=off)",
                   MISSING_INDEX: "(id) WHERE search_vector IS NULL"}
    if selection_only:
        definitions.pop(VECTOR_INDEX)
    concurrent = "CONCURRENTLY " if concurrently else ""
    for name, definition in definitions.items():
        existing = state.get(name)
        if existing and existing["valid"] and existing["ready"]:
            if name == VECTOR_INDEX and "fastupdate=off" not in existing["options"]:
                # ALTER alone leaves an existing GIN pending list behind.
                # Never declare that experimentally unsafe state ready.
                raise RuntimeError("vector index requires a rebuild with fastupdate=off; its existing pending list must not be hidden")
            continue
        if existing:
            connection.execute(text(f"DROP INDEX {concurrent}{schema}.{name}"))
        connection.execute(text(f"CREATE INDEX {concurrent}{name} ON {table} {definition}"))
    return document_search_indexes(connection)


def cleanup_invalid_document_search_indexes(connection: Connection) -> list[str]:
    schema, _, _ = _target(connection)
    removed = []
    for name, state in document_search_indexes(connection).items():
        if name in (VECTOR_INDEX, MISSING_INDEX) and not state["valid"]:
            connection.execute(text(f"DROP INDEX CONCURRENTLY {schema}.{name}"))
            removed.append(name)
    return removed


def retire_legacy_document_search_index(connection: Connection, *, runtime_uses_vectors: bool) -> bool:
    """Explicit post-restart retirement; never performed during startup."""
    if not runtime_uses_vectors:
        raise ValueError("legacy retirement requires a restarted runtime using the vector index")
    schema, _, _ = _target(connection)
    indexes = document_search_indexes(connection)
    current = indexes.get(VECTOR_INDEX, {})
    if not (current.get("valid") and current.get("ready") and "fastupdate=off" in current.get("options", [])):
        raise RuntimeError("a valid fastupdate-off vector index is required before retirement")
    if LEGACY_INDEX not in indexes:
        return False
    connection.execute(text(f"DROP INDEX CONCURRENTLY {schema}.{LEGACY_INDEX}"))
    return True


def ensure_document_search_startup(connection: Connection) -> None:
    ensure_document_search_schema(connection)
    # Empty databases can initialize indexes quickly. Populated databases keep
    # the legacy query until the explicit, concurrent index rollout completes.
    if not connection.execute(text("SELECT EXISTS(SELECT 1 FROM documents LIMIT 1)")).scalar_one():
        ensure_document_search_indexes(connection, concurrently=False)


def backfill_document_search_batch(connection: Connection, *, batch_size: int = 32,
                                   max_input_bytes: int = 16 * 1024 * 1024) -> dict:
    """Locked, bounded NULL selection makes committed batches resumable.

    The caller owns commit/rollback. SKIP LOCKED protects active ingestion;
    skipped rows remain NULL and are selected on a later run without a cursor.
    """
    if not 1 <= batch_size <= 256 or not 1 <= max_input_bytes <= 64 * 1024 * 1024:
        raise ValueError("invalid document backfill batch/input bound")
    _, table, _ = _target(connection)
    indexes = document_search_indexes(connection)
    if not (indexes.get(MISSING_INDEX, {}).get("valid") and indexes[MISSING_INDEX]["ready"]):
        raise RuntimeError("a valid NULL-selection index is required before backfill")
    candidates = connection.execute(text(f"SELECT id, octet_length(coalesce(title,'')) + octet_length(coalesce(normalized_text,'')) + octet_length(coalesce(translation_en,'')) + 2 AS input_bytes FROM {table} WHERE search_vector IS NULL ORDER BY id LIMIT :limit FOR UPDATE SKIP LOCKED"), {"limit": batch_size}).all()
    identifiers, input_bytes = [], 0
    for identifier, length in candidates:
        if input_bytes + length > max_input_bytes:
            if not identifiers:
                return {"updated": 0, "input_bytes": 0, "blocked_document_id": identifier,
                        "blocked_document_input_bytes": length, "empty": False}
            break
        identifiers.append(identifier)
        input_bytes += length
    if not identifiers:
        return {"updated": 0, "input_bytes": 0, "empty": True}
    rows = connection.execute(text(f"UPDATE {table} SET search_vector={document_vector_sql()} WHERE id=ANY(:ids) AND search_vector IS NULL RETURNING id, pg_column_size(search_vector)"), {"ids": identifiers}).all()
    return {"updated": len(rows), "document_ids": [row[0] for row in rows], "input_bytes": input_bytes,
            "vector_datum_bytes": sum(row[1] for row in rows), "empty": False}


@contextmanager
def cancel_document_search_on_pressure(connection: Connection, capture, *, deadline: float | None = None):
    """Cancel database work as soon as the existing native guard trips."""
    done = threading.Event()
    driver = connection.connection.driver_connection
    def watch():
        while not done.wait(.05):
            if deadline is not None and time.monotonic() >= deadline:
                capture.failure = "document search maintenance wall budget reached"
            if capture.pressure_reason or capture.failure:
                try:
                    driver.cancel()
                except Exception as error:
                    capture.record({"kind": "search_query_cancel_failure", "error_type": type(error).__name__})
                return
    watcher = threading.Thread(target=watch, name="document-search-pressure-cancel", daemon=True)
    watcher.start()
    try:
        yield
    finally:
        done.set()
        watcher.join(timeout=2)
        if watcher.is_alive():
            raise RuntimeError("document search cancellation watcher did not stop")
