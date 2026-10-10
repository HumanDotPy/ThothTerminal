from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session
from sqlalchemy.orm import joinedload

from thoth_terminal.config import ensure_data_dirs, settings
from thoth_terminal.db import utcnow
from thoth_terminal.models import (
    Document,
    Note,
    NoteDocument,
    NoteProposition,
    NoteThesis,
    Proposition,
    Thesis,
    ThesisEvidence,
    ThesisProposition,
)


def _exports_dir() -> Path:
    ensure_data_dirs()
    path = settings.data_dir / "exports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def export_bundle(session: Session, *, document_ids: list[int], label: str | None = None) -> Path:
    docs = (
        session.query(Document)
        .options(joinedload(Document.attachments))
        .filter(Document.id.in_(document_ids))
        .order_by(Document.ingested_at.desc())
        .all()
    )
    ts = utcnow().strftime("%Y%m%d_%H%M%S")
    bundle_dir = _exports_dir() / f"bundle_{ts}"
    bundle_dir.mkdir(parents=True, exist_ok=True)

    objects_out = bundle_dir / "objects"
    objects_out.mkdir(parents=True, exist_ok=True)

    def _copy_object(object_path: str) -> str:
        rel = Path(object_path)
        src = settings.objects_dir / rel
        dst = objects_out / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            if src.exists() and not dst.exists():
                dst.write_bytes(src.read_bytes())
        except Exception:
            pass
        return str(Path("objects") / rel).replace("\\", "/")

    index = []
    md_lines: list[str] = []
    md_lines.append(f"# Thoth Bundle ({label or ts})")
    md_lines.append("")

    for d in docs:
        translated_title = None
        if isinstance(d.extra, dict):
            translated_title = d.extra.get("translation_title_en")

        attachments = []
        for a in d.attachments or []:
            rel = _copy_object(a.object_path)
            attachments.append({"kind": a.kind, "object_path": rel, "content_type": a.content_type})

        index.append(
            {
                "id": d.id,
                "kind": d.kind,
                "source_id": d.source_id,
                "title": d.title,
                "translation_title_en": translated_title,
                "author": d.author,
                "published_at": d.published_at.isoformat() if d.published_at else None,
                "ingested_at": d.ingested_at.isoformat(),
                "language": d.language,
                "canonical_url": d.canonical_url,
                "url": d.url,
                "domains": [dom.slug for dom in d.domains],
                "tags": [t.slug for t in d.tags],
                "event_types": [e.slug for e in d.event_types],
                "attachments": attachments,
            }
        )

        title = translated_title or d.title or "(untitled)"
        stamp = (d.published_at or d.ingested_at).isoformat(timespec="seconds")
        md_lines.append(f"## {title}")
        md_lines.append(f"- Time: `{stamp}`")
        if d.url:
            md_lines.append(f"- URL: `{d.url}`")
        if d.language:
            md_lines.append(f"- Lang: `{d.language}`")
        if translated_title and d.title and translated_title != d.title:
            md_lines.append(f"- Original title: `{d.title}`")
        if attachments:
            md_lines.append("- Attachments:")
            for a in attachments:
                md_lines.append(f"  - `{a['kind']}`: `{a['object_path']}`")
        md_lines.append("")
        md_lines.append(d.translation_en or d.normalized_text)
        md_lines.append("")

    (bundle_dir / "index.json").write_text(json.dumps(index, indent=2, ensure_ascii=False))
    (bundle_dir / "bundle.md").write_text("\n".join(md_lines), encoding="utf-8")

    if settings.auto_action_on_export and docs:
        try:
            from thoth_terminal.triage_service import bulk_apply

            bulk_apply(
                session,
                document_ids=[d.id for d in docs],
                action="actioned",
                meta={"source": "auto:export_bundle", "label": label or ts},
            )
        except Exception:
            pass
    return bundle_dir


