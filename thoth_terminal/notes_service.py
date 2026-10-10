from __future__ import annotations

import html
import re
from datetime import datetime
from typing import Any

from markdown_it import MarkdownIt
from sqlalchemy.orm import Session, load_only

from thoth_terminal.db import utcnow
from thoth_terminal.house_view_service import normalize_note_provenance_extra
from thoth_terminal.models import (
    Document,
    Domain,
    Entity,
    EventType,
    Note,
    NoteDocument,
    NoteProposition,
    NoteThesis,
    Proposition,
    Tag,
    Thesis,
)
from thoth_terminal.thesis_service import resolve_domains, resolve_entities, resolve_event_types, resolve_tags


DOC_TOKEN_RE = re.compile(r"\[\[doc:(?P<id>\d+)\]\]", re.IGNORECASE)
THESIS_TOKEN_RE = re.compile(r"\[\[thesis:(?P<id>\d+)\]\]", re.IGNORECASE)
_MARKDOWN_RENDERER = MarkdownIt("commonmark", {"html": False, "linkify": True})


NOTE_TYPES = [
    "general",
    "discord",
    "sector_view",
    "framework",
    "assumptions_model",
    "decision_log",
    "postmortem",
    "meeting_notes",
    "watchlist",
    "risk_register",
]

NOTE_LINK_RELATIONS = ["context", "assumption", "impact", "counterpoint", "reference"]
NOTE_PROP_RELATIONS = ["references", "assumption", "derived_from"]


def split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [p.strip() for p in value.split(",") if p.strip()]


def create_note(
    session: Session,
    *,
    title: str,
    body_md: str,
    note_type: str = "general",
    status: str = "active",
    importance: int = 3,
    pinned: bool = False,
    entity_tokens: list[str] | None = None,
    domain_slugs: list[str] | None = None,
    tag_slugs: list[str] | None = None,
    event_type_slugs: list[str] | None = None,
    client_request_id: str | None = None,
    stale_after_days: int | None = None,
    extra: dict[str, Any] | None = None,
) -> Note:
    if client_request_id:
        existing = session.query(Note).filter_by(client_request_id=client_request_id).first()
        if existing:
            return existing

    now = utcnow()
    note = Note(
        client_request_id=client_request_id,
        title=title.strip() or "(untitled)",
        body_md=body_md or "",
        note_type=(note_type or "general").strip(),
        status=(status or "active").strip(),
        importance=int(importance),
        pinned=bool(pinned),
        stale_after_days=int(stale_after_days) if stale_after_days is not None else None,
        last_reviewed_at=None,
        last_stale_check_at=None,
        stale_check_state=None,
        created_at=now,
        updated_at=now,
        extra=normalize_note_provenance_extra(extra),
    )
    session.add(note)
    session.flush()

    if entity_tokens:
        note.entities = resolve_entities(session, entity_tokens)
    if domain_slugs:
        note.domains = resolve_domains(session, domain_slugs)[:2]
    if tag_slugs:
        note.tags = resolve_tags(session, tag_slugs)
    if event_type_slugs:
        note.event_types = resolve_event_types(session, event_type_slugs)

    return note


