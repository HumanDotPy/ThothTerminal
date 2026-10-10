from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, case, func, or_, text as sql_text
from sqlalchemy.orm import Session, defer, joinedload, load_only, selectinload

from thoth_terminal.db import utcnow
from thoth_terminal.document_search_maintenance import (
    coalesced_document_vector_sql, document_search_ready,
)
from thoth_terminal.models import (
    Attachment,
    CompanyDocument,
    Document,
    DocumentPassage,
    Domain,
    ResearchInstrument,
    ResearchUniverse,
    ResearchUniverseRow,
    SecuritiesFiling,
    Source,
)

_HUMAN_FEED_HIDDEN_SOURCE_NAME_TOKENS = ("opendart",)
_RESTRICTED_FINANCIAL_DOCUMENT_KINDS = (
    "annual_report_text",
    "earnings_transcript",
    "sec_filing",
    "securities_filing",
    "transcript",
)
_RESTRICTED_COMPANY_DOCUMENT_TYPES = (
    "annual_report",
    "annual_report_text",
    "earnings_release",
    "earnings_transcript",
    "filing",
    "financial_report",
    "transcript",
)
_RESTRICTED_SOURCE_NAME_TOKENS = (
    "annual_report",
    "annual_reports",
    "earnings_transcript",
    "financial_report",
    "financial_reports",
    "filing",
    "filings",
    "transcript",
)

_POSTGRES_DOCUMENT_SEARCH_SQL = """
to_tsvector(
    'simple',
    coalesce(documents.title, '') || ' ' || coalesce(documents.normalized_text, '') || ' ' || coalesce(documents.translation_en, '')
) @@ websearch_to_tsquery('simple', :document_search_query)
"""
_POSTGRES_DOCUMENT_RANK_SQL = """
ts_rank_cd(
    to_tsvector(
        'simple',
        coalesce(documents.title, '') || ' ' || coalesce(documents.normalized_text, '') || ' ' || coalesce(documents.translation_en, '')
    ),
    websearch_to_tsquery('simple', :document_search_query)
)
"""
_POSTGRES_VECTOR_DOCUMENT_SEARCH_SQL = (
    coalesced_document_vector_sql("documents.") + " @@ websearch_to_tsquery('simple', :document_search_query)"
)
_POSTGRES_VECTOR_DOCUMENT_RANK_SQL = (
    "ts_rank_cd(" + coalesced_document_vector_sql("documents.")
    + ", websearch_to_tsquery('simple', :document_search_query))"
)

_DOCUMENT_QUERY_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./:+-]*")
_DOCUMENT_QUERY_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "for",
    "from",
    "in",
    "is",
    "of",
    "on",
    "or",
    "the",
    "to",
    "what",
    "with",
}


def _document_query_terms(query: str) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for raw in _DOCUMENT_QUERY_TOKEN_RE.findall(str(query or "")):
        term = raw.casefold().strip("._/+-:")
        if not term or term in _DOCUMENT_QUERY_STOPWORDS or (len(term) < 2 and not term.isdigit()):
            continue
        if term not in seen:
            seen.add(term)
            terms.append(term)
    return sorted(terms, key=lambda value: (-len(value), value))


