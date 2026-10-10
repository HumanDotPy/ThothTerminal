"""Explicit PostgreSQL catalog index rollout; never invoked at startup.

Queries and DDL share compiled expressions. PostgreSQL itself canonicalizes the
expected definition on an empty temporary table, avoiding brittle SQL parsing.
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import String, and_, func, literal, text
from sqlalchemy.engine import Connection

from thoth_terminal.dataset_discovery import _visible, dataset_search_text, dataset_scope_index_expressions, dataset_scope_text
from thoth_terminal.models import Dataset
from thoth_terminal.research_library_service import _research_library_doc_filter


@dataclass(frozen=True)
class CatalogIndex:
    name: str
    table: str
    definition: str


def catalog_index_definitions(connection: Connection, *, catalog: str = "all") -> list[CatalogIndex]:
    if connection.dialect.name != "postgresql":
        raise ValueError("catalog index maintenance requires PostgreSQL")
    if catalog not in {"all", "datasets", "research-library"}:
        raise ValueError("unknown catalog")
    def compile_expression(expression):
        return str(expression.compile(dialect=connection.dialect,
                                     compile_kwargs={"literal_binds": True, "include_table": False}))
    result = []
    if catalog in {"all", "datasets"}:
        canonical = "canonical_dataset_id IS NULL"
        result.append(CatalogIndex("ix_datasets_search_trgm", "datasets",
                                  f"USING gin (({compile_expression(dataset_search_text())}) gin_trgm_ops) WHERE {canonical}"))
        for name, expression in dataset_scope_index_expressions().items():
            result.append(CatalogIndex(f"ix_datasets_scope_{name}", "datasets",
                                      f"USING btree (({compile_expression(expression)})) WHERE {canonical}"))
        visible = compile_expression(and_(_visible(), Dataset.canonical_dataset_id.is_(None)))
        result.append(CatalogIndex("ix_datasets_visible_updated", "datasets",
                                  f"(updated_at DESC, id DESC) INCLUDE (family) WHERE {visible}"))
    if catalog in {"all", "research-library"}:
        predicate = compile_expression(_research_library_doc_filter("postgresql"))
        result.append(CatalogIndex("ix_documents_research_library", "documents",
                                  f"(ingested_at DESC, id DESC) WHERE {predicate}"))
    return result


def _index_state(connection: Connection, schema: str, name: str):
    return connection.execute(text("""SELECT i.indrelid, i.indisvalid, i.indisready,
        am.amname, i.indisunique, i.indnkeyatts, i.indnatts,
        ARRAY(SELECT pg_get_indexdef(c.oid, n, true) FROM generate_series(1, i.indnatts) n),
        pg_get_expr(i.indexprs, i.indrelid), pg_get_expr(i.indpred, i.indrelid),
        c.reloptions, i.indcollation::text, i.indclass::text, i.indoption::text
        FROM pg_class c JOIN pg_namespace ns ON ns.oid=c.relnamespace
        JOIN pg_index i ON i.indexrelid=c.oid JOIN pg_am am ON am.oid=c.relam
        WHERE ns.nspname=:schema AND c.relname=:name"""), {"schema": schema, "name": name}).one_or_none()


def _expected_state(connection: Connection, table: str, definition: str):
    # LIKE copies column types/collations without copying data or indexes.
    temporary = "catalog_index_" + uuid4().hex
    connection.execute(text(f"CREATE TEMP TABLE {temporary} (LIKE {table})"))
    try:
        connection.execute(text(f"CREATE INDEX ON {temporary} {definition}"))
        schema, name = connection.execute(text("""SELECT ns.nspname,c.relname FROM pg_index i
            JOIN pg_class c ON c.oid=i.indexrelid JOIN pg_namespace ns ON ns.oid=c.relnamespace
            WHERE i.indrelid=to_regclass(:table)"""), {"table": temporary}).one()
        return _index_state(connection, schema, name)
    finally:
        connection.execute(text(f"DROP TABLE {temporary}"))


def _retire_scope_words(connection: Connection, *, concurrently: bool) -> list[str]:
    """Remove only the two known superseded definitions, never arbitrary drift."""
    schema, relation, oid = connection.execute(text("""SELECT ns.nspname,c.relname,c.oid
        FROM pg_class c JOIN pg_namespace ns ON ns.oid=c.relnamespace
        WHERE c.oid=to_regclass('datasets')""")).one()
    names = ("ix_datasets_scope_words", "ix_datasets_scope_words_ready")
    states = {name: _index_state(connection, schema, name) for name in names}
    if not any(states.values()):
        return []
    quote = connection.dialect.identifier_preparer.quote
    table = f"{quote(schema)}.{quote(relation)}"
    # Kept solely to recognize the retired expression index installed by the
    # previous dataset migration; active scope matching uses the phrase join.
    expression = func.string_to_array(dataset_scope_text(), literal(" ", type_=String, literal_execute=True))
    sql = str(expression.compile(dialect=connection.dialect,
                                 compile_kwargs={"literal_binds": True, "include_table": False}))
    expected = _expected_state(connection, table, f"USING gin (({sql})) WHERE canonical_dataset_id IS NULL")
    for name, state in states.items():
        if state and (state[0] != oid or state[3:] != expected[3:]):
            raise RuntimeError(f"superseded index {name} has an unknown definition; manual reviewed retirement required")
    concurrent = "CONCURRENTLY " if concurrently else ""
    for name, state in states.items():
        if state:
            connection.execute(text(f"DROP INDEX {concurrent}{quote(schema)}.{quote(name)}"))
    return [name for name, state in states.items() if state]


def ensure_catalog_indexes(connection: Connection, *, catalog: str = "all", concurrently: bool = True) -> dict:
    """Install or repair indexes, preserving valid old indexes during rebuild.

    Requires an AUTOCOMMIT connection for concurrent DDL. The caller supplies
    finite lock/statement timeouts. A session advisory lock serializes rollout.
    Interrupted builds or swaps are recovered by rerunning the same command.
    """
    definitions = catalog_index_definitions(connection, catalog=catalog)
    if concurrently and not connection.connection.driver_connection.autocommit:
        raise ValueError("concurrent index maintenance requires AUTOCOMMIT")
    lock = connection.execute(text("SELECT pg_try_advisory_lock(hashtext('thoth_catalog_indexes'))")).scalar_one()
    if not lock:
        raise RuntimeError("catalog index maintenance already running")
    quote = connection.dialect.identifier_preparer.quote
    concurrent = "CONCURRENTLY " if concurrently else ""
    result = {}
    try:
        if catalog in {"all", "datasets"}:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        for index in definitions:
            schema, relation, oid = connection.execute(text("""SELECT ns.nspname,c.relname,c.oid
                FROM pg_class c JOIN pg_namespace ns ON ns.oid=c.relnamespace
                WHERE c.oid=to_regclass(:table)"""), {"table": index.table}).one()
            table = f"{quote(schema)}.{quote(relation)}"
            expected = _expected_state(connection, table, index.definition)
            def matches(state):
                return bool(state and state[0] == oid and state[1] and state[2] and state[3:] == expected[3:])
            current = _index_state(connection, schema, index.name)
            replacement = index.name + "_replacement"
            staged = _index_state(connection, schema, replacement)
            for name, state in ((index.name, current), (replacement, staged)):
                if state and state[0] != oid:
                    raise RuntimeError(f"index {name} belongs to another table; refusing to replace it")
            if matches(current):
                if staged:
                    connection.execute(text(f"DROP INDEX {concurrent}{quote(schema)}.{quote(replacement)}"))
                result[index.name] = "unchanged"
                continue
            if not matches(staged):
                if staged:
                    connection.execute(text(f"DROP INDEX {concurrent}{quote(schema)}.{quote(replacement)}"))
                connection.execute(text(f"CREATE INDEX {concurrent}{quote(replacement)} ON {table} {index.definition}"))
            if not matches(_index_state(connection, schema, replacement)):
                raise RuntimeError(f"replacement index {replacement} failed verification")
            if current:
                connection.execute(text(f"DROP INDEX {concurrent}{quote(schema)}.{quote(index.name)}"))
            connection.execute(text(f"ALTER INDEX {quote(schema)}.{quote(replacement)} RENAME TO {quote(index.name)}"))
            result[index.name] = "replaced" if current else "installed"
        if catalog in {"all", "datasets"}:
            for name in _retire_scope_words(connection, concurrently=concurrently):
                result[name] = "retired"
        for table in {index.table for index in definitions}:
            connection.execute(text(f"ANALYZE {quote(table)}"))
        return result
    finally:
        connection.execute(text("SELECT pg_advisory_unlock(hashtext('thoth_catalog_indexes'))"))


def catalog_index_status(connection: Connection) -> dict:
    """Read-only readiness inventory; full drift validation is maintenance work."""
    rows = connection.execute(text("""SELECT c.relname,i.indisvalid,i.indisready
        FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid
        WHERE (i.indrelid=to_regclass('datasets') AND c.relname='ix_datasets_visible_updated')
           OR (i.indrelid=to_regclass('documents') AND c.relname='ix_documents_research_library')""")).all()
    states = {row[0]: bool(row[1] and row[2]) for row in rows}
    return {name: states.get(name, False) for name in
            ("ix_datasets_visible_updated", "ix_documents_research_library")}
