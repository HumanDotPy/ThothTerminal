"""Immutable shared dataset payloads; row IDs remain stable version memberships."""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import inspect, text

from thoth_terminal.models import Dataset, DatasetRow, DatasetRowContent


def canonical_dataset(session, dataset: Dataset) -> Dataset:
    seen = set()
    while dataset.canonical_dataset_id is not None:
        if dataset.id in seen:
            raise ValueError("Dataset alias cycle")
        seen.add(dataset.id)
        dataset = session.get(Dataset, int(dataset.canonical_dataset_id))
        if dataset is None:
            raise ValueError("Dataset alias target is missing")
    return dataset


def replace_version_rows(session, version_id: int, rows: list[dict], now) -> set[str]:
    """Update memberships in bounded batches, retaining IDs for unchanged keys."""
    keys = set()
    changed = set()
    conn = session.connection()
    postgres = conn.dialect.name == "postgresql"
    for offset in range(0, len(rows), 500):
        chunk = [
            {"row_key": str(row.get("row_key") or offset + i + 1),
             "sort_order": offset + i, "values": row.get("values") or {}}
            for i, row in enumerate(rows[offset:offset + 500])
        ]
        for row in chunk:
            if row["row_key"] in keys:
                raise ValueError(f"Duplicate row key: {row['row_key']}")
            keys.add(row["row_key"])
        if postgres:
            payload = json.dumps(chunk, allow_nan=False)
            conn.execute(text("""
                INSERT INTO dataset_row_contents (digest, "values")
                SELECT DISTINCT encode(sha256(convert_to((item->'values')::text, 'UTF8')), 'hex'),
                                item->'values'
                FROM jsonb_array_elements(CAST(:payload AS jsonb)) item
                ON CONFLICT (digest) DO NOTHING
            """), {"payload": payload})
            # DISTINCT JSON needs JSONB equality; the input includes arbitrary nested values.
            changed.update(conn.execute(text("""
                INSERT INTO dataset_rows
                    (dataset_version_id,row_key,content_id,sort_order,created_at,updated_at)
                SELECT :version_id,item->>'row_key',c.id,(item->>'sort_order')::integer,:now,:now
                FROM jsonb_array_elements(CAST(:payload AS jsonb)) item
                JOIN dataset_row_contents c ON c.digest =
                    encode(sha256(convert_to((item->'values')::text, 'UTF8')), 'hex')
                ON CONFLICT (dataset_version_id,row_key) DO UPDATE
                    SET content_id=excluded.content_id,sort_order=excluded.sort_order,
                        updated_at=excluded.updated_at
                    WHERE dataset_rows.content_id<>excluded.content_id
                       OR dataset_rows.sort_order<>excluded.sort_order
                RETURNING row_key
            """), {"payload": payload, "version_id": version_id, "now": now}).scalars())
        else:
            existing = {row.row_key: row for row in session.query(DatasetRow).filter(
                DatasetRow.dataset_version_id == version_id,
                DatasetRow.row_key.in_([item["row_key"] for item in chunk]),
            )}
            for item in chunk:
                row = existing.get(item["row_key"])
                if row is None:
                    session.add(DatasetRow(dataset_version_id=version_id, row_key=item["row_key"],
                                           values=item["values"], sort_order=item["sort_order"],
                                           created_at=now, updated_at=now))
                    changed.add(item["row_key"])
                elif row.values != item["values"] or row.sort_order != item["sort_order"]:
                    row.values = item["values"]
                    row.sort_order = item["sort_order"]
                    row.updated_at = now
                    changed.add(item["row_key"])
            session.flush()
    # A temporary table bounds the anti-join without a catalog-sized NOT IN list.
    if postgres:
        conn.execute(text("CREATE TEMP TABLE IF NOT EXISTS thoth_dataset_row_keys (row_key text PRIMARY KEY) ON COMMIT DROP"))
        conn.execute(text("TRUNCATE thoth_dataset_row_keys"))
        ordered_keys = sorted(keys)
        for offset in range(0, len(ordered_keys), 1000):
            conn.execute(text("INSERT INTO thoth_dataset_row_keys SELECT unnest(CAST(:keys AS text[]))"),
                         {"keys": ordered_keys[offset:offset + 1000]})
        changed.update(conn.execute(text("""
            DELETE FROM dataset_rows r WHERE r.dataset_version_id=:version_id
              AND NOT EXISTS (SELECT 1 FROM thoth_dataset_row_keys k WHERE k.row_key=r.row_key)
            RETURNING r.row_key
        """), {"version_id": version_id}).scalars())
    else:
        for row in session.query(DatasetRow).filter_by(dataset_version_id=version_id):
            if row.row_key not in keys:
                changed.add(row.row_key)
                session.delete(row)
        session.flush()
    if postgres:
        for row in list(session.identity_map.values()):
            if isinstance(row, DatasetRow) and row.dataset_version_id == version_id:
                session.expire(row)
    return changed