def document_match_excerpt(text: object, query: str, *, limit: int = 420) -> dict[str, Any]:
    """Return a bounded excerpt and exact offsets in the supplied text representation."""
    body = str(text or "")
    limit_v = max(160, min(int(limit or 420), 1600))
    terms = _document_query_terms(query)
    lowered = body.casefold()
    candidate_positions: set[int] = set()
    for term in terms:
        start = 0
        for _ in range(80):
            pos = lowered.find(term, start)
            if pos < 0:
                break
            candidate_positions.add(pos)
            start = pos + max(1, len(term))

    if not candidate_positions:
        excerpt = body[:limit_v].strip()
        return {
            "match_excerpt": excerpt + ("…" if len(body) > len(excerpt) else ""),
            "match_found": False,
            "match_score": 0.0,
            "match_start": None,
            "match_end": None,
            "excerpt_start": 0,
            "excerpt_end": len(excerpt),
        }

    best: tuple[float, int, int] | None = None
    query_phrase = re.sub(r"\s+", " ", str(query or "")).casefold().strip()
    for pos in candidate_positions:
        start = max(0, pos - limit_v // 3)
        end = min(len(body), start + limit_v)
        start = max(0, end - limit_v)
        window = lowered[start:end]
        normalized_window = re.sub(r"\s+", " ", window)
        matched = [term for term in terms if term in window]
        frequency = sum(min(window.count(term), 6) for term in matched)
        score = float(len(matched) * 10 + frequency)
        if query_phrase and query_phrase in normalized_window:
            score += 30.0
        score += min(8.0, sum(len(term) for term in matched) / 12.0)
        candidate = (score, -start, pos)
        if best is None or candidate > best:
            best = candidate

    assert best is not None
    anchor = best[2]
    start = max(0, anchor - limit_v // 3)
    end = min(len(body), start + limit_v)
    start = max(0, end - limit_v)
    if start > 0:
        boundary = max(body.rfind(". ", max(0, start - 100), start + 1), body.rfind("; ", max(0, start - 100), start + 1))
        if boundary >= 0:
            start = boundary + 2
    if end < len(body):
        boundary = body.find(". ", end, min(len(body), end + 100))
        if boundary >= 0:
            end = boundary + 1
    excerpt = body[start:end].strip()
    leading_trim = len(body[start:end]) - len(body[start:end].lstrip())
    trailing_trim = len(body[start:end]) - len(body[start:end].rstrip())
    excerpt_start = start + leading_trim
    excerpt_end = end - trailing_trim
    spans: list[tuple[int, int]] = []
    for term in terms:
        term_start = excerpt_start
        while term_start < excerpt_end:
            pos = lowered.find(term, term_start, excerpt_end)
            if pos < 0:
                break
            spans.append((pos, pos + len(term)))
            term_start = pos + max(1, len(term))
    match_start = min((span[0] for span in spans), default=anchor)
    match_end = max((span[1] for span in spans), default=min(len(body), anchor + 1))
    return {
        "match_excerpt": ("…" if start > 0 else "") + excerpt + ("…" if end < len(body) else ""),
        "match_found": True,
        "match_score": round(float(best[0]), 4),
        "match_start": match_start,
        "match_end": match_end,
        "excerpt_start": excerpt_start,
        "excerpt_end": excerpt_end,
    }


def document_text_version(text: object) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def document_match_projection(
    session: Session,
    document_ids: list[int],
    *,
    query: str,
    limit: int = 420,
) -> dict[int, dict[str, Any]]:
    """Project query-matched excerpts without materializing Document ORM rows."""
    ids = sorted({int(doc_id) for doc_id in document_ids if int(doc_id or 0) > 0})
    if not ids:
        return {}
    text_expr = func.coalesce(Document.translation_en, Document.normalized_text, "")
    terms = _document_query_terms(query)
    out: dict[int, dict[str, Any]] = {}
    # Stream one text representation at a time. A search page may touch dozens
    # of large technical PDFs; `.all()` retained every full body concurrently
    # merely to return short excerpts.
    rows = (
        session.query(Document.id, Document.title, text_expr)
        .filter(Document.id.in_(ids))
        .execution_options(stream_results=True)
        .yield_per(1)
    )
    for doc_id, title, text in rows:
        match = document_match_excerpt(text, query, limit=limit)
        text_version = document_text_version(text)
        match["text_version"] = text_version
        match["text_length_chars"] = len(str(text or ""))
        if match.get("match_found"):
            match["match_id"] = (
                f"doc:{int(doc_id)}:{text_version}:{int(match['match_start'])}:{int(match['match_end'])}"
            )
        else:
            match["match_id"] = None
        title_surface = str(title or "").casefold()
        title_matches = sum(1 for term in terms if term in title_surface)
        match["relevance_score"] = round(float(match["match_score"]) + title_matches * 15.0, 4)
        out[int(doc_id)] = match

    located = [
        (doc_id, match)
        for doc_id, match in out.items()
        if match.get("match_found") and match.get("match_start") is not None and match.get("match_end") is not None
    ]
    if located:
        overlap = or_(
            *[
                and_(
                    DocumentPassage.document_id == int(doc_id),
                    DocumentPassage.content_hash == str(match["text_version"]),
                    DocumentPassage.char_end > int(match["match_start"]),
                    DocumentPassage.char_start < int(match["match_end"]),
                )
                for doc_id, match in located
            ]
        )
        passage_rows = session.query(
            DocumentPassage.document_id,
            DocumentPassage.page_start,
            DocumentPassage.page_end,
        ).filter(overlap).all()
        for doc_id, page_start, page_end in passage_rows:
            match = out.get(int(doc_id))
            if match is None:
                continue
            starts = [value for value in (match.get("page_start"), page_start) if value is not None]
            ends = [value for value in (match.get("page_end"), page_end) if value is not None]
            match["page_start"] = min(int(value) for value in starts) if starts else None
            match["page_end"] = max(int(value) for value in ends) if ends else None
    return out


def list_sources(session: Session) -> list[Source]:
    sources = session.query(Source).order_by(Source.name.asc()).all()
    return [s for s in sources if not (s.config or {}).get("deleted")]


def _days_ago(days: int) -> datetime:
    return utcnow() - timedelta(days=days)


def search_document_ids(session: Session, query: str, *, limit: int = 5000) -> list[int]:
    q = query.strip()
    if not q:
        return []
    try:
        dialect_name = str(session.get_bind().dialect.name or "").lower()
    except Exception:
        dialect_name = ""
    if dialect_name == "sqlite":
        try:
            rows = session.execute(
                sql_text(
                    "SELECT rowid FROM document_fts WHERE document_fts MATCH :q LIMIT :limit"
                ),
                {"q": q, "limit": int(limit)},
            ).fetchall()
            return [int(r[0]) for r in rows]
        except Exception:
            return []
    if dialect_name.startswith("postgres"):
        try:
            search_sql = _POSTGRES_VECTOR_DOCUMENT_SEARCH_SQL if document_search_ready(session) else _POSTGRES_DOCUMENT_SEARCH_SQL
            rows = session.execute(
                sql_text(
                    f"""
                    SELECT id
                    FROM documents
                    WHERE {search_sql}
                    LIMIT :limit
                    """
                ),
                {"document_search_query": q, "limit": int(limit)},
            ).fetchall()
            return [int(r[0]) for r in rows]
        except Exception:
            return []
    return []


def _active_universe_company_ids(session: Session):
    return (
        session.query(ResearchInstrument.company_id)
        .join(ResearchUniverseRow, ResearchUniverseRow.instrument_id == ResearchInstrument.id)
        .join(ResearchUniverse, ResearchUniverse.id == ResearchUniverseRow.universe_id)
        .filter(ResearchUniverse.status == "active")
        .filter(ResearchUniverseRow.archived_at.is_(None))
        .filter(ResearchInstrument.company_id.isnot(None))
        .distinct()
    )


def _active_universe_issuer_ids(session: Session):
    return (
        session.query(ResearchInstrument.issuer_id)
        .join(ResearchUniverseRow, ResearchUniverseRow.instrument_id == ResearchInstrument.id)
        .join(ResearchUniverse, ResearchUniverse.id == ResearchUniverseRow.universe_id)
        .filter(ResearchUniverse.status == "active")
        .filter(ResearchUniverseRow.archived_at.is_(None))
        .filter(ResearchInstrument.issuer_id.isnot(None))
        .distinct()
    )


def _periodic_sec_filing_filter():
    title = func.upper(func.coalesce(Document.title, ""))
    return or_(
        title.like("10-Q%"),
        title.like("10-K%"),
        title.like("FORM 10-Q%"),
        title.like("FORM 10-K%"),
        title.like("% - 10-Q%"),
        title.like("% - 10-K%"),
        title.like("%: 10-Q%"),
        title.like("%: 10-K%"),
    )


def is_human_feed_hidden_source_name(name: str | None) -> bool:
    source_name = str(name or "").strip().lower()
    if not source_name:
        return False
    return any(token in source_name for token in _HUMAN_FEED_HIDDEN_SOURCE_NAME_TOKENS)


def human_feed_hidden_source_filter(session: Session):
    hidden_source_ids = session.query(Source.id).filter(
        or_(
            *[
                func.lower(func.coalesce(Source.name, "")).like(f"%{token}%")
                for token in _HUMAN_FEED_HIDDEN_SOURCE_NAME_TOKENS
            ]
        )
    )
    return Document.source_id.in_(hidden_source_ids)


def active_universe_document_filter(session: Session):
    """
    True when a document is linked to an active universe company or issuer.

    Official filings, financial reports, earnings releases, and transcripts are
    retained in storage even when the source rail sees off-universe rows. They
    should only surface to the analyst feed or local-agent intake once the row
    maps to a tracked universe company/issuer.
    """
    company_doc_ids = session.query(CompanyDocument.document_id).filter(
        CompanyDocument.document_id.isnot(None),
        CompanyDocument.company_id.in_(_active_universe_company_ids(session)),
    )
    securities_filing_ids = session.query(SecuritiesFiling.document_id).filter(
        SecuritiesFiling.document_id.isnot(None),
        SecuritiesFiling.issuer_id.in_(_active_universe_issuer_ids(session)),
    )
    return or_(Document.id.in_(company_doc_ids), Document.id.in_(securities_filing_ids))


def active_universe_filing_filter(session: Session):
    return active_universe_document_filter(session)


def filing_or_transcript_document_filter(session: Session):
    """True for document rows that are filing/report/release/transcript rails."""
    kind = func.lower(func.coalesce(Document.kind, ""))
    company_doc_type = func.lower(func.coalesce(CompanyDocument.doc_type, ""))
    source_name = func.lower(func.coalesce(Source.name, ""))
    source_category = func.lower(func.coalesce(Source.category, ""))
    restricted_source_ids = session.query(Source.id).filter(
        or_(
            source_category.like("%filing%"),
            *[source_name.like(f"%{token}%") for token in _RESTRICTED_SOURCE_NAME_TOKENS],
        )
    )
    # Uncorrelated membership lets PostgreSQL build each visibility set once.
    # Correlated EXISTS inflated this feed plan's cost enough to trigger ~1s
    # of JIT compilation for a 20-row page. Exclude NULL IDs because these sets
    # are also negated, where NOT IN would otherwise hide unrelated documents.
    restricted_company_doc_ids = session.query(CompanyDocument.document_id).filter(
        CompanyDocument.document_id.isnot(None),
        company_doc_type.in_(_RESTRICTED_COMPANY_DOCUMENT_TYPES),
    )
    securities_filing_ids = session.query(SecuritiesFiling.document_id).filter(
        SecuritiesFiling.document_id.isnot(None),
    )
    return or_(
        kind.in_(_RESTRICTED_FINANCIAL_DOCUMENT_KINDS),
        _periodic_sec_filing_filter(),
        Document.source_id.in_(restricted_source_ids),
        Document.id.in_(restricted_company_doc_ids),
        Document.id.in_(securities_filing_ids),
    )


def agent_intake_visible_filter(session: Session):
    """Filter expression for documents allowed into automatic local-agent intake."""
    restricted = filing_or_transcript_document_filter(session)
    return or_(~restricted, active_universe_document_filter(session))


def transcript_document_filter(session: Session):
    """True for transcript documents and transcript source rails."""
    kind = func.lower(func.coalesce(Document.kind, ""))
    company_doc_type = func.lower(func.coalesce(CompanyDocument.doc_type, ""))
    source_name = func.lower(func.coalesce(Source.name, ""))
    transcript_source_ids = session.query(Source.id).filter(source_name.like("%transcript%"))
    transcript_company_doc_ids = session.query(CompanyDocument.document_id).filter(
        CompanyDocument.document_id.isnot(None),
        company_doc_type.in_(("earnings_transcript", "transcript")),
    )
    return or_(
        kind.in_(("earnings_transcript", "transcript")),
        Document.source_id.in_(transcript_source_ids),
        Document.id.in_(transcript_company_doc_ids),
    )


def human_feed_financial_document_visible_filter(session: Session):
    """Human feed policy for financial-document rails: only in-universe transcripts."""
    restricted = filing_or_transcript_document_filter(session)
    return or_(
        ~restricted,
        and_(transcript_document_filter(session), active_universe_document_filter(session)),
    )


def human_feed_visible_filter(session: Session):
    """Filter expression for documents allowed on the human triage feed."""
    return and_(
        ~human_feed_hidden_source_filter(session),
        human_feed_financial_document_visible_filter(session),
    )


def _document_effective_ts(now: datetime):
    return case(
        (
            (Document.published_at.isnot(None)) & (Document.published_at > now),
            func.coalesce(Document.ingested_at, now),
        ),
        else_=func.coalesce(Document.published_at, Document.ingested_at),
    )


def _document_effective_datetime(doc: Document, now: datetime) -> datetime:
    published = doc.published_at
    if published is not None and published > now:
        return doc.ingested_at or now
    return published or doc.ingested_at


def list_documents(
    session: Session,
    *,
    query: str | None = None,
    days: int | None = 10,
    triage_state: str | None = None,
    saved: bool | None = None,
    source_id: int | None = None,
    domain: str | None = None,
    language: str | None = None,
    include_attachments: bool = False,
    include_domains: bool = True,
    defer_text: bool = False,
    exclude_kinds: list[str] | None = None,
    human_feed_visible_only: bool = False,
    feed_projection: bool = False,
    order_by_relevance: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> list[Document]:
    opts = [joinedload(Document.source).load_only(Source.id, Source.name, Source.category)]
    if feed_projection:
        opts.append(
            load_only(
                Document.id,
                Document.kind,
                Document.source_id,
                Document.canonical_url,
                Document.url,
                Document.title,
                Document.published_at,
                Document.ingested_at,
                Document.language,
                Document.triage_state,
                Document.saved,
                Document.extra,
            )
        )
    else:
        opts.append(defer(Document.raw_text))
    if include_domains:
        opts.append(selectinload(Document.domains).load_only(Domain.id, Domain.slug, Domain.name))
    if include_attachments:
        attachment_load = selectinload(Document.attachments)
        if feed_projection:
            attachment_load = attachment_load.load_only(
                Attachment.id,
                Attachment.document_id,
                Attachment.kind,
                Attachment.original_url,
                Attachment.content_type,
                Attachment.object_path,
            )
        opts.append(attachment_load)
    if defer_text and not feed_projection:
        opts.extend([defer(Document.normalized_text), defer(Document.translation_en)])
    now = utcnow()
    effective_ts = _document_effective_ts(now)
    try:
        dialect_name = str(session.get_bind().dialect.name or "").lower()
    except Exception:
        dialect_name = ""
    vector_ready = bool(query and dialect_name.startswith("postgres") and document_search_ready(session))

    def build_base_query(*extra_filters: object, with_options: bool = True):
        q = session.query(Document)
        if with_options:
            q = q.options(*opts)

        if days is not None:
            since = _days_ago(days)
            q = q.filter(effective_ts >= since)

        if triage_state and triage_state.lower() not in {"all", "any"}:
            q = q.filter(Document.triage_state == triage_state)

        if saved is not None:
            q = q.filter(Document.saved.is_(bool(saved)))

        if source_id is not None:
            q = q.filter(Document.source_id == source_id)

        if domain:
            q = q.join(Document.domains).filter(Domain.slug == domain)

        if language:
            q = q.filter(Document.language == language)

        if exclude_kinds:
            q = q.filter(~Document.kind.in_([str(kind) for kind in exclude_kinds if str(kind).strip()]))

        for extra_filter in extra_filters:
            q = q.filter(extra_filter)

        if query:
            query_text = query.strip()
            if dialect_name.startswith("postgres"):
                search_sql = _POSTGRES_VECTOR_DOCUMENT_SEARCH_SQL if vector_ready else _POSTGRES_DOCUMENT_SEARCH_SQL
                q = q.filter(sql_text(search_sql)).params(document_search_query=query_text)
            else:
                ids = search_document_ids(session, query_text, limit=5000)
                if ids:
                    q = q.filter(Document.id.in_(ids))
                else:
                    # Fallback: naive LIKE
                    like = f"%{query_text}%"
                    q = q.filter(
                        (Document.title.ilike(like))
                        | (Document.normalized_text.ilike(like))
                        | (Document.translation_en.ilike(like))
                    )

        return q

    def order_query(q):
        if query and order_by_relevance and dialect_name.startswith("postgres"):
            return q.order_by(
                sql_text(f"{_POSTGRES_VECTOR_DOCUMENT_RANK_SQL if vector_ready else _POSTGRES_DOCUMENT_RANK_SQL} DESC"),
                effective_ts.desc(),
                Document.id.desc(),
            ).params(document_search_query=query.strip())
        return q.order_by(effective_ts.desc(), Document.id.desc())

    if human_feed_visible_only:
        page_window = max(1, int(offset) + int(limit))
        hidden_source = human_feed_hidden_source_filter(session)
        restricted = filing_or_transcript_document_filter(session)
        active_universe_transcript = and_(transcript_document_filter(session), active_universe_document_filter(session))
        effective_label = effective_ts.label("effective_ts")

        def candidate_rows(*extra_filters: object) -> list[tuple[int, str | None, datetime]]:
            rows = (
                order_query(build_base_query(*extra_filters, with_options=False))
                .with_entities(Document.id, Document.title, effective_label)
                .limit(page_window)
                .all()
            )
            return [(int(row[0]), row[1], row[2]) for row in rows if row[0] is not None and row[2] is not None]

        candidates_by_id: dict[int, tuple[int, str | None, datetime]] = {
            row[0]: row for row in candidate_rows(~hidden_source, ~restricted)
        }
        for row in candidate_rows(~hidden_source, restricted, active_universe_transcript):
            candidates_by_id[row[0]] = row

        candidates = sorted(
            candidates_by_id.values(),
            key=lambda row: (row[2], row[0]),
            reverse=True,
        )
        selected_ids = [row[0] for row in candidates[int(offset) : int(offset) + int(limit)]]
        if not selected_ids:
            return []
        docs_by_id = {
            int(doc.id): doc
            for doc in session.query(Document)
            .options(*opts)
            .filter(Document.id.in_(selected_ids))
            .all()
        }
        return [docs_by_id[doc_id] for doc_id in selected_ids if doc_id in docs_by_id]

    q = order_query(build_base_query())
    q = q.limit(int(limit)).offset(int(offset))
    return q.all()


def document_text_projection(session: Session, document_ids: list[int], *, limit: int = 421) -> dict[int, str]:
    ids = sorted({int(doc_id) for doc_id in document_ids if int(doc_id or 0) > 0})
    if not ids:
        return {}
    text_expr = func.coalesce(Document.translation_en, Document.normalized_text, "")
    rows = (
        session.query(Document.id, func.substr(text_expr, 1, max(1, int(limit))))
        .filter(Document.id.in_(ids))
        .all()
    )
    return {int(doc_id): str(text or "") for doc_id, text in rows}
