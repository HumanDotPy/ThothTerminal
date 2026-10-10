"""Online upgrade of dataset payloads and managed identities on existing rails."""
from __future__ import annotations

from collections import defaultdict

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from thoth_terminal.models import Dataset, DatasetRowContent, DatasetVersion


def _ensure_payload_immutability(conn) -> None:
    if conn.execute(text("SELECT 1 FROM pg_trigger WHERE tgrelid='dataset_row_contents'::regclass AND tgname='thoth_dataset_content_immutable' AND NOT tgisinternal")).first():
        return
    conn.execute(text("""
        CREATE OR REPLACE FUNCTION thoth_dataset_content_immutable() RETURNS trigger AS $$
        BEGIN
          IF NEW.digest<>OLD.digest OR NEW."values"::jsonb<>OLD."values"::jsonb THEN
            RAISE EXCEPTION 'Dataset payload content is immutable';
          END IF;
          RETURN NEW;
        END $$ LANGUAGE plpgsql
    """))
    conn.execute(text("CREATE TRIGGER thoth_dataset_content_immutable BEFORE UPDATE ON dataset_row_contents FOR EACH ROW EXECUTE FUNCTION thoth_dataset_content_immutable()"))


def ensure_dataset_storage_schema(engine) -> None:
    inspector = inspect(engine)
    if "datasets" not in inspector.get_table_names():
        return
    DatasetRowContent.__table__.create(engine, checkfirst=True)
    postgres = engine.dialect.name == "postgresql"
    fields = {
        "datasets": {"managed_key":"VARCHAR(255)", "canonical_dataset_id":"INTEGER REFERENCES datasets(id)"},
        "dataset_versions": {"content_revision":"INTEGER NOT NULL DEFAULT 0"},
        "dataset_rows": {"content_id":"INTEGER REFERENCES dataset_row_contents(id)"},
    }
    with engine.begin() as conn:
        for table, additions in fields.items():
            columns = {column["name"] for column in inspect(conn).get_columns(table)}
            for name, ddl in additions.items():
                if name not in columns:
                    conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {ddl}'))
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_datasets_managed_key ON datasets(managed_key)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_datasets_canonical_dataset_id ON datasets(canonical_dataset_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_dataset_rows_content_id ON dataset_rows(content_id)"))
        if postgres:
            _ensure_payload_immutability(conn)
        if postgres and "values" in {c["name"] for c in inspect(conn).get_columns("dataset_rows")}:
            # Bridge old runtime writers during the online conversion. Removed at finalization.
            conn.execute(text('ALTER TABLE dataset_rows ALTER COLUMN "values" DROP NOT NULL'))
            conn.execute(text("""
                CREATE OR REPLACE FUNCTION thoth_dataset_payload_bridge() RETURNS trigger AS $$
                BEGIN
                  IF NEW."values" IS NOT NULL THEN
                    INSERT INTO dataset_row_contents(digest,"values")
                    VALUES(encode(sha256(convert_to(NEW."values"::jsonb::text,'UTF8')),'hex'),NEW."values")
                    ON CONFLICT(digest) DO UPDATE SET digest=excluded.digest
                    RETURNING id INTO NEW.content_id;
                  END IF;
                  RETURN NEW;
                END $$ LANGUAGE plpgsql
            """))
            conn.execute(text("DROP TRIGGER IF EXISTS thoth_dataset_payload_bridge ON dataset_rows"))
            conn.execute(text('CREATE TRIGGER thoth_dataset_payload_bridge BEFORE INSERT OR UPDATE OF "values" ON dataset_rows FOR EACH ROW EXECUTE FUNCTION thoth_dataset_payload_bridge()'))


def migrate_dataset_payloads(engine, *, batch_size: int = 5000, progress=None) -> dict:
    ensure_dataset_storage_schema(engine)
    columns = {c["name"] for c in inspect(engine).get_columns("dataset_rows")}
    if "values" not in columns:
        return {"migrated_rows":0,"remaining":0}
    migrated = 0
    cursor = 0
    if engine.dialect.name != "postgresql":
        import hashlib
        import json
        with engine.begin() as conn:
            for row_id, payload in conn.execute(text('SELECT id,"values" FROM dataset_rows WHERE content_id IS NULL')):
                values = json.loads(payload) if isinstance(payload,str) else payload
                encoded = json.dumps(values,sort_keys=True,allow_nan=False)
                digest = hashlib.sha256(encoded.encode()).hexdigest()
                conn.execute(text('INSERT INTO dataset_row_contents(digest,"values") VALUES(:digest,:payload) ON CONFLICT(digest) DO NOTHING'),{"digest":digest,"payload":encoded})
                conn.execute(text("UPDATE dataset_rows SET content_id=(SELECT id FROM dataset_row_contents WHERE digest=:digest) WHERE id=:id"),{"digest":digest,"id":row_id})
                migrated += 1
            conn.execute(text('ALTER TABLE dataset_rows DROP COLUMN "values"'))
        return {"migrated_rows":migrated,"remaining":0}
    # A newly added content_id initially has no statistics. Its NULL predicate
    # otherwise looks selective and can scan the entire heap for every batch.
    with engine.begin() as conn:
        conn.execute(text("ANALYZE dataset_rows"))
    while True:
        with engine.begin() as conn:
            ids = list(conn.execute(text("SELECT id FROM dataset_rows WHERE id>:cursor AND content_id IS NULL ORDER BY id LIMIT :lim"),{"cursor":cursor,"lim":batch_size}).scalars())
            if not ids:
                break
            conn.execute(text('''
                INSERT INTO dataset_row_contents(digest,"values")
                SELECT DISTINCT encode(sha256(convert_to(r."values"::jsonb::text,'UTF8')),'hex'),r."values"::jsonb
                FROM dataset_rows r WHERE r.id=ANY(:ids)
                ON CONFLICT(digest) DO NOTHING
            '''),{"ids":ids})
            conn.execute(text('''
                UPDATE dataset_rows r SET content_id=c.id FROM dataset_row_contents c
                WHERE r.id=ANY(:ids) AND c.digest=encode(sha256(convert_to(r."values"::jsonb::text,'UTF8')),'hex')
            '''),{"ids":ids})
            cursor = ids[-1]
            migrated += len(ids)
        if progress:
            progress({"migrated_rows":migrated,"last_id":cursor})
    with engine.connect() as conn:
        remaining = conn.execute(text("SELECT count(*) FROM dataset_rows WHERE content_id IS NULL")).scalar_one()
    if remaining:
        raise RuntimeError(f"Dataset payload migration incomplete: {remaining} memberships missing content")
    return {"migrated_rows":migrated,"remaining":0}


def finalize_dataset_payloads(engine) -> None:
    """Retire the inline payload only after all runtime processes use shared content."""
    if engine.dialect.name != "postgresql":
        return
    columns = {column["name"]: column for column in inspect(engine).get_columns("dataset_rows")}
    if columns["content_id"]["nullable"]:
        checks = {check["name"] for check in inspect(engine).get_check_constraints("dataset_rows")}
        with engine.begin() as conn:
            conn.execute(text("SET LOCAL lock_timeout='5s'"))
            if "ck_dataset_rows_content_required" not in checks:
                conn.execute(text("ALTER TABLE dataset_rows ADD CONSTRAINT ck_dataset_rows_content_required CHECK (content_id IS NOT NULL) NOT VALID"))
        # Validation permits concurrent reads/writes; the subsequent NOT NULL
        # change can use this proof instead of scanning under an exclusive lock.
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE dataset_rows VALIDATE CONSTRAINT ck_dataset_rows_content_required"))
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL lock_timeout='5s'"))
        if conn.execute(text("SELECT 1 FROM dataset_rows WHERE content_id IS NULL LIMIT 1")).first():
            raise RuntimeError("Cannot finalize incomplete dataset payload migration")
        conn.execute(text("ALTER TABLE dataset_rows ALTER COLUMN content_id SET NOT NULL"))
        conn.execute(text("ALTER TABLE dataset_rows DROP CONSTRAINT IF EXISTS ck_dataset_rows_content_required"))
        conn.execute(text("DROP TRIGGER IF EXISTS thoth_dataset_payload_bridge ON dataset_rows"))
        conn.execute(text("DROP FUNCTION IF EXISTS thoth_dataset_payload_bridge()"))
        conn.execute(text('ALTER TABLE dataset_rows DROP COLUMN IF EXISTS "values"'))
        _ensure_payload_immutability(conn)


def consolidate_managed_datasets(engine, *, progress=None) -> dict:
    from thoth_terminal.dataset_identity import legacy_managed_company_dataset_key, canonical_managed_dataset_slug
    from thoth_terminal.dataset_storage import copy_version_memberships
    from thoth_terminal.dataset_mirror_cache import mark_dataset_mirror_dirty
    from thoth_terminal.db import utcnow
    from thoth_terminal.models import DatasetLink, DatasetView, StructuredDataFact, SupplyChainMetricBinding, IntelligenceEvent

    groups = defaultdict(list)
    with Session(engine) as session:
        for dataset in session.query(Dataset).filter(Dataset.canonical_dataset_id.is_(None)).yield_per(500):
            key = dataset.managed_key or legacy_managed_company_dataset_key(dataset)
            if key:
                groups[key].append(dataset.id)
    aliases = 0
    canonical_count = 0
    for key, candidates in sorted(groups.items()):
        with Session(engine) as session, session.begin():
            if engine.dialect.name == "postgresql":
                session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),{"key":f"dataset-managed:{key}"})
            # Re-read the current producer state after obtaining the write lock.
            ids = candidates
            datasets = list(session.query(Dataset).filter(Dataset.id.in_(ids),Dataset.canonical_dataset_id.is_(None)).with_for_update())
            if not datasets:
                continue
            base = canonical_managed_dataset_slug(f"{datasets[0].extra['company_slug']}-{datasets[0].family.replace('_','-')}")
            canonical = min(datasets,key=lambda d:(d.managed_key!=key,d.slug!=base,d.id))
            current = max(datasets,key=lambda d:(d.updated_at,d.id))
            alias_ids = [dataset.id for dataset in datasets if dataset.id != canonical.id]
            canonical.managed_key = key
            versions = list(session.query(DatasetVersion).filter(
                DatasetVersion.dataset_id.in_({canonical.id, current.id}),
                DatasetVersion.status.in_(["draft", "published"]),
            ).order_by(DatasetVersion.version.desc(), DatasetVersion.updated_at.desc(), DatasetVersion.id.desc()))
            version_by_status = {}
            for version in versions:
                version_by_status.setdefault((version.dataset_id, version.status), version)
            source = version_by_status.get((current.id, "draft")) or version_by_status.get((current.id, "published"))
            copy_current = current.id != canonical.id and source is not None

            # A unique negative number for each retired draft permits repeated
            # repair without historical version-0 collisions. No old version or
            # row is deleted, and each retains its original owning dataset ID.
            retire_ids = alias_ids + ([canonical.id] if copy_current else [])
            if retire_ids:
                session.query(DatasetVersion).filter(
                    DatasetVersion.dataset_id.in_(retire_ids), DatasetVersion.status == "draft",
                ).update({DatasetVersion.status: "historical", DatasetVersion.version: -DatasetVersion.id},
                         synchronize_session=False)
            if copy_current:
                now = utcnow()
                draft = DatasetVersion(dataset_id=canonical.id, version=0, status="draft", label=source.label,
                                       source_event=source.source_event, created_at=now, updated_at=now,
                                       extra=dict(source.extra or {}))
                session.add(draft)
                session.flush()
                copy_version_memberships(session, source.id, draft.id, now)
                mark_dataset_mirror_dirty(session, draft.id, replace=True, columns_changed=True)
                for name in ("title", "description", "category", "family", "source_kind", "status", "updated_at"):
                    setattr(canonical, name, getattr(current, name))
                canonical.extra = dict(current.extra or {})

            if alias_ids:
                # Read only actual reference objects, once for the entire group.
                # Most duplicate ledgers have no views, links, or bindings.
                views = list(session.query(DatasetView).filter(DatasetView.dataset_id.in_(ids)).order_by(DatasetView.id))
                slugs = {view.slug for view in views if view.dataset_id == canonical.id}
                for view in views:
                    if view.dataset_id == canonical.id:
                        continue
                    if view.slug in slugs:
                        history_slug = f"{view.slug[:96]}-history-{view.id}"
                        candidate_slug, suffix = history_slug, 2
                        while candidate_slug in slugs:
                            candidate_slug = f"{history_slug}-{suffix}"
                            suffix += 1
                        view.slug = candidate_slug
                    slugs.add(view.slug)
                    view.dataset_id = canonical.id

                links = list(session.query(DatasetLink).filter(DatasetLink.dataset_id.in_(ids)).order_by(DatasetLink.id))
                link_key = lambda link: (link.object_type, link.object_id, link.relation)
                link_keys = {link_key(link) for link in links if link.dataset_id == canonical.id}
                for link in links:
                    if link.dataset_id != canonical.id and link_key(link) not in link_keys:
                        link.dataset_id = canonical.id
                        link_keys.add(link_key(link))
                    # Identical links keep their old IDs under the surviving
                    # alias; its canonical target supplies the equivalent link.

                bindings = list(session.query(SupplyChainMetricBinding).filter(
                    SupplyChainMetricBinding.dataset_id.in_(ids),
                ).order_by(SupplyChainMetricBinding.id))
                def binding_key(binding):
                    return (binding.snapshot_id, binding.scope_type, binding.scope_key, binding.metric_kind,
                            binding.dataset_view_id, binding.column_key)
                binding_keys = {binding_key(binding) for binding in bindings if binding.dataset_id == canonical.id}
                for binding in bindings:
                    if binding.dataset_id != canonical.id and binding_key(binding) not in binding_keys:
                        binding.dataset_id = canonical.id
                        binding_keys.add(binding_key(binding))

                for model in (StructuredDataFact, IntelligenceEvent):
                    session.query(model).filter(model.dataset_id.in_(alias_ids)).update(
                        {model.dataset_id: canonical.id}, synchronize_session=False,
                    )
                session.query(Dataset).filter(Dataset.id.in_(alias_ids)).update(
                    {Dataset.canonical_dataset_id: canonical.id, Dataset.managed_key: None},
                    synchronize_session=False,
                )
                aliases += len(alias_ids)
            canonical_count += 1
            session.flush()
        if progress:
            progress({"canonical_ledgers":canonical_count,"aliases":aliases})
    return {"canonical_ledgers":canonical_count,"aliases":aliases}
