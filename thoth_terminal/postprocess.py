from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from thoth_terminal.ingest import (
    _apply_defaults,
    _apply_entity_dictionary,
    _headline_language,
    _maybe_auto_translate,
    _apply_money_mentions,
    _record_mentions,
    _backfill_mentions_for_doc,
    _ensure_images_for_doc,
)
from thoth_terminal.image_ocr import describe_ocr_status, extract_text_from_image_path, summarize_ocr_text
from thoth_terminal.models import Document, Source
from thoth_terminal.object_store import abs_path
from thoth_terminal.document_media import extract_candidate_image_urls
from thoth_terminal.document_refinery import ensure_document_refinery


def _apply_image_ocr(doc: Document, *, cfg: dict[str, Any]) -> int:
    if not bool(cfg.get("ocr_enabled")):
        return 0
    extra = dict(doc.extra or {}) if isinstance(doc.extra, dict) else {}
    status = describe_ocr_status(preferred_backend=str(cfg.get("ocr_backend") or "").strip() or None)
    extra["image_ocr_status"] = status
    if not status.get("available"):
        doc.extra = extra
        return 0

    existing_rows = list(extra.get("image_ocr") or [])
    seen_keys = {
        str(row.get("original_url") or "").strip()
        for row in existing_rows
        if isinstance(row, dict) and str(row.get("original_url") or "").strip()
    }
    seen_keys.update(
        {
            str(row.get("object_path") or "").strip()
            for row in existing_rows
            if isinstance(row, dict) and str(row.get("object_path") or "").strip()
        }
    )

    max_images = int(cfg.get("ocr_max_images_per_doc") or 6)
    max_bytes = int(cfg.get("ocr_max_image_bytes") or 5_000_000)
    min_chars = int(cfg.get("ocr_min_chars") or 20)
    languages = str(cfg.get("ocr_languages") or "eng").strip() or "eng"
    backend = str(status.get("backend") or "").strip() or None

    added = 0
    for attachment in doc.attachments or []:
        is_image = attachment.kind == "image" or (attachment.content_type or "").startswith("image/")
        if not is_image:
            continue
        key = str(attachment.original_url or "").strip() or str(attachment.object_path or "").strip()
        if not key or key in seen_keys:
            continue
        path = abs_path(str(attachment.object_path))
        try:
            if max_bytes > 0 and path.exists() and path.stat().st_size > max_bytes:
                continue
        except Exception:
            continue
        result = extract_text_from_image_path(path, preferred_backend=backend, languages=languages)
        if not result or len(result.text) < min_chars:
            continue
        existing_rows.append(
            {
                "backend": result.backend,
                "original_url": str(attachment.original_url or "").strip() or None,
                "object_path": str(attachment.object_path or "").strip() or None,
                "text": result.text,
                "summary": summarize_ocr_text(result.text),
                "content_type": attachment.content_type,
                "detail": result.detail,
            }
        )
        seen_keys.add(key)
        added += 1
        if max_images > 0 and added >= max_images:
            break
    if added:
        extra["image_ocr"] = existing_rows
    doc.extra = extra
    return added


def postprocess_document(session: Session, *, doc_id: int, source_id: int | None = None) -> bool:
    doc = session.query(Document).filter_by(id=int(doc_id)).first()
    if not doc:
        return False

    src = None
    if source_id is not None:
        src = session.query(Source).filter_by(id=int(source_id)).first()
    if not src:
        src = session.query(Source).filter_by(id=int(doc.source_id)).first()
    cfg: dict[str, Any] = dict(src.config or {}) if src else {}

    _apply_defaults(session, doc, cfg=cfg)
    _apply_entity_dictionary(session, doc)

    if doc.translation_en is None:
        title_lang = _headline_language(doc.title)
        _maybe_auto_translate(session, doc, title_lang=title_lang, cfg=cfg)
    _apply_money_mentions(session, doc, cfg=cfg)

    extra = doc.extra if isinstance(doc.extra, dict) else {}
    links = extra.get("links") or []
    if isinstance(links, str):
        links = [links]
    if isinstance(links, list) and links:
        _record_mentions(session, doc, [str(l) for l in links if l])

    _backfill_mentions_for_doc(session, doc)

    image_urls = extract_candidate_image_urls(extra)
    if image_urls:
        _ensure_images_for_doc(session, doc, image_urls=image_urls, cfg=cfg)

    _apply_image_ocr(doc, cfg=cfg)
    ensure_document_refinery(doc, source_name=(src.name if src else None))

    return True