def export_thesis_dossier(session: Session, *, thesis_id: int, label: str | None = None) -> Path:
    thesis = (
        session.query(Thesis)
        .options(
            joinedload(Thesis.entities),
            joinedload(Thesis.domains),
            joinedload(Thesis.tags),
            joinedload(Thesis.event_types),
            joinedload(Thesis.evidence)
            .joinedload(ThesisEvidence.document)
            .joinedload(Document.source),
            joinedload(Thesis.evidence).joinedload(ThesisEvidence.document).joinedload(Document.attachments),
            joinedload(Thesis.proposition_links).joinedload(ThesisProposition.proposition),
            joinedload(Thesis.proposition_links)
            .joinedload(ThesisProposition.proposition)
            .joinedload(Proposition.observations),
        )
        .filter(Thesis.id == int(thesis_id))
        .first()
    )
    if not thesis:
        raise ValueError("Thesis not found")

    ts = utcnow().strftime("%Y%m%d_%H%M%S")
    safe_label = (label or f"thesis_{thesis.id}")[:60].replace(" ", "_")
    bundle_dir = _exports_dir() / f"thesis_{thesis.id}_{ts}"
    bundle_dir.mkdir(parents=True, exist_ok=True)

    # Copy referenced object files for portability (best-effort).
    objects_out = bundle_dir / "objects"
    objects_out.mkdir(parents=True, exist_ok=True)

    def _copy_object(object_path: str) -> str:
        rel = Path(object_path)
        src = settings.objects_dir / rel
        dst = objects_out / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            if src.exists() and not dst.exists():
                dst.write_bytes(src.read_bytes())
        except Exception:
            pass
        return str(Path("objects") / rel).replace("\\", "/")

    index: dict[str, Any] = {
        "kind": "thesis_dossier",
        "thesis": {
            "id": thesis.id,
            "title": thesis.title,
            "statement": thesis.statement,
            "status": thesis.status,
            "importance": thesis.importance,
            "stale_after_days": thesis.stale_after_days,
            "created_at": thesis.created_at.isoformat(),
            "updated_at": thesis.updated_at.isoformat(),
            "entities": [e.canonical_name for e in thesis.entities],
            "domains": [d.slug for d in thesis.domains],
            "tags": [t.slug for t in thesis.tags],
            "event_types": [e.slug for e in thesis.event_types],
        },
        "evidence": [],
        "propositions": [],
    }

    md_lines: list[str] = []
    md_lines.append(f"# Thesis Dossier: {thesis.title}")
    md_lines.append("")
    md_lines.append(f"- Status: `{thesis.status}`")
    md_lines.append(f"- Importance: `{thesis.importance}`")
    md_lines.append(f"- Stale-after days: `{thesis.stale_after_days if thesis.stale_after_days is not None else 'none'}`")
    md_lines.append(f"- Entities: `{', '.join([e.canonical_name for e in thesis.entities])}`")
    if thesis.domains:
        md_lines.append(f"- Domains: `{', '.join([d.slug for d in thesis.domains])}`")
    if thesis.tags:
        md_lines.append(f"- Tags: `{', '.join([t.slug for t in thesis.tags])}`")
    if thesis.event_types:
        md_lines.append(f"- Event types: `{', '.join([e.slug for e in thesis.event_types])}`")
    md_lines.append("")
    md_lines.append("## Statement")
    md_lines.append("")
    md_lines.append(thesis.statement)
    md_lines.append("")

    # Propositions summary (if any)
    if thesis.proposition_links:
        md_lines.append("## Propositions")
        md_lines.append("")
        for link in thesis.proposition_links:
            p = link.proposition
            if not p:
                continue
            obs = sorted(getattr(p, "observations", []) or [], key=lambda o: o.created_at, reverse=True)
            latest = obs[0] if obs else None
            latest_val = None
            latest_period = None
            if latest:
                latest_val = latest.value_number if latest.value_number is not None else latest.value_text
                if latest.value_bool is not None:
                    latest_val = bool(latest.value_bool)
                if latest.period_granularity:
                    latest_period = f"{latest.target_year}{('Q' + str(latest.target_quarter)) if latest.target_quarter else ''}{latest.estimate_flag or ''}"

            index["propositions"].append(
                {
                    "id": p.id,
                    "key": p.key,
                    "kind": p.kind,
                    "role": link.role,
                    "unit": p.unit,
                    "value_type": p.value_type,
                    "latest": {
                        "value": latest_val,
                        "unit": latest.unit if latest else None,
                        "target_period": latest_period,
                        "document_id": latest.document_id if latest else None,
                        "created_at": latest.created_at.isoformat() if latest else None,
                    }
                    if latest
                    else None,
                }
            )

            md_lines.append(f"- `{link.role}` · `{p.kind}` · `{p.key}`")
            if latest:
                md_lines.append(
                    f"  - Latest: `{latest_val}` `{latest.unit or p.unit or ''}`"
                    + (f" @ `{latest_period}`" if latest_period else "")
                )
            md_lines.append("")

    # Evidence
    md_lines.append("## Evidence")
    md_lines.append("")
    evidence_sorted = sorted(thesis.evidence or [], key=lambda e: e.created_at, reverse=True)
    for ev in evidence_sorted:
        doc = ev.document
        if not doc:
            continue
        translated_title = None
        if isinstance(doc.extra, dict):
            translated_title = doc.extra.get("translation_title_en")

        title = translated_title or doc.title or "(untitled)"
        stamp = (doc.published_at or doc.ingested_at).isoformat(timespec="seconds")

        attachments = []
        for a in doc.attachments or []:
            rel = _copy_object(a.object_path)
            attachments.append({"kind": a.kind, "object_path": rel, "content_type": a.content_type})

        index["evidence"].append(
            {
                "thesis_id": thesis.id,
                "polarity": ev.polarity,
                "snippet": ev.snippet,
                "note": ev.note,
                "document": {
                    "id": doc.id,
                    "source": doc.source.name if doc.source else None,
                    "title": doc.title,
                    "translation_title_en": translated_title,
                    "published_at": doc.published_at.isoformat() if doc.published_at else None,
                    "ingested_at": doc.ingested_at.isoformat(),
                    "language": doc.language,
                    "url": doc.url,
                    "canonical_url": doc.canonical_url,
                },
                "attachments": attachments,
            }
        )

        md_lines.append(f"### {ev.polarity.upper()} — {title}")
        md_lines.append(f"- Doc: `{doc.id}`")
        md_lines.append(f"- Source: `{doc.source.name if doc.source else ''}`")
        md_lines.append(f"- Time: `{stamp}`")
        if doc.url:
            md_lines.append(f"- URL: `{doc.url}`")
        if doc.language:
            md_lines.append(f"- Lang: `{doc.language}`")
        if translated_title and doc.title and translated_title != doc.title:
            md_lines.append(f"- Original title: `{doc.title}`")
        if attachments:
            md_lines.append("- Attachments:")
            for a in attachments:
                md_lines.append(f"  - `{a['kind']}`: `{a['object_path']}`")
        md_lines.append("")

        if ev.snippet:
            md_lines.append("**Snippet**")
            md_lines.append("")
            for line in ev.snippet.splitlines():
                md_lines.append(f"> {line}")
            md_lines.append("")
        if ev.note:
            md_lines.append("**Note**")
            md_lines.append("")
            md_lines.append(ev.note)
            md_lines.append("")

        md_lines.append("**Document text**")
        md_lines.append("")
        md_lines.append(doc.translation_en or doc.normalized_text)
        md_lines.append("")

    (bundle_dir / "index.json").write_text(json.dumps(index, indent=2, ensure_ascii=False))
    (bundle_dir / "bundle.md").write_text("\n".join(md_lines), encoding="utf-8")
    return bundle_dir


