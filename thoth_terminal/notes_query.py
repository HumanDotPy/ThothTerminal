from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, text as sql_text
from sqlalchemy.orm import Session, load_only, selectinload

from thoth_terminal.db import utcnow
from thoth_terminal.models import Domain, Entity, Note, Tag


def _days_ago(days: int) -> datetime:
    return utcnow() - timedelta(days=days)


def search_note_ids(session: Session, query: str, *, limit: int = 5000) -> list[int]:
    q = query.strip()
    if not q:
        return []
    rows = session.execute(
        sql_text("SELECT rowid FROM note_fts WHERE note_fts MATCH :q LIMIT :limit"),
        {"q": q, "limit": int(limit)},
    ).fetchall()
    return [int(r[0]) for r in rows]


def list_notes(
    session: Session,
    *,
    query: str | None = None,
    status: str | None = "active",
    note_type: str | None = None,
    pinned: int | None = None,
    domain: str | None = None,
    tag: str | None = None,
    entity: str | None = None,
    days: int | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[Note]:
    q = session.query(Note).options(
        load_only(
            Note.id,
            Note.title,
            Note.body_md,
            Note.note_type,
            Note.status,
            Note.importance,
            Note.pinned,
            Note.updated_at,
        ),
        selectinload(Note.entities),
        selectinload(Note.domains),
        selectinload(Note.tags),
    )

    if status:
        q = q.filter(Note.status == status)

    if note_type:
        q = q.filter(Note.note_type == note_type)

    if pinned is not None:
        q = q.filter(Note.pinned.is_(bool(int(pinned))))

    if days is not None:
        q = q.filter(Note.updated_at >= _days_ago(int(days)))

    if domain:
        q = q.join(Note.domains).filter(Domain.slug == domain)

    if tag:
        q = q.join(Note.tags).filter(Tag.slug == tag)

    if entity:
        # entity can be canonical_name or id
        if entity.isdigit():
            q = q.join(Note.entities).filter(Entity.id == int(entity))
        else:
            q = q.join(Note.entities).filter(Entity.canonical_name.ilike(entity))

    if query:
        ids = search_note_ids(session, query, limit=5000)
        if ids:
            q = q.filter(Note.id.in_(ids))
        else:
            like = f"%{query.strip()}%"
            q = q.filter((Note.title.ilike(like)) | (Note.body_md.ilike(like)))

    # Default: pinned first, then recently updated
    q = q.order_by(Note.pinned.desc(), Note.updated_at.desc(), Note.id.desc())
    q = q.limit(int(limit)).offset(int(offset))
    return q.all()