def version_fingerprint(session, version_id: int) -> str:
    from thoth_terminal.models import DatasetColumn

    digest = hashlib.sha256()
    for col in session.query(DatasetColumn).filter_by(dataset_version_id=version_id).order_by(
        DatasetColumn.sort_order, DatasetColumn.key,
    ):
        digest.update(json.dumps([col.key,col.label,col.data_type,col.role,col.sort_order,col.config],
                                 sort_keys=True,allow_nan=False).encode())
        digest.update(b"\n")
    digest.update(b"rows\n")
    for key, order, payload in session.query(DatasetRow.row_key, DatasetRow.sort_order,
                                             DatasetRowContent.digest).join(DatasetRowContent).filter(
        DatasetRow.dataset_version_id == version_id,
    ).order_by(DatasetRow.sort_order, DatasetRow.row_key).yield_per(500):
        digest.update(json.dumps([key,order,payload]).encode())
        digest.update(b"\n")
    return digest.hexdigest()


def copy_version_memberships(session, source_id: int, target_id: int, now) -> None:
    from sqlalchemy import insert, literal, select
    from thoth_terminal.models import DatasetColumn

    session.execute(insert(DatasetRow).from_select(
        ["dataset_version_id","row_key","content_id","sort_order","created_at","updated_at"],
        select(literal(target_id), DatasetRow.row_key, DatasetRow.content_id,
               DatasetRow.sort_order,literal(now),literal(now)).where(
            DatasetRow.dataset_version_id == source_id,
        ).order_by(DatasetRow.sort_order,DatasetRow.id),
    ))
    session.execute(insert(DatasetColumn).from_select(
        ["dataset_version_id","key","label","data_type","role","sort_order","config"],
        select(literal(target_id),DatasetColumn.key,DatasetColumn.label,DatasetColumn.data_type,
               DatasetColumn.role,DatasetColumn.sort_order,DatasetColumn.config).where(
            DatasetColumn.dataset_version_id == source_id,
        ),
    ))


def prepare_dataset_row_payloads(session) -> None:
    for content in session.dirty:
        if isinstance(content, DatasetRowContent) and any(
            inspect(content).attrs[key].history.has_changes() for key in ("digest", "values")
        ):
            raise ValueError("Dataset row content is immutable; assign a new row value instead")
    pending = [
        row for row in session.new.union(session.dirty)
        if isinstance(row, DatasetRow) and "_pending_dataset_values" in row.__dict__
    ]
    if not pending:
        return
    conn = session.connection()
    content_by_payload = {}
    for row in pending:
        payload = json.dumps(row.__dict__["_pending_dataset_values"], sort_keys=True, allow_nan=False)
        content = content_by_payload.get(payload)
        if content is None:
            if conn.dialect.name == "postgresql":
                content_id = conn.execute(text("""
                    INSERT INTO dataset_row_contents (digest, "values")
                    SELECT encode(sha256(convert_to(CAST(:payload AS jsonb)::text, 'UTF8')), 'hex'),
                           CAST(:payload AS json)
                    ON CONFLICT (digest) DO UPDATE SET digest = excluded.digest
                    RETURNING id
                """), {"payload": payload}).scalar_one()
            else:
                digest = hashlib.sha256(payload.encode()).hexdigest()
                conn.execute(text("""
                    INSERT INTO dataset_row_contents (digest, "values") VALUES (:digest, :payload)
                    ON CONFLICT (digest) DO NOTHING
                """), {"digest": digest, "payload": payload})
                content_id = conn.execute(text(
                    "SELECT id FROM dataset_row_contents WHERE digest = :digest"
                ), {"digest": digest}).scalar_one()
            content = session.get(DatasetRowContent, content_id)
            content_by_payload[payload] = content
        row.content = content
        del row.__dict__["_pending_dataset_values"]