def update_note(
    session: Session,
    *,
    note_id: int,
    title: str | None = None,
    body_md: str | None = None,
    note_type: str | None = None,
    status: str | None = None,
    importance: int | None = None,
    pinned: bool | None = None,
    stale_after_days: int | None = None,
    entities_csv: str | None = None,
    domains_csv: str | None = None,
    tags_csv: str | None = None,
    event_types_csv: str | None = None,
) -> Note:
    note = session.query(Note).filter_by(id=int(note_id)).first()
    if not note:
        raise ValueError("Note not found.")

    changed = False
    if title is not None and title.strip() != note.title:
        note.title = title.strip() or "(untitled)"
        changed = True
    if body_md is not None and body_md != note.body_md:
        note.body_md = body_md
        changed = True
    if note_type is not None and note_type.strip() and note_type.strip() != note.note_type:
        note.note_type = note_type.strip()
        changed = True
    if status is not None and status.strip() and status.strip() != note.status:
        note.status = status.strip()
        changed = True
    if importance is not None and int(importance) != int(note.importance):
        note.importance = int(importance)
        changed = True
    if pinned is not None and bool(pinned) != bool(note.pinned):
        note.pinned = bool(pinned)
        changed = True
    if stale_after_days is not None and stale_after_days != note.stale_after_days:
        note.stale_after_days = int(stale_after_days)
        changed = True

    # Taxonomy (explicit update only when field provided)
    if entities_csv is not None:
        note.entities = resolve_entities(session, split_csv(entities_csv))
        changed = True
    if domains_csv is not None:
        note.domains = resolve_domains(session, split_csv(domains_csv)[:2])
        changed = True
    if tags_csv is not None:
        note.tags = resolve_tags(session, split_csv(tags_csv))
        changed = True
    if event_types_csv is not None:
        note.event_types = resolve_event_types(session, split_csv(event_types_csv))
        changed = True

    if changed:
        note.updated_at = utcnow()
    return note


def link_note_thesis(
    session: Session,
    *,
    note_id: int,
    thesis_id: int,
    relation: str = "context",
) -> NoteThesis | None:
    note = session.query(Note).filter_by(id=int(note_id)).first()
    thesis = session.query(Thesis).filter_by(id=int(thesis_id)).first()
    if not note or not thesis:
        raise ValueError("Note or thesis not found.")

    existing = (
        session.query(NoteThesis)
        .filter_by(note_id=int(note_id), thesis_id=int(thesis_id))
        .first()
    )
    if existing:
        existing.relation = (relation or existing.relation).strip()
        return existing

    link = NoteThesis(
        note_id=int(note_id),
        thesis_id=int(thesis_id),
        relation=(relation or "context").strip(),
        created_at=utcnow(),
    )
    session.add(link)
    note.updated_at = utcnow()
    return link


def unlink_note_thesis(session: Session, *, note_id: int, thesis_id: int) -> None:
    link = session.query(NoteThesis).filter_by(note_id=int(note_id), thesis_id=int(thesis_id)).first()
    if link:
        session.delete(link)


def link_note_document(
    session: Session,
    *,
    note_id: int,
    document_id: int,
    snippet: str | None = None,
) -> NoteDocument | None:
    note = session.query(Note).filter_by(id=int(note_id)).first()
    doc = session.query(Document).filter_by(id=int(document_id)).first()
    if not note or not doc:
        raise ValueError("Note or document not found.")

    existing = (
        session.query(NoteDocument)
        .filter_by(note_id=int(note_id), document_id=int(document_id))
        .first()
    )
    if existing:
        if snippet and (not existing.snippet or existing.snippet.strip() != snippet.strip()):
            existing.snippet = snippet.strip()
        return existing

    link = NoteDocument(
        note_id=int(note_id),
        document_id=int(document_id),
        snippet=snippet.strip() if snippet else None,
        created_at=utcnow(),
    )
    session.add(link)
    note.updated_at = utcnow()
    from thoth_terminal.triage_service import mark_actioned

    mark_actioned(
        session,
        document_id=int(document_id),
        meta={"source": "auto:note_link", "note_id": note.id},
    )
    return link


def unlink_note_document(session: Session, *, note_id: int, document_id: int) -> None:
    link = session.query(NoteDocument).filter_by(note_id=int(note_id), document_id=int(document_id)).first()
    if link:
        session.delete(link)


def link_note_proposition(
    session: Session,
    *,
    note_id: int,
    proposition_id: int,
    relation: str = "references",
) -> NoteProposition | None:
    note = session.query(Note).filter_by(id=int(note_id)).first()
    prop = session.query(Proposition).filter_by(id=int(proposition_id)).first()
    if not note or not prop:
        raise ValueError("Note or proposition not found.")

    existing = (
        session.query(NoteProposition)
        .filter_by(note_id=int(note_id), proposition_id=int(proposition_id))
        .first()
    )
    if existing:
        existing.relation = (relation or existing.relation).strip()
        return existing

    link = NoteProposition(
        note_id=int(note_id),
        proposition_id=int(proposition_id),
        relation=(relation or "references").strip(),
        created_at=utcnow(),
    )
    session.add(link)
    note.updated_at = utcnow()
    return link