def export_note_bundle(session: Session, *, note_id: int, label: str | None = None) -> Path:
    note = (
        session.query(Note)
        .options(
            joinedload(Note.entities),
            joinedload(Note.domains),
            joinedload(Note.tags),
            joinedload(Note.event_types),
            joinedload(Note.document_links).joinedload(NoteDocument.document).joinedload(Document.source),
            joinedload(Note.document_links).joinedload(NoteDocument.document).joinedload(Document.attachments),
            joinedload(Note.thesis_links).joinedload(NoteThesis.thesis),
            joinedload(Note.proposition_links).joinedload(NoteProposition.proposition).joinedload(Proposition.observations),
        )
        .filter(Note.id == int(note_id))
        .first()
    )
    if not note:
        raise ValueError("Note not found")

    ts = utcnow().strftime("%Y%m%d_%H%M%S")
    safe_label = (label or f"note_{note.id}")[:60].replace(" ", "_")
    bundle_dir = _exports_dir() / f"note_{note.id}_{ts}"
    bundle_dir.mkdir(parents=True, exist_ok=True)

    objects_out = bundle_dir / "objects"
    objects_out.mkdir(parents=True, exist_ok=True)

    def _copy_object(object_path: str) -> str:
        rel = Path(object_path)
        src = settings.objects_dir / rel
        dst = objects_out / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            if src.exists() and not dst.exists():
                dst.write_bytes(src.read_bytes())
        except Exception:
            pass
        return str(Path("objects") / rel).replace("\\", "/")

    index: dict[str, Any] = {
        "kind": "note_bundle",
        "note": {
            "id": note.id,
            "title": note.title,
            "note_type": note.note_type,
            "status": note.status,
            "importance": note.importance,
            "pinned": bool(note.pinned),
            "created_at": note.created_at.isoformat(),
            "updated_at": note.updated_at.isoformat(),
            "entities": [e.canonical_name for e in note.entities],
            "domains": [d.slug for d in note.domains],
            "tags": [t.slug for t in note.tags],
            "event_types": [e.slug for e in note.event_types],
        },
        "linked_theses": [],
        "linked_documents": [],
        "linked_propositions": [],
    }

    md_lines: list[str] = []
    md_lines.append(f"# Note: {note.title}")
    md_lines.append("")
    md_lines.append(f"- Type: `{note.note_type}`")
    md_lines.append(f"- Status: `{note.status}`")
    md_lines.append(f"- Importance: `{note.importance}`")
    md_lines.append(f"- Pinned: `{bool(note.pinned)}`")
    md_lines.append(f"- Created: `{note.created_at.isoformat(timespec='seconds')}`")
    md_lines.append(f"- Updated: `{note.updated_at.isoformat(timespec='seconds')}`")
    if note.entities:
        md_lines.append(f"- Entities: `{', '.join([e.canonical_name for e in note.entities])}`")
    if note.domains:
        md_lines.append(f"- Domains: `{', '.join([d.slug for d in note.domains])}`")
    if note.tags:
        md_lines.append(f"- Tags: `{', '.join([t.slug for t in note.tags])}`")
    if note.event_types:
        md_lines.append(f"- Event types: `{', '.join([e.slug for e in note.event_types])}`")
    md_lines.append("")
    md_lines.append("## Body")
    md_lines.append("")
    md_lines.append(note.body_md or "")
    md_lines.append("")

    # Linked theses
    if note.thesis_links:
        md_lines.append("## Linked Theses")
        md_lines.append("")
        for link in note.thesis_links:
            th = link.thesis
            if not th:
                continue
            index["linked_theses"].append({"id": th.id, "title": th.title, "relation": link.relation})
            md_lines.append(f"- `{link.relation}` · thesis `{th.id}` · {th.title}")
        md_lines.append("")

    # Linked propositions
    if note.proposition_links:
        md_lines.append("## Linked Propositions")
        md_lines.append("")
        for link in note.proposition_links:
            p = link.proposition
            if not p:
                continue
            obs = sorted(getattr(p, "observations", []) or [], key=lambda o: o.created_at, reverse=True)
            latest = obs[0] if obs else None
            latest_val = None
            latest_period = None
            if latest:
                latest_val = latest.value_number if latest.value_number is not None else latest.value_text
                if latest.value_bool is not None:
                    latest_val = bool(latest.value_bool)
                if latest.period_granularity:
                    latest_period = f"{latest.target_year}{('Q' + str(latest.target_quarter)) if latest.target_quarter else ''}{latest.estimate_flag or ''}"

            index["linked_propositions"].append(
                {
                    "id": p.id,
                    "key": p.key,
                    "kind": p.kind,
                    "relation": link.relation,
                    "unit": p.unit,
                    "value_type": p.value_type,
                    "latest": {
                        "value": latest_val,
                        "unit": latest.unit if latest else None,
                        "target_period": latest_period,
                        "document_id": latest.document_id if latest else None,
                        "created_at": latest.created_at.isoformat() if latest else None,
                    }
                    if latest
                    else None,
                }
            )

            line = f"- `{link.relation}` · `{p.kind}` · `{p.key}`"
            if latest:
                line += (
                    f" — latest `{latest_val}` `{latest.unit or p.unit or ''}`"
                    + (f" @ `{latest_period}`" if latest_period else "")
                )
            md_lines.append(line)
        md_lines.append("")

    # Linked documents (citations)
    if note.document_links:
        md_lines.append("## Linked Documents")
        md_lines.append("")
        for link in note.document_links:
            doc = link.document
            if not doc:
                continue
            translated_title = None
            if isinstance(doc.extra, dict):
                translated_title = doc.extra.get("translation_title_en")
            title = translated_title or doc.title or "(untitled)"
            stamp = (doc.published_at or doc.ingested_at).isoformat(timespec="seconds")

            attachments = []
            for a in doc.attachments or []:
                rel = _copy_object(a.object_path)
                attachments.append({"kind": a.kind, "object_path": rel, "content_type": a.content_type})

            index["linked_documents"].append(
                {
                    "note_id": note.id,
                    "snippet": link.snippet,
                    "document": {
                        "id": doc.id,
                        "source": doc.source.name if doc.source else None,
                        "title": doc.title,
                        "translation_title_en": translated_title,
                        "published_at": doc.published_at.isoformat() if doc.published_at else None,
                        "ingested_at": doc.ingested_at.isoformat(),
                        "language": doc.language,
                        "url": doc.url,
                        "canonical_url": doc.canonical_url,
                    },
                    "attachments": attachments,
                }
            )

            md_lines.append(f"### doc {doc.id}: {title}")
            md_lines.append(f"- Source: `{doc.source.name if doc.source else ''}`")
            md_lines.append(f"- Time: `{stamp}`")
            if doc.url:
                md_lines.append(f"- URL: `{doc.url}`")
            if doc.language:
                md_lines.append(f"- Lang: `{doc.language}`")
            if translated_title and doc.title and translated_title != doc.title:
                md_lines.append(f"- Original title: `{doc.title}`")
            if attachments:
                md_lines.append("- Attachments:")
                for a in attachments:
                    md_lines.append(f"  - `{a['kind']}`: `{a['object_path']}`")
            if link.snippet:
                md_lines.append("")
                md_lines.append("**Snippet**")
                md_lines.append("")
                for line in link.snippet.splitlines():
                    md_lines.append(f"> {line}")
            md_lines.append("")
        md_lines.append("")

    (bundle_dir / "index.json").write_text(json.dumps(index, indent=2, ensure_ascii=False))
    (bundle_dir / "bundle.md").write_text("\n".join(md_lines), encoding="utf-8")
    return bundle_dir
