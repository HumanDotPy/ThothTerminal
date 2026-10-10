from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from typing import Any

from sqlalchemy.orm import Session

from thoth_terminal.db import utcnow
from thoth_terminal.document_page_service import ensure_document_pages
from thoth_terminal.models import Document, DocumentPage, DocumentPassage
from thoth_terminal.query import document_match_excerpt


PASSAGE_TARGET_CHARS = 2400
PASSAGE_MIN_CHARS = 1400
PASSAGE_MAX_CHARS = 3200
PASSAGE_MAX_COUNT = 20000
_RANK_TOKEN_RE = re.compile(r"[a-z0-9]+")
_TECHNICAL_TERM_GROUPS = (
    {"retention", "retain", "preserve", "persistence", "refresh"},
    {"endurance", "lifetime", "durability", "writes", "write", "cycles", "program", "erase"},
    {"reset", "restart", "refresh", "power", "cycle"},
    {"bandwidth", "throughput", "rate", "transfer"},
    {"latency", "delay", "response"},
    {"thermal", "temperature", "heat", "cooling"},
    {"reliability", "lifetime", "failure", "durability"},
)


def _document_text(document: Document) -> str:
    return str(document.translation_en or document.normalized_text or "")


def _content_hash(document: Document, text: str) -> str:
    del document
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _passage_key(document_id: int, content_hash: str, passage_index: int) -> str:
    return f"doc:{int(document_id)}:{content_hash[:16]}:p{int(passage_index)}"


def _rank_tokens(value: object) -> list[str]:
    return [token for token in _RANK_TOKEN_RE.findall(str(value or "").casefold()) if len(token) >= 2 or token.isdigit()]


def _expanded_terms(query: str) -> tuple[list[str], dict[str, float]]:
    original = set(_rank_tokens(query))
    weights = {term: 1.0 for term in original}
    for group in _TECHNICAL_TERM_GROUPS:
        if original.intersection(group):
            for term in group:
                weights.setdefault(term, 0.45)
    return sorted(weights), weights


def _bm25_scores(rows: list[DocumentPassage], terms: list[str], weights: dict[str, float]) -> dict[int, float]:
    if not rows or not terms:
        return {}
    token_counts = {int(row.id): Counter(_rank_tokens(row.text)) for row in rows}
    lengths = {row_id: sum(counts.values()) for row_id, counts in token_counts.items()}
    average_length = max(1.0, sum(lengths.values()) / max(1, len(lengths)))
    document_frequency = {
        term: sum(1 for counts in token_counts.values() if counts.get(term, 0) > 0)
        for term in terms
    }
    scores: dict[int, float] = {}
    k1 = 1.2
    b = 0.75
    for row_id, counts in token_counts.items():
        score = 0.0
        for term in terms:
            frequency = float(counts.get(term, 0))
            if frequency <= 0:
                continue
            df = float(document_frequency.get(term, 0))
            inverse_frequency = math.log(1.0 + (len(rows) - df + 0.5) / (df + 0.5))
            denominator = frequency + k1 * (1.0 - b + b * lengths[row_id] / average_length)
            score += float(weights.get(term, 1.0)) * inverse_frequency * (frequency * (k1 + 1.0) / denominator)
        scores[row_id] = score
    return scores


def _choose_break(text: str, start: int) -> int:
    remaining = len(text) - start
    if remaining <= PASSAGE_MAX_CHARS:
        return len(text)
    lower = min(len(text), start + PASSAGE_MIN_CHARS)
    target = min(len(text), start + PASSAGE_TARGET_CHARS)
    upper = min(len(text), start + PASSAGE_MAX_CHARS)
    candidates: list[tuple[int, int, int]] = []
    for marker, quality in (("\n\n", 4), ("\n", 3), (". ", 2), ("; ", 1)):
        cursor = lower
        while cursor < upper:
            pos = text.find(marker, cursor, upper)
            if pos < 0:
                break
            boundary = pos + len(marker)
            candidates.append((quality, -abs(boundary - target), boundary))
            cursor = boundary
    if candidates:
        return max(candidates)[2]
    return upper