def unlink_note_proposition(session: Session, *, note_id: int, proposition_id: int) -> None:
    link = session.query(NoteProposition).filter_by(note_id=int(note_id), proposition_id=int(proposition_id)).first()
    if link:
        session.delete(link)


def _display_title_for_doc(doc: Document) -> str:
    if isinstance(doc.extra, dict) and doc.extra.get("translation_title_en"):
        return str(doc.extra.get("translation_title_en"))
    return doc.title or (doc.url or f"doc {doc.id}")


def _markdown_token_labels(session: Session, markdown_texts: list[str]) -> tuple[dict[int, str], dict[int, str]]:
    doc_ids: set[int] = set()
    thesis_ids: set[int] = set()
    for src in markdown_texts:
        doc_ids.update(int(m.group("id")) for m in DOC_TOKEN_RE.finditer(src or ""))
        thesis_ids.update(int(m.group("id")) for m in THESIS_TOKEN_RE.finditer(src or ""))
    doc_labels: dict[int, str] = {}
    thesis_labels: dict[int, str] = {}

    if doc_ids:
        rows = (
            session.query(Document)
            .options(load_only(Document.id, Document.title, Document.url, Document.extra))
            .filter(Document.id.in_(sorted(doc_ids)))
            .all()
        )
        doc_labels = {
            int(doc.id): _display_title_for_doc(doc).replace("[", "").replace("]", "")[:120]
            for doc in rows
        }

    if thesis_ids:
        rows = (
            session.query(Thesis)
            .options(load_only(Thesis.id, Thesis.title))
            .filter(Thesis.id.in_(sorted(thesis_ids)))
            .all()
        )
        thesis_labels = {
            int(th.id): (th.title or f"thesis {int(th.id)}").replace("[", "").replace("]", "")[:120]
            for th in rows
        }

    return doc_labels, thesis_labels


def _render_markdown_with_labels(src: str, *, doc_labels: dict[int, str], thesis_labels: dict[int, str]) -> str:
    def _replace_doc(m: re.Match[str]) -> str:
        doc_id = int(m.group("id"))
        label = doc_labels.get(doc_id)
        if not label:
            return f"[doc {doc_id}](/docs/{doc_id})"
        return f"[doc {doc_id}: {label}](/docs/{doc_id})"

    def _replace_thesis(m: re.Match[str]) -> str:
        th_id = int(m.group("id"))
        label = thesis_labels.get(th_id)
        if not label:
            return f"[thesis {th_id}](/theses/{th_id})"
        return f"[thesis {th_id}: {label}](/theses/{th_id})"

    src = DOC_TOKEN_RE.sub(_replace_doc, src)
    src = THESIS_TOKEN_RE.sub(_replace_thesis, src)

    html_out = _MARKDOWN_RENDERER.render(src)
    # Small hardening: ensure any stray <script> is escaped (belt and suspenders).
    html_out = re.sub(r"<\s*script", lambda m: html.escape(m.group(0)), html_out, flags=re.IGNORECASE)
    return html_out


def render_markdown_to_html(session: Session, markdown_text: str) -> str:
    """
    Safe-ish markdown rendering for local-first UI preview/export.
    - Disables raw HTML in markdown.
    - Expands [[doc:123]] and [[thesis:5]] tokens into links with helpful labels.
    """
    src = markdown_text or ""
    doc_labels, thesis_labels = _markdown_token_labels(session, [src])
    return _render_markdown_with_labels(src, doc_labels=doc_labels, thesis_labels=thesis_labels)


def render_markdown_batch_to_html(session: Session, markdown_texts: list[str]) -> list[str]:
    sources = [text or "" for text in markdown_texts]
    if not sources:
        return []
    doc_labels, thesis_labels = _markdown_token_labels(session, sources)
    return [
        _render_markdown_with_labels(src, doc_labels=doc_labels, thesis_labels=thesis_labels)
        for src in sources
    ]
