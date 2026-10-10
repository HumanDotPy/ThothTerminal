from __future__ import annotations

import re
from typing import Any

from sqlalchemy.orm import Session

from thoth_terminal.models import Document, DocumentPassage
from thoth_terminal.query import document_text_version


_MATCH_ID_RE = re.compile(r"^doc:(\d+):([0-9a-f]{64}):(\d+):(\d+)$")
MAX_EXPANSION_CHARS = 40_000


def _canonical_text(document: Document) -> str:
    return str(document.translation_en or document.normalized_text or "")


def expand_document_match(
    session: Session,
    *,
    match_id: str,
    before_chars: int = 4_000,
    after_chars: int = 4_000,
) -> dict[str, Any]:
    """Expand a versioned search hit against the exact representation search used."""
    parsed = _MATCH_ID_RE.fullmatch(str(match_id or "").strip())
    if parsed is None:
        raise ValueError("match_id is invalid.")
    document_id, expected_version, raw_start, raw_end = parsed.groups()
    document = session.query(Document).filter_by(id=int(document_id)).first()
    if document is None:
        raise ValueError("Document not found.")

    text = _canonical_text(document)
    current_version = document_text_version(text)
    if current_version != expected_version:
        raise ValueError("match_id is stale because the document text representation changed.")

    match_start = int(raw_start)
    match_end = int(raw_end)
    if match_start < 0 or match_end <= match_start or match_end > len(text):
        raise ValueError("match_id offsets are outside the current document text.")

    before = max(0, min(int(before_chars or 0), MAX_EXPANSION_CHARS))
    after = max(0, min(int(after_chars or 0), MAX_EXPANSION_CHARS))
    if before + after > MAX_EXPANSION_CHARS:
        scale = MAX_EXPANSION_CHARS / float(before + after)
        before = int(before * scale)
        after = MAX_EXPANSION_CHARS - before
    context_start = max(0, match_start - before)
    context_end = min(len(text), match_end + after)
    context = text[context_start:context_end]

    page_rows = (
        session.query(DocumentPassage.page_start, DocumentPassage.page_end)
        .filter(
            DocumentPassage.document_id == int(document.id),
            DocumentPassage.content_hash == current_version,
            DocumentPassage.char_end > context_start,
            DocumentPassage.char_start < context_end,
        )
        .all()
    )
    starts = [int(row.page_start) for row in page_rows if row.page_start is not None]
    ends = [int(row.page_end) for row in page_rows if row.page_end is not None]
    return {
        "match_id": str(match_id),
        "document": {
            "id": int(document.id),
            "title": document.title,
            "url": document.url or document.canonical_url,
        },
        "text_version": current_version,
        "text_length_chars": len(text),
        "match_start": match_start,
        "match_end": match_end,
        "context_start": context_start,
        "context_end": context_end,
        "before_chars_returned": match_start - context_start,
        "after_chars_returned": context_end - match_end,
        "returned_text_length_chars": len(context),
        "approximate_token_count": max(1, (len(context) + 3) // 4),
        "has_more_before": context_start > 0,
        "has_more_after": context_end < len(text),
        "page_start": min(starts) if starts else None,
        "page_end": max(ends) if ends else None,
        "text": context,
    }