def _passage_specs(text: str) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    start = 0
    while start < len(text) and len(specs) < PASSAGE_MAX_COUNT:
        while start < len(text) and text[start].isspace():
            start += 1
        if start >= len(text):
            break
        end = _choose_break(text, start)
        while end > start and text[end - 1].isspace():
            end -= 1
        if end <= start:
            end = min(len(text), start + PASSAGE_MAX_CHARS)
        passage_text = text[start:end]
        specs.append(
            {
                "passage_index": len(specs),
                "char_start": start,
                "char_end": end,
                "text": passage_text,
                "token_estimate": max(1, (len(passage_text) + 3) // 4),
            }
        )
        start = end
    return specs


def _page_text_representation(pages: list[DocumentPage]) -> str:
    return "\n\n".join(str(page.text or "") for page in pages)


def _passage_specs_from_pages(pages: list[DocumentPage]) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    representation_offset = 0
    for page in pages:
        page_text = str(page.text or "")
        for local in _passage_specs(page_text):
            specs.append(
                {
                    **local,
                    "passage_index": len(specs),
                    "char_start": representation_offset + int(local["char_start"]),
                    "char_end": representation_offset + int(local["char_end"]),
                    "page_start": int(page.page_number),
                    "page_end": int(page.page_number),
                }
            )
        representation_offset += len(page_text) + 2
    return specs


def serialize_document_passage(
    row: DocumentPassage,
    *,
    include_text: bool = True,
    passage_count: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": int(row.id),
        "passage_id": row.passage_key,
        "document_id": int(row.document_id),
        "passage_index": int(row.passage_index),
        "char_start": int(row.char_start),
        "char_end": int(row.char_end),
        "page_start": int(row.page_start) if row.page_start is not None else None,
        "page_end": int(row.page_end) if row.page_end is not None else None,
        "heading": row.heading,
        "token_estimate": int(row.token_estimate or 0),
        "previous_passage_id": (
            _passage_key(int(row.document_id), row.content_hash, int(row.passage_index) - 1)
            if int(row.passage_index) > 0
            else None
        ),
        "next_passage_id": (
            _passage_key(int(row.document_id), row.content_hash, int(row.passage_index) + 1)
            if passage_count is None or int(row.passage_index) + 1 < int(passage_count)
            else None
        ),
    }
    if include_text:
        payload["text"] = row.text
    return payload


def ensure_document_passages(session: Session, *, document_id: int) -> dict[str, Any]:
    document = session.query(Document).filter_by(id=int(document_id)).first()
    if document is None:
        raise ValueError("Document not found.")
    ensure_document_pages(session, document_id=int(document.id))
    pages = (
        session.query(DocumentPage)
        .filter_by(document_id=int(document.id))
        .order_by(DocumentPage.page_number.asc())
        .all()
    )
    text = _page_text_representation(pages) if pages else _document_text(document)
    content_hash = _content_hash(document, text)
    existing = (
        session.query(DocumentPassage)
        .filter_by(document_id=int(document.id), content_hash=content_hash)
        .order_by(DocumentPassage.passage_index.asc())
        .all()
    )
    if existing:
        return {
            "document_id": int(document.id),
            "content_hash": content_hash,
            "passage_count": len(existing),
            "created": False,
        }

    session.query(DocumentPassage).filter(DocumentPassage.document_id == int(document.id)).delete()
    if not text:
        session.flush()
        return {
            "document_id": int(document.id),
            "content_hash": content_hash,
            "passage_count": 0,
            "created": False,
        }
    now = utcnow()
    rows: list[DocumentPassage] = []
    specs = _passage_specs_from_pages(pages) if pages else _passage_specs(text)
    for spec in specs:
        row = DocumentPassage(
            document_id=int(document.id),
            passage_key=_passage_key(int(document.id), content_hash, int(spec["passage_index"])),
            content_hash=content_hash,
            passage_index=int(spec["passage_index"]),
            char_start=int(spec["char_start"]),
            char_end=int(spec["char_end"]),
            page_start=int(spec["page_start"]) if spec.get("page_start") is not None else None,
            page_end=int(spec["page_end"]) if spec.get("page_end") is not None else None,
            heading=None,
            text=str(spec["text"]),
            token_estimate=int(spec["token_estimate"]),
            extra={
                "index_version": "document-passages-v1",
                "source": "pdf_pages" if pages else "normalized_text",
            },
            created_at=now,
        )
        session.add(row)
        rows.append(row)
    session.flush()
    return {
        "document_id": int(document.id),
        "content_hash": content_hash,
        "passage_count": len(rows),
        "created": True,
    }


def query_document_passages(
    session: Session,
    *,
    document_id: int,
    query: str,
    top_k: int = 5,
    max_tokens: int = 2500,
    mode: str = "hybrid",
) -> dict[str, Any]:
    query_v = str(query or "").strip()
    if not query_v:
        raise ValueError("query is required.")
    index = ensure_document_passages(session, document_id=int(document_id))
    document = session.query(Document).filter_by(id=int(document_id)).first()
    assert document is not None
    rows = (
        session.query(DocumentPassage)
        .filter_by(document_id=int(document_id), content_hash=index["content_hash"])
        .order_by(DocumentPassage.passage_index.asc())
        .all()
    )
    mode_v = str(mode or "hybrid").strip().lower()
    if mode_v not in {"hybrid", "lexical"}:
        raise ValueError("mode must be hybrid|lexical.")
    original_terms = sorted(set(_rank_tokens(query_v)))
    expanded_terms, term_weights = _expanded_terms(query_v)
    ranking_terms = expanded_terms if mode_v == "hybrid" else original_terms
    bm25 = _bm25_scores(rows, ranking_terms, term_weights if mode_v == "hybrid" else {term: 1.0 for term in original_terms})
    ranked: list[tuple[float, DocumentPassage, dict[str, Any]]] = []
    for row in rows:
        exact_match = document_match_excerpt(row.text, query_v, limit=900)
        retrieval_query = query_v if exact_match["match_found"] or mode_v == "lexical" else " ".join(ranking_terms)
        match = exact_match if retrieval_query == query_v else document_match_excerpt(row.text, retrieval_query, limit=900)
        bm25_score = float(bm25.get(int(row.id), 0.0))
        if not match["match_found"] and bm25_score <= 0:
            continue
        row_tokens = set(_rank_tokens(row.text))
        original_coverage = len(row_tokens.intersection(original_terms)) / max(1, len(original_terms))
        heading_bonus = 8.0 if row.heading and any(term in row.heading.casefold() for term in original_terms) else 0.0
        numeric_terms = {term for term in original_terms if any(char.isdigit() for char in term)}
        numeric_bonus = 12.0 * len(numeric_terms.intersection(row_tokens))
        total_score = (
            bm25_score * 12.0
            + float(exact_match["match_score"]) * 0.7
            + original_coverage * 20.0
            + heading_bonus
            + numeric_bonus
        )
        match["score_components"] = {
            "bm25": round(bm25_score, 4),
            "exact": round(float(exact_match["match_score"]), 4),
            "original_term_coverage": round(original_coverage, 4),
            "heading_bonus": heading_bonus,
            "numeric_bonus": numeric_bonus,
        }
        ranked.append((total_score, row, match))
    ranked.sort(key=lambda item: (item[0], -int(item[1].passage_index)), reverse=True)

    results: list[dict[str, Any]] = []
    token_budget = max(200, min(int(max_tokens or 2500), 12000))
    used_tokens = 0
    for score, row, match in ranked[: max(1, min(int(top_k or 5), 12))]:
        serialized = serialize_document_passage(
            row,
            include_text=False,
            passage_count=int(index["passage_count"]),
        )
        excerpt = str(match["match_excerpt"])
        estimate = max(1, (len(excerpt) + 3) // 4)
        if results and used_tokens + estimate > token_budget:
            break
        serialized.update(
            {
                "score": round(score, 4),
                "score_components": match.get("score_components") or {},
                "match_excerpt": excerpt,
            }
        )
        results.append(serialized)
        used_tokens += estimate
    return {
        "document": {
            "id": int(document.id),
            "title": document.title,
            "url": document.url or document.canonical_url,
            "text_length_chars": len(_document_text(document)),
            "passage_count": int(index["passage_count"]),
        },
        "query": query_v,
        "count": len(results),
        "passages": results,
        "returned_token_estimate": used_tokens,
        "index_created": bool(index["created"]),
        "retrieval_mode": "hybrid_lexical_v1" if mode_v == "hybrid" else "lexical_passage_v1",
        "semantic_embeddings_used": False,
        "expanded_terms": [term for term in ranking_terms if term not in original_terms],
    }


def read_document_passages(
    session: Session,
    *,
    document_id: int,
    passage_id: str | None = None,
    passage_index: int | None = None,
    before: int = 1,
    after: int = 1,
    max_tokens: int = 4000,
) -> dict[str, Any]:
    index = ensure_document_passages(session, document_id=int(document_id))
    if passage_index is None:
        value = str(passage_id or "").strip()
        match = re.search(r":p(\d+)$", value)
        if match is None:
            raise ValueError("passage_id or passage_index is required.")
        passage_index = int(match.group(1))
    center = int(passage_index)
    before_v = max(0, min(int(before or 0), 10))
    after_v = max(0, min(int(after or 0), 10))
    rows = (
        session.query(DocumentPassage)
        .filter(
            DocumentPassage.document_id == int(document_id),
            DocumentPassage.content_hash == index["content_hash"],
            DocumentPassage.passage_index >= center - before_v,
            DocumentPassage.passage_index <= center + after_v,
        )
        .order_by(DocumentPassage.passage_index.asc())
        .all()
    )
    if not any(int(row.passage_index) == center for row in rows):
        raise ValueError("Passage not found for the current document content.")
    budget = max(200, min(int(max_tokens or 4000), 16000))
    used = 0
    output: list[dict[str, Any]] = []
    for row in rows:
        estimate = int(row.token_estimate or 0)
        if output and used + estimate > budget:
            break
        output.append(serialize_document_passage(row, passage_count=int(index["passage_count"])))
        used += estimate
    return {
        "document_id": int(document_id),
        "center_passage_index": center,
        "count": len(output),
        "passages": output,
        "returned_token_estimate": used,
        "truncated": len(output) < len(rows),
    }


def _heuristic_outline(pages: list[DocumentPage]) -> list[dict[str, Any]]:
    heading_re = re.compile(r"^(?:(?:chapter|section)\s+)?\d+(?:\.\d+){0,4}\s+[A-Z][^.!?]{2,120}$", re.IGNORECASE)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page in pages:
        for line in str(page.text or "").splitlines()[:40]:
            value = re.sub(r"\s+", " ", line).strip()
            if not value or len(value) > 140:
                continue
            is_numbered = bool(heading_re.match(value))
            is_upper = 5 <= len(value) <= 90 and value.upper() == value and any(char.isalpha() for char in value)
            if not (is_numbered or is_upper):
                continue
            key = value.casefold()
            if key in seen:
                continue
            seen.add(key)
            rows.append({"title": value, "page_number": int(page.page_number), "depth": 0, "source": "heuristic"})
            if len(rows) >= 120:
                return rows
    return rows


def inspect_document_structure(session: Session, *, document_id: int) -> dict[str, Any]:
    document = session.query(Document).filter_by(id=int(document_id)).first()
    if document is None:
        raise ValueError("Document not found.")
    page_index = ensure_document_pages(session, document_id=int(document_id))
    passage_index = ensure_document_passages(session, document_id=int(document_id))
    pages = (
        session.query(DocumentPage)
        .filter_by(document_id=int(document_id))
        .order_by(DocumentPage.page_number.asc())
        .all()
    )
    structure = dict((document.extra or {}).get("pdf_structure") or {})
    outline = structure.get("outline") if isinstance(structure.get("outline"), list) else []
    outline_source = "pdf_bookmarks" if outline else "heuristic"
    if not outline:
        outline = _heuristic_outline(pages)
    text = _document_text(document)
    return {
        "document": {
            "id": int(document.id),
            "title": document.title,
            "author": document.author,
            "url": document.url or document.canonical_url,
            "published_at": document.published_at.isoformat() if document.published_at else None,
            "kind": document.kind,
            "language": document.language,
            "text_length_chars": len(text),
        },
        "structure": {
            "page_count": int(page_index.get("page_count") or 0),
            "passage_count": int(passage_index.get("passage_count") or 0),
            "outline": outline[:120],
            "outline_source": outline_source if outline else None,
            "page_anchors_available": bool(pages),
            "page_images_available": bool(pages),
            "representations": [
                "metadata",
                "normalized_text",
                *(["pdf_page_text", "pdf_page_image"] if pages else []),
            ],
        },
        "recommended_next_tools": ["query_document", "read_document", *(["render_document_page"] if pages else [])],
    }
