"""Commit-bound invalidation and incremental maintenance of the analytic cache.

PostgreSQL is canonical. Transaction-local dirty sets are discarded on rollback;
DuckDB failures leave its revision stale and cannot invalidate a canonical commit.
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Iterable

from sqlalchemy import event, update
from sqlalchemy.orm import Session

from thoth_terminal.models import DatasetVersion

_DIRTY = "dataset_mirror_dirty"
_REVISIONS = "dataset_mirror_committed_revisions"
_SNAPSHOTS = "dataset_mirror_savepoints"
_LOG = logging.getLogger(__name__)


def mark_dataset_mirror_dirty(
    session: Session,
    version_id: int,
    *,
    row_keys: Iterable[str] | None = None,
    replace: bool = False,
    columns_changed: bool = False,
) -> None:
    """Coalesce a canonical write into the owning transaction's commit batch."""
    pending = session.info.setdefault(_DIRTY, {})
    change = pending.setdefault(
        int(version_id), {"row_keys": set(), "replace": False, "columns_changed": False}
    )
    change["replace"] |= bool(replace or row_keys is None)
    change["columns_changed"] |= bool(columns_changed)
    if not change["replace"]:
        change["row_keys"].update(str(key) for key in row_keys or ())
        if len(change["row_keys"]) > 50_000:
            change["replace"] = True
    if change["replace"]:
        change["row_keys"].clear()


def dataset_mirror_pending(session: Session, version_id: int) -> bool:
    return int(version_id) in session.info.get(_DIRTY, {})


@event.listens_for(Session, "after_transaction_create")
def _savepoint_started(session: Session, transaction: Any) -> None:
    if transaction.nested:
        session.info.setdefault(_SNAPSHOTS, {})[transaction] = copy.deepcopy(
            session.info.get(_DIRTY, {})
        )


@event.listens_for(Session, "before_commit")
def _advance_revisions(session: Session) -> None:
    if session.in_nested_transaction():
        return
    revisions = {}
    # Lock/update in a stable order so concurrent multi-version batches agree.
    for version_id in sorted(session.info.get(_DIRTY, {})):
        revision = session.execute(
            update(DatasetVersion)
            .where(DatasetVersion.id == version_id)
            .values(content_revision=DatasetVersion.content_revision + 1)
            .returning(DatasetVersion.content_revision)
            .execution_options(synchronize_session="fetch")
        ).scalar_one_or_none()
        revisions[version_id] = int(revision) if revision is not None else None
    session.info[_REVISIONS] = revisions


@event.listens_for(Session, "after_commit")
def _committed(session: Session) -> None:
    if session.in_nested_transaction():
        return
    changes = session.info.pop(_DIRTY, {})
    revisions = session.info.pop(_REVISIONS, {})
    session.info.pop(_SNAPSHOTS, None)
    if not changes:
        return
    from thoth_terminal.duckdb_store import maintain_committed_dataset_mirrors

    bind = session.get_bind()
    # Never read through the committing Session/Connection: it can still be in
    # SQLAlchemy's committed transaction state, or belong to an external txn.
    if hasattr(bind, "engine"):
        bind = bind.engine
    try:
        with Session(bind=bind) as committed_session:
            maintain_committed_dataset_mirrors(committed_session, changes, revisions)
    except Exception:
        _LOG.exception("Committed dataset batch left the derived DuckDB cache stale")


@event.listens_for(Session, "after_soft_rollback")
def _rolled_back(session: Session, transaction: Any) -> None:
    if transaction.nested:
        snapshot = session.info.get(_SNAPSHOTS, {}).pop(transaction, {})
        session.info[_DIRTY] = snapshot
    elif transaction.parent is None:
        session.info.pop(_DIRTY, None)
        session.info.pop(_REVISIONS, None)
        session.info.pop(_SNAPSHOTS, None)


@event.listens_for(Session, "after_transaction_end")
def _transaction_ended(session: Session, transaction: Any) -> None:
    # Session.close() can end an abandoned transaction without a rollback
    # event. A subsequently reused Session must not inherit its candidate keys.
    if transaction.parent is None:
        session.info.pop(_DIRTY, None)
        session.info.pop(_REVISIONS, None)
        session.info.pop(_SNAPSHOTS, None)
